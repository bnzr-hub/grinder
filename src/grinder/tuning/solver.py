"""Deterministic symbol tuning solver (PR-B2, ADR-124).

Given symbol constraints, current price, and risk/grid config, computes a
legal grid_v2 order size or declares NO_GO with reason.

Pure computation — no exchange I/O, no state mutation, no runtime wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.execution.engine import SymbolConstraints


class TuningStatus(Enum):
    """Outcome of tuning solver."""

    TUNED = "TUNED"
    NO_GO = "NO_GO"


class NoGoReason(Enum):
    """Reason why a symbol cannot be legally traded."""

    NOTIONAL_TOO_LOW = "NOTIONAL_TOO_LOW"
    POSITION_EXCEEDS_CAP = "POSITION_EXCEEDS_CAP"
    TICK_SIZE_UNAVAILABLE = "TICK_SIZE_UNAVAILABLE"
    STEP_SIZE_UNAVAILABLE = "STEP_SIZE_UNAVAILABLE"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    BLACKLISTED = "BLACKLISTED"


@dataclass(frozen=True)
class TuningResult:
    """Result of the tuning solver for one symbol.

    Attributes:
        symbol: Trading pair (e.g. "BTCUSDT").
        status: TUNED or NO_GO.
        reason: If NO_GO, the specific reason. None if TUNED.
        order_size: Legal per-order quantity if TUNED. None if NO_GO.
        tick_size: Carried through from constraints for downstream use.
        step_size: Carried through from constraints for downstream use.
        max_inventory_levels: Carried through from inputs for downstream use.
    """

    symbol: str
    status: TuningStatus
    reason: NoGoReason | None = None
    order_size: Decimal | None = None
    tick_size: Decimal | None = None
    step_size: Decimal | None = None
    max_inventory_levels: int | None = None


@dataclass(frozen=True)
class TuningSolverConfig:
    """Inputs controlling the tuning solve.

    Attributes:
        max_position_usd: Maximum total position value in quote asset.
        max_inventory_levels: Maximum number of inventory levels.
        entry_levels_per_side: Number of entry levels per side of the grid.
        spacing_pct: Grid spacing as a fraction (e.g., 0.0025 = 25 bps).
        blacklist: Symbols that must be rejected unconditionally.
    """

    max_position_usd: Decimal = Decimal("1000")
    max_inventory_levels: int = 5
    entry_levels_per_side: int = 5
    spacing_pct: Decimal = Decimal("0.0025")
    blacklist: frozenset[str] = frozenset()


def ceil_to_step(qty: Decimal, step_size: Decimal) -> Decimal:
    """Ceil quantity to nearest step size.

    Deterministic upward rounding — result is always >= input qty
    and a multiple of step_size. Uses Decimal arithmetic only.

    Args:
        qty: Raw quantity to round up.
        step_size: Exchange lot size step.

    Returns:
        Ceiled quantity as multiple of step_size.
    """
    if step_size <= 0:
        return qty
    steps = (qty / step_size).quantize(Decimal("1"), rounding=ROUND_UP)
    return steps * step_size


def _no_go(symbol: str, reason: NoGoReason) -> TuningResult:
    return TuningResult(symbol=symbol, status=TuningStatus.NO_GO, reason=reason)


def solve(
    symbol: str,
    constraints: SymbolConstraints,
    price: Decimal,
    config: TuningSolverConfig,
) -> TuningResult:
    """Compute a legal grid_v2 order size or declare NO_GO.

    Deterministic: same inputs always produce the same result.

    Args:
        symbol: Trading pair.
        constraints: Exchange symbol constraints (from ConstraintProvider).
        price: Current market price in quote asset.
        config: Risk and grid configuration.

    Returns:
        TuningResult with status TUNED or NO_GO.
    """
    # --- Hard prerequisite checks ---

    if symbol in config.blacklist:
        return _no_go(symbol, NoGoReason.BLACKLISTED)

    if price <= 0:
        return _no_go(symbol, NoGoReason.PRICE_UNAVAILABLE)

    if constraints.tick_size <= 0:
        return _no_go(symbol, NoGoReason.TICK_SIZE_UNAVAILABLE)

    if constraints.step_size <= 0:
        return _no_go(symbol, NoGoReason.STEP_SIZE_UNAVAILABLE)

    # --- Compute worst-case deep entry price ---
    # Deepest BUY entry sits entry_levels_per_side * spacing below mid price.
    # min_notional must be satisfied at this worst-case price, not mid.
    worst_case_price = price * (1 - config.spacing_pct * config.entry_levels_per_side)
    if worst_case_price <= 0:
        worst_case_price = constraints.tick_size  # absolute floor

    # --- Compute minimum legal quantity (at worst-case deep price) ---

    notional_driven = False
    order_size = constraints.min_qty

    if constraints.min_notional > 0:
        raw_qty_for_notional = constraints.min_notional / worst_case_price
        min_qty_for_notional = ceil_to_step(raw_qty_for_notional, constraints.step_size)
        if min_qty_for_notional > order_size:
            notional_driven = True
            order_size = min_qty_for_notional

    # Ensure step-aligned (min_qty may already be, but be explicit)
    order_size = ceil_to_step(order_size, constraints.step_size)

    # --- Check worst-case inventory budget ---

    worst_case_usd = order_size * price * config.max_inventory_levels
    if worst_case_usd > config.max_position_usd:
        # Distinguish root cause: exchange notional floor vs risk cap
        reason = NoGoReason.NOTIONAL_TOO_LOW if notional_driven else NoGoReason.POSITION_EXCEEDS_CAP
        return _no_go(symbol, reason)

    # --- All checks passed ---

    return TuningResult(
        symbol=symbol,
        status=TuningStatus.TUNED,
        order_size=order_size,
        tick_size=constraints.tick_size,
        step_size=constraints.step_size,
        max_inventory_levels=config.max_inventory_levels,
    )
