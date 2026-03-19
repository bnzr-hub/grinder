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

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def reconstruction_ok(self) -> bool:
        return self._reconstruction_ok

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

    def on_fill(
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
                    raise ValueError(
                        f"Exit CID not in open lot ledger: {client_order_id}"
                    ) from None
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
        parsed = self._adapter.parse_cid(client_order_id)
        if parsed is not None and parsed.kind == GridV2OrderKind.ENTRY:
            self._adapter.confirm_entry_fill(client_order_id)
        elif parsed is not None and parsed.kind == GridV2OrderKind.EXIT:
            self._adapter.confirm_exit_fill(client_order_id)

        # Step 4: resolve actions
        resolved = self._adapter.resolve_actions(result.actions, ts)

        # Step 5: convert to ExecutionActions
        exec_actions = self._to_execution_actions(resolved)

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
            resolved_actions=resolved,
            execution_actions=exec_actions,
            rejected=False,
            reject_reason=None,
        )

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
        """Convert ResolvedActions to ExecutionActions for the dispatch pipeline."""
        result: list[ExecutionAction] = []
        for ra in resolved:
            if ra.kind in (ActionIntentKind.PLACE_ENTRY, ActionIntentKind.PLACE_EXIT):
                result.append(
                    ExecutionAction(
                        action_type=ActionType.PLACE,
                        symbol=self._symbol,
                        side=ra.side,
                        price=self._quantize_price(ra.price, ra.side),
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
