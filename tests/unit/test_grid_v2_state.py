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
    _grid_step_price,
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
    tick_size: Decimal = Decimal("0.01"),
    reseed_on_flat: bool = True,
    reseed_on_flat_only_on_skew: bool = True,
    reseed_cooldown_ms: int = 0,
) -> GridV2Config:
    return GridV2Config(
        grid_step_pct=step,
        entry_levels_per_side=levels,
        order_size=order_size,
        max_inventory_levels=max_levels,
        max_inventory_notional_usd=max_notional,
        price_tick_size=tick_size,
        reseed_on_flat=reseed_on_flat,
        reseed_on_flat_only_on_skew=reseed_on_flat_only_on_skew,
        reseed_cooldown_ms=reseed_cooldown_ms,
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

    def test_strict_tick_step_intervals_match(self) -> None:
        cfg = _config(step=Decimal("0.0025"), levels=5, tick_size=Decimal("0.0001"))
        sm = _sm(ref=Decimal("0.09583"), cfg=cfg)
        buys = sm.snapshot.entry_window.buy_entry_prices
        sells = sm.snapshot.entry_window.sell_entry_prices
        buy_deltas = [buys[i] - buys[i + 1] for i in range(len(buys) - 1)]
        sell_deltas = [sells[i + 1] - sells[i] for i in range(len(sells) - 1)]
        assert len(set(buy_deltas)) == 1
        assert len(set(sell_deltas)) == 1
        assert buy_deltas[0] == sell_deltas[0] == Decimal("0.0003")


# ---------------------------------------------------------------------------
# T2/T3: First entry fill (11.1, 11.2)
# ---------------------------------------------------------------------------


class TestFirstEntryFill:
    def test_buy_creates_long_branch(self) -> None:
        """T2: BUY fill -> LONG_BRANCH + SELL exit + all SELL entries cancelled (one-sided)."""
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
        # One-sided: all SELL entries cancelled on branch entry.
        assert snap.entry_window.sell_entry_prices == ()
        # CANCEL_ENTRY actions emitted for opposite side (rolling trim + one-sided cancel).
        cancel_sell_actions = [
            a
            for a in result.actions
            if a.kind == ActionIntentKind.CANCEL_ENTRY and a.side == OrderSide.SELL
        ]
        # 1 ROLLING_TRIM + remaining sells cancelled via ONE_SIDED_CANCEL_OPPOSITE
        rolling_trims = [a for a in cancel_sell_actions if a.reason == "ROLLING_TRIM"]
        one_sided = [a for a in cancel_sell_actions if a.reason == "ONE_SIDED_CANCEL_OPPOSITE"]
        assert len(rolling_trims) == 1
        assert len(one_sided) == len(initial_sells) - 1  # all except the one trimmed by rolling

    def test_sell_creates_short_branch(self) -> None:
        """T3: SELL fill -> SHORT_BRANCH + BUY exit + all BUY entries cancelled (one-sided)."""
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
        # One-sided: all BUY entries cancelled on branch entry.
        assert snap.entry_window.buy_entry_prices == ()
        # CANCEL_ENTRY actions emitted for opposite side.
        cancel_buy_actions = [
            a
            for a in result.actions
            if a.kind == ActionIntentKind.CANCEL_ENTRY and a.side == OrderSide.BUY
        ]
        rolling_trims = [a for a in cancel_buy_actions if a.reason == "ROLLING_TRIM"]
        one_sided = [a for a in cancel_buy_actions if a.reason == "ONE_SIDED_CANCEL_OPPOSITE"]
        assert len(rolling_trims) == 1
        assert len(one_sided) == len(initial_buys) - 1

    def test_action_order_correct(self) -> None:
        """Actions: PLACE_EXIT -> ROLLING_TRIM -> FILL_REPLACEMENT -> ONE_SIDED_CANCEL."""
        sm = _sm()
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1))

        kinds = [a.kind for a in result.actions]
        reasons = [a.reason for a in result.actions]
        # PLACE_EXIT first
        assert kinds[0] == ActionIntentKind.PLACE_EXIT
        # Rolling trim before fill replacement
        rolling_idx = [i for i, r in enumerate(reasons) if r == "ROLLING_TRIM"]
        fill_repl_idx = [i for i, r in enumerate(reasons) if r == "FILL_REPLACEMENT"]
        assert all(ri < fi for ri in rolling_idx for fi in fill_repl_idx)
        # ONE_SIDED_CANCEL_OPPOSITE actions are present (cancel remaining opposite entries)
        one_sided_idx = [i for i, r in enumerate(reasons) if r == "ONE_SIDED_CANCEL_OPPOSITE"]
        assert len(one_sided_idx) >= 1


# ---------------------------------------------------------------------------
# T4/T5: Branch continuation (11.3, 11.4)
# ---------------------------------------------------------------------------


