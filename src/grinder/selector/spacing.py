"""Adaptive grid spacing policy for symbol selection.

SSOT for computing grid spacing from market volatility (NATR).
Used by selector prefilter and (in future PR B) by tuning/bridge.

Formula:
    spacing_bps = natr_percent * 50

Tradability rule:
    spacing_bps >= 50 bps (reject, not clamp)

Examples:
    NATR 1.0% → 50 bps  (boundary — passes)
    NATR 2.0% → 100 bps
    NATR 3.0% → 150 bps
"""

from __future__ import annotations

from decimal import Decimal

SPACING_NATR_MULTIPLIER = Decimal("50")

# Minimum adaptive spacing for grid tradability
DEFAULT_MIN_SPACING_BPS = Decimal("50")


def compute_adaptive_spacing_bps(natr_percent: Decimal) -> Decimal:
    """Compute adaptive grid spacing from NATR.

    Args:
        natr_percent: NATR(14) on 5m candles as percentage (e.g. 1.5 = 1.5%).

    Returns:
        Spacing in basis points.
    """
    return natr_percent * SPACING_NATR_MULTIPLIER
