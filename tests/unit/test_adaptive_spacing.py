"""Tests for adaptive grid spacing policy."""

from __future__ import annotations

from decimal import Decimal

from grinder.selector.models import SkipReason
from grinder.selector.spacing import (
    DEFAULT_MIN_SPACING_BPS,
    compute_adaptive_spacing_bps,
)


class TestComputeAdaptiveSpacingBps:
    def test_natr_2pct(self) -> None:
        # 2.0 * 30 = 60
        assert compute_adaptive_spacing_bps(Decimal("2.0")) == Decimal("60")

    def test_natr_3pct(self) -> None:
        # 3.0 * 30 = 90
        assert compute_adaptive_spacing_bps(Decimal("3.0")) == Decimal("90")

    def test_natr_1_67pct_boundary(self) -> None:
        # 1.67 * 30 = 50.1 → just above min_spacing floor
        assert compute_adaptive_spacing_bps(Decimal("1.67")) > DEFAULT_MIN_SPACING_BPS

    def test_natr_1_5pct_below_floor(self) -> None:
        # 1.5 * 30 = 45 → below min_spacing floor
        assert compute_adaptive_spacing_bps(Decimal("1.5")) < DEFAULT_MIN_SPACING_BPS

    def test_natr_zero(self) -> None:
        assert compute_adaptive_spacing_bps(Decimal("0")) == Decimal("0")

    def test_decimal_precision(self) -> None:
        result = compute_adaptive_spacing_bps(Decimal("1.33"))
        # 1.33 * 30 = 39.90
        assert result == Decimal("39.90")


class TestConstants:
    def test_default_min_spacing(self) -> None:
        assert Decimal("50") == DEFAULT_MIN_SPACING_BPS

    def test_skip_reason_exists(self) -> None:
        assert SkipReason.GRID_SPACING_BELOW_MIN.value == "GRID_SPACING_BELOW_MIN"
