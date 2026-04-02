"""Tests for duplicate price prevention in grid planner (Batch 2 PR-2).

Covers:
- Cheap symbol duplicate BUY levels prevented
- Cheap symbol duplicate SELL levels prevented
- All prices unique per side invariant
- Normal spacing behavior unchanged
- Determinism
"""

from __future__ import annotations

from decimal import Decimal

from grinder.live.grid_planner import LiveGridConfig, LiveGridPlannerV1


def _build_grid(
    mid_price: Decimal,
    spacing_bps: float,
    tick_size: Decimal,
    levels: int = 5,
) -> list:  # type: ignore[type-arg]
    """Build grid levels using LiveGridPlannerV1."""
    config = LiveGridConfig(levels=levels)
    planner = LiveGridPlannerV1(config=config)
    return planner._build_desired_grid(mid_price, spacing_bps, tick_size)


class TestDuplicateBuyPrevented:
    """Cheap symbol duplicate BUY levels after tick rounding are skipped."""

    def test_tight_spacing_coarse_tick(self) -> None:
        """spacing_bps=15, tick=0.0001, price=0.049 → some levels collapse."""
        levels = _build_grid(
            mid_price=Decimal("0.049"),
            spacing_bps=15.0,
            tick_size=Decimal("0.0001"),
            levels=5,
        )
        buy_prices = [lv.price for lv in levels if lv.side.value == "BUY"]
        assert len(buy_prices) == len(set(buy_prices)), f"Duplicate BUY prices: {buy_prices}"


class TestDuplicateSellPrevented:
    """Cheap symbol duplicate SELL levels after tick rounding are skipped."""

    def test_tight_spacing_coarse_tick(self) -> None:
        levels = _build_grid(
            mid_price=Decimal("0.049"),
            spacing_bps=15.0,
            tick_size=Decimal("0.0001"),
            levels=5,
        )
        sell_prices = [lv.price for lv in levels if lv.side.value == "SELL"]
        assert len(sell_prices) == len(set(sell_prices)), f"Duplicate SELL prices: {sell_prices}"


class TestAllPricesUniquePerSide:
    """Invariant: no duplicate prices within BUY or SELL side."""

    def test_various_cheap_symbols(self) -> None:
        """Test several price/tick combinations that could cause collisions."""
        cases = [
            (Decimal("0.049"), 15.0, Decimal("0.0001")),
            (Decimal("0.001"), 25.0, Decimal("0.0001")),
            (Decimal("0.10"), 10.0, Decimal("0.001")),
            (Decimal("1.50"), 5.0, Decimal("0.01")),
        ]
        for mid, spacing, tick in cases:
            levels = _build_grid(mid, spacing, tick, levels=5)
            buy = [lv.price for lv in levels if lv.side.value == "BUY"]
            sell = [lv.price for lv in levels if lv.side.value == "SELL"]
            assert len(buy) == len(set(buy)), f"BUY dup at mid={mid}: {buy}"
            assert len(sell) == len(set(sell)), f"SELL dup at mid={mid}: {sell}"


class TestNormalSpacingUnchanged:
    """Normal spacing on expensive symbols produces same levels as before."""

    def test_btc_like(self) -> None:
        levels = _build_grid(
            mid_price=Decimal("80000"),
            spacing_bps=10.0,
            tick_size=Decimal("0.10"),
            levels=5,
        )
        buy_prices = [lv.price for lv in levels if lv.side.value == "BUY"]
        sell_prices = [lv.price for lv in levels if lv.side.value == "SELL"]
        # 5 unique BUY + 5 unique SELL
        assert len(buy_prices) == 5
        assert len(sell_prices) == 5


class TestReducedLevelsOnCollapse:
    """When spacing is sub-tick, fewer unique levels are emitted."""

    def test_very_tight_spacing(self) -> None:
        """spacing=1bps on price=0.01 with tick=0.001 → many collisions."""
        levels = _build_grid(
            mid_price=Decimal("0.01"),
            spacing_bps=1.0,
            tick_size=Decimal("0.001"),
            levels=5,
        )
        buy = [lv.price for lv in levels if lv.side.value == "BUY"]
        sell = [lv.price for lv in levels if lv.side.value == "SELL"]
        # May have fewer than 5 unique levels, but no duplicates
        assert len(buy) == len(set(buy))
        assert len(sell) == len(set(sell))
        assert len(buy) < 5  # Confirm some were actually skipped


class TestDeterminism:
    def test_same_inputs_same_levels(self) -> None:
        l1 = _build_grid(Decimal("0.049"), 15.0, Decimal("0.0001"), 5)
        l2 = _build_grid(Decimal("0.049"), 15.0, Decimal("0.0001"), 5)
        assert [(lv.price, lv.side) for lv in l1] == [(lv.price, lv.side) for lv in l2]
