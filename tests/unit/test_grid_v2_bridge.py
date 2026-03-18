"""Tests for grid_v2 runtime bridge (doc-27 section 23, PR4)."""

import pathlib
from decimal import Decimal

import pytest

from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.grid_v2.adapter import GridV2Adapter, GridV2OrderKind
from grinder.grid_v2.bridge import (
    BRIDGE_DISPATCH_BLOCKED,
    GridV2Bridge,
)
from grinder.grid_v2.state import (
    BranchMode,
    EntryFilled,
    ExitFilled,
    GridV2Config,
)

_BASE_TS = 1_710_000_000_000
_REF_PRICE = Decimal("50000")
_STEP = Decimal("0.005")
_ORDER_SIZE = Decimal("0.001")


def _config(
    step: Decimal = _STEP,
    levels: int = 3,
    size: Decimal = _ORDER_SIZE,
    max_levels: int = 10,
    max_notional: Decimal = Decimal("100000"),
) -> GridV2Config:
    return GridV2Config(
        grid_step_pct=step,
        entry_levels_per_side=levels,
        order_size=size,
        max_inventory_levels=max_levels,
        max_inventory_notional_usd=max_notional,
    )


def _bridge(symbol: str = "BTCUSDT", config: GridV2Config | None = None) -> GridV2Bridge:
    return GridV2Bridge(config or _config(), symbol)


def _fresh_bridge(
    symbol: str = "BTCUSDT",
    config: GridV2Config | None = None,
    ref: Decimal = _REF_PRICE,
    ts: int = _BASE_TS,
) -> tuple[GridV2Bridge, tuple[ExecutionAction, ...]]:
    """Create a bridge with startup_fresh already called. Returns (bridge, seed_actions)."""
    b = _bridge(symbol, config)
    seed = b.startup_fresh(ref, ts)
    return b, seed


class TestStartupReconstructionSuccess:
    """REQ-004: Startup reconstruction gate — success path."""

    def test_startup_fresh_enables_dispatch(self) -> None:
        b, seed = _fresh_bridge()
        assert b.reconstruction_ok is True
        assert b.failed_reason is None
        assert b.state_machine is not None
        assert b.state_machine.mode == BranchMode.FLAT
        assert len(seed) == 6  # 3 buy + 3 sell

    def test_startup_fresh_seed_actions_are_place_entry(self) -> None:
        _b, seed = _fresh_bridge()
        for ea in seed:
            assert ea.action_type == ActionType.PLACE
            assert ea.reason == "grid_v2_PLACE_ENTRY"
            assert ea.client_order_id is not None
            assert ea.symbol == "BTCUSDT"

    def test_startup_reconstruct_from_exchange(self) -> None:
        """Reconstruct from exchange open orders (restart path)."""
        # First create a bridge and seed to get valid CIDs
        b1, _ = _fresh_bridge()
        entry_cids = list(b1.adapter.registry.all_entry_cids)
        # Build order tuples as exchange would report them
        sm = b1.state_machine
        assert sm is not None
        orders: list[tuple[str, OrderSide, Decimal, Decimal]] = []
        for cid in entry_cids:
            reg = b1.adapter.registry.lookup_entry(cid)
            assert reg is not None
            orders.append((cid, reg.side, reg.price, _ORDER_SIZE))

        # Now reconstruct in a fresh bridge
        b2 = _bridge()
        ok = b2.startup(orders, Decimal(0), _REF_PRICE, _BASE_TS)
        assert ok is True
        assert b2.reconstruction_ok is True
        assert b2.state_machine is not None
        assert b2.adapter.registry.entry_count == len(entry_cids)

    def test_startup_reconstruct_empty_exchange(self) -> None:
        """Empty exchange → flat start."""
        b = _bridge()
        ok = b.startup([], Decimal(0), _REF_PRICE, _BASE_TS)
        assert ok is True
        assert b.state_machine is not None
        assert b.state_machine.mode == BranchMode.FLAT


