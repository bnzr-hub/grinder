"""Tests for order identity module (LC-12).

Tests cover:
- OrderIdentityConfig validation
- parse_client_order_id() for v1 and legacy formats
- is_ours() allowlist checking
- generate_client_order_id() formatting
- Legacy format handling with/without env var
"""

import os
import re
from collections.abc import Generator

import pytest

from grinder.reconcile.identity import (
    BINANCE_MAX_CLIENT_ORDER_ID_LEN,
    DEFAULT_PREFIX,
    DEFAULT_STRATEGY_ID,
    ENV_ALLOW_LEGACY_ORDER_ID,
    LEGACY_STRATEGY_ID,
    OrderIdentityConfig,
    generate_client_order_id,
    get_default_identity_config,
    is_ours,
    is_tp_order,
    normalize_symbol_for_cid,
    parse_client_order_id,
    reset_default_identity_config,
    set_default_identity_config,
)


@pytest.fixture(autouse=True)
def reset_identity_config() -> Generator[None, None, None]:
    """Reset identity config before and after each test."""
    reset_default_identity_config()
    # Clear env var if set
    old_env = os.environ.pop(ENV_ALLOW_LEGACY_ORDER_ID, None)
    yield
    reset_default_identity_config()
    if old_env is not None:
        os.environ[ENV_ALLOW_LEGACY_ORDER_ID] = old_env
    else:
        os.environ.pop(ENV_ALLOW_LEGACY_ORDER_ID, None)


# =============================================================================
# OrderIdentityConfig Tests
# =============================================================================


class TestOrderIdentityConfig:
    """Tests for OrderIdentityConfig dataclass."""

    def test_default_values(self) -> None:
        """Default config has expected values."""
        config = OrderIdentityConfig()
        assert config.prefix == DEFAULT_PREFIX
        assert config.strategy_id == DEFAULT_STRATEGY_ID
        assert config.allowed_strategies == {DEFAULT_STRATEGY_ID}
        assert config.require_strategy_allowlist is True
        assert config.allow_legacy_format is False
        assert config.identity_format_version == 1

    def test_prefix_normalized_with_underscore(self) -> None:
        """Prefix without trailing underscore gets one added."""
        config = OrderIdentityConfig(prefix="mybot")
        assert config.prefix == "mybot_"

    def test_prefix_already_has_underscore(self) -> None:
        """Prefix with trailing underscore stays unchanged."""
        config = OrderIdentityConfig(prefix="mybot_")
        assert config.prefix == "mybot_"

    def test_allowed_strategies_defaults_to_strategy_id(self) -> None:
        """Empty allowed_strategies defaults to {strategy_id}."""
        config = OrderIdentityConfig(strategy_id="momentum")
        assert config.allowed_strategies == {"momentum"}

    def test_allowed_strategies_explicit(self) -> None:
        """Explicit allowed_strategies is preserved."""
        config = OrderIdentityConfig(
            strategy_id="momentum",
            allowed_strategies={"momentum", "scalper", "arb"},
        )
        assert config.allowed_strategies == {"momentum", "scalper", "arb"}

    def test_is_strategy_allowed_in_list(self) -> None:
        """Strategy in allowlist returns True."""
        config = OrderIdentityConfig(
            strategy_id="momentum",
            allowed_strategies={"momentum", "scalper"},
        )
        assert config.is_strategy_allowed("momentum") is True
        assert config.is_strategy_allowed("scalper") is True

    def test_is_strategy_allowed_not_in_list(self) -> None:
        """Strategy not in allowlist returns False."""
        config = OrderIdentityConfig(
            strategy_id="momentum",
            allowed_strategies={"momentum"},
        )
        assert config.is_strategy_allowed("rogue") is False

    def test_is_strategy_allowed_no_requirement(self) -> None:
        """With require_strategy_allowlist=False, any strategy allowed."""
        config = OrderIdentityConfig(
            strategy_id="momentum",
            require_strategy_allowlist=False,
        )
        assert config.is_strategy_allowed("anything") is True
        assert config.is_strategy_allowed("random") is True

    def test_legacy_strategy_blocked_by_default(self) -> None:
        """Legacy strategy marker is blocked by default."""
        config = OrderIdentityConfig()
        assert config.is_strategy_allowed(LEGACY_STRATEGY_ID) is False

    def test_legacy_strategy_allowed_with_flag(self) -> None:
        """Legacy strategy marker allowed when allow_legacy_format=True."""
        config = OrderIdentityConfig(allow_legacy_format=True)
        assert config.is_strategy_allowed(LEGACY_STRATEGY_ID) is True

    def test_env_var_enables_legacy(self) -> None:
        """ALLOW_LEGACY_ORDER_ID=1 enables legacy format."""
        os.environ[ENV_ALLOW_LEGACY_ORDER_ID] = "1"
        config = OrderIdentityConfig()
        assert config.allow_legacy_format is True
        assert config.is_strategy_allowed(LEGACY_STRATEGY_ID) is True


