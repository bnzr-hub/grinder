"""Tests for deterministic symbol tuning solver (PR-B2, ADR-124).

Covers:
- TUNED for typical and low-price symbols
- NO_GO for all reason codes
- Determinism (same inputs -> same output)
- Boundary cases for min_notional rounding
- ceil_to_step correctness
"""

from __future__ import annotations

from decimal import Decimal

from grinder.execution.engine import SymbolConstraints
from grinder.tuning.solver import (
    NoGoReason,
    TuningSolverConfig,
    TuningStatus,
    ceil_to_step,
    solve,
)

# --- Reusable fixtures ---

BTC_CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    tick_size=Decimal("0.10"),
    min_notional=Decimal("5"),
)

CHEAP_CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("1"),
    min_qty=Decimal("1"),
    tick_size=Decimal("0.0001"),
    min_notional=Decimal("5"),
)

DEFAULT_CONFIG = TuningSolverConfig(
    max_position_usd=Decimal("10000"),
    max_inventory_levels=5,
)


# --- Tests: ceil_to_step ---


class TestCeilToStep:
    """Tests for upward step-rounding."""

    def test_exact_multiple(self) -> None:
        """Exact multiple stays unchanged."""
        assert ceil_to_step(Decimal("0.003"), Decimal("0.001")) == Decimal("0.003")

    def test_rounds_up(self) -> None:
        """Fractional step rounds upward."""
        assert ceil_to_step(Decimal("0.0015"), Decimal("0.001")) == Decimal("0.002")

    def test_rounds_up_tiny_excess(self) -> None:
        """Even smallest excess rounds up one step."""
        assert ceil_to_step(Decimal("1.0001"), Decimal("1")) == Decimal("2")

    def test_zero_qty(self) -> None:
        """Zero quantity stays zero."""
        assert ceil_to_step(Decimal("0"), Decimal("0.001")) == Decimal("0.000")

    def test_zero_step(self) -> None:
        """Zero step size returns qty unchanged."""
        assert ceil_to_step(Decimal("1.5"), Decimal("0")) == Decimal("1.5")

    def test_integer_step(self) -> None:
        """Integer step size."""
        assert ceil_to_step(Decimal("3"), Decimal("5")) == Decimal("5")
        assert ceil_to_step(Decimal("5"), Decimal("5")) == Decimal("5")
        assert ceil_to_step(Decimal("6"), Decimal("5")) == Decimal("10")


# --- Tests: TUNED cases ---


class TestTunedCases:
    """Solver returns TUNED with legal config."""

    def test_typical_btc(self) -> None:
        """BTC-like symbol with generous cap -> TUNED."""
        result = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), DEFAULT_CONFIG)

        assert result.status == TuningStatus.TUNED
        assert result.reason is None
        assert result.symbol == "BTCUSDT"
        assert result.order_size == Decimal("0.001")
        assert result.tick_size == Decimal("0.10")
        assert result.step_size == Decimal("0.001")
        assert result.max_inventory_levels == 5

    def test_cheap_symbol_notional_drives_qty(self) -> None:
        """Cheap symbol where min_notional forces qty upward.

        With worst-case deep price (default spacing 25bps * 5 levels = 1.25% below mid):
        - worst_case_price = 1.26 * (1 - 0.0025 * 5) = 1.24425
        - raw_qty = 5 / 1.24425 = 4.018..., ceil_to_step = 5
        """
        constraints = SymbolConstraints(
            step_size=Decimal("1"),
            min_qty=Decimal("1"),
            tick_size=Decimal("0.0001"),
            min_notional=Decimal("5"),
        )
        config = TuningSolverConfig(max_position_usd=Decimal("100"), max_inventory_levels=5)

        result = solve("AIOTUSDT", constraints, Decimal("1.26"), config)

        assert result.status == TuningStatus.TUNED
        # Verify legal at deepest level
        deepest = Decimal("1.26") * (1 - Decimal("0.0025") * 5)
        assert result.order_size is not None
        assert result.order_size * deepest >= Decimal("5")

    def test_min_notional_zero(self) -> None:
        """min_notional=0 means notional check is skipped, use min_qty."""
        constraints = SymbolConstraints(
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            tick_size=Decimal("0.10"),
            min_notional=Decimal("0"),
        )
        result = solve("BTCUSDT", constraints, Decimal("80000"), DEFAULT_CONFIG)

        assert result.status == TuningStatus.TUNED
        assert result.order_size == Decimal("0.001")

    def test_min_qty_greater_than_notional_driven(self) -> None:
        """When min_qty already exceeds notional requirement, use min_qty."""
        # min_notional=5, price=100, raw_qty=0.05, ceil(0.05, 0.01)=0.05
        # but min_qty=1 > 0.05, so order_size=1
        constraints = SymbolConstraints(
            step_size=Decimal("0.01"),
            min_qty=Decimal("1"),
            tick_size=Decimal("0.01"),
            min_notional=Decimal("5"),
        )
        result = solve("SOLUSDT", constraints, Decimal("100"), DEFAULT_CONFIG)

        assert result.status == TuningStatus.TUNED
        assert result.order_size == Decimal("1")


