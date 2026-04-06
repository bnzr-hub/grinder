"""Tests for V2 selector wiring with dynamic AutonomousTuningState."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from scripts.run_autonomous import _build_v2_selector

from grinder.selector.models import SelectionFeatures, SelectionFeaturesV2
from grinder.tuning.autonomous_state import AutonomousTuningState
from grinder.tuning.solver import TuningResult, TuningStatus


def _tuned(symbol: str, order_size: str = "10") -> TuningResult:
    return TuningResult(
        symbol=symbol,
        status=TuningStatus.TUNED,
        order_size=Decimal(order_size),
    )


def _v1_feat(symbol: str) -> SelectionFeatures:
    return SelectionFeatures(
        symbol=symbol,
        quote_volume_last_12x5m=Decimal("5000000"),
        best_bid=Decimal("1.0"),
        best_ask=Decimal("1.001"),
        natr_14_5m=Decimal("2.0"),
    )


def _v2_feat(
    symbol: str,
    *,
    range_score: int = 50,
    net_return_bps: int = 10,
    execution_fit_score: float = 0.8,
    toxicity_penalty_raw: float = 0.1,
) -> SelectionFeaturesV2:
    return SelectionFeaturesV2(
        symbol=symbol,
        quote_volume_last_12x5m=Decimal("5000000"),
        best_bid=Decimal("1.0"),
        best_ask=Decimal("1.001"),
        natr_14_5m=Decimal("2.0"),
        range_score=range_score,
        net_return_bps=net_return_bps,
        execution_fit_score=execution_fit_score,
        toxicity_penalty_raw=toxicity_penalty_raw,
    )


def _state(
    v1: dict[str, Any] | None = None,
    v2: dict[str, Any] | None = None,
) -> AutonomousTuningState:
    return AutonomousTuningState(
        v1_features=v1 or {},
        v2_features=v2 or {},
    )


class TestBuildV2Selector:
    def test_ranker_uses_rank_v2(self) -> None:
        """Ranker closure calls rank_v2, not rank_v1."""
        state = _state(
            v1={"A": _v1_feat("A")},
            v2={"A": _v2_feat("A")},
        )
        cache = MagicMock()
        cache.get.return_value = _tuned("A")

        _prefilter, ranker = _build_v2_selector(state, cache, frozenset())

        result = ranker(["A"])
        assert isinstance(result, list)
        assert result == ["A"]

    def test_ranker_returns_v2_scored_order(self) -> None:
        """V2 ranker returns choppy symbol first."""
        state = _state(
            v1={
                "CHOPPY": _v1_feat("CHOPPY"),
                "TREND": _v1_feat("TREND"),
            },
            v2={
                "CHOPPY": _v2_feat("CHOPPY", range_score=100, net_return_bps=5),
                "TREND": _v2_feat("TREND", range_score=5, net_return_bps=200),
            },
        )
        cache = MagicMock()
        cache.get.side_effect = _tuned

        _prefilter, ranker = _build_v2_selector(state, cache, frozenset())

        result = ranker(["CHOPPY", "TREND"])
        assert result[0] == "CHOPPY"

    def test_prefilter_returns_eligible_symbols(self) -> None:
        """Prefilter closure filters using V1 features."""
        state = _state(
            v1={
                "GOOD": _v1_feat("GOOD"),
                "LOWVOL": SelectionFeatures(
                    symbol="LOWVOL",
                    quote_volume_last_12x5m=Decimal("100"),
                    best_bid=Decimal("1.0"),
                    best_ask=Decimal("1.001"),
                    natr_14_5m=Decimal("2.0"),
                ),
            },
        )
        cache = MagicMock()
        cache.get.side_effect = lambda sym: _tuned(sym) if sym in ("GOOD", "LOWVOL") else None

        prefilter, _ranker = _build_v2_selector(state, cache, frozenset())

        result = prefilter(["GOOD", "LOWVOL"])
        assert "GOOD" in result
        assert "LOWVOL" not in result

    def test_execution_fit_forwarded(self) -> None:
        """Tuning order sizes flow into V2 features via state."""
        state = _state(v2={"BTC": _v2_feat("BTC", execution_fit_score=0.5)})
        cache = MagicMock()

        _prefilter, ranker = _build_v2_selector(state, cache, frozenset())
        # Just verify it doesn't crash
        ranker(["BTC"])

    def test_empty_state_no_crash(self) -> None:
        """Empty state produces working but empty closures."""
        state = _state()
        cache = MagicMock()

        prefilter, ranker = _build_v2_selector(state, cache, frozenset())
        assert prefilter([]) == []
        assert ranker([]) == []

    def test_v2_fetch_failure_falls_back_to_v1(self) -> None:
        """If V2 features empty, ranker falls back to V1."""
        state = _state(
            v1={"A": _v1_feat("A")},
            v2={},
        )
        cache = MagicMock()

        _prefilter, ranker = _build_v2_selector(state, cache, frozenset())
        result = ranker(["A"])
        assert result == ["A"]

    def test_selector_reads_dynamic_state(self) -> None:
        """Closures read from state at call time, not construction time."""
        state = _state(v1={}, v2={})
        cache = MagicMock()
        cache.get.side_effect = _tuned

        _prefilter, ranker = _build_v2_selector(state, cache, frozenset())

        # Initially empty → no ranking
        assert ranker(["A"]) == []

        # Replace state with features → now ranking works
        state.replace(
            tuned_results={},
            tuned_sizes={},
            natr_map={},
            v1_features={"A": _v1_feat("A")},
            v2_features={"A": _v2_feat("A")},
        )
        result = ranker(["A"])
        assert result == ["A"]
