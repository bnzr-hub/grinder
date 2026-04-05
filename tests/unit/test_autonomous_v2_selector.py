"""Tests for V2 selector wiring into autonomous runtime."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from scripts.run_autonomous import _build_v2_selector

from grinder.selector.models import SelectionFeatures, SelectionFeaturesV2
from grinder.tuning.solver import TuningResult, TuningStatus


def _tuned(symbol: str, order_size: str = "10") -> TuningResult:
    return TuningResult(
        symbol=symbol,
        status=TuningStatus.TUNED,
        order_size=Decimal(order_size),
    )


def _v1_feat(symbol: str) -> SelectionFeatures:
    return SelectionFeatures(
        symbol=symbol,
        quote_volume_last_12x5m=Decimal("5000000"),
        best_bid=Decimal("1.0"),
        best_ask=Decimal("1.001"),
        natr_14_5m=Decimal("2.0"),
    )


def _v2_feat(
    symbol: str,
    *,
    range_score: int = 50,
    net_return_bps: int = 10,
    execution_fit_score: float = 0.8,
    toxicity_penalty_raw: float = 0.1,
) -> SelectionFeaturesV2:
    return SelectionFeaturesV2(
        symbol=symbol,
        quote_volume_last_12x5m=Decimal("5000000"),
        best_bid=Decimal("1.0"),
        best_ask=Decimal("1.001"),
        natr_14_5m=Decimal("2.0"),
        range_score=range_score,
        net_return_bps=net_return_bps,
        execution_fit_score=execution_fit_score,
        toxicity_penalty_raw=toxicity_penalty_raw,
    )


class TestBuildV2Selector:
    @patch("grinder.selector.feature_provider.fetch_selection_features_v2")
    @patch("grinder.selector.feature_provider.fetch_selection_features")
    def test_ranker_uses_rank_v2(
        self,
        mock_v1_fetch: MagicMock,
        mock_v2_fetch: MagicMock,
    ) -> None:
        """Ranker closure calls rank_v2, not rank_v1."""

        mock_v1_fetch.return_value = {"A": _v1_feat("A")}
        mock_v2_fetch.return_value = {"A": _v2_feat("A")}

        cache = MagicMock()
        cache.get.return_value = _tuned("A")

        _prefilter, ranker = _build_v2_selector({"A": _tuned("A")}, cache, frozenset(), True)

        result = ranker(["A"])
        assert isinstance(result, list)
        assert result == ["A"]

    @patch("grinder.selector.feature_provider.fetch_selection_features_v2")
    @patch("grinder.selector.feature_provider.fetch_selection_features")
    def test_ranker_returns_v2_scored_order(
        self,
        mock_v1_fetch: MagicMock,
        mock_v2_fetch: MagicMock,
    ) -> None:
        """V2 ranker returns choppy symbol first."""

        mock_v1_fetch.return_value = {
            "CHOPPY": _v1_feat("CHOPPY"),
            "TREND": _v1_feat("TREND"),
        }
        mock_v2_fetch.return_value = {
            "CHOPPY": _v2_feat("CHOPPY", range_score=100, net_return_bps=5),
            "TREND": _v2_feat("TREND", range_score=5, net_return_bps=200),
        }

        cache = MagicMock()
        cache.get.side_effect = _tuned

        _prefilter, ranker = _build_v2_selector(
            {"CHOPPY": _tuned("CHOPPY"), "TREND": _tuned("TREND")},
            cache,
            frozenset(),
            True,
        )

        result = ranker(["CHOPPY", "TREND"])
        assert result[0] == "CHOPPY"

    @patch("grinder.selector.feature_provider.fetch_selection_features_v2")
    @patch("grinder.selector.feature_provider.fetch_selection_features")
    def test_prefilter_returns_eligible_symbols(
        self,
        mock_v1_fetch: MagicMock,
        mock_v2_fetch: MagicMock,
    ) -> None:
        """Prefilter closure filters using V1 features (prefilter_v1 semantics)."""

        mock_v1_fetch.return_value = {
            "GOOD": _v1_feat("GOOD"),
            "LOWVOL": SelectionFeatures(
                symbol="LOWVOL",
                quote_volume_last_12x5m=Decimal("100"),  # below $2M floor
                best_bid=Decimal("1.0"),
                best_ask=Decimal("1.001"),
                natr_14_5m=Decimal("2.0"),
            ),
        }
        mock_v2_fetch.return_value = {}

        cache = MagicMock()
        cache.get.side_effect = lambda sym: _tuned(sym) if sym in ("GOOD", "LOWVOL") else None

        prefilter, _ranker = _build_v2_selector(
            {"GOOD": _tuned("GOOD"), "LOWVOL": _tuned("LOWVOL")},
            cache,
            frozenset(),
            True,
        )

        result = prefilter(["GOOD", "LOWVOL"])
        assert "GOOD" in result
        assert "LOWVOL" not in result  # filtered by prefilter_v1 volume floor

    @patch("grinder.selector.feature_provider.fetch_selection_features_v2")
    @patch("grinder.selector.feature_provider.fetch_selection_features")
    def test_execution_fit_forwarded(
        self,
        mock_v1_fetch: MagicMock,
        mock_v2_fetch: MagicMock,
    ) -> None:
        """Tuning order sizes and max_notional are forwarded to V2 fetch."""

        mock_v1_fetch.return_value = {}
        mock_v2_fetch.return_value = {}

        _build_v2_selector(
            {"BTC": _tuned("BTC", order_size="0.05")},
            MagicMock(),
            frozenset(),
            True,
            max_notional_per_order="100",
        )

        mock_v2_fetch.assert_called_once()
        call_kwargs = mock_v2_fetch.call_args
        assert call_kwargs.kwargs["tuning_order_sizes"] == {"BTC": Decimal("0.05")}
        assert call_kwargs.kwargs["max_notional_per_order"] == Decimal("100")

    @patch("grinder.selector.feature_provider.fetch_selection_features_v2")
    @patch("grinder.selector.feature_provider.fetch_selection_features")
    def test_empty_tuned_results_no_crash(
        self,
        mock_v1_fetch: MagicMock,
        mock_v2_fetch: MagicMock,
    ) -> None:
        """Empty tuned results produces working but empty closures."""

        prefilter, ranker = _build_v2_selector({}, MagicMock(), frozenset(), True)

        # Neither fetch should be called for empty symbols
        mock_v1_fetch.assert_not_called()
        mock_v2_fetch.assert_not_called()

        assert prefilter([]) == []
        assert ranker([]) == []

    @patch("grinder.selector.feature_provider.fetch_selection_features_v2")
    @patch("grinder.selector.feature_provider.fetch_selection_features")
    def test_v2_fetch_failure_falls_back_to_v1(
        self,
        mock_v1_fetch: MagicMock,
        mock_v2_fetch: MagicMock,
    ) -> None:
        """If V2 features fail, ranker falls back to V1 ranking (not empty)."""

        mock_v1_fetch.return_value = {"A": _v1_feat("A")}
        mock_v2_fetch.return_value = {}  # V2 fetch failed

        _prefilter, ranker = _build_v2_selector({"A": _tuned("A")}, MagicMock(), frozenset(), True)

        result = ranker(["A"])
        assert result == ["A"]  # V1 fallback produces ranking
