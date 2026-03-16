"""Binance Futures USDT-M exchange port implementation.

Implements ExchangePort protocol for real order operations via Binance Futures API.

Key design decisions (see ADR-040):
- SafeMode.LIVE_TRADE required for any write operation (impossible by default)
- Mainnet requires explicit opt-in via allow_mainnet=True + strict guards
- dry_run=True returns synthetic results WITHOUT calling http_client (0 HTTP calls)
- Injectable HttpClient for integration testing
- Symbol whitelist enforcement
- Error mapping: Binance errors → Connector*Error types

Futures-specific safety (LC-08b-F, ADR-040):
- leverage enforcement (default: 1x)
- Position cleanup on fill (reduceOnly close)
- marginType logging (ISOLATED recommended)
- positionMode logging

Testing modes:
1. True dry-run (0 HTTP calls):
    config = BinanceFuturesPortConfig(
        mode=SafeMode.LIVE_TRADE,
        dry_run=True,  # ← 0 http_client calls, returns synthetic results
    )
    port = BinanceFuturesPort(http_client=any_client, config=config)

2. Real HTTP (production):
    config = BinanceFuturesPortConfig(
        mode=SafeMode.LIVE_TRADE,
        api_key=os.environ["BINANCE_API_KEY"],
        api_secret=os.environ["BINANCE_API_SECRET"],
        symbol_whitelist=["BTCUSDT"],
        allow_mainnet=True,
        max_notional_per_order=Decimal("50"),
    )
    port = BinanceFuturesPort(http_client=client, config=config)

See: ADR-040 for design decisions
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from grinder.account.contracts import (
    AccountSnapshot,
    OpenOrderSnap,
    PositionSnap,
    build_account_snapshot,
)
from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide, OrderState
from grinder.env_parse import parse_bool
from grinder.execution.binance_port import HttpClient, map_binance_error
from grinder.execution.port_metrics import get_port_metrics
from grinder.execution.types import OrderRecord
from grinder.net.retry_policy import (
    OP_CANCEL_ALL,
    OP_CANCEL_ORDER,
    OP_GET_ACCOUNT,
    OP_GET_OPEN_ORDERS,
    OP_GET_ORDER_STATUS,
    OP_GET_POSITIONS,
    OP_GET_USER_TRADES,
    OP_PLACE_ORDER,
)
from grinder.reconcile.identity import (
    OrderIdentityConfig,
    generate_client_order_id,
    get_default_identity_config,
    parse_client_order_id,
)

logger = logging.getLogger(__name__)

# --- Binance Futures URLs ---

BINANCE_FUTURES_TESTNET_URL = "https://testnet.binancefuture.com"
BINANCE_FUTURES_MAINNET_URL = "https://fapi.binance.com"


def _parse_reduce_only(val: Any) -> bool:
    """Parse reduceOnly from Binance (may be bool or string ``"false"``)."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1")
    return bool(val)