class TestBranchContinuation:
    def test_long_continuation(self) -> None:
        """T4: Second BUY in LONG_BRANCH — no opposite entries left to cancel (one-sided)."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        # After first fill, all SELL entries already cancelled (one-sided).
        assert sm.snapshot.entry_window.sell_entry_prices == ()
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))

        assert not result.rejected
        assert result.snapshot.mode == BranchMode.LONG_BRANCH
        assert len(result.snapshot.open_lots) == 2
        # No CANCEL_ENTRY: opposite side already empty from first fill.
        cancel_entries = [a for a in result.actions if a.kind == ActionIntentKind.CANCEL_ENTRY]
        assert len(cancel_entries) == 0
        # Still only BUY entries.
        assert result.snapshot.entry_window.sell_entry_prices == ()

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

    def test_long_exit_restores_buy_side_only(self) -> None:
        """T6: Exit fill in LONG branch restores BUY entries only (one-sided)."""
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
        # One-sided: only BUY restore actions (no SELL restore).
        assert len(result.actions) == 2
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
        assert buy_action.reason == "EXIT_RESTORE"
        assert buy_cancel.reason == "EXIT_RESTORE_SHIFT"
        # No SELL entries restored.
        assert result.snapshot.entry_window.sell_entry_prices == ()
        assert result.snapshot.mode == BranchMode.LONG_BRANCH
        # BUY side shifted closer to reference.
        expected_buys = (
            pre_exit_window.buy_entry_prices[0] + pre_exit_window.reference_price * _STEP,
            *pre_exit_window.buy_entry_prices[:-1],
        )
        assert result.snapshot.entry_window.buy_entry_prices == expected_buys
        assert buy_action.price == expected_buys[0]
        assert buy_cancel.price == pre_exit_window.buy_entry_prices[-1]
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

    def test_short_exit_restores_sell_side_only(self) -> None:
        """T7: Exit fill in SHORT branch restores SELL entries only (one-sided)."""
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
        # One-sided: only SELL restore actions, no BUY restore.
        sell_actions = [
            a
            for a in result.actions
            if a.kind == ActionIntentKind.PLACE_ENTRY and a.side == OrderSide.SELL
        ]
        assert len(sell_actions) >= 1
        assert sell_actions[0].reason == "EXIT_RESTORE"
        assert result.snapshot.mode == BranchMode.SHORT_BRANCH
        # No BUY entries restored.
        assert result.snapshot.entry_window.buy_entry_prices == ()
        # Verify no duplicate prices in resulting SELL window
        w = result.snapshot.entry_window
        assert len(w.sell_entry_prices) == len(set(w.sell_entry_prices)), (
            f"Duplicate sell prices: {w.sell_entry_prices}"
        )
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

    def test_last_exit_goes_flat_without_reseed_when_both_disabled(self) -> None:
        """Both reseed flags off: one-sided dead state is allowed (operator choice)."""
        cfg = _config(reseed_on_flat=False, reseed_on_flat_only_on_skew=False)
        sm = _sm(cfg=cfg)
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))

        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 2)
        )

        assert not result.rejected
        snap = result.snapshot
        assert snap.mode == BranchMode.FLAT
        assert snap.open_lots == ()
        # One-sided: only BUY side restored (was LONG_BRANCH). SELL stays empty.
        assert len(snap.entry_window.buy_entry_prices) == cfg.entry_levels_per_side
        assert len(snap.entry_window.sell_entry_prices) == 0
        assert any(a.reason == "EXIT_RESTORE" for a in result.actions)
        assert all(a.reason not in {"RECENTER", "RECENTER_REPLACE"} for a in result.actions)

    def test_last_exit_reseeds_on_skew_long_branch(self) -> None:
        """Default prod config: LONG_BRANCH → FLAT with empty SELL → reseeds both sides."""
        cfg = _config(reseed_on_flat=False, reseed_on_flat_only_on_skew=True)
        sm = _sm(cfg=cfg)
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        # After entry fill: LONG_BRANCH, SELL entries cancelled (one-sided).
        assert sm.snapshot.mode == BranchMode.LONG_BRANCH
        assert sm.snapshot.entry_window.sell_entry_prices == ()

        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 2)
        )

        assert not result.rejected
        snap = result.snapshot
        assert snap.mode == BranchMode.FLAT
        # Skew detected: both sides rebuilt symmetrically.
        assert len(snap.entry_window.buy_entry_prices) == cfg.entry_levels_per_side
        assert len(snap.entry_window.sell_entry_prices) == cfg.entry_levels_per_side
        assert any(a.reason == "RECENTER" for a in result.actions)

    def test_last_exit_reseeds_on_skew_short_branch(self) -> None:
        """Default prod config: SHORT_BRANCH → FLAT with empty BUY → reseeds both sides."""
        cfg = _config(reseed_on_flat=False, reseed_on_flat_only_on_skew=True)
        sm = _sm(cfg=cfg)
        sell1 = sm.snapshot.entry_window.sell_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.SELL, sell1, _ORDER_SIZE, _BASE_TS + 1))
        assert sm.snapshot.mode == BranchMode.SHORT_BRANCH
        assert sm.snapshot.entry_window.buy_entry_prices == ()

        lot = sm.snapshot.open_lots[0]
        exit_eo = sm.snapshot.exit_orders[0]
        result = sm.apply(
            ExitFilled(exit_eo.exit_order_id, lot.lot_id, lot.exit_price, _ORDER_SIZE, _BASE_TS + 2)
        )

        assert not result.rejected
        snap = result.snapshot
        assert snap.mode == BranchMode.FLAT
        assert len(snap.entry_window.buy_entry_prices) == cfg.entry_levels_per_side
        assert len(snap.entry_window.sell_entry_prices) == cfg.entry_levels_per_side
        assert any(a.reason == "RECENTER" for a in result.actions)

    def test_multi_lot_unwind_reseeds_on_skew(self) -> None:
        """Prod config: 2-lot LONG unwind → FLAT reseeds both sides."""
        cfg = _config(reseed_on_flat=False, reseed_on_flat_only_on_skew=True)
        sm = _sm(cfg=cfg)
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))
        assert sm.snapshot.entry_window.sell_entry_prices == ()

        # Close first lot — still in branch
        lot1 = sm.snapshot.open_lots[0]
        exit1 = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot1.lot_id)
        r1 = sm.apply(
            ExitFilled(exit1.exit_order_id, lot1.lot_id, lot1.exit_price, _ORDER_SIZE, _BASE_TS + 3)
        )
        assert r1.snapshot.mode == BranchMode.LONG_BRANCH
        assert r1.snapshot.entry_window.sell_entry_prices == ()

        # Close second lot → FLAT with skew → reseed
        lot2 = sm.snapshot.open_lots[0]
        exit2 = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == lot2.lot_id)
        r2 = sm.apply(
            ExitFilled(exit2.exit_order_id, lot2.lot_id, lot2.exit_price, _ORDER_SIZE, _BASE_TS + 4)
        )
        assert r2.snapshot.mode == BranchMode.FLAT
        assert len(r2.snapshot.entry_window.buy_entry_prices) == cfg.entry_levels_per_side
        assert len(r2.snapshot.entry_window.sell_entry_prices) == cfg.entry_levels_per_side
        assert any(a.reason == "RECENTER" for a in r2.actions)

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
    def test_buy_in_short_impossible(self) -> None:
        """One-sided: BUY entries absent in SHORT_BRANCH, so opposite fill is impossible."""
        sm = _sm()
        sell1 = sm.snapshot.entry_window.sell_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.SELL, sell1, _ORDER_SIZE, _BASE_TS + 1))
        # All BUY entries were cancelled on branch entry.
        assert sm.snapshot.entry_window.buy_entry_prices == ()
        # Attempting a BUY at what was a buy price gets PRICE_NOT_IN_ACTIVE_WINDOW.
        result = sm.apply(
            EntryFilled(
                "E2", OrderSide.BUY, _REF_PRICE * Decimal("0.99"), _ORDER_SIZE, _BASE_TS + 2
            )
        )
        assert result.rejected
        assert result.reject_reason == "PRICE_NOT_IN_ACTIVE_WINDOW"

    def test_sell_in_long_impossible(self) -> None:
        """One-sided: SELL entries absent in LONG_BRANCH, so opposite fill is impossible."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        # All SELL entries were cancelled on branch entry.
        assert sm.snapshot.entry_window.sell_entry_prices == ()
        # Attempting a SELL at what was a sell price gets PRICE_NOT_IN_ACTIVE_WINDOW.
        result = sm.apply(
            EntryFilled(
                "E2", OrderSide.SELL, _REF_PRICE * Decimal("1.01"), _ORDER_SIZE, _BASE_TS + 2
            )
        )
        assert result.rejected
        assert result.reject_reason == "PRICE_NOT_IN_ACTIVE_WINDOW"


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
            EntryFilled(
                "g_g_PIPPINUSDT_e1_1773952851_0", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1
            )
        )

        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(
            EntryFilled(
                "g_g_PIPPINUSDT_e2_1773952852_0", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2
            )
        )
        assert not result.rejected
        assert len(result.snapshot.open_lots) == 2

    def test_grid_cid_bypasses_max_notional_rejection(self) -> None:
        cfg = _config(max_notional=Decimal("100"), max_levels=10)
        sm = GridV2StateMachine.create_initial(cfg, _REF_PRICE, _BASE_TS)
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(
            EntryFilled(
                "g_g_PIPPINUSDT_e1_1773952851_0", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1
            )
        )

        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(
            EntryFilled(
                "g_g_PIPPINUSDT_e2_1773952852_0", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2
            )
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
        """PLACE_EXIT -> ROLLING_TRIM -> FILL_REPLACEMENT -> ONE_SIDED_CANCEL."""
        sm = _sm()
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))

        kinds = [a.kind for a in result.actions]
        reasons = [a.reason for a in result.actions]
        # PLACE_EXIT first
        pe_idx = kinds.index(ActionIntentKind.PLACE_EXIT)
        assert pe_idx == 0
        # ROLLING_TRIM before FILL_REPLACEMENT
        rolling_idx = [i for i, r in enumerate(reasons) if r == "ROLLING_TRIM"]
        fill_repl_idx = [i for i, r in enumerate(reasons) if r == "FILL_REPLACEMENT"]
        assert all(ri < fi for ri in rolling_idx for fi in fill_repl_idx)
        # ONE_SIDED_CANCEL_OPPOSITE actions after FILL_REPLACEMENT
        one_sided_idx = [i for i, r in enumerate(reasons) if r == "ONE_SIDED_CANCEL_OPPOSITE"]
        assert len(one_sided_idx) >= 1
        if fill_repl_idx:
            assert all(oi > max(fill_repl_idx) for oi in one_sided_idx)

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
        # One-sided: CANCEL_ENTRY includes both ROLLING_TRIM and ONE_SIDED_CANCEL_OPPOSITE.
        rolling = [a for a in ce if a.reason == "ROLLING_TRIM"]
        one_sided = [a for a in ce if a.reason == "ONE_SIDED_CANCEL_OPPOSITE"]
        assert len(rolling) == 1
        assert len(one_sided) >= 1
        assert len(ce) == len(rolling) + len(one_sided)

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
    def test_default_flat_reseed_mode_is_skew_only(self) -> None:
        cfg = GridV2Config(Decimal("0.01"), 3, Decimal("1"), 5, Decimal("10000"))
        assert cfg.reseed_on_flat is False
        assert cfg.reseed_on_flat_only_on_skew is True

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