def test_generate_client_order_id_normalizes_non_ascii_symbol() -> None:
    """Non-ASCII symbols are normalized to CID-safe tokens."""
    config = OrderIdentityConfig(strategy_id="d")
    cid = generate_client_order_id(config, "币安人生USDT", 1, 1704067200000, 1)
    parsed = parse_client_order_id(cid)
    assert parsed is not None
    assert normalize_symbol_for_cid("币安人生USDT").startswith(parsed.symbol)
    assert re.fullmatch(r"[A-Z0-9]+", parsed.symbol)
    assert len(cid) <= BINANCE_MAX_CLIENT_ORDER_ID_LEN


# =============================================================================
# parse_client_order_id Tests
# =============================================================================


class TestParseClientOrderId:
    """Tests for parse_client_order_id function."""

    def test_parse_v1_format(self) -> None:
        """Parse v1 format with strategy_id."""
        client_order_id = "grinder_momentum_BTCUSDT_1_1704067200000_42"
        parsed = parse_client_order_id(client_order_id)

        assert parsed is not None
        assert parsed.prefix == "grinder_"
        assert parsed.strategy_id == "momentum"
        assert parsed.symbol == "BTCUSDT"
        assert parsed.level_id == "1"
        assert parsed.ts == 1704067200000
        assert parsed.seq == 42
        assert parsed.is_legacy is False

    def test_parse_v1_format_cleanup(self) -> None:
        """Parse v1 format with cleanup level_id."""
        client_order_id = "grinder_scalper_ETHUSDT_cleanup_1704067200000_5"
        parsed = parse_client_order_id(client_order_id)

        assert parsed is not None
        assert parsed.prefix == "grinder_"
        assert parsed.strategy_id == "scalper"
        assert parsed.symbol == "ETHUSDT"
        assert parsed.level_id == "cleanup"
        assert parsed.ts == 1704067200000
        assert parsed.seq == 5
        assert parsed.is_legacy is False

    def test_parse_v1_format_custom_prefix(self) -> None:
        """Parse v1 format with custom prefix."""
        client_order_id = "mybot_arb_BTCUSDT_2_1704067200000_1"
        parsed = parse_client_order_id(client_order_id)

        assert parsed is not None
        assert parsed.prefix == "mybot_"
        assert parsed.strategy_id == "arb"
        assert parsed.symbol == "BTCUSDT"

    def test_parse_legacy_format(self) -> None:
        """Parse legacy format without strategy_id."""
        client_order_id = "grinder_BTCUSDT_1_1704067200000_42"
        parsed = parse_client_order_id(client_order_id)

        assert parsed is not None
        assert parsed.prefix == "grinder_"
        assert parsed.strategy_id == LEGACY_STRATEGY_ID
        assert parsed.symbol == "BTCUSDT"
        assert parsed.level_id == "1"
        assert parsed.ts == 1704067200000
        assert parsed.seq == 42
        assert parsed.is_legacy is True

    def test_parse_legacy_format_cleanup(self) -> None:
        """Parse legacy format with cleanup level_id."""
        client_order_id = "grinder_ETHUSDT_cleanup_1704067200000_5"
        parsed = parse_client_order_id(client_order_id)

        assert parsed is not None
        assert parsed.prefix == "grinder_"
        assert parsed.strategy_id == LEGACY_STRATEGY_ID
        assert parsed.symbol == "ETHUSDT"
        assert parsed.level_id == "cleanup"
        assert parsed.is_legacy is True

    def test_parse_empty_string(self) -> None:
        """Empty string returns None."""
        assert parse_client_order_id("") is None

    def test_parse_invalid_format(self) -> None:
        """Invalid format returns None."""
        assert parse_client_order_id("random_order_123") is None
        assert parse_client_order_id("not_enough_parts") is None
        assert parse_client_order_id("manual_order") is None

    def test_parse_manual_order(self) -> None:
        """Manual order from web UI returns None."""
        assert parse_client_order_id("web_123456789") is None
        assert parse_client_order_id("ios_abcdef") is None

    def test_parse_other_bot_order(self) -> None:
        """Other bot order with different prefix returns None for legacy pattern."""
        # This will be parsed as v1 format, not legacy
        # otherbot_BTCUSDT_1_... has "BTCUSDT" as strategy_id
        result = parse_client_order_id("otherbot_BTCUSDT_1_1704067200000_1")
        # This matches v1 pattern: prefix=otherbot_, strategy=BTCUSDT, symbol=1, etc.
        # Actually this doesn't match because "1" is not a valid symbol (needs uppercase letters)
        assert result is None


