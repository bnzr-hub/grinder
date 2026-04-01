"""Engine registry and execution state model (PR-E1, ADR-134).

SSOT: docs/39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md Section 3.

Pure data model: stores per-symbol engine state, validates transitions.
No engine start/stop, no exchange I/O, no ceremony logic.

The registry owns:
- Per-symbol EngineHandle instances
- State transition validation
- Simple queries (get, list, detect missing/orphan)

The registry does NOT own:
- Engine lifecycle execution (ceremonies)
- Retry policies
- Operator controls
- Reconciliation logic
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EngineState(Enum):
    """Per-symbol execution lifecycle state (doc 39 Section 3.3)."""

    ABSENT = "ABSENT"
    ACTIVATING = "ACTIVATING"
    ACTIVE = "ACTIVE"
    GRACEFUL_EXIT = "GRACEFUL_EXIT"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


# Data-driven transition table: set of (from, to) pairs
VALID_TRANSITIONS: set[tuple[EngineState, EngineState]] = {
    (EngineState.ABSENT, EngineState.ACTIVATING),
    (EngineState.ACTIVATING, EngineState.ACTIVE),
    (EngineState.ACTIVATING, EngineState.FAILED),
    (EngineState.ACTIVE, EngineState.GRACEFUL_EXIT),
    (EngineState.GRACEFUL_EXIT, EngineState.SHUTTING_DOWN),
    (EngineState.SHUTTING_DOWN, EngineState.STOPPED),
    (EngineState.SHUTTING_DOWN, EngineState.FAILED),
    (EngineState.STOPPED, EngineState.ABSENT),
    (EngineState.FAILED, EngineState.QUARANTINED),
    (EngineState.QUARANTINED, EngineState.ABSENT),
}

# States that count as "present" (engine exists in some form)
PRESENT_STATES: frozenset[EngineState] = frozenset(
    {
        EngineState.ACTIVATING,
        EngineState.ACTIVE,
        EngineState.GRACEFUL_EXIT,
        EngineState.SHUTTING_DOWN,
    }
)


class InvalidEngineTransitionError(Exception):
    """Raised when an invalid engine state transition is attempted."""

    def __init__(self, symbol: str, current: EngineState, target: EngineState) -> None:
        self.symbol = symbol
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition {symbol} from {current.value} to {target.value}")


class DuplicateEngineError(Exception):
    """Raised when registering a symbol that already has a non-ABSENT engine."""

    def __init__(self, symbol: str, current_state: EngineState) -> None:
        self.symbol = symbol
        self.current_state = current_state
        super().__init__(f"Cannot register {symbol}: already exists in state {current_state.value}")


@dataclass
class EngineHandle:
    """Metadata for one per-symbol engine instance.

    Attributes:
        symbol: Trading pair.
        state: Current execution state.
        engine_ref: Opaque reference to the engine instance (None if not yet created).
        created_at: Timestamp when handle was created.
        last_transition_at: Timestamp of most recent state transition.
    """

    symbol: str
    state: EngineState = EngineState.ABSENT
    engine_ref: Any = None
    created_at: float = 0.0
    last_transition_at: float = 0.0


@dataclass
class EngineRegistry:
    """Tracks per-symbol engine instances and their execution state.

    Pure data store with transition validation. Does not execute
    engine operations — that is the coordinator's responsibility.
    """

    _handles: dict[str, EngineHandle] = field(default_factory=dict)
    _clock: Any = field(default=time.monotonic)

    def register(self, symbol: str, engine_ref: Any = None) -> EngineHandle:
        """Register a new engine for a symbol.

        Creates an EngineHandle in ACTIVATING state. Rejects duplicate
        registration if symbol already has a non-ABSENT engine.

        Args:
            symbol: Trading pair.
            engine_ref: Opaque reference to the engine instance.

        Returns:
            The created EngineHandle.

        Raises:
            DuplicateEngineError: If symbol already has a non-ABSENT engine.
        """
        existing = self._handles.get(symbol)
        if existing is not None and existing.state != EngineState.ABSENT:
            raise DuplicateEngineError(symbol, existing.state)

        now = self._clock()
        handle = EngineHandle(
            symbol=symbol,
            state=EngineState.ACTIVATING,
            engine_ref=engine_ref,
            created_at=now,
            last_transition_at=now,
        )
        self._handles[symbol] = handle
        return handle

    def deregister(self, symbol: str) -> None:
        """Remove a symbol's engine handle.

        Sets state to ABSENT and clears engine_ref. Safe to call on
        already-absent symbols (no-op).
        """
        handle = self._handles.get(symbol)
        if handle is not None:
            handle.state = EngineState.ABSENT
            handle.engine_ref = None
            handle.last_transition_at = self._clock()

    def get(self, symbol: str) -> EngineHandle | None:
        """Get engine handle for a symbol, or None if never registered."""
        return self._handles.get(symbol)

    def get_state(self, symbol: str) -> EngineState:
        """Get current state for a symbol. Returns ABSENT if unknown."""
        handle = self._handles.get(symbol)
        if handle is None:
            return EngineState.ABSENT
        return handle.state

    def transition(self, symbol: str, target: EngineState, reason: str = "") -> None:  # noqa: ARG002
        """Transition a symbol's engine to a new state.

        Validates the transition against VALID_TRANSITIONS.

        Args:
            symbol: Trading pair.
            target: Desired next state.
            reason: Optional reason string for audit.

        Raises:
            InvalidEngineTransitionError: If transition is not allowed.
        """
        current = self.get_state(symbol)
        if (current, target) not in VALID_TRANSITIONS:
            raise InvalidEngineTransitionError(symbol, current, target)

        handle = self._handles.get(symbol)
        if handle is None:
            # Transitioning from implicit ABSENT — create handle
            handle = EngineHandle(symbol=symbol, created_at=self._clock())
            self._handles[symbol] = handle

        handle.state = target
        handle.last_transition_at = self._clock()

    def list_present(self) -> list[EngineHandle]:
        """List engine handles in present states (ACTIVATING, ACTIVE, GRACEFUL_EXIT, SHUTTING_DOWN)."""
        return sorted(
            (h for h in self._handles.values() if h.state in PRESENT_STATES),
            key=lambda h: h.symbol,
        )

    def list_symbols(self) -> list[str]:
        """List all tracked symbols (including non-present states)."""
        return sorted(self._handles.keys())

    def find_missing(self, desired: set[str]) -> set[str]:
        """Find symbols desired but not in a present state."""
        present = {h.symbol for h in self._handles.values() if h.state in PRESENT_STATES}
        return desired - present

    def find_orphan(self, desired: set[str]) -> set[str]:
        """Find symbols in present state but not in desired set."""
        present = {h.symbol for h in self._handles.values() if h.state in PRESENT_STATES}
        return present - desired

    def reset(self) -> None:
        """Clear all handles (for testing)."""
        self._handles.clear()
