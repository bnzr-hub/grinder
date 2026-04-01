"""Tests for engine DeactivationCeremony (PR-E3, ADR-136).

Covers:
- Graceful exit + clean finalize succeeds
- Finalize blocked when not clean
- Cleanup failure marks FAILED
- Wrong state blocked
- Stop error marks FAILED
- Full lifecycle path
- Determinism
"""

from __future__ import annotations

from grinder.execution_plane.engine_deactivation import (
    DeactivationOutcome,
    deactivate,
)
from grinder.execution_plane.registry import EngineRegistry, EngineState
from grinder.orchestration.deactivation import DeactivationFacts


def _setup_graceful_exit(reg: EngineRegistry, symbol: str = "BTC") -> None:
    """Helper: register, activate, then graceful exit."""
    reg.register(symbol, engine_ref="fake_engine")
    reg.transition(symbol, EngineState.ACTIVE)
    reg.transition(symbol, EngineState.GRACEFUL_EXIT)


def _clean_facts() -> DeactivationFacts:
    return DeactivationFacts(is_flat=True, open_orders_count=0)


def _dirty_facts() -> DeactivationFacts:
    return DeactivationFacts(is_flat=False, open_orders_count=3)


def _noop_stop(_engine: object) -> None:
    pass


def _ok_cleanup(_symbol: str) -> bool:
    return True


def _ok_verify(_symbol: str) -> bool:
    return True


def _fail_cleanup(_symbol: str) -> bool:
    return False


def _fail_verify(_symbol: str) -> bool:
    return False


class TestCleanFinalize:
    def test_success(self) -> None:
        reg = EngineRegistry()
        _setup_graceful_exit(reg)

        result = deactivate("BTC", reg, _clean_facts(), _noop_stop, _ok_cleanup, _ok_verify)

        assert result.outcome == DeactivationOutcome.SUCCESS
        assert reg.get_state("BTC") == EngineState.STOPPED

    def test_engine_ref_cleared_path(self) -> None:
        """Engine stop hook is called with engine_ref."""
        reg = EngineRegistry()
        _setup_graceful_exit(reg)

        stopped: list[object] = []
        deactivate(
            "BTC",
            reg,
            _clean_facts(),
            stopped.append,
            _ok_cleanup,
            _ok_verify,
        )
        assert stopped == ["fake_engine"]


class TestFinalizeBlocked:
    def test_dirty_facts_block(self) -> None:
        reg = EngineRegistry()
        _setup_graceful_exit(reg)

        result = deactivate("BTC", reg, _dirty_facts(), _noop_stop, _ok_cleanup, _ok_verify)

        assert result.outcome == DeactivationOutcome.FINALIZE_BLOCKED
        assert reg.get_state("BTC") == EngineState.GRACEFUL_EXIT  # unchanged

    def test_flat_but_orders_block(self) -> None:
        facts = DeactivationFacts(is_flat=True, open_orders_count=2)
        reg = EngineRegistry()
        _setup_graceful_exit(reg)

        result = deactivate("BTC", reg, facts, _noop_stop, _ok_cleanup, _ok_verify)

        assert result.outcome == DeactivationOutcome.FINALIZE_BLOCKED


class TestCleanupFailure:
    def test_cleanup_returns_false(self) -> None:
        reg = EngineRegistry()
        _setup_graceful_exit(reg)

        result = deactivate("BTC", reg, _clean_facts(), _noop_stop, _fail_cleanup, _ok_verify)

        assert result.outcome == DeactivationOutcome.CLEANUP_FAILED
        assert reg.get_state("BTC") == EngineState.FAILED

    def test_cleanup_raises(self) -> None:
        reg = EngineRegistry()
        _setup_graceful_exit(reg)

        def boom(_s: str) -> bool:
            raise RuntimeError("cleanup exploded")

        result = deactivate("BTC", reg, _clean_facts(), _noop_stop, boom, _ok_verify)

        assert result.outcome == DeactivationOutcome.CLEANUP_FAILED
        assert reg.get_state("BTC") == EngineState.FAILED


class TestVerifyFailure:
    def test_verify_returns_false(self) -> None:
        reg = EngineRegistry()
        _setup_graceful_exit(reg)

        result = deactivate("BTC", reg, _clean_facts(), _noop_stop, _ok_cleanup, _fail_verify)

        assert result.outcome == DeactivationOutcome.VERIFY_FAILED
        assert reg.get_state("BTC") == EngineState.FAILED


class TestWrongState:
    def test_active_blocked(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC", engine_ref="e")
        reg.transition("BTC", EngineState.ACTIVE)

        result = deactivate("BTC", reg, _clean_facts(), _noop_stop, _ok_cleanup, _ok_verify)

        assert result.outcome == DeactivationOutcome.WRONG_STATE
        assert reg.get_state("BTC") == EngineState.ACTIVE

    def test_absent_blocked(self) -> None:
        reg = EngineRegistry()
        result = deactivate("BTC", reg, _clean_facts(), _noop_stop, _ok_cleanup, _ok_verify)
        assert result.outcome == DeactivationOutcome.WRONG_STATE


class TestStopError:
    def test_stop_raises(self) -> None:
        reg = EngineRegistry()
        _setup_graceful_exit(reg)

        def boom(_e: object) -> None:
            raise RuntimeError("engine won't stop")

        result = deactivate("BTC", reg, _clean_facts(), boom, _ok_cleanup, _ok_verify)

        assert result.outcome == DeactivationOutcome.STOP_FAILED
        assert reg.get_state("BTC") == EngineState.FAILED


class TestFullLifecycle:
    def test_activate_graceful_deactivate(self) -> None:
        """ABSENT -> ACTIVATING -> ACTIVE -> GRACEFUL_EXIT -> SHUTTING_DOWN -> STOPPED."""
        reg = EngineRegistry()
        reg.register("BTC", engine_ref="e")
        reg.transition("BTC", EngineState.ACTIVE)
        reg.transition("BTC", EngineState.GRACEFUL_EXIT)

        result = deactivate("BTC", reg, _clean_facts(), _noop_stop, _ok_cleanup, _ok_verify)

        assert result.outcome == DeactivationOutcome.SUCCESS
        assert reg.get_state("BTC") == EngineState.STOPPED


class TestDeterminism:
    def test_same_inputs(self) -> None:
        def run() -> DeactivationOutcome:
            reg = EngineRegistry()
            _setup_graceful_exit(reg)
            return deactivate(
                "BTC", reg, _clean_facts(), _noop_stop, _ok_cleanup, _ok_verify
            ).outcome

        assert run() == run()
