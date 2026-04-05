"""Tests for V1 feature provider: rolling 12x5m volume + NATR from unified 5m kline fetch."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grinder.selector.feature_provider import (
    _compute_12x5m_volume,
    _compute_natr,
    _fetch_5m_klines,
    _fetch_one,
    fetch_selection_features,
)


def _make_kline(
    *,
    high: str = "105",
    low: str = "95",
    close: str = "100",
    quote_volume: str = "1000000",
) -> list[Any]:
    """Build a synthetic kline row matching Binance kline format.

    Index mapping:
    0=openTime, 1=open, 2=high, 3=low, 4=close, 5=volume,
    6=closeTime, 7=quoteAssetVolume, 8=count, 9=takerBuyBase,
    10=takerBuyQuote, 11=ignore
    """
    return [
        1700000000000,  # 0: openTime
        "100",  # 1: open
        high,  # 2: high
        low,  # 3: low
        close,  # 4: close
        "100",  # 5: volume
        1700000300000,  # 6: closeTime
        quote_volume,  # 7: quoteAssetVolume
        50,  # 8: count
        "50",  # 9: takerBuyBaseAssetVolume
        "5000",  # 10: takerBuyQuoteAssetVolume
        "0",  # 11: ignore
    ]


def _make_klines(n: int, *, quote_volume: str = "1000000") -> list[list[Any]]:
    """Build n klines with specified quote volume."""
    return [_make_kline(quote_volume=quote_volume) for _ in range(n)]


# --- _compute_12x5m_volume tests ---


class TestCompute12x5mVolume:
    def test_sums_last_12_closed_candles(self) -> None:
        """Volume is exact sum of last 12 candles' quoteAssetVolume."""
        closed = _make_klines(17, quote_volume="1000000")
        result = _compute_12x5m_volume(closed)
        assert result == Decimal("12000000")

    def test_exact_12_candles(self) -> None:
        """Works with exactly 12 closed candles."""
        closed = _make_klines(12, quote_volume="500000")
        result = _compute_12x5m_volume(closed)
        assert result == Decimal("6000000")

    def test_fewer_than_12_returns_none(self) -> None:
        """Returns None if fewer than 12 closed candles."""
        closed = _make_klines(11)
        assert _compute_12x5m_volume(closed) is None

    def test_uses_last_12_not_first_12(self) -> None:
        """When more than 12 candles, uses the LAST 12."""
        # First 5 candles with volume=999, last 12 with volume=1000000
        early = _make_klines(5, quote_volume="999")
        recent = _make_klines(12, quote_volume="1000000")
        closed = early + recent
        result = _compute_12x5m_volume(closed)
        assert result == Decimal("12000000")

    def test_decimal_precision(self) -> None:
        """Quote volumes with decimals are summed precisely."""
        closed = [_make_kline(quote_volume="123456.789") for _ in range(12)]
        result = _compute_12x5m_volume(closed)
        assert result == Decimal("123456.789") * 12


# --- Unclosed candle exclusion tests ---


class TestUnclosedCandleExclusion:
    def test_unclosed_candle_excluded_from_volume(self) -> None:
        """The current unclosed candle (last element) must not be included in volume."""
        # 18 klines: 17 closed + 1 unclosed
        closed_candles = _make_klines(17, quote_volume="1000000")
        unclosed = _make_kline(quote_volume="9999999")
        all_klines = closed_candles + [unclosed]

        # Simulate what _fetch_one does: closed = data[:-1]
        closed = all_klines[:-1]
        volume = _compute_12x5m_volume(closed)
        # Should be 12 * 1M = 12M, NOT including the 9999999 candle
        assert volume == Decimal("12000000")

    def test_unclosed_candle_excluded_from_natr(self) -> None:
        """NATR computation uses only closed candles."""
        closed = _make_klines(15, quote_volume="1000000")
        natr_from_closed = _compute_natr(closed, period=14)

        # Adding an unclosed candle should not change NATR when we slice properly
        unclosed = _make_kline(high="999", low="1", close="500")
        all_klines = closed + [unclosed]
        natr_with_unclosed_sliced = _compute_natr(all_klines[:-1], period=14)

        assert natr_from_closed == natr_with_unclosed_sliced


# --- _compute_natr tests ---


