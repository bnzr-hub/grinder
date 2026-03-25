"""Risk base metrics for Prometheus /metrics endpoint (PR-1).

Singleton pattern matching account/metrics.py.
Emits:
- grinder_risk_base_usd (gauge): current risk base value in USD.
- grinder_risk_base_stale_seconds (gauge): seconds since last balance update.
- grinder_risk_base_status (gauge): 0=unavailable, 1=fresh, 2=soft-stale, 3=hard-stale.

SSOT: docs/DECISIONS.md (ADR-092)
"""

from __future__ import annotations

from dataclasses import dataclass

# Metric names (stable contract)
METRIC_RISK_BASE_USD = "grinder_risk_base_usd"
METRIC_RISK_BASE_STALE_SECONDS = "grinder_risk_base_stale_seconds"
METRIC_RISK_BASE_STATUS = "grinder_risk_base_status"

# Status enum values for grinder_risk_base_status gauge
STATUS_UNAVAILABLE = 0
STATUS_FRESH = 1
STATUS_SOFT_STALE = 2
STATUS_HARD_STALE = 3


@dataclass
class RiskBaseMetrics:
    """Metrics collector for risk base.

    Thread-safe via simple attribute writes (GIL protection).
    """

    value_usd: float = 0.0
    age_s: int = 0
    status: int = STATUS_UNAVAILABLE  # 0=unavailable, 1=fresh, 2=soft, 3=hard

    def record_snapshot(
        self,
        value_usd: float,
        age_s: int,
        is_stale_soft: bool,
        is_stale_hard: bool,
    ) -> None:
        """Record a successful risk base snapshot."""
        self.value_usd = value_usd
        self.age_s = age_s
        if is_stale_hard:
            self.status = STATUS_HARD_STALE
        elif is_stale_soft:
            self.status = STATUS_SOFT_STALE
        else:
            self.status = STATUS_FRESH

    def record_unavailable(self) -> None:
        """Record that risk base could not be derived."""
        self.value_usd = 0.0
        self.status = STATUS_UNAVAILABLE

    def to_prometheus_lines(self) -> list[str]:
        """Generate Prometheus text format lines."""
        return [
            f"# HELP {METRIC_RISK_BASE_USD} Current risk base value in USD",
            f"# TYPE {METRIC_RISK_BASE_USD} gauge",
            f"{METRIC_RISK_BASE_USD} {self.value_usd:.2f}",
            f"# HELP {METRIC_RISK_BASE_STALE_SECONDS} Seconds since last balance update",
            f"# TYPE {METRIC_RISK_BASE_STALE_SECONDS} gauge",
            f"{METRIC_RISK_BASE_STALE_SECONDS} {self.age_s}",
            f"# HELP {METRIC_RISK_BASE_STATUS} Risk base status (0=unavailable, 1=fresh, 2=soft-stale, 3=hard-stale)",
            f"# TYPE {METRIC_RISK_BASE_STATUS} gauge",
            f"{METRIC_RISK_BASE_STATUS} {self.status}",
        ]


# Global singleton
_metrics: RiskBaseMetrics | None = None


def get_risk_base_metrics() -> RiskBaseMetrics:
    """Get or create global risk base metrics instance."""
    global _metrics  # noqa: PLW0603
    if _metrics is None:
        _metrics = RiskBaseMetrics()
    return _metrics


def reset_risk_base_metrics() -> None:
    """Reset risk base metrics (for testing)."""
    global _metrics  # noqa: PLW0603
    _metrics = None
