"""Tests for LiveSymbolDegradationController — day-mode-driven graceful-exit."""

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


class TestDegradeTriggers:
    def test_stop_new_risk_degrades(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, mock)
        assert newly == ["BTCUSDT"]
        assert "BTCUSDT" in mock.calls

    def test_stop_for_day_degrades(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"ETHUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        assert newly == ["ETHUSDT"]

    def test_force_reduce_degrades(self) -> None:
        """FORCE_REDUCE → graceful-exit-only (staged unload deferred)."""
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"SOLUSDT"}), DayRiskMode.FORCE_REDUCE, mock)
        assert newly == ["SOLUSDT"]
        assert "SOLUSDT" in mock.calls

    def test_multiple_symbols_all_degraded(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        live = frozenset({"AAAUSDT", "BBBUSDT", "CCCUSDT"})
        newly = ctrl.evaluate(live, DayRiskMode.STOP_FOR_DAY, mock)
        assert sorted(newly) == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]


class TestNoDegradation:
    def test_normal_mode(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        assert ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.NORMAL, mock) == []
        assert mock.calls == []

    def test_defensive_mode(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        assert ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.DEFENSIVE, mock) == []

    def test_empty_live(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        assert ctrl.evaluate(frozenset(), DayRiskMode.STOP_FOR_DAY, mock) == []


class TestIdempotency:
    def test_no_repeat(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, mock)
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, mock)
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, mock)
        assert mock.calls == ["BTCUSDT"]

    def test_new_symbol_later(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        newly = ctrl.evaluate(frozenset({"BTCUSDT", "ETHUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        assert newly == ["ETHUSDT"]


class TestRetryOnFailure:
    def test_failed_retries_next_cycle(self) -> None:
        ctrl = LiveSymbolDegradationController()
        fail = _MockGracefulExit(result=GracefulExitResult.FAILED)
        ok = _MockGracefulExit(result=GracefulExitResult.SUCCESS)

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, fail)
        assert "BTCUSDT" not in ctrl.degraded_symbols

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, ok)
        assert "BTCUSDT" in ctrl.degraded_symbols

    def test_exception_retries(self) -> None:
        ctrl = LiveSymbolDegradationController()

        def _raise(symbol: str) -> None:
            raise RuntimeError("host error")

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, _raise)
        assert "BTCUSDT" not in ctrl.degraded_symbols

    def test_not_applicable_still_latched(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit(result=GracefulExitResult.NOT_APPLICABLE)
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        assert "BTCUSDT" in ctrl.degraded_symbols


class TestPruning:
    def test_deactivated_pruned(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        assert "BTCUSDT" in ctrl.degraded_symbols

        ctrl.evaluate(frozenset(), DayRiskMode.NORMAL, mock)
        assert "BTCUSDT" not in ctrl.degraded_symbols

    def test_pruned_can_degrade_again(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        ctrl.evaluate(frozenset(), DayRiskMode.NORMAL, mock)  # pruned
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        assert newly == ["BTCUSDT"]
        assert mock.calls == ["BTCUSDT", "BTCUSDT"]


class TestReset:
    def test_reset_clears(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        ctrl.reset()
        assert ctrl.degraded_symbols == frozenset()

    def test_after_reset_can_degrade_again(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        ctrl.reset()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        assert newly == ["BTCUSDT"]
