"""Grid V2 shadow runner (doc-27 section 24, PR5).

Runs grid_v2 computation in shadow mode alongside the legacy engine.
Shadow path is fully isolated: owns its own GridV2Bridge instance,
never injects actions into the live dispatch pipeline.

Purpose: compare grid_v2 action intents vs legacy planner output
to validate behavior before PR6 live ceremony.

Invariants:
- Shadow bridge is a separate instance (not shared with engine primary bridge).
- Shadow never produces actions that reach dispatch.
- Shadow failures do not affect the live engine path.
- All shadow output is observability-only (logs + artifacts).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from grinder.grid_v2.bridge import GridV2Bridge

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal

    from grinder.core import OrderSide
    from grinder.execution.types import ExecutionAction
    from grinder.grid_v2.state import GridV2Config

logger = logging.getLogger(__name__)

# Stable log reason codes for shadow events.
SHADOW_STARTUP_OK = "GRID_V2_SHADOW_STARTUP_OK"
SHADOW_STARTUP_FAILED = "GRID_V2_SHADOW_STARTUP_FAILED"
SHADOW_STARTUP_BLOCKED = "GRID_V2_SHADOW_STARTUP_BLOCKED"
SHADOW_TICK = "GRID_V2_SHADOW_TICK"
SHADOW_DIVERGENCE = "GRID_V2_SHADOW_DIVERGENCE"
SHADOW_ERROR = "GRID_V2_SHADOW_ERROR"


@dataclass(frozen=True)
class ShadowActionSummary:
    """Summary of a single shadow action intent (no CIDs, no dispatch info)."""

    action_type: str  # PLACE / CANCEL
    side: str | None  # BUY / SELL / None
    reason: str


@dataclass(frozen=True)
class ShadowResult:
    """Result of one shadow tick. Observability-only, never dispatched."""

    shadow_started: bool
    shadow_active: bool
    shadow_action_count: int
    shadow_actions: tuple[ShadowActionSummary, ...]
    legacy_action_count: int
    divergence: str  # "none" | "count_mismatch" | "type_mismatch" | "shadow_blocked"
    detail: str  # human-readable divergence detail

    def to_dict(self) -> dict[str, object]:
        return {
            "shadow_started": self.shadow_started,
            "shadow_active": self.shadow_active,
            "shadow_action_count": self.shadow_action_count,
            "shadow_actions": [
                {"action_type": a.action_type, "side": a.side, "reason": a.reason}
                for a in self.shadow_actions
            ],
            "legacy_action_count": self.legacy_action_count,
            "divergence": self.divergence,
            "detail": self.detail,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


_BLOCKED_RESULT = ShadowResult(
    shadow_started=False,
    shadow_active=False,
    shadow_action_count=0,
    shadow_actions=(),
    legacy_action_count=0,
    divergence="shadow_blocked",
    detail="shadow startup not complete or failed",
)


class GridV2ShadowRunner:
    """Shadow-mode runner for grid_v2 alongside legacy engine.

    Owns an isolated GridV2Bridge. Processes the same snapshots as the
    live engine but never produces actions for dispatch.

    Lifecycle:
    1. Constructed with same config as primary bridge.
    2. on_snapshot() called after legacy planner produces actions.
    3. Shadow computes what grid_v2 WOULD do.
    4. Returns ShadowResult for logging/artifacts. Never dispatched.

    Fail-open for the live path: any shadow error is caught and logged,
    never propagated to caller.
    """

    def __init__(self, config: GridV2Config, symbol: str) -> None:
        self._bridge = GridV2Bridge(config, symbol)
        self._symbol = symbol
        self._started = False
        self._startup_tick = False  # True on the tick that startup_fresh() ran
        self._tick_count = 0

    @property
    def bridge(self) -> GridV2Bridge:
        """Expose bridge for test assertions only. Not for dispatch."""
        return self._bridge

    @property
    def started(self) -> bool:
        return self._started

    @property
    def tick_count(self) -> int:
        return self._tick_count

    def try_startup(
        self,
        g_orders: Sequence[tuple[str, OrderSide, Decimal, Decimal]],
        pos_qty: Decimal,
        reference_price: Decimal,
        ts: int,
    ) -> bool:
        """Attempt shadow startup. Same logic as primary bridge."""
        if self._started:
            return self._bridge.reconstruction_ok

        if g_orders:
            ok = self._bridge.startup(list(g_orders), pos_qty, reference_price, ts)
            if ok:
                logger.info(
                    "%s symbol=%s mode=reconstruct",
                    SHADOW_STARTUP_OK,
                    self._symbol,
                )
            else:
                logger.warning(
                    "%s symbol=%s reason=%s",
                    SHADOW_STARTUP_FAILED,
                    self._symbol,
                    self._bridge.failed_reason,
                )
            self._started = True
            return ok
        elif pos_qty != 0:
            logger.warning(
                "%s symbol=%s reason=non_flat_no_orders pos_qty=%s",
                SHADOW_STARTUP_BLOCKED,
                self._symbol,
                pos_qty,
            )
            self._started = True
            return False
        else:
            self._bridge.startup_fresh(reference_price, ts)
            logger.info(
                "%s symbol=%s mode=fresh",
                SHADOW_STARTUP_OK,
                self._symbol,
            )
            self._started = True
            self._startup_tick = True  # skip fill detection this tick
            return True

    def on_snapshot(
        self,
        legacy_actions: Sequence[ExecutionAction],
        exchange_cids: frozenset[str],
        pending_cancel_cids: frozenset[str],
        ts: int,
    ) -> ShadowResult:
        """Run shadow tick. Compare grid_v2 output with legacy actions.

        This method NEVER raises — all errors are caught and logged.
        Returns a ShadowResult for observability.

        Args:
            legacy_actions: Actions the live engine produced this tick.
            exchange_cids: Current grid_v2 CIDs on exchange.
            pending_cancel_cids: CIDs with pending cancel dispatched.
            ts: Tick timestamp.
        """
        self._tick_count += 1

        try:
            return self._on_snapshot_inner(legacy_actions, exchange_cids, pending_cancel_cids, ts)
        except Exception:
            logger.exception(
                "%s symbol=%s tick=%d",
                SHADOW_ERROR,
                self._symbol,
                self._tick_count,
            )
            return ShadowResult(
                shadow_started=self._started,
                shadow_active=False,
                shadow_action_count=0,
                shadow_actions=(),
                legacy_action_count=len(legacy_actions),
                divergence="shadow_blocked",
                detail="shadow error (see logs)",
            )

    def _on_snapshot_inner(
        self,
        legacy_actions: Sequence[ExecutionAction],
        exchange_cids: frozenset[str],
        pending_cancel_cids: frozenset[str],
        ts: int,
    ) -> ShadowResult:
        """Inner implementation (may raise)."""
        if not self._started or not self._bridge.reconstruction_ok:
            return ShadowResult(
                shadow_started=self._started,
                shadow_active=False,
                shadow_action_count=0,
                shadow_actions=(),
                legacy_action_count=len(legacy_actions),
                divergence="shadow_blocked",
                detail="shadow startup not complete or failed",
            )

        shadow_actions: list[ShadowActionSummary] = []

        # Startup-tick skip: after startup_fresh(), seeded CIDs are not on
        # exchange yet. Registry-vs-exchange diff would flag all seeds as
        # "disappeared" → false fills. Skip fill detection for exactly 1 tick.
        if self._startup_tick:
            self._startup_tick = False
            return ShadowResult(
                shadow_started=True,
                shadow_active=True,
                shadow_action_count=0,
                shadow_actions=(),
                legacy_action_count=len(legacy_actions),
                divergence="none",
                detail="startup_tick_skip",
            )

        # Shadow fill detection: same algorithm as engine but read-only.
        # Compare registry CIDs vs exchange CIDs.
        registry_cids = set(self._bridge.adapter.registry.all_entry_cids) | set(
            self._bridge.adapter.registry.all_exit_cids
        )
        disappeared = registry_cids - exchange_cids
        filled_cids = disappeared - set(pending_cancel_cids)

        # Process detected fills through shadow bridge (mutates shadow state only).
        for cid in sorted(filled_cids):
            entry_reg = self._bridge.adapter.registry.lookup_entry(cid)
            if entry_reg is not None:
                result = self._bridge.on_fill(
                    cid,
                    entry_reg.side,
                    entry_reg.price,
                    self._bridge._config.order_size,
                    ts,
                )
                for ea in result.execution_actions:
                    shadow_actions.append(
                        ShadowActionSummary(
                            action_type=ea.action_type.value,
                            side=ea.side.value if ea.side else None,
                            reason=ea.reason,
                        )
                    )
                continue

            exit_reg = self._bridge.adapter.registry.lookup_exit(cid)
            if exit_reg is None:
                continue

            # Infer exit side from shadow lot ledger
            sm = self._bridge.state_machine
            side: OrderSide | None = None
            if sm is not None:
                from grinder.core import OrderSide as _OS  # noqa: PLC0415
                from grinder.grid_v2.state import LotSide  # noqa: PLC0415

                for lot in sm.snapshot.open_lots:
                    if lot.exit_order_id == exit_reg.exit_order_id:
                        side = _OS.SELL if lot.side == LotSide.LONG else _OS.BUY
                        break
            if side is None:
                from grinder.core import OrderSide as _OS  # noqa: PLC0415

                side = _OS.SELL

            from decimal import Decimal as _D  # noqa: PLC0415

            result = self._bridge.on_fill(cid, side, _D("0"), self._bridge._config.order_size, ts)
            for ea in result.execution_actions:
                shadow_actions.append(
                    ShadowActionSummary(
                        action_type=ea.action_type.value,
                        side=ea.side.value if ea.side else None,
                        reason=ea.reason,
                    )
                )

        # Cancel-ack detection (read-only for shadow pending cancels — not tracked)
        # Shadow doesn't track its own pending cancels because it never dispatches.

        shadow_tuple = tuple(shadow_actions)

        # Compute divergence
        legacy_count = len(legacy_actions)
        shadow_count = len(shadow_tuple)
        divergence, detail = _compute_divergence(
            shadow_tuple, legacy_actions, shadow_count, legacy_count
        )

        result_obj = ShadowResult(
            shadow_started=True,
            shadow_active=True,
            shadow_action_count=shadow_count,
            shadow_actions=shadow_tuple,
            legacy_action_count=legacy_count,
            divergence=divergence,
            detail=detail,
        )

        logger.info(
            "%s symbol=%s tick=%d shadow=%d legacy=%d divergence=%s",
            SHADOW_TICK,
            self._symbol,
            self._tick_count,
            shadow_count,
            legacy_count,
            divergence,
        )

        if divergence != "none":
            logger.info(
                "%s symbol=%s detail=%s",
                SHADOW_DIVERGENCE,
                self._symbol,
                detail,
            )

        return result_obj


def _extract_legacy_action_type(action: object) -> str:
    """Extract action_type string from ExecutionAction or dict.

    Engine raw_actions can be either ExecutionAction objects or dicts
    (from PaperEngine). Handle both gracefully.
    """
    if isinstance(action, dict):
        return str(action.get("action_type", "UNKNOWN"))
    # ExecutionAction: action_type is an ActionType enum
    at = getattr(action, "action_type", None)
    if at is not None and hasattr(at, "value"):
        return str(at.value)
    return "UNKNOWN"


def _compute_divergence(
    shadow_actions: tuple[ShadowActionSummary, ...],
    legacy_actions: Sequence[ExecutionAction],
    shadow_count: int,
    legacy_count: int,
) -> tuple[str, str]:
    """Compare shadow vs legacy action intents. Returns (divergence_type, detail)."""
    if shadow_count == 0 and legacy_count == 0:
        return "none", "both empty"

    if shadow_count != legacy_count:
        return (
            "count_mismatch",
            f"shadow={shadow_count} legacy={legacy_count}",
        )

    # Compare action types (sorted for determinism)
    shadow_types = sorted(a.action_type for a in shadow_actions)
    legacy_types = sorted(_extract_legacy_action_type(a) for a in legacy_actions)

    if shadow_types != legacy_types:
        return (
            "type_mismatch",
            f"shadow_types={shadow_types} legacy_types={legacy_types}",
        )

    return "none", f"match count={shadow_count}"
