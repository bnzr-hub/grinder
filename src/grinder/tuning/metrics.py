"""Tuning metrics for observability (PR-B3b, ADR-126).

Metrics exported:
- grinder_tuning_result_total{status}: Counter of tuning outcomes by status
- grinder_tuning_no_go_total{reason}: Counter of NO_GO outcomes by reason
- grinder_tuning_cache_size: Gauge of non-expired cache entries
- grinder_tuning_cache_expired_total: Counter of cache expirations

These metric names and label keys are stable contracts.
DO NOT rename without updating metric contracts and tests.

No ``symbol=`` label — per FORBIDDEN_METRIC_LABELS (ADR-028).
Per-symbol detail remains in structured logs (B3a SYMBOL_TUNED / SYMBOL_NO_GO).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.tuning.solver import TuningResult

# Metric name constants (stable contracts)
METRIC_TUNING_RESULT_TOTAL = "grinder_tuning_result_total"
METRIC_TUNING_NO_GO_TOTAL = "grinder_tuning_no_go_total"
METRIC_TUNING_CACHE_SIZE = "grinder_tuning_cache_size"
METRIC_TUNING_CACHE_EXPIRED_TOTAL = "grinder_tuning_cache_expired_total"

# Label key constants
LABEL_STATUS = "status"
LABEL_REASON = "reason"


@dataclass
class TuningMetrics:
    """Tuning metrics collector.

    Tracks tuning outcomes by status and NO_GO reason.
    """

    result_total: dict[str, int] = field(default_factory=dict)
    """Outcome counter by status value ("TUNED", "NO_GO")."""

    no_go_total: dict[str, int] = field(default_factory=dict)
    """NO_GO counter by reason value."""

    cache_size: int = 0
    """Current non-expired cache entries (set externally)."""

    cache_expired_total: int = 0
    """Total cache expirations (set externally)."""

    def record_result(self, result: TuningResult) -> None:
        """Record a tuning outcome.

        Args:
            result: TuningResult from solver.
        """
        status_key = result.status.value
        self.result_total[status_key] = self.result_total.get(status_key, 0) + 1

        if result.reason is not None:
            reason_key = result.reason.value
            self.no_go_total[reason_key] = self.no_go_total.get(reason_key, 0) + 1

    def to_prometheus_lines(self) -> list[str]:
        """Export metrics in Prometheus text format."""
        lines: list[str] = []

        # Result total counter — always emit TUNED and NO_GO series for visibility
        lines.append(f"# HELP {METRIC_TUNING_RESULT_TOTAL} Total tuning outcomes by status")
        lines.append(f"# TYPE {METRIC_TUNING_RESULT_TOTAL} counter")
        effective = {**{"TUNED": 0, "NO_GO": 0}, **self.result_total}
        for status, count in sorted(effective.items()):
            lines.append(f'{METRIC_TUNING_RESULT_TOTAL}{{{LABEL_STATUS}="{status}"}} {count}')

        # NO_GO total counter
        lines.append(f"# HELP {METRIC_TUNING_NO_GO_TOTAL} Total NO_GO outcomes by reason")
        lines.append(f"# TYPE {METRIC_TUNING_NO_GO_TOTAL} counter")
        for reason, count in sorted(self.no_go_total.items()):
            lines.append(f'{METRIC_TUNING_NO_GO_TOTAL}{{{LABEL_REASON}="{reason}"}} {count}')

        # Cache size gauge
        lines.append(f"# HELP {METRIC_TUNING_CACHE_SIZE} Non-expired tuning cache entries")
        lines.append(f"# TYPE {METRIC_TUNING_CACHE_SIZE} gauge")
        lines.append(f"{METRIC_TUNING_CACHE_SIZE} {self.cache_size}")

        # Cache expired total counter
        lines.append(f"# HELP {METRIC_TUNING_CACHE_EXPIRED_TOTAL} Total tuning cache expirations")
        lines.append(f"# TYPE {METRIC_TUNING_CACHE_EXPIRED_TOTAL} counter")
        lines.append(f"{METRIC_TUNING_CACHE_EXPIRED_TOTAL} {self.cache_expired_total}")

        return lines

    def initialize_zero_series(self) -> None:
        """Pre-populate zero-value series for Prometheus visibility."""
        for status in ("TUNED", "NO_GO"):
            if status not in self.result_total:
                self.result_total[status] = 0

    def reset(self) -> None:
        """Reset all metrics."""
        self.result_total.clear()
        self.no_go_total.clear()
        self.cache_size = 0
        self.cache_expired_total = 0


# Module-level singleton
_metrics = TuningMetrics()


def get_tuning_metrics() -> TuningMetrics:
    """Get global tuning metrics instance."""
    return _metrics


def reset_tuning_metrics() -> None:
    """Reset global tuning metrics."""
    _metrics.reset()
