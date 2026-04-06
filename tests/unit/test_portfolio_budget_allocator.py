"""Tests for PortfolioBudgetAllocator."""

from __future__ import annotations

from decimal import Decimal

from grinder.risk.day_risk_manager import DayRiskMode
from grinder.risk.portfolio_budget_allocator import (
    MarketRegime,
    PortfolioBudgetAllocator,
    PortfolioBudgetInput,
)


def _inp(
    equity: str = "1000",
    day_mode: DayRiskMode = DayRiskMode.NORMAL,
    regime: MarketRegime = MarketRegime.NEUTRAL,
    active: int = 0,
    exposure: str = "0",
) -> PortfolioBudgetInput:
    return PortfolioBudgetInput(
        equity=Decimal(equity),
        day_mode=day_mode,
        market_regime=regime,
        active_symbol_count=active,
        gross_exposure_used_usd=Decimal(exposure),
    )


def _alloc() -> PortfolioBudgetAllocator:
    return PortfolioBudgetAllocator()


class TestBaseRiskByRegime:
    def test_good_normal_3pct(self) -> None:
        snap = _alloc().compute(_inp(regime=MarketRegime.GOOD))
        assert snap.base_risk_pct == Decimal("3.0")
        assert snap.effective_risk_pct == Decimal("3.0")
        assert snap.per_symbol_risk_budget_usd == Decimal("30.0")

    def test_neutral_normal_2pct(self) -> None:
        snap = _alloc().compute(_inp(regime=MarketRegime.NEUTRAL))
        assert snap.effective_risk_pct == Decimal("2.0")
        assert snap.per_symbol_risk_budget_usd == Decimal("20.0")

    def test_toxic_normal_1pct(self) -> None:
        snap = _alloc().compute(_inp(regime=MarketRegime.TOXIC))
        assert snap.effective_risk_pct == Decimal("1.0")
        assert snap.per_symbol_risk_budget_usd == Decimal("10.0")


class TestDayModeModifier:
    def test_defensive_halves_risk(self) -> None:
        snap = _alloc().compute(
            _inp(regime=MarketRegime.GOOD, day_mode=DayRiskMode.DEFENSIVE)
        )
        assert snap.effective_risk_pct == Decimal("1.5")
        assert snap.per_symbol_risk_budget_usd == Decimal("15.0")

    def test_stop_for_day_zero_risk(self) -> None:
        snap = _alloc().compute(
            _inp(day_mode=DayRiskMode.STOP_FOR_DAY)
        )
        assert snap.effective_risk_pct == Decimal("0")
        assert snap.per_symbol_risk_budget_usd == Decimal("0")
        assert not snap.new_entries_allowed
        assert "STOP_FOR_DAY" in snap.reasons

    def test_stop_new_risk_blocks_entries(self) -> None:
        snap = _alloc().compute(
            _inp(day_mode=DayRiskMode.STOP_NEW_RISK)
        )
        assert snap.effective_risk_pct == Decimal("0")
        assert not snap.new_entries_allowed
        assert "STOP_NEW_RISK" in snap.reasons

    def test_force_reduce_blocks_entries(self) -> None:
        snap = _alloc().compute(
            _inp(day_mode=DayRiskMode.FORCE_REDUCE)
        )
        assert snap.effective_risk_pct == Decimal("0")
        assert not snap.new_entries_allowed
        assert "FORCE_REDUCE" in snap.reasons


class TestSymbolSlots:
    def test_zero_active_allows_entries(self) -> None:
        snap = _alloc().compute(_inp(active=0))
        assert snap.remaining_symbol_slots == 5
        assert snap.new_entries_allowed

    def test_max_active_blocks_entries(self) -> None:
        snap = _alloc().compute(_inp(active=5))
        assert snap.remaining_symbol_slots == 0
        assert not snap.new_entries_allowed
        assert "MAX_SYMBOLS_REACHED" in snap.reasons

    def test_partial_active_has_slots(self) -> None:
        snap = _alloc().compute(_inp(active=3))
        assert snap.remaining_symbol_slots == 2
        assert snap.new_entries_allowed


class TestLeverageCapacity:
    def test_no_exposure_full_capacity(self) -> None:
        snap = _alloc().compute(_inp(equity="1000", exposure="0"))
        assert snap.gross_exposure_limit_usd == Decimal("5000")
        assert snap.gross_exposure_free_usd == Decimal("5000")

    def test_full_exposure_blocks_entries(self) -> None:
        snap = _alloc().compute(_inp(equity="1000", exposure="5000"))
        assert snap.gross_exposure_free_usd == Decimal("0")
        assert not snap.new_entries_allowed
        assert "LEVERAGE_CAPACITY_FULL" in snap.reasons

    def test_over_exposure_clamps_to_zero(self) -> None:
        snap = _alloc().compute(_inp(equity="1000", exposure="6000"))
        assert snap.gross_exposure_free_usd == Decimal("0")
        assert not snap.new_entries_allowed


class TestZeroEquity:
    def test_zero_equity_blocks_entries(self) -> None:
        snap = _alloc().compute(_inp(equity="0"))
        assert not snap.new_entries_allowed
        assert "NO_EQUITY" in snap.reasons


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        alloc = _alloc()
        inp = _inp(equity="500", active=2, regime=MarketRegime.GOOD)
        s1 = alloc.compute(inp)
        s2 = alloc.compute(inp)
        assert s1 == s2
