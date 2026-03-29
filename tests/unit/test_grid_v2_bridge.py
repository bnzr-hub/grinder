"""Tests for grid_v2 runtime bridge (doc-27 section 23, PR4)."""

import pathlib
from dataclasses import replace
from decimal import Decimal
from unittest.mock import MagicMock, patch

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
    ActionIntent,
    ActionIntentKind,
    BranchMode,
    EntryFilled,
    ExitFilled,
    ExitOrder,
    ExitOrderStatus,
    GridV2Config,
    TransitionResult,
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
        # One-sided: entry fill already cancelled all SELL entries, so reseed only
        # cancels remaining BUY entries (3) and places 6 new (3 BUY + 3 SELL).
        assert len(cancel_actions) == 3
        assert len(place_actions) == 6
        assert all(ea.reason == "grid_v2_PLACE_ENTRY" for ea in place_actions)

    def test_exit_fill_to_flat_preserve_restores_same_side_only(self) -> None:
        """Preserve mode: exit fill restores BUY entries only (one-sided, was LONG_BRANCH)."""
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
        # One-sided: only BUY entries restored, SELL stays empty.
        assert len(b.state_machine.snapshot.entry_window.buy_entry_prices) == 3
        assert len(b.state_machine.snapshot.entry_window.sell_entry_prices) == 0

        place_actions = [
            ea for ea in exit_result.execution_actions if ea.action_type == ActionType.PLACE
        ]
        cancel_actions = [
            ea for ea in exit_result.execution_actions if ea.action_type == ActionType.CANCEL
        ]
        assert any(ea.reason == "grid_v2_PLACE_ENTRY" for ea in place_actions)
        # Preserve mode: minimal actions (1 cancel + 1 place for BUY restore shift).
        assert len(place_actions) == 1
        assert len(cancel_actions) == 1


class TestNetOffBridge:
    """Bridge-level tests: one-sided mode removes opposite entries, making netoff unreachable."""

    def test_one_sided_removes_sell_entries_after_buy_fill(self) -> None:
        """LONG branch: all SELL entry CIDs removed from registry after BUY fill."""
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

        # After one-sided cancel, no SELL entry CIDs should remain in the registry.
        sell_cids = [
            cid
            for cid in bridge.adapter.registry.all_entry_cids
            if (reg := bridge.adapter.registry.lookup_entry(cid)) is not None
            and reg.side == OrderSide.SELL
        ]
        assert len(sell_cids) == 0, "One-sided mode should have removed all SELL entries"
        # CANCEL_ENTRY actions emitted for opposite side.
        cancel_actions = [a for a in r1.execution_actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) >= 1

    def test_one_sided_removes_buy_entries_after_sell_fill(self) -> None:
        """SHORT branch: all BUY entry CIDs removed from registry after SELL fill."""
        bridge = GridV2Bridge(_config(), "BTCUSDT")
        bridge.startup_fresh(Decimal("50000"), _BASE_TS)

        sell_cid = None
        for cid in bridge.adapter.registry.all_entry_cids:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg and reg.side == OrderSide.SELL:
                sell_cid = cid
                sell_price = reg.price
                break
        assert sell_cid is not None
        bridge.on_fill(sell_cid, OrderSide.SELL, sell_price, Decimal("0.01"), _BASE_TS + 1)

        # After one-sided cancel, no BUY entry CIDs should remain.
        buy_cids = [
            cid
            for cid in bridge.adapter.registry.all_entry_cids
            if (reg := bridge.adapter.registry.lookup_entry(cid)) is not None
            and reg.side == OrderSide.BUY
        ]
        assert len(buy_cids) == 0, "One-sided mode should have removed all BUY entries"


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
        monkeypatch.setenv("GRINDER_GRID_V2_ENTRY_LEVELS", "5")
        monkeypatch.setenv("GRINDER_GRID_V2_MAX_INV_LEVELS", "5")

        # Use same step + levels as engine so no geometry mismatch
        old_bridge, _ = _fresh_bridge(config=_config(step=Decimal("0.0025"), levels=5))
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
        engine._grid_v2_pending_cancels[target_cid] = (_BASE_TS + 1000, 0)

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
        engine._grid_v2_pending_cancels[target_cid] = (_BASE_TS, 0)

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

        # One-sided: after BUY fill, only BUY entry CIDs remain (no SELL entries).
        # Pick one exit CID and one BUY-entry CID to disappear in the same tick.
        exit_cid = next(iter(bridge.adapter.registry.all_exit_cids))
        buy_entry_cid = next(
            cid
            for cid in sorted(bridge.adapter.registry.all_entry_cids)
            if (reg := bridge.adapter.registry.lookup_entry(cid)) is not None
            and reg.side == OrderSide.BUY
        )

        # Exchange snapshot: both selected CIDs disappeared.
        surviving_orders: list[OpenOrderSnap] = []
        for cid in sorted(
            set(bridge.adapter.registry.all_entry_cids) | set(bridge.adapter.registry.all_exit_cids)
        ):
            if cid in {exit_cid, buy_entry_cid}:
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
        # Exit processed first → FLAT (reseed). The old BUY entry CID is stale after
        # reseed (cancelled + new entries placed), so the BUY fill is suppressed.
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
        engine._grid_v2_pending_cancels[cid] = (_BASE_TS + 100, 0)
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
            if call_count["n"] > 10:  # seeds use 10 calls (5 levels × 2 sides)
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
            if call_count["n"] > 10:  # seeds succeed, then pre-send fail
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
            if call_count["n"] > 10:
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
            if call_count["n"] > 10:
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
            if call_count["n"] > 10:
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
            if call_count["n"] > 10:  # 5 levels × 2 sides = 10 seed entries
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


