"""Tests for DayRiskManager — day/session risk state machine."""

from __future__ import annotations

from decimal import Decimal

from grinder.risk.day_risk_manager import (
    DayRiskConfig,
    DayRiskManager,
    DayRiskMode,
)


def _mgr(trigger: str = "3.0", giveback: str = "1.0", loss: str = "10.0") -> DayRiskManager:
    return DayRiskManager(
        config=DayRiskConfig(
            profit_lock_trigger_pct=Decimal(trigger),
            profit_giveback_pct=Decimal(giveback),
            daily_loss_limit_pct=Decimal(loss),
        )
    )


class TestDayRiskModeEnum:
    def test_has_five_members(self) -> None:
        assert len(DayRiskMode) == 5
        assert {m.value for m in DayRiskMode} == {
            "NORMAL",
            "DEFENSIVE",
            "STOP_NEW_RISK",
            "FORCE_REDUCE",
            "STOP_FOR_DAY",
        }


class TestDerivedFields:
    def test_pnl_fields_correct(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))  # init
        state = m.update(Decimal("104"))  # +4%
        assert state.day_pnl_pct == Decimal("4.0")
        assert state.equity_day_start == Decimal("100")

    def test_peak_pnl_tracks_high_water(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("106"))  # peak at +6%
        state = m.update(Decimal("103"))  # current at +3%
        assert state.day_peak_pnl_pct == Decimal("6.0")
        assert state.day_pnl_pct == Decimal("3.0")


class TestNormalToDefensive:
    def test_stays_normal_below_3pct(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        state = m.update(Decimal("102.9"))
        assert state.mode == DayRiskMode.NORMAL

    def test_defensive_at_3pct(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        state = m.update(Decimal("103"))
        assert state.mode == DayRiskMode.DEFENSIVE


class TestProfitLock:
    def test_profit_lock_floor_minimum_3pct(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        state = m.update(Decimal("103"))  # peak exactly +3%
        assert state.profit_lock_floor_pct == Decimal("3.0")  # max(3, 3-1) = 3

    def test_stop_for_day_on_giveback(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("104"))  # peak +4%, floor = max(3, 4-1) = 3
        state = m.update(Decimal("103"))  # current +3% = floor → STOP
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_not_triggered_above_floor(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("104"))  # peak +4%, floor = 3
        state = m.update(Decimal("103.1"))  # +3.1% > floor 3% → still DEFENSIVE
        assert state.mode == DayRiskMode.DEFENSIVE

    def test_higher_peak_raises_floor(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("108"))  # peak +8%, floor = max(3, 8-1) = 7
        state = m.update(Decimal("107"))  # +7% = floor → STOP
        assert state.mode == DayRiskMode.STOP_FOR_DAY


class TestLossLimit:
    def test_stop_from_normal_at_minus_10(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        state = m.update(Decimal("90"))  # -10%
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_stop_from_defensive_at_minus_10(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("103.5"))  # DEFENSIVE
        state = m.update(Decimal("90"))  # -10%
        assert state.mode == DayRiskMode.STOP_FOR_DAY


class TestStopForDayLatched:
    def test_recovery_does_not_exit_stop(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("90"))  # STOP_FOR_DAY
        assert m.mode == DayRiskMode.STOP_FOR_DAY
        state = m.update(Decimal("105"))  # recovery
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_multiple_updates_stay_latched(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("90"))
        for eq in [Decimal("95"), Decimal("100"), Decimal("110")]:
            state = m.update(eq)
            assert state.mode == DayRiskMode.STOP_FOR_DAY


class TestZeroEquity:
    def test_zero_start_skips_transitions(self) -> None:
        m = _mgr()
        state = m.update(Decimal("0"))
        assert state.mode == DayRiskMode.NORMAL
        assert state.day_pnl_pct == Decimal("0")
