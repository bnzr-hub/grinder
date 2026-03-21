"""Tests for connector metrics (H5 Observability v1).

Tests verify:
- ConnectorMetrics records H2/H3/H4 events correctly
- Prometheus output includes all required patterns
- Wiring into retries/idempotency/circuit breaker works
"""

from __future__ import annotations

import pytest

from grinder.connectors.metrics import (
    METRIC_CIRCUIT_REJECTED,
    METRIC_CIRCUIT_STATE,
    METRIC_CIRCUIT_TRIPS,
    METRIC_IDEMPOTENCY_CONFLICTS,
    METRIC_IDEMPOTENCY_HITS,
    METRIC_IDEMPOTENCY_MISSES,
    METRIC_RETRIES_TOTAL,
    METRIC_USER_DATA_UNKNOWN_EVENTS_TOTAL,
    CircuitMetricState,
    ConnectorMetrics,
    get_connector_metrics,
    reset_connector_metrics,
)


@pytest.fixture(autouse=True)
def reset_metrics() -> None:
    """Reset connector metrics before each test."""
    reset_connector_metrics()


class TestConnectorMetrics:
    """Tests for ConnectorMetrics class."""

    def test_record_retry(self) -> None:
        """Test recording retry events."""
        metrics = ConnectorMetrics()
        metrics.record_retry("place", "transient")
        metrics.record_retry("place", "transient")
        metrics.record_retry("cancel", "timeout")

        assert metrics.retries[("place", "transient")] == 2
        assert metrics.retries[("cancel", "timeout")] == 1

    def test_record_idempotency_hit(self) -> None:
        """Test recording idempotency cache hits."""
        metrics = ConnectorMetrics()
        metrics.record_idempotency_hit("place")
        metrics.record_idempotency_hit("place")
        metrics.record_idempotency_hit("cancel")

        assert metrics.idempotency_hits["place"] == 2
        assert metrics.idempotency_hits["cancel"] == 1

    def test_record_idempotency_conflict(self) -> None:
        """Test recording idempotency conflicts."""
        metrics = ConnectorMetrics()
        metrics.record_idempotency_conflict("place")

        assert metrics.idempotency_conflicts["place"] == 1

    def test_record_idempotency_miss(self) -> None:
        """Test recording idempotency misses."""
        metrics = ConnectorMetrics()
        metrics.record_idempotency_miss("place")
        metrics.record_idempotency_miss("place")

        assert metrics.idempotency_misses["place"] == 2

    def test_set_circuit_state(self) -> None:
        """Test setting circuit state."""
        metrics = ConnectorMetrics()
        metrics.set_circuit_state("place", CircuitMetricState.OPEN)

        assert metrics.circuit_states["place"] == CircuitMetricState.OPEN

    def test_record_circuit_rejected(self) -> None:
        """Test recording circuit rejections."""
        metrics = ConnectorMetrics()
        metrics.record_circuit_rejected("place")
        metrics.record_circuit_rejected("place")

        assert metrics.circuit_rejected["place"] == 2

    def test_record_circuit_trip(self) -> None:
        """Test recording circuit trips."""
        metrics = ConnectorMetrics()
        metrics.record_circuit_trip("place", "threshold")

        assert metrics.circuit_trips[("place", "threshold")] == 1

    def test_record_user_data_unknown_event_bounded_labels(self) -> None:
        """Unknown user-data labels must stay bounded (known + other)."""
        metrics = ConnectorMetrics()
        metrics.record_user_data_unknown_event("ACCOUNT_CONFIG_UPDATE")
        metrics.record_user_data_unknown_event("FOO_BAR_CUSTOM")

        assert metrics.user_data_unknown_events["ACCOUNT_CONFIG_UPDATE"] == 1
        assert metrics.user_data_unknown_events["other"] == 1


