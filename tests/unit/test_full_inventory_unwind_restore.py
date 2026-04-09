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
    return GridV2Config(
        grid_step_pct=Decimal("0.0025"),
        entry_levels_per_side=5,
        order_size=Decimal("15"),
        max_inventory_levels=10,
        max_inventory_notional_usd=Decimal("100"),
        price_tick_size=Decimal("0.0001"),
        reseed_on_flat=True,
    )


def _fill_to_max(sm: GridV2StateMachine) -> None:
    """Fill entries until no more buy entries available or max reached.

    ADR-178 near-cap guard may suppress FILL_REPLACEMENT before
    max_inventory_levels, so this fills as many as the window allows.
    """
    buy_prices = list(sm.snapshot.entry_window.buy_entry_prices)
    for i, price in enumerate(buy_prices):
        sm.apply(EntryFilled(f"e{i}", OrderSide.BUY, price, Decimal("15"), 2000 + i))
    while True:
        bp = list(sm.snapshot.entry_window.buy_entry_prices)
        if not bp:
            break
        idx = len(sm.snapshot.open_lots)
        sm.apply(EntryFilled(f"e{idx}", OrderSide.BUY, bp[0], Decimal("15"), 3000 + idx))


class TestLongBranchUnwindRestore:
    """LONG_BRANCH: full inventory → exit fills → entries restore."""

    def test_first_exit_fill_restores_entry(self) -> None:
        """Going from 10→9 lots: entry must be restored despite exit collision."""
        sm = GridV2StateMachine.create_initial(_cfg(), Decimal("0.5024"), 1000)
        _fill_to_max(sm)
        # ADR-178: near-cap guard may stop before max_inventory_levels.
        # With max=10, levels=5, threshold=5: fills ~9 lots (initial 5 + rolling ~4).
        assert len(sm.snapshot.open_lots) >= 5, (
            f"Need at least 5 lots, got {len(sm.snapshot.open_lots)}"
        )
        assert len(sm.snapshot.entry_window.buy_entry_prices) == 0

        lot = sm.snapshot.open_lots[0]
        exit_eo = next(
            e
            for e in sm.snapshot.exit_orders
            if e.lot_id == lot.lot_id and e.status.value == "OPEN"
        )
        r = sm.apply(ExitFilled(exit_eo.exit_order_id, lot.lot_id, exit_eo.price, lot.qty, 5000))

        place_entries = [a for a in r.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
        assert len(place_entries) == 1, f"Expected 1 restored entry, got {len(place_entries)}"
        assert len(sm.snapshot.entry_window.buy_entry_prices) == 1

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

        # Fill sell entries to build SHORT inventory
        sell_prices = list(sm.snapshot.entry_window.sell_entry_prices)
        for i, price in enumerate(sell_prices):
            sm.apply(EntryFilled(f"e{i}", OrderSide.SELL, price, Decimal("15"), 2000 + i))
        while True:
            sp = list(sm.snapshot.entry_window.sell_entry_prices)
            if not sp:
                break
            idx = len(sm.snapshot.open_lots)
            sm.apply(EntryFilled(f"e{idx}", OrderSide.SELL, sp[0], Decimal("15"), 3000 + idx))

        assert len(sm.snapshot.open_lots) >= 5

        lot = sm.snapshot.open_lots[0]
        exit_eo = next(
            e
            for e in sm.snapshot.exit_orders
            if e.lot_id == lot.lot_id and e.status.value == "OPEN"
        )
        r = sm.apply(ExitFilled(exit_eo.exit_order_id, lot.lot_id, exit_eo.price, lot.qty, 5000))

        pe = [a for a in r.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
        assert len(pe) == 1, f"Expected 1 restored sell entry, got {len(pe)}"
