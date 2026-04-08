"""Helpers for deriving autonomous sizing budget from real account truth."""

from __future__ import annotations

from decimal import Decimal

from grinder.risk.day_risk_manager import DayRiskMode
from grinder.risk.portfolio_budget_allocator import (
    MarketRegime,
    PortfolioBudgetAllocator,
    PortfolioBudgetInput,
    PortfolioBudgetSnapshot,
)

_ZERO = Decimal("0")


def derive_autonomous_portfolio_snapshot(
    *,
    equity: Decimal,
    gross_exposure_used_usd: Decimal,
    active_symbol_count: int = 0,
    day_mode: DayRiskMode = DayRiskMode.NORMAL,
    market_regime: MarketRegime = MarketRegime.NEUTRAL,
    allocator: PortfolioBudgetAllocator | None = None,
) -> PortfolioBudgetSnapshot:
    """Derive portfolio budget snapshot for autonomous sizing.

    This is the shared non-legacy sizing entrypoint for bootstrap and refresher.
    """
    alloc = allocator or PortfolioBudgetAllocator()
    return alloc.compute(
        PortfolioBudgetInput(
            equity=equity,
            day_mode=day_mode,
            market_regime=market_regime,
            active_symbol_count=max(0, active_symbol_count),
            gross_exposure_used_usd=max(_ZERO, gross_exposure_used_usd),
        )
    )
