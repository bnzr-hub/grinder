"""Tests for entry restoration when unwinding from full inventory.

Proves that exit fills restore entries even when all candidate prices
are initially occupied by exits (search outward for free slots).
"""

from __future__ import annotations

from decimal import Decimal

from grinder.core import OrderSide
from grinder.grid_v2.state import (
    ActionIntentKind,
    EntryFilled,
    ExitFilled,
    GridV2Config,
    GridV2StateMachine,
)


def _cfg() -> GridV2Config:
    # entry_levels=5, max_inventory=20. Near-cap threshold = min(20, max(5,20-5)) = 15.
    # Rolling fills freely up to 15, then near-cap suppresses.
    # 5 levels per side gives enough distance for rolling to reach 20.
    return GridV2Config(
        grid_step_pct=Decimal("0.0025"),
        entry_levels_per_side=5,
        order_size=Decimal("15"),
        max_inventory_levels=20,
        max_inventory_notional_usd=Decimal("100"),
        price_tick_size=Decimal("0.0001"),
        reseed_on_flat=True,
    )


def _fill_to_max(sm: GridV2StateMachine) -> None:
    """Fill BUY entries until the inventory cap is reached.

    Under the H2 fix (2026-04-11), the same-side window count stays at
    ``entry_levels_per_side`` even when near-cap burst suppression or the
    B3-alt distance guard is active — only the inline FILL_REPLACEMENT
    PLACE is gated. So the old ``while buy_prices: ...`` termination
    condition never fires. Instead we bound by ``max_inventory_levels``
    and stop when the state machine starts rejecting fills with
    ``MAX_INVENTORY_LEVELS``.
    """
    buy_prices = list(sm.snapshot.entry_window.buy_entry_prices)
    for i, price in enumerate(buy_prices):
        sm.apply(EntryFilled(f"e{i}", OrderSide.BUY, price, Decimal("15"), 2000 + i))
    max_iters = 30
    for _ in range(max_iters):
        bp = list(sm.snapshot.entry_window.buy_entry_prices)
        if not bp:
            break
        prev_lots = len(sm.snapshot.open_lots)
        sm.apply(
            EntryFilled(f"e{prev_lots}", OrderSide.BUY, bp[0], Decimal("15"), 3000 + prev_lots)
        )
        if len(sm.snapshot.open_lots) == prev_lots:
            break  # fill rejected (cap reached)


