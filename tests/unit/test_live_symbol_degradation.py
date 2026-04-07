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
        assert "ETHUSDT" in mock.calls

    def test_force_reduce_degrades(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"SOLUSDT"}), DayRiskMode.FORCE_REDUCE, mock)
        assert newly == ["SOLUSDT"]
        assert "SOLUSDT" in mock.calls

    def test_multiple_live_symbols_all_degraded(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        live = frozenset({"AAAUSDT", "BBBUSDT", "CCCUSDT"})
        newly = ctrl.evaluate(live, DayRiskMode.STOP_FOR_DAY, mock)
        assert sorted(newly) == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        assert sorted(mock.calls) == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]


class TestNoDegradation:
    def test_normal_mode_no_degradation(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.NORMAL, mock)
        assert newly == []
        assert mock.calls == []

    def test_defensive_mode_no_degradation(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.DEFENSIVE, mock)
        assert newly == []
        assert mock.calls == []

    def test_empty_live_symbols_no_degradation(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset(), DayRiskMode.STOP_FOR_DAY, mock)
        assert newly == []
        assert mock.calls == []


class TestIdempotency:
    def test_repeated_cycles_no_duplicate_request(self) -> None:
        """Same symbol + same mode across multiple cycles → request only once."""
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        live = frozenset({"BTCUSDT"})

        newly1 = ctrl.evaluate(live, DayRiskMode.STOP_NEW_RISK, mock)
        newly2 = ctrl.evaluate(live, DayRiskMode.STOP_NEW_RISK, mock)
        newly3 = ctrl.evaluate(live, DayRiskMode.STOP_NEW_RISK, mock)

        assert newly1 == ["BTCUSDT"]
        assert newly2 == []
        assert newly3 == []
        assert mock.calls == ["BTCUSDT"]  # only one call

    def test_new_symbol_in_later_cycle_degraded(self) -> None:
        """New live symbol appearing later still gets degraded."""
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        newly = ctrl.evaluate(frozenset({"BTCUSDT", "ETHUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)

        assert newly == ["ETHUSDT"]
        assert mock.calls == ["BTCUSDT", "ETHUSDT"]


class TestDegradedSymbolsProperty:
    def test_tracks_degraded(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()
        assert ctrl.degraded_symbols == frozenset()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        assert ctrl.degraded_symbols == frozenset({"BTCUSDT"})


class TestReset:
    def test_reset_clears_degraded(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        assert len(ctrl.degraded_symbols) == 1

        ctrl.reset()
        assert ctrl.degraded_symbols == frozenset()

    def test_after_reset_can_degrade_again(self) -> None:
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        ctrl.reset()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)

        assert newly == ["BTCUSDT"]
        assert mock.calls == ["BTCUSDT", "BTCUSDT"]  # called twice (before + after reset)


class TestGracefulExitResultHandling:
    def test_not_applicable_still_tracked(self) -> None:
        """NOT_APPLICABLE result doesn't prevent tracking."""
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit(result=GracefulExitResult.NOT_APPLICABLE)
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        assert newly == ["BTCUSDT"]
        assert "BTCUSDT" in ctrl.degraded_symbols

    def test_failed_not_latched_retries_next_cycle(self) -> None:
        """FAILED result → not latched → retried next cycle."""
        ctrl = LiveSymbolDegradationController()
        fail_mock = _MockGracefulExit(result=GracefulExitResult.FAILED)
        newly1 = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, fail_mock)
        assert newly1 == []
        assert "BTCUSDT" not in ctrl.degraded_symbols

        # Next cycle with success
        ok_mock = _MockGracefulExit(result=GracefulExitResult.SUCCESS)
        newly2 = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, ok_mock)
        assert newly2 == ["BTCUSDT"]
        assert "BTCUSDT" in ctrl.degraded_symbols

    def test_exception_not_latched_retries(self) -> None:
        """Exception → not latched → retried next cycle."""
        ctrl = LiveSymbolDegradationController()

        def _raise(symbol: str) -> None:
            raise RuntimeError("host error")

        newly1 = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, _raise)
        assert newly1 == []
        assert "BTCUSDT" not in ctrl.degraded_symbols


class TestPruning:
    def test_deactivated_symbol_pruned(self) -> None:
        """Symbol removed from live set gets pruned from degraded."""
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        assert "BTCUSDT" in ctrl.degraded_symbols

        # Symbol no longer live → pruned
        ctrl.evaluate(frozenset(), DayRiskMode.STOP_FOR_DAY, mock)
        assert "BTCUSDT" not in ctrl.degraded_symbols

    def test_pruned_symbol_can_be_degraded_again(self) -> None:
        """After deactivation + re-activation, symbol can be degraded again."""
        ctrl = LiveSymbolDegradationController()
        mock = _MockGracefulExit()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)
        ctrl.evaluate(frozenset(), DayRiskMode.NORMAL, mock)  # pruned
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_FOR_DAY, mock)

        assert newly == ["BTCUSDT"]
        assert mock.calls == ["BTCUSDT", "BTCUSDT"]
