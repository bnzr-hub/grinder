"""Tests for LiveSymbolDegradationController — day-mode degradation ladder."""

from __future__ import annotations

from grinder.risk.day_risk_manager import DayRiskMode
from grinder.risk.live_symbol_degradation import (
    LiveSymbolDegradationController,
)
from grinder.runtime.autonomous_host import GracefulExitResult


class _MockGracefulExit:
    """Records graceful-exit calls for assertion."""

    def __init__(self, result: GracefulExitResult = GracefulExitResult.SUCCESS) -> None:
        self.calls: list[str] = []
        self._result = result

    def __call__(self, symbol: str) -> GracefulExitResult:
        self.calls.append(symbol)
        return self._result


class _MockDeactivate:
    """Records finalize-deactivation calls."""

    def __init__(self, ok: bool = True) -> None:
        self.calls: list[str] = []
        self._ok = ok

    def __call__(self, symbol: str) -> bool:
        self.calls.append(symbol)
        return self._ok


class TestGracefulExitTriggers:
    def test_stop_new_risk_graceful_exit(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, mock)
        assert newly == ["BTCUSDT"]
        assert "BTCUSDT" in mock.calls
        assert "BTCUSDT" in ctrl.degraded_symbols
        assert "BTCUSDT" not in ctrl.force_reduced_symbols

    def test_stop_for_day_graceful_exit(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"ETHUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        assert newly == ["ETHUSDT"]
        assert "ETHUSDT" in ctrl.degraded_symbols
        assert "ETHUSDT" not in ctrl.force_reduced_symbols

    def test_stop_new_risk_does_not_deactivate(self) -> None:
        """STOP_NEW_RISK should NOT call deactivate."""
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        da = _MockDeactivate()
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, ge, da)
        assert da.calls == []


class TestForceReduce:
    def test_force_reduce_graceful_exit_plus_deactivation(self) -> None:
        """FORCE_REDUCE triggers both graceful exit AND finalize deactivation."""
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        da = _MockDeactivate()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, da)
        assert newly == ["BTCUSDT"]
        assert "BTCUSDT" in ge.calls
        assert "BTCUSDT" in da.calls
        assert "BTCUSDT" in ctrl.degraded_symbols
        assert "BTCUSDT" in ctrl.force_reduced_symbols

    def test_force_reduce_multiple_symbols(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        da = _MockDeactivate()
        live = frozenset({"AAAUSDT", "BBBUSDT"})
        newly = ctrl.evaluate(live, DayRiskMode.FORCE_REDUCE, ge, da)
        assert sorted(newly) == ["AAAUSDT", "BBBUSDT"]
        assert sorted(ge.calls) == ["AAAUSDT", "BBBUSDT"]
        assert sorted(da.calls) == ["AAAUSDT", "BBBUSDT"]

    def test_force_reduce_no_deactivate_fn_skips(self) -> None:
        """Without deactivate_fn, force-reduce only does graceful exit."""
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, None)
        assert newly == []  # force-reduce not latched
        assert "BTCUSDT" in ctrl.degraded_symbols  # graceful exit happened
        assert "BTCUSDT" not in ctrl.force_reduced_symbols

    def test_force_reduce_deactivation_failed_retries(self) -> None:
        """Failed deactivation not latched — retries next cycle."""
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        da_fail = _MockDeactivate(ok=False)
        da_ok = _MockDeactivate(ok=True)

        newly1 = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, da_fail)
        assert newly1 == []
        assert "BTCUSDT" not in ctrl.force_reduced_symbols

        newly2 = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, da_ok)
        assert newly2 == ["BTCUSDT"]
        assert "BTCUSDT" in ctrl.force_reduced_symbols


class TestNoDegradation:
    def test_normal_mode(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.NORMAL, mock)
        assert newly == []

    def test_defensive_mode(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.DEFENSIVE, mock)
        assert newly == []

    def test_empty_live(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset(), DayRiskMode.STOP_FOR_DAY, mock)
        assert newly == []


class TestIdempotency:
    def test_graceful_exit_no_repeat(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, mock)
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, mock)
        assert mock.calls == ["BTCUSDT"]

    def test_force_reduce_no_repeat(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        da = _MockDeactivate()
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, da)
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, da)
        assert ge.calls == ["BTCUSDT"]
        assert da.calls == ["BTCUSDT"]

    def test_escalation_from_graceful_to_force(self) -> None:
        """Symbol already graceful-exit → FORCE_REDUCE escalates to deactivation."""
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        da = _MockDeactivate()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, ge)
        assert "BTCUSDT" in ctrl.degraded_symbols
        assert "BTCUSDT" not in ctrl.force_reduced_symbols

        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, da)
        assert newly == ["BTCUSDT"]
        assert ge.calls == ["BTCUSDT"]  # no second graceful exit
        assert da.calls == ["BTCUSDT"]  # but deactivation called
        assert "BTCUSDT" in ctrl.force_reduced_symbols


class TestPruning:
    def test_deactivated_symbol_pruned(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        da = _MockDeactivate()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, da)
        assert "BTCUSDT" in ctrl.force_reduced_symbols

        ctrl.evaluate(frozenset(), DayRiskMode.NORMAL, ge)
        assert "BTCUSDT" not in ctrl.degraded_symbols
        assert "BTCUSDT" not in ctrl.force_reduced_symbols

    def test_pruned_symbol_can_be_force_reduced_again(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        da = _MockDeactivate()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, da)
        ctrl.evaluate(frozenset(), DayRiskMode.NORMAL, ge)
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, da)
        assert newly == ["BTCUSDT"]
        assert da.calls == ["BTCUSDT", "BTCUSDT"]


class TestReset:
    def test_reset_clears_both(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        da = _MockDeactivate()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, da)
        ctrl.reset()
        assert ctrl.degraded_symbols == frozenset()
        assert ctrl.force_reduced_symbols == frozenset()


class TestRetryOnFailure:
    def test_graceful_exit_failed_retries(self) -> None:
        ctrl = LiveSymbolDegradationController()
        fail = _MockGracefulExit(result=GracefulExitResult.FAILED)
        ok = _MockGracefulExit(result=GracefulExitResult.SUCCESS)

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, fail)
        assert "BTCUSDT" not in ctrl.degraded_symbols

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, ok)
        assert "BTCUSDT" in ctrl.degraded_symbols

    def test_graceful_exit_exception_retries(self) -> None:
        ctrl = LiveSymbolDegradationController()

        def _raise(symbol: str) -> None:
            raise RuntimeError("host error")

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, _raise)
        assert "BTCUSDT" not in ctrl.degraded_symbols
