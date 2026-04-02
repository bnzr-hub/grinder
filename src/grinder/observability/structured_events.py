"""Structured operator-facing events for incident triage (ADR-146).

Provides typed event helpers that produce deterministic, grep-friendly
log lines with stable event names and consistent key=value fields.

All functions are pure: no I/O, no logging calls. Callers decide
whether to print() or logger.info() the formatted output.

SSOT: this module for event shapes. Callers for emission.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConstraintSource(Enum):
    """Where symbol constraints were resolved from."""

    EXCHANGE = "exchange"
    CACHE = "cache"
    STALE_CACHE = "stale_cache"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ResolvedConstraint:
    """Per-symbol resolved constraint with source label."""

    symbol: str
    tick_size: str
    step_size: str
    min_qty: str
    min_notional: str
    source: ConstraintSource


def format_resolved_constraints(
    constraints: list[ResolvedConstraint],
) -> str:
    """Format resolved constraints as a structured block.

    Example output:
        RESOLVED CONSTRAINTS (source=cache, 2 symbols)
          BTCUSDT  tick=0.10  step=0.001  min_qty=0.001  min_notional=100
          DRIFTUSDT  tick=0.0001  step=1  min_qty=1  min_notional=5
    """
    if not constraints:
        return "RESOLVED CONSTRAINTS (0 symbols)"
    source = constraints[0].source.value
    lines = [f"RESOLVED CONSTRAINTS (source={source}, {len(constraints)} symbols)"]
    for c in sorted(constraints, key=lambda x: x.symbol):
        lines.append(
            f"  {c.symbol}  tick={c.tick_size}  step={c.step_size}"
            f"  min_qty={c.min_qty}  min_notional={c.min_notional}"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class SafetyBlockEvent:
    """Structured event for stop-the-line safety block."""

    rule: str
    symbols: list[str]
    action_blocked: str
    reason: str = ""

    def format(self) -> str:
        """Format as grep-friendly structured log line."""
        syms = ",".join(self.symbols) if self.symbols else "(global)"
        reason_part = f" reason={self.reason}" if self.reason else ""
        return (
            f"SAFETY_BLOCK rule={self.rule} symbols={syms}"
            f" action={self.action_blocked}{reason_part}"
        )


def format_safety_summary(
    *,
    execution_allowed: bool,
    triggered_rules: list[str],
    quarantine_symbols: set[str] | None = None,
) -> str:
    """Format safety evaluation summary as structured line.

    Example: SAFETY_EVAL allowed=False rules=[STL_E3_ORPHAN_ENGINE] quarantine=[]
    """
    qs = sorted(quarantine_symbols) if quarantine_symbols else []
    return f"SAFETY_EVAL allowed={execution_allowed} rules={triggered_rules} quarantine={qs}"


def format_execution_gate_result(
    *,
    gate: str,
    passed: bool,
    reason: str = "",
    run_id: str = "",
) -> str:
    """Format execution gate result as structured line.

    Example: EXECUTION_GATE gate=SAFETY passed=False reason=STL_E3 run_id=20260402-143052-a7f3
    """
    parts = [f"EXECUTION_GATE gate={gate} passed={passed}"]
    if reason:
        parts.append(f"reason={reason}")
    if run_id:
        parts.append(f"run_id={run_id}")
    return " ".join(parts)
