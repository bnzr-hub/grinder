"""Autonomous multi-symbol orchestration loop (PR-D2, ADR-132).

SSOT: docs/37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md Section 3.
      docs/38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md Phase D.

Continuous control-plane loop that wires:
  UniverseProvider → TuningCache filter → SymbolOrchestrator → cycle report

Each cycle:
1. Refresh universe (UniverseProvider.get_candidates)
2. Filter to tuned symbols via TuningCache
3. Reconcile via SymbolOrchestrator (which delegates to RotationController)
4. Produce deterministic CycleReport

Pure control-plane: produces action intents and reports. Does not execute
engine start/stop or exchange writes. Execution is the caller's responsibility.

Stop-the-line: orchestrator errors halt rotation. Universe/tuning errors
degrade gracefully (retain previous state).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.orchestration.symbol_orchestrator import SymbolOrchestrator
    from grinder.orchestration.universe_provider import UniverseProvider
    from grinder.rotation.controller import RotationAction, SymbolFacts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutonomousLoopConfig:
    """Configuration for the autonomous loop.

    Attributes:
        operator_universe_override: If non-empty, restricts discovery
            to this subset (operator --symbols constraint).
        stop_on_orchestrator_error: If True, halt loop on orchestrator
            exceptions. If False, retain previous cycle result.
    """

    operator_universe_override: frozenset[str] = frozenset()
    stop_on_orchestrator_error: bool = True


@dataclass(frozen=True)
class CycleReport:
    """Deterministic report for one orchestration cycle.

    Attributes:
        cycle: Monotonic cycle counter.
        discovered: Raw candidates from UniverseProvider.
        admitted: Symbols that passed TuningCache filter.
        skipped: Symbols skipped with reasons.
        actions: RotationAction intents from controller.
        error: If cycle failed, the error message. None on success.
    """

    cycle: int
    discovered: list[str] = field(default_factory=list)
    admitted: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    actions: list[RotationAction] = field(default_factory=list)
    error: str | None = None


@dataclass
class AutonomousLoop:
    """Continuous control-plane orchestration loop.

    Wires UniverseProvider → TuningCache filter → SymbolOrchestrator.
    Produces CycleReport per cycle. Does not execute engine actions.

    Deterministic: same (universe, cache, facts) -> same cycle report.
    """

    universe_provider: UniverseProvider
    orchestrator: SymbolOrchestrator
    config: AutonomousLoopConfig = field(default_factory=AutonomousLoopConfig)

    _cycle: int = 0
    _last_report: CycleReport | None = None
    _stopped: bool = False
    _stop_reason: str | None = None

    def run_cycle(
        self,
        facts: dict[str, SymbolFacts] | None = None,
    ) -> CycleReport:
        """Run one orchestration cycle.

        Steps:
        1. Check stop-the-line
        2. Refresh universe (fail-safe: retain previous on error)
        3. Apply operator override if configured
        4. Reconcile via orchestrator (tuning filter + rotation controller)
        5. Produce cycle report

        Args:
            facts: Runtime facts per symbol (is_flat, etc.).

        Returns:
            CycleReport with discovered/admitted/skipped/actions.
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

        # Step 1: Universe discovery (fail-safe via provider)
        try:
            discovered = self.universe_provider.get_candidates()
        except Exception as e:
            logger.warning("AUTONOMOUS_LOOP_UNIVERSE_FAILED cycle=%d error=%s", self._cycle, e)
            discovered = []

        # Step 2: Apply operator override
        if self.config.operator_universe_override:
            override = self.config.operator_universe_override
            discovered = [s for s in discovered if s in override]

        # Step 3: Orchestrator reconcile (tuning filter + rotation)
        try:
            decision = self.orchestrator.reconcile(discovered, facts)
        except Exception as e:
            error_msg = f"orchestrator_error: {e}"
            logger.error("AUTONOMOUS_LOOP_ORCHESTRATOR_FAILED cycle=%d error=%s", self._cycle, e)
            if self.config.stop_on_orchestrator_error:
                self._stopped = True
                self._stop_reason = error_msg
            report = CycleReport(
                cycle=self._cycle,
                discovered=discovered,
                error=error_msg,
            )
            self._last_report = report
            return report

        # Step 4: Build cycle report
        skipped_str = {s: r.value for s, r in decision.skipped.items()}

        report = CycleReport(
            cycle=self._cycle,
            discovered=discovered,
            admitted=decision.admitted,
            skipped=skipped_str,
            actions=list(decision.actions),
        )

        self._last_report = report

        logger.info(
            "AUTONOMOUS_LOOP_CYCLE_COMPLETED cycle=%d discovered=%d admitted=%d"
            " skipped=%d actions=%d",
            self._cycle,
            len(discovered),
            len(decision.admitted),
            len(decision.skipped),
            len(decision.actions),
        )

        return report

    @property
    def cycle(self) -> int:
        """Current cycle counter."""
        return self._cycle

    @property
    def stopped(self) -> bool:
        """Whether the loop has been stopped."""
        return self._stopped

    @property
    def stop_reason(self) -> str | None:
        """Reason for stop, if stopped."""
        return self._stop_reason

    @property
    def last_report(self) -> CycleReport | None:
        """Most recent cycle report."""
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
