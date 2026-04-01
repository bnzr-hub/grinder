"""Tests for symbol lifecycle state machine (PR-C1a, ADR-127).

Covers:
- All 9 states defined
- All valid transitions from doc 37 Section 5
- Invalid transitions rejected
- Transition reason codes emitted
- Determinism: same sequence → same state
- Re-entry paths (COOLDOWN → ELIGIBLE, NO_GO → ELIGIBLE, PREFILTER_BLOCKED → ELIGIBLE)
- No engine/selector imports
"""

from __future__ import annotations

import pytest

from grinder.rotation.state_machine import (
    VALID_TRANSITIONS,
    InvalidTransitionError,
    SymbolLifecycle,
    SymbolState,
    TransitionReason,
)


class TestAllNineStates:
    """SymbolState enum has exactly 9 members."""

    def test_state_count(self) -> None:
        assert len(SymbolState) == 9

    def test_state_names(self) -> None:
        expected = {
            "DISCOVERED",
            "PREFILTER_BLOCKED",
            "ELIGIBLE",
            "TUNING_CHECK",
            "NO_GO",
            "TUNED",
            "ACTIVE",
            "GRACEFUL_EXIT_ONLY",
            "COOLDOWN",
        }
        assert {s.value for s in SymbolState} == expected


class TestValidTransitions:
    """Each valid transition from doc 37 succeeds."""

    def test_discovered_to_eligible(self) -> None:
        lc = SymbolLifecycle(symbol="TEST")
        lc.transition(SymbolState.ELIGIBLE, TransitionReason.PREFILTER_PASS)
        assert lc.state == SymbolState.ELIGIBLE

    def test_discovered_to_prefilter_blocked(self) -> None:
        lc = SymbolLifecycle(symbol="TEST")
        lc.transition(SymbolState.PREFILTER_BLOCKED, TransitionReason.PREFILTER_FAIL)
        assert lc.state == SymbolState.PREFILTER_BLOCKED

    def test_eligible_to_tuning_check(self) -> None:
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.ELIGIBLE)
        lc.transition(SymbolState.TUNING_CHECK, TransitionReason.TUNING_STARTED)
        assert lc.state == SymbolState.TUNING_CHECK

    def test_tuning_check_to_tuned(self) -> None:
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.TUNING_CHECK)
        lc.transition(SymbolState.TUNED, TransitionReason.TUNING_SUCCEEDED)
        assert lc.state == SymbolState.TUNED

    def test_tuning_check_to_no_go(self) -> None:
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.TUNING_CHECK)
        lc.transition(SymbolState.NO_GO, TransitionReason.TUNING_FAILED)
        assert lc.state == SymbolState.NO_GO

    def test_tuned_to_active(self) -> None:
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.TUNED)
        lc.transition(SymbolState.ACTIVE, TransitionReason.SELECTED_INTO_TOP_K)
        assert lc.state == SymbolState.ACTIVE

    def test_active_to_graceful_exit_only(self) -> None:
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.ACTIVE)
        lc.transition(SymbolState.GRACEFUL_EXIT_ONLY, TransitionReason.DROPPED_FROM_TOP_K)
        assert lc.state == SymbolState.GRACEFUL_EXIT_ONLY

    def test_graceful_exit_to_cooldown(self) -> None:
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.GRACEFUL_EXIT_ONLY)
        lc.transition(SymbolState.COOLDOWN, TransitionReason.POSITION_FLAT)
        assert lc.state == SymbolState.COOLDOWN

    def test_cooldown_to_eligible(self) -> None:
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.COOLDOWN)
        lc.transition(SymbolState.ELIGIBLE, TransitionReason.COOLDOWN_EXPIRED)
        assert lc.state == SymbolState.ELIGIBLE


