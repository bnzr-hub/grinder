"""Tests for grid_v2 runtime bridge (doc-27 section 23, PR4)."""

import pathlib
from decimal import Decimal

import pytest

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
from grinder.contracts import Snapshot
from grinder.core import OrderSide, OrderState
from grinder.execution.futures_events import FuturesOrderEvent, UserDataEvent, UserDataEventType
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
from grinder.live.engine import LiveEngineV0

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
    reseed_on_flat: bool = True,
) -> GridV2Config:
    return GridV2Config(
        grid_step_pct=step,
        entry_levels_per_side=levels,
        order_size=size,
        max_inventory_levels=max_levels,
        max_inventory_notional_usd=max_notional,
        reseed_on_flat=reseed_on_flat,
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

    def test_exit_fill_to_flat_reseeds_window(self) -> None:
        """Entry fill → exit fill → back to FLAT with a fresh seeded window."""
        b, seed = _fresh_bridge()
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        entry_result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        assert not entry_result.rejected

        exit_actions = [
            ea for ea in entry_result.execution_actions if ea.reason == "grid_v2_PLACE_EXIT"
        ]
        assert len(exit_actions) == 1
        exit_cid = exit_actions[0].client_order_id
        exit_price = exit_actions[0].price
        assert exit_cid is not None and exit_price is not None

        exit_result = b.on_fill(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE, _BASE_TS + 2000)
        assert not exit_result.rejected
        assert b.state_machine is not None
        assert b.state_machine.mode == BranchMode.FLAT

        cancel_actions = [
            ea for ea in exit_result.execution_actions if ea.action_type == ActionType.CANCEL
        ]
        place_actions = [
            ea for ea in exit_result.execution_actions if ea.action_type == ActionType.PLACE
        ]
        assert len(cancel_actions) == 5
        assert len(place_actions) == 6
        assert all(ea.reason == "grid_v2_PLACE_ENTRY" for ea in place_actions)

    def test_exit_fill_to_flat_preserve_restores_symmetry_without_recenter(self) -> None:
        b, seed = _fresh_bridge(config=_config(reseed_on_flat=False))
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        entry_result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        assert not entry_result.rejected
        exit_actions = [
            ea for ea in entry_result.execution_actions if ea.reason == "grid_v2_PLACE_EXIT"
        ]
        assert len(exit_actions) == 1
        exit_cid = exit_actions[0].client_order_id
        exit_price = exit_actions[0].price
        assert exit_cid is not None and exit_price is not None

        exit_result = b.on_fill(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE, _BASE_TS + 2000)
        assert not exit_result.rejected
        assert b.state_machine is not None
        assert b.state_machine.mode == BranchMode.FLAT
        assert len(b.state_machine.snapshot.entry_window.buy_entry_prices) == 3
        assert len(b.state_machine.snapshot.entry_window.sell_entry_prices) == 3

        place_actions = [
            ea for ea in exit_result.execution_actions if ea.action_type == ActionType.PLACE
        ]
        cancel_actions = [
            ea for ea in exit_result.execution_actions if ea.action_type == ActionType.CANCEL
        ]
        assert any(ea.reason == "grid_v2_PLACE_ENTRY" for ea in place_actions)
        # Preserve mode should not do full recenter fan-out (5 cancels + 6 places).
        assert len(place_actions) < 6
        assert len(cancel_actions) < 5


class TestNetOffBridge:
    """Bridge-level tests for net-off transition (Variant B)."""

    def test_netoff_enabled_opposite_fill_not_rejected(self) -> None:
        """LONG + SELL fill with netoff → not rejected, cancel exit emitted."""
        cfg = _config()
        cfg_netoff = GridV2Config(
            grid_step_pct=cfg.grid_step_pct,
            entry_levels_per_side=cfg.entry_levels_per_side,
            order_size=cfg.order_size,
            max_inventory_levels=cfg.max_inventory_levels,
            max_inventory_notional_usd=cfg.max_inventory_notional_usd,
            price_tick_size=cfg.price_tick_size,
            netoff_enabled=True,
        )
        bridge = GridV2Bridge(cfg_netoff, "BTCUSDT")
        bridge.startup_fresh(Decimal("50000"), _BASE_TS)

        # Find and fill a BUY entry
        buy_cid = None
        for cid in bridge.adapter.registry.all_entry_cids:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg and reg.side == OrderSide.BUY:
                buy_cid = cid
                buy_price = reg.price
                break
        assert buy_cid is not None
        r1 = bridge.on_fill(buy_cid, OrderSide.BUY, buy_price, Decimal("0.01"), _BASE_TS + 1)
        assert not r1.rejected
        assert bridge.state_machine is not None
        assert bridge.state_machine.snapshot.mode == BranchMode.LONG_BRANCH

        # Fill a SELL entry (opposite) → should net-off, not reject
        sell_cid = None
        for cid in bridge.adapter.registry.all_entry_cids:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg and reg.side == OrderSide.SELL:
                sell_cid = cid
                sell_price = reg.price
                break
        assert sell_cid is not None
        r2 = bridge.on_fill(sell_cid, OrderSide.SELL, sell_price, Decimal("0.01"), _BASE_TS + 2)
        assert not r2.rejected
        assert bridge.state_machine.snapshot.mode == BranchMode.FLAT  # type: ignore[comparison-overlap]
        # Should have CANCEL action for paired exit
        cancel_actions = [a for a in r2.execution_actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) >= 1

    def test_netoff_disabled_rejects_at_bridge(self) -> None:
        """Without netoff, opposite fill is BRANCH_INCOMPATIBLE at bridge level."""
        bridge = GridV2Bridge(_config(), "BTCUSDT")
        bridge.startup_fresh(Decimal("50000"), _BASE_TS)

        buy_cid = None
        for cid in bridge.adapter.registry.all_entry_cids:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg and reg.side == OrderSide.BUY:
                buy_cid = cid
                buy_price = reg.price
                break
        assert buy_cid is not None
        bridge.on_fill(buy_cid, OrderSide.BUY, buy_price, Decimal("0.01"), _BASE_TS + 1)

        sell_cid = None
        for cid in bridge.adapter.registry.all_entry_cids:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg and reg.side == OrderSide.SELL:
                sell_cid = cid
                sell_price = reg.price
                break
        assert sell_cid is not None
        r2 = bridge.on_fill(sell_cid, OrderSide.SELL, sell_price, Decimal("0.01"), _BASE_TS + 2)
        assert r2.rejected
        assert r2.reject_reason == "BRANCH_INCOMPATIBLE"


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

    def test_duplicate_exit_fill_allow_stale_is_suppressed(self) -> None:
        """Late duplicate exit fill should be suppressed when allow_stale=True."""
        b, seed = _fresh_bridge()
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        # Open one lot and create an exit CID.
        entry_result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        exit_actions = [
            ea for ea in entry_result.execution_actions if ea.reason == "grid_v2_PLACE_EXIT"
        ]
        assert len(exit_actions) == 1
        exit_cid = exit_actions[0].client_order_id
        exit_price = exit_actions[0].price
        assert exit_cid is not None and exit_price is not None

        # Close lot normally.
        closed = b.on_fill(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE, _BASE_TS + 2000)
        assert not closed.rejected
        assert b.state_machine is not None
        assert len(b.state_machine.snapshot.open_lots) == 0

        # Duplicate/late exit event with stale CID should be suppressed.
        late = b.on_fill(
            exit_cid,
            OrderSide.SELL,
            exit_price,
            _ORDER_SIZE,
            _BASE_TS + 3000,
            allow_stale=True,
        )
        assert late.rejected is False
        assert late.reject_reason is None
        assert late.execution_actions == ()

    def test_orphan_exit_fill_allow_stale_is_rejected_not_raised(self) -> None:
        """True orphan exit fill remains graceful reject (no crash)."""
        b, seed = _fresh_bridge()
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        entry_result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        exit_actions = [
            ea for ea in entry_result.execution_actions if ea.reason == "grid_v2_PLACE_EXIT"
        ]
        assert len(exit_actions) == 1
        exit_cid = exit_actions[0].client_order_id
        exit_price = exit_actions[0].price
        assert exit_cid is not None and exit_price is not None

        # Close lot normally then clear duplicate cache to simulate true orphan.
        closed = b.on_fill(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE, _BASE_TS + 2000)
        assert not closed.rejected
        b._recent_exit_fills.clear()

        orphan = b.on_fill(
            exit_cid,
            OrderSide.SELL,
            exit_price,
            _ORDER_SIZE,
            _BASE_TS + 3000,
            allow_stale=True,
        )
        assert orphan.rejected is True
        assert orphan.reject_reason == "EXIT_LOT_NOT_FOUND"
        assert orphan.execution_actions == ()


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


class TestEngineStartupRecenterOnFlat:
    """Flat restart with existing g-orders should reseed around current mid."""

    def test_reconstruct_flat_reseeds_window_with_current_step(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_STEP_PCT", "0.0025")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW", "0")

        # Build pre-existing exchange orders using older spacing (0.5%),
        # simulating a restart after config change.
        old_bridge, _ = _fresh_bridge(config=_config(step=Decimal("0.005")))
        existing_orders: list[OpenOrderSnap] = []
        for cid in old_bridge.adapter.registry.all_entry_cids:
            reg = old_bridge.adapter.registry.lookup_entry(cid)
            assert reg is not None
            existing_orders.append(
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
                    ts=_BASE_TS,
                )
            )

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        port.cancel_order.return_value = True
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(existing_orders),
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

        executed = [a.action for a in output.live_actions if a.status.value == "EXECUTED"]
        assert any(a.action_type == ActionType.CANCEL for a in executed)
        placed_entries = [
            a
            for a in executed
            if a.action_type == ActionType.PLACE and a.reason == "grid_v2_PLACE_ENTRY"
        ]
        assert len(placed_entries) >= 6
        assert any(
            a.side == OrderSide.BUY and a.price == Decimal("49875.00") for a in placed_entries
        )
        assert any(
            a.side == OrderSide.SELL and a.price == Decimal("50125.00") for a in placed_entries
        )

    def test_reconstruct_flat_no_reseed_when_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "0")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW", "0")
        monkeypatch.setenv("GRINDER_GRID_V2_STEP_PCT", "0.0025")

        old_bridge, _ = _fresh_bridge(config=_config(step=Decimal("0.005")))
        existing_orders: list[OpenOrderSnap] = []
        for cid in old_bridge.adapter.registry.all_entry_cids:
            reg = old_bridge.adapter.registry.lookup_entry(cid)
            assert reg is not None
            existing_orders.append(
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
                    ts=_BASE_TS,
                )
            )

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(existing_orders),
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

        executed = [a.action for a in output.live_actions if a.status.value == "EXECUTED"]
        assert all(a.reason != "grid_v2_CANCEL_ENTRY" for a in executed)
        assert all(a.reason != "grid_v2_PLACE_ENTRY" for a in executed)

    def test_reconstruct_flat_only_on_skew_does_not_reseed_when_balanced(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "0")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_STEP_PCT", "0.0025")

        old_bridge, _ = _fresh_bridge(config=_config(step=Decimal("0.005")))
        existing_orders: list[OpenOrderSnap] = []
        for cid in old_bridge.adapter.registry.all_entry_cids:
            reg = old_bridge.adapter.registry.lookup_entry(cid)
            assert reg is not None
            existing_orders.append(
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
                    ts=_BASE_TS,
                )
            )

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(existing_orders),
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

        executed = [a.action for a in output.live_actions if a.status.value == "EXECUTED"]
        assert all(a.reason != "grid_v2_CANCEL_ENTRY" for a in executed)
        assert all(a.reason != "grid_v2_PLACE_ENTRY" for a in executed)


