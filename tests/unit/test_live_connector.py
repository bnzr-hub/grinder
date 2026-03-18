"""Unit tests for LiveConnectorV0.

Tests cover:
- Connector lifecycle (connect/close)
- SafeMode enforcement
- H2/H4/H5 hardening (retries, circuit breaker, metrics)
- Bounded-time testing with injectable clock/sleep
- Error handling
- Statistics tracking
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import pytest

from grinder.connectors import (
    CircuitOpenError,
    CircuitState,
    ConnectorClosedError,
    ConnectorNonRetryableError,
    ConnectorState,
    LiveConnectorConfig,
    LiveConnectorV0,
    SafeMode,
    reset_connector_metrics,
)
from grinder.connectors.binance_ws import FakeWsTransport
from grinder.connectors.circuit_breaker import CircuitBreakerConfig
from grinder.connectors.metrics import get_connector_metrics
from grinder.core import OrderSide


class FakeClock:
    """Fake clock for bounded-time testing."""

    def __init__(self, start_time: float = 0.0) -> None:
        self._time = start_time

    def time(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


class FakeSleep:
    """Fake sleep for bounded-time testing."""

    def __init__(self) -> None:
        self.total_slept: float = 0.0
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.total_slept += seconds
        self.calls.append(seconds)
        # No actual sleep - instant return


@pytest.fixture(autouse=True)
def reset_metrics() -> None:
    """Reset connector metrics before each test."""
    reset_connector_metrics()


@pytest.fixture
def fake_clock() -> FakeClock:
    """Provide fake clock for tests."""
    return FakeClock()


@pytest.fixture
def fake_sleep() -> FakeSleep:
    """Provide fake sleep for tests."""
    return FakeSleep()


@pytest.fixture
def fake_ws_transport() -> FakeWsTransport:
    """Provide fake WS transport for tests with symbols.

    Uses delay_ms=2 to ensure different timestamps for idempotency checks.
    """
    return FakeWsTransport(messages=[], delay_ms=2)


# --- SafeMode Tests ---


class TestSafeMode:
    """Tests for SafeMode enum."""

    def test_safe_mode_values(self) -> None:
        """SafeMode has correct values."""
        assert SafeMode.READ_ONLY.value == "read_only"
        assert SafeMode.PAPER.value == "paper"
        assert SafeMode.LIVE_TRADE.value == "live_trade"

    def test_default_mode_is_read_only(self) -> None:
        """Default config mode is READ_ONLY."""
        config = LiveConnectorConfig()
        assert config.mode == SafeMode.READ_ONLY

    @pytest.mark.asyncio
    async def test_connector_mode_property(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Connector exposes mode property."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        assert connector.mode == SafeMode.PAPER


# --- Lifecycle Tests ---


class TestLiveConnectorLifecycle:
    """Tests for connector lifecycle management."""

    @pytest.mark.asyncio
    async def test_initial_state_is_disconnected(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Connector starts in DISCONNECTED state."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        assert connector.state == ConnectorState.DISCONNECTED
        assert connector.last_seen_ts is None

    @pytest.mark.asyncio
    async def test_connect_changes_state(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Connect transitions to CONNECTED state."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        assert connector.state == ConnectorState.CONNECTED
        await connector.close()

    @pytest.mark.asyncio
    async def test_close_changes_state(self, fake_clock: FakeClock, fake_sleep: FakeSleep) -> None:
        """Close transitions to CLOSED state."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.close()
        assert connector.state == ConnectorState.CLOSED

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, fake_clock: FakeClock, fake_sleep: FakeSleep) -> None:
        """Close can be called multiple times safely."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.close()
        await connector.close()  # Should not raise
        assert connector.state == ConnectorState.CLOSED

    @pytest.mark.asyncio
    async def test_connect_when_already_connected(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Connect is no-op if already connected."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.connect()  # Should not raise
        assert connector.state == ConnectorState.CONNECTED
        assert connector.stats.connection_attempts == 1  # Only counted once
        await connector.close()


# --- Stream Tests ---


class TestLiveConnectorStream:
    """Tests for streaming methods."""

    @pytest.mark.asyncio
    async def test_stream_ticks_without_connect_raises(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """stream_ticks without connect raises ConnectionError."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)

        with pytest.raises(ConnectionError, match="connector state is disconnected"):
            async for _ in connector.stream_ticks():
                pass

    @pytest.mark.asyncio
    async def test_stream_ticks_after_close_raises(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """stream_ticks after close raises ConnectorClosedError."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.close()

        with pytest.raises(ConnectorClosedError) as exc_info:
            async for _ in connector.stream_ticks():
                pass

        assert exc_info.value.op == "stream_ticks"

    @pytest.mark.asyncio
    async def test_iter_snapshots_is_alias(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """iter_snapshots is alias for stream_ticks."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()

        # Both should work identically (v0 yields nothing)
        async for _ in connector.iter_snapshots():
            pass
        async for _ in connector.stream_ticks():
            pass

        await connector.close()


# --- LC-21: Stream Ticks with Real WS Tests ---


class TestStreamTicksWithWs:
    """Tests for stream_ticks with real WS connector (LC-21)."""

    @pytest.mark.asyncio
    async def test_stream_ticks_yields_snapshots_from_ws(self, fake_sleep: FakeSleep) -> None:
        """stream_ticks yields snapshots from WS connector when messages available."""
        # Prepare fake bookTicker messages
        messages = [
            json.dumps({"s": "BTCUSDT", "b": "50000.00", "B": "1.5", "a": "50001.00", "A": "2.0"}),
            json.dumps({"s": "BTCUSDT", "b": "50002.00", "B": "1.2", "a": "50003.00", "A": "1.8"}),
        ]
        # delay_ms=2 ensures different timestamps for idempotency checks
        transport = FakeWsTransport(messages=messages, delay_ms=2)

        # Use real time module for WS connector timestamps (not FakeClock)
        # This is needed because BinanceWsConnector uses clock for idempotency
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                symbols=["BTCUSDT"],
                ws_transport=transport,
            ),
            clock=time,  # Use real time for proper timestamps
            sleep_func=fake_sleep,
        )
        await connector.connect()

        # Collect snapshots with timeout to prevent hanging
        snapshots = []
        try:
            async with asyncio.timeout(5):
                async for snapshot in connector.stream_ticks():
                    snapshots.append(snapshot)
                    if len(snapshots) >= 2:
                        await connector.close()
                        break
        except TimeoutError:
            await connector.close()

        # Verify snapshots
        assert len(snapshots) == 2
        assert snapshots[0].symbol == "BTCUSDT"
        assert snapshots[0].bid_price == 50000
        assert snapshots[1].bid_price == 50002

    @pytest.mark.asyncio
    async def test_stream_ticks_updates_stats(self, fake_sleep: FakeSleep) -> None:
        """stream_ticks updates connector stats."""
        messages = [
            json.dumps({"s": "BTCUSDT", "b": "50000.00", "B": "1.5", "a": "50001.00", "A": "2.0"}),
        ]
        transport = FakeWsTransport(messages=messages, delay_ms=2)

        # Use real time for proper timestamps
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                symbols=["BTCUSDT"],
                ws_transport=transport,
            ),
            clock=time,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        # Stream one message with timeout
        try:
            async with asyncio.timeout(5):
                async for _ in connector.stream_ticks():
                    await connector.close()
                    break
        except TimeoutError:
            await connector.close()

        # Verify stats updated
        assert connector.stats.ticks_received == 1
        assert connector.last_seen_ts is not None

    @pytest.mark.asyncio
    async def test_stream_ticks_records_metrics(self, fake_sleep: FakeSleep) -> None:
        """stream_ticks records WS metrics."""
        messages = [
            json.dumps({"s": "BTCUSDT", "b": "50000.00", "B": "1.5", "a": "50001.00", "A": "2.0"}),
        ]
        transport = FakeWsTransport(messages=messages, delay_ms=2)

        # Use real time for proper timestamps
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                symbols=["BTCUSDT"],
                ws_transport=transport,
            ),
            clock=time,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        # Stream one message with timeout
        try:
            async with asyncio.timeout(5):
                async for _ in connector.stream_ticks():
                    await connector.close()
                    break
        except TimeoutError:
            await connector.close()

        # Verify metrics (use connector name, not symbol, per ADR-028)
        metrics = get_connector_metrics()
        assert metrics.ticks_received.get("bookTicker", 0) == 1
        assert metrics.last_tick_ts.get("bookTicker") is not None

    @pytest.mark.asyncio
    async def test_stream_ticks_graceful_stop_on_close(self, fake_sleep: FakeSleep) -> None:
        """stream_ticks stops gracefully when connector closed."""
        # Many messages to simulate long stream (different bid prices for idempotency)
        messages = [
            json.dumps(
                {
                    "s": "BTCUSDT",
                    "b": f"{50000 + i}.00",
                    "B": "1.5",
                    "a": f"{50001 + i}.00",
                    "A": "2.0",
                }
            )
            for i in range(100)
        ]
        transport = FakeWsTransport(messages=messages, delay_ms=2)

        # Use real time for proper timestamps
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                symbols=["BTCUSDT"],
                ws_transport=transport,
            ),
            clock=time,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        # Stream with timeout
        snapshots = []
        try:
            async with asyncio.timeout(5):
                async for snapshot in connector.stream_ticks():
                    snapshots.append(snapshot)
                    if len(snapshots) >= 3:
                        await connector.close()
                        break
        except TimeoutError:
            await connector.close()

        # Verify we got some snapshots before close
        assert len(snapshots) >= 3
        assert connector.state == ConnectorState.CLOSED


