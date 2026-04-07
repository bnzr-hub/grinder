"""Integration test: bridge-wired regime publication through real engine path.

Proves the full wiring: bridge constructs engine with FeatureEngine →
engine.process_snapshot() → classify_regime() → SharedRegimeRegistry,
then bridge cleanup removes the symbol from the registry.

Uses bridge.build_engine_only() — the same construction path as production.
If bridge stops injecting FeatureEngine, these tests fail.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from grinder.contracts import Snapshot
from grinder.controller.regime import Regime, RegimeReason
from grinder.risk.regime_registry import SharedRegimeRegistry
from grinder.runtime.live_engine_bridge import BridgeConfig, EngineHandle, LiveEngineBridge


def _make_snapshot(symbol: str, ts_ms: int, price: Decimal) -> Snapshot:
    """Create a minimal deterministic snapshot."""
    spread = Decimal("0.01")
    return Snapshot(
        ts=ts_ms,
        symbol=symbol,
        bid_price=price - spread,
        ask_price=price + spread,
        bid_qty=Decimal("10"),
        ask_qty=Decimal("10"),
        last_price=price,
        last_qty=Decimal("1"),
    )


def _build_bridge_and_engine(
    symbol: str, registry: SharedRegimeRegistry
) -> tuple[LiveEngineBridge, Any]:
    """Build engine through the real bridge construction path."""
    bridge = LiveEngineBridge(config=BridgeConfig())
    bridge.set_regime_registry(registry)
    engine, _port = bridge.build_engine_only(symbol)
    return bridge, engine


class TestRegimeBridgeWiring:
    def test_engine_publishes_regime_after_warmup(self) -> None:
        """Bridge-constructed engine publishes real regime after FeatureEngine warmup."""
        symbol = "TESTUSDT"
        registry = SharedRegimeRegistry()
        _bridge, engine = _build_bridge_and_engine(symbol, registry)

        assert len(registry) == 0

        # Feed enough snapshots to warm up FeatureEngine (default: 60s bars, ATR period 14).
        # Need 15+ bars → 15 ticks at 60s intervals.
        base_ts = 1_000_000_000
        price = Decimal("100.00")
        for i in range(20):
            snap = _make_snapshot(symbol, base_ts + i * 60_000, price + Decimal(str(i * 0.1)))
            engine.process_snapshot(snap)

        # After warmup, regime should be published
        assert len(registry) > 0, "Registry should have published regime after warmup"
        entry = registry.get(symbol)
        assert entry is not None
        assert entry.symbol == symbol
        assert isinstance(entry.regime, Regime)
        assert entry.confidence > 0

    def test_cleanup_removes_regime_from_registry(self) -> None:
        """Bridge cleanup removes symbol from registry (noop port path)."""
        symbol = "TESTUSDT"
        registry = SharedRegimeRegistry()
        bridge = LiveEngineBridge(config=BridgeConfig())
        bridge.set_regime_registry(registry)

        # Manually publish a regime (simulating what engine would do)
        registry.publish(symbol, Regime.RANGE, RegimeReason.DEFAULT, 80)
        assert len(registry) == 1

        # Cleanup with noop handle — regime should still be removed
        handle = EngineHandle(
            symbol=symbol,
            thread=None,  # type: ignore[arg-type]
            shutdown_event=None,  # type: ignore[arg-type]
        )
        bridge.cleanup(symbol, handle)
        assert len(registry) == 0, "Cleanup should remove symbol from registry"

    def test_full_lifecycle_publish_then_cleanup(self) -> None:
        """Full lifecycle: bridge build → warmup → regime publish → cleanup → empty."""
        symbol = "TESTUSDT"
        registry = SharedRegimeRegistry()
        bridge, engine = _build_bridge_and_engine(symbol, registry)

        # Warmup
        base_ts = 1_000_000_000
        price = Decimal("100.00")
        for i in range(20):
            snap = _make_snapshot(symbol, base_ts + i * 60_000, price + Decimal(str(i * 0.1)))
            engine.process_snapshot(snap)

        assert len(registry) > 0

        # Cleanup via bridge
        handle = EngineHandle(
            symbol=symbol,
            thread=None,  # type: ignore[arg-type]
            shutdown_event=None,  # type: ignore[arg-type]
        )
        bridge.cleanup(symbol, handle)
        assert len(registry) == 0

    def test_no_regime_without_feature_engine(self) -> None:
        """Engine without FeatureEngine never publishes regime — regression guard."""
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.execution.port import NoOpExchangePort  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415
        from grinder.paper.engine import PaperEngine  # noqa: PLC0415

        symbol = "TESTUSDT"
        registry = SharedRegimeRegistry()

        # Construct engine WITHOUT FeatureEngine — like old bridge before #615
        engine = LiveEngineV0(
            paper_engine=PaperEngine(spacing_bps=10.0, levels=5, size_per_level=Decimal("0.001")),
            exchange_port=NoOpExchangePort(),
            config=LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY),
            operator_symbols=[symbol],
            regime_registry=registry,
            # NO feature_engine — intentionally omitted
        )

        base_ts = 1_000_000_000
        price = Decimal("100.00")
        for i in range(20):
            snap = _make_snapshot(symbol, base_ts + i * 60_000, price)
            engine.process_snapshot(snap)

        assert len(registry) == 0, "Without FeatureEngine, regime should never publish"
