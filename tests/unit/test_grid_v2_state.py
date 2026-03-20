"""Tests for grid_v2 pure state machine.

SSOT: docs/27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md (sections 4-21).
Acceptance tests T1-T20 + edge cases from section 18 and PR2 contract (section 21).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.core import OrderSide
from grinder.grid_v2.state import (
    ActionIntentKind,
    BranchMode,
    EmergencyStopTriggered,
    EntryFilled,
    EntryWindow,
    ExitFilled,
    ExitOrder,
    ExitOrderStatus,
    GridV2Config,
    GridV2InvariantError,
    GridV2Snapshot,
    GridV2StateMachine,
    InventoryLot,
    LotSide,
    LotStatus,
    OperatorCleanup,
    RecenterRequested,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_BASE_TS = 1_000_000
_REF_PRICE = Decimal("100")
_STEP = Decimal("0.01")  # 1%
_ORDER_SIZE = Decimal("1")


def _config(
    *,
    step: Decimal = _STEP,
    levels: int = 3,
    order_size: Decimal = _ORDER_SIZE,
    max_levels: int = 5,
    max_notional: Decimal = Decimal("10000"),
) -> GridV2Config:
    return GridV2Config(
        grid_step_pct=step,
        entry_levels_per_side=levels,
        order_size=order_size,
        max_inventory_levels=max_levels,
        max_inventory_notional_usd=max_notional,
    )


def _sm(
    ref: Decimal = _REF_PRICE,
    ts: int = _BASE_TS,
    cfg: GridV2Config | None = None,
) -> GridV2StateMachine:
    return GridV2StateMachine.create_initial(cfg or _config(), ref, ts)


def _entry_filled(
    side: OrderSide = OrderSide.BUY,
    price: Decimal | None = None,
    qty: Decimal = _ORDER_SIZE,
    order_id: str = "E1",
    ts: int = _BASE_TS + 1,
) -> EntryFilled:
    return EntryFilled(
        order_id=order_id,
        side=side,
        price=price if price is not None else _REF_PRICE * (Decimal(1) - _STEP),
        qty=qty,
        ts=ts,
    )


def _exit_filled(
    exit_order_id: str,
    lot_id: str,
    qty: Decimal = _ORDER_SIZE,
    price: Decimal | None = None,
    ts: int = _BASE_TS + 2,
) -> ExitFilled:
    return ExitFilled(
        exit_order_id=exit_order_id,
        lot_id=lot_id,
        price=price or _REF_PRICE,
        qty=qty,
        ts=ts,
    )


# ---------------------------------------------------------------------------
# T1: Initial placement (section 9, 21.3)
# ---------------------------------------------------------------------------


class TestInitialPlacement:
    def test_symmetric_window(self) -> None:
        sm = _sm()
        snap = sm.snapshot
        assert snap.mode == BranchMode.FLAT
        assert len(snap.entry_window.buy_entry_prices) == 3
        assert len(snap.entry_window.sell_entry_prices) == 3
        assert snap.open_lots == ()
        assert snap.closed_lots == ()
        assert snap.exit_orders == ()
        assert snap.emergency_stopped is False

    def test_buy_prices_descending(self) -> None:
        snap = _sm().snapshot
        buys = snap.entry_window.buy_entry_prices
        # Closest to ref first (highest buy)
        assert buys[0] > buys[1] > buys[2]

    def test_sell_prices_ascending(self) -> None:
        snap = _sm().snapshot
        sells = snap.entry_window.sell_entry_prices
        # Closest to ref first (lowest sell)
        assert sells[0] < sells[1] < sells[2]

    def test_correct_spacing(self) -> None:
        sm = _sm()
        buys = sm.snapshot.entry_window.buy_entry_prices
        sells = sm.snapshot.entry_window.sell_entry_prices
        # Buy level 1 = ref * (1 - 0.01*1) = 99
        assert buys[0] == _REF_PRICE * (Decimal(1) - _STEP)
        # Buy level 2 = ref * (1 - 0.01*2) = 98
        assert buys[1] == _REF_PRICE * (Decimal(1) - _STEP * 2)
        # Sell level 1 = ref * (1 + 0.01*1) = 101
        assert sells[0] == _REF_PRICE * (Decimal(1) + _STEP)


# ---------------------------------------------------------------------------
# T2/T3: First entry fill (11.1, 11.2)
# ---------------------------------------------------------------------------


class TestFirstEntryFill:
    def test_buy_creates_long_branch(self) -> None:
        """T2: BUY fill -> LONG_BRANCH + SELL exit + opposite side trimmed by 1."""
        sm = _sm()
        initial_sells = sm.snapshot.entry_window.sell_entry_prices
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1))

        assert not result.rejected
        snap = result.snapshot
        assert snap.mode == BranchMode.LONG_BRANCH
        assert len(snap.open_lots) == 1
        assert snap.open_lots[0].side == LotSide.LONG
        assert snap.open_lots[0].entry_price == buy_price
        assert len(snap.exit_orders) == 1
        assert snap.exit_orders[0].side == OrderSide.SELL
        # Opposite side keeps nearest levels, far edge is trimmed.
        assert snap.entry_window.sell_entry_prices == initial_sells[:-1]

    def test_sell_creates_short_branch(self) -> None:
        """T3: SELL fill -> SHORT_BRANCH + BUY exit + opposite side trimmed by 1."""
        sm = _sm()
        initial_buys = sm.snapshot.entry_window.buy_entry_prices
        sell_price = sm.snapshot.entry_window.sell_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.SELL, sell_price, _ORDER_SIZE, _BASE_TS + 1))

        assert not result.rejected
        snap = result.snapshot
        assert snap.mode == BranchMode.SHORT_BRANCH
        assert len(snap.open_lots) == 1
        assert snap.open_lots[0].side == LotSide.SHORT
        assert len(snap.exit_orders) == 1
        assert snap.exit_orders[0].side == OrderSide.BUY
        # Opposite side keeps nearest levels, far edge is trimmed.
        assert snap.entry_window.buy_entry_prices == initial_buys[:-1]

    def test_action_order_correct(self) -> None:
        """Actions: PLACE_EXIT -> CANCEL_ENTRY(s) -> PLACE_ENTRY."""
        sm = _sm()
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1))

        kinds = [a.kind for a in result.actions]
        # PLACE_EXIT first, then CANCEL_ENTRY(s) for opposite side, then PLACE_ENTRY
        assert kinds[0] == ActionIntentKind.PLACE_EXIT
        cancel_idx = [i for i, k in enumerate(kinds) if k == ActionIntentKind.CANCEL_ENTRY]
        place_entry_idx = [i for i, k in enumerate(kinds) if k == ActionIntentKind.PLACE_ENTRY]
        assert all(ci < pi for ci in cancel_idx for pi in place_entry_idx)


# ---------------------------------------------------------------------------
# T4/T5: Branch continuation (11.3, 11.4)
# ---------------------------------------------------------------------------


class TestBranchContinuation:
    def test_long_continuation(self) -> None:
        """T4: Second BUY in LONG_BRANCH continues and trims opposite far edge."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        sells_before = sm.snapshot.entry_window.sell_entry_prices
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))

        assert not result.rejected
        assert result.snapshot.mode == BranchMode.LONG_BRANCH
        assert len(result.snapshot.open_lots) == 2
        # One opposite far-edge trim per fill.
        cancel_entries = [a for a in result.actions if a.kind == ActionIntentKind.CANCEL_ENTRY]
        assert len(cancel_entries) == 1
        assert cancel_entries[0].side == OrderSide.SELL
        assert cancel_entries[0].reason == "ROLLING_TRIM"
        assert cancel_entries[0].price == sells_before[-1]
        assert result.snapshot.entry_window.sell_entry_prices == sells_before[:-1]

    def test_short_continuation(self) -> None:
        """T5: Second SELL in SHORT_BRANCH continues."""
        sm = _sm()
        sell1 = sm.snapshot.entry_window.sell_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.SELL, sell1, _ORDER_SIZE, _BASE_TS + 1))
        sell2 = sm.snapshot.entry_window.sell_entry_prices[0]
        result = sm.apply(EntryFilled("E2", OrderSide.SELL, sell2, _ORDER_SIZE, _BASE_TS + 2))

        assert not result.rejected
        assert result.snapshot.mode == BranchMode.SHORT_BRANCH
        assert len(result.snapshot.open_lots) == 2

    def test_window_bounded_after_fills(self) -> None:
        """I1: entry window never exceeds levels_per_side."""
        sm = _sm()
        for i in range(3):
            buy = sm.snapshot.entry_window.buy_entry_prices[0]
            sm.apply(EntryFilled(f"E{i}", OrderSide.BUY, buy, _ORDER_SIZE, _BASE_TS + i + 1))
            assert len(sm.snapshot.entry_window.buy_entry_prices) <= 3


