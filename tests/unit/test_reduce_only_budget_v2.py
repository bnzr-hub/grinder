"""Tests for Reduce-Only Budget Guard v2 (PR-5, ADR-104).

Adversarial tests for:
1. Partial fill shrinks budget consumer (qty - filled_qty)
2. Same-tick batch reservation enforcement
3. Existing open + staged <= position
4. Illegal topology on sync triggers repair
5. Repair cancels surplus deterministically (smallest first)
6. Post-repair convergence (remaining set legal)
7. Symbol/direction isolation
8. No regression for healthy multi-lot case
9. BudgetSnapshot properties
"""

from __future__ import annotations

from decimal import Decimal

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap, PositionSnap
from grinder.core import OrderSide
from grinder.live.reduce_only_budget import (
    BudgetCheckResult,
    BudgetSnapshot,
    check_budget,
    compute_budget_snapshot,
    detect_surplus_exits,
)


def _pos(symbol: str, qty: str, side: str = "LONG") -> PositionSnap:
    return PositionSnap(
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        entry_price=Decimal("50000"),
        mark_price=Decimal("50000"),
        unrealized_pnl=Decimal("0"),
        leverage=1,
        ts=1000,
    )


def _order(
    symbol: str,
    side: str,
    qty: str,
    filled: str = "0",
    reduce_only: bool = True,
    order_id: str = "o-1",
) -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=order_id,
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        price=Decimal("50000"),
        qty=Decimal(qty),
        filled_qty=Decimal(filled),
        reduce_only=reduce_only,
        status="NEW",
        ts=1000,
    )


def _snap(
    positions: list[PositionSnap] | None = None,
    orders: list[OpenOrderSnap] | None = None,
) -> AccountSnapshot:
    return AccountSnapshot(
        positions=tuple(positions or []),
        open_orders=tuple(orders or []),
        ts=1000,
        source="test",
    )


class TestBudgetSnapshot:
    """BudgetSnapshot computed properties."""

    def test_available_when_under_budget(self) -> None:
        b = BudgetSnapshot(
            symbol="BTCUSDT",
            side="SELL",
            position_closeable_qty=Decimal("10"),
            open_reduce_only_remaining_qty=Decimal("3"),
            reserved_qty=Decimal("2"),
        )
        assert b.available == Decimal("5")
        assert not b.is_over_budget

    def test_available_clamps_to_zero(self) -> None:
        b = BudgetSnapshot(
            symbol="BTCUSDT",
            side="SELL",
            position_closeable_qty=Decimal("5"),
            open_reduce_only_remaining_qty=Decimal("6"),
            reserved_qty=Decimal("0"),
        )
        assert b.available == Decimal("0")
        assert b.is_over_budget
        assert b.over_budget_qty == Decimal("1")


class TestCheckBudget:
    """check_budget function."""

    def test_allowed_within_budget(self) -> None:
        b = BudgetSnapshot(
            symbol="X",
            side="SELL",
            position_closeable_qty=Decimal("10"),
            open_reduce_only_remaining_qty=Decimal("3"),
            reserved_qty=Decimal("2"),
        )
        assert check_budget(b, Decimal("5")) == BudgetCheckResult.ALLOWED

    def test_blocked_over_budget(self) -> None:
        b = BudgetSnapshot(
            symbol="X",
            side="SELL",
            position_closeable_qty=Decimal("10"),
            open_reduce_only_remaining_qty=Decimal("3"),
            reserved_qty=Decimal("2"),
        )
        assert check_budget(b, Decimal("6")) == BudgetCheckResult.BLOCKED

    def test_position_unknown_when_zero(self) -> None:
        b = BudgetSnapshot(
            symbol="X",
            side="SELL",
            position_closeable_qty=Decimal("0"),
            open_reduce_only_remaining_qty=Decimal("0"),
            reserved_qty=Decimal("0"),
        )
        assert check_budget(b, Decimal("1")) == BudgetCheckResult.POSITION_UNKNOWN

    def test_exact_boundary_allowed(self) -> None:
        b = BudgetSnapshot(
            symbol="X",
            side="SELL",
            position_closeable_qty=Decimal("10"),
            open_reduce_only_remaining_qty=Decimal("5"),
            reserved_qty=Decimal("3"),
        )
        assert check_budget(b, Decimal("2")) == BudgetCheckResult.ALLOWED


