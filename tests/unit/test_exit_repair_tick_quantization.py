"""Tests for exit-repair tick-size quantization (ADR-114).

Repair-generated exit placements must have tick-aligned prices before dispatch.
Regression test for live -4014 reject on repaired exit with price like 0.05313250.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.account.contracts import AccountSnapshot, PositionSnap
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.types import ActionType
from grinder.grid_v2.state import ExitOrder, ExitOrderStatus
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.engine import LiveActionStatus


class TestRepairPriceQuantization:
    """ADR-114: Repair placements must be tick-quantized."""

    def _setup_engine(
        self, exit_price: Decimal, tick_size: Decimal = Decimal("0.0001")
    ) -> tuple[LiveEngineV0, MagicMock, AccountSnapshot]:
        """Engine with one exit at given price, configurable tick size."""
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        port = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(paper, port, config)
        engine._grid_v2_symbol = "PIPPINUSDT"

        bridge = MagicMock()
        bridge.state_machine = MagicMock()
        bridge.state_machine.mode.value = "LONG_BRANCH"
        exit_order = ExitOrder(
            exit_order_id="exit-e10",
            lot_id="lot-e10",
            side=OrderSide.SELL,
            price=exit_price,
            qty=Decimal("100"),
            status=ExitOrderStatus.OPEN,
        )
        bridge.state_machine.snapshot.exit_orders = [exit_order]
        bridge.adapter.registry.cid_for_exit.return_value = "g-X-existing"
        bridge.adapter.parse_cid.return_value = None
        bridge.adapter.generate_exit_cid.return_value = "g-X-rereg"

        # Real quantize logic using tick_size
        bridge._config.price_tick_size = tick_size

        def _real_quantize(price: Decimal, side: OrderSide | None) -> Decimal:
            from decimal import ROUND_DOWN, ROUND_UP  # noqa: PLC0415

            if side is not None and side.value == "SELL":
                return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_UP) * tick_size
            return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size

        bridge._quantize_price = _real_quantize
        engine._grid_v2_bridge = bridge

        snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="PIPPINUSDT",
                    side="LONG",
                    qty=Decimal("100"),
                    entry_price=Decimal("0.0530"),
                    mark_price=Decimal("0.0530"),
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

    def test_off_tick_price_quantized_before_dispatch(self) -> None:
        """Reproduce: repair price 0.05313250 → quantized to 0.0532 (tick=0.0001)."""
        engine, _bridge, snapshot = self._setup_engine(
            exit_price=Decimal("0.05313250"), tick_size=Decimal("0.0001")
        )
        # CID exists in registry but not on exchange → MISSING → PLACE
        mock_ok = MagicMock()
        mock_ok.status = LiveActionStatus.EXECUTED

        with patch.object(engine, "_process_action", return_value=mock_ok) as mock_pa:
            engine._exit_topology_repair_on_sync(snapshot)

        place_calls = [c for c in mock_pa.call_args_list if c[0][0].action_type == ActionType.PLACE]
        assert len(place_calls) >= 1
        dispatched_price = place_calls[0][0][0].price
        # Must be tick-aligned: 0.0532 (SELL rounds up)
        assert dispatched_price == Decimal("0.0532"), f"Got {dispatched_price}"
        # Original was 0.05313250 — NOT tick-aligned
        assert dispatched_price != Decimal("0.05313250")

    def test_already_valid_price_unchanged(self) -> None:
        """Valid tick-aligned price stays the same."""
        engine, _bridge, snapshot = self._setup_engine(
            exit_price=Decimal("0.0531"), tick_size=Decimal("0.0001")
        )
        mock_ok = MagicMock()
        mock_ok.status = LiveActionStatus.EXECUTED

        with patch.object(engine, "_process_action", return_value=mock_ok) as mock_pa:
            engine._exit_topology_repair_on_sync(snapshot)

        place_calls = [c for c in mock_pa.call_args_list if c[0][0].action_type == ActionType.PLACE]
        if place_calls:
            assert place_calls[0][0][0].price == Decimal("0.0531")

    def test_deferred_reregister_price_also_quantized(self) -> None:
        """DEFERRED re-register path also quantizes price."""
        engine, _bridge, snapshot = self._setup_engine(
            exit_price=Decimal("0.05313250"), tick_size=Decimal("0.0001")
        )
        # No CID → DEFERRED → re-register
        _bridge.adapter.registry.cid_for_exit.return_value = None

        mock_ok = MagicMock()
        mock_ok.status = LiveActionStatus.EXECUTED

        with patch.object(engine, "_process_action", return_value=mock_ok) as mock_pa:
            engine._exit_topology_repair_on_sync(snapshot)

        place_calls = [c for c in mock_pa.call_args_list if c[0][0].action_type == ActionType.PLACE]
        assert len(place_calls) >= 1
        dispatched_price = place_calls[0][0][0].price
        assert dispatched_price == Decimal("0.0532")
