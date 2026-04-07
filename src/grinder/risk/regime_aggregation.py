"""Portfolio-level regime aggregation from live engine RegimeDecisions.

Consumes SharedRegimeRegistry snapshots and produces one portfolio-level
MarketRegime for PortfolioBudgetAllocator. No heuristics — maps and
aggregates the real controller/regime.py output from engine threads.

Mapping (engine Regime → allocator MarketRegime):
  RANGE                        → GOOD
  TREND_UP, TREND_DOWN         → NEUTRAL
  VOL_SHOCK, THIN_BOOK, TOXIC  → TOXIC
  PAUSED, EMERGENCY            → TOXIC

Aggregation (worst-case precedence):
  any TOXIC  → portfolio TOXIC
  any NEUTRAL → portfolio NEUTRAL
  all GOOD   → portfolio GOOD
  empty      → NEUTRAL (fail-open)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from grinder.controller.regime import Regime
    from grinder.risk.portfolio_budget_allocator import MarketRegime


def map_engine_regime(regime: Regime) -> MarketRegime:
    """Map one engine Regime to allocator MarketRegime bucket.

    Explicit mapping — no default fallthrough. Every Regime value is handled.
    """
    from grinder.controller.regime import Regime as R  # noqa: PLC0415
    from grinder.risk.portfolio_budget_allocator import MarketRegime as MR  # noqa: PLC0415

    _MAP = {
        R.RANGE: MR.GOOD,
        R.TREND_UP: MR.NEUTRAL,
        R.TREND_DOWN: MR.NEUTRAL,
        R.VOL_SHOCK: MR.TOXIC,
        R.THIN_BOOK: MR.TOXIC,
        R.TOXIC: MR.TOXIC,
        R.PAUSED: MR.TOXIC,
        R.EMERGENCY: MR.TOXIC,
    }
    try:
        return _MAP[regime]
    except KeyError:
        raise ValueError(f"Unmapped Regime value: {regime!r}. Update _MAP.") from None


def aggregate_portfolio_regime(regimes: Iterable[Regime]) -> MarketRegime:
    """Aggregate per-symbol engine regimes into one portfolio MarketRegime.

    Worst-case precedence: TOXIC > NEUTRAL > GOOD.
    Empty input → NEUTRAL (fail-open).
    """
    from grinder.risk.portfolio_budget_allocator import MarketRegime as MR  # noqa: PLC0415

    has_neutral = False
    has_any = False

    for regime in regimes:
        has_any = True
        bucket = map_engine_regime(regime)
        if bucket == MR.TOXIC:
            return MR.TOXIC  # short-circuit — worst case reached
        if bucket == MR.NEUTRAL:
            has_neutral = True

    if not has_any:
        return MR.NEUTRAL

    return MR.NEUTRAL if has_neutral else MR.GOOD
