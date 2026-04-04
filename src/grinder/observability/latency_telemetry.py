"""Phase-level latency telemetry for autonomous grid_v2 order path.

Provides structured phase-summary logs for startup, seed, fill reaction,
sync polling, and shutdown. Designed for grep-friendly diagnostics:

    grep LATENCY_ grinder.log | head

Log format:
    LATENCY_BOOTSTRAP total_ms=...
    LATENCY_ENGINE_STARTUP engine_ms=... ws_ms=...
    LATENCY_SEED symbol=... total_ms=... count=...
    LATENCY_FILL_REACTION symbol=... fill_to_exit_ms=... fill_to_cancel_ms=...
    LATENCY_SYNC_POLL symbol=... position_ms=... orders_ms=...
    LATENCY_SHUTDOWN symbol=... cancel_ms=... close_ms=... total_ms=...
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class PhaseTimer:
    """Monotonic stopwatch for a single phase measurement."""

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)


def log_bootstrap(total_ms: int, symbols_count: int, tuned_count: int) -> None:
    logger.info(
        "LATENCY_BOOTSTRAP total_ms=%d symbols=%d tuned=%d",
        total_ms,
        symbols_count,
        tuned_count,
    )


def log_engine_startup(symbol: str, engine_ms: int, ws_ms: int, total_ms: int) -> None:
    logger.info(
        "LATENCY_ENGINE_STARTUP symbol=%s engine_ms=%d ws_ms=%d total_ms=%d",
        symbol,
        engine_ms,
        ws_ms,
        total_ms,
    )


def log_grid_v2_startup(symbol: str, startup_ms: int, seed_count: int) -> None:
    logger.info(
        "LATENCY_GRID_V2_STARTUP symbol=%s startup_ms=%d seed_count=%d",
        symbol,
        startup_ms,
        seed_count,
    )


def log_seed_dispatch(symbol: str, total_ms: int, count: int) -> None:
    logger.info(
        "LATENCY_SEED symbol=%s total_ms=%d count=%d",
        symbol,
        total_ms,
        count,
    )


def log_fill_reaction(
    symbol: str,
    fill_to_actions_ms: int,
    action_count: int,
) -> None:
    logger.info(
        "LATENCY_FILL_REACTION symbol=%s fill_to_actions_ms=%d actions=%d",
        symbol,
        fill_to_actions_ms,
        action_count,
    )


def log_account_sync(symbol: str, sync_ms: int) -> None:
    """Log account sync cycle duration (positionRisk + openOrders combined)."""
    logger.info(
        "LATENCY_ACCOUNT_SYNC symbol=%s sync_ms=%d",
        symbol,
        sync_ms,
    )


def log_reconcile(symbol: str, cycle_ms: int, actions: int) -> None:
    logger.info(
        "LATENCY_RECONCILE symbol=%s cycle_ms=%d actions=%d",
        symbol,
        cycle_ms,
        actions,
    )


def log_fill_exit(symbol: str, action_ms: int) -> None:
    """Log time from fill detection to protective exit action dispatched."""
    logger.info(
        "LATENCY_FILL_EXIT symbol=%s action_ms=%d",
        symbol,
        action_ms,
    )


def log_fill_cancel_wave(
    symbol: str, first_submit_ms: int, last_submit_ms: int, count: int
) -> None:
    """Log latency of opposite-side cancel wave after fill."""
    logger.info(
        "LATENCY_FILL_CANCEL_WAVE symbol=%s first_submit_ms=%d last_submit_ms=%d count=%d",
        symbol,
        first_submit_ms,
        last_submit_ms,
        count,
    )


def log_branch_convergence(symbol: str, total_ms: int, actions: int) -> None:
    """Log total time from fill detection to all branch actions dispatched."""
    logger.info(
        "LATENCY_BRANCH_CONVERGENCE symbol=%s total_ms=%d actions=%d",
        symbol,
        total_ms,
        actions,
    )


def log_shutdown(symbol: str, cancel_ms: int, close_ms: int, total_ms: int) -> None:
    logger.info(
        "LATENCY_SHUTDOWN symbol=%s cancel_ms=%d close_ms=%d total_ms=%d",
        symbol,
        cancel_ms,
        close_ms,
        total_ms,
    )
