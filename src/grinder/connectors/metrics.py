"""Metrics for connector operations (H2/H3/H4).

Provides Prometheus-compatible metrics for:
- H2 Retries: retry counts by operation and reason
- H3 Idempotency: cache hits, conflicts, misses
- H4 Circuit Breaker: state, rejections, trips

Design decisions (ADR-028):
- Low cardinality labels only: op, reason, state (no symbol, order_id, key)
- Counter-based for events, gauge for state
- Thread-safe via simple dict operations
- Global singleton with reset for testing

Usage:
    from grinder.connectors.metrics import get_connector_metrics

    metrics = get_connector_metrics()
    metrics.record_retry("place", "transient")
    metrics.record_idempotency_hit("place")
    metrics.record_circuit_rejected("place")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CircuitMetricState(Enum):
    """Circuit breaker states for metrics."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# Metric names (stable contract)
METRIC_RETRIES_TOTAL = "grinder_connector_retries_total"
METRIC_IDEMPOTENCY_HITS = "grinder_idempotency_hits_total"
METRIC_IDEMPOTENCY_CONFLICTS = "grinder_idempotency_conflicts_total"
METRIC_IDEMPOTENCY_MISSES = "grinder_idempotency_misses_total"
METRIC_CIRCUIT_STATE = "grinder_circuit_state"
METRIC_CIRCUIT_REJECTED = "grinder_circuit_rejected_total"
METRIC_CIRCUIT_TRIPS = "grinder_circuit_trips_total"

# WebSocket metrics (LC-21)
METRIC_WS_CONNECTED = "grinder_ws_connected"
METRIC_WS_RECONNECT_TOTAL = "grinder_ws_reconnect_total"
METRIC_TICKS_RECEIVED_TOTAL = "grinder_ticks_received_total"
METRIC_LAST_TICK_TS = "grinder_last_tick_ts"
METRIC_USER_DATA_UNKNOWN_EVENTS_TOTAL = "grinder_user_data_unknown_events_total"

# Label keys
LABEL_OP = "op"
LABEL_REASON = "reason"
LABEL_STATE = "state"
LABEL_EVENT_TYPE = "event_type"

# Keep label cardinality bounded for unknown user-data events.
_KNOWN_USER_DATA_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "ACCOUNT_CONFIG_UPDATE",
        "STRATEGY_UPDATE",
        "MARGIN_CALL",
        "CONDITIONAL_ORDER_TRIGGER_REJECT",
    }
)