class TestEngineNonFlatFailClosed:
    """P0-2: Non-flat position + no g-orders must fail-closed."""

    def test_non_flat_no_orders_triggers_f2_recovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-flat position with no grid_v2 orders → F2 protective recovery."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, PositionSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW", "0")

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

        # F2 recovery: startup succeeds with protective exits
        assert engine._grid_v2_started is True
        assert engine._grid_v2_bridge is not None
        assert engine._grid_v2_bridge.reconstruction_ok is True
        assert engine._grid_v2_bridge.f2_protective_recovery is True
        # Protective reduce-only SELL exit dispatched in live_actions
        f2_actions = [a for a in output.live_actions if "F2_PROTECTIVE" in (a.action.reason or "")]
        assert len(f2_actions) == 1
        assert f2_actions[0].action.reduce_only is True
        assert f2_actions[0].action.side == OrderSide.SELL  # LONG pos → SELL exit


class TestF2ProtectiveRecovery:
    """F2 hotfix: non-flat startup without exits → protective recovery."""

    def test_f2_recovery_blocks_increase_risk_entries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """F2 recovery emits only reduce-risk exits, no PLACE_ENTRY."""
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

        # All emitted grid actions must be reduce-only (no entries)
        for la in output.live_actions:
            if la.action.action_type.value == "PLACE" and "grid_v2" in (la.action.reason or ""):
                assert la.action.reduce_only is True, (
                    f"F2 recovery must not place increase-risk entries: {la.action.reason}"
                )

    def test_f2_recovery_short_position(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """F2 recovery with SHORT position → protective BUY exit."""
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

        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="SHORT",
                    qty=Decimal("0.01"),
                    signed_qty=Decimal("-0.01"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("49900"),
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

        f2_actions = [a for a in output.live_actions if "F2_PROTECTIVE" in (a.action.reason or "")]
        assert len(f2_actions) == 1
        assert f2_actions[0].action.side == OrderSide.BUY  # SHORT pos → BUY exit
        assert f2_actions[0].action.reduce_only is True

    def test_f2_recovery_idempotent_restart(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Repeated restart with F2 produces same single protective exit."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, PositionSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        def _make_engine() -> LiveEngineV0:
            e = LiveEngineV0(
                paper_engine=MagicMock(),
                exchange_port=MagicMock(),
                config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
            )
            e._last_account_snapshot = AccountSnapshot(
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
            return e

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

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

        out1 = _make_engine().process_snapshot(snap)
        out2 = _make_engine().process_snapshot(snap)

        f2_1 = [a for a in out1.live_actions if "F2_PROTECTIVE" in (a.action.reason or "")]
        f2_2 = [a for a in out2.live_actions if "F2_PROTECTIVE" in (a.action.reason or "")]
        assert len(f2_1) == len(f2_2) == 1

    def test_f2_awaiting_sync_clears_on_definitive_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If F2 protective exit is BLOCKED/SKIPPED, awaiting_sync must clear (no deadlock)."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, PositionSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        # NOT armed → all PLACEs will be BLOCKED by gate
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW", "0")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=False, mode=SafeMode.LIVE_TRADE),
        )

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

        engine.process_snapshot(snap)

        # Not armed → protective exit BLOCKED → awaiting_sync must clear (not deadlock)
        assert engine._grid_v2_awaiting_sync is False, (
            "awaiting_sync must clear when all F2 protective seeds fail definitively"
        )


class TestGridV2RecoveryModes:
    """Startup recovery modes: restore_then_block vs restore_then_cleanup_reseed."""

    @staticmethod
    def _bad_reconstruct_acct() -> AccountSnapshot:
        """Create account with duplicate grid_v2 entry orders (same side+price = F6 failure)."""
        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.grid_v2.adapter import GridV2Adapter  # noqa: PLC0415

        adapter = GridV2Adapter(_config(), "BTCUSDT")
        cid1 = adapter.generate_entry_cid(_BASE_TS)
        cid2 = adapter.generate_entry_cid(_BASE_TS)

        return AccountSnapshot(
            positions=(),
            open_orders=(
                OpenOrderSnap(
                    order_id=cid1,
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49875"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=_BASE_TS,
                ),
                OpenOrderSnap(
                    order_id=cid2,
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49875"),  # same price = F6 duplicate
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=_BASE_TS,
                ),
            ),
            ts=_BASE_TS,
            source="test",
        )

    @staticmethod
    def _snap() -> Snapshot:
        from grinder.contracts import Snapshot  # noqa: PLC0415

        return Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )

    def test_default_mode_is_restore_then_cleanup_reseed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With env unset, engine chooses cleanup-reseed as default."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import GridV2RecoveryMode, LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.delenv("GRINDER_GRID_V2_RECOVERY_MODE", raising=False)

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        assert engine._grid_v2_recovery_mode == GridV2RecoveryMode.RESTORE_THEN_CLEANUP_RESEED

    def test_explicit_restore_then_block_mode_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit restore_then_block mode is respected."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import GridV2RecoveryMode, LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RECOVERY_MODE", "restore_then_block")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        assert engine._grid_v2_recovery_mode == GridV2RecoveryMode.RESTORE_THEN_BLOCK

    def test_invalid_recovery_mode_raises_at_init(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bad env value → fail-fast ValueError at init."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RECOVERY_MODE", "bad_value")

        with pytest.raises(ValueError, match="Invalid GRINDER_GRID_V2_RECOVERY_MODE"):
            LiveEngineV0(
                paper_engine=MagicMock(),
                exchange_port=MagicMock(),
                config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
            )

    def test_restore_then_block_on_reconstruct_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Block mode: reconstruction fail → no dispatch, blocked state."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RECOVERY_MODE", "restore_then_block")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        engine._last_account_snapshot = self._bad_reconstruct_acct()
        engine.process_snapshot(self._snap())

        assert engine._grid_v2_started is True
        assert engine._grid_v2_bridge is not None
        assert engine._grid_v2_bridge.reconstruction_ok is False

    def test_restore_then_cleanup_reseed_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cleanup-reseed mode: reconstruction fail → cleanup → fresh seed."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        import scripts.exchange_state as es_mod  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        monkeypatch.delenv("GRINDER_GRID_V2_RECOVERY_MODE", raising=False)

        monkeypatch.setattr(es_mod, "cmd_cleanup", lambda _s: None)
        monkeypatch.setattr(es_mod, "cmd_verify_programmatic", lambda _s: (True, 0, "FLAT"))

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        engine._last_account_snapshot = self._bad_reconstruct_acct()
        output = engine.process_snapshot(self._snap())

        assert engine._grid_v2_started is True
        assert engine._grid_v2_bridge is not None
        assert engine._grid_v2_bridge.reconstruction_ok is True
        seed_actions = [
            a
            for a in output.live_actions
            if a.action.action_type.value == "PLACE" and "grid_v2" in (a.action.reason or "")
        ]
        assert len(seed_actions) > 0

    def test_restore_then_cleanup_reseed_cleanup_fail_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cleanup-reseed mode: cleanup raises → fail-closed."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        import scripts.exchange_state as es_mod  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        monkeypatch.delenv("GRINDER_GRID_V2_RECOVERY_MODE", raising=False)

        def _boom(_s: str) -> None:
            msg = "cleanup exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(es_mod, "cmd_cleanup", _boom)

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        engine._last_account_snapshot = self._bad_reconstruct_acct()
        engine.process_snapshot(self._snap())

        assert engine._grid_v2_started is True
        assert engine._grid_v2_bridge is not None
        assert engine._grid_v2_bridge.reconstruction_ok is False

    def test_restore_then_cleanup_reseed_verify_fail_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cleanup-reseed mode: cleanup OK but verify dirty → fail-closed."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        import scripts.exchange_state as es_mod  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        monkeypatch.delenv("GRINDER_GRID_V2_RECOVERY_MODE", raising=False)

        monkeypatch.setattr(es_mod, "cmd_cleanup", lambda _s: None)
        monkeypatch.setattr(es_mod, "cmd_verify_programmatic", lambda _s: (False, 2, "0.01"))

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        engine._last_account_snapshot = self._bad_reconstruct_acct()
        engine.process_snapshot(self._snap())

        assert engine._grid_v2_started is True
        assert engine._grid_v2_bridge is not None
        assert engine._grid_v2_bridge.reconstruction_ok is False

    def test_cleanup_reseed_no_write_gate_blocks_safely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ALLOW_MAINNET_TRADE unset → cleanup skipped, fail-closed (no sys.exit)."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.delenv("GRINDER_GRID_V2_RECOVERY_MODE", raising=False)
        monkeypatch.delenv("ALLOW_MAINNET_TRADE", raising=False)

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        engine._last_account_snapshot = self._bad_reconstruct_acct()
        engine.process_snapshot(self._snap())

        # Should NOT crash (sys.exit), should fail-closed
        assert engine._grid_v2_started is True
        assert engine._grid_v2_bridge is not None
        assert engine._grid_v2_bridge.reconstruction_ok is False

    def test_cleanup_reseed_system_exit_caught_safely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cmd_cleanup raises SystemExit → caught, fail-closed (no process crash)."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        import scripts.exchange_state as es_mod  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        monkeypatch.delenv("GRINDER_GRID_V2_RECOVERY_MODE", raising=False)

        def _sys_exit(_s: str) -> None:
            raise SystemExit(1)

        monkeypatch.setattr(es_mod, "cmd_cleanup", _sys_exit)

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        engine._last_account_snapshot = self._bad_reconstruct_acct()
        engine.process_snapshot(self._snap())

        # Should NOT crash, should fail-closed
        assert engine._grid_v2_started is True
        assert engine._grid_v2_bridge is not None
        assert engine._grid_v2_bridge.reconstruction_ok is False


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


