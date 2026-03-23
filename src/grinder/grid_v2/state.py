"""Pure deterministic state machine for Two-Sided Rolling Window Grid v2.

SSOT: docs/27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md (sections 4-21).

Design constraints:
- Pure logic: no I/O, no logging, no metrics emission.
- Deterministic: same (state, event) -> same (next_state, actions).
- All domain objects are frozen dataclasses.
- Caller responsible for logging/metrics/evidence on transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from enum import Enum

from grinder.core import OrderSide
from grinder.reconcile.identity import parse_client_order_id


class BranchMode(Enum):
    """Inventory branch mode (section 6)."""

    FLAT = "FLAT"
    LONG_BRANCH = "LONG_BRANCH"
    SHORT_BRANCH = "SHORT_BRANCH"


class LotSide(Enum):
    """Inventory lot direction."""

    LONG = "LONG"
    SHORT = "SHORT"


class LotStatus(Enum):
    """Inventory lot lifecycle status."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ExitOrderStatus(Enum):
    """Exit order lifecycle status."""

    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"


class ActionIntentKind(Enum):
    """External action intent types (21.3, 21.9)."""

    PLACE_EXIT = "PLACE_EXIT"
    PLACE_ENTRY = "PLACE_ENTRY"
    CANCEL_ENTRY = "CANCEL_ENTRY"
    CANCEL_EXIT = "CANCEL_EXIT"


@dataclass(frozen=True)
class GridV2Config:
    """Grid configuration. Validated at construction (21.12)."""

    grid_step_pct: Decimal
    entry_levels_per_side: int
    order_size: Decimal
    max_inventory_levels: int
    max_inventory_notional_usd: Decimal
    price_tick_size: Decimal = Decimal("0.01")  # PR6: exchange tick size for price quantization
    reseed_on_flat: bool = (
        False  # Default: preserve current window on full unwind to FLAT (no unconditional recenter)
    )
    reseed_on_flat_only_on_skew: bool = (
        True  # Default: in preserve mode, reseed only when BUY/SELL ladder is skewed
    )

    def __post_init__(self) -> None:
        if self.grid_step_pct <= 0:
            raise ValueError("grid_step_pct must be positive")
        if self.entry_levels_per_side <= 0:
            raise ValueError("entry_levels_per_side must be positive")
        if self.order_size <= 0:
            raise ValueError("order_size must be positive")
        if self.max_inventory_levels <= 0:
            raise ValueError("max_inventory_levels must be positive")
        if self.max_inventory_notional_usd <= 0:
            raise ValueError("max_inventory_notional_usd must be positive")
        if self.price_tick_size <= 0:
            raise ValueError("price_tick_size must be positive")


@dataclass(frozen=True)
class EntryWindow:
    """Active entry price window (section 9, 21.3)."""

    reference_price: Decimal
    buy_entry_prices: tuple[Decimal, ...]
    sell_entry_prices: tuple[Decimal, ...]
    levels_per_side: int
    step_pct: Decimal


@dataclass(frozen=True)
class InventoryLot:
    """Single inventory lot (section 6)."""

    lot_id: str
    side: LotSide
    entry_price: Decimal
    qty: Decimal
    opened_at_ts: int
    source_entry_order_id: str
    exit_price: Decimal
    exit_order_id: str | None
    status: LotStatus


@dataclass(frozen=True)
class ExitOrder:
    """Exit order paired to an inventory lot (section 6)."""

    exit_order_id: str
    lot_id: str
    side: OrderSide
    price: Decimal
    qty: Decimal
    status: ExitOrderStatus


@dataclass(frozen=True)
class EntryFilled:
    """An entry order was filled on the exchange."""

    order_id: str
    side: OrderSide
    price: Decimal
    qty: Decimal
    ts: int


@dataclass(frozen=True)
class ExitFilled:
    """An exit order was filled on the exchange."""

    exit_order_id: str
    lot_id: str
    price: Decimal
    qty: Decimal
    ts: int


@dataclass(frozen=True)
class RecenterRequested:
    """Caller requests window rebuild around a new reference price."""

    new_reference_price: Decimal
    ts: int


@dataclass(frozen=True)
class EmergencyStopTriggered:
    """Emergency stop: freeze new exposure, keep exits."""

    ts: int


@dataclass(frozen=True)
class OperatorCleanup:
    """Full state reset: close all lots, cancel all orders."""

    ts: int


GridV2Event = (
    EntryFilled | ExitFilled | RecenterRequested | EmergencyStopTriggered | OperatorCleanup
)