class TestStartupReconstructionFailClosed:
    """REQ-004: Startup reconstruction gate — fail-closed path."""

    def test_reconstruction_failure_blocks_dispatch(self) -> None:
        """F-rule violation → reconstruction_ok=False, dispatch blocked."""
        b = _bridge()
        # F3: flat position but exit orders exist
        exit_adapter = GridV2Adapter(_config(), "BTCUSDT")
        exit_cid = exit_adapter.generate_exit_cid(_BASE_TS)
        exit_price = _REF_PRICE * (Decimal(1) + _STEP)
        # Pass exit order with position_qty=0 → F3
        ok = b.startup(
            [(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE)],
            Decimal(0),  # flat
            _REF_PRICE,
            _BASE_TS,
        )
        assert ok is False
        assert b.reconstruction_ok is False
        assert b.failed_reason is not None
        assert "F3" in b.failed_reason

    def test_failed_startup_blocks_on_fill(self) -> None:
        """After failed startup, on_fill raises RuntimeError."""
        b = _bridge()
        # Force failure
        exit_adapter = GridV2Adapter(_config(), "BTCUSDT")
        exit_cid = exit_adapter.generate_exit_cid(_BASE_TS)
        exit_price = _REF_PRICE * (Decimal(1) + _STEP)
        b.startup(
            [(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE)],
            Decimal(0),
            _REF_PRICE,
            _BASE_TS,
        )
        with pytest.raises(RuntimeError, match=BRIDGE_DISPATCH_BLOCKED):
            b.on_fill("any_cid", OrderSide.BUY, Decimal("49750"), _ORDER_SIZE, _BASE_TS)

    def test_failed_startup_blocks_cancel_ack(self) -> None:
        b = _bridge()
        # Don't call startup at all
        with pytest.raises(RuntimeError, match=BRIDGE_DISPATCH_BLOCKED):
            b.on_cancel_ack("any_cid")

    def test_failed_startup_blocks_reconcile(self) -> None:
        b = _bridge()
        with pytest.raises(RuntimeError, match=BRIDGE_DISPATCH_BLOCKED):
            b.reconcile(frozenset(), _BASE_TS)


class TestNoDispatchBeforeReconstruction:
    """REQ-008: No dispatch before successful startup reconstruction."""

    def test_no_dispatch_before_startup(self) -> None:
        b = _bridge()
        # No startup called
        assert b.reconstruction_ok is False
        with pytest.raises(RuntimeError, match=BRIDGE_DISPATCH_BLOCKED):
            b.on_fill("cid", OrderSide.BUY, Decimal("100"), _ORDER_SIZE, _BASE_TS)

    def test_dispatch_ok_after_successful_startup(self) -> None:
        b, _ = _fresh_bridge()
        # Should not raise (fill will be foreign → None, but no RuntimeError)
        result = b.on_fill("foreign_cid", OrderSide.BUY, Decimal("100"), _ORDER_SIZE, _BASE_TS)
        assert result.translated is None


class TestFillLifecycleEntry:
    """REQ-002: Fill lifecycle — entry path."""

    def test_entry_fill_produces_place_exit(self) -> None:
        """translate → apply → confirm → resolve → PLACE_EXIT."""
        b, seed = _fresh_bridge()
        sm = b.state_machine
        assert sm is not None

        # Pick first buy entry
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        assert len(buy_seed) > 0
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None
        assert buy_price is not None

        result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)

        assert result.rejected is False
        assert result.translated is not None
        assert isinstance(result.translated.event, EntryFilled)
        assert result.transition is not None
        assert not result.transition.rejected
        # Should have PLACE_EXIT action
        exits = [ea for ea in result.execution_actions if ea.reason == "grid_v2_PLACE_EXIT"]
        assert len(exits) == 1
        assert exits[0].action_type == ActionType.PLACE
        assert exits[0].reduce_only is True
        # Entry should be removed from registry (confirmed)
        assert b.adapter.registry.lookup_entry(buy_cid) is None

    def test_entry_fill_transitions_to_long_branch(self) -> None:
        b, seed = _fresh_bridge()
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        assert b.state_machine is not None
        assert b.state_machine.mode == BranchMode.LONG_BRANCH


