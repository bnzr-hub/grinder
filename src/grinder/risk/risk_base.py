"""Exchange-balance risk base contract (PR-1 plumbing).

Derives a single risk base value (USD) from the exchange account snapshot.
This value is the denominator for all portfolio and symbol risk limits.

Modes:
- total_margin_balance (default): totalMarginBalance from Binance Futures.
- wallet_balance: totalWalletBalance (more stable, less sensitive to uPnL).
- available_balance: availableBalance (conservative, includes margin impact).

Stale model:
- soft stale (TTL): warn + block INCREASE_RISK (enforced in PR-2).
- hard stale (MAX_AGE): fail-closed marker for PR-2 enforcement.

SSOT: docs/DECISIONS.md (ADR-092)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal  # noqa: TC003 - used at runtime in builder
from enum import Enum

logger = logging.getLogger(__name__)


class RiskBaseMode(Enum):
    """Balance mode for risk base derivation."""

    TOTAL_MARGIN_BALANCE = "total_margin_balance"
    WALLET_BALANCE = "wallet_balance"
    AVAILABLE_BALANCE = "available_balance"


# Canonical mode strings for env parsing
RISK_BASE_MODES: set[str] = {m.value for m in RiskBaseMode}


@dataclass(frozen=True)
class RiskBaseConfig:
    """Configuration for risk base derivation.

    Parsed from environment variables at engine init.
    """

    mode: RiskBaseMode = RiskBaseMode.TOTAL_MARGIN_BALANCE
    min_usd: float = 50.0
    stale_ttl_s: int = 30
    max_age_hard_s: int = 60

    def __post_init__(self) -> None:
        if self.min_usd < 0:
            raise ValueError(f"min_usd must be >= 0, got {self.min_usd}")
        if self.stale_ttl_s < 0:
            raise ValueError(f"stale_ttl_s must be >= 0, got {self.stale_ttl_s}")
        if self.max_age_hard_s < 0:
            raise ValueError(f"max_age_hard_s must be >= 0, got {self.max_age_hard_s}")
        if self.max_age_hard_s < self.stale_ttl_s:
            raise ValueError(
                f"max_age_hard_s ({self.max_age_hard_s}) must be >= stale_ttl_s ({self.stale_ttl_s})"
            )


@dataclass(frozen=True)
class RiskBaseSnapshot:
    """Point-in-time risk base derived from exchange balance.

    API freeze (PR-1): these fields are stable for PR-2 consumption.

    Attributes:
        value_usd: Risk base value in USD (Decimal for precision).
        mode: Which balance mode was used.
        asof_ts_ms: Exchange timestamp of the source balance (ms).
        age_s: Seconds since asof_ts_ms at build time.
        is_stale_soft: True if age_s > config.stale_ttl_s.
        is_stale_hard: True if age_s > config.max_age_hard_s.
        source_fields_present: Which balance fields were available in the source.
        is_below_min: True if value_usd < config.min_usd (informational in PR-1).
    """

    value_usd: Decimal
    mode: str
    asof_ts_ms: int
    age_s: int
    is_stale_soft: bool
    is_stale_hard: bool
    source_fields_present: tuple[str, ...]
    is_below_min: bool = False


# Type alias for balance data fetcher.
# Keys: "total_margin_balance", "wallet_balance", "available_balance"
# Values: Decimal amounts in USD.  ts_ms: exchange timestamp.
@dataclass(frozen=True)
class BalanceData:
    """Raw balance fields from exchange, normalized for risk base builder."""

    total_margin_balance: Decimal | None = None
    wallet_balance: Decimal | None = None
    available_balance: Decimal | None = None
    ts_ms: int = 0


def build_risk_base_snapshot(
    balance: BalanceData,
    config: RiskBaseConfig,
    now_ms: int,
) -> RiskBaseSnapshot | None:
    """Build RiskBaseSnapshot from exchange balance data.

    Returns None if the selected mode's field is missing or <= 0,
    with a structured log explaining why.

    Args:
        balance: Raw balance fields from exchange.
        config: Risk base configuration.
        now_ms: Current wall-clock time in milliseconds.

    Returns:
        RiskBaseSnapshot or None if base cannot be derived.
    """
    # Determine which fields are present
    present: list[str] = []
    if balance.total_margin_balance is not None:
        present.append("total_margin_balance")
    if balance.wallet_balance is not None:
        present.append("wallet_balance")
    if balance.available_balance is not None:
        present.append("available_balance")

    # Select value by mode
    mode = config.mode
    value: Decimal | None = None
    if mode == RiskBaseMode.TOTAL_MARGIN_BALANCE:
        value = balance.total_margin_balance
    elif mode == RiskBaseMode.WALLET_BALANCE:
        value = balance.wallet_balance
    elif mode == RiskBaseMode.AVAILABLE_BALANCE:
        value = balance.available_balance

    if value is None or value <= 0:
        logger.warning(
            "RISK_BASE_UNAVAILABLE mode=%s reason=%s present=%s",
            mode.value,
            "missing" if value is None else "non_positive",
            ",".join(present) or "none",
        )
        return None

    # Compute age
    age_s = max(0, (now_ms - balance.ts_ms) // 1000) if balance.ts_ms > 0 else 0

    return RiskBaseSnapshot(
        value_usd=value,
        mode=mode.value,
        asof_ts_ms=balance.ts_ms,
        age_s=age_s,
        is_stale_soft=age_s > config.stale_ttl_s,
        is_stale_hard=age_s > config.max_age_hard_s,
        source_fields_present=tuple(present),
        is_below_min=float(value) < config.min_usd,
    )