# ---------------------------------------------------------------------------
# T6/T7: Exit fill (11.5, 11.6)
# ---------------------------------------------------------------------------


class TestExitFill:
    def test_long_exit_closes_lot(self) -> None:
        """T6: Exit fill closes correct lot."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))

        lot = sm.snapshot.open_lots[0]
        exit_eo = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot.lot_id)
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 3)
        )

        assert not result.rejected
        assert len(result.snapshot.open_lots) == 1  # one lot remains
        assert len(result.snapshot.closed_lots) == 1
        assert result.snapshot.closed_lots[0].lot_id == lot.lot_id

    def test_long_exit_restores_both_sides(self) -> None:
        """T6: Exit fill in LONG branch restores SELL far edge + BUY near edge."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))

        pre_exit_window = sm.snapshot.entry_window
        lot = sm.snapshot.open_lots[0]
        exit_eo = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot.lot_id)
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 3)
        )

        assert not result.rejected
        assert len(result.actions) == 3
        sell_action = next(
            a
            for a in result.actions
            if a.kind == ActionIntentKind.PLACE_ENTRY and a.side == OrderSide.SELL
        )
        buy_action = next(
            a
            for a in result.actions
            if a.kind == ActionIntentKind.PLACE_ENTRY and a.side == OrderSide.BUY
        )
        buy_cancel = next(
            a
            for a in result.actions
            if a.kind == ActionIntentKind.CANCEL_ENTRY and a.side == OrderSide.BUY
        )
        assert sell_action.kind == ActionIntentKind.PLACE_ENTRY
        assert buy_action.kind == ActionIntentKind.PLACE_ENTRY
        assert sell_action.reason == "EXIT_RESTORE"
        assert buy_action.reason == "EXIT_RESTORE"
        expected_sells = (
            *pre_exit_window.sell_entry_prices,
            pre_exit_window.sell_entry_prices[-1] + pre_exit_window.reference_price * _STEP,
        )
        expected_buys = (
            pre_exit_window.buy_entry_prices[0] + pre_exit_window.reference_price * _STEP,
            *pre_exit_window.buy_entry_prices[:-1],
        )
        assert result.snapshot.mode == BranchMode.LONG_BRANCH
        assert result.snapshot.entry_window.sell_entry_prices == expected_sells
        assert result.snapshot.entry_window.buy_entry_prices == expected_buys
        assert sell_action.price == expected_sells[-1]
        assert buy_action.price == expected_buys[0]
        assert buy_cancel.price == pre_exit_window.buy_entry_prices[-1]
        assert buy_cancel.reason == "EXIT_RESTORE_SHIFT"
        assert len(result.snapshot.open_lots) == 1

    def test_short_exit_closes_lot(self) -> None:
        """T7: Short exit fill closes correct lot."""
        sm = _sm()
        sell1 = sm.snapshot.entry_window.sell_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.SELL, sell1, _ORDER_SIZE, _BASE_TS + 1))
        sell2 = sm.snapshot.entry_window.sell_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.SELL, sell2, _ORDER_SIZE, _BASE_TS + 2))

        lot = sm.snapshot.open_lots[0]
        exit_eo = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot.lot_id)
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 3)
        )

        assert not result.rejected
        assert len(result.snapshot.open_lots) == 1
        assert len(result.snapshot.closed_lots) == 1

    def test_short_exit_restores_both_sides(self) -> None:
        """T7: Exit fill in SHORT branch restores BUY far edge + SELL near edge."""
        sm = _sm()
        sell1 = sm.snapshot.entry_window.sell_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.SELL, sell1, _ORDER_SIZE, _BASE_TS + 1))
        sell2 = sm.snapshot.entry_window.sell_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.SELL, sell2, _ORDER_SIZE, _BASE_TS + 2))

        pre_exit_window = sm.snapshot.entry_window
        lot = sm.snapshot.open_lots[0]
        exit_eo = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot.lot_id)
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 3)
        )

        assert not result.rejected
        assert len(result.actions) == 3
        buy_action = next(
            a
            for a in result.actions
            if a.kind == ActionIntentKind.PLACE_ENTRY and a.side == OrderSide.BUY
        )
        sell_action = next(
            a
            for a in result.actions
            if a.kind == ActionIntentKind.PLACE_ENTRY and a.side == OrderSide.SELL
        )
        sell_cancel = next(
            a
            for a in result.actions
            if a.kind == ActionIntentKind.CANCEL_ENTRY and a.side == OrderSide.SELL
        )
        assert buy_action.kind == ActionIntentKind.PLACE_ENTRY
        assert sell_action.kind == ActionIntentKind.PLACE_ENTRY
        assert buy_action.reason == "EXIT_RESTORE"
        assert sell_action.reason == "EXIT_RESTORE"
        expected_buys = (
            *pre_exit_window.buy_entry_prices,
            pre_exit_window.buy_entry_prices[-1] - pre_exit_window.reference_price * _STEP,
        )
        expected_sells = (
            pre_exit_window.sell_entry_prices[0] - pre_exit_window.reference_price * _STEP,
            *pre_exit_window.sell_entry_prices[:-1],
        )
        assert result.snapshot.mode == BranchMode.SHORT_BRANCH
        assert result.snapshot.entry_window.buy_entry_prices == expected_buys
        assert result.snapshot.entry_window.sell_entry_prices == expected_sells
        assert buy_action.price == expected_buys[-1]
        assert sell_action.price == expected_sells[0]
        assert sell_cancel.price == pre_exit_window.sell_entry_prices[-1]
        assert sell_cancel.reason == "EXIT_RESTORE_SHIFT"
        assert len(result.snapshot.open_lots) == 1


