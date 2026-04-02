"""Startup contract summary for operator-facing truthful startup output.

Consolidates all runtime mode flags, gate statuses, and unsafe combination
warnings into a single deterministic block that operators can copy into
incident chat.

SSOT: this module. Printed once at startup by run_trading.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GateStatus(Enum):
    """Status of a startup gate."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    WARN = "WARN"
    NOT_APPLICABLE = "N/A"


@dataclass(frozen=True)
class GateResult:
    """Result of a single startup gate evaluation."""

    name: str
    status: GateStatus
    reason: str = ""


@dataclass(frozen=True)
class UnsafeComboWarning:
    """Warning about a misleading or incomplete feature combination."""

    code: str
    message: str
    severity: str = "WARN"  # WARN or BLOCK


@dataclass(frozen=True)
class RuntimeModeFlags:
    """Resolved effective runtime mode flags."""

    grid_v2: bool = False
    grid_v2_symbol: str = ""
    rolling_grid: bool = False
    planner: bool = False
    account_sync: bool = False
    execution_port: str = "noop"
    armed: bool = False
    mainnet: bool = False
    mode: str = "read_only"
    ha_enabled: bool = False


@dataclass
class StartupContractSummary:
    """Full startup contract summary."""

    gates: list[GateResult] = field(default_factory=list)
    mode_flags: RuntimeModeFlags = field(default_factory=RuntimeModeFlags)
    warnings: list[UnsafeComboWarning] = field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        """True if any gate FAIL or any BLOCK-severity warning."""
        return any(g.status == GateStatus.FAIL for g in self.gates) or any(
            w.severity == "BLOCK" for w in self.warnings
        )

    def format_summary(self) -> str:
        """Format the full startup contract as a single copyable block."""
        lines: list[str] = []
        lines.append("=" * 72)
        lines.append("STARTUP CONTRACT SUMMARY")
        lines.append("=" * 72)

        # Section 1: Effective runtime mode
        mf = self.mode_flags
        lines.append("")
        lines.append("  EFFECTIVE RUNTIME MODE")
        lines.append(f"    mode            = {mf.mode}")
        lines.append(f"    armed           = {mf.armed}")
        lines.append(f"    mainnet         = {mf.mainnet}")
        lines.append(f"    exchange_port   = {mf.execution_port}")
        lines.append(f"    ha_enabled      = {mf.ha_enabled}")
        lines.append(f"    grid_v2         = {mf.grid_v2}")
        if mf.grid_v2:
            lines.append(f"    grid_v2_symbol  = {mf.grid_v2_symbol or '(not set)'}")
        lines.append(f"    rolling_grid    = {mf.rolling_grid}")
        lines.append(f"    planner         = {mf.planner}")
        lines.append(f"    account_sync    = {mf.account_sync}")

        # Section 2: Gate results
        lines.append("")
        lines.append("  STARTUP GATES")
        for g in self.gates:
            reason_part = f" reason={g.reason}" if g.reason else ""
            lines.append(f"    {g.name:<24s} status={g.status.value}{reason_part}")

        # Section 3: Warnings
        if self.warnings:
            lines.append("")
            lines.append("  WARNINGS")
            for w in self.warnings:
                lines.append(f"    [{w.severity}] {w.code}: {w.message}")

        # Verdict
        lines.append("")
        if self.has_blockers:
            lines.append("  VERDICT: BLOCKED — fix blockers above before proceeding")
        else:
            lines.append("  VERDICT: OK")
        lines.append("=" * 72)
        return "\n".join(lines)


