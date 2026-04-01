"""Tests for execution-plane integration (PR-E6, ADR-139).

Covers:
- Execution disabled = shadow only
- Execution enabled without ACK = shadow only
- Execution enabled + ACK = actions run
- Safety block prevents execution
- Operator pause prevents execution
- Quarantined symbol not activated
- Execution error reported fail-safe
- Coordinator maps actions to correct ceremonies
- Bounded actions respected
- Determinism in shadow mode
"""

from __future__ import annotations

from grinder.execution_plane.coordinator import (
    ExecutionCoordinator,
    ExecutionOutcome,
)
from grinder.execution_plane.operator import OperatorControls
from grinder.execution_plane.reconciler import (
    CorrectiveAction,
    CorrectiveActionKind,
    ReconcileReport,
)
from grinder.execution_plane.safety import SafetyResult, StopRule


def _report_with_activate(symbol: str = "BTC") -> ReconcileReport:
    return ReconcileReport(
        actions=[CorrectiveAction(kind=CorrectiveActionKind.ACTIVATE_ENGINE, symbol=symbol)]
    )


def _safe() -> SafetyResult:
    return SafetyResult(execution_allowed=True)


def _blocked() -> SafetyResult:
    return SafetyResult(execution_allowed=False, triggered_rules=[StopRule.STL_E3_ORPHAN_ENGINE])


def _ok_activate(_symbol: str) -> bool:
    return True


def _ok_graceful(_symbol: str) -> bool:
    return True


def _ok_deactivate(_symbol: str) -> bool:
    return True


class TestExecutionDisabled:
    def test_shadow_only(self) -> None:
        coord = ExecutionCoordinator(activate_fn=_ok_activate)
        op = OperatorControls()

        report = coord.execute(_report_with_activate(), _safe(), op, execution_enabled=False)

        assert report.execution_enabled is False
        assert report.execution_attempted is False
        assert report.skipped_reason == "execution_disabled"
        assert report.action_results == []


class TestExecutionNoAck:
    def test_shadow_without_ack(self) -> None:
        coord = ExecutionCoordinator(activate_fn=_ok_activate)
        op = OperatorControls()

        report = coord.execute(
            _report_with_activate(),
            _safe(),
            op,
            execution_enabled=True,
            execution_acknowledged=False,
        )

        assert report.execution_enabled is True
        assert report.execution_acknowledged is False
        assert report.execution_attempted is False
        assert "no_operator_ack" in report.skipped_reason


class TestExecutionEnabledAcked:
    def test_actions_run(self) -> None:
        activated: list[str] = []

        def capture_activate(symbol: str) -> bool:
            activated.append(symbol)
            return True

        coord = ExecutionCoordinator(activate_fn=capture_activate)
        op = OperatorControls()

        report = coord.execute(
            _report_with_activate("BTC"),
            _safe(),
            op,
            execution_enabled=True,
            execution_acknowledged=True,
        )

        assert report.execution_attempted is True
        assert len(report.action_results) == 1
        assert report.action_results[0].outcome == ExecutionOutcome.SUCCESS
        assert activated == ["BTC"]


class TestSafetyBlock:
    def test_safety_prevents_execution(self) -> None:
        coord = ExecutionCoordinator(activate_fn=_ok_activate)
        op = OperatorControls()

        report = coord.execute(
            _report_with_activate(),
            _blocked(),
            op,
            execution_enabled=True,
            execution_acknowledged=True,
        )

        assert report.execution_attempted is False
        assert "safety_block" in report.skipped_reason


class TestOperatorPause:
    def test_pause_prevents_execution(self) -> None:
        coord = ExecutionCoordinator(activate_fn=_ok_activate)
        op = OperatorControls()
        op.pause()

        report = coord.execute(
            _report_with_activate(),
            _safe(),
            op,
            execution_enabled=True,
            execution_acknowledged=True,
        )

        assert report.execution_attempted is False
        assert "operator_pause" in report.skipped_reason