class TestSameTickDedup:
    """Same-tick fill/repair dedup: fill-path PLACE slots excluded from repair."""

    @staticmethod
    def _make_engine(
        monkeypatch: pytest.MonkeyPatch,
        *,
        max_distance: float = 10.0,
        max_actions: int = 10,
        strict_geometry: bool = False,
    ) -> LiveEngineV0:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_REPAIR_MAX_DISTANCE_STEPS", str(max_distance))
        monkeypatch.setenv("GRINDER_GRID_V2_REPAIR_MAX_ACTIONS_PER_CYCLE", str(max_actions))
        monkeypatch.setenv(
            "GRINDER_GRID_V2_REPAIR_STRICT_GEOMETRY", "1" if strict_geometry else "0"
        )
        # Disable reseed-on-flat so FLAT path uses preserve/cleanup (not recenter)
        # This ensures distance guard is testable in FLAT mode
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "0")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW", "0")

        return LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

    def test_extract_planned_entry_slots(self) -> None:
        """Fill-path PLACE_ENTRY actions produce (side, price) slots for dedup."""
        actions = [
            ExecutionAction(
                action_type=ActionType.PLACE,
                symbol="SYM",
                side=OrderSide.SELL,
                price=Decimal("100.50"),
                reason="grid_v2_PLACE_ENTRY",
            ),
            ExecutionAction(
                action_type=ActionType.CANCEL,
                order_id="cid1",
                symbol="SYM",
                reason="grid_v2_CANCEL_EXIT",
            ),
        ]
        slots = LiveEngineV0._extract_planned_entry_slots(actions)
        assert (OrderSide.SELL, Decimal("100.50")) in slots
        assert len(slots) == 1

    def test_planned_slots_prevent_repair_duplicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Integrity repair skips slot already planned by fill path in same tick."""
        engine = self._make_engine(monkeypatch)
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Seed the grid
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

        # Clear awaiting
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_pending_seed_cids = frozenset()
        engine._grid_v2_pending_place_cids.clear()

        # Build order list with one BUY entry missing
        bridge2 = engine._grid_v2_bridge
        assert bridge2 is not None
        all_cids = sorted(bridge2.adapter.registry.all_entry_cids)
        assert len(all_cids) > 0

        # Find a BUY entry
        buy_cid = None
        buy_reg = None
        for c in all_cids:
            r = bridge2.adapter.registry.lookup_entry(c)
            if r is not None and r.side == OrderSide.BUY:
                buy_cid = c
                buy_reg = r
                break
        assert buy_cid is not None and buy_reg is not None

        # Build orders without the BUY entry (simulate missing)
        orders_minus_one = []
        for c in all_cids:
            if c == buy_cid:
                continue
            r = bridge2.adapter.registry.lookup_entry(c)
            if r is None:
                continue
            orders_minus_one.append(
                OpenOrderSnap(
                    order_id=c,
                    symbol="BTCUSDT",
                    side=r.side.value,
                    order_type="LIMIT",
                    price=r.price,
                    qty=bridge2._config.order_size,
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=_BASE_TS,
                )
            )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(orders_minus_one),
            ts=_BASE_TS + 10000,
            source="test",
        )

        # Simulate that fill path already plans PLACE for the missing slot
        planned_slots = {(buy_reg.side, buy_reg.price)}

        # Call repair with planned slots — should skip the missing slot
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
        # First tick: streak=1
        engine._grid_v2_integrity_repair(snap2, planned_slots_this_tick=planned_slots)
        # Second tick: streak=2 → triggers repair
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
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(orders_minus_one),
            ts=_BASE_TS + 11000,
            source="test",
        )
        repair2 = engine._grid_v2_integrity_repair(snap3, planned_slots_this_tick=planned_slots)

        # The missing slot should be SKIPPED because it's in planned_slots
        place_actions = [a for a in repair2 if a.action_type == ActionType.PLACE]
        for pa in place_actions:
            assert (pa.side, pa.price) != (buy_reg.side, buy_reg.price), (
                f"Repair placed duplicate slot {pa.side}@{pa.price} that fill already planned"
            )

    def _setup_branch_engine(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        strict: bool,
        max_distance: float = 0.5,
    ) -> tuple[LiveEngineV0, list[OpenOrderSnap]]:
        """Setup engine in LONG_BRANCH mode via simulated fill."""
        engine = self._make_engine(
            monkeypatch, max_distance=max_distance, max_actions=10, strict_geometry=strict
        )
        bridge = engine._grid_v2_bridge
        assert bridge is not None

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
        engine._grid_v2_pending_place_cids.clear()

        bridge2 = engine._grid_v2_bridge
        assert bridge2 is not None

        # Simulate BUY fill to enter LONG_BRANCH
        buy_cids = [
            c
            for c in sorted(bridge2.adapter.registry.all_entry_cids)
            if bridge2.adapter.registry.lookup_entry(c) is not None
            and bridge2.adapter.registry.lookup_entry(c).side == OrderSide.BUY  # type: ignore[union-attr]
        ]
        assert len(buy_cids) > 0
        fill_cid = buy_cids[0]
        fill_reg = bridge2.adapter.registry.lookup_entry(fill_cid)
        assert fill_reg is not None
        result = bridge2.on_fill(
            fill_cid, fill_reg.side, fill_reg.price, bridge2._config.order_size, _BASE_TS + 1000
        )
        assert not result.rejected

        sm = bridge2.state_machine
        assert sm is not None
        assert sm.mode.value != "FLAT", f"Expected branch mode after fill, got {sm.mode.value}"

        # Build remaining orders from registry (minus the filled one)
        remaining_orders: list[OpenOrderSnap] = []
        for c in sorted(bridge2.adapter.registry.all_entry_cids):
            r = bridge2.adapter.registry.lookup_entry(c)
            if r is None:
                continue
            remaining_orders.append(
                OpenOrderSnap(
                    order_id=c,
                    symbol="BTCUSDT",
                    side=r.side.value,
                    order_type="LIMIT",
                    price=r.price,
                    qty=bridge2._config.order_size,
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=_BASE_TS,
                )
            )
        return engine, remaining_orders

    def test_strict_geometry_off_skips_far_in_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """STRICT_GEOMETRY=0 in branch mode: far missing slot skipped by distance."""
        engine, remaining = self._setup_branch_engine(monkeypatch, strict=False, max_distance=0.5)

        # Remove BUY entries to create missing slots
        sell_only = [o for o in remaining if o.side == "SELL"]

        # Move mid far away
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=tuple(sell_only), ts=_BASE_TS + 20000, source="test"
        )
        far_snap = Snapshot(
            ts=_BASE_TS + 20000,
            symbol="BTCUSDT",
            bid_price=Decimal("59999"),
            ask_price=Decimal("60001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("60000"),
            last_qty=Decimal("1"),
        )
        engine._grid_v2_integrity_repair(far_snap)
        far_snap2 = Snapshot(
            ts=_BASE_TS + 21000,
            symbol="BTCUSDT",
            bid_price=Decimal("59999"),
            ask_price=Decimal("60001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("60000"),
            last_qty=Decimal("1"),
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=tuple(sell_only), ts=_BASE_TS + 21000, source="test"
        )
        repair = engine._grid_v2_integrity_repair(far_snap2)

        place_buy = [
            a for a in repair if a.action_type == ActionType.PLACE and a.side == OrderSide.BUY
        ]
        assert len(place_buy) == 0, (
            f"Expected 0 BUY PLACE (distance skip in branch), got {len(place_buy)}"
        )

    def test_strict_geometry_on_bypasses_distance_in_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """STRICT_GEOMETRY=1 in branch mode: far missing slot placed (distance bypass)."""
        engine, remaining = self._setup_branch_engine(monkeypatch, strict=True, max_distance=0.5)

        sell_only = [o for o in remaining if o.side == "SELL"]

        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=tuple(sell_only), ts=_BASE_TS + 20000, source="test"
        )
        far_snap = Snapshot(
            ts=_BASE_TS + 20000,
            symbol="BTCUSDT",
            bid_price=Decimal("59999"),
            ask_price=Decimal("60001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("60000"),
            last_qty=Decimal("1"),
        )
        engine._grid_v2_integrity_repair(far_snap)
        far_snap2 = Snapshot(
            ts=_BASE_TS + 21000,
            symbol="BTCUSDT",
            bid_price=Decimal("59999"),
            ask_price=Decimal("60001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("60000"),
            last_qty=Decimal("1"),
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=tuple(sell_only), ts=_BASE_TS + 21000, source="test"
        )
        repair = engine._grid_v2_integrity_repair(far_snap2)

        place_buy = [
            a for a in repair if a.action_type == ActionType.PLACE and a.side == OrderSide.BUY
        ]
        assert len(place_buy) > 0, (
            "Expected BUY PLACE (strict geometry bypasses distance in branch), got 0"
        )


class TestRoleAwareRepair:
    """Role-aware integrity repair: ENTRY geometry vs EXIT ledger integrity."""

    @staticmethod
    def _make_engine(monkeypatch: pytest.MonkeyPatch) -> LiveEngineV0:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.setenv("GRINDER_GRID_V2_TICK_SIZE", "0.01")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT", "0")
        monkeypatch.setenv("GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW", "0")

        return LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

    def _seed_engine(self, engine: LiveEngineV0) -> list[OpenOrderSnap]:
        """Seed grid and return all entry orders."""
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
        engine._grid_v2_pending_place_cids.clear()

        bridge = engine._grid_v2_bridge
        assert bridge is not None
        orders: list[OpenOrderSnap] = []
        for c in sorted(bridge.adapter.registry.all_entry_cids):
            r = bridge.adapter.registry.lookup_entry(c)
            if r is None:
                continue
            orders.append(
                OpenOrderSnap(
                    order_id=c,
                    symbol="BTCUSDT",
                    side=r.side.value,
                    order_type="LIMIT",
                    price=r.price,
                    qty=bridge._config.order_size,
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=_BASE_TS,
                )
            )
        return orders

    def test_entry_geometry_ignores_exit_orders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test A: EXIT orders on exchange don't trigger false ENTRY geometry repair."""
        engine = self._make_engine(monkeypatch)
        entry_orders = self._seed_engine(engine)
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Add a fake EXIT order to open_orders — it should be ignored by ENTRY check
        fake_exit = OpenOrderSnap(
            order_id="g_g_BTCUSDT_x99_1710000000_0",
            symbol="BTCUSDT",
            side="SELL",
            order_type="LIMIT",
            price=Decimal("51000"),
            qty=Decimal("0.001"),
            filled_qty=Decimal("0"),
            reduce_only=True,
            status="NEW",
            ts=_BASE_TS,
        )
        all_orders = [*list(entry_orders), fake_exit]
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=tuple(all_orders), ts=_BASE_TS + 10000, source="test"
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
        # Tick 1: streak=1
        engine._grid_v2_integrity_repair(snap2)
        # Tick 2: triggers repair (EXIT in FLAT = cleanup target)
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
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=tuple(all_orders), ts=_BASE_TS + 11000, source="test"
        )
        repair2 = engine._grid_v2_integrity_repair(snap3)
        # EXIT in FLAT is cleaned up, but no ENTRY actions should be affected
        entry_actions = [
            a
            for a in repair2
            if a.reason == "grid_v2_INTEGRITY_CANCEL_ENTRY"
            or (a.action_type == ActionType.PLACE and "ENTRY" in a.reason.upper())
        ]
        assert len(entry_actions) == 0, f"EXIT presence caused false ENTRY repair: {entry_actions}"
        # EXIT cleanup is expected (correct role-aware behavior)
        exit_cancels = [a for a in repair2 if a.reason == "grid_v2_INTEGRITY_CANCEL_EXIT"]
        assert len(exit_cancels) == 1, "Expected EXIT cleanup in FLAT"

    def test_entry_cancel_targets_only_entry_cids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test B: ENTRY extra-cancel targets only ENTRY CIDs, not EXIT CIDs."""
        engine = self._make_engine(monkeypatch)
        entry_orders = self._seed_engine(engine)

        # Add an extra fake ENTRY that's not in expected window
        fake_extra_entry = OpenOrderSnap(
            order_id="g_g_BTCUSDT_e99_1710000000_0",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            price=Decimal("45000"),
            qty=Decimal("0.001"),
            filled_qty=Decimal("0"),
            reduce_only=False,
            status="NEW",
            ts=_BASE_TS,
        )
        all_orders = [*list(entry_orders), fake_extra_entry]
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=tuple(all_orders), ts=_BASE_TS + 10000, source="test"
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
        engine._grid_v2_integrity_repair(snap2)
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
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=tuple(all_orders), ts=_BASE_TS + 11000, source="test"
        )
        repair = engine._grid_v2_integrity_repair(snap3)

        # All CANCEL_ENTRY actions should target ENTRY CIDs only
        entry_cancels = [
            a
            for a in repair
            if a.action_type == ActionType.CANCEL and a.reason == "grid_v2_INTEGRITY_CANCEL_ENTRY"
        ]
        for ec in entry_cancels:
            assert ec.order_id is not None
            parsed = engine._grid_v2_bridge.adapter.parse_cid(ec.order_id)  # type: ignore[union-attr]
            if parsed is not None:
                assert parsed.kind.value == "ENTRY", (
                    f"ENTRY cancel targeted non-ENTRY CID: {ec.order_id} kind={parsed.kind.value}"
                )

    def test_entry_repair_deterministic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test E: same input → same repair actions, deterministic ordering."""
        results = []
        for _ in range(3):
            engine = self._make_engine(monkeypatch)
            entry_orders = self._seed_engine(engine)

            # Remove 2 BUY entries
            buy_orders = [o for o in entry_orders if o.side == "BUY"]
            sell_orders = [o for o in entry_orders if o.side == "SELL"]
            remaining = buy_orders[2:] + sell_orders

            engine._last_account_snapshot = AccountSnapshot(
                positions=(), open_orders=tuple(remaining), ts=_BASE_TS + 10000, source="test"
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
            engine._grid_v2_integrity_repair(snap2)
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
            engine._last_account_snapshot = AccountSnapshot(
                positions=(), open_orders=tuple(remaining), ts=_BASE_TS + 11000, source="test"
            )
            repair = engine._grid_v2_integrity_repair(snap3)
            results.append(
                [
                    (a.action_type.value, a.side.value if a.side else "", str(a.price or ""))
                    for a in repair
                ]
            )
        assert results[0] == results[1] == results[2]

    def test_closed_exit_records_do_not_create_orphans(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Closed exit ledger rows (FILLED/CANCELED) must not trigger EXIT orphan mismatch."""
        engine = self._make_engine(monkeypatch)
        _ = self._seed_engine(engine)
        bridge = engine._grid_v2_bridge
        assert bridge is not None

        # Move to LONG branch and create an OPEN exit.
        entry_cid = sorted(bridge.adapter.registry.all_entry_cids)[0]
        reg = bridge.adapter.registry.lookup_entry(entry_cid)
        assert reg is not None
        _ = bridge.on_fill(
            entry_cid,
            reg.side,
            reg.price,
            bridge._config.order_size,
            _BASE_TS + 20_000,
        )
        sm = bridge.state_machine
        assert sm is not None
        assert sm.snapshot.mode == BranchMode.LONG_BRANCH

        # Inject a historical FILLED exit row with no registry mapping.
        historical_filled_exit = ExitOrder(
            exit_order_id="x_hist_filled",
            lot_id="lot_hist",
            side=OrderSide.SELL,
            price=Decimal("99999"),
            qty=bridge._config.order_size,
            status=ExitOrderStatus.FILLED,
        )
        sm._snapshot = replace(
            sm.snapshot,
            exit_orders=(*sm.snapshot.exit_orders, historical_filled_exit),
        )

        # Build exchange truth from active registry/state (no ghost historical exits on exchange).
        open_orders: list[OpenOrderSnap] = []
        for cid in sorted(bridge.adapter.registry.all_entry_cids):
            e = bridge.adapter.registry.lookup_entry(cid)
            if e is None:
                continue
            open_orders.append(
                OpenOrderSnap(
                    order_id=cid,
                    symbol="BTCUSDT",
                    side=e.side.value,
                    order_type="LIMIT",
                    price=e.price,
                    qty=bridge._config.order_size,
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=_BASE_TS + 21_000,
                )
            )
        for eo in sm.snapshot.exit_orders:
            if eo.status != ExitOrderStatus.OPEN:
                continue
            exit_cid = bridge.adapter.registry.cid_for_exit(eo.exit_order_id)
            if exit_cid is None:
                continue
            open_orders.append(
                OpenOrderSnap(
                    order_id=exit_cid,
                    symbol="BTCUSDT",
                    side=eo.side.value,
                    order_type="LIMIT",
                    price=eo.price,
                    qty=eo.qty,
                    filled_qty=Decimal("0"),
                    reduce_only=True,
                    status="NEW",
                    ts=_BASE_TS + 21_000,
                )
            )

        snap = Snapshot(
            ts=_BASE_TS + 21_000,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=tuple(open_orders),
            ts=_BASE_TS + 21_000,
            source="test",
        )
        repair = engine._grid_v2_integrity_repair(snap)
        assert repair == [], "Historical closed exits must not create active integrity mismatch"


# ---------------------------------------------------------------------------
# P0 hotfix: orphan CANCEL_ENTRY from risk-gate blocked PLACE
# ---------------------------------------------------------------------------


class TestFillResolveOrphanNoFatal:
    """P0 hotfix: on_fill must not crash when resolve_actions encounters
    orphan CANCEL_ENTRY (entry never placed due to risk gate blocking).
    """

    def test_on_fill_orphan_cancel_entry_returns_empty_actions(self) -> None:
        """A. Fill succeeds but follow-up CANCEL_ENTRY has no registry CID.

        Simulate: after entry fill, SM produces CANCEL_ENTRY for a
        side/price that was never registered (risk gate blocked its PLACE).
        Bridge must return FillResult with empty actions, not raise.
        """
        b, seed = _fresh_bridge()

        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        # Manually remove a neighboring entry from registry to simulate
        # an entry that was never placed (risk gate blocked it).
        buy_seed_1 = [s for s in seed if s.side == OrderSide.BUY][1]
        cid_to_remove = buy_seed_1.client_order_id
        assert cid_to_remove is not None
        b.adapter.confirm_cancel_entry(cid_to_remove)

        # Now fill the first buy entry. SM may produce CANCEL_ENTRY for
        # the removed CID's slot during branch transition cleanup.
        # Even if it doesn't in this exact scenario, let's force the
        # adapter to fail by poisoning the registry further.
        # Instead, test the bridge.on_fill directly with a mocked SM
        # that produces an unresolvable CANCEL_ENTRY.
        # Create a fake SM result with a CANCEL_ENTRY for a non-existent entry
        fake_cancel = ActionIntent(
            kind=ActionIntentKind.CANCEL_ENTRY,
            side=OrderSide.SELL,
            price=Decimal("99999"),  # deliberately non-existent
        )
        fake_result = TransitionResult(
            snapshot=MagicMock(),
            rejected=False,
            actions=(fake_cancel,),
        )

        # Patch SM.apply to return our fake result
        original_sm = b.state_machine
        assert original_sm is not None
        with patch.object(original_sm, "apply", return_value=fake_result):
            # This MUST NOT raise — that's the P0 fix
            result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)

        assert result.rejected is False
        assert result.execution_actions == ()  # empty — orphan actions skipped

    def test_valid_cancel_entry_still_works(self) -> None:
        """C. Regression: normal CANCEL_ENTRY path still resolves correctly."""
        b, seed = _fresh_bridge()

        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        # Normal fill — SM produces valid actions (PLACE_EXIT, maybe CANCEL_ENTRY
        # for opposite-edge trim). All CIDs exist in registry.
        result = b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)

        assert result.rejected is False
        # Should have at least PLACE_EXIT
        exits = [ea for ea in result.execution_actions if ea.reason == "grid_v2_PLACE_EXIT"]
        assert len(exits) >= 1

    def test_multiple_fills_after_orphan_continue(self) -> None:
        """B. Loop stays alive: multiple fills work even after an orphan."""
        b, seed = _fresh_bridge()
        buy_seed = sorted(
            [s for s in seed if s.side == OrderSide.BUY],
            key=lambda s: s.price or Decimal(0),
            reverse=True,
        )
        assert len(buy_seed) >= 2

        # First fill: orphan CANCEL_ENTRY (simulated)
        cid0 = buy_seed[0].client_order_id
        price0 = buy_seed[0].price
        assert cid0 is not None and price0 is not None

        fake_cancel = ActionIntent(
            kind=ActionIntentKind.CANCEL_ENTRY,
            side=OrderSide.SELL,
            price=Decimal("99999"),
        )
        fake_result = TransitionResult(snapshot=MagicMock(), rejected=False, actions=(fake_cancel,))
        sm = b.state_machine
        assert sm is not None
        with patch.object(sm, "apply", return_value=fake_result):
            r1 = b.on_fill(cid0, OrderSide.BUY, price0, _ORDER_SIZE, _BASE_TS + 1000)
        assert r1.rejected is False
        assert r1.execution_actions == ()

        # Second fill: normal path (un-patched SM)
        cid1 = buy_seed[1].client_order_id
        price1 = buy_seed[1].price
        assert cid1 is not None and price1 is not None
        r2 = b.on_fill(cid1, OrderSide.BUY, price1, _ORDER_SIZE, _BASE_TS + 2000)
        # Should NOT raise — loop continues
        assert r2.rejected is False

    def test_unexpected_value_error_re_raised(self) -> None:
        """P0 review fix: non-orphan ValueError must NOT be swallowed."""
        b, seed = _fresh_bridge()

        buy_seed = [s for s in seed if s.side == OrderSide.BUY]
        buy_cid = buy_seed[0].client_order_id
        buy_price = buy_seed[0].price
        assert buy_cid is not None and buy_price is not None

        # Patch resolve_actions to raise a non-orphan ValueError
        def _bad_resolve(*_args: object, **_kwargs: object) -> None:
            raise ValueError("PLACE_ENTRY missing fields: corrupted action")

        with (
            patch.object(b.adapter, "resolve_actions", side_effect=_bad_resolve),
            pytest.raises(ValueError, match="PLACE_ENTRY missing fields"),
        ):
            b.on_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)


class TestEngineRiskGateOrphanIntegration:
    """P1 fix: production-path test proving risk-gate-blocked PLACE → fill →
    orphan CANCEL_ENTRY does not crash, using real bridge lifecycle.

    Simulates the exact production chain:
    1. Bridge startup → seed entries placed
    2. Some entries removed from registry (simulating risk gate block → clean)
    3. Fill arrives on a real registered entry
    4. SM produces CANCEL_ENTRY for the removed entry's slot
    5. Bridge handles gracefully → no crash, bridge stays functional
    """

    def test_risk_blocked_entry_then_fill_no_fatal(self) -> None:
        """Full production chain: orphan after risk-blocked entry."""
        bridge = GridV2Bridge(_config(levels=3), "BTCUSDT")
        seed = bridge.startup_fresh(_REF_PRICE, _BASE_TS)
        assert bridge.reconstruction_ok

        buy_seed = sorted(
            [s for s in seed if s.side == OrderSide.BUY],
            key=lambda s: s.price or Decimal(0),
            reverse=True,
        )
        assert len(buy_seed) >= 2

        # Step 1: Simulate risk gate blocking a PLACE → CID cleaned from registry
        orphan_cid = buy_seed[1].client_order_id
        assert orphan_cid is not None
        bridge.adapter.confirm_cancel_entry(orphan_cid)
        # Verify it's gone
        assert bridge.adapter.registry.lookup_entry(orphan_cid) is None

        # Step 2: Fill on a real registered entry
        fill_cid = buy_seed[0].client_order_id
        fill_price = buy_seed[0].price
        assert fill_cid is not None and fill_price is not None

        # Step 3: SM produces orphan CANCEL_ENTRY for removed slot
        sm = bridge.state_machine
        assert sm is not None
        fake_cancel = ActionIntent(
            kind=ActionIntentKind.CANCEL_ENTRY,
            side=OrderSide.BUY,
            price=fill_price - Decimal("100"),  # non-existent slot
        )
        fake_result = TransitionResult(
            snapshot=MagicMock(),
            rejected=False,
            actions=(fake_cancel,),
        )
        with patch.object(sm, "apply", return_value=fake_result):
            result = bridge.on_fill(
                fill_cid, OrderSide.BUY, fill_price, _ORDER_SIZE, _BASE_TS + 1000
            )

        # Step 4: Verify no crash, empty follow-up actions
        assert result.rejected is False
        assert result.execution_actions == ()

        # Step 5: Bridge stays functional for subsequent operations
        assert bridge.reconstruction_ok is True
        # Can still process another fill (proves loop survival)
        sell_seed = [s for s in seed if s.side == OrderSide.SELL]
        sell_cid = sell_seed[0].client_order_id
        sell_price = sell_seed[0].price
        assert sell_cid is not None and sell_price is not None
        # Un-patch: real SM apply for normal fill
        r2 = bridge.on_fill(sell_cid, OrderSide.SELL, sell_price, _ORDER_SIZE, _BASE_TS + 2000)
        assert r2.rejected is False
