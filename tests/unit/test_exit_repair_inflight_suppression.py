"""Tests for exit-topology repair in-flight suppression.

When the fill path already placed an exit but the snapshot hasn't caught up,
repair should not re-place the same exit. Suppression is exact: only the
matching in-flight CID is suppressed, not all repairs.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap, PositionSnap
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.types import ActionType
from grinder.grid_v2.state import ExitOrder, ExitOrderStatus
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.engine import LiveActionStatus


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    return LiveEngineV0(paper, port, LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE))


def _account_snap(
    exit_cids: list[str] | None = None,
    symbol: str = "BTCUSDT",
    position_qty: str = "0.003",
) -> AccountSnapshot:
    """Account snapshot with optional exit orders and a LONG position."""
    orders = []
    for cid in exit_cids or []:
        orders.append(
            OpenOrderSnap(
                order_id=cid,
                symbol=symbol,
                side="SELL",
                order_type="LIMIT",
                price=Decimal("50100"),
                qty=Decimal("0.001"),
                filled_qty=Decimal("0"),
                reduce_only=True,
                status="NEW",
                ts=1000,
            )
        )
    positions = (
        PositionSnap(
            symbol=symbol,
            side="LONG",
            qty=Decimal(position_qty),
            entry_price=Decimal("50000"),
            mark_price=Decimal("50050"),
            unrealized_pnl=Decimal("0.15"),
            leverage=1,
            ts=1000,
        ),
    )
    return AccountSnapshot(
        positions=positions,
        open_orders=tuple(orders),
        ts=1000,
        source="test",
    )


def _mock_bridge_with_exits(
    exit_orders: list[ExitOrder],
    registry_cids: dict[str, str],
) -> MagicMock:
    """Mock bridge with SM exit orders and registry CID mapping."""
    bridge = MagicMock()
    bridge.state_machine = MagicMock()
    bridge.state_machine.mode.value = "LONG_BRANCH"
    bridge.state_machine.snapshot.exit_orders = tuple(exit_orders)
    bridge.state_machine.snapshot.open_lots = (MagicMock(),) * len(exit_orders)
    bridge.reconstruction_ok = True
    bridge._config.price_tick_size = Decimal("0.01")

    def _cid_for_exit(exit_order_id: str) -> str | None:
        return registry_cids.get(exit_order_id)

    bridge.adapter.registry.cid_for_exit.side_effect = _cid_for_exit

    def _parse_cid(cid: str) -> MagicMock | None:
        if cid.startswith("g-x"):
            m = MagicMock()
            m.kind.value = "EXIT"
            return m
        return None

    bridge.adapter.parse_cid.side_effect = _parse_cid
    bridge._quantize_price = lambda p, _s: p
    return bridge


class TestInFlightSuppression:
    """Repair suppresses re-place when exit CID is in pending_place_cids."""

    def test_inflight_exit_not_replaced(self) -> None:
        """Fill path placed exit, repair sees it as 'missing' but suppresses."""
        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"
        engine._grid_v2_started = True

        # SM has 1 exit order
        exit_order = ExitOrder(
            exit_order_id="exit-e1",
            lot_id="lot-e1",
            side=OrderSide.SELL,
            price=Decimal("50100"),
            qty=Decimal("0.001"),
            status=ExitOrderStatus.OPEN,
        )
        bridge = _mock_bridge_with_exits(
            [exit_order],
            {"exit-e1": "g-x0"},
        )
        engine._grid_v2_bridge = bridge

        # Exit g-x0 was just placed by fill path (in pending_place_cids)
        engine._grid_v2_pending_place_cids = {"g-x0": 0}

        # Snapshot does NOT have g-x0 yet (stale)
        snap = _account_snap(exit_cids=[], position_qty="0.001")

        with patch.object(engine, "_process_action") as mock_pa:
            engine._exit_topology_repair_on_sync(snap)

        # _process_action should NOT be called for PLACE (suppressed)
        place_calls = [c for c in mock_pa.call_args_list if c[0][0].action_type == ActionType.PLACE]
        assert len(place_calls) == 0, f"Expected no PLACE calls, got {len(place_calls)}"

    def test_genuine_missing_exit_still_repaired(self) -> None:
        """Exit not in pending_place_cids -> repair places it normally."""
        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"
        engine._grid_v2_started = True

        exit_order = ExitOrder(
            exit_order_id="exit-e1",
            lot_id="lot-e1",
            side=OrderSide.SELL,
            price=Decimal("50100"),
            qty=Decimal("0.001"),
            status=ExitOrderStatus.OPEN,
        )
        bridge = _mock_bridge_with_exits(
            [exit_order],
            {"exit-e1": "g-x0"},
        )
        engine._grid_v2_bridge = bridge

        # g-x0 is NOT in pending_place_cids (genuinely missing)
        engine._grid_v2_pending_place_cids = {}

        snap = _account_snap(exit_cids=[], position_qty="0.001")

        mock_ok = MagicMock()
        mock_ok.status = LiveActionStatus.EXECUTED

        with patch.object(engine, "_process_action", return_value=mock_ok) as mock_pa:
            engine._exit_topology_repair_on_sync(snap)

        # Should have placed the missing exit
        place_calls = [c for c in mock_pa.call_args_list if c[0][0].action_type == ActionType.PLACE]
        assert len(place_calls) == 1
        assert place_calls[0][0][0].client_order_id == "g-x0"

    def test_suppression_is_exact_not_global(self) -> None:
        """Only the in-flight CID is suppressed; other missing exits are placed."""
        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"
        engine._grid_v2_started = True

        exit1 = ExitOrder(
            exit_order_id="exit-e1",
            lot_id="lot-e1",
            side=OrderSide.SELL,
            price=Decimal("50100"),
            qty=Decimal("0.001"),
            status=ExitOrderStatus.OPEN,
        )
        exit2 = ExitOrder(
            exit_order_id="exit-e2",
            lot_id="lot-e2",
            side=OrderSide.SELL,
            price=Decimal("50200"),
            qty=Decimal("0.001"),
            status=ExitOrderStatus.OPEN,
        )
        bridge = _mock_bridge_with_exits(
            [exit1, exit2],
            {"exit-e1": "g-x0", "exit-e2": "g-x1"},
        )
        engine._grid_v2_bridge = bridge

        # g-x0 is in-flight, g-x1 is genuinely missing
        engine._grid_v2_pending_place_cids = {"g-x0": 0}

        snap = _account_snap(exit_cids=[], position_qty="0.002")

        mock_ok = MagicMock()
        mock_ok.status = LiveActionStatus.EXECUTED

        with patch.object(engine, "_process_action", return_value=mock_ok) as mock_pa:
            engine._exit_topology_repair_on_sync(snap)

        # Only g-x1 should be placed (g-x0 suppressed)
        place_calls = [c for c in mock_pa.call_args_list if c[0][0].action_type == ActionType.PLACE]
        placed_cids = {c[0][0].client_order_id for c in place_calls}
        assert "g-x0" not in placed_cids, "In-flight g-x0 should be suppressed"
        assert "g-x1" in placed_cids, "Genuinely missing g-x1 should be placed"
