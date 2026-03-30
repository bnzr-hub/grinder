"""Unit tests for invariant helpers — positive and negative proofs.

Each invariant helper must:
- pass on valid input
- fail on deliberately malformed input (negative proof)
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.grid_v2.state import ExitOrder, ExitOrderStatus, InventoryLot, LotSide, LotStatus
from tests.helpers.invariants import (
    assert_actions_step_aligned,
    assert_actions_tick_aligned,
    assert_cleanup_converged,
    assert_entries_on_ladder,
    assert_exit_budget_legal,
    assert_lot_exit_consistency,
    assert_no_deterministic_churn,
    assert_no_duplicate_slots,
    assert_step_aligned,
    assert_tick_aligned,
)


def _place(
    cid: str,
    side: OrderSide = OrderSide.BUY,
    price: str = "50000",
    qty: str = "0.001",
) -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="BTCUSDT",
        side=side,
        price=Decimal(price),
        quantity=Decimal(qty),
        client_order_id=cid,
        reason="test",
    )


def _cancel(cid: str) -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.CANCEL,
        order_id=cid,
        symbol="BTCUSDT",
        reason="test",
    )


def _lot(lot_id: str, status: LotStatus = LotStatus.OPEN) -> InventoryLot:
    return InventoryLot(
        lot_id=lot_id,
        side=LotSide.LONG,
        entry_price=Decimal("50000"),
        qty=Decimal("0.001"),
        opened_at_ts=1000,
        source_entry_order_id="entry-1",
        exit_price=Decimal("50100"),
        exit_order_id=f"exit-{lot_id}",
        status=status,
    )


def _exit(lot_id: str, status: ExitOrderStatus = ExitOrderStatus.OPEN) -> ExitOrder:
    return ExitOrder(
        exit_order_id=f"exit-{lot_id}",
        lot_id=lot_id,
        side=OrderSide.SELL,
        price=Decimal("50100"),
        qty=Decimal("0.001"),
        status=status,
    )


# ===== INV-1: tick_aligned =====


class TestTickAligned:
    def test_aligned_passes(self) -> None:
        assert_tick_aligned(Decimal("50000.10"), Decimal("0.10"))

    def test_misaligned_fails(self) -> None:
        with pytest.raises(AssertionError, match="INV-1"):
            assert_tick_aligned(Decimal("50000.15"), Decimal("0.10"))

    def test_zero_remainder_edge(self) -> None:
        assert_tick_aligned(Decimal("0.0001"), Decimal("0.0001"))

    def test_invalid_tick_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            assert_tick_aligned(Decimal("50000"), Decimal("0"))


class TestActionsTickAligned:
    def test_all_aligned_passes(self) -> None:
        actions = [_place("a", price="50000.10"), _place("b", price="50000.20")]
        assert_actions_tick_aligned(actions, Decimal("0.10"))

    def test_one_misaligned_fails(self) -> None:
        actions = [_place("a", price="50000.10"), _place("bad", price="50000.15")]
        with pytest.raises(AssertionError, match="cid=bad"):
            assert_actions_tick_aligned(actions, Decimal("0.10"))

    def test_cancel_actions_ignored(self) -> None:
        actions: list[ExecutionAction] = [_cancel("c1")]
        assert_actions_tick_aligned(actions, Decimal("0.10"))


# ===== INV-2: step_aligned =====


class TestStepAligned:
    def test_aligned_passes(self) -> None:
        assert_step_aligned(Decimal("0.003"), Decimal("0.001"))

    def test_misaligned_fails(self) -> None:
        with pytest.raises(AssertionError, match="INV-2"):
            assert_step_aligned(Decimal("0.0015"), Decimal("0.001"))


class TestActionsStepAligned:
    def test_all_aligned_passes(self) -> None:
        actions = [_place("a", qty="0.001"), _place("b", qty="0.002")]
        assert_actions_step_aligned(actions, Decimal("0.001"))

    def test_one_misaligned_fails(self) -> None:
        actions = [_place("ok", qty="0.001"), _place("bad", qty="0.0015")]
        with pytest.raises(AssertionError, match="cid=bad"):
            assert_actions_step_aligned(actions, Decimal("0.001"))


# ===== INV-3: no_duplicate_slots =====


class TestNoDuplicateSlots:
    def test_distinct_slots_pass(self) -> None:
        actions = [
            _place("a", side=OrderSide.BUY, price="49900"),
            _place("b", side=OrderSide.BUY, price="49800"),
            _place("c", side=OrderSide.SELL, price="49900"),  # same price, different side
        ]
        assert_no_duplicate_slots(actions)

    def test_duplicate_slot_fails(self) -> None:
        actions = [
            _place("a", side=OrderSide.BUY, price="49900"),
            _place("b", side=OrderSide.BUY, price="49900"),
        ]
        with pytest.raises(AssertionError, match="INV-3"):
            assert_no_duplicate_slots(actions)

    def test_cancel_actions_ignored(self) -> None:
        actions: list[ExecutionAction] = [_cancel("c1"), _cancel("c2")]
        assert_no_duplicate_slots(actions)


# ===== INV-4: lot/exit consistency =====


class TestLotExitConsistency:
    def test_matched_lots_pass(self) -> None:
        lots = (_lot("L1"), _lot("L2"))
        exits = (_exit("L1"), _exit("L2"))
        assert_lot_exit_consistency(lots, exits)

    def test_orphan_lot_fails(self) -> None:
        lots = (_lot("L1"), _lot("L2"))
        exits = (_exit("L1"),)  # L2 has no exit
        with pytest.raises(AssertionError, match=r"INV-4.*L2"):
            assert_lot_exit_consistency(lots, exits)

    def test_closed_lot_ignored(self) -> None:
        lots = (_lot("L1"), _lot("L2", status=LotStatus.CLOSED))
        exits = (_exit("L1"),)  # L2 is closed, no exit needed
        assert_lot_exit_consistency(lots, exits)

    def test_canceled_exit_is_orphan(self) -> None:
        lots = (_lot("L1"),)
        exits = (_exit("L1", status=ExitOrderStatus.CANCELED),)
        with pytest.raises(AssertionError, match=r"INV-4.*L1"):
            assert_lot_exit_consistency(lots, exits)


# ===== INV-5: exit budget =====


class TestExitBudgetLegal:
    def test_within_budget_passes(self) -> None:
        assert_exit_budget_legal(Decimal("0.003"), Decimal("0.005"))

    def test_exact_budget_passes(self) -> None:
        assert_exit_budget_legal(Decimal("0.005"), Decimal("0.005"))

    def test_over_budget_fails(self) -> None:
        with pytest.raises(AssertionError, match="INV-5"):
            assert_exit_budget_legal(Decimal("0.006"), Decimal("0.005"))


# ===== INV-6: entries on ladder =====


class TestEntriesOnLadder:
    def test_all_on_ladder_passes(self) -> None:
        entries = {Decimal("49900"), Decimal("49800")}
        ladder = {Decimal("49900"), Decimal("49800"), Decimal("49700")}
        assert_entries_on_ladder(entries, ladder)

    def test_off_ladder_fails(self) -> None:
        entries = {Decimal("49900"), Decimal("49999")}
        ladder = {Decimal("49900"), Decimal("49800")}
        with pytest.raises(AssertionError, match=r"INV-6.*49999"):
            assert_entries_on_ladder(entries, ladder)

    def test_empty_entries_passes(self) -> None:
        assert_entries_on_ladder(set(), {Decimal("49900")})


# ===== INV-7: cleanup converged =====


class TestCleanupConverged:
    def test_fully_converged_passes(self) -> None:
        assert_cleanup_converged(mode="FLAT", open_lot_count=0, active_order_count=0)

    def test_non_flat_mode_fails(self) -> None:
        with pytest.raises(AssertionError, match=r"INV-7.*mode=LONG_BRANCH"):
            assert_cleanup_converged(mode="LONG_BRANCH", open_lot_count=0, active_order_count=0)

    def test_remaining_lots_fails(self) -> None:
        with pytest.raises(AssertionError, match=r"INV-7.*open_lots=2"):
            assert_cleanup_converged(mode="FLAT", open_lot_count=2, active_order_count=0)

    def test_remaining_orders_fails(self) -> None:
        with pytest.raises(AssertionError, match=r"INV-7.*active_orders=3"):
            assert_cleanup_converged(mode="FLAT", open_lot_count=0, active_order_count=3)

    def test_multiple_violations_reported(self) -> None:
        with pytest.raises(AssertionError, match=r"INV-7.*mode=.*open_lots=.*active_orders="):
            assert_cleanup_converged(mode="SHORT_BRANCH", open_lot_count=1, active_order_count=2)


# ===== INV-8: no deterministic churn =====


class TestNoDeterministicChurn:
    def test_no_overlap_passes(self) -> None:
        assert_no_deterministic_churn({"a", "b"}, {"c", "d"})

    def test_churn_detected(self) -> None:
        with pytest.raises(AssertionError, match=r"INV-8.*placed then canceled"):
            assert_no_deterministic_churn({"a", "b"}, {"b", "c"})

    def test_empty_sets_pass(self) -> None:
        assert_no_deterministic_churn(set(), set())