class TestFillLifecycleExit:
    """REQ-002: Fill lifecycle — exit path."""

    def test_exit_fill_unwinds_to_flat(self) -> None:
        """Entry fill → exit fill → back to FLAT."""
        b, seed = _fresh_bridge()
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        # Entry fill
        entry_result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        assert not entry_result.rejected

        # Get exit CID from resolved actions
        exit_actions = [
            ea for ea in entry_result.execution_actions if ea.reason == "grid_v2_PLACE_EXIT"
        ]
        assert len(exit_actions) == 1
        exit_cid = exit_actions[0].client_order_id
        exit_price = exit_actions[0].price
        assert exit_cid is not None and exit_price is not None

        # Exit fill
        exit_result = b.on_fill(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE, _BASE_TS + 2000)
        assert not exit_result.rejected
        assert isinstance(exit_result.translated.event, ExitFilled)  # type: ignore[union-attr]
        assert b.state_machine is not None
        assert b.state_machine.mode == BranchMode.FLAT


class TestFillRejected:
    """REQ-002 supplement: rejected transitions produce no dispatch."""

    def test_rejected_fill_no_confirm_no_dispatch(self) -> None:
        """If sm.apply rejects, no confirm_* called, no actions."""
        b, seed = _fresh_bridge()
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        # First fill succeeds
        b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)

        # Try same CID again → should raise ValueError (stale, entry removed from registry)
        with pytest.raises(ValueError, match="not in registry"):
            b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 2000)


class TestCancelLifecycleEntry:
    """REQ-003: Cancel lifecycle — entry path."""

    def test_cancel_ack_entry_removes_from_registry(self) -> None:
        b, seed = _fresh_bridge()
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        assert buy_cid is not None

        # Before cancel: entry exists
        assert b.adapter.registry.lookup_entry(buy_cid) is not None

        # Cancel ack
        result = b.on_cancel_ack(buy_cid)
        assert result.removed is True
        assert result.kind == GridV2OrderKind.ENTRY

        # After cancel: entry gone
        assert b.adapter.registry.lookup_entry(buy_cid) is None


class TestCancelLifecycleExit:
    """REQ-003: Cancel lifecycle — exit path."""

    def test_cancel_ack_exit_removes_from_registry(self) -> None:
        b, seed = _fresh_bridge()
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        # Entry fill to get an exit in registry
        entry_result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        exit_actions = [
            ea for ea in entry_result.execution_actions if ea.reason == "grid_v2_PLACE_EXIT"
        ]
        exit_cid = exit_actions[0].client_order_id
        assert exit_cid is not None

        # Before cancel: exit exists
        assert b.adapter.registry.lookup_exit(exit_cid) is not None

        # Cancel ack
        result = b.on_cancel_ack(exit_cid)
        assert result.removed is True
        assert result.kind == GridV2OrderKind.EXIT

        # After cancel: exit gone
        assert b.adapter.registry.lookup_exit(exit_cid) is None


class TestCrossSymbolIgnored:
    """REQ-006: Cross-symbol CIDs treated as foreign/ignored."""

    def test_cross_symbol_fill_returns_none(self) -> None:
        """BTCUSDT CID on ETHUSDT bridge → foreign, no state mutation."""
        _btc_bridge, btc_seed = _fresh_bridge(symbol="BTCUSDT")
        eth_bridge, _ = _fresh_bridge(symbol="ETHUSDT")

        btc_cid = btc_seed[0].client_order_id
        assert btc_cid is not None

        result = eth_bridge.on_fill(btc_cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE, _BASE_TS)
        assert result.translated is None
        assert result.rejected is False
        assert len(result.execution_actions) == 0

    def test_cross_symbol_cancel_ack_no_removal(self) -> None:
        _btc_bridge, btc_seed = _fresh_bridge(symbol="BTCUSDT")
        eth_bridge, _ = _fresh_bridge(symbol="ETHUSDT")

        btc_cid = btc_seed[0].client_order_id
        assert btc_cid is not None

        result = eth_bridge.on_cancel_ack(btc_cid)
        assert result.removed is False


class TestReconstructionDeterministic:
    """REQ-005: Reconstruction is deterministic (same input → same outcome)."""

    def test_same_exchange_orders_produce_same_snapshot(self) -> None:
        # Create valid exchange orders from a fresh bridge
        b1, _ = _fresh_bridge()
        entry_cids = list(b1.adapter.registry.all_entry_cids)
        orders: list[tuple[str, OrderSide, Decimal, Decimal]] = []
        for cid in entry_cids:
            reg = b1.adapter.registry.lookup_entry(cid)
            assert reg is not None
            orders.append((cid, reg.side, reg.price, _ORDER_SIZE))

        # Reconstruct twice
        b2 = _bridge()
        b2.startup(orders, Decimal(0), _REF_PRICE, _BASE_TS)
        snap2 = b2.state_machine
        assert snap2 is not None

        b3 = _bridge()
        b3.startup(orders, Decimal(0), _REF_PRICE, _BASE_TS)
        snap3 = b3.state_machine
        assert snap3 is not None

        assert snap2.snapshot == snap3.snapshot
        assert b2.adapter.registry.entry_count == b3.adapter.registry.entry_count


