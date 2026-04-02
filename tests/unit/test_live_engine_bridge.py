"""Tests for LiveEngineBridge real engine lifecycle (ADR-149).

Tests use a mock connector transport (FakeWsTransport) to avoid real
WebSocket connections while still exercising real LiveEngineV0 codepaths.
"""

from __future__ import annotations

import threading
from typing import Any

from grinder.connectors.binance_ws import FakeWsTransport
from grinder.execution_plane.registry import EngineRegistry, EngineState
from grinder.runtime.autonomous_host import AutonomousEngineHost, GracefulExitResult
from grinder.runtime.live_engine_bridge import BridgeConfig, EngineHandle, LiveEngineBridge


def _fake_transport() -> FakeWsTransport:
    """Create a FakeWsTransport that connects instantly and yields no messages."""
    return FakeWsTransport(messages=[])


def _bridge(*, shutdown_timeout_s: float = 5.0) -> LiveEngineBridge:
    """Build a bridge with safe test defaults and fake WS transport."""
    return LiveEngineBridge(
        config=BridgeConfig(
            mode="read_only",
            armed=False,
            use_testnet=True,
            shutdown_timeout_s=shutdown_timeout_s,
            ws_transport=_fake_transport(),
        )
    )


class TestBridgeFactory:
    def test_live_engine_bridge_start_creates_real_handle(self) -> None:
        """Factory returns EngineHandle with a running thread."""
        bridge = _bridge()
        handle = bridge.factory("BTCUSDT")
        try:
            assert isinstance(handle, EngineHandle)
            assert handle.symbol == "BTCUSDT"
            assert handle.thread.is_alive()
            assert handle.started_at > 0
        finally:
            handle.shutdown_event.set()
            handle.thread.join(timeout=5.0)

    def test_engine_ref_propagated_to_handle(self) -> None:
        """After factory, engine_ref on handle is a real LiveEngineV0."""
        bridge = _bridge()
        handle = bridge.factory("ETHUSDT")
        try:
            # engine_ref should be set after engine_ready event fires
            assert handle.engine_ref is not None
            assert hasattr(handle.engine_ref, "process_snapshot")
            assert hasattr(handle.engine_ref, "force_graceful_exit")
        finally:
            handle.shutdown_event.set()
            handle.thread.join(timeout=5.0)


class TestBridgeFactoryFailure:
    def test_activation_fails_when_factory_raises(self) -> None:
        """Host.activate returns False when engine factory raises RuntimeError."""
        registry = EngineRegistry()

        def _boom(_symbol: str) -> Any:
            raise RuntimeError("factory boom")

        host = AutonomousEngineHost(
            registry=registry,
            engine_factory=_boom,  # type: ignore[arg-type]
            engine_stop_fn=lambda _s, _e: True,
            graceful_exit_fn=lambda _s, _e: GracefulExitResult.SUCCESS,
        )
        ok = host.activate("BTCUSDT")
        assert not ok
        assert registry.get_state("BTCUSDT") == EngineState.FAILED
        assert not host.is_live("BTCUSDT")


class TestBridgeGracefulExit:
    def test_graceful_exit_reaches_real_live_engine(self) -> None:
        """graceful_exit calls engine.force_graceful_exit(symbol)."""
        bridge = _bridge()
        handle = bridge.factory("BTCUSDT")
        try:
            assert handle.engine_ref is not None
            result = bridge.graceful_exit("BTCUSDT", handle)
            # No position in read-only test → NOT_APPLICABLE (benign)
            assert result == GracefulExitResult.NOT_APPLICABLE
        finally:
            handle.shutdown_event.set()
            handle.thread.join(timeout=5.0)

    def test_graceful_exit_no_engine_fails(self) -> None:
        """graceful_exit on handle with no engine_ref fails."""
        bridge = _bridge()
        handle = EngineHandle(
            symbol="NOPE",
            thread=threading.Thread(target=lambda: None),
            shutdown_event=threading.Event(),
        )
        result = bridge.graceful_exit("NOPE", handle)
        assert result == GracefulExitResult.FAILED


class TestBridgeStop:
    def test_final_deactivation_stops_real_live_engine(self) -> None:
        """stop() signals shutdown and waits for thread to exit."""
        bridge = _bridge()
        handle = bridge.factory("BTCUSDT")
        # Thread may have already exited (empty FakeWsTransport) — that's OK
        ok = bridge.stop("BTCUSDT", handle)
        assert ok
        assert not handle.thread.is_alive()

    def test_stop_timeout_returns_false(self) -> None:
        """If thread doesn't stop in time, stop returns False."""
        # Create a thread that ignores shutdown
        stuck_event = threading.Event()

        def stuck_thread() -> None:
            stuck_event.wait(timeout=30.0)

        handle = EngineHandle(
            symbol="STUCK",
            thread=threading.Thread(target=stuck_thread, daemon=True),
            shutdown_event=threading.Event(),
        )
        handle.thread.start()
        bridge = _bridge(shutdown_timeout_s=0.1)
        ok = bridge.stop("STUCK", handle)
        assert not ok
        # Cleanup
        stuck_event.set()
        handle.thread.join(timeout=1.0)