# --- Subscribe Tests ---


class TestLiveConnectorSubscribe:
    """Tests for subscription management."""

    @pytest.mark.asyncio
    async def test_subscribe_adds_symbols(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep, fake_ws_transport: FakeWsTransport
    ) -> None:
        """subscribe adds symbols to config."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                symbols=["BTCUSDT"],
                ws_transport=fake_ws_transport,
            ),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        await connector.subscribe(["ETHUSDT", "SOLUSDT"])

        assert "BTCUSDT" in connector.symbols
        assert "ETHUSDT" in connector.symbols
        assert "SOLUSDT" in connector.symbols
        await connector.close()

    @pytest.mark.asyncio
    async def test_subscribe_without_connect_raises(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """subscribe without connect raises ConnectionError."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)

        with pytest.raises(ConnectionError, match="connector state is disconnected"):
            await connector.subscribe(["BTCUSDT"])

    @pytest.mark.asyncio
    async def test_subscribe_after_close_raises(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """subscribe after close raises ConnectorClosedError."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.close()

        with pytest.raises(ConnectorClosedError) as exc_info:
            await connector.subscribe(["BTCUSDT"])

        assert exc_info.value.op == "subscribe"


# --- Reconnect Tests ---


class TestLiveConnectorReconnect:
    """Tests for reconnection behavior."""

    @pytest.mark.asyncio
    async def test_reconnect_increments_stats(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """reconnect increments reconnection stats."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()

        await connector.reconnect()
        await connector.reconnect()

        assert connector.stats.reconnections == 2
        assert connector.state == ConnectorState.CONNECTED
        await connector.close()

    @pytest.mark.asyncio
    async def test_reconnect_after_close_raises(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """reconnect after close raises ConnectorClosedError."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.close()

        with pytest.raises(ConnectorClosedError) as exc_info:
            await connector.reconnect()

        assert exc_info.value.op == "reconnect"


# --- SafeMode Enforcement Tests ---


class TestSafeModeEnforcement:
    """Tests for safe mode enforcement."""

    @pytest.mark.asyncio
    async def test_assert_mode_read_only_allows_read_only(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """READ_ONLY mode allows READ_ONLY operations."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.READ_ONLY),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        connector.assert_mode(SafeMode.READ_ONLY)  # Should not raise

    @pytest.mark.asyncio
    async def test_assert_mode_read_only_blocks_paper(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """READ_ONLY mode blocks PAPER operations."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.READ_ONLY),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        with pytest.raises(ConnectorNonRetryableError, match="requires mode=paper"):
            connector.assert_mode(SafeMode.PAPER)

    @pytest.mark.asyncio
    async def test_assert_mode_paper_allows_paper(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """PAPER mode allows PAPER operations."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        connector.assert_mode(SafeMode.PAPER)  # Should not raise

    @pytest.mark.asyncio
    async def test_assert_mode_paper_blocks_live_trade(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """PAPER mode blocks LIVE_TRADE operations."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        with pytest.raises(ConnectorNonRetryableError, match="requires mode=live_trade"):
            connector.assert_mode(SafeMode.LIVE_TRADE)

    @pytest.mark.asyncio
    async def test_assert_mode_live_trade_allows_all(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """LIVE_TRADE mode allows all operations."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.LIVE_TRADE),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        connector.assert_mode(SafeMode.READ_ONLY)  # Should not raise
        connector.assert_mode(SafeMode.PAPER)  # Should not raise
        connector.assert_mode(SafeMode.LIVE_TRADE)  # Should not raise


# --- H4 Circuit Breaker Tests ---


class TestLiveConnectorCircuitBreaker:
    """Tests for circuit breaker integration (H4)."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_exposed(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Circuit breaker is accessible for monitoring."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        assert connector.circuit_breaker is not None
        assert connector.circuit_breaker.state("connect") == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_on_failures(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Circuit breaker trips after threshold failures."""
        config = LiveConnectorConfig(
            circuit_breaker_config=CircuitBreakerConfig(
                failure_threshold=2,  # Trip after 2 failures
            ),
        )
        connector = LiveConnectorV0(
            config=config,
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        # Manually record failures to trip the breaker
        connector.circuit_breaker.record_failure("connect", "test")
        connector.circuit_breaker.record_failure("connect", "test")

        # Circuit should be OPEN now
        assert connector.circuit_breaker.state("connect") == CircuitState.OPEN

        # connect() should fail with CircuitOpenError
        with pytest.raises(CircuitOpenError):
            await connector.connect()


# --- Statistics Tests ---


class TestLiveConnectorStats:
    """Tests for statistics tracking."""

    @pytest.mark.asyncio
    async def test_stats_connection_attempts(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Stats track connection attempts."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        assert connector.stats.connection_attempts == 1
        await connector.close()

    @pytest.mark.asyncio
    async def test_stats_reconnections(self, fake_clock: FakeClock, fake_sleep: FakeSleep) -> None:
        """Stats track reconnection attempts."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.reconnect()
        assert connector.stats.reconnections == 1
        await connector.close()

    @pytest.mark.asyncio
    async def test_stats_errors_list_accessible(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Stats errors list is accessible and modifiable."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()

        # Errors list is empty by default on successful connect
        assert isinstance(connector.stats.errors, list)

        await connector.close()


# --- Configuration Tests ---


class TestLiveConnectorConfig:
    """Tests for configuration."""

    def test_default_config_values(self) -> None:
        """Default config has safe values."""
        config = LiveConnectorConfig()
        assert config.mode == SafeMode.READ_ONLY
        assert config.symbols == []
        assert config.ws_url is None  # No explicit override by default
        assert config.use_testnet is True  # Testnet by default (safe)

    def test_config_with_symbols(self) -> None:
        """Config accepts symbol list."""
        config = LiveConnectorConfig(symbols=["BTCUSDT", "ETHUSDT"])
        assert "BTCUSDT" in config.symbols
        assert "ETHUSDT" in config.symbols

    @pytest.mark.asyncio
    async def test_connector_uses_config(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Connector uses provided config."""
        config = LiveConnectorConfig(
            mode=SafeMode.PAPER,
            symbols=["BTCUSDT"],
        )
        connector = LiveConnectorV0(
            config=config,
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        assert connector.mode == SafeMode.PAPER
        assert "BTCUSDT" in connector.symbols


# --- Bounded-Time Testing ---


class TestBoundedTimeTesting:
    """Tests for bounded-time behavior with injectable clock/sleep."""

    @pytest.mark.asyncio
    async def test_fake_sleep_no_actual_delay(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Fake sleep causes no actual delay."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)

        start = time.time()
        await connector.connect()
        elapsed = time.time() - start

        # Should be nearly instant (< 100ms) even though connect has a small sleep
        assert elapsed < 0.1
        assert fake_sleep.total_slept > 0  # But fake sleep was called
        await connector.close()

    @pytest.mark.asyncio
    async def test_fake_clock_controllable(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Fake clock allows time control."""
        _ = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)

        assert fake_clock.time() == 0.0
        fake_clock.advance(10.0)
        assert fake_clock.time() == 10.0


# --- LC-22: LIVE_TRADE 3-Gate Tests ---


class FakeFuturesPort:
    """Fake BinanceFuturesPort for testing LIVE_TRADE delegation."""

    def __init__(self) -> None:
        self.place_order_calls: list[dict[str, object]] = []
        self.cancel_order_calls: list[str] = []
        self.replace_order_calls: list[dict[str, object]] = []

    def place_order(
        self,
        symbol: str,
        side: object,
        price: object,
        quantity: object,
        level_id: int,
        ts: int,
        reduce_only: bool = False,  # noqa: ARG002
    ) -> str:
        """Record place_order call and return fake order_id."""
        self.place_order_calls.append(
            {
                "symbol": symbol,
                "side": side,
                "price": price,
                "quantity": quantity,
                "level_id": level_id,
                "ts": ts,
            }
        )
        return f"grinder_{symbol}_{level_id}_{ts}"

    def cancel_order(self, order_id: str) -> bool:
        """Record cancel_order call and return success."""
        self.cancel_order_calls.append(order_id)
        return True

    def replace_order(
        self,
        order_id: str,
        new_price: object,
        new_quantity: object,
        ts: int,
    ) -> str:
        """Record replace_order call and return new order_id."""
        self.replace_order_calls.append(
            {
                "order_id": order_id,
                "new_price": new_price,
                "new_quantity": new_quantity,
                "ts": ts,
            }
        )
        return f"grinder_replaced_{ts}"


class TestLiveTradeGates:
    """Tests for LC-22 LIVE_TRADE 3-gate safety checks.

    Gates:
    1. armed=True (explicit arming)
    2. mode=LIVE_TRADE (explicit mode)
    3. ALLOW_MAINNET_TRADE=1 env var (external safeguard)

    All gates must pass for real write operations.
    """

    @pytest.fixture
    def fake_port(self) -> FakeFuturesPort:
        """Provide fake futures port for testing."""
        return FakeFuturesPort()

    @pytest.fixture
    def set_allow_mainnet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set ALLOW_MAINNET_TRADE=1 for tests that need it."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")

    @pytest.fixture
    def clear_allow_mainnet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clear ALLOW_MAINNET_TRADE env var."""
        monkeypatch.delenv("ALLOW_MAINNET_TRADE", raising=False)

    # --- Negative Path Tests: Gate Failures ---

    @pytest.mark.asyncio
    async def test_live_trade_armed_false_blocks_place_order(
        self,
        fake_clock: FakeClock,
        fake_sleep: FakeSleep,
        fake_port: FakeFuturesPort,
        set_allow_mainnet: None,  # noqa: ARG002
    ) -> None:
        """Gate 1 failure: LIVE_TRADE + armed=False → block."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.LIVE_TRADE,
                armed=False,  # Gate 1 fails
                futures_port=fake_port,  # type: ignore[arg-type]
            ),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            connector.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.001"),
            )

        # Verify specific error message
        assert "armed=False" in str(exc_info.value)
        assert "Set armed=True" in str(exc_info.value)

        # Verify port was NOT called
        assert len(fake_port.place_order_calls) == 0

        await connector.close()

    @pytest.mark.asyncio
    async def test_live_trade_wrong_mode_blocks_place_order(
        self,
        fake_clock: FakeClock,
        fake_sleep: FakeSleep,
        fake_port: FakeFuturesPort,
        set_allow_mainnet: None,  # noqa: ARG002
    ) -> None:
        """Gate 2 failure: mode≠LIVE_TRADE + armed=True → block."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.PAPER,  # Gate 2 fails (not LIVE_TRADE)
                armed=True,
                futures_port=fake_port,  # type: ignore[arg-type]
            ),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        # PAPER mode uses paper adapter, NOT futures_port
        # So place_order should go to paper adapter (which is initialized for PAPER mode)
        # This test verifies that armed=True + PAPER mode uses paper adapter, not futures_port
        result = connector.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
        )

        # Should succeed via paper adapter
        assert result is not None

        # Verify futures_port was NOT called
        assert len(fake_port.place_order_calls) == 0

        await connector.close()

    @pytest.mark.asyncio
    async def test_live_trade_env_var_missing_blocks_place_order(
        self,
        fake_clock: FakeClock,
        fake_sleep: FakeSleep,
        fake_port: FakeFuturesPort,
        clear_allow_mainnet: None,  # noqa: ARG002
    ) -> None:
        """Gate 3 failure: LIVE_TRADE + armed=True + ALLOW_MAINNET_TRADE!=1 → block."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.LIVE_TRADE,
                armed=True,
                futures_port=fake_port,  # type: ignore[arg-type]
            ),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            connector.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.001"),
            )

        # Verify specific error message
        assert "ALLOW_MAINNET_TRADE=1" in str(exc_info.value)

        # Verify port was NOT called
        assert len(fake_port.place_order_calls) == 0

        await connector.close()

    @pytest.mark.asyncio
    async def test_live_trade_no_port_blocks_place_order(
        self,
        fake_clock: FakeClock,
        fake_sleep: FakeSleep,
        set_allow_mainnet: None,  # noqa: ARG002
    ) -> None:
        """Gate 4 failure: LIVE_TRADE + armed + env=1 but futures_port=None → block."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.LIVE_TRADE,
                armed=True,
                futures_port=None,  # Gate 4 fails
            ),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            connector.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.001"),
            )

        # Verify specific error message
        assert "futures_port not configured" in str(exc_info.value)

        await connector.close()

    # --- Positive Path Tests: All Gates Pass ---

    @pytest.mark.asyncio
    async def test_live_trade_all_gates_pass_delegates_place_order(
        self,
        fake_clock: FakeClock,
        fake_sleep: FakeSleep,
        fake_port: FakeFuturesPort,
        set_allow_mainnet: None,  # noqa: ARG002
    ) -> None:
        """All gates pass → place_order delegates to futures_port."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.LIVE_TRADE,
                armed=True,
                futures_port=fake_port,  # type: ignore[arg-type]
            ),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        result = connector.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=5,
        )

        # Verify order_id returned (LIVE_TRADE returns str, not OrderResult)
        assert isinstance(result, str)
        assert "grinder_BTCUSDT_5_" in result

        # Verify futures_port was called with correct args
        assert len(fake_port.place_order_calls) == 1
        call = fake_port.place_order_calls[0]
        assert call["symbol"] == "BTCUSDT"
        assert call["side"] == OrderSide.BUY
        assert call["price"] == Decimal("50000")
        assert call["quantity"] == Decimal("0.001")
        assert call["level_id"] == 5

        await connector.close()

    @pytest.mark.asyncio
    async def test_live_trade_all_gates_pass_delegates_cancel_order(
        self,
        fake_clock: FakeClock,
        fake_sleep: FakeSleep,
        fake_port: FakeFuturesPort,
        set_allow_mainnet: None,  # noqa: ARG002
    ) -> None:
        """All gates pass → cancel_order delegates to futures_port."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.LIVE_TRADE,
                armed=True,
                futures_port=fake_port,  # type: ignore[arg-type]
            ),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        result = connector.cancel_order("grinder_BTCUSDT_5_1234567890")

        # Verify success
        assert result is True

        # Verify futures_port was called
        assert len(fake_port.cancel_order_calls) == 1
        assert fake_port.cancel_order_calls[0] == "grinder_BTCUSDT_5_1234567890"

        await connector.close()

    @pytest.mark.asyncio
    async def test_live_trade_all_gates_pass_delegates_replace_order(
        self,
        fake_clock: FakeClock,
        fake_sleep: FakeSleep,
        fake_port: FakeFuturesPort,
        set_allow_mainnet: None,  # noqa: ARG002
    ) -> None:
        """All gates pass → replace_order delegates to futures_port."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.LIVE_TRADE,
                armed=True,
                futures_port=fake_port,  # type: ignore[arg-type]
            ),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        result = connector.replace_order(
            order_id="grinder_BTCUSDT_5_1234567890",
            new_price=Decimal("51000"),
            new_quantity=Decimal("0.002"),
        )

        # Verify order_id returned (LIVE_TRADE returns str, not OrderResult)
        assert isinstance(result, str)
        assert "grinder_replaced_" in result

        # Verify futures_port was called
        assert len(fake_port.replace_order_calls) == 1
        call = fake_port.replace_order_calls[0]
        assert call["order_id"] == "grinder_BTCUSDT_5_1234567890"
        assert call["new_price"] == Decimal("51000")
        assert call["new_quantity"] == Decimal("0.002")

        await connector.close()

    @pytest.mark.asyncio
    async def test_live_trade_replace_requires_both_price_and_quantity(
        self,
        fake_clock: FakeClock,
        fake_sleep: FakeSleep,
        fake_port: FakeFuturesPort,
        set_allow_mainnet: None,  # noqa: ARG002
    ) -> None:
        """replace_order in LIVE_TRADE requires both new_price and new_quantity."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.LIVE_TRADE,
                armed=True,
                futures_port=fake_port,  # type: ignore[arg-type]
            ),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        # Missing new_quantity
        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            connector.replace_order(
                order_id="grinder_BTCUSDT_5_1234567890",
                new_price=Decimal("51000"),
                new_quantity=None,
            )

        assert "requires both new_price and new_quantity" in str(exc_info.value)

        await connector.close()