@dataclass(frozen=True)
class ActionIntent:
    """External action intent emitted by state machine (21.9, 21.11)."""

    kind: ActionIntentKind
    side: OrderSide | None = None
    price: Decimal | None = None
    qty: Decimal | None = None
    lot_id: str | None = None
    order_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class GridV2Snapshot:
    """Complete state machine snapshot (section 6, 21.3)."""

    mode: BranchMode
    entry_window: EntryWindow
    open_lots: tuple[InventoryLot, ...]
    closed_lots: tuple[InventoryLot, ...]
    exit_orders: tuple[ExitOrder, ...]
    emergency_stopped: bool
    last_recenter_ts: int | None


@dataclass(frozen=True)
class TransitionResult:
    """Result of applying an event to the state machine."""

    snapshot: GridV2Snapshot
    actions: tuple[ActionIntent, ...]
    rejected: bool = False
    reject_reason: str = ""


class GridV2InvariantError(Exception):
    """Raised when a snapshot violates state machine invariants."""


def _build_entry_window(
    reference_price: Decimal,
    levels_per_side: int,
    step_pct: Decimal,
    tick_size: Decimal,
) -> EntryWindow:
    """Build symmetric entry window around reference price (section 9)."""
    step_price = _grid_step_price(reference_price, step_pct, tick_size)
    anchor = _round_to_tick(reference_price, tick_size, rounding=ROUND_HALF_UP)
    buy_prices = tuple(anchor - step_price * i for i in range(1, levels_per_side + 1))
    sell_prices = tuple(anchor + step_price * i for i in range(1, levels_per_side + 1))
    return EntryWindow(
        reference_price=reference_price,
        buy_entry_prices=buy_prices,
        sell_entry_prices=sell_prices,
        levels_per_side=levels_per_side,
        step_pct=step_pct,
    )


def _round_to_tick(
    price: Decimal,
    tick_size: Decimal,
    *,
    rounding: str = ROUND_HALF_UP,
) -> Decimal:
    """Quantize a price to tick_size."""
    return (price / tick_size).quantize(Decimal("1"), rounding=rounding) * tick_size


def _grid_step_price(reference_price: Decimal, step_pct: Decimal, tick_size: Decimal) -> Decimal:
    """Convert pct step to a stable integer number of ticks (min 1 tick)."""
    raw_step = reference_price * step_pct
    step_ticks = (raw_step / tick_size).quantize(Decimal("1"), rounding=ROUND_CEILING)
    if step_ticks < 1:
        step_ticks = Decimal("1")
    return step_ticks * tick_size


def _empty_entry_window(window: EntryWindow) -> EntryWindow:
    """Return window with cleared price tuples but preserved config."""
    return EntryWindow(
        reference_price=window.reference_price,
        buy_entry_prices=(),
        sell_entry_prices=(),
        levels_per_side=window.levels_per_side,
        step_pct=window.step_pct,
    )


def _check_invariants(snapshot: GridV2Snapshot, config: GridV2Config) -> None:
    """Validate snapshot invariants I1, I3, I4 (sections 4, 21.15)."""
    _check_i1_bounded_window(snapshot, config)
    _check_i3_paired_exits(snapshot)
    _check_i4_branch_consistency(snapshot)


def _check_i1_bounded_window(snapshot: GridV2Snapshot, config: GridV2Config) -> None:
    if len(snapshot.entry_window.buy_entry_prices) > config.entry_levels_per_side:
        raise GridV2InvariantError(
            f"I1: buy_entry_prices has {len(snapshot.entry_window.buy_entry_prices)} levels, "
            f"max {config.entry_levels_per_side}"
        )
    if len(snapshot.entry_window.sell_entry_prices) > config.entry_levels_per_side:
        raise GridV2InvariantError(
            f"I1: sell_entry_prices has {len(snapshot.entry_window.sell_entry_prices)} levels, "
            f"max {config.entry_levels_per_side}"
        )


def _check_i3_paired_exits(snapshot: GridV2Snapshot) -> None:
    open_exit_lot_ids = {
        eo.lot_id for eo in snapshot.exit_orders if eo.status == ExitOrderStatus.OPEN
    }
    for lot in snapshot.open_lots:
        if lot.lot_id not in open_exit_lot_ids:
            raise GridV2InvariantError(f"I3: open lot {lot.lot_id} has no paired OPEN exit order")


