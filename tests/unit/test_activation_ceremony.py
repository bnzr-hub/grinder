"""Tests for ActivationCeremony (PR-E2, ADR-135).

Covers:
- Clean exchange → activation succeeds
- Dirty exchange → blocked
- Build error → FAILED
- Health check failure → FAILED
- Duplicate activation → rejected
- Registry transitions correct
- Timestamps progress
- Determinism
"""

from __future__ import annotations

from grinder.execution_plane.activation import (
    ActivationOutcome,
    activate,
)
from grinder.execution_plane.registry import EngineRegistry, EngineState


def _clean_verify(_symbol: str) -> bool:
    return True


def _dirty_verify(_symbol: str) -> bool:
    return False


def _good_factory(_symbol: str, _config: object) -> str:
    return "fake_engine"


def _failing_factory(_symbol: str, _config: object) -> str:
    raise RuntimeError("build exploded")


def _healthy_check(_engine: object) -> bool:
    return True


def _unhealthy_check(_engine: object) -> bool:
    return False


def _exploding_check(_engine: object) -> bool:
    raise RuntimeError("health check exploded")


class TestCleanActivation:
    """Clean exchange + good engine → ACTIVE."""

    def test_success(self) -> None:
        reg = EngineRegistry()
        result = activate(
            symbol="BTCUSDT",
            registry=reg,
            verify_clean=_clean_verify,
            engine_factory=_good_factory,
            health_check=_healthy_check,
        )

        assert result.outcome == ActivationOutcome.SUCCESS
        assert result.engine_ref == "fake_engine"
        assert reg.get_state("BTCUSDT") == EngineState.ACTIVE

    def test_engine_ref_stored_in_handle(self) -> None:
        reg = EngineRegistry()
        activate(
            symbol="BTCUSDT",
            registry=reg,
            verify_clean=_clean_verify,
            engine_factory=_good_factory,
            health_check=_healthy_check,
        )

        handle = reg.get("BTCUSDT")
        assert handle is not None
        assert handle.engine_ref == "fake_engine"


class TestDirtyExchange:
    """Dirty exchange → activation blocked."""

    def test_dirty_blocks(self) -> None:
        reg = EngineRegistry()
        result = activate(
            symbol="BTCUSDT",
            registry=reg,
            verify_clean=_dirty_verify,
            engine_factory=_good_factory,
            health_check=_healthy_check,
        )

        assert result.outcome == ActivationOutcome.DIRTY_EXCHANGE
        assert reg.get_state("BTCUSDT") == EngineState.ABSENT

    def test_verify_error_blocks(self) -> None:
        def verify_error(_s: str) -> bool:
            raise ConnectionError("offline")

        reg = EngineRegistry()
        result = activate(
            symbol="BTCUSDT",
            registry=reg,
            verify_clean=verify_error,
            engine_factory=_good_factory,
            health_check=_healthy_check,
        )

        assert result.outcome == ActivationOutcome.DIRTY_EXCHANGE
        assert reg.get_state("BTCUSDT") == EngineState.ABSENT


class TestBuildError:
    """Engine factory error → FAILED."""

    def test_build_failure(self) -> None:
        reg = EngineRegistry()
        result = activate(
            symbol="BTCUSDT",
            registry=reg,
            verify_clean=_clean_verify,
            engine_factory=_failing_factory,
            health_check=_healthy_check,
        )

        assert result.outcome == ActivationOutcome.BUILD_FAILED
        assert reg.get_state("BTCUSDT") == EngineState.FAILED


class TestHealthCheckFailure:
    """Health check failure → FAILED."""

    def test_unhealthy(self) -> None:
        reg = EngineRegistry()
        result = activate(
            symbol="BTCUSDT",
            registry=reg,
            verify_clean=_clean_verify,
            engine_factory=_good_factory,
            health_check=_unhealthy_check,
        )

        assert result.outcome == ActivationOutcome.HEALTH_CHECK_FAILED
        assert reg.get_state("BTCUSDT") == EngineState.FAILED

    def test_health_error(self) -> None:
        reg = EngineRegistry()
        result = activate(
            symbol="BTCUSDT",
            registry=reg,
            verify_clean=_clean_verify,
            engine_factory=_good_factory,
            health_check=_exploding_check,
        )

        assert result.outcome == ActivationOutcome.HEALTH_CHECK_FAILED
        assert reg.get_state("BTCUSDT") == EngineState.FAILED


