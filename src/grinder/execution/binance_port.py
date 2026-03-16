"""Binance Spot exchange port implementation.

Implements ExchangePort protocol for real order operations via Binance Spot API.

Key design decisions (see ADR-035, ADR-039):
- SafeMode.LIVE_TRADE required for any write operation (impossible by default)
- Mainnet requires explicit opt-in via allow_mainnet=True + strict guards (ADR-039)
- dry_run=True returns synthetic results WITHOUT calling http_client (0 HTTP calls)
- Injectable HttpClient for integration testing (NoopHttpClient still receives calls)
- Symbol whitelist enforcement
- Error mapping: Binance errors → Connector*Error types
- Integrates with H2/H3/H4 via IdempotentExchangePort wrapper

Mainnet guards (LC-08b, ADR-039):
- allow_mainnet=True required (default: False)
- ALLOW_MAINNET_TRADE=1 env var required
- symbol_whitelist MUST be non-empty
- max_notional_per_order MUST be set
- max_orders_per_run=1 default (single order per run)
- max_open_orders=1 default (single open order)

Testing modes:

1. True dry-run (0 HTTP calls):
    config = BinanceExchangePortConfig(
        mode=SafeMode.LIVE_TRADE,
        dry_run=True,  # ← 0 http_client calls, returns synthetic results
    )
    port = BinanceExchangePort(http_client=any_client, config=config)
    # place_order returns synthetic order_id, cancel_order returns True
    # fetch_open_orders returns [], replace_order returns synthetic order_id

2. Mock transport (with call recording):
    client = NoopHttpClient()
    config = BinanceExchangePortConfig(mode=SafeMode.LIVE_TRADE, dry_run=False)
    port = BinanceExchangePort(http_client=client, config=config)
    # Operations call http_client.request() but NoopHttpClient records + mocks

3. Real HTTP (production):
    client = AiohttpClient(api_key, api_secret)
    config = BinanceExchangePortConfig(
        mode=SafeMode.LIVE_TRADE,
        api_key=os.environ["BINANCE_API_KEY"],
        api_secret=os.environ["BINANCE_API_SECRET"],
        symbol_whitelist=["BTCUSDT", "ETHUSDT"],
    )
    port = BinanceExchangePort(http_client=client, config=config)

See: ADR-035 for design decisions
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from grinder.connectors.errors import (
    ConnectorNonRetryableError,
    ConnectorTransientError,
)
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide, OrderState
from grinder.execution.types import OrderRecord
from grinder.reconcile.identity import (
    OrderIdentityConfig,
    generate_client_order_id,
    get_default_identity_config,
)

if TYPE_CHECKING:
    from grinder.account.contracts import AccountSnapshot, PositionSnap

# --- HTTP Client Protocol ---


class HttpClient(Protocol):
    """Protocol for HTTP client operations.

    Injectable for testing - allows dry-run with 0 HTTP calls.
    """

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 5000,
        op: str = "",
    ) -> HttpResponse:
        """Execute HTTP request.

        Args:
            method: HTTP method (GET, POST, DELETE)
            url: Full URL
            params: Query parameters
            headers: HTTP headers
            timeout_ms: Timeout in milliseconds
            op: Operation name from ops taxonomy (Launch-05).
                Used by MeasuredSyncHttpClient for per-op deadlines/metrics.

        Returns:
            HttpResponse with status_code and json data

        Raises:
            ConnectorTimeoutError: On timeout
            ConnectorTransientError: On network/5xx errors
            ConnectorNonRetryableError: On 4xx/auth errors
        """
        ...


@dataclass(frozen=True)
class HttpResponse:
    """HTTP response data."""

    status_code: int
    json_data: dict[str, Any] | list[dict[str, Any]]


# --- Noop HTTP Client (for testing) ---


@dataclass
class NoopHttpClient:
    """HTTP client that makes 0 real HTTP calls.

    Returns configurable mock responses for testing.
    Tracks all "calls" for verification.
    """

    # Mock responses per operation
    place_response: dict[str, Any] = field(
        default_factory=lambda: {
            "orderId": 12345,
            "clientOrderId": "test_order",
            "status": "NEW",
            "executedQty": "0",
        }
    )
    cancel_response: dict[str, Any] = field(
        default_factory=lambda: {
            "orderId": 12345,
            "status": "CANCELED",
        }
    )
    open_orders_response: list[dict[str, Any]] = field(default_factory=list)
    positions_response: list[dict[str, Any]] = field(default_factory=list)
    listen_key_response: dict[str, Any] = field(
        default_factory=lambda: {"listenKey": "test_listen_key_12345"}
    )

    # Response control
    status_code: int = 200
    raise_exception: Exception | None = None

    # Call tracking
    calls: list[dict[str, Any]] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 5000,
        op: str = "",
    ) -> HttpResponse:
        """Record call and return mock response (0 real HTTP calls)."""
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "headers": headers,
                "timeout_ms": timeout_ms,
                "op": op,
            }
        )

        # Raise exception if configured (for error testing)
        if self.raise_exception is not None:
            raise self.raise_exception

        # Use configured status code
        status = self.status_code

        # Determine response based on endpoint
        if "listenKey" in url:
            return HttpResponse(status_code=status, json_data=self.listen_key_response)
        if "order" in url and method == "POST":
            return HttpResponse(status_code=status, json_data=self.place_response)
        if "order" in url and method == "DELETE":
            return HttpResponse(status_code=status, json_data=self.cancel_response)
        if "positionRisk" in url:
            return HttpResponse(status_code=status, json_data=self.positions_response)
        if "openOrders" in url:
            return HttpResponse(status_code=status, json_data=self.open_orders_response)

        # Default empty response
        return HttpResponse(status_code=status, json_data={})


# --- Error Mapping ---


def map_binance_error(status_code: int, json_data: Any) -> None:
    """Map Binance API error to Connector*Error.

    Binance error codes:
    - -1000 to -1099: General (often transient)
    - -1100 to -1199: Request issues (non-retryable)
    - -2010 to -2019: Order issues (non-retryable)

    Raises:
        ConnectorTransientError: 5xx, 429, -1000 series
        ConnectorNonRetryableError: 4xx, -1100/-2000 series
        ConnectorTimeoutError: Not raised here (handled by HTTP client)
    """
    if status_code >= 500:
        raise ConnectorTransientError(f"Binance server error ({status_code}): {json_data}")

    if status_code == 429:
        raise ConnectorTransientError(f"Binance rate limit: {json_data}")

    if status_code == 418:
        # IP banned - non-retryable
        raise ConnectorNonRetryableError(f"Binance IP banned: {json_data}")

    if status_code >= 400:
        # Extract error details if json_data is a dict
        code = 0
        msg = "Unknown error"
        if isinstance(json_data, dict):
            code = json_data.get("code", 0)
            msg = json_data.get("msg", "Unknown error")

        # -1000 series: often transient (WAF, overload)
        if -1099 <= code <= -1000:
            raise ConnectorTransientError(f"Binance transient error {code}: {msg}")

        # All other 4xx: non-retryable
        raise ConnectorNonRetryableError(f"Binance error {code}: {msg}")


# --- Binance Exchange Port ---


# Binance Testnet base URLs
BINANCE_SPOT_TESTNET_URL = "https://testnet.binance.vision"
BINANCE_SPOT_MAINNET_URL = "https://api.binance.com"


@dataclass
class BinanceExchangePortConfig:
    """Configuration for BinanceExchangePort.

    Attributes:
        mode: SafeMode (default: READ_ONLY - blocks all writes)
        base_url: API base URL (default: testnet)
        api_key: API key for authentication
        api_secret: API secret for signing
        symbol_whitelist: Allowed symbols (empty = all allowed for testnet, REQUIRED for mainnet)
        recv_window_ms: Binance recvWindow parameter (default: 5000)
        timeout_ms: Request timeout (default: 5000)
        dry_run: If True, return synthetic results WITHOUT calling http_client.
                 This is distinct from NoopHttpClient (which still receives calls).
                 dry_run=True guarantees 0 http_client.request() calls.

    Mainnet guards (ADR-039, LC-08b):
        allow_mainnet: Explicit opt-in for mainnet (default: False)
        max_notional_per_order: Maximum notional (price*qty) per order (REQUIRED for mainnet)
        max_orders_per_run: Maximum orders in single run (default: 1 for mainnet)
        max_open_orders: Maximum concurrent open orders (default: 1 for mainnet)

    To enable mainnet, ALL of these must be true:
    1. allow_mainnet=True
    2. ALLOW_MAINNET_TRADE=1 env var set
    3. symbol_whitelist is non-empty
    4. max_notional_per_order is set
    """

    mode: SafeMode = SafeMode.READ_ONLY
    base_url: str = BINANCE_SPOT_TESTNET_URL
    api_key: str = ""
    api_secret: str = ""
    symbol_whitelist: list[str] = field(default_factory=list)
    recv_window_ms: int = 5000
    timeout_ms: int = 5000
    dry_run: bool = False

    # Mainnet guards (ADR-039)
    allow_mainnet: bool = False
    max_notional_per_order: Decimal | None = None
    max_orders_per_run: int = 1
    max_open_orders: int = 1

    # Order identity (LC-12): configurable prefix and strategy
    identity_config: OrderIdentityConfig | None = None

    # Internal counter for order limit enforcement
    _orders_this_run: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        """Validate configuration."""
        is_mainnet = BINANCE_SPOT_MAINNET_URL in self.base_url or "api.binance.com" in self.base_url

        if is_mainnet:
            # Mainnet requires explicit opt-in + env var + guards
            if not self.allow_mainnet:
                raise ConnectorNonRetryableError(
                    "Mainnet requires allow_mainnet=True. "
                    "Set allow_mainnet=True in config to enable mainnet trading."
                )

            allow_env = os.environ.get("ALLOW_MAINNET_TRADE", "").lower() in (
                "1",
                "true",
                "yes",
            )
            if not allow_env:
                raise ConnectorNonRetryableError(
                    "Mainnet requires ALLOW_MAINNET_TRADE=1 environment variable. "
                    "This is a safety guard to prevent accidental mainnet trading."
                )

            if not self.symbol_whitelist:
                raise ConnectorNonRetryableError(
                    "Mainnet requires non-empty symbol_whitelist. "
                    "Specify allowed symbols (e.g., symbol_whitelist=['BTCUSDT'])."
                )

            if self.max_notional_per_order is None:
                raise ConnectorNonRetryableError(
                    "Mainnet requires max_notional_per_order to be set. "
                    "Specify maximum notional (price*qty) per order (e.g., Decimal('50'))."
                )

    def is_mainnet(self) -> bool:
        """Check if configured for mainnet."""
        return BINANCE_SPOT_MAINNET_URL in self.base_url or "api.binance.com" in self.base_url


@dataclass
class BinanceExchangePort:
    """Binance Spot exchange port implementing ExchangePort protocol.

    Thread-safety: No (use separate instances per thread or external locking)
    Determinism: No (depends on exchange state)

    IMPORTANT: Requires SafeMode.LIVE_TRADE for any write operation.
    Default SafeMode.READ_ONLY blocks all writes by design.

    Integration with H2/H3/H4:
    - Wrap with IdempotentExchangePort for idempotency (H3) + circuit breaker (H4)
    - H2 retries happen at IdempotentExchangePort level

    Example:
        # Create port with explicit LIVE_TRADE opt-in
        config = BinanceExchangePortConfig(
            mode=SafeMode.LIVE_TRADE,
            api_key=os.environ["BINANCE_API_KEY"],
            api_secret=os.environ["BINANCE_API_SECRET"],
            symbol_whitelist=["BTCUSDT", "ETHUSDT"],
        )
        raw_port = BinanceExchangePort(http_client, config)

        # Wrap with idempotency + circuit breaker
        store = InMemoryIdempotencyStore()
        breaker = CircuitBreaker(config)
        port = IdempotentExchangePort(raw_port, store, breaker=breaker)

        # Now use port.place_order() with H3/H4 guarantees
    """

    http_client: HttpClient
    config: BinanceExchangePortConfig = field(default_factory=BinanceExchangePortConfig)

    # Internal state
    _order_counter: int = field(default=0, repr=False)

    def _validate_mode(self, op: str) -> None:
        """Validate SafeMode allows write operations.

        Raises:
            ConnectorNonRetryableError: If mode is not LIVE_TRADE
        """
        if self.config.mode != SafeMode.LIVE_TRADE:
            raise ConnectorNonRetryableError(
                f"Cannot {op}: mode={self.config.mode.value}, requires LIVE_TRADE. "
                "Set mode=SafeMode.LIVE_TRADE to enable trading."
            )

    def _validate_symbol(self, symbol: str) -> None:
        """Validate symbol is in whitelist.

        Raises:
            ConnectorNonRetryableError: If symbol not in whitelist
        """
        if self.config.symbol_whitelist and symbol not in self.config.symbol_whitelist:
            raise ConnectorNonRetryableError(
                f"Symbol '{symbol}' not in whitelist: {self.config.symbol_whitelist}"
            )

    def _validate_notional(self, price: Decimal, quantity: Decimal) -> None:
        """Validate order notional is within limits (mainnet guard).

        Raises:
            ConnectorNonRetryableError: If notional exceeds max_notional_per_order
        """
        if self.config.max_notional_per_order is not None:
            notional = price * quantity
            if notional > self.config.max_notional_per_order:
                raise ConnectorNonRetryableError(
                    f"Order notional ${notional:.2f} exceeds max_notional_per_order "
                    f"${self.config.max_notional_per_order:.2f}. "
                    "Reduce price or quantity."
                )

    def _validate_order_count(self) -> None:
        """Validate order count is within limits (mainnet guard).

        Raises:
            ConnectorNonRetryableError: If order count exceeds max_orders_per_run
        """
        if self.config._orders_this_run >= self.config.max_orders_per_run:
            raise ConnectorNonRetryableError(
                f"Order count limit reached: {self.config.max_orders_per_run} orders per run. "
                "Reset port or create new instance to place more orders."
            )

    def _sign_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Add timestamp and signature to request params."""
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.config.recv_window_ms

        query_string = urllib.parse.urlencode(params)
        signature = hmac.new(
            self.config.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        params["signature"] = signature
        return params

    def _get_headers(self) -> dict[str, str]:
        """Get authenticated request headers."""
        return {"X-MBX-APIKEY": self.config.api_key}

    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
        level_id: int,
        ts: int,
        reduce_only: bool = False,  # noqa: ARG002 - spot has no reduceOnly
        client_order_id: str | None = None,
    ) -> str:
        """Place a limit order on Binance.

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
            side: BUY or SELL
            price: Limit price
            quantity: Order quantity
            level_id: Grid level identifier (used for client order ID)
            ts: Current timestamp
            reduce_only: Ignored for spot (no reduceOnly in spot API)
            client_order_id: Pre-generated clientOrderId override (PR-INV-3)

        Returns:
            order_id: Binance order ID as string

        Raises:
            ConnectorNonRetryableError: Mode violation, symbol not allowed, or 4xx error
            ConnectorTransientError: Network/5xx/rate limit
            ConnectorTimeoutError: Request timeout
        """
        self._validate_mode("place_order")
        self._validate_symbol(symbol)
        self._validate_notional(price, quantity)
        self._validate_order_count()

        # Generate deterministic client order ID (LC-12: configurable identity)
        # If client_order_id override provided (e.g., TP orders), skip generation.
        self._order_counter += 1
        if client_order_id is None:
            identity = self.config.identity_config or get_default_identity_config()
            client_order_id = generate_client_order_id(
                config=identity,
                symbol=symbol,
                level_id=level_id,
                ts=ts,
                seq=self._order_counter,
            )

        # Track order count (even for dry-run to maintain consistency)
        # Use object.__setattr__ since config is a dataclass
        object.__setattr__(self.config, "_orders_this_run", self.config._orders_this_run + 1)

        # DRY-RUN: Return synthetic order_id WITHOUT calling http_client
        if self.config.dry_run:
            return client_order_id

        params = {
            "symbol": symbol,
            "side": side.value.upper(),
            "type": "LIMIT",
            "timeInForce": "GTC",
            "price": str(price),
            "quantity": str(quantity),
            "newClientOrderId": client_order_id,
        }
        params = self._sign_request(params)

        url = f"{self.config.base_url}/api/v3/order"
        response = self.http_client.request(
            method="POST",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        # Response is always a dict for order operations
        if isinstance(response.json_data, dict):
            return str(response.json_data.get("orderId", client_order_id))
        return client_order_id

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order on Binance.

        Args:
            order_id: Binance order ID or client order ID

        Returns:
            True if cancellation succeeded

        Raises:
            ConnectorNonRetryableError: Mode violation or order not found
            ConnectorTransientError: Network/5xx/rate limit
            ConnectorTimeoutError: Request timeout
        """
        self._validate_mode("cancel_order")

        # For cancel, we need symbol - extract from order_id if it's our format
        # Otherwise, we need to query first (not implemented in v0.1)
        # For now, assume order_id contains symbol prefix: "grinder_{symbol}_..."
        parts = order_id.split("_")
        symbol = parts[1] if len(parts) > 1 and parts[0] == "grinder" else ""

        if not symbol:
            raise ConnectorNonRetryableError(
                f"Cannot determine symbol from order_id '{order_id}'. "
                "In v0.1, only orders placed via BinanceExchangePort can be cancelled."
            )

        self._validate_symbol(symbol)

        # DRY-RUN: Return True (success) WITHOUT calling http_client
        if self.config.dry_run:
            return True

        params = {
            "symbol": symbol,
            "origClientOrderId": order_id,
        }
        params = self._sign_request(params)

        url = f"{self.config.base_url}/api/v3/order"
        response = self.http_client.request(
            method="DELETE",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        # Response is always a dict for cancel operations
        if isinstance(response.json_data, dict):
            return response.json_data.get("status") == "CANCELED"
        return False

    def replace_order(
        self,
        order_id: str,
        new_price: Decimal,
        new_quantity: Decimal,
        ts: int,
    ) -> str:
        """Replace an order (cancel + new).

        IMPORTANT: Uses H3 idempotency via IdempotentExchangePort wrapper.
        Without idempotency, cancel+place is not safe under retries.

        Args:
            order_id: Order to replace
            new_price: New limit price
            new_quantity: New quantity
            ts: Current timestamp

        Returns:
            new_order_id: ID of the replacement order

        Raises:
            ConnectorNonRetryableError: Mode violation or original order not found
            ConnectorTransientError: Network/5xx/rate limit
            ConnectorTimeoutError: Request timeout
        """
        self._validate_mode("replace_order")

        # Extract symbol and level_id from order_id
        parts = order_id.split("_")
        if len(parts) < 3 or parts[0] != "grinder":
            raise ConnectorNonRetryableError(
                f"Cannot parse order_id '{order_id}'. "
                "In v0.1, only orders placed via BinanceExchangePort can be replaced."
            )

        symbol = parts[1]
        level_id = int(parts[2])

        # Determine side from original order (would need query in production)
        # For v0.1, assume BUY (this is a limitation)
        side = OrderSide.BUY

        # Cancel old order (may fail if already filled - that's expected)
        with contextlib.suppress(ConnectorNonRetryableError):
            self.cancel_order(order_id)

        # Place new order
        return self.place_order(
            symbol=symbol,
            side=side,
            price=new_price,
            quantity=new_quantity,
            level_id=level_id,
            ts=ts,
        )

    def fetch_open_orders(self, symbol: str) -> list[OrderRecord]:
        """Fetch all open orders for a symbol.

        Note: This is a read-only operation, allowed in all modes.

        Args:
            symbol: Trading symbol

        Returns:
            List of open order records
        """
        # Validate symbol even for reads (consistent behavior)
        self._validate_symbol(symbol)

        # DRY-RUN: Return empty list WITHOUT calling http_client
        if self.config.dry_run:
            return []

        params = {"symbol": symbol}
        params = self._sign_request(params)

        url = f"{self.config.base_url}/api/v3/openOrders"
        response = self.http_client.request(
            method="GET",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        orders: list[OrderRecord] = []
        order_list: list[dict[str, Any]] = (
            response.json_data if isinstance(response.json_data, list) else []
        )
        for order_data in order_list:
            side = OrderSide.BUY if order_data.get("side") == "BUY" else OrderSide.SELL
            state = OrderState.OPEN
            if order_data.get("status") == "PARTIALLY_FILLED":
                state = OrderState.PARTIALLY_FILLED

            orders.append(
                OrderRecord(
                    order_id=order_data.get("clientOrderId", str(order_data.get("orderId"))),
                    symbol=symbol,
                    side=side,
                    price=Decimal(order_data.get("price", "0")),
                    quantity=Decimal(order_data.get("origQty", "0")),
                    state=state,
                    level_id=0,  # Not available from Binance
                    created_ts=order_data.get("time", 0),
                )
            )

        return orders

    def fetch_positions(self) -> list[PositionSnap]:
        """Fetch positions from Binance (Launch-15 stub -- not yet implemented)."""
        msg = "BinanceExchangePort.fetch_positions() not yet implemented (Launch-15 PR3+)"
        raise NotImplementedError(msg)

    def fetch_account_snapshot(self) -> AccountSnapshot:
        """Fetch account snapshot from Binance (Launch-15 stub -- not yet implemented)."""
        msg = "BinanceExchangePort.fetch_account_snapshot() not yet implemented (Launch-15 PR3+)"
        raise NotImplementedError(msg)

    def debug_get_order_status(
        self,
        *,
        symbol: str,  # noqa: ARG002
        client_order_id: str,  # noqa: ARG002
    ) -> dict[str, str] | None:
        """Debug-only: not implemented for spot port."""
        return None

    def orders_remaining(self) -> int | None:
        """Remaining order budget (PR-P0-RACE-1)."""
        return self.config.max_orders_per_run - self.config._orders_this_run

    def reset(self) -> None:
        """Reset internal state (for testing and new runs)."""
        self._order_counter = 0
        # Reset order count limit (use object.__setattr__ for dataclass)
        object.__setattr__(self.config, "_orders_this_run", 0)