# ---------------------------------------------------------------------------
# Net-off tests (Variant B: opposite fill closes lot)
# ---------------------------------------------------------------------------


def _netoff_config() -> GridV2Config:
    """Config with netoff enabled, reusing _config defaults."""
    base = _config()
    return GridV2Config(
        grid_step_pct=base.grid_step_pct,
        entry_levels_per_side=base.entry_levels_per_side,
        order_size=base.order_size,
        max_inventory_levels=base.max_inventory_levels,
        max_inventory_notional_usd=base.max_inventory_notional_usd,
        price_tick_size=base.price_tick_size,
        reseed_on_flat=base.reseed_on_flat,
        netoff_enabled=True,
    )


class TestNetOff:
    """One-sided grid: opposite entries cancelled, so netoff is unreachable via normal path.

    These tests verify that:
    1. Opposite entries are absent after branch entry (one-sided).
    2. Netoff code path still exists but can't be triggered via the normal entry path.
    3. Attempting an opposite fill at the old price is rejected (PRICE_NOT_IN_ACTIVE_WINDOW).
    """

    def test_long_branch_has_no_sell_entries(self) -> None:
        """After BUY fill, SELL entries are empty — netoff is unreachable."""
        sm = GridV2StateMachine.create_initial(_netoff_config(), _REF_PRICE, _BASE_TS)
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("e1", OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1))
        assert sm.snapshot.mode == BranchMode.LONG_BRANCH
        assert sm.snapshot.entry_window.sell_entry_prices == ()

    def test_short_branch_has_no_buy_entries(self) -> None:
        """After SELL fill, BUY entries are empty — netoff is unreachable."""
        sm = GridV2StateMachine.create_initial(_netoff_config(), _REF_PRICE, _BASE_TS)
        sell_price = sm.snapshot.entry_window.sell_entry_prices[0]
        sm.apply(EntryFilled("e1", OrderSide.SELL, sell_price, _ORDER_SIZE, _BASE_TS + 1))
        assert sm.snapshot.mode == BranchMode.SHORT_BRANCH
        assert sm.snapshot.entry_window.buy_entry_prices == ()

    def test_opposite_fill_rejected_price_not_in_window(self) -> None:
        """Opposite fill at old price is rejected (entries were cancelled)."""
        sm = GridV2StateMachine.create_initial(_netoff_config(), _REF_PRICE, _BASE_TS)
        initial_sells = sm.snapshot.entry_window.sell_entry_prices
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("e1", OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1))

        # Try SELL at what was previously a valid sell price.
        r = sm.apply(EntryFilled("e2", OrderSide.SELL, initial_sells[0], _ORDER_SIZE, _BASE_TS + 2))
        assert r.rejected
        assert r.reject_reason == "PRICE_NOT_IN_ACTIVE_WINDOW"

    def test_netoff_disabled_opposite_fill_rejected(self) -> None:
        """Without netoff, opposite fill rejected (entries absent, not BRANCH_INCOMPATIBLE)."""
        sm = GridV2StateMachine.create_initial(_config(), _REF_PRICE, _BASE_TS)
        initial_sells = sm.snapshot.entry_window.sell_entry_prices
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("e1", OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1))

        # SELL entries are gone — rejected before BRANCH_INCOMPATIBLE check.
        r = sm.apply(EntryFilled("e2", OrderSide.SELL, initial_sells[0], _ORDER_SIZE, _BASE_TS + 2))
        assert r.rejected
        assert r.reject_reason == "PRICE_NOT_IN_ACTIVE_WINDOW"

    def test_one_sided_prevents_mixed_inventory(self) -> None:
        """One-sided mode prevents mixed inventory by removing opposite entries."""
        sm = GridV2StateMachine.create_initial(_netoff_config(), _REF_PRICE, _BASE_TS)
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("e1", OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1))

        # Only LONG lots, no way to create SHORT lots (sell entries absent).
        assert all(lot.side == LotSide.LONG for lot in sm.snapshot.open_lots)
        assert sm.snapshot.entry_window.sell_entry_prices == ()

    def test_cancel_entry_actions_emitted_for_opposite_side(self) -> None:
        """Branch entry emits CANCEL_ENTRY for all opposite-side entries."""
        sm = GridV2StateMachine.create_initial(_netoff_config(), _REF_PRICE, _BASE_TS)
        initial_sells = sm.snapshot.entry_window.sell_entry_prices
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        r = sm.apply(EntryFilled("e1", OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1))

        cancel_sell_actions = [
            a
            for a in r.actions
            if a.kind == ActionIntentKind.CANCEL_ENTRY and a.side == OrderSide.SELL
        ]
        # All initial SELL prices are cancelled (1 ROLLING_TRIM + rest ONE_SIDED).
        cancelled_prices = {a.price for a in cancel_sell_actions}
        assert cancelled_prices == set(initial_sells)

    def test_two_lots_then_opposite_still_blocked(self) -> None:
        """2 LONG lots — SELL entries still empty, opposite fill still impossible."""
        sm = GridV2StateMachine.create_initial(_netoff_config(), _REF_PRICE, _BASE_TS)
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("e1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("e2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2))
        assert len(sm.snapshot.open_lots) == 2
        assert sm.snapshot.entry_window.sell_entry_prices == ()