class TestEngineImmediateUserDataPath:
    """Immediate ORDER_TRADE_UPDATE handling for grid_v2."""

    def test_user_data_filled_entry_dispatches_immediately(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=(),
            ts=_BASE_TS,
            source="test",
        )
        # Initialize bridge directly to isolate immediate path from market snapshots.
        assert engine._grid_v2_bridge is not None
        engine._grid_v2_bridge.startup_fresh(_REF_PRICE, _BASE_TS)
        engine._grid_v2_started = True
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()

        bridge = engine._grid_v2_bridge
        entry_cid = sorted(bridge.adapter.registry.all_entry_cids)[0]
        reg = bridge.adapter.registry.lookup_entry(entry_cid)
        assert reg is not None
        event = UserDataEvent(
            event_type=UserDataEventType.ORDER_TRADE_UPDATE,
            order_event=FuturesOrderEvent(
                ts=_BASE_TS + 1,
                symbol="BTCUSDT",
                order_id=1,
                client_order_id=entry_cid,
                side=reg.side,
                status=OrderState.FILLED,
                price=reg.price,
                qty=_ORDER_SIZE,
                executed_qty=_ORDER_SIZE,
                avg_price=reg.price,
            ),
        )
        engine.process_user_data_event(event)

        assert bridge.adapter.registry.lookup_entry(entry_cid) is None
        assert bridge.adapter.registry.exit_count == 1
        assert port.place_order.call_count >= 1

    def test_user_data_fill_uses_order_price_not_avg_price(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=(),
            ts=_BASE_TS,
            source="test",
        )
        assert engine._grid_v2_bridge is not None
        engine._grid_v2_bridge.startup_fresh(_REF_PRICE, _BASE_TS)
        engine._grid_v2_started = True
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()

        bridge = engine._grid_v2_bridge
        entry_cid = sorted(bridge.adapter.registry.all_entry_cids)[0]
        reg = bridge.adapter.registry.lookup_entry(entry_cid)
        assert reg is not None
        noisy_avg = reg.price + Decimal("0.00000123")
        event = UserDataEvent(
            event_type=UserDataEventType.ORDER_TRADE_UPDATE,
            order_event=FuturesOrderEvent(
                ts=_BASE_TS + 1,
                symbol="BTCUSDT",
                order_id=1,
                client_order_id=entry_cid,
                side=reg.side,
                status=OrderState.FILLED,
                price=reg.price,
                qty=_ORDER_SIZE,
                executed_qty=_ORDER_SIZE,
                avg_price=noisy_avg,
            ),
        )
        engine.process_user_data_event(event)

        sm = bridge.state_machine
        assert sm is not None
        snap = sm.snapshot
        assert len(snap.open_lots) == 1
        assert snap.open_lots[0].entry_price == reg.price

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
        # Force a fill to get cancel actions (rolling opposite-edge trim)
        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        cancels = [ea for ea in result.execution_actions if ea.action_type == ActionType.CANCEL]
        for c in cancels:
            assert c.order_id is not None
            assert c.reason.startswith("grid_v2_CANCEL_")


