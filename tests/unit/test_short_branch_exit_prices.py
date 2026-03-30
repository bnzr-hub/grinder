"""Regression tests for short-branch exit price generation.

Reproduces the live anomaly from post-PR-503 bounded run:
  e16 @ 0.0529 -> x1 @ 0.0527 (correct)
  e17 @ 0.0531 -> x2 @ 0.0525 (ANOMALY: jumped 2 steps instead of 1)
  e18 @ 0.0533 -> x3 @ 0.0531 (correct)

Root cause: exit price was computed as raw fractional value before the
collision guard, causing false collision detection. Fix: quantize exit
price to tick size BEFORE collision guard.
"""

from __future__ import annotations

from decimal import Decimal

from grinder.core import OrderSide
from grinder.grid_v2.state import (
    BranchMode,
    EntryFilled,
    ExitOrderStatus,
    GridV2Config,
    GridV2StateMachine,
)


def _pippin_config() -> GridV2Config:
    """Config matching the live PIPPINUSDT run."""
    return GridV2Config(
        grid_step_pct=Decimal("0.0025"),
        entry_levels_per_side=5,
        order_size=Decimal("100"),
        max_inventory_levels=10,
        max_inventory_notional_usd=Decimal("50"),
        price_tick_size=Decimal("0.0001"),
        reseed_on_flat=True,
    )


def _sm_at(ref_price: str, cfg: GridV2Config | None = None) -> GridV2StateMachine:
    return GridV2StateMachine.create_initial(cfg or _pippin_config(), Decimal(ref_price), 1000)


def _sell_fill(order_id: str, price: str, ts: int = 2000) -> EntryFilled:
    return EntryFilled(
        order_id=order_id,
        side=OrderSide.SELL,
        price=Decimal(price),
        qty=Decimal("100"),
        ts=ts,
    )


def _buy_fill(order_id: str, price: str, ts: int = 2000) -> EntryFilled:
    return EntryFilled(
        order_id=order_id,
        side=OrderSide.BUY,
        price=Decimal(price),
        qty=Decimal("100"),
        ts=ts,
    )


class TestShortBranchExitPriceReproduction:
    """Exact reproduction of the live e16/e17/e18 -> x1/x2/x3 sequence."""

    def test_three_short_fills_exit_prices_topology_consistent(self) -> None:
        """Each short lot exit must be exactly 1 step_price apart."""
        sm = _sm_at("0.0527")

        # Fill 1: e16 @ 0.0529 (first SELL entry on short side)
        sm.apply(_sell_fill("e16", "0.0529", ts=2001))
        assert sm.mode == BranchMode.SHORT_BRANCH

        exits_1 = [eo for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN]
        assert len(exits_1) == 1
        x1_price = exits_1[0].price
        # Expected: 0.0529 * (1 - 0.0025) = 0.05276775 -> quantized BUY ROUND_DOWN -> 0.0527
        assert x1_price == Decimal("0.0527"), f"x1={x1_price}"

        # Fill 2: e17 @ 0.0531
        sm.apply(_sell_fill("e17", "0.0531", ts=2002))

        exits_2 = [eo for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN]
        assert len(exits_2) == 2
        x2_price = next(eo.price for eo in exits_2 if eo.lot_id == "lot-e17")
        # Expected: 0.0531 * (1 - 0.0025) = 0.05296725 -> quantized -> 0.0529
        # Then collision check: |0.0529 - 0.0527| = 0.0002 >= step_price -> NO collision
        assert x2_price == Decimal("0.0529"), f"x2={x2_price}"

        # Fill 3: e18 @ 0.0533
        sm.apply(_sell_fill("e18", "0.0533", ts=2003))

        exits_3 = [eo for eo in sm.snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN]
        assert len(exits_3) == 3
        x3_price = next(eo.price for eo in exits_3 if eo.lot_id == "lot-e18")
        # Expected: 0.0533 * (1 - 0.0025) = 0.05316675 -> quantized -> 0.0531
        # Collision check: |0.0531 - 0.0529| = 0.0002 >= step_price -> NO collision
        assert x3_price == Decimal("0.0531"), f"x3={x3_price}"

    def test_exit_prices_are_tick_aligned(self) -> None:
        """All exit prices must be exact multiples of tick_size."""
        sm = _sm_at("0.0527")
        tick = Decimal("0.0001")

        sm.apply(_sell_fill("e1", "0.0529", ts=2001))
        sm.apply(_sell_fill("e2", "0.0531", ts=2002))
        sm.apply(_sell_fill("e3", "0.0533", ts=2003))

        for eo in sm.snapshot.exit_orders:
            assert eo.price % tick == 0, (
                f"Exit {eo.exit_order_id} price {eo.price} not tick-aligned"
            )

    def test_exit_spacing_at_least_one_step(self) -> None:
        """All pairs of open exit prices must be >= step_price apart."""
        sm = _sm_at("0.0527")

        sm.apply(_sell_fill("e1", "0.0529", ts=2001))
        sm.apply(_sell_fill("e2", "0.0531", ts=2002))
        sm.apply(_sell_fill("e3", "0.0533", ts=2003))

        cfg = _pippin_config()
        from grinder.grid_v2.state import _grid_step_price  # noqa: PLC0415

        step_price = _grid_step_price(Decimal("0.0527"), cfg.grid_step_pct, cfg.price_tick_size)

        exit_prices = sorted(eo.price for eo in sm.snapshot.exit_orders)
        for i in range(len(exit_prices) - 1):
            gap = exit_prices[i + 1] - exit_prices[i]
            assert gap >= step_price, (
                f"Exit prices {exit_prices[i]} and {exit_prices[i + 1]} "
                f"too close: gap={gap} < step_price={step_price}"
            )


