"""V1 market-aware ranker for symbol selection.

Score = 0.45 * volume_score + 0.25 * spread_score + 0.30 * volatility_score

Normalization: min-max within current candidate set.
Volume uses log scale to prevent one monster symbol from dominating.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.selector.models import ScoredSymbol, SelectionFeatures

logger = logging.getLogger(__name__)

# V1 weights
W_VOLUME = 0.45
W_SPREAD = 0.25
W_VOLATILITY = 0.30


def _min_max(values: list[float]) -> tuple[float, float]:
    """Return (min, max) or (0, 1) if empty/single."""
    if not values:
        return 0.0, 1.0
    lo, hi = min(values), max(values)
    if lo == hi:
        return lo, lo + 1.0  # avoid division by zero
    return lo, hi


def _normalize(value: float, lo: float, hi: float) -> float:
    """Normalize value to [0, 1] range."""
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def rank_v1(
    symbols: list[str],
    features: dict[str, SelectionFeatures],
) -> list[ScoredSymbol]:
    """Rank symbols by V1 composite score. Returns sorted highest-first."""
    from grinder.selector.models import ScoredSymbol  # noqa: PLC0415

    if not symbols:
        return []

    # Gather raw metrics
    raw: dict[str, tuple[float, float, float]] = {}
    for sym in symbols:
        feat = features.get(sym)
        if feat is None:
            continue
        vol = float(feat.quote_volume_last_12x5m)
        log_vol = math.log1p(vol)  # log(1+vol) for scale
        spread = float(feat.spread_bps)
        natr = float(feat.natr_14_5m)
        raw[sym] = (log_vol, spread, natr)

    if not raw:
        return []

    # Compute normalization ranges
    log_vols = [v[0] for v in raw.values()]
    spreads = [v[1] for v in raw.values()]
    natrs = [v[2] for v in raw.values()]

    vol_lo, vol_hi = _min_max(log_vols)
    spread_lo, spread_hi = _min_max(spreads)
    natr_lo, natr_hi = _min_max(natrs)

    # Score each symbol
    scored: list[ScoredSymbol] = []
    for sym, (log_vol, spread, natr) in raw.items():
        volume_score = _normalize(log_vol, vol_lo, vol_hi)
        # Spread: lower is better → invert
        spread_score = 1.0 - _normalize(spread, spread_lo, spread_hi)
        volatility_score = _normalize(natr, natr_lo, natr_hi)

        score = W_VOLUME * volume_score + W_SPREAD * spread_score + W_VOLATILITY * volatility_score

        scored.append(
            ScoredSymbol(
                symbol=sym,
                score=score,
                volume_score=volume_score,
                spread_score=spread_score,
                volatility_score=volatility_score,
            )
        )

        logger.debug(
            "SELECTOR_SCORE symbol=%s score=%.4f vol=%.3f spread=%.3f natr=%.3f",
            sym,
            score,
            volume_score,
            spread_score,
            volatility_score,
        )

    # Sort by score descending, stable tie-break by symbol name
    scored.sort(key=lambda s: (-s.score, s.symbol))

    if scored:
        logger.info(
            "SELECTOR_TOP symbol=%s rank=1 score=%.4f",
            scored[0].symbol,
            scored[0].score,
        )

    return scored
