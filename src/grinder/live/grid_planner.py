"""LiveGridPlannerV1: exchange-truth grid reconciliation engine (doc-25).

Replaces PaperEngine as the source of order decisions in live/mainnet mode.
Uses exchange open orders (via AccountSync) as source of truth, diffs against
a computed desired grid, and emits PLACE / CANCEL actions.

Core invariants (doc-25 SS 25.6):
- I1: Exchange is source of truth for open orders (no ghost state)
- I2: effective_spacing_bps >= step_min_bps always
- I3: No actions if shift < threshold AND no missing/extra
- I5: clientOrderId scheme used for level matching
- I6: Orders not matching our clientOrderId prefix are ignored
- I7: Read-only: planner never calls exchange directly
- I8: Planner is stateless per call (except last_plan_center cache)

See: docs/25_LIVE_GRID_PLANNER_SPEC.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from grinder.controller.regime import Regime
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.policies.grid.adaptive import AdaptiveGridConfig, compute_step_bps
from grinder.reconcile.identity import DEFAULT_PREFIX, is_tp_order, parse_client_order_id

if TYPE_CHECKING:
    from grinder.account.contracts import OpenOrderSnap

logger = logging.getLogger(__name__)


@dataclass
class LiveGridConfig:
    """Configuration for LiveGridPlannerV1 (doc-25 SS 25.3).

    Attributes:
        base_spacing_bps: Static grid spacing (used as-is or as adaptive base).
        levels: Levels per side (total = 2 * levels).
        size_per_level: Order quantity per level (base asset).
        adaptive_enabled: Use NATR-driven spacing.
        step_alpha: NATR multiplier (scaled /100).
        step_min_bps: Spacing floor.
        natr_stale_ms: NATR staleness threshold (2 * bar_interval).
        rebalance_threshold_steps: Min grid shift (in steps) to trigger rebalance.
        price_epsilon_bps: Price match tolerance for diff.
        qty_epsilon_pct: Qty match tolerance (%).
        tick_size: Price rounding increment (from exchange constraints).
            None = no constraints available (fail-safe: zero actions).
        max_level_distance_bps: Cap on max distance from mid for any level (PR-VERIF-KNOBS-1).
            None = no cap (default). If set, levels beyond this distance are skipped.
    """

    base_spacing_bps: float = 10.0
    levels: int = 5
    size_per_level: Decimal = field(default_factory=lambda: Decimal("0.01"))
    adaptive_enabled: bool = False
    step_alpha: int = 30
    step_min_bps: int = 5
    natr_stale_ms: int = 120_000
    rebalance_threshold_steps: float = 1.0
    price_epsilon_bps: float = 0.5
    qty_epsilon_pct: float = 1.0
    tick_size: Decimal | None = None
    max_level_distance_bps: int | None = None


@dataclass(frozen=True)
class GridPlanResult:
    """Output of LiveGridPlannerV1.plan() (doc-25 SS 25.4).

    Attributes:
        actions: PLACE / CANCEL actions.
        desired_count: Number of desired grid levels.
        actual_count: Number of matched exchange orders.
        diff_missing: Desired but not on exchange.
        diff_extra: On exchange but not in desired.
        diff_extra_tp: TP orders in extra (not cancelled, INV-9b).
        diff_mismatch: Matched but price/qty differs.
        effective_spacing_bps: Actual spacing used.
        natr_fallback: True if NATR unavailable, using static.
    """

    actions: list[ExecutionAction] = field(default_factory=list)
    desired_count: int = 0
    actual_count: int = 0
    diff_missing: int = 0
    diff_extra: int = 0
    diff_extra_tp: int = 0
    diff_mismatch: int = 0
    effective_spacing_bps: float = 0.0
    natr_fallback: bool = False


@dataclass
class _DesiredLevel:
    """Internal: one level of the desired grid."""

    key: str  # e.g., "BUY:L1", "SELL:L3"
    side: OrderSide
    level_id: int
    price: Decimal


@dataclass
class _DiffResult:
    """Internal: result of matching exchange orders to desired grid."""

    matched_keys: set[str]
    extra_orders: list[OpenOrderSnap]
    mismatch_orders: list[tuple[OpenOrderSnap, _DesiredLevel]]
    missing_keys: list[str]


@dataclass
class _RollingLadderState:
    """Rolling ladder state per symbol (spec doc-26 SS 4.1).

    Volatile (in-memory only). Created on first planner tick in rolling mode.
    Updated on grid fill events (net_offset only). Destroyed on restart.
    """

    anchor_price: Decimal  # mid_price when grid was first built this session
    step_price: Decimal  # round_to_tick(anchor_price * spacing_bps / 10000)
    net_offset: int = 0  # +1 per grid SELL fill, -1 per grid BUY fill


def _round_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    """Round price DOWN to nearest tick increment."""
    if tick_size <= 0:
        return price
    return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size


class LiveGridPlannerV1:
    """Exchange-truth grid reconciliation planner (doc-25).

    Stateless per call except for hysteresis cache (last_plan_center).
    Never calls exchange directly — actions go through LiveEngine pipeline.
    """

    def __init__(self, config: LiveGridConfig) -> None:
        self._config = config
        # Hysteresis cache: per-symbol last plan center + timestamp
        self._last_plan_center: dict[str, Decimal] = {}
        self._last_plan_ts_ms: dict[str, int] = {}
        # Rolling ladder state: per-symbol (doc-26, PR-ROLLING-GRID-V1A)
        self._rolling_state: dict[str, _RollingLadderState] = {}
        # INV-9: TP slot reservations (planner-local).
        # symbol -> {"BUY": count, "SELL": count, "age": int}.
        # Created on fill tick, cleared when TP visible in open_orders or
        # after MAX_RESERVATION_AGE plan() calls (INV-9b, cross-tick).
        self._tp_slot_reservations: dict[str, dict[str, int]] = {}
        self._max_reservation_age: int = 50  # plan() calls before force-clear

    def init_rolling_state(self, symbol: str, anchor_price: Decimal, spacing_bps: float) -> None:
        """Initialize rolling ladder state for symbol (doc-26 SS 4.1).

        Called on first planner tick in rolling mode (auto-init from plan()),
        or externally for restart re-anchor.
        """
        tick = self._config.tick_size or Decimal("0.01")
        step = _round_to_tick(anchor_price * Decimal(str(spacing_bps)) / Decimal("10000"), tick)
        if step <= 0:
            step = tick  # safety: never zero step
        self._rolling_state[symbol] = _RollingLadderState(
            anchor_price=anchor_price, step_price=step
        )

    def reset_rolling_state(self, symbol: str) -> None:
        """Clear rolling state for symbol. Next plan() re-inits from fresh mid.

        Clears planner-owned state: anchor, step, net_offset, reservations.
        Engine must separately clear engine-owned transient state
        (_prev_rolling_orders, _rolling_pending_cancels for symbol).

        Part of INV-10 ANCHOR_RESET contract (ADR-088).
        """
        self._rolling_state.pop(symbol, None)
        self._tp_slot_reservations.pop(symbol, None)

    def apply_fill_offset(self, symbol: str, side: str) -> None:
        """Update net_offset after grid fill detection (doc-26 SS 6.1/6.2).

        BUY fill: net_offset -= 1.  SELL fill: net_offset += 1.
        No-op if rolling state not initialized for symbol.

        INV-9: also reserves a slot on the opposite side for the TP order
        that cycle layer will generate. Reservation is planner-local.
        INV-9b: reservation persists until TP is visible in open_orders
        (cross-tick protection against WS/REST propagation lag), or until
        max_reservation_age is exceeded (handles failed TP placements).
        """
        st = self._rolling_state.get(symbol)
        if st is None:
            return
        if side.upper() == "BUY":
            st.net_offset -= 1
            tp_side = "SELL"
        else:
            st.net_offset += 1
            tp_side = "BUY"
        # INV-9: reserve slot for TP on opposite side
        res = self._tp_slot_reservations.setdefault(symbol, {"BUY": 0, "SELL": 0, "age": 0})
        res[tp_side] += 1
        res["age"] = 0  # reset age on new fill

    def _clear_reservation_if_ready(self, symbol: str) -> None:
        """INV-9b: clear TP reservation on age timeout only.

        Reservation persists across ticks until age exceeds
        max_reservation_age (TP failed/rejected or REST lag resolved).

        Primary cross-tick protection is TP inclusion in open_orders
        (price matching). Reservation is defense-in-depth for the gap
        between fill detection and TP visibility in REST snapshot.

        Age-only policy avoids false-clear when existing TP from a prior
        fill is visible but new reservation is for a different slot
        (multi-fill case, Contract 2a). See ADR-086.
        """
        res = self._tp_slot_reservations.get(symbol)
        if res is None:
            return

        res["age"] = res.get("age", 0) + 1

        # Force-clear on age timeout (handles failed TP placements)
        if res["age"] > self._max_reservation_age:
            self._tp_slot_reservations.pop(symbol, None)

    def get_rolling_state(self, symbol: str) -> _RollingLadderState | None:
        """Read-only access to current rolling state (for tests/logging)."""
        return self._rolling_state.get(symbol)

    def plan(
        self,
        *,
        symbol: str,
        mid_price: Decimal,
        ts_ms: int,
        open_orders: tuple[OpenOrderSnap, ...],
        natr_bps: int | None = None,
        natr_last_ts: int = 0,
        regime: Regime = Regime.RANGE,
        suppress_increase: bool = False,
        rolling_mode: bool = False,
    ) -> GridPlanResult:
        """Compute grid plan by diffing desired vs exchange orders.

        Args:
            symbol: Trading pair.
            mid_price: Current mid-price.
            ts_ms: Epoch milliseconds.
            open_orders: Exchange open orders (from AccountSync).
            natr_bps: NATR(14) in integer bps (None = unavailable).
            natr_last_ts: Timestamp of last NATR computation.
            regime: Market regime (default RANGE).
            suppress_increase: If True, filter out PLACE/REPLACE actions
                (cancel-only mode for non-ACTIVE states, PR-INV-2).
            rolling_mode: If True, use rolling ladder state instead of
                mid-price anchor (doc-26, PR-ROLLING-GRID-V1A).

        Returns:
            GridPlanResult with actions and diff statistics.
        """
        cfg = self._config

        # Step A: Compute effective spacing (I2: floor holds)
        effective_spacing_bps, natr_fallback = self._compute_spacing(
            natr_bps, natr_last_ts, ts_ms, regime
        )

        # Step B: Build desired grid (fail-safe: no tick_size → zero actions)
        if cfg.tick_size is None or cfg.tick_size <= 0:
            logger.warning("No tick_size for %s, skipping grid plan", symbol)
            return GridPlanResult(
                effective_spacing_bps=effective_spacing_bps,
                natr_fallback=natr_fallback,
            )

        if rolling_mode:
            # Rolling mode (doc-26): auto-init state on first tick
            if symbol not in self._rolling_state:
                self.init_rolling_state(symbol, mid_price, effective_spacing_bps)
                # INV-10 (ADR-088): anchor_price = raw mid_price (not rounded).
                logger.info(
                    "ANCHOR_INIT symbol=%s anchor=%s spacing_bps=%.1f",
                    symbol,
                    mid_price,
                    effective_spacing_bps,
                )
            # INV-9b: clear reservation when TP visible or aged out.
            # Must run BEFORE _build_rolling_grid so that once TP is on exchange,
            # the slot is occupied via price-matching (not reservation skip).
            self._clear_reservation_if_ready(symbol)
            desired = self._build_rolling_grid(symbol)
            diff = self._match_orders_by_price(open_orders, desired, mid_price)
            # No hysteresis in rolling mode — grid doesn't track mid_price
        else:
            desired = self._build_desired_grid(mid_price, effective_spacing_bps, cfg.tick_size)
            diff = self._match_orders(open_orders, desired, mid_price)
            # Step E: Hysteresis (anti-churn, I3) — only in mid-anchored mode
            if self._should_skip_rebalance(symbol, mid_price, effective_spacing_bps, diff):
                return GridPlanResult(
                    desired_count=len(desired),
                    actual_count=len(diff.matched_keys),
                    effective_spacing_bps=effective_spacing_bps,
                    natr_fallback=natr_fallback,
                )

        # Step F: Generate actions
        actions = self._generate_actions(symbol, diff, desired, mid_price)

        # PR-INV-2: cancel-only mode (suppress PLACE/REPLACE in non-ACTIVE states)
        if suppress_increase:
            actions = [a for a in actions if a.action_type == ActionType.CANCEL]

        # Update hysteresis cache (only in mid-anchored mode)
        if not rolling_mode:
            self._last_plan_center[symbol] = mid_price
            self._last_plan_ts_ms[symbol] = ts_ms

        tp_extra = sum(1 for o in diff.extra_orders if is_tp_order(o.order_id))
        return GridPlanResult(
            actions=actions,
            desired_count=len(desired),
            actual_count=len(diff.matched_keys) - len(diff.mismatch_orders),
            diff_missing=len(diff.missing_keys),
            diff_extra=len(diff.extra_orders),
            diff_extra_tp=tp_extra,
            diff_mismatch=len(diff.mismatch_orders),
            effective_spacing_bps=effective_spacing_bps,
            natr_fallback=natr_fallback,
        )

    def _compute_spacing(
        self,
        natr_bps: int | None,
        natr_last_ts: int,
        ts_ms: int,
        regime: Regime,
    ) -> tuple[float, bool]:
        """Step A: Compute effective spacing with NATR fallback (doc-25 SS 25.5)."""
        cfg = self._config
        natr_fallback = True
        effective = cfg.base_spacing_bps

        if (
            cfg.adaptive_enabled
            and natr_bps is not None
            and natr_bps > 0
            and (ts_ms - natr_last_ts) <= cfg.natr_stale_ms
        ):
            adaptive_cfg = AdaptiveGridConfig(
                step_alpha=cfg.step_alpha,
                step_min_bps=cfg.step_min_bps,
            )
            raw_step = compute_step_bps(natr_bps, regime, adaptive_cfg)
            effective = float(max(cfg.step_min_bps, raw_step))
            natr_fallback = False

        # Invariant I2: floor holds
        return max(effective, float(cfg.step_min_bps)), natr_fallback

    def _match_orders(
        self,
        open_orders: tuple[OpenOrderSnap, ...],
        desired: list[_DesiredLevel],
        mid_price: Decimal,
    ) -> _DiffResult:
        """Step C+D: Match exchange orders to desired grid levels (doc-25 SS 25.5)."""
        cfg = self._config
        desired_by_key = {d.key: d for d in desired}
        matched_keys: set[str] = set()
        extra_orders: list[OpenOrderSnap] = []
        mismatch_orders: list[tuple[OpenOrderSnap, _DesiredLevel]] = []

        for order in open_orders:
            # Step C: Filter foreign orders (I6)
            parsed = parse_client_order_id(order.order_id)
            if parsed is None or not parsed.prefix.startswith(DEFAULT_PREFIX.rstrip("_")):
                continue

            # Build match key from parsed order
            level_key = f"{order.side.upper()}:L{parsed.level_id}"

            if level_key in desired_by_key and level_key not in matched_keys:
                desired_level = desired_by_key[level_key]
                price_diff = float(abs(order.price - desired_level.price) / mid_price) * 10000
                qty_diff = (
                    float(abs(order.qty - cfg.size_per_level) / cfg.size_per_level) * 100
                    if cfg.size_per_level > 0
                    else 0.0
                )
                if price_diff > cfg.price_epsilon_bps or qty_diff > cfg.qty_epsilon_pct:
                    mismatch_orders.append((order, desired_level))
                matched_keys.add(level_key)
            else:
                extra_orders.append(order)

        missing_keys = [k for k in desired_by_key if k not in matched_keys]
        return _DiffResult(
            matched_keys=matched_keys,
            extra_orders=extra_orders,
            mismatch_orders=mismatch_orders,
            missing_keys=missing_keys,
        )

    def _should_skip_rebalance(
        self,
        symbol: str,
        mid_price: Decimal,
        effective_spacing_bps: float,
        diff: _DiffResult,
    ) -> bool:
        """Step E: Check hysteresis — skip rebalance if shift < threshold (I3)."""
        # If there are diffs, always rebalance (fills/expires removed orders)
        if diff.missing_keys or diff.extra_orders or diff.mismatch_orders:
            return False

        last_center = self._last_plan_center.get(symbol)
        if last_center is None or effective_spacing_bps <= 0:
            return False

        step_abs = mid_price * Decimal(str(effective_spacing_bps)) / Decimal("10000")
        shift_steps = float(abs(mid_price - last_center) / step_abs) if step_abs > 0 else 0.0
        return shift_steps < self._config.rebalance_threshold_steps

    def _generate_actions(
        self,
        symbol: str,
        diff: _DiffResult,
        desired: list[_DesiredLevel],
        mid_price: Decimal,
    ) -> list[ExecutionAction]:
        """Step F: Generate PLACE/CANCEL actions from diff (doc-25 SS 25.5)."""
        cfg = self._config
        desired_by_key = {d.key: d for d in desired}
        actions: list[ExecutionAction] = []

        # Missing → PLACE
        for key in diff.missing_keys:
            level = desired_by_key[key]
            actions.append(
                ExecutionAction(
                    action_type=ActionType.PLACE,
                    symbol=symbol,
                    side=level.side,
                    price=level.price,
                    quantity=cfg.size_per_level,
                    level_id=level.level_id,
                    reason="GRID_FILL",
                )
            )

        # Extra → CANCEL (skip TP orders — managed by cycle layer, INV-9b)
        for order in diff.extra_orders:
            if is_tp_order(order.order_id):
                continue
            actions.append(
                ExecutionAction(
                    action_type=ActionType.CANCEL,
                    order_id=order.order_id,
                    symbol=symbol,
                    reason="GRID_TRIM",
                )
            )

        # Mismatch → CANCEL + PLACE (skip TP orders, INV-9b)
        for order, desired_level in diff.mismatch_orders:
            if is_tp_order(order.order_id):
                continue
            price_diff = float(abs(order.price - desired_level.price) / mid_price) * 10000
            reason = "GRID_SHIFT" if price_diff > cfg.price_epsilon_bps else "GRID_RESIZE"
            actions.append(
                ExecutionAction(
                    action_type=ActionType.CANCEL,
                    order_id=order.order_id,
                    symbol=symbol,
                    reason=reason,
                )
            )
            actions.append(
                ExecutionAction(
                    action_type=ActionType.PLACE,
                    symbol=symbol,
                    side=desired_level.side,
                    price=desired_level.price,
                    quantity=cfg.size_per_level,
                    level_id=desired_level.level_id,
                    reason=reason,
                )
            )

        return actions

    def _build_rolling_grid(
        self,
        symbol: str,
    ) -> list[_DesiredLevel]:
        """Build desired grid from rolling ladder state (doc-26 SS 5.1).

        Additive formula:
            ec = anchor_price + net_offset * step_price
            BUY[i]  = round_to_tick(ec - i * step_price)
            SELL[i] = round_to_tick(ec + i * step_price)

        INV-9: On fill ticks, innermost levels on the TP side are skipped
        to reserve slots for TP orders. Reservation is per-tick and clamped
        to [0, N]. desired_grid_count = (N - sell_skip) + (N - buy_skip).
        """
        cfg = self._config
        st = self._rolling_state[symbol]
        tick = cfg.tick_size or Decimal("0.01")
        ec = st.anchor_price + st.net_offset * st.step_price
        levels: list[_DesiredLevel] = []

        # INV-9: read TP slot reservations (planner-local, one-tick)
        res = self._tp_slot_reservations.get(symbol, {"BUY": 0, "SELL": 0})
        buy_skip = min(res.get("BUY", 0), cfg.levels)
        sell_skip = min(res.get("SELL", 0), cfg.levels)

        # Max level distance cap (PR-VERIF-KNOBS-1)
        cap_bps = cfg.max_level_distance_bps
        if cap_bps is not None and cap_bps > 0:
            cap_factor = Decimal(str(cap_bps)) / Decimal("10000")
            cap_low = ec * (Decimal("1") - cap_factor)
            cap_high = ec * (Decimal("1") + cap_factor)
        else:
            cap_low = None
            cap_high = None

        for i in range(1, cfg.levels + 1):
            buy_price = _round_to_tick(ec - i * st.step_price, tick)
            sell_price = _round_to_tick(ec + i * st.step_price, tick)

            # Skip levels outside cap (PR-VERIF-KNOBS-1)
            if cap_low is not None and buy_price < cap_low:
                continue
            if cap_high is not None and sell_price > cap_high:
                continue

            # INV-9: skip innermost levels reserved for TP
            if i > buy_skip:
                levels.append(
                    _DesiredLevel(key=f"BUY:L{i}", side=OrderSide.BUY, level_id=i, price=buy_price)
                )
            if i > sell_skip:
                levels.append(
                    _DesiredLevel(
                        key=f"SELL:L{i}", side=OrderSide.SELL, level_id=i, price=sell_price
                    )
                )

        return levels

    def _match_orders_by_price(
        self,
        open_orders: tuple[OpenOrderSnap, ...],
        desired: list[_DesiredLevel],
        ref_price: Decimal,
    ) -> _DiffResult:
        """Match exchange orders to desired levels by (side, price) (doc-26 SS 9.2).

        Replaces level_id matching for rolling mode. Each exchange order can
        match at most one desired level (closest price within epsilon wins).
        """
        cfg = self._config
        matched_keys: set[str] = set()
        extra_orders: list[OpenOrderSnap] = []
        mismatch_orders: list[tuple[OpenOrderSnap, _DesiredLevel]] = []
        used_orders: set[str] = set()

        # Filter to grinder orders only (same I6 invariant as _match_orders)
        grinder_orders: list[OpenOrderSnap] = []
        for order in open_orders:
            parsed = parse_client_order_id(order.order_id)
            if parsed is not None and parsed.prefix.startswith(DEFAULT_PREFIX.rstrip("_")):
                grinder_orders.append(order)

        # For each desired level, find best-matching unmatched exchange order
        for desired_level in desired:
            best_match: OpenOrderSnap | None = None
            best_diff = float("inf")
            for order in grinder_orders:
                if order.order_id in used_orders:
                    continue
                if order.side.upper() != desired_level.side.value:
                    continue
                price_diff_bps = (
                    float(abs(order.price - desired_level.price) / ref_price) * 10000
                    if ref_price > 0
                    else float("inf")
                )
                if price_diff_bps <= cfg.price_epsilon_bps and price_diff_bps < best_diff:
                    best_diff = price_diff_bps
                    best_match = order

            if best_match is not None:
                used_orders.add(best_match.order_id)
                matched_keys.add(desired_level.key)
                # Check qty match
                qty_diff = (
                    float(abs(best_match.qty - cfg.size_per_level) / cfg.size_per_level) * 100
                    if cfg.size_per_level > 0
                    else 0.0
                )
                if qty_diff > cfg.qty_epsilon_pct:
                    mismatch_orders.append((best_match, desired_level))

        # Remaining grinder orders = extra
        for order in grinder_orders:
            if order.order_id not in used_orders:
                extra_orders.append(order)

        missing_keys = [d.key for d in desired if d.key not in matched_keys]
        return _DiffResult(
            matched_keys=matched_keys,
            extra_orders=extra_orders,
            mismatch_orders=mismatch_orders,
            missing_keys=missing_keys,
        )

    def _build_desired_grid(
        self,
        mid_price: Decimal,
        spacing_bps: float,
        tick_size: Decimal,
    ) -> list[_DesiredLevel]:
        """Build symmetric bilateral grid centered on mid_price (doc-25 SS 25.5 Step 2).

        PR-VERIF-KNOBS-1: If max_level_distance_bps is set, levels beyond the cap
        are skipped (not clamped). This reduces desired_count, which is reflected
        in GridPlanResult.desired_count and diff_missing.
        """
        cfg = self._config
        levels: list[_DesiredLevel] = []
        spacing_factor = Decimal(str(spacing_bps)) / Decimal("10000")

        # PR-VERIF-KNOBS-1: compute cap boundaries if configured
        cap_bps = cfg.max_level_distance_bps
        if cap_bps is not None and cap_bps > 0:
            cap_factor = Decimal(str(cap_bps)) / Decimal("10000")
            cap_low = mid_price * (Decimal("1") - cap_factor)
            cap_high = mid_price * (Decimal("1") + cap_factor)
        else:
            cap_low = None
            cap_high = None

        for i in range(1, cfg.levels + 1):
            buy_price = _round_to_tick(
                mid_price * (Decimal("1") - spacing_factor * i),
                tick_size,
            )
            sell_price = _round_to_tick(
                mid_price * (Decimal("1") + spacing_factor * i),
                tick_size,
            )

            # Skip levels outside cap (PR-VERIF-KNOBS-1)
            if cap_low is not None and buy_price < cap_low:
                continue
            if cap_high is not None and sell_price > cap_high:
                continue

            levels.append(
                _DesiredLevel(key=f"BUY:L{i}", side=OrderSide.BUY, level_id=i, price=buy_price)
            )
            levels.append(
                _DesiredLevel(key=f"SELL:L{i}", side=OrderSide.SELL, level_id=i, price=sell_price)
            )

        return levels
