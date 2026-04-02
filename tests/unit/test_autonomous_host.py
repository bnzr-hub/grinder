"""Tests for autonomous engine host lifecycle (ADR-147)."""

from __future__ import annotations

from grinder.execution_plane.registry import EngineRegistry, EngineState
from grinder.runtime.autonomous_host import AutonomousEngineHost

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TICK = 0


def _clock() -> float:
    global _TICK  # noqa: PLW0603
    _TICK += 1
    return float(_TICK)


def _reset_clock() -> None:
    global _TICK  # noqa: PLW0603
    _TICK = 0


class FakeEngine:
    """Minimal fake engine for testing."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.stopped = False
        self.graceful_exit_requested = False
        self.cleaned = False


def _build_host(
    *,
    factory_raises: bool = False,
    stop_returns: bool = True,
    graceful_exit_returns: bool = True,
) -> tuple[AutonomousEngineHost, EngineRegistry, dict[str, FakeEngine]]:
    """Build a host with injectable fakes."""
    _reset_clock()
    registry = EngineRegistry(_clock=_clock)
    engines: dict[str, FakeEngine] = {}

    def factory(symbol: str) -> FakeEngine:
        if factory_raises:
            raise RuntimeError("factory boom")
        eng = FakeEngine(symbol)
        engines[symbol] = eng
        return eng

    def stop_fn(symbol: str, engine_ref: FakeEngine) -> bool:
        engine_ref.stopped = True
        return stop_returns

    def cleanup_fn(symbol: str, engine_ref: FakeEngine) -> bool:
        engine_ref.cleaned = True
        return True

    def graceful_fn(symbol: str, engine_ref: FakeEngine) -> bool:
        engine_ref.graceful_exit_requested = True
        return graceful_exit_returns

    host = AutonomousEngineHost(
        registry=registry,
        engine_factory=factory,
        engine_stop_fn=stop_fn,
        engine_cleanup_fn=cleanup_fn,
        graceful_exit_fn=graceful_fn,
        _clock=_clock,
    )
    return host, registry, engines


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


class TestActivation:
    def test_activate_creates_real_handle_and_updates_registry(self) -> None:
        host, registry, engines = _build_host()
        ok = host.activate("BTCUSDT")
        assert ok
        assert "BTCUSDT" in engines
        assert host.is_live("BTCUSDT")
        assert registry.get_state("BTCUSDT") == EngineState.ACTIVE
        handle = registry.get("BTCUSDT")
        assert handle is not None
        assert handle.engine_ref is engines["BTCUSDT"]

    def test_duplicate_activate_is_safe(self) -> None:
        host, registry, _ = _build_host()
        host.activate("BTCUSDT")
        ok = host.activate("BTCUSDT")
        assert not ok  # Rejected, not crashed
        assert registry.get_state("BTCUSDT") == EngineState.ACTIVE

    def test_factory_failure_transitions_to_failed(self) -> None:
        host, registry, _ = _build_host(factory_raises=True)
        ok = host.activate("BTCUSDT")
        assert not ok
        assert registry.get_state("BTCUSDT") == EngineState.FAILED
        assert not host.is_live("BTCUSDT")


# ---------------------------------------------------------------------------
# Graceful exit
# ---------------------------------------------------------------------------


class TestGracefulExit:
    def test_graceful_exit_reaches_live_handle(self) -> None:
        host, registry, engines = _build_host()
        host.activate("BTCUSDT")
        ok = host.request_graceful_exit("BTCUSDT")
        assert ok
        assert engines["BTCUSDT"].graceful_exit_requested
        assert registry.get_state("BTCUSDT") == EngineState.GRACEFUL_EXIT

    def test_graceful_exit_missing_symbol_fails_cleanly(self) -> None:
        host, _, _ = _build_host()
        ok = host.request_graceful_exit("NOPE")
        assert not ok  # No crash, clean failure

    def test_graceful_exit_fn_failure_blocks_transition(self) -> None:
        host, registry, _ = _build_host(graceful_exit_returns=False)
        host.activate("BTCUSDT")
        ok = host.request_graceful_exit("BTCUSDT")
        assert not ok
        # Registry still ACTIVE — transition blocked
        assert registry.get_state("BTCUSDT") == EngineState.ACTIVE

    def test_graceful_exit_without_fn_fails_closed(self) -> None:
        """No graceful_exit_fn wired → fail closed, never registry-only transition."""
        _reset_clock()
        registry = EngineRegistry(_clock=_clock)
        engines: dict[str, FakeEngine] = {}

        def factory(symbol: str) -> FakeEngine:
            eng = FakeEngine(symbol)
            engines[symbol] = eng
            return eng

        def stop_fn(symbol: str, engine_ref: FakeEngine) -> bool:
            engine_ref.stopped = True
            return True

        host = AutonomousEngineHost(
            registry=registry,
            engine_factory=factory,
            engine_stop_fn=stop_fn,
            graceful_exit_fn=None,  # No graceful exit wired
            _clock=_clock,
        )
        host.activate("BTCUSDT")
        ok = host.request_graceful_exit("BTCUSDT")
        assert not ok
        assert registry.get_state("BTCUSDT") == EngineState.ACTIVE


# ---------------------------------------------------------------------------
# Deactivation
# ---------------------------------------------------------------------------


class TestDeactivation:
    def test_finalize_deactivation_removes_handle_and_updates_registry(self) -> None:
        host, registry, engines = _build_host()
        host.activate("BTCUSDT")
        host.request_graceful_exit("BTCUSDT")
        ok = host.finalize_deactivation("BTCUSDT")
        assert ok
        assert engines["BTCUSDT"].stopped
        assert engines["BTCUSDT"].cleaned
        assert not host.is_live("BTCUSDT")
        assert registry.get_state("BTCUSDT") == EngineState.STOPPED

    def test_finalize_missing_symbol_fails_cleanly(self) -> None:
        host, _, _ = _build_host()
        ok = host.finalize_deactivation("NOPE")
        assert not ok

    def test_stop_failure_transitions_to_failed(self) -> None:
        host, registry, _ = _build_host(stop_returns=False)
        host.activate("BTCUSDT")
        host.request_graceful_exit("BTCUSDT")
        ok = host.finalize_deactivation("BTCUSDT")
        assert not ok
        assert registry.get_state("BTCUSDT") == EngineState.FAILED
        assert not host.is_live("BTCUSDT")


# ---------------------------------------------------------------------------
# Registry/runtime coherence
# ---------------------------------------------------------------------------


class TestCoherence:
    def test_registry_and_host_remain_coherent(self) -> None:
        host, registry, _ = _build_host()
        # Initially empty
        assert host.live_symbols == frozenset()

        # After activate
        host.activate("BTCUSDT")
        assert "BTCUSDT" in host.live_symbols
        assert registry.get_state("BTCUSDT") == EngineState.ACTIVE

        # After graceful exit
        host.request_graceful_exit("BTCUSDT")
        assert "BTCUSDT" in host.live_symbols  # Still live during exit
        assert registry.get_state("BTCUSDT") == EngineState.GRACEFUL_EXIT

        # After deactivation
        host.finalize_deactivation("BTCUSDT")
        assert "BTCUSDT" not in host.live_symbols
        assert registry.get_state("BTCUSDT") == EngineState.STOPPED

    def test_no_live_engine_without_registry_visibility(self) -> None:
        """Invariant: every live engine has a corresponding registry entry."""
        host, registry, _ = _build_host()
        host.activate("ETHUSDT")
        state = registry.get_state("ETHUSDT")
        assert state in {EngineState.ACTIVATING, EngineState.ACTIVE}
        assert host.is_live("ETHUSDT")


# ---------------------------------------------------------------------------
# Shutdown all
# ---------------------------------------------------------------------------


class TestShutdownAll:
    def test_shutdown_all_stops_all_handles(self) -> None:
        host, _registry, engines = _build_host()
        host.activate("BTCUSDT")
        host.activate("ETHUSDT")
        report = host.shutdown_all()
        assert report.clean
        assert set(report.stopped) == {"BTCUSDT", "ETHUSDT"}
        assert report.failed == []
        assert host.live_symbols == frozenset()
        for eng in engines.values():
            assert eng.stopped

    def test_host_reports_partial_shutdown_failures(self) -> None:
        host, _registry, _engines = _build_host(stop_returns=False)
        host.activate("BTCUSDT")
        report = host.shutdown_all()
        assert not report.clean
        assert "BTCUSDT" in report.failed

    def test_shutdown_all_deterministic_order(self) -> None:
        """Symbols processed in sorted order."""
        host, _, _ = _build_host()
        host.activate("ZZZUSDT")
        host.activate("AAAUSDT")
        report = host.shutdown_all()
        # Stopped in alphabetical order
        assert report.stopped == ["AAAUSDT", "ZZZUSDT"]

    def test_shutdown_empty_host(self) -> None:
        host, _, _ = _build_host()
        report = host.shutdown_all()
        assert report.clean
        assert report.stopped == []
        assert report.failed == []