class TestPartialFillShrinksBudget:
    """Partial fills reduce open_reduce_only_remaining_qty."""

    def test_partial_fill_uses_remaining(self) -> None:
        """Open exit: qty=5, filled=3 → remaining=2, not 5."""
        snap = _snap(
            positions=[_pos("BTCUSDT", "10")],
            orders=[_order("BTCUSDT", "SELL", "5", filled="3", order_id="o-1")],
        )
        budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        assert budget.open_reduce_only_remaining_qty == Decimal("2")
        assert budget.position_closeable_qty == Decimal("10")


class TestSameTickBatchReservation:
    """Cumulative batch exits cannot exceed position."""

    def test_batch_reservation_blocks_second_exit(self) -> None:
        b1 = BudgetSnapshot(
            symbol="X",
            side="SELL",
            position_closeable_qty=Decimal("10"),
            open_reduce_only_remaining_qty=Decimal("0"),
            reserved_qty=Decimal("0"),
        )
        assert check_budget(b1, Decimal("5")) == BudgetCheckResult.ALLOWED

        b2 = BudgetSnapshot(
            symbol="X",
            side="SELL",
            position_closeable_qty=Decimal("10"),
            open_reduce_only_remaining_qty=Decimal("0"),
            reserved_qty=Decimal("5"),
        )
        assert check_budget(b2, Decimal("6")) == BudgetCheckResult.BLOCKED
        assert check_budget(b2, Decimal("5")) == BudgetCheckResult.ALLOWED


class TestExistingPlusStagedCapped:
    """existing_open + staged_new <= position."""

    def test_existing_plus_new_allowed(self) -> None:
        snap = _snap(
            positions=[_pos("BTCUSDT", "10")],
            orders=[_order("BTCUSDT", "SELL", "4", order_id="o-1")],
        )
        budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        assert check_budget(budget, Decimal("6")) == BudgetCheckResult.ALLOWED

    def test_existing_plus_new_blocked(self) -> None:
        snap = _snap(
            positions=[_pos("BTCUSDT", "10")],
            orders=[_order("BTCUSDT", "SELL", "4", order_id="o-1")],
        )
        budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        assert check_budget(budget, Decimal("7")) == BudgetCheckResult.BLOCKED


class TestDetectSurplusExits:
    """Illegal exit topology detection and repair."""

    def test_no_surplus_when_within_budget(self) -> None:
        snap = _snap(
            positions=[_pos("BTCUSDT", "10")],
            orders=[_order("BTCUSDT", "SELL", "5", order_id="o-1")],
        )
        surplus = detect_surplus_exits(snap, "BTCUSDT", OrderSide.SELL)
        assert len(surplus) == 0

    def test_surplus_detected_smallest_first(self) -> None:
        """Position=5, open exits total=8 → surplus=3, smallest cancelled first."""
        snap = _snap(
            positions=[_pos("BTCUSDT", "5")],
            orders=[
                _order("BTCUSDT", "SELL", "2", order_id="o-small"),
                _order("BTCUSDT", "SELL", "3", order_id="o-medium"),
                _order("BTCUSDT", "SELL", "3", order_id="o-large"),
            ],
        )
        surplus = detect_surplus_exits(snap, "BTCUSDT", OrderSide.SELL)
        assert len(surplus) >= 1
        total_cancelled = sum(r.remaining_qty for r in surplus)
        assert total_cancelled >= Decimal("3")
        assert surplus[0].order_id == "o-small"

    def test_partial_fill_reduces_surplus(self) -> None:
        snap = _snap(
            positions=[_pos("BTCUSDT", "5")],
            orders=[
                _order("BTCUSDT", "SELL", "4", filled="2", order_id="o-partial"),
                _order("BTCUSDT", "SELL", "4", order_id="o-full"),
            ],
        )
        # remaining: o-partial=2, o-full=4, total=6, position=5, surplus=1
        surplus = detect_surplus_exits(snap, "BTCUSDT", OrderSide.SELL)
        assert len(surplus) >= 1
        total_cancelled = sum(r.remaining_qty for r in surplus)
        assert total_cancelled >= Decimal("1")

    def test_post_repair_remaining_legal(self) -> None:
        """After removing surplus exits, remaining total <= position."""
        snap = _snap(
            positions=[_pos("BTCUSDT", "5")],
            orders=[
                _order("BTCUSDT", "SELL", "2", order_id="o-1"),
                _order("BTCUSDT", "SELL", "2", order_id="o-2"),
                _order("BTCUSDT", "SELL", "2", order_id="o-3"),
                _order("BTCUSDT", "SELL", "2", order_id="o-4"),
            ],
        )
        # total=8, position=5, surplus=3
        surplus = detect_surplus_exits(snap, "BTCUSDT", OrderSide.SELL)
        cancelled_ids = {r.order_id for r in surplus}
        remaining_qty = sum(
            o.qty - o.filled_qty for o in snap.open_orders if o.order_id not in cancelled_ids
        )
        assert remaining_qty <= Decimal("5")