def detect_unsafe_combos(flags: RuntimeModeFlags) -> list[UnsafeComboWarning]:
    """Detect misleading or incomplete feature combinations.

    Pure function: no I/O, no env reads. All inputs via flags.
    """
    warnings: list[UnsafeComboWarning] = []

    # Rolling grid requires all three companion features
    if flags.rolling_grid and not flags.planner:
        warnings.append(
            UnsafeComboWarning(
                code="ROLLING_NO_PLANNER",
                message=(
                    "GRINDER_LIVE_ROLLING_GRID=true but GRINDER_LIVE_PLANNER_ENABLED=false. "
                    "Rolling grid will not replenish entries without planner."
                ),
            )
        )
    if flags.rolling_grid and not flags.account_sync:
        warnings.append(
            UnsafeComboWarning(
                code="ROLLING_NO_SYNC",
                message=(
                    "GRINDER_LIVE_ROLLING_GRID=true but GRINDER_ACCOUNT_SYNC_ENABLED=false. "
                    "Rolling grid requires account sync for fill detection."
                ),
            )
        )

    # Grid_v2 without account sync on futures
    if flags.grid_v2 and not flags.account_sync and flags.execution_port == "futures":
        warnings.append(
            UnsafeComboWarning(
                code="GRID_V2_NO_SYNC_FUTURES",
                message=(
                    "GRINDER_GRID_V2_ENABLED=true on futures without GRINDER_ACCOUNT_SYNC_ENABLED. "
                    "Grid_v2 may miss fills without account sync."
                ),
                severity="BLOCK",
            )
        )

    # Grid_v2 enabled without symbol
    if flags.grid_v2 and not flags.grid_v2_symbol:
        warnings.append(
            UnsafeComboWarning(
                code="GRID_V2_NO_SYMBOL",
                message="GRINDER_GRID_V2_ENABLED=true but GRINDER_GRID_V2_SYMBOL is not set.",
                severity="BLOCK",
            )
        )

    # Planner without account sync
    if flags.planner and not flags.account_sync:
        warnings.append(
            UnsafeComboWarning(
                code="PLANNER_NO_SYNC",
                message=(
                    "GRINDER_LIVE_PLANNER_ENABLED=true but GRINDER_ACCOUNT_SYNC_ENABLED=false. "
                    "Planner cannot observe fills without account sync."
                ),
            )
        )

    # Armed on mainnet without futures port (probably misconfigured)
    if flags.armed and flags.mainnet and flags.execution_port == "noop":
        warnings.append(
            UnsafeComboWarning(
                code="ARMED_MAINNET_NOOP",
                message=(
                    "armed=true mainnet=true but exchange_port=noop. "
                    "No real orders will be placed despite mainnet config."
                ),
            )
        )

    return warnings


def build_startup_contract_summary(
    *,
    mode: str,
    armed: bool,
    mainnet: bool,
    exchange_port: str,
    ha_enabled: bool,
    grid_v2: bool,
    grid_v2_symbol: str,
    rolling_grid: bool,
    planner: bool,
    account_sync: bool,
    preflight_passed: bool,
    skip_launch_guard: bool,
    launch_guard_status: str = "",
    launch_guard_reason: str = "",
) -> StartupContractSummary:
    """Build the full startup contract summary.

    Pure function: all inputs passed explicitly. No env reads, no I/O.
    """
    flags = RuntimeModeFlags(
        grid_v2=grid_v2,
        grid_v2_symbol=grid_v2_symbol,
        rolling_grid=rolling_grid,
        planner=planner,
        account_sync=account_sync,
        execution_port=exchange_port,
        armed=armed,
        mainnet=mainnet,
        mode=mode,
        ha_enabled=ha_enabled,
    )

    gates: list[GateResult] = []

    # Gate 1: Live preflight (always runs)
    gates.append(
        GateResult(
            name="LIVE_PREFLIGHT",
            status=GateStatus.PASS if preflight_passed else GateStatus.FAIL,
        )
    )

    # Gate 2: Launch guard (skippable)
    if skip_launch_guard:
        gates.append(
            GateResult(
                name="LAUNCH_GUARD",
                status=GateStatus.SKIP,
                reason="--skip-launch-guard",
            )
        )
    elif launch_guard_status:
        _lg_pass_statuses = {
            "verify_clean",
            "cleanup_then_clean",
            "recovery_non_flat_skip_cleanup",
        }
        _lg_skip_statuses = {"skipped"}
        if launch_guard_status in _lg_pass_statuses:
            lg_gate_status = GateStatus.PASS
        elif launch_guard_status in _lg_skip_statuses:
            lg_gate_status = GateStatus.SKIP
        else:
            lg_gate_status = GateStatus.FAIL
        gates.append(
            GateResult(
                name="LAUNCH_GUARD",
                status=lg_gate_status,
                reason=launch_guard_reason or launch_guard_status,
            )
        )
    else:
        gates.append(
            GateResult(
                name="LAUNCH_GUARD",
                status=GateStatus.NOT_APPLICABLE,
                reason="non-futures port",
            )
        )

    warnings = detect_unsafe_combos(flags)

    return StartupContractSummary(
        gates=gates,
        mode_flags=flags,
        warnings=warnings,
    )
