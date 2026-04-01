"""Desired-vs-actual engine reconciliation (PR-E4, ADR-137).

SSOT: docs/39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md Section 5.
      docs/40_EXECUTION_PLANE_IMPLEMENTATION_PLAN.md Phase E4.

Compares desired engine set (from control-plane) with actual state
(from EngineRegistry) and emits bounded corrective actions.

Pure reconciliation: does NOT execute engine operations.
Caller (coordinator) executes returned actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from grinder.execution_plane.registry import EngineRegistry, EngineState


class CorrectiveActionKind(Enum):
    """Symbolic corrective action types."""

    ACTIVATE_ENGINE = "ACTIVATE_ENGINE"
    REQUEST_GRACEFUL_EXIT = "REQUEST_GRACEFUL_EXIT"
    FINALIZE_DEACTIVATION = "FINALIZE_DEACTIVATION"
    QUARANTINE_ENGINE = "QUARANTINE_ENGINE"


@dataclass(frozen=True)
class CorrectiveAction:
    """One corrective action intent.

    Not executed — caller processes these externally.
    """

    kind: CorrectiveActionKind
    symbol: str
    reason: str = ""


class MismatchKind(Enum):
    """Categories of desired-vs-actual mismatches."""

    MISSING = "MISSING"
    ORPHAN = "ORPHAN"
    STATE_MISMATCH = "STATE_MISMATCH"


@dataclass(frozen=True)
class Mismatch:
    """One detected mismatch between desired and actual state."""

    kind: MismatchKind
    symbol: str
    actual_state: EngineState
    detail: str = ""


@dataclass(frozen=True)
class ReconcileConfig:
    """Configuration for reconciliation.

    Attributes:
        max_actions_per_cycle: Maximum corrective actions emitted per reconcile.
    """

    max_actions_per_cycle: int = 3


@dataclass(frozen=True)
class ReconcileReport:
    """Result of one reconciliation cycle.

    Attributes:
        mismatches: All detected mismatches (before bounding).
        actions: Bounded corrective actions (up to max_actions_per_cycle).
        truncated: True if more actions were needed than allowed.
    """

    mismatches: list[Mismatch] = field(default_factory=list)
    actions: list[CorrectiveAction] = field(default_factory=list)
    truncated: bool = False


def reconcile(
    desired: set[str],
    registry: EngineRegistry,
    config: ReconcileConfig | None = None,
) -> ReconcileReport:
    """Compare desired engine set with actual registry state.

    Detects mismatches and emits bounded corrective actions.
    Deterministic: same inputs -> same report (sorted by symbol).

    Args:
        desired: Symbols that should have a live engine.
        registry: Current engine state.
        config: Reconciliation configuration.

    Returns:
        ReconcileReport with mismatches and bounded actions.
    """
    cfg = config or ReconcileConfig()
    mismatches: list[Mismatch] = []
    actions: list[CorrectiveAction] = []

    # Collect actual present symbols
    present = {h.symbol: h.state for h in registry.list_present()}
    all_tracked = {s: registry.get_state(s) for s in registry.list_symbols()}

    # --- Detect orphan engines (present but not desired) ---
    for symbol in sorted(present.keys() - desired):
        state = present[symbol]
        mismatches.append(
            Mismatch(
                kind=MismatchKind.ORPHAN,
                symbol=symbol,
                actual_state=state,
                detail=f"present as {state.value} but not in desired set",
            )
        )
        if state == EngineState.ACTIVE:
            actions.append(
                CorrectiveAction(
                    kind=CorrectiveActionKind.REQUEST_GRACEFUL_EXIT,
                    symbol=symbol,
                    reason="orphan_active",
                )
            )
        elif state == EngineState.GRACEFUL_EXIT:
            actions.append(
                CorrectiveAction(
                    kind=CorrectiveActionKind.FINALIZE_DEACTIVATION,
                    symbol=symbol,
                    reason="orphan_graceful_exit",
                )
            )

    # --- Detect missing engines (desired but not present) ---
    for symbol in sorted(desired - present.keys()):
        actual = all_tracked.get(symbol, EngineState.ABSENT)
        mismatches.append(
            Mismatch(
                kind=MismatchKind.MISSING,
                symbol=symbol,
                actual_state=actual,
                detail=f"desired but actual state is {actual.value}",
            )
        )
        if actual in (EngineState.ABSENT, EngineState.STOPPED):
            actions.append(
                CorrectiveAction(
                    kind=CorrectiveActionKind.ACTIVATE_ENGINE,
                    symbol=symbol,
                    reason="missing_desired",
                )
            )
        elif actual == EngineState.FAILED:
            actions.append(
                CorrectiveAction(
                    kind=CorrectiveActionKind.QUARANTINE_ENGINE,
                    symbol=symbol,
                    reason="missing_desired_but_failed",
                )
            )

    # --- Detect state mismatches (desired + present but wrong state) ---
    for symbol in sorted(desired & present.keys()):
        state = present[symbol]
        if state == EngineState.SHUTTING_DOWN:
            mismatches.append(
                Mismatch(
                    kind=MismatchKind.STATE_MISMATCH,
                    symbol=symbol,
                    actual_state=state,
                    detail="desired active but shutting down",
                )
            )

    # --- Bound actions ---
    truncated = len(actions) > cfg.max_actions_per_cycle
    bounded_actions = actions[: cfg.max_actions_per_cycle]

    return ReconcileReport(
        mismatches=mismatches,
        actions=bounded_actions,
        truncated=truncated,
    )