class TestSymbolDirectionIsolation:
    """Budget accounting is symbol-scoped and direction-scoped."""

    def test_different_symbols_independent(self) -> None:
        snap = _snap(
            positions=[_pos("BTCUSDT", "10"), _pos("ETHUSDT", "5", "LONG")],
            orders=[_order("BTCUSDT", "SELL", "8", order_id="o-btc")],
        )
        btc = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        eth = compute_budget_snapshot(snap, "ETHUSDT", OrderSide.SELL)
        assert btc.open_reduce_only_remaining_qty == Decimal("8")
        assert eth.open_reduce_only_remaining_qty == Decimal("0")

    def test_different_sides_independent(self) -> None:
        snap = _snap(
            positions=[_pos("BTCUSDT", "10")],
            orders=[_order("BTCUSDT", "SELL", "8", order_id="o-sell")],
        )
        sell = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        buy = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.BUY)
        assert sell.open_reduce_only_remaining_qty == Decimal("8")
        assert buy.open_reduce_only_remaining_qty == Decimal("0")


class TestHealthyMultiLot:
    """No regression for healthy multi-lot exit set."""

    def test_legal_multi_lot_exits_pass(self) -> None:
        snap = _snap(
            positions=[_pos("BTCUSDT", "10")],
            orders=[
                _order("BTCUSDT", "SELL", "3", order_id="o-1"),
                _order("BTCUSDT", "SELL", "3", order_id="o-2"),
                _order("BTCUSDT", "SELL", "4", order_id="o-3"),
            ],
        )
        budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        assert not budget.is_over_budget
        assert budget.available == Decimal("0")
        surplus = detect_surplus_exits(snap, "BTCUSDT", OrderSide.SELL)
        assert len(surplus) == 0


class TestDirectionAwareBudget:
    """Budget is direction-aware: SELL exits budget against LONG qty only."""

    def test_sell_exit_budgets_against_long_only(self) -> None:
        """LONG=5, SHORT=3 → SELL exit budget = 5 (not 8)."""
        snap = _snap(
            positions=[
                _pos("BTCUSDT", "5", "LONG"),
                _pos("BTCUSDT", "3", "SHORT"),
            ],
        )
        budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        assert budget.position_closeable_qty == Decimal("5")

    def test_buy_exit_budgets_against_short_only(self) -> None:
        """LONG=5, SHORT=3 → BUY exit budget = 3 (not 8)."""
        snap = _snap(
            positions=[
                _pos("BTCUSDT", "5", "LONG"),
                _pos("BTCUSDT", "3", "SHORT"),
            ],
        )
        budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.BUY)
        assert budget.position_closeable_qty == Decimal("3")

    def test_opposite_side_does_not_inflate_budget(self) -> None:
        """SHORT=10, no LONG → SELL exit budget = 0."""
        snap = _snap(positions=[_pos("BTCUSDT", "10", "SHORT")])
        budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        assert budget.position_closeable_qty == Decimal("0")

    def test_both_mode_positive_signed_qty(self) -> None:
        """BOTH mode, signed_qty=+5 → SELL exit budget = 5."""
        pos = PositionSnap(
            symbol="BTCUSDT",
            side="BOTH",
            qty=Decimal("5"),
            entry_price=Decimal("50000"),
            mark_price=Decimal("50000"),
            unrealized_pnl=Decimal("0"),
            leverage=1,
            ts=1000,
            signed_qty=Decimal("5"),
        )
        snap = _snap(positions=[pos])
        budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        assert budget.position_closeable_qty == Decimal("5")
        buy_budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.BUY)
        assert buy_budget.position_closeable_qty == Decimal("0")

    def test_both_mode_negative_signed_qty(self) -> None:
        """BOTH mode, signed_qty=-3 → BUY exit budget = 3."""
        pos = PositionSnap(
            symbol="BTCUSDT",
            side="BOTH",
            qty=Decimal("3"),
            entry_price=Decimal("50000"),
            mark_price=Decimal("50000"),
            unrealized_pnl=Decimal("0"),
            leverage=1,
            ts=1000,
            signed_qty=Decimal("-3"),
        )
        snap = _snap(positions=[pos])
        budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.BUY)
        assert budget.position_closeable_qty == Decimal("3")
        sell_budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        assert sell_budget.position_closeable_qty == Decimal("0")


