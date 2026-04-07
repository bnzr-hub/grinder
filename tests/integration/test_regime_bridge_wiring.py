"""Integration test: bridge-wired regime publication through real engine path.

Proves the full wiring: FeatureEngine → classify_regime → SharedRegimeRegistry
through the real LiveEngineV0.process_snapshot() path, then cleanup removes
the symbol from the registry.

Uses a short bar_interval to avoid needing 15+ minutes of synthetic data.
"""

from __future__ import annotations

from decimal import Decimal

from grinder.contracts import Snapshot
from grinder.controller.regime import Regime, RegimeReason
from grinder.features.engine import FeatureEngine, FeatureEngineConfig
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import LiveEngineV0
from grinder.paper.engine import PaperEngine
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


def _build_engine_with_regime(
    symbol: str,
    registry: SharedRegimeRegistry,
    bar_interval_ms: int = 1000,
    atr_period: int = 3,
) -> LiveEngineV0:
    """Build a real LiveEngineV0 with FeatureEngine and regime registry.

    Uses short bar_interval and small ATR period for fast warmup in tests.
    Mirrors the bridge construction path from _build_engine_and_connector().
    """
    from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415

    paper = PaperEngine(spacing_bps=10.0, levels=5, size_per_level=Decimal("0.001"))
    config = LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY)
    feature_engine = FeatureEngine(
        FeatureEngineConfig(
            bar_interval_ms=bar_interval_ms,
            atr_period=atr_period,
            range_horizon=atr_period,
        )
    )
    return LiveEngineV0(
        paper_engine=paper,
        exchange_port=_noop_port(),
        config=config,
        operator_symbols=[symbol],
        feature_engine=feature_engine,
        regime_registry=registry,
    )


def _noop_port() -> object:
    """Build a NoOp exchange port."""
    from grinder.execution.port import NoOpExchangePort  # noqa: PLC0415

    return NoOpExchangePort()


class TestRegimeBridgeWiring:
    def test_engine_publishes_regime_after_warmup(self) -> None:
        """Engine with FeatureEngine publishes real regime after warmup bars."""
        symbol = "TESTUSDT"
        registry = SharedRegimeRegistry()
        engine = _build_engine_with_regime(symbol, registry, bar_interval_ms=1000, atr_period=3)

        assert len(registry) == 0

        # Feed enough snapshots to warm up FeatureEngine:
        # atr_period=3 needs 4+ bars, bar_interval=1000ms, so ~5 ticks at 1s apart
        base_ts = 1_000_000_000
        price = Decimal("100.00")
        for i in range(8):
            snap = _make_snapshot(symbol, base_ts + i * 1000, price + Decimal(str(i * 0.1)))
            engine.process_snapshot(snap)

        # After warmup, regime should be published
        assert len(registry) > 0, "Registry should have published regime after warmup"
        entry = registry.get(symbol)
        assert entry is not None
        assert entry.symbol == symbol
        assert isinstance(entry.regime, Regime)
        assert entry.confidence > 0

    def test_cleanup_removes_regime_from_registry(self) -> None:
        """Bridge cleanup removes symbol from registry regardless of port type."""
        symbol = "TESTUSDT"
        registry = SharedRegimeRegistry()
        bridge = LiveEngineBridge(config=BridgeConfig())
        bridge.set_regime_registry(registry)

        # Manually publish a regime (simulating what engine would do)
        registry.publish(symbol, Regime.RANGE, RegimeReason.DEFAULT, 80)

        assert len(registry) == 1

        # Create minimal handle for cleanup
        handle = EngineHandle(
            symbol=symbol,
            thread=None,  # type: ignore[arg-type]
            shutdown_event=None,  # type: ignore[arg-type]
        )

        bridge.cleanup(symbol, handle)
        assert len(registry) == 0, "Cleanup should remove symbol from registry"

    def test_full_lifecycle_publish_then_cleanup(self) -> None:
        """Full lifecycle: engine warmup → regime publish → bridge cleanup → registry empty."""
        symbol = "TESTUSDT"
        registry = SharedRegimeRegistry()
        engine = _build_engine_with_regime(symbol, registry, bar_interval_ms=1000, atr_period=3)

        # Warmup
        base_ts = 1_000_000_000
        price = Decimal("100.00")
        for i in range(8):
            snap = _make_snapshot(symbol, base_ts + i * 1000, price + Decimal(str(i * 0.1)))
            engine.process_snapshot(snap)

        assert len(registry) > 0

        # Cleanup via bridge
        bridge = LiveEngineBridge(config=BridgeConfig())
        bridge.set_regime_registry(registry)
        handle = EngineHandle(
            symbol=symbol,
            thread=None,  # type: ignore[arg-type]
            shutdown_event=None,  # type: ignore[arg-type]
        )
        bridge.cleanup(symbol, handle)

        assert len(registry) == 0
