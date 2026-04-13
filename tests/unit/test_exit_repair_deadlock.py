"""Tests for exit repair deadlock fix (ADR-113).

When an exit placement fails (-2022) and the registry entry is cleaned,
topology repair must re-register and re-place the exit instead of
looping forever in DEFERRED state.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap, PositionSnap
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.types import ActionType
from grinder.grid_v2.exit_repair import (
    DesiredExit,
    compute_exit_topology_repair,
)
from grinder.grid_v2.adapter import GridV2OrderKind
from grinder.grid_v2.state import ExitOrder, ExitOrderStatus
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.engine import LiveActionStatus


def _desired(
    eid: str = "exit-1",
    lid: str = "lot-1",
    side: OrderSide = OrderSide.SELL,
    price: str = "50250",
    qty: str = "0.001",
    cid: str | None = None,
) -> DesiredExit:
    return DesiredExit(
        exit_order_id=eid,
        lot_id=lid,
        side=side,
        price=Decimal(price),
        qty=Decimal(qty),
        registry_cid=cid,
    )


class TestDeferredExitClassification:
    """Exits with no registry_cid are classified as DEFERRED."""

    def test_unregistered_exit_deferred(self) -> None:
        desired = [_desired("exit-1", cid=None)]
        result = compute_exit_topology_repair(desired, set())
        assert result.deferred_count == 1
        assert not result.is_converged

    def test_registered_exit_is_missing_not_deferred(self) -> None:
        desired = [_desired("exit-1", cid="g-X-1")]
        result = compute_exit_topology_repair(desired, set())
        assert result.missing_count == 1
        assert result.deferred_count == 0

    def test_registered_and_on_exchange_is_converged(self) -> None:
        desired = [_desired("exit-1", cid="g-X-1")]
        result = compute_exit_topology_repair(desired, {"g-X-1"})
        assert result.is_converged


class TestReregisterPath:
    """ADR-113: DEFERRED exits should be re-registerable by engine repair."""

    def test_deferred_exit_has_sufficient_info(self) -> None:
        """DEFERRED action carries all info needed for re-registration."""
        desired = [_desired("exit-1", lid="lot-1", cid=None)]
        result = compute_exit_topology_repair(desired, set())

        deferred = [a for a in result.actions if a.action_type == "DEFERRED"]
        assert len(deferred) == 1
        d = deferred[0]
        assert d.exit_order_id == "exit-1"
        assert d.lot_id == "lot-1"
        assert d.side == OrderSide.SELL
        assert d.price == Decimal("50250")
        assert d.qty == Decimal("0.001")

    def test_multiple_deferred_all_carry_info(self) -> None:
        """Multiple DEFERRED exits all have complete info."""
        desired = [
            _desired("exit-1", lid="lot-1", cid=None),
            _desired("exit-2", lid="lot-2", cid=None, price="50500"),
        ]
        result = compute_exit_topology_repair(desired, set())
        assert result.deferred_count == 2
        for a in result.actions:
            if a.action_type == "DEFERRED":
                assert a.exit_order_id is not None
                assert a.lot_id is not None
                assert a.side is not None
                assert a.price is not None
                assert a.qty is not None


class TestNoInfiniteLoop:
    """After re-registration, exit should not keep appearing as DEFERRED."""

    def test_after_reregister_exit_becomes_missing_or_present(self) -> None:
        """Once re-registered (has CID), exit is missing (PLACE) not deferred."""
        # Before re-register: deferred
        desired_before = [_desired("exit-1", cid=None)]
        r1 = compute_exit_topology_repair(desired_before, set())
        assert r1.deferred_count == 1

        # After re-register: has CID → missing (will be placed)
        desired_after = [_desired("exit-1", cid="g-X-new")]
        r2 = compute_exit_topology_repair(desired_after, set())
        assert r2.deferred_count == 0
        assert r2.missing_count == 1

        # After placement succeeds: on exchange → converged
        r3 = compute_exit_topology_repair(desired_after, {"g-X-new"})
        assert r3.is_converged


class TestEnginePathReregistration:
    """Engine-path tests for _exit_topology_repair_on_sync re-register branch."""

    def _setup_engine(
        self,
    ) -> tuple[LiveEngineV0, MagicMock, AccountSnapshot]:
        """Create engine with mocked bridge for exit repair testing."""

        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        port = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(paper, port, config)
        engine._grid_v2_symbol = "BTCUSDT"

        # Mock bridge with SM that has an OPEN exit with no registry entry
        bridge = MagicMock()
        bridge.state_machine = MagicMock()
        bridge.state_machine.mode.value = "LONG_BRANCH"
        deferred_exit = ExitOrder(
            exit_order_id="exit-e10",
            lot_id="lot-e10",
            side=OrderSide.SELL,
            price=Decimal("50250"),
            qty=Decimal("0.001"),
            status=ExitOrderStatus.OPEN,
        )
        bridge.state_machine.snapshot.exit_orders = [deferred_exit]
        # cid_for_exit returns None (registry was cleaned after -2022)
        bridge.adapter.registry.cid_for_exit.return_value = None
        bridge.adapter.parse_cid.return_value = None
        bridge.adapter.generate_exit_cid.return_value = "g-X-new-1"
        engine._grid_v2_bridge = bridge

        # Mock snapshot: LONG position (for SELL exit budget), no exit orders

        snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("1"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )

        return engine, bridge, snapshot

    def test_deferred_exit_reregistered_and_placed(self) -> None:
        """DEFERRED exit → generate_exit_cid + register_exit + _process_action called."""

        engine, bridge, snapshot = self._setup_engine()

        mock_result = MagicMock()
        mock_result.status = LiveActionStatus.EXECUTED

        with patch.object(engine, "_process_action", return_value=mock_result) as mock_pa:
            engine._exit_topology_repair_on_sync(snapshot)

        # Should have called generate_exit_cid
        bridge.adapter.generate_exit_cid.assert_called_once()
        # Should have registered the exit
        bridge.adapter.registry.register_exit.assert_called_once_with(
            "g-X-new-1", "exit-e10", "lot-e10"
        )
        # Should have called _process_action with a PLACE
        assert mock_pa.call_count >= 1
        place_calls = [c for c in mock_pa.call_args_list if c[0][0].action_type == ActionType.PLACE]
        assert len(place_calls) == 1
        placed = place_calls[0][0][0]
        assert placed.client_order_id == "g-X-new-1"
        assert placed.reduce_only is True

    def test_pending_repair_clears_on_converged_topology(self) -> None:
        """Pending reduce-only repair clears only when topology is converged."""
        from types import SimpleNamespace  # noqa: PLC0415

        engine, bridge, snapshot = self._setup_engine()

        # Simulate a registered exit already on exchange.
        bridge.adapter.registry.cid_for_exit.return_value = "g-X-1"
        bridge.adapter.parse_cid.return_value = SimpleNamespace(kind=GridV2OrderKind.EXIT)
        open_exit = OpenOrderSnap(
            order_id="g-X-1",
            symbol="BTCUSDT",
            side="SELL",
            order_type="LIMIT",
            price=Decimal("50250"),
            qty=Decimal("0.001"),
            filled_qty=Decimal("0"),
            reduce_only=True,
            status="NEW",
            ts=1000,
        )
        snapshot = AccountSnapshot(
            positions=snapshot.positions,
            open_orders=(open_exit,),
            ts=1000,
            source="test",
        )

        engine._reduce_only_pending_repair.add(("BTCUSDT", "SELL"))

        with patch.object(engine, "_process_action") as mock_pa:
            engine._exit_topology_repair_on_sync(snapshot)

        assert ("BTCUSDT", "SELL") not in engine._reduce_only_pending_repair
        assert mock_pa.call_count == 0

    def test_two_sync_breaks_infinite_loop(self) -> None:
        """Two syncs: first re-registers (PLACE fails), second sees registered exit.

        This is the core proof that the infinite DEFERRED loop is broken.
        The mock registry becomes stateful: after register_exit, cid_for_exit
        returns the registered CID.
        """
        engine, bridge, snapshot = self._setup_engine()

        # Make registry stateful: cid_for_exit returns None initially,
        # then returns the registered CID after register_exit is called.
        _registry_store: dict[str, str] = {}

        def _mock_register(cid: str, exit_order_id: str, lot_id: str) -> None:
            _registry_store[exit_order_id] = cid

        def _mock_cid_for_exit(exit_order_id: str) -> str | None:
            return _registry_store.get(exit_order_id)

        bridge.adapter.registry.register_exit.side_effect = _mock_register
        bridge.adapter.registry.cid_for_exit.side_effect = _mock_cid_for_exit

        # Sync 1: DEFERRED → re-register → PLACE fails
        mock_fail = MagicMock()
        mock_fail.status = LiveActionStatus.FAILED
        with patch.object(engine, "_process_action", return_value=mock_fail):
            engine._exit_topology_repair_on_sync(snapshot)

        # Verify: exit is now registered
        assert _registry_store.get("exit-e10") == "g-X-new-1"

        # Sync 2: exit is now registered → should be MISSING (not DEFERRED)
        # MISSING exits get a PLACE action, not a DEFERRED log
        mock_ok = MagicMock()
        mock_ok.status = LiveActionStatus.EXECUTED
        with patch.object(engine, "_process_action", return_value=mock_ok) as mock_pa:
            engine._exit_topology_repair_on_sync(snapshot)

        # On second sync, repair should attempt PLACE for the registered-missing exit
        # NOT log DEFERRED reason=not_yet_registered
        place_calls = [c for c in mock_pa.call_args_list if c[0][0].action_type == ActionType.PLACE]
        # The exit should have been placed (not deferred) because it's now registered
        assert len(place_calls) >= 1, (
            "Second sync should PLACE the registered-missing exit, not DEFER it"
        )