# ---------------------------------------------------------------------------
# Price collision guard tests (rolling window + exit restore)
# ---------------------------------------------------------------------------


def _all_entry_prices(sm: GridV2StateMachine) -> list[Decimal]:
    """Get all entry prices from both sides."""
    w = sm.snapshot.entry_window
    return list(w.buy_entry_prices) + list(w.sell_entry_prices)


def _open_exit_prices(sm: GridV2StateMachine) -> set[Decimal]:
    """Get all exit prices of OPEN exit orders."""
    return {eo.price for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN}


def _assert_no_duplicate_prices(sm: GridV2StateMachine) -> None:
    """Assert no duplicate prices within buy or sell entry lists."""
    w = sm.snapshot.entry_window
    assert len(w.buy_entry_prices) == len(set(w.buy_entry_prices)), (
        f"Duplicate buy prices: {w.buy_entry_prices}"
    )
    assert len(w.sell_entry_prices) == len(set(w.sell_entry_prices)), (
        f"Duplicate sell prices: {w.sell_entry_prices}"
    )
    # No entry price should match an open exit price
    entry_prices = set(w.buy_entry_prices) | set(w.sell_entry_prices)
    exit_prices = _open_exit_prices(sm)
    collision = entry_prices & exit_prices
    assert not collision, f"Entry/exit price collision: {collision}"