# ---------------------------------------------------------------------------
# ExitFilled pair-integrity (21.5)
# ---------------------------------------------------------------------------


class TestExitPairIntegrity:
    def _setup_two_lots(self) -> GridV2StateMachine:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))
        return sm

    def test_unknown_lot_rejected(self) -> None:
        sm = self._setup_two_lots()
        result = sm.apply(
            ExitFilled("exit-E1", "lot-NONEXISTENT", _REF_PRICE, _ORDER_SIZE, _BASE_TS + 3)
        )
        assert result.rejected
        assert result.reject_reason == "UNKNOWN_LOT_ID"

    def test_unknown_exit_rejected(self) -> None:
        sm = self._setup_two_lots()
        lot = sm.snapshot.open_lots[0]
        result = sm.apply(
            ExitFilled("exit-NONEXISTENT", lot.lot_id, _REF_PRICE, _ORDER_SIZE, _BASE_TS + 3)
        )
        assert result.rejected
        assert result.reject_reason == "UNKNOWN_EXIT_ORDER_ID"

    def test_exit_lot_mismatch_rejected(self) -> None:
        sm = self._setup_two_lots()
        lot1 = sm.snapshot.open_lots[0]
        lot2 = sm.snapshot.open_lots[1]
        # Use exit for lot2 but claim lot1
        exit_for_lot2 = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot2.lot_id)
        result = sm.apply(
            ExitFilled(
                exit_for_lot2.exit_order_id, lot1.lot_id, _REF_PRICE, _ORDER_SIZE, _BASE_TS + 3
            )
        )
        assert result.rejected
        assert result.reject_reason == "EXIT_LOT_MISMATCH"

    def test_lot_exit_mismatch_rejected(self) -> None:
        sm = self._setup_two_lots()
        lot1 = sm.snapshot.open_lots[0]
        lot2 = sm.snapshot.open_lots[1]
        exit_for_lot1 = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot1.lot_id)
        # Use exit for lot1 but claim lot2
        result = sm.apply(
            ExitFilled(
                exit_for_lot1.exit_order_id, lot2.lot_id, _REF_PRICE, _ORDER_SIZE, _BASE_TS + 3
            )
        )
        assert result.rejected
        assert result.reject_reason == "EXIT_LOT_MISMATCH"


# ---------------------------------------------------------------------------
# T8: Full unwind (11.5, 21.1)
# ---------------------------------------------------------------------------


