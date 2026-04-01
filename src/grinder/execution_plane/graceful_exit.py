"""Graceful exit signal for per-symbol engine (PR-E3, ADR-136).

SSOT: docs/39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md Section 4.2.
      docs/40_EXECUTION_PLANE_IMPLEMENTATION_PLAN.md Phase E3.

Signals a running engine to enter graceful-exit-only mode:
- No new entries after signal
- Engine continues managing exits and cancels
- Registry transitions ACTIVE -> GRACEFUL_EXIT

All dependencies injectable. Fail-closed on error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from grinder.execution_plane.registry import (
    EngineRegistry,
    EngineState,
    InvalidEngineTransitionError,
)

logger = logging.getLogger(__name__)


class GracefulExitOutcome(Enum):
    """Result of graceful exit signal."""

    SUCCESS = "SUCCESS"
    WRONG_STATE = "WRONG_STATE"
    SIGNAL_FAILED = "SIGNAL_FAILED"


@dataclass(frozen=True)
class GracefulExitResult:
    """Result of one graceful exit signal attempt.

    Attributes:
        symbol: Trading pair.
        outcome: What happened.
        detail: Human-readable detail.
    """

    symbol: str
    outcome: GracefulExitOutcome
    detail: str = ""


# Injectable: Callable[[Any], bool] — signals engine to block new entries, returns True on success
GracefulExitHookFn = Any


def _check_graceful_exit_preconditions(
    symbol: str,
    registry: EngineRegistry,
) -> GracefulExitResult | None:
    """Check state preconditions. Returns early result or None to proceed."""
    current = registry.get_state(symbol)
    if current == EngineState.GRACEFUL_EXIT:
        logger.info("ENGINE_GRACEFUL_EXIT_ALREADY symbol=%s", symbol)
        return GracefulExitResult(
            symbol=symbol, outcome=GracefulExitOutcome.SUCCESS, detail="already in GRACEFUL_EXIT"
        )

    if current != EngineState.ACTIVE:
        logger.warning(
            "ENGINE_GRACEFUL_EXIT_FAILED symbol=%s reason=WRONG_STATE state=%s",
            symbol,
            current.value,
        )
        return GracefulExitResult(
            symbol=symbol,
            outcome=GracefulExitOutcome.WRONG_STATE,
            detail=f"expected ACTIVE, got {current.value}",
        )

    handle = registry.get(symbol)
    if handle is None or handle.engine_ref is None:
        logger.warning("ENGINE_GRACEFUL_EXIT_FAILED symbol=%s reason=NO_ENGINE_REF", symbol)
        return GracefulExitResult(
            symbol=symbol, outcome=GracefulExitOutcome.SIGNAL_FAILED, detail="no engine reference"
        )

    return None


def signal_graceful_exit(
    symbol: str,
    registry: EngineRegistry,
    exit_hook: GracefulExitHookFn,
) -> GracefulExitResult:
    """Signal a running engine to enter graceful-exit-only mode.

    Engine must be ACTIVE. After signal, engine blocks new entries
    but continues managing exits. Registry transitions to GRACEFUL_EXIT.
    """
    precondition = _check_graceful_exit_preconditions(symbol, registry)
    if precondition is not None:
        return precondition

    handle = registry.get(symbol)
    logger.info("ENGINE_GRACEFUL_EXIT_STARTED symbol=%s", symbol)

    try:
        success = exit_hook(handle.engine_ref)  # type: ignore[union-attr]
    except Exception as e:
        logger.error("ENGINE_GRACEFUL_EXIT_FAILED symbol=%s reason=HOOK_ERROR error=%s", symbol, e)
        return GracefulExitResult(
            symbol=symbol, outcome=GracefulExitOutcome.SIGNAL_FAILED, detail=f"hook error: {e}"
        )

    if not success:
        logger.warning("ENGINE_GRACEFUL_EXIT_FAILED symbol=%s reason=HOOK_RETURNED_FALSE", symbol)
        return GracefulExitResult(
            symbol=symbol, outcome=GracefulExitOutcome.SIGNAL_FAILED, detail="hook returned false"
        )

    try:
        registry.transition(symbol, EngineState.GRACEFUL_EXIT, reason="graceful_exit_signal")
    except InvalidEngineTransitionError as e:
        logger.error(
            "ENGINE_GRACEFUL_EXIT_FAILED symbol=%s reason=TRANSITION_ERROR error=%s", symbol, e
        )
        return GracefulExitResult(
            symbol=symbol, outcome=GracefulExitOutcome.SIGNAL_FAILED, detail=str(e)
        )

    logger.info("ENGINE_GRACEFUL_EXIT_COMPLETED symbol=%s", symbol)
    return GracefulExitResult(symbol=symbol, outcome=GracefulExitOutcome.SUCCESS)