class TestBridgeCleanup:
    def test_cleanup_succeeds(self) -> None:
        bridge = _bridge()
        handle = EngineHandle(
            symbol="X",
            thread=threading.Thread(target=lambda: None),
            shutdown_event=threading.Event(),
        )
        ok = bridge.cleanup("X", handle)
        assert ok


class TestHostWithBridge:
    def test_host_activate_uses_live_engine_bridge(self) -> None:
        """Full integration: host.activate creates real engine via bridge."""
        bridge = _bridge()
        registry = EngineRegistry()
        host = AutonomousEngineHost(
            registry=registry,
            engine_factory=bridge.factory,
            engine_stop_fn=bridge.stop,
            engine_cleanup_fn=bridge.cleanup,
            graceful_exit_fn=bridge.graceful_exit,
        )
        try:
            ok = host.activate("BTCUSDT")
            assert ok
            assert host.is_live("BTCUSDT")
            assert registry.get_state("BTCUSDT") == EngineState.ACTIVE
            # Real engine thread is running
            handle = host._live_engines["BTCUSDT"]
            assert isinstance(handle, EngineHandle)
            assert handle.thread.is_alive()
        finally:
            host.shutdown_all()

    def test_shutdown_all_stops_real_engine_threads(self) -> None:
        """Full integration: shutdown_all signals and joins engine threads."""
        bridge = _bridge()
        registry = EngineRegistry()
        # Use bridge.stop directly (bypass graceful_exit which returns False
        # for engines with no active ticks — that's correct engine behavior).
        host = AutonomousEngineHost(
            registry=registry,
            engine_factory=bridge.factory,
            engine_stop_fn=bridge.stop,
            engine_cleanup_fn=bridge.cleanup,
            # Graceful exit optional — for no-tick engines it returns False.
            # Use a permissive fn that just signals shutdown for the test.
            graceful_exit_fn=lambda _sym, handle: (
                handle.shutdown_event.set(),
                GracefulExitResult.NOT_APPLICABLE,
            )[1],
        )
        host.activate("BTCUSDT")
        # Thread may have already exited (empty FakeWsTransport) — that's OK
        report = host.shutdown_all()
        assert report.clean
        assert host.live_symbols == frozenset()

    def test_registry_and_host_stay_coherent_with_real_engine_handles(self) -> None:
        """Registry states track real engine lifecycle through activate → stop."""
        bridge = _bridge()
        registry = EngineRegistry()
        host = AutonomousEngineHost(
            registry=registry,
            engine_factory=bridge.factory,
            engine_stop_fn=bridge.stop,
            engine_cleanup_fn=bridge.cleanup,
            graceful_exit_fn=lambda _sym, handle: (
                handle.shutdown_event.set(),
                GracefulExitResult.NOT_APPLICABLE,
            )[1],
        )
        try:
            host.activate("ETHUSDT")
            assert registry.get_state("ETHUSDT") == EngineState.ACTIVE

            # NOT_APPLICABLE graceful exit: registry stays ACTIVE
            result = host.request_graceful_exit("ETHUSDT")
            assert result == GracefulExitResult.NOT_APPLICABLE
            assert registry.get_state("ETHUSDT") == EngineState.ACTIVE

            # Shutdown handles NOT_APPLICABLE bypass cleanly
            report = host.shutdown_all()
            assert report.clean
            assert registry.get_state("ETHUSDT") == EngineState.STOPPED
            assert not host.is_live("ETHUSDT")
        finally:
            pass  # shutdown_all already called above


class TestShadowMode:
    def test_run_autonomous_shadow_mode_still_noops(self) -> None:
        """Shadow mode: build_runtime doesn't start any engines."""
        import argparse  # noqa: PLC0415

        from scripts import run_autonomous as mod  # noqa: PLC0415

        args = argparse.Namespace(
            symbols="BTCUSDT",
            blacklist="",
            cycle_interval_s=1.0,
            top_k=3,
            max_changes_per_cycle=1,
            execution_enabled=False,
            execution_ack=False,
            max_cycles=None,
            exchange_port="noop",
            mainnet=False,
            armed=False,
            max_notional_per_order="100",
            max_orders_per_run=500,
        )
        runtime = mod.build_runtime(args)
        host = runtime["host"]
        assert host.live_symbols == frozenset()