class TestQuarantinedSymbol:
    def test_quarantined_not_activated(self) -> None:
        coord = ExecutionCoordinator(activate_fn=_ok_activate)
        op = OperatorControls()
        op.quarantine("BTC")

        report = coord.execute(
            _report_with_activate("BTC"),
            _safe(),
            op,
            execution_enabled=True,
            execution_acknowledged=True,
        )

        assert len(report.action_results) == 1
        assert report.action_results[0].outcome == ExecutionOutcome.SKIPPED_QUARANTINE


class TestExecutionError:
    def test_error_reported_fail_safe(self) -> None:
        def failing_activate(_symbol: str) -> bool:
            raise RuntimeError("engine build failed")

        coord = ExecutionCoordinator(activate_fn=failing_activate)
        op = OperatorControls()

        report = coord.execute(
            _report_with_activate(),
            _safe(),
            op,
            execution_enabled=True,
            execution_acknowledged=True,
        )

        assert len(report.action_results) == 1
        assert report.action_results[0].outcome == ExecutionOutcome.FAILED
        assert "engine build failed" in report.action_results[0].detail


class TestCoordinatorMapsActions:
    def test_graceful_exit_mapped(self) -> None:
        signaled: list[str] = []

        reconcile = ReconcileReport(
            actions=[
                CorrectiveAction(kind=CorrectiveActionKind.REQUEST_GRACEFUL_EXIT, symbol="ETH")
            ]
        )

        coord = ExecutionCoordinator(
            graceful_exit_fn=lambda s: signaled.append(s) or True,  # type: ignore[func-returns-value]
        )
        op = OperatorControls()

        report = coord.execute(
            reconcile, _safe(), op, execution_enabled=True, execution_acknowledged=True
        )

        assert signaled == ["ETH"]
        assert report.action_results[0].outcome == ExecutionOutcome.SUCCESS

    def test_deactivate_mapped(self) -> None:
        deactivated: list[str] = []

        reconcile = ReconcileReport(
            actions=[
                CorrectiveAction(kind=CorrectiveActionKind.FINALIZE_DEACTIVATION, symbol="SOL")
            ]
        )

        coord = ExecutionCoordinator(
            deactivate_fn=lambda s: deactivated.append(s) or True,  # type: ignore[func-returns-value]
        )
        op = OperatorControls()

        coord.execute(reconcile, _safe(), op, execution_enabled=True, execution_acknowledged=True)

        assert deactivated == ["SOL"]

    def test_unmapped_action_skipped(self) -> None:
        reconcile = ReconcileReport(
            actions=[CorrectiveAction(kind=CorrectiveActionKind.QUARANTINE_ENGINE, symbol="X")]
        )

        coord = ExecutionCoordinator()  # no ceremonies wired
        op = OperatorControls()

        report = coord.execute(
            reconcile, _safe(), op, execution_enabled=True, execution_acknowledged=True
        )

        assert report.action_results[0].outcome == ExecutionOutcome.SKIPPED_DISABLED


class TestBounded:
    def test_only_bounded_actions_reach_coordinator(self) -> None:
        """Reconciler already bounds actions. Coordinator processes what it receives."""
        activated: list[str] = []

        reconcile = ReconcileReport(
            actions=[
                CorrectiveAction(kind=CorrectiveActionKind.ACTIVATE_ENGINE, symbol="A"),
                CorrectiveAction(kind=CorrectiveActionKind.ACTIVATE_ENGINE, symbol="B"),
            ]
        )

        coord = ExecutionCoordinator(
            activate_fn=lambda s: activated.append(s) or True,  # type: ignore[func-returns-value]
        )
        op = OperatorControls()

        coord.execute(reconcile, _safe(), op, execution_enabled=True, execution_acknowledged=True)

        assert activated == ["A", "B"]


class TestDeterminism:
    def test_shadow_mode_deterministic(self) -> None:
        coord = ExecutionCoordinator()
        op = OperatorControls()

        r1 = coord.execute(_report_with_activate(), _safe(), op)
        r2 = coord.execute(_report_with_activate(), _safe(), op)

        assert r1.skipped_reason == r2.skipped_reason
        assert r1.execution_enabled == r2.execution_enabled
