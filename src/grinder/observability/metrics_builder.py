"""Metrics builder for Prometheus /metrics endpoint.

Consolidates all metrics into a single Prometheus-compatible text output:
- System metrics (uptime, status)
- Gating metrics (allowed/blocked counters)
- Risk metrics (kill-switch, drawdown)
- HA metrics (role)
- Connector metrics (retries, idempotency, circuit breaker) - H5
- HTTP latency/retry metrics (Launch-05)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from grinder.account.metrics import get_account_sync_metrics
from grinder.connectors.metrics import get_connector_metrics
from grinder.data.quality_metrics import get_data_quality_metrics
from grinder.execution.port_metrics import get_port_metrics
from grinder.execution.sor_metrics import get_sor_metrics
from grinder.gating import get_gating_metrics
from grinder.ha.role import HARole, get_ha_state
from grinder.live.cycle_metrics import get_cycle_metrics
from grinder.live.fsm_metrics import get_fsm_metrics
from grinder.live.live_metrics import get_live_engine_metrics
from grinder.ml.fill_model_loader import fill_model_metrics_to_prometheus_lines
from grinder.ml.metrics import ml_metrics_to_prometheus_lines
from grinder.observability.fill_metrics import get_fill_metrics
from grinder.observability.latency_metrics import get_http_metrics
from grinder.reconcile.metrics import get_reconcile_metrics
from grinder.risk.emergency_exit_metrics import get_emergency_exit_metrics
from grinder.selection.shadow_selector import get_selector_metrics


@dataclass
class RiskMetricsState:
    """Current state of risk metrics for Prometheus export.

    Attributes:
        kill_switch_triggered: Whether kill-switch is active (0 or 1)
        kill_switch_trips: Total trips by reason {reason: count}
        drawdown_pct: Current drawdown percentage (0-100)
        high_water_mark: Current equity HWM
    """

    kill_switch_triggered: int = 0
    kill_switch_trips: dict[str, int] = field(default_factory=dict)
    drawdown_pct: float = 0.0
    high_water_mark: Decimal = Decimal("0")


# Module-level state for risk metrics (updated by external caller)
_risk_state: list[RiskMetricsState | None] = [None]


def set_risk_metrics_state(state: RiskMetricsState) -> None:
    """Set current risk metrics state (called by engine/server)."""
    _risk_state[0] = state


def get_risk_metrics_state() -> RiskMetricsState | None:
    """Get current risk metrics state."""
    return _risk_state[0]


def reset_risk_metrics_state() -> None:
    """Reset risk metrics state (for testing)."""
    _risk_state[0] = None


# Module-level state for consecutive loss metrics (PR-C3b)
_consec_loss_state: list[tuple[int, int]] = [(0, 0)]  # (count, trips)


def set_consecutive_loss_metrics(count: int, trips: int) -> None:
    """Set consecutive loss guard metrics (called by wiring service)."""
    _consec_loss_state[0] = (count, trips)


def get_consecutive_loss_metrics() -> tuple[int, int]:
    """Get consecutive loss guard metrics (count, trips)."""
    return _consec_loss_state[0]


def reset_consecutive_loss_metrics() -> None:
    """Reset consecutive loss metrics (for testing)."""
    _consec_loss_state[0] = (0, 0)


# Module-level readyz callback (PR-ALERTS-0)
# Uses list-as-mutable-container pattern (same as _risk_state, _consec_loss_state).
_ready_fn: list[Callable[[], bool] | None] = [None]


def set_ready_fn(fn: Callable[[], bool]) -> None:
    """Register readyz callback (called by run_trading at startup)."""
    _ready_fn[0] = fn


def reset_ready_fn() -> None:
    """Reset readyz callback (for testing)."""
    _ready_fn[0] = None


@dataclass
class MetricsBuilder:
    """Builds consolidated Prometheus metrics output.

    Attributes:
        start_time: Server start time for uptime calculation.
    """

    start_time: float = field(default_factory=time.time)

    def build(self) -> str:
        """Build complete metrics output in Prometheus text format.

        Returns:
            Prometheus-compatible metrics text.
        """
        lines: list[str] = []

        # System metrics
        lines.extend(self._build_system_metrics())

        # Readyz gauges (PR-ALERTS-0)
        lines.extend(self._build_readyz_metrics())

        # Gating metrics
        lines.extend(self._build_gating_metrics())

        # Risk metrics (if available)
        lines.extend(self._build_risk_metrics())

        # HA metrics
        lines.extend(self._build_ha_metrics())

        # Connector metrics (H2/H3/H4)
        lines.extend(self._build_connector_metrics())

        # Reconcile metrics (LC-09b/LC-10/LC-11/LC-15b)
        lines.extend(self._build_reconcile_metrics())

        # ML metrics (M8-02c-2)
        lines.extend(self._build_ml_metrics())

        # Data quality metrics (Launch-03)
        lines.extend(self._build_data_quality_metrics())

        # HTTP latency/retry metrics (Launch-05)
        lines.extend(self._build_http_metrics())

        # Fill tracking metrics (Launch-06)
        lines.extend(self._build_fill_metrics())

        # FSM metrics (Launch-13)
        lines.extend(self._build_fsm_metrics())

        # SOR metrics (Launch-14)
        lines.extend(self._build_sor_metrics())

        # Port order attempt metrics (PR-FUT-1)
        lines.extend(self._build_port_metrics())

        # Account sync metrics (Launch-15)
        lines.extend(self._build_account_sync_metrics())

        # Fill model shadow metrics (PR-C4a)
        lines.extend(self._build_fill_model_metrics())

        # Emergency exit metrics (RISK-EE-1)
        lines.extend(self._build_emergency_exit_metrics())

        # Cycle layer metrics (PR-INV-3b)
        lines.extend(self._build_cycle_metrics())

        # Live engine metrics (PR-ROLL-1)
        lines.extend(self._build_live_engine_metrics())

        # Shadow selector metrics (doc-36 Phase 1)
        lines.extend(self._build_selector_metrics())

        return "\n".join(lines)

    def _build_system_metrics(self) -> list[str]:
        """Build system-level metrics."""
        uptime = time.time() - self.start_time
        return [
            "# HELP grinder_up Whether grinder is running",
            "# TYPE grinder_up gauge",
            "grinder_up 1",
            "# HELP grinder_uptime_seconds Uptime in seconds",
            "# TYPE grinder_uptime_seconds gauge",
            f"grinder_uptime_seconds {uptime:.2f}",
        ]

    def _build_readyz_metrics(self) -> list[str]:
        """Build readyz readiness gauges (PR-ALERTS-0).

        Emits two gauges:
        - grinder_readyz_callback_registered: 1 if set_ready_fn() was called.
          Used to scope ReadyzNotReady alert to trading loop processes only.
        - grinder_readyz_ready: 1 if is_trading_ready() returns True, 0 otherwise.
        """
        fn = _ready_fn[0]
        registered = 1 if fn is not None else 0
        ready = 1 if (fn is not None and fn()) else 0
        return [
            "# HELP grinder_readyz_callback_registered Whether readyz callback is registered (1=yes, 0=no)",
            "# TYPE grinder_readyz_callback_registered gauge",
            f"grinder_readyz_callback_registered {registered}",
            "# HELP grinder_readyz_ready Whether trading loop is ready (1=yes, 0=no)",
            "# TYPE grinder_readyz_ready gauge",
            f"grinder_readyz_ready {ready}",
        ]

    def _build_gating_metrics(self) -> list[str]:
        """Build gating metrics from global gating metrics instance."""
        gating_metrics = get_gating_metrics()
        return gating_metrics.to_prometheus_lines()

    def _build_risk_metrics(self) -> list[str]:
        """Build risk metrics (kill-switch, drawdown, consecutive loss)."""
        state = get_risk_metrics_state()
        if state is None:
            # Return metrics with default values (risk module not active)
            lines = [
                "# HELP grinder_kill_switch_triggered Whether kill-switch is active",
                "# TYPE grinder_kill_switch_triggered gauge",
                "grinder_kill_switch_triggered 0",
                "# HELP grinder_kill_switch_trips_total Total kill-switch trips by reason",
                "# TYPE grinder_kill_switch_trips_total counter",
                "# HELP grinder_drawdown_pct Current drawdown percentage",
                "# TYPE grinder_drawdown_pct gauge",
                "grinder_drawdown_pct 0",
                "# HELP grinder_high_water_mark Current equity high-water mark",
                "# TYPE grinder_high_water_mark gauge",
                "grinder_high_water_mark 0",
            ]
        else:
            lines = [
                "# HELP grinder_kill_switch_triggered Whether kill-switch is active",
                "# TYPE grinder_kill_switch_triggered gauge",
                f"grinder_kill_switch_triggered {state.kill_switch_triggered}",
                "# HELP grinder_kill_switch_trips_total Total kill-switch trips by reason",
                "# TYPE grinder_kill_switch_trips_total counter",
            ]

            # Add trip counts by reason
            for reason, count in sorted(state.kill_switch_trips.items()):
                lines.append(f'grinder_kill_switch_trips_total{{reason="{reason}"}} {count}')

            # If no trips yet, add a zero-value entry for visibility
            if not state.kill_switch_trips:
                lines.append('grinder_kill_switch_trips_total{reason="none"} 0')

            lines.extend(
                [
                    "# HELP grinder_drawdown_pct Current drawdown percentage",
                    "# TYPE grinder_drawdown_pct gauge",
                    f"grinder_drawdown_pct {state.drawdown_pct:.2f}",
                    "# HELP grinder_high_water_mark Current equity high-water mark",
                    "# TYPE grinder_high_water_mark gauge",
                    f"grinder_high_water_mark {float(state.high_water_mark):.2f}",
                ]
            )

        # PR-C3b: Consecutive loss guard metrics (always emitted, default 0)
        cl_count, cl_trips = get_consecutive_loss_metrics()
        lines.extend(
            [
                "# HELP grinder_risk_consecutive_losses Current consecutive loss count",
                "# TYPE grinder_risk_consecutive_losses gauge",
                f"grinder_risk_consecutive_losses {cl_count}",
                "# HELP grinder_risk_consecutive_loss_trips_total Total consecutive loss guard trips",
                "# TYPE grinder_risk_consecutive_loss_trips_total counter",
                f"grinder_risk_consecutive_loss_trips_total {cl_trips}",
            ]
        )

        return lines

    def _build_ha_metrics(self) -> list[str]:
        """Build HA (high availability) metrics.

        Outputs all possible roles with 1 for current role, 0 for others.
        This follows Prometheus best practices for enum-like gauges.

        Also outputs grinder_ha_is_leader (LC-20) as a convenience metric:
        1 if role == ACTIVE, 0 otherwise.
        """
        current_role = get_ha_state().role
        lines = [
            "# HELP grinder_ha_role Current HA role (1 = this role, 0 = other roles)",
            "# TYPE grinder_ha_role gauge",
        ]
        for role in HARole:
            value = 1 if role == current_role else 0
            lines.append(f'grinder_ha_role{{role="{role.value}"}} {value}')

        # LC-20: is_leader convenience metric for remediation gating
        is_leader = 1 if current_role == HARole.ACTIVE else 0
        lines.extend(
            [
                "# HELP grinder_ha_is_leader Whether this instance is the HA leader (1=yes, 0=no)",
                "# TYPE grinder_ha_is_leader gauge",
                f"grinder_ha_is_leader {is_leader}",
            ]
        )

        return lines

    def _build_connector_metrics(self) -> list[str]:
        """Build connector metrics (H2 retries, H3 idempotency, H4 circuit breaker)."""
        connector_metrics = get_connector_metrics()
        return connector_metrics.to_prometheus_lines()

    def _build_reconcile_metrics(self) -> list[str]:
        """Build reconcile metrics (LC-09b/LC-10/LC-11/LC-15b)."""
        reconcile_metrics = get_reconcile_metrics()
        return reconcile_metrics.to_prometheus_lines()

    def _build_ml_metrics(self) -> list[str]:
        """Build ML metrics (M8-02c-2 ADR-065)."""
        return ml_metrics_to_prometheus_lines()

    def _build_data_quality_metrics(self) -> list[str]:
        """Build data quality metrics (Launch-03)."""
        dq_metrics = get_data_quality_metrics()
        return dq_metrics.to_prometheus_lines()

    def _build_http_metrics(self) -> list[str]:
        """Build HTTP latency/retry metrics (Launch-05)."""
        http_metrics = get_http_metrics()
        return http_metrics.to_prometheus_lines()

    def _build_fill_metrics(self) -> list[str]:
        """Build fill tracking metrics (Launch-06)."""
        fill_metrics = get_fill_metrics()
        return fill_metrics.to_prometheus_lines()

    def _build_fsm_metrics(self) -> list[str]:
        """Build FSM state machine metrics (Launch-13)."""
        fsm_metrics = get_fsm_metrics()
        return fsm_metrics.to_prometheus_lines()

    def _build_sor_metrics(self) -> list[str]:
        """Build SmartOrderRouter metrics (Launch-14)."""
        sor_metrics = get_sor_metrics()
        return sor_metrics.to_prometheus_lines()

    def _build_port_metrics(self) -> list[str]:
        """Build execution port order attempt metrics (PR-FUT-1)."""
        port_metrics = get_port_metrics()
        return port_metrics.to_prometheus_lines()

    def _build_account_sync_metrics(self) -> list[str]:
        """Build account sync metrics (Launch-15)."""
        account_metrics = get_account_sync_metrics()
        return account_metrics.to_prometheus_lines()

    def _build_fill_model_metrics(self) -> list[str]:
        """Build fill model shadow metrics (PR-C4a)."""
        return fill_model_metrics_to_prometheus_lines()

    def _build_emergency_exit_metrics(self) -> list[str]:
        """Build emergency exit metrics (RISK-EE-1)."""
        ee_metrics = get_emergency_exit_metrics()
        return ee_metrics.to_prometheus_lines()

    def _build_cycle_metrics(self) -> list[str]:
        """Build cycle layer metrics (PR-INV-3b)."""
        return get_cycle_metrics().format_metrics()

    def _build_live_engine_metrics(self) -> list[str]:
        """Build live engine metrics (PR-ROLL-1)."""
        return get_live_engine_metrics().format_metrics()

    def _build_selector_metrics(self) -> list[str]:
        """Build shadow selector metrics (doc-36 Phase 1)."""
        return get_selector_metrics().to_prometheus_lines()


class _BuilderHolder:
    """Holder for global metrics builder instance."""

    instance: MetricsBuilder | None = None


def get_metrics_builder() -> MetricsBuilder:
    """Get or create global metrics builder instance."""
    if _BuilderHolder.instance is None:
        _BuilderHolder.instance = MetricsBuilder()
    return _BuilderHolder.instance


def reset_metrics_builder() -> None:
    """Reset global metrics builder (for testing)."""
    _BuilderHolder.instance = None


def build_metrics_output() -> str:
    """Convenience function to build metrics output.

    Returns:
        Prometheus-compatible metrics text.
    """
    return get_metrics_builder().build()
