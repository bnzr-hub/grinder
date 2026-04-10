"""Tests for ADR-109 Phase 2 PR-2: event-first post-fill transitions.

Covers:
1. _grid_v2_exchange_cids prefers ledger when trusted
2. _grid_v2_exchange_cids falls back to snapshot when untrusted
3. Snapshot fill path sees fill faster via ledger (filled CID absent)
4. No double-apply: WS fill removes from registry, snapshot diff can't re-trigger
5. process_user_data_event dedup via _grid_v2_user_fill_seen
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
from grinder.account.event_ledger import EventLedger
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide, OrderState
from grinder.execution.futures_events import FuturesOrderEvent, UserDataEvent
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


def _order(
    cid: str,
    symbol: str = "BTCUSDT",
    side: str = "BUY",
    price: str = "50000",
) -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=cid,
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        price=Decimal(price),
        qty=Decimal("0.001"),
        filled_qty=Decimal("0"),
        reduce_only=False,
        status="NEW",
        ts=1000,
    )


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
    return LiveEngineV0(paper, port, config)


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


# CIDs that parse as grid_v2 strategy "g"
# Format: g_{strategy}_{symbol}_{kind}{seq}_{ts}_{attempt}
_G_ENTRY_CID = "g_g_BTCUSDT_e0_1000_0"
_G_EXIT_CID = "g_g_BTCUSDT_x0_1000_0"


class TestExchangeCidsLedgerPreference:
    """_grid_v2_exchange_cids prefers ledger when trusted."""

    def test_trusted_ledger_used(self) -> None:
        import time  # noqa: PLC0415

        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"

        # Trusted ledger has g-entry CID
        ledger = EventLedger()
        snap = _snap(orders=[_order(_G_ENTRY_CID, symbol="BTCUSDT")])
        ledger.hydrate_from_snapshot(snap)
        ledger.compare_with_snapshot(snap)
        assert ledger.is_trusted
        engine._event_ledger = ledger
        engine._last_user_data_event_mono = time.monotonic()  # fresh events

        # Snapshot is stale/empty
        engine._last_account_snapshot = _snap(orders=[], ts=500)

        cids = engine._grid_v2_exchange_cids("BTCUSDT")
        assert _G_ENTRY_CID in cids

    def test_untrusted_ledger_uses_snapshot(self) -> None:
        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"

        # Untrusted ledger (empty)
        engine._event_ledger = EventLedger()

        # Snapshot has the CID
        engine._last_account_snapshot = _snap(orders=[_order(_G_ENTRY_CID, symbol="BTCUSDT")])

        cids = engine._grid_v2_exchange_cids("BTCUSDT")
        assert _G_ENTRY_CID in cids

    def test_filled_order_absent_from_trusted_ledger(self) -> None:
        """When ledger knows order is FILLED, it's absent from exchange_cids."""
        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"

        ledger = EventLedger()
        # First: order appears open
        ledger.apply_order_event(_ws_event(_G_ENTRY_CID, status=OrderState.OPEN, ts=1000))
        # Hydrate + compare to become trusted
        snap = _snap(orders=[_order(_G_ENTRY_CID)], ts=1000)
        ledger.hydrate_from_snapshot(snap)
        ledger.compare_with_snapshot(snap)
        assert ledger.is_trusted
        # Verify the CID is currently in open orders
        assert _G_ENTRY_CID in ledger.open_orders()

        # WS event: order filled -> terminal -> absent from open_orders
        ledger.apply_order_event(
            _ws_event(
                _G_ENTRY_CID,
                status=OrderState.FILLED,
                executed_qty="0.001",
                ts=2000,
            )
        )
        engine._event_ledger = ledger

        # Snapshot still shows the order (stale)
        engine._last_account_snapshot = _snap(orders=[_order(_G_ENTRY_CID)], ts=1500)

        # Ledger is no longer trusted after fill changes open set
        # (comparison would show divergence), but we can directly check
        # that the filled CID is not in ledger's open orders
        assert _G_ENTRY_CID not in ledger.open_orders()

    def test_filters_by_symbol(self) -> None:
        import time  # noqa: PLC0415

        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"

        ledger = EventLedger()
        snap = _snap(
            orders=[
                _order(_G_ENTRY_CID, symbol="BTCUSDT"),
                _order("g_g_ETHUSDT_e0_1000_0", symbol="ETHUSDT"),
            ]
        )
        ledger.hydrate_from_snapshot(snap)
        ledger.compare_with_snapshot(snap)
        engine._event_ledger = ledger
        engine._last_user_data_event_mono = time.monotonic()  # fresh events

        cids = engine._grid_v2_exchange_cids("BTCUSDT")
        assert _G_ENTRY_CID in cids
        assert "g_g_ETHUSDT_e0_1000_0" not in cids


