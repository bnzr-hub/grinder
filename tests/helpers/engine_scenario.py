"""Reusable engine scenario harness for grid_v2 transitions.

Provides a controllable LiveEngineV0 wrapper that scripts snapshots,
fills, sync cycles, and asserts outcomes without real exchange I/O.

Usage:
    scenario = EngineScenario(symbol="BTCUSDT")
    scenario.tick()
    scenario.assert_bridge_exists()
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from grinder.account.contracts import AccountSnapshot, PositionSnap
from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import OrderSide
from grinder.execution.types import ActionType
from grinder.grid_v2.state import ExitOrder, ExitOrderStatus
from grinder.live import LiveEngineConfig, LiveEngineV0


class EngineScenario:
    """Controllable engine for scripted grid_v2 scenarios.

    Uses save/restore pattern for os.environ to prevent env pollution
    across tests. Must be used as a context manager or call cleanup() explicitly.
    """

    # Env vars set by the harness (used for cleanup)
    _ENV_KEYS: tuple[str, ...] = (
        "GRINDER_GRID_V2_ENABLED",
        "GRINDER_GRID_V2_SYMBOL",
        "GRINDER_GRID_V2_TICK_SIZE",
        "GRINDER_GRID_V2_STEP_PCT",
        "GRINDER_GRID_V2_ENTRY_LEVELS",
        "GRINDER_GRID_V2_MAX_INV_LEVELS",
        "GRINDER_GRID_V2_ORDER_SIZE",
        "GRINDER_GRID_V2_RESEED_ON_FLAT",
        "GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW",
        "GRINDER_GRID_V2_NETOFF_ENABLED",
        "GRINDER_GRID_V2_SYNC_RECONCILER_ENABLED",
        "GRINDER_GRID_V2_SYNC_RECONCILER_PRIMARY",
        "GRINDER_GRID_V2_SYNC_RECONCILER_SHADOW",
    )

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        tick_size: str = "0.01",
        step_pct: str = "0.005",
        levels: int = 3,
        max_inv: int = 5,
        order_size: str = "0.001",
        ref_price: str = "50000",
    ) -> None:
        self.symbol = symbol
        self.tick_size = Decimal(tick_size)
        self.ref_price = Decimal(ref_price)

        # Save original env state for cleanup
        self._saved_env: dict[str, str | None] = {k: os.environ.get(k) for k in self._ENV_KEYS}

        # Set env BEFORE engine creation
        os.environ["GRINDER_GRID_V2_ENABLED"] = "1"
        os.environ["GRINDER_GRID_V2_SYMBOL"] = symbol
        os.environ["GRINDER_GRID_V2_TICK_SIZE"] = tick_size
        os.environ["GRINDER_GRID_V2_STEP_PCT"] = step_pct
        os.environ["GRINDER_GRID_V2_ENTRY_LEVELS"] = str(levels)
        os.environ["GRINDER_GRID_V2_MAX_INV_LEVELS"] = str(max_inv)
        os.environ["GRINDER_GRID_V2_ORDER_SIZE"] = order_size
        os.environ["GRINDER_GRID_V2_RESEED_ON_FLAT"] = "1"
        os.environ["GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW"] = "0"
        os.environ["GRINDER_GRID_V2_NETOFF_ENABLED"] = "0"
        os.environ["GRINDER_GRID_V2_SYNC_RECONCILER_ENABLED"] = "1"
        os.environ["GRINDER_GRID_V2_SYNC_RECONCILER_PRIMARY"] = "1"
        os.environ["GRINDER_GRID_V2_SYNC_RECONCILER_SHADOW"] = "0"

        # Create engine with tracking port AFTER env is set
        self.paper = MagicMock()
        self.paper.process_snapshot.return_value = MagicMock(actions=[])
        self.port = MagicMock()
        self._place_counter = 0

        def _place(**kwargs: Any) -> str:
            self._place_counter += 1
            return f"ORDER_{self._place_counter}"

        self.port.place_order.side_effect = _place
        self.port.cancel_order.return_value = True

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        self.engine = LiveEngineV0(self.paper, self.port, config)

        self._ts = 1_710_000_000_000
        self._cleaned_up = False

    def _next_ts(self) -> int:
        self._ts += 1000
        return self._ts

    def cleanup(self) -> None:
        """Restore os.environ to pre-scenario state. Idempotent."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        for key, original in self._saved_env.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

    def __enter__(self) -> EngineScenario:
        return self

    def __exit__(self, *args: object) -> None:
        self.cleanup()

    def __del__(self) -> None:
        self.cleanup()

    def snapshot(self, price: Decimal | None = None) -> Snapshot:
        """Create a market data snapshot."""
        p = price or self.ref_price
        return Snapshot(
            ts=self._next_ts(),
            symbol=self.symbol,
            bid_price=p - Decimal("1"),
            ask_price=p + Decimal("1"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=p,
            last_qty=Decimal("1"),
        )

    def tick(self, price: Decimal | None = None) -> Any:
        """Process one market snapshot tick."""
        return self.engine.process_snapshot(self.snapshot(price))

    @property
    def bridge(self) -> Any:
        return self.engine._grid_v2_bridge

    @property
    def sm(self) -> Any:
        if self.bridge and self.bridge.state_machine:
            return self.bridge.state_machine
        return None

    @property
    def mode(self) -> str:
        if self.sm:
            return str(self.sm.mode.value)
        return "NONE"

    @property
    def staged_actions(self) -> list[Any]:
        return list(self.engine._sync_reconciler_pending_actions)

    def stage_reconciler_place(
        self,
        cid: str,
        side: OrderSide = OrderSide.BUY,
        price: str = "49000",
    ) -> None:
        """Stage a reconciler PLACE_ENTRY action."""
        from grinder.execution.types import ExecutionAction  # noqa: PLC0415

        self.engine._sync_reconciler_pending_actions = [
            ExecutionAction(
                action_type=ActionType.PLACE,
                symbol=self.symbol,
                side=side,
                price=Decimal(price),
                quantity=Decimal("0.001"),
                client_order_id=cid,
                reason="grid_v2_RECONCILE_PLACE_ENTRY",
            )
        ]

    def set_staged_mode(self, mode: str) -> None:
        """Set the reconciler staged mode."""
        self.engine._reconciler_staged_mode = mode

    def set_primary_reconciler(self) -> None:
        """Enable primary reconciler drain path."""
        self.engine._sync_reconciler_primary = True
        self.engine._grid_v2_awaiting_sync = False
        self.engine._grid_v2_seed_actions = []

    def mock_exit_repair_bridge(
        self,
        exit_price: str,
        exit_id: str = "exit-1",
        lot_id: str = "lot-1",
        cid_in_registry: str | None = "g-X-1",
    ) -> MagicMock:
        """Set up bridge for exit topology repair testing."""
        from decimal import ROUND_UP  # noqa: PLC0415

        bridge = MagicMock()
        bridge.state_machine = MagicMock()
        bridge.state_machine.mode.value = "LONG_BRANCH"
        bridge.state_machine.snapshot.exit_orders = [
            ExitOrder(
                exit_order_id=exit_id,
                lot_id=lot_id,
                side=OrderSide.SELL,
                price=Decimal(exit_price),
                qty=Decimal("0.001"),
                status=ExitOrderStatus.OPEN,
            )
        ]
        bridge.adapter.registry.cid_for_exit.return_value = cid_in_registry
        bridge.adapter.parse_cid.return_value = None
        bridge.adapter.generate_exit_cid.return_value = "g-X-rereg"
        tick = self.tick_size
        bridge._config.price_tick_size = tick
        bridge._quantize_price = lambda p, _s: (
            (p / tick).quantize(Decimal("1"), rounding=ROUND_UP) * tick
        )
        self.engine._grid_v2_bridge = bridge
        return bridge

    def long_position_snapshot(self, qty: str = "1") -> AccountSnapshot:
        """Account snapshot with a LONG position."""
        return AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol=self.symbol,
                    side="LONG",
                    qty=Decimal(qty),
                    entry_price=self.ref_price,
                    mark_price=self.ref_price,
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=self._ts,
                ),
            ),
            open_orders=(),
            ts=self._ts,
            source="test",
        )

    # Assertions

    def assert_bridge_exists(self) -> None:
        assert self.bridge is not None, "Bridge must exist"

    def assert_grid_enabled(self) -> None:
        assert self.engine._grid_v2_enabled, "Grid V2 must be enabled"
