"""Autonomous multi-symbol orchestration loop (PR-D2, ADR-132).

SSOT: docs/37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md Section 3.
      docs/38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md Phase D.

Continuous loop that wires all orchestration stages per doc 37 Section 3:
  1. Universe discovery (UniverseProvider)
  2. Hard prefilter (injectable)
  3. Tuning admission (TuningCache via SymbolOrchestrator)
  4. Scoring/ranking (injectable)
  5. Orchestrator reconcile (SymbolOrchestrator → RotationController)
  6. Cycle report

Control-plane produces action intents and cycle reports. Optional Stage 8
runs ExecutionCoordinator when execution_enabled + execution_acknowledged.
Default: shadow-only (no execution).

Continuous runner: run_forever() loops on configurable cadence with
injectable clock and sleep. stop() halts gracefully.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grinder.orchestration.symbol_orchestrator import SymbolOrchestrator
    from grinder.orchestration.universe_provider import UniverseProvider
    from grinder.rotation.controller import RotationAction, SymbolFacts

logger = logging.getLogger(__name__)

# Type aliases for injectable stage functions
PrefilterFn = "Callable[[list[str]], list[str]]"
"""Takes raw candidates, returns eligible symbols."""

RankerFn = "Callable[[list[str]], list[str]]"
"""Takes eligible symbols, returns ranked/ordered desired symbols."""


def _passthrough(symbols: list[str]) -> list[str]:
    """Default no-op stage: passes symbols through unchanged."""
    return symbols


@dataclass(frozen=True)
class AutonomousLoopConfig:
    """Configuration for the autonomous loop.

    Attributes:
        cycle_interval_s: Seconds between cycles in continuous mode.
        operator_universe_override: If non-empty, restricts discovery
            to this subset (operator --symbols constraint).
        stop_on_orchestrator_error: If True, halt loop on orchestrator
            exceptions. If False, retain previous cycle result.
    """

    cycle_interval_s: float = 60.0
    operator_universe_override: frozenset[str] = frozenset()
    stop_on_orchestrator_error: bool = True


@dataclass(frozen=True)
class CycleReport:
    """Deterministic report for one orchestration cycle.

    Attributes:
        cycle: Monotonic cycle counter.
        discovered: Raw candidates from UniverseProvider.
        eligible: Symbols that passed hard prefilter.
        tuned: Symbols admitted by TuningCache (subset of eligible).
        selected: Symbols after scoring/ranking (ordered desired set).
        admitted: Symbols admitted by orchestrator (may differ from selected).
        skipped: Symbols skipped with reasons.
        actions: RotationAction intents from controller.
        error: If cycle failed, the error message. None on success.
    """

    cycle: int
    discovered: list[str] = field(default_factory=list)
    eligible: list[str] = field(default_factory=list)
    tuned: list[str] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    admitted: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    actions: list[RotationAction] = field(default_factory=list)
    error: str | None = None
    execution_report: Any = None


@dataclass
class AutonomousLoop:
    """Continuous control-plane orchestration loop.

    Wires all stages per doc 37 Section 3:
    UniverseProvider → prefilter → TuningCache → ranker → SymbolOrchestrator

    Produces CycleReport per cycle. Optionally executes engine actions
    via ExecutionCoordinator when execution_enabled + execution_acknowledged.
    Default: shadow-only (no execution).

    Stages are injectable: prefilter_fn and ranker_fn allow callers to plug
    in real market-data prefilter and scoring/ranking without coupling this
    module to live data feeds.
    """

    universe_provider: UniverseProvider
    orchestrator: SymbolOrchestrator
    config: AutonomousLoopConfig = field(default_factory=AutonomousLoopConfig)
    prefilter_fn: Any = field(default=_passthrough)
    ranker_fn: Any = field(default=_passthrough)

    # Optional execution-plane integration (default: shadow-only)
    execution_coordinator: Any = None  # ExecutionCoordinator | None
    execution_registry: Any = None  # EngineRegistry | None
    execution_operator: Any = None  # OperatorControls | None
    execution_enabled: bool = False
    execution_acknowledged: bool = False

    _cycle: int = 0
    _last_report: CycleReport | None = None
    _stopped: bool = False
    _stop_reason: str | None = None

    def run_cycle(  # noqa: PLR0915
        self,
        facts: dict[str, SymbolFacts] | None = None,
    ) -> CycleReport:
        """Run one orchestration cycle through all stages.

        Steps per doc 37 Section 3 (order matters):
        1. Check stop-the-line
        2. Universe discovery (fail-safe via provider)
        3. Operator override
        4. Hard prefilter (injectable prefilter_fn)
        5. Tuning admission (TuningCache — only TUNED symbols proceed)
        6. Scoring/ranking among TUNED symbols only (injectable ranker_fn)
        7. Orchestrator reconcile (rotation controller)
        8. Cycle report

        Args:
            facts: Runtime facts per symbol (is_flat, etc.).

        Returns:
            CycleReport with all stage outputs.
        """
        self._cycle += 1

        if self._stopped:
            report = CycleReport(
                cycle=self._cycle,
                error=f"STOPPED: {self._stop_reason}",
            )
            logger.warning(
                "AUTONOMOUS_LOOP_STOPPED cycle=%d reason=%s",
                self._cycle,
                self._stop_reason,
            )
            return report

        # Stage 1: Universe discovery
        try:
            discovered = self.universe_provider.get_candidates()
        except Exception as e:
            logger.warning("AUTONOMOUS_LOOP_UNIVERSE_FAILED cycle=%d error=%s", self._cycle, e)
            discovered = []

        # Stage 2: Operator override
        if self.config.operator_universe_override:
            override = self.config.operator_universe_override
            discovered = [s for s in discovered if s in override]

        # Stage 3: Hard prefilter
        try:
            eligible = self.prefilter_fn(discovered)
        except Exception as e:
            logger.warning("AUTONOMOUS_LOOP_PREFILTER_FAILED cycle=%d error=%s", self._cycle, e)
            # Fail-closed for new activation but preserve active set
            eligible = list(self._last_report.admitted if self._last_report else [])

        # Stage 4: Tuning admission (before ranking — doc 37 invariant)
        # Only TUNED symbols enter ranking. Cache miss / NO_GO / expired = skipped.
        tuned: list[str] = []
        tuning_skipped: dict[str, str] = {}
        cache = self.orchestrator.cache
        for symbol in eligible:
            from grinder.tuning.solver import TuningStatus  # noqa: PLC0415

            result = cache.get(symbol)
            if result is None:
                tuning_skipped[symbol] = "CACHE_MISS"
                continue
            if result.status != TuningStatus.TUNED:
                tuning_skipped[symbol] = "NOT_TUNED"
                continue
            tuned.append(symbol)

        # Stage 5: Scoring/ranking among TUNED symbols only
        try:
            selected = self.ranker_fn(tuned)
        except Exception as e:
            logger.warning("AUTONOMOUS_LOOP_RANKING_FAILED cycle=%d error=%s", self._cycle, e)
            # Fail-closed for new activation but preserve active set
            selected = list(self._last_report.admitted if self._last_report else [])

        # Stage 6: Orchestrator reconcile (TuningCache re-check + rotation controller)
        # Orchestrator will re-validate tuning (defense in depth) and run controller.
        try:
            decision = self.orchestrator.reconcile(selected, facts)
        except Exception as e:
            error_msg = f"orchestrator_error: {e}"
            logger.error("AUTONOMOUS_LOOP_ORCHESTRATOR_FAILED cycle=%d error=%s", self._cycle, e)
            if self.config.stop_on_orchestrator_error:
                self._stopped = True
                self._stop_reason = error_msg
            report = CycleReport(
                cycle=self._cycle,
                discovered=discovered,
                eligible=eligible,
                tuned=tuned,
                selected=selected,
                error=error_msg,
            )
            self._last_report = report
            return report

        # Stage 7: Build cycle report
        # Merge tuning_skipped with orchestrator skipped (orchestrator may add more)
        all_skipped = {**tuning_skipped, **{s: r.value for s, r in decision.skipped.items()}}

        report = CycleReport(
            cycle=self._cycle,
            discovered=discovered,
            eligible=eligible,
            tuned=tuned,
            selected=selected,
            admitted=decision.admitted,
            skipped=all_skipped,
            actions=list(decision.actions),
        )

        # Stage 8: Optional execution-plane integration
        # Execution desired = controller-owned symbols (ACTIVE + GRACEFUL_EXIT)
        # plus symbols the controller explicitly chose to ACTIVATE this cycle.
        # This prevents ranking churn from orphaning live engines (STL_E3 deadlock)
        # while respecting controller's top_k / max_changes / min_hold boundaries.
        controller = self.orchestrator.controller
        controller_owned = set(controller.get_active_symbols()) | set(
            controller.get_graceful_exit_symbols()
        )
        from grinder.rotation.controller import RotationActionKind  # noqa: PLC0415

        activate_intent = {
            a.symbol for a in decision.actions if a.kind == RotationActionKind.ACTIVATE
        }
        execution_desired = list(controller_owned | activate_intent)
        exec_report = self._run_execution_phase(execution_desired, facts)
        if exec_report is not None:
            # Attach execution report to cycle report
            report = CycleReport(
                cycle=report.cycle,
                discovered=report.discovered,
                eligible=report.eligible,
                tuned=report.tuned,
                selected=report.selected,
                admitted=report.admitted,
                skipped=report.skipped,
                actions=report.actions,
                execution_report=exec_report,
            )

        self._last_report = report

        logger.info(
            "AUTONOMOUS_LOOP_CYCLE_COMPLETED cycle=%d discovered=%d eligible=%d"
            " tuned=%d selected=%d admitted=%d skipped=%d actions=%d execution=%s",
            self._cycle,
            len(discovered),
            len(eligible),
            len(tuned),
            len(selected),
            len(decision.admitted),
            len(all_skipped),
            len(decision.actions),
            "enabled" if exec_report and exec_report.execution_attempted else "shadow",
        )

        return report

    def _run_execution_phase(
        self,
        admitted: list[str],
        facts: dict[str, SymbolFacts] | None,  # noqa: ARG002
    ) -> Any:
        """Run optional execution-plane phase. Returns ExecutionReport or None."""
        if self.execution_coordinator is None or self.execution_registry is None:
            return None

        try:
            from grinder.execution_plane.reconciler import reconcile  # noqa: PLC0415
            from grinder.execution_plane.safety import evaluate_safety  # noqa: PLC0415

            desired = set(admitted)
            reconcile_report = reconcile(desired, self.execution_registry)

            paused = self.execution_operator.paused if self.execution_operator else False
            safety_result = evaluate_safety(reconcile_report, paused=paused)

            operator = self.execution_operator
            if operator is None:
                from grinder.execution_plane.operator import OperatorControls  # noqa: PLC0415

                operator = OperatorControls()

            return self.execution_coordinator.execute(
                reconcile_report,
                safety_result,
                operator,
                execution_enabled=self.execution_enabled,
                execution_acknowledged=self.execution_acknowledged,
            )
        except Exception as e:
            logger.warning("AUTONOMOUS_LOOP_EXECUTION_FAILED cycle=%d error=%s", self._cycle, e)
            return None

    def run_forever(
        self,
        facts_fn: Any = None,
        clock: Any = None,
        sleep_fn: Any = None,
        max_cycles: int | None = None,
    ) -> list[CycleReport]:
        """Run the loop continuously until stopped.

        Args:
            facts_fn: Callable returning runtime facts per cycle.
                If None, passes empty facts.
            clock: Injectable clock (default: time.monotonic).
            sleep_fn: Injectable sleep (default: time.sleep).
            max_cycles: If set, stop after this many cycles (for testing).

        Returns:
            List of all CycleReports produced.
        """
        _clock = clock or time.monotonic
        _sleep = sleep_fn or time.sleep
        reports: list[CycleReport] = []
        cycles_run = 0

        logger.info("AUTONOMOUS_LOOP_STARTED interval_s=%.1f", self.config.cycle_interval_s)

        while not self._stopped:
            cycle_start = _clock()

            facts = facts_fn() if facts_fn else None
            report = self.run_cycle(facts)
            reports.append(report)
            cycles_run += 1

            if max_cycles is not None and cycles_run >= max_cycles:
                break

            if self._stopped:
                break

            elapsed = _clock() - cycle_start
            remaining = self.config.cycle_interval_s - elapsed
            if remaining > 0:
                _sleep(remaining)

        logger.info("AUTONOMOUS_LOOP_FINISHED cycles=%d stopped=%s", cycles_run, self._stopped)

        return reports

    @property
    def cycle(self) -> int:
        return self._cycle

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    @property
    def last_report(self) -> CycleReport | None:
        return self._last_report

    def stop(self, reason: str) -> None:
        """Externally stop the loop."""
        self._stopped = True
        self._stop_reason = reason
        logger.warning("AUTONOMOUS_LOOP_STOP_THE_LINE reason=%s", reason)

    def reset(self) -> None:
        """Reset loop state (for testing)."""
        self._cycle = 0
        self._last_report = None
        self._stopped = False
        self._stop_reason = None