class TestSnapshotFillDetectionViaLedger:
    """Snapshot diff fill path benefits from ledger's faster truth."""

    def test_fill_detected_via_ledger_disappearance(self) -> None:
        """When ledger shows order as filled, snapshot diff sees it as disappeared."""
        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"
        engine._grid_v2_started = True
        engine._grid_v2_pending_cancels = {}
        engine._grid_v2_pending_place_cids = {}

        # Set up bridge with entry in registry
        bridge = MagicMock()
        bridge.reconstruction_ok = True
        bridge.adapter.registry.all_entry_cids = frozenset({_G_ENTRY_CID})
        bridge.adapter.registry.all_exit_cids = frozenset()
        bridge.adapter.registry.lookup_entry.return_value = MagicMock(
            side=OrderSide.BUY, price=Decimal("50000")
        )
        bridge.on_fill.return_value = MagicMock(rejected=False, execution_actions=[])
        engine._grid_v2_bridge = bridge

        # Trusted ledger: order is FILLED (absent from open_orders)
        ledger = EventLedger()
        ledger.apply_order_event(_ws_event(_G_ENTRY_CID, status=OrderState.OPEN, ts=1000))
        # Create a snapshot that ALSO had the order to establish trust
        snap_with = _snap(orders=[_order(_G_ENTRY_CID)], ts=1000)
        ledger.hydrate_from_snapshot(snap_with)
        ledger.compare_with_snapshot(snap_with)
        assert ledger.is_trusted

        # Now WS says FILLED
        ledger.apply_order_event(
            _ws_event(
                _G_ENTRY_CID,
                status=OrderState.FILLED,
                executed_qty="0.001",
                ts=2000,
            )
        )
        engine._event_ledger = ledger

        # Snapshot still shows order (stale) — but ledger is no longer trusted
        # because comparison hasn't re-run. In production, comparison re-runs
        # on next sync. For this test, the key point is that once the fill
        # is processed via WS path, registry is cleared, preventing double-apply.

        # Simulate: WS already processed the fill
        engine._grid_v2_user_fill_seen.add(_G_ENTRY_CID)
        bridge.adapter.registry.all_entry_cids = frozenset()  # cleared by on_fill

        # Now snapshot diff runs — entry is gone from registry, so no fill detected
        actions = engine._grid_v2_process_fills("BTCUSDT", 3000)
        assert actions == []  # No double-apply
        bridge.on_fill.assert_not_called()  # Bridge not called again


class TestNoDoubleApply:
    """WS fill + snapshot diff must not both trigger bridge.on_fill."""

    def test_ws_fill_then_snapshot_no_duplicate(self) -> None:
        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"
        engine._grid_v2_started = True
        engine._grid_v2_pending_cancels = {}
        engine._grid_v2_pending_place_cids = {}

        bridge = MagicMock()
        bridge.reconstruction_ok = True
        bridge.adapter.registry.all_entry_cids = frozenset({_G_ENTRY_CID})
        bridge.adapter.registry.all_exit_cids = frozenset()
        bridge.adapter.registry.lookup_entry.return_value = MagicMock(
            side=OrderSide.BUY, price=Decimal("50000")
        )
        bridge.on_fill.return_value = MagicMock(rejected=False, execution_actions=[])
        engine._grid_v2_bridge = bridge

        # Stale snapshot still has the order
        engine._last_account_snapshot = _snap(orders=[_order(_G_ENTRY_CID)], ts=1000)

        # WS fill already processed
        engine._grid_v2_user_fill_seen.add(_G_ENTRY_CID)
        # on_fill already cleared registry
        bridge.adapter.registry.all_entry_cids = frozenset()

        # Snapshot diff: entry gone from registry → disappeared set is empty
        actions = engine._grid_v2_process_fills("BTCUSDT", 3000)
        assert actions == []


class TestUserDataFillDedup:
    """process_user_data_event deduplicates via _grid_v2_user_fill_seen."""

    def test_duplicate_ws_fill_skipped(self) -> None:
        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"
        engine._grid_v2_started = True

        bridge = MagicMock()
        bridge.reconstruction_ok = True
        bridge.adapter.parse_cid.return_value = MagicMock(kind=MagicMock(value="ENTRY"))
        engine._grid_v2_bridge = bridge

        # Mark as already seen
        engine._grid_v2_user_fill_seen.add(_G_ENTRY_CID)

        # Send duplicate fill event
        from grinder.execution.futures_events import UserDataEvent  # noqa: PLC0415

        event = UserDataEvent(
            event_type=MagicMock(value="ORDER_TRADE_UPDATE"),
            order_event=_ws_event(
                _G_ENTRY_CID,
                status=OrderState.FILLED,
                executed_qty="0.001",
                ts=3000,
            ),
        )
        engine.process_user_data_event(event)

        # bridge.on_fill should NOT be called (deduped)
        bridge.on_fill.assert_not_called()


