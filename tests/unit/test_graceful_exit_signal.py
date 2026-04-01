"""Tests for GracefulExitSignal (PR-E3, ADR-136).

Covers:
- Active to graceful exit success
- Entry blocking after signal (via hook contract)
- Signal failure does not fake success
- Idempotent on already GRACEFUL_EXIT
- Wrong state rejected
"""

from __future__ import annotations

from grinder.execution_plane.graceful_exit import (
    GracefulExitOutcome,
    signal_graceful_exit,
)
from grinder.execution_plane.registry import EngineRegistry, EngineState


def _setup_active_engine(reg: EngineRegistry, symbol: str = "BTC") -> None:
    """Helper: register and activate an engine."""
    reg.register(symbol, engine_ref="fake_engine")
    reg.transition(symbol, EngineState.ACTIVE)


class TestActiveToGracefulExit:
    def test_success(self) -> None:
        reg = EngineRegistry()
        _setup_active_engine(reg)

        result = signal_graceful_exit("BTC", reg, exit_hook=lambda _e: True)

        assert result.outcome == GracefulExitOutcome.SUCCESS
        assert reg.get_state("BTC") == EngineState.GRACEFUL_EXIT

    def test_hook_called_with_engine_ref(self) -> None:
        reg = EngineRegistry()
        _setup_active_engine(reg)

        called_with: list[object] = []

        def capture_hook(engine_ref: object) -> bool:
            called_with.append(engine_ref)
            return True

        signal_graceful_exit("BTC", reg, exit_hook=capture_hook)
        assert called_with == ["fake_engine"]


class TestEntryBlockingAfterSignal:
    def test_hook_represents_entry_blocking(self) -> None:
        """Hook returning True = entries blocked. State proves it."""
        reg = EngineRegistry()
        _setup_active_engine(reg)

        entries_blocked = [False]

        def blocking_hook(_engine: object) -> bool:
            entries_blocked[0] = True
            return True

        signal_graceful_exit("BTC", reg, exit_hook=blocking_hook)

        assert entries_blocked[0] is True
        assert reg.get_state("BTC") == EngineState.GRACEFUL_EXIT


class TestSignalFailure:
    def test_hook_returns_false(self) -> None:
        reg = EngineRegistry()
        _setup_active_engine(reg)

        result = signal_graceful_exit("BTC", reg, exit_hook=lambda _e: False)

        assert result.outcome == GracefulExitOutcome.SIGNAL_FAILED
        assert reg.get_state("BTC") == EngineState.ACTIVE  # unchanged

    def test_hook_raises(self) -> None:
        reg = EngineRegistry()
        _setup_active_engine(reg)

        def exploding_hook(_e: object) -> bool:
            raise RuntimeError("hook broke")

        result = signal_graceful_exit("BTC", reg, exit_hook=exploding_hook)

        assert result.outcome == GracefulExitOutcome.SIGNAL_FAILED
        assert reg.get_state("BTC") == EngineState.ACTIVE

    def test_no_engine_ref(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")  # no engine_ref
        reg.transition("BTC", EngineState.ACTIVE)

        result = signal_graceful_exit("BTC", reg, exit_hook=lambda _e: True)

        assert result.outcome == GracefulExitOutcome.SIGNAL_FAILED


class TestIdempotent:
    def test_already_graceful_exit(self) -> None:
        reg = EngineRegistry()
        _setup_active_engine(reg)
        signal_graceful_exit("BTC", reg, exit_hook=lambda _e: True)

        result = signal_graceful_exit("BTC", reg, exit_hook=lambda _e: True)

        assert result.outcome == GracefulExitOutcome.SUCCESS
        assert "already" in result.detail


class TestWrongState:
    def test_absent(self) -> None:
        reg = EngineRegistry()
        result = signal_graceful_exit("BTC", reg, exit_hook=lambda _e: True)
        assert result.outcome == GracefulExitOutcome.WRONG_STATE

    def test_activating(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        result = signal_graceful_exit("BTC", reg, exit_hook=lambda _e: True)
        assert result.outcome == GracefulExitOutcome.WRONG_STATE
