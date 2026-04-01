"""Tests for ExecutionReconciler (PR-E4, ADR-137).

Covers:
- Missing engine emits ACTIVATE
- Orphan active emits GRACEFUL_EXIT
- Orphan graceful-exit emits FINALIZE
- Failed engine in desired emits QUARANTINE
- Bounded actions per cycle
- No action when converged
- Determinism
- Report contains mismatches and actions
- State mismatch detected
- Stable sort order
"""

from __future__ import annotations

from grinder.execution_plane.reconciler import (
    CorrectiveActionKind,
    MismatchKind,
    ReconcileConfig,
    reconcile,
)
from grinder.execution_plane.registry import EngineRegistry, EngineState


def _setup(reg: EngineRegistry, symbol: str, state: EngineState) -> None:
    """Helper: register symbol and transition to target state."""
    reg.register(symbol, engine_ref="e")
    if state != EngineState.ACTIVATING:
        reg.transition(symbol, EngineState.ACTIVE)
        if state == EngineState.GRACEFUL_EXIT:
            reg.transition(symbol, EngineState.GRACEFUL_EXIT)
        elif state == EngineState.SHUTTING_DOWN:
            reg.transition(symbol, EngineState.GRACEFUL_EXIT)
            reg.transition(symbol, EngineState.SHUTTING_DOWN)
        elif state == EngineState.FAILED:
            reg.transition(symbol, EngineState.GRACEFUL_EXIT)
            reg.transition(symbol, EngineState.SHUTTING_DOWN)
            reg.transition(symbol, EngineState.FAILED)
        elif state == EngineState.STOPPED:
            reg.transition(symbol, EngineState.GRACEFUL_EXIT)
            reg.transition(symbol, EngineState.SHUTTING_DOWN)
            reg.transition(symbol, EngineState.STOPPED)


class TestMissingEngine:
    def test_missing_emits_activate(self) -> None:
        reg = EngineRegistry()
        report = reconcile({"BTC"}, reg)

        assert len(report.mismatches) == 1
        assert report.mismatches[0].kind == MismatchKind.MISSING
        assert len(report.actions) == 1
        assert report.actions[0].kind == CorrectiveActionKind.ACTIVATE_ENGINE
        assert report.actions[0].symbol == "BTC"

    def test_stopped_engine_emits_activate(self) -> None:
        reg = EngineRegistry()
        _setup(reg, "BTC", EngineState.STOPPED)

        report = reconcile({"BTC"}, reg)
        activate = [a for a in report.actions if a.kind == CorrectiveActionKind.ACTIVATE_ENGINE]
        assert len(activate) == 1


class TestOrphanEngine:
    def test_orphan_active_emits_graceful_exit(self) -> None:
        reg = EngineRegistry()
        _setup(reg, "BTC", EngineState.ACTIVE)

        report = reconcile(set(), reg)

        assert len(report.mismatches) == 1
        assert report.mismatches[0].kind == MismatchKind.ORPHAN
        assert len(report.actions) == 1
        assert report.actions[0].kind == CorrectiveActionKind.REQUEST_GRACEFUL_EXIT

    def test_orphan_graceful_exit_emits_finalize(self) -> None:
        reg = EngineRegistry()
        _setup(reg, "BTC", EngineState.GRACEFUL_EXIT)

        report = reconcile(set(), reg)

        finalize = [
            a for a in report.actions if a.kind == CorrectiveActionKind.FINALIZE_DEACTIVATION
        ]
        assert len(finalize) == 1


class TestFailedEngine:
    def test_failed_desired_emits_quarantine(self) -> None:
        reg = EngineRegistry()
        _setup(reg, "BTC", EngineState.FAILED)

        report = reconcile({"BTC"}, reg)

        quarantine = [a for a in report.actions if a.kind == CorrectiveActionKind.QUARANTINE_ENGINE]
        assert len(quarantine) == 1
        assert quarantine[0].symbol == "BTC"


class TestBounded:
    def test_actions_capped(self) -> None:
        reg = EngineRegistry()
        config = ReconcileConfig(max_actions_per_cycle=2)

        report = reconcile({"A", "B", "C", "D"}, reg, config)

        assert len(report.actions) == 2
        assert report.truncated is True

    def test_not_truncated_when_within_budget(self) -> None:
        reg = EngineRegistry()
        config = ReconcileConfig(max_actions_per_cycle=10)

        report = reconcile({"A"}, reg, config)
        assert report.truncated is False


class TestConverged:
    def test_no_action_when_aligned(self) -> None:
        reg = EngineRegistry()
        _setup(reg, "BTC", EngineState.ACTIVE)

        report = reconcile({"BTC"}, reg)

        assert report.mismatches == []
        assert report.actions == []
        assert report.truncated is False


class TestDeterminism:
    def test_same_inputs(self) -> None:
        def run() -> list[str]:
            reg = EngineRegistry()
            _setup(reg, "BTC", EngineState.ACTIVE)
            report = reconcile({"ETH"}, reg)
            return [a.symbol for a in report.actions]

        assert run() == run()


class TestReportStructure:
    def test_report_has_mismatches_and_actions(self) -> None:
        reg = EngineRegistry()
        _setup(reg, "ORPHAN", EngineState.ACTIVE)

        report = reconcile({"MISSING"}, reg)

        assert len(report.mismatches) == 2  # 1 orphan + 1 missing
        assert len(report.actions) == 2  # graceful_exit + activate


class TestStateMismatch:
    def test_shutting_down_while_desired(self) -> None:
        reg = EngineRegistry()
        _setup(reg, "BTC", EngineState.SHUTTING_DOWN)

        report = reconcile({"BTC"}, reg)

        state_mismatches = [m for m in report.mismatches if m.kind == MismatchKind.STATE_MISMATCH]
        assert len(state_mismatches) == 1
        assert state_mismatches[0].actual_state == EngineState.SHUTTING_DOWN


class TestStableSort:
    def test_actions_sorted_by_symbol(self) -> None:
        reg = EngineRegistry()
        config = ReconcileConfig(max_actions_per_cycle=10)

        report = reconcile({"C", "A", "B"}, reg, config)

        symbols = [a.symbol for a in report.actions]
        assert symbols == sorted(symbols)
