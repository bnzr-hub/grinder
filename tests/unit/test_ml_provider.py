"""Tests for ML adjust provider (ONNX wiring).

Covers:
1. Provider factory returns expected format
2. Provider handles model prediction returning None (fail-open)
3. Regime mapping: HIGH → positive, LOW → negative, MID → zero
4. Empty feature cache → empty dict
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from grinder.selection.ml_provider import build_ml_adjust_provider


@dataclass
class _FakeFeatureSnapshot:
    ts: int = 1000
    symbol: str = "BTCUSDT"
    mid_price: Decimal = Decimal("100")
    spread_bps: int = 10
    imbalance_l1_bps: int = 0
    thin_l1: Decimal = Decimal("10.0")
    natr_bps: int = 200
    atr: Decimal | None = Decimal("1.0")
    sum_abs_returns_bps: int = 1000
    net_return_bps: int = 50
    range_score: int = 500
    warmup_bars: int = 20


def _make_signal(regime: str = "HIGH") -> MagicMock:
    signal = MagicMock()
    signal.predicted_regime = regime
    signal.spacing_multiplier_x1000 = 1000
    return signal


class TestProviderFactory:
    def test_returns_dict_format(self) -> None:
        model = MagicMock()
        model.predict.return_value = _make_signal("HIGH")
        cache: dict[str, Any] = {"BTCUSDT": _FakeFeatureSnapshot()}
        provider = build_ml_adjust_provider(model, cache, adjust_base_bps=200)
        result = provider()
        assert isinstance(result, dict)
        assert "BTCUSDT" in result
        assert isinstance(result["BTCUSDT"], int)

    def test_empty_cache_returns_empty(self) -> None:
        model = MagicMock()
        cache: dict[str, Any] = {}
        provider = build_ml_adjust_provider(model, cache, adjust_base_bps=200)
        result = provider()
        assert result == {}


class TestRegimeMapping:
    def test_high_positive(self) -> None:
        model = MagicMock()
        model.predict.return_value = _make_signal("HIGH")
        cache: dict[str, Any] = {"AAA": _FakeFeatureSnapshot(symbol="AAA")}
        provider = build_ml_adjust_provider(model, cache, adjust_base_bps=200)
        result = provider()
        assert result["AAA"] == 200

    def test_low_negative(self) -> None:
        model = MagicMock()
        model.predict.return_value = _make_signal("LOW")
        cache: dict[str, Any] = {"AAA": _FakeFeatureSnapshot(symbol="AAA")}
        provider = build_ml_adjust_provider(model, cache, adjust_base_bps=200)
        result = provider()
        assert result["AAA"] == -200

    def test_mid_zero(self) -> None:
        model = MagicMock()
        model.predict.return_value = _make_signal("MID")
        cache: dict[str, Any] = {"AAA": _FakeFeatureSnapshot(symbol="AAA")}
        provider = build_ml_adjust_provider(model, cache, adjust_base_bps=200)
        result = provider()
        assert result["AAA"] == 0


class TestPredictionFailure:
    def test_predict_none_skipped(self) -> None:
        model = MagicMock()
        model.predict.return_value = None
        cache: dict[str, Any] = {"AAA": _FakeFeatureSnapshot(symbol="AAA")}
        provider = build_ml_adjust_provider(model, cache, adjust_base_bps=200)
        result = provider()
        assert "AAA" not in result

    def test_multi_symbol_partial_failure(self) -> None:
        model = MagicMock()
        # AAA succeeds, BBB fails
        model.predict.side_effect = [_make_signal("HIGH"), None]
        cache: dict[str, Any] = {
            "AAA": _FakeFeatureSnapshot(symbol="AAA"),
            "BBB": _FakeFeatureSnapshot(symbol="BBB"),
        }
        provider = build_ml_adjust_provider(model, cache, adjust_base_bps=100)
        result = provider()
        assert "AAA" in result
        assert "BBB" not in result
