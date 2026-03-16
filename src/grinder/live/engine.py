"""LiveEngineV0: Live write-path wiring from PaperEngine to ExchangePort.

This module provides the integration point for live trading:
- Wraps PaperEngine for decision-making (grid plan → actions)
- Applies safety gates (arming, mode, kill-switch, symbol whitelist)
- Translates actions to intents for DrawdownGuardV1
- Executes orders via ExchangePort (with H3/H4 wrappers)

Key design (ADR-036):
    1. By default nothing writes (armed=False)
    2. Kill-switch blocks PLACE/REPLACE but allows CANCEL
    3. DrawdownGuardV1 blocks INCREASE_RISK in DRAWDOWN state
    4. Idempotency key created BEFORE retries (H3)
    5. Circuit breaker fast-fails degraded upstream (H4)

Usage:
    paper_engine = PaperEngine(...)
    port = IdempotentExchangePort(
        inner=BinanceExchangePort(...),
        breaker=CircuitBreaker(...),
    )
    live_engine = LiveEngineV0(paper_engine, port, config)

    output = live_engine.process_snapshot(snapshot)
    # output.live_actions contains execution results

See: ADR-036 for design decisions
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

from grinder.account.evidence import write_evidence_bundle
from grinder.account.syncer import AccountSyncer
from grinder.connectors.errors import (
    CircuitOpenError,
    ConnectorError,
    ConnectorNonRetryableError,
    ConnectorTransientError,
)
from grinder.connectors.live_connector import SafeMode
from grinder.connectors.retries import RetryPolicy, is_retryable
from grinder.core import OrderSide, SystemState
from grinder.env_parse import parse_bool, parse_csv, parse_enum, parse_int
from grinder.execution.fill_prob_evidence import maybe_emit_fill_prob_evidence
from grinder.execution.fill_prob_gate import (
    FillProbCircuitBreaker,
    FillProbVerdict,
    check_fill_prob,
)
from grinder.execution.smart_order_router import (
    ExchangeFilters,
    MarketSnapshot,
    RouterDecision,
    RouterInputs,
    route,
)
from grinder.execution.smart_order_router import (
    OrderIntent as SorOrderIntent,
)
from grinder.execution.sor_metrics import get_sor_metrics
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.grid_planner import GridPlanResult
from grinder.live.live_metrics import get_live_engine_metrics
from grinder.live.place_tracker import correlate_recent_places
from grinder.ml.fill_model_loader import extract_online_features
from grinder.ml.threshold_resolver import (
    resolve_threshold_result,
    write_threshold_resolution_evidence,
)
from grinder.reconcile.identity import (
    DEFAULT_PREFIX,
    DEFAULT_STRATEGY_ID,
    OrderIdentityConfig,
    generate_client_order_id,
    is_tp_order,
    parse_client_order_id,
)
from grinder.risk.drawdown_guard_v1 import DrawdownGuardV1
from grinder.risk.drawdown_guard_v1 import OrderIntent as RiskIntent
from grinder.risk.emergency_exit import EmergencyExitExecutor
from grinder.risk.emergency_exit_metrics import get_emergency_exit_metrics

if TYPE_CHECKING:
    from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
    from grinder.contracts import Snapshot
    from grinder.execution.port import ExchangePort
    from grinder.features.engine import FeatureEngine
    from grinder.features.types import FeatureSnapshot
    from grinder.gating.toxicity_gate import ToxicityGate
    from grinder.live.config import LiveEngineConfig
    from grinder.live.cycle_layer import LiveCycleLayerV1
    from grinder.live.fsm_driver import FsmDriver
    from grinder.live.grid_planner import LiveGridPlannerV1
    from grinder.ml.fill_model_v0 import FillModelV0
    from grinder.paper.engine import PaperEngine

logger = logging.getLogger(__name__)

# PR-P0-TP-CLOSE-ATOMIC: Binance error code parser for retry decisions.
# Expected format: "Binance error {code}: {msg}" (from binance_port.py map_binance_error).
_BINANCE_ERROR_RE = re.compile(r"Binance error (-?\d+):")

# Only -4118 (ReduceOnly Order Failed) is retryable for TP_CLOSE.
# Temporary conflict from race-duplicate orders that resolves after account sync.
_TP_CLOSE_RETRYABLE_CODES = frozenset({-4118})

_TP_CLOSE_MAX_RETRIES = 3  # 3 retry attempts AFTER initial failure
_TP_CLOSE_RETRY_COOLDOWN_MS = 10_000  # 10s between retry attempts

# PR-P0-RACE-1: Convergence guard constants
_CONVERGENCE_TIMEOUT_MS = 30_000  # 30s safety valve for inflight latch


@dataclass
class _InflightShift:
    """Tracks a dispatched grid shift awaiting AccountSync convergence."""

    sync_gen: int  # _account_sync_generation at dispatch time
    place_count: int  # PLACEs dispatched
    ts_ms: int  # wall-clock for timeout


@dataclass(frozen=True)
class _InflightPlacedOrder:
    """Tracks a dispatched PLACE awaiting first REST sync confirmation (ADR-090)."""

    symbol: str
    side: str  # "BUY" or "SELL"
    sync_gen: int  # _account_sync_generation at dispatch time


def _extract_binance_error_code(error: str | None) -> int | None:
    """Extract numeric error code from Binance error message.

    Expected format: "Binance error {code}: {msg}" (from binance_port.py:250).
    Returns None if format doesn't match.
    """
    if error is None:
        return None
    m = _BINANCE_ERROR_RE.search(error)
    return int(m.group(1)) if m else None


class BlockReason(Enum):
    """Reason why an action was blocked at engine level."""

    NOT_ARMED = "NOT_ARMED"
    MODE_NOT_LIVE_TRADE = "MODE_NOT_LIVE_TRADE"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    SYMBOL_NOT_WHITELISTED = "SYMBOL_NOT_WHITELISTED"
    DRAWDOWN_BLOCKED = "DRAWDOWN_BLOCKED"
    CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"
    MAX_RETRIES_EXCEEDED = "MAX_RETRIES_EXCEEDED"
    NON_RETRYABLE_ERROR = "NON_RETRYABLE_ERROR"
    FSM_STATE_BLOCKED = "FSM_STATE_BLOCKED"
    ROUTER_BLOCKED = "ROUTER_BLOCKED"
    FILL_PROB_LOW = "FILL_PROB_LOW"
    MAX_POSITION_EXCEEDED = "MAX_POSITION_EXCEEDED"
    TP_RENEW_PLACE_FAILED = "TP_RENEW_PLACE_FAILED"
    TP_CLOSE_PLACE_FAILED = "TP_CLOSE_PLACE_FAILED"
    CANCEL_ALREADY_FAILED = "CANCEL_ALREADY_FAILED"


class LiveActionStatus(Enum):
    """Status of a live action execution."""

    EXECUTED = "EXECUTED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass
class LiveAction:
    """Result of attempting to execute an action on live exchange.

    Attributes:
        action: Original ExecutionAction from PaperEngine
        status: Execution status (EXECUTED/BLOCKED/SKIPPED/FAILED)
        block_reason: Why action was blocked (if status=BLOCKED)
        order_id: Exchange order ID (if EXECUTED)
        error: Error message (if FAILED)
        attempts: Number of attempts made
        intent: Risk intent classification (INCREASE_RISK/REDUCE_RISK/CANCEL)
    """

    action: ExecutionAction
    status: LiveActionStatus
    block_reason: BlockReason | None = None
    order_id: str | None = None
    error: str | None = None
    attempts: int = 1
    intent: RiskIntent | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "action": self.action.to_dict(),
            "status": self.status.value,
            "block_reason": self.block_reason.value if self.block_reason else None,
            "order_id": self.order_id,
            "error": self.error,
            "attempts": self.attempts,
            "intent": self.intent.value if self.intent else None,
        }


@dataclass
class LiveEngineOutput:
    """Output from LiveEngineV0.process_snapshot().

    Extends PaperOutput with live execution results.

    Attributes:
        paper_output: Original output from PaperEngine
        live_actions: List of LiveAction results
        armed: Whether engine was armed
        mode: SafeMode at time of processing
        kill_switch_active: Whether kill-switch was active
    """

    paper_output: Any  # PaperOutput
    live_actions: list[LiveAction] = field(default_factory=list)
    armed: bool = False
    mode: SafeMode = SafeMode.READ_ONLY
    kill_switch_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "paper_output": self.paper_output.to_dict()
            if hasattr(self.paper_output, "to_dict")
            else str(self.paper_output),
            "live_actions": [a.to_dict() for a in self.live_actions],
            "armed": self.armed,
            "mode": self.mode.value,
            "kill_switch_active": self.kill_switch_active,
        }


# PR-338: FSM states where paper engine evaluation is deferred.
# In INIT/READY, paper engine would mutate internal state via NoOp port,
# creating ghost orders that freeze reconciliation after ACTIVE transition.
# Post-ACTIVE states (PAUSED/THROTTLED/etc) are handled by Gate 7.
_FSM_DEFER_STATES = frozenset({SystemState.INIT, SystemState.READY})


@dataclass
class _DeferredPaperOutput:
    """Minimal paper output for FSM-deferred ticks (no state mutation)."""

    ts: int
    symbol: str
    actions: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for LiveEngineOutput compatibility."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "actions": [a.to_dict() if hasattr(a, "to_dict") else a for a in self.actions],
        }


def classify_intent(
    action: ExecutionAction,
    pos_sign: int | None = None,
) -> RiskIntent:
    """Classify execution action into risk intent (PR-INV-1: position-aware).

    Mapping:
        CANCEL → CANCEL (always allowed)
        NOOP → CANCEL (no action, treated as safe)
        PLACE/REPLACE with reduce_only=True → REDUCE_RISK (PR-P0-REDUCEONLY-INTENT)
        PLACE/REPLACE with pos_sign:
            pos_sign=+1 (LONG) + SELL → REDUCE_RISK
            pos_sign=-1 (SHORT) + BUY → REDUCE_RISK
            pos_sign=None (unknown/BOTH) → INCREASE_RISK (fail-closed)
            Otherwise → INCREASE_RISK

    Args:
        action: ExecutionAction from PaperEngine or LiveGridPlanner.
        pos_sign: +1 if net LONG, -1 if net SHORT, None if unknown/BOTH.
            None triggers fail-closed conservative behavior.

    Returns:
        RiskIntent for DrawdownGuardV1 / FSM evaluation.
    """
    if action.action_type == ActionType.CANCEL:
        return RiskIntent.CANCEL
    elif action.action_type == ActionType.NOOP:
        return RiskIntent.CANCEL  # NOOP is safe, treat as CANCEL
    else:
        # PR-P0-REDUCEONLY-INTENT: reduce_only=True always = REDUCE_RISK.
        # Exchange enforces reduce-only server-side, so this is safe regardless
        # of pos_sign (even None/unknown).
        if action.reduce_only:
            return RiskIntent.REDUCE_RISK
        # PLACE and REPLACE: check if this would reduce existing position
        if pos_sign is not None and action.side is not None:
            if pos_sign > 0 and action.side == OrderSide.SELL:
                return RiskIntent.REDUCE_RISK
            if pos_sign < 0 and action.side == OrderSide.BUY:
                return RiskIntent.REDUCE_RISK
        # Default: conservative — all PLACE/REPLACE = INCREASE_RISK
        return RiskIntent.INCREASE_RISK


class LiveEngineV0:
    """Live write-path engine wiring PaperEngine to real ExchangePort.

    This class provides the integration point for live trading:
    1. Calls PaperEngine.process_snapshot() to get trading decisions
    2. Applies safety gates (arming, mode, kill-switch, whitelist)
    3. Checks DrawdownGuardV1 for intent-based blocking
    4. Executes allowed actions via ExchangePort (with retries)

    Thread safety: NOT thread-safe. Use one instance per symbol/stream.

    Args:
        paper_engine: PaperEngine for decision-making
        exchange_port: ExchangePort (ideally wrapped with IdempotentExchangePort)
        config: LiveEngineConfig with safety settings
        drawdown_guard: Optional DrawdownGuardV1 for DD-based blocking
        retry_policy: Optional RetryPolicy for transient error retries
    """

    def __init__(  # noqa: PLR0915
        self,
        paper_engine: PaperEngine,
        exchange_port: ExchangePort,
        config: LiveEngineConfig,
        drawdown_guard: DrawdownGuardV1 | None = None,
        retry_policy: RetryPolicy | None = None,
        fsm_driver: FsmDriver | None = None,
        exchange_filters: ExchangeFilters | None = None,
        account_syncer: AccountSyncer | None = None,
        fill_model: FillModelV0 | None = None,
        toxicity_gate: ToxicityGate | None = None,
        feature_engine: FeatureEngine | None = None,
        grid_planners: dict[str, LiveGridPlannerV1] | None = None,
        cycle_layer: LiveCycleLayerV1 | None = None,
    ) -> None:
        """Initialize LiveEngineV0.

        Args:
            paper_engine: Paper engine for grid plan generation
            exchange_port: Exchange port for order execution
            config: Engine configuration (arming, mode, kill-switch)
            drawdown_guard: Optional drawdown guard for intent blocking
            retry_policy: Optional retry policy for transient errors
            fsm_driver: Optional FSM driver for state-based intent gating (Launch-13)
            exchange_filters: Optional exchange filters for SOR (Launch-14)
            account_syncer: Optional account syncer for position/order sync (Launch-15)
            fill_model: Optional FillModelV0 for fill probability gating (PR-C5)
            toxicity_gate: Optional ToxicityGate for toxicity signal (PR-A1)
            feature_engine: Optional FeatureEngine for NATR/volatility features (PR-L0)
            grid_planners: Per-symbol grid planners for live mode (PR-L2). None = disabled.
            cycle_layer: Optional LiveCycleLayerV1 for TP generation (PR-INV-3). None = disabled.
        """
        self._paper_engine = paper_engine
        self._exchange_port = exchange_port
        self._config = config
        self._drawdown_guard = drawdown_guard
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=3)
        self._fsm_driver = fsm_driver
        self._exchange_filters = exchange_filters
        self._account_syncer = account_syncer
        self._fill_model = fill_model
        self._toxicity_gate = toxicity_gate
        self._feature_engine = feature_engine
        self._last_feature_snapshot: FeatureSnapshot | None = None
        self._last_snapshot: Snapshot | None = None
        self._grid_planners = grid_planners
        self._cycle_layer = cycle_layer
        self._last_account_snapshot: AccountSnapshot | None = None
        # Read GRINDER_LIVE_PLANNER_ENABLED once at init (PR-L2)
        self._live_planner_env_override = parse_bool(
            "GRINDER_LIVE_PLANNER_ENABLED", default=False, strict=False
        )
        self._warned_live_planner_no_sync = False
        # Read GRINDER_LIVE_CYCLE_ENABLED once at init (PR-INV-3)
        self._live_cycle_env_override = parse_bool(
            "GRINDER_LIVE_CYCLE_ENABLED", default=False, strict=False
        )
        # Per-symbol feed staleness tracking (ms timestamps, PR-A1)
        self._prev_snapshot_ts: dict[str, int] = {}
        # Read GRINDER_SOR_ENABLED once at init (via env_parse SSOT)
        self._sor_env_override = parse_bool("GRINDER_SOR_ENABLED", default=False, strict=False)
        # Read GRINDER_ACCOUNT_SYNC_ENABLED once at init (Launch-15)
        self._account_sync_env_override = parse_bool(
            "GRINDER_ACCOUNT_SYNC_ENABLED", default=False, strict=False
        )
        # Read fill prob gate env vars once at init (PR-C5)
        self._fill_prob_enforce = parse_bool(
            "GRINDER_FILL_MODEL_ENFORCE", default=False, strict=False
        )
        self._fill_prob_min_bps: int = (
            parse_int(
                "GRINDER_FILL_PROB_MIN_BPS",
                default=2500,
                min_value=0,
                max_value=10000,
                strict=False,
            )
            or 2500
        )
        # Circuit breaker: trips when block rate exceeds threshold (PR-C8, ADR-073)
        self._fill_prob_cb = FillProbCircuitBreaker()
        # Symbol allowlist for canary rollout (PR-C2): uppercase-normalized
        raw_allowlist = parse_csv("GRINDER_FILL_PROB_ENFORCE_SYMBOLS")
        self._fill_prob_enforce_symbols: frozenset[str] | None = (
            frozenset(s.upper() for s in raw_allowlist) if raw_allowlist else None
        )
        # Set enforce_enabled metric at init (always emitted, default 0)
        sor_metrics = get_sor_metrics()
        sor_metrics.set_fill_prob_enforce_enabled(self._fill_prob_enforce)
        sor_metrics.set_fill_prob_enforce_allowlist_enabled(
            self._fill_prob_enforce_symbols is not None
        )

        # Auto-threshold resolution from eval report (PR-C9, ADR-074)
        self._resolve_auto_threshold()

        # PR-C4: Signal that engine init completed (observable via /metrics)
        sor_metrics.set_engine_initialized()

        # RISK-EE-1: Emergency exit (safe-by-default, opt-in)
        self._emergency_exit_enabled = parse_bool(
            "GRINDER_EMERGENCY_EXIT_ENABLED", default=False, strict=False
        )
        self._emergency_exit_executor: EmergencyExitExecutor | None = None
        self._emergency_exit_executed = False
        self._position_notional_usd: float | None = None  # measured by AccountSyncer
        # Account sync throttle: at most once per interval to avoid REST rate-limits
        self._account_sync_interval_ms: int = 5_000  # 5s default
        self._account_sync_last_attempt_ms: int = -(5_000)  # ensures first tick always syncs
        # P0-2: debug open orders + recent places correlation
        self._debug_open_orders = parse_bool(
            "GRINDER_ACCOUNT_SYNC_DEBUG_OPEN_ORDERS", default=False, strict=False
        )
        self._recent_places: deque[tuple[str, int, str]] = deque(maxlen=20)
        # P0-2b: debug order lookup for missing openOrders
        self._looked_up_ids: set[str] = set()
        self._prev_open_orders_count: int = -1
        self._debug_lookup_limit = (
            parse_int(
                "GRINDER_ACCOUNT_SYNC_DEBUG_LOOKUP_LIMIT",
                default=5,
                strict=False,
            )
            or 5
        )
        # Freeze grid when in position (pos != 0) — prevents GRID_SHIFT churn
        self._freeze_grid_in_position = parse_bool(
            "GRINDER_LIVE_FREEZE_GRID_WHEN_IN_POSITION", default=False, strict=False
        )
        # Anti-churn: min mid move (bps) before GRID_SHIFT allowed
        self._grid_shift_min_move_bps = (
            parse_int(
                "GRINDER_LIVE_GRID_SHIFT_MIN_MOVE_BPS",
                default=0,
                strict=False,
            )
            or 0
        )
        self._grid_anchor_mid: dict[str, Decimal] = {}  # per-symbol anchor
        self._was_grid_frozen: dict[str, bool] = {}  # per-symbol freeze state tracker
        # Replenish-on-TP-fill: add BUY below + SELL above when TP fills
        self._replenish_on_tp_fill = parse_bool(
            "GRINDER_LIVE_REPLENISH_ON_TP_FILL", default=False, strict=False
        )
        self._prev_pos_qty: dict[str, Decimal] = {}  # per-symbol previous pos qty
        self._grid_anchor_low_buy: dict[str, Decimal] = {}  # per-symbol lowest BUY price
        self._grid_anchor_high_sell: dict[str, Decimal] = {}  # per-symbol highest SELL price
        self._tp_fill_replenish_seq = 0
        # PR-ROLL-1b: reduce-only enforcement toggle (default=ON for safety)
        self._reduce_only_enforcement = parse_bool(
            "GRINDER_LIVE_REDUCE_ONLY_ENFORCEMENT", default=True, strict=False
        )
        # Order budget exhaustion latch: suppress planner when port is dead
        self._order_budget_exhausted = False
        # PR-P0-TP-CLOSE-ATOMIC: retry queue for failed TP_CLOSE PLACEs
        # key: correlation_id, value: (action, retry_count, last_attempt_ts_ms)
        # retry_count 0 = enqueued (not yet retried), exhausted at >= _TP_CLOSE_MAX_RETRIES
        self._tp_close_retries: dict[str, tuple[ExecutionAction, int, int]] = {}
        # PR-P0-RACE-1: Convergence guards
        self._converge_first_enabled = parse_bool(
            "GRINDER_LIVE_CONVERGE_FIRST", default=True, strict=False
        )
        self._inflight_shift: dict[str, _InflightShift] = {}
        self._inflight_deferred_logged: set[str] = set()  # BUG-3: log once per latch
        self._cancel_failed_ids: set[str] = set()  # BUG-4: skip re-cancel on -2011
        self._account_sync_generation: int = 0
        # ADR-089: rolling steady-state log throttle (1 per 100 zero-action ticks per symbol)
        self._rolling_steady_state_count: dict[str, int] = {}
        # PR-ROLLING-GRID-V1B: rolling grid mode (doc-26, safe-by-default)
        self._rolling_grid_enabled = parse_bool(
            "GRINDER_LIVE_ROLLING_GRID", default=False, strict=False
        )
        # Rolling fill detection state (engine-owned, no cycle_layer private access)
        self._prev_rolling_orders: dict[str, dict[str, OpenOrderSnap]] = {}
        self._rolling_pending_cancels: dict[str, int] = {}  # order_id -> ts_ms
        # ADR-090: inflight CID fill detection + unreconciled placement cap
        self._inflight_placed_cids: dict[str, _InflightPlacedOrder] = {}
        self._unreconciled_place_count: dict[str, dict[str, int]] = {}
        # INV-10 (ADR-088): ANCHOR_RESET_BLOCKED throttle (log once per reason)
        self._anchor_reset_blocked_logged: set[str] = set()  # "{symbol}:{reason}"
        if self._emergency_exit_enabled:
            # Duck-type check: port must have cancel_all_orders + place_market_order + get_positions
            port = self._exchange_port
            if (
                hasattr(port, "cancel_all_orders")
                and hasattr(port, "place_market_order")
                and hasattr(port, "get_positions")
            ):
                self._emergency_exit_executor = EmergencyExitExecutor(port)  # type: ignore[arg-type]
                logger.info("RISK-EE-1: EmergencyExitExecutor enabled")
            else:
                logger.warning(
                    "RISK-EE-1: GRINDER_EMERGENCY_EXIT_ENABLED=1 but port lacks "
                    "cancel_all_orders/place_market_order/get_positions — executor not created"
                )
        ee_metrics = get_emergency_exit_metrics()
        ee_metrics.set_enabled(self._emergency_exit_enabled)

        # PR-ROLL-1b: log enforcement status at startup
        logger.info(
            "Reduce-only enforcement: %s",
            "enabled" if self._reduce_only_enforcement else "disabled",
        )
        logger.info(
            "Rolling grid mode: %s",
            "enabled" if self._rolling_grid_enabled else "disabled",
        )

    def _resolve_auto_threshold(self) -> None:
        """Resolve threshold from eval report at startup (PR-C9).

        Reads GRINDER_FILL_PROB_EVAL_DIR and GRINDER_FILL_PROB_AUTO_THRESHOLD.
        If eval_dir is unset, does nothing.  If set, resolves threshold.
        In auto-apply mode, overrides self._fill_prob_min_bps.
        In recommend-only mode (default), logs but does not override.
        Fail-open: any error -> keep configured threshold.
        """
        eval_dir = os.environ.get("GRINDER_FILL_PROB_EVAL_DIR", "").strip()
        if not eval_dir:
            return

        model_dir = os.environ.get("GRINDER_FILL_MODEL_DIR", "").strip()
        if not model_dir:
            logger.warning(
                "THRESHOLD_RESOLVE_SKIPPED reason=model_dir_unset eval_dir=%s",
                eval_dir,
            )
            return

        auto_apply = parse_bool("GRINDER_FILL_PROB_AUTO_THRESHOLD", default=False, strict=False)
        mode = "auto_apply" if auto_apply else "recommend_only"

        result = resolve_threshold_result(eval_dir, model_dir)
        if result.resolution is None:
            logger.warning(
                "FILL_PROB_THRESHOLD_RESOLUTION_FAILED reason_code=%s "
                "detail=%s eval_path=%s mode=%s configured_bps=%d",
                result.reason_code,
                result.detail,
                eval_dir,
                mode,
                self._fill_prob_min_bps,
            )
            return

        resolution = result.resolution
        configured_bps = self._fill_prob_min_bps
        if auto_apply:
            self._fill_prob_min_bps = resolution.threshold_bps
        effective_bps = self._fill_prob_min_bps

        logger.info(
            "FILL_PROB_THRESHOLD_RESOLUTION_OK mode=%s recommended_bps=%d "
            "configured_bps=%d effective_bps=%d provenance_ok=true",
            mode,
            resolution.threshold_bps,
            configured_bps,
            effective_bps,
        )

        # Set metric (visible to operator)
        get_sor_metrics().set_fill_prob_auto_threshold(resolution.threshold_bps)

        # Evidence artifact (gated on GRINDER_ARTIFACT_DIR, not GRINDER_FILL_PROB_EVIDENCE)
        write_threshold_resolution_evidence(
            resolution=resolution,
            configured_bps=configured_bps,
            mode=mode,
            effective_bps=effective_bps,
        )

    @property
    def last_feature_snapshot(self) -> FeatureSnapshot | None:
        """Latest FeatureSnapshot from FeatureEngine (None if no engine or no tick yet)."""
        return self._last_feature_snapshot

    @property
    def last_account_snapshot(self) -> AccountSnapshot | None:
        """Latest AccountSnapshot from AccountSync (None if never synced)."""
        return self._last_account_snapshot

    @property
    def config(self) -> LiveEngineConfig:
        """Get current configuration."""
        return self._config

    def update_config(self, config: LiveEngineConfig) -> None:
        """Update configuration (e.g., arm/disarm, change mode)."""
        self._config = config

    def process_snapshot(self, snapshot: Snapshot) -> LiveEngineOutput:  # noqa: PLR0912, PLR0915
        """Process snapshot through paper engine and execute on live exchange.

        Flow:
            1. Call paper_engine.process_snapshot() → actions
            2. For each action:
                a. Classify intent (INCREASE_RISK/REDUCE_RISK/CANCEL)
                b. Check safety gates (arming, mode, kill-switch, whitelist)
                c. Check DrawdownGuardV1.allow(intent)
                d. Execute via exchange_port (with retries for transient errors)
            3. Return LiveEngineOutput with execution results

        Args:
            snapshot: Market data snapshot

        Returns:
            LiveEngineOutput with paper output and live action results
        """
        # Store snapshot for SOR market data (Launch-14 PR2)
        self._last_snapshot = snapshot

        # PR-L0: Feed FeatureEngine (must run every tick for bar building, even in FSM defer)
        if self._feature_engine is not None:
            self._last_feature_snapshot = self._feature_engine.process_snapshot(snapshot)

        # Record price for toxicity gate (needs history before check, PR-A1)
        if self._toxicity_gate is not None:
            self._toxicity_gate.record_price(snapshot.ts, snapshot.symbol, snapshot.mid_price)

        # PR-338: Defer paper engine during FSM startup states (INIT/READY).
        # Paper engine mutates internal state via NoOp port; if run before ACTIVE,
        # ghost orders freeze reconciliation after ACTIVE transition.
        # Tick FSM first so it can advance toward ACTIVE.
        if self._fsm_driver is not None and self._fsm_driver.state in _FSM_DEFER_STATES:
            self._tick_fsm(snapshot.ts, snapshot.symbol)
            return LiveEngineOutput(
                paper_output=_DeferredPaperOutput(ts=snapshot.ts, symbol=snapshot.symbol),
                live_actions=[],
                armed=self._config.armed,
                mode=self._config.mode,
                kill_switch_active=self._config.kill_switch_active,
            )

        # PR-ROLLING-GRID-V1B: compute effective rolling mode for this tick
        rolling = self._rolling_grid_enabled and self._is_live_planner_enabled()

        # Freeze check: skip grid planner when position open (prevents GRID_SHIFT churn)
        # In rolling mode, freeze is disabled — fill-driven shifts must pass through.
        # Safety: rolling planner's additive formula is bounded (no mid-driven rebuilds).
        if rolling:
            grid_frozen = False
        else:
            grid_frozen = self._freeze_grid_in_position and self._has_open_position(snapshot.symbol)
        if grid_frozen:
            logger.warning(
                "GRID_FREEZE_IN_POSITION symbol=%s — skipping planner + replenish",
                snapshot.symbol,
            )

        # PR-ANTI-CHURN-2: detect freeze→unfreeze transition, reset anchor so
        # anti-churn allows full grid rebuild on first tick after position closes.
        # Not needed in rolling mode (no mid-anchor tracking).
        if not rolling:
            was_frozen = self._was_grid_frozen.get(snapshot.symbol, False)
            if was_frozen and not grid_frozen:
                self._grid_anchor_mid.pop(snapshot.symbol, None)
                logger.warning(
                    "GRID_UNFREEZE symbol=%s — anchor reset, planner will recenter grid",
                    snapshot.symbol,
                )
            self._was_grid_frozen[snapshot.symbol] = grid_frozen

        # Budget exhaustion latch: skip planner when order budget is dead
        budget_dead = self._order_budget_exhausted
        if budget_dead and not grid_frozen:
            logger.warning(
                "ORDER_BUDGET_EXHAUSTED symbol=%s — planner suppressed",
                snapshot.symbol,
            )

        # PR-ROLLING-GRID-V1B: detect grid fills and update rolling offset
        # BEFORE planner runs, so planner uses updated effective_center.
        if rolling and not budget_dead:
            self._cleanup_rolling_pending_cancels(snapshot.ts)
            grid_fills = self._detect_grid_fills_for_rolling(snapshot.symbol)
            planner = self._grid_planners.get(snapshot.symbol) if self._grid_planners else None
            if planner and grid_fills:
                for _oid, side in grid_fills:
                    planner.apply_fill_offset(snapshot.symbol, side)
                rs = planner.get_rolling_state(snapshot.symbol)
                logger.info(
                    "ROLLING_FILL_OFFSET symbol=%s fills=%d net_offset=%s",
                    snapshot.symbol,
                    len(grid_fills),
                    rs.net_offset if rs else "N/A",
                )

        # INV-10 (ADR-088): ANCHOR_RESET — re-anchor if exchange truly empty + flat
        # + no inflight + no pending cancels. Same-tick: reset now, plan() re-inits
        # from fresh mid_price in _plan_grid() on this same tick.
        if rolling and not budget_dead and not grid_frozen:
            _ra_planner = self._grid_planners.get(snapshot.symbol) if self._grid_planners else None
            if _ra_planner and _ra_planner.get_rolling_state(snapshot.symbol) is not None:
                # INV-10 fix: inflight blocks reset ONLY while account sync
                # hasn't refreshed since dispatch (no_fresh_inflight_latch).
                # Once sync refreshes, REST is the source of truth: if REST
                # shows 0 orders, exchange is truly empty.
                _inflight_blocking = False
                if snapshot.symbol in self._inflight_shift:
                    _if_entry = self._inflight_shift[snapshot.symbol]
                    _inflight_blocking = self._account_sync_generation <= _if_entry.sync_gen
                if not self._has_grinder_orders(snapshot.symbol) and not _inflight_blocking:
                    _pos_qty = self._get_position_qty(snapshot.symbol)
                    _pending_count = self._count_pending_cancels_for_symbol(snapshot.symbol)

                    if _pos_qty is None:
                        _key = f"{snapshot.symbol}:POSITION_UNKNOWN"
                        if _key not in self._anchor_reset_blocked_logged:
                            logger.warning(
                                "ANCHOR_RESET_BLOCKED symbol=%s reason=POSITION_UNKNOWN",
                                snapshot.symbol,
                            )
                            self._anchor_reset_blocked_logged.add(_key)
                    elif _pos_qty != 0:
                        _key = f"{snapshot.symbol}:POSITION_OPEN"
                        if _key not in self._anchor_reset_blocked_logged:
                            logger.warning(
                                "ANCHOR_RESET_BLOCKED symbol=%s reason=POSITION_OPEN pos_qty=%s",
                                snapshot.symbol,
                                _pos_qty,
                            )
                            self._anchor_reset_blocked_logged.add(_key)
                    elif _pending_count > 0:
                        _key = f"{snapshot.symbol}:PENDING_CANCELS"
                        if _key not in self._anchor_reset_blocked_logged:
                            logger.warning(
                                "ANCHOR_RESET_BLOCKED symbol=%s reason=PENDING_CANCELS count=%d",
                                snapshot.symbol,
                                _pending_count,
                            )
                            self._anchor_reset_blocked_logged.add(_key)
                    else:
                        # All 5 conditions met → ANCHOR_RESET (same-tick)
                        old_rs = _ra_planner.get_rolling_state(snapshot.symbol)
                        assert old_rs is not None  # guarded by line 719 check
                        logger.warning(
                            "ANCHOR_RESET symbol=%s old_anchor=%s old_offset=%d "
                            "new_mid=%s reason=EXCHANGE_EMPTY_FLAT",
                            snapshot.symbol,
                            old_rs.anchor_price,
                            old_rs.net_offset,
                            snapshot.mid_price,
                        )
                        # Planner-owned cleanup
                        _ra_planner.reset_rolling_state(snapshot.symbol)
                        # Engine-owned cleanup
                        self._prev_rolling_orders.pop(snapshot.symbol, None)
                        self._clear_pending_cancels_for_symbol(snapshot.symbol)
                        self._inflight_deferred_logged.discard(snapshot.symbol)
                        # ADR-090: clear inflight CIDs and reconciliation counter
                        self._inflight_placed_cids = {
                            c: i
                            for c, i in self._inflight_placed_cids.items()
                            if i.symbol != snapshot.symbol
                        }
                        self._unreconciled_place_count.pop(snapshot.symbol, None)
                        # Clear all throttle keys for this symbol on success
                        self._anchor_reset_blocked_logged.discard(
                            f"{snapshot.symbol}:POSITION_OPEN"
                        )
                        self._anchor_reset_blocked_logged.discard(
                            f"{snapshot.symbol}:PENDING_CANCELS"
                        )
                        self._anchor_reset_blocked_logged.discard(
                            f"{snapshot.symbol}:POSITION_UNKNOWN"
                        )
                else:
                    # Orders present or inflight active → clear blocked-log latch
                    # (state changed, next empty+blocked cycle should re-log)
                    self._anchor_reset_blocked_logged.discard(f"{snapshot.symbol}:POSITION_OPEN")
                    self._anchor_reset_blocked_logged.discard(f"{snapshot.symbol}:PENDING_CANCELS")
                    self._anchor_reset_blocked_logged.discard(f"{snapshot.symbol}:POSITION_UNKNOWN")

        # Step 1: Get actions -- either from LiveGridPlannerV1 or PaperEngine
        if grid_frozen or budget_dead:
            # Frozen/budget dead: no planner actions (TP reduce-only still allowed)
            raw_actions: list[ExecutionAction] = []
            paper_output: Any = _DeferredPaperOutput(
                ts=snapshot.ts, symbol=snapshot.symbol, actions=raw_actions
            )
        elif self._is_live_planner_enabled():
            # PR-L2: Exchange-truth grid planner replaces PaperEngine for action generation.
            # PaperEngine is NOT called (avoids ghost state mutation, doc-25 I1).
            plan_result = self._plan_grid(snapshot, rolling_mode=rolling)
            raw_actions = plan_result.actions
            # Anti-churn: suppress GRID_SHIFT if mid hasn't moved enough from anchor
            # Skipped in rolling mode — no mid-driven shifts to suppress.
            if not rolling:
                raw_actions = self._filter_grid_shift(
                    snapshot.symbol, snapshot.mid_price, raw_actions
                )
            # PR-P0-RACE-1: convergence guards (sync-gate, cancel-first, budget)
            # Scoped to planner path ONLY — cycle layer (TP/replenish) appended AFTER.
            raw_actions = self._apply_convergence_guards(
                snapshot.symbol, raw_actions, plan_result, snapshot.ts
            )
            paper_output = _DeferredPaperOutput(
                ts=snapshot.ts, symbol=snapshot.symbol, actions=raw_actions
            )
        else:
            paper_output = self._paper_engine.process_snapshot(snapshot)

        # PR-INV-3: Cycle layer — detect fills, generate TP actions
        if self._is_cycle_layer_enabled() and self._last_account_snapshot is not None:
            symbol_orders = tuple(
                o for o in self._last_account_snapshot.open_orders if o.symbol == snapshot.symbol
            )
            # PR-TP-RENEW: pass position qty for auto-renew decision
            pos_qty = self._get_position_qty(snapshot.symbol)
            self._cycle_layer.register_cancels(raw_actions, ts_ms=snapshot.ts)  # type: ignore[union-attr]
            cycle_actions = self._cycle_layer.on_snapshot(  # type: ignore[union-attr]
                symbol=snapshot.symbol,
                open_orders=symbol_orders,
                mid_price=snapshot.mid_price,
                ts_ms=snapshot.ts,
                pos_qty=pos_qty,
            )
            # Filter out replenish when grid frozen OR rolling mode
            # Rolling: planner diff handles level restoration, replenish would duplicate.
            if (grid_frozen or rolling) and cycle_actions:
                cycle_actions = [a for a in cycle_actions if a.reason != "REPLENISH"]
            if cycle_actions:
                logger.info("Cycle layer %s: %d TP actions", snapshot.symbol, len(cycle_actions))
                raw_actions = raw_actions + cycle_actions
                # INV-9: defense-in-depth overlap guard (anomaly detector).
                # Suppresses grid PLACEs that overlap with TP PLACEs on same side.
                # Expected fire count: ZERO. Firing = bug evidence.
                if rolling:
                    raw_actions = self._filter_tp_grid_overlap(raw_actions, snapshot.symbol)
                paper_output = _DeferredPaperOutput(
                    ts=snapshot.ts, symbol=snapshot.symbol, actions=raw_actions
                )

        # PR-ROLLING-GRID-V1B: register ALL cancels for rolling fill detection
        # Must run AFTER cycle_layer to capture TP_SLOT_TAKEOVER CANCELs.
        if rolling:
            self._register_rolling_cancels(raw_actions, snapshot.ts)

        # Replenish-on-TP-fill: detect position decrease → add BUY below + SELL above
        # Bypassed in rolling mode — planner diff handles slot restoration.
        if (
            self._is_cycle_layer_enabled()
            and self._last_account_snapshot is not None
            and not rolling
        ):
            pos_qty_for_anchor = self._get_position_qty(snapshot.symbol)
            self._update_grid_anchors(snapshot.symbol, pos_qty_for_anchor)
            tp_fill_event = self._detect_tp_fill_event(snapshot.symbol, pos_qty_for_anchor)
            if tp_fill_event:
                tp_replenish = self._generate_tp_fill_replenish(
                    snapshot.symbol,
                    pos_qty_for_anchor,
                    snapshot.ts,
                )
                if tp_replenish:
                    raw_actions = raw_actions + tp_replenish
                    paper_output = _DeferredPaperOutput(
                        ts=snapshot.ts,
                        symbol=snapshot.symbol,
                        actions=raw_actions,
                    )

        # FSM tick: update state before action processing (Launch-13 PR3)
        if self._fsm_driver is not None:
            self._tick_fsm(snapshot.ts, snapshot.symbol)

        # RISK-EE-1: Emergency exit trigger (after FSM tick, before action processing)
        if (
            self._emergency_exit_enabled
            and self._emergency_exit_executor is not None
            and not self._emergency_exit_executed
            and self._fsm_driver is not None
            and self._fsm_driver.state == SystemState.EMERGENCY
        ):
            self._execute_emergency_exit(snapshot.ts)

        # Account sync: read-only fetch + mismatch detection (Launch-15)
        # Throttled: at most once per _account_sync_interval_ms to avoid REST rate-limits
        if self._is_account_sync_enabled() and snapshot.ts > 0:
            elapsed = snapshot.ts - self._account_sync_last_attempt_ms
            if elapsed >= self._account_sync_interval_ms:
                self._account_sync_last_attempt_ms = snapshot.ts
                self._tick_account_sync()

        # Step 2: Process actions
        live_actions: list[LiveAction] = []
        raw_actions = paper_output.actions if hasattr(paper_output, "actions") else []

        # PR-P0-TP-CLOSE-ATOMIC: retry failed TP_CLOSE PLACEs from previous ticks
        retry_results = self._process_tp_close_retries(snapshot.symbol, snapshot.ts)
        live_actions.extend(retry_results)

        # PR-P0-TP-RENEW-ATOMIC: track whether TP_RENEW PLACE succeeded per symbol.
        # If PLACE was blocked, skip the paired CANCEL to keep old TP alive.
        tp_renew_place_ok: dict[str, bool] = {}

        # PR-P0-TP-CLOSE-ATOMIC: track TP_CLOSE PLACE success per correlation_id.
        # If PLACE failed, skip paired TP_SLOT_TAKEOVER CANCEL (same correlation_id).
        tp_close_place_ok: dict[str, bool] = {}

        for raw_action in raw_actions:
            # PaperOutput.actions is list[dict], but tests may pass ExecutionAction directly
            if isinstance(raw_action, dict):
                action = ExecutionAction.from_dict(raw_action)
            else:
                action = raw_action

            # Guard: skip TP_SLOT_TAKEOVER CANCEL if paired TP_CLOSE PLACE failed
            if (
                action.action_type == ActionType.CANCEL
                and action.reason == "TP_SLOT_TAKEOVER"
                and action.correlation_id is not None
                and not tp_close_place_ok.get(action.correlation_id, True)
            ):
                logger.warning(
                    "TP_SLOT_TAKEOVER_SKIPPED symbol=%s order_id=%s corr=%s — "
                    "TP_CLOSE PLACE failed, keeping grid order alive",
                    action.symbol,
                    action.order_id,
                    action.correlation_id,
                )
                if self._cycle_layer is not None and action.order_id is not None:
                    self._cycle_layer.unregister_pending_cancel(action.order_id)
                live_actions.append(
                    LiveAction(
                        action=action,
                        status=LiveActionStatus.BLOCKED,
                        block_reason=BlockReason.TP_CLOSE_PLACE_FAILED,
                        intent=RiskIntent.CANCEL,
                    )
                )
                continue

            # Guard: skip TP_RENEW CANCEL if the paired PLACE was blocked
            if (
                action.action_type == ActionType.CANCEL
                and action.reason == "TP_RENEW"
                and action.symbol is not None
                and not tp_renew_place_ok.get(action.symbol, True)
            ):
                logger.warning(
                    "TP_RENEW_CANCEL_SKIPPED symbol=%s order_id=%s — "
                    "PLACE was blocked, keeping old TP alive",
                    action.symbol,
                    action.order_id,
                )
                live_actions.append(
                    LiveAction(
                        action=action,
                        status=LiveActionStatus.BLOCKED,
                        block_reason=BlockReason.TP_RENEW_PLACE_FAILED,
                        intent=RiskIntent.CANCEL,
                    )
                )
                continue

            # BUG-4: skip CANCEL for order_ids that already returned -2011.
            # Stale snapshot may still show the order → planner re-generates CANCEL →
            # Binance returns -2011 → repeat every tick until sync refreshes.
            if (
                action.action_type == ActionType.CANCEL
                and action.order_id is not None
                and action.order_id in self._cancel_failed_ids
            ):
                # ADR-089: explicit log for skipped cancel (previously silent)
                logger.debug(
                    "CANCEL_SKIP_ALREADY_FAILED symbol=%s order_id=%s",
                    snapshot.symbol,
                    action.order_id,
                )
                live_actions.append(
                    LiveAction(
                        action=action,
                        status=LiveActionStatus.SKIPPED,
                        block_reason=BlockReason.CANCEL_ALREADY_FAILED,
                        intent=RiskIntent.CANCEL,
                    )
                )
                continue

            live_action = self._process_action(action, snapshot.ts)
            live_actions.append(live_action)

            # BUG-4: track failed CANCELs for -2011 suppression
            if (
                action.action_type == ActionType.CANCEL
                and action.order_id is not None
                and live_action.status == LiveActionStatus.FAILED
            ):
                self._cancel_failed_ids.add(action.order_id)

            # Track TP_CLOSE PLACE result by correlation_id
            if action.reason == "TP_CLOSE" and action.action_type == ActionType.PLACE:
                if action.correlation_id is None:
                    # Invariant breach: new TP path MUST set correlation_id.
                    logger.error(
                        "TP_CLOSE_MISSING_CORRELATION_ID sym=%s id=%s — "
                        "generation bug, atomicity guard disabled for this pair",
                        action.symbol,
                        action.client_order_id,
                    )
                else:
                    ok = live_action.status == LiveActionStatus.EXECUTED
                    tp_close_place_ok[action.correlation_id] = ok
                    if not ok and self._is_tp_close_retryable(live_action):
                        self._enqueue_tp_close_retry(action, snapshot.ts)

            # Track TP_RENEW PLACE results
            if action.reason == "TP_RENEW" and action.action_type == ActionType.PLACE:
                tp_renew_place_ok[action.symbol or ""] = (
                    live_action.status == LiveActionStatus.EXECUTED
                )

        # Step 3: Build output
        return LiveEngineOutput(
            paper_output=paper_output,
            live_actions=live_actions,
            armed=self._config.armed,
            mode=self._config.mode,
            kill_switch_active=self._config.kill_switch_active,
        )

    def _tick_fsm(self, ts_ms: int, symbol: str) -> None:
        """Tick FSM driver with current runtime signals.

        Reads kill_switch, drawdown from existing guards.
        operator_override from GRINDER_OPERATOR_OVERRIDE env var.
        feed_gap_ms from per-symbol snapshot gap (PR-A2a: numeric, FSM owns threshold).
        spread_bps + toxicity_score_bps from snapshot + ToxicityGate (PR-A2a).

        Uses snapshot clock (ts_ms) for deterministic duration tracking.
        All timestamps in milliseconds (Snapshot.ts contract).
        """
        assert self._fsm_driver is not None  # caller guards

        # Signal: operator override from env var (via env_parse SSOT)
        override = parse_enum(
            "GRINDER_OPERATOR_OVERRIDE",
            allowed={"PAUSE", "EMERGENCY"},
            default=None,
            strict=False,
        )

        # Compute feed_gap_ms from per-symbol snapshot gap (PR-A2a)
        prev_ts = self._prev_snapshot_ts.get(symbol, 0)
        feed_gap_ms = (ts_ms - prev_ts) if prev_ts > 0 else 0
        self._prev_snapshot_ts[symbol] = ts_ms

        # Compute spread_bps + toxicity_score_bps (PR-A2a: raw numerics, FSM owns thresholds)
        spread_bps = 0.0
        toxicity_score_bps = 0.0
        if self._last_snapshot is not None:
            spread_bps = self._last_snapshot.spread_bps
        if self._toxicity_gate is not None and self._last_snapshot is not None:
            snap = self._last_snapshot
            toxicity_score_bps = self._toxicity_gate.price_impact_bps(
                ts_ms, snap.symbol, snap.mid_price
            )

        self._fsm_driver.step(
            ts_ms=ts_ms,
            kill_switch_active=self._config.kill_switch_active,
            drawdown_pct=(
                self._drawdown_guard.current_drawdown_pct
                if self._drawdown_guard is not None
                else 0.0
            ),
            feed_gap_ms=feed_gap_ms,
            spread_bps=spread_bps,
            toxicity_score_bps=toxicity_score_bps,
            position_notional_usd=self._position_notional_usd,  # PR-A4: measured by AccountSyncer
            operator_override=override,
        )

    def _execute_emergency_exit(self, ts_ms: int) -> None:
        """Execute emergency exit sequence (RISK-EE-1, § 10.6).

        Determines target symbols from config whitelist or open positions.
        Calls EmergencyExitExecutor.execute().
        Runs at most once (latch: _emergency_exit_executed).

        Does NOT override _position_notional_usd — that is measured by
        AccountSyncer (PR-A4). Recovery waits for confirmed measurement.
        """
        assert self._emergency_exit_executor is not None  # caller guards

        # Determine target symbols: whitelist > positions-derived
        symbols = list(self._config.symbol_whitelist)
        if not symbols:
            # No whitelist: derive symbols from open positions
            try:
                positions = self._exchange_port.fetch_positions()
                symbols = list({p.symbol for p in positions if hasattr(p, "symbol")})
            except Exception:
                logger.exception("Failed to derive symbols from positions for emergency exit")

        if not symbols:
            logger.critical(
                "EMERGENCY EXIT: no symbols to process (whitelist empty, no positions found)"
            )
            self._emergency_exit_executed = True
            return

        result = self._emergency_exit_executor.execute(
            ts_ms=ts_ms,
            reason="fsm_emergency",
            symbols=symbols,
        )
        self._emergency_exit_executed = True

        get_emergency_exit_metrics().record_exit(result)

        logger.critical(
            "EMERGENCY EXIT %s: cancelled=%d market=%d remaining=%d",
            "SUCCESS" if result.success else "PARTIAL",
            result.orders_cancelled,
            result.market_orders_placed,
            result.positions_remaining,
        )

    def _is_account_sync_enabled(self) -> bool:
        """Check if account sync is active.

        Requires: feature flag (config or env) AND syncer instance.
        """
        flag_on = self._config.account_sync_enabled or self._account_sync_env_override
        if not flag_on:
            return False
        if self._account_syncer is None:
            logger.debug("Account sync flag ON but no syncer instance, skipping")
            return False
        return True

    def _is_live_planner_enabled(self) -> bool:
        """Check if live grid planner is active (PR-L2).

        Requires: env flag AND planner instances AND AccountSync enabled.
        """
        if not self._live_planner_env_override:
            return False
        if not self._grid_planners:
            return False
        if not self._is_account_sync_enabled():
            if not self._warned_live_planner_no_sync:
                logger.warning(
                    "GRINDER_LIVE_PLANNER_ENABLED=1 but AccountSync disabled "
                    "-- planner cannot function without exchange order truth"
                )
                self._warned_live_planner_no_sync = True
            return False
        return True

    def _is_cycle_layer_enabled(self) -> bool:
        """Check if live cycle layer is active (PR-INV-3).

        Requires: env flag AND cycle_layer instance AND live planner enabled.
        """
        if not self._live_cycle_env_override:
            return False
        if self._cycle_layer is None:
            return False
        return self._is_live_planner_enabled()

    # --- Rolling grid fill detection (PR-ROLLING-GRID-V1B) ---

    _ROLLING_CANCEL_TTL_MS = 30_000  # 30s, same as cycle_layer._CANCEL_TTL_MS

    def _detect_grid_fills_for_rolling(self, symbol: str) -> list[tuple[str, str]]:  # noqa: PLR0912
        """Detect grid fills by snapshot diff for rolling offset update.

        Rolling fill classification contract:
        - Fill = grid order (strategy_id="d") in prev but not current
          AND not in pending cancels.
        - Not fill = our cancel, TP_SLOT_TAKEOVER cancel, TP order,
          non-grid strategy order, restart bootstrap.
        - Limitation: disappearance heuristic, not trade evidence check.

        Returns list of (order_id, side) for each detected grid fill.
        Does NOT generate TP actions — cycle layer handles that separately.
        """
        snap = self._last_account_snapshot
        if snap is None:
            return []

        current: dict[str, OpenOrderSnap] = {}
        for o in snap.open_orders:
            if o.symbol != symbol:
                continue
            parsed = parse_client_order_id(o.order_id)
            if parsed is None:
                continue
            # Only grid orders (strategy_id="d") participate in rolling offset.
            # TP orders (strategy_id="tp") and any future non-grid strategies
            # are excluded — their disappearance must NOT shift net_offset.
            if parsed.strategy_id != DEFAULT_STRATEGY_ID:
                continue
            current[o.order_id] = o

        # ADR-090: Inflight CID reconciliation (see Reconciliation Contract).
        # Each dispatched PLACE is reconciled on the first post-dispatch sync:
        #   - CID in current REST -> survived (on book) -> decrement unreconciled
        #   - CID absent from REST -> inflight fill -> decrement unreconciled + emit fill
        # Note: all_open_ids is unfiltered (no parse_client_order_id gate) because
        # inflight CID survival is identity-based, not strategy-based.
        all_open_ids: set[str] = set()
        for o in snap.open_orders:
            if o.symbol == symbol:
                all_open_ids.add(o.order_id)

        fills: list[tuple[str, str]] = []
        expired_cids: list[str] = []
        for cid, info in self._inflight_placed_cids.items():
            if info.symbol != symbol:
                continue
            if self._account_sync_generation <= info.sync_gen:
                continue  # sync hasn't refreshed since dispatch -- too early
            # Reconciliation: decrement unreconciled counter for BOTH outcomes
            sym_counts = self._unreconciled_place_count.get(symbol, {})
            if info.side in sym_counts and sym_counts[info.side] > 0:
                sym_counts[info.side] -= 1
            expired_cids.append(cid)
            if cid in all_open_ids:
                pass  # survived -- normal disappearance heuristic tracks from here
            else:
                fills.append((cid, info.side))
                logger.info(
                    "INFLIGHT_FILL_DETECTED symbol=%s cid=%s side=%s "
                    "dispatch_gen=%d current_gen=%d",
                    symbol,
                    cid,
                    info.side,
                    info.sync_gen,
                    self._account_sync_generation,
                )
        for cid in expired_cids:
            del self._inflight_placed_cids[cid]

        prev = self._prev_rolling_orders.get(symbol, {})

        for oid, snap_order in prev.items():
            if oid in current:
                continue  # still open
            if oid in self._rolling_pending_cancels:
                del self._rolling_pending_cancels[oid]  # consumed
                continue
            fills.append((oid, snap_order.side))

        self._prev_rolling_orders[symbol] = current
        return fills

    def _register_rolling_cancels(self, actions: list[ExecutionAction], ts_ms: int) -> None:
        """Register CANCEL actions as pending for rolling fill detection."""
        for a in actions:
            if a.action_type == ActionType.CANCEL and a.order_id:
                self._rolling_pending_cancels[a.order_id] = ts_ms

    def _cleanup_rolling_pending_cancels(self, ts_ms: int) -> None:
        """Remove expired pending cancel entries (30s TTL, same as cycle_layer)."""
        expired = [
            oid
            for oid, reg_ts in self._rolling_pending_cancels.items()
            if ts_ms - reg_ts > self._ROLLING_CANCEL_TTL_MS
        ]
        for oid in expired:
            del self._rolling_pending_cancels[oid]

    def _filter_tp_grid_overlap(
        self, actions: list[ExecutionAction], symbol: str
    ) -> list[ExecutionAction]:
        """INV-9 defense-in-depth: suppress grid PLACEs overlapping TP PLACEs.

        Anomaly guard. Expected fire count: ZERO. Firing in production is
        bug evidence requiring investigation.

        Detection uses reduce_only as structural discriminator:
        - TP PLACE: reduce_only=True (cycle_layer.py:278)
        - Grid PLACE: reduce_only=False (planner default)

        Fail-open: if planner config unavailable, returns actions unchanged.
        """
        # Resolve epsilon from planner config (SSOT: LiveGridConfig.price_epsilon_bps)
        planner = self._grid_planners.get(symbol) if self._grid_planners else None
        if planner is None:
            logger.warning(
                "TP_GRID_OVERLAP_GUARD_SKIP symbol=%s reason=no_planner_config",
                symbol,
            )
            return actions

        epsilon_bps = planner._config.price_epsilon_bps

        # Collect TP PLACE targets: (side, price)
        tp_places: list[tuple[OrderSide, Decimal]] = []
        for a in actions:
            if (
                a.action_type == ActionType.PLACE
                and a.reduce_only
                and a.side is not None
                and a.price is not None
                and a.symbol == symbol
            ):
                tp_places.append((a.side, a.price))

        if not tp_places:
            return actions

        # Use mid_price as reference for bps calculation
        ref_price = max(p for _, p in tp_places)  # safe nonzero approximation

        filtered: list[ExecutionAction] = []
        for a in actions:
            if (
                a.action_type == ActionType.PLACE
                and not a.reduce_only
                and a.side is not None
                and a.price is not None
                and a.symbol == symbol
            ):
                # Check overlap with any TP PLACE on same side
                overlap = False
                for tp_side, tp_price in tp_places:
                    if a.side != tp_side:
                        continue
                    delta_bps = (
                        float(abs(a.price - tp_price) / ref_price) * 10000
                        if ref_price > 0
                        else float("inf")
                    )
                    if delta_bps <= epsilon_bps:
                        overlap = True
                        logger.warning(
                            "TP_GRID_OVERLAP_SUPPRESSED symbol=%s side=%s "
                            "grid_price=%s tp_price=%s delta_bps=%.2f "
                            "reason=DEFENSE_IN_DEPTH",
                            symbol,
                            a.side.value if a.side else "?",
                            a.price,
                            tp_price,
                            delta_bps,
                        )
                        break
                if overlap:
                    continue
            filtered.append(a)
        return filtered

    def _plan_grid(self, snapshot: Snapshot, rolling_mode: bool = False) -> GridPlanResult:
        """Generate grid actions from LiveGridPlannerV1 (PR-L2).

        Uses last AccountSync snapshot for exchange truth.
        Returns empty GridPlanResult if no snapshot yet (safe startup).

        PR-P0-RACE-1: returns full GridPlanResult (not just .actions) so
        convergence guards can inspect diff_extra.
        """
        assert self._grid_planners is not None

        planner = self._grid_planners.get(snapshot.symbol)
        if planner is None:
            logger.debug("No grid planner for %s, skipping", snapshot.symbol)
            return GridPlanResult()

        if self._last_account_snapshot is None:
            logger.debug("No account snapshot yet, planner returns 0 actions (safe startup)")
            return GridPlanResult()

        # Filter open orders for this symbol only.
        # INV-9b: TP orders are now INCLUDED so the planner can match them
        # to desired levels (prevents cross-tick grid/TP overlap). The planner
        # skips CANCEL/REPLACE for TP orders (managed by cycle layer).
        open_orders = tuple(
            o for o in self._last_account_snapshot.open_orders if o.symbol == snapshot.symbol
        )

        # Extract NATR from FeatureEngine (PR-L0)
        features = self._last_feature_snapshot
        natr_bps = features.natr_bps if features and features.symbol == snapshot.symbol else None
        natr_last_ts = features.ts if features else 0

        # PR-INV-2: suppress PLACE/REPLACE when FSM not ACTIVE
        suppress_increase = (
            self._fsm_driver is not None and self._fsm_driver.state != SystemState.ACTIVE
        )
        if suppress_increase:
            logger.info(
                "Grid planner cancel-only mode: FSM state=%s",
                self._fsm_driver.state.value if self._fsm_driver else "None",
            )

        plan_result = planner.plan(
            symbol=snapshot.symbol,
            mid_price=snapshot.mid_price,
            ts_ms=snapshot.ts,
            open_orders=open_orders,
            natr_bps=natr_bps,
            natr_last_ts=natr_last_ts,
            suppress_increase=suppress_increase,
            rolling_mode=rolling_mode,
        )

        if plan_result.actions:
            # Reset steady-state counter when actions resume
            self._rolling_steady_state_count.pop(snapshot.symbol, None)
            # P0-2d: promote to WARNING when debug active (visible without logging.basicConfig)
            log_fn = logger.warning if self._debug_open_orders else logger.info
            # INV-10: rolling mode appends ec= and anchor= to log (non-rolling unchanged)
            rs = planner.get_rolling_state(snapshot.symbol) if rolling_mode else None
            if rs is not None:
                ec_val = rs.anchor_price + rs.net_offset * rs.step_price
                log_fn(
                    "PLANNER_ACTIONS_SUMMARY %s: desired=%d actual=%d missing=%d "
                    "extra=%d extra_tp=%d mismatch=%d spacing=%.1f bps "
                    "natr_fallback=%s actions=%d mid=%s ec=%s anchor=%s",
                    snapshot.symbol,
                    plan_result.desired_count,
                    plan_result.actual_count,
                    plan_result.diff_missing,
                    plan_result.diff_extra,
                    plan_result.diff_extra_tp,
                    plan_result.diff_mismatch,
                    plan_result.effective_spacing_bps,
                    plan_result.natr_fallback,
                    len(plan_result.actions),
                    snapshot.mid_price,
                    ec_val,
                    rs.anchor_price,
                )
            else:
                log_fn(
                    "PLANNER_ACTIONS_SUMMARY %s: desired=%d actual=%d missing=%d "
                    "extra=%d extra_tp=%d mismatch=%d spacing=%.1f bps "
                    "natr_fallback=%s actions=%d mid=%s",
                    snapshot.symbol,
                    plan_result.desired_count,
                    plan_result.actual_count,
                    plan_result.diff_missing,
                    plan_result.diff_extra,
                    plan_result.diff_extra_tp,
                    plan_result.diff_mismatch,
                    plan_result.effective_spacing_bps,
                    plan_result.natr_fallback,
                    len(plan_result.actions),
                    snapshot.mid_price,
                )
        elif rolling_mode:
            # ADR-089: throttled steady-state log for rolling mode (1 per 100 ticks)
            count = self._rolling_steady_state_count.get(snapshot.symbol, 0) + 1
            self._rolling_steady_state_count[snapshot.symbol] = count
            if count % 100 == 1:
                logger.debug(
                    "ROLLING_STEADY_STATE symbol=%s tick=%d desired=%d actual=%d",
                    snapshot.symbol,
                    count,
                    plan_result.desired_count,
                    plan_result.actual_count,
                )

        return plan_result

    def _tick_account_sync(self) -> None:  # noqa: PLR0912
        """Run one account sync cycle (read-only).

        Fetches snapshot, detects mismatches, records metrics.
        Updates _position_notional_usd from snapshot positions (PR-A4).
        Evidence writing is delegated to evidence.py (env-gated).
        """
        assert self._account_syncer is not None  # caller guards

        result = self._account_syncer.sync()

        if result.error is not None:
            logger.warning("Account sync failed: %s", result.error)
            return

        if result.snapshot is not None and result.mismatches:
            logger.warning(
                "Account sync mismatches detected: %d",
                len(result.mismatches),
            )
            for m in result.mismatches:
                logger.warning("  [%s] %s", m.rule, m.detail)

        # PR-A4: update position notional from confirmed snapshot
        if result.snapshot is not None:
            self._position_notional_usd = AccountSyncer.compute_position_notional(result.snapshot)
            # PR-L2: Store full snapshot for LiveGridPlannerV1 (open_orders as exchange truth)
            self._last_account_snapshot = result.snapshot
            # PR-P0-RACE-1: monotonic generation counter for convergence guards
            self._account_sync_generation += 1
            # BUG-4: clear cancel-failed blacklist — fresh snapshot replaces stale state
            if self._cancel_failed_ids:
                logger.info(
                    "CANCEL_FAILED_IDS_CLEARED count=%d gen=%d",
                    len(self._cancel_failed_ids),
                    self._account_sync_generation,
                )
                self._cancel_failed_ids.clear()

        # Evidence writing (env-gated, safe-by-default)
        if result.snapshot is not None:
            evidence_dir = write_evidence_bundle(result.snapshot, result.mismatches)
            if evidence_dir is not None:
                logger.info("Account sync evidence written to %s", evidence_dir)

        # P0-2: correlate recent PLACEs with AccountSync open_orders
        if self._debug_open_orders and result.snapshot is not None:
            open_ids = {o.order_id for o in result.snapshot.open_orders}
            parsable_grinder_ids = sum(
                1 for oid in open_ids if parse_client_order_id(oid) is not None
            )
            now_ms = int(time.time() * 1000)
            corr = correlate_recent_places(self._recent_places, open_ids, now_ms)
            logger.warning(
                "PLACE_CORRELATION open_orders_count=%d parsable_grinder=%d "
                "recent=%d found=%d missing=%d",
                len(open_ids),
                parsable_grinder_ids,
                corr.total,
                corr.found,
                corr.missing,
            )
            for entry in corr.missing_details[:5]:  # bounded
                logger.warning("  MISSING: %s", entry)

            # P0-2b: detect open_orders drop to 0
            current_count = len(open_ids)
            if self._prev_open_orders_count > 0 and current_count == 0 and corr.total > 0:
                logger.warning(
                    "OPEN_ORDERS_DROP prev_count=%d now_count=0 recent=%d",
                    self._prev_open_orders_count,
                    corr.total,
                )
            self._prev_open_orders_count = current_count

            # P0-2b: lookup terminal status for missing orders
            if corr.missing > 0:
                looked_up = 0
                for cid, _placed_ts, sym in self._recent_places:
                    if looked_up >= self._debug_lookup_limit:
                        break
                    if cid in open_ids:
                        continue
                    if cid in self._looked_up_ids:
                        continue
                    self._looked_up_ids.add(cid)
                    info = self._exchange_port.debug_get_order_status(
                        symbol=sym,
                        client_order_id=cid,
                    )
                    if info is not None:
                        logger.warning(
                            "ORDER_LOOKUP clientOrderId=%s status=%s "
                            "executed=%s orig=%s avgPrice=%s side=%s "
                            "updateTime=%s",
                            cid,
                            info.get("status"),
                            info.get("executedQty"),
                            info.get("origQty"),
                            info.get("avgPrice"),
                            info.get("side"),
                            info.get("updateTime"),
                        )
                    looked_up += 1

            # P0-2b: bound dedup set
            if len(self._looked_up_ids) > 100:
                self._looked_up_ids.clear()

    def _get_position_sign(self, symbol: str) -> int | None:
        """Determine net position direction for a symbol (PR-INV-1).

        Returns:
            +1 if net LONG, -1 if net SHORT, None if unknown/BOTH/flat.

        Hedge-mode (LONG/SHORT separate entries): returns sign of the
        non-zero side.  If both sides have qty > 0, returns None (hedged).

        One-way mode (side="BOTH"): returns None (fail-closed, qty is
        always absolute and sign is lost in BinanceFuturesPort parsing).
        """
        snap = self._last_account_snapshot
        if snap is None:
            return None
        has_long = False
        has_short = False
        for p in snap.positions:
            if p.symbol != symbol:
                continue
            if p.side == "BOTH":
                return None  # one-way mode, sign unknown
            if p.side == "LONG" and p.qty > 0:
                has_long = True
            elif p.side == "SHORT" and p.qty > 0:
                has_short = True
        if has_long and has_short:
            return None  # hedged
        if has_long:
            return 1
        if has_short:
            return -1
        return None  # flat or no position

    def _enforce_reduce_only(
        self,
        action: ExecutionAction,
        pos_sign: int | None,
    ) -> bool:
        """Enforce reduce_only on opposite-side orders when position open (PR-ROLL-1).

        Rules:
        - pos_sign=+1 (LONG) -> SELL orders get reduce_only=True
        - pos_sign=-1 (SHORT) -> BUY orders get reduce_only=True
        - pos_sign=None -> no enforcement (flat/unknown = fail-open)
        - CANCEL actions: skip (no side relevance)
        - Already reduce_only=True: skip (no metric, no log)

        Returns True if enforcement was applied, False otherwise.
        """
        # PR-ROLL-1b: toggle — disabled for MM mode
        if not self._reduce_only_enforcement:
            return False

        # Skip CANCEL — no side relevance
        if action.action_type == ActionType.CANCEL:
            return False

        # Fail-open: unknown/flat position → don't enforce
        if pos_sign is None:
            return False

        # Already reduce_only → no-op (TP orders come pre-set)
        if action.reduce_only:
            return False

        sym = action.symbol or ""
        enforced = False

        if pos_sign == 1 and action.side == OrderSide.SELL:
            action.reduce_only = True
            reason = "position_long"
            enforced = True
        elif pos_sign == -1 and action.side == OrderSide.BUY:
            action.reduce_only = True
            reason = "position_short"
            enforced = True

        if enforced:
            side_str = action.side.value if action.side else "UNKNOWN"
            logger.warning(
                "REDUCE_ONLY_ENFORCED sym=%s side=%s reason=%s action=%s",
                sym,
                side_str,
                reason,
                action.action_type.value,
            )
            get_live_engine_metrics().record_reduce_only_enforced(sym, side_str, reason)

        return enforced

    def _has_open_position(self, symbol: str) -> bool:
        """Check if symbol has any non-zero position (cycle open).

        Returns True if any position entry for symbol has qty > 0.
        Returns False if no snapshot, no positions, or all qty == 0.
        """
        snap = self._last_account_snapshot
        if snap is None:
            return False
        return any(p.qty > 0 for p in snap.positions if p.symbol == symbol)

    def _get_position_qty(self, symbol: str) -> Decimal | None:
        """Get absolute position quantity for symbol (PR-TP-RENEW).

        Returns:
            Decimal quantity (>= 0) if position found, None if no snapshot.
        """
        snap = self._last_account_snapshot
        if snap is None:
            return None
        for p in snap.positions:
            if p.symbol == symbol:
                return p.qty
        return Decimal("0")

    def _has_grinder_orders(self, symbol: str) -> bool:
        """Check if exchange has any grinder-owned orders for symbol.

        Used by INV-10 ANCHOR_RESET condition (ADR-088).
        """
        snap = self._last_account_snapshot
        if snap is None:
            return False
        for o in snap.open_orders:
            if o.symbol != symbol:
                continue
            parsed = parse_client_order_id(o.order_id)
            if parsed is not None and parsed.prefix.startswith(DEFAULT_PREFIX.rstrip("_")):
                return True
        return False

    def _count_pending_cancels_for_symbol(self, symbol: str) -> int:
        """Count rolling pending cancel entries for a specific symbol.

        Used by INV-10 ANCHOR_RESET condition (ADR-088).
        """
        count = 0
        for oid in self._rolling_pending_cancels:
            parsed = parse_client_order_id(oid)
            if parsed is not None and parsed.symbol == symbol:
                count += 1
        return count

    def _clear_pending_cancels_for_symbol(self, symbol: str) -> None:
        """Remove rolling pending cancel entries for a specific symbol.

        Multi-symbol safe: only clears entries matching the given symbol.
        Part of INV-10 ANCHOR_RESET engine-side cleanup (ADR-088).
        """
        stale = [
            oid
            for oid in self._rolling_pending_cancels
            if (parsed := parse_client_order_id(oid)) is not None and parsed.symbol == symbol
        ]
        for oid in stale:
            del self._rolling_pending_cancels[oid]

    def _detect_tp_fill_event(self, symbol: str, pos_qty: Decimal | None) -> bool:
        """Detect TP fill event: position magnitude decreased.

        Long: prev > 0 and cur >= 0 and cur < prev.
        Short: prev < 0 and cur <= 0 and cur > prev (magnitude decreased).

        Updates _prev_pos_qty. Returns False if pos_qty unknown.
        """
        if pos_qty is None:
            return False
        prev = self._prev_pos_qty.get(symbol, Decimal("0"))
        self._prev_pos_qty[symbol] = pos_qty

        if prev > 0 and pos_qty >= 0 and pos_qty < prev:
            return True
        return bool(prev < 0 and pos_qty <= 0 and pos_qty > prev)

    def _update_grid_anchors(self, symbol: str, pos_qty: Decimal | None) -> None:
        """Update grid price anchors from open orders when position is flat.

        Anchors are only set/updated when pos_qty == 0 (cycle closed).
        Stores lowest BUY and highest SELL from current open orders.
        """
        if pos_qty is None or pos_qty != 0:
            return
        snap = self._last_account_snapshot
        if snap is None:
            return
        buy_prices: list[Decimal] = []
        sell_prices: list[Decimal] = []
        for o in snap.open_orders:
            if o.symbol != symbol:
                continue
            if is_tp_order(o.order_id):
                continue
            if o.side.upper() == "BUY":
                buy_prices.append(o.price)
            elif o.side.upper() == "SELL":
                sell_prices.append(o.price)
        if buy_prices:
            self._grid_anchor_low_buy[symbol] = min(buy_prices)
        if sell_prices:
            self._grid_anchor_high_sell[symbol] = max(sell_prices)

    def _generate_tp_fill_replenish(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        symbol: str,
        pos_qty: Decimal | None,
        ts_ms: int,
    ) -> list[ExecutionAction]:
        """Generate inward BUY + outward SELL after partial TP close (PR-ROLL-3b).

        LONG (pos_qty > 0): BUY above highest_buy (inward), SELL above highest_sell (outward).
        SHORT (pos_qty < 0): SELL below lowest_sell (inward), BUY below lowest_buy (outward).

        Guards: mid-cross → skip inward order. Missing anchors → [].
        """
        if not self._replenish_on_tp_fill:
            return []
        if pos_qty is None or pos_qty == 0:
            return []

        snap = self._last_account_snapshot
        if snap is None:
            return []

        # Get grid planner config for spacing/tick/qty
        planners = self._grid_planners
        if not planners:
            return []
        planner = planners.get(symbol)
        if planner is None:
            return []
        cfg = planner._config
        spacing_bps = cfg.base_spacing_bps
        tick_size = cfg.tick_size
        qty = cfg.size_per_level

        if tick_size is None or tick_size <= 0:
            return []

        # Collect current grid orders (exclude TPs)
        buy_prices: list[Decimal] = []
        sell_prices: list[Decimal] = []
        for o in snap.open_orders:
            if o.symbol != symbol:
                continue
            if is_tp_order(o.order_id):
                continue
            if o.side.upper() == "BUY":
                buy_prices.append(o.price)
            elif o.side.upper() == "SELL":
                sell_prices.append(o.price)

        is_long = pos_qty > 0

        spacing_factor = Decimal(str(spacing_bps)) / Decimal("10000")
        identity = OrderIdentityConfig(
            prefix=DEFAULT_PREFIX,
            strategy_id=DEFAULT_STRATEGY_ID,
            require_strategy_allowlist=False,
        )

        if is_long:
            # LONG: BUY inward (above highest_buy), SELL outward (above highest_sell)
            highest_buy = max(buy_prices) if buy_prices else self._grid_anchor_low_buy.get(symbol)
            highest_sell = (
                max(sell_prices) if sell_prices else self._grid_anchor_high_sell.get(symbol)
            )
            if highest_buy is None or highest_sell is None:
                logger.warning(
                    "TP_FILL_REPLENISH_SKIPPED symbol=%s -- no anchor "
                    "(highest_buy=%s highest_sell=%s)",
                    symbol,
                    highest_buy,
                    highest_sell,
                )
                return []
            new_buy_raw = highest_buy * (Decimal("1") + spacing_factor)
            new_sell_raw = highest_sell * (Decimal("1") + spacing_factor)
            buy_anchor_label = "highest_buy"
            buy_anchor_val = highest_buy
            sell_anchor_label = "highest_sell"
            sell_anchor_val = highest_sell
        else:
            # SHORT: SELL inward (below lowest_sell), BUY outward (below lowest_buy)
            lowest_sell = (
                min(sell_prices) if sell_prices else self._grid_anchor_high_sell.get(symbol)
            )
            lowest_buy = min(buy_prices) if buy_prices else self._grid_anchor_low_buy.get(symbol)
            if lowest_sell is None or lowest_buy is None:
                logger.warning(
                    "TP_FILL_REPLENISH_SKIPPED symbol=%s -- no anchor "
                    "(lowest_sell=%s lowest_buy=%s)",
                    symbol,
                    lowest_sell,
                    lowest_buy,
                )
                return []
            new_sell_raw = lowest_sell * (Decimal("1") - spacing_factor)
            new_buy_raw = lowest_buy * (Decimal("1") - spacing_factor)
            buy_anchor_label = "lowest_buy"
            buy_anchor_val = lowest_buy
            sell_anchor_label = "lowest_sell"
            sell_anchor_val = lowest_sell

        new_buy_price = (new_buy_raw / tick_size).quantize(
            Decimal("1"), rounding=ROUND_DOWN
        ) * tick_size
        new_sell_price = (new_sell_raw / tick_size).quantize(
            Decimal("1"), rounding=ROUND_DOWN
        ) * tick_size

        # Guard: mid-cross — inward order must not cross mid
        mid = self._grid_anchor_mid.get(symbol)

        actions: list[ExecutionAction] = []
        skip_buy = False
        skip_sell = False

        if is_long and mid is not None and new_buy_price >= mid:
            logger.warning(
                "TP_FILL_REPLENISH_MID_CROSS symbol=%s side=BUY price=%s >= mid=%s -- skipped",
                symbol,
                new_buy_price,
                mid,
            )
            skip_buy = True
        if not is_long and mid is not None and new_sell_price <= mid:
            logger.warning(
                "TP_FILL_REPLENISH_MID_CROSS symbol=%s side=SELL price=%s <= mid=%s -- skipped",
                symbol,
                new_sell_price,
                mid,
            )
            skip_sell = True

        if not skip_buy:
            self._tp_fill_replenish_seq += 1
            buy_id = generate_client_order_id(
                config=identity,
                symbol=symbol,
                level_id=0,
                ts=ts_ms,
                seq=self._tp_fill_replenish_seq,
            )
            actions.append(
                ExecutionAction(
                    action_type=ActionType.PLACE,
                    symbol=symbol,
                    side=OrderSide.BUY,
                    price=new_buy_price,
                    quantity=qty,
                    level_id=0,
                    reason="TP_FILL_REPLENISH",
                    reduce_only=False,
                    client_order_id=buy_id,
                )
            )

        if not skip_sell:
            self._tp_fill_replenish_seq += 1
            sell_id = generate_client_order_id(
                config=identity,
                symbol=symbol,
                level_id=0,
                ts=ts_ms,
                seq=self._tp_fill_replenish_seq,
            )
            actions.append(
                ExecutionAction(
                    action_type=ActionType.PLACE,
                    symbol=symbol,
                    side=OrderSide.SELL,
                    price=new_sell_price,
                    quantity=qty,
                    level_id=0,
                    reason="TP_FILL_REPLENISH",
                    reduce_only=False,
                    client_order_id=sell_id,
                )
            )

        if actions:
            logger.warning(
                "TP_FILL_REPLENISH symbol=%s pos_qty=%s dir=%s "
                "buy=%s sell=%s %s=%s %s=%s spacing=%s bps",
                symbol,
                pos_qty,
                "LONG_INWARD" if is_long else "SHORT_INWARD",
                new_buy_price if not skip_buy else "SKIPPED",
                new_sell_price if not skip_sell else "SKIPPED",
                buy_anchor_label,
                buy_anchor_val,
                sell_anchor_label,
                sell_anchor_val,
                spacing_bps,
            )
        return actions

    def _apply_convergence_guards(  # noqa: PLR0912
        self,
        symbol: str,
        actions: list[ExecutionAction],
        plan_result: GridPlanResult,
        ts_ms: int,
    ) -> list[ExecutionAction]:
        """PR-P0-RACE-1: convergence guards for planner/grid-shift path.

        Three independent guards:
        1. Sync-gated planner: skip planner until AccountSync refreshes after dispatch.
        2. Cancel-first on extras: if diff_extra > 0, only CANCEL actions pass.
        3. Budget pre-check: if PLACEs > budget remaining, entire shift deferred.

        Scope: ONLY planner/grid actions. Cycle layer (TP/replenish) is
        appended AFTER this method and is never filtered by it.

        Returns filtered actions list.
        """
        if not self._converge_first_enabled:
            return actions

        if not actions:
            # INV-10 fix: clear stale inflight latch when planner confirms convergence.
            # actions=[] means planner found no diff — orders match desired state.
            # Precondition: this method is only called from the live planner path
            # (line 806), not the budget_dead/grid_frozen path. So actions=[]
            # genuinely means "planner found no diff", not suppression.
            # If sync has refreshed since dispatch, convergence is confirmed.
            inflight = self._inflight_shift.get(symbol)
            if inflight is not None and self._account_sync_generation > inflight.sync_gen:
                self._inflight_shift.pop(symbol, None)
                self._inflight_deferred_logged.discard(symbol)
                # ADR-089: explicit log when stale inflight cleared on convergence
                logger.info(
                    "INFLIGHT_STALE_CLEARED symbol=%s sync_gen=%d inflight_gen=%d",
                    symbol,
                    self._account_sync_generation,
                    inflight.sync_gen,
                )
            return actions

        # Guard 1: Inflight latch — wait for sync refresh after dispatch
        convergence_cleared = False
        inflight = self._inflight_shift.get(symbol)
        if inflight is not None:
            # Check timeout first (30s safety valve)
            elapsed = ts_ms - inflight.ts_ms
            if elapsed > _CONVERGENCE_TIMEOUT_MS:
                logger.warning(
                    "INFLIGHT_GENERATION_TIMEOUT symbol=%s elapsed_ms=%d",
                    symbol,
                    elapsed,
                )
                self._inflight_shift.pop(symbol, None)
                self._inflight_deferred_logged.discard(symbol)
                convergence_cleared = True
            elif self._account_sync_generation <= inflight.sync_gen:
                # Sync hasn't refreshed since dispatch → skip planner entirely.
                # BUG-3 fix: log only once per latch cycle, not every WS tick.
                if symbol not in self._inflight_deferred_logged:
                    logger.info(
                        "GRID_SHIFT_DEFERRED reason=INFLIGHT_GENERATION symbol=%s "
                        "sync_gen=%d inflight_gen=%d",
                        symbol,
                        self._account_sync_generation,
                        inflight.sync_gen,
                    )
                    self._inflight_deferred_logged.add(symbol)
                return []
            elif plan_result.diff_extra == 0 or (
                plan_result.diff_extra > 0 and plan_result.diff_extra == plan_result.diff_extra_tp
            ):
                # Converged: no non-TP extras after fresh sync → clear latch.
                # TP extras are intentional (INV-9b) and do not block convergence.
                self._inflight_shift.pop(symbol, None)
                self._inflight_deferred_logged.discard(symbol)
                convergence_cleared = True
            # else: sync refreshed but non-TP extras > 0 → fall through to Guard 2

        # Guard 2: Cancel-first on extras (no inflight, latch just cleared, or post-timeout)
        # INV-9b: TP extras are intentional (not cancelled) — only grid extras block.
        non_tp_extras = plan_result.diff_extra - plan_result.diff_extra_tp
        if non_tp_extras > 0:
            filtered = [a for a in actions if a.action_type == ActionType.CANCEL]
            logger.warning(
                "PLACEMENT_DEFERRED reason=ACCOUNT_SYNC_NOT_CONVERGED "
                "symbol=%s extras=%d tp_extras=%d open=%d desired=%d",
                symbol,
                plan_result.diff_extra,
                plan_result.diff_extra_tp,
                plan_result.actual_count,
                plan_result.desired_count,
            )
            return filtered

        # Guard 3: Budget pre-check
        place_count = sum(1 for a in actions if a.action_type == ActionType.PLACE)
        budget = self._exchange_port.orders_remaining()
        if budget is not None and place_count > 0 and budget < place_count:
            reason = "ORDER_BUDGET_EXHAUSTED" if budget <= 0 else "ORDER_BUDGET_NEAR_EXHAUSTION"
            logger.warning(
                "GRID_SHIFT_DEFERRED reason=%s symbol=%s budget_remaining=%d shift_cost=%d",
                reason,
                symbol,
                budget,
                place_count,
            )
            return []

        # Guard 4 (ADR-090): Unreconciled placement hard cap.
        if self._rolling_grid_enabled and self._grid_planners:
            planner = self._grid_planners.get(symbol)
            if planner is not None:
                cap = planner._config.levels * 2
                sym_counts = self._unreconciled_place_count.get(symbol, {})
                suppressed_sides: set[str] = set()
                for side_str in ("BUY", "SELL"):
                    if sym_counts.get(side_str, 0) >= cap:
                        suppressed_sides.add(side_str)
                if suppressed_sides:
                    actions = [
                        a
                        for a in actions
                        if not (
                            a.action_type == ActionType.PLACE
                            and a.side is not None
                            and a.side.value in suppressed_sides
                        )
                    ]
                    logger.warning(
                        "PLACEMENT_CAPPED symbol=%s suppressed_sides=%s unreconciled=%s cap=%d",
                        symbol,
                        suppressed_sides,
                        sym_counts,
                        cap,
                    )

        # All guards passed — record inflight if PLACEs dispatched.
        # BUG-3 fix: after convergence just cleared, skip re-latch for pure
        # shifts (cancel_count >= place_count). This avoids the re-latch cycle
        # where every sync clears the latch, planner shifts, and re-latches.
        # Net-new PLACEs (initial placement, fill recovery) still latch.
        cancel_count = sum(1 for a in actions if a.action_type == ActionType.CANCEL)
        is_pure_shift = cancel_count >= place_count > 0
        if place_count > 0 and not (convergence_cleared and is_pure_shift):
            self._inflight_shift[symbol] = _InflightShift(
                sync_gen=self._account_sync_generation,
                place_count=place_count,
                ts_ms=ts_ms,
            )

        return actions

    def _filter_grid_shift(
        self,
        symbol: str,
        mid_price: Decimal,
        actions: list[ExecutionAction],
    ) -> list[ExecutionAction]:
        """Anti-churn: suppress GRID_SHIFT actions if mid hasn't moved enough.

        Keeps GRID_FILL, GRID_TRIM, and other non-GRID_SHIFT reasons.
        Only suppresses GRID_SHIFT (price mismatch) when min_move_bps is set
        and mid hasn't moved beyond threshold from anchor.

        On first call (no anchor), sets anchor and passes through.
        When GRID_SHIFT passes through, anchor is updated.
        """
        min_bps = self._grid_shift_min_move_bps
        if min_bps <= 0:
            return actions

        has_grid_shift = any(a.reason == "GRID_SHIFT" for a in actions)
        if not has_grid_shift:
            # No GRID_SHIFT actions — nothing to suppress, set anchor if missing
            if symbol not in self._grid_anchor_mid:
                self._grid_anchor_mid[symbol] = mid_price
            return actions

        anchor = self._grid_anchor_mid.get(symbol)
        if anchor is None:
            # First time: set anchor and allow
            self._grid_anchor_mid[symbol] = mid_price
            return actions

        # Compute move from anchor in bps
        move_bps = float(abs(mid_price - anchor) / anchor) * 10_000 if anchor > 0 else 0.0

        if move_bps < min_bps:
            # Suppress GRID_SHIFT — keep everything else (GRID_FILL, GRID_TRIM, etc.)
            filtered = [a for a in actions if a.reason != "GRID_SHIFT"]
            suppressed = len(actions) - len(filtered)
            if suppressed > 0:
                logger.warning(
                    "GRID_SHIFT_SUPPRESSED symbol=%s move=%.1f bps < threshold=%d bps "
                    "suppressed=%d kept=%d anchor=%.2f mid=%.2f",
                    symbol,
                    move_bps,
                    min_bps,
                    suppressed,
                    len(filtered),
                    float(anchor),
                    float(mid_price),
                )
            return filtered

        # Move exceeds threshold — allow GRID_SHIFT and update anchor
        self._grid_anchor_mid[symbol] = mid_price
        return actions

    def _process_action(self, action: ExecutionAction, ts: int) -> LiveAction:  # noqa: PLR0911
        """Process single action through safety gates and execute.

        Args:
            action: ExecutionAction from PaperEngine or LiveGridPlanner
            ts: Current timestamp

        Returns:
            LiveAction with execution result
        """
        # PR-INV-1: position-aware intent classification
        pos_sign = self._get_position_sign(action.symbol) if action.symbol else None

        # PR-ROLL-1: enforce reduce_only on opposite-side orders
        self._enforce_reduce_only(action, pos_sign)

        intent = classify_intent(action, pos_sign=pos_sign)

        # Gate 1: Arming check
        if not self._config.armed:
            logger.debug("Action blocked: NOT_ARMED (action=%s)", action.action_type.value)
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.NOT_ARMED,
                intent=intent,
            )

        # Gate 2: Mode check
        if self._config.mode != SafeMode.LIVE_TRADE:
            logger.debug(
                "Action blocked: MODE_NOT_LIVE_TRADE (mode=%s, action=%s)",
                self._config.mode.value,
                action.action_type.value,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.MODE_NOT_LIVE_TRADE,
                intent=intent,
            )

        # Gate 3: Kill-switch (blocks PLACE/REPLACE, allows CANCEL)
        if self._config.kill_switch_active and intent != RiskIntent.CANCEL:
            logger.warning(
                "Action blocked: KILL_SWITCH_ACTIVE (intent=%s, action=%s)",
                intent.value,
                action.action_type.value,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.KILL_SWITCH_ACTIVE,
                intent=intent,
            )
        # Note: CANCEL allowed even with kill-switch active

        # Gate 4: Symbol whitelist
        if action.symbol and not self._config.is_symbol_allowed(action.symbol):
            logger.warning(
                "Action blocked: SYMBOL_NOT_WHITELISTED (symbol=%s)",
                action.symbol,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.SYMBOL_NOT_WHITELISTED,
                intent=intent,
            )

        # Gate 5: Max position cap (PR-INV-1)
        if (
            self._config.max_position_usd is not None
            and self._position_notional_usd is not None
            and intent == RiskIntent.INCREASE_RISK
            and self._position_notional_usd >= self._config.max_position_usd
        ):
            logger.warning(
                "Action blocked: MAX_POSITION_EXCEEDED "
                "(symbol=%s side=%s notional=%.2f cap=%.2f intent=%s action=%s)",
                action.symbol,
                action.side.value if action.side else "None",
                self._position_notional_usd,
                self._config.max_position_usd,
                intent.value,
                action.action_type.value,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.MAX_POSITION_EXCEEDED,
                intent=intent,
            )

        # Gate 6: DrawdownGuardV1 (if configured)
        if self._drawdown_guard is not None:
            allow_decision = self._drawdown_guard.allow(intent, symbol=action.symbol or None)
            if not allow_decision.allowed:
                logger.warning(
                    "Action blocked: DRAWDOWN_BLOCKED (intent=%s, reason=%s)",
                    intent.value,
                    allow_decision.reason.value,
                )
                return LiveAction(
                    action=action,
                    status=LiveActionStatus.BLOCKED,
                    block_reason=BlockReason.DRAWDOWN_BLOCKED,
                    intent=intent,
                )

        # Gate 7: FSM state permission (Launch-13)
        # PR-P0-REDUCEONLY-INTENT: reduce_only bypasses FSM gate — TP must
        # always be placeable when position is open (even in INIT/READY).
        if (
            self._fsm_driver is not None
            and not action.reduce_only
            and not self._fsm_driver.check_intent(intent)
        ):
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.FSM_STATE_BLOCKED,
                intent=intent,
            )

        # Gate 8: Fill probability gate (PR-C5, PLACE/REPLACE only)
        if self._fill_model is not None and action.action_type in (
            ActionType.PLACE,
            ActionType.REPLACE,
        ):
            fill_result = self._check_fill_prob(action, intent)
            if fill_result is not None:
                return fill_result

        # SOR routing (Launch-14 PR2): after all safety gates, before execution
        if self._is_sor_enabled() and action.action_type in (
            ActionType.PLACE,
            ActionType.REPLACE,
        ):
            sor_result = self._apply_sor(action, ts, intent)
            if sor_result is not None:
                return sor_result

        # All gates passed - execute action
        return self._execute_action(action, ts, intent)

    def _is_sor_enabled(self) -> bool:
        """Check if SOR routing is active.

        Requires all of: feature flag (config or env), exchange filters, and snapshot.
        """
        flag_on = self._config.sor_enabled or self._sor_env_override
        if not flag_on:
            return False
        if self._exchange_filters is None:
            logger.debug("SOR flag ON but exchange_filters missing, skipping SOR")
            return False
        if self._last_snapshot is None:
            logger.debug("SOR flag ON but no snapshot available, skipping SOR")
            return False
        return True

    def _apply_sor(
        self, action: ExecutionAction, _ts: int, intent: RiskIntent
    ) -> LiveAction | None:
        """Apply SmartOrderRouter to decide execution method.

        Returns LiveAction for BLOCK/NOOP, None to continue with normal execution
        (CANCEL_REPLACE falls through to standard _execute_action).

        Args:
            action: PLACE or REPLACE action from PaperEngine
            ts: Current timestamp
            intent: Risk intent classification

        Returns:
            LiveAction if SOR blocks/skips, None to continue normal execution.
        """
        assert self._exchange_filters is not None  # caller guards via _is_sor_enabled
        assert self._last_snapshot is not None  # caller guards via _is_sor_enabled
        assert action.price is not None
        assert action.quantity is not None
        assert action.side is not None

        router_inputs = RouterInputs(
            intent=SorOrderIntent(
                price=action.price,
                qty=action.quantity,
                side=action.side.value,
            ),
            existing=None,  # PR2: no order state tracking yet
            market=MarketSnapshot(
                best_bid=self._last_snapshot.bid_price,
                best_ask=self._last_snapshot.ask_price,
            ),
            filters=self._exchange_filters,
            drawdown_breached=False,  # Already handled by Gate 6
        )

        result = route(router_inputs)

        # Normalize AMEND to CANCEL_REPLACE before recording metrics (P1-1)
        decision = result.decision
        reason = result.reason
        if decision == RouterDecision.AMEND:
            logger.warning(
                "SOR returned AMEND with existing=None (unreachable), normalizing to CANCEL_REPLACE"
            )
            decision = RouterDecision.CANCEL_REPLACE
            reason = "AMEND_NORMALIZED_TO_CANCEL_REPLACE"

        # Record metric (single call, after normalization)
        get_sor_metrics().record_decision(decision.value, reason)

        if decision == RouterDecision.BLOCK:
            logger.info(
                "SOR blocked action: reason=%s, action=%s",
                reason,
                action.action_type.value,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.ROUTER_BLOCKED,
                intent=intent,
            )

        if decision == RouterDecision.NOOP:
            logger.debug("SOR NOOP: reason=%s", reason)
            return LiveAction(
                action=action,
                status=LiveActionStatus.SKIPPED,
                intent=intent,
            )

        # CANCEL_REPLACE: fall through to normal execution
        return None

    def _check_fill_prob(self, action: ExecutionAction, intent: RiskIntent) -> LiveAction | None:
        """Check fill probability gate for a PLACE/REPLACE action.

        Returns LiveAction on BLOCK, None to continue normal processing.
        Circuit breaker (PR-C8): if tripped, bypass gate → ALLOW.

        Args:
            action: PLACE or REPLACE action from PaperEngine.
            intent: Risk intent classification.

        Returns:
            LiveAction if gate blocks, None to proceed.
        """
        # Circuit breaker: if tripped, bypass gate entirely (fail-open)
        if self._fill_prob_cb.is_tripped():
            get_sor_metrics().record_cb_trip()
            return None

        # Symbol allowlist (PR-C2): if set and symbol not in list, skip gate (ALLOW)
        if (
            self._fill_prob_enforce_symbols is not None
            and action.symbol.upper() not in self._fill_prob_enforce_symbols
        ):
            return None

        assert action.price is not None
        assert action.quantity is not None
        assert action.side is not None

        direction = "long" if action.side.value == "BUY" else "short"
        notional = float(action.price * action.quantity)
        features = extract_online_features(direction=direction, notional=notional)

        result = check_fill_prob(
            model=self._fill_model,
            features=features,
            threshold_bps=self._fill_prob_min_bps,
            enforce=self._fill_prob_enforce,
        )

        # Record verdict in circuit breaker (no-op in shadow mode)
        self._fill_prob_cb.record(result.verdict, enforce=self._fill_prob_enforce)

        # Record metrics
        sor_metrics = get_sor_metrics()

        # Evidence: emit on BLOCK/SHADOW (log + optional artifact)
        if result.verdict in (FillProbVerdict.BLOCK, FillProbVerdict.SHADOW):
            action_meta = {
                "action_type": action.action_type.value,
                "symbol": action.symbol,
                "side": action.side.value,
                "price": str(action.price),
                "qty": str(action.quantity),
            }
            maybe_emit_fill_prob_evidence(
                result=result,
                features=features,
                model=self._fill_model,
                action_meta=action_meta,
            )

        if result.verdict == FillProbVerdict.BLOCK:
            sor_metrics.record_fill_prob_block()
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.FILL_PROB_LOW,
                intent=intent,
            )

        # ALLOW or SHADOW: continue normal processing
        return None

    # --- PR-P0-TP-CLOSE-ATOMIC: retry queue for failed TP_CLOSE PLACEs ---

    @staticmethod
    def _is_tp_close_retryable(live_action: LiveAction) -> bool:
        """Check if failed TP_CLOSE PLACE should be retried.

        Only -4118 (ReduceOnly Order Failed) is retryable — temporary conflict
        from race-duplicate orders that resolves after account sync reconciliation.
        All other failures (budget exhaustion, circuit breaker, gates) are terminal.
        """
        if live_action.status != LiveActionStatus.FAILED:
            return False
        code = _extract_binance_error_code(live_action.error)
        return code is not None and code in _TP_CLOSE_RETRYABLE_CODES

    def _enqueue_tp_close_retry(self, action: ExecutionAction, ts_ms: int) -> None:
        """Enqueue a failed TP_CLOSE PLACE for retry on next tick.

        Invariant: action.correlation_id MUST be set for new TP path.
        Missing correlation_id = generation bug -> log and skip.
        """
        if action.correlation_id is None:
            logger.error(
                "TP_CLOSE_RETRY_INVARIANT_BREACH sym=%s id=%s — "
                "missing correlation_id, cannot enqueue",
                action.symbol,
                action.client_order_id,
            )
            return
        # Overwrite is intentional: one retry slot per correlation_id.
        # If same pair enqueues again, the old entry is stale (already processed).
        self._tp_close_retries[action.correlation_id] = (action, 0, ts_ms)
        logger.warning(
            "TP_CLOSE_RETRY_QUEUED sym=%s id=%s corr=%s",
            action.symbol,
            action.client_order_id,
            action.correlation_id,
        )

    def _process_tp_close_retries(self, symbol: str, ts_ms: int) -> list[LiveAction]:
        """Retry failed TP_CLOSE PLACEs (max 3 retries after initial, 10s cooldown).

        Safe iteration: builds to_update/to_delete, applies AFTER loop.
        """
        results: list[LiveAction] = []
        to_update: dict[str, tuple[ExecutionAction, int, int]] = {}
        to_delete: list[str] = []

        for corr_id, (action, retry_count, last_ts) in list(self._tp_close_retries.items()):
            if (action.symbol or "") != symbol:
                continue
            if retry_count >= _TP_CLOSE_MAX_RETRIES:
                logger.warning(
                    "TP_CLOSE_RETRY_EXHAUSTED sym=%s id=%s corr=%s retries=%d",
                    symbol,
                    action.client_order_id,
                    corr_id,
                    retry_count,
                )
                to_delete.append(corr_id)
                continue
            if ts_ms - last_ts < _TP_CLOSE_RETRY_COOLDOWN_MS:
                continue  # cooldown not elapsed
            live_action = self._process_action(action, ts_ms)
            results.append(live_action)
            if live_action.status == LiveActionStatus.EXECUTED:
                logger.info(
                    "TP_CLOSE_RETRY_OK sym=%s id=%s corr=%s retry=%d",
                    symbol,
                    action.client_order_id,
                    corr_id,
                    retry_count + 1,
                )
                to_delete.append(corr_id)
            else:
                to_update[corr_id] = (action, retry_count + 1, ts_ms)

        # Apply mutations AFTER iteration (safe pattern)
        for corr_id in to_delete:
            self._tp_close_retries.pop(corr_id, None)
        self._tp_close_retries.update(to_update)
        return results

    def _execute_action(self, action: ExecutionAction, ts: int, intent: RiskIntent) -> LiveAction:  # noqa: PLR0912
        """Execute action on exchange port with retries.

        Args:
            action: ExecutionAction to execute
            ts: Current timestamp
            intent: Risk intent classification

        Returns:
            LiveAction with execution result
        """
        if action.action_type == ActionType.NOOP:
            return LiveAction(
                action=action,
                status=LiveActionStatus.SKIPPED,
                intent=intent,
            )

        # PR-VIS-1: log place intent before execution (env-gated)
        if action.action_type == ActionType.PLACE and self._debug_open_orders:
            logger.warning(
                "PLACE_INTENT order_id=%s symbol=%s side=%s price=%s qty=%s "
                "reduceOnly=%s reason=%s",
                action.client_order_id or "?",
                action.symbol,
                action.side.value if action.side else "?",
                action.price,
                action.quantity,
                action.reduce_only,
                action.reason or "planner",
            )

        # P0-2d: log cancel intent before execution (env-gated)
        if action.action_type == ActionType.CANCEL and self._debug_open_orders:
            logger.warning(
                "CANCEL_INTENT order_id=%s symbol=%s reason=%s",
                action.order_id,
                action.symbol,
                action.reason or "planner",
            )

        max_attempts = self._retry_policy.max_attempts
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                order_id = self._execute_single(action, ts)
                live_action = LiveAction(
                    action=action,
                    status=LiveActionStatus.EXECUTED,
                    order_id=order_id,
                    attempts=attempt,
                    intent=intent,
                )
                # P0-2: track successful PLACEs for AccountSync correlation
                if action.action_type == ActionType.PLACE:
                    cid_sent = action.client_order_id or live_action.order_id or ""
                    self._recent_places.append((cid_sent, int(time.time() * 1000), action.symbol))
                    # ADR-090: track dispatched PLACE CID for inflight fill detection
                    if self._rolling_grid_enabled and action.side is not None:
                        self._inflight_placed_cids[cid_sent] = _InflightPlacedOrder(
                            symbol=action.symbol,
                            side=action.side.value,
                            sync_gen=self._account_sync_generation,
                        )
                        sym_counts = self._unreconciled_place_count.setdefault(
                            action.symbol, {"BUY": 0, "SELL": 0}
                        )
                        sym_counts[action.side.value] += 1
                return live_action
            except ConnectorNonRetryableError as e:
                # Non-retryable: fail immediately
                error_msg = str(e)
                logger.error(
                    "Non-retryable error on %s: %s",
                    action.action_type.value,
                    error_msg,
                )
                # Latch: order budget exhausted → suppress planner on future ticks
                if "Order count limit reached" in error_msg and not self._order_budget_exhausted:
                    self._order_budget_exhausted = True
                    logger.warning(
                        "ORDER_BUDGET_LATCH activated — planner suppressed for remaining run"
                    )
                return LiveAction(
                    action=action,
                    status=LiveActionStatus.FAILED,
                    block_reason=BlockReason.NON_RETRYABLE_ERROR,
                    error=error_msg,
                    attempts=attempt,
                    intent=intent,
                )
            except ConnectorTransientError as e:
                # Transient: retry with backoff
                last_error = e
                if attempt < max_attempts:
                    delay_ms = self._retry_policy.compute_delay_ms(attempt)
                    logger.warning(
                        "Transient error on %s (attempt %d/%d), retrying in %dms: %s",
                        action.action_type.value,
                        attempt,
                        max_attempts,
                        delay_ms,
                        str(e),
                    )
                    time.sleep(delay_ms / 1000.0)
            except CircuitOpenError as e:
                # Circuit breaker is OPEN: fail immediately (non-retryable)
                logger.warning(
                    "Circuit breaker OPEN for %s: %s",
                    action.action_type.value,
                    str(e),
                )
                return LiveAction(
                    action=action,
                    status=LiveActionStatus.FAILED,
                    block_reason=BlockReason.CIRCUIT_BREAKER_OPEN,
                    error=str(e),
                    attempts=attempt,
                    intent=intent,
                )
            except ConnectorError as e:
                # Other connector errors: check if retryable
                if is_retryable(e, self._retry_policy):
                    last_error = e
                    if attempt < max_attempts:
                        delay_ms = self._retry_policy.compute_delay_ms(attempt)
                        time.sleep(delay_ms / 1000.0)
                else:
                    return LiveAction(
                        action=action,
                        status=LiveActionStatus.FAILED,
                        block_reason=BlockReason.NON_RETRYABLE_ERROR,
                        error=str(e),
                        attempts=attempt,
                        intent=intent,
                    )

        # All retries exhausted
        logger.error(
            "Max retries exceeded for %s: %s",
            action.action_type.value,
            str(last_error),
        )
        return LiveAction(
            action=action,
            status=LiveActionStatus.FAILED,
            block_reason=BlockReason.MAX_RETRIES_EXCEEDED,
            error=str(last_error) if last_error else "Unknown error",
            attempts=max_attempts,
            intent=intent,
        )

    def _execute_single(self, action: ExecutionAction, ts: int) -> str | None:
        """Execute single action on exchange port (no retries).

        Args:
            action: ExecutionAction to execute
            ts: Current timestamp

        Returns:
            Order ID (str for PLACE/REPLACE, None for CANCEL)

        Raises:
            ConnectorError: On execution failure
        """
        if action.action_type == ActionType.PLACE:
            assert action.side is not None, "PLACE requires side"
            assert action.price is not None, "PLACE requires price"
            assert action.quantity is not None, "PLACE requires quantity"
            return self._exchange_port.place_order(
                symbol=action.symbol,
                side=action.side,
                price=action.price,
                quantity=action.quantity,
                level_id=action.level_id,
                ts=ts,
                reduce_only=action.reduce_only,
                client_order_id=action.client_order_id,
            )
        elif action.action_type == ActionType.CANCEL:
            assert action.order_id is not None, "CANCEL requires order_id"
            success = self._exchange_port.cancel_order(action.order_id)
            return action.order_id if success else None
        elif action.action_type == ActionType.REPLACE:
            assert action.order_id is not None, "REPLACE requires order_id"
            assert action.price is not None, "REPLACE requires new price"
            assert action.quantity is not None, "REPLACE requires new quantity"
            return self._exchange_port.replace_order(
                order_id=action.order_id,
                new_price=action.price,
                new_quantity=action.quantity,
                ts=ts,
            )
        else:
            # NOOP - should not reach here
            return None

    def reset(self) -> None:
        """Reset engine state (for testing)."""
        if hasattr(self._exchange_port, "reset"):
            self._exchange_port.reset()