class TestRollingWindowCollisionGuard:
    """Tests A-G: verify no duplicate prices after rolling/restore operations."""

    def test_a_rolling_shift_no_exit_collision(self) -> None:
        """BUY fill → rolling extends → new entry price != any open exit price."""
        sm = _sm()
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]  # closest BUY
        result = sm.apply(_entry_filled(side=OrderSide.BUY, price=buy_price))
        assert not result.rejected
        _assert_no_duplicate_prices(sm)

    def test_b_rolling_shift_skips_occupied_price(self) -> None:
        """When new_farthest would collide with exit, entry window is shorter by 1."""
        # Use tight config where exit price = entry price + step overlaps
        cfg = _config(step=Decimal("0.01"), levels=3, tick_size=Decimal("0.01"))
        sm = GridV2StateMachine.create_initial(cfg, Decimal("100"), _BASE_TS)

        # Fill top BUY → creates lot with exit
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        r1 = sm.apply(_entry_filled(side=OrderSide.BUY, price=buy1, order_id="e1"))
        assert not r1.rejected
        _assert_no_duplicate_prices(sm)

        # Fill another BUY
        if sm.snapshot.entry_window.buy_entry_prices:
            buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
            r2 = sm.apply(
                _entry_filled(side=OrderSide.BUY, price=buy2, order_id="e2", ts=_BASE_TS + 2)
            )
            assert not r2.rejected
            _assert_no_duplicate_prices(sm)

    def test_c_exit_restore_no_duplicate_entry(self) -> None:
        """Exit fill → restore entry → restored price not duplicate in window."""
        sm = _sm()
        # Fill BUY → get lot with exit
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(_entry_filled(side=OrderSide.BUY, price=buy1, order_id="e1"))

        # Now close lot via exit fill
        if sm.snapshot.exit_orders:
            eo = sm.snapshot.exit_orders[0]
            lot = sm.snapshot.open_lots[0]
            sm.apply(
                ExitFilled(
                    exit_order_id=eo.exit_order_id,
                    lot_id=lot.lot_id,
                    price=eo.price,
                    qty=eo.qty,
                    ts=_BASE_TS + 10,
                )
            )
            _assert_no_duplicate_prices(sm)

    def test_d_exit_restore_no_exit_collision(self) -> None:
        """Exit fill → restore entry → restored price != other open lot exit."""
        cfg = _config(levels=3)
        sm = GridV2StateMachine.create_initial(cfg, _REF_PRICE, _BASE_TS)

        # Fill 2 BUYs → 2 lots with exits
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(_entry_filled(side=OrderSide.BUY, price=buy1, order_id="e1"))
        if sm.snapshot.entry_window.buy_entry_prices:
            buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
            sm.apply(_entry_filled(side=OrderSide.BUY, price=buy2, order_id="e2", ts=_BASE_TS + 2))

        # Close first lot
        if sm.snapshot.exit_orders:
            eo = next(e for e in sm.snapshot.exit_orders if e.status == ExitOrderStatus.OPEN)
            lot = next(lt for lt in sm.snapshot.open_lots if lt.lot_id == eo.lot_id)
            sm.apply(
                ExitFilled(
                    exit_order_id=eo.exit_order_id,
                    lot_id=lot.lot_id,
                    price=eo.price,
                    qty=eo.qty,
                    ts=_BASE_TS + 20,
                )
            )
            _assert_no_duplicate_prices(sm)

    def test_e_oscillation_no_duplicates(self) -> None:
        """BUY fill → exit fill → BUY fill at same level → zero duplicate prices."""
        sm = _sm()
        # Fill closest BUY
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(_entry_filled(side=OrderSide.BUY, price=buy1, order_id="e1"))
        _assert_no_duplicate_prices(sm)

        # Close via exit
        eo = sm.snapshot.exit_orders[0]
        lot = sm.snapshot.open_lots[0]
        sm.apply(
            ExitFilled(
                exit_order_id=eo.exit_order_id,
                lot_id=lot.lot_id,
                price=eo.price,
                qty=eo.qty,
                ts=_BASE_TS + 10,
            )
        )
        _assert_no_duplicate_prices(sm)

        # Fill another BUY (may be at same price as first if window restored)
        if sm.snapshot.entry_window.buy_entry_prices:
            buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
            sm.apply(_entry_filled(side=OrderSide.BUY, price=buy2, order_id="e3", ts=_BASE_TS + 20))
            _assert_no_duplicate_prices(sm)

    def test_f_initial_window_no_duplicates(self) -> None:
        """Initial entry window has no duplicate prices."""
        sm = _sm()
        w = sm.snapshot.entry_window
        all_prices = list(w.buy_entry_prices) + list(w.sell_entry_prices)
        assert len(all_prices) == len(set(all_prices))

    def test_g_multiple_fills_no_collision(self) -> None:
        """3 sequential BUY fills → all resulting prices unique."""
        cfg = _config(levels=5, max_levels=10)
        sm = GridV2StateMachine.create_initial(cfg, _REF_PRICE, _BASE_TS)

        for i in range(3):
            if not sm.snapshot.entry_window.buy_entry_prices:
                break
            buy = sm.snapshot.entry_window.buy_entry_prices[0]
            r = sm.apply(
                _entry_filled(
                    side=OrderSide.BUY,
                    price=buy,
                    order_id=f"e{i}",
                    ts=_BASE_TS + i + 1,
                )
            )
            assert not r.rejected
            _assert_no_duplicate_prices(sm)

    def test_oscillation_cycle_5_rounds(self) -> None:
        """5 full BUY→exit cycles with no price duplicates at any point."""
        cfg = _config(levels=3, max_levels=10, reseed_on_flat=False)
        sm = GridV2StateMachine.create_initial(cfg, _REF_PRICE, _BASE_TS)

        ts = _BASE_TS
        for cycle in range(5):
            ts += 1
            if not sm.snapshot.entry_window.buy_entry_prices:
                break
            buy = sm.snapshot.entry_window.buy_entry_prices[0]
            r = sm.apply(_entry_filled(side=OrderSide.BUY, price=buy, order_id=f"e{cycle}", ts=ts))
            assert not r.rejected, f"Cycle {cycle} entry rejected"
            _assert_no_duplicate_prices(sm)

            # Close lot via exit
            ts += 1
            open_exits = [e for e in sm.snapshot.exit_orders if e.status == ExitOrderStatus.OPEN]
            if open_exits:
                eo = open_exits[0]
                lot = next(lt for lt in sm.snapshot.open_lots if lt.lot_id == eo.lot_id)
                r2 = sm.apply(
                    ExitFilled(
                        exit_order_id=eo.exit_order_id,
                        lot_id=lot.lot_id,
                        price=eo.price,
                        qty=eo.qty,
                        ts=ts,
                    )
                )
                assert not r2.rejected, f"Cycle {cycle} exit rejected"
                _assert_no_duplicate_prices(sm)


# ---------------------------------------------------------------------------
# Exit step-spacing tests
# ---------------------------------------------------------------------------


def _open_exit_prices_list(sm: GridV2StateMachine) -> list[Decimal]:
    """Get sorted list of open exit prices."""
    return sorted(eo.price for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN)


