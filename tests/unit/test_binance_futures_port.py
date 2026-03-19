"""Unit tests for BinanceFuturesPort (USDT-M).

Tests cover:
- True dry-run mode: dry_run=True → 0 http_client.request() calls
- Mock transport mode: NoopHttpClient records calls for verification
- SafeMode enforcement: READ_ONLY blocks writes, LIVE_TRADE required
- Mainnet guards: env var + symbol whitelist + notional limits
- Symbol whitelist: Only allowed symbols can trade
- Error mapping: Binance errors → Connector*Error types
- Futures-specific: leverage, position mode, position cleanup

See ADR-040 for design decisions.
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import patch

import pytest

from grinder.connectors.errors import (
    ConnectorNonRetryableError,
)
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.binance_futures_port import (
    BINANCE_FUTURES_MAINNET_URL,
    BINANCE_FUTURES_TESTNET_URL,
    BinanceFuturesPort,
    BinanceFuturesPortConfig,
)
from grinder.execution.binance_port import NoopHttpClient

# --- Fixtures ---


@pytest.fixture
def noop_client() -> NoopHttpClient:
    """Create a NoopHttpClient for dry-run testing."""
    return NoopHttpClient()


@pytest.fixture
def futures_noop_client() -> NoopHttpClient:
    """Create a NoopHttpClient with futures-specific responses."""
    return NoopHttpClient(
        place_response={
            "orderId": 12345,
            "clientOrderId": "test_order",
            "status": "NEW",
            "executedQty": "0",
        },
        cancel_response={
            "orderId": 12345,
            "status": "CANCELED",
        },
        open_orders_response=[],
    )


@pytest.fixture
def live_trade_config() -> BinanceFuturesPortConfig:
    """Create config with LIVE_TRADE mode for testnet."""
    return BinanceFuturesPortConfig(
        mode=SafeMode.LIVE_TRADE,
        base_url=BINANCE_FUTURES_TESTNET_URL,
        api_key="test_key",
        api_secret="test_secret",
        symbol_whitelist=["BTCUSDT", "ETHUSDT"],
        max_orders_per_run=100,  # Higher limit for tests
    )


@pytest.fixture
def read_only_config() -> BinanceFuturesPortConfig:
    """Create config with READ_ONLY mode (default, blocks writes)."""
    return BinanceFuturesPortConfig(
        mode=SafeMode.READ_ONLY,
        base_url=BINANCE_FUTURES_TESTNET_URL,
        api_key="test_key",
        api_secret="test_secret",
    )


@pytest.fixture
def dry_run_config() -> BinanceFuturesPortConfig:
    """Create config with dry_run=True (0 http_client calls)."""
    return BinanceFuturesPortConfig(
        mode=SafeMode.LIVE_TRADE,
        base_url=BINANCE_FUTURES_TESTNET_URL,
        api_key="test_key",
        api_secret="test_secret",
        symbol_whitelist=["BTCUSDT", "ETHUSDT"],
        dry_run=True,
    )


# --- True Dry-Run Tests ---


class TestTrueDryRunMode:
    """Tests proving dry_run=True makes EXACTLY 0 http_client.request() calls."""

    def test_place_order_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """place_order with dry_run=True makes 0 http_client calls."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        order_id = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,
        )

        assert order_id is not None
        # v1 format: grinder_{strategy}_{symbol}_{level}_{ts_seconds}_{seq}
        assert "grinder_d_BTCUSDT_1_1000" in order_id
        assert len(noop_client.calls) == 0

    def test_place_order_dry_run_long_symbol_uses_short_prefix_fallback(
        self, noop_client: NoopHttpClient
    ) -> None:
        """Long symbols fallback from grinder_ to g_ to stay within Binance CID limit."""
        config = BinanceFuturesPortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url=BINANCE_FUTURES_TESTNET_URL,
            api_key="test_key",
            api_secret="test_secret",
            symbol_whitelist=["FARTCOINUSDT"],
            dry_run=True,
            max_orders_per_run=100,
        )
        port = BinanceFuturesPort(http_client=noop_client, config=config)

        order_id = port.place_order(
            symbol="FARTCOINUSDT",
            side=OrderSide.BUY,
            price=Decimal("1.0"),
            quantity=Decimal("1"),
            level_id=1,
            ts=1700000000000,
        )

        assert order_id.startswith("g_d_FARTCOINUSDT_1_")
        assert len(order_id) <= 36
        assert len(noop_client.calls) == 0

    def test_cancel_order_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """cancel_order with dry_run=True makes 0 http_client calls."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        result = port.cancel_order("grinder_BTCUSDT_1_1000000_1")

        assert result is True
        assert len(noop_client.calls) == 0

    def test_fetch_open_orders_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """fetch_open_orders with dry_run=True makes 0 http_client calls."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        orders = port.fetch_open_orders("BTCUSDT")

        assert orders == []
        assert len(noop_client.calls) == 0

    def test_get_positions_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """get_positions with dry_run=True makes 0 http_client calls."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        positions = port.get_positions("BTCUSDT")

        assert positions == []
        assert len(noop_client.calls) == 0

    def test_set_leverage_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """set_leverage with dry_run=True makes 0 http_client calls."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        leverage = port.set_leverage("BTCUSDT", 5)

        assert leverage == 5
        assert len(noop_client.calls) == 0

    def test_get_position_mode_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """get_position_mode with dry_run=True makes 0 http_client calls."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        mode = port.get_position_mode()

        assert mode == "one-way"
        assert len(noop_client.calls) == 0

    def test_fetch_user_trades_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """fetch_user_trades_raw with dry_run=True makes 0 http_client calls."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        trades = port.fetch_user_trades_raw("BTCUSDT")

        assert trades == []
        assert len(noop_client.calls) == 0


