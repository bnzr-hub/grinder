"""Symbol lifecycle state machine for rotation controller (PR-C1a, ADR-127).

SSOT: docs/37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md Section 5.

Pure logic: no I/O, no logging, no metrics, no exchange interaction.
Deterministic: same (state, transition) -> same next_state.
Caller responsible for logging/metrics/evidence on transitions.

9 states, data-driven transition table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SymbolState(Enum):
    """Symbol lifecycle state (doc 37 Section 5).

    9 states covering the full discovery → trading → deactivation cycle.
    """

    DISCOVERED = "DISCOVERED"
    PREFILTER_BLOCKED = "PREFILTER_BLOCKED"
    ELIGIBLE = "ELIGIBLE"
    TUNING_CHECK = "TUNING_CHECK"
    NO_GO = "NO_GO"
    TUNED = "TUNED"
    ACTIVE = "ACTIVE"
    GRACEFUL_EXIT_ONLY = "GRACEFUL_EXIT_ONLY"
    COOLDOWN = "COOLDOWN"


class TransitionReason(Enum):
    """Reason code for state transitions (doc 37 Section 5)."""

    PREFILTER_PASS = "PREFILTER_PASS"
    PREFILTER_FAIL = "PREFILTER_FAIL"
    TUNING_STARTED = "TUNING_STARTED"
    TUNING_SUCCEEDED = "TUNING_SUCCEEDED"
    TUNING_FAILED = "TUNING_FAILED"
    SELECTED_INTO_TOP_K = "SELECTED_INTO_TOP_K"
    DROPPED_FROM_TOP_K = "DROPPED_FROM_TOP_K"
    POSITION_FLAT = "POSITION_FLAT"
    COOLDOWN_EXPIRED = "COOLDOWN_EXPIRED"
    RECHECK_PREFILTER_FAIL = "RECHECK_PREFILTER_FAIL"


# Data-driven transition table: (from_state, to_state) -> required reason
# Adding new transitions is additive — no if/else chains.
VALID_TRANSITIONS: dict[tuple[SymbolState, SymbolState], TransitionReason] = {
    (SymbolState.DISCOVERED, SymbolState.ELIGIBLE): TransitionReason.PREFILTER_PASS,
    (SymbolState.DISCOVERED, SymbolState.PREFILTER_BLOCKED): TransitionReason.PREFILTER_FAIL,
    (SymbolState.ELIGIBLE, SymbolState.TUNING_CHECK): TransitionReason.TUNING_STARTED,
    (SymbolState.TUNING_CHECK, SymbolState.TUNED): TransitionReason.TUNING_SUCCEEDED,
    (SymbolState.TUNING_CHECK, SymbolState.NO_GO): TransitionReason.TUNING_FAILED,
    (SymbolState.TUNED, SymbolState.ACTIVE): TransitionReason.SELECTED_INTO_TOP_K,
    (SymbolState.ACTIVE, SymbolState.GRACEFUL_EXIT_ONLY): TransitionReason.DROPPED_FROM_TOP_K,
    (SymbolState.GRACEFUL_EXIT_ONLY, SymbolState.COOLDOWN): TransitionReason.POSITION_FLAT,
    (SymbolState.COOLDOWN, SymbolState.ELIGIBLE): TransitionReason.COOLDOWN_EXPIRED,
    # Re-check paths: symbols can cycle back through prefilter/tuning
    (SymbolState.NO_GO, SymbolState.ELIGIBLE): TransitionReason.COOLDOWN_EXPIRED,
    (SymbolState.PREFILTER_BLOCKED, SymbolState.ELIGIBLE): TransitionReason.PREFILTER_PASS,
    (SymbolState.ELIGIBLE, SymbolState.PREFILTER_BLOCKED): TransitionReason.RECHECK_PREFILTER_FAIL,
}


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, current: SymbolState, target: SymbolState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition from {current.value} to {target.value}")


@dataclass(frozen=True)
class TransitionRecord:
    """Immutable record of a state transition.

    Attributes:
        from_state: State before transition.
        to_state: State after transition.
        reason: Why the transition occurred.
    """

    from_state: SymbolState
    to_state: SymbolState
    reason: TransitionReason


@dataclass
class SymbolLifecycle:
    """Lifecycle tracker for a single symbol.

    Manages current state and validates transitions against the
    data-driven transition table. Tracks transition history for
    audit/determinism verification.

    Attributes:
        symbol: Trading pair (e.g. "BTCUSDT").
        state: Current lifecycle state.
        history: Ordered list of transition records.
    """

    symbol: str
    state: SymbolState = SymbolState.DISCOVERED
    history: list[TransitionRecord] = field(default_factory=list)

    def transition(self, target: SymbolState, reason: TransitionReason) -> TransitionRecord:
        """Attempt a state transition.

        Validates the transition against VALID_TRANSITIONS. If valid,
        updates state and appends to history. If invalid, raises
        InvalidTransitionError.

        Args:
            target: Desired next state.
            reason: Why this transition is happening.

        Returns:
            TransitionRecord for the completed transition.

        Raises:
            InvalidTransitionError: If (current, target) is not in the
                valid transition table.
            InvalidTransitionError: If the reason does not match the
                required reason for this transition.
        """
        key = (self.state, target)
        required_reason = VALID_TRANSITIONS.get(key)

        if required_reason is None:
            raise InvalidTransitionError(self.state, target)

        if reason != required_reason:
            raise InvalidTransitionError(self.state, target)

        record = TransitionRecord(
            from_state=self.state,
            to_state=target,
            reason=reason,
        )
        self.state = target
        self.history.append(record)
        return record

    def can_transition(self, target: SymbolState) -> bool:
        """Check if a transition to target state is valid.

        Args:
            target: Desired next state.

        Returns:
            True if (current_state, target) is in the valid transition table.
        """
        return (self.state, target) in VALID_TRANSITIONS

    def required_reason(self, target: SymbolState) -> TransitionReason | None:
        """Get the required reason for a transition to target, or None if invalid."""
        return VALID_TRANSITIONS.get((self.state, target))
