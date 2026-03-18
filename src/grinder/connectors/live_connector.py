"""Live WebSocket connector for Binance market data.

This module provides a production-ready connector for live market data streams
from Binance via WebSocket. It implements the DataConnector ABC with full
hardening (H2 retries, H4 circuit breaker, H5 metrics).

Key design decisions (see ADR-029):
- SafeMode enum for explicit read_only/paper/live_trade modes
- read_only is default (safe by design)
- H2/H4/H5 hardening applied to all operations
- Bounded-time testing support via injectable clock/sleep
- No trading operations in read_only mode

Usage:
    connector = LiveConnectorV0(
        mode=SafeMode.READ_ONLY,  # Default: safe
        symbols=["BTCUSDT", "ETHUSDT"],
    )
    await connector.connect()
    async for snapshot in connector.stream_ticks():
        process(snapshot)
    await connector.close()
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as time_module
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from grinder.connectors.binance_ws import (
    BinanceWsConfig,
    BinanceWsConnector,
    WsTransport,
)
from grinder.connectors.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, default_trip_on
from grinder.connectors.data_connector import (
    ConnectorState,
    DataConnector,
    TimeoutConfig,
)
from grinder.connectors.errors import (
    ConnectorClosedError,
    ConnectorNonRetryableError,
    ConnectorTimeoutError,
    ConnectorTransientError,
)
from grinder.connectors.metrics import CircuitMetricState, get_connector_metrics
from grinder.connectors.paper_execution import (
    OrderRequest,
    OrderResult,
    PaperExecutionAdapter,
)
from grinder.connectors.retries import RetryPolicy, retry_with_policy
from grinder.connectors.timeouts import cancel_tasks_with_timeout, wait_for_with_op

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from decimal import Decimal

    from grinder.contracts import Snapshot
    from grinder.core import OrderSide
    from grinder.execution.binance_futures_port import BinanceFuturesPort

logger = logging.getLogger(__name__)


class SafeMode(Enum):
    """Safe mode for connector operations.

    Determines what operations are allowed:
    - READ_ONLY: Only read market data (no trading) - DEFAULT
    - PAPER: Read data, simulate trading (no real orders)
    - LIVE_TRADE: Full trading capability (requires explicit opt-in)

    See ADR-029 for design decisions.
    """

    READ_ONLY = "read_only"
    PAPER = "paper"
    LIVE_TRADE = "live_trade"


@dataclass
class LiveConnectorConfig:
    """Configuration for LiveConnectorV0.

    Attributes:
        mode: Safe mode (default: READ_ONLY)
        symbols: List of symbols to subscribe to
        ws_url: WebSocket URL (default: Binance testnet for safety)
        use_testnet: Use testnet endpoint (default True for safety)
        timeout_config: Timeout configuration
        retry_policy: Retry policy for transient failures
        circuit_breaker_config: Circuit breaker configuration
        ws_transport: Injectable WS transport for testing (None = real WebSocket)
        armed: Arming gate for LIVE_TRADE mode (default: False, must be True for real trades)
        futures_port: Injectable BinanceFuturesPort for LIVE_TRADE mode (None = not configured)

    LIVE_TRADE Mode Gates (LC-22, ADR-056):
        All 3 gates must pass for real write operations:
        1. armed=True (explicit arming)
        2. mode=LIVE_TRADE (explicit mode)
        3. ALLOW_MAINNET_TRADE=1 env var (external safeguard)

        Any gate failure → ConnectorNonRetryableError with specific message.
    """

    mode: SafeMode = SafeMode.READ_ONLY
    symbols: list[str] = field(default_factory=list)
    ws_url: str | None = None  # Explicit WS URL override (None = derive from use_testnet)
    use_testnet: bool = True
    timeout_config: TimeoutConfig = field(default_factory=TimeoutConfig)
    retry_policy: RetryPolicy = field(default_factory=lambda: RetryPolicy(max_attempts=3))
    circuit_breaker_config: CircuitBreakerConfig = field(
        default_factory=lambda: CircuitBreakerConfig(trip_on=default_trip_on)
    )
    ws_transport: WsTransport | None = None  # Injectable for testing

    # LC-22: LIVE_TRADE gates
    armed: bool = False  # Gate 1: Must be True for real trades
    futures_port: BinanceFuturesPort | None = None  # Injectable port for LIVE_TRADE


@dataclass
class LiveConnectorStats:
    """Statistics for live connector operations."""

    ticks_received: int = 0
    connection_attempts: int = 0
    reconnections: int = 0
    retries: int = 0
    circuit_trips: int = 0
    timeouts: int = 0
    errors: list[str] = field(default_factory=list)


class LiveConnectorV0(DataConnector):
    """Live WebSocket connector for Binance market data (read-only by default).

    Implements DataConnector ABC with full H2/H4/H5 hardening:
    - H2: Retries with exponential backoff
    - H4: Circuit breaker for fast-fail
    - H5: Prometheus metrics for observability

    Safe mode enforcement:
    - READ_ONLY (default): Only stream_ticks() is allowed
    - PAPER: stream_ticks() + simulated trading
    - LIVE_TRADE: Full trading (requires explicit opt-in)

    Example:
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.READ_ONLY,
                symbols=["BTCUSDT"],
            )
        )
        await connector.connect()
        async for snapshot in connector.stream_ticks():
            print(f"Got {snapshot.symbol} @ {snapshot.mid_price}")
        await connector.close()
    """

    def __init__(
        self,
        config: LiveConnectorConfig | None = None,
        *,
        clock: Any = None,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize live connector.

        Args:
            config: Configuration (uses defaults if None)
            clock: Injectable clock for testing (default: time module)
            sleep_func: Injectable sleep function for testing (default: asyncio.sleep)
        """
        self._config = config or LiveConnectorConfig()
        self._clock = clock if clock is not None else time_module
        self._sleep_func = sleep_func if sleep_func is not None else asyncio.sleep

        # Internal state
        self._state = ConnectorState.DISCONNECTED
        self._last_seen_ts: int | None = None
        self._stats = LiveConnectorStats()

        # WebSocket state (not used in v0 mock, placeholder for real impl)
        self._ws: Any = None
        self._subscription_id: int = 0

        # Task tracking for clean shutdown
        self._tasks: set[asyncio.Task[Any]] = set()
        self._task_name_prefix = f"live_connector_{id(self)}_"

        # H4: Circuit breaker
        self._circuit_breaker = CircuitBreaker(
            self._config.circuit_breaker_config,
            clock=self._clock,
        )

        # Initialize circuit state metrics
        for symbol in self._config.symbols:
            op_name = f"stream_{symbol}"
            get_connector_metrics().set_circuit_state(op_name, CircuitMetricState.CLOSED)

        # Paper execution adapter (only used in PAPER mode)
        self._paper_adapter: PaperExecutionAdapter | None = None
        if self._config.mode == SafeMode.PAPER:
            self._paper_adapter = PaperExecutionAdapter(
                clock=self._clock,
                order_id_prefix="PAPER",
            )

        # Real WebSocket connector for market data (LC-21)
        self._ws_connector: BinanceWsConnector | None = None
        self._ws_config: BinanceWsConfig | None = None
        if self._config.symbols:
            # Pass ws_url as override so BinanceWsConfig uses the exact URL
            # chosen by the caller (spot, futures, or testnet).
            self._ws_config = BinanceWsConfig(
                symbols=list(self._config.symbols),
                use_testnet=self._config.use_testnet,
                timeout=self._config.timeout_config,
                ws_url_override=self._config.ws_url if self._config.ws_url else None,
            )
            self._ws_connector = BinanceWsConnector(
                config=self._ws_config,
                transport=self._config.ws_transport,
                clock=lambda: float(self._clock.time()),
            )

    def _now(self) -> float:
        """Get current time from clock."""
        return float(self._clock.time())

    @property
    def state(self) -> ConnectorState:
        """Get current connector state."""
        return self._state

    @property
    def last_seen_ts(self) -> int | None:
        """Get timestamp of last delivered snapshot."""
        return self._last_seen_ts

    @property
    def stats(self) -> LiveConnectorStats:
        """Get connector statistics."""
        return self._stats

    @property
    def mode(self) -> SafeMode:
        """Get current safe mode."""
        return self._config.mode

    @property
    def symbols(self) -> list[str]:
        """Get subscribed symbols."""
        return list(self._config.symbols)

    async def connect(self) -> None:
        """Establish WebSocket connection.

        Uses H2 retry policy for transient failures.
        Uses H4 circuit breaker for fast-fail.

        Raises:
            ConnectionError: If connection fails after retries
            ConnectorTimeoutError: If connection times out
        """
        if self._state == ConnectorState.CONNECTED:
            return  # Already connected

        self._state = ConnectorState.CONNECTING
        self._stats.connection_attempts += 1

        try:
            # H4: Check circuit breaker before attempting
            self._circuit_breaker.before_call("connect")

            # H2: Retry with policy
            _, retry_stats = await retry_with_policy(
                "connect",
                self._do_connect,
                self._config.retry_policy,
                sleep_func=self._sleep_func,
            )
            self._stats.retries += retry_stats.retries

            # Record success
            self._circuit_breaker.record_success("connect")

        except ConnectorTimeoutError:
            self._state = ConnectorState.DISCONNECTED
            self._stats.timeouts += 1
            self._stats.errors.append("Connect timeout")
            self._circuit_breaker.record_failure("connect", "timeout")
            raise
        except ConnectorTransientError as e:
            self._state = ConnectorState.DISCONNECTED
            self._stats.errors.append(f"Connect failed: {e}")
            self._circuit_breaker.record_failure("connect", "transient")
            raise ConnectionError(f"Connect failed: {e}") from e
        except Exception as e:
            self._state = ConnectorState.DISCONNECTED
            self._stats.errors.append(f"Connect error: {e}")
            raise

    async def _do_connect(self) -> None:
        """Internal connect implementation.

        Connects real WebSocket if symbols are configured.
        """
        # Connect real WS if configured (LC-21)
        if self._ws_connector is not None:
            await wait_for_with_op(
                self._ws_connector.connect(),
                timeout_ms=self._config.timeout_config.connect_timeout_ms,
                op="connect",
            )
            # Update metrics
            get_connector_metrics().set_ws_connected("bookTicker", True)
        else:
            # No symbols: minimal async yield for testing
            await self._sleep_func(0.001)

        self._state = ConnectorState.CONNECTED
        logger.info(
            "LiveConnectorV0 connected (mode=%s, symbols=%s, ws=%s)",
            self._config.mode.value,
            self._config.symbols,
            "real" if self._ws_connector else "none",
        )

    async def close(self) -> None:
        """Close WebSocket connection and release resources.

        Cancels all background tasks and waits for completion.
        Safe to call multiple times. Idempotent.
        """
        if self._state == ConnectorState.CLOSED:
            return

        self._state = ConnectorState.CLOSED

        # Close real WS if configured (LC-21)
        if self._ws_connector is not None:
            try:
                await self._ws_connector.close()
                get_connector_metrics().set_ws_connected("bookTicker", False)
            except Exception as e:
                logger.warning("Error closing WS connector: %s", e)

        # Cancel and await all tracked tasks
        if self._tasks:
            cancelled, timed_out = await cancel_tasks_with_timeout(
                self._tasks,
                timeout_ms=self._config.timeout_config.close_timeout_ms,
                task_name_prefix=self._task_name_prefix,
            )
            self._stats.errors.append(f"Closed: {cancelled} tasks cancelled, {timed_out} timed out")

        self._tasks.clear()
        logger.info("LiveConnectorV0 closed")

    def iter_snapshots(self) -> AsyncIterator[Snapshot]:
        """Iterate over snapshots (DataConnector ABC compliance).

        Alias for stream_ticks() for DataConnector interface compliance.
        """
        return self.stream_ticks()

    async def stream_ticks(self) -> AsyncIterator[Snapshot]:
        """Stream market data ticks.

        This is the primary data streaming method. Always available
        regardless of SafeMode (reading data is always safe).

        Yields snapshots in timestamp order with idempotency guarantees.

        Raises:
            ConnectionError: If not connected
            ConnectorClosedError: If connector is closed during streaming
            ConnectorTimeoutError: If read times out
        """
        if self._state == ConnectorState.CLOSED:
            raise ConnectorClosedError("stream_ticks")
        if self._state != ConnectorState.CONNECTED:
            raise ConnectionError(f"Cannot stream: connector state is {self._state.value}")

        # LC-21: Delegate to real WS connector if available
        if self._ws_connector is not None:
            metrics = get_connector_metrics()
            try:
                async for snapshot in self._ws_connector.iter_snapshots():
                    # Check if closed during iteration (state can change during async yield)
                    if self._state == ConnectorState.CLOSED:  # type: ignore[comparison-overlap]
                        break

                    # Update stats
                    self._stats.ticks_received += 1
                    self._last_seen_ts = snapshot.ts

                    # Record metrics (use connector name, not symbol, per ADR-028)
                    metrics.record_tick_received("bookTicker")
                    metrics.set_last_tick_ts("bookTicker", snapshot.ts)

                    yield snapshot

            except ConnectorTransientError as e:
                # WS disconnected - record reconnect metric
                logger.warning("WS transient error in stream_ticks: %s", e)
                metrics.record_ws_reconnect("bookTicker", "error")
                metrics.set_ws_connected("bookTicker", False)
                raise

            except ConnectorClosedError:
                # Normal close - don't re-raise
                metrics.set_ws_connected("bookTicker", False)
                return

        else:
            # No WS connector: yield nothing (v0 mock behavior)
            return
            yield  # Makes this an async generator

    async def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to additional symbols.

        Adds symbols to the active subscription. Only available when connected.

        Args:
            symbols: List of symbols to add to subscription

        Raises:
            ConnectionError: If not connected
            ConnectorClosedError: If connector is closed
        """
        if self._state == ConnectorState.CLOSED:
            raise ConnectorClosedError("subscribe")
        if self._state != ConnectorState.CONNECTED:
            raise ConnectionError(f"Cannot subscribe: connector state is {self._state.value}")

        # Add to config
        for symbol in symbols:
            if symbol not in self._config.symbols:
                self._config.symbols.append(symbol)
                # Initialize circuit state for new symbol
                op_name = f"stream_{symbol}"
                get_connector_metrics().set_circuit_state(op_name, CircuitMetricState.CLOSED)

        logger.info("Subscribed to additional symbols: %s", symbols)

    async def reconnect(self) -> None:
        """Reconnect after failure.

        Uses retry policy for backoff. Resumes from last_seen_ts.

        Raises:
            ConnectionError: If reconnection fails after max retries
            ConnectorClosedError: If connector is closed
        """
        if self._state == ConnectorState.CLOSED:
            raise ConnectorClosedError("reconnect")

        self._state = ConnectorState.RECONNECTING
        self._stats.reconnections += 1

        # Record reconnect metric (LC-21)
        if self._ws_connector is not None:
            get_connector_metrics().record_ws_reconnect("bookTicker", "disconnect")
            get_connector_metrics().set_ws_connected("bookTicker", False)

        try:
            # Reconnect WS connector if available (LC-21)
            if self._ws_connector is not None:
                await self._ws_connector.reconnect()
                get_connector_metrics().set_ws_connected("bookTicker", True)
                self._state = ConnectorState.CONNECTED
            else:
                # Use same connect logic with retries
                _, retry_stats = await retry_with_policy(
                    "reconnect",
                    self._do_connect,
                    self._config.retry_policy,
                    sleep_func=self._sleep_func,
                )
                self._stats.retries += retry_stats.retries

        except Exception as e:
            self._state = ConnectorState.DISCONNECTED
            self._stats.errors.append(f"Reconnect failed: {e}")
            raise ConnectionError(f"Reconnect failed: {e}") from e

    def assert_mode(self, required_mode: SafeMode) -> None:
        """Assert that current mode allows the requested operation.

        Args:
            required_mode: Minimum required mode for operation

        Raises:
            ConnectorNonRetryableError: If current mode is insufficient.
                This is non-retryable by design - mode violations are
                configuration errors, not transient failures.
        """
        mode_order = {SafeMode.READ_ONLY: 0, SafeMode.PAPER: 1, SafeMode.LIVE_TRADE: 2}

        if mode_order[self._config.mode] < mode_order[required_mode]:
            raise ConnectorNonRetryableError(
                f"SafeMode violation: operation requires mode={required_mode.value}, "
                f"but connector is in mode={self._config.mode.value}"
            )

    def _check_live_trade_gates(self, op: str) -> None:
        """Check all 3 gates for LIVE_TRADE write operations (LC-22).

        Gates (all must pass):
        1. armed=True (explicit arming)
        2. mode=LIVE_TRADE (explicit mode)
        3. ALLOW_MAINNET_TRADE=1 env var (external safeguard)

        Args:
            op: Operation name for error messages

        Raises:
            ConnectorNonRetryableError: If any gate fails, with specific reason.
        """
        # Gate 1: armed check
        if not self._config.armed:
            raise ConnectorNonRetryableError(
                f"Cannot {op}: armed=False. "
                "Set armed=True in config to enable LIVE_TRADE operations."
            )

        # Gate 2: mode check (already LIVE_TRADE if we get here, but explicit)
        if self._config.mode != SafeMode.LIVE_TRADE:
            raise ConnectorNonRetryableError(
                f"Cannot {op}: mode={self._config.mode.value}, requires LIVE_TRADE. "
                "Set mode=SafeMode.LIVE_TRADE to enable real trading."
            )

        # Gate 3: env var check
        allow_mainnet = os.environ.get("ALLOW_MAINNET_TRADE", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if not allow_mainnet:
            raise ConnectorNonRetryableError(
                f"Cannot {op}: ALLOW_MAINNET_TRADE=1 not set. "
                "Set ALLOW_MAINNET_TRADE=1 environment variable to enable mainnet trading."
            )

        # Gate 4: futures_port must be configured
        if self._config.futures_port is None:
            raise ConnectorNonRetryableError(
                f"Cannot {op}: futures_port not configured. "
                "Set futures_port in config to enable LIVE_TRADE operations."
            )

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Get circuit breaker for testing/monitoring."""
        return self._circuit_breaker

    @property
    def paper_adapter(self) -> PaperExecutionAdapter | None:
        """Get paper execution adapter (only available in PAPER mode)."""
        return self._paper_adapter

    # --- Write Operations (PAPER mode only in v0) ---

    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
        client_order_id: str | None = None,
        level_id: int = 0,
        ts: int | None = None,
    ) -> OrderResult | str:
        """Place a new order.

        In PAPER mode: Simulates order placement with instant fill.
        In READ_ONLY mode: Raises ConnectorNonRetryableError.
        In LIVE_TRADE mode (LC-22): Delegates to BinanceFuturesPort if all 3 gates pass.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            side: Order side (BUY or SELL)
            price: Limit price
            quantity: Order quantity
            client_order_id: Optional client-provided order ID
            level_id: Grid level identifier (for LIVE_TRADE mode)
            ts: Current timestamp (for LIVE_TRADE mode, uses now if None)

        Returns:
            OrderResult with the placed order details (PAPER mode)
            str order_id (LIVE_TRADE mode)

        Raises:
            ConnectorNonRetryableError: If mode doesn't allow write operations
            ConnectorClosedError: If connector is closed
            PaperOrderError: If order parameters are invalid (PAPER mode)
        """
        if self._state == ConnectorState.CLOSED:
            raise ConnectorClosedError("place_order")

        # Check mode - READ_ONLY blocks all writes
        self.assert_mode(SafeMode.PAPER)

        # LIVE_TRADE mode (LC-22): check 3 gates and delegate to futures_port
        if self._config.mode == SafeMode.LIVE_TRADE:
            self._check_live_trade_gates("place_order")

            # Use current time if not provided
            if ts is None:
                ts = int(self._now() * 1000)

            return self._config.futures_port.place_order(  # type: ignore[union-attr]
                symbol=symbol,
                side=side,
                price=price,
                quantity=quantity,
                level_id=level_id,
                ts=ts,
            )

        # PAPER mode: use paper adapter
        if self._paper_adapter is None:
            raise ConnectorNonRetryableError("Paper adapter not initialized. This is a bug.")

        request = OrderRequest(
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            client_order_id=client_order_id,
        )

        return self._paper_adapter.place_order(request)

    def cancel_order(self, order_id: str) -> OrderResult | bool:
        """Cancel an existing order.

        In PAPER mode: Cancels the simulated order.
        In READ_ONLY mode: Raises ConnectorNonRetryableError.
        In LIVE_TRADE mode (LC-22): Delegates to BinanceFuturesPort if all 3 gates pass.

        Args:
            order_id: ID of order to cancel

        Returns:
            OrderResult with the cancelled order state (PAPER mode)
            bool indicating success (LIVE_TRADE mode)

        Raises:
            ConnectorNonRetryableError: If mode doesn't allow write operations
            ConnectorClosedError: If connector is closed
            PaperOrderError: If order not found or cannot be cancelled
        """
        if self._state == ConnectorState.CLOSED:
            raise ConnectorClosedError("cancel_order")

        # Check mode - READ_ONLY blocks all writes
        self.assert_mode(SafeMode.PAPER)

        # LIVE_TRADE mode (LC-22): check 3 gates and delegate to futures_port
        if self._config.mode == SafeMode.LIVE_TRADE:
            self._check_live_trade_gates("cancel_order")

            return self._config.futures_port.cancel_order(order_id)  # type: ignore[union-attr]

        # PAPER mode: use paper adapter
        if self._paper_adapter is None:
            raise ConnectorNonRetryableError("Paper adapter not initialized. This is a bug.")

        return self._paper_adapter.cancel_order(order_id)

    def replace_order(
        self,
        order_id: str,
        new_price: Decimal | None = None,
        new_quantity: Decimal | None = None,
        ts: int | None = None,
    ) -> OrderResult | str:
        """Replace an existing order with new parameters.

        In PAPER mode: Cancels old order and places new one (cancel+new pattern).
        In READ_ONLY mode: Raises ConnectorNonRetryableError.
        In LIVE_TRADE mode (LC-22): Delegates to BinanceFuturesPort if all 3 gates pass.

        Args:
            order_id: ID of order to replace
            new_price: New price (uses old price if None)
            new_quantity: New quantity (uses old quantity if None)
            ts: Current timestamp (for LIVE_TRADE mode, uses now if None)

        Returns:
            OrderResult with the NEW order (not the cancelled one) (PAPER mode)
            str new_order_id (LIVE_TRADE mode)

        Raises:
            ConnectorNonRetryableError: If mode doesn't allow write operations
            ConnectorClosedError: If connector is closed
            PaperOrderError: If order not found or cannot be replaced
        """
        if self._state == ConnectorState.CLOSED:
            raise ConnectorClosedError("replace_order")

        # Check mode - READ_ONLY blocks all writes
        self.assert_mode(SafeMode.PAPER)

        # LIVE_TRADE mode (LC-22): check 3 gates and delegate to futures_port
        if self._config.mode == SafeMode.LIVE_TRADE:
            self._check_live_trade_gates("replace_order")

            # Use current time if not provided
            if ts is None:
                ts = int(self._now() * 1000)

            # For replace, both price and quantity are required for futures_port
            if new_price is None or new_quantity is None:
                raise ConnectorNonRetryableError(
                    "replace_order in LIVE_TRADE mode requires both new_price and new_quantity"
                )

            return self._config.futures_port.replace_order(  # type: ignore[union-attr]
                order_id=order_id,
                new_price=new_price,
                new_quantity=new_quantity,
                ts=ts,
            )

        # PAPER mode: use paper adapter
        if self._paper_adapter is None:
            raise ConnectorNonRetryableError("Paper adapter not initialized. This is a bug.")

        return self._paper_adapter.replace_order(order_id, new_price, new_quantity)
