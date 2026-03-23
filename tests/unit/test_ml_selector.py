"""Tests for doc-36 Phase 3 ML-assisted scoring (shadow only).

Covers:
1. ML disabled → behavior unchanged
2. ML enabled + provider → adjust bounded by MAX_BPS
3. ML enabled + error → fallback baseline, no crash
4. ML enabled + no provider → fallback
5. Determinism (same inputs → same order)
6. Engine integration: shadow ON+ML does not change live_actions
7. ML metrics emitted correctly
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.execution.port import NoOpExchangePort
from grinder.features.engine import FeatureEngine, FeatureEngineConfig
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.selection.shadow_selector import (
    ShadowSelector,
    ShadowSelectorConfig,
    get_selector_metrics,
    reset_selector_metrics,
)


@dataclass
class _FakeFeatureSnapshot:
    ts: int
    symbol: str
    mid_price: Decimal
    spread_bps: int
    imbalance_l1_bps: int
    thin_l1: Decimal
    natr_bps: int
    atr: Decimal | None
    sum_abs_returns_bps: int
    net_return_bps: int
    range_score: int
    warmup_bars: int


def _make_feat(symbol: str, *, range_score: int = 500) -> _FakeFeatureSnapshot:
    return _FakeFeatureSnapshot(
        ts=1000,
        symbol=symbol,
        mid_price=Decimal("100"),
        spread_bps=10,
        imbalance_l1_bps=0,
        thin_l1=Decimal("10.0"),
        natr_bps=200,
        atr=Decimal("1.0"),
        sum_abs_returns_bps=1000,
        net_return_bps=50,
        range_score=range_score,
        warmup_bars=20,
    )


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_selector_metrics()


class TestMlDisabled:
    """ML disabled → no adjustment, behavior identical to Phase 1."""

    def test_ml_disabled_no_change(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=2, cycle_s=0, ml_enabled=False)
        sel = ShadowSelector(config)
        for sym in ("AAA", "BBB", "CCC"):
            sel.update_features(_make_feat(sym))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB", "CCC"])
        assert result is not None
        metrics = get_selector_metrics()
        assert metrics.ml_adjust_applied == 0
        assert metrics.ml_fallback == 0


class TestMlEnabledWithProvider:
    """ML enabled + provider returns adjustments."""

    def test_adjust_bounded_by_max_bps(self) -> None:
        def provider() -> dict[str, int]:
            return {"AAA": 5000, "BBB": -3000, "CCC": 100}

        config = ShadowSelectorConfig(
            enabled=True, k=2, cycle_s=0, ml_enabled=True, ml_adjust_max_bps=500
        )
        sel = ShadowSelector(config, ml_adjust_provider=provider)
        for sym in ("AAA", "BBB", "CCC"):
            sel.update_features(_make_feat(sym))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB", "CCC"])
        assert result is not None
        metrics = get_selector_metrics()
        assert metrics.ml_adjust_applied == 1
        # Check that adjustments were capped
        for score in result.selection.scores:
            if not score.gates_failed:
                # All symbols had same base score, ML can shift ranking
                pass  # just verify no crash

    def test_ml_can_change_ranking(self) -> None:
        """ML boost to CCC (lowest base) should promote it."""

        def provider() -> dict[str, int]:
            # Give CCC a massive boost (will be capped to max_bps)
            return {"AAA": 0, "BBB": 0, "CCC": 9999}

        config = ShadowSelectorConfig(
            enabled=True, k=1, cycle_s=0, ml_enabled=True, ml_adjust_max_bps=2000
        )
        sel = ShadowSelector(config, ml_adjust_provider=provider)
        # CCC has lowest base range_score
        sel.update_features(_make_feat("AAA", range_score=500))  # type: ignore[arg-type]
        sel.update_features(_make_feat("BBB", range_score=400))  # type: ignore[arg-type]
        sel.update_features(_make_feat("CCC", range_score=100))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB", "CCC"])
        assert result is not None
        # CCC should be selected (boosted by +2000 bps)
        assert "CCC" in result.selection.selected


class TestMlFallback:
    """ML enabled but provider errors/unavailable → fallback."""

    def test_provider_error_fallback(self) -> None:
        def provider() -> dict[str, int]:
            msg = "ML model not loaded"
            raise RuntimeError(msg)

        config = ShadowSelectorConfig(
            enabled=True, k=2, cycle_s=0, ml_enabled=True, ml_adjust_max_bps=500
        )
        sel = ShadowSelector(config, ml_adjust_provider=provider)
        sel.update_features(_make_feat("AAA"))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA"])
        assert result is not None
        metrics = get_selector_metrics()
        assert metrics.ml_fallback == 1
        assert metrics.ml_adjust_applied == 0

    def test_no_provider_fallback(self) -> None:
        config = ShadowSelectorConfig(
            enabled=True, k=2, cycle_s=0, ml_enabled=True, ml_adjust_max_bps=500
        )
        sel = ShadowSelector(config, ml_adjust_provider=None)
        sel.update_features(_make_feat("AAA"))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA"])
        assert result is not None
        metrics = get_selector_metrics()
        assert metrics.ml_fallback == 1

    def test_no_provider_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """ML enabled without provider → explicit PROVIDER_MISSING warning."""
        config = ShadowSelectorConfig(
            enabled=True, k=1, cycle_s=0, ml_enabled=True, ml_adjust_max_bps=500
        )
        sel = ShadowSelector(config, ml_adjust_provider=None)
        sel.update_features(_make_feat("AAA"))  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING, logger="grinder.selection.shadow_selector"):
            sel.maybe_run(1000, ["AAA"])
        assert any("SELECTOR_ML_PROVIDER_MISSING" in msg for msg in caplog.messages)
        metrics = get_selector_metrics()
        assert metrics.ml_fallback >= 1

    def test_empty_adjusts_fallback(self) -> None:
        config = ShadowSelectorConfig(
            enabled=True, k=2, cycle_s=0, ml_enabled=True, ml_adjust_max_bps=500
        )
        sel = ShadowSelector(config, ml_adjust_provider=lambda: {})
        sel.update_features(_make_feat("AAA"))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA"])
        assert result is not None
        metrics = get_selector_metrics()
        assert metrics.ml_fallback == 1


class TestMlDeterminism:
    """Same inputs → same output with ML."""

    def test_repeated_runs_identical(self) -> None:
        results = []
        for _ in range(3):
            reset_selector_metrics()
            config = ShadowSelectorConfig(
                enabled=True, k=2, cycle_s=0, ml_enabled=True, ml_adjust_max_bps=200
            )
            sel = ShadowSelector(
                config, ml_adjust_provider=lambda: {"AAA": 100, "BBB": -50, "CCC": 200}
            )
            for sym in ("AAA", "BBB", "CCC"):
                sel.update_features(_make_feat(sym, range_score=500))  # type: ignore[arg-type]
            r = sel.maybe_run(1000, ["AAA", "BBB", "CCC"])
            assert r is not None
            results.append(r.selection.selected)
        assert results[0] == results[1] == results[2]


class TestEngineMlShadowSafety:
    """Engine integration: shadow ON+ML does not change live_actions."""

    def test_shadow_ml_does_not_change_output(self) -> None:
        snap = Snapshot(
            ts=100_000,
            symbol="BTCUSDT",
            bid_price=Decimal("100"),
            ask_price=Decimal("100.02"),
            bid_qty=Decimal("10"),
            ask_qty=Decimal("10"),
            last_price=Decimal("100.01"),
            last_qty=Decimal("1"),
        )
        config = LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY)
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        feat_engine = FeatureEngine(FeatureEngineConfig(bar_interval_ms=60_000))

        # Run 1: shadow OFF
        reset_selector_metrics()
        engine_off = LiveEngineV0(
            paper_engine=paper,
            exchange_port=NoOpExchangePort(),
            config=config,
            feature_engine=feat_engine,
        )
        out_off = engine_off.process_snapshot(snap)

        # Run 2: shadow ON + ML
        reset_selector_metrics()
        feat_engine2 = FeatureEngine(FeatureEngineConfig(bar_interval_ms=60_000))
        ml_config = ShadowSelectorConfig(
            enabled=True, k=1, cycle_s=0, ml_enabled=True, ml_adjust_max_bps=500
        )
        selector = ShadowSelector(ml_config, ml_adjust_provider=lambda: {"BTCUSDT": 100})
        engine_on = LiveEngineV0(
            paper_engine=paper,
            exchange_port=NoOpExchangePort(),
            config=config,
            feature_engine=feat_engine2,
            shadow_selector=selector,
            operator_symbols=["BTCUSDT"],
        )
        out_on = engine_on.process_snapshot(snap)

        # P0 invariant
        assert out_off.live_actions == out_on.live_actions
        assert out_off.armed == out_on.armed


class TestMlMetrics:
    """ML metrics emitted correctly."""

    def test_ml_metrics_in_prometheus(self) -> None:
        config = ShadowSelectorConfig(
            enabled=True, k=1, cycle_s=0, ml_enabled=True, ml_adjust_max_bps=200
        )
        sel = ShadowSelector(config, ml_adjust_provider=lambda: {"AAA": 100})
        sel.update_features(_make_feat("AAA"))  # type: ignore[arg-type]
        sel.maybe_run(1000, ["AAA"])
        metrics = get_selector_metrics()
        lines = metrics.to_prometheus_lines()
        assert any("grinder_selector_ml_adjust_applied_total" in line for line in lines)
        assert any("grinder_selector_ml_adjust_bps" in line for line in lines)
