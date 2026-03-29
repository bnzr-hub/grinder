"""Tests for EventLedger shadow mode (ADR-109 Phase 1).

Order-only shadow ledger. Position tracking deferred to Phase 2.

Adversarial tests for:
1. Order lifecycle (new/cancel/fill)
2. Partial fill accounting
3. Duplicate event idempotency
4. Shadow divergence detection (orders)
5. Shadow convergence
6. Reset for bootstrap/recovery
"""

from __future__ import annotations

from decimal import Decimal

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
from grinder.account.event_ledger import (
    DivergenceKind,
    EventLedger,
)
from grinder.core import OrderSide, OrderState
from grinder.execution.futures_events import FuturesOrderEvent


def _order_event(
    cid: str = "g-E-1",
    symbol: str = "BTCUSDT",
    side: OrderSide = OrderSide.BUY,
    status: OrderState = OrderState.OPEN,
    qty: str = "100",
    executed: str = "0",
    price: str = "50000",
    ts: int = 1000,
) -> FuturesOrderEvent:
    return FuturesOrderEvent(
        ts=ts,
        symbol=symbol,
        order_id=12345,
        client_order_id=cid,
        side=side,
        status=status,
        price=Decimal(price),
        qty=Decimal(qty),
        executed_qty=Decimal(executed),
        avg_price=Decimal(price),
    )


def _snap(
    orders: list[OpenOrderSnap] | None = None,
) -> AccountSnapshot:
    return AccountSnapshot(
        positions=(),
        open_orders=tuple(orders or []),
        ts=1000,
        source="test",
    )


def _snap_order(cid: str, filled: str = "0") -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=cid,
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        price=Decimal("50000"),
        qty=Decimal("100"),
        filled_qty=Decimal(filled),
        reduce_only=False,
        status="NEW",
        ts=1000,
    )


class TestOrderLifecycle:
    """Order new/cancel/fill lifecycle tracking."""

    def test_new_order_tracked(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_order_event(status=OrderState.OPEN))
        assert len(ledger.open_orders()) == 1
        assert ledger.events_applied == 1

    def test_filled_order_not_open(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_order_event(status=OrderState.OPEN, ts=1))
        ledger.apply_order_event(_order_event(status=OrderState.FILLED, executed="100", ts=2))
        assert len(ledger.open_orders()) == 0

    def test_cancelled_order_not_open(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_order_event(status=OrderState.OPEN, ts=1))
        ledger.apply_order_event(_order_event(status=OrderState.CANCELLED, ts=2))
        assert len(ledger.open_orders()) == 0

    def test_multiple_orders_tracked(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_order_event(cid="g-E-1", status=OrderState.OPEN, ts=1))
        ledger.apply_order_event(_order_event(cid="g-E-2", status=OrderState.OPEN, ts=2))
        assert len(ledger.open_orders()) == 2


class TestPartialFillAccounting:
    """Remaining qty derived correctly after partial fills."""

    def test_partial_fill_remaining(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_order_event(status=OrderState.OPEN, qty="100", ts=1))
        ledger.apply_order_event(
            _order_event(
                status=OrderState.PARTIALLY_FILLED,
                qty="100",
                executed="40",
                ts=2,
            )
        )
        order = ledger.get_order("g-E-1")
        assert order is not None
        assert order.remaining_qty == Decimal("60")
        assert order.is_open


class TestDuplicateIdempotency:
    """Same event twice does not double-apply."""

    def test_duplicate_order_event_suppressed(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_order_event(status=OrderState.OPEN, ts=1))
        ledger.apply_order_event(_order_event(status=OrderState.OPEN, ts=1))
        assert ledger.events_applied == 1
        assert ledger.duplicates_suppressed == 1

    def test_older_event_suppressed(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_order_event(status=OrderState.OPEN, ts=2))
        ledger.apply_order_event(_order_event(status=OrderState.FILLED, ts=1))
        order = ledger.get_order("g-E-1")
        assert order is not None
        assert order.status == "OPEN"  # newer event preserved


