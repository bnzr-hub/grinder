"""Tests for EngineRegistry (PR-E1, ADR-134).

Covers:
- All 8 engine states defined
- All allowed transitions succeed
- Invalid transitions rejected
- Register and get
- Deregister returns ABSENT
- Duplicate register rejected
- list_present returns live states only
- Quarantine transition and release
- Determinism
- Timestamps updated
- find_missing / find_orphan helpers
"""

from __future__ import annotations

import pytest

from grinder.execution_plane.registry import (
    PRESENT_STATES,
    VALID_TRANSITIONS,
    DuplicateEngineError,
    EngineHandle,
    EngineRegistry,
    EngineState,
    InvalidEngineTransitionError,
)


class TestAllEngineStates:
    def test_exactly_8_states(self) -> None:
        assert len(EngineState) == 8

    def test_state_names(self) -> None:
        expected = {
            "ABSENT",
            "ACTIVATING",
            "ACTIVE",
            "GRACEFUL_EXIT",
            "SHUTTING_DOWN",
            "STOPPED",
            "FAILED",
            "QUARANTINED",
        }
        assert {s.value for s in EngineState} == expected


class TestAllowedTransitions:
    def test_full_lifecycle(self) -> None:
        """ABSENT -> ACTIVATING -> ACTIVE -> GRACEFUL_EXIT -> SHUTTING_DOWN -> STOPPED -> ABSENT."""
        reg = EngineRegistry()
        reg.register("BTC")
        assert reg.get_state("BTC") == EngineState.ACTIVATING

        reg.transition("BTC", EngineState.ACTIVE)
        assert reg.get_state("BTC") == EngineState.ACTIVE

        reg.transition("BTC", EngineState.GRACEFUL_EXIT)
        assert reg.get_state("BTC") == EngineState.GRACEFUL_EXIT

        reg.transition("BTC", EngineState.SHUTTING_DOWN)
        assert reg.get_state("BTC") == EngineState.SHUTTING_DOWN

        reg.transition("BTC", EngineState.STOPPED)
        assert reg.get_state("BTC") == EngineState.STOPPED

        reg.transition("BTC", EngineState.ABSENT)
        assert reg.get_state("BTC") == EngineState.ABSENT

    def test_activation_failure_path(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.transition("BTC", EngineState.FAILED)
        assert reg.get_state("BTC") == EngineState.FAILED

    def test_shutdown_failure_path(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.transition("BTC", EngineState.ACTIVE)
        reg.transition("BTC", EngineState.GRACEFUL_EXIT)
        reg.transition("BTC", EngineState.SHUTTING_DOWN)
        reg.transition("BTC", EngineState.FAILED)
        assert reg.get_state("BTC") == EngineState.FAILED

    def test_all_transitions_in_table(self) -> None:
        """Every edge in VALID_TRANSITIONS can be executed."""
        for from_state, to_state in VALID_TRANSITIONS:
            reg = EngineRegistry()
            handle = EngineHandle(symbol="X", state=from_state)
            reg._handles["X"] = handle
            reg.transition("X", to_state)
            assert reg.get_state("X") == to_state


class TestInvalidTransitions:
    def test_active_to_activating(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.transition("BTC", EngineState.ACTIVE)
        with pytest.raises(InvalidEngineTransitionError, match=r"ACTIVE.*ACTIVATING"):
            reg.transition("BTC", EngineState.ACTIVATING)

    def test_absent_to_stopped(self) -> None:
        reg = EngineRegistry()
        with pytest.raises(InvalidEngineTransitionError, match=r"ABSENT.*STOPPED"):
            reg.transition("BTC", EngineState.STOPPED)

    def test_state_unchanged_after_invalid(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.transition("BTC", EngineState.ACTIVE)
        with pytest.raises(InvalidEngineTransitionError):
            reg.transition("BTC", EngineState.ABSENT)
        assert reg.get_state("BTC") == EngineState.ACTIVE


class TestRegisterAndGet:
    def test_register_creates_activating(self) -> None:
        reg = EngineRegistry()
        handle = reg.register("BTC", engine_ref="fake_engine")
        assert handle.symbol == "BTC"
        assert handle.state == EngineState.ACTIVATING
        assert handle.engine_ref == "fake_engine"

    def test_get_returns_handle(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        handle = reg.get("BTC")
        assert handle is not None
        assert handle.symbol == "BTC"

    def test_get_unknown_returns_none(self) -> None:
        reg = EngineRegistry()
        assert reg.get("UNKNOWN") is None

    def test_get_state_unknown_returns_absent(self) -> None:
        reg = EngineRegistry()
        assert reg.get_state("UNKNOWN") == EngineState.ABSENT


class TestDeregister:
    def test_deregister_sets_absent(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.transition("BTC", EngineState.ACTIVE)
        reg.deregister("BTC")
        assert reg.get_state("BTC") == EngineState.ABSENT

    def test_deregister_unknown_is_noop(self) -> None:
        reg = EngineRegistry()
        reg.deregister("UNKNOWN")  # no error


class TestDuplicateRegister:
    def test_duplicate_rejected(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        with pytest.raises(DuplicateEngineError, match="BTC"):
            reg.register("BTC")

    def test_reregister_after_absent(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.transition("BTC", EngineState.ACTIVE)
        reg.deregister("BTC")
        handle = reg.register("BTC")  # allowed after ABSENT
        assert handle.state == EngineState.ACTIVATING


class TestListPresent:
    def test_only_present_states(self) -> None:
        reg = EngineRegistry()
        reg.register("A")  # ACTIVATING
        reg.register("B")
        reg.transition("B", EngineState.ACTIVE)  # ACTIVE
        reg.register("C")
        reg.transition("C", EngineState.FAILED)  # FAILED — not present

        present = reg.list_present()
        symbols = [h.symbol for h in present]
        assert "A" in symbols
        assert "B" in symbols
        assert "C" not in symbols

    def test_present_states_match_definition(self) -> None:
        assert (
            frozenset(
                {
                    EngineState.ACTIVATING,
                    EngineState.ACTIVE,
                    EngineState.GRACEFUL_EXIT,
                    EngineState.SHUTTING_DOWN,
                }
            )
            == PRESENT_STATES
        )


class TestQuarantine:
    def test_failed_to_quarantined(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.transition("BTC", EngineState.FAILED)
        reg.transition("BTC", EngineState.QUARANTINED)
        assert reg.get_state("BTC") == EngineState.QUARANTINED

    def test_quarantined_to_absent(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.transition("BTC", EngineState.FAILED)
        reg.transition("BTC", EngineState.QUARANTINED)
        reg.transition("BTC", EngineState.ABSENT)
        assert reg.get_state("BTC") == EngineState.ABSENT


class TestDeterminism:
    def test_same_sequence_same_result(self) -> None:
        def run() -> list[EngineState]:
            reg = EngineRegistry()
            reg.register("BTC")
            reg.transition("BTC", EngineState.ACTIVE)
            reg.transition("BTC", EngineState.GRACEFUL_EXIT)
            return [reg.get_state("BTC")]

        assert run() == run()


class TestTimestamps:
    def test_transition_updates_timestamp(self) -> None:
        t = [0.0]
        reg = EngineRegistry(_clock=lambda: t[0])

        t[0] = 1.0
        reg.register("BTC")
        handle = reg.get("BTC")
        assert handle is not None
        assert handle.created_at == 1.0
        assert handle.last_transition_at == 1.0

        t[0] = 5.0
        reg.transition("BTC", EngineState.ACTIVE)
        assert handle.last_transition_at == 5.0


class TestFindMissingOrphan:
    def test_find_missing(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.transition("BTC", EngineState.ACTIVE)

        missing = reg.find_missing({"BTC", "ETH", "SOL"})
        assert missing == {"ETH", "SOL"}

    def test_find_orphan(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.transition("BTC", EngineState.ACTIVE)
        reg.register("ETH")
        reg.transition("ETH", EngineState.ACTIVE)

        orphan = reg.find_orphan({"BTC"})
        assert orphan == {"ETH"}

    def test_no_missing_no_orphan(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.transition("BTC", EngineState.ACTIVE)

        assert reg.find_missing({"BTC"}) == set()
        assert reg.find_orphan({"BTC"}) == set()


class TestListSymbols:
    def test_all_tracked(self) -> None:
        reg = EngineRegistry()
        reg.register("BTC")
        reg.register("ETH")
        assert reg.list_symbols() == ["BTC", "ETH"]
