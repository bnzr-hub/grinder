"""Per-symbol engine activation ceremony (PR-E2, ADR-135).

SSOT: docs/39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md Section 4.1.

Safely starts one per-symbol engine and verifies it reaches ACTIVE state.
All dependencies are injected — no hidden globals.

Sequence:
1. Verify exchange state is clean (injectable verifier)
2. Register symbol as ACTIVATING in EngineRegistry
3. Build engine via injectable factory
4. Run health check (injectable checker)
5. On success: transition to ACTIVE
6. On failure/timeout: transition to FAILED

Fail-closed: any error → FAILED. No retry loops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from grinder.execution_plane.registry import (
    DuplicateEngineError,
    EngineRegistry,
    EngineState,
)

logger = logging.getLogger(__name__)


class ActivationOutcome(Enum):
    """Result of an activation ceremony."""

    SUCCESS = "SUCCESS"
    DIRTY_EXCHANGE = "DIRTY_EXCHANGE"
    DUPLICATE_ENGINE = "DUPLICATE_ENGINE"
    BUILD_FAILED = "BUILD_FAILED"
    HEALTH_CHECK_FAILED = "HEALTH_CHECK_FAILED"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True)
class ActivationResult:
    """Result of one activation ceremony.

    Attributes:
        symbol: Trading pair.
        outcome: What happened.
        engine_ref: Engine instance if successful, None otherwise.
        detail: Human-readable detail for logging/audit.
    """

    symbol: str
    outcome: ActivationOutcome
    engine_ref: Any = None
    detail: str = ""


@dataclass(frozen=True)
class ActivationConfig:
    """Configuration for activation ceremony.

    Attributes:
        health_check_timeout_s: Max seconds to wait for health check.
    """

    health_check_timeout_s: float = 60.0


# Type aliases for injectable dependencies
VerifyCleanFn = Any  # Callable[[str], bool] — returns True if exchange is clean
EngineFactoryFn = Any  # Callable[[str, Any], Any] — builds engine from (symbol, config)
HealthCheckFn = Any  # Callable[[Any], bool] — returns True if engine is healthy


def _check_preconditions(
    symbol: str,
    registry: EngineRegistry,
    verify_clean: VerifyCleanFn,
) -> ActivationResult | None:
    """Check duplicate and clean-exchange preconditions. Returns failure result or None."""
    current_state = registry.get_state(symbol)
    if current_state not in (EngineState.ABSENT, EngineState.STOPPED):
        logger.warning(
            "ENGINE_ACTIVATION_FAILED symbol=%s reason=DUPLICATE state=%s",
            symbol,
            current_state.value,
        )
        return ActivationResult(
            symbol=symbol,
            outcome=ActivationOutcome.DUPLICATE_ENGINE,
            detail=f"already in state {current_state.value}",
        )

    try:
        is_clean = verify_clean(symbol)
    except Exception as e:
        logger.warning("ENGINE_ACTIVATION_FAILED symbol=%s reason=VERIFY_ERROR error=%s", symbol, e)
        return ActivationResult(
            symbol=symbol,
            outcome=ActivationOutcome.DIRTY_EXCHANGE,
            detail=f"verify error: {e}",
        )

    if not is_clean:
        logger.warning("ENGINE_ACTIVATION_FAILED symbol=%s reason=DIRTY_EXCHANGE", symbol)
        return ActivationResult(
            symbol=symbol,
            outcome=ActivationOutcome.DIRTY_EXCHANGE,
            detail="exchange state not clean",
        )

    return None


def _build_and_check(
    symbol: str,
    registry: EngineRegistry,
    engine_factory: EngineFactoryFn,
    health_check: HealthCheckFn,
    engine_config: Any,
) -> ActivationResult:
    """Build engine and run health check. Updates registry on success/failure."""
    try:
        engine_ref = engine_factory(symbol, engine_config)
    except Exception as e:
        logger.error("ENGINE_ACTIVATION_FAILED symbol=%s reason=BUILD_FAILED error=%s", symbol, e)
        registry.transition(symbol, EngineState.FAILED, reason=f"build_failed: {e}")
        return ActivationResult(
            symbol=symbol, outcome=ActivationOutcome.BUILD_FAILED, detail=str(e)
        )

    try:
        is_healthy = health_check(engine_ref)
    except Exception as e:
        logger.error(
            "ENGINE_ACTIVATION_FAILED symbol=%s reason=HEALTH_CHECK_ERROR error=%s", symbol, e
        )
        registry.transition(symbol, EngineState.FAILED, reason=f"health_check_error: {e}")
        return ActivationResult(
            symbol=symbol, outcome=ActivationOutcome.HEALTH_CHECK_FAILED, detail=f"error: {e}"
        )

    if not is_healthy:
        logger.warning("ENGINE_ACTIVATION_FAILED symbol=%s reason=HEALTH_CHECK_FAILED", symbol)
        registry.transition(symbol, EngineState.FAILED, reason="health_check_failed")
        return ActivationResult(
            symbol=symbol, outcome=ActivationOutcome.HEALTH_CHECK_FAILED, detail="returned false"
        )

    handle = registry.get(symbol)
    if handle is not None:
        handle.engine_ref = engine_ref
    registry.transition(symbol, EngineState.ACTIVE, reason="activation_ceremony_success")
    logger.info("ENGINE_ACTIVATION_COMPLETED symbol=%s", symbol)
    return ActivationResult(symbol=symbol, outcome=ActivationOutcome.SUCCESS, engine_ref=engine_ref)


def activate(
    symbol: str,
    registry: EngineRegistry,
    verify_clean: VerifyCleanFn,
    engine_factory: EngineFactoryFn,
    health_check: HealthCheckFn,
    engine_config: Any = None,
    config: ActivationConfig | None = None,  # noqa: ARG001
) -> ActivationResult:
    """Run activation ceremony for one symbol.

    Fail-closed: any error → FAILED in registry. No retry.

    Args:
        symbol: Trading pair to activate.
        registry: Engine registry to update.
        verify_clean: Callable(symbol) -> bool. True if exchange is clean.
        engine_factory: Callable(symbol, config) -> engine_ref. Builds engine.
        health_check: Callable(engine_ref) -> bool. True if engine is healthy.
        engine_config: Symbol-specific config passed to factory.
        config: Ceremony configuration.

    Returns:
        ActivationResult with outcome and optional engine reference.
    """
    # Preconditions: duplicate check + clean exchange verify
    failure = _check_preconditions(symbol, registry, verify_clean)
    if failure is not None:
        return failure

    # Register as ACTIVATING
    logger.info("ENGINE_ACTIVATION_STARTED symbol=%s", symbol)
    try:
        registry.register(symbol)
    except DuplicateEngineError as e:
        logger.warning(
            "ENGINE_ACTIVATION_FAILED symbol=%s reason=DUPLICATE_REGISTER error=%s", symbol, e
        )
        return ActivationResult(
            symbol=symbol, outcome=ActivationOutcome.DUPLICATE_ENGINE, detail=str(e)
        )

    # Build engine + health check + transition to ACTIVE or FAILED
    return _build_and_check(symbol, registry, engine_factory, health_check, engine_config)