class TestExitStepSpacing:
    """ADR-176: Exits preserve exact mirrored price — no spacing drift."""

    def test_two_nearby_entries_exits_both_mirrored(self) -> None:
        """Two BUY fills at adjacent prices → exits at exact mirrored targets."""
        cfg = _config(step=Decimal("0.01"), levels=5, tick_size=Decimal("0.01"))
        sm = GridV2StateMachine.create_initial(cfg, Decimal("100"), _BASE_TS)

        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(_entry_filled(side=OrderSide.BUY, price=buy1, order_id="e1"))

        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(_entry_filled(side=OrderSide.BUY, price=buy2, order_id="e2", ts=_BASE_TS + 2))

        for lot in sm.snapshot.open_lots:
            expected = lot.entry_price * (Decimal(1) + Decimal("0.01"))
            assert abs(lot.exit_price - expected) <= Decimal("0.02"), (
                f"Lot {lot.lot_id}: exit {lot.exit_price} drifted from expected {expected}"
            )

    def test_three_clustered_lots_all_mirrored(self) -> None:
        """Three BUY fills → all exits at exact mirrored step (may share price)."""
        cfg = _config(step=Decimal("0.01"), levels=5, tick_size=Decimal("0.01"))
        sm = GridV2StateMachine.create_initial(cfg, Decimal("100"), _BASE_TS)

        for i in range(3):
            if not sm.snapshot.entry_window.buy_entry_prices:
                break
            buy = sm.snapshot.entry_window.buy_entry_prices[0]
            sm.apply(
                _entry_filled(side=OrderSide.BUY, price=buy, order_id=f"e{i}", ts=_BASE_TS + i + 1)
            )

        for lot in sm.snapshot.open_lots:
            expected = lot.entry_price * (Decimal(1) + Decimal("0.01"))
            assert abs(lot.exit_price - expected) <= Decimal("0.02"), (
                f"Lot {lot.lot_id}: exit {lot.exit_price} drifted from expected {expected}"
            )

    def test_short_branch_buy_exits_mirrored(self) -> None:
        """SHORT lots → BUY exits at exact mirrored step."""
        cfg = _config(step=Decimal("0.01"), levels=5, tick_size=Decimal("0.01"))
        sm = GridV2StateMachine.create_initial(cfg, Decimal("100"), _BASE_TS)

        for i in range(2):
            if not sm.snapshot.entry_window.sell_entry_prices:
                break
            sell = sm.snapshot.entry_window.sell_entry_prices[0]
            sm.apply(
                _entry_filled(
                    side=OrderSide.SELL, price=sell, order_id=f"e{i}", ts=_BASE_TS + i + 1
                )
            )

        for lot in sm.snapshot.open_lots:
            expected = lot.entry_price * (Decimal(1) - Decimal("0.01"))
            assert abs(lot.exit_price - expected) <= Decimal("0.02"), (
                f"Lot {lot.lot_id}: exit {lot.exit_price} drifted from expected {expected}"
            )

    def test_no_shift_when_base_exit_already_clear(self) -> None:
        """Single lot: base exit has no collision → no shift needed."""
        cfg = _config(step=Decimal("0.01"), levels=3, tick_size=Decimal("0.01"))
        sm = GridV2StateMachine.create_initial(cfg, Decimal("100"), _BASE_TS)

        buy = sm.snapshot.entry_window.buy_entry_prices[0]
        r = sm.apply(_entry_filled(side=OrderSide.BUY, price=buy, order_id="e1"))
        assert not r.rejected

        exits = _open_exit_prices_list(sm)
        assert len(exits) == 1
        # Base exit = entry * (1 + 0.01) = 99 * 1.01 = 99.99 → no shift
        expected = buy * (Decimal(1) + Decimal("0.01"))
        assert exits[0] == expected

    def test_dense_entries_all_exits_placed(self) -> None:
        """ADR-176: Dense fills → all exits placed at exact mirrored price (no fail-closed)."""
        cfg = _config(step=Decimal("0.01"), levels=5, max_levels=3, tick_size=Decimal("0.01"))
        sm = GridV2StateMachine.create_initial(cfg, Decimal("100"), _BASE_TS)

        for i in range(3):
            if not sm.snapshot.entry_window.buy_entry_prices:
                break
            buy = sm.snapshot.entry_window.buy_entry_prices[0]
            r = sm.apply(
                _entry_filled(side=OrderSide.BUY, price=buy, order_id=f"e{i}", ts=_BASE_TS + i + 1)
            )
            assert not r.rejected

        # All lots get exits — no fail-closed skip
        exits = [eo for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN]
        assert len(exits) == len(sm.snapshot.open_lots)
        for lot in sm.snapshot.open_lots:
            expected = lot.entry_price * (Decimal(1) + Decimal("0.01"))
            assert abs(lot.exit_price - expected) <= Decimal("0.02")


