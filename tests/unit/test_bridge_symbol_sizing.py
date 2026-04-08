"""Tests for per-symbol tuned order size wiring in LiveEngineBridge."""

from __future__ import annotations

from decimal import Decimal

from grinder.runtime.live_engine_bridge import BridgeConfig, LiveEngineBridge


class TestSymbolSizing:
    def test_set_symbol_size_stored(self) -> None:
        bridge = LiveEngineBridge(config=BridgeConfig())
        bridge.set_symbol_size("DRIFTUSDT", "124")
        assert bridge._symbol_sizes["DRIFTUSDT"] == "124"

    def test_set_symbol_size_overrides_default(self) -> None:
        """Tuned size takes precedence over BridgeConfig.size_per_level."""
        bridge = LiveEngineBridge(config=BridgeConfig(size_per_level="0.001"))
        bridge.set_symbol_size("DRIFTUSDT", "124")
        # The bridge should use "124" for DRIFTUSDT, not "0.001"
        assert bridge._symbol_sizes.get("DRIFTUSDT") == "124"
        # Default still applies for untuned symbols
        assert bridge._symbol_sizes.get("BTCUSDT") is None

    def test_multiple_symbols_independent(self) -> None:
        bridge = LiveEngineBridge(config=BridgeConfig())
        bridge.set_symbol_size("DRIFTUSDT", "124")
        bridge.set_symbol_size("BTCUSDT", "0.002")
        assert bridge._symbol_sizes["DRIFTUSDT"] == "124"
        assert bridge._symbol_sizes["BTCUSDT"] == "0.002"

    def test_deterministic_same_tuning_same_size(self) -> None:
        """Same set_symbol_size calls produce same internal state."""
        b1 = LiveEngineBridge(config=BridgeConfig())
        b2 = LiveEngineBridge(config=BridgeConfig())
        b1.set_symbol_size("X", "42")
        b2.set_symbol_size("X", "42")
        assert b1._symbol_sizes == b2._symbol_sizes

    def test_set_symbol_risk_caps_stored(self) -> None:
        bridge = LiveEngineBridge(config=BridgeConfig())
        bridge.set_symbol_risk_caps(
            "DRIFTUSDT",
            max_inventory_notional_usd=Decimal("250"),
            max_order_notional_usd=Decimal("250"),
        )
        assert bridge._symbol_max_inventory_notionals["DRIFTUSDT"] == Decimal("250")
        assert bridge._symbol_max_order_notionals["DRIFTUSDT"] == Decimal("250")
