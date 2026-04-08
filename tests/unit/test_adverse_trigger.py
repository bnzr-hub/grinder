"""Tests for adverse grid level trigger computation."""

from __future__ import annotations

from decimal import Decimal

from grinder.risk.adverse_trigger import (
    compute_adverse_threshold,
    is_adverse_level_breached,
)


class TestComputeThreshold:
    def test_long_adverse_down(self) -> None:
        """LONG branch: adverse = price DOWN."""
        t = compute_adverse_threshold(
            reference_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            adverse_level=16,
            side="LONG",
        )
        # anchor=100, step=1.00, threshold = 100 - 1.00*16 = 84
        assert t == Decimal("84.00")

    def test_short_adverse_up(self) -> None:
        """SHORT branch: adverse = price UP."""
        t = compute_adverse_threshold(
            reference_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            adverse_level=16,
            side="SHORT",
        )
        # anchor=100, step=1.00, threshold = 100 + 1.00*16 = 116
        assert t == Decimal("116.00")

    def test_tick_quantized_step(self) -> None:
        """Step is rounded up to nearest tick."""
        t = compute_adverse_threshold(
            reference_price=Decimal("0.01"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.0001"),
            adverse_level=16,
            side="LONG",
        )
        # anchor=0.0100, step=ceil(0.0001/0.0001)=1 tick=0.0001
        # threshold = 0.0100 - 0.0001*16 = 0.0084
        assert t == Decimal("0.0084")

    def test_invalid_inputs_none(self) -> None:
        assert (
            compute_adverse_threshold(Decimal("0"), Decimal("0.01"), Decimal("0.01"), 16, "LONG")
            is None
        )
        assert (
            compute_adverse_threshold(Decimal("100"), Decimal("0"), Decimal("0.01"), 16, "LONG")
            is None
        )
        assert (
            compute_adverse_threshold(Decimal("100"), Decimal("0.01"), Decimal("0"), 16, "LONG")
            is None
        )
        assert (
            compute_adverse_threshold(Decimal("100"), Decimal("0.01"), Decimal("0.01"), 0, "LONG")
            is None
        )

    def test_unknown_side_none(self) -> None:
        assert (
            compute_adverse_threshold(
                Decimal("100"), Decimal("0.01"), Decimal("0.01"), 16, "UNKNOWN"
            )
            is None
        )

    def test_deterministic(self) -> None:
        args = (Decimal("100"), Decimal("0.01"), Decimal("0.01"), 16, "LONG")
        assert compute_adverse_threshold(*args) == compute_adverse_threshold(*args)


class TestIsBreached:
    def test_long_breached_at_threshold(self) -> None:
        assert is_adverse_level_breached(Decimal("84"), Decimal("84"), "LONG")

    def test_long_breached_below(self) -> None:
        assert is_adverse_level_breached(Decimal("83"), Decimal("84"), "LONG")

    def test_long_not_breached_above(self) -> None:
        assert not is_adverse_level_breached(Decimal("85"), Decimal("84"), "LONG")

    def test_short_breached_at_threshold(self) -> None:
        assert is_adverse_level_breached(Decimal("116"), Decimal("116"), "SHORT")

    def test_short_breached_above(self) -> None:
        assert is_adverse_level_breached(Decimal("117"), Decimal("116"), "SHORT")

    def test_short_not_breached_below(self) -> None:
        assert not is_adverse_level_breached(Decimal("115"), Decimal("116"), "SHORT")