@dataclass
class ConnectorMetrics:
    """Metrics collector for connector operations.

    Thread-safe via simple dict operations (GIL protection).
    Production-ready for Prometheus export.

    Attributes:
        retries: {(op, reason): count} - retry events
        idempotency_hits: {op: count} - cache hits (DONE returned)
        idempotency_conflicts: {op: count} - INFLIGHT conflicts
        idempotency_misses: {op: count} - new entries created
        circuit_states: {op: state} - current circuit state per operation
        circuit_rejected: {op: count} - rejections due to OPEN/probe limit
        circuit_trips: {(op, reason): count} - transitions to OPEN
    """

    retries: dict[tuple[str, str], int] = field(default_factory=dict)
    idempotency_hits: dict[str, int] = field(default_factory=dict)
    idempotency_conflicts: dict[str, int] = field(default_factory=dict)
    idempotency_misses: dict[str, int] = field(default_factory=dict)
    circuit_states: dict[str, CircuitMetricState] = field(default_factory=dict)
    circuit_rejected: dict[str, int] = field(default_factory=dict)
    circuit_trips: dict[tuple[str, str], int] = field(default_factory=dict)

    # WebSocket metrics (LC-21)
    ws_connected: dict[str, bool] = field(default_factory=dict)
    ws_reconnect_total: dict[tuple[str, str], int] = field(default_factory=dict)
    ticks_received: dict[str, int] = field(default_factory=dict)
    last_tick_ts: dict[str, int] = field(default_factory=dict)
    user_data_unknown_events: dict[str, int] = field(default_factory=dict)

    # --- H2 Retries ---

    def record_retry(self, op: str, reason: str) -> None:
        """Record a retry event.

        Args:
            op: Operation name (place, cancel, replace, connect, etc.)
            reason: Reason category (transient, timeout, other)
        """
        key = (op, reason)
        self.retries[key] = self.retries.get(key, 0) + 1

    # --- H3 Idempotency ---

    def record_idempotency_hit(self, op: str) -> None:
        """Record idempotency cache hit (DONE returned)."""
        self.idempotency_hits[op] = self.idempotency_hits.get(op, 0) + 1

    def record_idempotency_conflict(self, op: str) -> None:
        """Record idempotency conflict (INFLIGHT collision)."""
        self.idempotency_conflicts[op] = self.idempotency_conflicts.get(op, 0) + 1

    def record_idempotency_miss(self, op: str) -> None:
        """Record idempotency miss (new entry created)."""
        self.idempotency_misses[op] = self.idempotency_misses.get(op, 0) + 1

    # --- H4 Circuit Breaker ---

    def set_circuit_state(self, op: str, state: CircuitMetricState) -> None:
        """Set current circuit breaker state for operation."""
        self.circuit_states[op] = state

    def record_circuit_rejected(self, op: str) -> None:
        """Record circuit breaker rejection (OPEN or probe limit)."""
        self.circuit_rejected[op] = self.circuit_rejected.get(op, 0) + 1

    def record_circuit_trip(self, op: str, reason: str = "threshold") -> None:
        """Record circuit breaker trip to OPEN state.

        Args:
            op: Operation name
            reason: Trip reason (default: "threshold")
        """
        key = (op, reason)
        self.circuit_trips[key] = self.circuit_trips.get(key, 0) + 1

    # --- WebSocket Metrics (LC-21) ---

    def set_ws_connected(self, connector: str, connected: bool) -> None:
        """Set WebSocket connection state.

        Args:
            connector: Connector name (e.g., "bookTicker", "userdata")
            connected: Whether connected (True) or disconnected (False)
        """
        self.ws_connected[connector] = connected

    def record_ws_reconnect(self, connector: str, reason: str = "disconnect") -> None:
        """Record WebSocket reconnection event.

        Args:
            connector: Connector name
            reason: Reconnection reason (disconnect, timeout, error)
        """
        key = (connector, reason)
        self.ws_reconnect_total[key] = self.ws_reconnect_total.get(key, 0) + 1

    def record_tick_received(self, connector: str) -> None:
        """Record tick received for a connector.

        Args:
            connector: Connector name (e.g., "bookTicker")
        """
        self.ticks_received[connector] = self.ticks_received.get(connector, 0) + 1

    def set_last_tick_ts(self, connector: str, ts: int) -> None:
        """Set timestamp of last tick for a connector.

        Args:
            connector: Connector name
            ts: Timestamp in milliseconds
        """
        self.last_tick_ts[connector] = ts

    def record_user_data_unknown_event(self, event_type: str) -> None:
        """Record unknown user-data event by bounded event-type label."""
        normalized = event_type if event_type in _KNOWN_USER_DATA_EVENT_TYPES else "other"
        self.user_data_unknown_events[normalized] = (
            self.user_data_unknown_events.get(normalized, 0) + 1
        )

    # --- Prometheus Export ---

    def to_prometheus_lines(self) -> list[str]:  # noqa: PLR0912, PLR0915
        """Generate Prometheus text format lines.

        Returns:
            List of lines in Prometheus exposition format.
        """
        lines: list[str] = []

        # H2 Retries
        lines.extend(
            [
                f"# HELP {METRIC_RETRIES_TOTAL} Total retry events by operation and reason",
                f"# TYPE {METRIC_RETRIES_TOTAL} counter",
            ]
        )
        if self.retries:
            for (op, reason), count in sorted(self.retries.items()):
                lines.append(
                    f'{METRIC_RETRIES_TOTAL}{{{LABEL_OP}="{op}",{LABEL_REASON}="{reason}"}} {count}'
                )
        else:
            # Placeholder for visibility
            lines.append(f'{METRIC_RETRIES_TOTAL}{{{LABEL_OP}="none",{LABEL_REASON}="none"}} 0')

        # H3 Idempotency hits
        lines.extend(
            [
                f"# HELP {METRIC_IDEMPOTENCY_HITS} Total idempotency cache hits by operation",
                f"# TYPE {METRIC_IDEMPOTENCY_HITS} counter",
            ]
        )
        if self.idempotency_hits:
            for op, count in sorted(self.idempotency_hits.items()):
                lines.append(f'{METRIC_IDEMPOTENCY_HITS}{{{LABEL_OP}="{op}"}} {count}')
        else:
            lines.append(f'{METRIC_IDEMPOTENCY_HITS}{{{LABEL_OP}="none"}} 0')

        # H3 Idempotency conflicts
        lines.extend(
            [
                f"# HELP {METRIC_IDEMPOTENCY_CONFLICTS} Total idempotency conflicts by operation",
                f"# TYPE {METRIC_IDEMPOTENCY_CONFLICTS} counter",
            ]
        )
        if self.idempotency_conflicts:
            for op, count in sorted(self.idempotency_conflicts.items()):
                lines.append(f'{METRIC_IDEMPOTENCY_CONFLICTS}{{{LABEL_OP}="{op}"}} {count}')
        else:
            lines.append(f'{METRIC_IDEMPOTENCY_CONFLICTS}{{{LABEL_OP}="none"}} 0')

        # H3 Idempotency misses
        lines.extend(
            [
                f"# HELP {METRIC_IDEMPOTENCY_MISSES} Total idempotency misses by operation",
                f"# TYPE {METRIC_IDEMPOTENCY_MISSES} counter",
            ]
        )
        if self.idempotency_misses:
            for op, count in sorted(self.idempotency_misses.items()):
                lines.append(f'{METRIC_IDEMPOTENCY_MISSES}{{{LABEL_OP}="{op}"}} {count}')
        else:
            lines.append(f'{METRIC_IDEMPOTENCY_MISSES}{{{LABEL_OP}="none"}} 0')

        # H4 Circuit state (gauge)
        lines.extend(
            [
                f"# HELP {METRIC_CIRCUIT_STATE} Circuit breaker state (1=current, 0=other)",
                f"# TYPE {METRIC_CIRCUIT_STATE} gauge",
            ]
        )
        if self.circuit_states:
            for op, current_state in sorted(self.circuit_states.items()):
                for state in CircuitMetricState:
                    value = 1 if state == current_state else 0
                    lines.append(
                        f'{METRIC_CIRCUIT_STATE}{{{LABEL_OP}="{op}",{LABEL_STATE}="{state.value}"}} {value}'
                    )
        else:
            # Placeholder: no operations tracked yet
            for state in CircuitMetricState:
                lines.append(
                    f'{METRIC_CIRCUIT_STATE}{{{LABEL_OP}="none",{LABEL_STATE}="{state.value}"}} 0'
                )

        # H4 Circuit rejected
        lines.extend(
            [
                f"# HELP {METRIC_CIRCUIT_REJECTED} Total circuit breaker rejections by operation",
                f"# TYPE {METRIC_CIRCUIT_REJECTED} counter",
            ]
        )
        if self.circuit_rejected:
            for op, count in sorted(self.circuit_rejected.items()):
                lines.append(f'{METRIC_CIRCUIT_REJECTED}{{{LABEL_OP}="{op}"}} {count}')
        else:
            lines.append(f'{METRIC_CIRCUIT_REJECTED}{{{LABEL_OP}="none"}} 0')

        # H4 Circuit trips
        lines.extend(
            [
                f"# HELP {METRIC_CIRCUIT_TRIPS} Total circuit breaker trips by operation and reason",
                f"# TYPE {METRIC_CIRCUIT_TRIPS} counter",
            ]
        )
        if self.circuit_trips:
            for (op, reason), count in sorted(self.circuit_trips.items()):
                lines.append(
                    f'{METRIC_CIRCUIT_TRIPS}{{{LABEL_OP}="{op}",{LABEL_REASON}="{reason}"}} {count}'
                )
        else:
            lines.append(f'{METRIC_CIRCUIT_TRIPS}{{{LABEL_OP}="none",{LABEL_REASON}="none"}} 0')

        # WebSocket connected (gauge)
        lines.extend(
            [
                f"# HELP {METRIC_WS_CONNECTED} WebSocket connection state (1=connected, 0=disconnected)",
                f"# TYPE {METRIC_WS_CONNECTED} gauge",
            ]
        )
        if self.ws_connected:
            for connector, connected in sorted(self.ws_connected.items()):
                value = 1 if connected else 0
                lines.append(f'{METRIC_WS_CONNECTED}{{connector="{connector}"}} {value}')
        else:
            lines.append(f'{METRIC_WS_CONNECTED}{{connector="none"}} 0')

        # WebSocket reconnects (counter)
        lines.extend(
            [
                f"# HELP {METRIC_WS_RECONNECT_TOTAL} Total WebSocket reconnection events",
                f"# TYPE {METRIC_WS_RECONNECT_TOTAL} counter",
            ]
        )
        if self.ws_reconnect_total:
            for (connector, reason), count in sorted(self.ws_reconnect_total.items()):
                lines.append(
                    f'{METRIC_WS_RECONNECT_TOTAL}{{connector="{connector}",{LABEL_REASON}="{reason}"}} {count}'
                )
        else:
            lines.append(f'{METRIC_WS_RECONNECT_TOTAL}{{connector="none",{LABEL_REASON}="none"}} 0')

        # Ticks received (counter)
        lines.extend(
            [
                f"# HELP {METRIC_TICKS_RECEIVED_TOTAL} Total ticks received per connector",
                f"# TYPE {METRIC_TICKS_RECEIVED_TOTAL} counter",
            ]
        )
        if self.ticks_received:
            for connector, count in sorted(self.ticks_received.items()):
                lines.append(f'{METRIC_TICKS_RECEIVED_TOTAL}{{connector="{connector}"}} {count}')
        else:
            lines.append(f'{METRIC_TICKS_RECEIVED_TOTAL}{{connector="none"}} 0')

        # Last tick timestamp (gauge)
        lines.extend(
            [
                f"# HELP {METRIC_LAST_TICK_TS} Timestamp of last tick per connector (ms)",
                f"# TYPE {METRIC_LAST_TICK_TS} gauge",
            ]
        )
        if self.last_tick_ts:
            for connector, ts in sorted(self.last_tick_ts.items()):
                lines.append(f'{METRIC_LAST_TICK_TS}{{connector="{connector}"}} {ts}')
        else:
            lines.append(f'{METRIC_LAST_TICK_TS}{{connector="none"}} 0')

        # Unknown user-data events by event type (bounded cardinality).
        lines.extend(
            [
                f"# HELP {METRIC_USER_DATA_UNKNOWN_EVENTS_TOTAL} Unknown user-data events by type",
                f"# TYPE {METRIC_USER_DATA_UNKNOWN_EVENTS_TOTAL} counter",
            ]
        )
        if self.user_data_unknown_events:
            for event_type, count in sorted(self.user_data_unknown_events.items()):
                lines.append(
                    f'{METRIC_USER_DATA_UNKNOWN_EVENTS_TOTAL}{{{LABEL_EVENT_TYPE}="{event_type}"}} {count}'
                )
        else:
            lines.append(f'{METRIC_USER_DATA_UNKNOWN_EVENTS_TOTAL}{{{LABEL_EVENT_TYPE}="none"}} 0')

        return lines


# Global singleton
_metrics: ConnectorMetrics | None = None


def get_connector_metrics() -> ConnectorMetrics:
    """Get or create global connector metrics instance."""
    global _metrics  # noqa: PLW0603
    if _metrics is None:
        _metrics = ConnectorMetrics()
    return _metrics


def reset_connector_metrics() -> None:
    """Reset connector metrics (for testing)."""
    global _metrics  # noqa: PLW0603
    _metrics = None
