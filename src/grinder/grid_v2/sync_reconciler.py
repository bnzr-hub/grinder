"""Sync-driven grid_v2 reconciler: compute repair actions from fresh account snapshot.

Designed to replace tick-level watchdog as primary repair path.
Input: fresh AccountSnapshot + SM/bridge desired state.
Output: deterministic action list (CANCEL extras first, then PLACE missing).
No side effects — caller decides whether to dispatch or shadow-log.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.grid_v2.state import BranchMode, ExitOrderStatus

if TYPE_CHECKING:
    from decimal import Decimal

    from grinder.account.contracts import AccountSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileResult:
    """Output of a single reconciliation pass."""

    desired_entry_count: int
    actual_entry_count: int
    desired_exit_count: int
    actual_exit_count: int
    missing_entries: int
    extra_entries: int
    missing_exits: int
    extra_exits: int
    actions: tuple[ExecutionAction, ...]
    cycle_ms: int
    # Shadow-only fields for observability
    would_cancel: int = 0
    would_place: int = 0


@dataclass
class ReconcileConfig:
    """Reconciler configuration."""

    enabled: bool = False
    shadow: bool = True  # shadow mode: compute + log, don't dispatch
    max_actions_per_sync: int = 10


def reconcile_grid_state(  # noqa: PLR0912, PLR0915
    snapshot: AccountSnapshot,
    symbol: str,
    bridge: Any,  # GridV2Bridge — use Any to avoid circular import
    max_actions: int = 10,
) -> ReconcileResult:
    """Compute deterministic repair actions from fresh account snapshot.

    Args:
        snapshot: Fresh account snapshot (just fetched from exchange).
        symbol: Trading symbol.
        bridge: GridV2Bridge instance (for SM state, adapter, quantize).
        max_actions: Max total actions (cancel + place) per sync cycle.

    Returns:
        ReconcileResult with deterministic action list.
    """
    t0 = time.monotonic()
    sm = bridge.state_machine
    if sm is None:
        return _empty_result(0)

    # --- Compute desired state from ideal geometry (not SM window) ---
    # SM window may have gaps from collision guard skips. Reconciler uses
    # pure geometry: uniform steps from reference price.
    from grinder.grid_v2.state import _grid_step_price  # noqa: PLC0415

    ref_price = sm.snapshot.entry_window.reference_price
    step = _grid_step_price(
        ref_price,
        bridge._config.grid_step_pct,
        bridge._config.price_tick_size,
    )
    levels = bridge._config.entry_levels_per_side

    # Collect exit prices to avoid placing entries on occupied exit slots
    exit_prices: set[Decimal] = set()
    for eo in sm.snapshot.exit_orders:
        if eo.status == ExitOrderStatus.OPEN:
            exit_prices.add(eo.price)

    desired_entry_keys: set[tuple[OrderSide, Decimal]] = set()
    for i in range(1, levels + 1):
        buy_price = bridge._quantize_price(ref_price - step * i, OrderSide.BUY)
        sell_price = bridge._quantize_price(ref_price + step * i, OrderSide.SELL)
        if buy_price not in exit_prices:
            desired_entry_keys.add((OrderSide.BUY, buy_price))
        if sell_price not in exit_prices:
            desired_entry_keys.add((OrderSide.SELL, sell_price))

    # Inventory cap: when full, don't desire new entries
    inventory_full = (
        sm.mode != BranchMode.FLAT
        and len(sm.snapshot.open_lots) >= bridge._config.max_inventory_levels
    )
    if inventory_full:
        desired_entry_keys = set()

    desired_exit_cids: set[str] = set()
    for eo in sm.snapshot.exit_orders:
        if eo.status != ExitOrderStatus.OPEN:
            continue
        reg_cid = bridge.adapter.registry.cid_for_exit(eo.exit_order_id)
        if reg_cid is not None:
            desired_exit_cids.add(reg_cid)

    # --- Compute actual state from fresh snapshot ---
    actual_entry_by_key: dict[tuple[OrderSide, Decimal], str] = {}
    actual_exit_cids: set[str] = set()
    for o in snapshot.open_orders:
        if o.symbol != symbol:
            continue
        parsed = bridge.adapter.parse_cid(o.order_id)
        if parsed is None:
            continue
        if parsed.kind.value == "ENTRY":
            try:
                side = OrderSide(o.side)
            except ValueError:
                continue
            actual_entry_by_key[(side, o.price)] = o.order_id
        elif parsed.kind.value == "EXIT":
            actual_exit_cids.add(o.order_id)

    # --- Compute diff ---
    actual_entry_keys = set(actual_entry_by_key.keys())
    missing_entries = desired_entry_keys - actual_entry_keys
    extra_entries = actual_entry_keys - desired_entry_keys
    missing_exits = desired_exit_cids - actual_exit_cids
    extra_exits = actual_exit_cids - desired_exit_cids

    # --- Build actions: CANCEL first, then PLACE (deterministic order) ---
    actions: list[ExecutionAction] = []
    budget = max_actions

    # Cancel extra entries (sorted for determinism)
    for side, price in sorted(extra_entries, key=lambda x: (x[0].value, x[1])):
        if len(actions) >= budget:
            break
        cid = actual_entry_by_key[(side, price)]
        actions.append(
            ExecutionAction(
                action_type=ActionType.CANCEL,
                order_id=cid,
                symbol=symbol,
                reason="grid_v2_RECONCILE_CANCEL_ENTRY",
            )
        )

    # Cancel extra exits (sorted for determinism)
    for cid in sorted(extra_exits):
        if len(actions) >= budget:
            break
        actions.append(
            ExecutionAction(
                action_type=ActionType.CANCEL,
                order_id=cid,
                symbol=symbol,
                reason="grid_v2_RECONCILE_CANCEL_EXIT",
            )
        )

    # Place missing entries (sorted for determinism)
    # Note: these are raw intents — caller must pass through risk gates.
    # Skip if adapter registry already has a CID for this slot (avoids duplicate entry fatal).
    for side, price in sorted(missing_entries, key=lambda x: (x[0].value, x[1])):
        if len(actions) >= budget:
            break
        existing_cid = bridge.adapter.registry.cid_for_entry(side, price)
        if existing_cid is not None:
            continue
        actions.append(
            ExecutionAction(
                action_type=ActionType.PLACE,
                symbol=symbol,
                side=side,
                price=price,
                quantity=bridge._config.order_size,
                reason="grid_v2_RECONCILE_PLACE_ENTRY",
            )
        )

    cancel_count = sum(1 for a in actions if a.action_type == ActionType.CANCEL)
    place_count = sum(1 for a in actions if a.action_type == ActionType.PLACE)

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    return ReconcileResult(
        desired_entry_count=len(desired_entry_keys),
        actual_entry_count=len(actual_entry_by_key),
        desired_exit_count=len(desired_exit_cids),
        actual_exit_count=len(actual_exit_cids),
        missing_entries=len(missing_entries),
        extra_entries=len(extra_entries),
        missing_exits=len(missing_exits),
        extra_exits=len(extra_exits),
        actions=tuple(actions),
        cycle_ms=elapsed_ms,
        would_cancel=cancel_count,
        would_place=place_count,
    )


def _empty_result(elapsed_ms: int) -> ReconcileResult:
    return ReconcileResult(
        desired_entry_count=0,
        actual_entry_count=0,
        desired_exit_count=0,
        actual_exit_count=0,
        missing_entries=0,
        extra_entries=0,
        missing_exits=0,
        extra_exits=0,
        actions=(),
        cycle_ms=elapsed_ms,
    )
