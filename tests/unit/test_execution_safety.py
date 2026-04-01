"""Tests for execution safety + operator controls (PR-E5, ADR-138).

Covers:
- Pause blocks execution
- Resume re-enables
- Quarantine blocks symbol activation
- Release quarantine requires explicit action
- Force deactivate emits safe intent
- STL-E3 orphan trigger
- STL-E5 mismatch threshold trigger
- Audit log records all operator actions
- Determinism
- No triggered rules when clean
"""

from __future__ import annotations

from grinder.execution_plane.operator import (
    OperatorAction,
    OperatorControls,
)
from grinder.execution_plane.reconciler import (
    Mismatch,
    MismatchKind,
    ReconcileReport,
)
from grinder.execution_plane.registry import EngineState
from grinder.execution_plane.safety import (
    SafetyConfig,
    StopRule,
    evaluate_safety,
)


def _empty_report() -> ReconcileReport:
    return ReconcileReport()


def _report_with_orphan() -> ReconcileReport:
    return ReconcileReport(
        mismatches=[
            Mismatch(
                kind=MismatchKind.ORPHAN,
                symbol="BTC",
                actual_state=EngineState.ACTIVE,
            )
        ]
    )


def _report_with_many_mismatches(n: int) -> ReconcileReport:
    return ReconcileReport(
        mismatches=[
            Mismatch(
                kind=MismatchKind.MISSING,
                symbol=f"SYM{i}",
                actual_state=EngineState.ABSENT,
            )
            for i in range(n)
        ]
    )


# --- Safety evaluator tests ---


class TestSafetyClean:
    def test_no_rules_when_clean(self) -> None:
        result = evaluate_safety(_empty_report())
        assert result.execution_allowed is True
        assert result.triggered_rules == []


class TestPauseBlocksExecution:
    def test_paused(self) -> None:
        result = evaluate_safety(_empty_report(), paused=True)
        assert result.execution_allowed is False
        assert result.triggered_rules == []  # pause is not a rule trigger


class TestStlE3Orphan:
    def test_orphan_triggers(self) -> None:
        result = evaluate_safety(_report_with_orphan())
        assert StopRule.STL_E3_ORPHAN_ENGINE in result.triggered_rules
        assert result.execution_allowed is False


class TestStlE5MismatchThreshold:
    def test_threshold_triggers(self) -> None:
        config = SafetyConfig(mismatch_threshold=3)
        report = _report_with_many_mismatches(4)
        result = evaluate_safety(report, config=config)
        assert StopRule.STL_E5_MISMATCH_THRESHOLD in result.triggered_rules
        assert result.execution_allowed is False

    def test_below_threshold_ok(self) -> None:
        config = SafetyConfig(mismatch_threshold=10)
        report = _report_with_many_mismatches(3)
        result = evaluate_safety(report, config=config)
        assert StopRule.STL_E5_MISMATCH_THRESHOLD not in result.triggered_rules


class TestSafetyDeterminism:
    def test_same_inputs(self) -> None:
        report = _report_with_orphan()
        r1 = evaluate_safety(report)
        r2 = evaluate_safety(report)
        assert r1.execution_allowed == r2.execution_allowed
        assert r1.triggered_rules == r2.triggered_rules


# --- Operator controls tests ---


class TestOperatorPauseResume:
    def test_pause(self) -> None:
        ctrl = OperatorControls()
        ctrl.pause(operator="admin", reason="maintenance")
        assert ctrl.paused is True

    def test_resume(self) -> None:
        ctrl = OperatorControls()
        ctrl.pause()
        ctrl.resume(operator="admin", reason="done")
        assert ctrl.paused is False


class TestOperatorQuarantine:
    def test_quarantine_blocks_activation(self) -> None:
        ctrl = OperatorControls()
        ctrl.quarantine("BTC", operator="admin", reason="investigation")
        assert ctrl.is_activation_allowed("BTC") is False
        assert ctrl.is_activation_allowed("ETH") is True

    def test_release_requires_explicit_action(self) -> None:
        ctrl = OperatorControls()
        ctrl.quarantine("BTC")
        assert ctrl.is_activation_allowed("BTC") is False

        ctrl.release_quarantine("BTC", operator="admin", reason="resolved")
        assert ctrl.is_activation_allowed("BTC") is True

    def test_release_does_not_auto_activate(self) -> None:
        """Release only removes quarantine. Does not create activation intent."""
        ctrl = OperatorControls()
        ctrl.quarantine("BTC")
        ctrl.release_quarantine("BTC")
        assert "BTC" not in ctrl.quarantined_symbols
        # No activation pending — that requires a separate control-plane decision


class TestOperatorForceDeactivate:
    def test_force_deactivate_adds_pending(self) -> None:
        ctrl = OperatorControls()
        ctrl.force_deactivate("BTC", operator="admin", reason="emergency")
        assert "BTC" in ctrl.force_deactivate_pending

    def test_clear_after_processing(self) -> None:
        ctrl = OperatorControls()
        ctrl.force_deactivate("BTC")
        ctrl.clear_force_deactivate("BTC")
        assert "BTC" not in ctrl.force_deactivate_pending


class TestPauseBlocksActivation:
    def test_paused_blocks_all_symbols(self) -> None:
        ctrl = OperatorControls()
        ctrl.pause()
        assert ctrl.is_activation_allowed("BTC") is False
        assert ctrl.is_activation_allowed("ETH") is False


class TestAuditLog:
    def test_all_actions_logged(self) -> None:
        ctrl = OperatorControls()
        ctrl.pause(operator="op1", reason="r1")
        ctrl.resume(operator="op2", reason="r2")
        ctrl.quarantine("BTC", operator="op3", reason="r3")
        ctrl.release_quarantine("BTC", operator="op4", reason="r4")
        ctrl.force_deactivate("ETH", operator="op5", reason="r5")

        log = ctrl.audit_log
        assert len(log) == 5

        actions = [r.action for r in log]
        assert actions == [
            OperatorAction.PAUSE,
            OperatorAction.RESUME,
            OperatorAction.QUARANTINE,
            OperatorAction.RELEASE_QUARANTINE,
            OperatorAction.FORCE_DEACTIVATE,
        ]

        assert log[0].operator == "op1"
        assert log[2].symbol == "BTC"
        assert log[4].symbol == "ETH"