# =============================================================================
# is_ours Tests
# =============================================================================


class TestIsOurs:
    """Tests for is_ours function."""

    def test_v1_format_our_strategy(self) -> None:
        """V1 format with our strategy returns True."""
        config = OrderIdentityConfig(strategy_id="momentum")
        assert is_ours("grinder_momentum_BTCUSDT_1_1704067200000_1", config) is True

    def test_v1_format_other_strategy_blocked(self) -> None:
        """V1 format with different strategy returns False."""
        config = OrderIdentityConfig(strategy_id="momentum")
        assert is_ours("grinder_rogue_BTCUSDT_1_1704067200000_1", config) is False

    def test_v1_format_in_allowlist(self) -> None:
        """V1 format with strategy in allowlist returns True."""
        config = OrderIdentityConfig(
            strategy_id="momentum",
            allowed_strategies={"momentum", "scalper"},
        )
        assert is_ours("grinder_momentum_BTCUSDT_1_1704067200000_1", config) is True
        assert is_ours("grinder_scalper_ETHUSDT_1_1704067200000_1", config) is True

    def test_wrong_prefix_blocked(self) -> None:
        """Different prefix returns False even if strategy matches."""
        config = OrderIdentityConfig(prefix="grinder_", strategy_id="momentum")
        assert is_ours("otherbot_momentum_BTCUSDT_1_1704067200000_1", config) is False

    def test_legacy_format_blocked_by_default(self) -> None:
        """Legacy format blocked when allow_legacy_format=False."""
        config = OrderIdentityConfig()
        # Legacy orders have LEGACY_STRATEGY_ID which is not in default allowlist
        assert is_ours("grinder_BTCUSDT_1_1704067200000_1", config) is False

    def test_legacy_format_allowed_with_flag(self) -> None:
        """Legacy format allowed when allow_legacy_format=True."""
        config = OrderIdentityConfig(allow_legacy_format=True)
        assert is_ours("grinder_BTCUSDT_1_1704067200000_1", config) is True

    def test_legacy_format_allowed_with_env_var(self) -> None:
        """Legacy format allowed when ALLOW_LEGACY_ORDER_ID=1."""
        os.environ[ENV_ALLOW_LEGACY_ORDER_ID] = "1"
        config = OrderIdentityConfig()
        assert is_ours("grinder_BTCUSDT_1_1704067200000_1", config) is True

    def test_unparseable_returns_false(self) -> None:
        """Unparseable order ID returns False."""
        config = OrderIdentityConfig()
        assert is_ours("random_garbage", config) is False
        assert is_ours("", config) is False

    def test_manual_order_returns_false(self) -> None:
        """Manual orders from web UI return False."""
        config = OrderIdentityConfig()
        assert is_ours("web_123456789", config) is False

    def test_require_allowlist_false_allows_any(self) -> None:
        """With require_strategy_allowlist=False, any valid format passes."""
        config = OrderIdentityConfig(
            strategy_id="momentum",
            require_strategy_allowlist=False,
        )
        assert is_ours("grinder_momentum_BTCUSDT_1_1704067200000_1", config) is True
        assert is_ours("grinder_unknown_BTCUSDT_1_1704067200000_1", config) is True