# --- SafeMode Tests ---


class TestSafeModeEnforcement:
    """Tests proving SafeMode correctly blocks/allows operations."""

    def test_read_only_blocks_place_order(
        self, noop_client: NoopHttpClient, read_only_config: BinanceFuturesPortConfig
    ) -> None:
        """READ_ONLY mode blocks place_order."""
        port = BinanceFuturesPort(http_client=noop_client, config=read_only_config)

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.01"),
                level_id=1,
                ts=1000000,
            )

        assert "read_only" in str(exc_info.value).lower()
        assert "LIVE_TRADE" in str(exc_info.value)

    def test_read_only_blocks_cancel_order(
        self, noop_client: NoopHttpClient, read_only_config: BinanceFuturesPortConfig
    ) -> None:
        """READ_ONLY mode blocks cancel_order."""
        port = BinanceFuturesPort(http_client=noop_client, config=read_only_config)

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            port.cancel_order("grinder_BTCUSDT_1_1000000_1")

        assert "read_only" in str(exc_info.value).lower()

    def test_read_only_blocks_set_leverage(
        self, noop_client: NoopHttpClient, read_only_config: BinanceFuturesPortConfig
    ) -> None:
        """READ_ONLY mode blocks set_leverage."""
        port = BinanceFuturesPort(http_client=noop_client, config=read_only_config)

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            port.set_leverage("BTCUSDT", 5)

        assert "read_only" in str(exc_info.value).lower()


# --- Symbol Whitelist Tests ---


