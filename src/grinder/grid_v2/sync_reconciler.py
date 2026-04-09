"""Sync-driven grid_v2 reconciler: compute repair actions from fresh account snapshot.

Designed to replace tick-level watchdog as primary repair path.
Input: fresh AccountSnapshot + SM/bridge desired state.
Output: deterministic action list (CANCEL extras first, then PLACE missing).
No side effects — caller decides whether to dispatch or shadow-log.

Three-layer model (ADR-103):
  1. Theoretical desired state: what SM wants absent hard constraints.
  2. Effective desired state: legal target after risk projection.
  3. Actual exchange state: what exists on exchange.
Reconciler diffs actual against effective, not theoretical.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.grid_v2.geometry import match_entries_with_tolerance
from grinder.grid_v2.state import BranchMode, ExitOrderStatus

if TYPE_CHECKING:
    from decimal import Decimal

    from grinder.account.contracts import AccountSnapshot

logger = logging.getLogger(__name__)


class ProjectionMode(Enum):
    """How desired entries were projected under risk constraints (ADR-103)."""

    UNCONSTRAINED = "UNCONSTRAINED"
    RISK_CONSTRAINED_PARTIAL = "RISK_CONSTRAINED_PARTIAL"
    RISK_CONSTRAINED_ZERO = "RISK_CONSTRAINED_ZERO"


@dataclass(frozen=True)
class ReconcileResult:
    """Output of a single reconciliation pass.

    Fields use the three-layer model:
    - theoretical_desired_entry_count: what SM wants (before risk projection).
    - desired_entry_count: effective desired (after risk projection).
    - actual_entry_count: what is on exchange.
    - projection_mode: how projection was applied.
    - legal_entry_capacity: risk cap passed to reconciler (None=unconstrained).
    """

    theoretical_desired_entry_count: int
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
    projection_mode: ProjectionMode = ProjectionMode.UNCONSTRAINED
    legal_entry_capacity: int | None = None
    # Shadow-only fields for observability
    would_cancel: int = 0
    would_place: int = 0
    # ADR-171: Diagnostic fields for canary analysis
    inventory_headroom: int | None = None  # cap - open_lots (None if FLAT)
    inflight_entry_places: int = 0  # pending entry placements (suppresses false missing)
    inflight_entry_cancels: int = 0  # pending entry cancels (suppresses false extra)
    inflight_exit_places: int = 0  # pending exit placements
    inflight_exit_cancels: int = 0  # pending exit cancels


@dataclass
class ReconcileConfig:
    """Reconciler configuration."""

    enabled: bool = False
    shadow: bool = True  # shadow mode: compute + log, don't dispatch
    max_actions_per_sync: int = 10


def _project_desired_entries(
    theoretical_keys: set[tuple[OrderSide, Decimal]],
    risk_entry_capacity: int | None,
    reference_price: Decimal,
) -> tuple[set[tuple[OrderSide, Decimal]], ProjectionMode]:
    """Project theoretical desired entries to effective desired state.

    Args:
        theoretical_keys: Full desired entry keys from SM (before risk).
        risk_entry_capacity: Legal capacity (None=unconstrained, 0=zero, N=partial).
        reference_price: SM reference price for proximity ranking.

    Returns:
        (effective_keys, projection_mode)
    """
    if risk_entry_capacity is None or risk_entry_capacity >= len(theoretical_keys):
        return theoretical_keys, ProjectionMode.UNCONSTRAINED

    if risk_entry_capacity <= 0:
        return set(), ProjectionMode.RISK_CONSTRAINED_ZERO

    # Partial: keep N entries closest to reference price (deterministic)
    ranked = sorted(
        theoretical_keys,
        key=lambda k: abs(k[1] - reference_price),
    )
    return set(ranked[:risk_entry_capacity]), ProjectionMode.RISK_CONSTRAINED_PARTIAL


def reconcile_grid_state(  # noqa: PLR0912, PLR0915
    snapshot: AccountSnapshot,
    symbol: str,
    bridge: Any,  # GridV2Bridge — use Any to avoid circular import
    max_actions: int = 10,
    *,
    risk_entry_capacity: int | None = None,
    pending_exit_place_cids: frozenset[str] | None = None,
    pending_exit_cancel_cids: frozenset[str] | None = None,
    pending_entry_place_keys: frozenset[tuple[OrderSide, Decimal]] | None = None,
    pending_entry_cancel_keys: frozenset[tuple[OrderSide, Decimal]] | None = None,
) -> ReconcileResult:
    """Compute deterministic repair actions from fresh account snapshot.

    Args:
        snapshot: Fresh account snapshot (just fetched from exchange).
        symbol: Trading symbol.
        bridge: GridV2Bridge instance (for SM state, adapter, quantize).
        max_actions: Max total actions (cancel + place) per sync cycle.
        risk_entry_capacity: Legal additional entry capacity (ADR-102/103).
            None = unconstrained (risk base disabled or data unavailable).
            0 = fully constrained (no new entries allowed).
            N > 0 = partial capacity (truncate desired to N entries).
        pending_exit_place_cids: Exit CIDs dispatched but not yet visible
            on exchange. Treated as effectively present for missing_exits.
        pending_exit_cancel_cids: Exit CIDs with pending cancel dispatched
            but not yet reflected. Treated as effectively absent for extra_exits.
        pending_entry_place_keys: Entry (side, price) keys dispatched but
            not yet visible. Treated as effectively present for missing_entries.
        pending_entry_cancel_keys: Entry (side, price) keys with pending
            cancel. Treated as effectively absent for extra_entries.

    Returns:
        ReconcileResult with deterministic action list and projection metadata.
    """
    t0 = time.monotonic()
    sm = bridge.state_machine
    if sm is None:
        return _empty_result(0)

    # --- Layer 1: Theoretical desired state (what SM wants) ---
    theoretical_entry_keys: set[tuple[OrderSide, Decimal]] = set()
    for p in sm.snapshot.entry_window.buy_entry_prices:
        theoretical_entry_keys.add((OrderSide.BUY, bridge._quantize_price(p, OrderSide.BUY)))
    for p in sm.snapshot.entry_window.sell_entry_prices:
        theoretical_entry_keys.add((OrderSide.SELL, bridge._quantize_price(p, OrderSide.SELL)))

    headroom: int = -1  # sentinel: -1 = FLAT mode (no headroom logic)
    # Inventory headroom: as inventory approaches cap, reduce same-side
    # entries to limit burst overshoot. At cap → zero entries. Near cap →
    # only a few entries remain, so rapid fills can't push far past limit.
    if sm.mode != BranchMode.FLAT:
        lots_open = len(sm.snapshot.open_lots)
        max_inv = bridge._config.max_inventory_levels
        _levels_per_side = getattr(bridge._config, "entry_levels_per_side", 5)
        headroom = max(0, max_inv - lots_open)
        if headroom == 0:
            theoretical_entry_keys = set()
        elif isinstance(_levels_per_side, int) and headroom < _levels_per_side:
            # Reduce same-side entries to headroom count.
            # Keep entries closest to reference price (highest priority).
            branch_side = OrderSide.BUY if sm.mode == BranchMode.LONG_BRANCH else OrderSide.SELL
            ref = sm.snapshot.entry_window.reference_price
            same_side = sorted(
                [(s, p) for s, p in theoretical_entry_keys if s == branch_side],
                key=lambda k: abs(k[1] - ref),
            )
            if len(same_side) > headroom:
                to_remove = set(same_side[headroom:])
                theoretical_entry_keys -= to_remove

    # --- Actual exchange state (computed early — needed for gap detection) ---
    actual_entry_by_key: dict[tuple[OrderSide, Decimal], str] = {}
    actual_exit_cids: set[str] = set()
    actual_exit_prices: dict[str, Decimal] = {}  # CID → exchange price
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
            actual_exit_prices[o.order_id] = o.price

    # --- Gap detection supplement (on theoretical, BEFORE projection) ---
    from grinder.grid_v2.state import _grid_step_price  # noqa: PLC0415

    step = _grid_step_price(
        sm.snapshot.entry_window.reference_price,
        bridge._config.grid_step_pct,
        bridge._config.price_tick_size,
    )
    exit_prices: set[Decimal] = set()
    for eo in sm.snapshot.exit_orders:
        if eo.status == ExitOrderStatus.OPEN:
            exit_prices.add(eo.price)

    actual_entry_keys_pre = set(actual_entry_by_key.keys())
    for side in (OrderSide.BUY, OrderSide.SELL):
        side_prices = sorted(
            [p for s, p in actual_entry_keys_pre if s == side],
            reverse=(side == OrderSide.BUY),
        )
        for i in range(len(side_prices) - 1):
            gap = abs(side_prices[i] - side_prices[i + 1])
            if gap > step + step // 2:  # 1.5 * step
                if side == OrderSide.BUY:
                    fill_price = side_prices[i] - step
                else:
                    fill_price = side_prices[i] + step
                fill_price = bridge._quantize_price(fill_price, side)
                if fill_price not in exit_prices:
                    theoretical_entry_keys.add((side, fill_price))

    # --- Layer 2: Effective desired state (legal target after projection) ---
    # Projection runs AFTER gap detection so the combined theoretical set
    # is projected down to legal capacity. Effective never exceeds capacity.
    effective_entry_keys, projection_mode = _project_desired_entries(
        theoretical_entry_keys,
        risk_entry_capacity,
        sm.snapshot.entry_window.reference_price,
    )

    # --- Exit desired state (CID + expected price + qty per lot) ---
    desired_exit_cids: set[str] = set()
    desired_exit_prices: dict[str, Decimal] = {}  # CID → expected price
    desired_exit_sides: dict[str, OrderSide] = {}  # CID → side
    desired_exit_qtys: dict[str, Decimal] = {}  # CID → qty
    for eo in sm.snapshot.exit_orders:
        if eo.status != ExitOrderStatus.OPEN:
            continue
        reg_cid = bridge.adapter.registry.cid_for_exit(eo.exit_order_id)
        if reg_cid is not None:
            desired_exit_cids.add(reg_cid)
            desired_exit_prices[reg_cid] = eo.price
            desired_exit_sides[reg_cid] = eo.side
            desired_exit_qtys[reg_cid] = eo.qty

    # --- Diff: actual vs effective (NOT vs theoretical) ---
    # Entry diff is geometry-aware for exchange-visible entries and inflight-aware
    # for pending place/cancel state. This avoids classifying a slightly off-grid
    # entry as a naive "extra + missing" pair.
    visible_entry_by_key = dict(actual_entry_by_key)
    if pending_entry_cancel_keys:
        for key in pending_entry_cancel_keys:
            visible_entry_by_key.pop(key, None)

    geometry_entry_mismatches: list[tuple[OrderSide, Decimal, Decimal, str]] = []
    if bridge._config.price_tick_size > 0:
        str_expected = {(side.value, price) for side, price in effective_entry_keys}
        str_actual = {
            (side.value, price): cid for (side, price), cid in visible_entry_by_key.items()
        }
        _matched, truly_missing_str, truly_extra_str, geometry_mismatch_str = (
            match_entries_with_tolerance(
                str_expected,
                str_actual,
                bridge._config.price_tick_size,
            )
        )
        missing_entries = {(OrderSide(side), price) for side, price in truly_missing_str}
        extra_entries = {(OrderSide(side), price) for side, price in truly_extra_str}
        geometry_entry_mismatches = [
            (OrderSide(side), expected_price, actual_price, cid)
            for side, expected_price, actual_price, cid in geometry_mismatch_str
        ]
    else:
        effective_actual_entries = set(visible_entry_by_key.keys())
        missing_entries = effective_entry_keys - effective_actual_entries
        extra_entries = effective_actual_entries - effective_entry_keys

    if pending_entry_place_keys:
        missing_entries -= pending_entry_place_keys
        geometry_entry_mismatches = [
            mismatch
            for mismatch in geometry_entry_mismatches
            if (mismatch[0], mismatch[1]) not in pending_entry_place_keys
        ]
    if pending_entry_cancel_keys:
        extra_entries -= pending_entry_cancel_keys
        geometry_entry_mismatches = [
            mismatch
            for mismatch in geometry_entry_mismatches
            if (mismatch[0], mismatch[2]) not in pending_entry_cancel_keys
        ]

    # Inflight-aware exit diff: pending placements count as effectively
    # present, pending cancels count as effectively absent.
    effective_actual_exits = set(actual_exit_cids)
    if pending_exit_place_cids:
        effective_actual_exits |= pending_exit_place_cids
    if pending_exit_cancel_cids:
        effective_actual_exits -= pending_exit_cancel_cids
    missing_exits = desired_exit_cids - effective_actual_exits
    extra_exits = effective_actual_exits - desired_exit_cids

    # Price-aware exit geometry: for CID-matched exits, check if exchange
    # price matches expected price within tolerance. Mispriced exits get
    # cancel+replace correction.
    tick = bridge._config.price_tick_size
    geometry_exit_mismatches: list[tuple[OrderSide, Decimal, Decimal, str]] = []
    if tick > 0:
        matched_cids = desired_exit_cids & effective_actual_exits
        for cid in sorted(matched_cids):
            expected = desired_exit_prices.get(cid)
            actual = actual_exit_prices.get(cid)
            if expected is None or actual is None:
                continue  # inflight-only or no price data
            if abs(expected - actual) > tick:
                side = desired_exit_sides.get(cid, OrderSide.SELL)
                geometry_exit_mismatches.append((side, expected, actual, cid))
    # Suppress exit repricing if correction already inflight
    if pending_exit_place_cids:
        geometry_exit_mismatches = [
            m for m in geometry_exit_mismatches if m[3] not in pending_exit_place_cids
        ]
    if pending_exit_cancel_cids:
        geometry_exit_mismatches = [
            m for m in geometry_exit_mismatches if m[3] not in pending_exit_cancel_cids
        ]

    # --- Build actions: CANCEL first, then PLACE (deterministic order) ---
    actions: list[ExecutionAction] = []
    budget = max_actions

    for side, price in sorted(extra_entries, key=lambda x: (x[0].value, x[1])):
        if len(actions) >= budget:
            break
        cid = actual_entry_by_key.get((side, price))
        if cid is None:
            continue  # inflight-only entry, not on exchange — skip cancel
        actions.append(
            ExecutionAction(
                action_type=ActionType.CANCEL,
                order_id=cid,
                symbol=symbol,
                reason="grid_v2_RECONCILE_CANCEL_ENTRY",
            )
        )

    for _side, _expected_price, _actual_price, cid in sorted(
        geometry_entry_mismatches,
        key=lambda x: (x[0].value, x[1], x[2], x[3]),
    ):
        if len(actions) >= budget:
            break
        actions.append(
            ExecutionAction(
                action_type=ActionType.CANCEL,
                order_id=cid,
                symbol=symbol,
                reason="grid_v2_RECONCILE_REPRICE_ENTRY_CANCEL",
            )
        )

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

    # Exit geometry: atomic cancel+place for mispriced exits (safety-first,
    # must not split across budget boundaries — exit correction before entry work)
    for side, expected_price, _actual_price, cid in sorted(
        geometry_exit_mismatches,
        key=lambda x: (x[0].value, x[1], x[2], x[3]),
    ):
        if len(actions) + 2 > budget:
            break  # need room for both cancel AND place
        qty = desired_exit_qtys.get(cid, bridge._config.order_size)
        actions.append(
            ExecutionAction(
                action_type=ActionType.CANCEL,
                order_id=cid,
                symbol=symbol,
                reason="grid_v2_RECONCILE_REPRICE_EXIT_CANCEL",
            )
        )
        actions.append(
            ExecutionAction(
                action_type=ActionType.PLACE,
                symbol=symbol,
                side=side,
                price=expected_price,
                quantity=qty,
                reduce_only=True,
                reason="grid_v2_RECONCILE_REPRICE_EXIT_PLACE",
            )
        )

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

    for side, expected_price, _actual_price, _cid in sorted(
        geometry_entry_mismatches,
        key=lambda x: (x[0].value, x[1], x[2], x[3]),
    ):
        if len(actions) >= budget:
            break
        existing_cid = bridge.adapter.registry.cid_for_entry(side, expected_price)
        if existing_cid is not None:
            continue
        actions.append(
            ExecutionAction(
                action_type=ActionType.PLACE,
                symbol=symbol,
                side=side,
                price=expected_price,
                quantity=bridge._config.order_size,
                reason="grid_v2_RECONCILE_REPRICE_ENTRY_PLACE",
            )
        )

    cancel_count = sum(1 for a in actions if a.action_type == ActionType.CANCEL)
    place_count = sum(1 for a in actions if a.action_type == ActionType.PLACE)

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    return ReconcileResult(
        theoretical_desired_entry_count=len(theoretical_entry_keys),
        desired_entry_count=len(effective_entry_keys),
        actual_entry_count=len(actual_entry_by_key),
        desired_exit_count=len(desired_exit_cids),
        actual_exit_count=len(actual_exit_cids),
        missing_entries=len(missing_entries),
        extra_entries=len(extra_entries),
        missing_exits=len(missing_exits),
        extra_exits=len(extra_exits),
        actions=tuple(actions),
        cycle_ms=elapsed_ms,
        projection_mode=projection_mode,
        legal_entry_capacity=risk_entry_capacity,
        would_cancel=cancel_count,
        would_place=place_count,
        inventory_headroom=headroom if sm.mode != BranchMode.FLAT else None,
        inflight_entry_places=len(pending_entry_place_keys) if pending_entry_place_keys else 0,
        inflight_entry_cancels=len(pending_entry_cancel_keys) if pending_entry_cancel_keys else 0,
        inflight_exit_places=len(pending_exit_place_cids) if pending_exit_place_cids else 0,
        inflight_exit_cancels=len(pending_exit_cancel_cids) if pending_exit_cancel_cids else 0,
    )


def _empty_result(elapsed_ms: int) -> ReconcileResult:
    return ReconcileResult(
        theoretical_desired_entry_count=0,
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