class TestFullUnwind:
    def test_last_exit_goes_flat(self) -> None:
        """T8: Last exit -> FLAT, entry window reseeds symmetrically."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        window_before_exit = sm.snapshot.entry_window

        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 2)
        )

        assert not result.rejected
        snap = result.snapshot
        assert snap.mode == BranchMode.FLAT
        assert snap.open_lots == ()
        assert len(snap.closed_lots) == 1
        assert any(a.kind == ActionIntentKind.CANCEL_ENTRY for a in result.actions)
        assert any(a.kind == ActionIntentKind.PLACE_ENTRY for a in result.actions)

        fresh = GridV2StateMachine.create_initial(
            _config(), window_before_exit.reference_price, _BASE_TS + 2
        )
        assert snap.entry_window.buy_entry_prices == fresh.snapshot.entry_window.buy_entry_prices
        assert snap.entry_window.sell_entry_prices == fresh.snapshot.entry_window.sell_entry_prices
        assert snap.entry_window.reference_price == window_before_exit.reference_price

    def test_short_last_exit_goes_flat(self) -> None:
        """T8: Short unwind to FLAT reseeds the symmetric entry window."""
        sm = _sm()
        sell1 = sm.snapshot.entry_window.sell_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.SELL, sell1, _ORDER_SIZE, _BASE_TS + 1))
        window_before_exit = sm.snapshot.entry_window

        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 2)
        )

        assert not result.rejected
        snap = result.snapshot
        assert snap.mode == BranchMode.FLAT
        assert snap.open_lots == ()
        assert len(snap.closed_lots) == 1
        assert any(a.kind == ActionIntentKind.CANCEL_ENTRY for a in result.actions)
        assert any(a.kind == ActionIntentKind.PLACE_ENTRY for a in result.actions)

        fresh = GridV2StateMachine.create_initial(
            _config(), window_before_exit.reference_price, _BASE_TS + 2
        )
        assert snap.entry_window.buy_entry_prices == fresh.snapshot.entry_window.buy_entry_prices
        assert snap.entry_window.sell_entry_prices == fresh.snapshot.entry_window.sell_entry_prices
        assert snap.entry_window.reference_price == window_before_exit.reference_price

    def test_entry_after_flat_reseed_reactivates_consumed_price(self) -> None:
        """After full unwind, the consumed price becomes active again."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 2)
        )

        result = sm.apply(EntryFilled("E2", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 3))
        assert not result.rejected
        assert result.snapshot.mode == BranchMode.LONG_BRANCH
        assert len(result.snapshot.open_lots) == 1

    def test_multi_lot_unwind(self) -> None:
        """Multiple lots unwound one by one."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))

        # Close first lot
        lot1 = sm.snapshot.open_lots[0]
        exit1 = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot1.lot_id)
        r1 = sm.apply(
            ExitFilled(exit1.exit_order_id, lot1.lot_id, lot1.exit_price, _ORDER_SIZE, _BASE_TS + 3)
        )
        assert not r1.rejected
        assert r1.snapshot.mode == BranchMode.LONG_BRANCH
        assert len(r1.snapshot.open_lots) == 1

        # Close second lot -> FLAT
        lot2 = sm.snapshot.open_lots[0]
        exit2 = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot2.lot_id)
        r2 = sm.apply(
            ExitFilled(exit2.exit_order_id, lot2.lot_id, lot2.exit_price, _ORDER_SIZE, _BASE_TS + 4)
        )
        assert not r2.rejected
        assert r2.snapshot.mode == BranchMode.FLAT
        assert r2.snapshot.open_lots == ()
        assert any(a.kind == ActionIntentKind.CANCEL_ENTRY for a in r2.actions)
        assert any(a.kind == ActionIntentKind.PLACE_ENTRY for a in r2.actions)
        assert len(r2.snapshot.entry_window.buy_entry_prices) == _config().entry_levels_per_side
        assert len(r2.snapshot.entry_window.sell_entry_prices) == _config().entry_levels_per_side


# ---------------------------------------------------------------------------
# T9/T10: Recenter (section 13, 21.8, 21.4)
# ---------------------------------------------------------------------------


class TestRecenter:
    def _make_flat_after_unwind(self) -> GridV2StateMachine:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 2)
        )
        return sm

    def test_flat_recenter_rebuilds_window(self) -> None:
        """T9: Recenter in FLAT rebuilds window, identical to create_initial."""
        sm = self._make_flat_after_unwind()
        new_ref = Decimal("105")
        result = sm.apply(RecenterRequested(new_ref, _BASE_TS + 3))

        assert not result.rejected
        snap = result.snapshot
        assert snap.entry_window.reference_price == new_ref
        assert len(snap.entry_window.buy_entry_prices) == 3
        assert len(snap.entry_window.sell_entry_prices) == 3

        # Should be identical to create_initial with same ref
        fresh = GridV2StateMachine.create_initial(_config(), new_ref, _BASE_TS + 3)
        assert snap.entry_window.buy_entry_prices == fresh.snapshot.entry_window.buy_entry_prices
        assert snap.entry_window.sell_entry_prices == fresh.snapshot.entry_window.sell_entry_prices

    def test_blocked_with_inventory(self) -> None:
        """T10: Recenter rejected when lots exist."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        result = sm.apply(RecenterRequested(Decimal("105"), _BASE_TS + 2))
        assert result.rejected
        assert result.reject_reason == "RECENTER_NOT_FLAT"

    def test_blocked_with_open_exits(self) -> None:
        """Recenter rejected when open exits exist."""
        # Need a state with mode=FLAT but open exits — not reachable normally
        # because FLAT implies no open lots which implies no open exits.
        # Test the precondition directly via the rejection.
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        result = sm.apply(RecenterRequested(Decimal("105"), _BASE_TS + 2))
        assert result.rejected  # RECENTER_NOT_FLAT covers this

    def test_same_reference_snapshot_idempotent(self) -> None:
        """Repeated recenter with same reference: snapshot-idempotent, actions emitted (21.4)."""
        sm = self._make_flat_after_unwind()
        new_ref = Decimal("105")
        r1 = sm.apply(RecenterRequested(new_ref, _BASE_TS + 3))
        snap_after_first = r1.snapshot

        r2 = sm.apply(RecenterRequested(new_ref, _BASE_TS + 4))
        snap_after_second = r2.snapshot

        # Snapshot is the same (ignoring last_recenter_ts which updates)
        assert (
            snap_after_first.entry_window.buy_entry_prices
            == snap_after_second.entry_window.buy_entry_prices
        )
        assert (
            snap_after_first.entry_window.sell_entry_prices
            == snap_after_second.entry_window.sell_entry_prices
        )
        assert snap_after_first.mode == snap_after_second.mode

        # But actions are NOT a no-op — CANCEL_ENTRY + PLACE_ENTRY emitted
        assert len(r2.actions) > 0
        cancel_actions = [a for a in r2.actions if a.kind == ActionIntentKind.CANCEL_ENTRY]
        place_actions = [a for a in r2.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
        assert len(cancel_actions) > 0  # cancel old entries
        assert len(place_actions) > 0  # place new entries


# ---------------------------------------------------------------------------
# T11: Mixed inventory forbidden (I4)
# ---------------------------------------------------------------------------


class TestMixedInventoryForbidden:
    def test_buy_in_short_rejected(self) -> None:
        sm = _sm()
        sell1 = sm.snapshot.entry_window.sell_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.SELL, sell1, _ORDER_SIZE, _BASE_TS + 1))
        # Try BUY in SHORT_BRANCH
        buy_price = (
            sm.snapshot.entry_window.buy_entry_prices[0]
            if sm.snapshot.entry_window.buy_entry_prices
            else _REF_PRICE * Decimal("0.99")
        )
        result = sm.apply(EntryFilled("E2", OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 2))
        assert result.rejected
        assert result.reject_reason == "BRANCH_INCOMPATIBLE"

    def test_sell_in_long_rejected(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        sell_price = (
            sm.snapshot.entry_window.sell_entry_prices[0]
            if sm.snapshot.entry_window.sell_entry_prices
            else _REF_PRICE * Decimal("1.01")
        )
        result = sm.apply(EntryFilled("E2", OrderSide.SELL, sell_price, _ORDER_SIZE, _BASE_TS + 2))
        assert result.rejected
        assert result.reject_reason == "BRANCH_INCOMPATIBLE"


# ---------------------------------------------------------------------------
# T12: Exit never canceled (I2)
# ---------------------------------------------------------------------------


class TestExitNeverCanceled:
    def test_entry_fill_no_cancel_exit(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        cancel_exits = [a for a in result.actions if a.kind == ActionIntentKind.CANCEL_EXIT]
        assert cancel_exits == []

    def test_recenter_no_cancel_exit(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 2)
        )
        result = sm.apply(RecenterRequested(Decimal("105"), _BASE_TS + 3))
        cancel_exits = [a for a in result.actions if a.kind == ActionIntentKind.CANCEL_EXIT]
        assert cancel_exits == []


# ---------------------------------------------------------------------------
# Active-entry validation (21.6)
# ---------------------------------------------------------------------------


class TestActiveEntryValidation:
    def test_buy_at_unknown_price_rejected(self) -> None:
        sm = _sm()
        result = sm.apply(
            EntryFilled("E1", OrderSide.BUY, Decimal("50"), _ORDER_SIZE, _BASE_TS + 1)
        )
        assert result.rejected
        assert result.reject_reason == "PRICE_NOT_IN_ACTIVE_WINDOW"

    def test_sell_at_unknown_price_rejected(self) -> None:
        sm = _sm()
        result = sm.apply(
            EntryFilled("E1", OrderSide.SELL, Decimal("200"), _ORDER_SIZE, _BASE_TS + 1)
        )
        assert result.rejected
        assert result.reject_reason == "PRICE_NOT_IN_ACTIVE_WINDOW"

    def test_grid_cid_entry_fill_outside_window_is_accepted(self) -> None:
        """Late fill on grid CID remains processable after window shift."""
        sm = _sm()
        result = sm.apply(
            EntryFilled(
                "g_g_PIPPINUSDT_e5_1773952851_0",
                OrderSide.SELL,
                Decimal("200"),
                _ORDER_SIZE,
                _BASE_TS + 1,
            )
        )
        assert not result.rejected
        assert result.snapshot.mode == BranchMode.SHORT_BRANCH
        assert len(result.snapshot.open_lots) == 1

    def test_sell_after_flat_reseed_reactivates_consumed_price(self) -> None:
        """After full unwind, the consumed SELL price becomes active again."""
        sm = _sm()
        sell1 = sm.snapshot.entry_window.sell_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.SELL, sell1, _ORDER_SIZE, _BASE_TS + 1))
        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 2)
        )
        result = sm.apply(EntryFilled("E2", OrderSide.SELL, sell1, _ORDER_SIZE, _BASE_TS + 3))
        assert not result.rejected
        assert result.snapshot.mode == BranchMode.SHORT_BRANCH
        assert len(result.snapshot.open_lots) == 1


