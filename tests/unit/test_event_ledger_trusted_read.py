"""Tests for EventLedger Phase 2: trusted read model.

Covers:
1. Bootstrap hydration from every sync (not just first)
2. Trust predicate (is_trusted)
3. open_orders_for_symbol filtering
4. Convergence updates trust signal
5. Divergence revokes trust
6. Reset clears trust
7. Engine-path integration: 4 trusted-read switch points
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


def _snapshot(
    orders: list[OpenOrderSnap] | None = None,
    ts: int = 1000,
) -> AccountSnapshot:
    return AccountSnapshot(
        positions=(),
        open_orders=tuple(orders or []),
        ts=ts,
        source="test",
    )


def _order_snap(
    cid: str,
    symbol: str = "BTCUSDT",
    side: str = "BUY",
    price: str = "50000",
    qty: str = "0.001",
) -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=cid,
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        price=Decimal(price),
        qty=Decimal(qty),
        filled_qty=Decimal("0"),
        reduce_only=False,
        status="NEW",
        ts=1000,
    )


def _ws_event(
    cid: str,
    symbol: str = "BTCUSDT",
    side: OrderSide = OrderSide.BUY,
    status: OrderState = OrderState.OPEN,
    price: str = "50000",
    qty: str = "0.001",
    executed_qty: str = "0",
    ts: int = 2000,
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
        executed_qty=Decimal(executed_qty),
        avg_price=Decimal(price),
    )


class TestBootstrapHydration:
    """Bootstrap hydration runs every sync, not just once."""

    def test_empty_first_sync_no_bootstrap(self) -> None:
        """First sync with 0 orders: bootstrapped stays False."""
        ledger = EventLedger()
        snap = _snapshot(orders=[], ts=1000)
        hydrated = ledger.hydrate_from_snapshot(snap)
        assert hydrated == 0
        assert not ledger.bootstrapped

    def test_second_sync_hydrates_orders(self) -> None:
        """Second sync with orders: hydration succeeds."""
        ledger = EventLedger()
        # First sync: empty
        ledger.hydrate_from_snapshot(_snapshot(orders=[], ts=1000))
        assert not ledger.bootstrapped
        # Second sync: orders appear
        snap = _snapshot(orders=[_order_snap("g-e0"), _order_snap("g-e1")], ts=2000)
        hydrated = ledger.hydrate_from_snapshot(snap)
        assert hydrated == 2
        assert ledger.bootstrapped
        assert len(ledger.open_orders()) == 2

    def test_idempotent_hydration(self) -> None:
        """Same orders in multiple syncs: no double-add."""
        ledger = EventLedger()
        snap = _snapshot(orders=[_order_snap("g-e0")], ts=1000)
        assert ledger.hydrate_from_snapshot(snap) == 1
        assert ledger.hydrate_from_snapshot(snap) == 0
        assert len(ledger.open_orders()) == 1

    def test_ws_event_then_hydration_no_overwrite(self) -> None:
        """WS event arrives before snapshot: hydration skips that order."""
        ledger = EventLedger()
        ledger.apply_order_event(_ws_event("g-e0", ts=1500))
        snap = _snapshot(orders=[_order_snap("g-e0")], ts=2000)
        hydrated = ledger.hydrate_from_snapshot(snap)
        assert hydrated == 0  # already known from WS
        assert ledger.bootstrapped  # WS event set an order

    def test_ws_only_bootstraps_without_snapshot(self) -> None:
        """WS event alone can bootstrap the ledger (no snapshot needed)."""
        ledger = EventLedger()
        assert not ledger.bootstrapped
        ledger.apply_order_event(_ws_event("g-e0", status=OrderState.OPEN, ts=1000))
        assert ledger.bootstrapped
        assert len(ledger.open_orders()) == 1

    def test_ws_terminal_event_does_not_bootstrap(self) -> None:
        """A terminal WS event (FILLED/CANCELLED) does not bootstrap."""
        ledger = EventLedger()
        ledger.apply_order_event(_ws_event("g-e0", status=OrderState.FILLED, ts=1000))
        assert not ledger.bootstrapped


class TestTrustPredicate:
    """is_trusted requires bootstrapped + last comparison converged."""

    def test_not_trusted_initially(self) -> None:
        ledger = EventLedger()
        assert not ledger.is_trusted

    def test_not_trusted_after_bootstrap_only(self) -> None:
        """Bootstrapped but no comparison yet: not trusted."""
        ledger = EventLedger()
        snap = _snapshot(orders=[_order_snap("g-e0")], ts=1000)
        ledger.hydrate_from_snapshot(snap)
        assert ledger.bootstrapped
        assert not ledger.is_trusted  # no comparison yet

    def test_trusted_after_converged_comparison(self) -> None:
        """Bootstrapped + converged comparison: trusted."""
        ledger = EventLedger()
        snap = _snapshot(orders=[_order_snap("g-e0")], ts=1000)
        ledger.hydrate_from_snapshot(snap)
        result = ledger.compare_with_snapshot(snap)
        assert result.is_converged
        assert ledger.is_trusted

    def test_trust_revoked_on_divergence(self) -> None:
        """Divergence revokes trust."""
        ledger = EventLedger()
        snap1 = _snapshot(orders=[_order_snap("g-e0")], ts=1000)
        ledger.hydrate_from_snapshot(snap1)
        ledger.compare_with_snapshot(snap1)
        assert ledger.is_trusted

        # New snapshot with different order — divergence
        snap2 = _snapshot(orders=[_order_snap("g-e99")], ts=2000)
        result = ledger.compare_with_snapshot(snap2)
        assert not result.is_converged
        assert not ledger.is_trusted

    def test_trust_restored_after_ws_cancel_and_convergence(self) -> None:
        """After divergence, WS cancel of stale order + convergence restores trust."""
        ledger = EventLedger()
        snap1 = _snapshot(orders=[_order_snap("g-e0")], ts=1000)
        ledger.hydrate_from_snapshot(snap1)
        ledger.compare_with_snapshot(snap1)
        assert ledger.is_trusted

        # New snapshot: g-e0 gone, g-e99 appeared -> divergence
        snap2 = _snapshot(orders=[_order_snap("g-e99")], ts=2000)
        ledger.compare_with_snapshot(snap2)
        assert not ledger.is_trusted

        # WS event cancels stale g-e0, hydration picks up g-e99
        ledger.apply_order_event(_ws_event("g-e0", status=OrderState.CANCELLED, ts=2001))
        ledger.hydrate_from_snapshot(snap2)
        result = ledger.compare_with_snapshot(snap2)
        assert result.is_converged
        assert ledger.is_trusted


class TestOpenOrdersForSymbol:
    """Symbol-filtered open orders."""

    def test_filters_by_symbol(self) -> None:
        ledger = EventLedger()
        snap = _snapshot(
            orders=[
                _order_snap("g-e0", symbol="BTCUSDT"),
                _order_snap("g-e1", symbol="ETHUSDT"),
                _order_snap("g-e2", symbol="BTCUSDT"),
            ],
            ts=1000,
        )
        ledger.hydrate_from_snapshot(snap)
        btc = ledger.open_orders_for_symbol("BTCUSDT")
        assert set(btc.keys()) == {"g-e0", "g-e2"}

    def test_empty_for_unknown_symbol(self) -> None:
        ledger = EventLedger()
        snap = _snapshot(orders=[_order_snap("g-e0", symbol="BTCUSDT")], ts=1000)
        ledger.hydrate_from_snapshot(snap)
        assert len(ledger.open_orders_for_symbol("XYZUSDT")) == 0


class TestResetClearsTrust:
    """Reset clears all state including trust."""

    def test_reset_clears_trusted(self) -> None:
        ledger = EventLedger()
        snap = _snapshot(orders=[_order_snap("g-e0")], ts=1000)
        ledger.hydrate_from_snapshot(snap)
        ledger.compare_with_snapshot(snap)
        assert ledger.is_trusted

        ledger.reset()
        assert not ledger.bootstrapped
        assert not ledger.is_trusted
        assert len(ledger.open_orders()) == 0


class TestTerminalOrderNotOpen:
    """Terminal orders are excluded from open_orders."""

    def test_filled_order_not_in_open(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_ws_event("g-e0", status=OrderState.OPEN, ts=1000))
        assert "g-e0" in ledger.open_orders()

        ledger.apply_order_event(
            _ws_event("g-e0", status=OrderState.FILLED, executed_qty="0.001", ts=2000)
        )
        assert "g-e0" not in ledger.open_orders()

    def test_cancelled_order_not_in_open(self) -> None:
        ledger = EventLedger()
        ledger.apply_order_event(_ws_event("g-e0", status=OrderState.OPEN, ts=1000))
        ledger.apply_order_event(_ws_event("g-e0", status=OrderState.CANCELLED, ts=2000))
        assert "g-e0" not in ledger.open_orders()


# ---------------------------------------------------------------------------
# Engine-path integration tests — drive real _tick_account_sync()
# ---------------------------------------------------------------------------


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
    return LiveEngineV0(paper, port, config)


def _make_trusted_ledger(cids: list[str]) -> EventLedger:
    """Create a ledger that is bootstrapped and trusted with given CIDs open."""
    ledger = EventLedger()
    orders = [_order_snap(cid) for cid in cids]
    snap = _snapshot(orders=orders, ts=1000)
    ledger.hydrate_from_snapshot(snap)
    ledger.compare_with_snapshot(snap)
    assert ledger.is_trusted
    return ledger


def _sync_result(
    orders: list[OpenOrderSnap] | None = None,
    ts: int = 2000,
) -> MagicMock:
    """Mock SyncResult with a real AccountSnapshot."""
    snap = _snapshot(orders=orders or [], ts=ts)
    result = MagicMock()
    result.snapshot = snap
    result.mismatches = []
    result.error = None
    return result


def _setup_engine_for_sync(engine: LiveEngineV0) -> MagicMock:
    """Configure engine so _tick_account_sync runs the grid_v2 paths."""
    engine._grid_v2_enabled = True
    engine._grid_v2_symbol = "BTCUSDT"
    engine._grid_v2_started = True
    syncer = MagicMock()
    engine._account_syncer = syncer
    # Disable downstream paths that aren't under test
    engine._risk_base_enabled = False
    engine._sync_reconciler_enabled = False
    # Prevent symbol risk evaluation from touching MagicMock attributes
    engine._symbol_risk_manager = MagicMock()
    engine._symbol_risk_manager.config.enabled = False
    # Prevent position-drift reconstruction from cascading
    engine._grid_v2_sync_reconstruct_on_position_drift = MagicMock()  # type: ignore[method-assign]
    return syncer


class TestEngineSeedVisibilityRealPath:
    """Seed-visibility via real _tick_account_sync."""

    def test_trusted_ledger_clears_awaiting_sync(self) -> None:
        """Snapshot has seeds + ledger agrees -> trusted -> awaiting cleared."""
        engine = _make_engine()
        syncer = _setup_engine_for_sync(engine)
        engine._grid_v2_awaiting_sync = True
        engine._grid_v2_pending_seed_cids = frozenset({"g-e0", "g-e1"})

        # Pre-populate ledger with WS events (faster than snapshot)
        engine._event_ledger.apply_order_event(_ws_event("g-e0", status=OrderState.OPEN, ts=1500))
        engine._event_ledger.apply_order_event(_ws_event("g-e1", status=OrderState.OPEN, ts=1500))

        # Snapshot also has both seeds -> convergence -> trusted
        syncer.sync.return_value = _sync_result(
            orders=[_order_snap("g-e0"), _order_snap("g-e1")], ts=2000
        )

        engine._tick_account_sync()

        assert not engine._grid_v2_awaiting_sync
        assert engine._grid_v2_pending_seed_cids == frozenset()
        assert engine._event_ledger.is_trusted

    def test_untrusted_ledger_falls_back_to_snapshot(self) -> None:
        """Untrusted ledger + empty snapshot -> awaiting_sync stays True."""
        engine = _make_engine()
        syncer = _setup_engine_for_sync(engine)
        engine._grid_v2_awaiting_sync = True
        engine._grid_v2_pending_seed_cids = frozenset({"g-e0", "g-e1"})

        # Untrusted ledger (fresh, no prior comparison)
        # Snapshot also empty -> no hydration -> not bootstrapped
        syncer.sync.return_value = _sync_result(orders=[], ts=2000)

        engine._tick_account_sync()

        assert engine._grid_v2_awaiting_sync  # still waiting
        assert not engine._event_ledger.is_trusted


class TestEnginePendingPlaceRealPath:
    """Pending-place CID cleanup via real _tick_account_sync."""

    def test_trusted_ledger_clears_visible_pending(self) -> None:
        engine = _make_engine()
        syncer = _setup_engine_for_sync(engine)
        engine._grid_v2_pending_place_cids = {"g-e0": 0, "g-e1": 0}
        engine._account_sync_generation = 0

        # WS event arrives for g-e0 before snapshot
        engine._event_ledger.apply_order_event(_ws_event("g-e0", status=OrderState.OPEN, ts=1500))

        # Snapshot also has g-e0 -> convergence -> trusted
        syncer.sync.return_value = _sync_result(orders=[_order_snap("g-e0")], ts=2000)

        engine._tick_account_sync()

        # g-e0 cleared via trusted ledger visibility
        assert "g-e0" not in engine._grid_v2_pending_place_cids
        # g-e1 still pending (not visible anywhere, not grace-expired)
        assert "g-e1" in engine._grid_v2_pending_place_cids


class TestEngineCancelFailedPruningRealPath:
    """Cancel-failed blacklist pruning via real _tick_account_sync."""

    def test_trusted_ledger_prunes_absent_cancel_failed(self) -> None:
        engine = _make_engine()
        syncer = _setup_engine_for_sync(engine)
        engine._cancel_failed_ids = {"g-e0", "g-gone"}

        # WS events + snapshot agree on g-e0 -> trusted
        engine._event_ledger.apply_order_event(_ws_event("g-e0", status=OrderState.OPEN, ts=1500))
        syncer.sync.return_value = _sync_result(orders=[_order_snap("g-e0")], ts=2000)

        engine._tick_account_sync()

        # g-gone pruned (not in trusted ledger), g-e0 survives
        assert engine._cancel_failed_ids == {"g-e0"}

    def test_untrusted_ledger_prunes_from_snapshot(self) -> None:
        engine = _make_engine()
        syncer = _setup_engine_for_sync(engine)
        engine._cancel_failed_ids = {"g-e0", "g-gone"}

        # No WS events -> untrusted ledger, but snapshot has g-e0
        syncer.sync.return_value = _sync_result(orders=[_order_snap("g-e0")], ts=2000)

        engine._tick_account_sync()

        # Ledger is bootstrapped+converged after this sync, but cancel-failed
        # pruning uses whatever truth was available at the time.
        # g-gone pruned, g-e0 survives via snapshot.
        assert engine._cancel_failed_ids == {"g-e0"}


def _mock_bridge_for_stale_registry(entry_cids: frozenset[str]) -> MagicMock:
    """Create a mock bridge with registry entries for stale-cleaning tests."""
    bridge = MagicMock()
    bridge.state_machine = MagicMock()
    bridge.state_machine.mode.value = "FLAT"
    bridge.state_machine.snapshot.open_lots = ()
    bridge.reconstruction_ok = True
    bridge.adapter.registry.all_entry_cids = entry_cids
    bridge.adapter.registry.all_exit_cids = frozenset()
    bridge._config.max_inventory_levels = 10
    return bridge


class TestEngineStaleRegistryRealPath:
    """Stale-registry cleaning via real _tick_account_sync."""

    def test_trusted_ledger_cleans_stale_registry_entry(self) -> None:
        from unittest.mock import patch as _patch  # noqa: PLC0415

        engine = _make_engine()
        syncer = _setup_engine_for_sync(engine)
        engine._sync_reconciler_enabled = True
        engine._grid_v2_pending_place_cids = {}
        engine._grid_v2_pending_cancels = {}
        engine._grid_v2_bridge = _mock_bridge_for_stale_registry(frozenset({"g-e0", "g-stale"}))

        # WS + snapshot agree on g-e0 -> trusted
        engine._event_ledger.apply_order_event(_ws_event("g-e0", status=OrderState.OPEN, ts=1500))
        syncer.sync.return_value = _sync_result(orders=[_order_snap("g-e0")], ts=2000)

        # Patch reconciler to avoid cascading into full reconciliation
        with _patch("grinder.grid_v2.sync_reconciler.reconcile_grid_state") as mock_recon:
            mock_recon.return_value = MagicMock(
                actions=(),
                would_cancel=0,
                would_place=0,
                desired_entry_count=0,
                theoretical_desired_entry_count=0,
                actual_entry_count=0,
                actual_exit_count=0,
                missing_entries=0,
                extra_entries=0,
                missing_exits=0,
                extra_exits=0,
                projection_mode=MagicMock(value="UNCONSTRAINED"),
                legal_entry_capacity=None,
            )
            # Two syncs needed: stale-registry uses 2-consecutive-absence rule
            engine._tick_account_sync()
            engine._tick_account_sync()

        # g-stale cleaned after 2 consecutive absences
        engine._grid_v2_bridge.adapter.confirm_cancel_entry.assert_called_once_with("g-stale")

    def test_untrusted_ledger_cleans_from_snapshot(self) -> None:
        from unittest.mock import patch as _patch  # noqa: PLC0415

        engine = _make_engine()
        syncer = _setup_engine_for_sync(engine)
        engine._sync_reconciler_enabled = True
        engine._grid_v2_pending_place_cids = {}
        engine._grid_v2_pending_cancels = {}
        engine._grid_v2_bridge = _mock_bridge_for_stale_registry(frozenset({"g-e0", "g-stale"}))

        # Snapshot sees g-e0 (no WS events)
        syncer.sync.return_value = _sync_result(orders=[_order_snap("g-e0")], ts=2000)

        with _patch("grinder.grid_v2.sync_reconciler.reconcile_grid_state") as mock_recon:
            mock_recon.return_value = MagicMock(
                actions=(),
                would_cancel=0,
                would_place=0,
                desired_entry_count=0,
                theoretical_desired_entry_count=0,
                actual_entry_count=0,
                actual_exit_count=0,
                missing_entries=0,
                extra_entries=0,
                missing_exits=0,
                extra_exits=0,
                projection_mode=MagicMock(value="UNCONSTRAINED"),
                legal_entry_capacity=None,
            )
            # Two syncs: 2-consecutive-absence rule
            engine._tick_account_sync()
            engine._tick_account_sync()

        # g-stale cleaned after 2 consecutive absences
        engine._grid_v2_bridge.adapter.confirm_cancel_entry.assert_called_once_with("g-stale")
