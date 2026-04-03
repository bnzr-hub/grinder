"""Grid V2 runtime bridge (doc-27 section 23, PR4).

Wires GridV2Adapter + GridV2StateMachine into the live engine's
action generation phase. No direct exchange I/O — produces
ExecutionActions for the existing dispatch pipeline.

Binding runtime contract (strict order):
1. exchange fact received
2. adapter.translate_fill(...)
3. sm.apply(event)
4. only if not rejected: adapter.confirm_entry_fill / confirm_exit_fill
5. adapter.resolve_actions(result.actions, ts)
6. dispatch resolved actions
7. only after exchange cancel ack: adapter.confirm_cancel_entry/exit

No other code path may mutate adapter registry.
No dispatch before successful startup reconstruction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from grinder.execution.types import ActionType, ExecutionAction
from grinder.grid_v2.adapter import (
    GridV2Adapter,
    GridV2OrderKind,
    ResolvedAction,
    TranslatedFill,
)
from grinder.grid_v2.state import (
    ActionIntentKind,
    EntryFilled,
    ExitFilled,
    GridV2Config,
    GridV2StateMachine,
    RecenterRequested,
    TransitionResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal

    from grinder.core import OrderSide

logger = logging.getLogger(__name__)

# Stable reason codes for runtime signals.
RECONSTRUCTION_OK = "GRID_V2_RECONSTRUCTION_OK"
RECONSTRUCTION_FAILED = "GRID_V2_RECONSTRUCTION_FAILED"
BRIDGE_STARTUP_OK = "GRID_V2_BRIDGE_STARTUP_OK"
BRIDGE_FILL_PROCESSED = "GRID_V2_FILL_PROCESSED"
BRIDGE_FILL_REJECTED = "GRID_V2_FILL_REJECTED"
BRIDGE_FILL_FOREIGN = "GRID_V2_FILL_FOREIGN"
BRIDGE_EXIT_FILL_ORPHAN = "GRID_V2_EXIT_FILL_ORPHAN"
BRIDGE_FILL_DUPLICATE_SUPPRESSED = "GRID_V2_FILL_DUPLICATE_SUPPRESSED"
BRIDGE_DISPATCH_BLOCKED = "GRID_V2_DISPATCH_BLOCKED"


@dataclass(frozen=True)
class FillResult:
    """Result of processing a single exchange fill through the bridge."""

    translated: TranslatedFill | None
    transition: TransitionResult | None
    resolved_actions: tuple[ResolvedAction, ...]
    execution_actions: tuple[ExecutionAction, ...]
    rejected: bool
    reject_reason: str | None


@dataclass(frozen=True)
class CancelAckResult:
    """Result of processing a cancel acknowledgement."""

    cid: str
    kind: GridV2OrderKind
    removed: bool


class GridV2Bridge:
    """Runtime bridge between exchange facts and grid_v2 domain.

    Pure orchestration: wires adapter + state machine + action conversion.
    No direct exchange I/O. Produces ExecutionActions for the engine's
    existing dispatch pipeline.

    Symbol-scoped: one bridge per trading symbol.
    """

    def __init__(
        self,
        config: GridV2Config,
        symbol: str,
    ) -> None:
        self._config = config
        self._symbol = symbol
        self._adapter = GridV2Adapter(config, symbol)
        self._sm: GridV2StateMachine | None = None
        self._reconstruction_ok = False
        self._failed_reason: str | None = None
        # Recently processed EXIT fill CIDs for stale duplicate suppression.
        # Prevents poll+user-data duplicate races from surfacing as orphan/rejected noise.
        self._recent_exit_fills: dict[str, int] = {}

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def reconstruction_ok(self) -> bool:
        return self._reconstruction_ok

    @property
    def f2_protective_recovery(self) -> bool:
        """True if startup recovered from F2 (non-flat, no exits on exchange)."""
        return self._adapter.f2_protective_recovery

    @property
    def failed_reason(self) -> str | None:
        return self._failed_reason

    @property
    def adapter(self) -> GridV2Adapter:
        return self._adapter

    @property
    def state_machine(self) -> GridV2StateMachine | None:
        return self._sm

    # --- Startup reconstruction (23.1) ---

    def startup(
        self,
        exchange_open_orders: Sequence[tuple[str, OrderSide, Decimal, Decimal]],
        position_qty: Decimal,
        reference_price: Decimal,
        ts: int,
    ) -> bool:
        """Run startup reconstruction. Must succeed before any dispatch.

        Returns True on success, False on failure (fail-closed).
        On failure, sets failed_reason and blocks all dispatch.
        """
        try:
            snapshot = self._adapter.reconstruct_snapshot(
                exchange_open_orders,
                position_qty,
                reference_price,
                ts,
            )
            self._sm = GridV2StateMachine(self._config, snapshot)
            self._reconstruction_ok = True
            self._failed_reason = None
            logger.info(
                "%s symbol=%s mode=%s entries=%d exits=%d",
                RECONSTRUCTION_OK,
                self._symbol,
                snapshot.mode.value,
                self._adapter.registry.entry_count,
                self._adapter.registry.exit_count,
            )
            return True
        except ValueError as exc:
            self._reconstruction_ok = False
            self._failed_reason = str(exc)
            self._sm = None
            logger.error(
                "%s symbol=%s reason=%s",
                RECONSTRUCTION_FAILED,
                self._symbol,
                self._failed_reason,
            )
            return False

    def get_f2_protective_exit_actions(self, ts: int) -> tuple[ExecutionAction, ...]:
        """Generate PLACE_EXIT actions for F2 protective recovery.

        Called once after startup when f2_protective_recovery is True.
        Creates real CIDs, registers exits, and returns reduce-only PLACE actions.
        """
        if not self._adapter.f2_protective_recovery or self._sm is None:
            return ()

        result: list[ExecutionAction] = []
        for exit_order in self._sm.snapshot.exit_orders:
            cid = self._adapter.generate_exit_cid(ts)
            self._adapter.registry.register_exit(cid, exit_order.exit_order_id, exit_order.lot_id)
            result.append(
                ExecutionAction(
                    action_type=ActionType.PLACE,
                    symbol=self._symbol,
                    side=exit_order.side,
                    price=self._quantize_price(exit_order.price, exit_order.side),
                    quantity=exit_order.qty,
                    client_order_id=cid,
                    reason="grid_v2_F2_PROTECTIVE_EXIT",
                    reduce_only=True,
                )
            )
        return tuple(result)

    def startup_fresh(
        self,
        reference_price: Decimal,
        ts: int,
    ) -> tuple[ExecutionAction, ...]:
        """Start from scratch (no exchange orders). Creates initial entry window.

        Returns ExecutionActions for initial PLACE_ENTRY orders.
        """
        self._sm = GridV2StateMachine.create_initial(self._config, reference_price, ts)
        self._reconstruction_ok = True
        self._failed_reason = None
        seed_resolved = self._adapter.seed_entry_window(
            self._sm.snapshot.entry_window,
            ts,
        )
        logger.info(
            "%s symbol=%s mode=%s seed_count=%d",
            BRIDGE_STARTUP_OK,
            self._symbol,
            self._sm.snapshot.mode.value,
            len(seed_resolved),
        )
        return self._to_execution_actions(seed_resolved)

    def recenter_flat(self, reference_price: Decimal, ts: int) -> tuple[ExecutionAction, ...]:
        """Rebuild FLAT entry window around reference_price and emit actions."""
        self._assert_dispatch_ok()
        assert self._sm is not None

        result = self._sm.apply(RecenterRequested(new_reference_price=reference_price, ts=ts))
        if result.rejected:
            logger.warning(
                "GRID_V2_RECENTER_SKIPPED symbol=%s reason=%s",
                self._symbol,
                result.reject_reason,
            )
            return ()

        resolved = self._adapter.resolve_actions(result.actions, ts)
        logger.info("GRID_V2_RECENTER_OK symbol=%s actions=%d", self._symbol, len(resolved))
        return self._to_execution_actions(resolved)

    # --- Fill lifecycle (23.2) ---

    def on_fill(  # noqa: PLR0912
        self,
        client_order_id: str,
        side: OrderSide,
        price: Decimal,
        qty: Decimal,
        ts: int,
        allow_stale: bool = False,
    ) -> FillResult:
        """Process an exchange fill through the binding runtime contract.

        1. translate_fill → event
        2. sm.apply(event) → TransitionResult
        3. if not rejected: confirm_* + resolve_actions
        4. convert to ExecutionActions

        Returns FillResult with all intermediate state for observability.
        Raises RuntimeError if dispatch is blocked (reconstruction failed).
        """
        self._assert_dispatch_ok()

        assert self._sm is not None  # guaranteed by _assert_dispatch_ok
        parsed = self._adapter.parse_cid(client_order_id)

        # Stale duplicate EXIT fills can arrive via poll/user-data races after
        # successful processing removed CID from registry. Suppress those early.
        if (
            allow_stale
            and parsed is not None
            and parsed.kind == GridV2OrderKind.EXIT
            and self._is_recent_exit_fill(client_order_id, ts)
        ):
            logger.info(
                "%s symbol=%s cid=%s",
                BRIDGE_FILL_DUPLICATE_SUPPRESSED,
                self._symbol,
                client_order_id,
            )
            return FillResult(
                translated=None,
                transition=None,
                resolved_actions=(),
                execution_actions=(),
                rejected=False,
                reject_reason=None,
            )

        # Step 1: translate
        try:
            translated = self._adapter.translate_fill(client_order_id, side, price, qty, ts)
        except ValueError:
            if not allow_stale:
                raise
            parsed = self._adapter.parse_cid(client_order_id)
            if parsed is None:
                raise
            if parsed.kind == GridV2OrderKind.ENTRY:
                translated = TranslatedFill(
                    event=EntryFilled(
                        order_id=client_order_id,
                        side=side,
                        price=price,
                        qty=qty,
                        ts=ts,
                    ),
                    source_cid=client_order_id,
                )
            else:
                lot_id = None
                assert self._sm is not None
                for lot in self._sm.snapshot.open_lots:
                    if lot.exit_order_id == client_order_id:
                        lot_id = lot.lot_id
                        break
                if lot_id is None:
                    logger.warning(
                        "%s symbol=%s cid=%s",
                        BRIDGE_EXIT_FILL_ORPHAN,
                        self._symbol,
                        client_order_id,
                    )
                    return FillResult(
                        translated=None,
                        transition=None,
                        resolved_actions=(),
                        execution_actions=(),
                        rejected=True,
                        reject_reason="EXIT_LOT_NOT_FOUND",
                    )
                translated = TranslatedFill(
                    event=ExitFilled(
                        exit_order_id=client_order_id,
                        lot_id=lot_id,
                        price=price,
                        qty=qty,
                        ts=ts,
                    ),
                    source_cid=client_order_id,
                )
        if translated is None:
            logger.debug(
                "%s symbol=%s cid=%s",
                BRIDGE_FILL_FOREIGN,
                self._symbol,
                client_order_id,
            )
            return FillResult(
                translated=None,
                transition=None,
                resolved_actions=(),
                execution_actions=(),
                rejected=False,
                reject_reason=None,
            )

        # Step 2: apply to state machine
        result = self._sm.apply(translated.event)

        if result.rejected:
            logger.warning(
                "%s symbol=%s cid=%s reason=%s",
                BRIDGE_FILL_REJECTED,
                self._symbol,
                client_order_id,
                result.reject_reason,
            )
            return FillResult(
                translated=translated,
                transition=result,
                resolved_actions=(),
                execution_actions=(),
                rejected=True,
                reject_reason=result.reject_reason,
            )

        # Step 3: confirm (remove from registry)
        if parsed is not None and parsed.kind == GridV2OrderKind.ENTRY:
            self._adapter.confirm_entry_fill(client_order_id)
        elif parsed is not None and parsed.kind == GridV2OrderKind.EXIT:
            self._adapter.confirm_exit_fill(client_order_id)
            self._mark_recent_exit_fill(client_order_id, ts)

        # Step 4: resolve actions — fault-tolerant for orphan CANCELs.
        # If a CANCEL_ENTRY/CANCEL_EXIT can't find its CID (entry was
        # cleaned from registry before fill arrived), skip that CANCEL
        # but still resolve remaining PLACE actions. This prevents
        # phantom lots from surviving when follow-up exits/entries
        # would otherwise be dropped entirely.
        resolved = self._adapter.resolve_actions_partial(result.actions, ts)
        if resolved.skipped_count > 0:
            logger.warning(
                "GRID_V2_FILL_RESOLVE_PARTIAL symbol=%s cid=%s "
                "total=%d resolved=%d skipped=%d reasons=%s",
                self._symbol,
                client_order_id,
                len(result.actions),
                len(resolved.actions),
                resolved.skipped_count,
                "; ".join(resolved.skip_reasons),
            )

        # Step 5: convert to ExecutionActions
        exec_actions = self._to_execution_actions(resolved.actions)

        logger.info(
            "%s symbol=%s cid=%s actions=%d",
            BRIDGE_FILL_PROCESSED,
            self._symbol,
            client_order_id,
            len(exec_actions),
        )

        return FillResult(
            translated=translated,
            transition=result,
            resolved_actions=resolved.actions,
            execution_actions=exec_actions,
            rejected=False,
            reject_reason=None,
        )

    def _prune_recent_exit_fills(self, ts: int) -> None:
        """Bound memory for duplicate-suppression cache."""
        ttl_ms = 300_000  # 5 minutes
        stale = [cid for cid, seen_ts in self._recent_exit_fills.items() if ts - seen_ts > ttl_ms]
        for cid in stale:
            del self._recent_exit_fills[cid]

    def _is_recent_exit_fill(self, client_order_id: str, ts: int) -> bool:
        self._prune_recent_exit_fills(ts)
        return client_order_id in self._recent_exit_fills

    def _mark_recent_exit_fill(self, client_order_id: str, ts: int) -> None:
        self._recent_exit_fills[client_order_id] = ts
        self._prune_recent_exit_fills(ts)

    # --- Cancel ack lifecycle (23.3) ---

    def on_cancel_ack(self, client_order_id: str) -> CancelAckResult:
        """Process exchange cancel acknowledgement.

        Only called AFTER exchange confirms the cancel.
        Removes the CID from registry.
        """
        self._assert_dispatch_ok()

        parsed = self._adapter.parse_cid(client_order_id)
        if parsed is None:
            return CancelAckResult(cid=client_order_id, kind=GridV2OrderKind.ENTRY, removed=False)

        if parsed.kind == GridV2OrderKind.ENTRY:
            entry_reg = self._adapter.confirm_cancel_entry(client_order_id)
            return CancelAckResult(
                cid=client_order_id,
                kind=GridV2OrderKind.ENTRY,
                removed=entry_reg is not None,
            )
        else:
            exit_reg = self._adapter.confirm_cancel_exit(client_order_id)
            return CancelAckResult(
                cid=client_order_id,
                kind=GridV2OrderKind.EXIT,
                removed=exit_reg is not None,
            )

    # --- Reconciliation (23.4) ---

    def reconcile(
        self,
        exchange_open_cids: frozenset[str],
        ts: int,
    ) -> tuple[()]:
        """Run reconciliation check. Detection only, no mutation.

        Returns mismatches for logging/alerting.
        """
        self._assert_dispatch_ok()
        assert self._sm is not None
        result = self._adapter.reconcile(self._sm.snapshot, exchange_open_cids, ts)
        if result.mismatches:
            for m in result.mismatches:
                logger.warning(
                    "GRID_V2_MISMATCH symbol=%s kind=%s severity=%s detail=%s cid=%s",
                    self._symbol,
                    m.kind.value,
                    m.severity.value,
                    m.detail,
                    m.cid or "?",
                )
        return ()  # detection only, no actions

    # --- Dispatch guard (23.5) ---

    def _assert_dispatch_ok(self) -> None:
        """Block all operations if reconstruction has not succeeded."""
        if not self._reconstruction_ok:
            raise RuntimeError(
                f"{BRIDGE_DISPATCH_BLOCKED} symbol={self._symbol} "
                f"reason={self._failed_reason or 'startup not called'}"
            )

    # --- Action conversion ---

    def _quantize_price(self, price: Decimal, side: OrderSide | None) -> Decimal:
        """Quantize price to exchange tick size (PR6).

        Side-aware rounding:
        - BUY: ROUND_DOWN (less aggressive, safer entry)
        - SELL: ROUND_UP (less aggressive, safer exit)
        - None/unknown: ROUND_HALF_UP (fallback)
        """
        from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP  # noqa: PLC0415
        from decimal import Decimal as _D  # noqa: PLC0415

        tick = self._config.price_tick_size
        if side is not None and side.value == "BUY":
            rounding = ROUND_DOWN
        elif side is not None and side.value == "SELL":
            rounding = ROUND_UP
        else:
            rounding = ROUND_HALF_UP
        return (price / tick).quantize(_D("1"), rounding=rounding) * tick

    def _to_execution_actions(
        self,
        resolved: tuple[ResolvedAction, ...] | Sequence[ResolvedAction],
    ) -> tuple[ExecutionAction, ...]:
        """Convert ResolvedActions to ExecutionActions for the dispatch pipeline.

        Entries whose quantized notional is below min_notional are skipped
        to prevent the pre-send gate from blocking them downstream.
        """
        min_notional = self._config.min_notional
        result: list[ExecutionAction] = []
        for ra in resolved:
            if ra.kind in (ActionIntentKind.PLACE_ENTRY, ActionIntentKind.PLACE_EXIT):
                quantized_price = self._quantize_price(ra.price, ra.side)
                # Skip entries that would violate min_notional at the actual
                # rounded price. Exits are always allowed (reduce-only).
                if (
                    ra.kind == ActionIntentKind.PLACE_ENTRY
                    and min_notional > 0
                    and ra.qty is not None
                    and quantized_price * ra.qty < min_notional
                ):
                    # Remove CID from adapter registry — it was registered
                    # during resolve but will never be dispatched to exchange.
                    if ra.cid:
                        self._adapter.confirm_cancel_entry(ra.cid)
                    logger.info(
                        "GRID_V2_ENTRY_BELOW_MIN_NOTIONAL symbol=%s cid=%s "
                        "price=%s qty=%s notional=%s min=%s — skipped+unregistered",
                        self._symbol,
                        ra.cid,
                        quantized_price,
                        ra.qty,
                        quantized_price * ra.qty,
                        min_notional,
                    )
                    continue
                result.append(
                    ExecutionAction(
                        action_type=ActionType.PLACE,
                        symbol=self._symbol,
                        side=ra.side,
                        price=quantized_price,
                        quantity=ra.qty,
                        client_order_id=ra.cid,
                        reason=f"grid_v2_{ra.kind.value}",
                        reduce_only=ra.kind == ActionIntentKind.PLACE_EXIT,
                    )
                )
            elif ra.kind in (ActionIntentKind.CANCEL_ENTRY, ActionIntentKind.CANCEL_EXIT):
                result.append(
                    ExecutionAction(
                        action_type=ActionType.CANCEL,
                        order_id=ra.cid,
                        symbol=self._symbol,
                        reason=f"grid_v2_{ra.kind.value}",
                    )
                )
        return tuple(result)