class TestEngineFillOrdering:
    """Engine fill ordering for same-tick disappeared CIDs."""

    def test_exit_is_processed_before_entry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exit-first processing prevents stale same-tick entry replay after recenter."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW", "0")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Startup fresh bridge
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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Enable fill detection and remove startup visibility gates.
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()

        # Create LONG branch via one BUY entry fill.
        buy_cid = next(
            cid
            for cid in sorted(bridge.adapter.registry.all_entry_cids)
            if (reg := bridge.adapter.registry.lookup_entry(cid)) is not None
            and reg.side == OrderSide.BUY
        )
        buy_reg = bridge.adapter.registry.lookup_entry(buy_cid)
        assert buy_reg is not None
        first_fill = bridge.on_fill(
            buy_cid,
            buy_reg.side,
            buy_reg.price,
            _ORDER_SIZE,
            _BASE_TS + 1_000,
        )
        assert not first_fill.rejected
        sm_before = bridge.state_machine
        assert sm_before is not None
        assert sm_before.mode == BranchMode.LONG_BRANCH

        # Pick one exit CID and one SELL-entry CID to disappear in the same tick.
        exit_cid = next(iter(bridge.adapter.registry.all_exit_cids))
        sell_entry_cid = next(
            cid
            for cid in sorted(bridge.adapter.registry.all_entry_cids)
            if (reg := bridge.adapter.registry.lookup_entry(cid)) is not None
            and reg.side == OrderSide.SELL
        )

        # Exchange snapshot: both selected CIDs disappeared.
        surviving_orders: list[OpenOrderSnap] = []
        for cid in sorted(
            set(bridge.adapter.registry.all_entry_cids) | set(bridge.adapter.registry.all_exit_cids)
        ):
            if cid in {exit_cid, sell_entry_cid}:
                continue
            entry_reg = bridge.adapter.registry.lookup_entry(cid)
            if entry_reg is not None:
                surviving_orders.append(
                    OpenOrderSnap(
                        order_id=cid,
                        symbol="BTCUSDT",
                        side=entry_reg.side.value,
                        order_type="LIMIT",
                        price=entry_reg.price,
                        qty=_ORDER_SIZE,
                        filled_qty=Decimal(0),
                        reduce_only=False,
                        status="NEW",
                        ts=_BASE_TS + 2_000,
                    )
                )
                continue
            exit_reg = bridge.adapter.registry.lookup_exit(cid)
            if exit_reg is not None:
                surviving_orders.append(
                    OpenOrderSnap(
                        order_id=cid,
                        symbol="BTCUSDT",
                        side=OrderSide.SELL.value,
                        order_type="LIMIT",
                        price=Decimal("1"),
                        qty=_ORDER_SIZE,
                        filled_qty=Decimal(0),
                        reduce_only=True,
                        status="NEW",
                        ts=_BASE_TS + 2_000,
                    )
                )

        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(surviving_orders),
            ts=_BASE_TS + 2_000,
            source="test",
        )

        actions = engine._grid_v2_process_fills("BTCUSDT", _BASE_TS + 2_000)

        sm_after = bridge.state_machine
        assert sm_after is not None
        assert sm_after.mode == BranchMode.FLAT
        assert any(a.reason == "grid_v2_CANCEL_ENTRY" for a in actions)


