"""Stable reason codes for live observability (ADR-106).

Every major suppressed/degraded/no-op path emits one of these reason codes.
Operators can explain system state from reason codes without log correlation.

Families:
- NoActionReason: why a sync cycle produced zero actions.
- EntrySuppressionReason: why entry placement was suppressed.
- ExitSuppressionReason: why exit placement was suppressed/blocked.
- HealthBlockReason: why writes were blocked by health gate.
- RepairStatusReason: repair lifecycle status.
"""

from __future__ import annotations

from enum import Enum


class NoActionReason(Enum):
    """Why a sync cycle produced zero repair/reconcile actions."""

    ACTUAL_MATCHES_EFFECTIVE_TARGET = "ACTUAL_MATCHES_EFFECTIVE_TARGET"
    RISK_SATURATED_TARGET_ZERO = "RISK_SATURATED_TARGET_ZERO"
    EFFECTIVE_TARGET_PARTIAL_MATCHED = "EFFECTIVE_TARGET_PARTIAL_MATCHED"
    AWAITING_SYNC = "AWAITING_SYNC"
    NOT_STARTED = "NOT_STARTED"
    RECONSTRUCTION_PENDING = "RECONSTRUCTION_PENDING"


class EntrySuppressionReason(Enum):
    """Why entry placement was suppressed."""

    RISK_SATURATED = "RISK_SATURATED"
    EFFECTIVE_TARGET_ZERO = "EFFECTIVE_TARGET_ZERO"
    EFFECTIVE_TARGET_PARTIAL = "EFFECTIVE_TARGET_PARTIAL"
    NO_RESTORE_DEMAND = "NO_RESTORE_DEMAND"
    INVENTORY_FULL = "INVENTORY_FULL"


class ExitSuppressionReason(Enum):
    """Why exit placement was suppressed or blocked."""

    REDUCE_ONLY_BUDGET_EXCEEDED = "REDUCE_ONLY_BUDGET_EXCEEDED"
    EXIT_NOT_REGISTERED_DEFERRED = "EXIT_NOT_REGISTERED_DEFERRED"
    PENDING_REPAIR_AFTER_REJECT = "PENDING_REPAIR_AFTER_REJECT"
    TOPOLOGY_REPAIR_INCOMPLETE = "TOPOLOGY_REPAIR_INCOMPLETE"


class HealthBlockReason(Enum):
    """Why writes were blocked by health gate."""

    STALE_TRUTH = "STALE_TRUTH"
    PAUSED_UNSAFE = "PAUSED_UNSAFE"
    DEGRADED_WS = "DEGRADED_WS"
    DEGRADED_SYNC = "DEGRADED_SYNC"


class RepairStatusReason(Enum):
    """Repair lifecycle status."""

    START = "START"
    CANCEL = "CANCEL"
    PLACE = "PLACE"
    DEFERRED = "DEFERRED"
    CONVERGED = "CONVERGED"
    INCOMPLETE = "INCOMPLETE"


def classify_no_action_reason(  # noqa: PLR0911
    *,
    recon_has_diff: bool,
    theoretical_entries: int,
    effective_entries: int,
    actual_entries: int,
    is_risk_saturated: bool,
    is_awaiting_sync: bool,
    is_started: bool,
    reconstruction_ok: bool,
) -> NoActionReason | None:
    """Classify why a sync cycle produced no actions.

    Returns None if there ARE actions (caller should not use this).
    """
    if recon_has_diff:
        return None  # there are actions, no reason needed

    if not is_started:
        return NoActionReason.NOT_STARTED
    if is_awaiting_sync:
        return NoActionReason.AWAITING_SYNC
    if not reconstruction_ok:
        return NoActionReason.RECONSTRUCTION_PENDING

    if is_risk_saturated and effective_entries == 0 and theoretical_entries > 0:
        return NoActionReason.RISK_SATURATED_TARGET_ZERO

    if effective_entries < theoretical_entries and actual_entries == effective_entries:
        return NoActionReason.EFFECTIVE_TARGET_PARTIAL_MATCHED

    return NoActionReason.ACTUAL_MATCHES_EFFECTIVE_TARGET
