"""Reusable grid/execution invariant helpers.

Assert properties of correct behavior — not implementation details.
Designed to be applied across scenarios and future bugfixes.

Usage:
    from tests.helpers.invariants import (
        assert_tick_aligned,
        assert_no_duplicate_slots,
        assert_lot_exit_consistency,
        assert_exit_budget_legal,
        assert_cleanup_converged,
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

    from grinder.execution.types import ExecutionAction
    from grinder.grid_v2.state import ExitOrder, InventoryLot


# ---------------------------------------------------------------------------
# INV-1: Tick size legality
# ---------------------------------------------------------------------------


def assert_tick_aligned(price: Decimal, tick_size: Decimal, label: str = "") -> None:
    """Every price must be an exact multiple of tick_size.

    Raises AssertionError with diagnostic if not aligned.
    """
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")
    remainder = price % tick_size
    if remainder != 0:
        ctx = f" ({label})" if label else ""
        raise AssertionError(
            f"INV-1 tick_aligned: price {price}{ctx} not aligned to tick {tick_size} "
            f"(remainder={remainder})"
        )


def assert_actions_tick_aligned(
    actions: list[ExecutionAction],
    tick_size: Decimal,
) -> None:
    """All PLACE actions in the list must have tick-aligned prices."""
    from grinder.execution.types import ActionType  # noqa: PLC0415

    for a in actions:
        if a.action_type == ActionType.PLACE and a.price is not None:
            assert_tick_aligned(a.price, tick_size, label=f"cid={a.client_order_id}")


# ---------------------------------------------------------------------------
# INV-2: Step (lot) size legality
# ---------------------------------------------------------------------------


def assert_step_aligned(qty: Decimal, step_size: Decimal, label: str = "") -> None:
    """Every order quantity must be an exact multiple of step_size."""
    if step_size <= 0:
        raise ValueError(f"step_size must be positive, got {step_size}")
    remainder = qty % step_size
    if remainder != 0:
        ctx = f" ({label})" if label else ""
        raise AssertionError(
            f"INV-2 step_aligned: qty {qty}{ctx} not aligned to step {step_size} "
            f"(remainder={remainder})"
        )


def assert_actions_step_aligned(
    actions: list[ExecutionAction],
    step_size: Decimal,
) -> None:
    """All PLACE actions must have step-aligned quantities."""
    from grinder.execution.types import ActionType  # noqa: PLC0415

    for a in actions:
        if a.action_type == ActionType.PLACE and a.quantity is not None:
            assert_step_aligned(a.quantity, step_size, label=f"cid={a.client_order_id}")


# ---------------------------------------------------------------------------
# INV-3: No duplicate active slot
# ---------------------------------------------------------------------------


def assert_no_duplicate_slots(
    actions: list[ExecutionAction],
) -> None:
    """No two PLACE actions should target the same (side, price) slot."""
    from grinder.execution.types import ActionType  # noqa: PLC0415

    seen: dict[tuple[str, Decimal], str] = {}
    for a in actions:
        if a.action_type != ActionType.PLACE:
            continue
        if a.side is None or a.price is None:
            continue
        key = (a.side.value, a.price)
        if key in seen:
            raise AssertionError(
                f"INV-3 no_duplicate_slots: slot {key} claimed by both "
                f"cid={seen[key]} and cid={a.client_order_id}"
            )
        seen[key] = a.client_order_id or "?"


# ---------------------------------------------------------------------------
# INV-4: Lot/exit consistency (no orphan lot without exit intent)
# ---------------------------------------------------------------------------


def assert_lot_exit_consistency(
    open_lots: tuple[InventoryLot, ...],
    exit_orders: tuple[ExitOrder, ...],
) -> None:
    """Every open lot must have a corresponding OPEN exit order.

    Args:
        open_lots: SM snapshot open_lots (InventoryLot with lot_id, status)
        exit_orders: SM snapshot exit_orders (ExitOrder with lot_id, status)
    """
    from grinder.grid_v2.state import ExitOrderStatus, LotStatus  # noqa: PLC0415

    # Build set of lot_ids that have an OPEN exit
    exit_lot_ids: set[str] = set()
    for ex in exit_orders:
        if ex.status == ExitOrderStatus.OPEN:
            exit_lot_ids.add(ex.lot_id)

    orphans: list[str] = []
    for lot in open_lots:
        if lot.status == LotStatus.OPEN and lot.lot_id not in exit_lot_ids:
            orphans.append(lot.lot_id)

    if orphans:
        raise AssertionError(
            f"INV-4 lot_exit_consistency: {len(orphans)} open lot(s) without OPEN exit: {orphans}"
        )


# ---------------------------------------------------------------------------
# INV-5: Exit budget legality
# ---------------------------------------------------------------------------


def assert_exit_budget_legal(
    total_exit_qty: Decimal,
    closeable_qty: Decimal,
    label: str = "",
) -> None:
    """Total active/proposed exits must not exceed closeable position budget."""
    if total_exit_qty > closeable_qty:
        ctx = f" ({label})" if label else ""
        raise AssertionError(
            f"INV-5 exit_budget_legal{ctx}: total_exit_qty={total_exit_qty} > "
            f"closeable_qty={closeable_qty}"
        )


# ---------------------------------------------------------------------------
# INV-6: Entry ladder consistency
# ---------------------------------------------------------------------------


def assert_entries_on_ladder(
    entry_prices: set[Decimal],
    ladder_prices: set[Decimal],
    label: str = "",
) -> None:
    """Active entries must be on the effective desired ladder."""
    off_ladder = entry_prices - ladder_prices
    if off_ladder:
        ctx = f" ({label})" if label else ""
        raise AssertionError(
            f"INV-6 entries_on_ladder{ctx}: {len(off_ladder)} entries not on ladder: "
            f"{sorted(off_ladder)}"
        )


# ---------------------------------------------------------------------------
# INV-7: Cleanup convergence
# ---------------------------------------------------------------------------


def assert_cleanup_converged(
    mode: str,
    open_lot_count: int,
    active_order_count: int,
) -> None:
    """After cleanup, state must be FLAT with no orders and no lots."""
    violations: list[str] = []
    if mode != "FLAT":
        violations.append(f"mode={mode}, expected FLAT")
    if open_lot_count != 0:
        violations.append(f"open_lots={open_lot_count}, expected 0")
    if active_order_count != 0:
        violations.append(f"active_orders={active_order_count}, expected 0")

    if violations:
        raise AssertionError(f"INV-7 cleanup_converged: {'; '.join(violations)}")


# ---------------------------------------------------------------------------
# INV-8: No place->cancel churn without state change
# ---------------------------------------------------------------------------


def assert_no_deterministic_churn(
    placed_cids: set[str],
    canceled_cids: set[str],
) -> None:
    """If an order is placed and then canceled in the same or next cycle
    with no meaningful state change, that is churn.

    Args:
        placed_cids: CIDs from PLACE actions in the current cycle
        canceled_cids: CIDs from CANCEL actions in the same/next cycle
    """
    churned = placed_cids & canceled_cids
    if churned:
        raise AssertionError(
            f"INV-8 no_deterministic_churn: {len(churned)} orders placed then canceled: "
            f"{sorted(churned)}"
        )
