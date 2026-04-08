"""Exit Topology Repair: deterministic convergence to legal exit set (ADR-105).

Three-layer model for exit topology:
1. Desired legal exits: SM OPEN exit orders constrained by reduce-only budget.
2. Actual exchange exits: open reduce-only orders parsed as EXIT CIDs.
3. Repair diff: extra (cancel), missing (place if legal), deferred (if not yet legal).

Composes with ADR-104 budget guard: repair respects closeable position budget.
Deterministic ordering: cancels sorted by CID, places sorted by CID.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from grinder.core import OrderSide
    from grinder.grid_v2.state import ExitOrder

logger = logging.getLogger(__name__)


class RepairTrigger(Enum):
    """Why exit topology repair was triggered."""

    SYNC_DRIFT = "SYNC_DRIFT"
    BUDGET_OVERRUN = "BUDGET_OVERRUN"
    REJECT_RECOVERY = "REJECT_RECOVERY"
    PARTIAL_FILL_RECOMPUTE = "PARTIAL_FILL_RECOMPUTE"


@dataclass(frozen=True)
class DesiredExit:
    """A single desired exit in the legal topology."""

    exit_order_id: str
    lot_id: str
    side: OrderSide
    price: Decimal
    qty: Decimal
    registry_cid: str | None  # None if not yet registered (needs placement)


@dataclass(frozen=True)
class RepairAction:
    """A single exit topology repair action."""

    action_type: str  # "CANCEL" or "PLACE" or "DEFERRED"
    cid: str | None  # exchange CID (for CANCEL) or None (for PLACE/DEFERRED)
    exit_order_id: str | None  # SM exit_order_id (for PLACE/DEFERRED)
    lot_id: str | None  # lot_id (for PLACE)
    side: OrderSide | None
    price: Decimal | None
    qty: Decimal | None
    reason: str


@dataclass(frozen=True)
class ExitTopologyResult:
    """Output of exit topology repair computation.

    Fields follow the three-layer model:
    - desired_exit_count: legal exits from SM (after budget constraint).
    - actual_exit_count: EXIT CIDs on exchange.
    - extra_count: actual but not desired (cancel).
    - missing_count: desired but not actual (place if legal).
    - deferred_count: desired but cannot place yet (budget/other constraint).
    - actions: deterministic repair actions (CANCEL first, then PLACE).
    - is_converged: True if actual already matches desired (zero actions).
    """

    desired_exit_count: int
    actual_exit_count: int
    extra_count: int
    missing_count: int
    deferred_count: int
    actions: tuple[RepairAction, ...]
    is_converged: bool
    trigger: RepairTrigger = RepairTrigger.SYNC_DRIFT


def compute_desired_exits(
    sm_exit_orders: list[ExitOrder],
    registry_cid_lookup: Callable[[str], str | None],
    closeable_budget: Decimal | None = None,
    actual_exit_cids: frozenset[str] | None = None,
) -> list[DesiredExit]:
    """Compute desired legal exit topology from SM state.

    Args:
        sm_exit_orders: SM snapshot.exit_orders (ExitOrder objects).
        registry_cid_lookup: callable(exit_order_id) -> cid | None.
        closeable_budget: If set, total desired exit qty capped at this.
            Exits are kept in original order (SM determines priority).
        actual_exit_cids: EXIT CIDs currently on exchange. When provided,
            budget allocation prioritizes exits that are already placed
            on exchange over unregistered/missing exits. This prevents
            stale deferred exits from consuming budget that is already
            occupied by live exchange orders.

    Returns:
        List of DesiredExit representing the legal target topology.
    """
    from grinder.grid_v2.state import ExitOrderStatus  # noqa: PLC0415

    # Two-pass budget allocation when actual_exit_cids is provided:
    # Pass 1: exits already on exchange (guaranteed budget)
    # Pass 2: remaining exits (only if budget remains)
    # Without actual_exit_cids, single-pass (backwards compatible).
    candidates: list[tuple[ExitOrder, str | None]] = []
    for eo in sm_exit_orders:
        if eo.status != ExitOrderStatus.OPEN:
            continue
        cid = registry_cid_lookup(eo.exit_order_id)
        candidates.append((eo, cid))

    if actual_exit_cids is not None:
        on_exchange = [
            (eo, cid) for eo, cid in candidates if cid is not None and cid in actual_exit_cids
        ]
        not_on_exchange = [
            (eo, cid) for eo, cid in candidates if cid is None or cid not in actual_exit_cids
        ]
        ordered = on_exchange + not_on_exchange
    else:
        ordered = candidates

    desired: list[DesiredExit] = []
    total_qty = Decimal(0)

    for eo, cid in ordered:
        qty = eo.qty
        if closeable_budget is not None and total_qty + qty > closeable_budget:
            continue
        total_qty += qty
        desired.append(
            DesiredExit(
                exit_order_id=eo.exit_order_id,
                lot_id=eo.lot_id,
                side=eo.side,
                price=eo.price,
                qty=qty,
                registry_cid=cid,
            )
        )
    return desired


def compute_exit_topology_repair(
    desired_exits: list[DesiredExit],
    actual_exit_cids: set[str],
    trigger: RepairTrigger = RepairTrigger.SYNC_DRIFT,
) -> ExitTopologyResult:
    """Compute deterministic repair actions to converge actual → desired.

    Args:
        desired_exits: Legal target exits from compute_desired_exits().
        actual_exit_cids: Set of EXIT CIDs currently on exchange.
        trigger: Why repair was triggered.

    Returns:
        ExitTopologyResult with deterministic action list.

    Ordering:
        1. CANCEL extra exits (sorted by CID for determinism).
        2. PLACE missing exits that have no registry CID (sorted by exit_order_id).
        3. DEFERRED for exits that need placement but can't be placed yet.
    """
    # Desired CIDs (only for exits that are already registered)
    desired_cids: set[str] = set()
    for de in desired_exits:
        if de.registry_cid is not None:
            desired_cids.add(de.registry_cid)

    # Extra: on exchange but not desired
    extra_cids = actual_exit_cids - desired_cids

    # Missing: desired but not on exchange
    missing: list[DesiredExit] = []
    deferred: list[DesiredExit] = []
    for de in desired_exits:
        if de.registry_cid is not None and de.registry_cid in actual_exit_cids:
            continue  # Already present on exchange
        if de.registry_cid is not None and de.registry_cid not in actual_exit_cids:
            # Registered but not on exchange — needs placement
            missing.append(de)
        elif de.registry_cid is None:
            # Not registered — needs registration + placement (deferred to bridge)
            deferred.append(de)

    # Build actions: CANCEL first, then PLACE, then DEFERRED
    actions: list[RepairAction] = []

    for cid in sorted(extra_cids):
        actions.append(
            RepairAction(
                action_type="CANCEL",
                cid=cid,
                exit_order_id=None,
                lot_id=None,
                side=None,
                price=None,
                qty=None,
                reason="GRID_V2_EXIT_TOPOLOGY_REPAIR_CANCEL",
            )
        )

    for de in sorted(missing, key=lambda d: d.exit_order_id):
        actions.append(
            RepairAction(
                action_type="PLACE",
                cid=de.registry_cid,
                exit_order_id=de.exit_order_id,
                lot_id=de.lot_id,
                side=de.side,
                price=de.price,
                qty=de.qty,
                reason="GRID_V2_EXIT_TOPOLOGY_REPAIR_PLACE",
            )
        )

    for de in sorted(deferred, key=lambda d: d.exit_order_id):
        actions.append(
            RepairAction(
                action_type="DEFERRED",
                cid=None,
                exit_order_id=de.exit_order_id,
                lot_id=de.lot_id,
                side=de.side,
                price=de.price,
                qty=de.qty,
                reason="GRID_V2_EXIT_TOPOLOGY_REPAIR_DEFERRED",
            )
        )

    is_converged = len(extra_cids) == 0 and len(missing) == 0 and len(deferred) == 0

    return ExitTopologyResult(
        desired_exit_count=len(desired_exits),
        actual_exit_count=len(actual_exit_cids),
        extra_count=len(extra_cids),
        missing_count=len(missing),
        deferred_count=len(deferred),
        actions=tuple(actions),
        is_converged=is_converged,
        trigger=trigger,
    )
