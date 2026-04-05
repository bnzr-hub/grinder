"""Futures user-data stream event types.

This module defines normalized event types for Binance Futures USDT-M
user-data stream:
- FuturesOrderEvent: From ORDER_TRADE_UPDATE
- FuturesPositionEvent: From ACCOUNT_UPDATE (position part)
- UserDataEvent: Tagged union wrapper

See ADR-041 for design decisions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from grinder.core import OrderSide, OrderState

logger = logging.getLogger(__name__)

# Event types we intentionally ignore for trading decisions.
# These are operational/config notifications that should not flood WARNING.
_NON_ACTIONABLE_USER_DATA_EVENTS: frozenset[str] = frozenset(
    {
        "ACCOUNT_CONFIG_UPDATE",
        "STRATEGY_UPDATE",
        # Binance sends this informational event frequently on futures user-data WS.
        # It is tracked via metrics but should not page operators as WARNING noise.
        "TRADE_LITE",
    }
)


# Binance status -> OrderState mapping
BINANCE_STATUS_MAP: dict[str, OrderState] = {
    "NEW": OrderState.OPEN,
    "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
    "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELLED,
    "REJECTED": OrderState.REJECTED,
    "EXPIRED": OrderState.EXPIRED,
    "EXPIRED_IN_MATCH": OrderState.EXPIRED,
}


class UserDataEventType(Enum):
    """Event types from user-data stream."""

    ORDER_TRADE_UPDATE = "ORDER_TRADE_UPDATE"
    ACCOUNT_UPDATE = "ACCOUNT_UPDATE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FuturesOrderEvent:
    """Normalized order update from ORDER_TRADE_UPDATE stream.

    Attributes:
        ts: Event timestamp (ms)
        symbol: Trading symbol (e.g., "BTCUSDT")
        order_id: Binance numeric order ID
        client_order_id: Our client order ID (grinder_BTCUSDT_...)
        side: BUY or SELL
        status: Order status (OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, etc.)
        price: Limit price
        qty: Original order quantity
        executed_qty: Filled quantity so far
        avg_price: Average fill price
    """

    ts: int
    symbol: str
    order_id: int
    client_order_id: str
    side: OrderSide
    status: OrderState
    price: Decimal
    qty: Decimal
    executed_qty: Decimal
    avg_price: Decimal

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "side": self.side.value,
            "status": self.status.value,
            "price": str(self.price),
            "qty": str(self.qty),
            "executed_qty": str(self.executed_qty),
            "avg_price": str(self.avg_price),
        }

    def to_json(self) -> str:
        """Serialize to JSON string (deterministic)."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FuturesOrderEvent:
        """Create from dict."""
        return cls(
            ts=d["ts"],
            symbol=d["symbol"],
            order_id=d["order_id"],
            client_order_id=d["client_order_id"],
            side=OrderSide(d["side"]),
            status=OrderState(d["status"]),
            price=Decimal(d["price"]),
            qty=Decimal(d["qty"]),
            executed_qty=Decimal(d["executed_qty"]),
            avg_price=Decimal(d["avg_price"]),
        )

    @classmethod
    def from_binance(cls, data: dict[str, Any]) -> FuturesOrderEvent:
        """Create from Binance ORDER_TRADE_UPDATE message.

        Expected format:
        {
          "e": "ORDER_TRADE_UPDATE",
          "E": 1568879465651,  # Event time
          "T": 1568879465650,  # Transaction time
          "o": {
            "s": "BTCUSDT",    # Symbol
            "c": "grinder_...",# Client order ID
            "S": "BUY",        # Side
            "o": "LIMIT",      # Order type
            "X": "NEW",        # Order status
            "i": 8886774,      # Order ID
            "p": "50000",      # Price
            "q": "0.001",      # Quantity
            "z": "0",          # Executed qty
            "ap": "0",         # Average price
            ...
          }
        }
        """
        o = data.get("o", {})
        binance_status = o.get("X", "NEW")
        status = BINANCE_STATUS_MAP.get(binance_status, OrderState.OPEN)

        return cls(
            ts=data.get("E", 0),
            symbol=o.get("s", ""),
            order_id=o.get("i", 0),
            client_order_id=o.get("c", ""),
            side=OrderSide(o.get("S", "BUY")),
            status=status,
            price=Decimal(o.get("p", "0")),
            qty=Decimal(o.get("q", "0")),
            executed_qty=Decimal(o.get("z", "0")),
            avg_price=Decimal(o.get("ap", "0")),
        )


