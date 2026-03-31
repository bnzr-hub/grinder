"""Execution engine for grid order management.

The ExecutionEngine:
- Takes GridPlan as input
- Computes required order levels
- Reconciles with current open orders
- Generates execution actions (place/cancel)
- Maintains deterministic state for replay
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from grinder.core import GridMode, OrderSide, OrderState, ResetAction
from grinder.execution.types import (
    ActionType,
    ExecutionAction,
    ExecutionEvent,
    ExecutionState,
    OrderRecord,
)

if TYPE_CHECKING:
    from grinder.execution.constraint_provider import ConstraintProvider
    from grinder.execution.port import ExchangePort
    from grinder.features.l2_types import L2FeatureSnapshot
    from grinder.policies.base import GridPlan


@dataclass(frozen=True)
class ExecutionEngineConfig:
    """Configuration for ExecutionEngine (M7-07, ADR-061; M7-09, ADR-062).

    Attributes:
        constraints_enabled: Whether to apply symbol qty constraints.
            When False (default), constraints are ignored even if provided.
            When True, step_size rounding and min_qty validation are applied.

        l2_execution_guard_enabled: Whether to apply L2-based guards on PLACE actions.
            When False (default), L2 features are ignored.
            When True, checks for stale data, insufficient depth, and high impact.

        l2_execution_max_age_ms: Maximum age in ms for L2 snapshot before considered stale.
            Only used when l2_execution_guard_enabled=True.

        l2_execution_impact_threshold_bps: Impact threshold in bps to skip order.
            Orders with impact >= threshold will be skipped.
            Only used when l2_execution_guard_enabled=True.
    """

    constraints_enabled: bool = False
    l2_execution_guard_enabled: bool = False
    l2_execution_max_age_ms: int = 1500
    l2_execution_impact_threshold_bps: int = 50


@dataclass(frozen=True)
class SymbolConstraints:
    """Exchange symbol constraints for order validation (M7-05, ADR-059).

    Attributes:
        step_size: Lot size step for qty rounding (e.g., 0.001 for BTC)
        min_qty: Minimum order quantity (e.g., 0.001 for BTC)
        tick_size: Price filter tick size (e.g., 0.10 for BTCUSDT futures)
        min_notional: Minimum order value in quote asset (e.g., 5 USDT)
    """

    step_size: Decimal
    min_qty: Decimal
    tick_size: Decimal = Decimal("0")
    min_notional: Decimal = Decimal("0")


def floor_to_step(qty: Decimal, step_size: Decimal) -> Decimal:
    """Floor quantity to nearest step size (M7-05, ADR-059).

    Deterministic floor rounding - never exceeds input qty.
    Uses Decimal arithmetic only, no floats.

    Args:
        qty: Raw quantity to round
        step_size: Exchange lot size step

    Returns:
        Floored quantity as multiple of step_size
    """
    if step_size <= 0:
        return qty
    # Compute number of whole steps, then multiply back
    steps = (qty / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return steps * step_size


@dataclass
class GridLevel:
    """Computed grid level for order placement."""

    level_id: int
    side: OrderSide
    price: Decimal
    quantity: Decimal

    def key(self) -> str:
        """Unique key for matching."""
        return f"{self.side.value}:{self.level_id}"


@dataclass
class ExecutionResult:
    """Result of execution engine evaluation."""

    actions: list[ExecutionAction]
    events: list[ExecutionEvent]
    state: ExecutionState
    plan_digest: str


class ExecutionEngine:
    """Grid order execution engine.

    Responsibilities:
    - Compute grid levels from GridPlan
    - Reconcile current orders with desired grid
    - Generate execution actions
    - Emit events for logging/metrics
    - Maintain deterministic state
    """

    def __init__(
        self,
        port: ExchangePort,
        price_precision: int = 2,
        quantity_precision: int = 3,
        symbol_constraints: dict[str, SymbolConstraints] | None = None,
        config: ExecutionEngineConfig | None = None,
        constraint_provider: ConstraintProvider | None = None,
        l2_features: dict[str, L2FeatureSnapshot] | None = None,
    ) -> None:
        """Initialize execution engine.

        Args:
            port: Exchange port for order operations
            price_precision: Decimal places for price rounding
            quantity_precision: Decimal places for quantity rounding
            symbol_constraints: Per-symbol step_size/min_qty constraints (M7-05).
                Takes precedence over constraint_provider if both provided.
            config: Engine configuration (M7-07). Defaults to constraints_enabled=False.
            constraint_provider: Provider for lazy-loading constraints (M7-07).
                Only used if symbol_constraints is not provided.
            l2_features: Per-symbol L2 feature snapshots for execution guards (M7-09).
                Only used when config.l2_execution_guard_enabled=True.
        """
        self._port = port
        self._price_precision = price_precision
        self._quantity_precision = quantity_precision
        self._config = config or ExecutionEngineConfig()
        self._constraint_provider = constraint_provider
        self._symbol_constraints: dict[str, SymbolConstraints] | None = symbol_constraints
        self._constraints_loaded = symbol_constraints is not None
        self._l2_features: dict[str, L2FeatureSnapshot] | None = l2_features

    def _get_symbol_constraints(self) -> dict[str, SymbolConstraints]:
        """Get symbol constraints, loading from provider if needed (M7-07).

        Lazy loading: constraints are loaded on first access if a provider
        is configured and symbol_constraints wasn't passed directly.

        Returns:
            Dict mapping symbol -> SymbolConstraints (may be empty)
        """
        if self._constraints_loaded:
            return self._symbol_constraints or {}

        # Lazy load from provider
        if self._constraint_provider is not None:
            self._symbol_constraints = self._constraint_provider.get_constraints()
        else:
            self._symbol_constraints = {}
        self._constraints_loaded = True
        return self._symbol_constraints

    def _round_price(self, price: Decimal, symbol: str = "") -> Decimal:
        """Round price to tick_size if available, else to configured precision."""
        if symbol and self._config.constraints_enabled:
            sc = self._get_symbol_constraints().get(symbol)
            if sc is not None and sc.tick_size > 0:
                return floor_to_step(price, sc.tick_size)
        quantize_str = "0." + "0" * self._price_precision
        return price.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)

    def _round_quantity(self, qty: Decimal) -> Decimal:
        """Round quantity to configured precision."""
        quantize_str = "0." + "0" * self._quantity_precision
        return qty.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)

    def _apply_qty_constraints(self, qty: Decimal, symbol: str) -> tuple[Decimal, str | None]:
        """Apply symbol-specific qty constraints (M7-05, ADR-059).

        Single point of application for:
        1. Floor to step_size (lot size rounding)
        2. Validate against min_qty

        Constraints are only applied if config.constraints_enabled=True (M7-07).

        Args:
            qty: Raw quantity from policy
            symbol: Trading symbol for constraint lookup

        Returns:
            (rounded_qty, reason_code) - reason_code is None if valid,
            or "EXEC_QTY_BELOW_MIN_QTY" if qty rounds to below minimum.
            If constraints disabled or not configured, returns (qty, None) unchanged.
        """
        # M7-07: Check config flag first
        if not self._config.constraints_enabled:
            return qty, None

        symbol_constraints = self._get_symbol_constraints()
        constraints = symbol_constraints.get(symbol)
        if constraints is None:
            return qty, None

        # Step 1: Floor to step_size
        rounded_qty = floor_to_step(qty, constraints.step_size)

        # Step 2: Validate min_qty
        if rounded_qty < constraints.min_qty:
            return rounded_qty, "EXEC_QTY_BELOW_MIN_QTY"

        return rounded_qty, None

    def _apply_l2_guard(
        self,
        symbol: str,
        side: OrderSide,
        ts: int,
    ) -> tuple[bool, str | None]:
        """Apply L2-based execution guard for PLACE actions (M7-09, ADR-062).

        Single point of application for L2 guards:
        1. Check for stale L2 data (ts - snapshot.ts_ms > max_age_ms)
        2. Check for insufficient depth on the relevant side
        3. Check for impact above threshold on the relevant side

        Guards are only applied if config.l2_execution_guard_enabled=True.

        Args:
            symbol: Trading symbol
            side: Order side (BUY/SELL) to check appropriate L2 metrics
            ts: Current timestamp in ms

        Returns:
            (should_skip, reason_code) - should_skip=True means skip the order,
            reason_code is one of:
                - "EXEC_L2_STALE": L2 data too old
                - "EXEC_L2_INSUFFICIENT_DEPTH_BUY/SELL": Not enough depth
                - "EXEC_L2_IMPACT_BUY_HIGH/SELL_HIGH": Impact above threshold
            Returns (False, None) if guard passes or is disabled.
        """
        # M7-09: Pass-through checks (config disabled, no features, no symbol)
        if not self._config.l2_execution_guard_enabled or self._l2_features is None:
            return False, None

        snapshot = self._l2_features.get(symbol)
        if snapshot is None:
            return False, None

        # Guard 1: Check staleness
        age_ms = ts - snapshot.ts_ms
        if age_ms > self._config.l2_execution_max_age_ms:
            return True, "EXEC_L2_STALE"

        # Guard 2 & 3: Check depth and impact for the order side
        threshold = self._config.l2_execution_impact_threshold_bps
        return self._check_l2_side_guards(snapshot, side, threshold)

    def _check_l2_side_guards(
        self,
        snapshot: L2FeatureSnapshot,
        side: OrderSide,
        threshold: int,
    ) -> tuple[bool, str | None]:
        """Check L2 depth and impact guards for a specific side."""
        if side == OrderSide.BUY:
            if snapshot.impact_buy_topN_insufficient_depth == 1:
                return True, "EXEC_L2_INSUFFICIENT_DEPTH_BUY"
            if snapshot.impact_buy_topN_bps >= threshold:
                return True, "EXEC_L2_IMPACT_BUY_HIGH"
        else:
            if snapshot.impact_sell_topN_insufficient_depth == 1:
                return True, "EXEC_L2_INSUFFICIENT_DEPTH_SELL"
            if snapshot.impact_sell_topN_bps >= threshold:
                return True, "EXEC_L2_IMPACT_SELL_HIGH"
        return False, None

    def _compute_plan_digest(self, plan: GridPlan) -> str:
        """Compute deterministic digest of a plan."""
        plan_dict = {
            "mode": plan.mode.value,
            "center_price": str(plan.center_price),
            "spacing_bps": plan.spacing_bps,
            "levels_up": plan.levels_up,
            "levels_down": plan.levels_down,
            "size_schedule": [str(s) for s in plan.size_schedule],
            "skew_bps": plan.skew_bps,
            "reset_action": plan.reset_action.value,
        }
        content = json.dumps(plan_dict, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _compute_grid_levels(self, plan: GridPlan, symbol: str) -> list[GridLevel]:
        """Compute grid levels from plan.

        Grid level computation:
        - center_price is the reference point
        - levels_up: SELL orders above center
        - levels_down: BUY orders below center
        - spacing_bps determines distance between levels
        - size_schedule provides quantity per level
        """
        if plan.mode in (GridMode.PAUSE, GridMode.EMERGENCY):
            return []

        levels: list[GridLevel] = []
        center = plan.center_price

        # Apply skew to center
        skew_factor = Decimal(str(1 + plan.skew_bps / 10000))
        skewed_center = center * skew_factor

        # Spacing factor
        spacing_factor = Decimal(str(1 + plan.spacing_bps / 10000))
        spacing_factor_down = Decimal(str(1 - plan.spacing_bps / 10000))

        # Generate SELL levels (above center) if not UNI_LONG mode
        # Price formula: center * (1 + spacing_bps/10000)^i
        if plan.mode not in (GridMode.UNI_LONG,):
            for i in range(1, plan.levels_up + 1):
                price = skewed_center
                for _ in range(i):
                    price = price * spacing_factor

                price = self._round_price(price, symbol)
                size_idx = min(i - 1, len(plan.size_schedule) - 1)
                quantity = self._round_quantity(plan.size_schedule[size_idx])

                levels.append(
                    GridLevel(
                        level_id=i,
                        side=OrderSide.SELL,
                        price=price,
                        quantity=quantity,
                    )
                )

        # Generate BUY levels (below center) if not UNI_SHORT mode
        # Price formula: center * (1 - spacing_bps/10000)^i
        if plan.mode not in (GridMode.UNI_SHORT,):
            for i in range(1, plan.levels_down + 1):
                price = skewed_center
                for _ in range(i):
                    price = price * spacing_factor_down

                price = self._round_price(price, symbol)
                size_idx = min(i - 1, len(plan.size_schedule) - 1)
                quantity = self._round_quantity(plan.size_schedule[size_idx])

                levels.append(
                    GridLevel(
                        level_id=i,
                        side=OrderSide.BUY,
                        price=price,
                        quantity=quantity,
                    )
                )

        return levels

    def _find_matching_order(
        self,
        level: GridLevel,
        open_orders: list[OrderRecord],
    ) -> OrderRecord | None:
        """Find an open order that matches the grid level."""
        for order in open_orders:
            if order.side == level.side and order.level_id == level.level_id:
                return order
        return None

    def _orders_match(self, level: GridLevel, order: OrderRecord) -> bool:
        """Check if order matches level exactly."""
        return order.price == level.price and order.quantity == level.quantity

    def evaluate(
        self,
        plan: GridPlan,
        symbol: str,
        state: ExecutionState,
        ts: int,
    ) -> ExecutionResult:
        """Evaluate plan and generate execution actions.

        Args:
            plan: GridPlan from policy
            symbol: Trading symbol
            state: Current execution state
            ts: Current timestamp

        Returns:
            ExecutionResult with actions, events, and updated state
        """
        actions: list[ExecutionAction] = []
        events: list[ExecutionEvent] = []

        # Compute plan digest
        plan_digest = self._compute_plan_digest(plan)

        # Get current open orders for symbol
        current_orders = [
            order
            for order in state.open_orders.values()
            if order.symbol == symbol
            and order.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED)
        ]

        # Handle PAUSE/EMERGENCY mode - cancel all
        if plan.mode in (GridMode.PAUSE, GridMode.EMERGENCY):
            for order in current_orders:
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.CANCEL,
                        order_id=order.order_id,
                        symbol=symbol,
                        reason=f"MODE_{plan.mode.value}",
                    )
                )
            events.append(
                ExecutionEvent(
                    ts=ts,
                    event_type=f"CANCEL_ALL_{plan.mode.value}",
                    symbol=symbol,
                    details={"cancelled_count": len(current_orders)},
                )
            )
            return self._apply_actions(actions, events, state, plan_digest, ts)

        # Handle HARD reset - cancel all, rebuild grid
        if plan.reset_action == ResetAction.HARD:
            # Cancel all existing orders
            for order in current_orders:
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.CANCEL,
                        order_id=order.order_id,
                        symbol=symbol,
                        reason="HARD_RESET",
                    )
                )

            # Compute new grid levels and place orders
            levels = self._compute_grid_levels(plan, symbol)
            for level in levels:
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.PLACE,
                        symbol=symbol,
                        side=level.side,
                        price=level.price,
                        quantity=level.quantity,
                        level_id=level.level_id,
                        reason="HARD_RESET",
                    )
                )

            events.append(
                ExecutionEvent(
                    ts=ts,
                    event_type="HARD_RESET",
                    symbol=symbol,
                    details={
                        "cancelled_count": len(current_orders),
                        "placed_count": len(levels),
                    },
                )
            )
            return self._apply_actions(actions, events, state, plan_digest, ts)

        # Handle SOFT reset or NONE - reconcile
        levels = self._compute_grid_levels(plan, symbol)

        # Build sets for reconciliation
        level_keys = {level.key() for level in levels}
        order_keys = {f"{o.side.value}:{o.level_id}" for o in current_orders}

        # Cancel orders that don't match any level
        for order in current_orders:
            order_key = f"{order.side.value}:{order.level_id}"
            if order_key not in level_keys:
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.CANCEL,
                        order_id=order.order_id,
                        symbol=symbol,
                        reason="RECONCILE_REMOVE",
                    )
                )
            else:
                # Check if order needs update (SOFT reset or price/qty mismatch)
                matching_level = next((lv for lv in levels if lv.key() == order_key), None)
                if (
                    matching_level
                    and not self._orders_match(matching_level, order)
                    and plan.reset_action == ResetAction.SOFT
                ):
                    # Cancel and replace
                    actions.append(
                        ExecutionAction(
                            action_type=ActionType.CANCEL,
                            order_id=order.order_id,
                            symbol=symbol,
                            reason="SOFT_RESET_REPLACE",
                        )
                    )
                    actions.append(
                        ExecutionAction(
                            action_type=ActionType.PLACE,
                            symbol=symbol,
                            side=matching_level.side,
                            price=matching_level.price,
                            quantity=matching_level.quantity,
                            level_id=matching_level.level_id,
                            reason="SOFT_RESET_REPLACE",
                        )
                    )

        # Place orders for missing levels
        for level in levels:
            if level.key() not in order_keys:
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.PLACE,
                        symbol=symbol,
                        side=level.side,
                        price=level.price,
                        quantity=level.quantity,
                        level_id=level.level_id,
                        reason="RECONCILE_ADD",
                    )
                )

        # Record reconcile event
        placed_count = sum(1 for a in actions if a.action_type == ActionType.PLACE)
        cancelled_count = sum(1 for a in actions if a.action_type == ActionType.CANCEL)
        events.append(
            ExecutionEvent(
                ts=ts,
                event_type="RECONCILE",
                symbol=symbol,
                details={
                    "placed_count": placed_count,
                    "cancelled_count": cancelled_count,
                    "reset_action": plan.reset_action.value,
                },
            )
        )

        return self._apply_actions(actions, events, state, plan_digest, ts)

    def _apply_actions(
        self,
        actions: list[ExecutionAction],
        events: list[ExecutionEvent],
        state: ExecutionState,
        plan_digest: str,
        ts: int,
    ) -> ExecutionResult:
        """Apply actions to state and port."""
        new_orders = dict(state.open_orders)
        tick = state.tick_counter + 1

        for action in actions:
            if action.action_type == ActionType.CANCEL and action.order_id:
                # Update order state to cancelled
                if action.order_id in new_orders:
                    old_order = new_orders[action.order_id]
                    new_orders[action.order_id] = OrderRecord(
                        order_id=old_order.order_id,
                        symbol=old_order.symbol,
                        side=old_order.side,
                        price=old_order.price,
                        quantity=old_order.quantity,
                        state=OrderState.CANCELLED,
                        level_id=old_order.level_id,
                        created_ts=old_order.created_ts,
                    )
                self._port.cancel_order(action.order_id)

            elif action.action_type == ActionType.PLACE:
                if action.side and action.price and action.quantity:
                    # M7-09: Apply L2 guard first (before qty constraints)
                    l2_skip, l2_reason = self._apply_l2_guard(action.symbol, action.side, ts)
                    if l2_skip and l2_reason:
                        # Skip order - L2 guard triggered
                        events.append(
                            ExecutionEvent(
                                ts=ts,
                                event_type="ORDER_SKIPPED",
                                symbol=action.symbol,
                                details={
                                    "reason": l2_reason,
                                    "level_id": action.level_id,
                                    "side": action.side.value,
                                    "price": str(action.price),
                                    "quantity": str(action.quantity),
                                },
                            )
                        )
                        continue

                    # M7-05: Apply symbol constraints (step_size, min_qty)
                    final_qty, constraint_reason = self._apply_qty_constraints(
                        action.quantity, action.symbol
                    )
                    if constraint_reason == "EXEC_QTY_BELOW_MIN_QTY":
                        # Skip order - qty below minimum after rounding
                        events.append(
                            ExecutionEvent(
                                ts=ts,
                                event_type="ORDER_SKIPPED",
                                symbol=action.symbol,
                                details={
                                    "reason": constraint_reason,
                                    "level_id": action.level_id,
                                    "side": action.side.value,
                                    "original_qty": str(action.quantity),
                                    "rounded_qty": str(final_qty),
                                },
                            )
                        )
                        continue

                    order_id = self._port.place_order(
                        symbol=action.symbol,
                        side=action.side,
                        price=action.price,
                        quantity=final_qty,
                        level_id=action.level_id,
                        ts=ts,
                    )
                    new_orders[order_id] = OrderRecord(
                        order_id=order_id,
                        symbol=action.symbol,
                        side=action.side,
                        price=action.price,
                        quantity=final_qty,
                        state=OrderState.OPEN,
                        level_id=action.level_id,
                        created_ts=ts,
                        placed_tick=tick,  # LC-03: Track tick for fill delay
                    )

        new_state = ExecutionState(
            open_orders=new_orders,
            last_plan_digest=plan_digest,
            tick_counter=tick,
        )

        return ExecutionResult(
            actions=actions,
            events=events,
            state=new_state,
            plan_digest=plan_digest,
        )
