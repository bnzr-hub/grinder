"""Portfolio-level risk evaluation (ADR-092, %-model extension).

Evaluates symbol and portfolio risk limits relative to exchange-balance risk base.
Backward-compatible with fraction-style caps while supporting pct-style inputs in engine.

Conventions:
- signed notional: long > 0, short < 0.
- gross = sum(abs(notional_i)) across all positions.
- net = abs(sum(signed_notional_i)) across all positions.
- mark_price from PositionSnap as price source.
- drawdown% = max(0, -unrealized_pnl / risk_base) * 100.

Fail-closed:
- if risk base is unavailable/stale/below min -> block INCREASE_RISK.
- if any cap or DD gate cannot be evaluated -> block INCREASE_RISK.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.account.contracts import AccountSnapshot
    from grinder.risk.risk_base import RiskBaseSnapshot

logger = logging.getLogger(__name__)


class RiskGateReason(Enum):
    """Reason for risk gate blocking INCREASE_RISK."""

    RISK_BASE_UNAVAILABLE = "RISK_BASE_UNAVAILABLE"
    RISK_BASE_STALE = "RISK_BASE_STALE"
    RISK_BASE_BELOW_MIN = "RISK_BASE_BELOW_MIN"
    SYMBOL_CAP_EXCEEDED = "SYMBOL_CAP_EXCEEDED"
    PORTFOLIO_GROSS_CAP_EXCEEDED = "PORTFOLIO_GROSS_CAP_EXCEEDED"
    PORTFOLIO_NET_CAP_EXCEEDED = "PORTFOLIO_NET_CAP_EXCEEDED"
    SYMBOL_DD_FREEZE = "SYMBOL_DD_FREEZE"
    SYMBOL_DD_UNLOAD = "SYMBOL_DD_UNLOAD"
    SYMBOL_DD_FORCED_FLAT = "SYMBOL_DD_FORCED_FLAT"
    PORTFOLIO_DD_FREEZE = "PORTFOLIO_DD_FREEZE"
    PORTFOLIO_DD_FORCE_REDUCE = "PORTFOLIO_DD_FORCE_REDUCE"
    PORTFOLIO_DD_KILL_SWITCH = "PORTFOLIO_DD_KILL_SWITCH"


@dataclass(frozen=True)
class PortfolioRiskConfig:
    """Configuration for portfolio risk enforcement.

    Cap fields are fractions (0.10 = 10%).
    A value of 0.0 means the check is disabled.
    """

    symbol_max_notional_pct: float = 0.0
    portfolio_max_gross_notional_pct: float = 0.0
    portfolio_max_net_notional_pct: float = 0.0
    symbol_freeze_dd_pct: float = 0.0
    symbol_unload_dd_pct: float = 0.0
    symbol_forced_flat_dd_pct: float = 0.0
    portfolio_dd_freeze_pct: float = 0.0
    portfolio_dd_force_reduce_pct: float = 0.0
    portfolio_dd_kill_switch_pct: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "symbol_max_notional_pct",
            "portfolio_max_gross_notional_pct",
            "portfolio_max_net_notional_pct",
        ):
            val = getattr(self, name)
            if val < 0:
                raise ValueError(f"{name} must be >= 0, got {val}")
            if val > 10.0:
                raise ValueError(
                    f"{name}={val} looks like a percentage, not a fraction. "
                    f"Expected fraction in [0.0, 10.0], got {val}"
                )
        for name in (
            "symbol_freeze_dd_pct",
            "symbol_unload_dd_pct",
            "symbol_forced_flat_dd_pct",
            "portfolio_dd_freeze_pct",
            "portfolio_dd_force_reduce_pct",
            "portfolio_dd_kill_switch_pct",
        ):
            val = getattr(self, name)
            if val < 0:
                raise ValueError(f"{name} must be >= 0, got {val}")
            if val > 100:
                raise ValueError(f"{name} must be <= 100, got {val}")
        _assert_dd_order(
            self.symbol_freeze_dd_pct,
            self.symbol_unload_dd_pct,
            self.symbol_forced_flat_dd_pct,
            "symbol",
        )
        _assert_dd_order(
            self.portfolio_dd_freeze_pct,
            self.portfolio_dd_force_reduce_pct,
            self.portfolio_dd_kill_switch_pct,
            "portfolio",
        )


@dataclass(frozen=True)
class RiskGateDecision:
    """Result of portfolio risk evaluation.

    Attributes:
        allowed: True if INCREASE_RISK is allowed.
        reason: Why blocked (None if allowed).
        detail: Human-readable detail for logging.
    """

    allowed: bool
    reason: RiskGateReason | None = None
    detail: str = ""
    symbol_drawdown_pct: float = 0.0
    portfolio_drawdown_pct: float = 0.0


def _assert_dd_order(freeze: float, reduce: float, kill: float, scope: str) -> None:
    if freeze > 0 and reduce > 0 and reduce < freeze:
        raise ValueError(f"{scope} dd thresholds invalid: reduce({reduce}) < freeze({freeze})")
    if reduce > 0 and kill > 0 and kill < reduce:
        raise ValueError(f"{scope} dd thresholds invalid: kill({kill}) < reduce({reduce})")
    if freeze > 0 and kill > 0 and reduce == 0 and kill < freeze:
        raise ValueError(f"{scope} dd thresholds invalid: kill({kill}) < freeze({freeze})")


def compute_symbol_notional(
    snapshot: AccountSnapshot,
    symbol: str,
) -> Decimal:
    """Compute absolute notional for a single symbol.

    Returns sum(qty * mark_price) across all position sides for the symbol.
    """
    total = Decimal(0)
    for p in snapshot.positions:
        if p.symbol == symbol:
            total += abs(p.qty) * p.mark_price
    return total


def compute_portfolio_notionals(
    snapshot: AccountSnapshot,
) -> tuple[Decimal, Decimal]:
    """Compute portfolio gross and net notionals.

    Returns:
        (gross, net) where:
        - gross = sum(abs(signed_notional_i))
        - net = abs(sum(signed_notional_i))
        - signed_notional: LONG = +qty*mark, SHORT = -qty*mark
        - BOTH (one-way mode): use signed_qty to determine direction.
          signed_qty < 0 → short, > 0 → long, None → fail-closed (treat as long).
    """
    signed_sum = Decimal(0)
    gross = Decimal(0)
    for p in snapshot.positions:
        notional = abs(p.qty) * p.mark_price
        is_short = p.side == "SHORT" or (
            p.side == "BOTH" and p.signed_qty is not None and p.signed_qty < 0
        )
        signed = -notional if is_short else notional
        gross += notional
        signed_sum += signed
    return gross, abs(signed_sum)


def compute_symbol_drawdown_pct(
    snapshot: AccountSnapshot,
    symbol: str,
    symbol_budget_usd: Decimal,
) -> float:
    """Compute symbol drawdown% relative to budget.

    Uses exchange unrealized_pnl summed across position sides for the symbol.
    """
    if symbol_budget_usd <= 0:
        return 0.0
    upnl = Decimal(0)
    for p in snapshot.positions:
        if p.symbol == symbol:
            upnl += p.unrealized_pnl
    if upnl >= 0:
        return 0.0
    return float((-upnl / symbol_budget_usd) * Decimal(100))


def compute_portfolio_drawdown_pct(
    snapshot: AccountSnapshot,
    risk_base_usd: Decimal,
) -> float:
    """Compute portfolio drawdown% relative to risk base."""
    if risk_base_usd <= 0:
        return 0.0
    upnl = Decimal(0)
    for p in snapshot.positions:
        upnl += p.unrealized_pnl
    if upnl >= 0:
        return 0.0
    return float((-upnl / risk_base_usd) * Decimal(100))


def evaluate_risk_gate(  # noqa: PLR0911, PLR0912
    risk_base: RiskBaseSnapshot | None,
    snapshot: AccountSnapshot | None,
    config: PortfolioRiskConfig,
    symbol: str,
) -> RiskGateDecision:
    """Evaluate whether INCREASE_RISK is allowed for a symbol.

    Checks in order:
    1. Risk base availability (fail-closed if None).
    2. Risk base staleness (fail-closed if soft or hard stale).
    3. Risk base below min USD (fail-closed).
    4. Symbol notional cap (per-symbol).
    5. Portfolio gross notional cap (all symbols).
    6. Portfolio net notional cap (all symbols).

    Returns RiskGateDecision with allowed=True if all checks pass.
    """
    # 1. Unavailable
    if risk_base is None:
        return RiskGateDecision(
            allowed=False,
            reason=RiskGateReason.RISK_BASE_UNAVAILABLE,
            detail="risk base snapshot not available",
        )

    # 2. Stale (soft or hard)
    if risk_base.is_stale_soft or risk_base.is_stale_hard:
        return RiskGateDecision(
            allowed=False,
            reason=RiskGateReason.RISK_BASE_STALE,
            detail=f"risk base stale: age={risk_base.age_s}s soft={risk_base.is_stale_soft} hard={risk_base.is_stale_hard}",
        )

    # 3. Below min
    if risk_base.is_below_min:
        return RiskGateDecision(
            allowed=False,
            reason=RiskGateReason.RISK_BASE_BELOW_MIN,
            detail=f"risk base below minimum: value={risk_base.value_usd} USD",
        )

    # For cap checks, need account snapshot.
    # Fail-closed: if any cap is enabled but snapshot unavailable, block.
    _any_cap_enabled = (
        config.symbol_max_notional_pct > 0
        or config.portfolio_max_gross_notional_pct > 0
        or config.portfolio_max_net_notional_pct > 0
    )
    if snapshot is None:
        if _any_cap_enabled:
            return RiskGateDecision(
                allowed=False,
                reason=RiskGateReason.RISK_BASE_UNAVAILABLE,
                detail="account snapshot unavailable, cannot evaluate caps",
            )
        return RiskGateDecision(allowed=True)

    base_usd = float(risk_base.value_usd)

    # 4. Symbol cap
    if config.symbol_max_notional_pct > 0:
        sym_notional = float(compute_symbol_notional(snapshot, symbol))
        sym_limit = base_usd * config.symbol_max_notional_pct
        if sym_notional >= sym_limit:
            return RiskGateDecision(
                allowed=False,
                reason=RiskGateReason.SYMBOL_CAP_EXCEEDED,
                detail=f"symbol={symbol} notional={sym_notional:.2f} >= limit={sym_limit:.2f} "
                f"(base={base_usd:.2f} * pct={config.symbol_max_notional_pct})",
            )

    # 4b. Symbol DD ladder
    symbol_dd = 0.0
    if (
        config.symbol_freeze_dd_pct > 0
        or config.symbol_unload_dd_pct > 0
        or config.symbol_forced_flat_dd_pct > 0
    ) and config.symbol_max_notional_pct > 0:
        sym_budget = risk_base.value_usd * Decimal(str(config.symbol_max_notional_pct))
        symbol_dd = compute_symbol_drawdown_pct(snapshot, symbol, sym_budget)
        if config.symbol_forced_flat_dd_pct > 0 and symbol_dd >= config.symbol_forced_flat_dd_pct:
            return RiskGateDecision(
                allowed=False,
                reason=RiskGateReason.SYMBOL_DD_FORCED_FLAT,
                detail=f"symbol={symbol} dd={symbol_dd:.2f}% >= forced_flat={config.symbol_forced_flat_dd_pct:.2f}%",
                symbol_drawdown_pct=symbol_dd,
            )
        if config.symbol_unload_dd_pct > 0 and symbol_dd >= config.symbol_unload_dd_pct:
            return RiskGateDecision(
                allowed=False,
                reason=RiskGateReason.SYMBOL_DD_UNLOAD,
                detail=f"symbol={symbol} dd={symbol_dd:.2f}% >= unload={config.symbol_unload_dd_pct:.2f}%",
                symbol_drawdown_pct=symbol_dd,
            )
        if config.symbol_freeze_dd_pct > 0 and symbol_dd >= config.symbol_freeze_dd_pct:
            return RiskGateDecision(
                allowed=False,
                reason=RiskGateReason.SYMBOL_DD_FREEZE,
                detail=f"symbol={symbol} dd={symbol_dd:.2f}% >= freeze={config.symbol_freeze_dd_pct:.2f}%",
                symbol_drawdown_pct=symbol_dd,
            )

    # 5. Portfolio gross cap
    if config.portfolio_max_gross_notional_pct > 0 or config.portfolio_max_net_notional_pct > 0:
        gross, net = compute_portfolio_notionals(snapshot)

        if config.portfolio_max_gross_notional_pct > 0:
            gross_limit = base_usd * config.portfolio_max_gross_notional_pct
            if float(gross) >= gross_limit:
                return RiskGateDecision(
                    allowed=False,
                    reason=RiskGateReason.PORTFOLIO_GROSS_CAP_EXCEEDED,
                    detail=f"gross={float(gross):.2f} >= limit={gross_limit:.2f} "
                    f"(base={base_usd:.2f} * pct={config.portfolio_max_gross_notional_pct})",
                )

        # 6. Portfolio net cap
        if config.portfolio_max_net_notional_pct > 0:
            net_limit = base_usd * config.portfolio_max_net_notional_pct
            if float(net) >= net_limit:
                return RiskGateDecision(
                    allowed=False,
                    reason=RiskGateReason.PORTFOLIO_NET_CAP_EXCEEDED,
                    detail=f"net={float(net):.2f} >= limit={net_limit:.2f} "
                    f"(base={base_usd:.2f} * pct={config.portfolio_max_net_notional_pct})",
                )

    # 7. Portfolio DD ladder
    if (
        config.portfolio_dd_freeze_pct > 0
        or config.portfolio_dd_force_reduce_pct > 0
        or config.portfolio_dd_kill_switch_pct > 0
    ):
        portfolio_dd = compute_portfolio_drawdown_pct(snapshot, risk_base.value_usd)
        if (
            config.portfolio_dd_kill_switch_pct > 0
            and portfolio_dd >= config.portfolio_dd_kill_switch_pct
        ):
            return RiskGateDecision(
                allowed=False,
                reason=RiskGateReason.PORTFOLIO_DD_KILL_SWITCH,
                detail=f"portfolio_dd={portfolio_dd:.2f}% >= kill_switch={config.portfolio_dd_kill_switch_pct:.2f}%",
                portfolio_drawdown_pct=portfolio_dd,
            )
        if (
            config.portfolio_dd_force_reduce_pct > 0
            and portfolio_dd >= config.portfolio_dd_force_reduce_pct
        ):
            return RiskGateDecision(
                allowed=False,
                reason=RiskGateReason.PORTFOLIO_DD_FORCE_REDUCE,
                detail=f"portfolio_dd={portfolio_dd:.2f}% >= force_reduce={config.portfolio_dd_force_reduce_pct:.2f}%",
                portfolio_drawdown_pct=portfolio_dd,
            )
        if config.portfolio_dd_freeze_pct > 0 and portfolio_dd >= config.portfolio_dd_freeze_pct:
            return RiskGateDecision(
                allowed=False,
                reason=RiskGateReason.PORTFOLIO_DD_FREEZE,
                detail=f"portfolio_dd={portfolio_dd:.2f}% >= freeze={config.portfolio_dd_freeze_pct:.2f}%",
                portfolio_drawdown_pct=portfolio_dd,
            )

    return RiskGateDecision(allowed=True)