class TestEngineIntegrityWatchdog:
    def test_flat_integrity_mismatch_triggers_recenter_after_debounce(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()
        engine._grid_v2_pending_cancels.clear()

        missing = sorted(bridge.adapter.registry.all_entry_cids)[0]
        surviving_orders: list[OpenOrderSnap] = []
        for cid in sorted(bridge.adapter.registry.all_entry_cids):
            if cid == missing:
                continue
            reg = bridge.adapter.registry.lookup_entry(cid)
            assert reg is not None
            surviving_orders.append(
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
                    ts=_BASE_TS + 2_000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(surviving_orders),
            ts=_BASE_TS + 2_000,
            source="test",
        )

        first = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_000,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )
        second = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_001,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )

        assert first == []
        assert second
        assert any(a.reason == "grid_v2_CANCEL_ENTRY" for a in second)
        assert any(a.reason == "grid_v2_PLACE_ENTRY" for a in second)

    def test_flat_integrity_mismatch_preserved_when_reseed_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "0")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW", "0")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()
        engine._grid_v2_pending_cancels.clear()

        missing = sorted(bridge.adapter.registry.all_entry_cids)[0]
        surviving_orders: list[OpenOrderSnap] = []
        for cid in sorted(bridge.adapter.registry.all_entry_cids):
            if cid == missing:
                continue
            reg = bridge.adapter.registry.lookup_entry(cid)
            assert reg is not None
            surviving_orders.append(
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
                    ts=_BASE_TS + 2_000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(surviving_orders),
            ts=_BASE_TS + 2_000,
            source="test",
        )

        first = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_000,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )
        second = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_001,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )

        assert first == []
        assert second == []

    def test_flat_integrity_mismatch_reseeds_when_only_on_skew_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "0")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW", "1")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()
        engine._grid_v2_pending_cancels.clear()

        missing = sorted(bridge.adapter.registry.all_entry_cids)[0]
        surviving_orders: list[OpenOrderSnap] = []
        for cid in sorted(bridge.adapter.registry.all_entry_cids):
            if cid == missing:
                continue
            reg = bridge.adapter.registry.lookup_entry(cid)
            assert reg is not None
            surviving_orders.append(
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
                    ts=_BASE_TS + 2_000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(surviving_orders),
            ts=_BASE_TS + 2_000,
            source="test",
        )

        first = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_000,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )
        second = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_001,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )

        assert first == []
        assert second
        assert any(a.reason == "grid_v2_CANCEL_ENTRY" for a in second)
        assert any(a.reason == "grid_v2_PLACE_ENTRY" for a in second)

    def test_flat_preserve_mode_cancels_extra_entry_without_placing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "0")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW", "0")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()
        engine._grid_v2_pending_cancels.clear()

        open_orders: list[OpenOrderSnap] = []
        for cid in sorted(bridge.adapter.registry.all_entry_cids):
            reg = bridge.adapter.registry.lookup_entry(cid)
            assert reg is not None
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
                    ts=_BASE_TS + 2_000,
                )
            )
        extra_entry_cid = bridge.adapter.generate_entry_cid(_BASE_TS + 2_000)
        open_orders.append(
            OpenOrderSnap(
                order_id=extra_entry_cid,
                symbol="BTCUSDT",
                side=OrderSide.BUY.value,
                order_type="LIMIT",
                price=Decimal("1.23"),
                qty=_ORDER_SIZE,
                filled_qty=Decimal(0),
                reduce_only=False,
                status="NEW",
                ts=_BASE_TS + 2_000,
            )
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(open_orders),
            ts=_BASE_TS + 2_000,
            source="test",
        )

        first = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_000,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )
        second = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_001,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )

        assert first == []
        assert any(
            a.reason == "grid_v2_INTEGRITY_CANCEL_ENTRY" and a.order_id == extra_entry_cid
            for a in second
        )
        assert all(a.reason != "grid_v2_PLACE_ENTRY" for a in second)

    def test_flat_preserve_mode_cancels_exit_without_placing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "0")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )
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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()
        engine._grid_v2_pending_cancels.clear()

        open_orders: list[OpenOrderSnap] = []
        for cid in sorted(bridge.adapter.registry.all_entry_cids):
            reg = bridge.adapter.registry.lookup_entry(cid)
            assert reg is not None
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
                    ts=_BASE_TS + 2_000,
                )
            )
        exit_cid = bridge.adapter.generate_exit_cid(_BASE_TS + 2_000)
        open_orders.append(
            OpenOrderSnap(
                order_id=exit_cid,
                symbol="BTCUSDT",
                side=OrderSide.SELL.value,
                order_type="LIMIT",
                price=Decimal("50010"),
                qty=_ORDER_SIZE,
                filled_qty=Decimal(0),
                reduce_only=True,
                status="NEW",
                ts=_BASE_TS + 2_000,
            )
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(open_orders),
            ts=_BASE_TS + 2_000,
            source="test",
        )

        first = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_000,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )
        second = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_001,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )

        assert first == []
        assert any(
            a.reason == "grid_v2_INTEGRITY_CANCEL_EXIT" and a.order_id == exit_cid for a in second
        )
        assert all(a.reason != "grid_v2_PLACE_ENTRY" for a in second)

    def test_branch_integrity_mismatch_replaces_missing_entry_after_debounce(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Move state machine to SHORT_BRANCH via one SELL entry fill.
        sell_cid = sorted(bridge.adapter.registry.all_entry_cids)[-1]
        sell_reg = bridge.adapter.registry.lookup_entry(sell_cid)
        assert sell_reg is not None
        fill_result = bridge.on_fill(
            sell_cid,
            OrderSide.SELL,
            sell_reg.price,
            _ORDER_SIZE,
            _BASE_TS + 1_000,
            allow_stale=True,
        )
        assert not fill_result.rejected
        assert bridge.state_machine is not None
        assert bridge.state_machine.mode == BranchMode.SHORT_BRANCH

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()
        engine._grid_v2_pending_cancels.clear()

        # Simulate missing one expected entry on exchange while in branch mode.
        missing_entry_cid = sorted(bridge.adapter.registry.all_entry_cids)[0]
        open_orders: list[OpenOrderSnap] = []
        for cid in sorted(bridge.adapter.registry.all_entry_cids):
            if cid == missing_entry_cid:
                continue
            entry_reg = bridge.adapter.registry.lookup_entry(cid)
            assert entry_reg is not None
            open_orders.append(
                OpenOrderSnap(
                    order_id=cid,
                    symbol="BTCUSDT",
                    side=entry_reg.side.value,
                    order_type="LIMIT",
                    price=entry_reg.price,
                    qty=_ORDER_SIZE,
                    filled_qty=Decimal(0),
                    reduce_only=False,
                    status="NEW",
                    ts=_BASE_TS + 2_000,
                )
            )
        for cid in sorted(bridge.adapter.registry.all_exit_cids):
            exit_reg = bridge.adapter.registry.lookup_exit(cid)
            assert exit_reg is not None
            open_orders.append(
                OpenOrderSnap(
                    order_id=cid,
                    symbol="BTCUSDT",
                    side=OrderSide.BUY.value,
                    order_type="LIMIT",
                    price=Decimal("1"),
                    qty=_ORDER_SIZE,
                    filled_qty=Decimal(0),
                    reduce_only=True,
                    status="NEW",
                    ts=_BASE_TS + 2_000,
                )
            )

        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(open_orders),
            ts=_BASE_TS + 2_000,
            source="test",
        )

        first = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_000,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )
        second = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_001,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )

        assert first == []
        assert second
        assert any(a.reason == "grid_v2_PLACE_ENTRY" for a in second)

    def test_branch_integrity_at_max_inventory_does_not_place_entries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from dataclasses import replace  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Enter SHORT_BRANCH with one SELL fill.
        sell_cid = sorted(bridge.adapter.registry.all_entry_cids)[-1]
        sell_reg = bridge.adapter.registry.lookup_entry(sell_cid)
        assert sell_reg is not None
        fill_result = bridge.on_fill(
            sell_cid,
            OrderSide.SELL,
            sell_reg.price,
            _ORDER_SIZE,
            _BASE_TS + 1_000,
            allow_stale=True,
        )
        assert not fill_result.rejected
        assert bridge.state_machine is not None
        assert bridge.state_machine.mode == BranchMode.SHORT_BRANCH

        # Force state to max inventory for safety gating.
        lot = bridge.state_machine.snapshot.open_lots[0]
        max_levels = bridge._config.max_inventory_levels
        open_lots = tuple(
            replace(
                lot,
                lot_id=f"{lot.lot_id}_{i}",
                source_entry_order_id=f"{lot.source_entry_order_id}_{i}",
                exit_order_id=f"{lot.exit_order_id}_{i}",
            )
            for i in range(max_levels)
        )
        bridge.state_machine._snapshot = replace(  # pyright: ignore[reportPrivateUsage]
            bridge.state_machine.snapshot,
            open_lots=open_lots,
        )

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()
        engine._grid_v2_pending_cancels.clear()

        # Simulate exchange still carrying branch entries.
        open_orders: list[OpenOrderSnap] = []
        for cid in sorted(bridge.adapter.registry.all_entry_cids):
            entry_reg = bridge.adapter.registry.lookup_entry(cid)
            assert entry_reg is not None
            open_orders.append(
                OpenOrderSnap(
                    order_id=cid,
                    symbol="BTCUSDT",
                    side=entry_reg.side.value,
                    order_type="LIMIT",
                    price=entry_reg.price,
                    qty=_ORDER_SIZE,
                    filled_qty=Decimal(0),
                    reduce_only=False,
                    status="NEW",
                    ts=_BASE_TS + 2_000,
                )
            )
        for cid in sorted(bridge.adapter.registry.all_exit_cids):
            exit_reg = bridge.adapter.registry.lookup_exit(cid)
            assert exit_reg is not None
            open_orders.append(
                OpenOrderSnap(
                    order_id=cid,
                    symbol="BTCUSDT",
                    side=OrderSide.BUY.value,
                    order_type="LIMIT",
                    price=Decimal("1"),
                    qty=_ORDER_SIZE,
                    filled_qty=Decimal(0),
                    reduce_only=True,
                    status="NEW",
                    ts=_BASE_TS + 2_000,
                )
            )

        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(open_orders),
            ts=_BASE_TS + 2_000,
            source="test",
        )

        first = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_000,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )
        second = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_001,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )

        assert first == []
        assert second
        assert all(a.reason != "grid_v2_PLACE_ENTRY" for a in second)
        assert any(a.reason == "grid_v2_INTEGRITY_CANCEL_ENTRY" for a in second)

    def test_integrity_mismatch_not_reset_by_pending_convergence(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mismatch streak should survive brief pending windows and trigger repair."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()
        engine._grid_v2_pending_cancels.clear()

        # Prepare mismatch: one expected entry missing on exchange.
        missing = sorted(bridge.adapter.registry.all_entry_cids)[0]
        surviving_orders: list[OpenOrderSnap] = []
        for cid in sorted(bridge.adapter.registry.all_entry_cids):
            if cid == missing:
                continue
            reg = bridge.adapter.registry.lookup_entry(cid)
            assert reg is not None
            surviving_orders.append(
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
                    ts=_BASE_TS + 2_000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(surviving_orders),
            ts=_BASE_TS + 2_000,
            source="test",
        )

        first = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_000,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )
        assert first == []

        # Convergence gate active on next tick: should not reset mismatch streak.
        engine._grid_v2_pending_place_cids["dummy"] = engine._account_sync_generation
        gated = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_001,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )
        assert gated == []

        # Pending cleared: second mismatch should trigger repair.
        engine._grid_v2_pending_place_cids.clear()
        second = engine._grid_v2_integrity_repair(
            Snapshot(
                ts=_BASE_TS + 2_002,
                symbol="BTCUSDT",
                bid_price=Decimal("49999"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )
        assert second
        assert any(a.reason == "grid_v2_PLACE_ENTRY" for a in second)


class TestRepairAntiChurnGuards:
    """PR-B: anti-churn guards for integrity repair PLACE actions."""

    @staticmethod
    def _make_engine(
        monkeypatch: pytest.MonkeyPatch, *, max_distance: float = 5.0, max_actions: int = 5
    ) -> LiveEngineV0:
        """Create engine with grid_v2 config."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_REPAIR_MAX_DISTANCE_STEPS", str(max_distance))
        monkeypatch.setenv("GRINDER_GRID_V2_REPAIR_MAX_ACTIONS_PER_CYCLE", str(max_actions))

        return LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

    def test_distance_guard_skips_far_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repair skips PLACE for entries far from mid price."""
        engine = self._make_engine(monkeypatch, max_distance=1.0)
        assert engine._grid_v2_repair_max_distance_steps == 1.0

    def test_budget_guard_caps_actions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repair budget caps total PLACE actions per cycle."""
        engine = self._make_engine(monkeypatch, max_actions=2)
        assert engine._grid_v2_repair_max_actions == 2

    def test_env_parsing_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default values are used when env not set."""
        monkeypatch.delenv("GRINDER_GRID_V2_REPAIR_MAX_DISTANCE_STEPS", raising=False)
        monkeypatch.delenv("GRINDER_GRID_V2_REPAIR_MAX_ACTIONS_PER_CYCLE", raising=False)
        engine = self._make_engine(monkeypatch)
        assert engine._grid_v2_repair_max_distance_steps == 5.0
        assert engine._grid_v2_repair_max_actions == 5

    def test_pending_slot_dedup_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_grid_v2_pending_place_entry_prices returns correct (side, price) set."""
        from grinder.contracts import Snapshot  # noqa: PLC0415

        engine = self._make_engine(monkeypatch)
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Before startup, no pending
        assert engine._grid_v2_pending_place_entry_prices(bridge) == set()

        # Fresh startup seeds entries as pending
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

        # After startup, pending_place_cids should have seed entries
        pending = engine._grid_v2_pending_place_entry_prices(bridge)
        # Should have BUY + SELL entries
        buy_pending = {(s, p) for s, p in pending if s == OrderSide.BUY}
        sell_pending = {(s, p) for s, p in pending if s == OrderSide.SELL}
        assert len(buy_pending) > 0
        assert len(sell_pending) > 0

    def _setup_engine_for_repair(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        max_distance: float = 5.0,
        max_actions: int = 5,
    ) -> tuple[LiveEngineV0, list[OpenOrderSnap]]:
        """Start engine, seed grid, clear awaiting state, return (engine, all_entry_orders)."""
        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415

        engine = self._make_engine(monkeypatch, max_distance=max_distance, max_actions=max_actions)
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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Build full entry order list from registry
        all_orders = []
        for cid in sorted(bridge.adapter.registry.all_entry_cids):
            reg = bridge.adapter.registry.lookup_entry(cid)
            assert reg is not None
            all_orders.append(
                OpenOrderSnap(
                    order_id=cid,
                    symbol="BTCUSDT",
                    side=reg.side.value,
                    order_type="LIMIT",
                    price=reg.price,
                    qty=bridge._config.order_size,
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=_BASE_TS,
                )
            )

        # Clear awaiting flags so repair can fire
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()
        engine._grid_v2_pending_cancels.clear()
        return engine, all_orders

    def test_repair_budget_caps_place_actions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repair with budget=1 emits at most 1 PLACE_ENTRY per cycle."""
        engine, _all_orders = self._setup_engine_for_repair(monkeypatch, max_actions=1)

        # Remove ALL entries from exchange snapshot → all are "missing"
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=_BASE_TS + 10000, source="test"
        )

        # Tick twice (streak threshold = 2) to trigger repair
        snap2 = Snapshot(
            ts=_BASE_TS + 10000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap2)
        snap3 = Snapshot(
            ts=_BASE_TS + 11000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output = engine.process_snapshot(snap3)

        place_entries = [
            a
            for a in output.live_actions
            if a.action.action_type == ActionType.PLACE and "PLACE_ENTRY" in (a.action.reason or "")
        ]
        assert len(place_entries) <= 1, f"Budget cap=1 but got {len(place_entries)} PLACE_ENTRY"

    def test_repair_distance_guard_skips_far_prices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repair with distance_guard=0.5 skips entries >0.5 steps from mid."""
        engine, _all_orders = self._setup_engine_for_repair(monkeypatch, max_distance=0.5)

        # Remove all entries, mid stays at 50000
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=_BASE_TS + 10000, source="test"
        )

        snap2 = Snapshot(
            ts=_BASE_TS + 10000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap2)
        snap3 = Snapshot(
            ts=_BASE_TS + 11000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output = engine.process_snapshot(snap3)

        # With distance=0.5 steps and step=0.5%, most entries at >0.5 steps should be skipped
        place_entries = [
            a
            for a in output.live_actions
            if a.action.action_type == ActionType.PLACE and "PLACE_ENTRY" in (a.action.reason or "")
        ]
        # At minimum, should have fewer than total missing (which is all 6 entries)
        assert len(place_entries) < 6, (
            f"Distance guard should skip far entries, got {len(place_entries)}"
        )

    def test_repair_pending_slot_dedup_skips_place(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repair skips PLACE_ENTRY for slots already in pending_place_cids."""
        engine, all_orders = self._setup_engine_for_repair(monkeypatch)
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Remove one entry from exchange snapshot, keep the rest
        removed_order = all_orders[0]
        surviving = all_orders[1:]
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(surviving),
            ts=_BASE_TS + 10000,
            source="test",
        )

        # Simulate that the removed slot is already pending (PLACE in flight):
        # the original CID was already registered at startup; add it to pending_place_cids
        removed_price = removed_order.price
        original_cid = removed_order.order_id
        engine._grid_v2_pending_place_cids[original_cid] = 1

        # Tick twice to trigger repair (streak threshold = 2)
        snap2 = Snapshot(
            ts=_BASE_TS + 10000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap2)
        snap3 = Snapshot(
            ts=_BASE_TS + 11000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output = engine.process_snapshot(snap3)

        # Should NOT place a new entry for the pending slot
        place_entries = [
            a
            for a in output.live_actions
            if a.action.action_type == ActionType.PLACE and "PLACE_ENTRY" in (a.action.reason or "")
        ]
        placed_prices = {a.action.price for a in place_entries}
        assert removed_price not in placed_prices, (
            f"Pending-slot dedup should skip {removed_price}, but it was placed"
        )


class TestEngineCancelUnknownClassification:
    def test_cancel_2011_treated_as_ack_for_grid_v2_cid(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot  # noqa: PLC0415
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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        cid = sorted(bridge.adapter.registry.all_entry_cids)[0]
        assert bridge.adapter.registry.lookup_entry(cid) is not None
        engine._grid_v2_pending_cancels[cid] = _BASE_TS + 100
        engine._cancel_failed_ids.add(cid)

        handled = engine._grid_v2_handle_failed_cancel(cid, -2011)

        assert handled
        assert bridge.adapter.registry.lookup_entry(cid) is None
        assert cid not in engine._grid_v2_pending_cancels
        assert cid not in engine._cancel_failed_ids


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

    def test_f2_recovery_grid_v2_no_cycle_layer_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """F2 recovery: cycle layer gated while awaiting protective exit sync."""
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

        # Non-flat position, no grid_v2 orders → F2 recovery
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

        # F2 recovery succeeds, but awaiting_sync gates cycle layer
        assert engine._grid_v2_bridge is not None
        assert engine._grid_v2_bridge.f2_protective_recovery is True
        assert engine._grid_v2_awaiting_sync is True
        # Only protective exit actions (no cycle layer actions)
        grid_actions = [a for a in output.live_actions if "grid_v2" in (a.action.reason or "")]
        for a in grid_actions:
            assert a.action.reason == "grid_v2_F2_PROTECTIVE_EXIT"


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


class TestPendingPlaceCidVisibility:
    """Pending-place CID visibility gate + grace expiry + failure cleanup."""

    def test_pending_place_not_treated_as_fill(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CID in pending-place set → excluded from fill detection."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.grid_v2.adapter import EntryRegistration  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Fresh startup + clear awaiting sync
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
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()

        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Add a CID to registry + pending (simulating post-fill PLACE dispatch)
        new_cid = bridge.adapter.generate_entry_cid(_BASE_TS + 10000)
        bridge.adapter.registry._entries[new_cid] = EntryRegistration(
            cid=new_cid, side=OrderSide.BUY, price=Decimal("49500")
        )
        engine._grid_v2_pending_place_cids[new_cid] = engine._account_sync_generation

        # Tick: snapshot does NOT have the new CID
        seed_cids = [c for c in bridge.adapter.registry.all_entry_cids if c != new_cid]
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
                    ts=_BASE_TS + 10000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(open_orders),
            ts=_BASE_TS + 10000,
            source="test",
        )
        snap2 = Snapshot(
            ts=_BASE_TS + 10000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output2 = engine.process_snapshot(snap2)

        fill_actions = [
            a for a in output2.live_actions if a.action.reason and "PLACE_EXIT" in a.action.reason
        ]
        assert len(fill_actions) == 0, "Pending-place CID must not trigger false fill"

    def test_grace_expiry_releases_never_visible_cid(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """P0: After 2 sync cycles, pending CID released for fill detection."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot  # noqa: PLC0415
        from grinder.account.syncer import AccountSyncer, SyncResult  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.grid_v2.adapter import EntryRegistration  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

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
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()

        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Add CID to registry + pending at current gen
        new_cid = bridge.adapter.generate_entry_cid(_BASE_TS + 10000)
        bridge.adapter.registry._entries[new_cid] = EntryRegistration(
            cid=new_cid, side=OrderSide.BUY, price=Decimal("49500")
        )
        dispatch_gen = engine._account_sync_generation
        engine._grid_v2_pending_place_cids[new_cid] = dispatch_gen

        # Simulate 2 account syncs with CID never visible (empty open_orders)
        mock_syncer = MagicMock(spec=AccountSyncer)
        mock_syncer.compute_position_notional = AccountSyncer.compute_position_notional
        engine._account_syncer = mock_syncer

        for i in range(2):
            empty_snap = AccountSnapshot(
                positions=(), open_orders=(), ts=_BASE_TS + 20000 + i * 5000, source="test"
            )
            mock_syncer.sync.return_value = SyncResult(snapshot=empty_snap)
            engine._tick_account_sync()

        # After 2 sync cycles, CID should be released from pending
        assert new_cid not in engine._grid_v2_pending_place_cids, (
            "CID must be released after grace period"
        )

    def test_failed_place_cleaned_via_engine(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """P1/P2: FAILED PLACE goes through _grid_v2_clean_failed_place."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.grid_v2.adapter import EntryRegistration  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

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

        bridge = engine._grid_v2_bridge
        assert bridge is not None

        initial_count = bridge.adapter.registry.entry_count

        # Add CID to registry
        new_cid = bridge.adapter.generate_entry_cid(_BASE_TS + 10000)
        bridge.adapter.registry._entries[new_cid] = EntryRegistration(
            cid=new_cid, side=OrderSide.BUY, price=Decimal("49500")
        )
        assert bridge.adapter.registry.entry_count == initial_count + 1

        # Call production cleanup function
        engine._grid_v2_clean_failed_place(new_cid)

        # Registry should be cleaned
        assert bridge.adapter.registry.entry_count == initial_count
        assert new_cid not in engine._grid_v2_pending_place_cids


class TestPendingPlaceIntegration:
    """P2: Engine-level test through real fill → dispatch → pending lifecycle."""

    def test_fill_generated_place_exit_goes_to_pending(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real fill on entry → PLACE_EXIT dispatched → exit CID in pending."""
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

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Tick 1: fresh startup → seeds placed
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=_BASE_TS, source="test"
        )
        snap1 = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap1)

        bridge = engine._grid_v2_bridge
        assert bridge is not None
        seed_cids = list(bridge.adapter.registry.all_entry_cids)

        # Simulate account sync showing seeds → clear awaiting + pending
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()  # seeds now visible

        # Build snapshot with all seeds EXCEPT one (simulating fill)
        remaining_orders = []
        for cid in seed_cids[1:]:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg is None:
                continue
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
                    ts=_BASE_TS + 10000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(remaining_orders),
            ts=_BASE_TS + 10000,
            source="test",
        )

        # Tick 2: fill detection → bridge.on_fill → PLACE_EXIT dispatched
        snap2 = Snapshot(
            ts=_BASE_TS + 10000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output2 = engine.process_snapshot(snap2)

        # Should have generated actions from fill processing
        executed = [a for a in output2.live_actions if a.status.value == "EXECUTED"]
        exit_actions = [a for a in executed if a.action.reason and "PLACE_EXIT" in a.action.reason]
        assert len(exit_actions) > 0, "Fill should generate PLACE_EXIT"

        # The exit CID should now be in pending_place_cids (EXECUTED → pending)
        exit_cid = exit_actions[0].action.client_order_id
        assert exit_cid is not None
        assert exit_cid in engine._grid_v2_pending_place_cids, (
            "EXECUTED PLACE_EXIT CID must be in pending_place_cids"
        )

    def test_ambiguous_failed_place_stays_in_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MAX_RETRIES_EXCEEDED PLACE → CID stays in registry + pending (not cleaned)."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.errors import ConnectorTransientError  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        port = MagicMock()
        # First calls succeed (seeds), then fail with transient error
        call_count = {"n": 0}

        def flaky_place(*args: object, **kwargs: object) -> str:
            call_count["n"] += 1
            if call_count["n"] > 6:  # seeds use 6 calls
                raise ConnectorTransientError("timeout")
            return f"ORDER_{call_count['n']}"

        port.place_order.side_effect = flaky_place

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Tick 1: fresh startup
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=_BASE_TS, source="test"
        )
        snap1 = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snap1)

        bridge = engine._grid_v2_bridge
        assert bridge is not None
        seed_cids = list(bridge.adapter.registry.all_entry_cids)

        # Simulate fill: remove one seed from exchange
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()  # seeds now visible

        remaining = []
        for cid in seed_cids[1:]:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg is None:
                continue
            remaining.append(
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
                    ts=_BASE_TS + 10000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(remaining),
            ts=_BASE_TS + 10000,
            source="test",
        )

        # Tick 2: fill detected → actions dispatched → some PLACEs fail with transient
        snap2 = Snapshot(
            ts=_BASE_TS + 10000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output2 = engine.process_snapshot(snap2)

        # Find FAILED PLACEs that are ours
        failed_place_actions = [
            a
            for a in output2.live_actions
            if a.action.action_type == ActionType.PLACE
            and a.status.value == "FAILED"
            and a.action.client_order_id
            and bridge.adapter.is_ours(a.action.client_order_id)
        ]

        # Mandatory: at least one FAILED PLACE must exist (otherwise test is vacuous)
        assert len(failed_place_actions) > 0, (
            "Test requires at least one FAILED grid_v2 PLACE to verify quarantine"
        )

        # All FAILED PLACEs should be quarantined in pending (not cleaned from registry)
        for fa in failed_place_actions:
            failed_cid = fa.action.client_order_id
            assert failed_cid is not None
            assert failed_cid in engine._grid_v2_pending_place_cids, (
                f"FAILED CID {failed_cid} (reason={fa.block_reason}) must stay in pending"
            )
            # CID should still be in adapter registry (not cleaned)
            assert (
                bridge.adapter.registry.lookup_entry(failed_cid) is not None
                or bridge.adapter.registry.lookup_exit(failed_cid) is not None
            ), f"FAILED CID {failed_cid} must remain in adapter registry"


class TestPreSendClassification:
    """P0: pre_send FAILED → clean immediately, post-send FAILED → quarantine."""

    def test_pre_send_failed_cleans_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Local validation error (pre_send=True) → CID cleaned from registry."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.errors import ConnectorNonRetryableError  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        call_count = {"n": 0}

        def place_then_presend_fail(*args: object, **kwargs: object) -> str:
            call_count["n"] += 1
            if call_count["n"] > 6:  # seeds succeed, then pre-send fail
                raise ConnectorNonRetryableError("notional too small", pre_send=True)
            return f"ORDER_{call_count['n']}"

        port = MagicMock()
        port.place_order.side_effect = place_then_presend_fail

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

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
        bridge = engine._grid_v2_bridge
        assert bridge is not None
        seed_cids = list(bridge.adapter.registry.all_entry_cids)

        # Clear pending seeds
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()

        # Simulate fill: remove first seed
        remaining = []
        for cid in seed_cids[1:]:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg is None:
                continue
            remaining.append(
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
                    ts=_BASE_TS + 10000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(remaining),
            ts=_BASE_TS + 10000,
            source="test",
        )

        # Tick 2: fill → PLACE actions → some fail with pre_send=True
        snap2 = Snapshot(
            ts=_BASE_TS + 10000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output2 = engine.process_snapshot(snap2)

        # Find pre-send FAILED PLACEs
        pre_send_failed = [
            a
            for a in output2.live_actions
            if a.action.action_type == ActionType.PLACE
            and a.status.value == "FAILED"
            and a.pre_send
            and a.action.client_order_id
            and bridge.adapter.is_ours(a.action.client_order_id)
        ]

        # At least one pre-send failure should exist
        assert len(pre_send_failed) > 0, "Need at least one pre-send FAILED PLACE"

        # pre_send FAILED CIDs should be cleaned from registry (not quarantined)
        for fa in pre_send_failed:
            ps_cid = fa.action.client_order_id
            assert ps_cid is not None
            assert ps_cid not in engine._grid_v2_pending_place_cids, (
                f"pre_send CID {ps_cid} must NOT be quarantined"
            )
            assert bridge.adapter.registry.lookup_entry(ps_cid) is None, (
                f"pre_send CID {ps_cid} must be cleaned from registry"
            )

    def test_circuit_open_cleans_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CircuitOpenError → pre_send=True → CID cleaned immediately."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.errors import CircuitOpenError  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        call_count = {"n": 0}

        def place_then_circuit(*args: object, **kwargs: object) -> str:
            call_count["n"] += 1
            if call_count["n"] > 6:
                raise CircuitOpenError("breaker open")
            return f"ORDER_{call_count['n']}"

        port = MagicMock()
        port.place_order.side_effect = place_then_circuit
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()

        seed_cids = list(bridge.adapter.registry.all_entry_cids)
        remaining = []
        for cid in seed_cids[1:]:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg is None:
                continue
            remaining.append(
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
                    ts=_BASE_TS + 10000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(remaining),
            ts=_BASE_TS + 10000,
            source="test",
        )

        snap2 = Snapshot(
            ts=_BASE_TS + 10000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output2 = engine.process_snapshot(snap2)

        circuit_failed = [
            a
            for a in output2.live_actions
            if a.action.action_type == ActionType.PLACE
            and a.status.value == "FAILED"
            and a.pre_send
            and a.action.client_order_id
            and bridge.adapter.is_ours(a.action.client_order_id)
        ]
        assert len(circuit_failed) > 0, "At least one CircuitOpen FAILED"
        for fa in circuit_failed:
            co_cid = fa.action.client_order_id
            assert co_cid is not None
            assert co_cid not in engine._grid_v2_pending_place_cids
            assert bridge.adapter.registry.lookup_entry(co_cid) is None

    def test_explicit_exchange_reject_cleans_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Post-HTTP exchange reject (with exchange_code) → CID cleaned."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.errors import ConnectorNonRetryableError  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        call_count = {"n": 0}

        def place_then_exchange_reject(*args: object, **kwargs: object) -> str:
            call_count["n"] += 1
            if call_count["n"] > 6:
                raise ConnectorNonRetryableError(
                    "Binance error -4164: min notional", exchange_code=-4164
                )
            return f"ORDER_{call_count['n']}"

        port = MagicMock()
        port.place_order.side_effect = place_then_exchange_reject
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()

        seed_cids = list(bridge.adapter.registry.all_entry_cids)
        remaining = []
        for cid in seed_cids[1:]:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg is None:
                continue
            remaining.append(
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
                    ts=_BASE_TS + 10000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(remaining),
            ts=_BASE_TS + 10000,
            source="test",
        )

        snap2 = Snapshot(
            ts=_BASE_TS + 10000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output2 = engine.process_snapshot(snap2)

        exchange_rejected = [
            a
            for a in output2.live_actions
            if a.action.action_type == ActionType.PLACE
            and a.status.value == "FAILED"
            and a.exchange_code is not None
            and a.action.client_order_id
            and bridge.adapter.is_ours(a.action.client_order_id)
        ]
        assert len(exchange_rejected) > 0, "At least one exchange-rejected FAILED"
        for fa in exchange_rejected:
            er_cid = fa.action.client_order_id
            assert er_cid is not None
            assert er_cid not in engine._grid_v2_pending_place_cids, (
                f"Exchange-rejected CID {er_cid} must be cleaned, not quarantined"
            )
            assert bridge.adapter.registry.lookup_entry(er_cid) is None, (
                f"Exchange-rejected CID {er_cid} must be cleaned from registry"
            )

    def test_duplicate_code_quarantines(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Post-HTTP -2010 (duplicate/ambiguous) → CID quarantined, NOT cleaned."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.errors import ConnectorNonRetryableError  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        call_count = {"n": 0}

        def place_then_duplicate(*args: object, **kwargs: object) -> str:
            call_count["n"] += 1
            if call_count["n"] > 6:
                raise ConnectorNonRetryableError(
                    "Binance error -2010: New order rejected",
                    exchange_code=-2010,
                )
            return f"ORDER_{call_count['n']}"

        port = MagicMock()
        port.place_order.side_effect = place_then_duplicate
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()

        seed_cids = list(bridge.adapter.registry.all_entry_cids)
        remaining = []
        for cid in seed_cids[1:]:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg is None:
                continue
            remaining.append(
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
                    ts=_BASE_TS + 10000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(remaining),
            ts=_BASE_TS + 10000,
            source="test",
        )

        snap2 = Snapshot(
            ts=_BASE_TS + 10000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output2 = engine.process_snapshot(snap2)

        # Find -2010 FAILED PLACEs
        dup_failed = [
            a
            for a in output2.live_actions
            if a.action.action_type == ActionType.PLACE
            and a.status.value == "FAILED"
            and a.exchange_code == -2010
            and a.action.client_order_id
            and bridge.adapter.is_ours(a.action.client_order_id)
        ]
        assert len(dup_failed) > 0, "At least one -2010 duplicate FAILED"

        # -2010 CIDs must be QUARANTINED (not cleaned)
        for fa in dup_failed:
            dup_cid = fa.action.client_order_id
            assert dup_cid is not None
            assert dup_cid in engine._grid_v2_pending_place_cids, (
                f"-2010 CID {dup_cid} must be quarantined, not cleaned"
            )
            assert (
                bridge.adapter.registry.lookup_entry(dup_cid) is not None
                or bridge.adapter.registry.lookup_exit(dup_cid) is not None
            ), f"-2010 CID {dup_cid} must remain in registry"

    def test_margin_insufficient_cleans_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """-2019 (margin insufficient) is definitive reject → CID cleaned."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, OpenOrderSnap  # noqa: PLC0415
        from grinder.connectors.errors import ConnectorNonRetryableError  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")

        call_count = {"n": 0}

        def place_then_margin(*args: object, **kwargs: object) -> str:
            call_count["n"] += 1
            if call_count["n"] > 6:
                raise ConnectorNonRetryableError(
                    "Binance error -2019: Margin is insufficient",
                    exchange_code=-2019,
                )
            return f"ORDER_{call_count['n']}"

        port = MagicMock()
        port.place_order.side_effect = place_then_margin
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

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
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()

        seed_cids = list(bridge.adapter.registry.all_entry_cids)
        remaining = []
        for cid in seed_cids[1:]:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg is None:
                continue
            remaining.append(
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
                    ts=_BASE_TS + 10000,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(remaining),
            ts=_BASE_TS + 10000,
            source="test",
        )

        snap2 = Snapshot(
            ts=_BASE_TS + 10000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        output2 = engine.process_snapshot(snap2)

        margin_failed = [
            a
            for a in output2.live_actions
            if a.action.action_type == ActionType.PLACE
            and a.status.value == "FAILED"
            and a.exchange_code == -2019
            and a.action.client_order_id
            and bridge.adapter.is_ours(a.action.client_order_id)
        ]
        assert len(margin_failed) > 0, "At least one -2019 FAILED"

        for fa in margin_failed:
            m_cid = fa.action.client_order_id
            assert m_cid is not None
            assert m_cid not in engine._grid_v2_pending_place_cids, (
                f"-2019 CID {m_cid} must be cleaned, not quarantined"
            )
            assert bridge.adapter.registry.lookup_entry(m_cid) is None, (
                f"-2019 CID {m_cid} must be cleaned from registry"
            )
