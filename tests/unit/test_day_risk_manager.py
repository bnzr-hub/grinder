"""Tests for DayRiskManager — day/session risk state machine."""

from __future__ import annotations

from decimal import Decimal

from grinder.risk.day_risk_manager import (
    DayRiskConfig,
    DayRiskManager,
    DayRiskMode,
)


def _mgr(
    trigger: str = "3.0",
    arm: str = "4.0",
    trailing: str = "0.5",
    loss: str = "12.0",
) -> DayRiskManager:
    return DayRiskManager(
        config=DayRiskConfig(
            profit_lock_trigger_pct=Decimal(trigger),
            profit_lock_arm_pct=Decimal(arm),
            profit_lock_trailing_fraction=Decimal(trailing),
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
    def test_no_floor_below_arm_threshold(self) -> None:
        """At +3% (DEFENSIVE but below arm), floor is zero."""
        m = _mgr()
        m.update(Decimal("100"))
        state = m.update(Decimal("103"))
        assert state.profit_lock_floor_pct == Decimal("0")

    def test_floor_at_arm_threshold(self) -> None:
        """At +4% peak, floor = max(4%, 4%*0.5) = 4%."""
        m = _mgr()
        m.update(Decimal("100"))
        state = m.update(Decimal("104"))
        assert state.profit_lock_floor_pct == Decimal("4.0")

    def test_not_armed_at_3pct(self) -> None:
        """+3% triggers DEFENSIVE but does NOT arm profit lock."""
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("103"))  # DEFENSIVE
        state = m.update(Decimal("100"))  # drop back to 0%
        # Should NOT be STOP_FOR_DAY — lock was never armed
        assert state.mode != DayRiskMode.STOP_FOR_DAY

    def test_stop_for_day_at_floor(self) -> None:
        """Peak +4%, floor = 4%. Drop to +4% → STOP."""
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("105"))  # peak +5%, floor = max(4%, 2.5) = 4%
        state = m.update(Decimal("104"))  # +4% = floor → STOP
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_not_triggered_above_floor(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("105"))  # peak +5%, floor = 4%
        state = m.update(Decimal("104.1"))  # +4.1% > floor
        assert state.mode == DayRiskMode.DEFENSIVE

    def test_peak_6pct_floor_stays_4pct(self) -> None:
        """Peak +6%, floor = max(4%, 3%) = 4%."""
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("106"))  # peak +6%
        state = m.update(Decimal("104.1"))  # +4.1% > 4% floor
        assert state.mode == DayRiskMode.DEFENSIVE
        state = m.update(Decimal("104"))  # +4% = floor → STOP
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_peak_8pct_floor_stays_4pct(self) -> None:
        """Peak +8%, floor = max(4%, 4%) = 4%."""
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("108"))  # peak +8%, floor = max(4%, 4%) = 4%
        state = m.update(Decimal("104"))  # +4% = floor → STOP
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_peak_10pct_floor_5pct(self) -> None:
        """Peak +10%, floor = max(4%, 5%) = 5%."""
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("110"))  # peak +10%, floor = 5%
        state = m.update(Decimal("105.1"))  # above floor
        assert state.mode == DayRiskMode.DEFENSIVE
        state = m.update(Decimal("105"))  # +5% = floor → STOP
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_peak_14pct_floor_7pct(self) -> None:
        """Peak +14%, floor = max(4%, 7%) = 7%."""
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("114"))  # peak +14%, floor = 7%
        state = m.update(Decimal("107"))  # +7% = floor → STOP
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_custom_trailing_fraction(self) -> None:
        """Non-default fraction=0.7: peak +10%, floor = max(4%, 7%) = 7%."""
        m = _mgr(trailing="0.7")
        m.update(Decimal("100"))
        m.update(Decimal("110"))  # peak +10%, floor = max(4%, 7%) = 7%
        state = m.update(Decimal("107.1"))  # above floor
        assert state.mode == DayRiskMode.DEFENSIVE
        state = m.update(Decimal("107"))  # +7% = floor → STOP
        assert state.mode == DayRiskMode.STOP_FOR_DAY
        assert state.profit_lock_floor_pct == Decimal("7.00")


class TestLossLimit:
    def test_stop_from_normal_at_minus_12(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        state = m.update(Decimal("88"))  # -12%
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_no_stop_at_minus_11(self) -> None:
        """11% loss not enough for 12% limit."""
        m = _mgr()
        m.update(Decimal("100"))
        state = m.update(Decimal("89"))  # -11%
        assert state.mode != DayRiskMode.STOP_FOR_DAY

    def test_stop_from_defensive_at_minus_12(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("103.5"))
        state = m.update(Decimal("88"))  # -12%
        assert state.mode == DayRiskMode.STOP_FOR_DAY


class TestStopForDayLatched:
    def test_recovery_does_not_exit_stop(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("88"))  # -12%
        assert m.mode == DayRiskMode.STOP_FOR_DAY
        state = m.update(Decimal("105"))
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_multiple_updates_stay_latched(self) -> None:
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("88"))
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
        m.update(Decimal("88"), session_key="2026-04-06")  # STOP_FOR_DAY
        assert m.mode == DayRiskMode.STOP_FOR_DAY

        # New day → reset
        state = m.update(Decimal("95"), session_key="2026-04-07")
        assert state.mode == DayRiskMode.NORMAL
        assert state.equity_day_start == Decimal("95")
        assert state.session_key == "2026-04-07"

    def test_same_session_key_no_reset(self) -> None:
        m = _mgr()
        m.update(Decimal("100"), session_key="2026-04-06")
        m.update(Decimal("88"), session_key="2026-04-06")
        assert m.mode == DayRiskMode.STOP_FOR_DAY

        # Same day → stays latched
        state = m.update(Decimal("105"), session_key="2026-04-06")
        assert state.mode == DayRiskMode.STOP_FOR_DAY

    def test_reset_for_new_day_explicit(self) -> None:
        """Explicit reset API works."""
        m = _mgr()
        m.update(Decimal("100"))
        m.update(Decimal("88"))
        assert m.mode == DayRiskMode.STOP_FOR_DAY

        m.reset_for_new_day(equity_start=Decimal("92"))
        state = m.update(Decimal("92"))
        assert state.mode == DayRiskMode.NORMAL
        assert state.equity_day_start == Decimal("92")

    def test_rollover_allows_new_transitions(self) -> None:
        """After rollover, normal transitions work again."""
        m = _mgr()
        m.update(Decimal("100"), session_key="day1")
        m.update(Decimal("88"), session_key="day1")  # STOP
        assert m.is_stop_for_day()

        m.update(Decimal("200"), session_key="day2")  # new day, reset
        state = m.update(Decimal("206"), session_key="day2")  # +3%
        assert state.mode == DayRiskMode.DEFENSIVE
