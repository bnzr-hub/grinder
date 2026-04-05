"""Tests for V2 ranker: seven-component formula with range/trend/toxicity/execution_fit."""

from __future__ import annotations

from decimal import Decimal

from grinder.selector.models import SelectionFeatures, SelectionFeaturesV2
from grinder.selector.ranker import (
    W_V2_EXEC_FIT,
    W_V2_LIQUIDITY,
    W_V2_RANGE,
    W_V2_SPREAD,
    W_V2_TOXICITY_PENALTY,
    W_V2_TREND_PENALTY,
    W_V2_VOLATILITY,
    rank_v1,
    rank_v2,
)


def _feat_v2(
    symbol: str,
    volume: str = "5000000",
    bid: str = "1.0",
    ask: str = "1.001",
    natr: str = "2.0",
    range_score: int = 50,
    net_return_bps: int = 10,
    execution_fit_score: float = 0.8,
    toxicity_penalty_raw: float = 0.1,
) -> SelectionFeaturesV2:
    return SelectionFeaturesV2(
        symbol=symbol,
        quote_volume_last_12x5m=Decimal(volume),
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        natr_14_5m=Decimal(natr),
        range_score=range_score,
        net_return_bps=net_return_bps,
        execution_fit_score=execution_fit_score,
        toxicity_penalty_raw=toxicity_penalty_raw,
    )


class TestRankV2:
    def test_choppy_symbol_ranks_above_trending(self) -> None:
        """Higher range_score (choppier) beats lower range_score when all else equal."""
        features = {
            "CHOPPY": _feat_v2("CHOPPY", range_score=100, net_return_bps=5),
            "TREND": _feat_v2("TREND", range_score=10, net_return_bps=5),
        }
        scored = rank_v2(["CHOPPY", "TREND"], features)
        assert scored[0].symbol == "CHOPPY"

    def test_trending_symbol_penalized(self) -> None:
        """Higher net_return_bps (more directional) ranks lower."""
        features = {
            "CALM": _feat_v2("CALM", net_return_bps=5, range_score=50),
            "DIRECTIONAL": _feat_v2("DIRECTIONAL", net_return_bps=200, range_score=50),
        }
        scored = rank_v2(["CALM", "DIRECTIONAL"], features)
        assert scored[0].symbol == "CALM"

    def test_tight_spread_ranks_above_wide(self) -> None:
        """Tight spread reduces toxicity penalty and increases spread score."""
        features = {
            "TIGHT": _feat_v2("TIGHT", bid="1.0000", ask="1.0001", toxicity_penalty_raw=0.05),
            "WIDE": _feat_v2("WIDE", bid="1.0000", ask="1.0100", toxicity_penalty_raw=0.9),
        }
        scored = rank_v2(["TIGHT", "WIDE"], features)
        assert scored[0].symbol == "TIGHT"

    def test_execution_fit_prefers_comfortable_cap(self) -> None:
        """Symbol with more headroom under cap ranks higher."""
        features = {
            "COMFY": _feat_v2("COMFY", execution_fit_score=0.9),
            "TIGHT_FIT": _feat_v2("TIGHT_FIT", execution_fit_score=0.1),
        }
        scored = rank_v2(["COMFY", "TIGHT_FIT"], features)
        assert scored[0].symbol == "COMFY"

    def test_score_formula_matches_weighted_sum(self) -> None:
        """Score for a single symbol equals the exact weighted formula."""
        feat = _feat_v2("ONLY", volume="5000000", natr="2.0", range_score=50,
                        net_return_bps=10, execution_fit_score=0.8, toxicity_penalty_raw=0.1)
        scored = rank_v2(["ONLY"], {"ONLY": feat})
        assert len(scored) == 1
        s = scored[0]
        # Single symbol → all normalized to 0.5 (min == max → lo, lo+1 → normalize returns 0.5-ish)
        # With single symbol, _min_max returns (val, val+1), _normalize returns ~0.5 for most
        # The key check: score matches the formula applied to the component values
        expected = (
            W_V2_LIQUIDITY * s.liquidity_score
            + W_V2_SPREAD * s.spread_score
            + W_V2_VOLATILITY * s.volatility_score
            + W_V2_RANGE * s.range_score
            + W_V2_EXEC_FIT * s.execution_fit_score
            - W_V2_TREND_PENALTY * s.trend_penalty
            - W_V2_TOXICITY_PENALTY * s.toxicity_penalty
        )
        assert abs(s.score - expected) < 1e-9

    def test_rank_v2_is_deterministic(self) -> None:
        """Two calls with same inputs produce identical ordering."""
        features = {
            "A": _feat_v2("A", volume="3000000", range_score=30, net_return_bps=20),
            "B": _feat_v2("B", volume="8000000", range_score=80, net_return_bps=5),
            "C": _feat_v2("C", volume="5000000", range_score=50, net_return_bps=50),
        }
        r1 = rank_v2(["A", "B", "C"], features)
        r2 = rank_v2(["A", "B", "C"], features)
        assert [s.symbol for s in r1] == [s.symbol for s in r2]

    def test_empty_input(self) -> None:
        assert rank_v2([], {}) == []

    def test_single_symbol(self) -> None:
        features = {"ONLY": _feat_v2("ONLY")}
        scored = rank_v2(["ONLY"], features)
        assert len(scored) == 1
        assert scored[0].symbol == "ONLY"

    def test_missing_features_excluded(self) -> None:
        """Symbol with no features is silently excluded."""
        scored = rank_v2(["A", "B"], {"A": _feat_v2("A")})
        assert len(scored) == 1
        assert scored[0].symbol == "A"

    def test_v2_differs_from_v1_on_mixed_set(self) -> None:
        """V2 should prefer choppy+liquid over pure-volume when V1 wouldn't."""
        # V1 features: BIGVOL has more volume, should win V1
        v1_features = {
            "BIGVOL": SelectionFeatures(
                symbol="BIGVOL",
                quote_volume_last_12x5m=Decimal("10000000"),
                best_bid=Decimal("1.0"), best_ask=Decimal("1.001"),
                natr_14_5m=Decimal("2.0"),
            ),
            "CHOPPY": SelectionFeatures(
                symbol="CHOPPY",
                quote_volume_last_12x5m=Decimal("4000000"),
                best_bid=Decimal("1.0"), best_ask=Decimal("1.001"),
                natr_14_5m=Decimal("2.0"),
            ),
        }
        v1_result = rank_v1(["BIGVOL", "CHOPPY"], v1_features)
        assert v1_result[0].symbol == "BIGVOL"  # V1 prefers volume

        # V2 features: CHOPPY has much higher range_score, BIGVOL is trending
        v2_features = {
            "BIGVOL": _feat_v2("BIGVOL", volume="10000000", range_score=5,
                               net_return_bps=150, toxicity_penalty_raw=0.3),
            "CHOPPY": _feat_v2("CHOPPY", volume="4000000", range_score=100,
                               net_return_bps=5, toxicity_penalty_raw=0.05),
        }
        v2_result = rank_v2(["BIGVOL", "CHOPPY"], v2_features)
        assert v2_result[0].symbol == "CHOPPY"  # V2 prefers choppy