# ---------------------------------------------------------------------------
# T13-T15: Idempotency (I8, 21.4)
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_duplicate_entry_fill_rejected(self) -> None:
        """T13: Same order_id already sourced a lot."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        # Try again with same order_id
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))
        assert result.rejected
        assert result.reject_reason == "DUPLICATE_ENTRY_FILL"

    def test_duplicate_exit_fill_rejected(self) -> None:
        """T14: ExitFilled with already-filled exit order."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))

        lot = sm.snapshot.open_lots[0]
        exit_eo = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot.lot_id)
        sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 3)
        )

        # Try same exit again
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 4)
        )
        assert result.rejected
        assert result.reject_reason == "LOT_ALREADY_CLOSED"

    def test_exit_closes_exactly_one_lot(self) -> None:
        """T15: Only the targeted lot is closed."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))

        lot1 = sm.snapshot.open_lots[0]
        exit1 = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot1.lot_id)
        result = sm.apply(
            ExitFilled(exit1.exit_order_id, lot1.lot_id, lot1.exit_price, _ORDER_SIZE, _BASE_TS + 3)
        )

        assert len(result.snapshot.open_lots) == 1
        assert result.snapshot.open_lots[0].lot_id != lot1.lot_id


# ---------------------------------------------------------------------------
# Idempotency expanded (21.4)
# ---------------------------------------------------------------------------


class TestIdempotencyExpanded:
    def test_exit_after_cleanup_rejected(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        sm.apply(OperatorCleanup(_BASE_TS + 2))
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 3)
        )
        assert result.rejected
        assert result.reject_reason == "LOT_ALREADY_CLOSED"

    def test_repeated_emergency_stop_noop(self) -> None:
        sm = _sm()
        r1 = sm.apply(EmergencyStopTriggered(_BASE_TS + 1))
        assert not r1.rejected
        assert r1.snapshot.emergency_stopped is True

        r2 = sm.apply(EmergencyStopTriggered(_BASE_TS + 2))
        assert not r2.rejected
        assert r2.actions == ()
        assert r2.snapshot == r1.snapshot

    def test_repeated_cleanup_noop(self) -> None:
        sm = _sm()
        # First cleanup: FLAT, no lots, no exits, but has entries -> not a no-op
        r1 = sm.apply(OperatorCleanup(_BASE_TS + 1))
        # Now truly empty
        r2 = sm.apply(OperatorCleanup(_BASE_TS + 2))
        assert not r2.rejected
        assert r2.actions == ()
        assert r2.snapshot == r1.snapshot


# ---------------------------------------------------------------------------
# T16: Snapshot reconstruction (I6)
# ---------------------------------------------------------------------------


class TestSnapshotReconstruction:
    def test_reconstruction_identity(self) -> None:
        """SM(config, snap_A).snapshot == snap_A for valid snapshot."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))

        snap_a = sm.snapshot
        cfg = _config()
        reconstructed = GridV2StateMachine(cfg, snap_a)
        assert reconstructed.snapshot == snap_a

    def test_reconstruction_produces_same_result(self) -> None:
        """Apply same event to original and reconstructed -> same output."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))

        snap_a = sm.snapshot
        cfg = _config()

        # Apply same event to both
        buy2 = snap_a.entry_window.buy_entry_prices[0]
        event = EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2)

        r_original = sm.apply(event)
        r_reconstructed = GridV2StateMachine(cfg, snap_a).apply(event)

        assert r_original.snapshot == r_reconstructed.snapshot
        assert r_original.actions == r_reconstructed.actions


# ---------------------------------------------------------------------------
# Constructor validation (21.15)
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_flat_with_open_lots_rejected(self) -> None:
        cfg = _config()
        lot = InventoryLot(
            lot_id="lot-X",
            side=LotSide.LONG,
            entry_price=Decimal("99"),
            qty=Decimal("1"),
            opened_at_ts=_BASE_TS,
            source_entry_order_id="X",
            exit_price=Decimal("100"),
            exit_order_id="exit-X",
            status=LotStatus.OPEN,
        )
        exit_order = ExitOrder(
            exit_order_id="exit-X",
            lot_id="lot-X",
            side=OrderSide.SELL,
            price=Decimal("100"),
            qty=Decimal("1"),
            status=ExitOrderStatus.OPEN,
        )
        snap = GridV2Snapshot(
            mode=BranchMode.FLAT,  # FLAT but has open lots -> I4 violation
            entry_window=EntryWindow(Decimal("100"), (), (), 3, _STEP),
            open_lots=(lot,),
            closed_lots=(),
            exit_orders=(exit_order,),
            emergency_stopped=False,
            last_recenter_ts=None,
        )
        with pytest.raises(GridV2InvariantError, match="I4"):
            GridV2StateMachine(cfg, snap)

    def test_open_lot_without_exit_rejected(self) -> None:
        cfg = _config()
        lot = InventoryLot(
            lot_id="lot-X",
            side=LotSide.LONG,
            entry_price=Decimal("99"),
            qty=Decimal("1"),
            opened_at_ts=_BASE_TS,
            source_entry_order_id="X",
            exit_price=Decimal("100"),
            exit_order_id="exit-X",
            status=LotStatus.OPEN,
        )
        snap = GridV2Snapshot(
            mode=BranchMode.LONG_BRANCH,
            entry_window=EntryWindow(Decimal("100"), (), (), 3, _STEP),
            open_lots=(lot,),
            closed_lots=(),
            exit_orders=(),  # No exit order -> I3 violation
            emergency_stopped=False,
            last_recenter_ts=None,
        )
        with pytest.raises(GridV2InvariantError, match="I3"):
            GridV2StateMachine(cfg, snap)

    def test_window_exceeding_levels_rejected(self) -> None:
        cfg = _config(levels=2)
        snap = GridV2Snapshot(
            mode=BranchMode.FLAT,
            entry_window=EntryWindow(
                Decimal("100"),
                (Decimal("99"), Decimal("98"), Decimal("97")),  # 3 > 2
                (),
                2,
                _STEP,
            ),
            open_lots=(),
            closed_lots=(),
            exit_orders=(),
            emergency_stopped=False,
            last_recenter_ts=None,
        )
        with pytest.raises(GridV2InvariantError, match="I1"):
            GridV2StateMachine(cfg, snap)

    def test_long_branch_with_short_lot_rejected(self) -> None:
        cfg = _config()
        lot = InventoryLot(
            lot_id="lot-X",
            side=LotSide.SHORT,  # SHORT in LONG_BRANCH -> I4
            entry_price=Decimal("101"),
            qty=Decimal("1"),
            opened_at_ts=_BASE_TS,
            source_entry_order_id="X",
            exit_price=Decimal("100"),
            exit_order_id="exit-X",
            status=LotStatus.OPEN,
        )
        exit_order = ExitOrder(
            exit_order_id="exit-X",
            lot_id="lot-X",
            side=OrderSide.BUY,
            price=Decimal("100"),
            qty=Decimal("1"),
            status=ExitOrderStatus.OPEN,
        )
        snap = GridV2Snapshot(
            mode=BranchMode.LONG_BRANCH,
            entry_window=EntryWindow(Decimal("100"), (), (), 3, _STEP),
            open_lots=(lot,),
            closed_lots=(),
            exit_orders=(exit_order,),
            emergency_stopped=False,
            last_recenter_ts=None,
        )
        with pytest.raises(GridV2InvariantError, match="I4"):
            GridV2StateMachine(cfg, snap)


# ---------------------------------------------------------------------------
# T17/T18: Risk guards (section 16)
# ---------------------------------------------------------------------------


class TestRiskGuards:
    def test_max_levels_rejection(self) -> None:
        """T17: Reject when at max inventory levels."""
        cfg = _config(max_levels=1, max_notional=Decimal("100000"))
        sm = GridV2StateMachine.create_initial(cfg, _REF_PRICE, _BASE_TS)
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))

        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))
        assert result.rejected
        assert result.reject_reason == "MAX_INVENTORY_LEVELS"

    def test_max_notional_rejection(self) -> None:
        """T18: Reject when projected notional exceeds limit."""
        cfg = _config(max_notional=Decimal("100"), max_levels=10)
        sm = GridV2StateMachine.create_initial(cfg, _REF_PRICE, _BASE_TS)
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        # qty=1, price~99, notional=99 < 100
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))

        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        # projected = 99 + ~98 = 197 > 100
        result = sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))
        assert result.rejected
        assert result.reject_reason == "MAX_INVENTORY_NOTIONAL_USD"

    def test_grid_cid_bypasses_max_levels_rejection(self) -> None:
        cfg = _config(max_levels=1, max_notional=Decimal("100000"))
        sm = GridV2StateMachine.create_initial(cfg, _REF_PRICE, _BASE_TS)
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(
            EntryFilled("g_g_PIPPINUSDT_e1_1773952851_0", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1)
        )

        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(
            EntryFilled("g_g_PIPPINUSDT_e2_1773952852_0", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2)
        )
        assert not result.rejected
        assert len(result.snapshot.open_lots) == 2

    def test_grid_cid_bypasses_max_notional_rejection(self) -> None:
        cfg = _config(max_notional=Decimal("100"), max_levels=10)
        sm = GridV2StateMachine.create_initial(cfg, _REF_PRICE, _BASE_TS)
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(
            EntryFilled("g_g_PIPPINUSDT_e1_1773952851_0", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1)
        )

        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(
            EntryFilled("g_g_PIPPINUSDT_e2_1773952852_0", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2)
        )
        assert not result.rejected


# ---------------------------------------------------------------------------
# T19/T20: Emergency stop (section 16, 21.2, 21.7, 21.16)
# ---------------------------------------------------------------------------


class TestEmergencyStop:
    def test_blocks_entries(self) -> None:
        """T19: Emergency stop blocks new entries."""
        sm = _sm()
        sm.apply(EmergencyStopTriggered(_BASE_TS + 1))
        buy1 = Decimal("99")  # Any price, will be rejected by EMERGENCY_STOPPED first
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 2))
        assert result.rejected
        assert result.reject_reason == "EMERGENCY_STOPPED"

    def test_blocks_recenter(self) -> None:
        sm = _sm()
        sm.apply(EmergencyStopTriggered(_BASE_TS + 1))
        result = sm.apply(RecenterRequested(Decimal("105"), _BASE_TS + 2))
        assert result.rejected
        assert result.reject_reason == "EMERGENCY_STOPPED"

    def test_exit_accepted_under_emergency(self) -> None:
        """ExitFilled accepted under emergency stop (21.16)."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))

        sm.apply(EmergencyStopTriggered(_BASE_TS + 2))
        assert sm.snapshot.emergency_stopped is True
        assert sm.snapshot.mode == BranchMode.LONG_BRANCH

        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 3)
        )

        assert not result.rejected
        assert result.snapshot.mode == BranchMode.FLAT
        assert result.snapshot.open_lots == ()
        assert len(result.snapshot.closed_lots) == 1

    def test_cleanup_after_emergency(self) -> None:
        """T20: Cleanup after emergency -> FLAT, no lots, no exits."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        sm.apply(EmergencyStopTriggered(_BASE_TS + 2))
        result = sm.apply(OperatorCleanup(_BASE_TS + 3))

        assert not result.rejected
        snap = result.snapshot
        assert snap.mode == BranchMode.FLAT
        assert snap.open_lots == ()
        assert snap.emergency_stopped is False
        # Lot closed by cleanup in closed_lots
        assert len(snap.closed_lots) == 1
        assert snap.closed_lots[0].status == LotStatus.CLOSED

    def test_emergency_preserves_exits(self) -> None:
        """Emergency stop does NOT cancel exits (I2)."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        result = sm.apply(EmergencyStopTriggered(_BASE_TS + 2))

        cancel_exits = [a for a in result.actions if a.kind == ActionIntentKind.CANCEL_EXIT]
        assert cancel_exits == []
        # Exit orders still OPEN
        assert all(eo.status == ExitOrderStatus.OPEN for eo in result.snapshot.exit_orders)


