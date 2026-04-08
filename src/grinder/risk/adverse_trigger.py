"""Adverse grid level trigger computation.

Computes the price threshold at which a given adverse level is breached,
using the same grid geometry as the live grid_v2 state machine.

Per spec docs/41_AUTONOMOUS_RISK_MANAGER_V1_SPEC.md §20.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal


def compute_adverse_threshold(
    reference_price: Decimal,
    step_pct: Decimal,
    tick_size: Decimal,
    adverse_level: int,
    side: str,
) -> Decimal | None:
    """Compute the price at which the Nth adverse grid level is breached.

    Uses the same geometry as grid_v2 state machine:
    - anchor = round(reference_price / tick, ROUND_HALF_UP) * tick
    - step_price = ceil(anchor * step_pct / tick) * tick (min 1 tick)
    - threshold = anchor ± step_price * adverse_level

    Args:
        reference_price: Grid anchor/reference price.
        step_pct: Grid step as fraction (e.g. 0.01 = 1%).
        tick_size: Exchange tick size for price quantization.
        adverse_level: Target adverse level (e.g. 16).
        side: "LONG" or "SHORT" — the current branch direction.
            LONG branch → adverse is price DOWN.
            SHORT branch → adverse is price UP.

    Returns:
        Threshold price, or None if inputs invalid.
    """
    if reference_price <= 0 or step_pct <= 0 or tick_size <= 0 or adverse_level < 1:
        return None

    # Same math as grid_v2/state.py _build_entry_window + _grid_step_price.
    # Step price uses reference_price (not anchor) — matches _grid_step_price().
    # Anchor rounding is only for the centerline.
    anchor = (reference_price / tick_size).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ) * tick_size
    raw_step = reference_price * step_pct
    step_ticks = (raw_step / tick_size).quantize(Decimal("1"), rounding=ROUND_CEILING)
    if step_ticks < 1:
        step_ticks = Decimal("1")
    step_price = step_ticks * tick_size

    if side == "LONG":
        # LONG branch: adverse = price moves DOWN
        return anchor - step_price * adverse_level
    if side == "SHORT":
        # SHORT branch: adverse = price moves UP
        return anchor + step_price * adverse_level
    return None


def is_adverse_level_breached(
    current_price: Decimal,
    threshold: Decimal,
    side: str,
) -> bool:
    """Check if current price has breached the adverse threshold.

    Args:
        current_price: Current market mid price.
        threshold: Computed adverse threshold from compute_adverse_threshold.
        side: "LONG" or "SHORT" — the current branch direction.

    Returns:
        True if adverse level is breached.
    """
    if side == "LONG":
        return current_price <= threshold
    if side == "SHORT":
        return current_price >= threshold
    return False
