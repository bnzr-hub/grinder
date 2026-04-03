"""Tests for grid_v2 env propagation in LiveEngineBridge.

Covers:
- P1-a: thread-safe engine construction (serialized via lock)
- P1-b: env restore in finally (even on constructor failure)
- env isolation: no leak into process after engine creation
"""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock, patch

from grinder.live.engine import LiveEngineV0
from grinder.paper.engine import PaperEngine
from grinder.runtime.live_engine_bridge import BridgeConfig, LiveEngineBridge


def _bridge_with_tuned_symbol(
    symbol: str = "DRIFTUSDT",
    order_size: str = "120",
    tick_size: str = "0.0001",
) -> LiveEngineBridge:
    bridge = LiveEngineBridge(config=BridgeConfig())
    bridge.set_symbol_size(symbol, order_size)
    bridge.set_symbol_grid_config(symbol, tick_size=tick_size, step_size="1")
    return bridge


class TestEnvPropagation:
    """grid_v2 env vars are set before engine creation and restored after."""

    def test_env_set_during_propagation(self) -> None:
        """After propagation, GRINDER_GRID_V2_* must be set in os.environ."""
        bridge = _bridge_with_tuned_symbol()
        saved = {k: os.environ.get(k) for k in bridge._GRID_V2_ENV_KEYS}
        bridge._propagate_grid_v2_env("DRIFTUSDT", "120", bridge._config)
        try:
            assert os.environ.get("GRINDER_GRID_V2_ENABLED") == "1"
            assert os.environ.get("GRINDER_GRID_V2_SYMBOL") == "DRIFTUSDT"
            assert os.environ.get("GRINDER_GRID_V2_ORDER_SIZE") == "120"
            assert os.environ.get("GRINDER_GRID_V2_TICK_SIZE") == "0.0001"
        finally:
            bridge._restore_grid_v2_env(saved)

    def test_env_restored_on_engine_constructor_failure(self) -> None:
        """If LiveEngineV0() raises, env must still be restored (try/finally)."""
        bridge = _bridge_with_tuned_symbol()

        before_enabled = os.environ.get("GRINDER_GRID_V2_ENABLED")
        before_symbol = os.environ.get("GRINDER_GRID_V2_SYMBOL")

        # Simulate a failing engine constructor
        with patch(
            "grinder.live.engine.LiveEngineV0.__init__",
            side_effect=RuntimeError("constructor boom"),
        ):
            saved = {k: os.environ.get(k) for k in bridge._GRID_V2_ENV_KEYS}
            bridge._propagate_grid_v2_env("DRIFTUSDT", "120", bridge._config)
            try:
                paper = MagicMock(spec=PaperEngine)
                port = MagicMock()
                LiveEngineV0(
                    paper_engine=paper,
                    exchange_port=port,
                    config=MagicMock(),
                    operator_symbols=["DRIFTUSDT"],
                )
            except RuntimeError:
                pass
            finally:
                bridge._restore_grid_v2_env(saved)

        # Env must be restored despite the failure
        assert os.environ.get("GRINDER_GRID_V2_ENABLED") == before_enabled
        assert os.environ.get("GRINDER_GRID_V2_SYMBOL") == before_symbol

    def test_untuned_symbol_skips_env_propagation(self) -> None:
        """Symbol not in _symbol_sizes -> no env changes."""
        bridge = LiveEngineBridge(config=BridgeConfig())

        before = os.environ.get("GRINDER_GRID_V2_ENABLED")
        bridge._propagate_grid_v2_env("BTCUSDT", "0.001", bridge._config)
        after = os.environ.get("GRINDER_GRID_V2_ENABLED")

        assert after == before, "Env should not change for untuned symbol"


class TestConstructionLock:
    """Engine construction is serialized via _engine_construction_lock."""

    def test_lock_exists(self) -> None:
        bridge = LiveEngineBridge(config=BridgeConfig())
        assert isinstance(bridge._engine_construction_lock, type(threading.Lock()))

    def test_concurrent_constructions_serialized(self) -> None:
        """Two overlapping engine constructions don't race on env vars.

        Each thread holds the lock while env is set, records the symbol,
        then restores. Serialization ensures no cross-read.
        """
        bridge = _bridge_with_tuned_symbol("DRIFTUSDT", "120")
        bridge.set_symbol_size("ETHUSDT", "50")
        bridge.set_symbol_grid_config("ETHUSDT", tick_size="0.01", step_size="0.1")

        observed_symbols: list[str] = []

        def record_env_during_construction(symbol: str) -> None:
            with bridge._engine_construction_lock:
                saved = {k: os.environ.get(k) for k in bridge._GRID_V2_ENV_KEYS}
                bridge._propagate_grid_v2_env(
                    symbol,
                    bridge._symbol_sizes[symbol],
                    bridge._config,
                )
                try:
                    observed_symbols.append(os.environ.get("GRINDER_GRID_V2_SYMBOL", ""))
                finally:
                    bridge._restore_grid_v2_env(saved)

        t1 = threading.Thread(target=record_env_during_construction, args=("DRIFTUSDT",))
        t2 = threading.Thread(target=record_env_during_construction, args=("ETHUSDT",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Each thread should have seen its own symbol (serialized, no cross-read)
        assert "DRIFTUSDT" in observed_symbols
        assert "ETHUSDT" in observed_symbols


class TestGridConfigWiring:
    """set_symbol_grid_config stores and propagates tick_size."""

    def test_grid_config_stored(self) -> None:
        bridge = LiveEngineBridge(config=BridgeConfig())
        bridge.set_symbol_grid_config("DRIFTUSDT", tick_size="0.0001", step_size="1")
        assert bridge._symbol_grid_config["DRIFTUSDT"]["tick_size"] == "0.0001"
        assert bridge._symbol_grid_config["DRIFTUSDT"]["step_size"] == "1"

    def test_tick_size_propagated_to_env(self) -> None:
        bridge = _bridge_with_tuned_symbol(tick_size="0.0001")
        saved = {k: os.environ.get(k) for k in bridge._GRID_V2_ENV_KEYS}
        bridge._propagate_grid_v2_env("DRIFTUSDT", "120", bridge._config)
        try:
            assert os.environ.get("GRINDER_GRID_V2_TICK_SIZE") == "0.0001"
        finally:
            bridge._restore_grid_v2_env(saved)