# ---------------------------------------------------------------------------
# Ordering contract (21.3)
# ---------------------------------------------------------------------------


class TestOrderingContract:
    def test_action_order_on_entry_fill(self) -> None:
        """PLACE_EXIT -> CANCEL_ENTRY -> PLACE_ENTRY (no internal mutations)."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))

        kinds = [a.kind for a in result.actions]
        # Find positions
        pe_idx = kinds.index(ActionIntentKind.PLACE_EXIT)
        # All CANCEL_ENTRY after PLACE_EXIT
        for i, k in enumerate(kinds):
            if k == ActionIntentKind.CANCEL_ENTRY:
                assert i > pe_idx
        # All PLACE_ENTRY after CANCEL_ENTRY
        cancel_indices = [i for i, k in enumerate(kinds) if k == ActionIntentKind.CANCEL_ENTRY]
        entry_indices = [i for i, k in enumerate(kinds) if k == ActionIntentKind.PLACE_ENTRY]
        if cancel_indices and entry_indices:
            assert max(cancel_indices) < min(entry_indices)

    def test_lots_chronological(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))

        lots = sm.snapshot.open_lots
        assert lots[0].opened_at_ts < lots[1].opened_at_ts

    def test_exit_orders_match_lot_order(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))

        lots = sm.snapshot.open_lots
        exits = sm.snapshot.exit_orders
        for lot, eo in zip(lots, exits, strict=True):
            assert eo.lot_id == lot.lot_id


# ---------------------------------------------------------------------------
# Cancel entry identity (21.9)
# ---------------------------------------------------------------------------


class TestCancelEntryIdentity:
    def test_cancel_entry_carries_side_price(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))

        for a in result.actions:
            if a.kind == ActionIntentKind.CANCEL_ENTRY:
                assert a.side is not None
                assert a.price is not None
                assert a.order_id is None  # No CID in PR2

    def test_cancel_exit_carries_order_id(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        result = sm.apply(OperatorCleanup(_BASE_TS + 2))

        for a in result.actions:
            if a.kind == ActionIntentKind.CANCEL_EXIT:
                assert a.order_id is not None


# ---------------------------------------------------------------------------
# Action reasons (21.11)
# ---------------------------------------------------------------------------


class TestActionReasons:
    def test_place_exit_reason(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        pe = next(a for a in result.actions if a.kind == ActionIntentKind.PLACE_EXIT)
        assert pe.reason == "PAIRED_EXIT_FOR_LOT"

    def test_place_entry_fill_replacement(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        pe = next(a for a in result.actions if a.kind == ActionIntentKind.PLACE_ENTRY)
        assert pe.reason == "FILL_REPLACEMENT"

    def test_cancel_entry_rolling_trim(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        ce = [a for a in result.actions if a.kind == ActionIntentKind.CANCEL_ENTRY]
        assert all(a.reason == "ROLLING_TRIM" for a in ce)

    def test_recenter_reasons(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 2)
        )
        result = sm.apply(RecenterRequested(Decimal("105"), _BASE_TS + 3))
        pe = [a for a in result.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
        assert all(a.reason == "RECENTER" for a in pe)

    def test_emergency_stop_reasons(self) -> None:
        sm = _sm()
        result = sm.apply(EmergencyStopTriggered(_BASE_TS + 1))
        ce = [a for a in result.actions if a.kind == ActionIntentKind.CANCEL_ENTRY]
        assert all(a.reason == "EMERGENCY_STOP" for a in ce)

    def test_cleanup_reasons(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        result = sm.apply(OperatorCleanup(_BASE_TS + 2))
        ce = [a for a in result.actions if a.kind == ActionIntentKind.CANCEL_ENTRY]
        cx = [a for a in result.actions if a.kind == ActionIntentKind.CANCEL_EXIT]
        assert all(a.reason == "OPERATOR_CLEANUP" for a in ce)
        assert all(a.reason == "OPERATOR_CLEANUP" for a in cx)


# ---------------------------------------------------------------------------
# Config validation (21.12)
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_step_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="grid_step_pct must be positive"):
            GridV2Config(Decimal("0"), 3, Decimal("1"), 5, Decimal("10000"))

    def test_step_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="grid_step_pct must be positive"):
            GridV2Config(Decimal("-0.01"), 3, Decimal("1"), 5, Decimal("10000"))

    def test_levels_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="entry_levels_per_side must be positive"):
            GridV2Config(Decimal("0.01"), 0, Decimal("1"), 5, Decimal("10000"))

    def test_order_size_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="order_size must be positive"):
            GridV2Config(Decimal("0.01"), 3, Decimal("0"), 5, Decimal("10000"))

    def test_max_levels_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="max_inventory_levels must be positive"):
            GridV2Config(Decimal("0.01"), 3, Decimal("1"), 0, Decimal("10000"))

    def test_max_notional_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="max_inventory_notional_usd must be positive"):
            GridV2Config(Decimal("0.01"), 3, Decimal("1"), 5, Decimal("0"))


# ---------------------------------------------------------------------------
# Invariants (section 4)
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_window_bounded(self) -> None:
        """I1: Window never exceeds levels_per_side after fills."""
        sm = _sm()
        for i in range(3):
            buy = sm.snapshot.entry_window.buy_entry_prices[0]
            sm.apply(EntryFilled(f"E{i}", OrderSide.BUY, buy, _ORDER_SIZE, _BASE_TS + i + 1))
        assert len(sm.snapshot.entry_window.buy_entry_prices) <= 3

    def test_every_open_lot_has_exit(self) -> None:
        """I3: Every open lot has a paired exit order."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))

        exit_lot_ids = {
            eo.lot_id for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN
        }
        for lot in sm.snapshot.open_lots:
            assert lot.lot_id in exit_lot_ids

    def test_mode_matches_lot_sides(self) -> None:
        """I4: All open lots match the branch mode."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        assert sm.snapshot.mode == BranchMode.LONG_BRANCH
        for lot in sm.snapshot.open_lots:
            assert lot.side == LotSide.LONG

    def test_determinism(self) -> None:
        """Same events -> same result."""
        cfg = _config()
        sm1 = GridV2StateMachine.create_initial(cfg, _REF_PRICE, _BASE_TS)
        sm2 = GridV2StateMachine.create_initial(cfg, _REF_PRICE, _BASE_TS)

        buy = sm1.snapshot.entry_window.buy_entry_prices[0]
        e = EntryFilled("E1", OrderSide.BUY, buy, _ORDER_SIZE, _BASE_TS + 1)

        r1 = sm1.apply(e)
        r2 = sm2.apply(e)
        assert r1.snapshot == r2.snapshot
        assert r1.actions == r2.actions


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_entry_zero_qty_rejected(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, Decimal("0"), _BASE_TS + 1))
        assert result.rejected
        assert result.reject_reason == "INVALID_QUANTITY"

    def test_entry_negative_qty_rejected(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, Decimal("-1"), _BASE_TS + 1))
        assert result.rejected
        assert result.reject_reason == "INVALID_QUANTITY"

    def test_exit_zero_qty_rejected(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        result = sm.apply(
            ExitFilled(
                exit_eo.exit_order_id, lot.lot_id, lot.exit_price, Decimal("0"), _BASE_TS + 2
            )
        )
        assert result.rejected
        assert result.reject_reason == "INVALID_QUANTITY"

    def test_exit_negative_qty_rejected(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        result = sm.apply(
            ExitFilled(
                exit_eo.exit_order_id, lot.lot_id, lot.exit_price, Decimal("-1"), _BASE_TS + 2
            )
        )
        assert result.rejected
        assert result.reject_reason == "INVALID_QUANTITY"

    def test_exit_qty_different_from_lot_still_full_closes(self) -> None:
        """ExitFilled.qty != lot.qty: lot still full-closes, retains original qty (21.13)."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, Decimal("5"), _BASE_TS + 1))
        lot = sm.snapshot.open_lots[0]
        assert lot.qty == Decimal("5")

        exit_eo = sm.snapshot.exit_orders[0]
        # Exit with qty=3 (different from lot.qty=5)
        result = sm.apply(
            ExitFilled(
                exit_eo.exit_order_id, lot.lot_id, lot.exit_price, Decimal("3"), _BASE_TS + 2
            )
        )
        assert not result.rejected
        assert result.snapshot.mode == BranchMode.FLAT  # fully closed
        closed = result.snapshot.closed_lots[0]
        assert closed.qty == Decimal("5")  # original qty preserved
        assert closed.status == LotStatus.CLOSED


# ---------------------------------------------------------------------------
# Cleanup structural (21.14)
# ---------------------------------------------------------------------------


class TestCleanupStructural:
    def test_cleanup_closed_lot_in_closed_lots(self) -> None:
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        result = sm.apply(OperatorCleanup(_BASE_TS + 2))

        assert len(result.snapshot.closed_lots) == 1
        assert result.snapshot.closed_lots[0].status == LotStatus.CLOSED

    def test_snapshot_has_no_pnl_fields(self) -> None:
        """GridV2Snapshot has no PnL fields."""
        snap = _sm().snapshot
        assert not hasattr(snap, "realized_pnl")
        assert not hasattr(snap, "unrealized_pnl")
        assert not hasattr(snap, "pnl")

    def test_transition_result_has_no_pnl_fields(self) -> None:
        """TransitionResult has no PnL fields."""
        sm = _sm()
        result = sm.apply(OperatorCleanup(_BASE_TS + 1))
        assert not hasattr(result, "realized_pnl")
        assert not hasattr(result, "unrealized_pnl")
        assert not hasattr(result, "pnl")