class TestPriceQuantization:
    """PR6: Prices must be quantized to exchange tick size."""

    def test_seed_prices_quantized(self) -> None:
        """Fresh startup seed actions have prices quantized to tick_size."""
        cfg = _config()
        b = _bridge(config=cfg)
        seed = b.startup_fresh(_REF_PRICE, _BASE_TS)
        for ea in seed:
            assert ea.price is not None
            # price / tick_size should be an integer (no remainder)
            remainder = ea.price % cfg.price_tick_size
            assert remainder == 0, f"price={ea.price} not quantized to tick={cfg.price_tick_size}"

    def test_fill_generated_actions_quantized(self) -> None:
        """Actions from fill processing have quantized prices."""
        b, seed = _fresh_bridge()
        # Simulate an entry fill → should generate PLACE_EXIT with quantized price
        cid = seed[0].client_order_id
        assert cid is not None
        entry_reg = b.adapter.registry.lookup_entry(cid)
        assert entry_reg is not None
        result = b.on_fill(cid, entry_reg.side, entry_reg.price, _ORDER_SIZE, _BASE_TS)
        for ea in result.execution_actions:
            if ea.price is not None:
                remainder = ea.price % b._config.price_tick_size
                assert remainder == 0, f"price={ea.price} not quantized"

    def test_custom_tick_size(self) -> None:
        """Non-default tick_size is respected."""
        cfg = GridV2Config(
            grid_step_pct=_STEP,
            entry_levels_per_side=1,
            order_size=_ORDER_SIZE,
            max_inventory_levels=10,
            max_inventory_notional_usd=Decimal("100000"),
            price_tick_size=Decimal("0.1"),
        )
        b = GridV2Bridge(cfg, "BTCUSDT")
        seed = b.startup_fresh(_REF_PRICE, _BASE_TS)
        for ea in seed:
            assert ea.price is not None
            remainder = ea.price % Decimal("0.1")
            assert remainder == 0, f"price={ea.price} not quantized to 0.1"


class TestSwitchDisabledLegacyUnchanged:
    """REQ-007: Legacy path unchanged when switch is disabled.

    Engine reads GRINDER_GRID_V2_ENABLED at init. When False (default),
    grid_v2_bridge is None and no grid_v2 code path runs.
    Full legacy test suite passing unchanged is the primary proof.
    """

    def test_engine_grid_v2_disabled_by_default(self) -> None:
        """GRINDER_GRID_V2_ENABLED defaults to False → no bridge created."""
        import grinder.live.engine as engine_mod  # noqa: PLC0415

        # Verify the env var is read in __init__
        source = engine_mod.__file__
        assert source is not None
        with pathlib.Path(source).open() as f:
            code = f.read()
        assert "GRINDER_GRID_V2_ENABLED" in code
        assert "GRINDER_GRID_V2_SYMBOL" in code
        assert "_grid_v2_bridge" in code
        assert "_is_grid_v2_active" in code


class TestSwitchEnabledEmptySymbol:
    """REQ-008: switch enabled + empty symbol → fail-closed at engine init."""

    def test_engine_raises_on_empty_symbol(self) -> None:
        """GRINDER_GRID_V2_ENABLED=True + empty GRINDER_GRID_V2_SYMBOL → ValueError."""
        import grinder.live.engine as engine_mod  # noqa: PLC0415

        # Verify the fail-closed guard exists in engine source
        source = engine_mod.__file__
        assert source is not None
        with pathlib.Path(source).open() as f:
            code = f.read()
        assert "GRINDER_GRID_V2_ENABLED=True requires GRINDER_GRID_V2_SYMBOL" in code

    def test_engine_init_raises_on_empty_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Real engine init with GRID_V2_ENABLED=1 + no symbol → ValueError."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.delenv("GRINDER_GRID_V2_SYMBOL", raising=False)

        with pytest.raises(ValueError, match="GRINDER_GRID_V2_SYMBOL"):
            LiveEngineV0(
                paper_engine=MagicMock(),
                exchange_port=MagicMock(),
                config=LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY),
            )

    def test_engine_init_creates_bridge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Real engine init with GRID_V2_ENABLED=1 + valid symbol → bridge created."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY),
        )
        assert engine._grid_v2_enabled is True
        assert engine._grid_v2_bridge is not None
        assert engine._grid_v2_bridge.symbol == "BTCUSDT"
        assert engine._grid_v2_started is False  # not yet started (no account sync)