class TestComputeNatr:
    def test_returns_percentage(self) -> None:
        """NATR returns a percentage value > 0 for normal data."""
        klines = _make_klines(15)
        result = _compute_natr(klines, period=14)
        assert result is not None
        assert result > Decimal("0")

    def test_insufficient_data_returns_none(self) -> None:
        """Returns None with fewer than period candles."""
        klines = _make_klines(13)
        assert _compute_natr(klines, period=14) is None

    def test_natr_unchanged_with_same_input(self) -> None:
        """NATR is deterministic: same input → same output."""
        klines = _make_klines(17)
        r1 = _compute_natr(klines, period=14)
        r2 = _compute_natr(klines, period=14)
        assert r1 == r2


# --- _fetch_5m_klines tests ---


class TestFetch5mKlines:
    @patch("requests.get")
    def test_uses_5m_interval_and_limit_18(self, mock_get: MagicMock) -> None:
        """Fetches with interval=5m and limit=18."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _make_klines(18)
        mock_get.return_value = mock_resp

        result = _fetch_5m_klines("BTCUSDT", "https://fapi.binance.com", 5)

        mock_get.assert_called_once_with(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": 18},
            timeout=5,
        )
        assert result is not None
        assert len(result) == 18

    @patch("requests.get")
    def test_returns_none_on_insufficient_data(self, mock_get: MagicMock) -> None:
        """Returns None if fewer than 16 klines returned."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _make_klines(14)
        mock_get.return_value = mock_resp

        assert _fetch_5m_klines("BTCUSDT", "https://fapi.binance.com", 5) is None

    @patch("requests.get")
    def test_returns_none_on_exception(self, mock_get: MagicMock) -> None:
        """Returns None on network error."""
        mock_get.side_effect = ConnectionError("timeout")
        assert _fetch_5m_klines("BTCUSDT", "https://fapi.binance.com", 5) is None


# --- _fetch_one integration tests ---


class TestFetchOne:
    @patch("grinder.selector.feature_provider._fetch_book_ticker")
    @patch("grinder.selector.feature_provider._fetch_5m_klines")
    def test_returns_features_with_new_volume_field(
        self, mock_klines: MagicMock, mock_book: MagicMock
    ) -> None:
        """_fetch_one returns SelectionFeatures with quote_volume_last_12x5m."""
        mock_klines.return_value = _make_klines(18, quote_volume="2000000")
        mock_book.return_value = (Decimal("100"), Decimal("100.01"))

        feat = _fetch_one("BTCUSDT", "https://fapi.binance.com", 5)

        assert feat is not None
        assert feat.quote_volume_last_12x5m == Decimal("24000000")  # 12 * 2M
        assert feat.natr_14_5m is not None
        assert feat.best_bid == Decimal("100")

    @patch("grinder.selector.feature_provider._fetch_book_ticker")
    @patch("grinder.selector.feature_provider._fetch_5m_klines")
    def test_returns_none_on_kline_failure(
        self, mock_klines: MagicMock, mock_book: MagicMock
    ) -> None:
        mock_klines.return_value = None
        assert _fetch_one("BTCUSDT", "https://fapi.binance.com", 5) is None

    @patch("grinder.selector.feature_provider._fetch_book_ticker")
    @patch("grinder.selector.feature_provider._fetch_5m_klines")
    def test_returns_none_on_insufficient_closed(
        self, mock_klines: MagicMock, mock_book: MagicMock
    ) -> None:
        """Returns None when closed candles < 15 after excluding unclosed."""
        # 15 total → 14 closed → below _MIN_CLOSED_CANDLES
        mock_klines.return_value = _make_klines(15)
        assert _fetch_one("BTCUSDT", "https://fapi.binance.com", 5) is None


# --- fetch_selection_features (top-level) tests ---


class TestFetchSelectionFeatures:
    @patch("grinder.selector.feature_provider._fetch_one")
    def test_fail_open_on_exception(self, mock_fetch_one: MagicMock) -> None:
        """If _fetch_one raises, symbol is excluded, no exception propagated."""
        mock_fetch_one.side_effect = ConnectionError("network down")
        result = fetch_selection_features(["BTCUSDT"])
        assert result == {}

    @patch("grinder.selector.feature_provider._fetch_one")
    def test_fail_open_on_none(self, mock_fetch_one: MagicMock) -> None:
        """If _fetch_one returns None, symbol is excluded."""
        mock_fetch_one.return_value = None
        result = fetch_selection_features(["BTCUSDT"])
        assert result == {}