class TestReplenishSuppressedWhenInventoryFull:
    """ADR-110: Suppress replenish when inventory is already full."""

    REF = Decimal("50000")
    STEP = Decimal("0.005")
    SIZE = Decimal("0.001")

    def test_exit_fill_replenish_when_below_max(self) -> None:
        """Exit fill with inventory below max and window below levels -> replenish fires.

        Construct a LONG_BRANCH snapshot where buy_entry_prices < levels_per_side
        and open_lots < max_inventory. This is the scenario where EXIT_RESTORE
        should create a new entry.
        """
        cfg = GridV2Config(
            grid_step_pct=self.STEP,
            entry_levels_per_side=3,
            order_size=self.SIZE,
            max_inventory_levels=5,
            max_inventory_notional_usd=Decimal("100000"),
            reseed_on_flat=True,
        )
        step = Decimal("250")
        # 2 lots, max=5. Window has only 1 buy entry (below levels=3).
        lots = tuple(
            InventoryLot(
                lot_id=f"lot-{i}",
                side=LotSide.LONG,
                entry_price=self.REF - step * (i + 1),
                qty=self.SIZE,
                opened_at_ts=1000 + i,
                source_entry_order_id=f"e-{i}",
                exit_price=self.REF - step * (i + 1) + step,
                exit_order_id=f"exit-{i}",
                status=LotStatus.OPEN,
            )
            for i in range(2)
        )
        exits = tuple(
            ExitOrder(
                exit_order_id=f"exit-{i}",
                lot_id=f"lot-{i}",
                side=OrderSide.SELL,
                price=self.REF - step * (i + 1) + step,
                qty=self.SIZE,
                status=ExitOrderStatus.OPEN,
            )
            for i in range(2)
        )
        # Only 1 buy price in window (below levels_per_side=3)
        window = EntryWindow(
            reference_price=self.REF,
            buy_entry_prices=(self.REF - step * 3,),
            sell_entry_prices=(),
            levels_per_side=3,
            step_pct=self.STEP,
        )
        snap = GridV2Snapshot(
            mode=BranchMode.LONG_BRANCH,
            entry_window=window,
            open_lots=lots,
            closed_lots=(),
            exit_orders=exits,
            emergency_stopped=False,
            last_recenter_ts=None,
        )
        sm = GridV2StateMachine(cfg, snap)
        assert len(sm.snapshot.open_lots) == 2

        # Fill exit -> 1 lot remains (below max=5), window has room
        result = sm.apply(
            ExitFilled(
                exit_order_id="exit-0",
                lot_id="lot-0",
                price=exits[0].price,
                qty=self.SIZE,
                ts=2000,
            )
        )
        assert len(sm.snapshot.open_lots) == 1
        places = [a for a in result.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
        assert len(places) == 1, (
            f"Replenish expected when below max (1 lot, max=5), got {len(places)}"
        )
        assert places[0].side == OrderSide.BUY
        assert places[0].reason == "EXIT_RESTORE"

    def test_exit_fill_no_replenish_when_still_at_max(self) -> None:
        """Exit fill but remaining lots still >= max -> no PLACE_ENTRY."""
        cfg = GridV2Config(
            grid_step_pct=self.STEP,
            entry_levels_per_side=5,
            order_size=self.SIZE,
            max_inventory_levels=2,
            max_inventory_notional_usd=Decimal("100000"),
            reseed_on_flat=True,
        )
        step = Decimal("250")
        lots = tuple(
            InventoryLot(
                lot_id=f"lot-{i}",
                side=LotSide.LONG,
                entry_price=self.REF - step * (i + 1),
                qty=self.SIZE,
                opened_at_ts=1000 + i,
                source_entry_order_id=f"e-{i}",
                exit_price=self.REF - step * (i + 1) + step,
                exit_order_id=f"exit-{i}",
                status=LotStatus.OPEN,
            )
            for i in range(3)
        )
        exits = tuple(
            ExitOrder(
                exit_order_id=f"exit-{i}",
                lot_id=f"lot-{i}",
                side=OrderSide.SELL,
                price=self.REF - step * (i + 1) + step,
                qty=self.SIZE,
                status=ExitOrderStatus.OPEN,
            )
            for i in range(3)
        )
        window = EntryWindow(
            reference_price=self.REF,
            buy_entry_prices=(self.REF - step * 4, self.REF - step * 5),
            sell_entry_prices=(),
            levels_per_side=5,
            step_pct=self.STEP,
        )
        snap = GridV2Snapshot(
            mode=BranchMode.LONG_BRANCH,
            entry_window=window,
            open_lots=lots,
            closed_lots=(),
            exit_orders=exits,
            emergency_stopped=False,
            last_recenter_ts=None,
        )
        sm = GridV2StateMachine(cfg, snap)
        assert len(sm.snapshot.open_lots) == 3

        result = sm.apply(
            ExitFilled(
                exit_order_id="exit-0", lot_id="lot-0", price=exits[0].price, qty=self.SIZE, ts=2000
            )
        )
        # 2 lots remain == max_inv=2
        assert len(sm.snapshot.open_lots) == 2
        places = [a for a in result.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
        assert len(places) == 0, f"No replenish when at max, got {len(places)}"


class TestExactMirroredExits:
    """ADR-176: Each lot preserves exact mirrored exit — no spacing drift."""

    def test_single_buy_entry_exact_exit(self) -> None:
        """Single BUY entry → SELL exit at entry * (1 + step)."""
        sm = _sm()
        buy = sm.snapshot.entry_window.buy_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.BUY, buy, _ORDER_SIZE, _BASE_TS + 1))
        assert not result.rejected
        lot = sm.snapshot.open_lots[0]
        expected_exit = buy * (Decimal(1) + _STEP)
        # Allow tick quantization tolerance
        assert abs(lot.exit_price - expected_exit) <= Decimal("0.01")

    def test_single_sell_entry_exact_exit(self) -> None:
        """Single SELL entry → BUY exit at entry * (1 - step)."""
        sm = _sm()
        sell = sm.snapshot.entry_window.sell_entry_prices[0]
        result = sm.apply(EntryFilled("E1", OrderSide.SELL, sell, _ORDER_SIZE, _BASE_TS + 1))
        assert not result.rejected
        lot = sm.snapshot.open_lots[0]
        expected_exit = sell * (Decimal(1) - _STEP)
        assert abs(lot.exit_price - expected_exit) <= Decimal("0.01")

    def test_multiple_same_side_no_cascade_drift(self) -> None:
        """Multiple BUY fills → all exits at exact one-step distance."""
        cfg = _config(levels=5, max_levels=10)
        sm = _sm(cfg=cfg)
        for i in range(5):
            buy = sm.snapshot.entry_window.buy_entry_prices[0]
            sm.apply(EntryFilled(f"E{i}", OrderSide.BUY, buy, _ORDER_SIZE, _BASE_TS + i + 1))

        for lot in sm.snapshot.open_lots:
            expected = lot.entry_price * (Decimal(1) + _STEP)
            distance = abs(lot.exit_price - expected)
            assert distance <= Decimal("0.02"), (
                f"Lot {lot.lot_id}: exit {lot.exit_price} is {distance} from "
                f"expected {expected} — cascade drift detected"
            )

    def test_same_price_exits_allowed(self) -> None:
        """Two entries at same price → two exits at same price (no shift)."""
        sm = _sm()
        buy = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy, _ORDER_SIZE, _BASE_TS + 1))
        # Second fill at same price (rolling may add it back)
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy, _ORDER_SIZE, _BASE_TS + 2))
        exits = [eo for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN]
        exit_prices = [eo.price for eo in exits]
        # Both exits should be at the same mirrored price (or very close)
        if len(exit_prices) >= 2:
            assert abs(exit_prices[0] - exit_prices[1]) <= Decimal("0.02"), (
                f"Same-price entries should produce same-price exits, "
                f"got {exit_prices[0]} and {exit_prices[1]}"
            )