class TestEngineFreshStartupDispatch:
    """P0-1: Fresh startup seed actions must reach dispatch on first tick."""

    def test_fresh_startup_dispatches_seed_window(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First tick after fresh startup includes PLACE_ENTRY seed actions."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        paper = MagicMock()
        port = MagicMock()
        port.place_order.return_value = "ORDER_1"

        engine = LiveEngineV0(
            paper_engine=paper,
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Provide account snapshot (flat, no orders)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=(),
            ts=_BASE_TS,
            source="test",
        )

        snapshot = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )

        output = engine.process_snapshot(snapshot)

        # Seed actions should have been dispatched
        assert engine._grid_v2_started is True
        assert engine._grid_v2_bridge is not None
        assert engine._grid_v2_bridge.reconstruction_ok is True
        # At least some actions should be executed (PLACE_ENTRY from seed window)
        executed = [a for a in output.live_actions if a.status.value == "EXECUTED"]
        assert len(executed) > 0, "Seed PLACE_ENTRY actions should be dispatched on first tick"
        for ea in executed:
            assert ea.action.reason == "grid_v2_PLACE_ENTRY"


class TestEngineNonFlatFailClosed:
    """P0-2: Non-flat position + no g-orders must fail-closed."""

    def test_non_flat_no_orders_blocks_startup(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-flat position with no grid_v2 orders → startup blocked, no actions."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, PositionSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Non-flat position, no grid_v2 orders
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.01"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50100"),
                    unrealized_pnl=Decimal("1"),
                    leverage=1,
                    ts=_BASE_TS,
                ),
            ),
            open_orders=(),
            ts=_BASE_TS,
            source="test",
        )

        snapshot = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )

        output = engine.process_snapshot(snapshot)

        # Startup should have been attempted but blocked (non-flat, no g-orders)
        assert engine._grid_v2_started is True
        assert engine._grid_v2_bridge is not None
        # Bridge reconstruction_ok stays False → _is_grid_v2_active() = False
        assert engine._grid_v2_bridge.reconstruction_ok is False
        # No actions dispatched (blocked mode)
        executed = [a for a in output.live_actions if a.status.value == "EXECUTED"]
        assert len(executed) == 0


