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

Control-plane: produces action intents and cycle reports. Does not execute
engine start/stop or exchange writes. Execution is the caller's responsibility.

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


@dataclass
class AutonomousLoop:
    """Continuous control-plane orchestration loop.

    Wires all stages per doc 37 Section 3:
    UniverseProvider → prefilter → TuningCache → ranker → SymbolOrchestrator

    Produces CycleReport per cycle. Does not execute engine actions.

    Stages are injectable: prefilter_fn and ranker_fn allow callers to plug
    in real market-data prefilter and scoring/ranking without coupling this
    module to live data feeds.
    """

    universe_provider: UniverseProvider
    orchestrator: SymbolOrchestrator
    config: AutonomousLoopConfig = field(default_factory=AutonomousLoopConfig)
    prefilter_fn: Any = field(default=_passthrough)
    ranker_fn: Any = field(default=_passthrough)

    _cycle: int = 0
    _last_report: CycleReport | None = None
    _stopped: bool = False
    _stop_reason: str | None = None

    def run_cycle(
        self,
        facts: dict[str, SymbolFacts] | None = None,
    ) -> CycleReport:
        """Run one orchestration cycle through all stages.

        Steps per doc 37 Section 3:
        1. Check stop-the-line
        2. Universe discovery (fail-safe via provider)
        3. Operator override
        4. Hard prefilter (injectable prefilter_fn)
        5. Scoring/ranking (injectable ranker_fn)
        6. Orchestrator reconcile (TuningCache filter + rotation controller)
        7. Cycle report

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
            eligible = discovered  # fail-open: skip prefilter

        # Stage 4: Scoring/ranking
        try:
            selected = self.ranker_fn(eligible)
        except Exception as e:
            logger.warning("AUTONOMOUS_LOOP_RANKING_FAILED cycle=%d error=%s", self._cycle, e)
            selected = eligible  # fail-open: skip ranking

        # Stage 5: Orchestrator reconcile (TuningCache filter + rotation controller)
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
                selected=selected,
                error=error_msg,
            )
            self._last_report = report
            return report

        # Stage 6: Build cycle report
        skipped_str = {s: r.value for s, r in decision.skipped.items()}

        report = CycleReport(
            cycle=self._cycle,
            discovered=discovered,
            eligible=eligible,
            tuned=decision.admitted,
            selected=selected,
            admitted=decision.admitted,
            skipped=skipped_str,
            actions=list(decision.actions),
        )

        self._last_report = report

        logger.info(
            "AUTONOMOUS_LOOP_CYCLE_COMPLETED cycle=%d discovered=%d eligible=%d"
            " tuned=%d selected=%d admitted=%d skipped=%d actions=%d",
            self._cycle,
            len(discovered),
            len(eligible),
            len(decision.admitted),
            len(selected),
            len(decision.admitted),
            len(decision.skipped),
            len(decision.actions),
        )

        return report

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
