"""PortfolioBudgetAllocator — portfolio-level risk budget for autonomous runtime.

Computes per-symbol risk budget from equity, day mode, market regime,
active symbol count, and leverage capacity. Shadow-only in first PR.

Per spec docs/41_AUTONOMOUS_RISK_MANAGER_V1_SPEC.md §10-11.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.risk.day_risk_manager import DayRiskMode

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")


class MarketRegime(Enum):
    """Market regime classification for risk sizing."""

    GOOD = "GOOD"
    NEUTRAL = "NEUTRAL"
    TOXIC = "TOXIC"


@dataclass(frozen=True)
class PortfolioBudgetConfig:
    """Portfolio budget thresholds."""

    max_active_symbols: int = 5
    max_leverage: int = 5
    risk_pct_good: Decimal = Decimal("6.0")
    risk_pct_neutral: Decimal = Decimal("3.0")
    risk_pct_toxic: Decimal = Decimal("2.0")
    defensive_multiplier: Decimal = Decimal("0.5")


DEFAULT_PORTFOLIO_BUDGET_CONFIG = PortfolioBudgetConfig()


@dataclass(frozen=True)
class PortfolioBudgetInput:
    """Inputs for one budget computation cycle."""

    equity: Decimal
    day_mode: DayRiskMode
    market_regime: MarketRegime
    active_symbol_count: int
    gross_exposure_used_usd: Decimal


@dataclass(frozen=True)
class PortfolioBudgetSnapshot:
    """Result of one budget computation."""

    equity: Decimal
    day_mode: DayRiskMode
    market_regime: MarketRegime
    base_risk_pct: Decimal
    effective_risk_pct: Decimal
    max_active_symbols: int
    active_symbol_count: int
    remaining_symbol_slots: int
    gross_exposure_limit_usd: Decimal
    gross_exposure_used_usd: Decimal
    gross_exposure_free_usd: Decimal
    per_symbol_risk_budget_usd: Decimal
    new_entries_allowed: bool
    reasons: tuple[str, ...]


class PortfolioBudgetAllocator:
    """Deterministic portfolio-level budget planner.

    Pure logic — no I/O. Call compute() with current inputs to get budget.
    """

    def __init__(self, config: PortfolioBudgetConfig | None = None) -> None:
        self._config = config or DEFAULT_PORTFOLIO_BUDGET_CONFIG

    def compute(self, inp: PortfolioBudgetInput) -> PortfolioBudgetSnapshot:  # noqa: PLR0912
        """Compute portfolio budget snapshot from current inputs."""
        from grinder.risk.day_risk_manager import DayRiskMode  # noqa: PLC0415

        cfg = self._config
        reasons: list[str] = []

        # Base risk % by market regime
        base_risk_pct = {
            MarketRegime.GOOD: cfg.risk_pct_good,
            MarketRegime.NEUTRAL: cfg.risk_pct_neutral,
            MarketRegime.TOXIC: cfg.risk_pct_toxic,
        }.get(inp.market_regime, cfg.risk_pct_neutral)

        # Day mode modifier
        if inp.day_mode == DayRiskMode.STOP_FOR_DAY:
            effective_risk_pct = _ZERO
            reasons.append("STOP_FOR_DAY")
        elif inp.day_mode == DayRiskMode.FORCE_REDUCE:
            effective_risk_pct = _ZERO
            reasons.append("FORCE_REDUCE")
        elif inp.day_mode == DayRiskMode.STOP_NEW_RISK:
            effective_risk_pct = _ZERO
            reasons.append("STOP_NEW_RISK")
        elif inp.day_mode == DayRiskMode.DEFENSIVE:
            effective_risk_pct = base_risk_pct * cfg.defensive_multiplier
        else:
            effective_risk_pct = base_risk_pct

        # Per-symbol risk budget
        per_symbol_risk_usd = inp.equity * effective_risk_pct / Decimal("100")

        # Leverage capacity
        gross_limit = inp.equity * Decimal(str(cfg.max_leverage))
        gross_free = max(_ZERO, gross_limit - inp.gross_exposure_used_usd)

        # Symbol slots
        remaining_slots = max(0, cfg.max_active_symbols - inp.active_symbol_count)

        # Admission decision
        new_entries_allowed = True

        if inp.day_mode == DayRiskMode.STOP_FOR_DAY:
            new_entries_allowed = False
            if "STOP_FOR_DAY" not in reasons:
                reasons.append("STOP_FOR_DAY")

        if inp.day_mode == DayRiskMode.STOP_NEW_RISK:
            new_entries_allowed = False
            if "STOP_NEW_RISK" not in reasons:
                reasons.append("STOP_NEW_RISK")

        if inp.day_mode == DayRiskMode.FORCE_REDUCE:
            new_entries_allowed = False
            if "FORCE_REDUCE" not in reasons:
                reasons.append("FORCE_REDUCE")

        if remaining_slots <= 0:
            new_entries_allowed = False
            reasons.append("MAX_SYMBOLS_REACHED")

        if gross_free <= _ZERO:
            new_entries_allowed = False
            reasons.append("LEVERAGE_CAPACITY_FULL")

        if inp.equity <= _ZERO:
            new_entries_allowed = False
            reasons.append("NO_EQUITY")

        return PortfolioBudgetSnapshot(
            equity=inp.equity,
            day_mode=inp.day_mode,
            market_regime=inp.market_regime,
            base_risk_pct=base_risk_pct,
            effective_risk_pct=effective_risk_pct,
            max_active_symbols=cfg.max_active_symbols,
            active_symbol_count=inp.active_symbol_count,
            remaining_symbol_slots=remaining_slots,
            gross_exposure_limit_usd=gross_limit,
            gross_exposure_used_usd=inp.gross_exposure_used_usd,
            gross_exposure_free_usd=gross_free,
            per_symbol_risk_budget_usd=per_symbol_risk_usd,
            new_entries_allowed=new_entries_allowed,
            reasons=tuple(reasons),
        )