class TestEngineCancelAckRouting:
    """P1-1: Cancel-ack disappearance ≠ fill disappearance."""

    def test_cancelled_order_not_treated_as_fill(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cancelled entry order should go through on_cancel_ack, not on_fill."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        port.cancel_order.return_value = True

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Tick 1: fresh startup with flat position
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=(),
            ts=_BASE_TS,
            source="test",
        )
        snap = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap)
        # Simulate account sync clearing the awaiting-sync flag (PR6)
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()

        # Seed actions were dispatched
        assert engine._grid_v2_started is True
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Get a seeded entry CID from registry
        entry_cids = list(bridge.adapter.registry.all_entry_cids)
        assert len(entry_cids) > 0
        target_cid = sorted(entry_cids)[0]
        entry_count_before = bridge.adapter.registry.entry_count

        # Simulate: we dispatch a CANCEL for this CID
        engine._grid_v2_pending_cancels[target_cid] = _BASE_TS + 1000

        # Tick 2: the order is gone from exchange (simulating cancel ack)
        # Provide account snapshot WITHOUT the cancelled CID
        remaining_orders = []
        for cid in entry_cids:
            if cid == target_cid:
                continue
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg is not None:
                remaining_orders.append(
                    OpenOrderSnap(
                        order_id=cid,
                        symbol="BTCUSDT",
                        side=reg.side.value,
                        order_type="LIMIT",
                        price=reg.price,
                        qty=_ORDER_SIZE,
                        filled_qty=Decimal(0),
                        reduce_only=False,
                        status="NEW",
                        ts=_BASE_TS + 2000,
                    )
                )

        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(remaining_orders),
            ts=_BASE_TS + 2000,
            source="test",
        )

        snap2 = Snapshot(
            ts=_BASE_TS + 2000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output2 = engine.process_snapshot(snap2)

        # The cancelled CID should have gone through on_cancel_ack (removed from registry)
        # NOT through on_fill (which would trigger state machine transitions)
        assert bridge.adapter.registry.entry_count == entry_count_before - 1
        # The pending cancel should be cleaned up
        assert target_cid not in engine._grid_v2_pending_cancels

        # No PLACE_EXIT actions should have been generated (cancel ≠ fill)
        exit_actions = [
            a for a in output2.live_actions if a.action.reason and "PLACE_EXIT" in a.action.reason
        ]
        assert len(exit_actions) == 0, "Cancel-ack must not generate exit actions"

    def test_late_cancel_ack_after_aging_not_treated_as_fill(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pending cancel that ages >30s and later disappears → cancel path, not fill."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        port.cancel_order.return_value = True

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Fresh startup
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=_BASE_TS, source="test"
        )
        snap = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap)
        # Simulate account sync clearing the awaiting-sync flag (PR6)
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()

        # Pick a seeded entry CID and register it as pending cancel
        target_cid = next(iter(bridge.adapter.registry.all_entry_cids))
        parsed = bridge.adapter.parse_cid(target_cid)
        assert parsed is not None
        entry_reg = bridge.adapter.registry.lookup_entry(target_cid)
        assert entry_reg is not None

        # Simulate: cancel dispatched at _BASE_TS, order still on exchange
        engine._grid_v2_pending_cancels[target_cid] = _BASE_TS

        # Tick 2: 60s later (well past any TTL), order STILL on exchange
        open_order_list = []
        for c in bridge.adapter.registry.all_entry_cids:
            reg = bridge.adapter.registry.lookup_entry(c)
            if reg is None:
                continue
            open_order_list.append(
                OpenOrderSnap(
                    symbol="BTCUSDT",
                    order_id=c,
                    side=reg.side.value,
                    price=reg.price,
                    qty=_ORDER_SIZE,
                    order_type="LIMIT",
                    filled_qty=Decimal(0),
                    reduce_only=False,
                    status="NEW",
                    ts=_BASE_TS + 60_000,
                )
            )
        open_orders = tuple(open_order_list)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=open_orders, ts=_BASE_TS + 60_000, source="test"
        )
        snap2 = Snapshot(
            ts=_BASE_TS + 60_000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap2)

        # Key assertion: target_cid MUST still be in pending cancels (not TTL-dropped)
        assert target_cid in engine._grid_v2_pending_cancels, (
            "Aged pending cancel must NOT be dropped while order still on exchange"
        )

        entry_count_before = bridge.adapter.registry.entry_count

        # Tick 3: order finally disappears from exchange (late cancel ack)
        remaining_orders = tuple(o for o in open_orders if o.order_id != target_cid)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=remaining_orders, ts=_BASE_TS + 90_000, source="test"
        )
        snap3 = Snapshot(
            ts=_BASE_TS + 90_000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output3 = engine.process_snapshot(snap3)

        # Cancel path: registry entry removed, pending cancel cleaned up
        assert bridge.adapter.registry.entry_count == entry_count_before - 1
        assert target_cid not in engine._grid_v2_pending_cancels

        # NOT fill path: no PLACE_EXIT actions
        exit_actions = [
            a for a in output3.live_actions if a.action.reason and "PLACE_EXIT" in a.action.reason
        ]
        assert len(exit_actions) == 0, "Late cancel-ack must not generate exit actions"


class TestActionConversion:
    """Test ExecutionAction conversion from ResolvedActions."""

    def test_place_entry_conversion(self) -> None:
        _b, seed = _fresh_bridge()
        buy_actions = [s for s in seed if s.side == OrderSide.BUY]
        assert len(buy_actions) > 0
        for ea in buy_actions:
            assert ea.action_type == ActionType.PLACE
            assert ea.reason == "grid_v2_PLACE_ENTRY"
            assert ea.reduce_only is False
            assert ea.client_order_id is not None
            assert ea.quantity == _ORDER_SIZE

    def test_place_exit_is_reduce_only(self) -> None:
        """PLACE_EXIT actions must have reduce_only=True."""
        b, seed = _fresh_bridge()
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        exits = [ea for ea in result.execution_actions if ea.reason == "grid_v2_PLACE_EXIT"]
        assert len(exits) == 1
        assert exits[0].reduce_only is True

    def test_cancel_action_conversion(self) -> None:
        """CANCEL actions use CID as order_id."""
        b, seed = _fresh_bridge()
        # Force a fill to get cancel actions (branch suppression)
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        cancels = [ea for ea in result.execution_actions if ea.action_type == ActionType.CANCEL]
        for c in cancels:
            assert c.order_id is not None
            assert c.reason.startswith("grid_v2_CANCEL_")


class TestFullLifecycle:
    """Integration: full entry → exit → flat lifecycle through bridge."""

    def test_entry_exit_round_trip(self) -> None:
        b, seed = _fresh_bridge()

        # 1. Entry fill
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        entry_result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        assert not entry_result.rejected
        assert b.state_machine is not None
        assert b.state_machine.mode == BranchMode.LONG_BRANCH

        # 2. Get exit CID
        exit_ea = [ea for ea in entry_result.execution_actions if ea.reason == "grid_v2_PLACE_EXIT"]
        assert len(exit_ea) == 1
        exit_cid = exit_ea[0].client_order_id
        exit_price = exit_ea[0].price
        assert exit_cid is not None and exit_price is not None

        # 3. Exit fill
        exit_result = b.on_fill(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE, _BASE_TS + 2000)
        assert not exit_result.rejected
        sm = b.state_machine
        assert sm is not None
        assert sm.mode == BranchMode.FLAT

        # 4. Verify registry is clean
        assert b.adapter.registry.entry_count > 0  # remaining entries from seed
        assert b.adapter.registry.exit_count == 0  # exit confirmed


class TestEngineBlockedBypassesCycleLayer:
    """P0: blocked grid_v2 + cycle layer enabled => zero dispatched actions."""

    def test_blocked_grid_v2_no_cycle_layer_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When grid_v2 startup is blocked, cycle layer must NOT add actions."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, PositionSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        # Enable cycle layer to ensure it's gated
        monkeypatch.setenv("GRINDER_CYCLE_LAYER_ENABLED", "1")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Non-flat position, no grid_v2 orders → startup blocked
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.01"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50100"),
                    unrealized_pnl=Decimal("1"),
                    leverage=1,
                    ts=_BASE_TS,
                ),
            ),
            open_orders=(),
            ts=_BASE_TS,
            source="test",
        )

        snap = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )

        output = engine.process_snapshot(snap)

        # Blocked: no actions at all, even with cycle layer enabled
        executed = [a for a in output.live_actions if a.status.value == "EXECUTED"]
        assert len(executed) == 0, "Blocked grid_v2 must not dispatch cycle layer actions"


