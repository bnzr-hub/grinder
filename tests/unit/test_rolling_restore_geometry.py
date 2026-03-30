"""Tests for rolling entry restore geometry.

Proves that after an entry fill, the rolling window extends at the outer
edge — not by reusing an inner level. Both short and long branches.

Investigation triggered by post-ADR-115 live run where e22 @ 0.0528
appeared to reuse an inner level. Root cause: e22 was from a full
FLAT reseed, not a rolling restore. Rolling restore is already correct.
"""

from __future__ import annotations

from decimal import Decimal

from grinder.core import OrderSide
from grinder.grid_v2.state import (
    ActionIntentKind,
    BranchMode,
    EntryFilled,
    ExitFilled,
    GridV2Config,
    GridV2StateMachine,
)


def _pippin_config() -> GridV2Config:
    return GridV2Config(
        grid_step_pct=Decimal("0.0025"),
        entry_levels_per_side=5,
        order_size=Decimal("100"),
        max_inventory_levels=10,
        max_inventory_notional_usd=Decimal("50"),
        price_tick_size=Decimal("0.0001"),
        reseed_on_flat=True,
    )


def _sm(ref: str = "0.0524") -> GridV2StateMachine:
    return GridV2StateMachine.create_initial(_pippin_config(), Decimal(ref), 1000)


