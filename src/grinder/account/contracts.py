"""Account snapshot data contracts (Launch-15).

Frozen dataclasses for positions + open orders fetched from exchange.
Canonical ordering and deterministic serialization per Spec 15.3-15.4.

SSOT: docs/15_ACCOUNT_SYNC_SPEC.md (Sec 15.3, 15.4, 15.5)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class PositionSnap:
    """Exchange position at a point in time (Spec 15.3.1).

    Attributes:
        symbol: Trading pair (e.g. "BTCUSDT").
        side: "LONG" | "SHORT" | "BOTH" (Binance hedge mode).
        qty: Absolute quantity (>= 0).
        signed_qty: Original positionAmt (positive=LONG, negative=SHORT).
        entry_price: Average entry price.
        mark_price: Current mark price (for uPnL calc).
        unrealized_pnl: Exchange-reported unrealized PnL.
        leverage: Current leverage setting.
        ts: Unix ms when fetched from exchange.
    """

    symbol: str
    side: str
    qty: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    leverage: int
    ts: int
    signed_qty: Decimal | None = None

    def sort_key(self) -> tuple[str, str]:
        """Canonical sort key: (symbol, side) -- Spec 15.4."""
        return (self.symbol, self.side)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        d: dict[str, Any] = {
            "symbol": self.symbol,
            "side": self.side,
            "qty": str(self.qty),
            "entry_price": str(self.entry_price),
            "mark_price": str(self.mark_price),
            "unrealized_pnl": str(self.unrealized_pnl),
            "leverage": self.leverage,
            "ts": self.ts,
        }
        if self.signed_qty is not None:
            d["signed_qty"] = str(self.signed_qty)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PositionSnap:
        """Deserialize from dict."""
        signed_qty = Decimal(d["signed_qty"]) if "signed_qty" in d else None
        return cls(
            symbol=d["symbol"],
            side=d["side"],
            qty=Decimal(d["qty"]),
            entry_price=Decimal(d["entry_price"]),
            mark_price=Decimal(d["mark_price"]),
            unrealized_pnl=Decimal(d["unrealized_pnl"]),
            leverage=d["leverage"],
            ts=d["ts"],
            signed_qty=signed_qty,
        )


@dataclass(frozen=True)
class OpenOrderSnap:
    """Exchange open order at a point in time (Spec 15.3.2).

    Attributes:
        order_id: Exchange order ID.
        symbol: Trading pair (e.g. "BTCUSDT").
        side: "BUY" | "SELL".
        order_type: "LIMIT" | "MARKET" | "STOP" | etc.
        price: Limit price (0 for market orders).
        qty: Original quantity.
        filled_qty: Already filled quantity.
        reduce_only: Whether reduce-only flag is set.
        status: "NEW" | "PARTIALLY_FILLED".
        ts: Unix ms when fetched from exchange.
    """

    order_id: str
    symbol: str
    side: str
    order_type: str
    price: Decimal
    qty: Decimal
    filled_qty: Decimal
    reduce_only: bool
    status: str
    ts: int

    def sort_key(self) -> tuple[str, str, str, Decimal, Decimal, str]:
        """Canonical sort key: (symbol, side, order_type, price, qty, order_id) -- Spec 15.4."""
        return (self.symbol, self.side, self.order_type, self.price, self.qty, self.order_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "price": str(self.price),
            "qty": str(self.qty),
            "filled_qty": str(self.filled_qty),
            "reduce_only": self.reduce_only,
            "status": self.status,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OpenOrderSnap:
        """Deserialize from dict."""
        return cls(
            order_id=d["order_id"],
            symbol=d["symbol"],
            side=d["side"],
            order_type=d["order_type"],
            price=Decimal(d["price"]),
            qty=Decimal(d["qty"]),
            filled_qty=Decimal(d["filled_qty"]),
            reduce_only=d["reduce_only"],
            status=d["status"],
            ts=d["ts"],
        )


@dataclass(frozen=True)
class AccountSnapshot:
    """Positions + open orders at a consistent point in time (Spec 15.3.3).

    Invariant: positions and open_orders are tuples in canonical sort order.

    Attributes:
        positions: Canonically-ordered position snapshots.
        open_orders: Canonically-ordered open order snapshots.
        ts: Snapshot timestamp (max of component ts values).
        source: Origin identifier ("exchange" | "test" | "fire_drill").
    """

    positions: tuple[PositionSnap, ...]
    open_orders: tuple[OpenOrderSnap, ...]
    ts: int
    source: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "positions": [p.to_dict() for p in self.positions],
            "open_orders": [o.to_dict() for o in self.open_orders],
            "ts": self.ts,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AccountSnapshot:
        """Deserialize from dict."""
        return cls(
            positions=tuple(PositionSnap.from_dict(p) for p in d["positions"]),
            open_orders=tuple(OpenOrderSnap.from_dict(o) for o in d["open_orders"]),
            ts=d["ts"],
            source=d["source"],
        )


def canonical_positions(positions: list[PositionSnap]) -> tuple[PositionSnap, ...]:
    """Sort positions into canonical order (Spec 15.4).

    Sort key: (symbol, side) -- both ascending, lexicographic.
    """
    return tuple(sorted(positions, key=lambda p: p.sort_key()))


def canonical_orders(orders: list[OpenOrderSnap]) -> tuple[OpenOrderSnap, ...]:
    """Sort open orders into canonical order (Spec 15.4).

    Sort key: (symbol, side, order_type, price, qty, order_id) -- all ascending.
    """
    return tuple(sorted(orders, key=lambda o: o.sort_key()))


def build_account_snapshot(
    positions: list[PositionSnap],
    open_orders: list[OpenOrderSnap],
    source: str = "exchange",
) -> AccountSnapshot:
    """Build AccountSnapshot with canonical ordering.

    Computes ts as max of all component timestamps.
    """
    all_ts = [p.ts for p in positions] + [o.ts for o in open_orders]
    ts = max(all_ts) if all_ts else 0
    return AccountSnapshot(
        positions=canonical_positions(positions),
        open_orders=canonical_orders(open_orders),
        ts=ts,
        source=source,
    )
