"""Tests for TuningMetrics (PR-B3b, ADR-126).

Covers: record_result, NO_GO by reason, Prometheus output, zero series,
contract registration, no symbol= label.
"""

from __future__ import annotations

from decimal import Decimal

from grinder.execution.engine import SymbolConstraints
from grinder.observability.metrics_contract import (
    FORBIDDEN_METRIC_LABELS,
    REQUIRED_METRICS_PATTERNS,
)
from grinder.tuning.metrics import (
    METRIC_TUNING_CACHE_EXPIRED_TOTAL,
    METRIC_TUNING_CACHE_SIZE,
    METRIC_TUNING_NO_GO_TOTAL,
    METRIC_TUNING_RESULT_TOTAL,
    TuningMetrics,
    reset_tuning_metrics,
)
from grinder.tuning.solver import TuningSolverConfig, solve

BTC_CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    tick_size=Decimal("0.10"),
    min_notional=Decimal("5"),
)

DEFAULT_CONFIG = TuningSolverConfig(max_position_usd=Decimal("10000"), max_inventory_levels=5)
TINY_CONFIG = TuningSolverConfig(max_position_usd=Decimal("100"), max_inventory_levels=5)


class TestRecordResult:
    def test_record_tuned(self) -> None:
        m = TuningMetrics()
        result = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), DEFAULT_CONFIG)
        m.record_result(result)
        assert m.result_total["TUNED"] == 1
        assert "NO_GO" not in m.result_total or m.result_total.get("NO_GO", 0) == 0

    def test_record_no_go(self) -> None:
        m = TuningMetrics()
        result = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), TINY_CONFIG)
        m.record_result(result)
        assert m.result_total["NO_GO"] == 1
        assert m.no_go_total.get("POSITION_EXCEEDS_CAP", 0) == 1

    def test_multiple_reasons_tracked(self) -> None:
        m = TuningMetrics()
        # POSITION_EXCEEDS_CAP
        r1 = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), TINY_CONFIG)
        m.record_result(r1)
        # PRICE_UNAVAILABLE
        r2 = solve("ETHUSDT", BTC_CONSTRAINTS, Decimal("0"), DEFAULT_CONFIG)
        m.record_result(r2)
        assert m.no_go_total["POSITION_EXCEEDS_CAP"] == 1
        assert m.no_go_total["PRICE_UNAVAILABLE"] == 1
        assert m.result_total["NO_GO"] == 2


class TestPrometheusOutput:
    def test_format(self) -> None:
        m = TuningMetrics()
        m.initialize_zero_series()
        r = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), DEFAULT_CONFIG)
        m.record_result(r)
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)

        assert f"# HELP {METRIC_TUNING_RESULT_TOTAL}" in text
        assert f"# TYPE {METRIC_TUNING_RESULT_TOTAL} counter" in text
        assert f'{METRIC_TUNING_RESULT_TOTAL}{{status="TUNED"}} 1' in text
        assert f'{METRIC_TUNING_RESULT_TOTAL}{{status="NO_GO"}} 0' in text
        assert f"# HELP {METRIC_TUNING_CACHE_SIZE}" in text
        assert f"{METRIC_TUNING_CACHE_SIZE} 0" in text
        assert f"{METRIC_TUNING_CACHE_EXPIRED_TOTAL} 0" in text

    def test_no_go_reason_in_output(self) -> None:
        m = TuningMetrics()
        r = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("0"), DEFAULT_CONFIG)
        m.record_result(r)
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert f'{METRIC_TUNING_NO_GO_TOTAL}{{reason="PRICE_UNAVAILABLE"}} 1' in text


class TestZeroSeries:
    def test_initialize_zero_series(self) -> None:
        m = TuningMetrics()
        m.initialize_zero_series()
        assert m.result_total["TUNED"] == 0
        assert m.result_total["NO_GO"] == 0

    def test_zero_series_idempotent(self) -> None:
        m = TuningMetrics()
        r = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), DEFAULT_CONFIG)
        m.record_result(r)
        m.initialize_zero_series()
        assert m.result_total["TUNED"] == 1  # not reset


class TestContractRegistration:
    def test_metrics_in_required_patterns(self) -> None:
        patterns = "\n".join(REQUIRED_METRICS_PATTERNS)
        assert METRIC_TUNING_RESULT_TOTAL in patterns
        assert METRIC_TUNING_NO_GO_TOTAL in patterns
        assert METRIC_TUNING_CACHE_SIZE in patterns
        assert METRIC_TUNING_CACHE_EXPIRED_TOTAL in patterns


class TestNoForbiddenLabels:
    def test_no_symbol_label(self) -> None:
        m = TuningMetrics()
        m.initialize_zero_series()
        r = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), DEFAULT_CONFIG)
        m.record_result(r)
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        for forbidden in FORBIDDEN_METRIC_LABELS:
            assert forbidden not in text, f"Forbidden label {forbidden!r} found in metrics output"


class TestReset:
    def test_reset_clears(self) -> None:
        m = TuningMetrics()
        r = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), DEFAULT_CONFIG)
        m.record_result(r)
        m.cache_size = 5
        m.cache_expired_total = 3
        m.reset()
        assert m.result_total == {}
        assert m.no_go_total == {}
        assert m.cache_size == 0
        assert m.cache_expired_total == 0

    def test_module_reset(self) -> None:
        from grinder.tuning.metrics import get_tuning_metrics  # noqa: PLC0415

        metrics = get_tuning_metrics()
        r = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("80000"), DEFAULT_CONFIG)
        metrics.record_result(r)
        reset_tuning_metrics()
        assert get_tuning_metrics().result_total == {}
