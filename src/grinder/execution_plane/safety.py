"""Execution-plane safety evaluator (PR-E5, ADR-138).

SSOT: docs/39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md Section 6.

Evaluates execution conditions and decides whether actions are allowed.
Pure logic: does not execute engine operations.

STL-E rules implemented (from doc 39):
- STL-E3: Orphan engine detected
- STL-E4: Duplicate engine (via registry — already enforced in E1)
- STL-E5: Mismatch count exceeds threshold

STL-E rules deferred (require runtime health data not yet available):
- STL-E1: Repeated activation failure (needs failure counter history)
- STL-E2: Deactivation cleanup failure (needs cleanup attempt history)
- STL-E6: Engine health check failure (needs per-engine health telemetry)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.execution_plane.reconciler import ReconcileReport


class StopRule(Enum):
    """Execution-plane stop-the-line rules."""

    STL_E3_ORPHAN_ENGINE = "STL_E3_ORPHAN_ENGINE"
    STL_E5_MISMATCH_THRESHOLD = "STL_E5_MISMATCH_THRESHOLD"


@dataclass(frozen=True)
class SafetyConfig:
    """Configuration for safety evaluator.

    Attributes:
        mismatch_threshold: Max mismatches before STL-E5 triggers.
    """

    mismatch_threshold: int = 5


@dataclass(frozen=True)
class SafetyResult:
    """Result of safety evaluation.

    Attributes:
        execution_allowed: Whether new execution actions are permitted.
        triggered_rules: Which stop-the-line rules fired.
        quarantine_symbols: Symbols that should be quarantined.
    """

    execution_allowed: bool = True
    triggered_rules: list[StopRule] = field(default_factory=list)
    quarantine_symbols: set[str] = field(default_factory=set)


def evaluate_safety(
    reconcile_report: ReconcileReport,
    paused: bool = False,
    config: SafetyConfig | None = None,
) -> SafetyResult:
    """Evaluate execution safety based on reconciliation report.

    Pure function. Same inputs -> same result.

    Args:
        reconcile_report: Latest reconciliation report.
        paused: Whether operator has paused execution.
        config: Safety thresholds.

    Returns:
        SafetyResult indicating whether execution is allowed.
    """
    cfg = config or SafetyConfig()
    triggered: list[StopRule] = []

    if paused:
        return SafetyResult(execution_allowed=False, triggered_rules=[])

    from grinder.execution_plane.reconciler import MismatchKind  # noqa: PLC0415

    # STL-E3: Orphan engine detected
    orphans = [m for m in reconcile_report.mismatches if m.kind == MismatchKind.ORPHAN]
    if orphans:
        triggered.append(StopRule.STL_E3_ORPHAN_ENGINE)

    # STL-E5: Mismatch count exceeds threshold
    if len(reconcile_report.mismatches) > cfg.mismatch_threshold:
        triggered.append(StopRule.STL_E5_MISMATCH_THRESHOLD)

    execution_allowed = len(triggered) == 0

    return SafetyResult(
        execution_allowed=execution_allowed,
        triggered_rules=triggered,
    )
