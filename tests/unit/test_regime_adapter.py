"""Tests for portfolio-level regime adapter — map + aggregate."""

from __future__ import annotations

from grinder.controller.regime import Regime
from grinder.risk.portfolio_budget_allocator import MarketRegime
from grinder.risk.regime_adapter import (
    aggregate_portfolio_regime,
    classify_from_v2_features,
    map_symbol_regime,
)


class TestMapSymbolRegime:
    def test_range_maps_to_good(self) -> None:
        assert map_symbol_regime(Regime.RANGE) == MarketRegime.GOOD

    def test_trend_up_maps_to_neutral(self) -> None:
        assert map_symbol_regime(Regime.TREND_UP) == MarketRegime.NEUTRAL

    def test_trend_down_maps_to_neutral(self) -> None:
        assert map_symbol_regime(Regime.TREND_DOWN) == MarketRegime.NEUTRAL

    def test_vol_shock_maps_to_toxic(self) -> None:
        assert map_symbol_regime(Regime.VOL_SHOCK) == MarketRegime.TOXIC

    def test_thin_book_maps_to_toxic(self) -> None:
        assert map_symbol_regime(Regime.THIN_BOOK) == MarketRegime.TOXIC

    def test_toxic_maps_to_toxic(self) -> None:
        assert map_symbol_regime(Regime.TOXIC) == MarketRegime.TOXIC

    def test_paused_maps_to_toxic(self) -> None:
        assert map_symbol_regime(Regime.PAUSED) == MarketRegime.TOXIC

    def test_emergency_maps_to_toxic(self) -> None:
        assert map_symbol_regime(Regime.EMERGENCY) == MarketRegime.TOXIC


class TestAggregatePortfolioRegime:
    def test_empty_returns_neutral(self) -> None:
        assert aggregate_portfolio_regime({}) == MarketRegime.NEUTRAL

    def test_all_range_returns_good(self) -> None:
        regimes = {"A": Regime.RANGE, "B": Regime.RANGE, "C": Regime.RANGE}
        assert aggregate_portfolio_regime(regimes) == MarketRegime.GOOD

    def test_mixed_range_and_trend_returns_neutral(self) -> None:
        regimes = {"A": Regime.RANGE, "B": Regime.TREND_UP}
        assert aggregate_portfolio_regime(regimes) == MarketRegime.NEUTRAL

    def test_any_toxic_returns_toxic(self) -> None:
        regimes = {"A": Regime.RANGE, "B": Regime.TOXIC, "C": Regime.RANGE}
        assert aggregate_portfolio_regime(regimes) == MarketRegime.TOXIC

    def test_any_vol_shock_returns_toxic(self) -> None:
        regimes = {"A": Regime.RANGE, "B": Regime.VOL_SHOCK}
        assert aggregate_portfolio_regime(regimes) == MarketRegime.TOXIC

    def test_any_thin_book_returns_toxic(self) -> None:
        regimes = {"A": Regime.TREND_UP, "B": Regime.THIN_BOOK}
        assert aggregate_portfolio_regime(regimes) == MarketRegime.TOXIC

    def test_emergency_returns_toxic(self) -> None:
        regimes = {"A": Regime.EMERGENCY}
        assert aggregate_portfolio_regime(regimes) == MarketRegime.TOXIC

    def test_single_range_returns_good(self) -> None:
        assert aggregate_portfolio_regime({"X": Regime.RANGE}) == MarketRegime.GOOD

    def test_deterministic(self) -> None:
        regimes = {"A": Regime.RANGE, "B": Regime.TREND_DOWN, "C": Regime.RANGE}
        r1 = aggregate_portfolio_regime(regimes)
        r2 = aggregate_portfolio_regime(regimes)
        assert r1 == r2 == MarketRegime.NEUTRAL


class _FakeV2Features:
    """Minimal V2 features for regime classification tests."""

    def __init__(
        self,
        *,
        natr_14_5m: float = 1.5,
        net_return_bps: int = 50,
        range_score: int = 10,
        toxicity_penalty_raw: float = 0.1,
    ) -> None:
        from decimal import Decimal  # noqa: PLC0415

        self.natr_14_5m = Decimal(str(natr_14_5m))
        self.net_return_bps = net_return_bps
        self.range_score = range_score
        self.toxicity_penalty_raw = toxicity_penalty_raw


class TestClassifyFromV2Features:
    def test_calm_market_returns_range(self) -> None:
        feat = _FakeV2Features(natr_14_5m=1.5, net_return_bps=50, range_score=10)
        assert classify_from_v2_features(feat) == Regime.RANGE

    def test_high_toxicity_returns_toxic(self) -> None:
        feat = _FakeV2Features(toxicity_penalty_raw=0.7)
        assert classify_from_v2_features(feat) == Regime.TOXIC

    def test_vol_shock_returns_vol_shock(self) -> None:
        feat = _FakeV2Features(natr_14_5m=6.0, toxicity_penalty_raw=0.1)
        assert classify_from_v2_features(feat) == Regime.VOL_SHOCK

    def test_trend_up_returns_trend_up(self) -> None:
        feat = _FakeV2Features(net_return_bps=300, range_score=2, toxicity_penalty_raw=0.1)
        assert classify_from_v2_features(feat) == Regime.TREND_UP

    def test_trend_down_returns_trend_down(self) -> None:
        feat = _FakeV2Features(net_return_bps=-300, range_score=2, toxicity_penalty_raw=0.1)
        assert classify_from_v2_features(feat) == Regime.TREND_DOWN

    def test_high_return_but_choppy_returns_range(self) -> None:
        """High net return but high range_score (choppy) → not a trend."""
        feat = _FakeV2Features(net_return_bps=300, range_score=10, toxicity_penalty_raw=0.1)
        assert classify_from_v2_features(feat) == Regime.RANGE

    def test_toxicity_takes_precedence_over_vol(self) -> None:
        """Toxicity check runs before vol shock."""
        feat = _FakeV2Features(natr_14_5m=6.0, toxicity_penalty_raw=0.8)
        assert classify_from_v2_features(feat) == Regime.TOXIC

    def test_vol_takes_precedence_over_trend(self) -> None:
        """Vol shock check runs before trend detection."""
        feat = _FakeV2Features(natr_14_5m=6.0, net_return_bps=300, range_score=2)
        assert classify_from_v2_features(feat) == Regime.VOL_SHOCK
