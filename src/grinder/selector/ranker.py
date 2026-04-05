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
    from grinder.selector.models import (
        ScoredSymbol,
        ScoredSymbolV2,
        SelectionFeatures,
        SelectionFeaturesV2,
    )

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


# ---------------------------------------------------------------------------
# V2 ranker — seven-component formula
# ---------------------------------------------------------------------------

# V2 weights
W_V2_LIQUIDITY = 0.25
W_V2_SPREAD = 0.15
W_V2_VOLATILITY = 0.20
W_V2_RANGE = 0.25
W_V2_EXEC_FIT = 0.15
W_V2_TREND_PENALTY = 0.15
W_V2_TOXICITY_PENALTY = 0.10


def rank_v2(
    symbols: list[str],
    features: dict[str, SelectionFeaturesV2],
) -> list[ScoredSymbolV2]:
    """Rank symbols by V2 composite score. Returns sorted highest-first.

    Score = 0.25L + 0.15S + 0.20V + 0.25R + 0.15E - 0.15T - 0.10X

    L = liquidity (log volume), S = spread (inverted), V = volatility (NATR),
    R = range_score (choppiness), E = execution_fit, T = trend_penalty
    (net_return_bps), X = toxicity_penalty (spread-based).
    """
    from grinder.selector.models import ScoredSymbolV2  # noqa: PLC0415

    if not symbols:
        return []

    # Gather raw metrics: (log_vol, spread, natr, range, exec_fit, net_return, toxicity)
    raw: dict[str, tuple[float, float, float, float, float, float, float]] = {}
    for sym in symbols:
        feat = features.get(sym)
        if feat is None:
            continue
        log_vol = math.log1p(float(feat.quote_volume_last_12x5m))
        spread = float(feat.spread_bps)
        natr = float(feat.natr_14_5m)
        rng = float(feat.range_score)
        exec_fit = feat.execution_fit_score
        net_ret = float(feat.net_return_bps)
        tox = feat.toxicity_penalty_raw
        raw[sym] = (log_vol, spread, natr, rng, exec_fit, net_ret, tox)

    if not raw:
        return []

    # Compute normalization ranges per dimension
    log_vols = [v[0] for v in raw.values()]
    spreads = [v[1] for v in raw.values()]
    natrs = [v[2] for v in raw.values()]
    ranges = [v[3] for v in raw.values()]
    exec_fits = [v[4] for v in raw.values()]
    net_rets = [v[5] for v in raw.values()]
    toxicities = [v[6] for v in raw.values()]

    vol_lo, vol_hi = _min_max(log_vols)
    spread_lo, spread_hi = _min_max(spreads)
    natr_lo, natr_hi = _min_max(natrs)
    range_lo, range_hi = _min_max(ranges)
    exec_lo, exec_hi = _min_max(exec_fits)
    net_ret_lo, net_ret_hi = _min_max(net_rets)
    tox_lo, tox_hi = _min_max(toxicities)

    scored: list[ScoredSymbolV2] = []
    for sym, (log_vol, spread, natr, rng, exec_fit, net_ret, tox) in raw.items():
        liquidity_n = _normalize(log_vol, vol_lo, vol_hi)
        spread_n = 1.0 - _normalize(spread, spread_lo, spread_hi)  # lower = better
        volatility_n = _normalize(natr, natr_lo, natr_hi)
        range_n = _normalize(rng, range_lo, range_hi)
        exec_n = _normalize(exec_fit, exec_lo, exec_hi)
        trend_n = _normalize(net_ret, net_ret_lo, net_ret_hi)  # higher = more trending = worse
        toxicity_n = _normalize(tox, tox_lo, tox_hi)  # higher = worse

        score = (
            W_V2_LIQUIDITY * liquidity_n
            + W_V2_SPREAD * spread_n
            + W_V2_VOLATILITY * volatility_n
            + W_V2_RANGE * range_n
            + W_V2_EXEC_FIT * exec_n
            - W_V2_TREND_PENALTY * trend_n
            - W_V2_TOXICITY_PENALTY * toxicity_n
        )

        scored.append(
            ScoredSymbolV2(
                symbol=sym,
                score=score,
                liquidity_score=liquidity_n,
                spread_score=spread_n,
                volatility_score=volatility_n,
                range_score=range_n,
                execution_fit_score=exec_n,
                trend_penalty=trend_n,
                toxicity_penalty=toxicity_n,
            )
        )

        logger.debug(
            "SELECTOR_SCORE_V2 symbol=%s score=%.4f liq=%.3f spread=%.3f vol=%.3f "
            "range=%.3f exec=%.3f trend=%.3f tox=%.3f",
            sym,
            score,
            liquidity_n,
            spread_n,
            volatility_n,
            range_n,
            exec_n,
            trend_n,
            toxicity_n,
        )

    scored.sort(key=lambda s: (-s.score, s.symbol))

    if scored:
        logger.info(
            "SELECTOR_TOP_V2 symbol=%s rank=1 score=%.4f",
            scored[0].symbol,
            scored[0].score,
        )

    return scored