@dataclass(frozen=True)
class FuturesPositionEvent:
    """Normalized position update from ACCOUNT_UPDATE stream.

    Attributes:
        ts: Event timestamp (ms)
        symbol: Trading symbol (e.g., "BTCUSDT")
        position_amt: Position size (positive=long, negative=short, 0=flat)
        entry_price: Average entry price
        unrealized_pnl: Current unrealized profit/loss
    """

    ts: int
    symbol: str
    position_amt: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    position_side: str = "BOTH"  # Binance "ps" field: BOTH, LONG, SHORT

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "position_amt": str(self.position_amt),
            "entry_price": str(self.entry_price),
            "unrealized_pnl": str(self.unrealized_pnl),
            "position_side": self.position_side,
        }

    def to_json(self) -> str:
        """Serialize to JSON string (deterministic)."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FuturesPositionEvent:
        """Create from dict."""
        return cls(
            ts=d["ts"],
            symbol=d["symbol"],
            position_amt=Decimal(d["position_amt"]),
            entry_price=Decimal(d["entry_price"]),
            unrealized_pnl=Decimal(d["unrealized_pnl"]),
            position_side=d.get("position_side", "BOTH"),
        )

    @classmethod
    def from_binance(cls, data: dict[str, Any], symbol: str) -> FuturesPositionEvent:
        """Create from Binance ACCOUNT_UPDATE message for a specific symbol.

        Expected format:
        {
          "e": "ACCOUNT_UPDATE",
          "E": 1564745798939,  # Event time
          "T": 1564745798938,  # Transaction time
          "a": {
            "m": "ORDER",      # Reason
            "B": [...],        # Balances (ignored for v0.1)
            "P": [             # Positions
              {
                "s": "BTCUSDT",  # Symbol
                "pa": "0.001",   # Position amount
                "ep": "50000",   # Entry price
                "up": "0.5",     # Unrealized PnL
                ...
              }
            ]
          }
        }

        Args:
            data: Full ACCOUNT_UPDATE message
            symbol: Symbol to extract position for

        Returns:
            FuturesPositionEvent for the specified symbol
        """
        a = data.get("a", {})
        positions = a.get("P", [])

        # Find position for requested symbol
        for pos in positions:
            if pos.get("s") == symbol:
                return cls(
                    ts=data.get("E", 0),
                    symbol=symbol,
                    position_amt=Decimal(pos.get("pa", "0")),
                    entry_price=Decimal(pos.get("ep", "0")),
                    unrealized_pnl=Decimal(pos.get("up", "0")),
                    position_side=pos.get("ps", "BOTH"),
                )

        # Symbol not in positions - return zero position
        return cls(
            ts=data.get("E", 0),
            symbol=symbol,
            position_amt=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
        )

    @classmethod
    def all_from_binance(cls, data: dict[str, Any]) -> list[FuturesPositionEvent]:
        """Create events for all positions in ACCOUNT_UPDATE message.

        Args:
            data: Full ACCOUNT_UPDATE message

        Returns:
            List of FuturesPositionEvent for each symbol in the update
        """
        a = data.get("a", {})
        positions = a.get("P", [])
        ts = data.get("E", 0)

        result = []
        for pos in positions:
            symbol = pos.get("s", "")
            if symbol:
                result.append(
                    cls(
                        ts=ts,
                        symbol=symbol,
                        position_amt=Decimal(pos.get("pa", "0")),
                        entry_price=Decimal(pos.get("ep", "0")),
                        unrealized_pnl=Decimal(pos.get("up", "0")),
                        position_side=pos.get("ps", "BOTH"),
                    )
                )
        return result


@dataclass(frozen=True)
class UserDataEvent:
    """Wrapper for Binance Futures user-data stream events.

    Tagged union pattern: event_type determines which field is populated.

    Attributes:
        event_type: Type of event (ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, UNKNOWN)
        order_event: FuturesOrderEvent if event_type is ORDER_TRADE_UPDATE
        position_event: FuturesPositionEvent if event_type is ACCOUNT_UPDATE
        raw_data: Raw message dict for UNKNOWN events (for debugging)
    """

    event_type: UserDataEventType
    order_event: FuturesOrderEvent | None = None
    position_event: FuturesPositionEvent | None = None
    raw_data: dict[str, Any] | None = field(default=None, hash=False)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        result: dict[str, Any] = {
            "event_type": self.event_type.value,
        }
        if self.order_event is not None:
            result["order_event"] = self.order_event.to_dict()
        if self.position_event is not None:
            result["position_event"] = self.position_event.to_dict()
        if self.raw_data is not None:
            result["raw_data"] = self.raw_data
        return result

    def to_json(self) -> str:
        """Serialize to JSON string (deterministic)."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UserDataEvent:
        """Create from dict."""
        event_type = UserDataEventType(d["event_type"])
        order_event = None
        position_event = None
        raw_data = d.get("raw_data")

        if d.get("order_event"):
            order_event = FuturesOrderEvent.from_dict(d["order_event"])
        if d.get("position_event"):
            position_event = FuturesPositionEvent.from_dict(d["position_event"])

        return cls(
            event_type=event_type,
            order_event=order_event,
            position_event=position_event,
            raw_data=raw_data,
        )

    @classmethod
    def from_binance(cls, data: dict[str, Any], symbol_filter: str | None = None) -> UserDataEvent:
        """Create from raw Binance user-data stream message.

        Args:
            data: Raw message dict with 'e' field indicating event type
            symbol_filter: If provided, extract position only for this symbol
                          (for ACCOUNT_UPDATE which may have multiple symbols)

        Returns:
            UserDataEvent with appropriate nested event
        """
        event_type_str = data.get("e", "")

        if event_type_str == "ORDER_TRADE_UPDATE":
            order_event = FuturesOrderEvent.from_binance(data)
            return cls(
                event_type=UserDataEventType.ORDER_TRADE_UPDATE,
                order_event=order_event,
            )

        if event_type_str == "ACCOUNT_UPDATE":
            # For ACCOUNT_UPDATE, extract position for first symbol or filtered symbol
            a = data.get("a", {})
            positions = a.get("P", [])

            if symbol_filter:
                position_event = FuturesPositionEvent.from_binance(data, symbol_filter)
            elif positions:
                # Use first position's symbol if no filter
                first_symbol = positions[0].get("s", "")
                position_event = FuturesPositionEvent.from_binance(data, first_symbol)
            else:
                # No positions in update
                position_event = FuturesPositionEvent(
                    ts=data.get("E", 0),
                    symbol="",
                    position_amt=Decimal("0"),
                    entry_price=Decimal("0"),
                    unrealized_pnl=Decimal("0"),
                )

            return cls(
                event_type=UserDataEventType.ACCOUNT_UPDATE,
                position_event=position_event,
            )

        # Unknown event type - keep parser fail-open but classify log severity.
        log_fn = (
            logger.debug if event_type_str in _NON_ACTIONABLE_USER_DATA_EVENTS else logger.warning
        )
        log_fn(
            "unknown_event_type event_type=%s",
            event_type_str,
            extra={"event_type": event_type_str, "raw_data_preview": str(data)[:200]},
        )
        return cls(
            event_type=UserDataEventType.UNKNOWN,
            raw_data=data,
        )
