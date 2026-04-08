"""Tests for GridRiskSizer — budget-driven grid admissibility."""

from __future__ import annotations

from decimal import Decimal

from grinder.risk.grid_risk_sizer import (
    GridRiskSizer,
    GridRiskSizerConfig,
    GridRiskSizerInput,
)


def _inp(
    symbol: str = "TESTUSDT",
    price: str = "100",
    step_pct: str = "0.01",  # 1%
    risk_budget: str = "30",  # $30
    levels: int = 5,
    min_qty: str = "0.001",
    min_notional: str = "5",
    qty_step: str = "0.001",
) -> GridRiskSizerInput:
    return GridRiskSizerInput(
        symbol=symbol,
        price=Decimal(price),
        step_pct=Decimal(step_pct),
        symbol_risk_budget_usd=Decimal(risk_budget),
        entry_levels=levels,
        exchange_min_qty=Decimal(min_qty),
        exchange_min_notional=Decimal(min_notional),
        qty_step=Decimal(qty_step),
    )


def _sizer() -> GridRiskSizer:
    return GridRiskSizer()


class TestAdmissibleCase:
    def test_happy_path(self) -> None:
        """Reasonable budget + minimums → admissible."""
        r = _sizer().compute(_inp())
        assert r.admissible
        assert r.reason == "OK"
        assert r.order_qty_rounded > Decimal("0")
        assert r.max_position_notional_usd > Decimal("0")
        assert r.actual_order_notional_usd > Decimal("0")
        assert r.actual_order_notional_usd == r.order_qty_rounded * Decimal("100")

    def test_adverse_move_pct(self) -> None:
        """adverse_move = step_pct * (adverse_depth + 1)."""
        r = _sizer().compute(_inp(step_pct="0.01", levels=5))
        # Default adverse_depth=20: 1% * 21 = 0.21
        assert r.adverse_move_pct == Decimal("0.21")

    def test_max_position_notional(self) -> None:
        """max_position = risk_budget / adverse_move."""
        r = _sizer().compute(_inp(risk_budget="30", step_pct="0.01", levels=5))
        # adverse = 0.21, max_position = 30 / 0.21 = 142.857...
        assert r.max_position_notional_usd == Decimal("30") / Decimal("0.21")


class TestStepPctEffect:
    def test_larger_step_reduces_notional(self) -> None:
        """Wider grid step → smaller max position for same budget."""
        r_narrow = _sizer().compute(_inp(step_pct="0.01"))
        r_wide = _sizer().compute(_inp(step_pct="0.02"))
        assert r_wide.max_position_notional_usd < r_narrow.max_position_notional_usd


class TestLevelsEffect:
    def test_more_levels_does_not_change_order_size(self) -> None:
        """entry_levels only gates min_entry_levels, not order sizing.

        Order size is driven by adverse_depth (20) and max_inventory (15),
        not by visible entry_levels.
        """
        r_few = _sizer().compute(_inp(levels=5))
        r_many = _sizer().compute(_inp(levels=10))
        # Same adverse_depth and max_inventory → same order size
        assert r_many.order_qty_rounded == r_few.order_qty_rounded


class TestNoGoMinQty:
    def test_below_min_qty(self) -> None:
        """Risk-consistent qty below exchange min → NO_GO."""
        r = _sizer().compute(_inp(risk_budget="1", min_qty="10"))
        assert not r.admissible
        assert r.reason == "BELOW_MIN_QTY"


class TestNoGoMinNotional:
    def test_below_min_notional(self) -> None:
        """Risk-consistent notional below exchange min → NO_GO."""
        r = _sizer().compute(_inp(risk_budget="1", price="0.01", min_notional="100"))
        assert not r.admissible
        assert r.reason == "BELOW_MIN_NOTIONAL"


class TestQtyRounding:
    def test_rounds_down_not_up(self) -> None:
        """Qty rounded DOWN to exchange step."""
        r = _sizer().compute(_inp(qty_step="1", risk_budget="30", price="100", levels=5))
        # max_pos=500, order_notional=100, raw_qty=1.0 → rounded=1
        assert r.order_qty_rounded == r.order_qty_rounded.quantize(Decimal("1"))
        assert r.order_qty_rounded <= r.order_qty_raw


class TestInvalidInput:
    def test_zero_price(self) -> None:
        r = _sizer().compute(_inp(price="0"))
        assert not r.admissible
        assert r.reason == "INVALID_INPUT"

    def test_zero_budget(self) -> None:
        r = _sizer().compute(_inp(risk_budget="0"))
        assert not r.admissible
        assert r.reason == "INVALID_INPUT"

    def test_zero_step(self) -> None:
        r = _sizer().compute(_inp(step_pct="0"))
        assert not r.admissible
        assert r.reason == "INVALID_INPUT"


class TestMinEntryLevels:
    def test_below_min_levels_rejected(self) -> None:
        """entry_levels < min_entry_levels → NO_GO."""
        sizer = GridRiskSizer(config=GridRiskSizerConfig(min_entry_levels=15))
        r = sizer.compute(_inp(levels=5))
        assert not r.admissible
        assert r.reason == "BELOW_MIN_LEVELS"

    def test_at_min_levels_accepted(self) -> None:
        """entry_levels == min_entry_levels → admissible."""
        sizer = GridRiskSizer(config=GridRiskSizerConfig(min_entry_levels=5))
        r = sizer.compute(_inp(levels=5))
        assert r.admissible


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        sizer = _sizer()
        inp = _inp()
        r1 = sizer.compute(inp)
        r2 = sizer.compute(inp)
        assert r1 == r2
