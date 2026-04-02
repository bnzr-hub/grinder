"""Tests for run_autonomous.py wiring to AutonomousEngineHost (ADR-148)."""

from __future__ import annotations

import argparse

# Import the module-level helpers and build_runtime
from scripts import run_autonomous as run_autonomous_mod

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
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestBuildRuntime:
    def test_run_autonomous_builds_real_host(self) -> None:
        """build_runtime returns a dict with a real AutonomousEngineHost."""
        args = _default_args()
        runtime = run_autonomous_mod.build_runtime(args)
        assert "host" in runtime
        assert isinstance(runtime["host"], AutonomousEngineHost)

    def test_coordinator_bindings_use_host_methods(self) -> None:
        """Coordinator ceremony fns are bound to host lifecycle methods."""
        args = _default_args()
        runtime = run_autonomous_mod.build_runtime(args)
        coordinator = runtime["coordinator"]
        host = runtime["host"]
        # Verify the coordinator's fns are the host's methods
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
        """With execution enabled + ACK, activate_fn hits real host."""
        args = _default_args(execution_enabled=True, execution_ack=True)
        runtime = run_autonomous_mod.build_runtime(args)
        host = runtime["host"]
        registry = runtime["registry"]

        ok = host.activate("BTCUSDT")
        assert ok
        assert host.is_live("BTCUSDT")
        assert registry.get_state("BTCUSDT") == EngineState.ACTIVE

    def test_graceful_exit_reaches_host(self) -> None:
        args = _default_args(execution_enabled=True, execution_ack=True)
        runtime = run_autonomous_mod.build_runtime(args)
        host = runtime["host"]
        registry = runtime["registry"]

        host.activate("BTCUSDT")
        ok = host.request_graceful_exit("BTCUSDT")
        assert ok
        assert registry.get_state("BTCUSDT") == EngineState.GRACEFUL_EXIT

    def test_deactivation_reaches_host(self) -> None:
        args = _default_args(execution_enabled=True, execution_ack=True)
        runtime = run_autonomous_mod.build_runtime(args)
        host = runtime["host"]
        registry = runtime["registry"]

        host.activate("BTCUSDT")
        host.request_graceful_exit("BTCUSDT")
        ok = host.finalize_deactivation("BTCUSDT")
        assert ok
        assert not host.is_live("BTCUSDT")
        assert registry.get_state("BTCUSDT") == EngineState.STOPPED

    def test_shutdown_calls_host_shutdown_all(self) -> None:
        args = _default_args(execution_enabled=True, execution_ack=True)
        runtime = run_autonomous_mod.build_runtime(args)
        host = runtime["host"]

        host.activate("BTCUSDT")
        host.activate("ETHUSDT")
        report = host.shutdown_all()
        assert report.clean
        assert host.live_symbols == frozenset()


class TestRegistryCoherence:
    def test_registry_and_host_stay_coherent_during_lifecycle(self) -> None:
        """Full lifecycle: activate → graceful → deactivate stays coherent."""
        args = _default_args(execution_enabled=True, execution_ack=True)
        runtime = run_autonomous_mod.build_runtime(args)
        host = runtime["host"]
        registry = runtime["registry"]

        # Activate
        host.activate("DRIFTUSDT")
        assert host.is_live("DRIFTUSDT")
        assert registry.get_state("DRIFTUSDT") == EngineState.ACTIVE

        # Graceful exit
        host.request_graceful_exit("DRIFTUSDT")
        assert host.is_live("DRIFTUSDT")  # Still live during exit
        assert registry.get_state("DRIFTUSDT") == EngineState.GRACEFUL_EXIT

        # Deactivate
        host.finalize_deactivation("DRIFTUSDT")
        assert not host.is_live("DRIFTUSDT")
        assert registry.get_state("DRIFTUSDT") == EngineState.STOPPED


class TestFailureSurfacing:
    def test_host_failure_surfaces_cleanly_in_runtime(self) -> None:
        """If engine factory fails, host reports failure honestly."""
        args = _default_args(execution_enabled=True, execution_ack=True)
        runtime = run_autonomous_mod.build_runtime(args)
        host = runtime["host"]

        # Activate one successfully
        host.activate("BTCUSDT")
        assert host.is_live("BTCUSDT")

        # Duplicate activation fails cleanly
        ok = host.activate("BTCUSDT")
        assert not ok

        # Missing symbol graceful exit fails cleanly
        ok = host.request_graceful_exit("NONEXISTENT")
        assert not ok
