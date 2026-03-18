"""Binance WebSocket connector for live market data.

This module provides a real-time WebSocket connection to Binance
for streaming bookTicker data (L1 best bid/ask).

Key features:
- Async WebSocket client using websockets library
- Auto-reconnect with exponential backoff
- Testable via transport injection (fake WS for tests)
- Converts bookTicker messages to Snapshot objects

Usage:
    async with BinanceWsConnector(symbols=["BTCUSDT"]) as ws:
        async for snapshot in ws.iter_snapshots():
            process(snapshot)

See ADR-037 for design decisions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from grinder.connectors.data_connector import (
    ConnectorState,
    DataConnector,
    RetryConfig,
    TimeoutConfig,
)
from grinder.connectors.errors import (
    ConnectorClosedError,
    ConnectorTimeoutError,
    ConnectorTransientError,
)
from grinder.contracts import Snapshot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

logger = logging.getLogger(__name__)


# Binance WebSocket endpoints
BINANCE_WS_TESTNET = "wss://testnet.binance.vision/ws"
BINANCE_WS_MAINNET = "wss://stream.binance.com:9443/ws"
BINANCE_WS_FUTURES_MAINNET = "wss://fstream.binance.com/ws"


class WsTransport(ABC):
    """Abstract WebSocket transport for testability."""

    @abstractmethod
    async def connect(self, url: str) -> None:
        """Connect to WebSocket endpoint."""

    @abstractmethod
    async def send(self, message: str) -> None:
        """Send message to WebSocket."""

    @abstractmethod
    async def recv(self) -> str:
        """Receive message from WebSocket."""

    @abstractmethod
    async def close(self) -> None:
        """Close WebSocket connection."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connected."""


class WebsocketsTransport(WsTransport):
    """Real WebSocket transport using websockets library."""

    def __init__(self) -> None:
        self._ws: Any = None

    async def connect(self, url: str) -> None:
        """Connect to WebSocket endpoint."""
        try:
            import websockets  # noqa: PLC0415 - optional dependency

            self._ws = await websockets.connect(url)
        except ImportError as e:
            msg = "websockets library required: pip install websockets"
            raise ImportError(msg) from e

    async def send(self, message: str) -> None:
        """Send message to WebSocket."""
        if self._ws is None:
            msg = "Not connected"
            raise ConnectorClosedError(msg)
        await self._ws.send(message)

    async def recv(self) -> str:
        """Receive message from WebSocket."""
        if self._ws is None:
            msg = "Not connected"
            raise ConnectorClosedError(msg)
        result: str = await self._ws.recv()
        return result

    async def close(self) -> None:
        """Close WebSocket connection."""
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._ws is not None and self._ws.open