class TestLongBranchExitPriceConsistency:
    """Mirror test: long-branch exits must also be topology-consistent."""

    def test_three_long_fills_exit_prices(self) -> None:
        sm = _sm_at("0.0527")

        sm.apply(_buy_fill("e1", "0.0525", ts=2001))
        sm.apply(_buy_fill("e2", "0.0523", ts=2002))
        sm.apply(_buy_fill("e3", "0.0521", ts=2003))

        exits = {eo.lot_id: eo.price for eo in sm.snapshot.exit_orders}
        # 0.0525 * 1.0025 = 0.05263125 -> SELL ROUND_UP -> 0.0527
        assert exits["lot-e1"] == Decimal("0.0527"), f"x1={exits['lot-e1']}"
        # 0.0523 * 1.0025 = 0.05243075 -> SELL ROUND_UP -> 0.0525
        assert exits["lot-e2"] == Decimal("0.0525"), f"x2={exits['lot-e2']}"
        # 0.0521 * 1.0025 = 0.05223025 -> SELL ROUND_UP -> 0.0523
        assert exits["lot-e3"] == Decimal("0.0523"), f"x3={exits['lot-e3']}"


class TestExitPriceDeterminism:
    """Exit prices must be deterministic: same input → same output."""

    def test_same_fills_same_exits(self) -> None:
        for _ in range(3):
            sm = _sm_at("0.0527")
            sm.apply(_sell_fill("e1", "0.0529", ts=2001))
            sm.apply(_sell_fill("e2", "0.0531", ts=2002))
            exits = {eo.lot_id: eo.price for eo in sm.snapshot.exit_orders}
            assert exits["lot-e1"] == Decimal("0.0527")
            assert exits["lot-e2"] == Decimal("0.0529")


class TestExitMappingStableAfterNextFill:
    """Later fills must not corrupt earlier lot exit mappings."""

    def test_earlier_exit_unchanged_after_later_fill(self) -> None:
        sm = _sm_at("0.0527")

        sm.apply(_sell_fill("e1", "0.0529", ts=2001))
        x1_before = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == "lot-e1").price

        sm.apply(_sell_fill("e2", "0.0531", ts=2002))
        x1_after = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == "lot-e1").price

        assert x1_before == x1_after, f"x1 mutated: {x1_before} -> {x1_after}"

        sm.apply(_sell_fill("e3", "0.0533", ts=2003))
        x1_final = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == "lot-e1").price
        x2_final = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == "lot-e2").price

        assert x1_final == x1_before, f"x1 mutated after e3: {x1_before} -> {x1_final}"
        assert x2_final == Decimal("0.0529"), f"x2 mutated after e3: {x2_final}"


class TestNegativeProofOldBehavior:
    """Without the fix, the old raw-price collision guard would produce x2=0.0525."""

    def test_anomalous_x2_no_longer_produced(self) -> None:
        """The old bug produced x2=0.0525 instead of 0.0529."""
        sm = _sm_at("0.0527")
        sm.apply(_sell_fill("e1", "0.0529", ts=2001))
        sm.apply(_sell_fill("e2", "0.0531", ts=2002))

        x2 = next(eo for eo in sm.snapshot.exit_orders if eo.lot_id == "lot-e2").price
        assert x2 != Decimal("0.0525"), "Old anomalous x2=0.0525 still produced"
        assert x2 == Decimal("0.0529"), f"Expected x2=0.0529, got {x2}"


class TestCollisionGuardStillWorks:
    """The collision guard must still shift exits when there's a genuine collision."""

    def test_genuine_collision_shifted(self) -> None:
        """Two entries at adjacent prices: exits properly spaced by guard."""
        cfg = GridV2Config(
            grid_step_pct=Decimal("0.01"),
            entry_levels_per_side=3,
            order_size=Decimal("1"),
            max_inventory_levels=5,
            max_inventory_notional_usd=Decimal("10000"),
            price_tick_size=Decimal("0.01"),
            reseed_on_flat=True,
        )
        sm = GridV2StateMachine.create_initial(cfg, Decimal("100"), 1000)
        # Sell entries at 101 and 102 (on the sell ladder)
        sm.apply(EntryFilled("e1", OrderSide.SELL, Decimal("101"), Decimal("1"), 2001))
        sm.apply(EntryFilled("e2", OrderSide.SELL, Decimal("102"), Decimal("1"), 2002))

        exits = {eo.lot_id: eo.price for eo in sm.snapshot.exit_orders}
        # x1: raw 99.99, no collision
        # x2: raw 100.98, too close to 99.99 (0.99 < 1.00), shifted twice to 98.98
        assert exits["lot-e1"] == Decimal("99.99"), f"x1={exits['lot-e1']}"
        assert exits["lot-e2"] == Decimal("98.98"), f"x2={exits['lot-e2']}"
        # Verify both are tick-aligned and properly spaced
        assert exits["lot-e1"] % Decimal("0.01") == 0
        assert exits["lot-e2"] % Decimal("0.01") == 0
        from grinder.grid_v2.state import _grid_step_price  # noqa: PLC0415

        step = _grid_step_price(Decimal("100"), cfg.grid_step_pct, cfg.price_tick_size)
        assert abs(exits["lot-e1"] - exits["lot-e2"]) >= step
