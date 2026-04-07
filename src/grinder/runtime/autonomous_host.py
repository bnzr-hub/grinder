"""Autonomous engine host: real per-symbol engine lifecycle ownership (ADR-147).

Owns the authoritative map of ``symbol -> live engine handle`` and provides
lifecycle operations that reach real engine instances. The host synchronizes
with :class:`EngineRegistry` to maintain registry/runtime coherence.

Runtime model: **in-process engine tasks**. Each engine is created by an
injectable factory function and referenced via ``EngineHandle.engine_ref``.
The host does not manage subprocesses or async tasks — it delegates to
synchronous factory/stop/cleanup callables so the caller controls the
execution model.

SSOT: this module for host lifecycle semantics. Registry for state transitions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from grinder.execution_plane.registry import EngineRegistry, EngineState

logger = logging.getLogger(__name__)


class EngineFactory(Protocol):
    """Creates a real engine instance for a symbol."""

    def __call__(self, symbol: str) -> Any: ...


class EngineStopFn(Protocol):
    """Stops a live engine instance. Called with (symbol, engine_ref)."""

    def __call__(self, symbol: str, engine_ref: Any) -> bool: ...


class EngineCleanupFn(Protocol):
    """Cleans up after engine stop. Called with (symbol, engine_ref)."""

    def __call__(self, symbol: str, engine_ref: Any) -> bool: ...


class GracefulExitResult(Enum):
    """Result of a graceful exit attempt."""

    SUCCESS = "SUCCESS"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # No position / read-only — benign
    FAILED = "FAILED"  # Real failure to signal graceful exit


class GracefulExitFn(Protocol):
    """Signals graceful exit on a live engine. Called with (symbol, engine_ref)."""

    def __call__(self, symbol: str, engine_ref: Any) -> GracefulExitResult: ...


@dataclass(frozen=True)
class HostShutdownReport:
    """Result of shutdown_all()."""

    stopped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return len(self.failed) == 0


@dataclass
class AutonomousEngineHost:
    """Owns real per-symbol engine lifecycle.

    Responsibilities:
    - Maintains ``symbol -> engine_ref`` mapping
    - Synchronizes lifecycle with :class:`EngineRegistry`
    - Provides ceremony-compatible callables for :class:`ExecutionCoordinator`

    All callables are injectable for testing.
    """

    registry: EngineRegistry
    engine_factory: EngineFactory
    engine_stop_fn: EngineStopFn
    engine_cleanup_fn: EngineCleanupFn | None = None
    graceful_exit_fn: GracefulExitFn | None = None
    force_reduce_fn: Any | None = None  # Callable(symbol, engine_ref) → bool
    _clock: Any = field(default=time.monotonic)

    # Internal handle map — authoritative source of "what is actually running"
    _live_engines: dict[str, Any] = field(default_factory=dict, init=False)

    def activate(self, symbol: str) -> bool:
        """Start a real engine for a symbol.

        1. Register in registry (ABSENT → ACTIVATING)
        2. Create engine via factory
        3. Store engine_ref in registry handle and local map
        4. Transition to ACTIVE

        Returns True on success, False on failure (fail-closed).
        """
        if symbol in self._live_engines:
            logger.warning(
                "HOST_ACTIVATE_DUPLICATE symbol=%s — already live, skipping",
                symbol,
            )
            return False

        try:
            handle = self.registry.register(symbol)
        except Exception as e:
            logger.error("HOST_ACTIVATE_REGISTER_FAILED symbol=%s error=%s", symbol, e)
            return False

        try:
            engine_ref = self.engine_factory(symbol)
        except Exception as e:
            logger.error("HOST_ACTIVATE_FACTORY_FAILED symbol=%s error=%s", symbol, e)
            self.registry.transition(symbol, EngineState.FAILED, reason="factory_failed")
            return False

        handle.engine_ref = engine_ref
        self._live_engines[symbol] = engine_ref

        try:
            self.registry.transition(symbol, EngineState.ACTIVE, reason="host_activate")
        except Exception as e:
            logger.error("HOST_ACTIVATE_TRANSITION_FAILED symbol=%s error=%s", symbol, e)
            self._live_engines.pop(symbol, None)
            return False

        logger.info("HOST_ACTIVATE_OK symbol=%s", symbol)
        return True

    def request_graceful_exit(self, symbol: str) -> GracefulExitResult:  # noqa: PLR0911
        """Signal graceful exit on a live engine.

        1. Verify engine is live
        2. Invoke graceful_exit_fn on real engine ref
        3. Transition registry to GRACEFUL_EXIT

        Returns GracefulExitResult: SUCCESS, NOT_APPLICABLE, or FAILED.
        """
        engine_ref = self._live_engines.get(symbol)
        if engine_ref is None:
            logger.warning(
                "HOST_GRACEFUL_EXIT_MISSING symbol=%s — no live engine",
                symbol,
            )
            return GracefulExitResult.FAILED

        if self.graceful_exit_fn is None:
            logger.error(
                "HOST_GRACEFUL_EXIT_NO_FN symbol=%s — no graceful_exit_fn wired",
                symbol,
            )
            return GracefulExitResult.FAILED

        try:
            result = self.graceful_exit_fn(symbol, engine_ref)
            if result == GracefulExitResult.NOT_APPLICABLE:
                logger.info("HOST_GRACEFUL_EXIT_NOT_APPLICABLE symbol=%s", symbol)
                return GracefulExitResult.NOT_APPLICABLE
            if result == GracefulExitResult.FAILED:
                logger.error("HOST_GRACEFUL_EXIT_FN_FAILED symbol=%s", symbol)
                return GracefulExitResult.FAILED
        except Exception as e:
            logger.error(
                "HOST_GRACEFUL_EXIT_FN_ERROR symbol=%s error=%s",
                symbol,
                e,
            )
            return GracefulExitResult.FAILED

        try:
            self.registry.transition(symbol, EngineState.GRACEFUL_EXIT, reason="host_graceful_exit")
        except Exception as e:
            logger.error(
                "HOST_GRACEFUL_EXIT_TRANSITION_FAILED symbol=%s error=%s",
                symbol,
                e,
            )
            return GracefulExitResult.FAILED

        logger.info("HOST_GRACEFUL_EXIT_OK symbol=%s", symbol)
        return GracefulExitResult.SUCCESS

    def get_engine_ref(self, symbol: str) -> Any:
        """Get engine handle for a live symbol. Returns None if not live."""
        return self._live_engines.get(symbol)

    def request_force_reduce(self, symbol: str) -> bool:
        """Request force-reduce on a live engine. Idempotent.

        Returns True if newly requested, False if not live / no fn / already set.
        """
        engine_ref = self._live_engines.get(symbol)
        if engine_ref is None:
            logger.warning("HOST_FORCE_REDUCE_MISSING symbol=%s — no live engine", symbol)
            return False

        if self.force_reduce_fn is None:
            logger.warning("HOST_FORCE_REDUCE_NO_FN symbol=%s", symbol)
            return False

        try:
            ok: bool = self.force_reduce_fn(symbol, engine_ref)
            logger.info("HOST_FORCE_REDUCE symbol=%s newly_set=%s", symbol, ok)
            return ok
        except Exception as e:
            logger.error("HOST_FORCE_REDUCE_ERROR symbol=%s error=%s", symbol, e)
            return False

    def finalize_deactivation(self, symbol: str) -> bool:
        """Complete deactivation: stop engine, cleanup, remove from host.

        1. Transition registry to SHUTTING_DOWN
        2. Stop engine via engine_stop_fn
        3. Optional cleanup via engine_cleanup_fn
        4. Transition to STOPPED
        5. Remove from live map

        Returns True on success, False on failure.
        """
        engine_ref = self._live_engines.get(symbol)
        if engine_ref is None:
            logger.warning(
                "HOST_DEACTIVATE_MISSING symbol=%s — no live engine",
                symbol,
            )
            return False

        try:
            self.registry.transition(symbol, EngineState.SHUTTING_DOWN, reason="host_deactivate")
        except Exception as e:
            logger.error(
                "HOST_DEACTIVATE_TRANSITION_FAILED symbol=%s error=%s",
                symbol,
                e,
            )
            return False

        stop_ok = True
        try:
            stop_ok = self.engine_stop_fn(symbol, engine_ref)
        except Exception as e:
            logger.error("HOST_DEACTIVATE_STOP_FAILED symbol=%s error=%s", symbol, e)
            stop_ok = False

        cleanup_ok = True
        if self.engine_cleanup_fn is not None:
            try:
                cleanup_ok = bool(self.engine_cleanup_fn(symbol, engine_ref))
            except Exception as e:
                logger.error(
                    "HOST_DEACTIVATE_CLEANUP_FAILED symbol=%s error=%s",
                    symbol,
                    e,
                )
                cleanup_ok = False

        import contextlib  # noqa: PLC0415

        all_ok = stop_ok and cleanup_ok
        target_state = EngineState.STOPPED if all_ok else EngineState.FAILED
        with contextlib.suppress(Exception):
            self.registry.transition(symbol, target_state, reason="host_deactivate_done")

        self._live_engines.pop(symbol, None)
        logger.info("HOST_DEACTIVATE_OK symbol=%s final_state=%s", symbol, target_state.value)
        return all_ok

    def shutdown_all(self) -> HostShutdownReport:
        """Stop all live engines in deterministic order.

        Returns a report of which symbols stopped cleanly and which failed.
        """
        stopped: list[str] = []
        failed: list[str] = []

        for symbol in sorted(self._live_engines):
            state = self.registry.get_state(symbol)
            if state == EngineState.ACTIVE:
                ge_result = self.request_graceful_exit(symbol)
                if ge_result == GracefulExitResult.NOT_APPLICABLE:
                    # Benign: no position / read-only. Force registry through
                    # GRACEFUL_EXIT so deactivation transition is legal.
                    import contextlib  # noqa: PLC0415

                    with contextlib.suppress(Exception):
                        self.registry.transition(
                            symbol,
                            EngineState.GRACEFUL_EXIT,
                            reason="shutdown_bypass_not_applicable",
                        )
                    logger.info(
                        "HOST_SHUTDOWN_GRACEFUL_BYPASS symbol=%s reason=not_applicable",
                        symbol,
                    )
                elif ge_result == GracefulExitResult.FAILED:
                    # Real failure — do NOT bypass. Let finalize_deactivation fail.
                    logger.error(
                        "HOST_SHUTDOWN_GRACEFUL_EXIT_FAILED symbol=%s",
                        symbol,
                    )

            ok = self.finalize_deactivation(symbol)
            if ok:
                stopped.append(symbol)
            else:
                failed.append(symbol)

        report = HostShutdownReport(stopped=stopped, failed=failed)
        logger.info(
            "HOST_SHUTDOWN_ALL stopped=%d failed=%d",
            len(stopped),
            len(failed),
        )
        return report

    @property
    def live_symbols(self) -> frozenset[str]:
        """Currently live engine symbols."""
        return frozenset(self._live_engines)

    def is_live(self, symbol: str) -> bool:
        """Whether a symbol has a live engine."""
        return symbol in self._live_engines
