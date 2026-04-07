"""Tests for portfolio-level regime aggregation from live engine decisions."""

from __future__ import annotations

from grinder.controller.regime import Regime
from grinder.risk.portfolio_budget_allocator import MarketRegime
from grinder.risk.regime_aggregation import (
    aggregate_portfolio_regime,
    map_engine_regime,
)


class TestMapEngineRegime:
    def test_range_to_good(self) -> None:
        assert map_engine_regime(Regime.RANGE) == MarketRegime.GOOD

    def test_trend_up_to_neutral(self) -> None:
        assert map_engine_regime(Regime.TREND_UP) == MarketRegime.NEUTRAL

    def test_trend_down_to_neutral(self) -> None:
        assert map_engine_regime(Regime.TREND_DOWN) == MarketRegime.NEUTRAL

    def test_vol_shock_to_toxic(self) -> None:
        assert map_engine_regime(Regime.VOL_SHOCK) == MarketRegime.TOXIC

    def test_thin_book_to_toxic(self) -> None:
        assert map_engine_regime(Regime.THIN_BOOK) == MarketRegime.TOXIC

    def test_toxic_to_toxic(self) -> None:
        assert map_engine_regime(Regime.TOXIC) == MarketRegime.TOXIC

    def test_paused_to_toxic(self) -> None:
        assert map_engine_regime(Regime.PAUSED) == MarketRegime.TOXIC

    def test_emergency_to_toxic(self) -> None:
        assert map_engine_regime(Regime.EMERGENCY) == MarketRegime.TOXIC


class TestAggregatePortfolioRegime:
    def test_empty_returns_neutral(self) -> None:
        assert aggregate_portfolio_regime([]) == MarketRegime.NEUTRAL

    def test_single_range_returns_good(self) -> None:
        assert aggregate_portfolio_regime([Regime.RANGE]) == MarketRegime.GOOD

    def test_all_range_returns_good(self) -> None:
        assert aggregate_portfolio_regime([Regime.RANGE, Regime.RANGE]) == MarketRegime.GOOD

    def test_single_trend_returns_neutral(self) -> None:
        assert aggregate_portfolio_regime([Regime.TREND_UP]) == MarketRegime.NEUTRAL

    def test_range_plus_trend_returns_neutral(self) -> None:
        assert aggregate_portfolio_regime([Regime.RANGE, Regime.TREND_DOWN]) == MarketRegime.NEUTRAL

    def test_single_toxic_returns_toxic(self) -> None:
        assert aggregate_portfolio_regime([Regime.TOXIC]) == MarketRegime.TOXIC

    def test_any_toxic_returns_toxic(self) -> None:
        assert aggregate_portfolio_regime([Regime.RANGE, Regime.TOXIC]) == MarketRegime.TOXIC

    def test_any_vol_shock_returns_toxic(self) -> None:
        assert aggregate_portfolio_regime([Regime.RANGE, Regime.VOL_SHOCK]) == MarketRegime.TOXIC

    def test_any_thin_book_returns_toxic(self) -> None:
        assert aggregate_portfolio_regime([Regime.TREND_UP, Regime.THIN_BOOK]) == MarketRegime.TOXIC

    def test_emergency_returns_toxic(self) -> None:
        assert aggregate_portfolio_regime([Regime.EMERGENCY]) == MarketRegime.TOXIC

    def test_mixed_good_neutral_toxic(self) -> None:
        regimes = [Regime.RANGE, Regime.TREND_UP, Regime.TOXIC]
        assert aggregate_portfolio_regime(regimes) == MarketRegime.TOXIC

    def test_deterministic(self) -> None:
        regimes = [Regime.RANGE, Regime.TREND_DOWN, Regime.RANGE]
        r1 = aggregate_portfolio_regime(regimes)
        r2 = aggregate_portfolio_regime(regimes)
        assert r1 == r2 == MarketRegime.NEUTRAL


class TestExhaustiveMapping:
    def test_all_regime_values_mapped(self) -> None:
        """Every Regime enum value must have an explicit mapping."""
        for regime in Regime:
            result = map_engine_regime(regime)
            assert isinstance(result, MarketRegime), f"{regime} not mapped"

    def test_unknown_regime_raises(self) -> None:
        """Unmapped value raises ValueError, not silent fallback."""
        import pytest  # noqa: PLC0415

        with pytest.raises(ValueError, match="Unmapped Regime"):
            map_engine_regime("FAKE_REGIME")  # type: ignore[arg-type]
