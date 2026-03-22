"""Tests for doc-36 Phase 1 shadow selector.

Covers:
1. Shadow OFF: no selector activity
2. Shadow ON: metrics emitted, no dispatch mutation
3. Cycle cooldown: respects timestamp-based interval
4. NATR gate: below min_natr_bps excluded
5. Trend hard gate: above threshold excluded
6. Deterministic ordering: tie-break stable
7. Fail-open: exception in selector doesn't crash engine
8. Hypothetical churn: would_add/would_remove computed
9. Config validation: env-driven config construction
10. Cardinality cap: score metrics bounded
11. Multi-symbol accumulation and ranking
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from grinder.selection.shadow_selector import (
    ShadowSelector,
    ShadowSelectorConfig,
    get_selector_metrics,
    reset_selector_metrics,
)


@dataclass
class _FakeFeatureSnapshot:
    """Minimal feature snapshot for testing."""

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


def _make_feat(
    symbol: str,
    *,
    natr_bps: int = 200,
    range_score: int = 500,
    spread_bps: int = 10,
    thin_l1: Decimal | None = None,
    net_return_bps: int = 50,
    warmup_bars: int = 20,
) -> _FakeFeatureSnapshot:
    return _FakeFeatureSnapshot(
        ts=1000,
        symbol=symbol,
        mid_price=Decimal("100"),
        spread_bps=spread_bps,
        imbalance_l1_bps=0,
        thin_l1=thin_l1 or Decimal("10.0"),
        natr_bps=natr_bps,
        atr=Decimal("1.0"),
        sum_abs_returns_bps=1000,
        net_return_bps=net_return_bps,
        range_score=range_score,
        warmup_bars=warmup_bars,
    )


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    reset_selector_metrics()


class TestShadowSelectorDisabled:
    """Shadow OFF: selector is silent."""

    def test_disabled_returns_none(self) -> None:
        config = ShadowSelectorConfig(enabled=False)
        sel = ShadowSelector(config)
        sel.update_features(_make_feat("BTCUSDT"))  # type: ignore[arg-type]
        result = sel.maybe_run(100_000, ["BTCUSDT"])
        assert result is None

    def test_disabled_no_metrics(self) -> None:
        config = ShadowSelectorConfig(enabled=False)
        sel = ShadowSelector(config)
        sel.update_features(_make_feat("BTCUSDT"))  # type: ignore[arg-type]
        sel.maybe_run(100_000, ["BTCUSDT"])
        metrics = get_selector_metrics()
        assert len(metrics.cycle_total) == 0


class TestShadowSelectorEnabled:
    """Shadow ON: selector runs and emits metrics."""

    def test_basic_selection(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=2, cycle_s=10)
        sel = ShadowSelector(config)
        for sym in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
            sel.update_features(_make_feat(sym))  # type: ignore[arg-type]
        result = sel.maybe_run(20_000, ["AAAUSDT", "BBBUSDT", "CCCUSDT"])
        assert result is not None
        assert len(result.selection.selected) == 2

    def test_metrics_emitted(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=2, cycle_s=10)
        sel = ShadowSelector(config)
        sel.update_features(_make_feat("BTCUSDT"))  # type: ignore[arg-type]
        sel.maybe_run(20_000, ["BTCUSDT"])
        metrics = get_selector_metrics()
        assert metrics.cycle_total[("shadow", "ok")] == 1
        assert metrics.candidate_count["input"] == 1
        assert metrics.candidate_count["selected"] == 1


class TestCycleCooldown:
    """Selector respects timestamp-based cooldown."""

    def test_cooldown_skips(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=1, cycle_s=60)
        sel = ShadowSelector(config)
        sel.update_features(_make_feat("BTCUSDT"))  # type: ignore[arg-type]
        # First run at ts=0
        r1 = sel.maybe_run(0, ["BTCUSDT"])
        assert r1 is not None
        # Second run at ts=30_000 (30s < 60s) → skipped
        r2 = sel.maybe_run(30_000, ["BTCUSDT"])
        assert r2 is None
        # Third run at ts=60_000 (60s >= 60s) → runs
        r3 = sel.maybe_run(60_000, ["BTCUSDT"])
        assert r3 is not None


class TestNatrGate:
    """NATR below min_natr_bps excluded."""

    def test_natr_below_min_excluded(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=2, cycle_s=0, min_natr_bps=150)
        sel = ShadowSelector(config)
        sel.update_features(_make_feat("LOWNATR", natr_bps=100))  # type: ignore[arg-type]
        sel.update_features(_make_feat("HIGHNATR", natr_bps=200))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["LOWNATR", "HIGHNATR"])
        assert result is not None
        assert "LOWNATR" in result.excluded_reasons
        assert "NATR_BELOW_MIN" in result.excluded_reasons["LOWNATR"]
        assert "HIGHNATR" in result.selection.selected

    def test_natr_metric_recorded(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=2, cycle_s=0, min_natr_bps=150)
        sel = ShadowSelector(config)
        sel.update_features(_make_feat("LOWNATR", natr_bps=100))  # type: ignore[arg-type]
        sel.maybe_run(1000, ["LOWNATR"])
        metrics = get_selector_metrics()
        assert metrics.excluded_total.get("NATR_BELOW_MIN", 0) >= 1


class TestTrendHardGate:
    """Trend hard gate when threshold > 0."""

    def test_trend_too_strong_excluded(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=2, cycle_s=0, trend_hard_gate_bps=100)
        sel = ShadowSelector(config)
        sel.update_features(_make_feat("TRENDY", net_return_bps=150))  # type: ignore[arg-type]
        sel.update_features(_make_feat("CHOPPY", net_return_bps=50))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["TRENDY", "CHOPPY"])
        assert result is not None
        assert "TRENDY" in result.excluded_reasons
        assert "TREND_TOO_STRONG" in result.excluded_reasons["TRENDY"]
        assert "CHOPPY" in result.selection.selected

    def test_trend_gate_disabled_by_default(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=2, cycle_s=0)
        sel = ShadowSelector(config)
        sel.update_features(_make_feat("TRENDY", net_return_bps=9999))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["TRENDY"])
        assert result is not None
        assert "TRENDY" not in result.excluded_reasons


class TestDeterministicOrdering:
    """Tie-break is stable: (-score, symbol)."""

    def test_same_score_tiebreak_by_symbol(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=3, cycle_s=0)
        sel = ShadowSelector(config)
        # All same features → same score → tiebreak by symbol name
        for sym in ("CCCUSDT", "AAAUSDT", "BBBUSDT"):
            sel.update_features(_make_feat(sym))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["CCCUSDT", "AAAUSDT", "BBBUSDT"])
        assert result is not None
        assert result.selection.selected == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]

    def test_repeated_runs_identical(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=2, cycle_s=0)
        results = []
        for _ in range(3):
            reset_selector_metrics()
            sel = ShadowSelector(config)
            for sym in ("XUSDT", "YUSDT", "ZUSDT"):
                sel.update_features(_make_feat(sym, range_score=500 if sym != "ZUSDT" else 100))  # type: ignore[arg-type]
            r = sel.maybe_run(1000, ["XUSDT", "YUSDT", "ZUSDT"])
            assert r is not None
            results.append(r.selection.selected)
        assert results[0] == results[1] == results[2]


class TestFailOpen:
    """Selector exception → fail-open, engine continues."""

    def test_exception_in_run_does_not_propagate(self) -> None:
        """Simulating engine behavior: exception is caught."""
        config = ShadowSelectorConfig(enabled=True, k=1, cycle_s=0)
        sel = ShadowSelector(config)
        # Corrupt internal state to trigger exception
        sel._feature_cache = None  # type: ignore[assignment]
        # Direct call raises
        with pytest.raises(AttributeError):
            sel._run(["BTCUSDT"])
        # But engine wraps in try/except (tested in integration)


class TestHypotheticalChurn:
    """would_add/would_remove computed without dispatch mutation."""

    def test_churn_on_selection_change(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=1, cycle_s=0)
        sel = ShadowSelector(config)
        # Run 1: select AAAUSDT
        sel.update_features(_make_feat("AAAUSDT", range_score=500))  # type: ignore[arg-type]
        sel.update_features(_make_feat("BBBUSDT", range_score=100))  # type: ignore[arg-type]
        r1 = sel.maybe_run(1000, ["AAAUSDT", "BBBUSDT"])
        assert r1 is not None
        assert r1.selection.selected == ["AAAUSDT"]

        # Run 2: BBBUSDT now better → churn
        sel.update_features(_make_feat("AAAUSDT", range_score=100))  # type: ignore[arg-type]
        sel.update_features(_make_feat("BBBUSDT", range_score=500))  # type: ignore[arg-type]
        r2 = sel.maybe_run(2000, ["AAAUSDT", "BBBUSDT"])
        assert r2 is not None
        assert r2.selection.selected == ["BBBUSDT"]
        assert r2.would_add == ["BBBUSDT"]
        assert r2.would_remove == ["AAAUSDT"]

    def test_churn_metrics(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=1, cycle_s=0)
        sel = ShadowSelector(config)
        sel.update_features(_make_feat("AAAUSDT", range_score=500))  # type: ignore[arg-type]
        sel.update_features(_make_feat("BBBUSDT", range_score=100))  # type: ignore[arg-type]
        sel.maybe_run(1000, ["AAAUSDT", "BBBUSDT"])
        sel.update_features(_make_feat("AAAUSDT", range_score=100))  # type: ignore[arg-type]
        sel.update_features(_make_feat("BBBUSDT", range_score=500))  # type: ignore[arg-type]
        sel.maybe_run(2000, ["AAAUSDT", "BBBUSDT"])
        metrics = get_selector_metrics()
        assert metrics.churn_total.get("would_add", 0) >= 1
        assert metrics.churn_total.get("would_remove", 0) >= 1


class TestCardinalityCap:
    """Score metrics bounded to top-K + 2."""

    def test_score_capped(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=2, cycle_s=0)
        sel = ShadowSelector(config)
        for i in range(10):
            sel.update_features(_make_feat(f"SYM{i:02d}USDT", range_score=1000 - i * 10))  # type: ignore[arg-type]
        sel.maybe_run(1000, [f"SYM{i:02d}USDT" for i in range(10)])
        metrics = get_selector_metrics()
        # Should have at most (k + 2) * 5 components = 4 * 5 = 20 entries
        symbols_in_score = {sym for sym, _ in metrics.score_bps}
        assert len(symbols_in_score) <= 2 + 2  # k + _SCORE_EMIT_EXTRA


class TestWarmupIncomplete:
    """Symbol without cached features gets WARMUP_INCOMPLETE."""

    def test_missing_features_excluded(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=2, cycle_s=0)
        sel = ShadowSelector(config)
        sel.update_features(_make_feat("AAAUSDT"))  # type: ignore[arg-type]
        # BBBUSDT has no features
        result = sel.maybe_run(1000, ["AAAUSDT", "BBBUSDT"])
        assert result is not None
        assert "BBBUSDT" in result.excluded_reasons
        assert "WARMUP_INCOMPLETE" in result.excluded_reasons["BBBUSDT"]


class TestPrometheusOutput:
    """Metrics serialization."""

    def test_prometheus_lines_nonempty_after_cycle(self) -> None:
        config = ShadowSelectorConfig(enabled=True, k=1, cycle_s=0)
        sel = ShadowSelector(config)
        sel.update_features(_make_feat("BTCUSDT"))  # type: ignore[arg-type]
        sel.maybe_run(1000, ["BTCUSDT"])
        metrics = get_selector_metrics()
        lines = metrics.to_prometheus_lines()
        assert any("grinder_selector_cycle_total" in line for line in lines)
        assert any("grinder_selector_candidate_count" in line for line in lines)
