"""Tests for _derive_market_regime — portfolio-level regime from NATR."""

from __future__ import annotations

from decimal import Decimal

from grinder.risk.portfolio_budget_allocator import MarketRegime


def _derive() -> object:
    """Import the adapter from scripts module."""
    from scripts.run_autonomous import _derive_market_regime  # noqa: PLC0415

    return _derive_market_regime


class TestRegimeFromNATR:
    def test_empty_natr_returns_neutral(self) -> None:
        derive = _derive()
        assert derive({}) == MarketRegime.NEUTRAL

    def test_low_natr_returns_good(self) -> None:
        """All symbols calm → GOOD."""
        derive = _derive()
        natr = {"A": Decimal("0.5"), "B": Decimal("0.8"), "C": Decimal("0.9")}
        assert derive(natr) == MarketRegime.GOOD

    def test_moderate_natr_returns_neutral(self) -> None:
        """Median NATR in middle range → NEUTRAL."""
        derive = _derive()
        natr = {"A": Decimal("1.5"), "B": Decimal("2.0"), "C": Decimal("2.5")}
        assert derive(natr) == MarketRegime.NEUTRAL

    def test_high_natr_returns_toxic(self) -> None:
        """All symbols volatile → TOXIC."""
        derive = _derive()
        natr = {"A": Decimal("3.5"), "B": Decimal("4.0"), "C": Decimal("5.0")}
        assert derive(natr) == MarketRegime.TOXIC

    def test_boundary_good(self) -> None:
        """Median exactly at good_threshold → GOOD."""
        derive = _derive()
        natr = {"A": Decimal("1.0")}
        assert derive(natr) == MarketRegime.GOOD

    def test_boundary_toxic(self) -> None:
        """Median exactly at toxic_threshold → TOXIC."""
        derive = _derive()
        natr = {"A": Decimal("3.0")}
        assert derive(natr) == MarketRegime.TOXIC

    def test_custom_thresholds(self) -> None:
        """Custom thresholds override defaults."""
        derive = _derive()
        natr = {"A": Decimal("2.0")}
        # Default: NEUTRAL. With lower toxic threshold → TOXIC.
        assert derive(natr, toxic_threshold=Decimal("1.5")) == MarketRegime.TOXIC

    def test_single_symbol(self) -> None:
        derive = _derive()
        assert derive({"X": Decimal("0.5")}) == MarketRegime.GOOD
        assert derive({"X": Decimal("2.0")}) == MarketRegime.NEUTRAL
        assert derive({"X": Decimal("4.0")}) == MarketRegime.TOXIC

    def test_even_count_uses_median(self) -> None:
        """Even number of symbols → average of middle two."""
        derive = _derive()
        # median of [0.5, 1.5, 2.5, 3.5] = (1.5+2.5)/2 = 2.0 → NEUTRAL
        natr = {"A": Decimal("0.5"), "B": Decimal("1.5"), "C": Decimal("2.5"), "D": Decimal("3.5")}
        assert derive(natr) == MarketRegime.NEUTRAL

    def test_deterministic(self) -> None:
        derive = _derive()
        natr = {"A": Decimal("1.5"), "B": Decimal("2.0")}
        r1 = derive(natr)
        r2 = derive(natr)
        assert r1 == r2