class TestReEntryPaths:
    """Symbols can cycle back through the lifecycle."""

    def test_no_go_to_eligible(self) -> None:
        """NO_GO symbol can re-enter ELIGIBLE on re-check."""
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.NO_GO)
        lc.transition(SymbolState.ELIGIBLE, TransitionReason.COOLDOWN_EXPIRED)
        assert lc.state == SymbolState.ELIGIBLE

    def test_prefilter_blocked_to_eligible(self) -> None:
        """PREFILTER_BLOCKED can re-enter ELIGIBLE on re-check pass."""
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.PREFILTER_BLOCKED)
        lc.transition(SymbolState.ELIGIBLE, TransitionReason.PREFILTER_PASS)
        assert lc.state == SymbolState.ELIGIBLE

    def test_eligible_to_prefilter_blocked(self) -> None:
        """ELIGIBLE can move to PREFILTER_BLOCKED on re-check fail."""
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.ELIGIBLE)
        lc.transition(SymbolState.PREFILTER_BLOCKED, TransitionReason.RECHECK_PREFILTER_FAIL)
        assert lc.state == SymbolState.PREFILTER_BLOCKED

    def test_full_lifecycle_round_trip(self) -> None:
        """Symbol goes through full lifecycle and re-enters."""
        lc = SymbolLifecycle(symbol="TEST")
        lc.transition(SymbolState.ELIGIBLE, TransitionReason.PREFILTER_PASS)
        lc.transition(SymbolState.TUNING_CHECK, TransitionReason.TUNING_STARTED)
        lc.transition(SymbolState.TUNED, TransitionReason.TUNING_SUCCEEDED)
        lc.transition(SymbolState.ACTIVE, TransitionReason.SELECTED_INTO_TOP_K)
        lc.transition(SymbolState.GRACEFUL_EXIT_ONLY, TransitionReason.DROPPED_FROM_TOP_K)
        lc.transition(SymbolState.COOLDOWN, TransitionReason.POSITION_FLAT)
        lc.transition(SymbolState.ELIGIBLE, TransitionReason.COOLDOWN_EXPIRED)
        assert lc.state == SymbolState.ELIGIBLE
        assert len(lc.history) == 7


class TestInvalidTransitions:
    """Invalid transitions raise InvalidTransitionError."""

    def test_discovered_to_active(self) -> None:
        lc = SymbolLifecycle(symbol="TEST")
        with pytest.raises(InvalidTransitionError, match=r"DISCOVERED.*ACTIVE"):
            lc.transition(SymbolState.ACTIVE, TransitionReason.SELECTED_INTO_TOP_K)

    def test_active_to_discovered(self) -> None:
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.ACTIVE)
        with pytest.raises(InvalidTransitionError, match=r"ACTIVE.*DISCOVERED"):
            lc.transition(SymbolState.DISCOVERED, TransitionReason.PREFILTER_PASS)

    def test_cooldown_to_active(self) -> None:
        """Cannot skip from COOLDOWN directly to ACTIVE."""
        lc = SymbolLifecycle(symbol="TEST", state=SymbolState.COOLDOWN)
        with pytest.raises(InvalidTransitionError, match=r"COOLDOWN.*ACTIVE"):
            lc.transition(SymbolState.ACTIVE, TransitionReason.SELECTED_INTO_TOP_K)

    def test_wrong_reason_for_valid_edge(self) -> None:
        """Valid edge but wrong reason code is rejected."""
        lc = SymbolLifecycle(symbol="TEST")
        with pytest.raises(InvalidTransitionError):
            lc.transition(SymbolState.ELIGIBLE, TransitionReason.TUNING_SUCCEEDED)

    def test_state_unchanged_after_invalid(self) -> None:
        """State does not change after a rejected transition."""
        lc = SymbolLifecycle(symbol="TEST")
        with pytest.raises(InvalidTransitionError):
            lc.transition(SymbolState.ACTIVE, TransitionReason.SELECTED_INTO_TOP_K)
        assert lc.state == SymbolState.DISCOVERED
        assert len(lc.history) == 0


class TestTransitionReasonCodes:
    """Each transition records a reason code."""

    def test_reason_in_record(self) -> None:
        lc = SymbolLifecycle(symbol="TEST")
        record = lc.transition(SymbolState.ELIGIBLE, TransitionReason.PREFILTER_PASS)
        assert record.reason == TransitionReason.PREFILTER_PASS
        assert record.from_state == SymbolState.DISCOVERED
        assert record.to_state == SymbolState.ELIGIBLE

    def test_history_records_all_reasons(self) -> None:
        lc = SymbolLifecycle(symbol="TEST")
        lc.transition(SymbolState.ELIGIBLE, TransitionReason.PREFILTER_PASS)
        lc.transition(SymbolState.TUNING_CHECK, TransitionReason.TUNING_STARTED)
        assert len(lc.history) == 2
        assert lc.history[0].reason == TransitionReason.PREFILTER_PASS
        assert lc.history[1].reason == TransitionReason.TUNING_STARTED


