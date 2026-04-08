"""Adaptive grid spacing policy for symbol selection.

SSOT for computing grid spacing from market volatility (NATR).
Used by selector prefilter, tuning solver, refresher, and bridge.

Formula:
    spacing_bps = natr_percent * 30

Tradability rule:
    spacing_bps >= 50 bps (reject, not clamp)

Effective NATR gate:
    With multiplier=30 and min_spacing=50, symbols need NATR >= 1.67%
    to pass the spacing tradability check. This intentionally narrows
    the universe to sufficiently volatile symbols.

Examples:
    NATR 1.67% → 50 bps  (boundary — passes)
    NATR 2.0%  → 60 bps
    NATR 3.0%  → 90 bps
"""

from __future__ import annotations

from decimal import Decimal

SPACING_NATR_MULTIPLIER = Decimal("30")

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
