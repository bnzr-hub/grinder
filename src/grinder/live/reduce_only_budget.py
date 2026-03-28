"""Reduce-Only Budget Guard v2: reservation model and repair semantics (ADR-104).

Three-layer budget accounting for reduce-only exits:
1. position_closeable_qty: current closeable position from exchange truth.
2. open_reduce_only_remaining_qty: sum(qty - filled_qty) for open reduce-only exits.
3. reserved_qty: same-tick dispatches + reconcile staged exits.

Core invariant:
  open_remaining + reserved + new_qty <= position_closeable_qty

Repair trigger:
  If open_remaining > position_closeable_qty, exits are over-budget and
  surplus must be cancelled deterministically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.account.contracts import AccountSnapshot
    from grinder.core import OrderSide

logger = logging.getLogger(__name__)


class BudgetCheckResult(Enum):
    """Result of a reduce-only budget check."""

    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"
    POSITION_UNKNOWN = "POSITION_UNKNOWN"


class RepairReason(Enum):
    """Why reduce-only repair was triggered."""

    SYNC_OVER_BUDGET = "SYNC_OVER_BUDGET"
    EXCHANGE_REJECT_2022 = "EXCHANGE_REJECT_2022"


@dataclass(frozen=True)
class BudgetSnapshot:
    """Point-in-time view of reduce-only budget for a symbol/direction."""

    symbol: str
    side: str  # "BUY" or "SELL"
    position_closeable_qty: Decimal
    open_reduce_only_remaining_qty: Decimal
    reserved_qty: Decimal

    @property
    def available(self) -> Decimal:
        """Remaining budget for new reduce-only exits."""
        return max(
            Decimal(0),
            self.position_closeable_qty - self.open_reduce_only_remaining_qty - self.reserved_qty,
        )

    @property
    def over_budget_qty(self) -> Decimal:
        """How much open reduce-only qty exceeds closeable position."""
        surplus = (
            self.open_reduce_only_remaining_qty + self.reserved_qty - self.position_closeable_qty
        )
        return max(Decimal(0), surplus)

    @property
    def is_over_budget(self) -> bool:
        return self.over_budget_qty > 0


@dataclass(frozen=True)
class RepairAction:
    """A single repair action: cancel a surplus reduce-only exit."""

    order_id: str
    symbol: str
    remaining_qty: Decimal
    reason: str


def _closeable_qty_for_side(
    snapshot: AccountSnapshot,
    symbol: str,
    exit_side: OrderSide,
) -> Decimal:
    """Compute closeable position qty for a specific exit direction.

    SELL exit closes LONG → sum long qty.
    BUY exit closes SHORT → sum short qty.

    Position side semantics:
    - "LONG": qty is long (closeable by SELL).
    - "SHORT": qty is short (closeable by BUY).
    - "BOTH" (one-way mode): use signed_qty to determine direction.
      signed_qty > 0 → long (closeable by SELL).
      signed_qty < 0 → short (closeable by BUY).
      signed_qty == 0 or None → flat (0 closeable).
    """
    from grinder.core import OrderSide as _OS  # noqa: PLC0415

    total = Decimal(0)
    for p in snapshot.positions:
        if p.symbol != symbol:
            continue
        if (p.side == "LONG" and exit_side == _OS.SELL) or (
            p.side == "SHORT" and exit_side == _OS.BUY
        ):
            total += abs(p.qty)
        elif p.side == "BOTH":
            # One-way mode: use signed_qty
            sq = p.signed_qty
            if sq is not None and (
                (sq > 0 and exit_side == _OS.SELL) or (sq < 0 and exit_side == _OS.BUY)
            ):
                total += abs(sq)
    return total


def compute_budget_snapshot(
    snapshot: AccountSnapshot,
    symbol: str,
    side: OrderSide,
    reserved_qty: Decimal = Decimal(0),
) -> BudgetSnapshot:
    """Compute reduce-only budget from exchange truth.

    Args:
        snapshot: Fresh account snapshot.
        symbol: Trading symbol.
        side: Exit side (BUY for closing SHORT, SELL for closing LONG).
        reserved_qty: Same-tick + staged reservations.

    Returns:
        BudgetSnapshot with current budget state.
    """
    # Position closeable qty: direction-aware.
    # SELL exit closes LONG → budget against long qty only.
    # BUY exit closes SHORT → budget against short qty only.
    position_qty = _closeable_qty_for_side(snapshot, symbol, side)

    # Open reduce-only remaining qty: sum(qty - filled_qty) for matching exits
    open_ro_remaining = Decimal(0)
    for o in snapshot.open_orders:
        if o.symbol == symbol and o.side == side.value and o.reduce_only:
            open_ro_remaining += o.qty - o.filled_qty

    return BudgetSnapshot(
        symbol=symbol,
        side=side.value,
        position_closeable_qty=position_qty,
        open_reduce_only_remaining_qty=open_ro_remaining,
        reserved_qty=reserved_qty,
    )


def check_budget(
    budget: BudgetSnapshot,
    new_qty: Decimal,
) -> BudgetCheckResult:
    """Check if a new reduce-only exit fits within budget.

    Args:
        budget: Current budget snapshot (includes reservations).
        new_qty: Quantity of the new exit to check.

    Returns:
        ALLOWED if total stays within position_closeable_qty.
        BLOCKED if it would exceed.
        POSITION_UNKNOWN if position_closeable_qty is 0 (fail-closed).
    """
    if budget.position_closeable_qty <= 0:
        return BudgetCheckResult.POSITION_UNKNOWN

    total = budget.open_reduce_only_remaining_qty + budget.reserved_qty + new_qty
    if total > budget.position_closeable_qty:
        return BudgetCheckResult.BLOCKED
    return BudgetCheckResult.ALLOWED


def detect_surplus_exits(
    snapshot: AccountSnapshot,
    symbol: str,
    side: OrderSide,
) -> list[RepairAction]:
    """Detect reduce-only exits that exceed closeable position.

    Returns deterministic list of surplus exits to cancel, ordered by
    remaining qty ascending (cancel smallest first to minimize disruption).
    """
    budget = compute_budget_snapshot(snapshot, symbol, side)
    if not budget.is_over_budget:
        return []

    # Collect all open reduce-only exits for this symbol/side
    exits: list[tuple[str, Decimal]] = []
    for o in snapshot.open_orders:
        if o.symbol == symbol and o.side == side.value and o.reduce_only:
            remaining = o.qty - o.filled_qty
            if remaining > 0:
                exits.append((o.order_id, remaining))

    if not exits:
        return []

    # Sort by remaining qty ascending (cancel smallest first)
    exits.sort(key=lambda x: (x[1], x[0]))

    # Cancel surplus: remove exits until we're back within budget
    surplus = budget.over_budget_qty
    repair_actions: list[RepairAction] = []
    for order_id, remaining in exits:
        if surplus <= 0:
            break
        repair_actions.append(
            RepairAction(
                order_id=order_id,
                symbol=symbol,
                remaining_qty=remaining,
                reason="REDUCE_ONLY_SURPLUS_CANCEL",
            )
        )
        surplus -= remaining

    return repair_actions