class FakeWsTransport(WsTransport):
    """Fake WebSocket transport for testing.

    Supports:
    - Pre-loaded messages to yield
    - Simulated delays
    - Error injection
    """

    def __init__(
        self,
        messages: list[str] | None = None,
        delay_ms: int = 0,
        error_after: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize fake transport.

        Args:
            messages: List of JSON messages to yield
            delay_ms: Simulated delay between messages (ms)
            error_after: Raise error after N messages
            clock: Optional fake clock function
        """
        self._messages = list(messages) if messages else []
        self._delay_ms = delay_ms
        self._error_after = error_after
        self._clock = clock or time.time
        self._connected = False
        self._index = 0
        self._recv_count = 0

    async def connect(self, url: str) -> None:
        """Simulate connection."""
        self._connected = True
        self._index = 0
        self._recv_count = 0
        logger.debug("FakeWsTransport connected to %s", url)

    async def send(self, message: str) -> None:
        """Simulate sending (no-op for bookTicker)."""
        if not self._connected:
            msg = "Not connected"
            raise ConnectorClosedError(msg)
        logger.debug("FakeWsTransport send: %s", message[:100])

    async def recv(self) -> str:
        """Return next pre-loaded message."""
        if not self._connected:
            msg = "Not connected"
            raise ConnectorClosedError(msg)

        # Check error injection
        if self._error_after is not None and self._recv_count >= self._error_after:
            msg = "Simulated WS error"
            raise ConnectorTransientError(msg)

        # Check if messages exhausted — signal end-of-data cleanly
        if self._index >= len(self._messages):
            msg = "Fixture messages exhausted"
            raise ConnectorClosedError(msg)

        # Simulate delay
        if self._delay_ms > 0:
            await asyncio.sleep(self._delay_ms / 1000.0)

        msg = self._messages[self._index]
        self._index += 1
        self._recv_count += 1
        return msg

    async def close(self) -> None:
        """Simulate disconnection."""
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._connected

    def add_message(self, message: str) -> None:
        """Add a message to the queue."""
        self._messages.append(message)

    def add_messages(self, messages: list[str]) -> None:
        """Add multiple messages to the queue."""
        self._messages.extend(messages)


@dataclass
class BinanceWsConfig:
    """Configuration for Binance WebSocket connector.

    Attributes:
        symbols: List of symbols to subscribe to (e.g., ["BTCUSDT", "ETHUSDT"])
        use_testnet: Use testnet endpoint (default True for safety)
        timeout: Timeout configuration
        retry: Retry configuration
    """

    symbols: list[str] = field(default_factory=list)
    use_testnet: bool = True
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)

    @property
    def ws_url(self) -> str:
        """Get WebSocket URL."""
        return BINANCE_WS_TESTNET if self.use_testnet else BINANCE_WS_MAINNET

    def get_subscribe_message(self) -> str:
        """Get subscription message for bookTicker streams."""
        streams = [f"{s.lower()}@bookTicker" for s in self.symbols]
        return json.dumps(
            {
                "method": "SUBSCRIBE",
                "params": streams,
                "id": 1,
            }
        )


@dataclass
class BinanceWsStats:
    """Statistics for Binance WebSocket connector."""

    messages_received: int = 0
    snapshots_yielded: int = 0
    reconnects: int = 0
    errors: int = 0
    last_message_ts: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "messages_received": self.messages_received,
            "snapshots_yielded": self.snapshots_yielded,
            "reconnects": self.reconnects,
            "errors": self.errors,
            "last_message_ts": self.last_message_ts,
        }


class BinanceWsConnector(DataConnector):
    """Binance WebSocket connector for bookTicker stream.

    Implements DataConnector ABC for live market data streaming.

    Features:
    - Connects to Binance bookTicker WebSocket stream
    - Converts messages to Snapshot objects
    - Auto-reconnect with exponential backoff
    - Idempotency via last_seen_ts tracking
    - Testable via transport injection

    Usage:
        config = BinanceWsConfig(symbols=["BTCUSDT"])
        async with BinanceWsConnector(config) as ws:
            async for snapshot in ws.iter_snapshots():
                print(snapshot)
    """

    def __init__(
        self,
        config: BinanceWsConfig,
        transport: WsTransport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize connector.

        Args:
            config: Connector configuration
            transport: WebSocket transport (injectable for testing)
            clock: Clock function for timestamps (injectable for testing)
        """
        self._config = config
        self._transport = transport or WebsocketsTransport()
        self._clock = clock or time.time
        self._state = ConnectorState.DISCONNECTED
        self._last_seen_ts: int | None = None
        self._stats = BinanceWsStats()
        self._closed = False

    @property
    def state(self) -> ConnectorState:
        """Get current connector state."""
        return self._state

    @property
    def last_seen_ts(self) -> int | None:
        """Get timestamp of last delivered snapshot."""
        return self._last_seen_ts

    @property
    def stats(self) -> BinanceWsStats:
        """Get connector statistics."""
        return self._stats

    @property
    def config(self) -> BinanceWsConfig:
        """Get connector configuration."""
        return self._config

    async def connect(self) -> None:
        """Establish WebSocket connection."""
        if self._closed:
            msg = "Connector is closed"
            raise ConnectorClosedError(msg)

        self._state = ConnectorState.CONNECTING
        try:
            await asyncio.wait_for(
                self._transport.connect(self._config.ws_url),
                timeout=self._config.timeout.connect_timeout_ms / 1000.0,
            )

            # Subscribe to bookTicker streams
            subscribe_msg = self._config.get_subscribe_message()
            await self._transport.send(subscribe_msg)

            self._state = ConnectorState.CONNECTED
            logger.info(
                "Connected to Binance WS (%s), symbols=%s",
                "testnet" if self._config.use_testnet else "mainnet",
                self._config.symbols,
            )
        except TimeoutError as e:
            self._state = ConnectorState.DISCONNECTED
            raise ConnectorTimeoutError(
                op="connect",
                timeout_ms=self._config.timeout.connect_timeout_ms,
                message=str(e),
            ) from e
        except Exception as e:
            self._state = ConnectorState.DISCONNECTED
            self._stats.errors += 1
            raise ConnectorTransientError(str(e)) from e

    async def close(self) -> None:
        """Close WebSocket connection."""
        self._closed = True
        self._state = ConnectorState.CLOSED
        try:  # noqa: SIM105 - contextlib.suppress doesn't work with async
            await asyncio.wait_for(
                self._transport.close(),
                timeout=self._config.timeout.close_timeout_ms / 1000.0,
            )
        except Exception:
            pass  # Best effort close
        logger.info("Binance WS connector closed")

    async def reconnect(self) -> None:
        """Reconnect after failure."""
        if self._closed:
            msg = "Connector is closed"
            raise ConnectorClosedError(msg)

        self._state = ConnectorState.RECONNECTING
        self._stats.reconnects += 1

        for attempt in range(self._config.retry.max_retries):
            try:
                await self._transport.close()
                await self.connect()
                return
            except Exception as e:
                delay_ms = self._config.retry.get_delay_ms(attempt)
                logger.warning(
                    "Reconnect attempt %d/%d failed: %s. Retrying in %dms",
                    attempt + 1,
                    self._config.retry.max_retries,
                    str(e),
                    delay_ms,
                )
                await asyncio.sleep(delay_ms / 1000.0)

        self._state = ConnectorState.DISCONNECTED
        msg = f"Max reconnect attempts ({self._config.retry.max_retries}) exceeded"
        raise ConnectorTransientError(msg)

    async def iter_snapshots(self) -> AsyncIterator[Snapshot]:
        """Iterate over snapshots from WebSocket stream.

        Yields Snapshot objects parsed from bookTicker messages.
        Maintains idempotency via timestamp tracking.

        Yields:
            Snapshot objects in timestamp order
        """
        if self._state != ConnectorState.CONNECTED:
            msg = f"Not connected (state={self._state.value})"
            raise ConnectorClosedError(msg)

        while not self._closed:
            # Silent disconnect: transport reports not connected (WS FIN/RST/drop).
            # Instead of exiting, attempt reconnect with backoff.
            if not self._transport.is_connected:
                logger.warning("WS disconnected (open=false), attempting reconnect")
                try:
                    await self.reconnect()
                except ConnectorClosedError:
                    break
                except ConnectorTransientError:
                    logger.error("WS reconnect failed after max retries, exiting")
                    break
                continue

            try:
                raw_msg = await asyncio.wait_for(
                    self._transport.recv(),
                    timeout=self._config.timeout.read_timeout_ms / 1000.0,
                )
                self._stats.messages_received += 1

                # Parse message
                snapshot = self._parse_message(raw_msg)
                if snapshot is None:
                    continue  # Skip non-snapshot messages (e.g., subscribe response)

                # Idempotency check
                if self._last_seen_ts is not None and snapshot.ts <= self._last_seen_ts:
                    logger.debug("Skipping duplicate/old snapshot ts=%d", snapshot.ts)
                    continue

                self._last_seen_ts = snapshot.ts
                self._stats.snapshots_yielded += 1
                self._stats.last_message_ts = snapshot.ts

                yield snapshot

            except TimeoutError:
                # Read timeout - reconnect
                logger.warning("Read timeout, attempting reconnect")
                await self.reconnect()

            except ConnectorTransientError:
                # Transient error - reconnect
                logger.warning("Transient error, attempting reconnect")
                await self.reconnect()

            except ConnectorClosedError:
                break

    def _parse_message(self, raw_msg: str) -> Snapshot | None:
        """Parse raw WebSocket message to Snapshot.

        Args:
            raw_msg: Raw JSON message string

        Returns:
            Snapshot if valid bookTicker message, None otherwise
        """
        try:
            data = json.loads(raw_msg)

            # Skip subscription response
            if "result" in data or "id" in data:
                return None

            # Validate bookTicker fields
            if not all(k in data for k in ["s", "b", "B", "a", "A"]):
                logger.debug("Skipping non-bookTicker message: %s", raw_msg[:100])
                return None

            # Get receive timestamp
            recv_ts = int(self._clock() * 1000)

            # Build Snapshot
            # Note: bookTicker doesn't have last_price/last_qty, use mid as approximation
            bid_price = Decimal(data["b"])
            ask_price = Decimal(data["a"])
            mid_price = (bid_price + ask_price) / 2

            return Snapshot(
                ts=recv_ts,
                symbol=data["s"],
                bid_price=bid_price,
                ask_price=ask_price,
                bid_qty=Decimal(data["B"]),
                ask_qty=Decimal(data["A"]),
                last_price=mid_price,  # Approximation
                last_qty=Decimal("0"),  # Not available in bookTicker
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Failed to parse message: %s - %s", raw_msg[:100], str(e))
            self._stats.errors += 1
            return None

    async def __aenter__(self) -> BinanceWsConnector:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()
