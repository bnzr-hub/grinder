"""Event-authoritative local ledger for orders (ADR-109 Phase 1).

Shadow mode: builds local order state from Binance ORDER_TRADE_UPDATE
events without changing live authority.

Phase 1 is order-only. Position tracking from ACCOUNT_UPDATE requires
upstream fixes (multi-position, positionSide) and is deferred to Phase 2.

Three-layer comparison:
1. EventLedger order state (from WS events)
2. AccountSnapshot open orders (from REST sync)
3. Divergence signals when they disagree

Zero behavioral change in Phase 1 — observability only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

    from grinder.account.contracts import AccountSnapshot
    from grinder.execution.futures_events import FuturesOrderEvent

logger = logging.getLogger(__name__)


# Terminal order states: order is no longer open
_TERMINAL_STATES: frozenset[str] = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED"})


@dataclass
class LedgerOrder:
    """Per-order state derived from events."""

    order_id: int
    client_order_id: str
    symbol: str
    side: str  # "BUY" or "SELL"
    status: str  # OrderState.value
    price: Decimal
    qty: Decimal
    executed_qty: Decimal
    avg_price: Decimal
    last_event_ts: int

    @property
    def remaining_qty(self) -> Decimal:
        return self.qty - self.executed_qty

    @property
    def is_open(self) -> bool:
        return self.status not in _TERMINAL_STATES


class DivergenceKind(Enum):
    """Types of divergence between ledger and snapshot."""

    ORDER_MISSING_IN_LEDGER = "ORDER_MISSING_IN_LEDGER"
    ORDER_MISSING_IN_SNAPSHOT = "ORDER_MISSING_IN_SNAPSHOT"
    ORDER_QTY_MISMATCH = "ORDER_QTY_MISMATCH"


@dataclass(frozen=True)
class Divergence:
    """A single divergence between ledger and snapshot."""

    kind: DivergenceKind
    symbol: str
    detail: str


@dataclass(frozen=True)
class ShadowComparisonResult:
    """Result of comparing EventLedger orders against AccountSnapshot."""

    divergences: tuple[Divergence, ...]
    ledger_open_orders: int
    snapshot_open_orders: int

    @property
    def is_converged(self) -> bool:
        return len(self.divergences) == 0


class EventLedger:
    """Local event-derived order state.

    Updated incrementally from Binance ORDER_TRADE_UPDATE events.
    Phase 1: shadow mode only — no authority over live decisions.
    Phase 1 is order-only; position tracking deferred to Phase 2.

    Thread safety: NOT thread-safe. Use from single event loop only.
    """

    def __init__(self) -> None:
        self._orders: dict[str, LedgerOrder] = {}  # keyed by client_order_id
        self._last_event_ts: int = 0
        self._events_applied: int = 0
        self._duplicates_suppressed: int = 0

    @property
    def last_event_ts(self) -> int:
        return self._last_event_ts

    @property
    def events_applied(self) -> int:
        return self._events_applied

    @property
    def duplicates_suppressed(self) -> int:
        return self._duplicates_suppressed

    def apply_order_event(self, event: FuturesOrderEvent) -> LedgerOrder:
        """Apply an ORDER_TRADE_UPDATE event to the ledger.

        Idempotent: if event ts <= existing order's last_event_ts for the
        same client_order_id, the event is suppressed as duplicate.

        Returns the updated LedgerOrder.
        """
        cid = event.client_order_id
        existing = self._orders.get(cid)

        # Dedup: suppress if event is not newer than existing state
        if existing is not None and event.ts <= existing.last_event_ts:
            self._duplicates_suppressed += 1
            return existing

        order = LedgerOrder(
            order_id=event.order_id,
            client_order_id=cid,
            symbol=event.symbol,
            side=event.side.value,
            status=event.status.value,
            price=event.price,
            qty=event.qty,
            executed_qty=event.executed_qty,
            avg_price=event.avg_price,
            last_event_ts=event.ts,
        )
        self._orders[cid] = order
        self._last_event_ts = max(self._last_event_ts, event.ts)
        self._events_applied += 1
        return order

    def open_orders(self) -> dict[str, LedgerOrder]:
        """Return all currently open orders (non-terminal status)."""
        return {cid: o for cid, o in self._orders.items() if o.is_open}

    def get_order(self, client_order_id: str) -> LedgerOrder | None:
        return self._orders.get(client_order_id)

    def hydrate_from_snapshot(self, snapshot: AccountSnapshot) -> int:
        """Bootstrap ledger from an AccountSnapshot's open orders.

        Populates the ledger with all open orders from the snapshot.
        Only applies orders not already in the ledger (idempotent).
        Sets last_event_ts to snapshot.ts so subsequent WS events
        with ts > snapshot.ts are applied normally.

        Returns the number of orders hydrated.
        """
        hydrated = 0
        for o in snapshot.open_orders:
            if o.order_id in self._orders:
                continue  # already known
            self._orders[o.order_id] = LedgerOrder(
                order_id=0,  # exchange numeric ID not available in snapshot
                client_order_id=o.order_id,
                symbol=o.symbol,
                side=o.side,
                status="OPEN" if o.filled_qty == 0 else "PARTIALLY_FILLED",
                price=o.price,
                qty=o.qty,
                executed_qty=o.filled_qty,
                avg_price=o.price,  # best available from snapshot
                last_event_ts=snapshot.ts,
            )
            hydrated += 1
        self._last_event_ts = max(self._last_event_ts, snapshot.ts)
        return hydrated

    def reset(self) -> None:
        """Clear all state. Used during bootstrap/recovery."""
        self._orders.clear()
        self._last_event_ts = 0
        self._events_applied = 0
        self._duplicates_suppressed = 0

    def compare_with_snapshot(self, snapshot: AccountSnapshot) -> ShadowComparisonResult:
        """Compare ledger order state against an AccountSnapshot.

        Order-only comparison. Position comparison deferred to Phase 2.
        Returns divergences for observability. Does NOT modify any state.
        """
        divergences: list[Divergence] = []

        # Compare open orders
        ledger_open = self.open_orders()
        snapshot_cids: set[str] = set()
        for o in snapshot.open_orders:
            snapshot_cids.add(o.order_id)

            # Check if snapshot order exists in ledger
            ledger_order = self._orders.get(o.order_id)
            if ledger_order is None or not ledger_order.is_open:
                divergences.append(
                    Divergence(
                        kind=DivergenceKind.ORDER_MISSING_IN_LEDGER,
                        symbol=o.symbol,
                        detail=f"cid={o.order_id} in snapshot but not open in ledger",
                    )
                )
            elif ledger_order.executed_qty != o.filled_qty:
                divergences.append(
                    Divergence(
                        kind=DivergenceKind.ORDER_QTY_MISMATCH,
                        symbol=o.symbol,
                        detail=f"cid={o.order_id} ledger_filled={ledger_order.executed_qty} "
                        f"snapshot_filled={o.filled_qty}",
                    )
                )

        # Check for orders open in ledger but missing from snapshot
        for cid, lo in ledger_open.items():
            if cid not in snapshot_cids:
                divergences.append(
                    Divergence(
                        kind=DivergenceKind.ORDER_MISSING_IN_SNAPSHOT,
                        symbol=lo.symbol,
                        detail=f"cid={cid} open in ledger but not in snapshot",
                    )
                )

        return ShadowComparisonResult(
            divergences=tuple(sorted(divergences, key=lambda d: (d.kind.value, d.symbol))),
            ledger_open_orders=len(ledger_open),
            snapshot_open_orders=len(snapshot.open_orders),
        )
