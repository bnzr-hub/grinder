"""Tests for adaptive grid spacing policy."""

from __future__ import annotations

from decimal import Decimal

from grinder.selector.models import SkipReason
from grinder.selector.spacing import (
    DEFAULT_MIN_SPACING_BPS,
    compute_adaptive_spacing_bps,
)


class TestComputeAdaptiveSpacingBps:
    def test_natr_1pct(self) -> None:
        assert compute_adaptive_spacing_bps(Decimal("1.0")) == Decimal("50")

    def test_natr_2pct(self) -> None:
        assert compute_adaptive_spacing_bps(Decimal("2.0")) == Decimal("100")

    def test_natr_3pct(self) -> None:
        assert compute_adaptive_spacing_bps(Decimal("3.0")) == Decimal("150")

    def test_natr_0_9pct(self) -> None:
        assert compute_adaptive_spacing_bps(Decimal("0.9")) == Decimal("45")

    def test_natr_1_5pct(self) -> None:
        assert compute_adaptive_spacing_bps(Decimal("1.5")) == Decimal("75")

    def test_natr_zero(self) -> None:
        assert compute_adaptive_spacing_bps(Decimal("0")) == Decimal("0")

    def test_decimal_precision(self) -> None:
        result = compute_adaptive_spacing_bps(Decimal("1.33"))
        assert result == Decimal("66.50")


class TestConstants:
    def test_default_min_spacing(self) -> None:
        assert Decimal("50") == DEFAULT_MIN_SPACING_BPS

    def test_skip_reason_exists(self) -> None:
        assert SkipReason.GRID_SPACING_BELOW_MIN.value == "GRID_SPACING_BELOW_MIN"