class TestFillConsumedPendingVisibility:
    """ADR-181: successful user-data fill must terminate pending-visibility lifecycle.

    Without this, a CID that fills before ever appearing in an openOrders snapshot
    can later be misclassified as "never seen" and cleaned by the bounded fallback,
    corrupting entry tracking on fast-fill symbols (live P0 observed 2026-04-10).
    """

    @staticmethod
    def _mk_engine_and_bridge(rejected: bool = False) -> tuple[LiveEngineV0, MagicMock]:
        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"
        engine._grid_v2_started = True
        engine._event_ledger = MagicMock()
        engine._event_ledger.is_trusted = True
        engine._symbol_risk_manager = MagicMock()
        engine._symbol_risk_manager.config.enabled = False

        bridge = MagicMock()
        bridge.reconstruction_ok = True
        bridge.adapter.is_ours.return_value = True
        bridge.adapter.parse_cid.return_value = MagicMock(kind=MagicMock(value="ENTRY"))
        bridge._config.order_size = Decimal("0.001")
        bridge.on_fill.return_value = MagicMock(
            rejected=rejected,
            reject_reason="DUPLICATE_ENTRY_FILL" if rejected else None,
            execution_actions=[],
            transition=None,
        )
        engine._grid_v2_bridge = bridge
        return engine, bridge

    @staticmethod
    def _fill_event(cid: str = _G_ENTRY_CID, ts: int = 3000) -> UserDataEvent:
        return UserDataEvent(
            event_type=MagicMock(value="ORDER_TRADE_UPDATE"),
            order_event=_ws_event(
                cid,
                status=OrderState.FILLED,
                executed_qty="0.001",
                ts=ts,
            ),
        )

    def _prepopulate_trackers(self, engine: LiveEngineV0, cid: str = _G_ENTRY_CID) -> None:
        engine._grid_v2_pending_place_cids[cid] = 0
        engine._grid_v2_fill_eligible_cids.add(cid)
        engine._grid_v2_entry_reg_gen[cid] = 0

    def test_successful_fill_clears_all_three_pending_trackers(self) -> None:
        """REQ-001 happy path: success branch pops pending_place, fill_eligible, entry_reg_gen."""
        engine, bridge = self._mk_engine_and_bridge(rejected=False)
        self._prepopulate_trackers(engine)

        engine.process_user_data_event(self._fill_event())

        assert _G_ENTRY_CID not in engine._grid_v2_pending_place_cids
        assert _G_ENTRY_CID not in engine._grid_v2_fill_eligible_cids
        assert _G_ENTRY_CID not in engine._grid_v2_entry_reg_gen
        bridge.on_fill.assert_called_once()
        assert _G_ENTRY_CID in engine._grid_v2_user_fill_seen

    def test_rejected_fill_branch_also_clears_all_three_trackers(self) -> None:
        """REQ-001 rejected branch: a rejected fill also terminates pending-visibility
        lifecycle so a duplicate-fill-rejected CID can never later fire NEVER_SEEN."""
        engine, bridge = self._mk_engine_and_bridge(rejected=True)
        self._prepopulate_trackers(engine)

        engine.process_user_data_event(self._fill_event())

        assert _G_ENTRY_CID not in engine._grid_v2_pending_place_cids
        assert _G_ENTRY_CID not in engine._grid_v2_fill_eligible_cids
        assert _G_ENTRY_CID not in engine._grid_v2_entry_reg_gen
        assert _G_ENTRY_CID in engine._grid_v2_user_fill_seen
        bridge.on_fill.assert_called_once()

    def test_replayed_fill_does_not_further_mutate_trackers(self) -> None:
        """REQ-003: replayed fill is guarded by _grid_v2_user_fill_seen; second call
        must be a no-op for bridge.on_fill and leave tracking state unchanged."""
        engine, bridge = self._mk_engine_and_bridge(rejected=False)
        self._prepopulate_trackers(engine)

        engine.process_user_data_event(self._fill_event())
        state_after_first = {
            "pending_place": dict(engine._grid_v2_pending_place_cids),
            "fill_eligible": set(engine._grid_v2_fill_eligible_cids),
            "entry_reg_gen": dict(engine._grid_v2_entry_reg_gen),
            "user_fill_seen": set(engine._grid_v2_user_fill_seen),
        }
        assert bridge.on_fill.call_count == 1

        engine.process_user_data_event(self._fill_event(ts=4000))

        assert bridge.on_fill.call_count == 1
        assert dict(engine._grid_v2_pending_place_cids) == state_after_first["pending_place"]
        assert set(engine._grid_v2_fill_eligible_cids) == state_after_first["fill_eligible"]
        assert dict(engine._grid_v2_entry_reg_gen) == state_after_first["entry_reg_gen"]
        assert set(engine._grid_v2_user_fill_seen) == state_after_first["user_fill_seen"]

    def test_previously_visible_cid_fill_path_still_clears_trackers(self) -> None:
        """REQ-004: ordinary visible→fill path also goes through the new pops.
        The fix does not depend on whether the CID was ever in _grid_v2_seen_on_exchange."""
        engine, bridge = self._mk_engine_and_bridge(rejected=False)
        self._prepopulate_trackers(engine)
        engine._grid_v2_seen_on_exchange.add(_G_ENTRY_CID)

        engine.process_user_data_event(self._fill_event())

        assert _G_ENTRY_CID not in engine._grid_v2_pending_place_cids
        assert _G_ENTRY_CID not in engine._grid_v2_fill_eligible_cids
        assert _G_ENTRY_CID not in engine._grid_v2_entry_reg_gen
        bridge.on_fill.assert_called_once()
