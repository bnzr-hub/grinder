"""Fetch market-data features for V1 symbol selection.

Uses Binance Futures REST endpoints:
- /fapi/v1/klines for 1h volume and 5m NATR
- /fapi/v1/ticker/bookTicker for spread
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from grinder.selector.models import SelectionFeatures

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")


def fetch_selection_features(
    symbols: list[str],
    *,
    mainnet: bool = True,
    timeout: int = 5,
) -> dict[str, SelectionFeatures]:
    """Fetch V1 selection features for a list of symbols.

    Fail-open per symbol: if any single fetch fails, that symbol is
    excluded from the result (no features → prefilter will skip it).
    """
    base = "https://fapi.binance.com" if mainnet else "https://testnet.binancefuture.com"
    result: dict[str, SelectionFeatures] = {}

    for symbol in symbols:
        try:
            feat = _fetch_one(symbol, base, timeout)
            if feat is not None:
                result[symbol] = feat
        except Exception:
            logger.debug("SELECTOR_FEATURE_FETCH_FAILED symbol=%s", symbol, exc_info=True)

    logger.info("SELECTOR_FEATURES_FETCHED count=%d total=%d", len(result), len(symbols))
    return result


def _fetch_one(symbol: str, base: str, timeout: int) -> SelectionFeatures | None:
    """Fetch features for one symbol. Returns None on failure."""
    # 1h kline for volume
    volume_1h = _fetch_1h_volume(symbol, base, timeout)
    if volume_1h is None:
        return None

    # Book ticker for spread
    bid, ask = _fetch_book_ticker(symbol, base, timeout)
    if bid is None or ask is None:
        return None

    # 5m klines for NATR(14)
    natr = _fetch_natr_5m(symbol, base, timeout)
    if natr is None:
        return None

    return SelectionFeatures(
        symbol=symbol,
        quote_volume_1h=volume_1h,
        best_bid=bid,
        best_ask=ask,
        natr_14_5m=natr,
    )


def _fetch_1h_volume(symbol: str, base: str, timeout: int) -> Decimal | None:
    """Fetch 1h quote volume from latest closed kline."""
    import requests  # noqa: PLC0415

    try:
        resp = requests.get(
            f"{base}/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 2},
            timeout=timeout,
        )
        data = resp.json()
        if not data or len(data) < 2:
            return None
        # Use second-to-last kline (latest closed)
        return Decimal(str(data[-2][7]))  # quoteAssetVolume
    except (Exception, InvalidOperation):
        return None


def _fetch_book_ticker(
    symbol: str, base: str, timeout: int
) -> tuple[Decimal | None, Decimal | None]:
    """Fetch best bid/ask from book ticker."""
    import requests  # noqa: PLC0415

    try:
        resp = requests.get(
            f"{base}/fapi/v1/ticker/bookTicker",
            params={"symbol": symbol},
            timeout=timeout,
        )
        data = resp.json()
        bid = Decimal(str(data["bidPrice"]))
        ask = Decimal(str(data["askPrice"]))
        return bid, ask
    except (Exception, InvalidOperation):
        return None, None


def _fetch_natr_5m(symbol: str, base: str, timeout: int) -> Decimal | None:
    """Compute NATR(14) from 5m klines."""
    import requests  # noqa: PLC0415

    try:
        resp = requests.get(
            f"{base}/fapi/v1/klines",
            params={"symbol": symbol, "interval": "5m", "limit": 16},
            timeout=timeout,
        )
        data = resp.json()
        if not data or len(data) < 15:
            return None
        return _compute_natr(data[:-1], period=14)  # exclude current unclosed
    except (Exception, InvalidOperation):
        return None


def _compute_natr(klines: list[Any], period: int = 14) -> Decimal | None:
    """Compute NATR from kline data. Returns percentage (e.g. 1.5 = 1.5%)."""
    if len(klines) < period:
        return None

    trs: list[Decimal] = []
    for i in range(1, len(klines)):
        high = Decimal(str(klines[i][2]))
        low = Decimal(str(klines[i][3]))
        prev_close = Decimal(str(klines[i - 1][4]))
        diff_hc = high - prev_close
        diff_lc = low - prev_close
        tr = max(
            high - low, diff_hc if diff_hc >= 0 else -diff_hc, diff_lc if diff_lc >= 0 else -diff_lc
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    # Simple moving average of last `period` TRs
    atr = sum(trs[-period:], _ZERO) / period
    close = Decimal(str(klines[-1][4]))
    if close <= 0:
        return None

    natr = (atr / close) * 100  # percentage
    return natr