# --- Tests: NO_GO cases ---


class TestNoGoCases:
    """Solver returns NO_GO with specific reason."""

    def test_blacklisted(self) -> None:
        """Blacklisted symbol is rejected immediately."""
        config = TuningSolverConfig(
            max_position_usd=Decimal("10000"),
            max_inventory_levels=5,
            blacklist=frozenset({"SCAMUSDT"}),
        )
        result = solve("SCAMUSDT", BTC_CONSTRAINTS, Decimal("80000"), config)

        assert result.status == TuningStatus.NO_GO
        assert result.reason == NoGoReason.BLACKLISTED
        assert result.order_size is None

    def test_price_zero(self) -> None:
        """Zero price -> PRICE_UNAVAILABLE."""
        result = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("0"), DEFAULT_CONFIG)

        assert result.status == TuningStatus.NO_GO
        assert result.reason == NoGoReason.PRICE_UNAVAILABLE

    def test_price_negative(self) -> None:
        """Negative price -> PRICE_UNAVAILABLE."""
        result = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("-1"), DEFAULT_CONFIG)

        assert result.status == TuningStatus.NO_GO
        assert result.reason == NoGoReason.PRICE_UNAVAILABLE

    def test_tick_size_zero(self) -> None:
        """tick_size=0 -> TICK_SIZE_UNAVAILABLE."""
        constraints = SymbolConstraints(
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            tick_size=Decimal("0"),
            min_notional=Decimal("5"),
        )
        result = solve("BTCUSDT", constraints, Decimal("80000"), DEFAULT_CONFIG)

        assert result.status == TuningStatus.NO_GO
        assert result.reason == NoGoReason.TICK_SIZE_UNAVAILABLE

    def test_step_size_zero(self) -> None:
        """step_size=0 -> STEP_SIZE_UNAVAILABLE."""
        constraints = SymbolConstraints(
            step_size=Decimal("0"),
            min_qty=Decimal("0.001"),
            tick_size=Decimal("0.10"),
            min_notional=Decimal("5"),
        )
        result = solve("BTCUSDT", constraints, Decimal("80000"), DEFAULT_CONFIG)

        assert result.status == TuningStatus.NO_GO
        assert result.reason == NoGoReason.STEP_SIZE_UNAVAILABLE

    def test_notional_too_low(self) -> None:
        """Notional floor forces order so large it busts budget -> NOTIONAL_TOO_LOW."""
        # price=1.26, min_notional=5, step=1 -> min_qty_for_notional=4
        # worst_case = 4 * 1.26 * 5 = 25.20 > cap=20
        # Root cause: exchange notional constraint, not risk cap misconfiguration
        constraints = SymbolConstraints(
            step_size=Decimal("1"),
            min_qty=Decimal("1"),
            tick_size=Decimal("0.0001"),
            min_notional=Decimal("5"),
        )
        config = TuningSolverConfig(max_position_usd=Decimal("20"), max_inventory_levels=5)

        result = solve("AIOTUSDT", constraints, Decimal("1.26"), config)

        assert result.status == TuningStatus.NO_GO
        assert result.reason == NoGoReason.NOTIONAL_TOO_LOW

    def test_position_exceeds_cap_without_notional(self) -> None:
        """min_qty alone busts budget (not notional-driven) -> POSITION_EXCEEDS_CAP."""
        # min_qty=0.001, price=80000, inv=5 -> 400 > cap=100
        # min_notional=5: at price=80000, notional_qty=0.001 (same as min_qty)
        # So NOT notional-driven — plain risk cap exceeded
        config = TuningSolverConfig(max_position_usd=Decimal("100"), max_inventory_levels=5)

        result = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), config)

        assert result.status == TuningStatus.NO_GO
        assert result.reason == NoGoReason.POSITION_EXCEEDS_CAP