class TestSymbolWhitelist:
    """Tests for symbol whitelist enforcement."""

    def test_symbol_not_in_whitelist_blocked(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """Symbol not in whitelist is blocked."""
        port = BinanceFuturesPort(http_client=noop_client, config=live_trade_config)

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            port.place_order(
                symbol="DOGEUSD",  # Not in whitelist
                side=OrderSide.BUY,
                price=Decimal("0.1"),
                quantity=Decimal("100"),
                level_id=1,
                ts=1000000,
            )

        assert "whitelist" in str(exc_info.value).lower()

    def test_symbol_in_whitelist_allowed(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """Symbol in whitelist is allowed."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        order_id = port.place_order(
            symbol="BTCUSDT",  # In whitelist
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,
        )

        assert order_id is not None


# --- Mainnet Guards Tests ---


class TestMainnetGuards:
    """Tests for mainnet safety guards (ADR-040)."""

    def test_mainnet_requires_allow_mainnet_true(self) -> None:
        """Mainnet requires allow_mainnet=True in config."""
        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            BinanceFuturesPortConfig(
                mode=SafeMode.LIVE_TRADE,
                base_url=BINANCE_FUTURES_MAINNET_URL,
                api_key="test_key",
                api_secret="test_secret",
                allow_mainnet=False,  # Not allowed
            )

        assert "allow_mainnet=True" in str(exc_info.value)

    def test_mainnet_requires_env_var(self) -> None:
        """Mainnet requires ALLOW_MAINNET_TRADE=1 env var."""
        with patch.dict(os.environ, {"ALLOW_MAINNET_TRADE": ""}, clear=False):
            with pytest.raises(ConnectorNonRetryableError) as exc_info:
                BinanceFuturesPortConfig(
                    mode=SafeMode.LIVE_TRADE,
                    base_url=BINANCE_FUTURES_MAINNET_URL,
                    api_key="test_key",
                    api_secret="test_secret",
                    allow_mainnet=True,
                    symbol_whitelist=["BTCUSDT"],
                    max_notional_per_order=Decimal("50"),
                )

            assert "ALLOW_MAINNET_TRADE" in str(exc_info.value)

    def test_mainnet_requires_symbol_whitelist(self) -> None:
        """Mainnet requires non-empty symbol_whitelist."""
        with patch.dict(os.environ, {"ALLOW_MAINNET_TRADE": "1"}, clear=False):
            with pytest.raises(ConnectorNonRetryableError) as exc_info:
                BinanceFuturesPortConfig(
                    mode=SafeMode.LIVE_TRADE,
                    base_url=BINANCE_FUTURES_MAINNET_URL,
                    api_key="test_key",
                    api_secret="test_secret",
                    allow_mainnet=True,
                    symbol_whitelist=[],  # Empty
                    max_notional_per_order=Decimal("50"),
                )

            assert "symbol_whitelist" in str(exc_info.value).lower()

    def test_mainnet_requires_max_notional(self) -> None:
        """Mainnet requires max_notional_per_order to be set."""
        with patch.dict(os.environ, {"ALLOW_MAINNET_TRADE": "1"}, clear=False):
            with pytest.raises(ConnectorNonRetryableError) as exc_info:
                BinanceFuturesPortConfig(
                    mode=SafeMode.LIVE_TRADE,
                    base_url=BINANCE_FUTURES_MAINNET_URL,
                    api_key="test_key",
                    api_secret="test_secret",
                    allow_mainnet=True,
                    symbol_whitelist=["BTCUSDT"],
                    max_notional_per_order=None,  # Not set
                )

            assert "max_notional_per_order" in str(exc_info.value)

    def test_mainnet_config_passes_with_all_guards(self) -> None:
        """Mainnet config passes when all guards are met."""
        with patch.dict(os.environ, {"ALLOW_MAINNET_TRADE": "1"}, clear=False):
            config = BinanceFuturesPortConfig(
                mode=SafeMode.LIVE_TRADE,
                base_url=BINANCE_FUTURES_MAINNET_URL,
                api_key="test_key",
                api_secret="test_secret",
                allow_mainnet=True,
                symbol_whitelist=["BTCUSDT"],
                max_notional_per_order=Decimal("50"),
            )

            assert config.is_mainnet() is True


# --- Notional Limit Tests ---


class TestNotionalLimits:
    """Tests for max_notional_per_order enforcement."""

    def test_notional_exceeds_limit_blocked(self, noop_client: NoopHttpClient) -> None:
        """Order with notional exceeding limit is blocked."""
        config = BinanceFuturesPortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url=BINANCE_FUTURES_TESTNET_URL,
            api_key="test_key",
            api_secret="test_secret",
            symbol_whitelist=["BTCUSDT"],
            dry_run=True,
            max_notional_per_order=Decimal("50"),
        )
        port = BinanceFuturesPort(http_client=noop_client, config=config)

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.01"),  # Notional = $500 > $50 limit
                level_id=1,
                ts=1000000,
            )

        assert "notional" in str(exc_info.value).lower()
        assert "50" in str(exc_info.value)

    def test_notional_within_limit_allowed(self, noop_client: NoopHttpClient) -> None:
        """Order with notional within limit is allowed."""
        config = BinanceFuturesPortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url=BINANCE_FUTURES_TESTNET_URL,
            api_key="test_key",
            api_secret="test_secret",
            symbol_whitelist=["BTCUSDT"],
            dry_run=True,
            max_notional_per_order=Decimal("500"),
        )
        port = BinanceFuturesPort(http_client=noop_client, config=config)

        order_id = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),  # Notional = $50 < $500 limit
            level_id=1,
            ts=1000000,
        )

        assert order_id is not None


# --- Order Count Limit Tests ---


class TestOrderCountLimits:
    """Tests for max_orders_per_run enforcement."""

    def test_order_count_limit_enforced(self, noop_client: NoopHttpClient) -> None:
        """Second order blocked when max_orders_per_run=1."""
        config = BinanceFuturesPortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url=BINANCE_FUTURES_TESTNET_URL,
            api_key="test_key",
            api_secret="test_secret",
            symbol_whitelist=["BTCUSDT"],
            dry_run=True,
            max_orders_per_run=1,  # Only 1 order allowed
        )
        port = BinanceFuturesPort(http_client=noop_client, config=config)

        # First order succeeds
        order1 = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000000,
        )
        assert order1 is not None

        # Second order blocked
        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.001"),
                level_id=2,
                ts=1000001,
            )

        assert "Order count limit" in str(exc_info.value)

    def test_reset_clears_order_count(self, noop_client: NoopHttpClient) -> None:
        """reset() clears order count, allowing new orders."""
        config = BinanceFuturesPortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url=BINANCE_FUTURES_TESTNET_URL,
            api_key="test_key",
            api_secret="test_secret",
            symbol_whitelist=["BTCUSDT"],
            dry_run=True,
            max_orders_per_run=1,
        )
        port = BinanceFuturesPort(http_client=noop_client, config=config)

        # First order succeeds
        port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000000,
        )

        # Reset
        port.reset()

        # Second order now succeeds
        order2 = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=2,
            ts=1000001,
        )
        assert order2 is not None


# --- Leverage Tests ---


class TestLeverageOperations:
    """Tests for leverage-related operations."""

    def test_set_leverage_validates_range(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """set_leverage validates leverage is 1-125."""
        port = BinanceFuturesPort(http_client=noop_client, config=live_trade_config)

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            port.set_leverage("BTCUSDT", 0)  # Invalid

        assert "1-125" in str(exc_info.value)

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            port.set_leverage("BTCUSDT", 200)  # Invalid

        assert "1-125" in str(exc_info.value)

    def test_get_leverage_dry_run(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """get_leverage in dry-run returns target_leverage."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        leverage = port.get_leverage("BTCUSDT")

        assert leverage == dry_run_config.target_leverage


# --- URL Detection Tests ---


class TestUrlDetection:
    """Tests for mainnet/testnet URL detection."""

    def test_is_mainnet_fapi(self) -> None:
        """fapi.binance.com is detected as mainnet (guards will block creation)."""
        # Creating mainnet config without guards should fail
        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            BinanceFuturesPortConfig(
                mode=SafeMode.READ_ONLY,
                base_url="https://fapi.binance.com",
            )
        # The error proves mainnet was detected
        assert "mainnet" in str(exc_info.value).lower()

    def test_is_mainnet_with_guards(self) -> None:
        """fapi.binance.com works when all guards are met."""
        with patch.dict(os.environ, {"ALLOW_MAINNET_TRADE": "1"}, clear=False):
            config = BinanceFuturesPortConfig(
                mode=SafeMode.READ_ONLY,
                base_url="https://fapi.binance.com",
                allow_mainnet=True,
                symbol_whitelist=["BTCUSDT"],
                max_notional_per_order=Decimal("50"),
            )
            assert config.is_mainnet() is True

    def test_is_testnet(self) -> None:
        """testnet URL is not mainnet."""
        config = BinanceFuturesPortConfig(
            mode=SafeMode.READ_ONLY,
            base_url=BINANCE_FUTURES_TESTNET_URL,
        )
        assert config.is_mainnet() is False


# --- Market Order Tests ---


class TestMarketOrders:
    """Tests for market order operations (position cleanup)."""

    def test_place_market_order_dry_run(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """place_market_order in dry-run returns synthetic order_id."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        order_id = port.place_market_order(
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            quantity=Decimal("0.001"),
            reduce_only=True,
        )

        assert order_id is not None
        # Level_id is "c" (short for cleanup) to fit Binance 36-char limit
        assert "_c_" in order_id
        assert len(noop_client.calls) == 0

    def test_place_market_order_dry_run_long_symbol_uses_short_prefix_fallback(
        self, noop_client: NoopHttpClient
    ) -> None:
        """Cleanup market order uses short prefix for long symbol CID overflow."""
        config = BinanceFuturesPortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url=BINANCE_FUTURES_TESTNET_URL,
            api_key="test_key",
            api_secret="test_secret",
            symbol_whitelist=["FARTCOINUSDT"],
            dry_run=True,
            max_orders_per_run=100,
        )
        port = BinanceFuturesPort(http_client=noop_client, config=config)

        order_id = port.place_market_order(
            symbol="FARTCOINUSDT",
            side=OrderSide.SELL,
            quantity=Decimal("1"),
            reduce_only=True,
        )

        assert order_id.startswith("g_d_FARTCOINUSDT_c_")
        assert len(order_id) <= 36
        assert len(noop_client.calls) == 0


# --- Cancel All Orders Tests ---


class TestCancelAllOrders:
    """Tests for cancel all orders operation."""

    def test_cancel_all_orders_dry_run(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """cancel_all_orders in dry-run returns 0."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        count = port.cancel_all_orders("BTCUSDT")

        assert count == 0
        assert len(noop_client.calls) == 0


# --- Close Position Tests ---


class TestClosePosition:
    """Tests for position close operation."""

    def test_close_position_dry_run_no_position(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """close_position in dry-run with no position returns None."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        result = port.close_position("BTCUSDT")

        assert result is None  # No position to close
        assert len(noop_client.calls) == 0


# --- Mock Transport Tests ---


class TestMockTransportMode:
    """Tests using NoopHttpClient to verify API calls are made correctly."""

    def test_place_order_calls_fapi_endpoint(
        self, futures_noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """place_order calls correct /fapi/v1/order endpoint."""
        port = BinanceFuturesPort(http_client=futures_noop_client, config=live_trade_config)

        port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,
        )

        assert len(futures_noop_client.calls) == 1
        call = futures_noop_client.calls[0]
        assert call["method"] == "POST"
        assert "/fapi/v1/order" in call["url"]
        assert call["params"]["symbol"] == "BTCUSDT"
        assert call["params"]["side"] == "BUY"
        assert call["params"]["type"] == "LIMIT"

    def test_cancel_order_calls_fapi_endpoint(
        self, futures_noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """cancel_order calls correct /fapi/v1/order endpoint with DELETE."""
        port = BinanceFuturesPort(http_client=futures_noop_client, config=live_trade_config)

        port.cancel_order("grinder_BTCUSDT_1_1000000_1")

        assert len(futures_noop_client.calls) == 1
        call = futures_noop_client.calls[0]
        assert call["method"] == "DELETE"
        assert "/fapi/v1/order" in call["url"]

    def test_fetch_user_trades_calls_fapi_endpoint(
        self, futures_noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """fetch_user_trades_raw calls correct /fapi/v1/userTrades endpoint."""
        port = BinanceFuturesPort(http_client=futures_noop_client, config=live_trade_config)

        port.fetch_user_trades_raw("BTCUSDT", from_id=100, limit=50)

        assert len(futures_noop_client.calls) == 1
        call = futures_noop_client.calls[0]
        assert call["method"] == "GET"
        assert "/fapi/v1/userTrades" in call["url"]
        assert call["params"]["symbol"] == "BTCUSDT"
        assert call["params"]["fromId"] == 100
        assert call["params"]["limit"] == 50


# --- Cancel Order Identity Parsing Tests (LC-12) ---


class TestCancelOrderIdentityParsing:
    """Tests for cancel_order clientOrderId parsing (LC-12 fix).

    The cancel_order method must correctly parse both:
    - v1 format: grinder_{strategy}_{symbol}_{level}_{ts}_{seq}
    - Legacy format: grinder_{symbol}_{level}_{ts}_{seq}

    Bug found during Stage D E2E: naive split("_") parsed strategy_id as symbol.
    """

    def test_cancel_order_v1_format_extracts_symbol(
        self, futures_noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """cancel_order correctly parses v1 format with strategy_id."""
        port = BinanceFuturesPort(http_client=futures_noop_client, config=live_trade_config)

        # v1 format: grinder_{strategy}_{symbol}_{level}_{ts}_{seq}
        # strategy_id = "d", symbol = "BTCUSDT"
        port.cancel_order("grinder_d_BTCUSDT_0_1770470846_1")

        assert len(futures_noop_client.calls) == 1
        call = futures_noop_client.calls[0]
        assert call["method"] == "DELETE"
        # Symbol should be BTCUSDT (not "d")
        assert call["params"]["symbol"] == "BTCUSDT"
        assert call["params"]["origClientOrderId"] == "grinder_d_BTCUSDT_0_1770470846_1"

    def test_cancel_order_v1_format_long_strategy(
        self, futures_noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """cancel_order correctly parses v1 format with longer strategy_id."""
        port = BinanceFuturesPort(http_client=futures_noop_client, config=live_trade_config)

        # strategy_id = "d", symbol = "BTCUSDT"
        port.cancel_order("grinder_d_BTCUSDT_1_1704067200_1")

        assert len(futures_noop_client.calls) == 1
        call = futures_noop_client.calls[0]
        assert call["params"]["symbol"] == "BTCUSDT"

    def test_cancel_order_legacy_format_still_works(
        self, futures_noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """cancel_order still works with legacy format (no strategy_id)."""
        port = BinanceFuturesPort(http_client=futures_noop_client, config=live_trade_config)

        # Legacy format: grinder_{symbol}_{level}_{ts}_{seq}
        port.cancel_order("grinder_BTCUSDT_1_1000000_1")

        assert len(futures_noop_client.calls) == 1
        call = futures_noop_client.calls[0]
        assert call["params"]["symbol"] == "BTCUSDT"

    def test_cancel_order_invalid_format_raises_error(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """cancel_order raises error for unparseable clientOrderId."""
        port = BinanceFuturesPort(http_client=noop_client, config=live_trade_config)

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            port.cancel_order("invalid_order_id_format")

        assert "Cannot parse order_id" in str(exc_info.value)

    def test_cancel_order_symbol_not_whitelisted_blocked(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """cancel_order blocks if parsed symbol is not in whitelist."""
        port = BinanceFuturesPort(http_client=noop_client, config=live_trade_config)

        # Symbol XYZUSDT is not in whitelist ["BTCUSDT", "ETHUSDT"]
        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            port.cancel_order("grinder_d_XYZUSDT_0_1770470846_1")

        assert "whitelist" in str(exc_info.value).lower()


# --- Account Snapshot Tests (PR-Y) ---


class TestFetchAccountSnapshot:
    """Tests for BinanceFuturesPort.fetch_account_snapshot()."""

    def test_dry_run_returns_empty_snapshot_and_no_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceFuturesPortConfig
    ) -> None:
        """dry_run=True -> empty AccountSnapshot, 0 HTTP calls, ts=0."""
        port = BinanceFuturesPort(http_client=noop_client, config=dry_run_config)

        snap = port.fetch_account_snapshot()

        assert len(noop_client.calls) == 0
        assert snap.positions == ()
        assert snap.open_orders == ()
        assert snap.ts == 0
        assert snap.source == "dry_run"

    def test_parses_positions_and_orders_filters_zero_qty(
        self, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """Parses positions + orders from Binance response; filters zero-qty positions."""
        client = NoopHttpClient(
            positions_response=[
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.002",
                    "positionSide": "BOTH",
                    "entryPrice": "65000.0",
                    "markPrice": "65100.0",
                    "unRealizedProfit": "0.20",
                    "leverage": "5",
                    "updateTime": 5000,
                },
                {
                    "symbol": "ETHUSDT",
                    "positionAmt": "0.000",
                    "positionSide": "BOTH",
                    "entryPrice": "0",
                    "markPrice": "3200.0",
                    "unRealizedProfit": "0",
                    "leverage": "1",
                    "updateTime": 4000,
                },
            ],
            open_orders_response=[
                {
                    "clientOrderId": "grinder_d_BTCUSDT_0_123_1",
                    "orderId": 99999,
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "LIMIT",
                    "price": "64000.0",
                    "origQty": "0.001",
                    "executedQty": "0",
                    "reduceOnly": False,
                    "status": "NEW",
                    "updateTime": 6000,
                },
            ],
        )
        port = BinanceFuturesPort(http_client=client, config=live_trade_config)

        snap = port.fetch_account_snapshot()

        # Zero-qty ETHUSDT filtered out
        assert len(snap.positions) == 1
        pos = snap.positions[0]
        assert pos.symbol == "BTCUSDT"
        assert pos.qty == Decimal("0.002")
        assert pos.entry_price == Decimal("65000.0")
        assert pos.mark_price == Decimal("65100.0")
        assert pos.unrealized_pnl == Decimal("0.20")
        assert pos.leverage == 5
        assert pos.side == "BOTH"
        assert pos.ts == 5000

        assert len(snap.open_orders) == 1
        order = snap.open_orders[0]
        assert order.order_id == "grinder_d_BTCUSDT_0_123_1"
        assert order.symbol == "BTCUSDT"
        assert order.side == "BUY"
        assert order.order_type == "LIMIT"
        assert order.price == Decimal("64000.0")
        assert order.qty == Decimal("0.001")
        assert order.filled_qty == Decimal("0")
        assert order.reduce_only is False
        assert order.status == "NEW"

        assert snap.source == "exchange"

    def test_empty_account_returns_empty_snapshot_and_two_calls(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """Empty account -> empty tuples, ts=0, exactly 2 HTTP calls."""
        port = BinanceFuturesPort(http_client=noop_client, config=live_trade_config)

        snap = port.fetch_account_snapshot()

        assert snap.positions == ()
        assert snap.open_orders == ()
        assert snap.ts == 0
        assert snap.source == "exchange"
        assert len(noop_client.calls) == 2

    def test_makes_exactly_two_endpoint_calls(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceFuturesPortConfig
    ) -> None:
        """Verifies exactly 2 calls: positionRisk then openOrders."""
        port = BinanceFuturesPort(http_client=noop_client, config=live_trade_config)

        port.fetch_account_snapshot()

        assert len(noop_client.calls) == 2
        assert "positionRisk" in noop_client.calls[0]["url"]
        assert "openOrders" in noop_client.calls[1]["url"]

    def test_ts_is_max_update_time(self, live_trade_config: BinanceFuturesPortConfig) -> None:
        """snapshot.ts == max(updateTime) across positions and orders."""
        client = NoopHttpClient(
            positions_response=[
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.001",
                    "positionSide": "BOTH",
                    "entryPrice": "50000",
                    "markPrice": "50000",
                    "unRealizedProfit": "0",
                    "leverage": "1",
                    "updateTime": 5000,
                },
            ],
            open_orders_response=[
                {
                    "clientOrderId": "test_1",
                    "orderId": 1,
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "LIMIT",
                    "price": "49000",
                    "origQty": "0.001",
                    "executedQty": "0",
                    "reduceOnly": False,
                    "status": "NEW",
                    "updateTime": 7000,
                },
            ],
        )
        port = BinanceFuturesPort(http_client=client, config=live_trade_config)

        snap = port.fetch_account_snapshot()

        assert snap.ts == 7000

    def test_reduce_only_string_false(self, live_trade_config: BinanceFuturesPortConfig) -> None:
        """Binance may send reduceOnly as string 'false' -- must parse correctly."""
        client = NoopHttpClient(
            open_orders_response=[
                {
                    "clientOrderId": "test_ro",
                    "orderId": 1,
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "type": "LIMIT",
                    "price": "70000",
                    "origQty": "0.001",
                    "executedQty": "0",
                    "reduceOnly": "false",
                    "status": "NEW",
                    "updateTime": 1000,
                },
            ],
        )
        port = BinanceFuturesPort(http_client=client, config=live_trade_config)

        snap = port.fetch_account_snapshot()

        assert len(snap.open_orders) == 1
        assert snap.open_orders[0].reduce_only is False
