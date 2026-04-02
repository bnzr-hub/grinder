"""Tests for structured operator-facing events (ADR-146)."""

from __future__ import annotations

from grinder.observability.structured_events import (
    ConstraintSource,
    ResolvedConstraint,
    SafetyBlockEvent,
    format_execution_gate_result,
    format_resolved_constraints,
    format_safety_summary,
)


class TestResolvedConstraints:
    def test_resolved_metadata_summary_reports_source(self) -> None:
        """Active symbol metadata summary includes source label."""
        constraints = [
            ResolvedConstraint(
                symbol="BTCUSDT",
                tick_size="0.10",
                step_size="0.001",
                min_qty="0.001",
                min_notional="100",
                source=ConstraintSource.CACHE,
            ),
        ]
        output = format_resolved_constraints(constraints)
        assert "source=cache" in output
        assert "BTCUSDT" in output
        assert "tick=0.10" in output

    def test_cache_vs_exchange_source_labeled(self) -> None:
        """Different sources produce different labels."""
        c_cache = ResolvedConstraint(
            symbol="A",
            tick_size="0.01",
            step_size="1",
            min_qty="1",
            min_notional="5",
            source=ConstraintSource.CACHE,
        )
        c_exchange = ResolvedConstraint(
            symbol="B",
            tick_size="0.01",
            step_size="1",
            min_qty="1",
            min_notional="5",
            source=ConstraintSource.EXCHANGE,
        )
        assert "source=cache" in format_resolved_constraints([c_cache])
        assert "source=exchange" in format_resolved_constraints([c_exchange])

    def test_stale_cache_source(self) -> None:
        c = ResolvedConstraint(
            symbol="X",
            tick_size="0.01",
            step_size="1",
            min_qty="1",
            min_notional="5",
            source=ConstraintSource.STALE_CACHE,
        )
        assert "source=stale_cache" in format_resolved_constraints([c])

    def test_empty_constraints(self) -> None:
        output = format_resolved_constraints([])
        assert "0 symbols" in output

    def test_multiple_symbols_sorted(self) -> None:
        constraints = [
            ResolvedConstraint(
                symbol="ETHUSDT",
                tick_size="0.01",
                step_size="0.01",
                min_qty="0.01",
                min_notional="10",
                source=ConstraintSource.CACHE,
            ),
            ResolvedConstraint(
                symbol="BTCUSDT",
                tick_size="0.10",
                step_size="0.001",
                min_qty="0.001",
                min_notional="100",
                source=ConstraintSource.CACHE,
            ),
        ]
        output = format_resolved_constraints(constraints)
        lines = output.strip().split("\n")
        # Header + 2 symbols, sorted alphabetically
        assert len(lines) == 3
        assert "BTCUSDT" in lines[1]
        assert "ETHUSDT" in lines[2]

    def test_format_is_deterministic(self) -> None:
        constraints = [
            ResolvedConstraint(
                symbol="BTCUSDT",
                tick_size="0.10",
                step_size="0.001",
                min_qty="0.001",
                min_notional="100",
                source=ConstraintSource.CACHE,
            ),
        ]
        assert format_resolved_constraints(constraints) == format_resolved_constraints(constraints)


class TestSafetyBlockEvent:
    def test_stl_block_event_includes_rule_symbol_action(self) -> None:
        """Stable structured output for safety block."""
        event = SafetyBlockEvent(
            rule="STL_E3_ORPHAN_ENGINE",
            symbols=["BTCUSDT"],
            action_blocked="ACTIVATE_ENGINE",
        )
        output = event.format()
        assert "rule=STL_E3_ORPHAN_ENGINE" in output
        assert "symbols=BTCUSDT" in output
        assert "action=ACTIVATE_ENGINE" in output
        assert output.startswith("SAFETY_BLOCK")

    def test_stl_block_event_with_reason(self) -> None:
        event = SafetyBlockEvent(
            rule="STL_E5_MISMATCH_THRESHOLD",
            symbols=["ETHUSDT", "BTCUSDT"],
            action_blocked="EXECUTE",
            reason="mismatch_count=7",
        )
        output = event.format()
        assert "reason=mismatch_count=7" in output
        assert "symbols=ETHUSDT,BTCUSDT" in output

    def test_stl_block_event_global(self) -> None:
        """No symbols = global block."""
        event = SafetyBlockEvent(
            rule="STL_E5_MISMATCH_THRESHOLD",
            symbols=[],
            action_blocked="ALL",
        )
        assert "symbols=(global)" in event.format()

    def test_stl_block_event_deterministic(self) -> None:
        event = SafetyBlockEvent(
            rule="STL_E3_ORPHAN_ENGINE",
            symbols=["X"],
            action_blocked="Y",
        )
        assert event.format() == event.format()


class TestSafetySummary:
    def test_format_safety_summary_allowed(self) -> None:
        output = format_safety_summary(
            execution_allowed=True,
            triggered_rules=[],
        )
        assert "allowed=True" in output
        assert "rules=[]" in output

    def test_format_safety_summary_blocked(self) -> None:
        output = format_safety_summary(
            execution_allowed=False,
            triggered_rules=["STL_E3_ORPHAN_ENGINE"],
            quarantine_symbols={"BTCUSDT"},
        )
        assert "allowed=False" in output
        assert "STL_E3_ORPHAN_ENGINE" in output
        assert "BTCUSDT" in output

    def test_format_safety_summary_deterministic(self) -> None:
        r1 = format_safety_summary(
            execution_allowed=False,
            triggered_rules=["STL_E3_ORPHAN_ENGINE", "STL_E5_MISMATCH_THRESHOLD"],
            quarantine_symbols={"A", "B"},
        )
        r2 = format_safety_summary(
            execution_allowed=False,
            triggered_rules=["STL_E3_ORPHAN_ENGINE", "STL_E5_MISMATCH_THRESHOLD"],
            quarantine_symbols={"A", "B"},
        )
        assert r1 == r2


class TestExecutionGateResult:
    def test_format_with_run_id(self) -> None:
        output = format_execution_gate_result(
            gate="SAFETY",
            passed=False,
            reason="STL_E3",
            run_id="20260402-143052-a7f3",
        )
        assert "gate=SAFETY" in output
        assert "passed=False" in output
        assert "reason=STL_E3" in output
        assert "run_id=20260402-143052-a7f3" in output

    def test_format_without_optional_fields(self) -> None:
        output = format_execution_gate_result(gate="ACK", passed=True)
        assert "gate=ACK" in output
        assert "passed=True" in output
        assert "reason=" not in output
        assert "run_id=" not in output

    def test_format_deterministic(self) -> None:
        r1 = format_execution_gate_result(gate="X", passed=True, run_id="abc")
        r2 = format_execution_gate_result(gate="X", passed=True, run_id="abc")
        assert r1 == r2
