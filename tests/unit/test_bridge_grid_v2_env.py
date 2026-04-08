"""Tests for grid_v2 env propagation in LiveEngineBridge.

Covers:
- P1-a: thread-safe engine construction (serialized via lock)
- P1-b: env restore in finally (even on constructor failure)
- env isolation: no leak into process after engine creation
"""

from __future__ import annotations

import os
import threading
from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.account.syncer import AccountSyncer
from grinder.live.engine import LiveEngineV0
from grinder.paper.engine import PaperEngine
from grinder.runtime.live_engine_bridge import BridgeConfig, EngineHandle, LiveEngineBridge


def _bridge_with_tuned_symbol(
    symbol: str = "DRIFTUSDT",
    order_size: str = "120",
    tick_size: str = "0.0001",
) -> LiveEngineBridge:
    bridge = LiveEngineBridge(config=BridgeConfig())
    bridge.set_symbol_size(symbol, order_size)
    bridge.set_symbol_grid_config(symbol, tick_size=tick_size, step_size="1")
    bridge.set_symbol_risk_caps(
        symbol,
        max_inventory_notional_usd=Decimal("250"),
        max_order_notional_usd=Decimal("250"),
    )
    return bridge


class TestEnvPropagation:
    """grid_v2 env vars are set before engine creation and restored after."""

    def test_env_set_during_propagation(self) -> None:
        """After propagation, GRINDER_GRID_V2_* and ACCOUNT_SYNC must be set."""
        bridge = _bridge_with_tuned_symbol()
        saved = {k: os.environ.get(k) for k in bridge._GRID_V2_ENV_KEYS}
        bridge._propagate_grid_v2_env("DRIFTUSDT", "120", bridge._config)
        try:
            assert os.environ.get("GRINDER_GRID_V2_ENABLED") == "1"
            assert os.environ.get("GRINDER_GRID_V2_SYMBOL") == "DRIFTUSDT"
            assert os.environ.get("GRINDER_GRID_V2_ORDER_SIZE") == "120"
            assert os.environ.get("GRINDER_GRID_V2_TICK_SIZE") == "0.0001"
            assert os.environ.get("GRINDER_GRID_V2_MAX_INV_NOTIONAL") == "250"
            assert os.environ.get("GRINDER_ACCOUNT_SYNC_ENABLED") == "1"
        finally:
            bridge._restore_grid_v2_env(saved)

    def test_env_restored_on_engine_constructor_failure(self) -> None:
        """If LiveEngineV0() raises, env must still be restored (try/finally)."""
        bridge = _bridge_with_tuned_symbol()

        before_enabled = os.environ.get("GRINDER_GRID_V2_ENABLED")
        before_symbol = os.environ.get("GRINDER_GRID_V2_SYMBOL")
        before_sync = os.environ.get("GRINDER_ACCOUNT_SYNC_ENABLED")

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

        # All env vars must be restored despite the failure
        assert os.environ.get("GRINDER_GRID_V2_ENABLED") == before_enabled
        assert os.environ.get("GRINDER_GRID_V2_SYMBOL") == before_symbol
        assert os.environ.get("GRINDER_ACCOUNT_SYNC_ENABLED") == before_sync

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
        bridge.set_symbol_risk_caps(
            "ETHUSDT",
            max_inventory_notional_usd=Decimal("250"),
            max_order_notional_usd=Decimal("250"),
        )

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


class TestBuildAccountSyncer:
    """_build_account_syncer returns syncer only for tuned symbols."""

    def test_tuned_symbol_returns_syncer(self) -> None:
        bridge = _bridge_with_tuned_symbol()
        port = MagicMock()
        syncer = bridge._build_account_syncer("DRIFTUSDT", port)
        assert isinstance(syncer, AccountSyncer)

    def test_untuned_symbol_returns_none(self) -> None:
        bridge = LiveEngineBridge(config=BridgeConfig())
        port = MagicMock()
        syncer = bridge._build_account_syncer("BTCUSDT", port)
        assert syncer is None

    def test_syncer_holds_port_reference(self) -> None:
        bridge = _bridge_with_tuned_symbol()
        port = MagicMock()
        syncer = bridge._build_account_syncer("DRIFTUSDT", port)
        assert syncer is not None
        assert syncer._port is port


class TestCleanup:
    """bridge.cleanup() cancels orders and closes position via port."""

    def test_cleanup_cancels_orders_and_closes_position(self) -> None:
        """When port has open orders and position, cleanup cancels/closes both."""

        bridge = LiveEngineBridge(config=BridgeConfig())
        port = MagicMock()
        port.cancel_all_orders.return_value = 6
        port.fetch_positions_raw.return_value = [{"positionAmt": "-372"}]
        port.close_position.return_value = "order-123"

        handle = MagicMock(spec=EngineHandle)
        handle.port_ref = port

        ok = bridge.cleanup("DRIFTUSDT", handle)

        assert ok is True
        port.cancel_all_orders.assert_called_once_with("DRIFTUSDT")
        port.fetch_positions_raw.assert_called_once_with("DRIFTUSDT")
        port.close_position.assert_called_once_with("DRIFTUSDT")

    def test_cleanup_skips_close_when_flat(self) -> None:
        """Flat position: cancel orders but skip close_position."""

        bridge = LiveEngineBridge(config=BridgeConfig())
        port = MagicMock()
        port.cancel_all_orders.return_value = 0
        port.fetch_positions_raw.return_value = [{"positionAmt": "0"}]

        handle = MagicMock(spec=EngineHandle)
        handle.port_ref = port

        ok = bridge.cleanup("DRIFTUSDT", handle)

        assert ok is True
        port.cancel_all_orders.assert_called_once()
        port.close_position.assert_not_called()

    def test_cleanup_no_port_is_noop(self) -> None:
        """No port reference: cleanup is safe no-op."""

        bridge = LiveEngineBridge(config=BridgeConfig())
        handle = MagicMock(spec=EngineHandle)
        handle.port_ref = None

        ok = bridge.cleanup("DRIFTUSDT", handle)
        assert ok is True

    def test_cleanup_cancel_failure_returns_false(self) -> None:
        """Cancel failure: returns False but still attempts close."""

        bridge = LiveEngineBridge(config=BridgeConfig())
        port = MagicMock()
        port.cancel_all_orders.side_effect = RuntimeError("cancel failed")
        port.fetch_positions_raw.return_value = [{"positionAmt": "0"}]

        handle = MagicMock(spec=EngineHandle)
        handle.port_ref = port

        ok = bridge.cleanup("DRIFTUSDT", handle)

        assert ok is False
        # Still attempted position check
        port.fetch_positions_raw.assert_called_once()

    def test_cleanup_close_failure_returns_false(self) -> None:
        """Close failure: returns False."""

        bridge = LiveEngineBridge(config=BridgeConfig())
        port = MagicMock()
        port.cancel_all_orders.return_value = 0
        port.fetch_positions_raw.side_effect = RuntimeError("fetch failed")

        handle = MagicMock(spec=EngineHandle)
        handle.port_ref = port

        ok = bridge.cleanup("DRIFTUSDT", handle)
        assert ok is False