# --- Tests: Determinism ---


class TestDeterminism:
    """Same inputs produce identical results."""

    def test_tuned_deterministic(self) -> None:
        """TUNED result is identical across invocations."""
        r1 = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), DEFAULT_CONFIG)
        r2 = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), DEFAULT_CONFIG)
        assert r1 == r2

    def test_no_go_deterministic(self) -> None:
        """NO_GO result is identical across invocations."""
        config = TuningSolverConfig(max_position_usd=Decimal("100"), max_inventory_levels=5)
        r1 = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), config)
        r2 = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), config)
        assert r1 == r2


# --- Tests: Boundary cases ---


class TestBoundaryCases:
    """Edge cases for notional rounding and budget checks."""

    def test_exact_notional_boundary(self) -> None:
        """Quantity sized for worst-case deep price, not just mid.

        price=5, min_notional=5, default spacing 25bps*5=1.25%:
        worst_case = 5 * (1 - 0.0125) = 4.9375
        raw_qty = 5 / 4.9375 = 1.0126..., ceil_to_step = 2
        """
        constraints = SymbolConstraints(
            step_size=Decimal("1"),
            min_qty=Decimal("1"),
            tick_size=Decimal("0.01"),
            min_notional=Decimal("5"),
        )
        result = solve("TESTUSDT", constraints, Decimal("5"), DEFAULT_CONFIG)

        assert result.status == TuningStatus.TUNED
        deepest = Decimal("5") * (1 - Decimal("0.0025") * 5)
        assert result.order_size is not None
        assert result.order_size * deepest >= Decimal("5")

    def test_just_below_notional_rounds_up(self) -> None:
        """Price just above min_notional/step boundary still rounds up."""
        # price=4.99, min_notional=5, step=1 -> raw_qty=1.002..., ceil=2
        constraints = SymbolConstraints(
            step_size=Decimal("1"),
            min_qty=Decimal("1"),
            tick_size=Decimal("0.01"),
            min_notional=Decimal("5"),
        )
        result = solve("TESTUSDT", constraints, Decimal("4.99"), DEFAULT_CONFIG)

        assert result.status == TuningStatus.TUNED
        assert result.order_size == Decimal("2")
        # Verify notional: 2 * 4.99 = 9.98 >= 5
        assert result.order_size is not None
        assert result.order_size * Decimal("4.99") >= Decimal("5")

    def test_worst_case_exactly_at_cap(self) -> None:
        """Worst-case position exactly equals cap -> TUNED (not exceeds)."""
        # order_size=0.001, price=80000, inv=5 -> 400.0
        config = TuningSolverConfig(max_position_usd=Decimal("400"), max_inventory_levels=5)

        result = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), config)

        assert result.status == TuningStatus.TUNED

    def test_worst_case_one_cent_over_cap(self) -> None:
        """Worst-case position barely exceeds cap -> NO_GO."""
        # order_size=0.001, price=80000, inv=5 -> 400.0 > 399.99
        config = TuningSolverConfig(max_position_usd=Decimal("399.99"), max_inventory_levels=5)

        result = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), config)

        assert result.status == TuningStatus.NO_GO
        assert result.reason == NoGoReason.POSITION_EXCEEDS_CAP

    def test_fine_step_ceil_precision(self) -> None:
        """Fine step size preserves Decimal precision."""
        # price=0.50, min_notional=5, step=0.01 -> raw_qty=10, ceil=10
        constraints = SymbolConstraints(
            step_size=Decimal("0.01"),
            min_qty=Decimal("0.01"),
            tick_size=Decimal("0.0001"),
            min_notional=Decimal("5"),
        )
        result = solve("TINYUSDT", constraints, Decimal("0.50"), DEFAULT_CONFIG)

        assert result.status == TuningStatus.TUNED
        # worst_case deep price = 0.50 * (1 - 0.0025 * 5) = 0.49375
        # min_qty_for_notional = ceil(5 / 0.49375, 0.01) = ceil(10.126...) = 10.13
        # order_size = max(0.01, 10.13) = 10.13
        assert result.order_size is not None
        assert result.order_size >= Decimal("10.00")