class TestPrometheusOutput:
    """Tests for Prometheus text format output."""

    def test_to_prometheus_lines_empty(self) -> None:
        """Test Prometheus output with no recorded events."""
        metrics = ConnectorMetrics()
        lines = metrics.to_prometheus_lines()
        output = "\n".join(lines)

        # Should have HELP/TYPE for all metrics
        assert f"# HELP {METRIC_RETRIES_TOTAL}" in output
        assert f"# TYPE {METRIC_RETRIES_TOTAL}" in output
        assert f"# HELP {METRIC_IDEMPOTENCY_HITS}" in output
        assert f"# TYPE {METRIC_IDEMPOTENCY_HITS}" in output
        assert f"# HELP {METRIC_IDEMPOTENCY_CONFLICTS}" in output
        assert f"# TYPE {METRIC_IDEMPOTENCY_CONFLICTS}" in output
        assert f"# HELP {METRIC_IDEMPOTENCY_MISSES}" in output
        assert f"# TYPE {METRIC_IDEMPOTENCY_MISSES}" in output
        assert f"# HELP {METRIC_CIRCUIT_STATE}" in output
        assert f"# TYPE {METRIC_CIRCUIT_STATE}" in output
        assert f"# HELP {METRIC_CIRCUIT_REJECTED}" in output
        assert f"# TYPE {METRIC_CIRCUIT_REJECTED}" in output
        assert f"# HELP {METRIC_CIRCUIT_TRIPS}" in output
        assert f"# TYPE {METRIC_CIRCUIT_TRIPS}" in output
        assert f"# HELP {METRIC_USER_DATA_UNKNOWN_EVENTS_TOTAL}" in output
        assert f"# TYPE {METRIC_USER_DATA_UNKNOWN_EVENTS_TOTAL}" in output

        # Should have placeholder values
        assert '{op="none"' in output

    def test_to_prometheus_lines_with_data(self) -> None:
        """Test Prometheus output with recorded events."""
        metrics = ConnectorMetrics()
        metrics.record_retry("place", "transient")
        metrics.record_idempotency_hit("place")
        metrics.record_idempotency_conflict("cancel")
        metrics.record_idempotency_miss("replace")
        metrics.set_circuit_state("place", CircuitMetricState.OPEN)
        metrics.record_circuit_rejected("place")
        metrics.record_circuit_trip("place", "threshold")
        metrics.record_user_data_unknown_event("ACCOUNT_CONFIG_UPDATE")
        metrics.record_user_data_unknown_event("WHATEVER_NEW_EVENT")

        lines = metrics.to_prometheus_lines()
        output = "\n".join(lines)

        # Check actual data values
        assert f'{METRIC_RETRIES_TOTAL}{{op="place",reason="transient"}} 1' in output
        assert f'{METRIC_IDEMPOTENCY_HITS}{{op="place"}} 1' in output
        assert f'{METRIC_IDEMPOTENCY_CONFLICTS}{{op="cancel"}} 1' in output
        assert f'{METRIC_IDEMPOTENCY_MISSES}{{op="replace"}} 1' in output
        assert f'{METRIC_CIRCUIT_STATE}{{op="place",state="open"}} 1' in output
        assert f'{METRIC_CIRCUIT_STATE}{{op="place",state="closed"}} 0' in output
        assert f'{METRIC_CIRCUIT_REJECTED}{{op="place"}} 1' in output
        assert f'{METRIC_CIRCUIT_TRIPS}{{op="place",reason="threshold"}} 1' in output
        assert (
            f'{METRIC_USER_DATA_UNKNOWN_EVENTS_TOTAL}{{event_type="ACCOUNT_CONFIG_UPDATE"}} 1'
            in output
        )
        assert f'{METRIC_USER_DATA_UNKNOWN_EVENTS_TOTAL}{{event_type="other"}} 1' in output


class TestGlobalSingleton:
    """Tests for global connector metrics singleton."""

    def test_get_connector_metrics_returns_instance(self) -> None:
        """Test that get_connector_metrics returns a ConnectorMetrics instance."""
        metrics = get_connector_metrics()
        assert isinstance(metrics, ConnectorMetrics)

    def test_global_metrics_persists(self) -> None:
        """Test that global metrics is reused across calls."""
        metrics1 = get_connector_metrics()
        metrics2 = get_connector_metrics()
        assert metrics1 is metrics2

    def test_reset_connector_metrics(self) -> None:
        """Test that reset clears the singleton."""
        metrics1 = get_connector_metrics()
        metrics1.record_retry("test", "transient")

        reset_connector_metrics()
        metrics2 = get_connector_metrics()

        assert metrics1 is not metrics2
        assert ("test", "transient") not in metrics2.retries


