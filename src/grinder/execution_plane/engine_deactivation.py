"""Per-symbol engine deactivation ceremony (PR-E3, ADR-136).

SSOT: docs/39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md Section 4.2.
      docs/40_EXECUTION_PLANE_IMPLEMENTATION_PLAN.md Phase E3.

Safely stops one per-symbol engine with cleanup verification.
Only legal from GRACEFUL_EXIT state after finalize gate passes.

Sequence:
1. Verify current state is GRACEFUL_EXIT
2. Check finalize gate (flat + no orders)
3. Transition to SHUTTING_DOWN
4. Stop engine via injectable hook
5. Cleanup exchange state
6. Verify clean state
7. On success: STOPPED. On failure: FAILED.

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
)
from grinder.orchestration.deactivation import (
    DeactivationFacts,
    can_finalize_deactivation,
)

logger = logging.getLogger(__name__)


class DeactivationOutcome(Enum):
    """Result of a deactivation ceremony."""

    SUCCESS = "SUCCESS"
    WRONG_STATE = "WRONG_STATE"
    FINALIZE_BLOCKED = "FINALIZE_BLOCKED"
    STOP_FAILED = "STOP_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"


@dataclass(frozen=True)
class EngineDeactivationResult:
    """Result of one deactivation ceremony.

    Attributes:
        symbol: Trading pair.
        outcome: What happened.
        detail: Human-readable detail.
    """

    symbol: str
    outcome: DeactivationOutcome
    detail: str = ""


# Injectable hooks
StopEngineFn = Any  # Callable[[Any], None] — stops the engine
CleanupFn = Any  # Callable[[str], bool] — runs exchange cleanup, returns True on success
VerifyCleanFn = Any  # Callable[[str], bool] — verifies clean exchange state


def _run_shutdown_and_cleanup(
    symbol: str,
    registry: EngineRegistry,
    stop_engine: StopEngineFn,
    cleanup: CleanupFn,
    verify_clean: VerifyCleanFn,
) -> EngineDeactivationResult:
    """Execute stop → cleanup → verify. Updates registry on success/failure."""
    handle = registry.get(symbol)
    if handle is not None and handle.engine_ref is not None:
        try:
            stop_engine(handle.engine_ref)
        except Exception as e:
            logger.error(
                "ENGINE_DEACTIVATION_FAILED symbol=%s reason=STOP_ERROR error=%s", symbol, e
            )
            registry.transition(symbol, EngineState.FAILED, reason=f"stop_failed: {e}")
            return EngineDeactivationResult(
                symbol=symbol, outcome=DeactivationOutcome.STOP_FAILED, detail=str(e)
            )

    try:
        cleanup_ok = cleanup(symbol)
    except Exception as e:
        logger.error(
            "ENGINE_DEACTIVATION_FAILED symbol=%s reason=CLEANUP_ERROR error=%s", symbol, e
        )
        registry.transition(symbol, EngineState.FAILED, reason=f"cleanup_failed: {e}")
        return EngineDeactivationResult(
            symbol=symbol, outcome=DeactivationOutcome.CLEANUP_FAILED, detail=str(e)
        )

    if not cleanup_ok:
        logger.warning("ENGINE_DEACTIVATION_FAILED symbol=%s reason=CLEANUP_RETURNED_FALSE", symbol)
        registry.transition(symbol, EngineState.FAILED, reason="cleanup_failed")
        return EngineDeactivationResult(
            symbol=symbol, outcome=DeactivationOutcome.CLEANUP_FAILED, detail="returned false"
        )

    try:
        is_clean = verify_clean(symbol)
    except Exception as e:
        logger.error("ENGINE_DEACTIVATION_FAILED symbol=%s reason=VERIFY_ERROR error=%s", symbol, e)
        registry.transition(symbol, EngineState.FAILED, reason=f"verify_failed: {e}")
        return EngineDeactivationResult(
            symbol=symbol, outcome=DeactivationOutcome.VERIFY_FAILED, detail=str(e)
        )

    if not is_clean:
        logger.warning("ENGINE_DEACTIVATION_FAILED symbol=%s reason=VERIFY_DIRTY", symbol)
        registry.transition(symbol, EngineState.FAILED, reason="verify_dirty")
        return EngineDeactivationResult(
            symbol=symbol,
            outcome=DeactivationOutcome.VERIFY_FAILED,
            detail="not clean after cleanup",
        )

    registry.transition(symbol, EngineState.STOPPED, reason="deactivation_ceremony_success")
    logger.info("ENGINE_DEACTIVATION_COMPLETED symbol=%s", symbol)
    return EngineDeactivationResult(symbol=symbol, outcome=DeactivationOutcome.SUCCESS)


def deactivate(
    symbol: str,
    registry: EngineRegistry,
    facts: DeactivationFacts,
    stop_engine: StopEngineFn,
    cleanup: CleanupFn,
    verify_clean: VerifyCleanFn,
) -> EngineDeactivationResult:
    """Run deactivation ceremony for one symbol.

    Only legal from GRACEFUL_EXIT. Requires finalize gate to pass.
    Fail-closed: any error → FAILED.

    Args:
        symbol: Trading pair.
        registry: Engine registry.
        facts: Current runtime facts for finalize gate.
        stop_engine: Callable(engine_ref) -> None. Stops the engine.
        cleanup: Callable(symbol) -> bool. Runs exchange cleanup.
        verify_clean: Callable(symbol) -> bool. Verifies clean state.

    Returns:
        EngineDeactivationResult with outcome.
    """
    # Step 1: Verify current state
    current = registry.get_state(symbol)
    if current != EngineState.GRACEFUL_EXIT:
        logger.warning(
            "ENGINE_DEACTIVATION_FAILED symbol=%s reason=WRONG_STATE state=%s",
            symbol,
            current.value,
        )
        return EngineDeactivationResult(
            symbol=symbol,
            outcome=DeactivationOutcome.WRONG_STATE,
            detail=f"expected GRACEFUL_EXIT, got {current.value}",
        )

    # Step 2: Check finalize gate
    gate = can_finalize_deactivation(symbol, facts)
    if not gate.can_finalize:
        reason_str = gate.block_reason.value if gate.block_reason else "unknown"
        logger.info("ENGINE_DEACTIVATION_BLOCKED symbol=%s reason=%s", symbol, reason_str)
        return EngineDeactivationResult(
            symbol=symbol,
            outcome=DeactivationOutcome.FINALIZE_BLOCKED,
            detail=f"finalize gate blocked: {reason_str}",
        )

    # Step 3: Transition to SHUTTING_DOWN, then stop → cleanup → verify
    logger.info("ENGINE_DEACTIVATION_STARTED symbol=%s", symbol)
    registry.transition(symbol, EngineState.SHUTTING_DOWN, reason="deactivation_ceremony")

    return _run_shutdown_and_cleanup(symbol, registry, stop_engine, cleanup, verify_clean)
