"""Portfolio-level regime adapter for autonomous risk planners.

Maps per-symbol regimes from the existing controller/regime.py model into
the allocator's GOOD/NEUTRAL/TOXIC buckets, then aggregates across the
relevant candidate set using worst-case precedence.

Does NOT invent new regime semantics — reuses the existing Regime enum
and its precedence hierarchy. The mapping is:

  RANGE                        → GOOD
  TREND_UP, TREND_DOWN         → NEUTRAL
  VOL_SHOCK, THIN_BOOK, TOXIC  → TOXIC
  PAUSED, EMERGENCY            → TOXIC

Portfolio aggregation (worst-case):
  any TOXIC symbol  → portfolio TOXIC
  any NEUTRAL       → portfolio NEUTRAL
  all GOOD          → portfolio GOOD
  empty set         → portfolio NEUTRAL (fail-open)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.controller.regime import Regime
    from grinder.risk.portfolio_budget_allocator import MarketRegime

logger = logging.getLogger(__name__)

# Mapping from per-symbol Regime → allocator MarketRegime bucket.
# Imported lazily to avoid circular imports at module scope.
_REGIME_MAP: dict[str, str] | None = None


def _get_regime_map() -> dict[str, str]:
    """Build regime → allocator-bucket mapping. Cached after first call."""
    global _REGIME_MAP  # noqa: PLW0603
    if _REGIME_MAP is not None:
        return _REGIME_MAP
    _REGIME_MAP = {
        "RANGE": "GOOD",
        "TREND_UP": "NEUTRAL",
        "TREND_DOWN": "NEUTRAL",
        "VOL_SHOCK": "TOXIC",
        "THIN_BOOK": "TOXIC",
        "TOXIC": "TOXIC",
        "PAUSED": "TOXIC",
        "EMERGENCY": "TOXIC",
    }
    return _REGIME_MAP


def map_symbol_regime(regime: Regime) -> MarketRegime:
    """Map a single per-symbol Regime to allocator MarketRegime bucket."""
    from grinder.risk.portfolio_budget_allocator import MarketRegime as MR  # noqa: PLC0415

    bucket = _get_regime_map().get(regime.value, "NEUTRAL")
    return MR(bucket)


def classify_from_v2_features(
    features: object,
    *,
    vol_shock_natr_pct: float = 5.0,
    trend_net_return_bps: int = 200,
    trend_range_score_max: int = 3,
    toxicity_threshold: float = 0.5,
) -> Regime:
    """Classify per-symbol regime from V2 selector features.

    Uses the same precedence rules as controller/regime.py but adapted
    to the fields available on SelectionFeaturesV2:
    - toxicity_penalty_raw > threshold → TOXIC
    - natr_14_5m > vol_shock threshold → VOL_SHOCK
    - abs(net_return_bps) > trend threshold AND range_score <= max → TREND
    - else → RANGE

    Thresholds match RegimeConfig defaults for consistency.
    """
    from grinder.controller.regime import Regime as R  # noqa: PLC0415

    # Toxicity check (maps to existing toxicity gate semantics)
    tox = getattr(features, "toxicity_penalty_raw", 0.0)
    if tox > toxicity_threshold:
        return R.TOXIC

    # Vol shock: natr_14_5m is in percent (e.g. 1.5 = 1.5%)
    natr_pct = float(getattr(features, "natr_14_5m", 0))
    if natr_pct > vol_shock_natr_pct:
        return R.VOL_SHOCK

    # Trend detection
    net_ret = getattr(features, "net_return_bps", 0)
    rscore = getattr(features, "range_score", 999)
    if abs(net_ret) > trend_net_return_bps and rscore <= trend_range_score_max:
        return R.TREND_UP if net_ret > 0 else R.TREND_DOWN

    return R.RANGE


def aggregate_portfolio_regime(
    symbol_regimes: dict[str, Regime],
) -> MarketRegime:
    """Aggregate per-symbol regimes into one portfolio-level MarketRegime.

    Worst-case precedence: TOXIC > NEUTRAL > GOOD.
    Empty input → NEUTRAL (fail-open).
    """
    from grinder.risk.portfolio_budget_allocator import MarketRegime as MR  # noqa: PLC0415

    if not symbol_regimes:
        return MR.NEUTRAL

    regime_map = _get_regime_map()
    has_toxic = False
    has_neutral = False

    for _sym, regime in symbol_regimes.items():
        bucket = regime_map.get(regime.value, "NEUTRAL")
        if bucket == "TOXIC":
            has_toxic = True
            break  # worst case reached, no need to continue
        if bucket == "NEUTRAL":
            has_neutral = True

    if has_toxic:
        return MR.TOXIC
    if has_neutral:
        return MR.NEUTRAL
    return MR.GOOD