class TestMetricsBuilderIntegration:
    """Tests for MetricsBuilder integration with connector metrics."""

    def test_build_includes_connector_metrics(self) -> None:
        """Test that MetricsBuilder includes connector metrics."""
        # Skip if redis not installed (observability imports ha which imports redis)
        pytest.importorskip("redis", reason="redis not installed")

        # Import here to avoid import cycles (redis dependency)
        from grinder.gating import reset_gating_metrics  # noqa: PLC0415
        from grinder.observability import build_metrics_output  # noqa: PLC0415
        from grinder.observability.metrics_builder import reset_metrics_builder  # noqa: PLC0415

        reset_gating_metrics()
        reset_metrics_builder()
        reset_connector_metrics()

        # Record some metrics
        metrics = get_connector_metrics()
        metrics.record_retry("place", "transient")

        output = build_metrics_output()

        # Should include connector metrics
        assert f"# HELP {METRIC_RETRIES_TOTAL}" in output
        assert f'{METRIC_RETRIES_TOTAL}{{op="place",reason="transient"}} 1' in output


class TestRetryWiring:
    """Tests for retry metrics wiring (H2)."""

    @pytest.mark.asyncio
    async def test_retry_records_metric(self) -> None:
        """Test that retry_with_policy records metrics."""
        from grinder.connectors.errors import ConnectorTransientError  # noqa: PLC0415
        from grinder.connectors.retries import RetryPolicy, retry_with_policy  # noqa: PLC0415

        call_count = 0

        async def failing_op() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectorTransientError("test error")
            return "success"

        policy = RetryPolicy(max_attempts=3, base_delay_ms=1)

        async def instant_sleep(_: float) -> None:
            pass

        result, stats = await retry_with_policy(
            "test_op", failing_op, policy, sleep_func=instant_sleep
        )

        assert result == "success"
        assert stats.retries == 2

        # Check metrics were recorded
        metrics = get_connector_metrics()
        assert metrics.retries[("test_op", "transient")] == 2


class TestCircuitBreakerWiring:
    """Tests for circuit breaker metrics wiring (H4)."""

    def test_circuit_rejected_records_metric(self) -> None:
        """Test that circuit rejection records metric."""
        from grinder.connectors import CircuitBreaker, CircuitBreakerConfig  # noqa: PLC0415

        config = CircuitBreakerConfig(failure_threshold=2)
        breaker = CircuitBreaker(config=config)

        # Trip the breaker
        breaker.record_failure("place", "error1")
        breaker.record_failure("place", "error2")

        # Try to allow - should be rejected
        assert breaker.allow("place") is False

        # Check metric
        metrics = get_connector_metrics()
        assert metrics.circuit_rejected["place"] >= 1

    def test_circuit_trip_records_metric(self) -> None:
        """Test that circuit trip records metric."""
        from grinder.connectors import (  # noqa: PLC0415
            CircuitBreaker,
            CircuitBreakerConfig,
            CircuitState,
        )

        config = CircuitBreakerConfig(failure_threshold=2)
        breaker = CircuitBreaker(config=config)

        # Trip the breaker
        breaker.record_failure("place", "error1")
        breaker.record_failure("place", "error2")

        assert breaker.state("place") == CircuitState.OPEN

        # Check metrics
        metrics = get_connector_metrics()
        assert metrics.circuit_trips[("place", "threshold")] == 1
        assert metrics.circuit_states["place"] == CircuitMetricState.OPEN

    def test_circuit_close_records_metric(self) -> None:
        """Test that circuit close records metric."""
        from dataclasses import dataclass  # noqa: PLC0415

        from grinder.connectors import (  # noqa: PLC0415
            CircuitBreaker,
            CircuitBreakerConfig,
            CircuitState,
        )

        @dataclass
        class FakeClock:
            _time: float = 0.0

            def time(self) -> float:
                return self._time

            def advance(self, seconds: float) -> None:
                self._time += seconds

        clock = FakeClock()
        config = CircuitBreakerConfig(failure_threshold=2, open_interval_s=30.0)
        breaker = CircuitBreaker(config=config, clock=clock)

        # Trip the breaker
        breaker.record_failure("place", "error1")
        breaker.record_failure("place", "error2")
        assert breaker.state("place") == CircuitState.OPEN

        # Wait for half-open
        clock.advance(31.0)
        assert breaker.state("place") == CircuitState.HALF_OPEN

        # Success closes the circuit
        breaker.record_success("place")
        assert breaker.state("place") == CircuitState.CLOSED

        # Check metrics
        metrics = get_connector_metrics()
        assert metrics.circuit_states["place"] == CircuitMetricState.CLOSED
