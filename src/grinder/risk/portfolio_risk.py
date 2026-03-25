"""Portfolio-level risk evaluation (PR-2, ADR-092).

Evaluates symbol-level and portfolio-level notional caps relative to the
exchange-balance risk base (RiskBaseSnapshot from PR-1).

Conventions:
- signed notional: long > 0, short < 0.
- gross = sum(abs(notional_i)) across all positions.
- net = abs(sum(signed_notional_i)) across all positions.
- mark_price from PositionSnap as price source.

Fail-closed: if risk base is unavailable, stale, or below min → block INCREASE_RISK.
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


@dataclass(frozen=True)
class PortfolioRiskConfig:
    """Configuration for portfolio risk enforcement.

    All pct values are fractions (0.10 = 10%).
    A value of 0.0 means the check is disabled.
    """

    symbol_max_notional_pct: float = 0.0
    portfolio_max_gross_notional_pct: float = 0.0
    portfolio_max_net_notional_pct: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "symbol_max_notional_pct",
            "portfolio_max_gross_notional_pct",
            "portfolio_max_net_notional_pct",
        ):
            val = getattr(self, name)
            if val < 0:
                raise ValueError(f"{name} must be >= 0, got {val}")
            if val > 1.0:
                raise ValueError(
                    f"{name}={val} looks like a percentage, not a fraction. "
                    f"Expected 0.0-1.0 (e.g. 0.10 = 10%), got {val}"
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


def evaluate_risk_gate(  # noqa: PLR0911
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

    return RiskGateDecision(allowed=True)