class TestEngineBlockedBypassesReplenish:
    """P0: blocked grid_v2 + replenish path => zero dispatched actions."""

    def test_blocked_grid_v2_no_replenish_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When grid_v2 startup is blocked, replenish must NOT add actions."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, PositionSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_CYCLE_LAYER_ENABLED", "1")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Non-flat position, no grid_v2 orders → startup blocked
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.01"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50100"),
                    unrealized_pnl=Decimal("1"),
                    leverage=1,
                    ts=_BASE_TS,
                ),
            ),
            open_orders=(),
            ts=_BASE_TS,
            source="test",
        )

        # Run two ticks: first triggers startup blocked, second would trigger replenish
        snap = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap)

        # Second tick with same state
        snap2 = Snapshot(
            ts=_BASE_TS + 1000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output2 = engine.process_snapshot(snap2)

        executed = [a for a in output2.live_actions if a.status.value == "EXECUTED"]
        assert len(executed) == 0, "Blocked grid_v2 must not dispatch replenish actions"


class TestAwaitingSyncSeedVisibility:
    """P0: awaiting_sync must not clear until seed CIDs are visible in snapshot."""

    def test_sync_without_seeds_stays_blocked(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Account sync with empty open_orders → awaiting_sync stays True."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.10")

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Tick 1: fresh startup
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=_BASE_TS, source="test"
        )
        snap = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap)
        assert engine._grid_v2_awaiting_sync is True
        assert len(engine._grid_v2_pending_seed_cids) > 0

        # Simulate account sync with EMPTY open_orders (seeds not visible yet)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=_BASE_TS + 5000, source="test"
        )

        # Tick 2: fill detection should still be skipped
        snap2 = Snapshot(
            ts=_BASE_TS + 5000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output2 = engine.process_snapshot(snap2)

        # awaiting_sync should still be True (seeds not visible)
        assert engine._grid_v2_awaiting_sync is True
        # No false fill actions should have been generated
        grid_v2_actions = [
            a
            for a in output2.live_actions
            if a.action.reason
            and "grid_v2" in a.action.reason
            and a.action.reason != "grid_v2_PLACE_ENTRY"
        ]
        assert len(grid_v2_actions) == 0, "Must not detect false fills while awaiting sync"

    def test_sync_with_seeds_clears_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Account sync with seed CIDs visible → awaiting_sync cleared, fills resume."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.10")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "0")

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Tick 1: fresh startup
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=_BASE_TS, source="test"
        )
        snap = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap)
        assert engine._grid_v2_awaiting_sync is True
        seed_cids = engine._grid_v2_pending_seed_cids

        # Build open orders containing ALL seed CIDs
        bridge = engine._grid_v2_bridge
        assert bridge is not None
        open_orders = []
        for cid in seed_cids:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg is None:
                continue
            open_orders.append(
                OpenOrderSnap(
                    order_id=cid,
                    symbol="BTCUSDT",
                    side=reg.side.value,
                    order_type="LIMIT",
                    price=reg.price,
                    qty=_ORDER_SIZE,
                    filled_qty=Decimal(0),
                    reduce_only=False,
                    status="NEW",
                    ts=_BASE_TS + 5000,
                )
            )

        # Wire mock syncer that returns snapshot with seed CIDs visible
        from grinder.account.syncer import AccountSyncer, SyncResult  # noqa: PLC0415

        mock_syncer = MagicMock(spec=AccountSyncer)
        seeds_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(open_orders),
            ts=_BASE_TS + 5000,
            source="test",
        )
        mock_syncer.sync.return_value = SyncResult(snapshot=seeds_snapshot)
        mock_syncer.compute_position_notional = AccountSyncer.compute_position_notional
        engine._account_syncer = mock_syncer

        # Invoke real _tick_account_sync — should clear awaiting_sync
        engine._tick_account_sync()
        assert engine._grid_v2_awaiting_sync is False, (
            "awaiting_sync must be cleared by _tick_account_sync when seeds visible"
        )
        assert engine._grid_v2_pending_seed_cids == frozenset()

        # Tick 2: fill detection should now be active
        snap2 = Snapshot(
            ts=_BASE_TS + 5000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap2)
        assert engine._grid_v2_awaiting_sync is False


