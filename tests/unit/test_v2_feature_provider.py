"""Tests for V2 feature provider: range/trend, execution fit, toxicity."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from grinder.features.bar import MidBar
from grinder.features.indicators import compute_range_trend
from grinder.selector.feature_provider import (
    _compute_execution_fit,
    _compute_range_trend_klines,
    _compute_toxicity_penalty_raw,
    fetch_selection_features_v2,
)


def _make_kline(*, close: str = "100", high: str = "105", low: str = "95") -> list[Any]:
    """Build a synthetic kline row matching Binance kline format."""
    return [
        1700000000000,  # 0: openTime
        "100",  # 1: open
        high,  # 2: high
        low,  # 3: low
        close,  # 4: close
        "100",  # 5: volume
        1700000300000,  # 6: closeTime
        "1000000",  # 7: quoteAssetVolume
        50,  # 8: count
        "50",  # 9: takerBuyBase
        "5000",  # 10: takerBuyQuote
        "0",  # 11: ignore
    ]


# --- _compute_range_trend_klines tests ---


class TestComputeRangeTrendKlines:
    def test_returns_tuple_of_three_ints(self) -> None:
        """Returns (sum_abs_bps, net_return_bps, range_score) as ints."""
        klines = [_make_kline(close=str(100 + i % 3)) for i in range(16)]
        result = _compute_range_trend_klines(klines, horizon=14)
        assert result is not None
        sum_abs, net_ret, rng = result
        assert isinstance(sum_abs, int)
        assert isinstance(net_ret, int)
        assert isinstance(rng, int)

    def test_choppy_data_high_range_score(self) -> None:
        """Alternating prices should yield high range_score (choppy)."""
        closes = [100, 102, 98, 103, 97, 101, 99, 102, 98, 101, 99, 103, 97, 100, 101, 100]
        klines = [_make_kline(close=str(c)) for c in closes]
        result = _compute_range_trend_klines(klines, horizon=14)
        assert result is not None
        _, _, range_score = result
        assert range_score > 5  # high choppiness

    def test_trending_data_low_range_score(self) -> None:
        """Monotonically increasing prices should yield low range_score."""
        closes = [100 + i * 2 for i in range(16)]  # 100, 102, 104, ...
        klines = [_make_kline(close=str(c)) for c in closes]
        result = _compute_range_trend_klines(klines, horizon=14)
        assert result is not None
        _, net_ret_bps, range_score = result
        assert net_ret_bps > 0
        assert range_score <= 2  # very trend-like: sum_abs ≈ net_ret

    def test_insufficient_data_returns_none(self) -> None:
        """Returns None with fewer than horizon+1 klines."""
        klines = [_make_kline() for _ in range(14)]  # need 15
        assert _compute_range_trend_klines(klines, horizon=14) is None

    def test_deterministic(self) -> None:
        """Same input → same output."""
        klines = [_make_kline(close=str(100 + i % 5)) for i in range(16)]
        r1 = _compute_range_trend_klines(klines, horizon=14)
        r2 = _compute_range_trend_klines(klines, horizon=14)
        assert r1 == r2

    def test_parity_with_indicators(self) -> None:
        """Result matches compute_range_trend() from indicators.py for equivalent data."""
        closes = [100, 103, 97, 105, 95, 102, 98, 101, 99, 104, 96, 100, 102, 98, 101, 100]
        # Build klines
        klines = [_make_kline(close=str(c), high=str(c + 3), low=str(c - 3)) for c in closes]
        # Build MidBars
        bars = [
            MidBar(
                bar_ts=i,
                open=Decimal(str(c)),
                high=Decimal(str(c + 3)),
                low=Decimal(str(c - 3)),
                close=Decimal(str(c)),
                tick_count=10,
            )
            for i, c in enumerate(closes)
        ]

        kline_result = _compute_range_trend_klines(klines, horizon=14)
        bar_result = compute_range_trend(bars, horizon=14)

        assert kline_result is not None
        assert kline_result == bar_result


# --- _compute_execution_fit tests ---


class TestExecutionFit:
    def test_fit_at_half_cap(self) -> None:
        """Order at 50% of cap → execution_fit ≈ 0.5."""
        # order_size=50, mid=1.0 → notional=50, cap=100 → fit=0.5
        result = _compute_execution_fit(Decimal("50"), Decimal("1.0"), Decimal("100"))
        assert abs(result - 0.5) < 1e-9

    def test_fit_none_cap(self) -> None:
        """No cap → execution_fit = 1.0."""
        result = _compute_execution_fit(Decimal("100"), Decimal("1.0"), None)
        assert result == 1.0

    def test_fit_none_order_size(self) -> None:
        """No order_size → execution_fit = 1.0."""
        result = _compute_execution_fit(None, Decimal("1.0"), Decimal("100"))
        assert result == 1.0

    def test_fit_at_cap_boundary(self) -> None:
        """Order exactly at cap → execution_fit = 0.0."""
        result = _compute_execution_fit(Decimal("100"), Decimal("1.0"), Decimal("100"))
        assert result == 0.0

    def test_fit_over_cap_clamps_to_zero(self) -> None:
        """Order above cap → clamped to 0.0."""
        result = _compute_execution_fit(Decimal("200"), Decimal("1.0"), Decimal("100"))
        assert result == 0.0

    def test_fit_tiny_order(self) -> None:
        """Very small order relative to cap → near 1.0."""
        result = _compute_execution_fit(Decimal("1"), Decimal("1.0"), Decimal("1000"))
        assert result > 0.99


# --- _compute_toxicity_penalty_raw tests ---


class TestToxicityPenaltyRaw:
    def test_tight_spread_low_penalty(self) -> None:
        result = _compute_toxicity_penalty_raw(2.0, 50)
        assert abs(result - 0.04) < 1e-9  # 2/50

    def test_wide_spread_high_penalty(self) -> None:
        result = _compute_toxicity_penalty_raw(50.0, 50)
        assert result == 1.0

    def test_very_wide_spread_clamped(self) -> None:
        result = _compute_toxicity_penalty_raw(100.0, 50)
        assert result == 1.0  # clamped

    def test_zero_cap_returns_zero(self) -> None:
        result = _compute_toxicity_penalty_raw(10.0, 0)
        assert result == 0.0


# --- fetch_selection_features_v2 tests ---


class TestFetchSelectionFeaturesV2:
    @patch("grinder.selector.feature_provider._fetch_one_v2")
    def test_fail_open_on_exception(self, mock_fetch: MagicMock) -> None:
        mock_fetch.side_effect = ConnectionError("network down")
        result = fetch_selection_features_v2(["BTCUSDT"])
        assert result == {}

    @patch("grinder.selector.feature_provider._fetch_one_v2")
    def test_fail_open_on_none(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = None
        result = fetch_selection_features_v2(["BTCUSDT"])
        assert result == {}
