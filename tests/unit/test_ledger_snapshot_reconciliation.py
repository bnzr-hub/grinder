"""Tests for EventLedger snapshot-backed reconciliation.

When orders disappear from snapshot but WS terminal event was missed,
the ledger can reconcile them closed after two consecutive absences.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
from grinder.account.event_ledger import EventLedger
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide, OrderState
from grinder.execution.futures_events import FuturesOrderEvent
from grinder.live import LiveEngineConfig, LiveEngineV0


def _snap(
    orders: list[OpenOrderSnap] | None = None,
    ts: int = 1000,
) -> AccountSnapshot:
    return AccountSnapshot(
        positions=(),
        open_orders=tuple(orders or []),
        ts=ts,
        source="test",
    )


def _order(cid: str, symbol: str = "BTCUSDT") -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=cid,
        symbol=symbol,
        side="BUY",
        order_type="LIMIT",
        price=Decimal("50000"),
        qty=Decimal("0.001"),
        filled_qty=Decimal("0"),
        reduce_only=False,
        status="NEW",
        ts=1000,
    )


def _ws_event(cid: str, status: OrderState = OrderState.OPEN, ts: int = 1500) -> FuturesOrderEvent:
    return FuturesOrderEvent(
        ts=ts,
        symbol="BTCUSDT",
        order_id=12345,
        client_order_id=cid,
        side=OrderSide.BUY,
        status=status,
        price=Decimal("50000"),
        qty=Decimal("0.001"),
        executed_qty=Decimal("0"),
        avg_price=Decimal("50000"),
    )


class TestReconcileWithSnapshot:
    """Conservative two-cycle reconciliation of stale ledger orders."""

    def test_single_absence_does_not_close(self) -> None:
        """First absence only flags divergence, does not reconcile."""
        ledger = EventLedger()
        snap_with = _snap(orders=[_order("g-e0"), _order("g-e1")], ts=1000)
        ledger.hydrate_from_snapshot(snap_with)

        # Snapshot 2: g-e0 gone
        snap_without = _snap(orders=[_order("g-e1")], ts=2000)
        ledger.compare_with_snapshot(snap_without)
        # First absence — _prev_missing_in_snapshot now has g-e0

        reconciled = ledger.reconcile_with_snapshot()
        assert reconciled == 0  # conservative: need 2 consecutive
        assert "g-e0" in ledger.open_orders()

    def test_two_consecutive_absences_closes(self) -> None:
        """Two consecutive absences reconcile the order closed."""
        ledger = EventLedger()
        snap_with = _snap(orders=[_order("g-e0"), _order("g-e1")], ts=1000)
        ledger.hydrate_from_snapshot(snap_with)

        # Cycle 1: g-e0 absent
        snap_without = _snap(orders=[_order("g-e1")], ts=2000)
        ledger.compare_with_snapshot(snap_without)

        # Cycle 2: g-e0 still absent → reconcile
        snap_without2 = _snap(orders=[_order("g-e1")], ts=3000)
        ledger.compare_with_snapshot(snap_without2)
        reconciled = ledger.reconcile_with_snapshot()

        assert reconciled == 1
        assert "g-e0" not in ledger.open_orders()
        assert "g-e1" in ledger.open_orders()

    def test_reappearance_resets_tracking(self) -> None:
        """If order reappears in snapshot between cycles, it is NOT closed."""
        ledger = EventLedger()
        snap_with = _snap(orders=[_order("g-e0")], ts=1000)
        ledger.hydrate_from_snapshot(snap_with)

        # Cycle 1: g-e0 absent
        ledger.compare_with_snapshot(_snap(orders=[], ts=2000))

        # Cycle 2: g-e0 reappears!
        ledger.compare_with_snapshot(_snap(orders=[_order("g-e0")], ts=3000))

        # Reconcile: g-e0 was only absent once (not consecutive)
        reconciled = ledger.reconcile_with_snapshot()
        assert reconciled == 0
        assert "g-e0" in ledger.open_orders()

    def test_ws_terminal_still_works(self) -> None:
        """Normal WS close path is unchanged by reconciliation."""
        ledger = EventLedger()
        ledger.apply_order_event(_ws_event("g-e0", status=OrderState.OPEN, ts=1000))
        assert "g-e0" in ledger.open_orders()

        ledger.apply_order_event(_ws_event("g-e0", status=OrderState.CANCELLED, ts=2000))
        assert "g-e0" not in ledger.open_orders()

    def test_trust_reconverges_after_reconciliation(self) -> None:
        """After reconciliation closes stale orders, comparison can converge."""
        ledger = EventLedger()
        snap = _snap(orders=[_order("g-e0"), _order("g-e1")], ts=1000)
        ledger.hydrate_from_snapshot(snap)
        ledger.compare_with_snapshot(snap)
        assert ledger.is_trusted

        # g-e0 disappears (cancelled without WS event)
        snap2 = _snap(orders=[_order("g-e1")], ts=2000)
        ledger.compare_with_snapshot(snap2)
        assert not ledger.is_trusted  # diverged

        # Second absence → reconcile
        snap3 = _snap(orders=[_order("g-e1")], ts=3000)
        ledger.compare_with_snapshot(snap3)
        ledger.reconcile_with_snapshot()

        # Re-compare after reconciliation
        result = ledger.compare_with_snapshot(snap3)
        assert result.is_converged
        assert ledger.is_trusted

    def test_no_false_close_on_matching_snapshot(self) -> None:
        """Orders present in snapshot are never reconciled closed."""
        ledger = EventLedger()
        snap = _snap(orders=[_order("g-e0")], ts=1000)
        ledger.hydrate_from_snapshot(snap)

        # Compare twice with order present
        ledger.compare_with_snapshot(snap)
        ledger.compare_with_snapshot(snap)

        reconciled = ledger.reconcile_with_snapshot()
        assert reconciled == 0
        assert "g-e0" in ledger.open_orders()


# ---------------------------------------------------------------------------
# Engine integration: real _tick_account_sync reconciliation path
# ---------------------------------------------------------------------------


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    return LiveEngineV0(paper, port, LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE))


def _setup_engine(engine: LiveEngineV0) -> MagicMock:
    engine._grid_v2_enabled = True
    engine._grid_v2_symbol = "BTCUSDT"
    engine._grid_v2_started = True
    syncer = MagicMock()
    engine._account_syncer = syncer
    engine._risk_base_enabled = False
    engine._sync_reconciler_enabled = False
    engine._symbol_risk_manager = MagicMock()
    engine._symbol_risk_manager.config.enabled = False
    engine._grid_v2_sync_reconstruct_on_position_drift = MagicMock()  # type: ignore[method-assign]
    return syncer


def _sync_result(orders: list[OpenOrderSnap] | None = None, ts: int = 2000) -> MagicMock:
    snap = _snap(orders=orders or [], ts=ts)
    result = MagicMock()
    result.snapshot = snap
    result.mismatches = []
    result.error = None
    return result


class TestEngineReconciliationPath:
    """Real _tick_account_sync drives ledger reconciliation."""

    def test_two_sync_cycles_reconcile_stale_order(self) -> None:
        engine = _make_engine()
        syncer = _setup_engine(engine)

        # WS populates ledger with g-e0 and g-e1
        engine._event_ledger.apply_order_event(_ws_event("g-e0", ts=1000))
        engine._event_ledger.apply_order_event(_ws_event("g-e1", ts=1000))

        # Sync 1: both present → trusted
        syncer.sync.return_value = _sync_result(orders=[_order("g-e0"), _order("g-e1")], ts=2000)
        engine._tick_account_sync()
        assert engine._event_ledger.is_trusted

        # Sync 2: g-e0 gone (cancelled without WS) → diverge
        syncer.sync.return_value = _sync_result(orders=[_order("g-e1")], ts=3000)
        engine._tick_account_sync()
        # First absence: diverged, not yet reconciled
        assert "g-e0" in engine._event_ledger.open_orders()

        # Sync 3: g-e0 still gone → reconcile closes it
        syncer.sync.return_value = _sync_result(orders=[_order("g-e1")], ts=4000)
        engine._tick_account_sync()
        assert "g-e0" not in engine._event_ledger.open_orders()
        assert engine._event_ledger.is_trusted