# =============================================================================
# generate_client_order_id Tests
# =============================================================================


class TestGenerateClientOrderId:
    """Tests for generate_client_order_id function."""

    def test_generate_v1_format(self) -> None:
        """Generate v1 format — ts millis truncated to seconds."""
        config = OrderIdentityConfig(
            prefix="grinder_",
            strategy_id="m",
        )
        result = generate_client_order_id(
            config=config,
            symbol="BTCUSDT",
            level_id=1,
            ts=1704067200000,
            seq=42,
        )
        assert result == "grinder_m_BTCUSDT_1_1704067200_42"

    def test_generate_with_string_level_id(self) -> None:
        """Generate with short string level_id (e.g., c for cleanup)."""
        config = OrderIdentityConfig(strategy_id="s")
        result = generate_client_order_id(
            config=config,
            symbol="ETHUSDT",
            level_id="c",
            ts=1704067200000,
            seq=5,
        )
        assert result == "grinder_s_ETHUSDT_c_1704067200_5"

    def test_generate_with_custom_prefix(self) -> None:
        """Generate with custom prefix."""
        config = OrderIdentityConfig(
            prefix="mybot_",
            strategy_id="a",
        )
        result = generate_client_order_id(
            config=config,
            symbol="BTCUSDT",
            level_id=2,
            ts=1704067200000,
            seq=1,
        )
        assert result == "mybot_a_BTCUSDT_2_1704067200_1"

    def test_generate_then_parse_roundtrip(self) -> None:
        """Generated ID can be parsed back correctly."""
        config = OrderIdentityConfig(strategy_id="t")
        generated = generate_client_order_id(
            config=config,
            symbol="BTCUSDT",
            level_id=5,
            ts=1704067200000,
            seq=99,
        )
        parsed = parse_client_order_id(generated)

        assert parsed is not None
        assert parsed.prefix == config.prefix
        assert parsed.strategy_id == config.strategy_id
        assert parsed.symbol == "BTCUSDT"
        assert parsed.level_id == "5"
        assert parsed.ts == 1704067200  # seconds, not millis
        assert parsed.seq == 99
        assert parsed.is_legacy is False

    def test_generate_then_is_ours_returns_true(self) -> None:
        """Generated ID passes is_ours check."""
        config = OrderIdentityConfig(strategy_id="m")
        generated = generate_client_order_id(
            config=config,
            symbol="BTCUSDT",
            level_id=1,
            ts=1704067200000,
            seq=1,
        )
        assert is_ours(generated, config) is True

    def test_generate_respects_36_char_limit(self) -> None:
        """Generated ID never exceeds Binance 36-char limit."""
        config = OrderIdentityConfig(strategy_id="d")
        result = generate_client_order_id(
            config=config,
            symbol="BTCUSDT",
            level_id=1,
            ts=1772292240701,
            seq=1,
        )
        assert len(result) <= BINANCE_MAX_CLIENT_ORDER_ID_LEN
        assert result == "grinder_d_BTCUSDT_1_1772292240_1"

    def test_generate_raises_if_too_long(self) -> None:
        """ValueError raised when generated ID exceeds 36 chars."""
        config = OrderIdentityConfig(
            prefix="grinder_",
            strategy_id="toolong",
        )
        with pytest.raises(ValueError, match="clientOrderId too long"):
            generate_client_order_id(
                config=config,
                symbol="BTCUSDT",
                level_id=1,
                ts=1772292240000,
                seq=1,
            )


# =============================================================================
# Default Config Singleton Tests
# =============================================================================