@dataclass
class BinanceFuturesPortConfig:
    """Configuration for BinanceFuturesPort (USDT-M).

    Attributes:
        mode: SafeMode (default: READ_ONLY - blocks all writes)
        base_url: API base URL (default: futures testnet)
        api_key: API key for authentication
        api_secret: API secret for signing
        symbol_whitelist: Allowed symbols (REQUIRED for mainnet)
        recv_window_ms: Binance recvWindow parameter (default: 5000)
        timeout_ms: Request timeout (default: 5000)
        dry_run: If True, return synthetic results WITHOUT calling http_client.

    Mainnet guards (ADR-040, LC-08b-F):
        allow_mainnet: Explicit opt-in for mainnet (default: False)
        max_notional_per_order: Maximum notional (price*qty) per order (REQUIRED for mainnet)
        max_orders_per_run: Maximum orders in single run (default: 1)
        max_open_orders: Maximum concurrent open orders (default: 1)
        target_leverage: Leverage to set/enforce (default: 1)

    To enable mainnet, ALL of these must be true:
    1. allow_mainnet=True
    2. ALLOW_MAINNET_TRADE=1 env var set
    3. symbol_whitelist is non-empty
    4. max_notional_per_order is set
    """

    mode: SafeMode = SafeMode.READ_ONLY
    base_url: str = BINANCE_FUTURES_TESTNET_URL
    api_key: str = ""
    api_secret: str = ""
    symbol_whitelist: list[str] = field(default_factory=list)
    recv_window_ms: int = 5000
    timeout_ms: int = 5000
    dry_run: bool = False

    # Mainnet guards (ADR-040)
    allow_mainnet: bool = False
    max_notional_per_order: Decimal | None = None
    max_orders_per_run: int = 1
    max_open_orders: int = 1
    target_leverage: int = 1

    # Order identity (LC-12): configurable prefix and strategy
    identity_config: OrderIdentityConfig | None = None

    # Internal counter for order limit enforcement
    _orders_this_run: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        """Validate configuration."""
        is_mainnet = self.is_mainnet()

        if is_mainnet:
            # Mainnet requires explicit opt-in + env var + guards
            if not self.allow_mainnet:
                raise ConnectorNonRetryableError(
                    "Futures mainnet requires allow_mainnet=True. "
                    "Set allow_mainnet=True in config to enable mainnet trading."
                )

            allow_env = os.environ.get("ALLOW_MAINNET_TRADE", "").lower() in (
                "1",
                "true",
                "yes",
            )
            if not allow_env:
                raise ConnectorNonRetryableError(
                    "Futures mainnet requires ALLOW_MAINNET_TRADE=1 environment variable. "
                    "This is a safety guard to prevent accidental mainnet trading."
                )

            if not self.symbol_whitelist:
                raise ConnectorNonRetryableError(
                    "Futures mainnet requires non-empty symbol_whitelist. "
                    "Specify allowed symbols (e.g., symbol_whitelist=['BTCUSDT'])."
                )

            if self.max_notional_per_order is None:
                raise ConnectorNonRetryableError(
                    "Futures mainnet requires max_notional_per_order to be set. "
                    "Specify maximum notional (price*qty) per order (e.g., Decimal('50'))."
                )

    def is_mainnet(self) -> bool:
        """Check if configured for mainnet."""
        return BINANCE_FUTURES_MAINNET_URL in self.base_url or "fapi.binance.com" in self.base_url


@dataclass
class FuturesAccountInfo:
    """Futures account information snapshot."""

    total_balance_usdt: Decimal
    available_balance_usdt: Decimal
    total_unrealized_pnl: Decimal
    margin_balance: Decimal
    position_mode: str  # "true" = hedge mode, "false" = one-way mode


@dataclass
class FuturesPositionInfo:
    """Futures position information."""

    symbol: str
    position_amt: Decimal  # Positive = long, negative = short
    entry_price: Decimal
    unrealized_pnl: Decimal
    leverage: int
    margin_type: str  # "isolated" or "cross"
    position_side: str  # "BOTH", "LONG", "SHORT"