class TestExitRestoreHeadroom:
    """ADR-170: EXIT_RESTORE respects headroom near inventory cap."""

    def test_near_cap_suppresses_exit_restore(self) -> None:
        """At 4/5 lots (headroom=1), exit fill should not restore if 1 entry exists."""
        cfg = _config(
            max_levels=5, levels=3, reseed_on_flat=False, reseed_on_flat_only_on_skew=False
        )
        sm = _sm(cfg=cfg)

        # Fill 4 entries to get near cap (4/5)
        for i in range(4):
            buy = sm.snapshot.entry_window.buy_entry_prices[0]
            sm.apply(EntryFilled(f"E{i}", OrderSide.BUY, buy, _ORDER_SIZE, _BASE_TS + i + 1))

        assert len(sm.snapshot.open_lots) == 4
        # Now we have some BUY entries in window. Exit one lot → 3 lots, headroom=2
        exit_eo = next(eo for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN)
        result = sm.apply(
            ExitFilled(
                exit_order_id=exit_eo.exit_order_id,
                lot_id=exit_eo.lot_id,
                price=exit_eo.price,
                qty=_ORDER_SIZE,
                ts=_BASE_TS + 100,
            )
        )
        # With headroom=2 and existing buy entries, restore should be limited
        buy_entries_after = len(result.snapshot.entry_window.buy_entry_prices)
        # buy entries should not exceed headroom
        assert buy_entries_after <= 2, (
            f"Expected <= 2 buy entries (headroom=2), got {buy_entries_after}"
        )

    def test_at_cap_no_exit_restore(self) -> None:
        """At 5/5 lots, exit fill → 4 lots (headroom=1), restore capped to 1."""
        cfg = _config(
            max_levels=5, levels=3, reseed_on_flat=False, reseed_on_flat_only_on_skew=False
        )
        sm = _sm(cfg=cfg)

        # Fill 5 entries to hit cap
        for i in range(5):
            buy = sm.snapshot.entry_window.buy_entry_prices[0]
            sm.apply(EntryFilled(f"E{i}", OrderSide.BUY, buy, _ORDER_SIZE, _BASE_TS + i + 1))

        assert len(sm.snapshot.open_lots) == 5
        # Exit one → 4 lots, headroom=1
        exit_eo = next(eo for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN)
        r = sm.apply(
            ExitFilled(
                exit_order_id=exit_eo.exit_order_id,
                lot_id=exit_eo.lot_id,
                price=exit_eo.price,
                qty=_ORDER_SIZE,
                ts=_BASE_TS + 100,
            )
        )
        buy_entries_after = len(r.snapshot.entry_window.buy_entry_prices)
        assert buy_entries_after <= 1, (
            f"Expected <= 1 buy entry (headroom=1), got {buy_entries_after}"
        )


class TestReseedCooldown:
    """Anti-churn: suppress repeated flat reseeds within cooldown window."""

    def test_normal_reseed_still_works(self) -> None:
        """First flat return triggers reseed as before."""
        cfg = _config(reseed_on_flat=True)
        sm = _sm(cfg=cfg)
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        exit_eo = sm.snapshot.exit_orders[0]
        result = sm.apply(
            ExitFilled(
                exit_order_id=exit_eo.exit_order_id,
                lot_id=exit_eo.lot_id,
                price=exit_eo.price,
                qty=_ORDER_SIZE,
                ts=_BASE_TS + 2,
            )
        )
        recenters = [a for a in result.actions if a.reason == "RECENTER"]
        assert len(recenters) > 0

    def test_rapid_reseed_suppressed_within_cooldown(self) -> None:
        """Second flat return within cooldown → no reseed (anti-churn)."""
        cfg = _config(reseed_on_flat=True, reseed_cooldown_ms=30_000)
        sm = _sm(cfg=cfg)

        # Cycle 1: entry → exit → FLAT → reseed
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        exit1 = next(eo for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN)
        r1 = sm.apply(
            ExitFilled(
                exit_order_id=exit1.exit_order_id,
                lot_id=exit1.lot_id,
                price=exit1.price,
                qty=_ORDER_SIZE,
                ts=_BASE_TS + 1000,  # first reseed
            )
        )
        assert any(a.reason == "RECENTER" for a in r1.actions)
        assert sm.snapshot.last_recenter_ts == _BASE_TS + 1000

        # Cycle 2: entry → exit → FLAT within cooldown → NO reseed
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E2", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 2000))
        exit2 = next(eo for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN)
        r2 = sm.apply(
            ExitFilled(
                exit_order_id=exit2.exit_order_id,
                lot_id=exit2.lot_id,
                price=exit2.price,
                qty=_ORDER_SIZE,
                ts=_BASE_TS + 5000,  # only 4s after first reseed, within 30s cooldown
            )
        )
        recenters = [a for a in r2.actions if a.reason == "RECENTER"]
        assert len(recenters) == 0, "Reseed should be suppressed within cooldown"

    def test_reseed_allowed_after_cooldown_expires(self) -> None:
        """After cooldown expires, reseed is allowed again."""
        cfg = _config(reseed_on_flat=True, reseed_cooldown_ms=30_000)
        sm = _sm(cfg=cfg)

        # Cycle 1: reseed
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        exit1 = next(eo for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN)
        sm.apply(
            ExitFilled(
                exit_order_id=exit1.exit_order_id,
                lot_id=exit1.lot_id,
                price=exit1.price,
                qty=_ORDER_SIZE,
                ts=_BASE_TS + 1000,
            )
        )

        # Cycle 2: after cooldown (31s later)
        buy2 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E3", OrderSide.BUY, buy2, _ORDER_SIZE, _BASE_TS + 32000))
        exit2 = next(eo for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN)
        r2 = sm.apply(
            ExitFilled(
                exit_order_id=exit2.exit_order_id,
                lot_id=exit2.lot_id,
                price=exit2.price,
                qty=_ORDER_SIZE,
                ts=_BASE_TS + 35000,  # 34s after first reseed, past 30s cooldown
            )
        )
        recenters = [a for a in r2.actions if a.reason == "RECENTER"]
        assert len(recenters) > 0, "Reseed should be allowed after cooldown"

    def test_batch_semantics_preserved(self) -> None:
        """When reseed does happen, it's still a batch (cancel+place all)."""
        cfg = _config(reseed_on_flat=True, levels=3)
        sm = _sm(cfg=cfg)
        buy1 = sm.snapshot.entry_window.buy_entry_prices[0]
        sm.apply(EntryFilled("E1", OrderSide.BUY, buy1, _ORDER_SIZE, _BASE_TS + 1))
        exit1 = sm.snapshot.exit_orders[0]
        result = sm.apply(
            ExitFilled(
                exit_order_id=exit1.exit_order_id,
                lot_id=exit1.lot_id,
                price=exit1.price,
                qty=_ORDER_SIZE,
                ts=_BASE_TS + 2,
            )
        )
        cancels = [a for a in result.actions if a.reason == "RECENTER_REPLACE"]
        places = [a for a in result.actions if a.reason == "RECENTER"]
        # Batch: cancel existing + place new
        assert len(cancels) > 0
        assert len(places) > 0