# --- Tests: Deep-level notional regression ---


class TestDeepLevelNotional:
    """Solver sizes against worst-case deep entry price, not just mid."""

    def test_drift_regression(self) -> None:
        """DRIFTUSDT regression: order_size must be legal at deepest BUY level.

        At price=0.0489, spacing=25bps, 5 levels:
        - deepest BUY = 0.0489 * (1 - 0.0025 * 5) = 0.04829
        - min_notional=5: need ceil(5 / 0.04829, 1) = 104
        - old solver would give 103 (illegal at deep level)
        """
        constraints = SymbolConstraints(
            step_size=Decimal("1"),
            min_qty=Decimal("1"),
            tick_size=Decimal("0.0001"),
            min_notional=Decimal("5"),
        )
        config = TuningSolverConfig(
            max_position_usd=Decimal("500"),
            max_inventory_levels=10,
            entry_levels_per_side=5,
            spacing_pct=Decimal("0.0025"),
        )
        result = solve("DRIFTUSDT", constraints, Decimal("0.0489"), config)

        assert result.status == TuningStatus.TUNED
        # Verify legal at deepest level
        deepest_price = Decimal("0.0489") * (1 - Decimal("0.0025") * 5)
        assert result.order_size is not None
        assert result.order_size * deepest_price >= Decimal("5")

    def test_mid_legal_deep_illegal_old_logic(self) -> None:
        """Symbol where mid price is legal but deepest is not under old logic."""
        constraints = SymbolConstraints(
            step_size=Decimal("1"),
            min_qty=Decimal("1"),
            tick_size=Decimal("0.0001"),
            min_notional=Decimal("5"),
        )
        # With spacing 50bps * 5 levels = 2.5% below mid
        config = TuningSolverConfig(
            max_position_usd=Decimal("500"),
            max_inventory_levels=10,
            entry_levels_per_side=5,
            spacing_pct=Decimal("0.005"),
        )
        result = solve("CHEAPUSDT", constraints, Decimal("0.0510"), config)

        assert result.status == TuningStatus.TUNED
        deepest = Decimal("0.0510") * (1 - Decimal("0.005") * 5)
        assert result.order_size is not None
        assert result.order_size * deepest >= Decimal("5")

    def test_deep_legal_qty_breaks_cap(self) -> None:
        """Deep-level legal qty is too large for position cap → NO_GO."""
        constraints = SymbolConstraints(
            step_size=Decimal("1"),
            min_qty=Decimal("1"),
            tick_size=Decimal("0.0001"),
            min_notional=Decimal("5"),
        )
        # Very wide spacing makes deepest price very low → large qty needed
        config = TuningSolverConfig(
            max_position_usd=Decimal("10"),
            max_inventory_levels=5,
            entry_levels_per_side=5,
            spacing_pct=Decimal("0.05"),  # 5% per level, 25% total
        )
        result = solve("LOWUSDT", constraints, Decimal("0.05"), config)

        assert result.status == TuningStatus.NO_GO

    def test_btc_unchanged(self) -> None:
        """BTC-like symbol behavior unchanged (deep price negligible impact)."""
        result = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), DEFAULT_CONFIG)
        assert result.status == TuningStatus.TUNED
        assert result.order_size == Decimal("0.001")
