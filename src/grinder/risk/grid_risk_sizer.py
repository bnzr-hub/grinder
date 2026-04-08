"""GridRiskSizer — budget-driven grid admissibility and sizing.

Translates portfolio/symbol risk budget into grid-level decisions:
- max position notional from risk budget + adverse move model
- order size from position notional / entry levels
- NO_GO when exchange minimums exceed risk-consistent size

Per spec docs/41_AUTONOMOUS_RISK_MANAGER_V1_SPEC.md §12.
Shadow-only in first PR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class GridRiskSizerConfig:
    """Grid risk sizer thresholds."""

    min_entry_levels: int = 5


@dataclass(frozen=True)
class GridRiskSizerInput:
    """Per-symbol inputs for grid risk sizing."""

    symbol: str
    price: Decimal
    step_pct: Decimal  # e.g. 0.01 = 1%
    symbol_risk_budget_usd: Decimal
    entry_levels: int  # live entry levels per side (for min_entry_levels check)
    adverse_depth_levels: int = 20  # worst-case depth for risk sizing
    max_inventory_levels: int = 15  # max accumulated lots for per-order sizing
    exchange_min_qty: Decimal = Decimal("0")
    exchange_min_notional: Decimal = Decimal("0")
    qty_step: Decimal = Decimal("0")


@dataclass(frozen=True)
class GridRiskSizerResult:
    """Result of grid risk sizing for one symbol."""

    symbol: str
    admissible: bool
    reason: str
    symbol_risk_budget_usd: Decimal
    step_pct: Decimal
    adverse_move_pct: Decimal
    entry_levels: int
    max_position_notional_usd: Decimal
    order_notional_usd: Decimal
    order_qty_raw: Decimal
    order_qty_rounded: Decimal
    actual_order_notional_usd: Decimal
    exchange_min_qty: Decimal
    exchange_min_notional: Decimal


class GridRiskSizer:
    """Deterministic budget-driven grid sizer.

    Pure logic — no I/O. Call compute() with per-symbol inputs.
    """

    def __init__(self, config: GridRiskSizerConfig | None = None) -> None:
        self._config = config or GridRiskSizerConfig()

    def compute(self, inp: GridRiskSizerInput) -> GridRiskSizerResult:  # noqa: PLR0911
        """Compute grid sizing from risk budget. Returns admissibility + sizing."""
        # Input validation
        if inp.price <= _ZERO or inp.step_pct <= _ZERO or inp.symbol_risk_budget_usd <= _ZERO:
            return self._no_go(inp, "INVALID_INPUT")

        if inp.entry_levels < 1:
            return self._no_go(inp, "NO_LEVELS")

        if inp.entry_levels < self._config.min_entry_levels:
            return self._no_go(inp, "BELOW_MIN_LEVELS")

        # Adverse move: full depth to forced-flat level
        adverse_move_pct = inp.step_pct * Decimal(str(inp.adverse_depth_levels + 1))
        if adverse_move_pct <= _ZERO:
            return self._no_go(inp, "ZERO_ADVERSE_MOVE")

        # Max position notional from risk budget
        max_position_notional = inp.symbol_risk_budget_usd / adverse_move_pct

        # Per-order sizing spread across max inventory depth
        order_notional = max_position_notional / Decimal(str(inp.max_inventory_levels))
        order_qty_raw = order_notional / inp.price

        # Round DOWN to exchange qty step (never up — preserve risk discipline)
        if inp.qty_step > _ZERO:
            order_qty_rounded = (order_qty_raw / inp.qty_step).quantize(
                _ONE, rounding=ROUND_DOWN
            ) * inp.qty_step
        else:
            order_qty_rounded = order_qty_raw

        # Validate against exchange minimums
        if order_qty_rounded < inp.exchange_min_qty:
            return self._result(
                inp,
                admissible=False,
                reason="BELOW_MIN_QTY",
                adverse_move_pct=adverse_move_pct,
                max_position_notional=max_position_notional,
                order_notional=order_notional,
                order_qty_raw=order_qty_raw,
                order_qty_rounded=order_qty_rounded,
            )

        actual_notional = order_qty_rounded * inp.price
        if actual_notional < inp.exchange_min_notional:
            return self._result(
                inp,
                admissible=False,
                reason="BELOW_MIN_NOTIONAL",
                adverse_move_pct=adverse_move_pct,
                max_position_notional=max_position_notional,
                order_notional=order_notional,
                order_qty_raw=order_qty_raw,
                order_qty_rounded=order_qty_rounded,
            )

        return self._result(
            inp,
            admissible=True,
            reason="OK",
            adverse_move_pct=adverse_move_pct,
            max_position_notional=max_position_notional,
            order_notional=order_notional,
            order_qty_raw=order_qty_raw,
            order_qty_rounded=order_qty_rounded,
        )

    def _no_go(self, inp: GridRiskSizerInput, reason: str) -> GridRiskSizerResult:
        return GridRiskSizerResult(
            symbol=inp.symbol,
            admissible=False,
            reason=reason,
            symbol_risk_budget_usd=inp.symbol_risk_budget_usd,
            step_pct=inp.step_pct,
            adverse_move_pct=_ZERO,
            entry_levels=inp.entry_levels,
            max_position_notional_usd=_ZERO,
            order_notional_usd=_ZERO,
            order_qty_raw=_ZERO,
            order_qty_rounded=_ZERO,
            actual_order_notional_usd=_ZERO,
            exchange_min_qty=inp.exchange_min_qty,
            exchange_min_notional=inp.exchange_min_notional,
        )

    def _result(
        self,
        inp: GridRiskSizerInput,
        *,
        admissible: bool,
        reason: str,
        adverse_move_pct: Decimal,
        max_position_notional: Decimal,
        order_notional: Decimal,
        order_qty_raw: Decimal,
        order_qty_rounded: Decimal,
    ) -> GridRiskSizerResult:
        actual_notional = order_qty_rounded * inp.price if order_qty_rounded > _ZERO else _ZERO
        return GridRiskSizerResult(
            symbol=inp.symbol,
            admissible=admissible,
            reason=reason,
            symbol_risk_budget_usd=inp.symbol_risk_budget_usd,
            step_pct=inp.step_pct,
            adverse_move_pct=adverse_move_pct,
            entry_levels=inp.entry_levels,
            max_position_notional_usd=max_position_notional,
            order_notional_usd=order_notional,
            order_qty_raw=order_qty_raw,
            order_qty_rounded=order_qty_rounded,
            actual_order_notional_usd=actual_notional,
            exchange_min_qty=inp.exchange_min_qty,
            exchange_min_notional=inp.exchange_min_notional,
        )
