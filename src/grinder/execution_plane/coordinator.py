"""Execution coordinator: maps corrective actions to ceremonies (PR-E6, ADR-139).

SSOT: docs/39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md Section 3.4.
      docs/40_EXECUTION_PLANE_IMPLEMENTATION_PLAN.md Phase E6.

Accepts symbolic corrective actions from the reconciler, checks safety
and operator gates, and maps them to the appropriate ceremony calls.
All ceremony functions are injectable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from grinder.execution_plane.reconciler import (
    CorrectiveAction,
    CorrectiveActionKind,
    ReconcileReport,
)

if TYPE_CHECKING:
    from grinder.execution_plane.operator import OperatorControls
    from grinder.execution_plane.safety import SafetyResult

logger = logging.getLogger(__name__)


class ExecutionOutcome(Enum):
    """Outcome of one execution action."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED_SAFETY = "SKIPPED_SAFETY"
    SKIPPED_QUARANTINE = "SKIPPED_QUARANTINE"
    SKIPPED_PAUSE = "SKIPPED_PAUSE"
    SKIPPED_DISABLED = "SKIPPED_DISABLED"


@dataclass(frozen=True)
class ExecutionActionResult:
    """Result of attempting one corrective action."""

    action: CorrectiveAction
    outcome: ExecutionOutcome
    detail: str = ""


@dataclass(frozen=True)
class ExecutionReport:
    """Result of one execution cycle.

    Attributes:
        execution_enabled: Whether execution was enabled.
        execution_acknowledged: Whether operator ACK was present.
        execution_attempted: Whether any actions were actually executed.
        skipped_reason: Why execution was skipped (if skipped entirely).
        reconcile_report: The reconciliation report that drove this cycle.
        safety_result: Safety evaluation result.
        action_results: Per-action execution results.
    """

    execution_enabled: bool = False
    execution_acknowledged: bool = False
    execution_attempted: bool = False
    skipped_reason: str = ""
    reconcile_report: ReconcileReport | None = None
    safety_result: SafetyResult | None = None
    action_results: list[ExecutionActionResult] = field(default_factory=list)


# Injectable ceremony function types
ActivateFn = Any  # Callable[[str], bool]
GracefulExitFn = Any  # Callable[[str], bool]
DeactivateFn = Any  # Callable[[str], bool]


@dataclass
class ExecutionCoordinator:
    """Maps corrective actions to ceremony calls.

    All ceremonies are injectable. Does not own control-plane logic.
    """

    activate_fn: ActivateFn = None
    graceful_exit_fn: GracefulExitFn = None
    deactivate_fn: DeactivateFn = None

    def execute(
        self,
        reconcile_report: ReconcileReport,
        safety_result: SafetyResult,
        operator: OperatorControls,
        execution_enabled: bool = False,
        execution_acknowledged: bool = False,
    ) -> ExecutionReport:
        """Execute bounded corrective actions from reconciler.

        Checks enablement, ACK, safety, and operator gates before
        executing each action.

        Args:
            reconcile_report: Reconciliation report with corrective actions.
            safety_result: Safety evaluation result.
            operator: Operator control state.
            execution_enabled: Whether execution is enabled.
            execution_acknowledged: Whether operator has ACKed execution.

        Returns:
            ExecutionReport with per-action results.
        """
        # Gate 1: Execution disabled
        if not execution_enabled:
            logger.info("EXECUTION_MODE shadow_only")
            return ExecutionReport(
                execution_enabled=False,
                reconcile_report=reconcile_report,
                safety_result=safety_result,
                skipped_reason="execution_disabled",
            )

        # Gate 2: No operator ACK
        if not execution_acknowledged:
            logger.info("EXECUTION_MODE enabled ack=false")
            return ExecutionReport(
                execution_enabled=True,
                execution_acknowledged=False,
                reconcile_report=reconcile_report,
                safety_result=safety_result,
                skipped_reason="no_operator_ack",
            )

        # Gate 3: Safety blocks execution
        if not safety_result.execution_allowed:
            rules = [r.value for r in safety_result.triggered_rules]
            from grinder.observability.structured_events import (  # noqa: PLC0415
                format_safety_summary,
            )

            logger.info(
                format_safety_summary(
                    execution_allowed=False,
                    triggered_rules=rules,
                    quarantine_symbols=safety_result.quarantine_symbols,
                )
            )
            return ExecutionReport(
                execution_enabled=True,
                execution_acknowledged=True,
                reconcile_report=reconcile_report,
                safety_result=safety_result,
                skipped_reason=f"safety_block: {rules}",
            )

        # Gate 4: Operator pause
        if operator.paused:
            logger.info("EXECUTION_SKIPPED reason=OPERATOR_PAUSE")
            return ExecutionReport(
                execution_enabled=True,
                execution_acknowledged=True,
                reconcile_report=reconcile_report,
                safety_result=safety_result,
                skipped_reason="operator_pause",
            )

        # Execute each bounded action
        results: list[ExecutionActionResult] = []
        for action in reconcile_report.actions:
            result = self._execute_one(action, operator)
            results.append(result)

        attempted = any(r.outcome == ExecutionOutcome.SUCCESS for r in results)

        if results:
            logger.info(
                "EXECUTION_ACTIONS_APPLIED count=%d success=%d failed=%d skipped=%d",
                len(results),
                sum(1 for r in results if r.outcome == ExecutionOutcome.SUCCESS),
                sum(1 for r in results if r.outcome == ExecutionOutcome.FAILED),
                sum(1 for r in results if r.outcome.value.startswith("SKIPPED")),
            )

        return ExecutionReport(
            execution_enabled=True,
            execution_acknowledged=True,
            execution_attempted=attempted,
            reconcile_report=reconcile_report,
            safety_result=safety_result,
            action_results=results,
        )

    def _execute_one(
        self, action: CorrectiveAction, operator: OperatorControls
    ) -> ExecutionActionResult:
        """Execute one corrective action with per-symbol gates."""
        # Per-symbol quarantine check
        if not operator.is_activation_allowed(action.symbol):
            return ExecutionActionResult(
                action=action,
                outcome=ExecutionOutcome.SKIPPED_QUARANTINE,
                detail="symbol quarantined or paused",
            )

        # Map to ceremony
        fn = self._get_ceremony(action.kind)
        if fn is None:
            return ExecutionActionResult(
                action=action,
                outcome=ExecutionOutcome.SKIPPED_DISABLED,
                detail=f"no ceremony wired for {action.kind.value}",
            )

        try:
            success = fn(action.symbol)
        except Exception as e:
            logger.error(
                "EXECUTION_ACTION_FAILED symbol=%s action=%s error=%s",
                action.symbol,
                action.kind.value,
                e,
            )
            return ExecutionActionResult(
                action=action, outcome=ExecutionOutcome.FAILED, detail=str(e)
            )

        if success:
            return ExecutionActionResult(action=action, outcome=ExecutionOutcome.SUCCESS)
        return ExecutionActionResult(
            action=action, outcome=ExecutionOutcome.FAILED, detail="ceremony returned false"
        )

    def _get_ceremony(self, kind: CorrectiveActionKind) -> Any:
        """Map action kind to injectable ceremony function."""
        mapping = {
            CorrectiveActionKind.ACTIVATE_ENGINE: self.activate_fn,
            CorrectiveActionKind.REQUEST_GRACEFUL_EXIT: self.graceful_exit_fn,
            CorrectiveActionKind.FINALIZE_DEACTIVATION: self.deactivate_fn,
        }
        return mapping.get(kind)
