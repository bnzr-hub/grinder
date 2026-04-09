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

import contextlib
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from decimal import ROUND_DOWN, ROUND_UP, Decimal
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
from grinder.core import OrderSide, OrderState, SystemState
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
from grinder.grid_v2.adapter import GRID_V2_STRATEGY_ID
from grinder.grid_v2.bridge import FillResult, GridV2Bridge
from grinder.grid_v2.geometry import match_entries_with_tolerance
from grinder.grid_v2.shadow import GridV2ShadowRunner
from grinder.grid_v2.state import ActionIntent, ActionIntentKind, BranchMode, GridV2Config, LotSide
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
from grinder.risk.adaptive_step import AdaptiveStepConfig, AdaptiveStepController, StepFailMode
from grinder.risk.drawdown_guard_v1 import DrawdownGuardV1
from grinder.risk.drawdown_guard_v1 import OrderIntent as RiskIntent
from grinder.risk.emergency_exit import EmergencyExitExecutor
from grinder.risk.emergency_exit_metrics import get_emergency_exit_metrics
from grinder.risk.order_size_policy import (
    OrderSizeInputs,
    OrderSizePolicyConfig,
    compute_target_order_size,
)
from grinder.risk.portfolio_risk import (
    PortfolioRiskConfig,
    RiskGateReason,
    compute_portfolio_notionals,
    compute_symbol_notional,
    evaluate_risk_gate,
)
from grinder.risk.risk_base import (
    BalanceData,
    RiskBaseConfig,
    RiskBaseMode,
    RiskBaseSnapshot,
    build_risk_base_snapshot,
)
from grinder.risk.risk_base_metrics import get_risk_base_metrics
from grinder.risk.symbol_risk_manager import (
    SymbolRiskConfig,
    SymbolRiskManager,
    SymbolRiskSnapshot,
    SymbolRiskState,
)
from grinder.risk.symbol_unload import SymbolUnloadController, UnloadConfig

if TYPE_CHECKING:
    from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
    from grinder.contracts import Snapshot
    from grinder.execution.futures_events import UserDataEvent
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
    from grinder.risk.regime_registry import SharedRegimeRegistry
    from grinder.selection.active_selector import ActiveSelector
    from grinder.selection.shadow_selector import ShadowSelector

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
_GRID_V2_INTEGRITY_MISMATCH_STREAK = 2
_GRID_V2_INTEGRITY_REPAIR_COOLDOWN_MS = 5_000
_GRID_V2_INTEGRITY_MISMATCH_WINDOW_MS = 30_000
_GRID_V2_INTEGRITY_CONVERGENCE_MAX_DEFERS = 6  # after 6 defers, repair proceeds anyway
_GRID_V2_PENDING_CANCEL_STALE_MS = 15_000  # 15s: pending cancel considered stale for convergence
_GRID_V2_PENDING_CANCEL_STALE_GENS = 2  # 2 sync generations: pending cancel stale (dual bound)
_GRID_V2_PENDING_PLACE_STALE_GENS = 4  # 4 sync generations: pending place considered stale
_GRID_V2_ESCALATION_LOG_INTERVAL = 10  # log escalation every Nth defer (anti-spam)
_GRID_V2_DRIFT_RECONSTRUCT_COOLDOWN_GENS = (
    3  # wait 3 sync cycles after FLAT before drift reconstruct
)
# Anti-churn guard defaults (env-overridable)
_GRID_V2_REPAIR_MAX_DISTANCE_STEPS = 5.0  # max distance from mid in grid steps
_GRID_V2_REPAIR_MAX_ACTIONS_PER_CYCLE = 5  # max PLACE actions per repair cycle


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


def _reorder_fill_actions(actions: list[ExecutionAction]) -> list[ExecutionAction]:
    """Reorder fill-generated actions for safe dispatch after a fill.

    Priority order:
    0. reduce-only PLACE (protective exit) — hedge first
    1. CANCEL (opposite-side cleanup) — remove stale exposure
    2. PLACE (new entries) — only after cleanup is dispatched
    """
    if not actions:
        return actions

    def _priority(a: ExecutionAction) -> int:
        if a.action_type == ActionType.PLACE and a.reduce_only:
            return 0  # protective exit first
        if a.action_type == ActionType.CANCEL:
            return 1  # opposite-side cleanup second
        if a.action_type == ActionType.PLACE:
            return 2  # new entries last
        return 3

    return sorted(actions, key=_priority)


@dataclass
class SeedDispatchResult:
    """Result of dispatching a seed batch through the placement path."""

    live_actions: list[Any]  # list[LiveAction] — Any avoids forward-ref
    executed_count: int = 0
    failed_count: int = 0
    blocked_count: int = 0


@dataclass(frozen=True)
class SubmitOutcome:
    """Raw result of an HTTP submit — no engine state mutations.

    Produced by _submit_to_exchange (thread-safe).
    Consumed by _apply_submit_outcome (serial, mutates engine state).
    """

    order_id: str | None = None
    error: str | None = None
    exchange_code: int | None = None
    pre_send: bool = False
    attempts: int = 1
    success: bool = False
    circuit_open: bool = False
    retries_exhausted: bool = False


class GridV2RecoveryMode(Enum):
    """Startup recovery mode for grid_v2 when reconstruction fails."""

    RESTORE_THEN_BLOCK = "restore_then_block"
    RESTORE_THEN_CLEANUP_RESEED = "restore_then_cleanup_reseed"


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
    SELECTOR_BLOCKED = "SELECTOR_BLOCKED"
    SYMBOL_RISK_CAPPED = "SYMBOL_RISK_CAPPED"
    SYMBOL_RISK_EXIT_ONLY = "SYMBOL_RISK_EXIT_ONLY"
    # PR-2 (ADR-092): Risk base enforcement
    RISK_BASE_UNAVAILABLE = "RISK_BASE_UNAVAILABLE"
    RISK_BASE_STALE = "RISK_BASE_STALE"
    RISK_BASE_BELOW_MIN = "RISK_BASE_BELOW_MIN"
    RISK_SYMBOL_CAP = "RISK_SYMBOL_CAP"
    RISK_SYMBOL_DD_FREEZE = "RISK_SYMBOL_DD_FREEZE"
    RISK_SYMBOL_DD_UNLOAD = "RISK_SYMBOL_DD_UNLOAD"
    RISK_SYMBOL_DD_FORCED_FLAT = "RISK_SYMBOL_DD_FORCED_FLAT"
    RISK_PORTFOLIO_GROSS_CAP = "RISK_PORTFOLIO_GROSS_CAP"
    RISK_PORTFOLIO_NET_CAP = "RISK_PORTFOLIO_NET_CAP"
    RISK_PORTFOLIO_DD_FREEZE = "RISK_PORTFOLIO_DD_FREEZE"
    RISK_PORTFOLIO_DD_FORCE_REDUCE = "RISK_PORTFOLIO_DD_FORCE_REDUCE"
    RISK_PORTFOLIO_DD_KILL_SWITCH = "RISK_PORTFOLIO_DD_KILL_SWITCH"
    REDUCE_ONLY_BUDGET_EXCEEDED = "REDUCE_ONLY_BUDGET_EXCEEDED"
    HEALTH_GATE_UNSAFE = "HEALTH_GATE_UNSAFE"
    NOTIONAL_TOO_LOW = "NOTIONAL_TOO_LOW"


# PR-2 (ADR-092): Map RiskGateReason → BlockReason for risk base enforcement gate.
_RISK_GATE_TO_BLOCK: dict[RiskGateReason | None, BlockReason] = {
    RiskGateReason.RISK_BASE_UNAVAILABLE: BlockReason.RISK_BASE_UNAVAILABLE,
    RiskGateReason.RISK_BASE_STALE: BlockReason.RISK_BASE_STALE,
    RiskGateReason.RISK_BASE_BELOW_MIN: BlockReason.RISK_BASE_BELOW_MIN,
    RiskGateReason.SYMBOL_CAP_EXCEEDED: BlockReason.RISK_SYMBOL_CAP,
    RiskGateReason.SYMBOL_DD_FREEZE: BlockReason.RISK_SYMBOL_DD_FREEZE,
    RiskGateReason.SYMBOL_DD_UNLOAD: BlockReason.RISK_SYMBOL_DD_UNLOAD,
    RiskGateReason.SYMBOL_DD_FORCED_FLAT: BlockReason.RISK_SYMBOL_DD_FORCED_FLAT,
    RiskGateReason.PORTFOLIO_GROSS_CAP_EXCEEDED: BlockReason.RISK_PORTFOLIO_GROSS_CAP,
    RiskGateReason.PORTFOLIO_NET_CAP_EXCEEDED: BlockReason.RISK_PORTFOLIO_NET_CAP,
    RiskGateReason.PORTFOLIO_DD_FREEZE: BlockReason.RISK_PORTFOLIO_DD_FREEZE,
    RiskGateReason.PORTFOLIO_DD_FORCE_REDUCE: BlockReason.RISK_PORTFOLIO_DD_FORCE_REDUCE,
    RiskGateReason.PORTFOLIO_DD_KILL_SWITCH: BlockReason.RISK_PORTFOLIO_DD_KILL_SWITCH,
}


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
    pre_send: bool = False  # True if error occurred before HTTP request
    exchange_code: int | None = None  # Binance error code if from exchange
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


# Binance error codes that indicate the order might actually exist on exchange
# Only -2010 is genuinely ambiguous: "New order rejected" can mean
# duplicate CID after network retry where the first attempt succeeded.
# All other codes (-2019 margin, -1111 precision, etc) are definitive rejects.
_AMBIGUOUS_EXCHANGE_CODES: frozenset[int] = frozenset(
    {
        -2010,  # "New order rejected" — can be duplicate CID after retry
    }
)