class TestCanTransition:
    """can_transition() and required_reason() helpers."""

    def test_can_transition_valid(self) -> None:
        lc = SymbolLifecycle(symbol="TEST")
        assert lc.can_transition(SymbolState.ELIGIBLE) is True
        assert lc.can_transition(SymbolState.PREFILTER_BLOCKED) is True

    def test_can_transition_invalid(self) -> None:
        lc = SymbolLifecycle(symbol="TEST")
        assert lc.can_transition(SymbolState.ACTIVE) is False

    def test_required_reason(self) -> None:
        lc = SymbolLifecycle(symbol="TEST")
        assert lc.required_reason(SymbolState.ELIGIBLE) == TransitionReason.PREFILTER_PASS
        assert lc.required_reason(SymbolState.ACTIVE) is None


class TestDeterminism:
    """Same inputs produce identical results."""

    def test_same_sequence_same_state(self) -> None:
        steps = [
            (SymbolState.ELIGIBLE, TransitionReason.PREFILTER_PASS),
            (SymbolState.TUNING_CHECK, TransitionReason.TUNING_STARTED),
            (SymbolState.TUNED, TransitionReason.TUNING_SUCCEEDED),
            (SymbolState.ACTIVE, TransitionReason.SELECTED_INTO_TOP_K),
        ]
        lc1 = SymbolLifecycle(symbol="A")
        lc2 = SymbolLifecycle(symbol="A")
        for target, reason in steps:
            lc1.transition(target, reason)
            lc2.transition(target, reason)
        assert lc1.state == lc2.state
        assert lc1.history == lc2.history


class TestTransitionTableCompleteness:
    """Verify transition table covers all doc 37 edges."""

    def test_all_doc37_edges_present(self) -> None:
        """The 9 primary edges from doc 37 Section 5 are in the table."""
        expected_edges = [
            (SymbolState.DISCOVERED, SymbolState.ELIGIBLE),
            (SymbolState.DISCOVERED, SymbolState.PREFILTER_BLOCKED),
            (SymbolState.ELIGIBLE, SymbolState.TUNING_CHECK),
            (SymbolState.TUNING_CHECK, SymbolState.TUNED),
            (SymbolState.TUNING_CHECK, SymbolState.NO_GO),
            (SymbolState.TUNED, SymbolState.ACTIVE),
            (SymbolState.ACTIVE, SymbolState.GRACEFUL_EXIT_ONLY),
            (SymbolState.GRACEFUL_EXIT_ONLY, SymbolState.COOLDOWN),
            (SymbolState.COOLDOWN, SymbolState.ELIGIBLE),
        ]
        for edge in expected_edges:
            assert edge in VALID_TRANSITIONS, f"Missing edge: {edge[0].value} → {edge[1].value}"

    def test_reentry_edges_present(self) -> None:
        """Re-entry edges from diagram are in the table."""
        reentry_edges = [
            (SymbolState.NO_GO, SymbolState.ELIGIBLE),
            (SymbolState.PREFILTER_BLOCKED, SymbolState.ELIGIBLE),
            (SymbolState.ELIGIBLE, SymbolState.PREFILTER_BLOCKED),
        ]
        for edge in reentry_edges:
            assert edge in VALID_TRANSITIONS, (
                f"Missing re-entry edge: {edge[0].value} → {edge[1].value}"
            )


class TestNoEngineImports:
    """State machine has no engine/selector dependencies."""

    def test_no_forbidden_imports(self) -> None:
        import grinder.rotation.state_machine as sm  # noqa: PLC0415

        source = sm.__file__
        assert source is not None
        from pathlib import Path  # noqa: PLC0415

        content = Path(source).read_text()
        for forbidden in (
            "grinder.live",
            "grinder.execution",
            "grinder.grid_v2",
            "grinder.selection",
        ):
            assert forbidden not in content, (
                f"Forbidden import {forbidden!r} found in state_machine.py"
            )