def _check_i4_branch_consistency(snapshot: GridV2Snapshot) -> None:
    if snapshot.mode == BranchMode.FLAT and snapshot.open_lots:
        raise GridV2InvariantError(f"I4: mode=FLAT but {len(snapshot.open_lots)} open lots exist")
    if snapshot.mode == BranchMode.LONG_BRANCH:
        for lot in snapshot.open_lots:
            if lot.side != LotSide.LONG:
                raise GridV2InvariantError(
                    f"I4: mode=LONG_BRANCH but lot {lot.lot_id} has side={lot.side.value}"
                )
    if snapshot.mode == BranchMode.SHORT_BRANCH:
        for lot in snapshot.open_lots:
            if lot.side != LotSide.SHORT:
                raise GridV2InvariantError(
                    f"I4: mode=SHORT_BRANCH but lot {lot.lot_id} has side={lot.side.value}"
                )


class GridV2StateMachine:
    """Pure deterministic state machine for the two-sided rolling window grid.

    No I/O, no logging, no metrics. Deterministic: same (state, event) -> same result.
    SSOT: docs/27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md.
    """

    def __init__(self, config: GridV2Config, snapshot: GridV2Snapshot) -> None:
        """Construct from existing snapshot. Validates invariants (21.15)."""
        self._config = config
        self._snapshot = snapshot
        _check_invariants(snapshot, config)

    @staticmethod
    def create_initial(
        config: GridV2Config,
        reference_price: Decimal,
        _ts: int,
    ) -> GridV2StateMachine:
        """Create state machine with initial symmetric entry window.

        Goes through __init__ for single validation path (21.15).
        """
        window = _build_entry_window(
            reference_price,
            config.entry_levels_per_side,
            config.grid_step_pct,
            config.price_tick_size,
        )
        snapshot = GridV2Snapshot(
            mode=BranchMode.FLAT,
            entry_window=window,
            open_lots=(),
            closed_lots=(),
            exit_orders=(),
            emergency_stopped=False,
            last_recenter_ts=None,
        )
        return GridV2StateMachine(config, snapshot)

    @property
    def snapshot(self) -> GridV2Snapshot:
        return self._snapshot

    @property
    def mode(self) -> BranchMode:
        return self._snapshot.mode

    def apply(self, event: GridV2Event) -> TransitionResult:
        """Apply event and return transition result."""
        if self._snapshot.emergency_stopped and isinstance(event, (EntryFilled, RecenterRequested)):
            return self._reject("EMERGENCY_STOPPED")

        if isinstance(event, EntryFilled):
            return self._apply_entry_filled(event)
        if isinstance(event, ExitFilled):
            return self._apply_exit_filled(event)
        if isinstance(event, RecenterRequested):
            return self._apply_recenter(event)
        if isinstance(event, EmergencyStopTriggered):
            return self._apply_emergency_stop()
        if isinstance(event, OperatorCleanup):
            return self._apply_operator_cleanup()
        raise TypeError(f"Unknown event type: {type(event)}")  # pragma: no cover

    def _reject(self, reason: str) -> TransitionResult:
        return TransitionResult(
            snapshot=self._snapshot, actions=(), rejected=True, reject_reason=reason
        )

    def _commit(
        self, snapshot: GridV2Snapshot, actions: tuple[ActionIntent, ...]
    ) -> TransitionResult:
        _check_invariants(snapshot, self._config)
        self._snapshot = snapshot
        return TransitionResult(snapshot=snapshot, actions=actions)

    def _apply_entry_filled(self, event: EntryFilled) -> TransitionResult:
        rejection = self._validate_entry(event)
        if rejection is not None:
            return rejection
        return self._execute_entry(event)

    def _validate_entry(self, event: EntryFilled) -> TransitionResult | None:
        """Validate entry fill preconditions. Returns rejection or None."""
        if event.qty <= 0:
            return self._reject("INVALID_QUANTITY")
        reason = self._entry_fill_reject_reason(event)
        return self._reject(reason) if reason else None

    def _entry_fill_reject_reason(self, event: EntryFilled) -> str:
        """Return reject reason string, or empty string if valid."""
        snap = self._snapshot
        cfg = self._config

        if any(
            lot.source_entry_order_id == event.order_id
            for lot in (*snap.open_lots, *snap.closed_lots)
        ):
            return "DUPLICATE_ENTRY_FILL"

        active_prices = (
            snap.entry_window.buy_entry_prices
            if event.side == OrderSide.BUY
            else snap.entry_window.sell_entry_prices
        )
        parsed = parse_client_order_id(event.order_id)
        is_grid_v2_cid = parsed is not None and parsed.strategy_id == "g"
        if event.price not in active_prices and not is_grid_v2_cid:
            return "PRICE_NOT_IN_ACTIVE_WINDOW"
        if len(snap.open_lots) >= cfg.max_inventory_levels and not is_grid_v2_cid:
            return "MAX_INVENTORY_LEVELS"
        current_notional = sum(lot.qty * lot.entry_price for lot in snap.open_lots)
        if (
            current_notional + event.qty * event.price > cfg.max_inventory_notional_usd
            and not is_grid_v2_cid
        ):
            return "MAX_INVENTORY_NOTIONAL_USD"
        if (snap.mode == BranchMode.SHORT_BRANCH and event.side == OrderSide.BUY) or (
            snap.mode == BranchMode.LONG_BRANCH and event.side == OrderSide.SELL
        ):
            return "BRANCH_INCOMPATIBLE"
        return ""

    def _execute_entry(self, event: EntryFilled) -> TransitionResult:
        """Execute a validated entry fill."""
        snap = self._snapshot
        cfg = self._config

        if event.side == OrderSide.BUY:
            lot_side, exit_side = LotSide.LONG, OrderSide.SELL
            exit_price = event.price * (Decimal(1) + cfg.grid_step_pct)
        else:
            lot_side, exit_side = LotSide.SHORT, OrderSide.BUY
            exit_price = event.price * (Decimal(1) - cfg.grid_step_pct)

        lot = InventoryLot(
            lot_id=f"lot-{event.order_id}",
            side=lot_side,
            entry_price=event.price,
            qty=event.qty,
            opened_at_ts=event.ts,
            source_entry_order_id=event.order_id,
            exit_price=exit_price,
            exit_order_id=f"exit-{event.order_id}",
            status=LotStatus.OPEN,
        )
        exit_order = ExitOrder(
            exit_order_id=f"exit-{event.order_id}",
            lot_id=lot.lot_id,
            side=exit_side,
            price=exit_price,
            qty=event.qty,
            status=ExitOrderStatus.OPEN,
        )

        actions: list[ActionIntent] = [
            ActionIntent(
                kind=ActionIntentKind.PLACE_EXIT,
                side=exit_side,
                price=exit_price,
                qty=event.qty,
                lot_id=lot.lot_id,
                reason="PAIRED_EXIT_FOR_LOT",
            ),
        ]

        new_window, rolling_actions = self._update_window_after_fill(event)
        actions.extend(rolling_actions)

        new_mode = BranchMode.LONG_BRANCH if lot_side == LotSide.LONG else BranchMode.SHORT_BRANCH
        new_snapshot = GridV2Snapshot(
            mode=new_mode,
            entry_window=new_window,
            open_lots=(*snap.open_lots, lot),
            closed_lots=snap.closed_lots,
            exit_orders=(*snap.exit_orders, exit_order),
            emergency_stopped=snap.emergency_stopped,
            last_recenter_ts=snap.last_recenter_ts,
        )
        return self._commit(new_snapshot, tuple(actions))

    def _update_window_after_fill(
        self, event: EntryFilled
    ) -> tuple[EntryWindow, tuple[ActionIntent, ...]]:
        """Apply rolling-window update for an accepted entry fill.

        Rolling rule:
        - extend entries on the fill side with one new farthest level
        - trim one farthest level from the opposite side
        - opposite trim emits one CANCEL_ENTRY action
        - replacement PLACE_ENTRY emits only when replacement gate allows it
        """
        window = self._snapshot.entry_window
        cfg = self._config
        actions: list[ActionIntent] = []

        if event.side == OrderSide.BUY:
            remaining = tuple(p for p in window.buy_entry_prices if p != event.price)
            farthest = remaining[-1] if remaining else event.price
            step_price = _grid_step_price(
                window.reference_price, cfg.grid_step_pct, cfg.price_tick_size
            )
            new_farthest = farthest - step_price
            if window.sell_entry_prices:
                trimmed = window.sell_entry_prices[-1]
                new_sells = window.sell_entry_prices[:-1]
                actions.append(
                    ActionIntent(
                        kind=ActionIntentKind.CANCEL_ENTRY,
                        side=OrderSide.SELL,
                        price=trimmed,
                        reason="ROLLING_TRIM",
                    )
                )
            else:
                new_sells = ()
            new_buys = (*remaining, new_farthest)[: cfg.entry_levels_per_side]
            actions.append(
                ActionIntent(
                    kind=ActionIntentKind.PLACE_ENTRY,
                    side=OrderSide.BUY,
                    price=new_farthest,
                    qty=cfg.order_size,
                    reason="FILL_REPLACEMENT",
                )
            )
        else:
            remaining = tuple(p for p in window.sell_entry_prices if p != event.price)
            farthest = remaining[-1] if remaining else event.price
            step_price = _grid_step_price(
                window.reference_price, cfg.grid_step_pct, cfg.price_tick_size
            )
            new_farthest = farthest + step_price
            if window.buy_entry_prices:
                trimmed = window.buy_entry_prices[-1]
                new_buys = window.buy_entry_prices[:-1]
                actions.append(
                    ActionIntent(
                        kind=ActionIntentKind.CANCEL_ENTRY,
                        side=OrderSide.BUY,
                        price=trimmed,
                        reason="ROLLING_TRIM",
                    )
                )
            else:
                new_buys = ()
            new_sells = (*remaining, new_farthest)[: cfg.entry_levels_per_side]
            actions.append(
                ActionIntent(
                    kind=ActionIntentKind.PLACE_ENTRY,
                    side=OrderSide.SELL,
                    price=new_farthest,
                    qty=cfg.order_size,
                    reason="FILL_REPLACEMENT",
                )
            )

        new_window = EntryWindow(
            reference_price=window.reference_price,
            buy_entry_prices=new_buys,
            sell_entry_prices=new_sells,
            levels_per_side=window.levels_per_side,
            step_pct=window.step_pct,
        )
        return new_window, tuple(actions)

    def _apply_exit_filled(self, event: ExitFilled) -> TransitionResult:  # noqa: PLR0912, PLR0915
        snap = self._snapshot

        if event.qty <= 0:
            return self._reject("INVALID_QUANTITY")

        target_lot, lot_rejection = self._find_open_lot(event.lot_id)
        if lot_rejection is not None:
            return lot_rejection

        target_exit = self._find_exit_order(event.exit_order_id)
        if target_exit is None:
            return self._reject("UNKNOWN_EXIT_ORDER_ID")

        assert target_lot is not None
        pair_rejection = self._validate_exit_pair(target_lot, target_exit, event)
        if pair_rejection is not None:
            return pair_rejection

        closed_lot = InventoryLot(
            lot_id=target_lot.lot_id,
            side=target_lot.side,
            entry_price=target_lot.entry_price,
            qty=target_lot.qty,
            opened_at_ts=target_lot.opened_at_ts,
            source_entry_order_id=target_lot.source_entry_order_id,
            exit_price=target_lot.exit_price,
            exit_order_id=target_lot.exit_order_id,
            status=LotStatus.CLOSED,
        )
        filled_exit = ExitOrder(
            exit_order_id=target_exit.exit_order_id,
            lot_id=target_exit.lot_id,
            side=target_exit.side,
            price=target_exit.price,
            qty=target_exit.qty,
            status=ExitOrderStatus.FILLED,
        )

        new_open = tuple(item for item in snap.open_lots if item.lot_id != event.lot_id)
        new_closed = (*snap.closed_lots, closed_lot)
        new_exits = tuple(
            filled_exit if eo.exit_order_id == event.exit_order_id else eo
            for eo in snap.exit_orders
        )

        actions: list[ActionIntent] = []
        new_window = snap.entry_window
        new_mode = BranchMode.FLAT if not new_open else snap.mode
        new_last_recenter_ts = snap.last_recenter_ts
        restore_without_reseed = (
            not self._config.reseed_on_flat
            and not new_open
            and snap.mode in {BranchMode.LONG_BRANCH, BranchMode.SHORT_BRANCH}
        )
        if new_open or restore_without_reseed:
            step_delta = _grid_step_price(
                snap.entry_window.reference_price,
                self._config.grid_step_pct,
                self._config.price_tick_size,
            )
            if snap.mode == BranchMode.LONG_BRANCH:
                buy_prices = list(snap.entry_window.buy_entry_prices)
                sell_prices = list(snap.entry_window.sell_entry_prices)
                if len(sell_prices) < self._config.entry_levels_per_side:
                    next_price = (
                        sell_prices[-1] + step_delta
                        if sell_prices
                        else snap.entry_window.reference_price + step_delta
                    )
                    sell_prices.append(next_price)
                    actions.append(
                        ActionIntent(
                            kind=ActionIntentKind.PLACE_ENTRY,
                            side=OrderSide.SELL,
                            price=next_price,
                            qty=self._config.order_size,
                            reason="EXIT_RESTORE",
                        )
                    )
                if len(buy_prices) < self._config.entry_levels_per_side:
                    next_price = (
                        buy_prices[0] + step_delta
                        if buy_prices
                        else snap.entry_window.reference_price - step_delta
                    )
                    buy_prices.append(next_price)
                    buy_prices.sort(reverse=True)
                    actions.append(
                        ActionIntent(
                            kind=ActionIntentKind.PLACE_ENTRY,
                            side=OrderSide.BUY,
                            price=next_price,
                            qty=self._config.order_size,
                            reason="EXIT_RESTORE",
                        )
                    )
                elif buy_prices:
                    next_price = buy_prices[0] + step_delta
                    if (
                        next_price < snap.entry_window.reference_price
                        and next_price not in buy_prices
                    ):
                        far_price = buy_prices[-1]
                        buy_prices = buy_prices[:-1]
                        buy_prices.append(next_price)
                        buy_prices.sort(reverse=True)
                        actions.append(
                            ActionIntent(
                                kind=ActionIntentKind.CANCEL_ENTRY,
                                side=OrderSide.BUY,
                                price=far_price,
                                reason="EXIT_RESTORE_SHIFT",
                            )
                        )
                        actions.append(
                            ActionIntent(
                                kind=ActionIntentKind.PLACE_ENTRY,
                                side=OrderSide.BUY,
                                price=next_price,
                                qty=self._config.order_size,
                                reason="EXIT_RESTORE",
                            )
                        )
                new_window = EntryWindow(
                    reference_price=snap.entry_window.reference_price,
                    buy_entry_prices=tuple(buy_prices),
                    sell_entry_prices=tuple(sell_prices),
                    levels_per_side=snap.entry_window.levels_per_side,
                    step_pct=snap.entry_window.step_pct,
                )
            elif snap.mode == BranchMode.SHORT_BRANCH:
                buy_prices = list(snap.entry_window.buy_entry_prices)
                sell_prices = list(snap.entry_window.sell_entry_prices)
                if len(buy_prices) < self._config.entry_levels_per_side:
                    next_price = (
                        buy_prices[-1] - step_delta
                        if buy_prices
                        else snap.entry_window.reference_price - step_delta
                    )
                    buy_prices.append(next_price)
                    actions.append(
                        ActionIntent(
                            kind=ActionIntentKind.PLACE_ENTRY,
                            side=OrderSide.BUY,
                            price=next_price,
                            qty=self._config.order_size,
                            reason="EXIT_RESTORE",
                        )
                    )
                if len(sell_prices) < self._config.entry_levels_per_side:
                    next_price = (
                        sell_prices[0] - step_delta
                        if sell_prices
                        else snap.entry_window.reference_price + step_delta
                    )
                    sell_prices.append(next_price)
                    sell_prices.sort()
                    actions.append(
                        ActionIntent(
                            kind=ActionIntentKind.PLACE_ENTRY,
                            side=OrderSide.SELL,
                            price=next_price,
                            qty=self._config.order_size,
                            reason="EXIT_RESTORE",
                        )
                    )
                elif sell_prices:
                    next_price = sell_prices[0] - step_delta
                    if (
                        next_price > snap.entry_window.reference_price
                        and next_price not in sell_prices
                    ):
                        far_price = sell_prices[-1]
                        sell_prices = sell_prices[:-1]
                        sell_prices.append(next_price)
                        sell_prices.sort()
                        actions.append(
                            ActionIntent(
                                kind=ActionIntentKind.CANCEL_ENTRY,
                                side=OrderSide.SELL,
                                price=far_price,
                                reason="EXIT_RESTORE_SHIFT",
                            )
                        )
                        actions.append(
                            ActionIntent(
                                kind=ActionIntentKind.PLACE_ENTRY,
                                side=OrderSide.SELL,
                                price=next_price,
                                qty=self._config.order_size,
                                reason="EXIT_RESTORE",
                            )
                        )
                new_window = EntryWindow(
                    reference_price=snap.entry_window.reference_price,
                    buy_entry_prices=tuple(buy_prices),
                    sell_entry_prices=tuple(sell_prices),
                    levels_per_side=snap.entry_window.levels_per_side,
                    step_pct=snap.entry_window.step_pct,
                )
        elif self._config.reseed_on_flat:
            new_window = _build_entry_window(
                snap.entry_window.reference_price,
                self._config.entry_levels_per_side,
                self._config.grid_step_pct,
                self._config.price_tick_size,
            )
            actions.extend(
                ActionIntent(
                    kind=ActionIntentKind.CANCEL_ENTRY,
                    side=OrderSide.BUY,
                    price=p,
                    reason="RECENTER_REPLACE",
                )
                for p in snap.entry_window.buy_entry_prices
            )
            actions.extend(
                ActionIntent(
                    kind=ActionIntentKind.CANCEL_ENTRY,
                    side=OrderSide.SELL,
                    price=p,
                    reason="RECENTER_REPLACE",
                )
                for p in snap.entry_window.sell_entry_prices
            )
            actions.extend(
                ActionIntent(
                    kind=ActionIntentKind.PLACE_ENTRY,
                    side=OrderSide.BUY,
                    price=p,
                    qty=self._config.order_size,
                    reason="RECENTER",
                )
                for p in new_window.buy_entry_prices
            )
            actions.extend(
                ActionIntent(
                    kind=ActionIntentKind.PLACE_ENTRY,
                    side=OrderSide.SELL,
                    price=p,
                    qty=self._config.order_size,
                    reason="RECENTER",
                )
                for p in new_window.sell_entry_prices
            )
            new_last_recenter_ts = event.ts
        else:
            new_window = snap.entry_window

        new_snapshot = GridV2Snapshot(
            mode=new_mode,
            entry_window=new_window,
            open_lots=new_open,
            closed_lots=new_closed,
            exit_orders=new_exits,
            emergency_stopped=snap.emergency_stopped,
            last_recenter_ts=new_last_recenter_ts,
        )
        return self._commit(new_snapshot, tuple(actions))

    def _find_open_lot(self, lot_id: str) -> tuple[InventoryLot | None, TransitionResult | None]:
        """Find open lot by ID. Returns (lot, None) or (None, rejection)."""
        snap = self._snapshot
        for lot in snap.open_lots:
            if lot.lot_id == lot_id:
                return lot, None
        for lot in snap.closed_lots:
            if lot.lot_id == lot_id:
                return None, self._reject("LOT_ALREADY_CLOSED")
        return None, self._reject("UNKNOWN_LOT_ID")

    def _find_exit_order(self, exit_order_id: str) -> ExitOrder | None:
        for eo in self._snapshot.exit_orders:
            if eo.exit_order_id == exit_order_id:
                return eo
        return None

    def _validate_exit_pair(
        self, lot: InventoryLot, exit_order: ExitOrder, event: ExitFilled
    ) -> TransitionResult | None:
        if exit_order.lot_id != event.lot_id:
            return self._reject("EXIT_LOT_MISMATCH")
        if lot.exit_order_id != event.exit_order_id:
            return self._reject("LOT_EXIT_MISMATCH")
        if lot.status == LotStatus.CLOSED:
            return self._reject("LOT_ALREADY_CLOSED")
        if exit_order.status == ExitOrderStatus.FILLED:
            return self._reject("EXIT_ALREADY_FILLED")
        return None

    def _apply_recenter(self, event: RecenterRequested) -> TransitionResult:
        snap = self._snapshot

        if snap.mode != BranchMode.FLAT:
            return self._reject("RECENTER_NOT_FLAT")
        if snap.open_lots:
            return self._reject("RECENTER_LOTS_OPEN")
        if any(eo.status == ExitOrderStatus.OPEN for eo in snap.exit_orders):
            return self._reject("RECENTER_EXITS_OPEN")

        actions: list[ActionIntent] = []
        for p in snap.entry_window.buy_entry_prices:
            actions.append(
                ActionIntent(
                    kind=ActionIntentKind.CANCEL_ENTRY,
                    side=OrderSide.BUY,
                    price=p,
                    reason="RECENTER_REPLACE",
                )
            )
        for p in snap.entry_window.sell_entry_prices:
            actions.append(
                ActionIntent(
                    kind=ActionIntentKind.CANCEL_ENTRY,
                    side=OrderSide.SELL,
                    price=p,
                    reason="RECENTER_REPLACE",
                )
            )

        new_window = _build_entry_window(
            event.new_reference_price,
            self._config.entry_levels_per_side,
            self._config.grid_step_pct,
            self._config.price_tick_size,
        )

        for p in new_window.buy_entry_prices:
            actions.append(
                ActionIntent(
                    kind=ActionIntentKind.PLACE_ENTRY,
                    side=OrderSide.BUY,
                    price=p,
                    qty=self._config.order_size,
                    reason="RECENTER",
                )
            )
        for p in new_window.sell_entry_prices:
            actions.append(
                ActionIntent(
                    kind=ActionIntentKind.PLACE_ENTRY,
                    side=OrderSide.SELL,
                    price=p,
                    qty=self._config.order_size,
                    reason="RECENTER",
                )
            )

        new_snapshot = GridV2Snapshot(
            mode=BranchMode.FLAT,
            entry_window=new_window,
            open_lots=snap.open_lots,
            closed_lots=snap.closed_lots,
            exit_orders=snap.exit_orders,
            emergency_stopped=snap.emergency_stopped,
            last_recenter_ts=event.ts,
        )
        return self._commit(new_snapshot, tuple(actions))

    def _apply_emergency_stop(self) -> TransitionResult:
        snap = self._snapshot

        if snap.emergency_stopped:
            return TransitionResult(snapshot=snap, actions=())

        actions: list[ActionIntent] = []
        for p in snap.entry_window.buy_entry_prices:
            actions.append(
                ActionIntent(
                    kind=ActionIntentKind.CANCEL_ENTRY,
                    side=OrderSide.BUY,
                    price=p,
                    reason="EMERGENCY_STOP",
                )
            )
        for p in snap.entry_window.sell_entry_prices:
            actions.append(
                ActionIntent(
                    kind=ActionIntentKind.CANCEL_ENTRY,
                    side=OrderSide.SELL,
                    price=p,
                    reason="EMERGENCY_STOP",
                )
            )

        new_snapshot = GridV2Snapshot(
            mode=snap.mode,
            entry_window=_empty_entry_window(snap.entry_window),
            open_lots=snap.open_lots,
            closed_lots=snap.closed_lots,
            exit_orders=snap.exit_orders,
            emergency_stopped=True,
            last_recenter_ts=snap.last_recenter_ts,
        )
        return self._commit(new_snapshot, tuple(actions))

    def _apply_operator_cleanup(self) -> TransitionResult:
        snap = self._snapshot

        has_open_exits = any(eo.status == ExitOrderStatus.OPEN for eo in snap.exit_orders)
        has_entries = bool(
            snap.entry_window.buy_entry_prices or snap.entry_window.sell_entry_prices
        )
        if (
            snap.mode == BranchMode.FLAT
            and not snap.open_lots
            and not has_open_exits
            and not has_entries
            and not snap.emergency_stopped
        ):
            return TransitionResult(snapshot=snap, actions=())

        actions: list[ActionIntent] = []
        new_closed_lots = list(snap.closed_lots)
        for lot in snap.open_lots:
            new_closed_lots.append(
                InventoryLot(
                    lot_id=lot.lot_id,
                    side=lot.side,
                    entry_price=lot.entry_price,
                    qty=lot.qty,
                    opened_at_ts=lot.opened_at_ts,
                    source_entry_order_id=lot.source_entry_order_id,
                    exit_price=lot.exit_price,
                    exit_order_id=lot.exit_order_id,
                    status=LotStatus.CLOSED,
                )
            )

        new_exit_orders: list[ExitOrder] = []
        for eo in snap.exit_orders:
            if eo.status == ExitOrderStatus.OPEN:
                actions.append(
                    ActionIntent(
                        kind=ActionIntentKind.CANCEL_EXIT,
                        order_id=eo.exit_order_id,
                        reason="OPERATOR_CLEANUP",
                    )
                )
                new_exit_orders.append(
                    ExitOrder(
                        exit_order_id=eo.exit_order_id,
                        lot_id=eo.lot_id,
                        side=eo.side,
                        price=eo.price,
                        qty=eo.qty,
                        status=ExitOrderStatus.CANCELED,
                    )
                )
            else:
                new_exit_orders.append(eo)

        for p in snap.entry_window.buy_entry_prices:
            actions.append(
                ActionIntent(
                    kind=ActionIntentKind.CANCEL_ENTRY,
                    side=OrderSide.BUY,
                    price=p,
                    reason="OPERATOR_CLEANUP",
                )
            )
        for p in snap.entry_window.sell_entry_prices:
            actions.append(
                ActionIntent(
                    kind=ActionIntentKind.CANCEL_ENTRY,
                    side=OrderSide.SELL,
                    price=p,
                    reason="OPERATOR_CLEANUP",
                )
            )

        new_snapshot = GridV2Snapshot(
            mode=BranchMode.FLAT,
            entry_window=_empty_entry_window(snap.entry_window),
            open_lots=(),
            closed_lots=tuple(new_closed_lots),
            exit_orders=tuple(new_exit_orders),
            emergency_stopped=False,
            last_recenter_ts=snap.last_recenter_ts,
        )
        return self._commit(new_snapshot, tuple(actions))