class TestDefaultConfig:
    """Tests for default config singleton."""

    def test_get_default_creates_config(self) -> None:
        """get_default_identity_config creates a default config."""
        config = get_default_identity_config()
        assert config is not None
        assert config.prefix == DEFAULT_PREFIX
        assert config.strategy_id == DEFAULT_STRATEGY_ID

    def test_get_default_returns_same_instance(self) -> None:
        """get_default_identity_config returns same instance."""
        config1 = get_default_identity_config()
        config2 = get_default_identity_config()
        assert config1 is config2

    def test_set_default_replaces_config(self) -> None:
        """set_default_identity_config replaces the singleton."""
        custom = OrderIdentityConfig(strategy_id="custom")
        set_default_identity_config(custom)
        assert get_default_identity_config() is custom

    def test_reset_default_clears_config(self) -> None:
        """reset_default_identity_config clears the singleton."""
        _ = get_default_identity_config()  # Create default
        reset_default_identity_config()
        # Next call creates a new instance
        config = get_default_identity_config()
        assert config.strategy_id == DEFAULT_STRATEGY_ID


# =============================================================================
# Edge Cases and Security Tests
# =============================================================================


class TestSecurityEdgeCases:
    """Security-focused edge case tests."""

    def test_empty_allowlist_uses_strategy_id(self) -> None:
        """Empty allowlist defaults to strategy_id, not open access."""
        config = OrderIdentityConfig(
            strategy_id="safe",
            allowed_strategies=set(),  # Empty
        )
        # After __post_init__, allowed_strategies should be {"safe"}
        assert config.allowed_strategies == {"safe"}
        assert is_ours("grinder_safe_BTCUSDT_1_1704067200000_1", config) is True
        assert is_ours("grinder_evil_BTCUSDT_1_1704067200000_1", config) is False

    def test_prefix_mismatch_always_blocked(self) -> None:
        """Wrong prefix is always blocked, even with open allowlist."""
        config = OrderIdentityConfig(
            prefix="grinder_",
            require_strategy_allowlist=False,
        )
        assert is_ours("otherbot_momentum_BTCUSDT_1_1704067200000_1", config) is False

    def test_injection_attempt_blocked(self) -> None:
        """Malformed IDs attempting injection are blocked."""
        config = OrderIdentityConfig()
        # Attempt to inject extra underscores or special chars
        assert is_ours("grinder_mo_mentum_BTCUSDT_1_1704067200000_1", config) is False
        assert is_ours("grinder_momen..tum_BTCUSDT_1_1704067200000_1", config) is False

    def test_legacy_only_grinder_prefix(self) -> None:
        """Legacy pattern only matches grinder_ prefix."""
        config = OrderIdentityConfig(allow_legacy_format=True)
        # This should NOT be parsed as legacy (otherbot_ doesn't match legacy pattern)
        assert is_ours("otherbot_BTCUSDT_1_1704067200000_1", config) is False

    def test_case_sensitivity(self) -> None:
        """Strategy IDs and symbols are case-sensitive."""
        config = OrderIdentityConfig(strategy_id="Momentum")
        assert is_ours("grinder_Momentum_BTCUSDT_1_1704067200000_1", config) is True
        assert is_ours("grinder_momentum_BTCUSDT_1_1704067200000_1", config) is False
        assert is_ours("grinder_MOMENTUM_BTCUSDT_1_1704067200000_1", config) is False


# =============================================================================
# is_tp_order Tests (PR-INV-3)
# =============================================================================


class TestIsTpOrder:
    """Tests for is_tp_order function."""

    def test_tp_order_detected(self) -> None:
        """TP order with strategy_id='tp' returns True."""
        assert is_tp_order("grinder_tp_BTCUSDT_3_1000_1") is True

    def test_grid_order_not_tp(self) -> None:
        """Grid order with strategy_id='d' returns False."""
        assert is_tp_order("grinder_d_BTCUSDT_3_1000_1") is False

    def test_foreign_order_not_tp(self) -> None:
        """Unparseable order returns False."""
        assert is_tp_order("manual_123") is False

    def test_empty_string_not_tp(self) -> None:
        """Empty string returns False."""
        assert is_tp_order("") is False
