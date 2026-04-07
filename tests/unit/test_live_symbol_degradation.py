"""Tests for LiveSymbolDegradationController — day-mode degradation ladder."""

from __future__ import annotations

from grinder.risk.day_risk_manager import DayRiskMode
from grinder.risk.live_symbol_degradation import LiveSymbolDegradationController
from grinder.runtime.autonomous_host import GracefulExitResult


class _MockGracefulExit:
    def __init__(self, result: GracefulExitResult = GracefulExitResult.SUCCESS) -> None:
        self.calls: list[str] = []
        self._result = result

    def __call__(self, symbol: str) -> GracefulExitResult:
        self.calls.append(symbol)
        return self._result


class _MockForceReduce:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[str] = []
        self._ok = ok

    def __call__(self, symbol: str) -> bool:
        self.calls.append(symbol)
        return self._ok


class TestGracefulExitTriggers:
    def test_stop_new_risk(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, ge)
        assert newly == ["BTCUSDT"]
        assert "BTCUSDT" in ctrl.degraded_symbols
        assert "BTCUSDT" not in ctrl.force_reduced_symbols

    def test_stop_for_day(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"ETHUSDT"}), DayRiskMode.STOP_FOR_DAY, ge)
        assert newly == ["ETHUSDT"]
        assert "ETHUSDT" in ctrl.degraded_symbols

    def test_stop_new_risk_no_force_reduce(self) -> None:
        """STOP_NEW_RISK does NOT call force_reduce_fn."""
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        fr = _MockForceReduce()
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, ge, fr)
        assert fr.calls == []


class TestForceReduce:
    def test_force_reduce_does_both(self) -> None:
        """FORCE_REDUCE: graceful-exit + force-reduce signal."""
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        fr = _MockForceReduce()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, fr)
        assert newly == ["BTCUSDT"]
        assert "BTCUSDT" in ge.calls
        assert "BTCUSDT" in fr.calls
        assert "BTCUSDT" in ctrl.degraded_symbols
        assert "BTCUSDT" in ctrl.force_reduced_symbols

    def test_force_reduce_multiple(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        fr = _MockForceReduce()
        live = frozenset({"AAAUSDT", "BBBUSDT"})
        newly = ctrl.evaluate(live, DayRiskMode.FORCE_REDUCE, ge, fr)
        assert sorted(newly) == ["AAAUSDT", "BBBUSDT"]
        assert sorted(fr.calls) == ["AAAUSDT", "BBBUSDT"]

    def test_escalation_from_graceful_to_force(self) -> None:
        """Symbol already graceful-exit → FORCE_REDUCE escalates."""
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        fr = _MockForceReduce()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, ge)
        assert "BTCUSDT" in ctrl.degraded_symbols

        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, fr)
        assert newly == ["BTCUSDT"]
        assert ge.calls == ["BTCUSDT"]  # no second graceful-exit
        assert fr.calls == ["BTCUSDT"]
        assert "BTCUSDT" in ctrl.force_reduced_symbols

    def test_no_escalation_if_graceful_exit_fails(self) -> None:
        """FORCE_REDUCE: if graceful-exit fails, don't escalate this cycle."""
        ctrl = LiveSymbolDegradationController()
        ge_fail = _MockGracefulExit(result=GracefulExitResult.FAILED)
        fr = _MockForceReduce()

        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge_fail, fr)
        assert newly == []
        assert fr.calls == []  # force-reduce NOT called
        assert "BTCUSDT" not in ctrl.degraded_symbols
        assert "BTCUSDT" not in ctrl.force_reduced_symbols

    def test_no_force_reduce_fn_skips(self) -> None:
        """Without force_reduce_fn, only graceful-exit happens."""
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, None)
        assert newly == []
        assert "BTCUSDT" in ctrl.degraded_symbols
        assert "BTCUSDT" not in ctrl.force_reduced_symbols


class TestNoDegradation:
    def test_normal(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        assert ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.NORMAL, ge) == []

    def test_defensive(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        assert ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.DEFENSIVE, ge) == []

    def test_empty(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        assert ctrl.evaluate(frozenset(), DayRiskMode.STOP_FOR_DAY, ge) == []


class TestIdempotency:
    def test_graceful_exit_no_repeat(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, ge)
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.STOP_NEW_RISK, ge)
        assert ge.calls == ["BTCUSDT"]

    def test_force_reduce_no_repeat(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        fr = _MockForceReduce()
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, fr)
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, fr)
        assert ge.calls == ["BTCUSDT"]
        assert fr.calls == ["BTCUSDT"]


class TestRetryOnFailure:
    def test_graceful_failed_retries(self) -> None:
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

    def test_force_reduce_exception_retries(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()

        def _raise(symbol: str) -> bool:
            raise RuntimeError("engine error")

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, _raise)
        assert "BTCUSDT" in ctrl.degraded_symbols  # graceful-exit latched
        assert "BTCUSDT" not in ctrl.force_reduced_symbols  # force-reduce retries


class TestPruning:
    def test_deactivated_pruned(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        fr = _MockForceReduce()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, fr)
        assert "BTCUSDT" in ctrl.force_reduced_symbols

        ctrl.evaluate(frozenset(), DayRiskMode.NORMAL, ge)
        assert "BTCUSDT" not in ctrl.degraded_symbols
        assert "BTCUSDT" not in ctrl.force_reduced_symbols

    def test_pruned_can_be_degraded_again(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        fr = _MockForceReduce()

        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, fr)
        ctrl.evaluate(frozenset(), DayRiskMode.NORMAL, ge)
        newly = ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, fr)
        assert newly == ["BTCUSDT"]


class TestReset:
    def test_reset_clears_both(self) -> None:
        ctrl = LiveSymbolDegradationController()
        ge = _MockGracefulExit()
        fr = _MockForceReduce()
        ctrl.evaluate(frozenset({"BTCUSDT"}), DayRiskMode.FORCE_REDUCE, ge, fr)
        ctrl.reset()
        assert ctrl.degraded_symbols == frozenset()
        assert ctrl.force_reduced_symbols == frozenset()