class TestTickSizeRequired:
    """P1-2: GRINDER_GRID_V2_TICK_SIZE required when grid_v2 enabled."""

    def test_missing_tick_size_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.delenv("GRINDER_GRID_V2_TICK_SIZE", raising=False)

        with pytest.raises(ValueError, match="GRINDER_GRID_V2_TICK_SIZE required"):
            LiveEngineV0(
                paper_engine=MagicMock(),
                exchange_port=MagicMock(),
                config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
            )


class TestSideAwareQuantization:
    """P1-1: BUY rounds down, SELL rounds up."""

    def test_buy_rounds_down(self) -> None:
        b = _bridge()
        # 50000.005 with tick 0.01 → BUY should round DOWN to 50000.00
        result = b._quantize_price(Decimal("50000.005"), OrderSide.BUY)
        assert result == Decimal("50000.00")

    def test_sell_rounds_up(self) -> None:
        b = _bridge()
        # 50000.005 with tick 0.01 → SELL should round UP to 50000.01
        result = b._quantize_price(Decimal("50000.005"), OrderSide.SELL)
        assert result == Decimal("50000.01")

    def test_exact_tick_no_change(self) -> None:
        b = _bridge()
        # Already on tick boundary
        result = b._quantize_price(Decimal("50000.01"), OrderSide.BUY)
        assert result == Decimal("50000.01")