@dataclass
class BinanceFuturesPort:
    """Binance Futures USDT-M exchange port implementing ExchangePort protocol.

    Thread-safety: No (use separate instances per thread or external locking)
    Determinism: No (depends on exchange state)

    IMPORTANT: Requires SafeMode.LIVE_TRADE for any write operation.
    Default SafeMode.READ_ONLY blocks all writes by design.
    """

    _PORT_NAME = "futures"

    http_client: HttpClient
    config: BinanceFuturesPortConfig = field(default_factory=BinanceFuturesPortConfig)

    # Internal state
    _order_counter: int = field(default=0, repr=False)
    _position_mode: str | None = field(default=None, repr=False)
    _leverage_set: dict[str, int] = field(default_factory=dict, repr=False)
    _ts_offset_ms: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        """Read debug flags once at init."""
        self._debug_open_orders = parse_bool(
            "GRINDER_ACCOUNT_SYNC_DEBUG_OPEN_ORDERS", default=False, strict=False
        )

    def _validate_mode(self, op: str) -> None:
        """Validate SafeMode allows write operations."""
        if self.config.mode != SafeMode.LIVE_TRADE:
            raise ConnectorNonRetryableError(
                f"Cannot {op}: mode={self.config.mode.value}, requires LIVE_TRADE. "
                "Set mode=SafeMode.LIVE_TRADE to enable trading."
            )

    def _validate_symbol(self, symbol: str) -> None:
        """Validate symbol is in whitelist."""
        if self.config.symbol_whitelist and symbol not in self.config.symbol_whitelist:
            raise ConnectorNonRetryableError(
                f"Symbol '{symbol}' not in whitelist: {self.config.symbol_whitelist}"
            )

    def _validate_notional(self, price: Decimal, quantity: Decimal) -> None:
        """Validate order notional is within limits (mainnet guard)."""
        if self.config.max_notional_per_order is not None:
            notional = price * quantity
            if notional > self.config.max_notional_per_order:
                raise ConnectorNonRetryableError(
                    f"Order notional ${notional:.2f} exceeds max_notional_per_order "
                    f"${self.config.max_notional_per_order:.2f}. "
                    "Reduce price or quantity."
                )

    def _validate_order_count(self) -> None:
        """Validate order count is within limits (mainnet guard)."""
        if self.config._orders_this_run >= self.config.max_orders_per_run:
            raise ConnectorNonRetryableError(
                f"Order count limit reached: {self.config.max_orders_per_run} orders per run. "
                "Reset port or create new instance to place more orders."
            )

    def _sign_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Add timestamp and signature to request params."""
        params["timestamp"] = int(time.time() * 1000) - self._ts_offset_ms
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

    # --- Account & Position Info ---

    def get_position_mode(self) -> str:
        """Get current position mode (hedge vs one-way).

        Returns:
            "hedge" if hedge mode (LONG/SHORT positions)
            "one-way" if one-way mode (BOTH position)
        """
        if self.config.dry_run:
            return "one-way"

        params = self._sign_request({})
        url = f"{self.config.base_url}/fapi/v1/positionSide/dual"
        response = self.http_client.request(
            method="GET",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_GET_ACCOUNT,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        if isinstance(response.json_data, dict):
            dual_side = response.json_data.get("dualSidePosition", False)
            self._position_mode = "hedge" if dual_side else "one-way"
            return self._position_mode

        return "one-way"

    def get_leverage(self, symbol: str) -> int:
        """Get current leverage for symbol."""
        self._validate_symbol(symbol)

        if self.config.dry_run:
            return self.config.target_leverage

        # Leverage is in position risk endpoint
        positions = self.get_positions(symbol)
        if positions:
            return positions[0].leverage
        return self.config.target_leverage

    def set_leverage(self, symbol: str, leverage: int) -> int:
        """Set leverage for symbol.

        Args:
            symbol: Trading symbol
            leverage: Target leverage (1-125)

        Returns:
            Actual leverage set
        """
        self._validate_mode("set_leverage")
        self._validate_symbol(symbol)

        if leverage < 1 or leverage > 125:
            raise ConnectorNonRetryableError(f"Leverage must be 1-125, got {leverage}")

        if self.config.dry_run:
            self._leverage_set[symbol] = leverage
            return leverage

        params = {
            "symbol": symbol,
            "leverage": leverage,
        }
        params = self._sign_request(params)

        url = f"{self.config.base_url}/fapi/v1/leverage"
        response = self.http_client.request(
            method="POST",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_PLACE_ORDER,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        if isinstance(response.json_data, dict):
            actual = response.json_data.get("leverage", leverage)
            self._leverage_set[symbol] = int(actual)
            return int(actual)

        return leverage

    def get_positions(self, symbol: str) -> list[FuturesPositionInfo]:
        """Get position information for symbol."""
        self._validate_symbol(symbol)

        if self.config.dry_run:
            return []

        params = {"symbol": symbol}
        params = self._sign_request(params)

        url = f"{self.config.base_url}/fapi/v2/positionRisk"
        response = self.http_client.request(
            method="GET",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_GET_POSITIONS,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        positions: list[FuturesPositionInfo] = []
        pos_list: list[dict[str, Any]] = (
            response.json_data if isinstance(response.json_data, list) else []
        )

        for pos_data in pos_list:
            pos_amt = Decimal(pos_data.get("positionAmt", "0"))
            # Only include positions with non-zero amount
            if pos_amt != 0:
                positions.append(
                    FuturesPositionInfo(
                        symbol=pos_data.get("symbol", symbol),
                        position_amt=pos_amt,
                        entry_price=Decimal(pos_data.get("entryPrice", "0")),
                        unrealized_pnl=Decimal(pos_data.get("unRealizedProfit", "0")),
                        leverage=int(pos_data.get("leverage", 1)),
                        margin_type=pos_data.get("marginType", "cross").lower(),
                        position_side=pos_data.get("positionSide", "BOTH"),
                    )
                )

        return positions

    def fetch_positions_raw(self, symbol: str) -> list[dict[str, Any]]:
        """Fetch raw position data from Binance (for reconcile observed state).

        Args:
            symbol: Trading symbol to fetch

        Returns:
            List of raw position dicts from Binance REST response
        """
        self._validate_symbol(symbol)

        if self.config.dry_run:
            return []

        params = {"symbol": symbol}
        params = self._sign_request(params)

        url = f"{self.config.base_url}/fapi/v2/positionRisk"
        response = self.http_client.request(
            method="GET",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_GET_POSITIONS,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        if isinstance(response.json_data, list):
            return response.json_data
        return []

    def fetch_account_snapshot(self) -> AccountSnapshot:
        """Fetch full account snapshot (positions + open orders).

        Makes exactly 2 REST calls:
          GET /fapi/v2/positionRisk (no symbol filter -> all positions)
          GET /fapi/v1/openOrders   (no symbol filter -> all open orders)

        Returns AccountSnapshot with canonical ordering.
        dry_run=True -> empty snapshot, 0 HTTP calls.
        """
        if self.config.dry_run:
            return build_account_snapshot([], [], source="dry_run")

        # --- Positions (all symbols) ---
        pos_params = self._sign_request({})
        pos_url = f"{self.config.base_url}/fapi/v2/positionRisk"
        pos_resp = self.http_client.request(
            method="GET",
            url=pos_url,
            params=pos_params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_GET_POSITIONS,
        )
        if pos_resp.status_code != 200:
            map_binance_error(pos_resp.status_code, pos_resp.json_data)
        raw_positions: list[dict[str, Any]] = (
            pos_resp.json_data if isinstance(pos_resp.json_data, list) else []
        )

        # --- Open orders (all symbols) ---
        ord_params = self._sign_request({})
        ord_url = f"{self.config.base_url}/fapi/v1/openOrders"
        ord_resp = self.http_client.request(
            method="GET",
            url=ord_url,
            params=ord_params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_GET_OPEN_ORDERS,
        )
        if ord_resp.status_code != 200:
            map_binance_error(ord_resp.status_code, ord_resp.json_data)
        raw_orders: list[dict[str, Any]] = (
            ord_resp.json_data if isinstance(ord_resp.json_data, list) else []
        )

        # P0-2: debug openOrders snapshot (bounded, env-gated)
        if self._debug_open_orders:
            symbols_sample = sorted({o.get("symbol", "?") for o in raw_orders[:20]})
            logger.warning(
                "ACCOUNT_SYNC_DEBUG raw_orders: count=%d symbols=%s",
                len(raw_orders),
                symbols_sample,
            )
            for i, o in enumerate(raw_orders[:10]):  # bounded: first 10
                logger.warning(
                    "  [%d] clientOrderId=%s orderId=%s symbol=%s side=%s "
                    "price=%s qty=%s status=%s type=%s timeInForce=%s reduceOnly=%s",
                    i,
                    o.get("clientOrderId", "?"),
                    o.get("orderId", "?"),
                    o.get("symbol", "?"),
                    o.get("side", "?"),
                    o.get("price", "?"),
                    o.get("origQty", "?"),
                    o.get("status", "?"),
                    o.get("type", "?"),
                    o.get("timeInForce", "?"),
                    o.get("reduceOnly", "?"),
                )

        # --- Parse positions -> PositionSnap ---
        positions: list[PositionSnap] = []
        for p in raw_positions:
            qty = abs(Decimal(str(p.get("positionAmt", "0"))))
            if qty == 0:
                continue
            positions.append(
                PositionSnap(
                    symbol=p.get("symbol", ""),
                    side=p.get("positionSide", "BOTH"),
                    qty=qty,
                    entry_price=Decimal(str(p.get("entryPrice", "0"))),
                    mark_price=Decimal(str(p.get("markPrice", "0"))),
                    unrealized_pnl=Decimal(str(p.get("unRealizedProfit", "0"))),
                    leverage=int(p.get("leverage", 1)),
                    ts=int(p.get("updateTime", 0)),
                )
            )

        # --- Parse orders -> OpenOrderSnap ---
        open_orders: list[OpenOrderSnap] = []
        for o in raw_orders:
            order_id = o.get("clientOrderId") or str(o.get("orderId", ""))
            open_orders.append(
                OpenOrderSnap(
                    order_id=order_id,
                    symbol=o.get("symbol", ""),
                    side=o.get("side", ""),
                    order_type=o.get("type", ""),
                    price=Decimal(str(o.get("price", "0"))),
                    qty=Decimal(str(o.get("origQty", "0"))),
                    filled_qty=Decimal(str(o.get("executedQty", "0"))),
                    reduce_only=_parse_reduce_only(o.get("reduceOnly", False)),
                    status=o.get("status", ""),
                    ts=int(o.get("updateTime", 0)),
                )
            )

        return build_account_snapshot(positions, open_orders, source="exchange")

    def get_account_info(self) -> FuturesAccountInfo:
        """Get futures account information."""
        if self.config.dry_run:
            return FuturesAccountInfo(
                total_balance_usdt=Decimal("1000"),
                available_balance_usdt=Decimal("1000"),
                total_unrealized_pnl=Decimal("0"),
                margin_balance=Decimal("1000"),
                position_mode="one-way",
            )

        params = self._sign_request({})
        url = f"{self.config.base_url}/fapi/v2/account"
        response = self.http_client.request(
            method="GET",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_GET_ACCOUNT,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        if isinstance(response.json_data, dict):
            data = response.json_data
            return FuturesAccountInfo(
                total_balance_usdt=Decimal(data.get("totalWalletBalance", "0")),
                available_balance_usdt=Decimal(data.get("availableBalance", "0")),
                total_unrealized_pnl=Decimal(data.get("totalUnrealizedProfit", "0")),
                margin_balance=Decimal(data.get("totalMarginBalance", "0")),
                position_mode="hedge" if data.get("dualSidePosition") else "one-way",
            )

        return FuturesAccountInfo(
            total_balance_usdt=Decimal("0"),
            available_balance_usdt=Decimal("0"),
            total_unrealized_pnl=Decimal("0"),
            margin_balance=Decimal("0"),
            position_mode="one-way",
        )

    # --- Order Operations ---

    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
        level_id: int,
        ts: int,
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> str:
        """Place a limit order on Binance Futures.

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
            side: BUY or SELL
            price: Limit price
            quantity: Order quantity
            level_id: Grid level identifier
            ts: Current timestamp
            reduce_only: If True, only reduce position (for cleanup)
            client_order_id: Pre-generated clientOrderId override (PR-INV-3)

        Returns:
            order_id: Binance order ID as string
        """
        get_port_metrics().record_order_attempt(self._PORT_NAME, "place")
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

        # Track order count
        object.__setattr__(self.config, "_orders_this_run", self.config._orders_this_run + 1)

        # DRY-RUN: Return synthetic order_id WITHOUT calling http_client
        if self.config.dry_run:
            return client_order_id

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.value.upper(),
            "type": "LIMIT",
            "timeInForce": "GTC",
            "price": str(price),
            "quantity": str(quantity),
            "newClientOrderId": client_order_id,
        }

        # One-way mode: use reduceOnly flag
        # Hedge mode: would need positionSide (not supported in v0.1)
        if reduce_only:
            params["reduceOnly"] = "true"

        params = self._sign_request(params)

        url = f"{self.config.base_url}/fapi/v1/order"
        response = self.http_client.request(
            method="POST",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_PLACE_ORDER,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        if isinstance(response.json_data, dict):
            # Return clientOrderId (our ID) for internal tracking, not orderId (Binance numeric)
            return str(response.json_data.get("clientOrderId", client_order_id))
        return client_order_id

    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        reduce_only: bool = False,
    ) -> str:
        """Place a market order on Binance Futures (for position cleanup).

        Args:
            symbol: Trading symbol
            side: BUY or SELL
            quantity: Order quantity
            reduce_only: If True, only reduce position

        Returns:
            order_id: Binance order ID as string
        """
        self._validate_mode("place_market_order")
        self._validate_symbol(symbol)

        # Generate client order ID (LC-12: configurable identity)
        self._order_counter += 1
        ts = int(time.time() * 1000)
        identity = self.config.identity_config or get_default_identity_config()
        client_order_id = generate_client_order_id(
            config=identity,
            symbol=symbol,
            level_id="c",  # Short for "cleanup" to fit 36-char Binance limit
            ts=ts,
            seq=self._order_counter,
        )

        if self.config.dry_run:
            return client_order_id

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.value.upper(),
            "type": "MARKET",
            "quantity": str(quantity),
            "newClientOrderId": client_order_id,
        }

        if reduce_only:
            params["reduceOnly"] = "true"

        params = self._sign_request(params)

        url = f"{self.config.base_url}/fapi/v1/order"
        response = self.http_client.request(
            method="POST",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_PLACE_ORDER,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        if isinstance(response.json_data, dict):
            # Return clientOrderId (our ID) for internal tracking, not orderId (Binance numeric)
            return str(response.json_data.get("clientOrderId", client_order_id))
        return client_order_id

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order on Binance Futures.

        Args:
            order_id: Binance order ID or client order ID

        Returns:
            True if cancellation succeeded
        """
        get_port_metrics().record_order_attempt(self._PORT_NAME, "cancel")
        self._validate_mode("cancel_order")

        # LC-12: Use proper identity parsing to extract symbol
        # Supports both v1 format (grinder_{strategy}_{symbol}_...) and legacy (grinder_{symbol}_...)
        parsed = parse_client_order_id(order_id)
        if parsed is None:
            raise ConnectorNonRetryableError(
                f"Cannot parse order_id '{order_id}'. "
                "In v0.1, only orders placed via BinanceFuturesPort can be cancelled."
            )
        symbol = parsed.symbol

        self._validate_symbol(symbol)

        if self.config.dry_run:
            return True

        params = {
            "symbol": symbol,
            "origClientOrderId": order_id,
        }
        params = self._sign_request(params)

        url = f"{self.config.base_url}/fapi/v1/order"
        response = self.http_client.request(
            method="DELETE",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_CANCEL_ORDER,
        )

        if response.status_code != 200:
            # P0-2: diagnostic for -2011 (unknown order)
            error_code = None
            if isinstance(response.json_data, dict):
                error_code = response.json_data.get("code")
            if error_code == -2011:
                logger.warning(
                    "CANCEL_2011_DIAG order_id=%s parsed=%s",
                    order_id,
                    parse_client_order_id(order_id),
                )
                get_port_metrics().record_cancel_unknown(self._PORT_NAME)
            map_binance_error(response.status_code, response.json_data)

        if isinstance(response.json_data, dict):
            canceled = response.json_data.get("status") == "CANCELED"
            if canceled:
                get_port_metrics().record_cancel_ok(self._PORT_NAME)
                if self._debug_open_orders:
                    logger.warning("CANCEL_OK order_id=%s", order_id)
            return canceled
        return False

    def cancel_order_by_binance_id(self, symbol: str, order_id: int) -> bool:
        """Cancel an order by Binance numeric order ID.

        Args:
            symbol: Trading symbol
            order_id: Binance numeric order ID

        Returns:
            True if cancellation succeeded
        """
        self._validate_mode("cancel_order_by_binance_id")
        self._validate_symbol(symbol)

        if self.config.dry_run:
            return True

        params = {
            "symbol": symbol,
            "orderId": order_id,
        }
        params = self._sign_request(params)

        url = f"{self.config.base_url}/fapi/v1/order"
        response = self.http_client.request(
            method="DELETE",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_CANCEL_ORDER,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        if isinstance(response.json_data, dict):
            return response.json_data.get("status") == "CANCELED"
        return False

    def cancel_all_orders(self, symbol: str) -> int:
        """Cancel all open orders for symbol.

        Args:
            symbol: Trading symbol

        Returns:
            Number of orders cancelled
        """
        self._validate_mode("cancel_all_orders")
        self._validate_symbol(symbol)

        if self.config.dry_run:
            return 0

        params = {"symbol": symbol}
        params = self._sign_request(params)

        url = f"{self.config.base_url}/fapi/v1/allOpenOrders"
        response = self.http_client.request(
            method="DELETE",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_CANCEL_ALL,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        # Returns {"code": 200, "msg": "The operation of cancel all open order is done."}
        if isinstance(response.json_data, dict):
            return 1 if response.json_data.get("code", 0) == 200 else 0
        return 0

    def replace_order(
        self,
        order_id: str,
        new_price: Decimal,
        new_quantity: Decimal,
        ts: int,
    ) -> str:
        """Replace an order (cancel + new)."""
        get_port_metrics().record_order_attempt(self._PORT_NAME, "replace")
        self._validate_mode("replace_order")

        parts = order_id.split("_")
        if len(parts) < 3 or parts[0] != "grinder":
            raise ConnectorNonRetryableError(
                f"Cannot parse order_id '{order_id}'. "
                "In v0.1, only orders placed via BinanceFuturesPort can be replaced."
            )

        symbol = parts[1]
        level_id = int(parts[2])
        side = OrderSide.BUY  # v0.1 limitation

        with contextlib.suppress(ConnectorNonRetryableError):
            self.cancel_order(order_id)

        return self.place_order(
            symbol=symbol,
            side=side,
            price=new_price,
            quantity=new_quantity,
            level_id=level_id,
            ts=ts,
        )

    def fetch_open_orders_raw(self, symbol: str) -> list[dict[str, Any]]:
        """Fetch all open orders for a symbol as raw Binance response.

        Returns raw dicts for ObservedStateStore.update_from_rest_orders().
        """
        self._validate_symbol(symbol)

        if self.config.dry_run:
            return []

        params = {"symbol": symbol}
        params = self._sign_request(params)

        url = f"{self.config.base_url}/fapi/v1/openOrders"
        response = self.http_client.request(
            method="GET",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_GET_OPEN_ORDERS,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        return response.json_data if isinstance(response.json_data, list) else []

    def fetch_open_orders(self, symbol: str) -> list[OrderRecord]:
        """Fetch all open orders for a symbol."""
        self._validate_symbol(symbol)

        if self.config.dry_run:
            return []

        params = {"symbol": symbol}
        params = self._sign_request(params)

        url = f"{self.config.base_url}/fapi/v1/openOrders"
        response = self.http_client.request(
            method="GET",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_GET_OPEN_ORDERS,
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
                    level_id=0,
                    created_ts=order_data.get("time", 0),
                )
            )

        return orders

    def fetch_user_trades_raw(
        self,
        symbol: str,
        *,
        start_time_ms: int | None = None,
        from_id: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Fetch recent user trades for a symbol (read-only).

        Uses GET /fapi/v1/userTrades.  Returns raw dicts from Binance.

        Args:
            symbol: Trading symbol to query.
            start_time_ms: Only trades >= this timestamp (optional).
            from_id: TradeId to fetch from (inclusive, optional).
            limit: Max trades to return (1-1000, default 500).

        Returns:
            List of raw trade dicts from Binance REST response.
        """
        self._validate_symbol(symbol)

        if self.config.dry_run:
            return []

        params: dict[str, Any] = {"symbol": symbol, "limit": min(limit, 1000)}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if from_id is not None:
            params["fromId"] = from_id

        params = self._sign_request(params)

        url = f"{self.config.base_url}/fapi/v1/userTrades"
        response = self.http_client.request(
            method="GET",
            url=url,
            params=params,
            headers=self._get_headers(),
            timeout_ms=self.config.timeout_ms,
            op=OP_GET_USER_TRADES,
        )

        if response.status_code != 200:
            map_binance_error(response.status_code, response.json_data)

        return response.json_data if isinstance(response.json_data, list) else []

    def debug_get_order_status(
        self,
        *,
        symbol: str,
        client_order_id: str,
    ) -> dict[str, str] | None:
        """Debug-only: GET /fapi/v1/order by origClientOrderId (P0-2b)."""
        if self.config.dry_run:
            return None

        params: dict[str, Any] = {
            "symbol": symbol,
            "origClientOrderId": client_order_id,
        }
        params = self._sign_request(params)
        url = f"{self.config.base_url}/fapi/v1/order"

        get_port_metrics().record_http_request(self._PORT_NAME, "GET", "/fapi/v1/order")

        try:
            response = self.http_client.request(
                method="GET",
                url=url,
                params=params,
                headers=self._get_headers(),
                timeout_ms=self.config.timeout_ms,
                op=OP_GET_ORDER_STATUS,
            )
        except (ConnectorTransientError, ConnectorNonRetryableError) as exc:
            get_port_metrics().record_order_lookup(self._PORT_NAME, "error")
            logger.warning(
                "ORDER_LOOKUP_ERROR clientOrderId=%s err=%s",
                client_order_id,
                exc,
            )
            return None

        if response.status_code != 200:
            error_code = None
            if isinstance(response.json_data, dict):
                error_code = response.json_data.get("code")
            # -2013 = "Order does not exist", -2011 = "Unknown order"
            if error_code in (-2013, -2011):
                get_port_metrics().record_order_lookup(self._PORT_NAME, "not_found")
                logger.warning(
                    "ORDER_LOOKUP_NOT_FOUND clientOrderId=%s code=%s msg=%s",
                    client_order_id,
                    error_code,
                    response.json_data.get("msg") if isinstance(response.json_data, dict) else "?",
                )
            else:
                get_port_metrics().record_order_lookup(self._PORT_NAME, "error")
                logger.warning(
                    "ORDER_LOOKUP_ERROR clientOrderId=%s status=%s code=%s data=%s",
                    client_order_id,
                    response.status_code,
                    error_code,
                    response.json_data,
                )
            return None

        if isinstance(response.json_data, dict):
            get_port_metrics().record_order_lookup(self._PORT_NAME, "found")
            return {
                "status": str(response.json_data.get("status", "?")),
                "executedQty": str(response.json_data.get("executedQty", "?")),
                "origQty": str(response.json_data.get("origQty", "?")),
                "avgPrice": str(response.json_data.get("avgPrice", "?")),
                "updateTime": str(response.json_data.get("updateTime", "?")),
                "type": str(response.json_data.get("type", "?")),
                "side": str(response.json_data.get("side", "?")),
            }
        get_port_metrics().record_order_lookup(self._PORT_NAME, "error")
        return None

    def close_position(self, symbol: str) -> str | None:
        """Close any open position for symbol (safety cleanup).

        Uses market order with reduceOnly=True.

        Returns:
            order_id if position was closed, None if no position
        """
        self._validate_mode("close_position")
        self._validate_symbol(symbol)

        positions = self.get_positions(symbol)
        if not positions:
            return None

        for pos in positions:
            if pos.position_amt == 0:
                continue

            # Determine close side (opposite of position)
            # Positive position_amt = long → need to SELL
            # Negative position_amt = short → need to BUY
            close_side = OrderSide.SELL if pos.position_amt > 0 else OrderSide.BUY
            close_qty = abs(pos.position_amt)

            return self.place_market_order(
                symbol=symbol,
                side=close_side,
                quantity=close_qty,
                reduce_only=True,
            )

        return None

    def orders_remaining(self) -> int | None:
        """Remaining order budget (PR-P0-RACE-1)."""
        return self.config.max_orders_per_run - self.config._orders_this_run

    def reset(self) -> None:
        """Reset internal state (for testing and new runs)."""
        self._order_counter = 0
        self._position_mode = None
        self._leverage_set.clear()
        object.__setattr__(self.config, "_orders_this_run", 0)
