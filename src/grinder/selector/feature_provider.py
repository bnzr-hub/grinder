"""Fetch market-data features for V1 symbol selection.

Uses Binance Futures REST endpoints:
- /fapi/v1/klines for 5m volume and NATR
- /fapi/v1/ticker/bookTicker for spread
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from grinder.selector.models import SelectionFeatures

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")

# Minimum closed candles required (15 for NATR(14) which needs 14 TRs + 1 prev close;
# also guarantees >= 12 for volume since 15 > 12).
_MIN_CLOSED_CANDLES = 15


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
    # Single 5m kline fetch for both volume and NATR
    klines = _fetch_5m_klines(symbol, base, timeout)
    if klines is None:
        return None

    # Exclude current unclosed candle
    closed = klines[:-1]
    if len(closed) < _MIN_CLOSED_CANDLES:
        return None

    # Rolling volume from last 12 closed 5m candles
    volume = _compute_12x5m_volume(closed)
    if volume is None:
        return None

    # NATR(14) from closed candles
    natr = _compute_natr(closed, period=14)
    if natr is None:
        return None

    # Book ticker for spread
    bid, ask = _fetch_book_ticker(symbol, base, timeout)
    if bid is None or ask is None:
        return None

    return SelectionFeatures(
        symbol=symbol,
        quote_volume_last_12x5m=volume,
        best_bid=bid,
        best_ask=ask,
        natr_14_5m=natr,
    )


def _fetch_5m_klines(symbol: str, base: str, timeout: int) -> list[Any] | None:
    """Fetch 5m klines. Returns raw kline list or None on failure.

    Requests limit=18 to guarantee at least 17 closed candles after
    excluding the current unclosed one — enough for both:
    - rolling volume (last 12 closed)
    - NATR(14) (needs 15 closed: 14 TRs + 1 prev close)
    """
    import requests  # noqa: PLC0415

    try:
        resp = requests.get(
            f"{base}/fapi/v1/klines",
            params={"symbol": symbol, "interval": "5m", "limit": 18},
            timeout=timeout,
        )
        data: list[Any] = resp.json()
        if not data or len(data) < _MIN_CLOSED_CANDLES + 1:
            return None
        return data
    except (Exception, InvalidOperation):
        return None


def _compute_12x5m_volume(closed_klines: list[Any]) -> Decimal | None:
    """Compute rolling volume as sum of last 12 closed 5m candle quote volumes.

    Args:
        closed_klines: list of closed klines (current unclosed already excluded).

    Returns:
        Sum of quoteAssetVolume (index 7) for the last 12 candles, or None if
        fewer than 12 closed candles available.
    """
    if len(closed_klines) < 12:
        return None
    last_12 = closed_klines[-12:]
    try:
        return sum((Decimal(str(k[7])) for k in last_12), _ZERO)
    except (InvalidOperation, IndexError, TypeError):
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
