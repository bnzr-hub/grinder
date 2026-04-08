"""RiskAdmissionGate — enforce shadow risk planners for new-risk admission.

Combines DayRiskManager + PortfolioBudgetAllocator + GridRiskSizer into a
single admission decision for each candidate symbol. Filters ranked candidate
list BEFORE orchestrator/rotation — only risk-admitted symbols enter rotation.

Active engines are unaffected: this gate controls new-risk activation only.

Per spec docs/41_AUTONOMOUS_RISK_MANAGER_V1_SPEC.md §13 (admission gating).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from grinder.risk.grid_risk_sizer import GridRiskSizer

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")


class AdmissionBlockReason(Enum):
    """Why a candidate was blocked from admission."""

    DAY_STOP = "DAY_STOP"
    PORTFOLIO_NO_ENTRIES = "PORTFOLIO_NO_ENTRIES"
    GRID_NO_GO = "GRID_NO_GO"
    NO_PRICE = "NO_PRICE"
    NO_CONSTRAINTS = "NO_CONSTRAINTS"
    NO_EQUITY = "NO_EQUITY"


@dataclass(frozen=True)
class AdmissionDecision:
    """Result of risk admission evaluation for one symbol."""

    symbol: str
    admitted: bool
    block_reason: AdmissionBlockReason | None = None
    detail: str = ""


class RiskAdmissionGate:
    """Deterministic admission gate combining all risk planners.

    Pure logic — no I/O. Reads current state from injected managers.
    """

    def __init__(
        self,
        day_risk_manager: object,
        portfolio_allocator: object,
        grid_sizer: GridRiskSizer,
    ) -> None:
        self._day_risk = day_risk_manager
        self._portfolio = portfolio_allocator
        self._grid_sizer = grid_sizer

    def evaluate(
        self,
        symbol: str,
        *,
        price: Decimal,
        step_pct: Decimal,
        entry_levels: int,
        exchange_min_qty: Decimal,
        exchange_min_notional: Decimal,
        qty_step: Decimal,
        day_state: object,
        portfolio_snapshot: object,
    ) -> AdmissionDecision:
        """Evaluate risk admission for one candidate symbol.

        Args:
            symbol: Trading pair.
            price: Current market price.
            step_pct: Grid step as fraction (e.g. 0.013 = 1.3%).
            entry_levels: Number of grid entry levels.
            exchange_min_qty: Exchange minimum order quantity.
            exchange_min_notional: Exchange minimum order notional.
            qty_step: Exchange lot size step.
            day_state: DayRiskState from DayRiskManager.
            portfolio_snapshot: PortfolioBudgetSnapshot from PortfolioBudgetAllocator.

        Returns:
            AdmissionDecision with admitted=True or block reason.
        """
        from grinder.risk.day_risk_manager import DayRiskMode  # noqa: PLC0415

        # Gate 1: Day risk — stop modes block all new risk
        mode = getattr(day_state, "mode", None)
        if mode in (
            DayRiskMode.STOP_FOR_DAY,
            DayRiskMode.STOP_NEW_RISK,
            DayRiskMode.FORCE_REDUCE,
        ):
            pnl = getattr(day_state, "day_pnl_pct", "?")
            floor = getattr(day_state, "profit_lock_floor_pct", "?")
            return AdmissionDecision(
                symbol=symbol,
                admitted=False,
                block_reason=AdmissionBlockReason.DAY_STOP,
                detail=f"day_mode={mode.value} pnl_pct={pnl} floor_pct={floor}",
            )

        # Gate 2: Portfolio — new entries allowed?
        if not getattr(portfolio_snapshot, "new_entries_allowed", True):
            reasons = getattr(portfolio_snapshot, "reasons", ())
            slots = getattr(portfolio_snapshot, "remaining_symbol_slots", "?")
            gross_free = getattr(portfolio_snapshot, "gross_exposure_free_usd", "?")
            return AdmissionDecision(
                symbol=symbol,
                admitted=False,
                block_reason=AdmissionBlockReason.PORTFOLIO_NO_ENTRIES,
                detail=(
                    f"reasons={','.join(reasons)} slots_free={slots} gross_free_usd={gross_free}"
                ),
            )

        # Gate 3: Grid risk sizer — admissible?
        risk_budget = getattr(portfolio_snapshot, "per_symbol_risk_budget_usd", _ZERO)
        if risk_budget <= _ZERO:
            return AdmissionDecision(
                symbol=symbol,
                admitted=False,
                block_reason=AdmissionBlockReason.PORTFOLIO_NO_ENTRIES,
                detail="risk_budget=0",
            )

        from grinder.risk.grid_policy import DEFAULT_GRID_POLICY  # noqa: PLC0415
        from grinder.risk.grid_risk_sizer import GridRiskSizerInput  # noqa: PLC0415

        sizer_input = GridRiskSizerInput(
            symbol=symbol,
            price=price,
            step_pct=step_pct,
            symbol_risk_budget_usd=risk_budget,
            entry_levels=entry_levels,
            adverse_depth_levels=DEFAULT_GRID_POLICY.adverse_depth_levels,
            max_inventory_levels=DEFAULT_GRID_POLICY.max_inventory_levels,
            exchange_min_qty=exchange_min_qty,
            exchange_min_notional=exchange_min_notional,
            qty_step=qty_step,
        )
        sizer_result = self._grid_sizer.compute(sizer_input)

        if not sizer_result.admissible:
            r = sizer_result
            return AdmissionDecision(
                symbol=symbol,
                admitted=False,
                block_reason=AdmissionBlockReason.GRID_NO_GO,
                detail=(
                    f"reason={r.reason}"
                    f" risk_usd={r.symbol_risk_budget_usd:.2f}"
                    f" step_pct={r.step_pct}"
                    f" adverse_pct={r.adverse_move_pct}"
                    f" max_pos_usd={r.max_position_notional_usd:.2f}"
                    f" order_notional_usd={r.order_notional_usd:.2f}"
                    f" qty={r.order_qty_rounded}"
                    f" actual_notional_usd={r.actual_order_notional_usd:.2f}"
                    f" min_qty={r.exchange_min_qty}"
                    f" min_notional_usd={r.exchange_min_notional}"
                ),
            )

        # All gates passed
        r = sizer_result
        return AdmissionDecision(
            symbol=symbol,
            admitted=True,
            detail=(
                f"risk_usd={r.symbol_risk_budget_usd:.2f}"
                f" levels={r.entry_levels}"
                f" qty={r.order_qty_rounded}"
                f" actual_notional_usd={r.actual_order_notional_usd:.2f}"
            ),
        )

    def filter_candidates(
        self,
        ranked: list[str],
        *,
        prices: dict[str, Decimal],
        step_pcts: dict[str, Decimal],
        entry_levels: int,
        constraints: Mapping[str, object],
        day_state: object,
        portfolio_snapshot: object,
    ) -> tuple[list[str], dict[str, AdmissionDecision]]:
        """Filter ranked candidates through all risk gates.

        Returns:
            (admitted_symbols, all_decisions) where admitted_symbols preserves
            ranking order and all_decisions maps every symbol to its decision.
        """
        admitted: list[str] = []
        decisions: dict[str, AdmissionDecision] = {}

        for symbol in ranked:
            price = prices.get(symbol)
            if price is None or price <= _ZERO:
                dec = AdmissionDecision(
                    symbol=symbol,
                    admitted=False,
                    block_reason=AdmissionBlockReason.NO_PRICE,
                )
                decisions[symbol] = dec
                continue

            sc = constraints.get(symbol)
            if sc is None:
                dec = AdmissionDecision(
                    symbol=symbol,
                    admitted=False,
                    block_reason=AdmissionBlockReason.NO_CONSTRAINTS,
                )
                decisions[symbol] = dec
                continue

            step_pct = step_pcts.get(symbol, _ZERO)
            exchange_min_qty = getattr(sc, "min_qty", _ZERO)
            exchange_min_notional = getattr(sc, "min_notional", _ZERO)
            qty_step = getattr(sc, "step_size", _ZERO)

            dec = self.evaluate(
                symbol,
                price=price,
                step_pct=step_pct,
                entry_levels=entry_levels,
                exchange_min_qty=exchange_min_qty,
                exchange_min_notional=exchange_min_notional,
                qty_step=qty_step,
                day_state=day_state,
                portfolio_snapshot=portfolio_snapshot,
            )
            decisions[symbol] = dec

            if dec.admitted:
                admitted.append(symbol)
                logger.debug(
                    "RISK_ADMISSION_ALLOWED symbol=%s detail=%s",
                    symbol,
                    dec.detail,
                )
            else:
                logger.info(
                    "RISK_ADMISSION_BLOCKED symbol=%s reason=%s detail=%s",
                    symbol,
                    dec.block_reason.value if dec.block_reason else "UNKNOWN",
                    dec.detail,
                )

        if admitted:
            logger.info(
                "RISK_ADMISSION_RESULT admitted=%d blocked=%d admitted_symbols=%s",
                len(admitted),
                len(ranked) - len(admitted),
                ",".join(admitted[:5]),
            )
        elif ranked:
            logger.warning(
                "RISK_ADMISSION_ALL_BLOCKED count=%d",
                len(ranked),
            )

        return admitted, decisions