class TestShadowDivergence:
    """Order divergence detection between ledger and snapshot."""

    def test_order_missing_in_ledger(self) -> None:
        ledger = EventLedger()
        snap = _snap(orders=[_snap_order("g-E-1")])
        result = ledger.compare_with_snapshot(snap)
        assert not result.is_converged
        assert any(d.kind == DivergenceKind.ORDER_MISSING_IN_LEDGER for d in result.divergences)

    def test_order_missing_in_snapshot(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_order_event(cid="g-E-1", status=OrderState.OPEN))
        snap = _snap()
        result = ledger.compare_with_snapshot(snap)
        assert not result.is_converged
        assert any(d.kind == DivergenceKind.ORDER_MISSING_IN_SNAPSHOT for d in result.divergences)

    def test_converged_when_matching(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_order_event(cid="g-E-1", status=OrderState.OPEN))
        snap = _snap(orders=[_snap_order("g-E-1")])
        result = ledger.compare_with_snapshot(snap)
        assert result.is_converged

    def test_qty_mismatch_detected(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(
            _order_event(cid="g-E-1", status=OrderState.PARTIALLY_FILLED, executed="50", ts=1)
        )
        snap = _snap(orders=[_snap_order("g-E-1", filled="0")])
        result = ledger.compare_with_snapshot(snap)
        assert not result.is_converged
        assert any(d.kind == DivergenceKind.ORDER_QTY_MISMATCH for d in result.divergences)

    def test_filled_in_ledger_open_in_snapshot(self) -> None:
        """Order filled in ledger but still in snapshot → MISSING_IN_LEDGER."""
        ledger = EventLedger()
        ledger.apply_order_event(_order_event(cid="g-E-1", status=OrderState.OPEN, ts=1))
        ledger.apply_order_event(
            _order_event(cid="g-E-1", status=OrderState.FILLED, executed="100", ts=2)
        )
        snap = _snap(orders=[_snap_order("g-E-1")])
        result = ledger.compare_with_snapshot(snap)
        assert any(d.kind == DivergenceKind.ORDER_MISSING_IN_LEDGER for d in result.divergences)


class TestReset:
    """Reset clears all state for bootstrap/recovery."""

    def test_reset_clears_everything(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_order_event(status=OrderState.OPEN))
        assert ledger.events_applied == 1

        ledger.reset()
        assert ledger.events_applied == 0
        assert len(ledger.open_orders()) == 0
        assert ledger.last_event_ts == 0


class TestBootstrapHydration:
    """ADR-109 PR-C: Bootstrap ledger from AccountSnapshot."""

    def test_hydrate_populates_open_orders(self) -> None:
        ledger = EventLedger()
        snap = _snap(orders=[_snap_order("g-E-1"), _snap_order("g-E-2")])
        hydrated = ledger.hydrate_from_snapshot(snap)
        assert hydrated == 2
        assert len(ledger.open_orders()) == 2

    def test_hydrate_idempotent(self) -> None:
        """Second hydration with same orders does not duplicate."""
        ledger = EventLedger()
        snap = _snap(orders=[_snap_order("g-E-1")])
        ledger.hydrate_from_snapshot(snap)
        hydrated = ledger.hydrate_from_snapshot(snap)
        assert hydrated == 0
        assert len(ledger.open_orders()) == 1

    def test_hydrate_sets_last_event_ts(self) -> None:
        ledger = EventLedger()
        snap = AccountSnapshot(
            positions=(), open_orders=(_snap_order("g-E-1"),), ts=5000, source="test"
        )
        ledger.hydrate_from_snapshot(snap)
        assert ledger.last_event_ts == 5000

    def test_hydrated_order_converges_with_snapshot(self) -> None:
        """After hydration, shadow comparison should converge."""
        ledger = EventLedger()
        snap = _snap(orders=[_snap_order("g-E-1")])
        ledger.hydrate_from_snapshot(snap)
        result = ledger.compare_with_snapshot(snap)
        assert result.is_converged

    def test_no_false_divergence_after_hydration(self) -> None:
        """Previously: ORDER_MISSING_IN_LEDGER on startup. After hydration: none."""
        ledger = EventLedger()
        snap = _snap(orders=[_snap_order("g-E-1"), _snap_order("g-E-2"), _snap_order("g-E-3")])
        ledger.hydrate_from_snapshot(snap)
        result = ledger.compare_with_snapshot(snap)
        assert result.is_converged
        assert len(result.divergences) == 0

    def test_ws_event_after_hydration_updates(self) -> None:
        """WS event with newer ts overwrites hydrated state."""
        ledger = EventLedger()
        snap = _snap(orders=[_snap_order("g-E-1")])
        ledger.hydrate_from_snapshot(snap)
        # WS event fills the order
        ledger.apply_order_event(
            _order_event(cid="g-E-1", status=OrderState.FILLED, executed="100", ts=6000)
        )
        assert len(ledger.open_orders()) == 0
        order = ledger.get_order("g-E-1")
        assert order is not None
        assert order.status == "FILLED"

    def test_partial_ws_prepopulation_then_hydrate(self) -> None:
        """WS event populates one order before first sync.
        Hydration fills remaining orders. Shadow converges."""
        ledger = EventLedger()
        # WS event arrives first: one order known
        ledger.apply_order_event(_order_event(cid="g-E-1", status=OrderState.OPEN, ts=900))
        assert len(ledger.open_orders()) == 1

        # First sync: snapshot has 3 orders
        snap = _snap(orders=[_snap_order("g-E-1"), _snap_order("g-E-2"), _snap_order("g-E-3")])
        hydrated = ledger.hydrate_from_snapshot(snap)
        # Only 2 new (g-E-1 already known)
        assert hydrated == 2
        assert len(ledger.open_orders()) == 3

        # Shadow should converge
        result = ledger.compare_with_snapshot(snap)
        assert result.is_converged