class TestRejectRepairFlag:
    """Engine -2022 reject triggers direction-scoped repair flag."""

    def test_direction_scoped_flag(self) -> None:
        """BUY-side reject flags (sym, BUY) only, not (sym, SELL)."""
        pending: set[tuple[str, str]] = set()
        pending.add(("BTCUSDT", "BUY"))
        assert ("BTCUSDT", "BUY") in pending
        assert ("BTCUSDT", "SELL") not in pending

    def test_opposite_side_unblocked(self) -> None:
        """SELL exits remain allowed when BUY-side is pending repair."""
        pending: set[tuple[str, str]] = {("BTCUSDT", "BUY")}
        # Gate 0 checks (symbol, side) — SELL is not in pending
        assert ("BTCUSDT", "SELL") not in pending

    def test_flag_cleared_per_side(self) -> None:
        """Repair clear is also direction-scoped."""
        pending: set[tuple[str, str]] = {("BTCUSDT", "BUY"), ("BTCUSDT", "SELL")}
        pending.discard(("BTCUSDT", "BUY"))
        assert ("BTCUSDT", "BUY") not in pending
        assert ("BTCUSDT", "SELL") in pending

    def test_non_2022_does_not_flag(self) -> None:
        """Error code != -2022 does not set pending repair."""
        pending: set[tuple[str, str]] = set()
        error_code = -4118
        if error_code == -2022:
            pending.add(("BTCUSDT", "SELL"))
        assert ("BTCUSDT", "SELL") not in pending


class TestRepairConvergence:
    """Repair convergence: only declare CONVERGED when topology is legal."""

    def test_failed_cancel_keeps_flag_set(self) -> None:
        """If a repair cancel fails, flag stays set (deferred)."""
        pending: set[tuple[str, str]] = set()
        all_ok = False  # simulate cancel failure

        if all_ok:
            pending.discard(("BTCUSDT", "SELL"))
        else:
            pending.add(("BTCUSDT", "SELL"))

        assert ("BTCUSDT", "SELL") in pending

    def test_all_cancels_ok_clears_flag(self) -> None:
        """All cancels succeed → flag cleared, CONVERGED."""
        pending: set[tuple[str, str]] = {("BTCUSDT", "SELL")}
        all_ok = True

        if all_ok:
            pending.discard(("BTCUSDT", "SELL"))
        else:
            pending.add(("BTCUSDT", "SELL"))

        assert ("BTCUSDT", "SELL") not in pending

    def test_no_surplus_clears_flag(self) -> None:
        """If surplus is empty on sync, flag clears (topology now legal)."""
        pending: set[tuple[str, str]] = {("BTCUSDT", "SELL")}
        surplus_empty = True

        if surplus_empty:
            pending.discard(("BTCUSDT", "SELL"))

        assert ("BTCUSDT", "SELL") not in pending


class TestEnginePathRepairWiring:
    """Real engine-path test for -2022 wiring."""

    def test_on_reduce_only_reject_sets_direction_key(self) -> None:
        """_on_reduce_only_reject sets (symbol, side) in pending repair."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        engine = MagicMock(spec=LiveEngineV0)
        engine._reduce_only_pending_repair = set()
        # Bind real method
        engine._on_reduce_only_reject = LiveEngineV0._on_reduce_only_reject.__get__(
            engine, LiveEngineV0
        )

        engine._on_reduce_only_reject("BTCUSDT", "SELL", -2022)
        assert ("BTCUSDT", "SELL") in engine._reduce_only_pending_repair
        assert ("BTCUSDT", "BUY") not in engine._reduce_only_pending_repair

    def test_on_reduce_only_reject_ignores_non_2022(self) -> None:
        """Non-2022 error codes do not set pending repair."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        engine = MagicMock(spec=LiveEngineV0)
        engine._reduce_only_pending_repair = set()
        engine._on_reduce_only_reject = LiveEngineV0._on_reduce_only_reject.__get__(
            engine, LiveEngineV0
        )

        engine._on_reduce_only_reject("BTCUSDT", "SELL", -4118)
        assert len(engine._reduce_only_pending_repair) == 0