class TestShortBranchOuterEdgeRestore:
    """After a SELL entry fill, rolling extends outer edge, not inner reuse."""

    def test_single_fill_extends_outer(self) -> None:
        sm = _sm()
        initial_sells = list(sm.snapshot.entry_window.sell_entry_prices)
        assert initial_sells == [
            Decimal("0.0526"),
            Decimal("0.0528"),
            Decimal("0.0530"),
            Decimal("0.0532"),
            Decimal("0.0534"),
        ]

        r = sm.apply(EntryFilled("e1", OrderSide.SELL, Decimal("0.0526"), Decimal("100"), 2001))

        new_sells = list(sm.snapshot.entry_window.sell_entry_prices)
        # Inner 0.0526 removed, outer 0.0536 added
        assert new_sells == [
            Decimal("0.0528"),
            Decimal("0.0530"),
            Decimal("0.0532"),
            Decimal("0.0534"),
            Decimal("0.0536"),
        ]
        # Verify PLACE_ENTRY was for the outer edge
        places = [a for a in r.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
        assert len(places) == 1
        assert places[0].price == Decimal("0.0536")
        assert places[0].reason == "FILL_REPLACEMENT"

    def test_three_fills_shift_ladder_outward(self) -> None:
        sm = _sm()

        sm.apply(EntryFilled("e1", OrderSide.SELL, Decimal("0.0526"), Decimal("100"), 2001))
        sm.apply(EntryFilled("e2", OrderSide.SELL, Decimal("0.0528"), Decimal("100"), 2002))
        sm.apply(EntryFilled("e3", OrderSide.SELL, Decimal("0.0530"), Decimal("100"), 2003))

        sells = list(sm.snapshot.entry_window.sell_entry_prices)
        assert sells == [
            Decimal("0.0532"),
            Decimal("0.0534"),
            Decimal("0.0536"),
            Decimal("0.0538"),
            Decimal("0.0540"),
        ]

    def test_no_inner_reuse(self) -> None:
        """Filled inner price must not reappear in the sell ladder."""
        sm = _sm()
        sm.apply(EntryFilled("e1", OrderSide.SELL, Decimal("0.0526"), Decimal("100"), 2001))

        sells = set(sm.snapshot.entry_window.sell_entry_prices)
        assert Decimal("0.0526") not in sells, "Filled inner level reappeared"

    def test_level_count_stays_constant(self) -> None:
        sm = _sm()
        for i, price in enumerate([Decimal("0.0526"), Decimal("0.0528"), Decimal("0.0530")]):
            sm.apply(EntryFilled(f"e{i}", OrderSide.SELL, price, Decimal("100"), 2001 + i))
            assert len(sm.snapshot.entry_window.sell_entry_prices) == 5


class TestLongBranchOuterEdgeRestore:
    """After a BUY entry fill, rolling extends outer edge downward."""

    def test_single_fill_extends_outer(self) -> None:
        sm = _sm()
        initial_buys = list(sm.snapshot.entry_window.buy_entry_prices)
        assert initial_buys == [
            Decimal("0.0522"),
            Decimal("0.0520"),
            Decimal("0.0518"),
            Decimal("0.0516"),
            Decimal("0.0514"),
        ]

        r = sm.apply(EntryFilled("e1", OrderSide.BUY, Decimal("0.0522"), Decimal("100"), 2001))

        new_buys = list(sm.snapshot.entry_window.buy_entry_prices)
        # Inner 0.0522 removed, outer 0.0512 added
        assert new_buys == [
            Decimal("0.0520"),
            Decimal("0.0518"),
            Decimal("0.0516"),
            Decimal("0.0514"),
            Decimal("0.0512"),
        ]
        places = [a for a in r.actions if a.kind == ActionIntentKind.PLACE_ENTRY]
        assert len(places) == 1
        assert places[0].price == Decimal("0.0512")

    def test_three_fills_shift_ladder_outward(self) -> None:
        sm = _sm()

        sm.apply(EntryFilled("e1", OrderSide.BUY, Decimal("0.0522"), Decimal("100"), 2001))
        sm.apply(EntryFilled("e2", OrderSide.BUY, Decimal("0.0520"), Decimal("100"), 2002))
        sm.apply(EntryFilled("e3", OrderSide.BUY, Decimal("0.0518"), Decimal("100"), 2003))

        buys = list(sm.snapshot.entry_window.buy_entry_prices)
        assert buys == [
            Decimal("0.0516"),
            Decimal("0.0514"),
            Decimal("0.0512"),
            Decimal("0.0510"),
            Decimal("0.0508"),
        ]


class TestReseedDoesNotReuseLevels:
    """Full reseed after FLAT creates fresh symmetric window, not inner reuse."""

    def test_reseed_after_full_unwind_is_fresh(self) -> None:
        sm = _sm()

        # Fill sell entry -> SHORT_BRANCH
        sm.apply(EntryFilled("e1", OrderSide.SELL, Decimal("0.0526"), Decimal("100"), 2001))
        assert sm.mode == BranchMode.SHORT_BRANCH

        # Fill exit -> back to FLAT with reseed
        lot = sm.snapshot.open_lots[0]
        assert lot.exit_order_id is not None
        sm.apply(ExitFilled(lot.exit_order_id, lot.lot_id, lot.exit_price, Decimal("100"), 3001))
        # After full unwind with reseed_on_flat=True, mode returns to FLAT
        assert sm.snapshot.mode == BranchMode.FLAT

        # Window should be fresh symmetric, not a continuation of shifted ladder
        sells = list(sm.snapshot.entry_window.sell_entry_prices)
        buys = list(sm.snapshot.entry_window.buy_entry_prices)
        assert len(sells) == 5
        assert len(buys) == 5
        # Both sides present (symmetric)
        assert buys[0] > buys[-1]  # BUY sorted descending
        assert sells[0] < sells[-1]  # SELL sorted ascending


class TestExitRestoreGeometry:
    """After exit fill with remaining inventory, restore extends toward reference."""

    def test_short_branch_exit_restore_extends_inward(self) -> None:
        """After exit fill, remaining sell entries get a new inner level."""
        sm = _sm()

        # Fill 3 sell entries to build inventory
        sm.apply(EntryFilled("e1", OrderSide.SELL, Decimal("0.0526"), Decimal("100"), 2001))
        sm.apply(EntryFilled("e2", OrderSide.SELL, Decimal("0.0528"), Decimal("100"), 2002))
        sm.apply(EntryFilled("e3", OrderSide.SELL, Decimal("0.0530"), Decimal("100"), 2003))

        # Sells after 3 fills: 0.0532, 0.0534, 0.0536, 0.0538, 0.0540
        sells_before_exit = list(sm.snapshot.entry_window.sell_entry_prices)
        assert sells_before_exit[0] == Decimal("0.0532")

        # Exit fill (close one lot)
        lot = sm.snapshot.open_lots[0]
        assert lot.exit_order_id is not None
        sm.apply(ExitFilled(lot.exit_order_id, lot.lot_id, lot.exit_price, Decimal("100"), 3001))
        assert sm.mode == BranchMode.SHORT_BRANCH  # still have 2 lots

        sells_after_exit = list(sm.snapshot.entry_window.sell_entry_prices)
        # Restore should add a new level closer to reference (inward)
        assert sells_after_exit[0] < sells_before_exit[0], (
            f"Restore should extend inward: {sells_after_exit[0]} should be < {sells_before_exit[0]}"
        )
