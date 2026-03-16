"""Exchange port interface and stub implementation.

The ExchangePort protocol defines the contract for exchange interactions.
NoOpExchangePort is a stub that tracks orders in memory without exchange writes.
"""

from __future__ import annotations

from decimal import Decimal  # noqa: TC003 - used at runtime in Protocol impl
from typing import Protocol

from grinder.account.contracts import AccountSnapshot, PositionSnap
from grinder.core import OrderSide, OrderState
from grinder.execution.port_metrics import get_port_metrics
from grinder.execution.types import OrderRecord


class ExchangePort(Protocol):
    """Protocol for exchange interactions.

    This defines the contract that any exchange adapter must implement.
    The stub implementation (NoOpExchangePort) is used for replay/paper mode.
    """

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
        """Place an order on the exchange.

        Args:
            symbol: Trading symbol
            side: BUY or SELL
            price: Limit price
            quantity: Order quantity
            level_id: Grid level identifier
            ts: Current timestamp
            reduce_only: If True, only reduce position (PR-INV-3)
            client_order_id: Pre-generated clientOrderId override (PR-INV-3)

        Returns:
            order_id: Unique order identifier
        """
        ...

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order.

        Args:
            order_id: Order to cancel

        Returns:
            True if cancellation succeeded
        """
        ...

    def replace_order(
        self,
        order_id: str,
        new_price: Decimal,
        new_quantity: Decimal,
        ts: int,
    ) -> str:
        """Replace an order with new price/quantity.

        Args:
            order_id: Order to replace
            new_price: New limit price
            new_quantity: New quantity
            ts: Current timestamp

        Returns:
            new_order_id: ID of the replacement order
        """
        ...

    def fetch_open_orders(self, symbol: str) -> list[OrderRecord]:
        """Fetch all open orders for a symbol.

        Args:
            symbol: Trading symbol

        Returns:
            List of open order records
        """
        ...

    def fetch_positions(self) -> list[PositionSnap]:
        """Fetch all current positions from the exchange (Launch-15).

        Returns:
            List of position snapshots.
        """
        ...

    def fetch_account_snapshot(self) -> AccountSnapshot:
        """Fetch full account snapshot (positions + open orders) (Launch-15).

        This is the preferred method for sync -- single consistent read.
        """
        ...

    def debug_get_order_status(
        self,
        *,
        symbol: str,
        client_order_id: str,
    ) -> dict[str, str] | None:
        """Debug-only: query single order status by clientOrderId.

        Returns dict with status fields, or None if not supported.
        """
        ...

    def orders_remaining(self) -> int | None:
        """Remaining order budget, or None if unlimited (PR-P0-RACE-1)."""
        ...


class NoOpExchangePort:
    """Stub exchange port that tracks orders in memory.

    This implementation:
    - Does NOT make real exchange calls
    - Tracks orders in memory
    - Generates deterministic order IDs
    - Is suitable for replay/paper mode
    """

    def __init__(self) -> None:
        """Initialize the stub exchange port."""
        self._orders: dict[str, OrderRecord] = {}
        self._order_counter: int = 0

    def _generate_order_id(
        self,
        symbol: str,
        side: OrderSide,
        level_id: int,
        ts: int,
    ) -> str:
        """Generate deterministic order ID.

        Format: {symbol}:{ts}:{level}:{side}:{counter}
        """
        self._order_counter += 1
        return f"{symbol}:{ts}:{level_id}:{side.value}:{self._order_counter}"

    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
        level_id: int,
        ts: int,
        reduce_only: bool = False,  # noqa: ARG002
        client_order_id: str | None = None,
    ) -> str:
        """Place an order (stub - stores in memory only)."""
        get_port_metrics().record_order_attempt("noop", "place")
        if client_order_id is not None:
            order_id = client_order_id
        else:
            order_id = self._generate_order_id(symbol, side, level_id, ts)

        record = OrderRecord(
            order_id=order_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            state=OrderState.OPEN,
            level_id=level_id,
            created_ts=ts,
        )

        self._orders[order_id] = record
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order (stub - updates memory state)."""
        get_port_metrics().record_order_attempt("noop", "cancel")
        if order_id not in self._orders:
            return False

        order = self._orders[order_id]
        if order.state not in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
            return False

        # Update state to cancelled
        self._orders[order_id] = OrderRecord(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            price=order.price,
            quantity=order.quantity,
            state=OrderState.CANCELLED,
            level_id=order.level_id,
            created_ts=order.created_ts,
        )
        return True

    def replace_order(
        self,
        order_id: str,
        new_price: Decimal,
        new_quantity: Decimal,
        ts: int,
    ) -> str:
        """Replace an order (stub - cancel old, place new)."""
        get_port_metrics().record_order_attempt("noop", "replace")
        old_order = self._orders.get(order_id)
        if old_order is None:
            msg = f"Order not found: {order_id}"
            raise ValueError(msg)

        # Cancel old order
        self.cancel_order(order_id)

        # Place new order with same level_id
        return self.place_order(
            symbol=old_order.symbol,
            side=old_order.side,
            price=new_price,
            quantity=new_quantity,
            level_id=old_order.level_id,
            ts=ts,
        )

    def fetch_open_orders(self, symbol: str) -> list[OrderRecord]:
        """Fetch all open orders for a symbol."""
        return [
            order
            for order in self._orders.values()
            if order.symbol == symbol
            and order.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED)
        ]

    def fetch_positions(self) -> list[PositionSnap]:
        """Fetch positions (stub - returns empty list)."""
        return []

    def fetch_account_snapshot(self) -> AccountSnapshot:
        """Fetch account snapshot (stub - returns empty snapshot)."""
        return AccountSnapshot(positions=(), open_orders=(), ts=0, source="stub")

    def cancel_all_orders(self, symbol: str) -> int:
        """Cancel all open orders for symbol (stub).

        Satisfies EmergencyExitPort protocol for testing.
        """
        open_orders = self.fetch_open_orders(symbol)
        for order in open_orders:
            self.cancel_order(order.order_id)
        return len(open_orders)

    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,  # noqa: ARG002
        reduce_only: bool = False,  # noqa: ARG002
    ) -> str:
        """Place market order (stub).

        Satisfies EmergencyExitPort protocol for testing.
        Returns synthetic order ID without modifying positions (positions are always []).
        """
        self._order_counter += 1
        return f"{symbol}:market:{side.value}:{self._order_counter}"

    def get_positions(self, symbol: str) -> list[PositionSnap]:  # noqa: ARG002
        """Get positions for symbol (stub - always empty)."""
        return []

    def debug_get_order_status(
        self,
        *,
        symbol: str,  # noqa: ARG002
        client_order_id: str,  # noqa: ARG002
    ) -> dict[str, str] | None:
        """Stub: not supported."""
        return None

    def orders_remaining(self) -> int | None:
        """Stub: unlimited budget."""
        return None

    def reset(self) -> None:
        """Reset all state (for testing)."""
        self._orders.clear()
        self._order_counter = 0
