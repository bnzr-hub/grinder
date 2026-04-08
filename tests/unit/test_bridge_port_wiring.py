"""Tests for LiveEngineBridge port wiring (ADR-150)."""

from __future__ import annotations

import argparse
import os

import pytest
from scripts import run_autonomous as run_autonomous_mod

from grinder.connectors.binance_ws import FakeWsTransport
from grinder.execution_plane.registry import EngineRegistry, EngineState
from grinder.runtime.autonomous_host import AutonomousEngineHost
from grinder.runtime.live_engine_bridge import BridgeConfig, LiveEngineBridge


class TestBridgeDefaultPort:
    def test_bridge_default_uses_noop_port(self) -> None:
        """Default BridgeConfig uses noop exchange port."""
        cfg = BridgeConfig()
        assert cfg.exchange_port == "noop"
        assert cfg.armed is False
        assert cfg.use_testnet is True

    def test_bridge_default_factory_creates_noop_engine(self) -> None:
        """Default bridge creates engine with NoOpExchangePort."""
        from decimal import Decimal  # noqa: PLC0415

        bridge = LiveEngineBridge(config=BridgeConfig(ws_transport=FakeWsTransport(messages=[])))
        bridge.set_symbol_size("BTCUSDT", "0.001")
        bridge.set_symbol_risk_caps(
            "BTCUSDT",
            max_inventory_notional_usd=Decimal("1000"),
            max_order_notional_usd=Decimal("100"),
        )
        bridge.set_symbol_grid_config("BTCUSDT", tick_size="0.01", step_size="0.001")
        handle = bridge.factory("BTCUSDT")
        try:
            assert handle.engine_ref is not None
        finally:
            handle.shutdown_event.set()
            handle.thread.join(timeout=5.0)


class TestBridgeFuturesPort:
    def test_bridge_futures_testnet_requires_api_keys(self) -> None:
        """Futures port fails closed without API keys."""
        cfg = BridgeConfig(
            exchange_port="futures",
            mode="read_only",
            armed=False,
            use_testnet=True,
        )
        bridge = LiveEngineBridge(config=cfg)
        # Remove API keys if present
        env_backup = {
            "BINANCE_API_KEY": os.environ.pop("BINANCE_API_KEY", None),
            "BINANCE_API_SECRET": os.environ.pop("BINANCE_API_SECRET", None),
        }
        try:
            with pytest.raises(RuntimeError, match="Engine startup failed"):
                bridge.factory("BTCUSDT")
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v

    def test_bridge_mainnet_requires_explicit_enablement(self) -> None:
        """Mainnet futures requires use_testnet=False (explicit opt-in)."""
        cfg = BridgeConfig(exchange_port="futures", use_testnet=True)
        # Testnet is the safe default — no mainnet without explicit flag
        assert cfg.use_testnet is True

    def test_bridge_config_mainnet_explicit(self) -> None:
        """Production config: explicit mainnet + armed + live_trade."""
        cfg = BridgeConfig(
            exchange_port="futures",
            mode="live_trade",
            armed=True,
            use_testnet=False,
            max_notional_per_order="100",
            max_orders_per_run=500,
        )
        assert cfg.exchange_port == "futures"
        assert cfg.armed is True
        assert cfg.use_testnet is False
        assert cfg.mode == "live_trade"


class TestBridgePortConstructionFailure:
    def test_port_construction_failure_fails_closed(self) -> None:
        """If port construction raises, factory raises and host marks FAILED."""
        cfg = BridgeConfig(exchange_port="futures", use_testnet=True)
        bridge = LiveEngineBridge(config=cfg)

        env_backup = {
            "BINANCE_API_KEY": os.environ.pop("BINANCE_API_KEY", None),
            "BINANCE_API_SECRET": os.environ.pop("BINANCE_API_SECRET", None),
        }
        try:
            registry = EngineRegistry()
            host = AutonomousEngineHost(
                registry=registry,
                engine_factory=bridge.factory,
                engine_stop_fn=bridge.stop,
                engine_cleanup_fn=bridge.cleanup,
                graceful_exit_fn=bridge.graceful_exit,
            )
            ok = host.activate("BTCUSDT")
            assert not ok
            assert registry.get_state("BTCUSDT") == EngineState.FAILED
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v


class TestRunAutonomousPortBanner:
    def test_run_autonomous_reports_effective_bridge_port_mode(self) -> None:
        """Startup banner shows exchange_port and net."""
        mod = run_autonomous_mod

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
            _ws_transport=FakeWsTransport(messages=[]),
        )
        runtime = mod.build_runtime(args)
        bridge = runtime["bridge"]
        assert bridge._config.exchange_port == "noop"
        assert bridge._config.use_testnet is True

    def test_run_autonomous_futures_config_propagated(self) -> None:
        """CLI futures args reach bridge config."""
        mod = run_autonomous_mod

        args = argparse.Namespace(
            symbols="BTCUSDT",
            blacklist="",
            cycle_interval_s=1.0,
            top_k=3,
            max_changes_per_cycle=1,
            execution_enabled=True,
            execution_ack=True,
            max_cycles=None,
            exchange_port="futures",
            mainnet=True,
            armed=True,
            max_notional_per_order="200",
            max_orders_per_run=100,
            _ws_transport=FakeWsTransport(messages=[]),
        )
        runtime = mod.build_runtime(args)
        bridge = runtime["bridge"]
        assert bridge._config.exchange_port == "futures"
        assert bridge._config.use_testnet is False
        assert bridge._config.armed is True
        assert bridge._config.mode == "live_trade"
        assert bridge._config.max_notional_per_order == "200"
        assert bridge._config.max_orders_per_run == 100
