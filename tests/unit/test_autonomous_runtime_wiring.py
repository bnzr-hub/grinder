"""Tests for run_autonomous.py wiring to AutonomousEngineHost (ADR-148/149).

Now that build_runtime creates real LiveEngineBridge-backed engines,
lifecycle tests that call host.activate() start real engine threads.
All such tests must cleanup via host.shutdown_all() in finally blocks.
"""

from __future__ import annotations

import argparse

# Import the module-level helpers and build_runtime
from scripts import run_autonomous as run_autonomous_mod

from grinder.connectors.binance_ws import FakeWsTransport
from grinder.execution_plane.registry import EngineState
from grinder.runtime.autonomous_host import AutonomousEngineHost


def _default_args(**overrides: object) -> argparse.Namespace:
    """Build args with safe defaults."""
    defaults = {
        "symbols": "BTCUSDT",
        "blacklist": "",
        "cycle_interval_s": 1.0,
        "top_k": 3,
        "max_changes_per_cycle": 1,
        "execution_enabled": False,
        "execution_ack": False,
        "max_cycles": None,
        "exchange_port": "noop",
        "mainnet": False,
        "armed": False,
        "max_notional_per_order": "100",
        "max_orders_per_run": 500,
        "_ws_transport": FakeWsTransport(messages=[]),
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _register_tuned_symbols(bridge: object, *symbols: str) -> None:
    """Register tuned size + risk caps so bridge allows autonomous activation."""
    from decimal import Decimal  # noqa: PLC0415

    for sym in symbols:
        bridge.set_symbol_size(sym, "0.001")  # type: ignore[attr-defined]
        bridge.set_symbol_risk_caps(  # type: ignore[attr-defined]
            sym,
            max_inventory_notional_usd=Decimal("1000"),
            max_order_notional_usd=Decimal("100"),
        )
        bridge.set_symbol_grid_config(sym, tick_size="0.10", step_size="0.001")  # type: ignore[attr-defined]


class TestBuildRuntime:
    def test_run_autonomous_builds_real_host(self) -> None:
        """build_runtime returns a dict with a real AutonomousEngineHost."""
        args = _default_args()
        runtime = run_autonomous_mod.build_runtime(args)
        assert "host" in runtime
        assert isinstance(runtime["host"], AutonomousEngineHost)
        assert "bridge" in runtime

    def test_coordinator_bindings_use_host_methods(self) -> None:
        """Coordinator ceremony fns are bound to host lifecycle methods."""
        args = _default_args()
        runtime = run_autonomous_mod.build_runtime(args)
        coordinator = runtime["coordinator"]
        host = runtime["host"]
        assert coordinator.activate_fn == host.activate
        assert coordinator.graceful_exit_fn == host.request_graceful_exit
        assert coordinator.deactivate_fn == host.finalize_deactivation


class TestShadowMode:
    def test_execution_disabled_keeps_shadow_only_behavior(self) -> None:
        """Shadow mode: no real engine lifecycle calls."""
        args = _default_args(execution_enabled=False)
        runtime = run_autonomous_mod.build_runtime(args)
        loop = runtime["loop"]
        assert not loop.execution_enabled
        host = runtime["host"]
        assert host.live_symbols == frozenset()


class TestHostLifecycleIntegration:
    def test_enabled_and_acked_activation_reaches_host(self) -> None:
        """With execution enabled + ACK, activate_fn starts real engine thread."""
        args = _default_args(execution_enabled=True, execution_ack=True)
        runtime = run_autonomous_mod.build_runtime(args)
        host = runtime["host"]
        registry = runtime["registry"]
        _register_tuned_symbols(runtime["bridge"], "BTCUSDT")

        try:
            ok = host.activate("BTCUSDT")
            assert ok
            assert host.is_live("BTCUSDT")
            assert registry.get_state("BTCUSDT") == EngineState.ACTIVE
        finally:
            host.shutdown_all()


class TestRegistryCoherence:
    def test_activation_creates_active_registry_entry(self) -> None:
        """Activation sets registry to ACTIVE with a real engine handle."""
        args = _default_args(execution_enabled=True, execution_ack=True)
        runtime = run_autonomous_mod.build_runtime(args)
        host = runtime["host"]
        registry = runtime["registry"]
        _register_tuned_symbols(runtime["bridge"], "ETHUSDT")
        try:
            host.activate("ETHUSDT")
            assert host.is_live("ETHUSDT")
            assert registry.get_state("ETHUSDT") == EngineState.ACTIVE
            handle = registry.get("ETHUSDT")
            assert handle is not None
            assert handle.engine_ref is not None
        finally:
            host.shutdown_all()


class TestFailureSurfacing:
    def test_host_failure_surfaces_cleanly_in_runtime(self) -> None:
        """Duplicate activation and missing-symbol graceful exit fail cleanly."""
        args = _default_args(execution_enabled=True, execution_ack=True)
        runtime = run_autonomous_mod.build_runtime(args)
        host = runtime["host"]
        _register_tuned_symbols(runtime["bridge"], "BTCUSDT")

        try:
            host.activate("BTCUSDT")
            assert host.is_live("BTCUSDT")
            # Duplicate activation fails cleanly
            ok = host.activate("BTCUSDT")
            assert not ok
            # Missing symbol graceful exit fails cleanly
            from grinder.runtime.autonomous_host import GracefulExitResult  # noqa: PLC0415

            result = host.request_graceful_exit("NONEXISTENT")
            assert result == GracefulExitResult.FAILED
        finally:
            host.shutdown_all()
