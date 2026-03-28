"""Live Health Gate: evaluates truth-source health and controls write policy.

SSOT for runtime health modes. Engine consults this before dispatching writes.
Fail-closed: when truth is stale/degraded beyond thresholds, writes are blocked.

Design constraints:
- Pure evaluation logic: no I/O, no exchange calls.
- Deterministic: same inputs → same mode.
- Caller (engine) owns the data sources and passes them in.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class LiveHealthMode(Enum):
    """Runtime health mode for live trading engine."""

    HEALTHY = "HEALTHY"
    DEGRADED_SYNC = "DEGRADED_SYNC"  # sync failing/delayed, truth not yet hard-stale
    DEGRADED_WS = "DEGRADED_WS"  # WS disconnected but sync still fresh
    STALE_TRUTH = "STALE_TRUTH"  # truth sources exceeded safe freshness budget
    PAUSED_UNSAFE = "PAUSED_UNSAFE"  # hard safety stop for writes


@dataclass(frozen=True)
class LiveHealthConfig:
    """Thresholds for health mode transitions."""

    # Account sync staleness
    sync_soft_stale_s: int = 15  # soft: sync older than this → DEGRADED_SYNC
    sync_hard_stale_s: int = 30  # hard: sync older than this → STALE_TRUTH

    # WS freshness (market data)
    ws_stale_s: int = 10  # no WS message for this long → DEGRADED_WS

    # Combined degradation → PAUSED_UNSAFE
    # If both WS and sync are degraded simultaneously
    combined_hard_stale_s: int = 60  # both degraded for this long → PAUSED_UNSAFE

    # Clock drift
    clock_drift_warn_ms: int = 500  # warn threshold
    clock_drift_block_ms: int = 1500  # block threshold → contributes to STALE_TRUTH

    # Consecutive sync failures
    sync_fail_soft_count: int = 3  # 3 consecutive fails → DEGRADED_SYNC
    sync_fail_hard_count: int = 6  # 6 consecutive fails → STALE_TRUTH


@dataclass
class LiveHealthInput:
    """Input signals for health evaluation. Engine populates this each tick."""

    # Account sync
    last_sync_success_ts: float = 0.0  # wall-clock seconds of last successful sync
    consecutive_sync_failures: int = 0

    # WS (market data)
    ws_connected: bool = True
    last_ws_message_ts: float = 0.0  # wall-clock seconds of last WS message

    # Clock drift
    last_clock_drift_error_ts: float = 0.0  # wall-clock of last -1021 error
    clock_drift_errors_recent: int = 0  # count in last 60s

    # DNS / connectivity
    last_dns_error_ts: float = 0.0
    dns_errors_recent: int = 0  # count in last 60s


@dataclass(frozen=True)
class LiveHealthResult:
    """Result of health evaluation."""

    mode: LiveHealthMode
    reason: str
    write_allowed: bool  # normal PLACE writes
    cancel_allowed: bool  # CANCEL/cleanup always allowed
    reduce_only_allowed: bool  # reduce-only exits may be allowed in some modes


def evaluate_health(  # noqa: PLR0911
    inputs: LiveHealthInput,
    config: LiveHealthConfig,
    now: float | None = None,
) -> LiveHealthResult:
    """Evaluate live health mode from current signals.

    Returns health mode + write policy. Deterministic for same inputs.
    """
    if now is None:
        now = time.time()

    # If no sync has ever succeeded, treat as HEALTHY (init phase).
    # Health gate only activates after first successful sync.
    if inputs.last_sync_success_ts == 0.0:
        return LiveHealthResult(
            mode=LiveHealthMode.HEALTHY,
            reason="init_no_sync_yet",
            write_allowed=True,
            cancel_allowed=True,
            reduce_only_allowed=True,
        )

    sync_age = now - inputs.last_sync_success_ts
    ws_age = now - inputs.last_ws_message_ts if inputs.last_ws_message_ts > 0 else float("inf")
    clock_drift_age = (
        now - inputs.last_clock_drift_error_ts
        if inputs.last_clock_drift_error_ts > 0
        else float("inf")
    )

    # PAUSED_UNSAFE: both sync AND WS hard-stale, or extreme sync failure
    if sync_age > config.combined_hard_stale_s and ws_age > config.combined_hard_stale_s:
        return LiveHealthResult(
            mode=LiveHealthMode.PAUSED_UNSAFE,
            reason=f"both_sync_and_ws_hard_stale sync_age={sync_age:.0f}s ws_age={ws_age:.0f}s",
            write_allowed=False,
            cancel_allowed=True,
            reduce_only_allowed=False,
        )

    if inputs.consecutive_sync_failures >= config.sync_fail_hard_count:
        return LiveHealthResult(
            mode=LiveHealthMode.PAUSED_UNSAFE,
            reason=f"consecutive_sync_failures={inputs.consecutive_sync_failures}",
            write_allowed=False,
            cancel_allowed=True,
            reduce_only_allowed=False,
        )

    # STALE_TRUTH: sync hard-stale OR recent clock drift storm
    if sync_age > config.sync_hard_stale_s:
        return LiveHealthResult(
            mode=LiveHealthMode.STALE_TRUTH,
            reason=f"sync_hard_stale age={sync_age:.0f}s threshold={config.sync_hard_stale_s}s",
            write_allowed=False,
            cancel_allowed=True,
            reduce_only_allowed=True,
        )

    if inputs.clock_drift_errors_recent >= 3 and clock_drift_age < 30:
        return LiveHealthResult(
            mode=LiveHealthMode.STALE_TRUTH,
            reason=f"clock_drift_storm errors={inputs.clock_drift_errors_recent} last={clock_drift_age:.0f}s_ago",
            write_allowed=False,
            cancel_allowed=True,
            reduce_only_allowed=True,
        )

    # DNS/connectivity storm → STALE_TRUTH
    dns_age = now - inputs.last_dns_error_ts if inputs.last_dns_error_ts > 0 else float("inf")
    if inputs.dns_errors_recent >= 3 and dns_age < 30:
        return LiveHealthResult(
            mode=LiveHealthMode.STALE_TRUTH,
            reason=f"dns_storm errors={inputs.dns_errors_recent} last={dns_age:.0f}s_ago",
            write_allowed=False,
            cancel_allowed=True,
            reduce_only_allowed=True,
        )

    # DEGRADED_SYNC: sync soft-stale or consecutive failures
    if sync_age > config.sync_soft_stale_s:
        return LiveHealthResult(
            mode=LiveHealthMode.DEGRADED_SYNC,
            reason=f"sync_soft_stale age={sync_age:.0f}s threshold={config.sync_soft_stale_s}s",
            write_allowed=True,
            cancel_allowed=True,
            reduce_only_allowed=True,
        )

    if inputs.consecutive_sync_failures >= config.sync_fail_soft_count:
        return LiveHealthResult(
            mode=LiveHealthMode.DEGRADED_SYNC,
            reason=f"consecutive_sync_failures={inputs.consecutive_sync_failures}",
            write_allowed=True,
            cancel_allowed=True,
            reduce_only_allowed=True,
        )

    # DEGRADED_WS: WS disconnected or stale, but sync still fresh
    if not inputs.ws_connected or ws_age > config.ws_stale_s:
        return LiveHealthResult(
            mode=LiveHealthMode.DEGRADED_WS,
            reason=f"ws_stale connected={inputs.ws_connected} age={ws_age:.0f}s",
            write_allowed=True,
            cancel_allowed=True,
            reduce_only_allowed=True,
        )

    # HEALTHY
    return LiveHealthResult(
        mode=LiveHealthMode.HEALTHY,
        reason="all_sources_fresh",
        write_allowed=True,
        cancel_allowed=True,
        reduce_only_allowed=True,
    )