class TestDuplicateActivation:
    """Symbol already present → rejected."""

    def test_duplicate_active(self) -> None:
        reg = EngineRegistry()
        activate(
            symbol="BTCUSDT",
            registry=reg,
            verify_clean=_clean_verify,
            engine_factory=_good_factory,
            health_check=_healthy_check,
        )
        assert reg.get_state("BTCUSDT") == EngineState.ACTIVE

        result = activate(
            symbol="BTCUSDT",
            registry=reg,
            verify_clean=_clean_verify,
            engine_factory=_good_factory,
            health_check=_healthy_check,
        )

        assert result.outcome == ActivationOutcome.DUPLICATE_ENGINE
        assert reg.get_state("BTCUSDT") == EngineState.ACTIVE


class TestRegistryTransitions:
    """Registry reflects correct lifecycle states."""

    def test_success_path_transitions(self) -> None:
        t = [0.0]
        reg = EngineRegistry(_clock=lambda: t[0])

        t[0] = 1.0
        result = activate(
            symbol="BTCUSDT",
            registry=reg,
            verify_clean=_clean_verify,
            engine_factory=_good_factory,
            health_check=_healthy_check,
        )

        assert result.outcome == ActivationOutcome.SUCCESS
        handle = reg.get("BTCUSDT")
        assert handle is not None
        assert handle.state == EngineState.ACTIVE
        assert handle.created_at == 1.0

    def test_failure_path_transitions(self) -> None:
        reg = EngineRegistry()
        activate(
            symbol="BTCUSDT",
            registry=reg,
            verify_clean=_clean_verify,
            engine_factory=_failing_factory,
            health_check=_healthy_check,
        )

        handle = reg.get("BTCUSDT")
        assert handle is not None
        assert handle.state == EngineState.FAILED


class TestDeterminism:
    """Same inputs → same result."""

    def test_repeated_success(self) -> None:
        def run() -> ActivationOutcome:
            reg = EngineRegistry()
            r = activate(
                symbol="BTC",
                registry=reg,
                verify_clean=_clean_verify,
                engine_factory=_good_factory,
                health_check=_healthy_check,
            )
            return r.outcome

        assert run() == run()

    def test_repeated_failure(self) -> None:
        def run() -> ActivationOutcome:
            reg = EngineRegistry()
            r = activate(
                symbol="BTC",
                registry=reg,
                verify_clean=_clean_verify,
                engine_factory=_failing_factory,
                health_check=_healthy_check,
            )
            return r.outcome

        assert run() == run()


class TestSignals:
    """Activation emits expected log signals."""

    def test_success_signals(self) -> None:
        import logging as _logging  # noqa: PLC0415

        messages: list[str] = []

        class Capture(_logging.Handler):
            def emit(self, record: _logging.LogRecord) -> None:
                messages.append(self.format(record))

        log = _logging.getLogger("grinder.execution_plane.activation")
        handler = Capture()
        log.addHandler(handler)
        log.setLevel(_logging.DEBUG)

        try:
            reg = EngineRegistry()
            activate(
                symbol="BTCUSDT",
                registry=reg,
                verify_clean=_clean_verify,
                engine_factory=_good_factory,
                health_check=_healthy_check,
            )

            text = "\n".join(messages)
            assert "ENGINE_ACTIVATION_STARTED symbol=BTCUSDT" in text
            assert "ENGINE_ACTIVATION_COMPLETED symbol=BTCUSDT" in text
        finally:
            log.removeHandler(handler)

    def test_failure_signal(self) -> None:
        import logging as _logging  # noqa: PLC0415

        messages: list[str] = []

        class Capture(_logging.Handler):
            def emit(self, record: _logging.LogRecord) -> None:
                messages.append(self.format(record))

        log = _logging.getLogger("grinder.execution_plane.activation")
        handler = Capture()
        log.addHandler(handler)
        log.setLevel(_logging.DEBUG)

        try:
            reg = EngineRegistry()
            activate(
                symbol="BTCUSDT",
                registry=reg,
                verify_clean=_dirty_verify,
                engine_factory=_good_factory,
                health_check=_healthy_check,
            )

            text = "\n".join(messages)
            assert "ENGINE_ACTIVATION_FAILED symbol=BTCUSDT" in text
        finally:
            log.removeHandler(handler)
