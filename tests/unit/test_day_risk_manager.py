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
        m.update(Decimal("100"))
        state = m.update(Decimal("104"))
        assert state.day_pnl_pct == Decimal("4.0")
        assert state.equity_day_start == Decimal("100")

    def test_peak_pnl_tracks_high_water(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("106"))
        state = m.update(Decimal("103"))
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
        state = m.update(Decimal("103"))
        assert state.profit_lock_floor_pct == Decimal("3.0")

    def test_stop_for_day_on_giveback(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("104"))  # peak +4%, arms lock
        state = m.update(Decimal("103"))  # +3% = floor → STOP
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_not_triggered_above_floor(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("104"))
        state = m.update(Decimal("103.1"))
        assert state.mode == DayRiskMode.DEFENSIVE

    def test_higher_peak_raises_floor(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("108"))  # peak +8%, floor = 7
        state = m.update(Decimal("107"))  # +7% = floor → STOP
        assert state.mode == DayRiskMode.STOP_FOR_DAY


class TestLossLimit:
    def test_stop_from_normal_at_minus_10(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        state = m.update(Decimal("90"))
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_stop_from_defensive_at_minus_10(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("103.5"))
        state = m.update(Decimal("90"))
        assert state.mode == DayRiskMode.STOP_FOR_DAY


class TestStopForDayLatched:
    def test_recovery_does_not_exit_stop(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("90"))
        assert m.mode == DayRiskMode.STOP_FOR_DAY
        state = m.update(Decimal("105"))
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_multiple_updates_stay_latched(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("90"))
        for eq in [Decimal("95"), Decimal("100"), Decimal("110")]:
            state = m.update(eq)
            assert state.mode == DayRiskMode.STOP_FOR_DAY


class TestZeroEquityRecovery:
    def test_zero_then_valid_initializes_normally(self) -> None:
        """Non-positive equity is skipped; first valid positive initializes."""
        m = _mgr()
        state = m.update(Decimal("0"))
        assert state.mode == DayRiskMode.NORMAL
        assert state.equity_day_start == Decimal("0")  # not yet initialized

        state = m.update(Decimal("100"))  # first valid
        assert state.equity_day_start == Decimal("100")
        assert state.mode == DayRiskMode.NORMAL

        state = m.update(Decimal("103"))  # should trigger DEFENSIVE
        assert state.mode == DayRiskMode.DEFENSIVE

    def test_negative_equity_skipped(self) -> None:
        m = _mgr()
        state = m.update(Decimal("-50"))
        assert state.mode == DayRiskMode.NORMAL
        state = m.update(Decimal("100"))
        assert state.equity_day_start == Decimal("100")


class TestSessionRollover:
    def test_new_session_key_resets_state(self) -> None:
        """Session key change triggers full reset."""
        m = _mgr()
        m.update(Decimal("100"), session_key="2026-04-06")
        m.update(Decimal("90"), session_key="2026-04-06")  # STOP_FOR_DAY
        assert m.mode == DayRiskMode.STOP_FOR_DAY

        # New day → reset
        state = m.update(Decimal("95"), session_key="2026-04-07")
        assert state.mode == DayRiskMode.NORMAL
        assert state.equity_day_start == Decimal("95")
        assert state.session_key == "2026-04-07"

    def test_same_session_key_no_reset(self) -> None:
        m = _mgr()
        m.update(Decimal("100"), session_key="2026-04-06")
        m.update(Decimal("90"), session_key="2026-04-06")
        assert m.mode == DayRiskMode.STOP_FOR_DAY

        # Same day → stays latched
        state = m.update(Decimal("105"), session_key="2026-04-06")
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_reset_for_new_day_explicit(self) -> None:
        """Explicit reset API works."""
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("90"))
        assert m.mode == DayRiskMode.STOP_FOR_DAY

        m.reset_for_new_day(equity_start=Decimal("92"))
        state = m.update(Decimal("92"))
        assert state.mode == DayRiskMode.NORMAL
        assert state.equity_day_start == Decimal("92")

    def test_rollover_allows_new_transitions(self) -> None:
        """After rollover, normal transitions work again."""
        m = _mgr()
        m.update(Decimal("100"), session_key="day1")
        m.update(Decimal("90"), session_key="day1")  # STOP
        assert m.is_stop_for_day()

        m.update(Decimal("200"), session_key="day2")  # new day, reset
        state = m.update(Decimal("206"), session_key="day2")  # +3%
        assert state.mode == DayRiskMode.DEFENSIVE
