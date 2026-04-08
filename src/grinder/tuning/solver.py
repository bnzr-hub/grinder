"""Deterministic symbol tuning solver (PR-B2, ADR-124).

Given symbol constraints, current price, and risk/grid config, computes a
legal grid_v2 order size or declares NO_GO with reason.

Pure computation — no exchange I/O, no state mutation, no runtime wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
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
    max_position_notional_usd: Decimal | None = None
    actual_order_notional_usd: Decimal | None = None


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

    max_position_usd: Decimal = Decimal("0")
    max_inventory_levels: int = 15
    entry_levels_per_side: int = 5
    adverse_depth_levels: int = 20
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


def solve(  # noqa: PLR0911
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
    # Must match the EXACT grid_v2 ladder construction from state.py:
    #   1. anchor = round(price / tick, ROUND_HALF_UP) * tick
    #   2. step_price = ceil(anchor * step_pct / tick) * tick  (min 1 tick)
    #   3. deepest_buy = anchor - step_price * levels
    #   4. quantized = round_down(deepest_buy / tick) * tick  (BUY-side)
    #
    # Previous approximation (price * (1 - pct * levels)) diverged by up to
    # 2 ticks for cheap symbols because it skipped anchor rounding and
    # tick-aligned step computation.
    tick = constraints.tick_size
    anchor = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    raw_step = anchor * config.spacing_pct
    step_ticks = (raw_step / tick).quantize(Decimal("1"), rounding=ROUND_CEILING)
    if step_ticks < 1:
        step_ticks = Decimal("1")
    step_price = step_ticks * tick
    deepest_raw = anchor - step_price * config.entry_levels_per_side
    if deepest_raw <= 0:
        deepest_raw = tick  # absolute floor
    # Apply BUY-side tick rounding (ROUND_DOWN) — matches bridge._quantize_price
    worst_case_price = (deepest_raw / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick
    if worst_case_price <= 0:
        worst_case_price = tick  # one tick minimum

    # --- Risk-driven order sizing ---
    # Derive order_size from risk budget using adverse depth, not from
    # exchange minimums. Exchange mins are a fail-closed gate, not a target.

    # Adverse move: full depth to forced-flat level
    adverse_move_pct = config.spacing_pct * Decimal(str(config.adverse_depth_levels + 1))
    if adverse_move_pct <= 0:
        return _no_go(symbol, NoGoReason.POSITION_EXCEEDS_CAP)

    # Max position from budget / adverse move
    max_position_notional = config.max_position_usd / adverse_move_pct
    # Per-order notional spread across max inventory depth
    order_notional = max_position_notional / Decimal(str(config.max_inventory_levels))
    # Convert to qty and round DOWN (never inflate risk)
    order_qty_raw = order_notional / price
    order_size = (order_qty_raw / constraints.step_size).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    ) * constraints.step_size

    if order_size <= 0:
        return _no_go(symbol, NoGoReason.POSITION_EXCEEDS_CAP)

    # --- Exchange minimum gate (fail-closed, not a target) ---

    if order_size < constraints.min_qty:
        return _no_go(symbol, NoGoReason.POSITION_EXCEEDS_CAP)

    actual_notional = order_size * worst_case_price
    if constraints.min_notional > 0 and actual_notional < constraints.min_notional:
        return _no_go(symbol, NoGoReason.NOTIONAL_TOO_LOW)

    # --- All checks passed ---

    return TuningResult(
        symbol=symbol,
        status=TuningStatus.TUNED,
        order_size=order_size,
        tick_size=constraints.tick_size,
        step_size=constraints.step_size,
        max_inventory_levels=config.max_inventory_levels,
        max_position_notional_usd=max_position_notional,
        actual_order_notional_usd=actual_notional,
    )
