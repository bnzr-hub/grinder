"""Order identity configuration and parsing.

See ADR-045 for design decisions.

Strategy reservations:
- "g" is reserved for grid_v2 (see doc-27 section 22.1).

This module provides:
- OrderIdentityConfig: Configuration for order identity (prefix, strategy, allowlist)
- ParsedOrderId: Parsed components of a clientOrderId
- parse_client_order_id(): Parse clientOrderId into components
- is_ours(): Check if an order belongs to our allowed strategies
- generate_client_order_id(): Generate a clientOrderId with identity

Format v1: {prefix}{strategy_id}_{symbol}_{level_id}_{ts_seconds}_{seq}
Example: grinder_d_BTCUSDT_1_1704067200_1

Legacy format: grinder_{symbol}_{level_id}_{ts}_{seq}
Example: grinder_BTCUSDT_1_1704067200000_1
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Environment variable for legacy support
ENV_ALLOW_LEGACY_ORDER_ID = "ALLOW_LEGACY_ORDER_ID"

# Default values
DEFAULT_PREFIX = "grinder_"
DEFAULT_STRATEGY_ID = "d"

# Binance enforces clientOrderId <= 36 chars (error -4015).
BINANCE_MAX_CLIENT_ORDER_ID_LEN = 36
LEGACY_STRATEGY_ID = "__legacy__"  # Internal marker for legacy orders


@dataclass
class OrderIdentityConfig:
    """Configuration for order identity.

    Attributes:
        prefix: Order ID prefix (default: "grinder_")
        strategy_id: Strategy identifier (default: "default")
        allowed_strategies: Set of allowed strategy IDs for remediation.
            If empty and require_strategy_allowlist=True, only self.strategy_id is allowed.
        require_strategy_allowlist: If True, strategy must be in allowlist (default: True)
        allow_legacy_format: Allow legacy format without strategy_id (default: False)
            Can also be enabled via ALLOW_LEGACY_ORDER_ID=1 env var.
        identity_format_version: Format version for clientOrderId (default: 1)
    """

    prefix: str = DEFAULT_PREFIX
    strategy_id: str = DEFAULT_STRATEGY_ID
    allowed_strategies: set[str] = field(default_factory=set)
    require_strategy_allowlist: bool = True
    allow_legacy_format: bool = False
    identity_format_version: int = 1

    def __post_init__(self) -> None:
        """Validate and normalize config."""
        # Ensure prefix ends with underscore for clean separation
        if self.prefix and not self.prefix.endswith("_"):
            object.__setattr__(self, "prefix", self.prefix + "_")

        # If allowed_strategies is empty, default to {strategy_id}
        if not self.allowed_strategies:
            object.__setattr__(self, "allowed_strategies", {self.strategy_id})

        # Check env var for legacy support
        if os.environ.get(ENV_ALLOW_LEGACY_ORDER_ID) == "1":
            object.__setattr__(self, "allow_legacy_format", True)

    def is_strategy_allowed(self, strategy_id: str) -> bool:
        """Check if a strategy ID is in the allowlist.

        Args:
            strategy_id: Strategy ID to check

        Returns:
            True if strategy is allowed
        """
        if not self.require_strategy_allowlist:
            return True

        # Legacy marker is allowed if allow_legacy_format is True
        if strategy_id == LEGACY_STRATEGY_ID:
            return self.allow_legacy_format

        return strategy_id in self.allowed_strategies


@dataclass(frozen=True)
class ParsedOrderId:
    """Parsed components of a clientOrderId.

    Attributes:
        prefix: Order ID prefix (e.g., "grinder_")
        strategy_id: Strategy identifier (e.g., "momentum")
        symbol: Trading symbol (e.g., "BTCUSDT")
        level_id: Level/shard ID (e.g., "1" or "cleanup")
        ts: Timestamp in milliseconds
        seq: Sequence number
        is_legacy: True if parsed from legacy format (no strategy_id)
    """

    prefix: str
    strategy_id: str
    symbol: str
    level_id: str
    ts: int
    seq: int
    is_legacy: bool = False


# Regex patterns for parsing
# v1 format: {prefix}{strategy_id}_{symbol}_{level_id}_{ts}_{seq}
# Note: strategy_id cannot contain underscore (separator)
_V1_PATTERN = re.compile(
    r"^(?P<prefix>\w+_)(?P<strategy_id>[^_]+)_(?P<symbol>[A-Z0-9]+)_(?P<level_id>\w+)_(?P<ts>\d+)_(?P<seq>\d+)$"
)

# Legacy format: grinder_{symbol}_{level_id}_{ts}_{seq}
_LEGACY_PATTERN = re.compile(
    r"^(?P<prefix>grinder_)(?P<symbol>[A-Z0-9]+)_(?P<level_id>\w+)_(?P<ts>\d+)_(?P<seq>\d+)$"
)


def parse_client_order_id(client_order_id: str) -> ParsedOrderId | None:
    """Parse a clientOrderId into its components.

    Tries v1 format first, then falls back to legacy format.

    Args:
        client_order_id: The clientOrderId string to parse

    Returns:
        ParsedOrderId if valid, None if unparseable
    """
    if not client_order_id:
        return None

    # Try v1 format first
    match = _V1_PATTERN.match(client_order_id)
    if match:
        return ParsedOrderId(
            prefix=match.group("prefix"),
            strategy_id=match.group("strategy_id"),
            symbol=match.group("symbol"),
            level_id=match.group("level_id"),
            ts=int(match.group("ts")),
            seq=int(match.group("seq")),
            is_legacy=False,
        )

    # Try legacy format
    match = _LEGACY_PATTERN.match(client_order_id)
    if match:
        return ParsedOrderId(
            prefix=match.group("prefix"),
            strategy_id=LEGACY_STRATEGY_ID,
            symbol=match.group("symbol"),
            level_id=match.group("level_id"),
            ts=int(match.group("ts")),
            seq=int(match.group("seq")),
            is_legacy=True,
        )

    return None


TP_STRATEGY_ID = "tp"


def is_tp_order(client_order_id: str) -> bool:
    """Check if a clientOrderId belongs to the TP cycle layer (PR-INV-3).

    TP orders use strategy_id="tp" -> format: grinder_tp_BTCUSDT_3_ts_seq.
    Returns False for unparseable or non-TP orders.
    """
    parsed = parse_client_order_id(client_order_id)
    return parsed is not None and parsed.strategy_id == TP_STRATEGY_ID


def is_ours(
    client_order_id: str,
    config: OrderIdentityConfig,
) -> bool:
    """Check if an order belongs to our allowed strategies.

    An order is "ours" if:
    1. It can be parsed
    2. Its prefix matches config.prefix
    3. Its strategy_id is in the allowlist (or legacy is allowed)

    Args:
        client_order_id: The clientOrderId to check
        config: Identity configuration

    Returns:
        True if the order is ours
    """
    parsed = parse_client_order_id(client_order_id)
    if parsed is None:
        return False

    # Check prefix matches
    if parsed.prefix != config.prefix:
        return False

    # Check strategy is allowed
    return config.is_strategy_allowed(parsed.strategy_id)


def generate_client_order_id(
    config: OrderIdentityConfig,
    symbol: str,
    level_id: str | int,
    ts: int,
    seq: int,
) -> str:
    """Generate a clientOrderId with the configured identity.

    Args:
        config: Identity configuration
        symbol: Trading symbol (e.g., "BTCUSDT")
        level_id: Level/shard ID (e.g., 1 or "cleanup")
        ts: Timestamp in milliseconds
        seq: Sequence number

    Returns:
        Formatted clientOrderId (guaranteed <= 36 chars for Binance)

    Raises:
        ValueError: If generated ID exceeds 36 characters

    Example:
        >>> config = OrderIdentityConfig(strategy_id="d")
        >>> generate_client_order_id(config, "BTCUSDT", 1, 1704067200000, 1)
        'grinder_d_BTCUSDT_1_1704067200_1'
    """
    # Truncate millis → seconds to save 3 chars (Binance 36-char limit).
    ts_sec = ts // 1000
    cid = f"{config.prefix}{config.strategy_id}_{symbol}_{level_id}_{ts_sec}_{seq}"
    if len(cid) > BINANCE_MAX_CLIENT_ORDER_ID_LEN:
        raise ValueError(
            f"clientOrderId too long ({len(cid)} chars, max {BINANCE_MAX_CLIENT_ORDER_ID_LEN}): {cid}"
        )
    return cid


# Singleton default config (can be replaced at runtime)
_default_config: OrderIdentityConfig | None = None


def get_default_identity_config() -> OrderIdentityConfig:
    """Get the default identity configuration.

    Returns a singleton instance. Use set_default_identity_config() to customize.
    """
    global _default_config  # noqa: PLW0603 - singleton pattern
    if _default_config is None:
        _default_config = OrderIdentityConfig()
    return _default_config


def set_default_identity_config(config: OrderIdentityConfig) -> None:
    """Set the default identity configuration.

    This should be called once at startup before any orders are placed.
    """
    global _default_config  # noqa: PLW0603 - singleton pattern
    _default_config = config


def reset_default_identity_config() -> None:
    """Reset the default identity config to None (for testing)."""
    global _default_config  # noqa: PLW0603 - singleton pattern
    _default_config = None
