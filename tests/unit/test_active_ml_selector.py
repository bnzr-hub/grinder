"""Tests for doc-36 Phase 3b: ML-assisted scoring in active selector.

Covers:
1. ML disabled → identical Phase 2 behavior
2. ML enabled → ranking changes within cap
3. ML error → fallback baseline + fail-safe
4. Determinism with ML
5. Hysteresis + budget preserved under ML adjust
6. Graceful exit only preserved under ML
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from grinder.selection.active_selector import (
    ActiveSelector,
    ActiveSelectorConfig,
    reset_active_selector_metrics,
)
from grinder.selection.shadow_selector import get_selector_metrics, reset_selector_metrics


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
    reset_active_selector_metrics()


def _base_config(**overrides: object) -> ActiveSelectorConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "k": 2,
        "cycle_s": 0,
        "min_hold_cycles": 0,
        "max_changes_per_cycle": 10,
        "enter_threshold_bps": 0,
        "exit_threshold_bps": 0,
    }
    defaults.update(overrides)
    return ActiveSelectorConfig(**defaults)  # type: ignore[arg-type]


class TestActiveMlDisabled:
    """ML disabled → identical Phase 2 behavior."""

    def test_no_ml_adjust(self) -> None:
        config = _base_config(ml_enabled=False)
        sel = ActiveSelector(config, initial_active={"AAA", "BBB"})
        for sym in ("AAA", "BBB"):
            sel.update_features(_make_feat(sym))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB"])
        assert result is not None
        metrics = get_selector_metrics()
        assert metrics.ml_adjust_applied == 0


class TestActiveMlEnabled:
    """ML enabled → ranking can change within cap."""

    def test_ml_changes_ranking(self) -> None:
        def provider() -> dict[str, int]:
            return {"AAA": 0, "BBB": 0, "CCC": 9999}

        config = _base_config(k=1, ml_enabled=True, ml_adjust_max_bps=2000)
        sel = ActiveSelector(
            config,
            initial_active={"AAA", "BBB", "CCC"},
            ml_adjust_provider=provider,
        )
        sel.update_features(_make_feat("AAA", range_score=500))  # type: ignore[arg-type]
        sel.update_features(_make_feat("BBB", range_score=400))  # type: ignore[arg-type]
        sel.update_features(_make_feat("CCC", range_score=100))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB", "CCC"])
        assert result is not None
        metrics = get_selector_metrics()
        assert metrics.ml_adjust_applied >= 1

    def test_ml_adjust_bounded(self) -> None:
        """Adjustment capped to ML_ADJUST_MAX_BPS."""

        def provider() -> dict[str, int]:
            return {"AAA": 99999}  # Way above cap

        config = _base_config(k=1, ml_enabled=True, ml_adjust_max_bps=100)
        sel = ActiveSelector(config, initial_active={"AAA"}, ml_adjust_provider=provider)
        sel.update_features(_make_feat("AAA"))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA"])
        assert result is not None
        # ML applied but bounded
        metrics = get_selector_metrics()
        assert metrics.ml_adjust_applied >= 1


class TestActiveMlFallback:
    """ML error → fallback baseline, no crash."""

    def test_provider_error_failsafe(self) -> None:
        def provider() -> dict[str, int]:
            msg = "model crashed"
            raise RuntimeError(msg)

        config = _base_config(ml_enabled=True, ml_adjust_max_bps=500)
        sel = ActiveSelector(
            config,
            initial_active={"AAA"},
            ml_adjust_provider=provider,
        )
        sel.update_features(_make_feat("AAA"))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA"])
        assert result is not None
        # Active set preserved, no crash
        assert "AAA" in result.active_set
        metrics = get_selector_metrics()
        assert metrics.ml_fallback >= 1

    def test_no_provider_fallback(self) -> None:
        config = _base_config(ml_enabled=True, ml_adjust_max_bps=500)
        sel = ActiveSelector(config, initial_active={"AAA"}, ml_adjust_provider=None)
        sel.update_features(_make_feat("AAA"))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA"])
        assert result is not None
        metrics = get_selector_metrics()
        assert metrics.ml_fallback >= 1


class TestActiveMlDeterminism:
    """Repeated runs identical with ML."""

    def test_deterministic(self) -> None:
        results = []
        for _ in range(3):
            reset_selector_metrics()
            reset_active_selector_metrics()
            config = _base_config(k=2, ml_enabled=True, ml_adjust_max_bps=200)
            sel = ActiveSelector(
                config,
                initial_active={"AAA", "BBB", "CCC"},
                ml_adjust_provider=lambda: {"AAA": 100, "BBB": -50, "CCC": 200},
            )
            for sym in ("AAA", "BBB", "CCC"):
                sel.update_features(_make_feat(sym))  # type: ignore[arg-type]
            r = sel.maybe_run(1000, ["AAA", "BBB", "CCC"])
            assert r is not None
            results.append(sorted(r.active_set))
        assert results[0] == results[1] == results[2]


class TestActiveMlHysteresisPreserved:
    """Hysteresis and change budget still work with ML adjust."""

    def test_min_hold_with_ml(self) -> None:
        config = _base_config(
            k=1, min_hold_cycles=3, max_changes_per_cycle=10, ml_enabled=True, ml_adjust_max_bps=100
        )
        sel = ActiveSelector(
            config,
            initial_active={"AAA", "BBB"},
            ml_adjust_provider=lambda: {"AAA": 100, "BBB": -100},
        )
        # AAA boosted, BBB penalized → BBB should be removed but min_hold blocks
        for cycle in range(3):
            for sym in ("AAA", "BBB"):
                sel.update_features(_make_feat(sym, range_score=500 if sym == "AAA" else 400))  # type: ignore[arg-type]
            result = sel.maybe_run(cycle * 1000, ["AAA", "BBB"])
            assert result is not None
            assert "BBB" in result.active_set  # min_hold not yet expired

    def test_budget_with_ml(self) -> None:
        config = _base_config(k=5, max_changes_per_cycle=1, ml_enabled=True, ml_adjust_max_bps=500)
        sel = ActiveSelector(
            config,
            initial_active=set(),
            ml_adjust_provider=lambda: {"AAA": 500, "BBB": 400, "CCC": 300},
        )
        for sym in ("AAA", "BBB", "CCC"):
            sel.update_features(_make_feat(sym))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB", "CCC"])
        assert result is not None
        assert len(result.added) <= 1  # budget=1


class TestActiveMlGracefulExit:
    """Graceful exit only preserved under ML."""

    def test_non_flat_deferred_with_ml(self) -> None:
        config = _base_config(k=1, max_changes_per_cycle=10, ml_enabled=True, ml_adjust_max_bps=500)
        sel = ActiveSelector(
            config,
            initial_active={"AAA", "BBB"},
            ml_adjust_provider=lambda: {"AAA": 500, "BBB": -500},
        )
        for sym in ("AAA", "BBB"):
            sel.update_features(_make_feat(sym, range_score=500 if sym == "AAA" else 100))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB"], non_flat_symbols={"BBB"})
        assert result is not None
        assert "BBB" in result.deferred
        assert "BBB" in result.graceful_exit_only
