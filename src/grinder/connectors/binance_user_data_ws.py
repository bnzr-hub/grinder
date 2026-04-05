"""Binance Futures USDT-M user-data stream connector.

This module provides:
- ListenKeyManager: HTTP operations for listenKey lifecycle
- FuturesUserDataWsConnector: WebSocket connector for user-data stream

See ADR-041 for design decisions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from grinder.connectors.binance_ws import WebsocketsTransport, WsTransport
from grinder.connectors.data_connector import ConnectorState, RetryConfig, TimeoutConfig
from grinder.connectors.errors import (
    ConnectorClosedError,
    ConnectorNonRetryableError,
    ConnectorTransientError,
)
from grinder.connectors.metrics import get_connector_metrics
from grinder.execution.futures_events import UserDataEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from grinder.execution.binance_port import HttpClient


class ListenKeyManagerProtocol(Protocol):
    """Protocol for listenKey lifecycle management."""

    def create(self) -> str:
        """Create a new listenKey."""
        ...

    def keepalive(self, listen_key: str) -> bool:
        """Keep a listenKey alive."""
        ...

    def close(self, listen_key: str) -> bool:
        """Close a listenKey."""
        ...


logger = logging.getLogger(__name__)

# Binance Futures URLs
BINANCE_FUTURES_TESTNET_URL = "https://testnet.binancefuture.com"
BINANCE_FUTURES_MAINNET_URL = "https://fapi.binance.com"
BINANCE_FUTURES_WS_TESTNET = "wss://stream.binancefuture.com/ws"
BINANCE_FUTURES_WS_MAINNET = "wss://fstream.binance.com/ws"


@dataclass
class ListenKeyConfig:
    """Configuration for ListenKeyManager."""

    base_url: str = BINANCE_FUTURES_TESTNET_URL
    api_key: str = ""
    timeout_ms: int = 5000


class ListenKeyManager:
    """Manages Binance Futures listenKey lifecycle via HTTP API.

    The listenKey is required to connect to the user-data stream.
    It must be kept alive via periodic PUT requests (every 30-60 minutes).

    Attributes:
        http_client: HttpClient for making REST requests
        config: Configuration for the manager
    """

    def __init__(self, http_client: HttpClient, config: ListenKeyConfig) -> None:
        self._http_client = http_client
        self._config = config

    def _get_headers(self) -> dict[str, str]:
        """Get headers with API key."""
        return {"X-MBX-APIKEY": self._config.api_key}

    def create(self) -> str:
        """Create a new listenKey.

        POST /fapi/v1/listenKey

        Returns:
            listenKey string for WebSocket connection

        Raises:
            ConnectorNonRetryableError: If API key is invalid
            ConnectorTransientError: If request fails (retryable)
        """
        response = self._http_client.request(
            method="POST",
            url=f"{self._config.base_url}/fapi/v1/listenKey",
            params={},
            headers=self._get_headers(),
            timeout_ms=self._config.timeout_ms,
        )

        if response.status_code == 401:
            raise ConnectorNonRetryableError("Invalid API key for listenKey creation")

        if response.status_code != 200:
            raise ConnectorTransientError(
                f"Failed to create listenKey: HTTP {response.status_code}"
            )

        listen_key = ""
        if isinstance(response.json_data, dict):
            listen_key = response.json_data.get("listenKey", "")

        if not listen_key:
            raise ConnectorTransientError("Empty listenKey in response")

        logger.info("listenKey_created", extra={"listen_key_prefix": listen_key[:8]})
        return listen_key

    def keepalive(self, listen_key: str) -> bool:
        """Keep a listenKey alive.

        PUT /fapi/v1/listenKey

        Args:
            listen_key: The listenKey to keep alive

        Returns:
            True if successful, False otherwise
        """
        try:
            response = self._http_client.request(
                method="PUT",
                url=f"{self._config.base_url}/fapi/v1/listenKey",
                params={"listenKey": listen_key},
                headers=self._get_headers(),
                timeout_ms=self._config.timeout_ms,
            )

            if response.status_code == 200:
                logger.debug("keepalive_ok", extra={"listen_key_prefix": listen_key[:8]})
                return True

            logger.warning(
                "keepalive_failed",
                extra={
                    "listen_key_prefix": listen_key[:8],
                    "status_code": response.status_code,
                },
            )
            return False

        except Exception as e:
            logger.warning(
                "keepalive_error",
                extra={"listen_key_prefix": listen_key[:8], "error": str(e)},
            )
            return False

    def close(self, listen_key: str) -> bool:
        """Close a listenKey.

        DELETE /fapi/v1/listenKey

        Args:
            listen_key: The listenKey to close

        Returns:
            True if successful, False otherwise
        """
        try:
            response = self._http_client.request(
                method="DELETE",
                url=f"{self._config.base_url}/fapi/v1/listenKey",
                params={"listenKey": listen_key},
                headers=self._get_headers(),
                timeout_ms=self._config.timeout_ms,
            )

            if response.status_code == 200:
                logger.info("listenKey_closed", extra={"listen_key_prefix": listen_key[:8]})
                return True

            return False

        except Exception as e:
            logger.warning(
                "listenKey_close_error",
                extra={"listen_key_prefix": listen_key[:8], "error": str(e)},
            )
            return False


@dataclass
class UserDataWsConfig:
    """Configuration for FuturesUserDataWsConnector."""

    # HTTP config (for listenKey)
    base_url: str = BINANCE_FUTURES_TESTNET_URL
    api_key: str = ""

    # WS config
    use_testnet: bool = True

    # Timing
    keepalive_interval_sec: int = 30
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)

    # Symbol filter (optional)
    symbol_filter: str | None = None

    @property
    def ws_base_url(self) -> str:
        """Get WebSocket base URL."""
        return BINANCE_FUTURES_WS_TESTNET if self.use_testnet else BINANCE_FUTURES_WS_MAINNET


@dataclass
class UserDataWsStats:
    """Statistics for user-data WebSocket connector."""

    messages_received: int = 0
    order_events: int = 0
    position_events: int = 0
    unknown_events: int = 0
    unknown_event_types: dict[str, int] = field(default_factory=dict)
    reconnects: int = 0
    keepalive_count: int = 0
    errors: int = 0
    last_message_ts: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "messages_received": self.messages_received,
            "order_events": self.order_events,
            "position_events": self.position_events,
            "unknown_events": self.unknown_events,
            "unknown_event_types": dict(self.unknown_event_types),
            "reconnects": self.reconnects,
            "keepalive_count": self.keepalive_count,
            "errors": self.errors,
            "last_message_ts": self.last_message_ts,
        }


class FuturesUserDataWsConnector:
    """WebSocket connector for Binance Futures user-data stream.

    Features:
    - ListenKey lifecycle management (create/keepalive/close)
    - Automatic keepalive (every 30 sec by default)
    - Auto-reconnect with exponential backoff
    - Event normalization (ORDER_TRADE_UPDATE, ACCOUNT_UPDATE)
    - Testable via transport/clock/listenKeyManager injection

    Usage:
        async with FuturesUserDataWsConnector(config) as ws:
            async for event in ws.iter_events():
                if event.event_type == UserDataEventType.ORDER_TRADE_UPDATE:
                    handle_order_update(event.order_event)
    """

    def __init__(
        self,
        config: UserDataWsConfig,
        listen_key_manager: ListenKeyManagerProtocol | None = None,
        transport: WsTransport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the connector.

        Args:
            config: Configuration for the connector
            listen_key_manager: Optional manager for listenKey lifecycle (for testing)
            transport: Optional WsTransport (FakeWsTransport for testing)
            clock: Optional clock function (for testing keepalive timing)
        """
        self._config = config
        self._listen_key_manager: ListenKeyManagerProtocol | None = listen_key_manager
        self._transport = transport or WebsocketsTransport()
        self._clock = clock or time.time

        self._listen_key: str = ""
        self._state = ConnectorState.DISCONNECTED
        self._keepalive_task: asyncio.Task[None] | None = None
        self._closed = False
        self._stats = UserDataWsStats()

    @property
    def state(self) -> ConnectorState:
        """Get current connector state."""
        return self._state

    @property
    def stats(self) -> UserDataWsStats:
        """Get connector statistics."""
        return self._stats

    @property
    def listen_key(self) -> str:
        """Get current listenKey (for debugging)."""
        return self._listen_key

    async def connect(self) -> None:
        """Connect to user-data stream.

        1. Create listenKey via REST API
        2. Connect WebSocket to stream URL with listenKey
        3. Start keepalive loop

        Raises:
            ConnectorClosedError: If connector is closed
            ConnectorNonRetryableError: If listenKey creation fails (invalid API key)
            ConnectorTransientError: If connection fails (retryable)
        """
        if self._closed:
            raise ConnectorClosedError("Connector is closed")

        self._state = ConnectorState.CONNECTING

        # Create listenKey
        if self._listen_key_manager:
            self._listen_key = self._listen_key_manager.create()
        else:
            raise ConnectorNonRetryableError("No ListenKeyManager configured")

        # Connect WebSocket
        ws_url = f"{self._config.ws_base_url}/{self._listen_key}"
        try:
            await asyncio.wait_for(
                self._transport.connect(ws_url),
                timeout=self._config.timeout.connect_timeout_ms / 1000.0,
            )
        except TimeoutError as e:
            self._state = ConnectorState.DISCONNECTED
            raise ConnectorTransientError(f"WebSocket connection timeout: {e}") from e

        self._state = ConnectorState.CONNECTED
        logger.info(
            "ws_connected",
            extra={"listen_key_prefix": self._listen_key[:8] if self._listen_key else ""},
        )

        # Start keepalive loop
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def close(self) -> None:
        """Close the connection and clean up resources."""
        self._closed = True

        # Cancel keepalive task
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive_task

        # Close WebSocket
        with contextlib.suppress(Exception):
            await self._transport.close()

        # Close listenKey
        if self._listen_key and self._listen_key_manager:
            self._listen_key_manager.close(self._listen_key)

        self._state = ConnectorState.CLOSED
        logger.info("ws_closed")

    async def reconnect(self) -> None:
        """Reconnect after failure.

        1. Close old connection
        2. Create new listenKey
        3. Reconnect WebSocket
        """
        if self._closed:
            raise ConnectorClosedError("Connector is closed")

        self._state = ConnectorState.RECONNECTING
        self._stats.reconnects += 1

        # Cancel old keepalive
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive_task

        # Close old transport
        with contextlib.suppress(Exception):
            await self._transport.close()

        # Close old listenKey
        if self._listen_key and self._listen_key_manager:
            self._listen_key_manager.close(self._listen_key)
            self._listen_key = ""

        # Reconnect with backoff
        for attempt in range(self._config.retry.max_retries):
            try:
                await self.connect()
                logger.info(
                    "ws_reconnected",
                    extra={"attempt": attempt + 1},
                )
                return
            except Exception as e:
                delay_ms = self._config.retry.get_delay_ms(attempt)
                logger.warning(
                    "reconnect_failed",
                    extra={
                        "attempt": attempt + 1,
                        "max_retries": self._config.retry.max_retries,
                        "error": str(e),
                        "delay_ms": delay_ms,
                    },
                )
                await asyncio.sleep(delay_ms / 1000.0)

        self._state = ConnectorState.DISCONNECTED
        raise ConnectorTransientError("Max reconnect attempts exceeded")

    async def iter_events(self) -> AsyncIterator[UserDataEvent]:
        """Iterate over events from user-data stream.

        Yields:
            UserDataEvent for each message received

        Raises:
            ConnectorClosedError: If connector is not connected
        """
        if self._state != ConnectorState.CONNECTED:
            raise ConnectorClosedError(f"Not connected (state={self._state.value})")

        while not self._closed and self._transport.is_connected:
            try:
                raw_msg = await asyncio.wait_for(
                    self._transport.recv(),
                    timeout=self._config.timeout.read_timeout_ms / 1000.0,
                )
                self._stats.messages_received += 1

                # Parse message
                event = self._parse_event(raw_msg)
                if event is None:
                    continue

                # Update stats
                self._stats.last_message_ts = int(self._clock() * 1000)

                yield event

            except TimeoutError:
                # Read timeout - this is normal for user-data stream (sparse events)
                continue
            except ConnectorTransientError:
                logger.warning("ws_transient_error")
                await self.reconnect()
            except ConnectorClosedError:
                break
            except Exception as e:
                self._stats.errors += 1
                logger.error("ws_unexpected_error", extra={"error": str(e)})
                break

    def _parse_event(self, raw_msg: str) -> UserDataEvent | None:
        """Parse raw WebSocket message to UserDataEvent.

        Args:
            raw_msg: Raw JSON string from WebSocket

        Returns:
            UserDataEvent or None if message should be skipped
        """
        try:
            data = json.loads(raw_msg)

            # Skip subscription/pong messages
            if "result" in data or "id" in data:
                return None

            # Trigger immediate reconnect on listenKey expiry signal.
            if data.get("e") == "listenKeyExpired":
                logger.warning("listenKey_expired")
                raise ConnectorTransientError("listenKeyExpired")

            # Parse event
            event = UserDataEvent.from_binance(data, self._config.symbol_filter)

            # Debug: log ACCOUNT_UPDATE arrival for position truth investigation
            if data.get("e") == "ACCOUNT_UPDATE":
                p_symbols = [p.get("s") for p in data.get("a", {}).get("P", [])]
                logger.debug(
                    "USER_DATA_ACCOUNT_UPDATE_RAW symbols_in_P=%s filter=%s position_event=%s",
                    p_symbols,
                    self._config.symbol_filter,
                    event.position_event is not None,
                )

            # Update stats by event type
            if event.order_event is not None:
                self._stats.order_events += 1
            elif event.position_event is not None:
                self._stats.position_events += 1
            else:
                self._stats.unknown_events += 1
                event_type = str(data.get("e", ""))
                self._stats.unknown_event_types[event_type] = (
                    self._stats.unknown_event_types.get(event_type, 0) + 1
                )
                get_connector_metrics().record_user_data_unknown_event(event_type)

            return event

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(
                "parse_error",
                extra={"raw_msg_preview": raw_msg[:100], "error": str(e)},
            )
            self._stats.errors += 1
            return None

    async def _keepalive_loop(self) -> None:
        """Periodically send keepalive for listenKey."""
        while not self._closed and self._state == ConnectorState.CONNECTED:
            try:
                await asyncio.sleep(self._config.keepalive_interval_sec)

                if self._closed:
                    break

                if self._listen_key and self._listen_key_manager:
                    success = self._listen_key_manager.keepalive(self._listen_key)
                    if success:
                        self._stats.keepalive_count += 1
                    else:
                        logger.warning("keepalive_failed_will_reconnect")
                        # Trigger reconnect on next iteration
                        await self.reconnect()
                        return

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("keepalive_loop_error", extra={"error": str(e)})
                break

    async def __aenter__(self) -> FuturesUserDataWsConnector:
        """Context manager entry."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Context manager exit."""
        await self.close()


class FakeListenKeyManager:
    """Fake ListenKeyManager for testing.

    Returns configurable listenKey without making HTTP calls.
    """

    def __init__(
        self,
        listen_key: str = "test_listen_key_12345",
        create_fails: bool = False,
        keepalive_fails: bool = False,
    ) -> None:
        self.listen_key = listen_key
        self.create_fails = create_fails
        self.keepalive_fails = keepalive_fails
        self.create_count = 0
        self.keepalive_count = 0
        self.close_count = 0

    def create(self) -> str:
        """Create listenKey (fake)."""
        self.create_count += 1
        if self.create_fails:
            raise ConnectorTransientError("Fake create failure")
        return self.listen_key

    def keepalive(self, _listen_key: str) -> bool:
        """Keepalive (fake)."""
        self.keepalive_count += 1
        return not self.keepalive_fails

    def close(self, _listen_key: str) -> bool:
        """Close (fake)."""
        self.close_count += 1
        return True