def _grid_v2_is_exchange_code_ambiguous(exchange_code: int | None) -> bool:
    """Return True if the exchange_code is ambiguous (order might exist).

    None (no exchange response) is always ambiguous (network error / retry exhaustion).
    Codes in _AMBIGUOUS_EXCHANGE_CODES are ambiguous (duplicate-like).
    All other codes are definitive rejects (order was NOT placed).
    """
    if exchange_code is None:
        return True
    return exchange_code in _AMBIGUOUS_EXCHANGE_CODES


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

    def __init__(  # noqa: PLR0915, PLR0912
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
        shadow_selector: ShadowSelector | None = None,
        active_selector: ActiveSelector | None = None,
        operator_symbols: list[str] | None = None,
        regime_registry: SharedRegimeRegistry | None = None,
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
            shadow_selector: Optional ShadowSelector for Phase 1 observability (doc-36). None = disabled.
            active_selector: Optional ActiveSelector for Phase 2 controlled activation (doc-36). None = disabled.
            operator_symbols: Operator-configured symbol universe for selector.
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
        # Doc-36 Phase 1: shadow selector (observability only, no dispatch mutation)
        self._shadow_selector = shadow_selector
        # Doc-36 Phase 2: active selector (controlled activation)
        self._active_selector = active_selector
        self._operator_symbols = operator_symbols or []
        # Shared regime registry for portfolio-level regime aggregation
        self._regime_registry = regime_registry
        # Force-reduce: set by orchestration when day mode requires active risk reduction.
        # Connected to SymbolUnloadController in _update_risk_state().
        self._force_reduce_requested: bool = False
        self._force_reduce_reason: str = ""
        self._force_reduce_flat_logged: bool = False
        self._force_reduce_exits_cleared: bool = False
        # Forced-flat: symbol-scoped emergency close at adverse price level 20.
        # Stronger than force-reduce — cancels orders + market-closes position.
        self._forced_flat_requested: bool = False
        self._forced_flat_executed: bool = False
        # Min-notional cache: symbol → Decimal. Loaded from constraint provider at init.
        # Used by pre-send notional gate to block sub-minimum orders before HTTP.
        # Only loaded when operator_symbols are set (autonomous/production path).
        self._min_notional_cache: dict[str, Decimal] = {}
        self._tick_size_cache: dict[str, Decimal] = {}
        self._step_size_cache: dict[str, Decimal] = {}
        if operator_symbols:
            self._load_min_notional_cache()
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
        # Account sync throttle: at most once per interval to avoid REST rate-limits.
        # When EventLedger is trusted and user-data events are fresh, the interval
        # extends to reduce unnecessary REST polls.
        self._account_sync_interval_ms: int = 5_000  # base interval (used when not trusted)
        self._account_sync_trusted_interval_ms: int = 15_000  # extended when ledger trusted
        self._account_sync_last_attempt_ms: int = -(5_000)  # ensures first tick always syncs
        self._last_user_data_event_mono: float = 0.0  # monotonic ts of last WS order event
        self._last_position_event_mono: float = 0.0  # monotonic ts of last WS position event
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
        # ADR-090 follow-up: cross-tick CANCEL dedup within one sync cycle.
        # Cleared on AccountSync refresh (snapshot reflects cancel result).
        self._cancel_dispatched_pending_sync: set[str] = set()
        self._account_sync_generation: int = 0
        # Live Health Gate (PR-1 of production program)
        from grinder.live.health_gate import (  # noqa: PLC0415
            LiveHealthConfig,
            LiveHealthInput,
            LiveHealthMode,
        )

        self._health_config = LiveHealthConfig()
        self._health_input = LiveHealthInput()
        self._health_mode = LiveHealthMode.HEALTHY
        self._health_mode_prev = LiveHealthMode.HEALTHY
        # ADR-109 Phase 2: EventLedger as trusted read model (healthy mode)
        from grinder.account.event_ledger import EventLedger  # noqa: PLC0415
        from grinder.account.position_ledger import PositionLedger  # noqa: PLC0415

        self._event_ledger = EventLedger()
        self._position_ledger = PositionLedger()
        # ADR-102: Risk-Saturated Mode — per-symbol tracking
        # Consecutive RISK_SYMBOL_CAP blocks (reset on allow, non-cap block, or sync headroom)
        self._risk_cap_consecutive_blocks: dict[str, int] = {}
        # Threshold: N consecutive cap blocks → enter saturation
        self._risk_saturation_threshold = int(
            os.environ.get("GRINDER_RISK_SATURATION_THRESHOLD", "3")
        )
        # Currently saturated symbols
        self._risk_saturated_symbols: set[str] = set()
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
        # Emergency exit executor: used for both global emergency and forced-flat.
        # Created whenever port supports it, regardless of GRINDER_EMERGENCY_EXIT_ENABLED.
        port = self._exchange_port
        _port_supports_emergency = (
            hasattr(port, "cancel_all_orders")
            and hasattr(port, "place_market_order")
            and hasattr(port, "get_positions")
        )
        if _port_supports_emergency:
            self._emergency_exit_executor = EmergencyExitExecutor(port)  # type: ignore[arg-type]

        if self._emergency_exit_enabled:
            if _port_supports_emergency:
                logger.info("RISK-EE-1: EmergencyExitExecutor enabled")
            else:
                logger.warning(
                    "RISK-EE-1: GRINDER_EMERGENCY_EXIT_ENABLED=1 but port lacks "
                    "cancel_all_orders/place_market_order/get_positions — executor not created"
                )
        ee_metrics = get_emergency_exit_metrics()
        ee_metrics.set_enabled(self._emergency_exit_enabled)

        # PR-1 (ADR-092): Risk base from exchange balance (plumbing only, no dispatch blocking)
        _rb_enabled = parse_bool("GRINDER_RISK_BASE_ENABLED", default=False, strict=False)
        _rb_mode_raw = parse_enum(
            "GRINDER_RISK_BASE_MODE",
            allowed={"total_margin_balance", "wallet_balance", "available_balance"},
            default="total_margin_balance",
            strict=False,
        )
        self._risk_base_config = RiskBaseConfig(
            mode=RiskBaseMode(_rb_mode_raw) if _rb_mode_raw else RiskBaseMode.TOTAL_MARGIN_BALANCE,
            min_usd=float(os.environ.get("GRINDER_RISK_BASE_MIN_USD", "50")),
            stale_ttl_s=parse_int(
                "GRINDER_RISK_BASE_STALE_TTL_S", default=30, min_value=0, strict=False
            )
            or 30,
            max_age_hard_s=parse_int(
                "GRINDER_RISK_BASE_MAX_AGE_HARD_S", default=60, min_value=0, strict=False
            )
            or 60,
        )
        self._risk_base_enabled = _rb_enabled
        self._risk_base_snapshot: RiskBaseSnapshot | None = None

        # PR-2/extended: Portfolio risk enforcement config
        def _frac_from_env(new_pct_var: str, legacy_frac_var: str, default: float = 0.0) -> float:
            """Parse percentage or legacy fraction env into fraction value.

            Priority:
            1) new_pct_var: integer/float percentage semantics (e.g. 33 = 33%).
            2) legacy_frac_var: fraction semantics (e.g. 0.33 = 33%).
            """
            raw_new = os.environ.get(new_pct_var, "").strip()
            if raw_new:
                try:
                    pct = float(raw_new)
                except ValueError as exc:
                    raise ValueError(
                        f"{new_pct_var} must be numeric percentage, got {raw_new!r}"
                    ) from exc
                if pct < 0:
                    raise ValueError(f"{new_pct_var} must be >= 0, got {pct}")
                return pct / 100.0
            raw_old = os.environ.get(legacy_frac_var, "").strip()
            if raw_old:
                try:
                    frac = float(raw_old)
                except ValueError as exc:
                    raise ValueError(
                        f"{legacy_frac_var} must be numeric fraction, got {raw_old!r}"
                    ) from exc
                if frac < 0:
                    raise ValueError(f"{legacy_frac_var} must be >= 0, got {frac}")
                return frac
            return default

        _symbol_budget_frac = _frac_from_env(
            "GRINDER_SYMBOL_RISK_BUDGET_PCT",
            "GRINDER_SYMBOL_RISK_MAX_NOTIONAL_PCT",
            default=0.0,
        )
        _symbol_leverage_x = float(os.environ.get("GRINDER_SYMBOL_RISK_LEVERAGE_X", "1.0"))
        if _symbol_leverage_x < 1.0:
            raise ValueError(
                f"GRINDER_SYMBOL_RISK_LEVERAGE_X must be >= 1.0, got {_symbol_leverage_x}"
            )
        _symbol_cap_frac = _symbol_budget_frac * _symbol_leverage_x
        self._portfolio_risk_config = PortfolioRiskConfig(
            symbol_max_notional_pct=_symbol_cap_frac,
            portfolio_max_gross_notional_pct=_frac_from_env(
                "GRINDER_PORTFOLIO_RISK_GROSS_CAP_PCT",
                "GRINDER_PORTFOLIO_RISK_MAX_GROSS_NOTIONAL_PCT",
                default=0.0,
            ),
            portfolio_max_net_notional_pct=_frac_from_env(
                "GRINDER_PORTFOLIO_RISK_NET_CAP_PCT",
                "GRINDER_PORTFOLIO_RISK_MAX_NET_NOTIONAL_PCT",
                default=0.0,
            ),
            symbol_freeze_dd_pct=float(os.environ.get("GRINDER_SYMBOL_RISK_FREEZE_DD_PCT", "0")),
            symbol_unload_dd_pct=float(os.environ.get("GRINDER_SYMBOL_RISK_UNLOAD_DD_PCT", "0")),
            symbol_forced_flat_dd_pct=float(
                os.environ.get("GRINDER_SYMBOL_RISK_FORCED_FLAT_DD_PCT", "0")
            ),
            portfolio_dd_freeze_pct=float(
                os.environ.get("GRINDER_PORTFOLIO_RISK_DD_FREEZE_PCT", "0")
            ),
            portfolio_dd_force_reduce_pct=float(
                os.environ.get("GRINDER_PORTFOLIO_RISK_DD_FORCE_REDUCE_PCT", "0")
            ),
            portfolio_dd_kill_switch_pct=float(
                os.environ.get("GRINDER_PORTFOLIO_RISK_DD_KILL_SWITCH_PCT", "0")
            ),
        )
        if _rb_enabled:
            logger.info(
                "RISK_BASE_ENABLED mode=%s min_usd=%.2f stale_ttl=%ds hard_age=%ds "
                "sym_cap=%.4f (budget=%.4f * lev=%.2f) gross_cap=%.4f net_cap=%.4f "
                "sym_dd(f/u/ff)=%.2f/%.2f/%.2f pf_dd(f/r/k)=%.2f/%.2f/%.2f",
                self._risk_base_config.mode.value,
                self._risk_base_config.min_usd,
                self._risk_base_config.stale_ttl_s,
                self._risk_base_config.max_age_hard_s,
                self._portfolio_risk_config.symbol_max_notional_pct,
                _symbol_budget_frac,
                _symbol_leverage_x,
                self._portfolio_risk_config.portfolio_max_gross_notional_pct,
                self._portfolio_risk_config.portfolio_max_net_notional_pct,
                self._portfolio_risk_config.symbol_freeze_dd_pct,
                self._portfolio_risk_config.symbol_unload_dd_pct,
                self._portfolio_risk_config.symbol_forced_flat_dd_pct,
                self._portfolio_risk_config.portfolio_dd_freeze_pct,
                self._portfolio_risk_config.portfolio_dd_force_reduce_pct,
                self._portfolio_risk_config.portfolio_dd_kill_switch_pct,
            )

        # PR-ROLL-1b: log enforcement status at startup
        logger.info(
            "Reduce-only enforcement: %s",
            "enabled" if self._reduce_only_enforcement else "disabled",
        )
        logger.info(
            "Rolling grid mode: %s",
            "enabled" if self._rolling_grid_enabled else "disabled",
        )

        # PR4 (doc-27): Grid V2 runtime switch (safe-by-default, off)
        self._grid_v2_enabled = parse_bool("GRINDER_GRID_V2_ENABLED", default=False, strict=False)
        self._grid_v2_symbol: str = os.environ.get("GRINDER_GRID_V2_SYMBOL", "")
        self._grid_v2_bridge: GridV2Bridge | None = None
        self._grid_v2_started = False
        self._grid_v2_seed_actions: list[ExecutionAction] = []
        self._grid_v2_pending_cancels: dict[str, tuple[int, int]] = {}  # cid → (ts_ms, sync_gen)
        self._grid_v2_awaiting_sync = False  # PR6: skip fill detection until seed CIDs visible
        self._fill_sync_skip_used = False  # one-shot: skip sync on first fill tick only
        self._seed_gate_only_mode = False  # when True, _process_action skips HTTP
        self._seed_gate_passed_intents: list[tuple[ExecutionAction, Any]] = []
        self._grid_v2_pending_seed_cids: frozenset[str] = frozenset()  # PR6: CIDs to confirm
        # cid → sync_gen at dispatch. Released after visibility OR 2 sync cycles grace.
        self._grid_v2_pending_place_cids: dict[str, int] = {}
        # Two-cycle stale-registry tracking: CIDs absent from exchange in
        # previous sync. Only clean CIDs absent in BOTH consecutive syncs
        # to avoid racing with fill detection (filled entries disappear
        # from snapshot but should be processed as fills, not cleaned).
        self._prev_absent_registry_cids: set[str] = set()
        # Definitive-reject blocklist: CIDs cleaned by _grid_v2_clean_failed_place
        # must never be treated as fills. Cleared on bridge reset.
        self._grid_v2_definitively_rejected_cids: set[str] = set()
        # Fill-eligible positive allowlist: only CIDs with credible live-on-exchange
        # evidence (EXECUTED or ambiguous failure) may be treated as fill candidates.
        # Mere registry presence is not sufficient. Cleared on bridge reset.
        self._grid_v2_fill_eligible_cids: set[str] = set()
        self._grid_v2_user_fill_seen: set[str] = set()
        self._grid_v2_integrity_mismatch_streak = 0
        self._grid_v2_integrity_mismatch_last_ts = 0
        self._grid_v2_integrity_repair_cooldown_until_ts = 0
        # Track when SM last became FLAT from a fill (gen-based cooldown for drift detection)
        self._grid_v2_flat_since_gen: int = -1  # -1 = never / not from fill
        self._grid_v2_integrity_convergence_defer_count = 0
        self._grid_v2_repair_max_distance_steps = float(
            os.environ.get(
                "GRINDER_GRID_V2_REPAIR_MAX_DISTANCE_STEPS", str(_GRID_V2_REPAIR_MAX_DISTANCE_STEPS)
            )
        )
        self._grid_v2_repair_max_actions = int(
            os.environ.get(
                "GRINDER_GRID_V2_REPAIR_MAX_ACTIONS_PER_CYCLE",
                str(_GRID_V2_REPAIR_MAX_ACTIONS_PER_CYCLE),
            )
        )
        # Geometry repair config
        self._grid_v2_geometry_repair_enabled = parse_bool(
            "GRINDER_GRID_V2_GEOMETRY_REPAIR_ENABLED", default=True, strict=False
        )
        self._grid_v2_geometry_epsilon_ticks = int(
            os.environ.get("GRINDER_GRID_V2_GEOMETRY_EPSILON_TICKS", "1")
        )
        self._grid_v2_geometry_max_actions = int(
            os.environ.get("GRINDER_GRID_V2_GEOMETRY_MAX_ACTIONS_PER_CYCLE", "3")
        )
        self._grid_v2_geometry_cooldown_ms = int(
            os.environ.get("GRINDER_GRID_V2_GEOMETRY_COOLDOWN_MS", "5000")
        )
        self._grid_v2_geometry_last_repair_ts = 0
        self._grid_v2_repair_strict_geometry = parse_bool(
            "GRINDER_GRID_V2_REPAIR_STRICT_GEOMETRY", default=False, strict=False
        )
        # Sync-driven reconciler (ADR-096)
        self._sync_reconciler_enabled = parse_bool(
            "GRINDER_GRID_V2_SYNC_RECONCILER_ENABLED", default=False, strict=False
        )
        self._sync_reconciler_shadow = parse_bool(
            "GRINDER_GRID_V2_SYNC_RECONCILER_SHADOW", default=True, strict=False
        )
        self._sync_reconciler_primary = parse_bool(
            "GRINDER_GRID_V2_SYNC_RECONCILER_PRIMARY", default=False, strict=False
        )
        self._sync_reconciler_max_actions = int(
            os.environ.get("GRINDER_GRID_V2_RECONCILE_MAX_ACTIONS_PER_SYNC", "10")
        )
        # Startup validation: fail-closed on invalid combos
        if self._sync_reconciler_primary and self._sync_reconciler_shadow:
            raise ValueError(
                "GRINDER_GRID_V2_SYNC_RECONCILER_PRIMARY=1 and "
                "GRINDER_GRID_V2_SYNC_RECONCILER_SHADOW=1 are mutually exclusive"
            )
        if self._sync_reconciler_primary and not self._sync_reconciler_enabled:
            raise ValueError(
                "GRINDER_GRID_V2_SYNC_RECONCILER_PRIMARY=1 requires "
                "GRINDER_GRID_V2_SYNC_RECONCILER_ENABLED=1"
            )
        # Pending actions from reconciler dispatch (for fill-detection exclusion)
        self._sync_reconciler_pending_actions: list[ExecutionAction] = []
        # ADR-112: SM mode at staging time, for stale-mode drain filter
        self._reconciler_staged_mode: str = ""
        # Fill watermark at staging time. If a fill lands before drain,
        # staged PLACE actions were computed on stale pre-fill visibility.
        self._reconciler_staged_fill_ts: int = 0
        # Batch accumulator for reduce-only budget guard (reset per tick)
        self._reduce_only_batch_qty: dict[tuple[str, str], Decimal] = {}
        # Provable current-tick lot additions from fill path (symbol → qty added)
        self._reduce_only_batch_new_lots_qty: dict[str, Decimal] = {}
        # ADR-111: Exchange timestamp of most recent fill (ms).
        # Both sources (user-data oe.ts, reconstructed snapshot.ts) use
        # exchange time — same domain as comparison target snapshot.ts.
        # When snapshot.ts < _last_fill_ts, snapshot is stale → suppress PLACE.
        self._last_fill_ts: int = 0
        self._burst_suppression_fired: bool = False
        # ADR-104: (symbol, side) pairs pending reduce-only repair after -2022 reject.
        # Blocks further reduce-only exits for that direction until sync repairs topology.
        self._reduce_only_pending_repair: set[tuple[str, str]] = set()
        # Adaptive Step Controller v1 (volatility-aware grid spacing)
        # Must be initialized BEFORE _create_grid_v2_bridge() so effective step
        # is available on cold start, not only on subsequent recreations.
        _as_enabled = parse_bool(
            "GRINDER_GRID_V2_ADAPTIVE_STEP_ENABLED", default=False, strict=False
        )
        raw_fail = os.environ.get("GRINDER_GRID_V2_STEP_FAIL_MODE", "freeze_last")
        try:
            _fail_mode = StepFailMode(raw_fail)
        except ValueError:
            _fail_mode = StepFailMode.FREEZE_LAST
        self._adaptive_step = AdaptiveStepController(
            AdaptiveStepConfig(
                enabled=_as_enabled,
                step_min_pct=float(os.environ.get("GRINDER_GRID_V2_STEP_MIN_PCT", "0.0020")),
                step_max_pct=float(os.environ.get("GRINDER_GRID_V2_STEP_MAX_PCT", "0.0100")),
                step_base_pct=float(os.environ.get("GRINDER_GRID_V2_STEP_BASE_PCT", "0.0025")),
                vol_ref_bps=float(os.environ.get("GRINDER_GRID_V2_STEP_VOL_REF_BPS", "100")),
                update_cooldown_s=float(
                    os.environ.get("GRINDER_GRID_V2_STEP_UPDATE_COOLDOWN_S", "60")
                ),
                hysteresis_bps=float(os.environ.get("GRINDER_GRID_V2_STEP_HYSTERESIS_BPS", "10")),
                fail_mode=_fail_mode,
            )
        )
        if _as_enabled:
            logger.info(
                "ADAPTIVE_STEP_ENABLED base=%.4f min=%.4f max=%.4f vol_ref=%.0f cooldown=%.0fs",
                self._adaptive_step.config.step_base_pct,
                self._adaptive_step.config.step_min_pct,
                self._adaptive_step.config.step_max_pct,
                self._adaptive_step.config.vol_ref_bps,
                self._adaptive_step.config.update_cooldown_s,
            )

        # Grid v2 order-size policy (flat-only automatic sizing)
        _os_enabled = parse_bool(
            "GRINDER_GRID_V2_ORDER_SIZE_POLICY_ENABLED", default=False, strict=False
        )
        # Grid_v2 order size: GRINDER_GRID_V2_ORDER_SIZE (explicit) > GRINDER_GRID_V2_CLI_SIZE
        # (set by run_trading from --paper-size-per-level) > default 0.001.
        _raw_env = os.environ.get("GRINDER_GRID_V2_ORDER_SIZE")
        _raw_cli = os.environ.get("GRINDER_GRID_V2_CLI_SIZE")
        if _raw_env:
            _base_order_size = float(_raw_env)
            _size_source = "env:GRINDER_GRID_V2_ORDER_SIZE"
        elif _raw_cli:
            _base_order_size = float(_raw_cli)
            _size_source = "cli:--paper-size-per-level"
        else:
            _base_order_size = 0.001
            _size_source = "default"
        self._grid_v2_order_size_effective = Decimal(str(_base_order_size))
        logger.info("GRID_V2_ORDER_SIZE effective=%s source=%s", _base_order_size, _size_source)
        self._grid_v2_order_size_last_update_ts = 0
        self._grid_v2_order_size_policy = OrderSizePolicyConfig(
            enabled=_os_enabled,
            flat_only=parse_bool(
                "GRINDER_GRID_V2_ORDER_SIZE_FLAT_ONLY", default=True, strict=False
            ),
            update_cooldown_s=float(
                os.environ.get("GRINDER_GRID_V2_ORDER_SIZE_UPDATE_COOLDOWN_S", "60")
            ),
            delta_threshold_pct=float(
                os.environ.get("GRINDER_GRID_V2_ORDER_SIZE_DELTA_THRESHOLD_PCT", "12")
            ),
            base_size=_base_order_size,
            min_size=float(os.environ.get("GRINDER_GRID_V2_ORDER_SIZE_MIN", str(_base_order_size))),
            max_size=float(os.environ.get("GRINDER_GRID_V2_ORDER_SIZE_MAX", str(_base_order_size))),
            natr_ref_bps=float(os.environ.get("GRINDER_GRID_V2_ORDER_SIZE_NATR_REF_BPS", "100")),
            step_ref_bps=float(os.environ.get("GRINDER_GRID_V2_ORDER_SIZE_STEP_REF_BPS", "25")),
            vol_k=float(os.environ.get("GRINDER_GRID_V2_ORDER_SIZE_VOL_K", "1.0")),
            step_k=float(os.environ.get("GRINDER_GRID_V2_ORDER_SIZE_STEP_K", "0.5")),
            ml_enabled=parse_bool(
                "GRINDER_GRID_V2_ORDER_SIZE_ML_ENABLED", default=False, strict=False
            ),
            ml_adjust_max_pct=float(
                os.environ.get("GRINDER_GRID_V2_ORDER_SIZE_ML_ADJUST_MAX_PCT", "80")
            ),
        )
        self._grid_v2_order_size_ml_model: Any | None = None
        if self._grid_v2_order_size_policy.enabled and self._grid_v2_order_size_policy.ml_enabled:
            model_dir = os.environ.get("GRINDER_ML_REGIME_MODEL_DIR", "").strip()
            if model_dir:
                try:
                    from grinder.ml.onnx.model import OnnxMlModel  # noqa: PLC0415

                    self._grid_v2_order_size_ml_model = OnnxMlModel.load_from_dir(model_dir)
                    logger.info("GRID_V2_ORDER_SIZE_ML_PROVIDER_WIRED model_dir=%s", model_dir)
                except Exception:
                    logger.warning(
                        "GRID_V2_ORDER_SIZE_ML_PROVIDER_MISSING reason=model_load_failed model_dir=%s",
                        model_dir,
                        exc_info=True,
                    )
            else:
                logger.warning("GRID_V2_ORDER_SIZE_ML_PROVIDER_MISSING reason=model_dir_unset")
        if self._grid_v2_order_size_policy.enabled:
            logger.info(
                "GRID_V2_ORDER_SIZE_POLICY_ENABLED flat_only=%s cooldown=%.0fs "
                "delta=%.2f base=%.6f min=%.6f max=%.6f ml=%s ml_max=%.2f",
                self._grid_v2_order_size_policy.flat_only,
                self._grid_v2_order_size_policy.update_cooldown_s,
                self._grid_v2_order_size_policy.delta_threshold_pct,
                self._grid_v2_order_size_policy.base_size,
                self._grid_v2_order_size_policy.min_size,
                self._grid_v2_order_size_policy.max_size,
                self._grid_v2_order_size_policy.ml_enabled,
                self._grid_v2_order_size_policy.ml_adjust_max_pct,
            )

        if self._grid_v2_enabled:
            if not self._grid_v2_symbol:
                raise ValueError(
                    "GRINDER_GRID_V2_ENABLED=True requires GRINDER_GRID_V2_SYMBOL to be set"
                )
            self._grid_v2_bridge = self._create_grid_v2_bridge()
            raw_mode = os.environ.get(
                "GRINDER_GRID_V2_RECOVERY_MODE", "restore_then_cleanup_reseed"
            )
            try:
                self._grid_v2_recovery_mode = GridV2RecoveryMode(raw_mode)
            except ValueError:
                valid = [m.value for m in GridV2RecoveryMode]
                raise ValueError(
                    f"Invalid GRINDER_GRID_V2_RECOVERY_MODE={raw_mode!r}, must be one of {valid}"
                ) from None
            import time as _time  # noqa: PLC0415

            self._grid_v2_startup_begin_mono: float = _time.monotonic()
            logger.info(
                "GRID_V2_ENABLED symbol=%s recovery_mode=%s",
                self._grid_v2_symbol,
                self._grid_v2_recovery_mode.value,
            )

        # PR5 (doc-27): Grid V2 shadow mode — run grid_v2 in parallel without dispatch.
        # Mutually exclusive with primary grid_v2 (shadow only runs when primary is OFF).
        self._grid_v2_shadow_enabled = parse_bool(
            "GRINDER_GRID_V2_SHADOW", default=False, strict=False
        )
        self._grid_v2_shadow: GridV2ShadowRunner | None = None
        if self._grid_v2_shadow_enabled and not self._grid_v2_enabled:
            if not self._grid_v2_symbol:
                self._grid_v2_symbol = os.environ.get("GRINDER_GRID_V2_SYMBOL", "")
            if self._grid_v2_symbol:
                shadow_config = GridV2Config(
                    grid_step_pct=Decimal(os.environ.get("GRINDER_GRID_V2_STEP_PCT", "0.0025")),
                    entry_levels_per_side=int(os.environ.get("GRINDER_GRID_V2_ENTRY_LEVELS", "5")),
                    order_size=self._grid_v2_order_size_effective,
                    max_inventory_levels=int(
                        os.environ.get("GRINDER_GRID_V2_MAX_INV_LEVELS", "20")
                    ),
                    max_inventory_notional_usd=Decimal(
                        os.environ.get("GRINDER_GRID_V2_MAX_INV_NOTIONAL", "1000")
                    ),
                    price_tick_size=Decimal(os.environ.get("GRINDER_GRID_V2_TICK_SIZE", "0.01")),
                )
                self._grid_v2_shadow = GridV2ShadowRunner(shadow_config, self._grid_v2_symbol)
                logger.info(
                    "GRID_V2_SHADOW_ENABLED symbol=%s",
                    self._grid_v2_symbol,
                )
            else:
                logger.warning(
                    "GRID_V2_SHADOW_DISABLED reason=missing_symbol "
                    "(GRINDER_GRID_V2_SHADOW=true but GRINDER_GRID_V2_SYMBOL is empty)"
                )

        # Symbol Risk Manager v1 (per-symbol safety layer)
        _sr_enabled = parse_bool("GRINDER_SYMBOL_RISK_ENABLED", default=False, strict=False)
        self._symbol_risk_manager = SymbolRiskManager(
            SymbolRiskConfig(
                enabled=_sr_enabled,
                max_notional_usd=float(os.environ.get("GRINDER_SYMBOL_RISK_MAX_NOTIONAL_USD", "0")),
                max_position_qty=float(os.environ.get("GRINDER_SYMBOL_RISK_MAX_POSITION_QTY", "0")),
                max_consecutive_losses=int(
                    os.environ.get("GRINDER_SYMBOL_RISK_MAX_CONSECUTIVE_LOSSES", "0")
                ),
                cooldown_s=float(os.environ.get("GRINDER_SYMBOL_RISK_COOLDOWN_S", "120")),
                exit_only_ttl_s=float(os.environ.get("GRINDER_SYMBOL_RISK_EXIT_ONLY_TTL_S", "300")),
                applies_to_grid_v2_only=parse_bool(
                    "GRINDER_SYMBOL_RISK_APPLIES_TO_GRID_V2_ONLY", default=True, strict=False
                ),
            )
        )
        # Per-symbol consecutive loss counters (reset on win, increment on loss)
        self._symbol_consecutive_losses: dict[str, int] = {}
        self._symbol_closed_lots_seen: dict[str, int] = {}
        if _sr_enabled:
            logger.info(
                "SYMBOL_RISK_MANAGER_ENABLED max_notional=%.2f max_qty=%.4f max_losses=%d "
                "cooldown=%.0fs exit_only_ttl=%.0fs",
                self._symbol_risk_manager.config.max_notional_usd,
                self._symbol_risk_manager.config.max_position_qty,
                self._symbol_risk_manager.config.max_consecutive_losses,
                self._symbol_risk_manager.config.cooldown_s,
                self._symbol_risk_manager.config.exit_only_ttl_s,
            )

        # Symbol Unload Controller v1 (staged reduce-only in EXIT_ONLY)
        _su_enabled = parse_bool("GRINDER_SYMBOL_UNLOAD_ENABLED", default=False, strict=False)
        self._symbol_unload = SymbolUnloadController(
            UnloadConfig(
                enabled=_su_enabled,
                step_pct=float(os.environ.get("GRINDER_SYMBOL_UNLOAD_STEP_PCT", "0.20")),
                min_step_qty=float(os.environ.get("GRINDER_SYMBOL_UNLOAD_MIN_STEP_QTY", "0")),
                step_cooldown_s=float(
                    os.environ.get("GRINDER_SYMBOL_UNLOAD_STEP_COOLDOWN_S", "20")
                ),
                max_steps_per_hour=int(
                    os.environ.get("GRINDER_SYMBOL_UNLOAD_MAX_STEPS_PER_HOUR", "60")
                ),
                order_ttl_s=float(os.environ.get("GRINDER_SYMBOL_UNLOAD_ORDER_TTL_S", "60")),
                max_reprices_per_step=int(
                    os.environ.get("GRINDER_SYMBOL_UNLOAD_MAX_REPRICES_PER_STEP", "3")
                ),
                reprice_max_bps=int(os.environ.get("GRINDER_SYMBOL_UNLOAD_REPRICE_MAX_BPS", "30")),
                exit_only_required=parse_bool(
                    "GRINDER_SYMBOL_UNLOAD_EXIT_ONLY_REQUIRED", default=True, strict=False
                ),
            )
        )
        if _su_enabled:
            logger.info(
                "SYMBOL_UNLOAD_ENABLED step_pct=%.2f cooldown=%.0fs max_steps/h=%d ttl=%.0fs",
                self._symbol_unload.config.step_pct,
                self._symbol_unload.config.step_cooldown_s,
                self._symbol_unload.config.max_steps_per_hour,
                self._symbol_unload.config.order_ttl_s,
            )

    # --- Grid V2 runtime methods (doc-27 section 23, PR4) ---

    def _create_grid_v2_bridge(self, order_size_override: Decimal | None = None) -> GridV2Bridge:
        """Construct GridV2Bridge from env-var config. Fail-closed on bad config."""
        # Use adaptive step if enabled, else env default
        base_step = os.environ.get("GRINDER_GRID_V2_STEP_PCT", "0.0025")
        if self._adaptive_step.config.enabled and self._grid_v2_symbol:
            effective = self._adaptive_step.get_effective_step(self._grid_v2_symbol)
            base_step = f"{effective:.8f}"
        config = GridV2Config(
            grid_step_pct=Decimal(base_step),
            entry_levels_per_side=int(os.environ.get("GRINDER_GRID_V2_ENTRY_LEVELS", "5")),
            order_size=order_size_override
            if order_size_override is not None
            else self._grid_v2_order_size_effective,
            max_inventory_levels=int(os.environ.get("GRINDER_GRID_V2_MAX_INV_LEVELS", "20")),
            max_inventory_notional_usd=Decimal(
                os.environ.get("GRINDER_GRID_V2_MAX_INV_NOTIONAL", "1000")
            ),
            price_tick_size=self._resolve_grid_v2_tick_size(),
            min_notional=self._min_notional_cache.get(self._grid_v2_symbol, Decimal("0")),
            reseed_on_flat=parse_bool(
                "GRINDER_GRID_V2_RESEED_ON_FLAT", default=False, strict=False
            ),
            reseed_on_flat_only_on_skew=parse_bool(
                "GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW",
                default=True,
                strict=False,
            ),
            reseed_cooldown_ms=int(os.environ.get("GRINDER_GRID_V2_RESEED_COOLDOWN_MS", "30000")),
            netoff_enabled=parse_bool(
                "GRINDER_GRID_V2_NETOFF_ENABLED",
                default=False,
                strict=False,
            ),
        )
        return GridV2Bridge(config, self._grid_v2_symbol)

    def _resolve_grid_v2_tick_size(self) -> Decimal:
        """Resolve tick size for grid_v2 symbol. Fail-closed if missing."""
        env_val = os.environ.get("GRINDER_GRID_V2_TICK_SIZE", "")
        if env_val:
            return Decimal(env_val)
        raise ValueError(
            f"GRINDER_GRID_V2_TICK_SIZE required for grid_v2 symbol={self._grid_v2_symbol}. "
            "Set it to the exchange tick size (e.g., 0.10 for BTCUSDT futures)."
        )

    @staticmethod
    def _feature_to_policy_inputs(features: FeatureSnapshot) -> dict[str, Any]:
        return {
            "natr_bps": features.natr_bps,
            "spread_bps": features.spread_bps,
            "range_score": features.range_score,
            "net_return_bps": features.net_return_bps,
            "imbalance_l1_bps": features.imbalance_l1_bps,
            "warmup_bars": features.warmup_bars,
        }

    def _grid_v2_order_size_ml_adjust_pct(self, symbol: str) -> float:
        """Compute ML adjustment percent for order-size policy.

        Regime mapping:
        - HIGH volatility -> reduce size
        - MID -> no change
        - LOW -> increase size
        """
        model = self._grid_v2_order_size_ml_model
        features = self._last_feature_snapshot
        policy = self._grid_v2_order_size_policy
        if (
            model is None
            or features is None
            or not policy.ml_enabled
            or policy.ml_adjust_max_pct <= 0
            or features.symbol != symbol
        ):
            return 0.0
        try:
            signal = model.predict(
                features.ts,
                symbol,
                self._feature_to_policy_inputs(features),
            )
            if signal is None:
                return 0.0
            if signal.predicted_regime == "HIGH":
                return -policy.ml_adjust_max_pct
            if signal.predicted_regime == "LOW":
                return policy.ml_adjust_max_pct
            return 0.0
        except Exception:
            logger.warning("GRID_V2_ORDER_SIZE_ML_FALLBACK symbol=%s", symbol, exc_info=True)
            return 0.0

    def _grid_v2_risk_headroom_ratio(self, symbol: str) -> float:
        """Estimate remaining headroom ratio from symbol + portfolio caps."""
        snap = self._last_account_snapshot
        base = self._risk_base_snapshot
        if snap is None or base is None or float(base.value_usd) <= 0:
            return 1.0
        base_usd = float(base.value_usd)
        headrooms: list[float] = []

        if self._portfolio_risk_config.symbol_max_notional_pct > 0:
            sym_limit = base_usd * self._portfolio_risk_config.symbol_max_notional_pct
            sym_now = (
                float(abs(self._get_signed_position_qty(symbol)) * self._last_snapshot.mid_price)
                if self._last_snapshot is not None and self._last_snapshot.symbol == symbol
                else float(compute_symbol_notional(snap, symbol))
            )
            if sym_limit > 0:
                headrooms.append(max(0.0, min(1.0, (sym_limit - sym_now) / sym_limit)))

        if self._portfolio_risk_config.portfolio_max_gross_notional_pct > 0:
            gross, _ = compute_portfolio_notionals(snap)
            limit = base_usd * self._portfolio_risk_config.portfolio_max_gross_notional_pct
            if limit > 0:
                headrooms.append(max(0.0, min(1.0, (limit - float(gross)) / limit)))

        if self._portfolio_risk_config.portfolio_max_net_notional_pct > 0:
            _, net = compute_portfolio_notionals(snap)
            limit = base_usd * self._portfolio_risk_config.portfolio_max_net_notional_pct
            if limit > 0:
                headrooms.append(max(0.0, min(1.0, (limit - float(net)) / limit)))

        return min(headrooms) if headrooms else 1.0

    def _grid_v2_apply_size_reseed(self, snapshot: Snapshot, new_size: Decimal) -> None:
        """Apply flat-only controlled reseed with a new order size."""
        bridge = self._grid_v2_bridge
        if bridge is None:
            return

        cancel_actions: list[ExecutionAction] = []
        for cid in sorted(
            set(bridge.adapter.registry.all_entry_cids) | set(bridge.adapter.registry.all_exit_cids)
        ):
            cancel_actions.append(
                ExecutionAction(
                    action_type=ActionType.CANCEL,
                    symbol=self._grid_v2_symbol,
                    order_id=cid,
                    reason="grid_v2_ORDER_SIZE_RESEED_CANCEL",
                )
            )

        self._grid_v2_order_size_effective = new_size
        bridge_fresh = self._create_grid_v2_bridge(order_size_override=new_size)
        seed = list(bridge_fresh.startup_fresh(snapshot.mid_price, snapshot.ts))
        self._grid_v2_bridge = bridge_fresh
        self._grid_v2_started = True
        self._grid_v2_seed_actions = cancel_actions + seed
        self._grid_v2_awaiting_sync = True
        self._grid_v2_pending_seed_cids = frozenset(
            a.order_id for a in seed if a.action_type == ActionType.PLACE and a.order_id
        )
        self._grid_v2_pending_cancels.clear()
        self._grid_v2_pending_place_cids.clear()
        self._grid_v2_definitively_rejected_cids.clear()
        self._grid_v2_fill_eligible_cids.clear()
        logger.warning(
            "GRID_V2_ORDER_SIZE_RESEED_APPLIED symbol=%s old=%s new=%s cancel=%d seed=%d",
            self._grid_v2_symbol,
            bridge._config.order_size,
            new_size,
            len(cancel_actions),
            len(seed),
        )

    def _maybe_update_grid_v2_order_size(self, snapshot: Snapshot) -> None:  # noqa: PLR0911
        """Flat-only automatic order-size update and reseed."""
        policy = self._grid_v2_order_size_policy
        if not policy.enabled or not self._grid_v2_enabled:
            return
        if snapshot.symbol != self._grid_v2_symbol:
            return
        bridge = self._grid_v2_bridge
        if bridge is None or not self._grid_v2_started or not bridge.reconstruction_ok:
            return
        if self._grid_v2_awaiting_sync or self._grid_v2_seed_actions:
            return

        pos_qty = self._get_signed_position_qty(snapshot.symbol)
        has_open_lots = bool(bridge.state_machine and bridge.state_machine.snapshot.open_lots)
        if policy.flat_only and (pos_qty != 0 or has_open_lots):
            return

        if snapshot.ts - self._grid_v2_order_size_last_update_ts < int(
            policy.update_cooldown_s * 1000
        ):
            return
        if self._symbol_risk_manager.get_state(snapshot.symbol) != SymbolRiskState.NORMAL:
            logger.info(
                "GRID_V2_ORDER_SIZE_UPDATE_SKIPPED symbol=%s reason=symbol_risk_state",
                snapshot.symbol,
            )
            return

        step_pct = float(bridge._config.grid_step_pct)
        step_bps = step_pct * 10000.0
        natr_bps = (
            float(self._last_feature_snapshot.natr_bps)
            if self._last_feature_snapshot is not None
            and self._last_feature_snapshot.symbol == snapshot.symbol
            else None
        )
        ml_adjust_pct = self._grid_v2_order_size_ml_adjust_pct(snapshot.symbol)
        current_size = float(bridge._config.order_size)
        decision = compute_target_order_size(
            current_size=current_size,
            config=policy,
            inputs=OrderSizeInputs(
                natr_bps=natr_bps,
                step_bps=step_bps,
                risk_headroom_ratio=self._grid_v2_risk_headroom_ratio(snapshot.symbol),
                ml_adjust_pct=ml_adjust_pct,
            ),
        )
        self._grid_v2_order_size_last_update_ts = snapshot.ts
        if not decision.changed:
            logger.info(
                "GRID_V2_ORDER_SIZE_UPDATE_SKIPPED symbol=%s reason=%s delta=%.2f "
                "current=%.6f target=%.6f",
                snapshot.symbol,
                decision.reason,
                decision.delta_pct,
                current_size,
                decision.target_size,
            )
            return

        self._grid_v2_apply_size_reseed(snapshot, Decimal(str(decision.target_size)))

    def _grid_v2_try_startup(self, snapshot: Snapshot) -> None:  # noqa: PLR0912, PLR0915
        """Attempt grid_v2 bridge startup on first tick with account data.

        Fresh start only if no exchange orders AND position is flat.
        Non-flat position with no g-orders = fail-closed (overexposure guard).
        Failed reconstruct = fail-closed (bridge.reconstruction_ok stays False).
        Called once — sets _grid_v2_started to prevent re-entry.
        """
        bridge = self._grid_v2_bridge
        if bridge is None or self._grid_v2_started:
            return

        acct = self._last_account_snapshot
        if acct is None:
            logger.debug("GRID_V2_STARTUP_DEFERRED reason=no_account_snapshot")
            return

        # Classify exchange orders that belong to grid_v2 (strategy "g")
        g_orders: list[tuple[str, OrderSide, Decimal, Decimal]] = []
        for o in acct.open_orders:
            if o.symbol != self._grid_v2_symbol:
                continue
            parsed = parse_client_order_id(o.order_id)
            if parsed is not None and parsed.strategy_id == GRID_V2_STRATEGY_ID:
                g_orders.append((o.order_id, OrderSide(o.side), o.price, o.qty))

        pos_qty = self._get_signed_position_qty(self._grid_v2_symbol)

        if g_orders:
            # Reconstruct from exchange state
            ok = bridge.startup(g_orders, pos_qty, snapshot.mid_price, snapshot.ts)
            if ok:
                self._grid_v2_seed_fill_eligible_from_registry()
            if not ok:
                logger.error(
                    "GRID_V2_STARTUP_FAILED symbol=%s reason=%s mode=%s",
                    self._grid_v2_symbol,
                    bridge.failed_reason,
                    self._grid_v2_recovery_mode.value,
                )
                if (
                    self._grid_v2_recovery_mode == GridV2RecoveryMode.RESTORE_THEN_CLEANUP_RESEED
                    and self._grid_v2_cleanup_and_reseed(snapshot)
                ):
                    self._grid_v2_started = True
                    return
            elif bridge.f2_protective_recovery:
                # F2 recovery: non-flat position with no exit orders on exchange.
                # Emit protective reduce-only exits. Block entries until exits confirmed.
                f2_exits = bridge.get_f2_protective_exit_actions(snapshot.ts)
                if f2_exits:
                    self._grid_v2_seed_actions = list(f2_exits)
                    self._grid_v2_awaiting_sync = True
                    self._grid_v2_pending_seed_cids = frozenset(
                        ea.client_order_id
                        for ea in f2_exits
                        if ea.action_type == ActionType.PLACE and ea.client_order_id is not None
                    )
                logger.warning(
                    "GRID_V2_F2_PROTECTIVE_RECOVERY symbol=%s protective_exits=%d pos_qty=%s",
                    self._grid_v2_symbol,
                    len(f2_exits),
                    pos_qty,
                )
            elif (
                pos_qty == 0
                and bridge.state_machine is not None
                and (
                    bridge._config.reseed_on_flat
                    or (
                        bridge._config.reseed_on_flat_only_on_skew
                        and self._grid_v2_flat_registry_is_skewed(bridge)
                    )
                )
            ):
                # If we're flat and restarting on existing grid orders, recenter once
                # so entry distance from mid reflects current step/levels config.
                reseed = bridge.recenter_flat(snapshot.mid_price, snapshot.ts)
                if reseed:
                    self._grid_v2_seed_actions = list(reseed)
                    self._grid_v2_awaiting_sync = True
                    self._grid_v2_pending_seed_cids = frozenset(
                        ea.client_order_id
                        for ea in reseed
                        if ea.action_type == ActionType.PLACE and ea.client_order_id is not None
                    )
            self._grid_v2_started = True
        elif pos_qty != 0:
            # Non-flat position with no grid_v2 orders: F2 recovery path.
            # Pass empty orders list to reconstruct_snapshot which will synthesize
            # a protective lot + exit from position_qty.
            ok = bridge.startup([], pos_qty, snapshot.mid_price, snapshot.ts)
            if ok:
                self._grid_v2_seed_fill_eligible_from_registry()
            if not ok:
                logger.error(
                    "GRID_V2_STARTUP_FAILED symbol=%s reason=%s mode=%s",
                    self._grid_v2_symbol,
                    bridge.failed_reason,
                    self._grid_v2_recovery_mode.value,
                )
                if (
                    self._grid_v2_recovery_mode == GridV2RecoveryMode.RESTORE_THEN_CLEANUP_RESEED
                    and self._grid_v2_cleanup_and_reseed(snapshot)
                ):
                    self._grid_v2_started = True
                    return
            elif bridge.f2_protective_recovery:
                f2_exits = bridge.get_f2_protective_exit_actions(snapshot.ts)
                if f2_exits:
                    self._grid_v2_seed_actions = list(f2_exits)
                    self._grid_v2_awaiting_sync = True
                    self._grid_v2_pending_seed_cids = frozenset(
                        ea.client_order_id
                        for ea in f2_exits
                        if ea.action_type == ActionType.PLACE and ea.client_order_id is not None
                    )
                logger.warning(
                    "GRID_V2_F2_PROTECTIVE_RECOVERY symbol=%s protective_exits=%d pos_qty=%s "
                    "reason=no_grid_v2_orders",
                    self._grid_v2_symbol,
                    len(f2_exits),
                    pos_qty,
                )
            self._grid_v2_started = True
        else:
            # Flat position, no orders: fresh start
            seed = bridge.startup_fresh(snapshot.mid_price, snapshot.ts)
            self._grid_v2_seed_actions = list(seed)
            self._grid_v2_started = True
            # PR6: skip fill detection until seed CIDs appear in account snapshot.
            # Seeds are not on exchange yet; registry-vs-exchange diff would be all
            # false positives until account sync confirms orders are visible.
            self._grid_v2_awaiting_sync = True
            self._grid_v2_definitively_rejected_cids.clear()
            self._grid_v2_fill_eligible_cids.clear()
            self._grid_v2_pending_seed_cids = frozenset(
                ea.client_order_id for ea in seed if ea.client_order_id is not None
            )

    def _grid_v2_cleanup_and_reseed(self, snapshot: Snapshot) -> bool:
        """Fallback: cleanup exchange state and startup fresh.

        Only runs in restore_then_cleanup_reseed mode after reconstruction failure.
        Returns True on success, False on failure (fail-closed).
        """
        from scripts.exchange_state import cmd_cleanup, cmd_verify_programmatic  # noqa: PLC0415

        symbol = self._grid_v2_symbol

        # Pre-check write gate: cmd_cleanup calls sys.exit(1) without it.
        allow_write = os.environ.get("ALLOW_MAINNET_TRADE", "").lower() in ("1", "true", "yes")
        if not allow_write:
            logger.error(
                "GRID_V2_RECOVERY_CLEANUP_BLOCKED symbol=%s reason=ALLOW_MAINNET_TRADE_not_set",
                symbol,
            )
            return False

        logger.warning(
            "GRID_V2_RECOVERY_CLEANUP_RESEED symbol=%s — attempting cleanup fallback",
            symbol,
        )

        try:
            cmd_cleanup(symbol)
        except BaseException as exc:  # catch SystemExit from cmd_cleanup
            logger.error(
                "GRID_V2_RECOVERY_CLEANUP_FAILED symbol=%s reason=%s type=%s",
                symbol,
                exc,
                type(exc).__name__,
            )
            return False

        try:
            ok, orders, position = cmd_verify_programmatic(symbol)
        except Exception as exc:
            logger.error(
                "GRID_V2_RECOVERY_VERIFY_FAILED symbol=%s reason=%s",
                symbol,
                exc,
            )
            return False

        if not ok:
            logger.error(
                "GRID_V2_RECOVERY_VERIFY_DIRTY symbol=%s orders=%d position=%s",
                symbol,
                orders,
                position,
            )
            return False

        # Clean state confirmed — fresh start
        bridge_fresh = self._create_grid_v2_bridge()
        seed = bridge_fresh.startup_fresh(snapshot.mid_price, snapshot.ts)
        self._grid_v2_bridge = bridge_fresh
        self._grid_v2_seed_actions = list(seed)
        self._grid_v2_fill_eligible_cids.clear()
        self._grid_v2_awaiting_sync = True
        self._grid_v2_definitively_rejected_cids.clear()
        self._grid_v2_pending_seed_cids = frozenset(
            ea.client_order_id for ea in seed if ea.client_order_id is not None
        )
        # Reset risk tracking counters for clean baseline
        self._symbol_consecutive_losses.pop(symbol, None)
        self._symbol_closed_lots_seen.pop(symbol, None)
        logger.warning(
            "GRID_V2_RECOVERY_CLEANUP_RESEED_OK symbol=%s seeds=%d",
            symbol,
            len(seed),
        )
        return True

    def _is_event_ledger_fresh_for_visibility(self) -> bool:
        """Check if EventLedger is safe to use as current open-order visibility source.

        Trusted is necessary but not sufficient: user-data order events must
        also be flowing. A trusted-but-stale ledger can blind disappearance-based
        fill detection when user-data WS is silent.
        """
        if not self._event_ledger.is_trusted:
            return False
        if self._last_user_data_event_mono <= 0:
            return False  # no order event ever received
        import time  # noqa: PLC0415

        age = time.monotonic() - self._last_user_data_event_mono
        return age <= 5.0

    def _is_position_ledger_fresh_for_reads(self) -> bool:
        """Check if PositionLedger is safe to use for position reads.

        Trusted + fresh position events must both hold. Without fresh events,
        the ledger may be stale and should not override snapshot positions.
        """
        if not self._position_ledger.is_trusted:
            return False
        if self._last_position_event_mono <= 0:
            return False
        import time  # noqa: PLC0415

        age = time.monotonic() - self._last_position_event_mono
        return age <= 5.0

    def get_effective_signed_position_qty(self, symbol: str) -> Decimal:
        """Get position qty from trusted PositionLedger or snapshot fallback.

        Phase 3: switched boundary. Uses PositionLedger when trusted+fresh,
        falls back to REST snapshot otherwise.
        """
        if self._is_position_ledger_fresh_for_reads():
            return self._position_ledger.get_signed_qty(symbol)
        return self._get_signed_position_qty_from_snapshot(symbol)

    def _grid_v2_exchange_cids(self, symbol: str) -> set[str]:
        """Get current grid_v2 CIDs on exchange.

        ADR-109 Phase 2 PR-2: prefer EventLedger when trusted AND fresh for
        faster fill/cancel detection. Falls back to last account snapshot when
        ledger is stale (no recent user-data order events).
        """
        if self._is_event_ledger_fresh_for_visibility():
            cids: set[str] = set()
            for cid in self._event_ledger.open_orders_for_symbol(symbol):
                parsed = parse_client_order_id(cid)
                if parsed is not None and parsed.strategy_id == GRID_V2_STRATEGY_ID:
                    cids.add(cid)
            return cids

        acct = self._last_account_snapshot
        if acct is None:
            return set()
        cids = set()
        for o in acct.open_orders:
            if o.symbol != symbol:
                continue
            parsed = parse_client_order_id(o.order_id)
            if parsed is not None and parsed.strategy_id == GRID_V2_STRATEGY_ID:
                cids.add(o.order_id)
        return cids

    def _grid_v2_sync_reconstruct_on_position_drift(self, snapshot: AccountSnapshot) -> None:  # noqa: PLR0911, PLR0912
        """Fast-path recovery for sync drift: position is non-flat while SM is FLAT.

        This addresses rapid-fill races where exchange position updates first, but
        fill-diff detection lags and the bridge can remain in FLAT temporarily.
        Reconstruct from fresh account snapshot and swap bridge only on success.
        """
        bridge = self._grid_v2_bridge
        if (
            bridge is None
            or not self._grid_v2_enabled
            or not self._grid_v2_started
            or not bridge.reconstruction_ok
        ):
            return
        sm = bridge.state_machine
        if sm is None or sm.mode != BranchMode.FLAT:
            return

        # Don't reconstruct while awaiting seed visibility — seeds are still being
        # placed on exchange. Early fills during seed placement are expected; fill
        # detection will handle them once awaiting_sync clears naturally.
        if self._grid_v2_awaiting_sync:
            return

        pos_qty = self._get_signed_position_qty(self._grid_v2_symbol)
        if pos_qty == 0:
            return

        # Don't reconstruct if grid_v2 orders are present on exchange.
        # Grid is alive — fill path will handle state transitions naturally
        # without destroying rolling history.
        grid_v2_cids = self._grid_v2_exchange_cids(self._grid_v2_symbol)
        if grid_v2_cids:
            return

        # Cooldown: if SM just became FLAT from fill processing, wait N sync cycles
        # before reconstructing. Fills may still be in-flight; natural SM transition
        # to branch will happen via fill-diff detection without reconstruction.
        if self._grid_v2_flat_since_gen >= 0:
            gens_since_flat = self._account_sync_generation - self._grid_v2_flat_since_gen
            if gens_since_flat < _GRID_V2_DRIFT_RECONSTRUCT_COOLDOWN_GENS:
                logger.info(
                    "GRID_V2_DRIFT_RECONSTRUCT_DEFERRED symbol=%s pos_qty=%s "
                    "flat_since_gen=%d current_gen=%d cooldown_remaining=%d",
                    self._grid_v2_symbol,
                    pos_qty,
                    self._grid_v2_flat_since_gen,
                    self._account_sync_generation,
                    _GRID_V2_DRIFT_RECONSTRUCT_COOLDOWN_GENS - gens_since_flat,
                )
                return

        g_orders: list[tuple[str, OrderSide, Decimal, Decimal]] = []
        for o in snapshot.open_orders:
            if o.symbol != self._grid_v2_symbol:
                continue
            parsed = parse_client_order_id(o.order_id)
            if parsed is None or parsed.strategy_id != GRID_V2_STRATEGY_ID:
                continue
            g_orders.append((o.order_id, OrderSide(o.side), o.price, o.qty))

        ref_price: Decimal | None = None
        if self._last_snapshot is not None and self._last_snapshot.symbol == self._grid_v2_symbol:
            ref_price = self._last_snapshot.mid_price
        elif g_orders:
            prices = [price for _cid, _side, price, _qty in g_orders]
            ref_price = (min(prices) + max(prices)) / Decimal(2)
        if ref_price is None:
            logger.warning(
                "GRID_V2_SYNC_POSITION_DRIFT_RECONSTRUCT_SKIPPED "
                "symbol=%s reason=no_reference_price pos_qty=%s",
                self._grid_v2_symbol,
                pos_qty,
            )
            return

        rebuilt = GridV2Bridge(bridge._config, self._grid_v2_symbol)
        ok = rebuilt.startup(g_orders, pos_qty, ref_price, snapshot.ts)
        if not ok:
            logger.error(
                "GRID_V2_SYNC_POSITION_DRIFT_RECONSTRUCT_FAILED symbol=%s reason=%s "
                "pos_qty=%s open_orders=%d",
                self._grid_v2_symbol,
                rebuilt.failed_reason,
                pos_qty,
                len(g_orders),
            )
            return

        self._grid_v2_bridge = rebuilt
        self._grid_v2_fill_eligible_cids.clear()
        self._grid_v2_pending_cancels.clear()
        self._grid_v2_pending_place_cids.clear()
        self._grid_v2_definitively_rejected_cids.clear()
        self._sync_reconciler_pending_actions = []
        self._grid_v2_awaiting_sync = False
        self._grid_v2_pending_seed_cids = frozenset()
        # Reconstructed orders from exchange are provably live
        self._grid_v2_seed_fill_eligible_from_registry()

        if rebuilt.f2_protective_recovery:
            protective = rebuilt.get_f2_protective_exit_actions(snapshot.ts)
            if protective:
                self._grid_v2_seed_actions = list(protective)
                self._grid_v2_awaiting_sync = True
                self._grid_v2_pending_seed_cids = frozenset(
                    ea.client_order_id
                    for ea in protective
                    if ea.action_type == ActionType.PLACE and ea.client_order_id is not None
                )

        logger.warning(
            "GRID_V2_SYNC_POSITION_DRIFT_RECONSTRUCTED symbol=%s old_mode=%s new_mode=%s "
            "pos_qty=%s open_orders=%d",
            self._grid_v2_symbol,
            sm.mode.value,
            rebuilt.state_machine.mode.value if rebuilt.state_machine is not None else "?",
            pos_qty,
            len(g_orders),
        )

    def _grid_v2_register_pending_cancels(
        self,
        actions: list[ExecutionAction],
        ts: int,
    ) -> None:
        """Track CANCEL actions dispatched for grid_v2 orders."""
        for a in actions:
            if a.action_type == ActionType.CANCEL and a.order_id:
                bridge = self._grid_v2_bridge
                if bridge is not None and bridge.adapter.is_ours(a.order_id):
                    self._grid_v2_pending_cancels[a.order_id] = (
                        ts,
                        self._account_sync_generation,
                    )

    def _grid_v2_materialize_reconciler_actions(
        self, actions: tuple[ExecutionAction, ...], ts: int
    ) -> list[ExecutionAction]:
        """Materialize sync-reconciler actions for primary dispatch.

        Reconciler emits pure intents. For PLACE we must attach grid_v2 CID
        and register in adapter registry before dispatch; otherwise PLACE falls
        back to legacy CID scheme (`grinder_d_*`) and breaks grid_v2 ownership.
        Handles both entry and exit reprice placements.
        """
        bridge = self._grid_v2_bridge
        if bridge is None:
            return list(actions)

        materialized: list[ExecutionAction] = []
        for a in actions:
            # Entry PLACE materialization
            if (
                a.action_type == ActionType.PLACE
                and a.reason
                in {
                    "grid_v2_RECONCILE_PLACE_ENTRY",
                    "grid_v2_RECONCILE_REPRICE_ENTRY_PLACE",
                }
                and a.side is not None
                and a.price is not None
                and a.client_order_id is None
            ):
                cid = bridge.adapter.generate_entry_cid(ts)
                bridge.adapter.registry.register_entry(cid, a.side, a.price)
                materialized.append(
                    replace(
                        a,
                        client_order_id=cid,
                        reason=a.reason,
                    )
                )
                continue
            # Exit PLACE materialization (reprice correction)
            if (
                a.action_type == ActionType.PLACE
                and a.reason == "grid_v2_RECONCILE_REPRICE_EXIT_PLACE"
                and a.side is not None
                and a.price is not None
                and a.client_order_id is None
            ):
                cid = bridge.adapter.generate_exit_cid(ts)
                # Exit registry needs exit_order_id and lot_id — for reprice
                # corrections these are not available from the reconciler action.
                # Register with synthetic IDs so the exit is tracked in the
                # grid_v2 CID namespace and visible to future reconcile cycles.
                bridge.adapter.registry.register_exit(
                    cid,
                    exit_order_id=f"reprice-{cid}",
                    lot_id=f"reprice-lot-{cid}",
                )
                materialized.append(
                    replace(
                        a,
                        client_order_id=cid,
                        reason=a.reason,
                    )
                )
                continue
            materialized.append(a)
        return materialized

    def _grid_v2_clean_failed_place(self, cid: str) -> None:
        """Remove a FAILED/BLOCKED/SKIPPED PLACE CID from registry and pending.

        Also adds the CID to the definitive-reject blocklist so it can never
        be mistaken for a fill when it appears absent from exchange snapshots.
        """
        self._grid_v2_pending_place_cids.pop(cid, None)
        self._grid_v2_definitively_rejected_cids.add(cid)
        self._grid_v2_fill_eligible_cids.discard(cid)
        bridge = self._grid_v2_bridge
        if bridge is None:
            return
        parsed_cid = bridge.adapter.parse_cid(cid)
        if parsed_cid is None:
            return
        from grinder.grid_v2.adapter import GridV2OrderKind  # noqa: PLC0415

        if parsed_cid.kind == GridV2OrderKind.ENTRY:
            bridge.adapter.confirm_cancel_entry(cid)
        else:
            bridge.adapter.confirm_cancel_exit(cid)
        logger.info(
            "GRID_V2_FAILED_PLACE_CLEANED cid=%s kind=%s",
            cid,
            parsed_cid.kind.value,
        )

    def _grid_v2_handle_failed_cancel(self, cid: str, exchange_code: int | None) -> bool:
        """Handle definitive CANCEL failures for grid_v2 and return True if handled.

        For exchange code -2011 ("Unknown order"), treat as confirmed cancel-ack:
        the order is already gone on exchange, so registry/pending state should
        be converged immediately.
        """
        if exchange_code != -2011:
            return False

        bridge = self._grid_v2_bridge
        if bridge is None or not bridge.reconstruction_ok:
            return False
        if not bridge.adapter.is_ours(cid):
            return False

        result = bridge.on_cancel_ack(cid)
        self._grid_v2_pending_cancels.pop(cid, None)
        self._cancel_failed_ids.discard(cid)
        logger.warning(
            "GRID_V2_CANCEL_UNKNOWN_TREATED_AS_ACK cid=%s removed=%s kind=%s",
            cid,
            result.removed,
            result.kind.value,
        )
        return True

    def _grid_v2_register_pending_place(self, cid: str) -> None:
        """Track an EXECUTED or ambiguous PLACE CID until visible on exchange.

        Stores current account_sync_generation so the CID can be released
        after a bounded grace period (2 sync cycles) even if never visible
        (e.g. immediate fill before first snapshot).

        Also marks the CID as fill-eligible: only CIDs registered here may
        later be treated as fill candidates by _grid_v2_process_fills.
        """
        self._grid_v2_pending_place_cids[cid] = self._account_sync_generation
        self._grid_v2_fill_eligible_cids.add(cid)

    def _grid_v2_seed_fill_eligible_from_registry(self) -> None:
        """Seed fill-eligible set from current bridge registry.

        Called after startup/reconstruct paths that import already-live
        exchange orders into the registry. These orders are provably live
        on exchange and must be fill-eligible.
        """
        bridge = self._grid_v2_bridge
        if bridge is None:
            return
        for cid in bridge.adapter.registry.all_entry_cids:
            self._grid_v2_fill_eligible_cids.add(cid)
        for cid in bridge.adapter.registry.all_exit_cids:
            self._grid_v2_fill_eligible_cids.add(cid)

    def _grid_v2_process_cancel_acks(self, symbol: str, _ts: int) -> None:
        """Detect confirmed cancels and route through bridge.on_cancel_ack().

        CIDs in pending-cancel set that are now absent from exchange =
        cancel confirmed. Routes through bridge.on_cancel_ack() which
        removes CID from registry. Cleans up pending-cancel set.
        """
        bridge = self._grid_v2_bridge
        if bridge is None or not bridge.reconstruction_ok:
            return
        if symbol != self._grid_v2_symbol:
            return
        if not self._grid_v2_pending_cancels:
            return

        current_cids = self._grid_v2_exchange_cids(symbol)
        confirmed: list[str] = []
        for cid in list(self._grid_v2_pending_cancels):
            if cid not in current_cids:
                bridge.on_cancel_ack(cid)
                confirmed.append(cid)
            # If still on exchange: keep in pending-cancels regardless of age.
            # Dropping on TTL would let a late disappearance route as fill.
        for cid in confirmed:
            del self._grid_v2_pending_cancels[cid]

    def _grid_v2_resolve_stale_pending_cancels(self, symbol: str, now_ts: int) -> int:
        """Force-resolve pending cancels that exceed stale threshold.

        Called during convergence escalation to unblock repair.
        Uses dual bound: time-based OR sync-generation-based staleness.
        Re-checks exchange visibility: if CID is gone, confirm cancel ack.
        If CID is still present but stale, remove from pending blocker set
        (it will be re-detected on next cancel dispatch if still needed).
        """
        bridge = self._grid_v2_bridge
        if bridge is None or not bridge.reconstruction_ok:
            return 0
        current_cids = self._grid_v2_exchange_cids(symbol)
        current_gen = self._account_sync_generation
        resolved = 0
        for cid, (dispatch_ts, dispatch_gen) in list(self._grid_v2_pending_cancels.items()):
            age_ms = now_ts - dispatch_ts
            gen_delta = current_gen - dispatch_gen
            is_stale = (
                age_ms >= _GRID_V2_PENDING_CANCEL_STALE_MS
                or gen_delta >= _GRID_V2_PENDING_CANCEL_STALE_GENS
            )
            if not is_stale:
                continue
            if cid not in current_cids:
                # Gone from exchange — confirm cancel ack
                bridge.on_cancel_ack(cid)
                del self._grid_v2_pending_cancels[cid]
                logger.info(
                    "GRID_V2_STALE_PENDING_CANCEL_RESOLVED cid=%s age_ms=%d "
                    "gen_delta=%d action=cancel_ack",
                    cid,
                    age_ms,
                    gen_delta,
                )
            else:
                # Still on exchange but stale — remove from convergence blocker.
                # Order is live; repair can proceed around it.
                # It stays in exchange state and will be handled by normal cancel path.
                del self._grid_v2_pending_cancels[cid]
                logger.warning(
                    "GRID_V2_STALE_PENDING_CANCEL_DROPPED cid=%s age_ms=%d "
                    "gen_delta=%d action=drop_from_pending "
                    "reason=still_on_exchange_but_stale",
                    cid,
                    age_ms,
                    gen_delta,
                )
            resolved += 1
        return resolved

    def _grid_v2_resolve_stale_pending_places(self) -> int:
        """Force-resolve pending places that exceed generation threshold.

        Called during convergence escalation to unblock repair.
        Uses sync generation bound: if dispatch_gen is too far behind current gen,
        the CID is removed from pending (either it appeared and was already processed,
        or it was a failed/dropped place).
        """
        gen = self._account_sync_generation
        resolved = 0
        for cid, dispatch_gen in list(self._grid_v2_pending_place_cids.items()):
            if (gen - dispatch_gen) >= _GRID_V2_PENDING_PLACE_STALE_GENS:
                del self._grid_v2_pending_place_cids[cid]
                logger.info(
                    "GRID_V2_STALE_PENDING_PLACE_RESOLVED cid=%s dispatch_gen=%d "
                    "current_gen=%d action=drop_from_pending",
                    cid,
                    dispatch_gen,
                    gen,
                )
                resolved += 1
        return resolved

    def _record_grid_v2_rejected_fill_cleaned(self, symbol: str, source: str, reason: str) -> None:
        """Emit metric for rejected fill cleanup (observability only)."""
        get_live_engine_metrics().record_grid_v2_rejected_fill_cleaned(symbol, source, reason)

    def _grid_v2_process_fills(
        self,
        symbol: str,
        ts: int,
    ) -> list[ExecutionAction]:
        """Detect fills for grid_v2 orders and route through bridge.

        Uses registry vs exchange diff: CIDs in registry but absent from
        exchange AND not in pending-cancel set = filled.
        Iterates in sorted order for deterministic action sequence.
        Returns ExecutionActions from bridge.on_fill() for dispatch.
        """
        from grinder.observability.latency_telemetry import PhaseTimer  # noqa: PLC0415

        fill_timer = PhaseTimer()
        bridge = self._grid_v2_bridge
        if bridge is None or not bridge.reconstruction_ok:
            return []
        if symbol != self._grid_v2_symbol:
            return []

        current_cids = self._grid_v2_exchange_cids(symbol)
        if not current_cids and self._last_account_snapshot is None:
            return []

        # Compare with registry: CIDs in registry but not on exchange = disappeared
        registry_cids = set(bridge.adapter.registry.all_entry_cids) | set(
            bridge.adapter.registry.all_exit_cids
        )
        disappeared = registry_cids - current_cids

        # Fill-eligible gate: only CIDs with credible live-on-exchange evidence
        # (EXECUTED or ambiguous quarantine) may be fill candidates. Mere registry
        # presence from response-action pre-registration is not sufficient.
        # Also exclude pending cancels, pending places, and definitively rejected.
        filled_cids = (
            (disappeared & self._grid_v2_fill_eligible_cids)
            - set(self._grid_v2_pending_cancels)
            - set(self._grid_v2_pending_place_cids)
            - self._grid_v2_definitively_rejected_cids
        )
        if not filled_cids:
            return []

        actions: list[ExecutionAction] = []

        # Deterministic order with EXIT before ENTRY.
        # This lets same-tick "exit + opposite entry" sequences flatten first,
        # reducing branch-incompatible rejects in one-sided inventory mode.
        def _fill_priority(fill_cid: str) -> tuple[int, str]:
            parsed = bridge.adapter.parse_cid(fill_cid)
            if parsed is not None and parsed.kind.value == "EXIT":
                return (0, fill_cid)
            if parsed is not None and parsed.kind.value == "ENTRY":
                return (1, fill_cid)
            return (2, fill_cid)

        for cid in sorted(filled_cids, key=_fill_priority):
            # Re-read registry per CID: prior fills in this batch may have
            # changed registration state via resolve_actions/confirm_*.
            entry_reg = bridge.adapter.registry.lookup_entry(cid)
            if entry_reg is not None:
                result = bridge.on_fill(
                    cid,
                    entry_reg.side,
                    entry_reg.price,
                    bridge._config.order_size,
                    ts,
                    allow_stale=True,
                )
                if result.rejected:
                    reason = result.reject_reason or "?"
                    bridge.adapter.confirm_cancel_entry(cid)
                    self._record_grid_v2_rejected_fill_cleaned(symbol, "snapshot_diff", reason)
                    logger.warning(
                        "GRID_V2_REJECTED_FILL_CLEANED cid=%s kind=entry reason=%s",
                        cid,
                        reason,
                    )
                    continue
                self._maybe_track_lot_closure(symbol, result)
                # Provable lot addition: entry fill created a new lot.
                fill_qty = (
                    result.transition.snapshot.open_lots[-1].qty
                    if result.transition
                    and result.transition.snapshot
                    and result.transition.snapshot.open_lots
                    else bridge._config.order_size
                )
                self._reduce_only_batch_new_lots_qty[symbol] = (
                    self._reduce_only_batch_new_lots_qty.get(symbol, Decimal(0)) + fill_qty
                )
                actions.extend(result.execution_actions)
                continue

            exit_reg = bridge.adapter.registry.lookup_exit(cid)
            if exit_reg is None:
                continue

            # Infer exit side from lot ledger
            side = self._grid_v2_infer_exit_side(bridge, exit_reg.exit_order_id)
            result = bridge.on_fill(
                cid,
                side,
                Decimal("0"),
                bridge._config.order_size,
                ts,
                allow_stale=True,
            )
            if result.rejected:
                reason = result.reject_reason or "?"
                bridge.adapter.confirm_cancel_exit(cid)
                self._record_grid_v2_rejected_fill_cleaned(symbol, "snapshot_diff", reason)
                logger.warning(
                    "GRID_V2_REJECTED_FILL_CLEANED cid=%s kind=exit reason=%s",
                    cid,
                    reason,
                )
                continue
            self._maybe_track_lot_closure(symbol, result)
            actions.extend(result.execution_actions)

        if actions:
            from grinder.observability.latency_telemetry import log_fill_reaction  # noqa: PLC0415

            log_fill_reaction(symbol, fill_timer.elapsed_ms(), len(actions))
        return actions

    def _grid_v2_track_flat_transition(self) -> None:
        """Track SM FLAT transition for drift-reconstruct cooldown.

        If SM just became FLAT (all lots closed), record current sync gen
        so drift detection waits before reconstructing.
        """
        bridge = self._grid_v2_bridge
        if bridge is None or bridge.state_machine is None:
            return
        if bridge.state_machine.mode == BranchMode.FLAT:
            if self._grid_v2_flat_since_gen < 0:
                self._grid_v2_flat_since_gen = self._account_sync_generation
        else:
            self._grid_v2_flat_since_gen = -1

    def _grid_v2_integrity_repair(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        snapshot: Snapshot,
        planned_slots_this_tick: set[tuple[OrderSide, Decimal]] | None = None,
    ) -> list[ExecutionAction]:
        """Continuously validate grid integrity and auto-repair drift."""
        if not self._is_grid_v2_active(snapshot.symbol):
            self._grid_v2_integrity_mismatch_streak = 0
            return []

        bridge = self._grid_v2_bridge
        if bridge is None or bridge.state_machine is None:
            self._grid_v2_integrity_mismatch_streak = 0
            return []

        # Don't attempt repair while orders are still converging —
        # UNLESS convergence has been pending for too many consecutive ticks.
        convergence_pending = (
            self._grid_v2_awaiting_sync
            or self._grid_v2_pending_cancels
            or self._grid_v2_pending_place_cids
        )
        if convergence_pending:
            self._grid_v2_integrity_convergence_defer_count += 1
            if (
                self._grid_v2_integrity_convergence_defer_count
                < _GRID_V2_INTEGRITY_CONVERGENCE_MAX_DEFERS
            ):
                # Preserve streak during convergence — do NOT reset on window expiry
                # while still pending, to avoid stuck-at-1 scenario.
                return []
            # Bounded escalation: force-resolve stale pending state, then proceed.
            stale_cancels = self._grid_v2_resolve_stale_pending_cancels(
                snapshot.symbol, snapshot.ts
            )
            stale_places = self._grid_v2_resolve_stale_pending_places()
            # Log on first escalation, then every Nth defer (anti-spam)
            defers = self._grid_v2_integrity_convergence_defer_count
            if defers == _GRID_V2_INTEGRITY_CONVERGENCE_MAX_DEFERS or (
                defers % _GRID_V2_ESCALATION_LOG_INTERVAL == 0
            ):
                logger.warning(
                    "GRID_V2_INTEGRITY_CONVERGENCE_ESCALATION symbol=%s defers=%d "
                    "awaiting_sync=%s pending_cancels=%d pending_places=%d "
                    "stale_cancels_resolved=%d stale_places_resolved=%d",
                    snapshot.symbol,
                    defers,
                    self._grid_v2_awaiting_sync,
                    len(self._grid_v2_pending_cancels),
                    len(self._grid_v2_pending_place_cids),
                    stale_cancels,
                    stale_places,
                )
            # After resolution, re-check if convergence is now clear
            convergence_pending = (
                self._grid_v2_awaiting_sync
                or self._grid_v2_pending_cancels
                or self._grid_v2_pending_place_cids
            )
            if not convergence_pending:
                self._grid_v2_integrity_convergence_defer_count = 0
        else:
            self._grid_v2_integrity_convergence_defer_count = 0

        sm = bridge.state_machine

        if snapshot.ts < self._grid_v2_integrity_repair_cooldown_until_ts:
            return []

        current_cids = self._grid_v2_exchange_cids(snapshot.symbol)
        acct = self._last_account_snapshot
        current_entries = 0
        current_exits = 0
        current_entry_by_key: dict[tuple[OrderSide, Decimal], str] = {}
        current_exit_cids: set[str] = set()
        if acct is not None:
            for o in acct.open_orders:
                if o.symbol != snapshot.symbol:
                    continue
                parsed = bridge.adapter.parse_cid(o.order_id)
                if parsed is None:
                    continue
                if parsed.kind.value == "ENTRY":
                    try:
                        side = OrderSide(o.side)
                    except ValueError:
                        continue
                    current_entry_by_key[(side, o.price)] = o.order_id
                elif parsed.kind.value == "EXIT":
                    current_exit_cids.add(o.order_id)
        for cid in current_cids:
            parsed = bridge.adapter.parse_cid(cid)
            if parsed is None:
                continue
            if parsed.kind.value == "ENTRY":
                current_entries += 1
            else:
                current_exits += 1

        expected_entry_keys: set[tuple[OrderSide, Decimal]] = set()
        for p in sm.snapshot.entry_window.buy_entry_prices:
            expected_entry_keys.add((OrderSide.BUY, bridge._quantize_price(p, OrderSide.BUY)))
        for p in sm.snapshot.entry_window.sell_entry_prices:
            expected_entry_keys.add((OrderSide.SELL, bridge._quantize_price(p, OrderSide.SELL)))
        # Headroom-aware entry suppression: reduce same-side entries as
        # inventory approaches cap. Mirrors reconciler headroom logic.
        target_entry_keys = set(expected_entry_keys)
        if sm.mode != BranchMode.FLAT:
            lots_open = len(sm.snapshot.open_lots)
            max_inv = bridge._config.max_inventory_levels
            _lps = getattr(bridge._config, "entry_levels_per_side", 5)
            headroom = max(0, max_inv - lots_open)
            if headroom == 0:
                target_entry_keys = set()
            elif isinstance(_lps, int) and headroom < _lps:
                branch_side = OrderSide.BUY if sm.mode == BranchMode.LONG_BRANCH else OrderSide.SELL
                ref = sm.snapshot.entry_window.reference_price
                same_side = sorted(
                    [(s, p) for s, p in target_entry_keys if s == branch_side],
                    key=lambda k: abs(k[1] - ref),
                )
                if len(same_side) > headroom:
                    to_remove = set(same_side[headroom:])
                    target_entry_keys -= to_remove
        expected_entries = len(target_entry_keys)

        # EXIT integrity: expected exits from lot ledger
        expected_exit_cids: set[str] = set()
        exit_registry_orphans = 0
        for eo in sm.snapshot.exit_orders:
            # Only OPEN exits are expected to have active registry/exchange presence.
            # Historical FILLED/CANCELED exits are kept in snapshot for ledger history
            # and must not be treated as active integrity orphans.
            if eo.status.value != "OPEN":
                continue
            reg_cid = bridge.adapter.registry.cid_for_exit(eo.exit_order_id)
            if reg_cid is not None:
                expected_exit_cids.add(reg_cid)
            else:
                # Registry lost mapping for a currently OPEN exit — integrity issue
                exit_registry_orphans += 1
        exit_mismatch = bool(
            sm.mode != BranchMode.FLAT
            and (
                (expected_exit_cids and not expected_exit_cids.issubset(current_exit_cids))
                or exit_registry_orphans > 0
            )
        )

        # Geometry-aware matching: detect structural + price-drift mismatches (ENTRY only)
        geometry_mismatches: list[tuple[str, Decimal, Decimal, str]] = []
        if sm.mode == BranchMode.FLAT:
            mismatch = current_entries != expected_entries or current_exits != 0
        elif self._grid_v2_geometry_repair_enabled and bridge._config.price_tick_size > 0:
            # Convert keys to string-side for geometry module
            str_expected = {(s.value, p) for s, p in target_entry_keys}
            str_actual = {(s.value, p): cid for (s, p), cid in current_entry_by_key.items()}
            _matched, truly_missing, truly_extra, geometry_mismatches = (
                match_entries_with_tolerance(
                    str_expected,
                    str_actual,
                    bridge._config.price_tick_size,
                    self._grid_v2_geometry_epsilon_ticks,
                )
            )
            structural_mismatch = bool(truly_missing or truly_extra)
            mismatch = structural_mismatch or bool(geometry_mismatches) or exit_mismatch
        else:
            mismatch = set(current_entry_by_key.keys()) != target_entry_keys or exit_mismatch
        if not mismatch:
            self._grid_v2_integrity_mismatch_streak = 0
            self._grid_v2_integrity_mismatch_last_ts = 0
            return []

        # If convergence just escalated, the time gap is from deferral, not absence
        # of mismatch. Treat as continuation to avoid stuck-at-1 streak.
        escalated = convergence_pending
        if not escalated and (
            self._grid_v2_integrity_mismatch_last_ts == 0
            or snapshot.ts - self._grid_v2_integrity_mismatch_last_ts
            > _GRID_V2_INTEGRITY_MISMATCH_WINDOW_MS
        ):
            self._grid_v2_integrity_mismatch_streak = 1
        else:
            self._grid_v2_integrity_mismatch_streak += 1
        self._grid_v2_integrity_mismatch_last_ts = snapshot.ts
        if self._grid_v2_integrity_mismatch_streak < _GRID_V2_INTEGRITY_MISMATCH_STREAK:
            get_live_engine_metrics().record_grid_v2_integrity_mismatch_pending(snapshot.symbol)
            logger.warning(
                "GRID_V2_INTEGRITY_MISMATCH_PENDING symbol=%s entries=%d expected=%d exits=%d "
                "streak=%d/%d",
                snapshot.symbol,
                current_entries,
                expected_entries,
                current_exits,
                self._grid_v2_integrity_mismatch_streak,
                _GRID_V2_INTEGRITY_MISMATCH_STREAK,
            )
            return []

        self._grid_v2_integrity_mismatch_streak = 0
        self._grid_v2_integrity_mismatch_last_ts = 0
        self._grid_v2_integrity_repair_cooldown_until_ts = (
            snapshot.ts + _GRID_V2_INTEGRITY_REPAIR_COOLDOWN_MS
        )
        repair_actions: list[ExecutionAction] = []
        if sm.mode == BranchMode.FLAT:
            flat_skew = self._grid_v2_flat_entry_is_skewed(
                current_entry_by_key=current_entry_by_key,
                expected_entry_keys=target_entry_keys,
            )
            if bridge._config.reseed_on_flat or (
                bridge._config.reseed_on_flat_only_on_skew and flat_skew
            ):
                logger.warning(
                    "GRID_V2_INTEGRITY_REPAIR_TRIGGER symbol=%s mode=%s entries=%d expected=%d "
                    "exits=%d reseed_mode=%s skew=%s",
                    snapshot.symbol,
                    sm.mode.value,
                    current_entries,
                    expected_entries,
                    current_exits,
                    "always" if bridge._config.reseed_on_flat else "only_on_skew",
                    flat_skew,
                )
                repair_actions = list(bridge.recenter_flat(snapshot.mid_price, snapshot.ts))
            else:
                current_keys = set(current_entry_by_key.keys())
                extra = current_keys - target_entry_keys
                # Preserve mode: never reseed/rebuild in FLAT.
                # Only perform cleanup for exchange-visible drift.
                for side, price in sorted(extra, key=lambda item: (item[0].value, item[1])):
                    cid = current_entry_by_key[(side, price)]
                    repair_actions.append(
                        ExecutionAction(
                            action_type=ActionType.CANCEL,
                            order_id=cid,
                            symbol=snapshot.symbol,
                            reason="grid_v2_INTEGRITY_CANCEL_ENTRY",
                        )
                    )
                for cid in sorted(current_cids):
                    parsed = bridge.adapter.parse_cid(cid)
                    if parsed is None or parsed.kind.value == "ENTRY":
                        continue
                    repair_actions.append(
                        ExecutionAction(
                            action_type=ActionType.CANCEL,
                            order_id=cid,
                            symbol=snapshot.symbol,
                            reason="grid_v2_INTEGRITY_CANCEL_EXIT",
                        )
                    )
                logger.warning(
                    "GRID_V2_INTEGRITY_FLAT_PRESERVE symbol=%s entries=%d expected=%d exits=%d "
                    "extra=%d cleanup_actions=%d reason=reseed_preserve_mode skew=%s",
                    snapshot.symbol,
                    current_entries,
                    expected_entries,
                    current_exits,
                    len(extra),
                    len(repair_actions),
                    flat_skew,
                )
        else:
            current_keys = set(current_entry_by_key.keys())
            missing = target_entry_keys - current_keys
            extra = current_keys - target_entry_keys
            missing_exits = expected_exit_cids - current_exit_cids
            logger.warning(
                "GRID_V2_INTEGRITY_REPAIR_TRIGGER symbol=%s mode=%s entries=%d expected=%d "
                "missing=%d extra=%d missing_exits=%d",
                snapshot.symbol,
                sm.mode.value,
                len(current_keys),
                len(expected_entry_keys),
                len(missing),
                len(extra),
                len(missing_exits),
            )
            if missing_exits or exit_registry_orphans:
                logger.warning(
                    "GRID_V2_EXIT_INTEGRITY_MISMATCH symbol=%s missing_exit_cids=%d "
                    "expected=%d current=%d registry_orphans=%d",
                    snapshot.symbol,
                    len(missing_exits),
                    len(expected_exit_cids),
                    len(current_exit_cids),
                    exit_registry_orphans,
                )

            # Cancel entry orders that exist on exchange but are outside expected window.
            for side, price in sorted(extra, key=lambda item: (item[0].value, item[1])):
                cid = current_entry_by_key[(side, price)]
                repair_actions.append(
                    ExecutionAction(
                        action_type=ActionType.CANCEL,
                        order_id=cid,
                        symbol=snapshot.symbol,
                        reason="grid_v2_INTEGRITY_CANCEL_ENTRY",
                    )
                )

            # Place missing entries with anti-churn guards:
            # 0) Skip if slot already planned by fill path this tick (same-tick dedup)
            # 1) Skip if slot already pending (pending_place_cids dedup)
            # 2) Skip if too far from mid (distance guard) — unless strict geometry
            # 3) Cap total PLACE actions per repair cycle (budget guard)
            pending_prices = self._grid_v2_pending_place_entry_prices(bridge)
            planned_this_tick = planned_slots_this_tick or set()
            mid = snapshot.mid_price
            step = bridge._config.grid_step_pct
            max_dist = self._grid_v2_repair_max_distance_steps
            budget = self._grid_v2_repair_max_actions
            strict_geo = self._grid_v2_repair_strict_geometry

            intents: list[ActionIntent] = []
            skipped_pending = 0
            skipped_distance = 0
            skipped_planned_slot = 0
            for side, price in sorted(missing, key=lambda item: (item[0].value, item[1])):
                # Guard 0: same-tick fill dedup
                if (side, price) in planned_this_tick:
                    skipped_planned_slot += 1
                    continue
                # Guard 1: pending slot dedup
                if (side, price) in pending_prices:
                    skipped_pending += 1
                    continue
                # Guard 2: distance from mid (bypassed in strict geometry mode)
                if mid > 0 and not strict_geo:
                    distance_steps = float(abs(price - mid) / mid / step)
                    if distance_steps > max_dist:
                        skipped_distance += 1
                        continue
                # Guard 3: budget cap
                if len(intents) >= budget:
                    break

                stale_cid = bridge.adapter.registry.cid_for_entry(side, price)
                if stale_cid is not None and stale_cid not in current_cids:
                    bridge.adapter.confirm_cancel_entry(stale_cid)
                    logger.warning(
                        "GRID_V2_INTEGRITY_STALE_ENTRY_DROPPED symbol=%s cid=%s side=%s price=%s",
                        snapshot.symbol,
                        stale_cid,
                        side.value,
                        price,
                    )
                intents.append(
                    ActionIntent(
                        kind=ActionIntentKind.PLACE_ENTRY,
                        side=side,
                        price=price,
                        qty=bridge._config.order_size,
                        reason="INTEGRITY_REPAIR",
                    )
                )
            if skipped_pending or skipped_distance or skipped_planned_slot:
                logger.info(
                    "GRID_V2_REPAIR_GUARDS symbol=%s skipped_planned_slot=%d "
                    "skipped_pending=%d skipped_distance=%d budget_used=%d/%d",
                    snapshot.symbol,
                    skipped_planned_slot,
                    skipped_pending,
                    skipped_distance,
                    len(intents),
                    budget,
                )
            if intents:
                try:
                    resolved = bridge.adapter.resolve_actions(tuple(intents), snapshot.ts)
                except ValueError as exc:
                    logger.warning(
                        "GRID_V2_INTEGRITY_REPAIR_RESOLVE_FAILED symbol=%s reason=%s",
                        snapshot.symbol,
                        exc,
                    )
                else:
                    repair_actions.extend(bridge._to_execution_actions(resolved))

            # Geometry repair: cancel+place for orders at wrong price
            if (
                geometry_mismatches
                and self._grid_v2_geometry_repair_enabled
                and snapshot.ts
                >= self._grid_v2_geometry_last_repair_ts + self._grid_v2_geometry_cooldown_ms
            ):
                geo_budget = self._grid_v2_geometry_max_actions
                geo_count = 0
                for side_str, expected_price, actual_price, cid in geometry_mismatches:
                    if geo_count >= geo_budget:
                        break
                    # Cancel the misaligned order
                    repair_actions.append(
                        ExecutionAction(
                            action_type=ActionType.CANCEL,
                            order_id=cid,
                            symbol=snapshot.symbol,
                            reason="grid_v2_GEOMETRY_CANCEL_ENTRY",
                        )
                    )
                    geo_count += 1
                    logger.warning(
                        "GRID_V2_GEOMETRY_MISMATCH symbol=%s side=%s expected=%s actual=%s cid=%s",
                        snapshot.symbol,
                        side_str,
                        expected_price,
                        actual_price,
                        cid,
                    )
                if geo_count > 0:
                    self._grid_v2_geometry_last_repair_ts = snapshot.ts
                    logger.info(
                        "GRID_V2_GEOMETRY_REPAIR_DISPATCH symbol=%s repairs=%d/%d",
                        snapshot.symbol,
                        geo_count,
                        len(geometry_mismatches),
                    )

        # Final dedup: remove PLACE actions for slots already planned by fill path this tick.
        # This covers both FLAT reseed and branch repair paths.
        planned_this_tick = planned_slots_this_tick or set()
        if planned_this_tick:
            before = len(repair_actions)
            repair_actions = [
                a
                for a in repair_actions
                if not (
                    a.action_type == ActionType.PLACE
                    and a.side is not None
                    and a.price is not None
                    and (a.side, a.price) in planned_this_tick
                )
            ]
            dropped = before - len(repair_actions)
            if dropped:
                logger.info(
                    "GRID_V2_REPAIR_DEDUP_PLANNED_SLOT symbol=%s dropped=%d",
                    snapshot.symbol,
                    dropped,
                )

        if repair_actions:
            logger.info(
                "GRID_V2_INTEGRITY_REPAIR_DISPATCH symbol=%s mode=%s actions=%d",
                snapshot.symbol,
                sm.mode.value,
                len(repair_actions),
            )
        return list(repair_actions)

    def _grid_v2_pending_place_entry_prices(
        self, bridge: GridV2Bridge
    ) -> set[tuple[OrderSide, Decimal]]:
        """Get (side, price) set for entries with pending PLACE CIDs."""
        result: set[tuple[OrderSide, Decimal]] = set()
        for cid in self._grid_v2_pending_place_cids:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg is not None:
                result.add((reg.side, reg.price))
        return result

    @staticmethod
    def _extract_planned_entry_slots(
        actions: list[ExecutionAction],
    ) -> set[tuple[OrderSide, Decimal]]:
        """Extract (side, price) slots from PLACE actions in fill-path output."""
        slots: set[tuple[OrderSide, Decimal]] = set()
        for a in actions:
            if (
                a.action_type == ActionType.PLACE
                and a.side is not None
                and a.price is not None
                and "ENTRY" in a.reason.upper()
            ):
                slots.add((a.side, a.price))
        return slots

    def _grid_v2_dispatch_immediate_actions(
        self,
        actions: list[ExecutionAction],
        ts: int,
    ) -> None:
        """Dispatch grid_v2 actions emitted by immediate user-data fill handling."""
        for action in actions:
            live_action = self._process_action(action, ts)
            if (
                action.action_type == ActionType.PLACE
                and action.client_order_id is not None
                and self._grid_v2_bridge is not None
                and self._grid_v2_bridge.adapter.is_ours(action.client_order_id)
            ):
                if live_action.status == LiveActionStatus.EXECUTED:
                    self._grid_v2_register_pending_place(action.client_order_id)
                elif live_action.status in (
                    LiveActionStatus.BLOCKED,
                    LiveActionStatus.SKIPPED,
                ) or (live_action.status == LiveActionStatus.FAILED and live_action.pre_send):
                    self._grid_v2_clean_failed_place(action.client_order_id)
                elif live_action.status == LiveActionStatus.FAILED:
                    if _grid_v2_is_exchange_code_ambiguous(live_action.exchange_code):
                        self._grid_v2_register_pending_place(action.client_order_id)
                        logger.warning(
                            "GRID_V2_FAILED_PLACE_QUARANTINED cid=%s code=%s reason=%s",
                            action.client_order_id,
                            live_action.exchange_code,
                            live_action.block_reason.value if live_action.block_reason else "?",
                        )
                    else:
                        self._grid_v2_clean_failed_place(action.client_order_id)

            if (
                action.action_type == ActionType.CANCEL
                and action.order_id is not None
                and live_action.status in (LiveActionStatus.EXECUTED, LiveActionStatus.FAILED)
            ):
                self._cancel_dispatched_pending_sync.add(action.order_id)

    def process_user_data_event(self, event: UserDataEvent) -> None:  # noqa: PLR0911, PLR0912
        """Process immediate user-data order events for grid_v2.

        This path is a low-latency supplement to account-sync polling:
        terminal ORDER_TRADE_UPDATE events are applied immediately.
        """
        # ADR-109 Phase 1/2: Feed order events to EventLedger.
        if event.order_event is not None:
            self._event_ledger.apply_order_event(event.order_event)
            self._last_user_data_event_mono = time.monotonic()

        # ADR-109 Phase 3: Feed position events to PositionLedger.
        if event.position_event is not None:
            pe = event.position_event
            self._position_ledger.apply_position_event(pe)
            self._last_position_event_mono = time.monotonic()
            logger.debug(
                "POSITION_LEDGER_EVENT_APPLIED symbol=%s side=%s amt=%s ts=%d",
                pe.symbol,
                pe.position_side,
                pe.position_amt,
                pe.ts,
            )

        if not self._is_grid_v2_active(self._grid_v2_symbol):
            return
        if event.order_event is None:
            return
        oe = event.order_event
        if oe.symbol != self._grid_v2_symbol:
            return

        bridge = self._grid_v2_bridge
        if bridge is None or not bridge.reconstruction_ok:
            return
        if not bridge.adapter.is_ours(oe.client_order_id):
            return

        if oe.status == OrderState.FILLED:
            if oe.client_order_id in self._grid_v2_user_fill_seen:
                return
            qty = oe.executed_qty if oe.executed_qty > 0 else bridge._config.order_size
            # Use order price for grid_v2 state transitions when available.
            # avg_price can carry noisy fractional values and break
            # price-keyed registry/state alignment for LIMIT grid orders.
            price = oe.price if oe.price > 0 else oe.avg_price
            result = bridge.on_fill(
                oe.client_order_id,
                oe.side,
                price,
                qty,
                oe.ts,
                allow_stale=True,
            )
            if result.rejected:
                reason = result.reject_reason or "?"
                parsed = bridge.adapter.parse_cid(oe.client_order_id)
                if parsed is not None:
                    from grinder.grid_v2.adapter import GridV2OrderKind  # noqa: PLC0415

                    if parsed.kind == GridV2OrderKind.ENTRY:
                        bridge.adapter.confirm_cancel_entry(oe.client_order_id)
                    else:
                        bridge.adapter.confirm_cancel_exit(oe.client_order_id)
                self._record_grid_v2_rejected_fill_cleaned(oe.symbol, "user_data", reason)
                logger.warning(
                    "GRID_V2_REJECTED_FILL_CLEANED cid=%s source=user_data reason=%s",
                    oe.client_order_id,
                    reason,
                )
                self._grid_v2_user_fill_seen.add(oe.client_order_id)
                return

            self._grid_v2_user_fill_seen.add(oe.client_order_id)
            self._last_fill_ts = max(self._last_fill_ts, oe.ts)  # ADR-111
            self._burst_suppression_fired = False  # new fill → allow one suppression
            # ADR-109 Phase 2 PR-2: event-first fill observability
            logger.info(
                "EVENT_FIRST_FILL_APPLIED cid=%s symbol=%s source=user_data actions=%d trusted=%s",
                oe.client_order_id,
                oe.symbol,
                len(result.execution_actions),
                self._event_ledger.is_trusted,
            )
            self._maybe_track_lot_closure(oe.symbol, result)
            # Provable lot addition from user-data fill path
            parsed_ud = bridge.adapter.parse_cid(oe.client_order_id)
            if parsed_ud is not None and parsed_ud.kind.value == "ENTRY" and not result.rejected:
                self._reduce_only_batch_new_lots_qty[oe.symbol] = (
                    self._reduce_only_batch_new_lots_qty.get(oe.symbol, Decimal(0)) + qty
                )
            if result.execution_actions:
                self._grid_v2_dispatch_immediate_actions(list(result.execution_actions), oe.ts)
            return

        if oe.status in (OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED):
            bridge.on_cancel_ack(oe.client_order_id)
            self._grid_v2_pending_cancels.pop(oe.client_order_id, None)

    @staticmethod
    def _grid_v2_infer_exit_side(bridge: GridV2Bridge, exit_order_id: str) -> OrderSide:
        """Infer OrderSide for an exit fill from the lot ledger."""
        sm = bridge.state_machine
        if sm is not None:
            for lot in sm.snapshot.open_lots:
                if lot.exit_order_id == exit_order_id:
                    return OrderSide.SELL if lot.side == LotSide.LONG else OrderSide.BUY
        return OrderSide.SELL  # default: LONG lots exit via SELL

    def _is_grid_v2_active(self, symbol: str) -> bool:
        """Check if grid_v2 is active for this symbol."""
        return (
            self._grid_v2_enabled
            and self._grid_v2_bridge is not None
            and self._grid_v2_started
            and self._grid_v2_bridge.reconstruction_ok
            and symbol == self._grid_v2_symbol
        )

    @staticmethod
    def _grid_v2_flat_entry_is_skewed(
        *,
        current_entry_by_key: dict[tuple[OrderSide, Decimal], str],
        expected_entry_keys: set[tuple[OrderSide, Decimal]],
    ) -> bool:
        """Whether FLAT ladder is skewed between BUY/SELL entry sides."""
        current_buy = sum(1 for side, _ in current_entry_by_key if side == OrderSide.BUY)
        current_sell = sum(1 for side, _ in current_entry_by_key if side == OrderSide.SELL)
        expected_buy = sum(1 for side, _ in expected_entry_keys if side == OrderSide.BUY)
        expected_sell = sum(1 for side, _ in expected_entry_keys if side == OrderSide.SELL)
        return (
            current_buy != expected_buy
            or current_sell != expected_sell
            or current_buy != current_sell
        )

    @staticmethod
    def _grid_v2_flat_registry_is_skewed(bridge: GridV2Bridge) -> bool:
        """Whether reconstructed registry is skewed while FLAT."""
        buy_count = 0
        sell_count = 0
        for cid in bridge.adapter.registry.all_entry_cids:
            reg = bridge.adapter.registry.lookup_entry(cid)
            if reg is None:
                continue
            if reg.side == OrderSide.BUY:
                buy_count += 1
            elif reg.side == OrderSide.SELL:
                sell_count += 1
        expected = bridge._config.entry_levels_per_side
        return buy_count != expected or sell_count != expected or buy_count != sell_count

    # --- Grid V2 shadow methods (doc-27 section 24, PR5) ---

    def _grid_v2_shadow_tick(
        self,
        snapshot: Any,
        legacy_actions: list[ExecutionAction] | Any,
    ) -> None:
        """Run shadow grid_v2 tick. Fail-open: errors logged, never propagated."""
        shadow = self._grid_v2_shadow
        if shadow is None:
            return

        acct = self._last_account_snapshot
        if acct is None:
            return

        # Shadow startup (same logic as primary, on isolated bridge)
        if not shadow.started:
            g_orders: list[tuple[str, OrderSide, Decimal, Decimal]] = []
            for o in acct.open_orders:
                if o.symbol != self._grid_v2_symbol:
                    continue
                parsed = parse_client_order_id(o.order_id)
                if parsed is not None and parsed.strategy_id == GRID_V2_STRATEGY_ID:
                    g_orders.append((o.order_id, OrderSide(o.side), o.price, o.qty))

            pos_qty = self._get_signed_position_qty(self._grid_v2_symbol)
            shadow.try_startup(g_orders, pos_qty, snapshot.mid_price, snapshot.ts)

        # Collect exchange CIDs and pending cancels for shadow fill detection
        exchange_cids = frozenset(self._grid_v2_exchange_cids(self._grid_v2_symbol))
        # Shadow doesn't have its own pending cancels (never dispatches).
        # Pass empty set — shadow fill detection is approximate.
        shadow.on_snapshot(
            legacy_actions=legacy_actions if isinstance(legacy_actions, list) else [],
            exchange_cids=exchange_cids,
            pending_cancel_cids=frozenset(),
            ts=snapshot.ts,
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
        # Reset per-tick batch accumulators
        self._reduce_only_batch_qty.clear()
        self._reduce_only_batch_new_lots_qty.clear()

        # Live Health Gate: evaluate truth-source health
        self._evaluate_and_update_health_mode(snapshot)

        # Store snapshot for SOR market data (Launch-14 PR2)
        self._last_snapshot = snapshot

        # PR-L0: Feed FeatureEngine (must run every tick for bar building, even in FSM defer)
        if self._feature_engine is not None:
            self._last_feature_snapshot = self._feature_engine.process_snapshot(snapshot)

        # Adaptive step evaluation (post-feature, pre-dispatch)
        if self._adaptive_step.config.enabled and self._last_feature_snapshot is not None:
            vol_bps = float(self._last_feature_snapshot.natr_bps)
            sym = self._last_feature_snapshot.symbol
            self._adaptive_step.evaluate(sym, vol_bps if vol_bps > 0 else None)

        # Record price for toxicity gate (needs history before check, PR-A1)
        if self._toxicity_gate is not None:
            self._toxicity_gate.record_price(snapshot.ts, snapshot.symbol, snapshot.mid_price)

        # Publish regime to shared registry for portfolio-level aggregation.
        # Requires FeatureEngine to be present (wired by bridge) so regime
        # classification is based on real market data, not warmup defaults.
        # Registry deduplicates unchanged regime (no per-tick log spam).
        if self._regime_registry is not None and self._last_feature_snapshot is not None:
            from grinder.controller.regime import classify_regime  # noqa: PLC0415

            fs = self._last_feature_snapshot
            toxicity_result = None
            if self._toxicity_gate is not None:
                toxicity_result = self._toxicity_gate.check(
                    fs.ts, fs.symbol, float(fs.spread_bps), fs.mid_price
                )
            rd = classify_regime(
                features=fs,
                kill_switch_active=self._config.kill_switch_active,
                toxicity_result=toxicity_result,
            )
            self._regime_registry.publish(snapshot.symbol, rd.regime, rd.reason, rd.confidence)

        # Adverse price-level trigger: FORCE_REDUCE at 16 steps, FORCED_FLAT at 20 steps
        # These are price thresholds (reference ± step_price * level), not lot counts.
        if (
            self._grid_v2_started
            and self._grid_v2_bridge is not None
            and not self._forced_flat_requested
            and self._grid_v2_bridge.state_machine is not None
        ):
            self._check_adverse_level_trigger(snapshot)

        # Execute forced-flat if requested (symbol-scoped emergency close).
        # Short-circuit: suppress all normal action generation after forced-flat.
        if self._forced_flat_requested:
            if not self._forced_flat_executed:
                self._execute_forced_flat(snapshot)
            return LiveEngineOutput(
                paper_output=_DeferredPaperOutput(ts=snapshot.ts, symbol=snapshot.symbol),
                live_actions=[],
                armed=self._config.armed,
                mode=self._config.mode,
                kill_switch_active=self._config.kill_switch_active,
            )

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

        # PR4 (doc-27): Grid V2 startup + fill processing + cancel-ack routing
        if self._grid_v2_enabled and snapshot.symbol == self._grid_v2_symbol:
            _was_started = self._grid_v2_started
            self._grid_v2_try_startup(snapshot)
            if not _was_started and self._grid_v2_started:
                import time as _time  # noqa: PLC0415

                from grinder.observability.latency_telemetry import (  # noqa: PLC0415
                    log_grid_v2_startup,
                )

                startup_ms = int(
                    (
                        _time.monotonic()
                        - getattr(self, "_grid_v2_startup_begin_mono", _time.monotonic())
                    )
                    * 1000
                )
                seed_count = len(self._grid_v2_seed_actions)
                log_grid_v2_startup(snapshot.symbol, startup_ms, seed_count)
            # Flat-only automatic sizing: may trigger controlled reseed.
            self._maybe_update_grid_v2_order_size(snapshot)
            # Skip fill/cancel-ack detection while awaiting first account sync after
            # fresh start: seed CIDs aren't on exchange yet, so registry-vs-exchange
            # diff would be all false positives. Flag cleared by _tick_account_sync().
            if not self._grid_v2_seed_actions and not self._grid_v2_awaiting_sync:
                self._grid_v2_process_cancel_acks(snapshot.symbol, snapshot.ts)
                grid_v2_fill_actions = self._grid_v2_process_fills(
                    snapshot.symbol,
                    snapshot.ts,
                )
                # ADR-111: Track fill timestamp for burst churn suppression.
                # Use snapshot.ts (exchange time) — same domain as comparison
                # target in _tick_account_sync. Never use wall-clock here.
                if grid_v2_fill_actions:
                    self._last_fill_ts = max(self._last_fill_ts, snapshot.ts)
                    self._burst_suppression_fired = False  # new fill → allow one suppression
                # Track SM FLAT transition for drift-reconstruct cooldown
                self._grid_v2_track_flat_transition()
                # Same-tick dedup: extract PLACE_ENTRY slots from fill path
                # to prevent integrity repair from duplicating them.
                planned_slots = self._extract_planned_entry_slots(grid_v2_fill_actions)
                # ADR-096 PR-2: primary reconciler replaces tick watchdog
                if self._sync_reconciler_primary:
                    # Drain staged actions from last sync cycle.
                    # Filter stale PLACEs: fill path may have registered CIDs
                    # for these slots between staging and drain.
                    bridge = self._grid_v2_bridge
                    # ADR-112: Check if SM mode changed since staging.
                    # If mode changed (e.g., FLAT→LONG_BRANCH from a fill),
                    # drop stale PLACEs staged under the old mode.
                    current_mode = (
                        bridge.state_machine.mode.value if bridge and bridge.state_machine else ""
                    )
                    mode_changed = (
                        self._reconciler_staged_mode
                        and current_mode != self._reconciler_staged_mode
                    )
                    fill_since_staging = self._last_fill_ts > self._reconciler_staged_fill_ts
                    drained: list[ExecutionAction] = []
                    for a in self._sync_reconciler_pending_actions:
                        if (
                            a.action_type == ActionType.PLACE
                            and a.side is not None
                            and a.price is not None
                            and bridge is not None
                            and bridge.adapter.registry.cid_for_entry(a.side, a.price) is not None
                        ):
                            continue  # slot already occupied by fill path
                        if a.action_type == ActionType.PLACE and mode_changed:
                            logger.info(
                                "GRID_V2_STALE_MODE_PLACE_DROPPED symbol=%s "
                                "staged_mode=%s current_mode=%s cid=%s",
                                self._grid_v2_symbol,
                                self._reconciler_staged_mode,
                                current_mode,
                                a.client_order_id or "?",
                            )
                            if a.client_order_id:
                                self._grid_v2_clean_failed_place(a.client_order_id)
                            continue  # stale mode PLACE
                        if a.action_type == ActionType.PLACE and fill_since_staging:
                            logger.info(
                                "GRID_V2_STALE_FILL_PLACE_DROPPED symbol=%s "
                                "staged_fill_ts=%d current_fill_ts=%d cid=%s",
                                self._grid_v2_symbol,
                                self._reconciler_staged_fill_ts,
                                self._last_fill_ts,
                                a.client_order_id or "?",
                            )
                            if a.client_order_id:
                                self._grid_v2_clean_failed_place(a.client_order_id)
                            continue  # stale fill PLACE
                        drained.append(a)
                    grid_v2_integrity_actions = drained
                    self._sync_reconciler_pending_actions = []
                    # Do NOT reset staged_fill_ts to 0 here. Staging (line 4488)
                    # sets it to _last_fill_ts. Resetting to 0 caused every
                    # subsequent drain to see "fill since staging" when _last_fill_ts
                    # was updated by a same-tick snapshot-diff fill, permanently
                    # suppressing all reconciler PLACEs in active markets.
                else:
                    grid_v2_integrity_actions = self._grid_v2_integrity_repair(
                        snapshot, planned_slots_this_tick=planned_slots
                    )
            else:
                grid_v2_fill_actions = []
                grid_v2_integrity_actions = []
        else:
            grid_v2_fill_actions = []
            grid_v2_integrity_actions = []

        _seed_batch: list[ExecutionAction] = []  # tracked for latency logging
        _fill_cancel_batch: list[ExecutionAction] = []  # post-fill concurrent cancel wave
        # Step 1: Get actions -- either from GridV2Bridge, LiveGridPlannerV1, or PaperEngine
        if self._grid_v2_enabled and snapshot.symbol == self._grid_v2_symbol:
            # Grid V2 symbol: either active (dispatch actions) or blocked (no actions).
            # Never falls through to legacy planner for this symbol.
            if self._is_grid_v2_active(snapshot.symbol):
                # Drain seed actions on first active tick, then fill-driven.
                # Fill actions are reordered: reduce-only exits first (protective),
                # then entry PLACEs, then CANCELs last. This minimizes time-to-hedge
                # after a real fill.
                fill_actions = _reorder_fill_actions(grid_v2_fill_actions)
                _seed_batch = list(self._grid_v2_seed_actions)
                self._grid_v2_seed_actions.clear()
                # Split fill actions: exits serial, grid_v2 cancels concurrent, rest serial.
                # Only grid_v2-originated cancels are safe for concurrent dispatch —
                # they bypass TP atomicity guards which only apply to non-grid_v2 cancels.
                _fill_cancel_batch = [
                    a
                    for a in fill_actions
                    if a.action_type == ActionType.CANCEL
                    and a.reason is not None
                    and a.reason.startswith("grid_v2_")
                ]
                _fill_cancel_cids = {a.order_id for a in _fill_cancel_batch}
                _fill_non_cancel = [
                    a
                    for a in fill_actions
                    if not (a.action_type == ActionType.CANCEL and a.order_id in _fill_cancel_cids)
                ]
                raw_actions: list[ExecutionAction] = _fill_non_cancel + list(
                    grid_v2_integrity_actions
                )
            else:
                # Blocked: startup not done, failed, or non-flat/no-orders guard hit.
                raw_actions = []
            paper_output: Any = _DeferredPaperOutput(
                ts=snapshot.ts, symbol=snapshot.symbol, actions=raw_actions
            )
        elif grid_frozen or budget_dead:
            # Frozen/budget dead: no planner actions (TP reduce-only still allowed)
            raw_actions = []
            paper_output = _DeferredPaperOutput(
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
        # PR4 (doc-27): grid_v2 symbol bypasses cycle layer entirely —
        # grid_v2 owns its own fill detection and exit placement.
        _grid_v2_owns_symbol = self._grid_v2_enabled and snapshot.symbol == self._grid_v2_symbol
        if (
            self._is_cycle_layer_enabled()
            and self._last_account_snapshot is not None
            and not _grid_v2_owns_symbol
        ):
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

        # PR4 (doc-27): register grid_v2 cancels for cancel-ack tracking
        if self._is_grid_v2_active(snapshot.symbol):
            self._grid_v2_register_pending_cancels(raw_actions, snapshot.ts)

        # Replenish-on-TP-fill: detect position decrease → add BUY below + SELL above
        # Bypassed in rolling mode — planner diff handles slot restoration.
        # PR4 (doc-27): grid_v2 symbol bypasses replenish — grid_v2 owns exit placement.
        if (
            self._is_cycle_layer_enabled()
            and self._last_account_snapshot is not None
            and not rolling
            and not _grid_v2_owns_symbol
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
        # Throttled: at most once per _account_sync_interval_ms to avoid REST rate-limits.
        # One-shot skip on the first fill tick to avoid blocking the fill→exit hot
        # path with a ~500ms REST roundtrip. The skip fires once then clears, so
        # sustained fills still get periodic sync (bounded, not indefinite).
        _fill_tick = bool(grid_v2_fill_actions) if self._grid_v2_enabled else False
        _skip_sync = _fill_tick and not getattr(self, "_fill_sync_skip_used", False)
        if _skip_sync:
            self._fill_sync_skip_used = True
        elif not _fill_tick:
            self._fill_sync_skip_used = False  # reset on non-fill tick
        if self._is_account_sync_enabled() and snapshot.ts > 0 and not _skip_sync:
            effective_interval, sync_reason = self._get_effective_sync_interval()
            elapsed = snapshot.ts - self._account_sync_last_attempt_ms
            if elapsed >= effective_interval:
                if sync_reason == "trusted_fresh":
                    logger.debug(
                        "REST_SYNC_DEFERRED_UNTIL_NOW symbol=%s reason=%s interval=%d elapsed=%d",
                        snapshot.symbol,
                        sync_reason,
                        effective_interval,
                        elapsed,
                    )
                self._account_sync_last_attempt_ms = snapshot.ts
                self._tick_account_sync()

        # Step 2: Process actions
        live_actions: list[LiveAction] = []
        raw_actions = paper_output.actions if hasattr(paper_output, "actions") else []

        # PR5 (doc-27): Grid V2 shadow tick — run AFTER legacy actions are final,
        # BEFORE dispatch. Shadow never modifies raw_actions or live_actions.
        if self._grid_v2_shadow is not None and snapshot.symbol == self._grid_v2_symbol:
            self._grid_v2_shadow_tick(snapshot, raw_actions)

        # PR-P0-TP-CLOSE-ATOMIC: retry failed TP_CLOSE PLACEs from previous ticks
        retry_results = self._process_tp_close_retries(snapshot.symbol, snapshot.ts)
        live_actions.extend(retry_results)

        # PR-P0-TP-RENEW-ATOMIC: track whether TP_RENEW PLACE succeeded per symbol.
        # If PLACE was blocked, skip the paired CANCEL to keep old TP alive.
        tp_renew_place_ok: dict[str, bool] = {}

        # PR-P0-TP-CLOSE-ATOMIC: track TP_CLOSE PLACE success per correlation_id.
        # If PLACE failed, skip paired TP_SLOT_TAKEOVER CANCEL (same correlation_id).
        tp_close_place_ok: dict[str, bool] = {}

        # Post-fill phase tracking for detailed latency telemetry.
        # Only active when fill actions are being dispatched.
        _fill_phase_timer: Any = None
        _fill_exit_ms: int = -1
        _fill_first_cancel_ms: int = -1
        _fill_last_cancel_ms: int = -1
        _fill_cancel_count: int = 0
        if grid_v2_fill_actions and self._grid_v2_enabled:
            from grinder.observability.latency_telemetry import PhaseTimer as _FT  # noqa: PLC0415

            _fill_phase_timer = _FT()

        # ADR-090 follow-up: per-sync-cycle CANCEL dedup (mechanisms 1+4).
        # Same-tick: planner + cycle_layer both emit CANCEL for same CID.
        # Cross-tick: successful CANCEL on tick N, planner re-generates on tick N+1
        # (snapshot not refreshed yet). Both caught by instance-level set,
        # cleared on AccountSync refresh.

        # Dispatch seed batch via isolated helper (includes latency logging)
        if _seed_batch:
            seed_result = self._dispatch_grid_v2_seed_batch(_seed_batch, snapshot.ts)
            live_actions.extend(seed_result.live_actions)

        # Dispatch post-fill cancel wave with bounded concurrency
        if _fill_cancel_batch:
            _cancel_wave_start = _fill_phase_timer.elapsed_ms() if _fill_phase_timer else 0
            cancel_results = self._dispatch_cancel_wave(_fill_cancel_batch, snapshot.ts)
            live_actions.extend(cancel_results)
            # Feed cancel wave timing into fill phase trackers
            if _fill_phase_timer is not None:
                _cancel_wave_end = _fill_phase_timer.elapsed_ms()
                _fill_first_cancel_ms = _cancel_wave_start
                _fill_last_cancel_ms = _cancel_wave_end
                _fill_cancel_count = len(
                    [r for r in cancel_results if r.status != LiveActionStatus.SKIPPED]
                )

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

            # ADR-090 follow-up: skip CANCEL if same CID already dispatched this sync cycle.
            if (
                action.action_type == ActionType.CANCEL
                and action.order_id is not None
                and action.order_id in self._cancel_dispatched_pending_sync
            ):
                logger.debug(
                    "CANCEL_SKIP_DUPLICATE symbol=%s order_id=%s reason=%s",
                    action.symbol,
                    action.order_id,
                    action.reason,
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

            # ADR-090 follow-up: skip stale CANCEL when order is absent from
            # current snapshot. Race: planner builds actions from old snapshot,
            # then AccountSync refreshes mid-tick, then dispatch runs stale actions.
            # Scoped to grid-planner reasons only (GRID_TRIM/GRID_SHIFT/GRID_RESIZE).
            # TP-managed CANCELs (TP_SLOT_TAKEOVER/TP_RENEW/TP_CLOSE) have their
            # own atomicity guards and must not be filtered here.
            _GRID_CANCEL_REASONS = {"GRID_TRIM", "GRID_SHIFT", "GRID_RESIZE"}
            if (
                action.action_type == ActionType.CANCEL
                and action.order_id is not None
                and action.reason in _GRID_CANCEL_REASONS
                and self._last_account_snapshot is not None
            ):
                live_order_ids = {
                    o.order_id
                    for o in self._last_account_snapshot.open_orders
                    if o.symbol == action.symbol
                }
                if action.order_id not in live_order_ids:
                    logger.debug(
                        "CANCEL_SKIP_STALE_ACTION symbol=%s order_id=%s reason=%s",
                        action.symbol,
                        action.order_id,
                        action.reason,
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

            # Post-fill phase timing capture
            if (
                _fill_phase_timer is not None
                and action.reason
                and action.reason.startswith("grid_v2_")
            ):
                elapsed = _fill_phase_timer.elapsed_ms()
                if (
                    action.action_type == ActionType.PLACE
                    and action.reduce_only
                    and _fill_exit_ms < 0
                ):
                    _fill_exit_ms = elapsed
                elif action.action_type == ActionType.CANCEL:
                    if _fill_first_cancel_ms < 0:
                        _fill_first_cancel_ms = elapsed
                    _fill_last_cancel_ms = elapsed
                    _fill_cancel_count += 1

            # BUG-4: track failed CANCELs for -2011 suppression
            if (
                action.action_type == ActionType.CANCEL
                and action.order_id is not None
                and live_action.status == LiveActionStatus.FAILED
                and not self._grid_v2_handle_failed_cancel(
                    action.order_id, live_action.exchange_code
                )
            ):
                self._cancel_failed_ids.add(action.order_id)

            # Grid V2 PLACE lifecycle:
            # - EXECUTED → pending (visibility gate until snapshot confirms)
            # - BLOCKED/SKIPPED → never sent to exchange, safe to clean
            # - FAILED + pre_send → local validation before HTTP, safe to clean
            # - FAILED + !pre_send → ambiguous (post-HTTP error, order might exist)
            #   Quarantine as pending; snapshot resolves via visibility or grace.
            if (
                action.action_type == ActionType.PLACE
                and action.client_order_id is not None
                and self._grid_v2_bridge is not None
                and self._grid_v2_bridge.adapter.is_ours(action.client_order_id)
            ):
                if live_action.status == LiveActionStatus.EXECUTED:
                    self._grid_v2_register_pending_place(action.client_order_id)
                elif live_action.status in (
                    LiveActionStatus.BLOCKED,
                    LiveActionStatus.SKIPPED,
                ):
                    # Never sent to exchange — safe to clean immediately
                    self._grid_v2_clean_failed_place(action.client_order_id)
                elif live_action.status == LiveActionStatus.FAILED and live_action.pre_send:
                    # Local validation error before HTTP (symbol, notional, order count).
                    # Order was never sent — safe to clean immediately.
                    self._grid_v2_clean_failed_place(action.client_order_id)
                elif live_action.status == LiveActionStatus.FAILED:
                    # Post-HTTP error: classify by exchange_code.
                    if _grid_v2_is_exchange_code_ambiguous(live_action.exchange_code):
                        # Ambiguous: no code (retry exhaustion / network), or
                        # duplicate-like code (-2010) where the order
                        # might actually exist on exchange. Quarantine.
                        self._grid_v2_register_pending_place(action.client_order_id)
                        logger.warning(
                            "GRID_V2_FAILED_PLACE_QUARANTINED cid=%s code=%s reason=%s",
                            action.client_order_id,
                            live_action.exchange_code,
                            live_action.block_reason.value if live_action.block_reason else "?",
                        )
                    else:
                        # Explicit exchange reject with definitive code
                        # (e.g. -1111 precision, -4014 tick, -4164 notional).
                        # Order was NOT placed — safe to clean.
                        self._grid_v2_clean_failed_place(action.client_order_id)

                # F2 protective exits: clear from pending_seed_cids on definitive failure
                # to prevent awaiting_sync deadlock.
                # Definitive = BLOCKED, SKIPPED, FAILED+pre_send, or FAILED+non-ambiguous code.
                # Ambiguous FAILED (no code / -2010-like) stays pending for snapshot resolution.
                seed_definitive_fail = (
                    action.client_order_id is not None
                    and action.client_order_id in self._grid_v2_pending_seed_cids
                    and (
                        live_action.status in (LiveActionStatus.BLOCKED, LiveActionStatus.SKIPPED)
                        or (live_action.status == LiveActionStatus.FAILED and live_action.pre_send)
                        or (
                            live_action.status == LiveActionStatus.FAILED
                            and not live_action.pre_send
                            and not _grid_v2_is_exchange_code_ambiguous(live_action.exchange_code)
                        )
                    )
                )
                if seed_definitive_fail and action.client_order_id is not None:
                    self._grid_v2_pending_seed_cids = self._grid_v2_pending_seed_cids - {
                        action.client_order_id
                    }
                    logger.warning(
                        "GRID_V2_SEED_CID_CLEARED cid=%s status=%s code=%s",
                        action.client_order_id,
                        live_action.status.value,
                        live_action.exchange_code,
                    )
                    if not self._grid_v2_pending_seed_cids:
                        self._grid_v2_awaiting_sync = False
                        logger.warning(
                            "GRID_V2_AWAITING_SYNC_CLEARED_ON_SEED_FAILURE "
                            "reason=all_seeds_definitively_failed"
                        )

            # ADR-090 follow-up: record dispatched CANCEL CID for per-sync-cycle dedup.
            if (
                action.action_type == ActionType.CANCEL
                and action.order_id is not None
                and live_action.status == LiveActionStatus.FAILED
                and not self._grid_v2_handle_failed_cancel(
                    action.order_id, live_action.exchange_code
                )
            ):
                self._cancel_failed_ids.add(action.order_id)

            if (
                action.action_type == ActionType.CANCEL
                and action.order_id is not None
                and live_action.status in (LiveActionStatus.EXECUTED, LiveActionStatus.FAILED)
            ):
                self._cancel_dispatched_pending_sync.add(action.order_id)

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

        # Emit post-fill phase latency summaries
        if _fill_phase_timer is not None and (_fill_exit_ms >= 0 or _fill_cancel_count > 0):
            from grinder.observability.latency_telemetry import (  # noqa: PLC0415
                log_branch_convergence,
                log_fill_cancel_wave,
                log_fill_exit,
            )

            total_ms = _fill_phase_timer.elapsed_ms()
            sym = snapshot.symbol
            if _fill_exit_ms >= 0:
                log_fill_exit(sym, _fill_exit_ms)
            if _fill_cancel_count > 0:
                log_fill_cancel_wave(
                    sym, _fill_first_cancel_ms, _fill_last_cancel_ms, _fill_cancel_count
                )
            log_branch_convergence(sym, total_ms, len(grid_v2_fill_actions))

        # Doc-36 Phase 1: shadow selector tick (post-dispatch, no side effects)
        if self._shadow_selector is not None and self._last_feature_snapshot is not None:
            self._shadow_selector.update_features(self._last_feature_snapshot)
            try:
                self._shadow_selector.maybe_run(snapshot.ts, self._operator_symbols)
            except Exception:
                logger.exception("SELECTOR_SHADOW_ERROR — fail-open, continuing")

        # Doc-36 Phase 2: active selector tick (post-dispatch)
        if self._active_selector is not None and self._last_feature_snapshot is not None:
            self._active_selector.update_features(self._last_feature_snapshot)
            try:
                non_flat = self._get_non_flat_symbols()
                self._active_selector.maybe_run(
                    snapshot.ts, self._operator_symbols, non_flat_symbols=non_flat
                )
            except Exception:
                logger.exception("SELECTOR_ACTIVE_ERROR — fail-safe, continuing")

        # Step 3: Build output
        return LiveEngineOutput(
            paper_output=paper_output,
            live_actions=live_actions,
            armed=self._config.armed,
            mode=self._config.mode,
            kill_switch_active=self._config.kill_switch_active,
        )

    def _get_non_flat_symbols(self) -> set[str]:
        """Get symbols with non-flat grid_v2 inventory (for graceful_exit_only).

        Returns empty set if grid_v2 is not active.
        """
        bridge = self._grid_v2_bridge
        if bridge is None:
            return set()
        sm = bridge.state_machine
        if sm is None:
            return set()
        if sm.mode.value != "FLAT":
            return {self._grid_v2_symbol}
        return set()

    def is_selector_dispatch_allowed(self, symbol: str) -> bool:
        """Check if active selector allows new entries for symbol.

        Returns True if no active selector configured (fail-open).
        """
        if self._active_selector is None:
            return True
        return self._active_selector.is_dispatch_allowed(symbol)

    @property
    def force_reduce_requested(self) -> bool:
        """Whether force-reduce has been requested for this engine."""
        return self._force_reduce_requested

    def request_force_reduce(self, reason: str = "") -> bool:
        """Request force-reduce mode. Idempotent — returns True on first request.

        Sets engine-side flag that activates SymbolUnloadController in _update_risk_state().
        Staged reduction through existing unload machinery — no instant flatten.
        """
        if self._force_reduce_requested:
            return False
        self._force_reduce_requested = True
        self._force_reduce_reason = reason
        logger.info(
            "ENGINE_FORCE_REDUCE_REQUESTED symbols=%s reason=%s",
            self._operator_symbols,
            reason or "unspecified",
        )
        return True

    def request_forced_flat(self, reason: str = "") -> bool:
        """Request forced-flat mode. Idempotent — returns True on first request.

        Stronger than force-reduce: cancels all symbol orders and market-closes
        position using EmergencyExitExecutor. No staged unload — direct flatten.
        """
        if self._forced_flat_requested:
            return False
        self._forced_flat_requested = True
        # Also ensure force-reduce is set (ladder escalation)
        if not self._force_reduce_requested:
            self._force_reduce_requested = True
            self._force_reduce_reason = reason
        logger.critical(
            "ENGINE_FORCED_FLAT_REQUESTED symbols=%s reason=%s",
            self._operator_symbols,
            reason or "unspecified",
        )
        return True

    def _execute_forced_flat(self, snapshot: Snapshot) -> None:
        """Execute symbol-scoped forced-flat: cancel orders + market close."""
        if self._forced_flat_executed:
            return

        symbol = snapshot.symbol
        executor = self._emergency_exit_executor
        if executor is None:
            logger.error(
                "FORCED_FLAT_NO_EXECUTOR symbol=%s reason=emergency_exit_not_configured",
                symbol,
            )
            return

        logger.critical("FORCED_FLAT_EXECUTING symbol=%s", symbol)
        result = executor.execute(
            ts_ms=snapshot.ts,
            reason="ADVERSE_LEVEL_20_FORCED_FLAT",
            symbols=[symbol],
        )
        if result.success:
            self._forced_flat_executed = True
            logger.info(
                "FORCED_FLAT_CONFIRMED symbol=%s cancelled=%d closed=%d",
                symbol,
                result.orders_cancelled,
                result.market_orders_placed,
            )
        else:
            logger.error(
                "FORCED_FLAT_PARTIAL symbol=%s remaining=%d — will retry next tick",
                symbol,
                result.positions_remaining,
            )

    def _check_adverse_level_trigger(self, snapshot: Snapshot) -> None:
        """Check if price has breached adverse price thresholds.

        Thresholds are computed as reference_price ± step_price * level:
        - Level 16: FORCE_REDUCE (reduce-only unload begins).
        - Level 20: FORCED_FLAT (emergency full flatten).
        These are adverse PRICE levels, not lot counts.
        Checks level 20 first (stronger overrides softer).
        Idempotent. Fail-open on errors.
        """
        import contextlib  # noqa: PLC0415

        with contextlib.suppress(Exception):
            self._check_adverse_level_trigger_inner(snapshot)

    def _check_adverse_level_trigger_inner(self, snapshot: Snapshot) -> None:
        from grinder.grid_v2.state import BranchMode  # noqa: PLC0415
        from grinder.risk.adverse_trigger import (  # noqa: PLC0415
            compute_adverse_threshold,
            is_adverse_level_breached,
        )
        from grinder.risk.grid_policy import DEFAULT_GRID_POLICY  # noqa: PLC0415

        bridge = self._grid_v2_bridge
        if bridge is None or bridge.state_machine is None:
            return

        sm = bridge.state_machine
        mode = sm.mode

        if mode == BranchMode.FLAT:
            return

        side = "LONG" if mode == BranchMode.LONG_BRANCH else "SHORT"
        ref_price = sm.snapshot.entry_window.reference_price
        step_pct = bridge._config.grid_step_pct
        tick = bridge._config.price_tick_size

        # Check adverse price level 20 (FORCED_FLAT) first — stronger overrides softer
        if not self._forced_flat_requested:
            flat_threshold = compute_adverse_threshold(
                ref_price,
                step_pct,
                tick,
                adverse_level=DEFAULT_GRID_POLICY.forced_flat_trigger_level,
                side=side,
            )
            if flat_threshold is not None and is_adverse_level_breached(
                snapshot.mid_price, flat_threshold, side
            ):
                logger.critical(
                    "GRID_ADVERSE_LEVEL_BREACHED symbol=%s level=%d price=%s threshold=%s side=%s",
                    snapshot.symbol,
                    DEFAULT_GRID_POLICY.forced_flat_trigger_level,
                    snapshot.mid_price,
                    flat_threshold,
                    side,
                )
                self.force_graceful_exit(snapshot.symbol)
                self.request_forced_flat(reason="ADVERSE_LEVEL_20")
                return

        # Check adverse price level 16 (FORCE_REDUCE)
        if not self._force_reduce_requested:
            reduce_threshold = compute_adverse_threshold(
                ref_price,
                step_pct,
                tick,
                adverse_level=DEFAULT_GRID_POLICY.force_reduce_trigger_level,
                side=side,
            )
            if reduce_threshold is not None and is_adverse_level_breached(
                snapshot.mid_price, reduce_threshold, side
            ):
                logger.warning(
                    "GRID_ADVERSE_LEVEL_BREACHED symbol=%s level=%d price=%s threshold=%s side=%s",
                    snapshot.symbol,
                    DEFAULT_GRID_POLICY.force_reduce_trigger_level,
                    snapshot.mid_price,
                    reduce_threshold,
                    side,
                )
                self.force_graceful_exit(snapshot.symbol)
                self.request_force_reduce(reason="ADVERSE_LEVEL_16")

    def force_graceful_exit(self, symbol: str) -> bool:
        """Force a symbol into graceful-exit-only mode (operator/ceremony use).

        Adds symbol to ActiveSelector.graceful_exit_only, blocking new entries.
        Exits, cancels, and grid_v2 internal actions continue normally.

        Returns True if applied, False if no active selector configured.
        """
        if self._active_selector is None:
            return False
        self._active_selector._graceful_exit_only.add(symbol)
        logger.info("FORCE_GRACEFUL_EXIT symbol=%s", symbol)
        return True

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

    def _get_effective_sync_interval(self) -> tuple[int, str]:
        """Determine sync interval based on EventLedger trust + user-data freshness.

        Returns (interval_ms, reason) for logging.

        Policy:
        - Startup not converged → base interval (5s)
        - EventLedger not trusted → base interval (5s)
        - User-data events stale (>5s) → base interval (5s)
        - All fresh + trusted → extended interval (15s)
        """
        # Startup: always use base interval until grid_v2 is active
        if not self._grid_v2_started:
            return self._account_sync_interval_ms, "startup"

        # EventLedger trust check
        if not self._event_ledger.is_trusted:
            return self._account_sync_interval_ms, "ledger_not_trusted"

        # User-data freshness check (monotonic, >5s = stale)
        now = time.monotonic()
        user_data_age = now - self._last_user_data_event_mono
        if self._last_user_data_event_mono <= 0 or user_data_age > 5.0:
            return self._account_sync_interval_ms, "user_data_stale"

        # Convergence check: pending state means reconciliation needs REST truth
        if (
            self._grid_v2_awaiting_sync
            or self._grid_v2_pending_place_cids
            or self._grid_v2_pending_cancels
        ):
            reason = (
                "awaiting_sync"
                if self._grid_v2_awaiting_sync
                else "pending_places"
                if self._grid_v2_pending_place_cids
                else "pending_cancels"
            )
            return self._account_sync_interval_ms, reason

        # All conditions met: extend interval
        return self._account_sync_trusted_interval_ms, "trusted_fresh"

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

    def _tick_account_sync(self) -> None:  # noqa: PLR0912, PLR0915
        """Run one account sync cycle (read-only).

        Fetches snapshot, detects mismatches, records metrics.
        Updates _position_notional_usd from snapshot positions (PR-A4).
        Evidence writing is delegated to evidence.py (env-gated).
        """
        assert self._account_syncer is not None  # caller guards
        from grinder.observability.latency_telemetry import (  # noqa: PLC0415
            PhaseTimer,
            log_account_sync,
        )

        sync_timer = PhaseTimer()
        result = self._account_syncer.sync()
        sync_ms = sync_timer.elapsed_ms()
        if self._grid_v2_symbol:
            log_account_sync(self._grid_v2_symbol, sync_ms)

        if result.error is not None:
            logger.warning("Account sync failed: %s", result.error)
            self._on_sync_failure()
            # Detect DNS errors for health gate
            error_str = str(result.error)
            if "name resolution" in error_str.lower() or "connection error" in error_str.lower():
                self._on_dns_error()
            if "-1021" in error_str:
                self._on_clock_drift_error()
                # Refresh server-time offset to recover from drift
                if hasattr(self._exchange_port, "refresh_ts_offset"):
                    self._exchange_port.refresh_ts_offset()
            return

        # Successful sync: update health signals
        self._on_sync_success()

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
            # Symbol risk evaluation (per-symbol, post account sync).
            # Force-reduce overrides the enabled gate — it's an explicit
            # risk management decision, not a config flag.
            if self._symbol_risk_manager.config.enabled or self._force_reduce_requested:
                self._evaluate_symbol_risk(result.snapshot)
            # PR-L2: Store full snapshot for LiveGridPlannerV1 (open_orders as exchange truth)
            self._last_account_snapshot = result.snapshot
            # ADR-109 Phase 2: Hydrate ledger from every sync (idempotent).
            # Fixes Phase 1 bug: one-shot bootstrap ran on preflight sync
            # (0 orders), causing permanent divergence for the session.
            hydrated = self._event_ledger.hydrate_from_snapshot(result.snapshot)
            if hydrated > 0:
                logger.info(
                    "EVENT_LEDGER_HYDRATED orders=%d snapshot_ts=%d bootstrapped=%s trusted=%s",
                    hydrated,
                    result.snapshot.ts,
                    self._event_ledger.bootstrapped,
                    self._event_ledger.is_trusted,
                )
            # ADR-109 Phase 2: Compare and update trust signal.
            shadow = self._event_ledger.compare_with_snapshot(result.snapshot)
            if not shadow.is_converged:
                logger.info(
                    "EVENT_LEDGER_SHADOW_DIVERGENCE "
                    "divergences=%d ledger_orders=%d snapshot_orders=%d "
                    "trusted=%s",
                    len(shadow.divergences),
                    shadow.ledger_open_orders,
                    shadow.snapshot_open_orders,
                    self._event_ledger.is_trusted,
                )
                for d in shadow.divergences[:5]:
                    logger.info("  %s symbol=%s %s", d.kind.value, d.symbol, d.detail)
                # Reconcile stale ledger-open orders using snapshot as recovery.
                # Conservative: requires two consecutive absences before closing.
                reconciled = self._event_ledger.reconcile_with_snapshot()
                if reconciled > 0:
                    # Re-compare after reconciliation to check convergence
                    shadow = self._event_ledger.compare_with_snapshot(result.snapshot)
                    logger.info(
                        "EVENT_LEDGER_RECONCILED orders=%d converged=%s",
                        reconciled,
                        shadow.is_converged,
                    )
            if shadow.is_converged:
                # Converged. If trust was revoked (degraded mode), attempt restore
                # only when health is back to HEALTHY.
                from grinder.live.health_gate import LiveHealthMode  # noqa: PLC0415

                if self._event_ledger.trust_revoked and self._health_mode == LiveHealthMode.HEALTHY:
                    self._event_ledger.restore_trust_after_recovery()
                if self._event_ledger.is_trusted:
                    logger.debug(
                        "EVENT_LEDGER_TRUSTED_READ_MODEL ledger_orders=%d snapshot_orders=%d",
                        shadow.ledger_open_orders,
                        shadow.snapshot_open_orders,
                    )
            # ADR-109 Phase 3: PositionLedger hydration + comparison + trust decision
            try:
                pos_hydrated = self._position_ledger.hydrate_from_snapshot(result.snapshot)
                if pos_hydrated > 0:
                    logger.info(
                        "POSITION_LEDGER_HYDRATED positions=%d snapshot_ts=%d "
                        "bootstrapped=%s trusted=%s",
                        pos_hydrated,
                        result.snapshot.ts,
                        self._position_ledger._bootstrapped,
                        self._position_ledger.is_trusted,
                    )
                pos_cmp = self._position_ledger.compare_with_snapshot(result.snapshot)
                self._position_ledger.record_comparison_result(pos_cmp.is_converged)
                logger.debug(
                    "POSITION_LEDGER_COMPARE converged=%s bootstrapped=%s trusted=%s "
                    "ledger=%d snapshot=%d",
                    pos_cmp.is_converged,
                    self._position_ledger._bootstrapped,
                    self._position_ledger.is_trusted,
                    pos_cmp.ledger_count,
                    pos_cmp.snapshot_count,
                )
                if not pos_cmp.is_converged:
                    for div in pos_cmp.divergences[:5]:
                        logger.info(
                            "POSITION_LEDGER_SHADOW_DIVERGENCE symbol=%s side=%s kind=%s detail=%s",
                            div.symbol,
                            div.position_side,
                            div.kind.value,
                            div.detail,
                        )
                elif self._position_ledger.is_trusted:
                    logger.debug(
                        "POSITION_LEDGER_TRUSTED_READ_MODEL ledger_positions=%d snapshot_positions=%d",
                        pos_cmp.ledger_count,
                        pos_cmp.snapshot_count,
                    )
            except Exception:
                logger.debug("POSITION_LEDGER_COMPARE_ERROR", exc_info=True)

            # PR6: clear awaiting-sync flag only when ALL seed CIDs are visible.
            # ADR-109 Phase 2: prefer ledger for visibility when trusted,
            # fall back to snapshot. Ledger may know about orders sooner
            # via WS events than the REST snapshot (cached/stale).
            if self._grid_v2_awaiting_sync and self._grid_v2_pending_seed_cids:
                if self._event_ledger.is_trusted:
                    visible_cids = set(self._event_ledger.open_orders().keys())
                else:
                    visible_cids = {o.order_id for o in result.snapshot.open_orders}
                missing = self._grid_v2_pending_seed_cids - visible_cids
                if not missing:
                    confirmed_count = len(self._grid_v2_pending_seed_cids)
                    self._grid_v2_awaiting_sync = False
                    self._grid_v2_pending_seed_cids = frozenset()
                    logger.info(
                        "GRID_V2_AWAITING_SYNC_CLEARED gen=%d seeds_confirmed=%d",
                        self._account_sync_generation + 1,
                        confirmed_count,
                    )
                else:
                    logger.debug(
                        "GRID_V2_AWAITING_SYNC_PENDING missing=%d/%d",
                        len(missing),
                        len(self._grid_v2_pending_seed_cids),
                    )
            # Clear pending-place CIDs: visible on exchange OR grace expired.
            # Grace = 2 sync cycles. After grace, CID released for fill detection
            # (handles immediate-fill before first snapshot visibility).
            # ADR-109 Phase 2: use ledger when trusted for faster visibility.
            if self._grid_v2_pending_place_cids:
                if self._event_ledger.is_trusted:
                    visible_cids = set(self._event_ledger.open_orders().keys())
                else:
                    visible_cids = {o.order_id for o in result.snapshot.open_orders}
                gen = self._account_sync_generation + 1  # gen about to be set
                expired: list[str] = []
                for cid, dispatch_gen in list(self._grid_v2_pending_place_cids.items()):
                    if cid in visible_cids or (gen - dispatch_gen) >= 2:
                        expired.append(cid)
                if expired:
                    for cid in expired:
                        del self._grid_v2_pending_place_cids[cid]
                    logger.debug(
                        "GRID_V2_PENDING_PLACES_CLEARED count=%d remaining=%d gen=%d",
                        len(expired),
                        len(self._grid_v2_pending_place_cids),
                        gen,
                    )
            # PR-P0-RACE-1: monotonic generation counter for convergence guards
            self._account_sync_generation += 1
            # BUG-4 + ADR-090 follow-up: selective prune of cancel-failed blacklist.
            # Keep CIDs that are still visible in fresh snapshot (Binance propagation
            # lag — cancel returned -2011 but order still appears in REST).
            # Remove CIDs that are absent from fresh snapshot (order is gone).
            # ADR-109 Phase 2: use ledger when trusted for faster visibility.
            if self._cancel_failed_ids:
                if self._event_ledger.is_trusted:
                    live_order_ids = set(self._event_ledger.open_orders().keys())
                else:
                    live_order_ids = {o.order_id for o in result.snapshot.open_orders}
                surviving = self._cancel_failed_ids & live_order_ids
                pruned_count = len(self._cancel_failed_ids) - len(surviving)
                logger.info(
                    "CANCEL_FAILED_IDS_PRUNED pruned=%d surviving=%d gen=%d",
                    pruned_count,
                    len(surviving),
                    self._account_sync_generation,
                )
                self._cancel_failed_ids = surviving

            # ADR-090 follow-up: clear cross-tick cancel dedup set on sync refresh.
            # Snapshot now reflects any successfully dispatched cancels.
            self._cancel_dispatched_pending_sync.clear()
            # Fast-path convergence guard: if exchange already has non-flat position
            # while SM is still FLAT, force reconstruction from fresh snapshot.
            self._grid_v2_sync_reconstruct_on_position_drift(result.snapshot)

        # Evidence writing (env-gated, safe-by-default)
        if result.snapshot is not None:
            evidence_dir = write_evidence_bundle(result.snapshot, result.mismatches)
            if evidence_dir is not None:
                logger.info("Account sync evidence written to %s", evidence_dir)

        # PR-1 (ADR-092): Update risk base snapshot from exchange balance
        # Use wall-clock as freshness marker: we just fetched the data now.
        # Exchange ts (result.snapshot.ts) can be stale if Binance caches the
        # account snapshot, causing age_s to grow indefinitely and triggering
        # false stale blocks.
        if self._risk_base_enabled and result.snapshot is not None:
            self._update_risk_base(asof_ts_ms=int(time.time() * 1000))

        # ADR-104: Reduce-only budget repair on sync.
        # Detect and cancel surplus reduce-only exits before reconciler runs.
        if (
            result.snapshot is not None
            and self._grid_v2_bridge is not None
            and self._grid_v2_started
        ):
            self._reduce_only_repair_on_sync(result.snapshot)

        # ADR-105: Exit topology repair on sync.
        # After budget repair (ADR-104), converge exit set to desired legal topology.
        if (
            result.snapshot is not None
            and self._grid_v2_bridge is not None
            and self._grid_v2_bridge.state_machine is not None
            and self._grid_v2_started
            and self._grid_v2_bridge.reconstruction_ok
        ):
            self._exit_topology_repair_on_sync(result.snapshot)

        # ADR-096: Sync-driven reconciler
        if (
            self._sync_reconciler_enabled
            and result.snapshot is not None
            and self._grid_v2_bridge is not None
            and self._grid_v2_bridge.state_machine is not None
            and self._grid_v2_started
            and self._grid_v2_bridge.reconstruction_ok
        ):
            # Pre-pass: clean stale registry entries that block reconciler PLACE.
            # Two-cycle rule: only clean CIDs absent from exchange in BOTH
            # previous AND current sync. This prevents racing with fill
            # detection — a filled entry disappears from snapshot but must
            # be processed as a fill before it can be cleaned as stale.
            if self._event_ledger.is_trusted:
                exchange_cids = set(self._event_ledger.open_orders().keys())
            else:
                exchange_cids = {o.order_id for o in result.snapshot.open_orders}
            bridge = self._grid_v2_bridge
            current_absent: set[str] = set()
            for cid in list(bridge.adapter.registry.all_entry_cids):
                if (
                    cid not in exchange_cids
                    and cid not in self._grid_v2_pending_place_cids
                    and cid not in self._grid_v2_pending_cancels
                ):
                    current_absent.add(cid)
            # Only clean CIDs absent in both consecutive syncs
            stale_candidates = current_absent & self._prev_absent_registry_cids
            stale_cleaned = 0
            for cid in stale_candidates:
                bridge.adapter.confirm_cancel_entry(cid)
                stale_cleaned += 1
            self._prev_absent_registry_cids = current_absent
            if stale_cleaned:
                logger.info(
                    "GRID_V2_STALE_REGISTRY_CLEANED symbol=%s entries=%d",
                    self._grid_v2_symbol,
                    stale_cleaned,
                )

            from grinder.grid_v2.sync_reconciler import reconcile_grid_state  # noqa: PLC0415

            # ADR-102: Proactive saturation evaluation on sync.
            # Decouple recovery from future entry actions — evaluate here.
            _sym = self._grid_v2_symbol
            _legal_cap, _is_sym_cap = self._compute_risk_legal_entry_capacity(_sym, result.snapshot)

            # Check whether entries are actually missing on exchange (before risk
            # projection). "Restore demand" = desired entry keys from SM minus
            # actual entry keys on exchange. If all desired are already present,
            # zero-headroom is passive — not a "blocked entry."
            _sm = self._grid_v2_bridge.state_machine
            _desired_keys: set[tuple[OrderSide, Decimal]] = set()
            for _p in _sm.snapshot.entry_window.buy_entry_prices:
                _desired_keys.add(
                    (OrderSide.BUY, self._grid_v2_bridge._quantize_price(_p, OrderSide.BUY))
                )
            for _p in _sm.snapshot.entry_window.sell_entry_prices:
                _desired_keys.add(
                    (OrderSide.SELL, self._grid_v2_bridge._quantize_price(_p, OrderSide.SELL))
                )
            if _sm.mode != BranchMode.FLAT:
                _lots = len(_sm.snapshot.open_lots)
                _max = self._grid_v2_bridge._config.max_inventory_levels
                _hr = max(0, _max - _lots)
                if _hr == 0:
                    _desired_keys = set()
                elif (
                    isinstance(
                        _lps2 := getattr(self._grid_v2_bridge._config, "entry_levels_per_side", 5),
                        int,
                    )
                    and _hr < _lps2
                ):
                    _bs = OrderSide.BUY if _sm.mode == BranchMode.LONG_BRANCH else OrderSide.SELL
                    _ref = _sm.snapshot.entry_window.reference_price
                    _ss = sorted(
                        [(s, p) for s, p in _desired_keys if s == _bs],
                        key=lambda k: abs(k[1] - _ref),
                    )
                    if len(_ss) > _hr:
                        _desired_keys -= set(_ss[_hr:])

            _actual_keys: set[tuple[OrderSide, Decimal]] = set()
            for _o in result.snapshot.open_orders:
                if _o.symbol != _sym:
                    continue
                _parsed = self._grid_v2_bridge.adapter.parse_cid(_o.order_id)
                if _parsed is not None and _parsed.kind.value == "ENTRY":
                    with contextlib.suppress(ValueError):
                        _actual_keys.add((OrderSide(_o.side), _o.price))
            _has_restore_demand = bool(_desired_keys - _actual_keys)

            if _legal_cap is not None:
                if _legal_cap == 0 and _is_sym_cap and _has_restore_demand:
                    # Symbol cap blocked actual entry demand → count toward saturation
                    prev = self._risk_cap_consecutive_blocks.get(_sym, 0)
                    self._risk_cap_consecutive_blocks[_sym] = prev + 1
                    if (
                        self._risk_cap_consecutive_blocks[_sym] >= self._risk_saturation_threshold
                        and _sym not in self._risk_saturated_symbols
                    ):
                        self._risk_saturated_symbols.add(_sym)
                        logger.warning(
                            "GRID_V2_RISK_SATURATED_ENTER symbol=%s "
                            "consecutive_cap_blocks=%d threshold=%d "
                            "trigger=sync_proactive",
                            _sym,
                            self._risk_cap_consecutive_blocks[_sym],
                            self._risk_saturation_threshold,
                        )
                elif _legal_cap == 0:
                    # Zero headroom but either non-symbol-cap reason or no restore
                    # demand. Do NOT count toward saturation.
                    # Also clear saturation flag if reason changed away from sym cap.
                    if self._risk_cap_consecutive_blocks.get(_sym, 0) > 0:
                        self._risk_cap_consecutive_blocks[_sym] = 0
                    if not _is_sym_cap and _sym in self._risk_saturated_symbols:
                        self._risk_saturated_symbols.discard(_sym)
                        logger.info(
                            "GRID_V2_RISK_SATURATED_EXIT symbol=%s "
                            "reason=blocking_reason_changed_from_sym_cap",
                            _sym,
                        )
                else:
                    # Headroom exists — clear saturation proactively
                    if _sym in self._risk_saturated_symbols:
                        self._risk_saturated_symbols.discard(_sym)
                        logger.info(
                            "GRID_V2_RISK_SATURATED_EXIT symbol=%s "
                            "reason=sync_headroom_restored legal_cap=%d",
                            _sym,
                            _legal_cap,
                        )
                    if self._risk_cap_consecutive_blocks.get(_sym, 0) > 0:
                        self._risk_cap_consecutive_blocks[_sym] = 0

            # Build inflight state for both entry and exit reconciliation.
            # Filter by CID kind AND freshness (ADR-173 TTL contract):
            # - pending places: fresh if within STALE_GENS of current gen
            # - pending cancels: fresh if within STALE_MS or STALE_GENS
            # Stale inflight records are excluded so reconciler re-evaluates
            # from exchange truth instead of suppressing corrections forever.
            _bridge = self._grid_v2_bridge
            _gen = self._account_sync_generation + 1  # gen about to be set
            _now_ms = int(time.time() * 1000) if self._grid_v2_pending_cancels else 0

            _pending_exit_places: set[str] = set()
            _pending_entry_place_keys: set[Any] = set()
            for cid, dispatch_gen in self._grid_v2_pending_place_cids.items():
                if (_gen - dispatch_gen) >= _GRID_V2_PENDING_PLACE_STALE_GENS:
                    continue  # stale — let reconciler re-evaluate
                parsed = _bridge.adapter.parse_cid(cid)
                if parsed is None:
                    continue
                if parsed.kind.value == "EXIT":
                    _pending_exit_places.add(cid)
                elif parsed.kind.value == "ENTRY":
                    reg = _bridge.adapter.registry.lookup_entry(cid)
                    if reg is not None:
                        _pending_entry_place_keys.add((reg.side, reg.price))

            _pending_exit_cancels: set[str] = set()
            _pending_entry_cancel_keys: set[Any] = set()
            for cid, (dispatch_ts, dispatch_gen) in self._grid_v2_pending_cancels.items():
                age_ms = _now_ms - dispatch_ts if _now_ms > 0 else 0
                gen_delta = _gen - dispatch_gen
                if (
                    age_ms >= _GRID_V2_PENDING_CANCEL_STALE_MS
                    or gen_delta >= _GRID_V2_PENDING_CANCEL_STALE_GENS
                ):
                    continue  # stale — let reconciler re-evaluate
                parsed = _bridge.adapter.parse_cid(cid)
                if parsed is None:
                    continue
                if parsed.kind.value == "EXIT":
                    _pending_exit_cancels.add(cid)
                elif parsed.kind.value == "ENTRY":
                    reg = _bridge.adapter.registry.lookup_entry(cid)
                    if reg is None:
                        reg = _bridge.adapter.registry.lookup_stale_entry(cid)
                    if reg is not None:
                        _pending_entry_cancel_keys.add((reg.side, reg.price))

            recon = reconcile_grid_state(
                snapshot=result.snapshot,
                symbol=self._grid_v2_symbol,
                bridge=_bridge,
                max_actions=self._sync_reconciler_max_actions,
                risk_entry_capacity=_legal_cap,
                pending_exit_place_cids=frozenset(_pending_exit_places),
                pending_exit_cancel_cids=frozenset(_pending_exit_cancels),
                pending_entry_place_keys=frozenset(_pending_entry_place_keys),
                pending_entry_cancel_keys=frozenset(_pending_entry_cancel_keys),
            )
            has_diff = bool(
                recon.actions
                or recon.missing_entries
                or recon.extra_entries
                or recon.missing_exits
                or recon.extra_exits
            )
            is_primary = self._sync_reconciler_primary

            # ADR-106: Explicit no-action reason when sync produces zero actions.
            if not has_diff:
                from grinder.live.reason_codes import classify_no_action_reason  # noqa: PLC0415

                _no_action = classify_no_action_reason(
                    recon_has_diff=has_diff,
                    theoretical_entries=recon.theoretical_desired_entry_count,
                    effective_entries=recon.desired_entry_count,
                    actual_entries=recon.actual_entry_count,
                    is_risk_saturated=self._grid_v2_symbol in self._risk_saturated_symbols,
                    is_awaiting_sync=getattr(self, "_grid_v2_awaiting_sync", False),
                    is_started=self._grid_v2_started,
                    reconstruction_ok=self._grid_v2_bridge.reconstruction_ok
                    if self._grid_v2_bridge
                    else False,
                )
                if _no_action is not None:
                    # Throttle: only log non-healthy reasons every time,
                    # healthy steady-state every 100 syncs
                    from grinder.live.reason_codes import NoActionReason  # noqa: PLC0415

                    _is_healthy = _no_action == NoActionReason.ACTUAL_MATCHES_EFFECTIVE_TARGET
                    _ss_key = self._grid_v2_symbol
                    _ss_count = self._rolling_steady_state_count.get(_ss_key, 0) + 1
                    self._rolling_steady_state_count[_ss_key] = _ss_count
                    if not _is_healthy or _ss_count % 100 == 1:
                        logger.info(
                            "GRID_V2_NO_ACTION symbol=%s reason=%s "
                            "theoretical_entries=%d effective_entries=%d "
                            "actual_entries=%d projection=%s",
                            self._grid_v2_symbol,
                            _no_action.value,
                            recon.theoretical_desired_entry_count,
                            recon.desired_entry_count,
                            recon.actual_entry_count,
                            recon.projection_mode.value,
                        )
                    if _is_healthy:
                        pass  # reset on diff below
                else:
                    self._rolling_steady_state_count[self._grid_v2_symbol] = 0

            # ADR-106: Entry suppression reason when projection active
            if recon.desired_entry_count < recon.theoretical_desired_entry_count:
                from grinder.live.reason_codes import EntrySuppressionReason  # noqa: PLC0415

                if recon.desired_entry_count == 0:
                    _entry_reason = EntrySuppressionReason.EFFECTIVE_TARGET_ZERO
                else:
                    _entry_reason = EntrySuppressionReason.EFFECTIVE_TARGET_PARTIAL
                logger.info(
                    "GRID_V2_ENTRY_SUPPRESSED symbol=%s reason=%s "
                    "theoretical=%d effective=%d projection=%s capacity=%s",
                    self._grid_v2_symbol,
                    _entry_reason.value,
                    recon.theoretical_desired_entry_count,
                    recon.desired_entry_count,
                    recon.projection_mode.value,
                    recon.legal_entry_capacity,
                )

            if has_diff:
                self._rolling_steady_state_count[self._grid_v2_symbol] = 0
                logger.info(
                    "GRID_V2_SYNC_RECONCILER symbol=%s mode=%s "
                    "theoretical_entries=%d effective_entries=%d actual_entries=%d "
                    "missing=%d extra=%d "
                    "desired_exits=%d actual_exits=%d missing_exits=%d extra_exits=%d "
                    "would_cancel=%d would_place=%d cycle_ms=%d "
                    "projection=%s capacity=%s primary=%s "
                    "headroom=%s inflight_ep=%d inflight_ec=%d inflight_xp=%d inflight_xc=%d",
                    self._grid_v2_symbol,
                    self._grid_v2_bridge.state_machine.mode.value
                    if self._grid_v2_bridge.state_machine
                    else "?",
                    recon.theoretical_desired_entry_count,
                    recon.desired_entry_count,
                    recon.actual_entry_count,
                    recon.missing_entries,
                    recon.extra_entries,
                    recon.desired_exit_count,
                    recon.actual_exit_count,
                    recon.missing_exits,
                    recon.extra_exits,
                    recon.would_cancel,
                    recon.would_place,
                    recon.cycle_ms,
                    recon.projection_mode.value,
                    recon.legal_entry_capacity,
                    is_primary,
                    recon.inventory_headroom,
                    recon.inflight_entry_places,
                    recon.inflight_entry_cancels,
                    recon.inflight_exit_places,
                    recon.inflight_exit_cancels,
                )
            # PRIMARY MODE: stage actions for dispatch on next process_snapshot tick
            if is_primary and recon.actions:
                materialized = self._grid_v2_materialize_reconciler_actions(
                    recon.actions, result.snapshot.ts
                )
                # ADR-111 (revised): One-shot burst suppression.
                # Suppress PLACE_ENTRY for ONE reconciler cycle after fills
                # when snapshot hasn't caught up (snapshot_ts < last_fill_ts).
                # Uses a separate flag to prevent re-firing; does NOT zero
                # _last_fill_ts (which would break the stale-fill watermark).
                _snapshot_ts = result.snapshot.ts if result.snapshot else 0
                _snapshot_stale = (
                    self._last_fill_ts > 0
                    and _snapshot_ts < self._last_fill_ts
                    and not self._burst_suppression_fired
                )
                if _snapshot_stale:
                    suppressed = [a for a in materialized if a.action_type == ActionType.PLACE]
                    if suppressed:
                        materialized = [
                            a for a in materialized if a.action_type != ActionType.PLACE
                        ]
                        logger.info(
                            "GRID_V2_BURST_CHURN_SUPPRESSED symbol=%s "
                            "suppressed_places=%d snapshot_ts=%d last_fill_ts=%d "
                            "reason=ONE_CYCLE_POST_FILL",
                            self._grid_v2_symbol,
                            len(suppressed),
                            _snapshot_ts,
                            self._last_fill_ts,
                        )
                        for sa in suppressed:
                            if sa.client_order_id:
                                self._grid_v2_clean_failed_place(sa.client_order_id)
                    # One-shot flag: prevent re-firing until next fill
                    self._burst_suppression_fired = True
                elif _snapshot_ts >= self._last_fill_ts:
                    # Snapshot caught up — reset one-shot flag for next fill
                    self._burst_suppression_fired = False
                self._sync_reconciler_pending_actions = materialized
                # ADR-112: Record SM mode at staging for stale-mode drain filter
                self._reconciler_staged_mode = (
                    self._grid_v2_bridge.state_machine.mode.value
                    if self._grid_v2_bridge and self._grid_v2_bridge.state_machine
                    else ""
                )
                self._reconciler_staged_fill_ts = self._last_fill_ts
                if materialized:
                    logger.info(
                        "GRID_V2_SYNC_RECONCILER_DISPATCH_STAGED symbol=%s actions=%d "
                        "cancel=%d place=%d",
                        self._grid_v2_symbol,
                        len(materialized),
                        sum(1 for a in materialized if a.action_type == ActionType.CANCEL),
                        sum(1 for a in materialized if a.action_type == ActionType.PLACE),
                    )

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

    def _update_risk_base(self, asof_ts_ms: int) -> None:
        """Fetch balance from exchange and update risk base snapshot (PR-1 plumbing).

        Args:
            asof_ts_ms: Exchange timestamp from account snapshot (not wall-clock).
                Used as BalanceData.ts_ms so stale model measures exchange data age.

        Uses duck-type check for get_account_info() on the exchange port.
        If the port doesn't support it, logs once and returns.
        """
        port = self._exchange_port
        if not hasattr(port, "get_account_info"):
            return

        rb_metrics = get_risk_base_metrics()
        try:
            info = port.get_account_info()
            balance = BalanceData(
                total_margin_balance=info.margin_balance,
                wallet_balance=info.total_balance_usdt,
                available_balance=info.available_balance_usdt,
                ts_ms=asof_ts_ms,
            )
        except Exception:
            logger.warning("RISK_BASE_FETCH_FAILED", exc_info=True)
            rb_metrics.record_unavailable()
            self._risk_base_snapshot = None
            return

        now_ms = int(time.time() * 1000)
        snap = build_risk_base_snapshot(balance, self._risk_base_config, now_ms)

        if snap is None:
            rb_metrics.record_unavailable()
            self._risk_base_snapshot = None
            return

        self._risk_base_snapshot = snap
        rb_metrics.record_snapshot(
            value_usd=float(snap.value_usd),
            age_s=snap.age_s,
            is_stale_soft=snap.is_stale_soft,
            is_stale_hard=snap.is_stale_hard,
        )
        logger.info(
            "RISK_BASE_UPDATED mode=%s value_usd=%.2f age_s=%d "
            "stale_soft=%s stale_hard=%s below_min=%s",
            snap.mode,
            float(snap.value_usd),
            snap.age_s,
            snap.is_stale_soft,
            snap.is_stale_hard,
            snap.is_below_min,
        )

    def _evaluate_and_update_health_mode(self, _snapshot: Snapshot) -> None:
        """Evaluate truth-source health and update mode. Log transitions."""
        from grinder.live.health_gate import LiveHealthMode, evaluate_health  # noqa: PLC0415

        # Update health input signals from real connector state
        now = time.time()
        # WS liveness from connector stats (not assumed from snapshot arrival)
        ws_stats = (
            getattr(self._live_connector, "stats", None)
            if hasattr(self, "_live_connector")
            else None
        )
        if ws_stats is not None:
            self._health_input.ws_connected = getattr(ws_stats, "is_connected", True)
            last_msg = getattr(ws_stats, "last_message_ts", 0)
            if last_msg > 0:
                self._health_input.last_ws_message_ts = last_msg / 1000.0  # ms → s
        else:
            # No connector reference: use snapshot ts as fallback
            self._health_input.last_ws_message_ts = now

        result = evaluate_health(self._health_input, self._health_config, now)
        self._health_mode = result.mode

        # Log mode transitions
        if self._health_mode != self._health_mode_prev:
            logger.warning(
                "LIVE_HEALTH_MODE_CHANGED from=%s to=%s reason=%s "
                "write_allowed=%s reduce_only_allowed=%s",
                self._health_mode_prev.value,
                result.mode.value,
                result.reason,
                result.write_allowed,
                result.reduce_only_allowed,
            )
            # ADR-109 Phase 2 PR-3: degraded-mode recovery boundary.
            # Revoke ledger trust on any non-HEALTHY mode transition.
            # Trust can only be restored after health returns to HEALTHY
            # AND the next sync comparison converges.
            if result.mode != LiveHealthMode.HEALTHY:
                self._event_ledger.revoke_trust(f"health_mode={result.mode.value}")
            self._health_mode_prev = self._health_mode

    def _on_sync_success(self) -> None:
        """Update health input after successful account sync."""
        self._health_input.last_sync_success_ts = time.time()
        self._health_input.consecutive_sync_failures = 0

    def _on_sync_failure(self) -> None:
        """Update health input after failed account sync."""
        self._health_input.consecutive_sync_failures += 1

    def _on_clock_drift_error(self) -> None:
        """Update health input after -1021 clock drift error."""
        self._health_input.last_clock_drift_error_ts = time.time()
        self._health_input.clock_drift_errors_recent += 1

    def _on_dns_error(self) -> None:
        """Update health input after DNS/connectivity error."""
        self._health_input.last_dns_error_ts = time.time()
        self._health_input.dns_errors_recent += 1

    def _compute_risk_legal_entry_capacity(
        self,
        symbol: str,
        snapshot: object,
    ) -> tuple[int | None, bool]:
        """Compute how many additional entries are legally allowed by risk caps.

        Computes capacity as min across all active Gate 5.5 caps:
        symbol cap, portfolio gross cap, portfolio net cap.

        Returns:
            (capacity, is_symbol_cap_blocked) where:
            - capacity: int >= 0 (0=fully blocked, N=partial), None=unconstrained.
            - is_symbol_cap_blocked: True only when SYMBOL_CAP is the binding
              constraint. Used by caller to decide saturation counter increment.
        """
        if not self._risk_base_enabled:
            return None, False
        if self._risk_base_snapshot is None or snapshot is None:
            return 0, False  # fail-closed, not symbol-cap-specific
        from grinder.risk.portfolio_risk import (  # noqa: PLC0415
            RiskGateReason,
            compute_portfolio_notionals,
            compute_symbol_notional,
            evaluate_risk_gate,
        )

        # First check if risk gate blocks outright (stale, below min, DD, etc.)
        decision = evaluate_risk_gate(
            risk_base=self._risk_base_snapshot,
            snapshot=snapshot,  # type: ignore[arg-type]
            config=self._portfolio_risk_config,
            symbol=symbol,
        )
        if not decision.allowed:
            is_sym_cap = decision.reason == RiskGateReason.SYMBOL_CAP_EXCEEDED
            return 0, is_sym_cap

        # Gate passed — compute headroom per entry from each active cap
        base_usd = float(self._risk_base_snapshot.value_usd)
        per_entry = self._estimate_per_entry_notional()
        if per_entry is None or per_entry <= 0:
            return None, False  # can't estimate, unconstrained

        capacities: list[int] = []
        cfg = self._portfolio_risk_config

        # Symbol cap headroom
        if cfg.symbol_max_notional_pct > 0:
            sym_limit = base_usd * cfg.symbol_max_notional_pct
            sym_notional = float(
                compute_symbol_notional(snapshot, symbol)  # type: ignore[arg-type]
            )
            sym_headroom = sym_limit - sym_notional
            capacities.append(max(0, int(sym_headroom / per_entry)))

        # Portfolio gross cap headroom
        if cfg.portfolio_max_gross_notional_pct > 0 or cfg.portfolio_max_net_notional_pct > 0:
            gross, net = compute_portfolio_notionals(snapshot)  # type: ignore[arg-type]
            if cfg.portfolio_max_gross_notional_pct > 0:
                gross_limit = base_usd * cfg.portfolio_max_gross_notional_pct
                gross_headroom = gross_limit - float(gross)
                capacities.append(max(0, int(gross_headroom / per_entry)))
            if cfg.portfolio_max_net_notional_pct > 0:
                net_limit = base_usd * cfg.portfolio_max_net_notional_pct
                net_headroom = net_limit - float(net)
                capacities.append(max(0, int(net_headroom / per_entry)))

        if not capacities:
            return None, False  # no caps enabled, unconstrained

        min_cap = min(capacities)
        # is_symbol_cap_blocked: True only when symbol cap is the tightest and = 0
        is_sym_cap = False
        if min_cap == 0 and cfg.symbol_max_notional_pct > 0:
            sym_limit = base_usd * cfg.symbol_max_notional_pct
            sym_notional = float(
                compute_symbol_notional(snapshot, symbol)  # type: ignore[arg-type]
            )
            is_sym_cap = sym_notional >= sym_limit
        return min_cap, is_sym_cap

    def _estimate_per_entry_notional(self) -> float | None:
        """Estimate notional value of a single grid entry order."""
        if self._grid_v2_bridge is None or self._grid_v2_bridge.state_machine is None:
            return None
        order_size = float(self._grid_v2_bridge._config.order_size)
        ref_price = float(self._grid_v2_bridge.state_machine.snapshot.entry_window.reference_price)
        if order_size > 0 and ref_price > 0:
            return order_size * ref_price
        return None

    def _is_write_allowed_by_health(self, action: ExecutionAction) -> bool:
        """Check if action is allowed under current health mode.

        Returns True if allowed, False if blocked.
        """
        from grinder.live.health_gate import evaluate_health  # noqa: PLC0415

        result = evaluate_health(self._health_input, self._health_config)

        # CANCEL always allowed
        if action.action_type == ActionType.CANCEL:
            return True

        # Reduce-only allowed in some degraded modes
        if action.reduce_only and result.reduce_only_allowed:
            return True

        # Normal writes
        return result.write_allowed

    def _load_min_notional_cache(self) -> None:
        """Load min_notional per symbol from constraint provider cache.

        Fail-open: if constraints unavailable, cache stays empty (no blocking).
        """
        try:
            from grinder.execution.constraint_provider import (  # noqa: PLC0415
                ConstraintProvider,
                ConstraintProviderConfig,
            )

            provider = ConstraintProvider(config=ConstraintProviderConfig(allow_fetch=False))
            constraints = provider.get_constraints()
            if constraints:
                for sym, sc in constraints.items():
                    if sc.min_notional > 0:
                        self._min_notional_cache[sym] = sc.min_notional
                    if sc.tick_size > 0:
                        self._tick_size_cache[sym] = sc.tick_size
                    if sc.step_size > 0:
                        self._step_size_cache[sym] = sc.step_size
                logger.info("MIN_NOTIONAL_CACHE_LOADED symbols=%d", len(self._min_notional_cache))
        except Exception as e:
            logger.warning("MIN_NOTIONAL_CACHE_FAILED error=%s", e)

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

    def _get_abs_position_qty(self, symbol: str) -> Decimal | None:
        """Get effective position qty for reduce-only budget guard.

        Base = exchange snapshot (SSOT, refreshes every ~5s).
        Addition = provable current-tick lot creations from fill path.
        Result = snapshot_qty + batch_new_lots_qty (fail-closed).

        Does NOT use generic SM open_lots as override for exchange truth.
        """
        snap = self._last_account_snapshot
        snap_qty = Decimal(0)
        if snap is not None:
            for p in snap.positions:
                if p.symbol == symbol:
                    snap_qty += abs(p.qty)

        # Add provable current-tick lot additions (fill-derived only)
        batch_additions = self._reduce_only_batch_new_lots_qty.get(symbol, Decimal(0))
        effective = snap_qty + batch_additions
        return effective if effective > 0 else None

    def _get_open_reduce_only_qty(self, symbol: str, side: OrderSide | None) -> Decimal:
        """Sum qty of open reduce-only orders actually on exchange.

        Uses account snapshot (what Binance sees) + partial fill accounting.
        SM exits are what we're TRYING to place — don't count them as existing.
        """
        snap = self._last_account_snapshot
        if snap is None or side is None:
            return Decimal(0)
        total = Decimal(0)
        for o in snap.open_orders:
            if o.symbol == symbol and o.side == side.value and o.reduce_only:
                total += o.qty - o.filled_qty
        return total

    def _reduce_only_repair_on_sync(
        self,
        snapshot: object,
    ) -> None:
        """Detect and cancel surplus reduce-only exits on sync (ADR-104).

        Runs before reconciler to converge illegal exit topology.
        Cancels smallest surplus exits first to minimize disruption.
        """
        from grinder.live.reduce_only_budget import (  # noqa: PLC0415
            RepairReason,
            detect_surplus_exits,
        )

        sym = self._grid_v2_symbol
        for side in (OrderSide.BUY, OrderSide.SELL):
            surplus = detect_surplus_exits(snapshot, sym, side)  # type: ignore[arg-type]
            repair_key = (sym, side.value)

            if not surplus:
                # No surplus on this side — if previously flagged, verify legal
                # and clear flag only when topology is confirmed clean.
                if repair_key in self._reduce_only_pending_repair:
                    self._reduce_only_pending_repair.discard(repair_key)
                    logger.info(
                        "GRID_V2_REDUCE_ONLY_REPAIR_CONVERGED symbol=%s side=%s "
                        "reason=topology_now_legal",
                        sym,
                        side.value,
                    )
                continue

            logger.warning(
                "GRID_V2_REDUCE_ONLY_REPAIR_START symbol=%s side=%s surplus_count=%d reason=%s",
                sym,
                side.value,
                len(surplus),
                RepairReason.SYNC_OVER_BUDGET.value,
            )
            all_ok = True
            for repair in surplus:
                cancel_action = ExecutionAction(
                    action_type=ActionType.CANCEL,
                    order_id=repair.order_id,
                    symbol=sym,
                    reason="GRID_V2_REDUCE_ONLY_REPAIR_CANCEL",
                )
                ts = int(time.time() * 1000)
                result = self._process_action(cancel_action, ts)
                if result.status == LiveActionStatus.EXECUTED:
                    logger.info(
                        "GRID_V2_REDUCE_ONLY_REPAIR_CANCEL symbol=%s order_id=%s remaining_qty=%s",
                        sym,
                        repair.order_id,
                        repair.remaining_qty,
                    )
                else:
                    all_ok = False
                    logger.warning(
                        "GRID_V2_REDUCE_ONLY_REPAIR_CANCEL_FAILED symbol=%s order_id=%s status=%s",
                        sym,
                        repair.order_id,
                        result.status.value,
                    )

            if all_ok:
                # All cancels succeeded — topology should now be legal.
                # Clear flag so exits can resume.
                self._reduce_only_pending_repair.discard(repair_key)
                logger.info(
                    "GRID_V2_REDUCE_ONLY_REPAIR_CONVERGED symbol=%s side=%s cancelled=%d",
                    sym,
                    side.value,
                    len(surplus),
                )
            else:
                # Some cancels failed — keep flag set, retry next sync.
                # Ensure flag is set even if it wasn't before (sync-detected).
                self._reduce_only_pending_repair.add(repair_key)
                logger.warning(
                    "GRID_V2_REDUCE_ONLY_REPAIR_DEFERRED symbol=%s side=%s "
                    "reason=cancel_failed surplus_remaining=%d",
                    sym,
                    side.value,
                    len(surplus),
                )

    def _exit_topology_repair_on_sync(self, snapshot: object) -> None:  # noqa: PLR0912, PLR0915
        """Compute and execute exit topology repair (ADR-105).

        Compares desired legal exit topology (from SM + budget) against
        actual exchange exits. Cancels extras, dispatches missing if legal,
        logs deferred placements.
        """
        from grinder.grid_v2.exit_repair import (  # noqa: PLC0415
            RepairTrigger,
            compute_desired_exits,
            compute_exit_topology_repair,
        )
        from grinder.live.reduce_only_budget import (  # noqa: PLC0415
            _closeable_qty_for_side,
        )

        sym = self._grid_v2_symbol
        bridge = self._grid_v2_bridge
        assert bridge is not None  # guarded by caller
        sm = bridge.state_machine
        assert sm is not None  # guarded by caller

        # Compute desired legal exits — per-side budgeting.
        # Split SM exits by side, budget each against its own closeable qty.
        from grinder.grid_v2.state import ExitOrderStatus as _EOS  # noqa: PLC0415

        sell_exits = [
            eo
            for eo in sm.snapshot.exit_orders
            if eo.status == _EOS.OPEN and eo.side == OrderSide.SELL
        ]
        buy_exits = [
            eo
            for eo in sm.snapshot.exit_orders
            if eo.status == _EOS.OPEN and eo.side == OrderSide.BUY
        ]
        sell_budget = (
            _closeable_qty_for_side(
                snapshot,  # type: ignore[arg-type]
                sym,
                OrderSide.SELL,
            )
            if sell_exits
            else None
        )
        buy_budget = (
            _closeable_qty_for_side(
                snapshot,  # type: ignore[arg-type]
                sym,
                OrderSide.BUY,
            )
            if buy_exits
            else None
        )

        # Compute actual exit CIDs from exchange (needed before desired-exit
        # computation so budget allocation prioritizes already-placed exits).
        actual_exit_cids: set[str] = set()
        for o in snapshot.open_orders:  # type: ignore[attr-defined]
            if o.symbol != sym:
                continue
            parsed = bridge.adapter.parse_cid(o.order_id)
            if parsed is not None and parsed.kind.value == "EXIT":
                actual_exit_cids.add(o.order_id)

        frozen_actual = frozenset(actual_exit_cids)
        desired_sell = compute_desired_exits(
            sell_exits,
            bridge.adapter.registry.cid_for_exit,
            sell_budget,
            actual_exit_cids=frozen_actual,
        )
        desired_buy = compute_desired_exits(
            buy_exits,
            bridge.adapter.registry.cid_for_exit,
            buy_budget,
            actual_exit_cids=frozen_actual,
        )
        desired = desired_sell + desired_buy

        # Determine trigger
        trigger = RepairTrigger.SYNC_DRIFT
        if (sym, "BUY") in self._reduce_only_pending_repair or (
            sym,
            "SELL",
        ) in self._reduce_only_pending_repair:
            trigger = RepairTrigger.REJECT_RECOVERY

        result = compute_exit_topology_repair(desired, actual_exit_cids, trigger)

        if result.is_converged:
            return  # Nothing to do

        logger.info(
            "GRID_V2_EXIT_TOPOLOGY_REPAIR_START symbol=%s trigger=%s "
            "desired=%d actual=%d extra=%d missing=%d deferred=%d",
            sym,
            result.trigger.value,
            result.desired_exit_count,
            result.actual_exit_count,
            result.extra_count,
            result.missing_count,
            result.deferred_count,
        )

        all_ok = True
        for action in result.actions:
            if action.action_type == "CANCEL" and action.cid:
                cancel = ExecutionAction(
                    action_type=ActionType.CANCEL,
                    order_id=action.cid,
                    symbol=sym,
                    reason="GRID_V2_EXIT_TOPOLOGY_REPAIR_CANCEL",
                )
                ts = int(time.time() * 1000)
                r = self._process_action(cancel, ts)
                if r.status != LiveActionStatus.EXECUTED:
                    all_ok = False
            elif action.action_type == "PLACE" and action.cid:
                # Suppress idempotent re-place when exit is already in-flight
                # (placed by fill path but not yet visible in snapshot).
                if action.cid in self._grid_v2_pending_place_cids:
                    logger.info(
                        "GRID_V2_EXIT_TOPOLOGY_REPAIR_PLACE_SUPPRESSED "
                        "symbol=%s cid=%s reason=in_flight",
                        sym,
                        action.cid,
                    )
                    continue
                # ADR-114: Quantize repair price to exchange tick size
                _repair_price = (
                    bridge._quantize_price(action.price, action.side)
                    if bridge and action.price
                    else action.price
                )
                place = ExecutionAction(
                    action_type=ActionType.PLACE,
                    symbol=sym,
                    side=action.side,
                    price=_repair_price,
                    quantity=action.qty,
                    client_order_id=action.cid,
                    reduce_only=True,
                    reason="GRID_V2_EXIT_TOPOLOGY_REPAIR_PLACE",
                )
                ts = int(time.time() * 1000)
                r = self._process_action(place, ts)
                if r.status != LiveActionStatus.EXECUTED:
                    all_ok = False
            elif action.action_type == "DEFERRED":
                # ADR-113: Re-register and place deferred exits instead of
                # infinite DEFERRED loop. The lot exists in SM but the exit
                # registry entry was lost (e.g., after -2022 cleanup).
                if (
                    action.exit_order_id
                    and action.lot_id
                    and action.side
                    and action.price
                    and action.qty
                    and bridge is not None
                ):
                    try:
                        new_cid = bridge.adapter.generate_exit_cid(int(time.time() * 1000))
                        bridge.adapter.registry.register_exit(
                            new_cid, action.exit_order_id, action.lot_id
                        )
                        # ADR-114: Quantize re-register price to tick size
                        _rereg_price = bridge._quantize_price(action.price, action.side)
                        place = ExecutionAction(
                            action_type=ActionType.PLACE,
                            symbol=sym,
                            side=action.side,
                            price=_rereg_price,
                            quantity=action.qty,
                            client_order_id=new_cid,
                            reduce_only=True,
                            reason="GRID_V2_EXIT_TOPOLOGY_REPAIR_REREGISTER",
                        )
                        ts = int(time.time() * 1000)
                        r = self._process_action(place, ts)
                        if r.status != LiveActionStatus.EXECUTED:
                            all_ok = False
                        logger.info(
                            "GRID_V2_EXIT_TOPOLOGY_REPAIR_REREGISTERED symbol=%s "
                            "exit_order_id=%s lot_id=%s new_cid=%s status=%s",
                            sym,
                            action.exit_order_id,
                            action.lot_id,
                            new_cid,
                            r.status.value,
                        )
                    except Exception:
                        all_ok = False
                        logger.warning(
                            "GRID_V2_EXIT_TOPOLOGY_REPAIR_REREGISTER_FAILED "
                            "symbol=%s exit_order_id=%s lot_id=%s",
                            sym,
                            action.exit_order_id,
                            action.lot_id,
                            exc_info=True,
                        )
                else:
                    all_ok = False
                    logger.info(
                        "GRID_V2_EXIT_TOPOLOGY_REPAIR_DEFERRED symbol=%s "
                        "exit_order_id=%s lot_id=%s reason=insufficient_info",
                        sym,
                        action.exit_order_id,
                        action.lot_id,
                    )

        if all_ok:
            logger.info(
                "GRID_V2_EXIT_TOPOLOGY_REPAIR_CONVERGED symbol=%s cancels=%d places=%d deferred=%d",
                sym,
                result.extra_count,
                result.missing_count,
                result.deferred_count,
            )
        else:
            logger.warning(
                "GRID_V2_EXIT_TOPOLOGY_REPAIR_INCOMPLETE symbol=%s "
                "reason=action_failed_or_deferred",
                sym,
            )

    def _on_reduce_only_reject(self, symbol: str, side: str, error_code: int) -> None:
        """Handle -2022 ReduceOnly reject from exchange (ADR-104).

        Flags (symbol, side) for repair. Further reduce-only exits for
        that direction are blocked until sync-time repair clears the flag.
        """
        key = (symbol, side)
        if error_code == -2022 and key not in self._reduce_only_pending_repair:
            self._reduce_only_pending_repair.add(key)
            logger.warning(
                "GRID_V2_REDUCE_ONLY_REPAIR_TRIGGERED symbol=%s side=%s "
                "reason=EXCHANGE_REJECT_2022 "
                "action=blocking_further_exits_until_sync_repair",
                symbol,
                side,
            )

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

    def _get_signed_position_qty_from_snapshot(self, symbol: str) -> Decimal:
        """Get signed position quantity from REST snapshot only.

        Returns:
            Positive for LONG, negative for SHORT, zero if flat/no snapshot.
            Uses signed_qty from PositionSnap (original positionAmt from exchange).
            Falls back to positive qty if signed_qty not available.
        """
        snap = self._last_account_snapshot
        if snap is None:
            return Decimal("0")
        for p in snap.positions:
            if p.symbol == symbol:
                if p.signed_qty is not None:
                    return p.signed_qty
                return p.qty  # fallback: positive (legacy PositionSnap without signed_qty)
        return Decimal("0")

    def _get_signed_position_qty(self, symbol: str) -> Decimal:
        """Get signed position quantity for symbol (Phase 3 switched boundary).

        Delegates to trusted PositionLedger when fresh and converged,
        falls back to REST snapshot otherwise.
        """
        return self.get_effective_signed_position_qty(symbol)

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

    def _maybe_track_lot_closure(self, symbol: str, result: FillResult) -> None:
        """Check fill result for lot closure and update consecutive loss tracker.

        Detects newly closed lots by comparing closed_lots count before/after.
        The SM appends closed lots to the tuple, so the last N are new.
        """
        if not self._symbol_risk_manager.config.enabled:
            return
        if result.transition is None:
            return
        snap = result.transition.snapshot
        prev_count = self._symbol_closed_lots_seen.get(symbol, 0)
        current_count = len(snap.closed_lots)
        if current_count > prev_count:
            for lot in snap.closed_lots[prev_count:]:
                self._record_grid_v2_lot_closed(
                    symbol,
                    lot.entry_price,
                    lot.exit_price,
                    lot.side.value,
                )
        self._symbol_closed_lots_seen[symbol] = current_count

    def _evaluate_symbol_risk(  # noqa: PLR0912
        self, acct: AccountSnapshot
    ) -> None:
        """Evaluate per-symbol risk after account sync."""
        cfg = self._symbol_risk_manager.config
        if cfg.applies_to_grid_v2_only and not self._grid_v2_enabled:
            return

        # Build position lookup from account snapshot
        pos_by_sym: dict[str, tuple[float, float]] = {}
        for pos in acct.positions:
            sym = pos.symbol
            if cfg.applies_to_grid_v2_only and sym != self._grid_v2_symbol:
                continue
            pos_by_sym[sym] = (float(abs(pos.qty) * pos.mark_price), float(abs(pos.qty)))

        # Evaluate all tracked symbols (including those with zero position)
        # to allow de-escalation when position closes
        tracked = set(pos_by_sym.keys()) | set(self._symbol_risk_manager.tracked_symbols())
        if cfg.applies_to_grid_v2_only and self._grid_v2_symbol:
            tracked.add(self._grid_v2_symbol)

        for sym in tracked:
            notional, qty = pos_by_sym.get(sym, (0.0, 0.0))
            losses = self._symbol_consecutive_losses.get(sym, 0)
            snap = SymbolRiskSnapshot(
                position_notional_usd=notional,
                position_qty=qty,
                consecutive_losses=losses,
            )
            decision = self._symbol_risk_manager.evaluate(sym, snap)
            # Activate/deactivate unload based on risk state transitions
            if decision.escalation_reason and decision.state == SymbolRiskState.EXIT_ONLY:
                self._symbol_unload.activate(sym)
            if decision.deescalation_reason and decision.state != SymbolRiskState.EXIT_ONLY:
                self._symbol_unload.clear(sym)

            # DD ladder via portfolio risk config: activate unload automatically
            # when symbol drawdown reaches unload/forced-flat thresholds.
            if self._risk_base_enabled:
                dd_decision = evaluate_risk_gate(
                    risk_base=self._risk_base_snapshot,
                    snapshot=acct,
                    config=self._portfolio_risk_config,
                    symbol=sym,
                )
                if dd_decision.reason in (
                    RiskGateReason.SYMBOL_DD_UNLOAD,
                    RiskGateReason.SYMBOL_DD_FORCED_FLAT,
                    RiskGateReason.PORTFOLIO_DD_FORCE_REDUCE,
                    RiskGateReason.PORTFOLIO_DD_KILL_SWITCH,
                ):
                    self._symbol_unload.activate(sym)

        # Force-reduce: cancel grid exits to free reduce-only budget, then unload.
        # Sequence: cancel → verify gone from snapshot → activate unload.
        if self._force_reduce_requested:
            for sym in tracked:
                _notional, _qty = pos_by_sym.get(sym, (0.0, 0.0))
                if _qty <= 0:
                    continue

                if not self._force_reduce_exits_cleared:
                    # Check if grid exits still present in snapshot
                    remaining_grid_exits = self._count_grid_exits(sym, acct)

                    if remaining_grid_exits > 0:
                        # Send cancels (retry each cycle until gone)
                        cancelled = self._cancel_grid_exits_for_force_reduce(sym, acct)
                        logger.info(
                            "FORCE_REDUCE_EXIT_ORDERS_WAITING symbol=%s remaining=%d cancelled=%d",
                            sym,
                            remaining_grid_exits,
                            cancelled,
                        )
                        continue  # don't unload until exits confirmed gone

                    # All grid exits gone from snapshot — budget is free
                    logger.info("FORCE_REDUCE_EXIT_ORDERS_CLEARED symbol=%s", sym)
                    self._force_reduce_exits_cleared = True

                # Activate unload after exits confirmed cleared
                if self._symbol_unload.get_status(sym).value == "INACTIVE":
                    self._symbol_unload.activate(sym, force=True)
                    logger.info("ENGINE_FORCE_REDUCE_UNLOAD_STARTED symbol=%s", sym)

        # Try unload steps for active symbols (force-reduce overrides enabled gate)
        if self._symbol_unload.config.enabled or self._force_reduce_requested:
            self._try_symbol_unload_steps(acct)

        # Force-reduce completion: check if all positions are flat
        if self._force_reduce_requested:
            all_flat = all(pos_by_sym.get(sym, (0.0, 0.0))[1] == 0.0 for sym in tracked)
            if all_flat and not self._force_reduce_flat_logged:
                logger.info(
                    "ENGINE_FORCE_REDUCE_UNLOAD_FLAT symbols=%s",
                    ",".join(sorted(tracked)),
                )
                self._force_reduce_flat_logged = True

    def _try_symbol_unload_steps(self, acct: AccountSnapshot) -> None:
        """Attempt staged unload steps for symbols in EXIT_ONLY."""
        signed_by_sym: dict[str, float] = {}
        mark_by_sym: dict[str, Decimal] = {}
        for pos in acct.positions:
            sq = float(pos.signed_qty) if pos.signed_qty is not None else float(pos.qty)
            signed_by_sym[pos.symbol] = sq
            mark_by_sym[pos.symbol] = pos.mark_price

        for sym in list(self._symbol_unload.tracked_symbols()):
            if self._symbol_unload.get_status(sym).value not in ("ACTIVE",):
                continue
            signed_qty = signed_by_sym.get(sym, 0.0)
            step = self._symbol_unload.try_step(sym, signed_qty, force=self._force_reduce_requested)
            if step is None:
                continue
            mark = mark_by_sym.get(sym)
            if mark is None or mark <= 0:
                logger.warning(
                    "SYMBOL_UNLOAD_STEP_SKIPPED symbol=%s reason=no_mark_price",
                    sym,
                )
                continue
            # Quantize price and qty to exchange constraints.
            # Fail-closed: skip if constraints unavailable — do not guess.
            side = OrderSide.BUY if step.side == "BUY" else OrderSide.SELL
            tick = self._tick_size_cache.get(sym)
            lot = self._step_size_cache.get(sym)
            if not tick or tick <= 0 or not lot or lot <= 0:
                logger.warning(
                    "SYMBOL_UNLOAD_STEP_SKIPPED symbol=%s reason=constraints_unavailable"
                    " tick=%s lot=%s",
                    sym,
                    tick,
                    lot,
                )
                continue
            # Price: BUY rounds UP (aggressive), SELL rounds DOWN (aggressive)
            if side == OrderSide.BUY:
                price = ((mark / tick).to_integral_value(rounding=ROUND_UP)) * tick
            else:
                price = ((mark / tick).to_integral_value(rounding=ROUND_DOWN)) * tick
            qty = ((Decimal(str(step.qty)) / lot).to_integral_value(rounding=ROUND_DOWN)) * lot
            if qty <= 0:
                logger.warning(
                    "SYMBOL_UNLOAD_STEP_SKIPPED symbol=%s reason=qty_rounds_to_zero"
                    " raw_qty=%s lot=%s",
                    sym,
                    step.qty,
                    lot,
                )
                continue
            action = ExecutionAction(
                action_type=ActionType.PLACE,
                symbol=step.symbol,
                side=side,
                price=price,
                quantity=qty,
                reduce_only=True,
                reason="SYMBOL_UNLOAD_STEP",
            )
            ts = int(time.time() * 1000)
            result = self._process_action(action, ts)
            if result.status == LiveActionStatus.EXECUTED:
                self._symbol_unload.record_step_success(sym)
                logger.info(
                    "SYMBOL_UNLOAD_STEP_EXECUTED symbol=%s side=%s qty=%.6f",
                    sym,
                    step.side,
                    step.qty,
                )
            elif result.status == LiveActionStatus.FAILED:
                self._symbol_unload.record_step_failure(sym)
                logger.warning(
                    "SYMBOL_UNLOAD_STEP_FAILED symbol=%s side=%s qty=%.6f reason=%s",
                    sym,
                    step.side,
                    step.qty,
                    result.block_reason.value if result.block_reason else "EXECUTION_ERROR",
                )

    def _count_grid_exits(self, symbol: str, acct: AccountSnapshot) -> int:
        """Count grid-owned reduce-only exit orders still on exchange."""
        grid_exit_ids = self._get_grid_exit_order_ids()
        count = 0
        for order in acct.open_orders:
            if (
                order.symbol == symbol
                and order.reduce_only
                and order.order_id in grid_exit_ids
                and order.qty - order.filled_qty > 0
            ):
                count += 1
        return count

    def _get_grid_exit_order_ids(self) -> set[str]:
        """Get exchange order IDs of all grid-owned exit orders."""
        bridge = self._grid_v2_bridge
        if bridge is None or bridge.adapter is None:
            return set()
        registry = bridge.adapter.registry
        ids: set[str] = set()
        for cid in registry.all_exit_cids:
            reg = registry.lookup_exit(cid)
            if reg is not None:
                ids.add(reg.exit_order_id)
        return ids

    def _cancel_grid_exits_for_force_reduce(self, symbol: str, acct: AccountSnapshot) -> int:
        """Cancel grid-owned reduce-only exit orders to free budget for unload.

        Only cancels orders that are:
        - for the given symbol
        - reduce_only
        - owned by the grid v2 adapter (matched by exchange order_id)

        Does NOT cancel unrelated/manual/external reduce-only orders.
        Returns count of cancel actions dispatched.
        """
        grid_exit_ids = self._get_grid_exit_order_ids()

        cancelled = 0
        ts = int(time.time() * 1000)
        for order in acct.open_orders:
            if order.symbol != symbol or not order.reduce_only:
                continue
            if order.order_id not in grid_exit_ids:
                continue
            remaining = order.qty - order.filled_qty
            if remaining <= 0:
                continue
            cancel_action = ExecutionAction(
                action_type=ActionType.CANCEL,
                symbol=order.symbol,
                order_id=order.order_id,
                reason="FORCE_REDUCE_BUDGET_CLEAR",
            )
            result = self._process_action(cancel_action, ts)
            if result.status == LiveActionStatus.EXECUTED:
                cancelled += 1
        return cancelled

    def _record_grid_v2_lot_closed(
        self, symbol: str, entry_price: Decimal, exit_price: Decimal, side: str
    ) -> None:
        """Track lot close for consecutive loss counting."""
        is_loss = (side == "LONG" and exit_price < entry_price) or (
            side == "SHORT" and exit_price > entry_price
        )
        if is_loss:
            self._symbol_consecutive_losses[symbol] = (
                self._symbol_consecutive_losses.get(symbol, 0) + 1
            )
        else:
            self._symbol_consecutive_losses[symbol] = 0

    def _filter_cancel_guards(
        self, cancel_actions: list[ExecutionAction]
    ) -> tuple[list[ExecutionAction], list[tuple[int, LiveAction]]]:
        """Apply serial cancel guards (duplicate/failed suppression)."""
        filtered: list[ExecutionAction] = []
        skipped: list[tuple[int, LiveAction]] = []
        for i, action in enumerate(cancel_actions):
            oid = action.order_id
            if oid and (
                oid in self._cancel_failed_ids or oid in self._cancel_dispatched_pending_sync
            ):
                skipped.append(
                    (
                        i,
                        LiveAction(
                            action=action,
                            status=LiveActionStatus.SKIPPED,
                            block_reason=BlockReason.CANCEL_ALREADY_FAILED,
                            intent=RiskIntent.CANCEL,
                        ),
                    )
                )
            else:
                filtered.append(action)
        return filtered, skipped

    def _dispatch_cancel_wave(
        self,
        cancel_actions: list[ExecutionAction],
        ts: int,
    ) -> list[LiveAction]:
        """Dispatch post-fill cancel wave with bounded concurrency.

        Same pattern as seed dispatch:
        1. Concurrent HTTP submit via _submit_to_exchange (thread-safe)
        2. Serial apply via _apply_submit_outcome in original order

        Only for grid_v2 post-fill opposite-side cancels.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

        _CANCEL_CONCURRENCY = 3
        results: list[LiveAction] = []

        if not cancel_actions:
            return results

        # Serial pre-submit guards: skip duplicates and already-failed cancels
        filtered, skipped_results = self._filter_cancel_guards(cancel_actions)

        # Classify intent for submit-eligible cancels
        intents: list[Any] = []
        for action in filtered:
            pos_sign = self._get_position_sign(action.symbol) if action.symbol else None
            intents.append(classify_intent(action, pos_sign))

        # Concurrent HTTP submit (filtered only)
        outcomes: list[SubmitOutcome | None] = [None] * len(filtered)
        if len(filtered) == 1:
            outcomes[0] = self._submit_to_exchange(filtered[0], ts)
        elif filtered:
            with ThreadPoolExecutor(max_workers=_CANCEL_CONCURRENCY) as pool:
                future_to_idx = {
                    pool.submit(self._submit_to_exchange, action, ts): idx
                    for idx, action in enumerate(filtered)
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        outcomes[idx] = future.result()
                    except Exception as e:
                        outcomes[idx] = SubmitOutcome(error=str(e), attempts=1)

        # Serial apply in original order (filtered cancels)
        for i, action in enumerate(filtered):
            outcome = outcomes[i]
            assert outcome is not None
            live_action = self._apply_submit_outcome(action, outcome, intents[i])
            results.append(live_action)

            # Post-cancel bookkeeping (same as serial loop)
            if (
                action.action_type == ActionType.CANCEL
                and action.order_id is not None
                and live_action.status == LiveActionStatus.FAILED
                and not self._grid_v2_handle_failed_cancel(
                    action.order_id, live_action.exchange_code
                )
            ):
                self._cancel_failed_ids.add(action.order_id)

            if (
                action.action_type == ActionType.CANCEL
                and action.order_id is not None
                and live_action.status in (LiveActionStatus.EXECUTED, LiveActionStatus.FAILED)
            ):
                self._cancel_dispatched_pending_sync.add(action.order_id)

        # Include pre-filtered skipped results
        for _idx, skipped_action in skipped_results:
            results.append(skipped_action)

        return results

    def _dispatch_grid_v2_seed_batch(
        self,
        seed_actions: list[ExecutionAction],
        ts: int,
    ) -> SeedDispatchResult:
        """Dispatch seed PLACEs and apply all post-dispatch bookkeeping.

        Dispatches each seed action through the full gate chain via
        _process_action, then applies pending-place tracking, failed-place
        cleanup, and seed CID clearing — all in one isolated helper.

        This method owns all seed-specific state mutation.

        Three phases:
        A. Serial gate check + serial HTTP submit via _process_action
           (all state mutation in main thread, exactly as before).
        B. Serial bookkeeping via _apply_seed_bookkeeping.

        HTTP concurrency is achieved by submitting all seeds through
        _submit_to_exchange concurrently, bypassing _process_action for
        the HTTP portion only. Gate checks run in _process_action as
        before; _submit_to_exchange is the thread-safe seam.

        Three phases:
        A. Serial gate check via _process_action in gate-only mode
           (no HTTP, records gate-passed actions with their intents)
        B. Concurrent HTTP submit via _submit_to_exchange (thread-safe)
        C. Serial apply via _apply_submit_outcome + seed bookkeeping
        """

        from grinder.observability.latency_telemetry import (  # noqa: PLC0415
            PhaseTimer,
            log_seed_dispatch,
        )

        _SEED_HTTP_CONCURRENCY = 3
        timer = PhaseTimer()
        result = SeedDispatchResult(live_actions=[])

        if not seed_actions:
            log_seed_dispatch(self._grid_v2_symbol or "?", 0, 0)
            return result

        # Phase A: serial gate check (no HTTP — gate-only mode)
        gate_results: list[Any] = [None] * len(seed_actions)
        self._seed_gate_only_mode = True
        self._seed_gate_passed_intents = []
        try:
            for i, action in enumerate(seed_actions):
                gate_results[i] = self._process_action(action, ts)
        finally:
            self._seed_gate_only_mode = False

        # Build submit queue from gate-passed actions
        gate_passed = list(self._seed_gate_passed_intents)
        self._seed_gate_passed_intents = []
        gate_passed_set = {id(a) for a, _intent in gate_passed}

        submit_actions: list[tuple[int, ExecutionAction, Any]] = []
        for i, action in enumerate(seed_actions):
            if id(action) not in gate_passed_set:
                continue
            intent = next(
                (intent for a, intent in gate_passed if id(a) == id(action)),
                RiskIntent.CANCEL,
            )
            # Pre-send gates (min_notional, NOOP) — same as _execute_action
            pre_send_block = self._check_pre_send_gates(action, intent)
            if pre_send_block is not None:
                gate_results[i] = pre_send_block
            else:
                submit_actions.append((i, action, intent))

        # Phase B: concurrent HTTP submit (thread-safe)
        outcomes = self._submit_seeds_concurrent(submit_actions, ts, _SEED_HTTP_CONCURRENCY)

        # Phase C: serial apply + bookkeeping in original order
        for i, action in enumerate(seed_actions):
            if i in outcomes:
                # Gate-passed: apply real HTTP outcome
                intent = next(
                    (intent for idx, _a, intent in submit_actions if idx == i),
                    RiskIntent.CANCEL,
                )
                live_action = self._apply_submit_outcome(action, outcomes[i], intent)
            else:
                # Gate-blocked: use the gate result directly
                live_action = gate_results[i]
            result.live_actions.append(live_action)
            self._apply_seed_bookkeeping(action, live_action, result)

        symbol = self._grid_v2_symbol or "?"
        log_seed_dispatch(symbol, timer.elapsed_ms(), len(seed_actions))
        logger.info(
            "SEED_DISPATCH_RESULT symbol=%s count=%d executed=%d failed=%d blocked=%d ms=%d",
            symbol,
            len(seed_actions),
            result.executed_count,
            result.failed_count,
            result.blocked_count,
            timer.elapsed_ms(),
        )
        return result

    def _submit_seeds_concurrent(
        self,
        submit_actions: list[tuple[int, ExecutionAction, Any]],
        ts: int,
        concurrency: int,
    ) -> dict[int, SubmitOutcome]:
        """Concurrent HTTP submit for gate-passed seeds. Thread-safe."""
        from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

        outcomes: dict[int, SubmitOutcome] = {}
        if not submit_actions:
            return outcomes
        if len(submit_actions) == 1:
            idx, action, _intent = submit_actions[0]
            outcomes[idx] = self._submit_to_exchange(action, ts)
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                future_to_idx = {
                    pool.submit(self._submit_to_exchange, action, ts): idx
                    for idx, action, _intent in submit_actions
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        outcomes[idx] = future.result()
                    except Exception as e:
                        outcomes[idx] = SubmitOutcome(error=str(e), attempts=1)
        logger.info(
            "SEED_HTTP_CONCURRENCY symbol=%s window=%d queued=%d",
            self._grid_v2_symbol or "?",
            min(concurrency, len(submit_actions)),
            len(submit_actions),
        )
        return outcomes

    def _apply_seed_bookkeeping(
        self, action: ExecutionAction, live_action: Any, result: SeedDispatchResult
    ) -> None:
        """Apply post-dispatch bookkeeping for one seed action (serial, safe)."""
        if action.action_type != ActionType.PLACE or action.client_order_id is None:
            return

        cid = action.client_order_id

        # Pending-place tracking
        if live_action.status == LiveActionStatus.EXECUTED:
            self._grid_v2_register_pending_place(cid)
            result.executed_count += 1
        elif live_action.status in (LiveActionStatus.BLOCKED, LiveActionStatus.SKIPPED):
            self._grid_v2_clean_failed_place(cid)
            result.blocked_count += 1
        elif live_action.status == LiveActionStatus.FAILED and live_action.pre_send:
            self._grid_v2_clean_failed_place(cid)
            result.failed_count += 1
        elif live_action.status == LiveActionStatus.FAILED:
            if _grid_v2_is_exchange_code_ambiguous(live_action.exchange_code):
                self._grid_v2_register_pending_place(cid)
                logger.warning(
                    "GRID_V2_FAILED_PLACE_QUARANTINED cid=%s code=%s reason=%s",
                    cid,
                    live_action.exchange_code,
                    live_action.block_reason.value if live_action.block_reason else "?",
                )
            else:
                self._grid_v2_clean_failed_place(cid)
            result.failed_count += 1

        # Seed CID clearing (awaiting_sync deadlock prevention)
        seed_definitive_fail = cid in self._grid_v2_pending_seed_cids and (
            live_action.status in (LiveActionStatus.BLOCKED, LiveActionStatus.SKIPPED)
            or (live_action.status == LiveActionStatus.FAILED and live_action.pre_send)
            or (
                live_action.status == LiveActionStatus.FAILED
                and not live_action.pre_send
                and not _grid_v2_is_exchange_code_ambiguous(live_action.exchange_code)
            )
        )
        if seed_definitive_fail:
            self._grid_v2_pending_seed_cids = self._grid_v2_pending_seed_cids - {cid}
            logger.warning(
                "GRID_V2_SEED_CID_CLEARED cid=%s status=%s code=%s",
                cid,
                live_action.status.value,
                live_action.exchange_code,
            )
            if not self._grid_v2_pending_seed_cids:
                self._grid_v2_awaiting_sync = False
                logger.warning(
                    "GRID_V2_AWAITING_SYNC_CLEARED_ON_SEED_FAILURE "
                    "reason=all_seeds_definitively_failed"
                )

    def _process_action(self, action: ExecutionAction, ts: int) -> LiveAction:  # noqa: PLR0911, PLR0912, PLR0915
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

        # Force-reduce exit suppression: block new grid reduce-only exits
        # while unload owns the reduce-only budget. Only SYMBOL_UNLOAD_STEP
        # actions are allowed through.
        if (
            self._force_reduce_requested
            and action.action_type == ActionType.PLACE
            and action.reduce_only
            and getattr(action, "reason", "") != "SYMBOL_UNLOAD_STEP"
        ):
            logger.debug(
                "FORCE_REDUCE_EXIT_SUPPRESSED symbol=%s reason=unload_owns_budget",
                action.symbol,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED,
                intent=intent,
            )

        # Gate 0: Reduce-only budget guard v2 (ADR-104)
        # Block further reduce-only exits when pending repair (after -2022 reject).
        # Direction-scoped: only blocks the affected (symbol, side).
        if (
            action.action_type == ActionType.PLACE
            and action.reduce_only
            and action.symbol
            and action.side is not None
            and (action.symbol, action.side.value) in self._reduce_only_pending_repair
        ):
            from grinder.live.reason_codes import ExitSuppressionReason  # noqa: PLC0415

            logger.warning(
                "GRID_V2_EXIT_SUPPRESSED symbol=%s side=%s reason=%s",
                action.symbol,
                action.side.value,
                ExitSuppressionReason.PENDING_REPAIR_AFTER_REJECT.value,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED,
                intent=intent,
            )
        # Uses reservation model: open_remaining + batch_reserved + new_qty <= position.
        if (
            action.action_type == ActionType.PLACE
            and action.reduce_only
            and action.quantity is not None
            and action.symbol
            and action.side is not None
        ):
            from grinder.live.reduce_only_budget import (  # noqa: PLC0415
                BudgetCheckResult,
                BudgetSnapshot,
                _closeable_qty_for_side,
                check_budget,
            )

            snap = self._last_account_snapshot
            # Direction-aware: SELL exit → long closeable, BUY exit → short closeable
            base_closeable = (
                _closeable_qty_for_side(snap, action.symbol, action.side)
                if snap is not None
                else Decimal(0)
            )
            # Add provable current-tick lot additions
            batch_additions = self._reduce_only_batch_new_lots_qty.get(action.symbol, Decimal(0))
            position_qty = base_closeable + batch_additions
            if position_qty > 0:
                batch_key = (action.symbol, action.side.value)
                batch_qty = self._reduce_only_batch_qty.get(batch_key, Decimal(0))
                existing_ro = self._get_open_reduce_only_qty(action.symbol, action.side)
                budget = BudgetSnapshot(
                    symbol=action.symbol,
                    side=action.side.value,
                    position_closeable_qty=position_qty,
                    open_reduce_only_remaining_qty=existing_ro,
                    reserved_qty=batch_qty,
                )
                result = check_budget(budget, action.quantity)
                if result == BudgetCheckResult.BLOCKED:
                    from grinder.live.reason_codes import (  # noqa: PLC0415
                        ExitSuppressionReason,
                    )

                    logger.warning(
                        "GRID_V2_EXIT_SUPPRESSED symbol=%s side=%s reason=%s "
                        "open_ro=%s reserved=%s new=%s "
                        "position=%s available=%s",
                        action.symbol,
                        action.side.value,
                        ExitSuppressionReason.REDUCE_ONLY_BUDGET_EXCEEDED.value,
                        budget.open_reduce_only_remaining_qty,
                        budget.reserved_qty,
                        action.quantity,
                        budget.position_closeable_qty,
                        budget.available,
                    )
                    return LiveAction(
                        action=action,
                        status=LiveActionStatus.BLOCKED,
                        block_reason=BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED,
                        intent=intent,
                    )
                # Passed: accumulate reservation for next action in same tick
                self._reduce_only_batch_qty[batch_key] = batch_qty + action.quantity

        # Gate 0.5: Live Health Gate — block writes when truth is unsafe
        if not self._is_write_allowed_by_health(action):
            from grinder.live.reason_codes import HealthBlockReason  # noqa: PLC0415

            _health_reason = HealthBlockReason.__members__.get(self._health_mode.value, None)
            logger.warning(
                "GRID_V2_HEALTH_BLOCK symbol=%s reason=%s action=%s",
                action.symbol or "?",
                _health_reason.value if _health_reason else self._health_mode.value,
                action.action_type.value,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.HEALTH_GATE_UNSAFE,
                intent=intent,
            )

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

        # Gate 5.5: Risk base enforcement (PR-2, ADR-092)
        # Blocks INCREASE_RISK when risk base unavailable/stale/below_min,
        # or when symbol/portfolio notional caps are breached.
        # REDUCE_RISK/CANCEL never blocked (intent already checked above).
        if self._risk_base_enabled and intent == RiskIntent.INCREASE_RISK:
            _rb_decision = evaluate_risk_gate(
                risk_base=self._risk_base_snapshot,
                snapshot=self._last_account_snapshot,
                config=self._portfolio_risk_config,
                symbol=action.symbol or "",
            )
            if not _rb_decision.allowed:
                _rb_block = _RISK_GATE_TO_BLOCK.get(
                    _rb_decision.reason, BlockReason.RISK_BASE_UNAVAILABLE
                )
                get_risk_base_metrics().record_gate_block(_rb_block.value)
                # ADR-102: Track consecutive RISK_SYMBOL_CAP blocks for saturation.
                # Only consecutive cap blocks count. A non-cap block resets the
                # counter (different failure mode, not sustained cap pressure).
                _sym = action.symbol or ""
                if _rb_block == BlockReason.RISK_SYMBOL_CAP and _sym:
                    prev = self._risk_cap_consecutive_blocks.get(_sym, 0)
                    self._risk_cap_consecutive_blocks[_sym] = prev + 1
                    if (
                        self._risk_cap_consecutive_blocks[_sym] >= self._risk_saturation_threshold
                        and _sym not in self._risk_saturated_symbols
                    ):
                        self._risk_saturated_symbols.add(_sym)
                        logger.warning(
                            "GRID_V2_RISK_SATURATED_ENTER symbol=%s "
                            "consecutive_cap_blocks=%d threshold=%d",
                            _sym,
                            self._risk_cap_consecutive_blocks[_sym],
                            self._risk_saturation_threshold,
                        )
                elif _sym and self._risk_cap_consecutive_blocks.get(_sym, 0) > 0:
                    # Non-cap block resets consecutive cap counter
                    self._risk_cap_consecutive_blocks[_sym] = 0
                logger.warning(
                    "Action blocked: %s (%s) symbol=%s action=%s",
                    _rb_block.value,
                    _rb_decision.detail,
                    action.symbol,
                    action.action_type.value,
                )
                return LiveAction(
                    action=action,
                    status=LiveActionStatus.BLOCKED,
                    block_reason=_rb_block,
                    intent=intent,
                )
            else:
                # ADR-102: Risk gate allowed — reset cap block counter, exit saturation
                _sym = action.symbol or ""
                if _sym and self._risk_cap_consecutive_blocks.get(_sym, 0) > 0:
                    self._risk_cap_consecutive_blocks[_sym] = 0
                if _sym and _sym in self._risk_saturated_symbols:
                    self._risk_saturated_symbols.discard(_sym)
                    logger.info(
                        "GRID_V2_RISK_SATURATED_EXIT symbol=%s reason=headroom_restored",
                        _sym,
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

        # Gate 9: Selector gate (doc-36 Phase 2) — blocks INCREASE_RISK for symbols
        # not in active set or in graceful_exit_only. Cancel/reduce allowed.
        # Bypass: grid_v2 internal actions (repair/seed/recenter/exit_restore) must not
        # be blocked — they are mandatory for grid integrity, not new risk entries.
        _is_grid_v2_internal = action.reason.startswith("grid_v2_") or action.reason in (
            "INTEGRITY_REPAIR",
            "RECENTER",
            "RECENTER_REPLACE",
            "EXIT_RESTORE",
            "EXIT_RESTORE_SHIFT",
        )
        if (
            intent == RiskIntent.INCREASE_RISK
            and action.symbol
            and not _is_grid_v2_internal
            and not self.is_selector_dispatch_allowed(action.symbol)
        ):
            logger.info(
                "Action blocked: SELECTOR_BLOCKED symbol=%s action=%s",
                action.symbol,
                action.action_type.value,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.SELECTOR_BLOCKED,
                intent=intent,
            )

        # Gate 10: Symbol Risk Manager (per-symbol CAPPED/EXIT_ONLY blocking)
        # Only safety-critical grid_v2 actions bypass: cancel, reduce-only exits,
        # integrity cancel. Regular grid_v2 entries (PLACE_ENTRY) are subject to
        # risk gating — this is the whole point of the risk manager.
        _is_grid_v2_safety_only = (
            action.reduce_only
            or action.action_type == ActionType.CANCEL
            or (
                action.reason
                in (
                    "grid_v2_INTEGRITY_CANCEL_ENTRY",
                    "grid_v2_INTEGRITY_CANCEL_EXIT",
                    "EXIT_RESTORE",
                    "EXIT_RESTORE_SHIFT",
                )
            )
        )
        if (
            intent == RiskIntent.INCREASE_RISK
            and action.symbol
            and not _is_grid_v2_safety_only
            and self._symbol_risk_manager.config.enabled
        ):
            sr_state = self._symbol_risk_manager.get_state(action.symbol)
            if sr_state == SymbolRiskState.EXIT_ONLY:
                logger.info(
                    "Action blocked: SYMBOL_RISK_EXIT_ONLY symbol=%s action=%s",
                    action.symbol,
                    action.action_type.value,
                )
                return LiveAction(
                    action=action,
                    status=LiveActionStatus.BLOCKED,
                    block_reason=BlockReason.SYMBOL_RISK_EXIT_ONLY,
                    intent=intent,
                )
            if sr_state == SymbolRiskState.CAPPED:
                logger.info(
                    "Action blocked: SYMBOL_RISK_CAPPED symbol=%s action=%s",
                    action.symbol,
                    action.action_type.value,
                )
                return LiveAction(
                    action=action,
                    status=LiveActionStatus.BLOCKED,
                    block_reason=BlockReason.SYMBOL_RISK_CAPPED,
                    intent=intent,
                )

        # SOR routing (Launch-14 PR2): after all safety gates, before execution
        if self._is_sor_enabled() and action.action_type in (
            ActionType.PLACE,
            ActionType.REPLACE,
        ):
            sor_result = self._apply_sor(action, ts, intent)
            if sor_result is not None:
                return sor_result

        # All gates passed - execute action (or defer if gate-only mode)
        if self._seed_gate_only_mode:
            # Seed concurrent mode: record intent, skip HTTP
            self._seed_gate_passed_intents.append((action, intent))
            return LiveAction(
                action=action,
                status=LiveActionStatus.EXECUTED,
                intent=intent,
            )
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

    def _submit_to_exchange(self, action: ExecutionAction, ts: int) -> SubmitOutcome:
        """Pure HTTP submit with retries — no engine state mutations.

        Thread-safe: only calls exchange port methods and retry policy.
        Does not touch pending maps, counters, or any mutable engine state.

        Returns SubmitOutcome describing what happened on the network.
        """
        max_attempts = self._retry_policy.max_attempts
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                order_id = self._execute_single(action, ts)
                return SubmitOutcome(order_id=order_id, attempts=attempt, success=True)
            except ConnectorNonRetryableError as e:
                return SubmitOutcome(
                    error=str(e),
                    exchange_code=getattr(e, "exchange_code", None),
                    pre_send=getattr(e, "pre_send", False),
                    attempts=attempt,
                )
            except CircuitOpenError as e:
                return SubmitOutcome(
                    error=str(e), pre_send=True, attempts=attempt, circuit_open=True
                )
            except ConnectorTransientError as e:
                last_error = e
                if attempt < max_attempts:
                    delay_ms = self._retry_policy.compute_delay_ms(attempt)
                    time.sleep(delay_ms / 1000.0)
            except ConnectorError as e:
                if is_retryable(e, self._retry_policy):
                    last_error = e
                    if attempt < max_attempts:
                        delay_ms = self._retry_policy.compute_delay_ms(attempt)
                        time.sleep(delay_ms / 1000.0)
                else:
                    return SubmitOutcome(
                        error=str(e),
                        exchange_code=_extract_binance_error_code(str(e)),
                        attempts=attempt,
                    )

        return SubmitOutcome(
            error=str(last_error) if last_error else "Unknown",
            attempts=max_attempts,
            retries_exhausted=True,
        )

    def _apply_submit_outcome(
        self,
        action: ExecutionAction,
        outcome: SubmitOutcome,
        intent: RiskIntent,
    ) -> LiveAction:
        """Apply post-submit state mutations and build LiveAction.

        Serial only — mutates shared engine state (pending maps, counters).
        """
        if outcome.success:
            live_action = LiveAction(
                action=action,
                status=LiveActionStatus.EXECUTED,
                order_id=outcome.order_id,
                attempts=outcome.attempts,
                intent=intent,
            )
            # Post-success bookkeeping
            if action.action_type == ActionType.PLACE:
                cid_sent = action.client_order_id or outcome.order_id or ""
                self._recent_places.append((cid_sent, int(time.time() * 1000), action.symbol))
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

        if outcome.circuit_open:
            return LiveAction(
                action=action,
                status=LiveActionStatus.FAILED,
                block_reason=BlockReason.CIRCUIT_BREAKER_OPEN,
                error=outcome.error,
                attempts=outcome.attempts,
                intent=intent,
                pre_send=True,
            )

        if outcome.retries_exhausted:
            # Post-exhaustion health signals
            error_str = outcome.error or ""
            if "-1021" in error_str:
                self._on_clock_drift_error()
                if hasattr(self._exchange_port, "refresh_ts_offset"):
                    self._exchange_port.refresh_ts_offset()
            if "name resolution" in error_str.lower() or "connection error" in error_str.lower():
                self._on_dns_error()
            return LiveAction(
                action=action,
                status=LiveActionStatus.FAILED,
                block_reason=BlockReason.MAX_RETRIES_EXCEEDED,
                error=outcome.error,
                attempts=outcome.attempts,
                intent=intent,
            )

        # Non-retryable failure
        error_msg = outcome.error or ""
        if "Order count limit reached" in error_msg and not self._order_budget_exhausted:
            self._order_budget_exhausted = True
            logger.warning("ORDER_BUDGET_LATCH activated — planner suppressed for remaining run")
        if action.reduce_only and action.symbol and action.side is not None:
            err_code = outcome.exchange_code or _extract_binance_error_code(error_msg)
            if err_code == -2022:
                self._on_reduce_only_reject(action.symbol, action.side.value, err_code)
        return LiveAction(
            action=action,
            status=LiveActionStatus.FAILED,
            block_reason=BlockReason.NON_RETRYABLE_ERROR,
            error=outcome.error,
            attempts=outcome.attempts,
            intent=intent,
            pre_send=outcome.pre_send,
            exchange_code=outcome.exchange_code,
        )

    def _check_pre_send_gates(
        self, action: ExecutionAction, intent: RiskIntent
    ) -> LiveAction | None:
        """Run pre-send gates (NOOP, min_notional). Returns LiveAction if blocked, None if OK.

        Shared by _execute_action (normal path) and seed concurrent path.
        """
        if action.action_type == ActionType.NOOP:
            return LiveAction(action=action, status=LiveActionStatus.SKIPPED, intent=intent)

        if (
            action.action_type == ActionType.PLACE
            and not action.reduce_only
            and action.price is not None
            and action.quantity is not None
            and action.symbol
        ):
            _min_notional = self._min_notional_cache.get(action.symbol)
            if _min_notional is not None and _min_notional > 0:
                _notional = action.price * action.quantity
                if _notional < _min_notional:
                    logger.warning(
                        "MIN_NOTIONAL_BLOCKED symbol=%s price=%s qty=%s notional=%s min=%s",
                        action.symbol,
                        action.price,
                        action.quantity,
                        _notional,
                        _min_notional,
                    )
                    return LiveAction(
                        action=action,
                        status=LiveActionStatus.BLOCKED,
                        block_reason=BlockReason.NOTIONAL_TOO_LOW,
                        intent=intent,
                        pre_send=True,
                    )
        return None

    def _execute_action(self, action: ExecutionAction, ts: int, intent: RiskIntent) -> LiveAction:
        """Execute action on exchange port with retries.

        Thin wrapper: pre-send gates → _submit_to_exchange → _apply_submit_outcome.
        """
        # Pre-send gates (shared with seed concurrent path)
        blocked = self._check_pre_send_gates(action, intent)
        if blocked is not None:
            return blocked

        # Debug logging (env-gated)
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
        if action.action_type == ActionType.CANCEL and self._debug_open_orders:
            logger.warning(
                "CANCEL_INTENT order_id=%s symbol=%s reason=%s",
                action.order_id,
                action.symbol,
                action.reason or "planner",
            )

        # Submit (thread-safe) → apply (serial state mutations)
        outcome = self._submit_to_exchange(action, ts)
        return self._apply_submit_outcome(action, outcome, intent)

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