class TestLongBranchUnwindRestore:
    """LONG_BRANCH: full inventory → exit fills → entries restore.

    Under the H2 fix (2026-04-11), the same-side window stays at
    ``entry_levels_per_side`` regardless of burst/distance gating, so
    EXIT_RESTORE takes the SHIFT path (cancel farthest + place closer)
    rather than a pure ADD. Assertions reflect that: we still expect
    exactly one PLACE_ENTRY per first exit fill, but the window count
    stays stable rather than going 0 → 1.
    """

    def test_first_exit_fill_restores_entry(self) -> None:
        """Deep inventory → single exit fill emits one EXIT_RESTORE PLACE."""
        cfg = _cfg()
        sm = GridV2StateMachine.create_initial(cfg, Decimal("0.5024"), 1000)
        _fill_to_max(sm)
        # Notional cap (max_inventory_notional_usd=100, size=15 * px~0.48)
        # limits rolling to ~13 lots before the cap rejects. Deep enough to
        # meaningfully exercise the unwind path.
        assert len(sm.snapshot.open_lots) >= 8, (
            f"Need deep inventory for unwind test, got {len(sm.snapshot.open_lots)}"
        )
        # H2 invariant: window stays stable at entry_levels_per_side.
        assert len(sm.snapshot.entry_window.buy_entry_prices) == cfg.entry_levels_per_side

        lot = sm.snapshot.open_lots[0]
        exit_eo = next(
            e
            for e in sm.snapshot.exit_orders
            if e.lot_id == lot.lot_id and e.status.value == "OPEN"
        )
        r = sm.apply(ExitFilled(exit_eo.exit_order_id, lot.lot_id, exit_eo.price, lot.qty, 5000))

        place_entries = [a for a in r.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
        shift_cancels = [
            a
            for a in r.actions
            if a.kind == ActionIntentKind.CANCEL_ENTRY and a.reason == "EXIT_RESTORE_SHIFT"
        ]
        assert len(place_entries) == 1, f"Expected 1 restored entry, got {len(place_entries)}"
        # EXIT_RESTORE SHIFT invariant: balanced cancel+place.
        assert len(shift_cancels) == 1, (
            f"Expected 1 EXIT_RESTORE_SHIFT cancel, got {len(shift_cancels)}"
        )
        # Window stays stable.
        assert len(sm.snapshot.entry_window.buy_entry_prices) == cfg.entry_levels_per_side

    def test_full_unwind_restores_all_entries(self) -> None:
        """10→0 lots: entries progressively restored, then reseed on FLAT."""
        sm = GridV2StateMachine.create_initial(_cfg(), Decimal("0.5024"), 1000)
        _fill_to_max(sm)

        total_place_entries = 0
        for i, lot in enumerate(list(sm.snapshot.open_lots)):
            exit_eo = next(
                (
                    e
                    for e in sm.snapshot.exit_orders
                    if e.lot_id == lot.lot_id and e.status.value == "OPEN"
                ),
                None,
            )
            if exit_eo is None:
                continue
            r = sm.apply(
                ExitFilled(exit_eo.exit_order_id, lot.lot_id, exit_eo.price, lot.qty, 5000 + i)
            )
            pe = [a for a in r.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
            total_place_entries += len(pe)

        # All lots closed → should have restored entries throughout + reseed at FLAT
        assert total_place_entries > 0, "No entries were restored during unwind"
        # Final state should be FLAT with full reseed
        assert sm.mode.value == "FLAT"
        assert len(sm.snapshot.entry_window.buy_entry_prices) == 5
        assert len(sm.snapshot.entry_window.sell_entry_prices) == 5

    def test_restored_entry_price_is_valid(self) -> None:
        """Restored entry price must not collide with any open exit."""
        sm = GridV2StateMachine.create_initial(_cfg(), Decimal("0.5024"), 1000)
        _fill_to_max(sm)

        lot = sm.snapshot.open_lots[0]
        exit_eo = next(
            e
            for e in sm.snapshot.exit_orders
            if e.lot_id == lot.lot_id and e.status.value == "OPEN"
        )
        r = sm.apply(ExitFilled(exit_eo.exit_order_id, lot.lot_id, exit_eo.price, lot.qty, 5000))

        pe = [a for a in r.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
        assert len(pe) == 1

        # Entry price must not collide with any remaining open exit
        open_exit_prices = {e.price for e in sm.snapshot.exit_orders if e.status.value == "OPEN"}
        assert pe[0].price not in open_exit_prices, (
            f"Restored entry {pe[0].price} collides with exit"
        )


class TestShortBranchUnwindRestore:
    """SHORT_BRANCH: mirror test for sell-side."""

    def test_first_exit_fill_restores_entry(self) -> None:
        cfg = _cfg()
        sm = GridV2StateMachine.create_initial(cfg, Decimal("0.5024"), 1000)

        # Fill sell entries to build SHORT inventory (H2-aware termination).
        sell_prices = list(sm.snapshot.entry_window.sell_entry_prices)
        for i, price in enumerate(sell_prices):
            sm.apply(EntryFilled(f"e{i}", OrderSide.SELL, price, Decimal("15"), 2000 + i))
        max_iters = 30
        for _ in range(max_iters):
            sp = list(sm.snapshot.entry_window.sell_entry_prices)
            if not sp:
                break
            prev_lots = len(sm.snapshot.open_lots)
            sm.apply(
                EntryFilled(f"e{prev_lots}", OrderSide.SELL, sp[0], Decimal("15"), 3000 + prev_lots)
            )
            if len(sm.snapshot.open_lots) == prev_lots:
                break  # fill rejected (cap reached)

        assert len(sm.snapshot.open_lots) >= 8, (
            f"Need deep inventory for unwind test, got {len(sm.snapshot.open_lots)}"
        )
        # H2 invariant: window stays stable at entry_levels_per_side.
        assert len(sm.snapshot.entry_window.sell_entry_prices) == cfg.entry_levels_per_side

        lot = sm.snapshot.open_lots[0]
        exit_eo = next(
            e
            for e in sm.snapshot.exit_orders
            if e.lot_id == lot.lot_id and e.status.value == "OPEN"
        )
        r = sm.apply(ExitFilled(exit_eo.exit_order_id, lot.lot_id, exit_eo.price, lot.qty, 5000))

        pe = [a for a in r.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
        shift_cancels = [
            a
            for a in r.actions
            if a.kind == ActionIntentKind.CANCEL_ENTRY and a.reason == "EXIT_RESTORE_SHIFT"
        ]
        assert len(pe) == 1, f"Expected 1 restored sell entry, got {len(pe)}"
        assert len(shift_cancels) == 1, (
            f"Expected 1 EXIT_RESTORE_SHIFT cancel, got {len(shift_cancels)}"
        )
        assert len(sm.snapshot.entry_window.sell_entry_prices) == cfg.entry_levels_per_side
