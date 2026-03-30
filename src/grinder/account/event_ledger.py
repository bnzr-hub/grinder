"""Event-authoritative local ledger for orders (ADR-109).

Phase 1 (shadow): builds local order state from Binance ORDER_TRADE_UPDATE
events without changing live authority.

Phase 2 (trusted read): EventLedger is the primary read model for open-order
and fill state in healthy mode. Snapshot remains bootstrap/audit/recovery source.

Phase 2 is order-only. Position tracking from ACCOUNT_UPDATE requires
upstream fixes (multi-position, positionSide) and is deferred to Phase 3.

Three-layer comparison:
1. EventLedger order state (from WS events)
2. AccountSnapshot open orders (from REST sync)
3. Divergence signals when they disagree
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
    Phase 2: trusted read model in healthy mode — primary source for
    open-order and fill state. Snapshot is bootstrap/audit/recovery.

    Thread safety: NOT thread-safe. Use from single event loop only.
    """

    def __init__(self) -> None:
        self._orders: dict[str, LedgerOrder] = {}  # keyed by client_order_id
        self._last_event_ts: int = 0
        self._events_applied: int = 0
        self._duplicates_suppressed: int = 0
        self._bootstrapped: bool = False
        self._last_convergence_ok: bool = False
        self._trust_revoked: bool = False
        # Two-cycle tracking for conservative reconciliation.
        # _prev_missing: CIDs missing in the comparison BEFORE the current one.
        # _curr_missing: CIDs missing in the most recent comparison.
        # reconcile_with_snapshot closes orders in _prev_missing ∩ _curr_missing.
        self._prev_missing_in_snapshot: set[str] = set()
        self._curr_missing_in_snapshot: set[str] = set()

    @property
    def last_event_ts(self) -> int:
        return self._last_event_ts

    @property
    def events_applied(self) -> int:
        return self._events_applied

    @property
    def duplicates_suppressed(self) -> int:
        return self._duplicates_suppressed

    @property
    def bootstrapped(self) -> bool:
        """True after at least one successful hydration with open orders."""
        return self._bootstrapped

    @property
    def trust_revoked(self) -> bool:
        """True when trust was explicitly revoked (degraded mode)."""
        return self._trust_revoked

    @property
    def is_trusted(self) -> bool:
        """True when ledger can be used as primary read model.

        Requires:
        - bootstrapped (hydrated from at least one snapshot with orders)
        - last comparison converged (no divergence between ledger and snapshot)
        - trust not explicitly revoked by degraded-mode boundary
        """
        return self._bootstrapped and self._last_convergence_ok and not self._trust_revoked

    def revoke_trust(self, reason: str) -> None:
        """Explicitly revoke trusted-read authority (degraded mode).

        Called when stream continuity is lost or uncertain.
        Trust can only be restored by restore_trust_after_recovery().
        """
        if not self._trust_revoked:
            self._trust_revoked = True
            logger.info("EVENT_LEDGER_TRUST_REVOKED reason=%s", reason)

    def restore_trust_after_recovery(self) -> bool:
        """Attempt to restore trusted-read authority after recovery.

        Only succeeds if the ledger is bootstrapped and the last
        comparison converged. Returns True if trust was restored.
        """
        if not self._bootstrapped or not self._last_convergence_ok:
            return False
        if self._trust_revoked:
            self._trust_revoked = False
            logger.info("EVENT_LEDGER_TRUST_RESTORED")
        return True

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
        if not self._bootstrapped and order.is_open:
            self._bootstrapped = True
        return order

    def open_orders(self) -> dict[str, LedgerOrder]:
        """Return all currently open orders (non-terminal status)."""
        return {cid: o for cid, o in self._orders.items() if o.is_open}

    def open_orders_for_symbol(self, symbol: str) -> dict[str, LedgerOrder]:
        """Return open orders filtered by symbol."""
        return {cid: o for cid, o in self._orders.items() if o.is_open and o.symbol == symbol}

    def get_order(self, client_order_id: str) -> LedgerOrder | None:
        return self._orders.get(client_order_id)

    def hydrate_from_snapshot(self, snapshot: AccountSnapshot) -> int:
        """Bootstrap/refresh ledger from an AccountSnapshot's open orders.

        Populates the ledger with all open orders from the snapshot.
        Only applies orders not already in the ledger (idempotent).
        Sets last_event_ts to snapshot.ts so subsequent WS events
        with ts > snapshot.ts are applied normally.

        Called on every sync cycle (not just first). This ensures that
        orders placed between the first sync and WS event arrival are
        eventually hydrated.

        Sets _bootstrapped = True once at least one order is present
        (from hydration or from WS events).

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
        # Mark bootstrapped once the ledger has any orders
        if not self._bootstrapped and len(self._orders) > 0:
            self._bootstrapped = True
        return hydrated

    def reset(self) -> None:
        """Clear all state. Used during bootstrap/recovery."""
        self._orders.clear()
        self._last_event_ts = 0
        self._events_applied = 0
        self._duplicates_suppressed = 0
        self._bootstrapped = False
        self._last_convergence_ok = False
        self._trust_revoked = False
        self._prev_missing_in_snapshot = set()
        self._curr_missing_in_snapshot = set()

    def compare_with_snapshot(self, snapshot: AccountSnapshot) -> ShadowComparisonResult:
        """Compare ledger order state against an AccountSnapshot.

        Order-only comparison. Position comparison deferred to Phase 3.
        Returns divergences for observability.
        Updates _last_convergence_ok for trust predicate.
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
        self._prev_missing_in_snapshot = self._curr_missing_in_snapshot
        current_missing: set[str] = set()
        for cid, lo in ledger_open.items():
            if cid not in snapshot_cids:
                current_missing.add(cid)
                divergences.append(
                    Divergence(
                        kind=DivergenceKind.ORDER_MISSING_IN_SNAPSHOT,
                        symbol=lo.symbol,
                        detail=f"cid={cid} open in ledger but not in snapshot",
                    )
                )
        self._curr_missing_in_snapshot = current_missing

        result = ShadowComparisonResult(
            divergences=tuple(sorted(divergences, key=lambda d: (d.kind.value, d.symbol))),
            ledger_open_orders=len(ledger_open),
            snapshot_open_orders=len(snapshot.open_orders),
        )
        # Update trust signal
        self._last_convergence_ok = result.is_converged
        return result

    def reconcile_with_snapshot(self) -> int:
        """Reconcile stale ledger-open orders using snapshot as recovery source.

        If a ledger-open order is absent from the snapshot in TWO consecutive
        comparison cycles, it is marked as recovered-closed. This handles the
        case where a terminal WS event was missed (e.g., cancel without WS ack).

        Conservative rule: requires two consecutive absences to prevent
        false-close from transient one-cycle visibility gaps.

        Must be called AFTER compare_with_snapshot (which updates
        _prev_missing_in_snapshot).

        Returns the number of orders reconciled closed.
        """
        # Close orders that were missing in BOTH the previous AND current comparison.
        consecutive_missing = self._prev_missing_in_snapshot & self._curr_missing_in_snapshot
        reconciled = 0
        for cid in consecutive_missing:
            order = self._orders.get(cid)
            if order is None or not order.is_open:
                continue
            order.status = "CANCELLED"  # recovered terminal state
            reconciled += 1
            logger.info(
                "EVENT_LEDGER_ORDER_RECOVERED cid=%s symbol=%s reason=snapshot_absence_consecutive",
                cid,
                order.symbol,
            )
        return reconciled
