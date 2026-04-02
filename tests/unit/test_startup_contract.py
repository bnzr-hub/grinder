"""Tests for startup contract summary (ADR-143).

Covers:
- Gate separation (preflight vs launch guard)
- Effective runtime mode summary
- Unsafe combination detection
- Determinism
"""

from __future__ import annotations

from grinder.live.startup_contract import (
    GateStatus,
    RuntimeModeFlags,
    StartupContractSummary,
    build_startup_contract_summary,
    detect_unsafe_combos,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_summary(**overrides: object) -> StartupContractSummary:
    """Build summary with safe defaults, overriding specific fields."""
    kwargs: dict[str, object] = {
        "mode": "read_only",
        "armed": False,
        "mainnet": False,
        "exchange_port": "noop",
        "ha_enabled": False,
        "grid_v2": False,
        "grid_v2_symbol": "",
        "rolling_grid": False,
        "planner": False,
        "account_sync": False,
        "preflight_passed": True,
        "skip_launch_guard": False,
        "launch_guard_status": "verify_clean",
        "launch_guard_reason": "all symbols clean",
    }
    kwargs.update(overrides)
    return build_startup_contract_summary(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T1: skip_launch_guard does not skip live preflight
# ---------------------------------------------------------------------------


class TestGateSeparation:
    def test_skip_launch_guard_does_not_skip_live_preflight(self) -> None:
        """--skip-launch-guard skips LAUNCH_GUARD but LIVE_PREFLIGHT still evaluated."""
        summary = _default_summary(skip_launch_guard=True, preflight_passed=True)
        preflight = next(g for g in summary.gates if g.name == "LIVE_PREFLIGHT")
        launch = next(g for g in summary.gates if g.name == "LAUNCH_GUARD")
        assert preflight.status == GateStatus.PASS
        assert launch.status == GateStatus.SKIP
        assert launch.reason == "--skip-launch-guard"

    def test_preflight_fail_blocks_even_with_skip_launch_guard(self) -> None:
        """Preflight failure is always a blocker regardless of launch guard skip."""
        summary = _default_summary(skip_launch_guard=True, preflight_passed=False)
        assert summary.has_blockers
        preflight = next(g for g in summary.gates if g.name == "LIVE_PREFLIGHT")
        assert preflight.status == GateStatus.FAIL

    def test_both_gates_pass(self) -> None:
        summary = _default_summary(
            preflight_passed=True,
            launch_guard_status="verify_clean",
            launch_guard_reason="all symbols clean",
        )
        preflight = next(g for g in summary.gates if g.name == "LIVE_PREFLIGHT")
        launch = next(g for g in summary.gates if g.name == "LAUNCH_GUARD")
        assert preflight.status == GateStatus.PASS
        assert launch.status == GateStatus.PASS

    def test_launch_guard_fail_detected(self) -> None:
        summary = _default_summary(
            launch_guard_status="verify_dirty_no_cleanup",
            launch_guard_reason="orders=3",
        )
        launch = next(g for g in summary.gates if g.name == "LAUNCH_GUARD")
        assert launch.status == GateStatus.FAIL

    def test_launch_guard_skipped_for_non_futures(self) -> None:
        """Non-futures/non-mainnet returns 'skipped' status, mapped to SKIP."""
        summary = _default_summary(
            launch_guard_status="skipped",
            launch_guard_reason="not_live_mainnet_futures",
        )
        launch = next(g for g in summary.gates if g.name == "LAUNCH_GUARD")
        assert launch.status == GateStatus.SKIP


# ---------------------------------------------------------------------------
# T2: effective runtime mode summary is printed
# ---------------------------------------------------------------------------


class TestEffectiveRuntimeModeSummary:
    def test_effective_runtime_mode_summary_is_printed(self) -> None:
        summary = _default_summary(
            mode="live_trade",
            armed=True,
            mainnet=True,
            exchange_port="futures",
            grid_v2=True,
            grid_v2_symbol="BTCUSDT",
            rolling_grid=True,
            planner=True,
            account_sync=True,
        )
        output = summary.format_summary()
        assert "mode            = live_trade" in output
        assert "armed           = True" in output
        assert "mainnet         = True" in output
        assert "exchange_port   = futures" in output
        assert "grid_v2         = True" in output
        assert "grid_v2_symbol  = BTCUSDT" in output
        assert "rolling_grid    = True" in output
        assert "planner         = True" in output
        assert "account_sync    = True" in output

    def test_grid_v2_symbol_hidden_when_disabled(self) -> None:
        summary = _default_summary(grid_v2=False)
        output = summary.format_summary()
        assert "grid_v2_symbol" not in output


# ---------------------------------------------------------------------------
# T3: execution enabled without ACK warns or blocks
# ---------------------------------------------------------------------------


class TestExecutionWithoutAck:
    def test_armed_mainnet_noop_warns(self) -> None:
        """Armed + mainnet + noop port → warning (not block, but clearly flagged)."""
        summary = _default_summary(armed=True, mainnet=True, exchange_port="noop")
        codes = [w.code for w in summary.warnings]
        assert "ARMED_MAINNET_NOOP" in codes


# ---------------------------------------------------------------------------
# T4: rolling grid feature matrix reported
# ---------------------------------------------------------------------------


class TestRollingGridFeatureMatrix:
    def test_rolling_without_planner_warned(self) -> None:
        summary = _default_summary(rolling_grid=True, planner=False, account_sync=True)
        codes = [w.code for w in summary.warnings]
        assert "ROLLING_NO_PLANNER" in codes

    def test_rolling_without_sync_warned(self) -> None:
        summary = _default_summary(rolling_grid=True, planner=True, account_sync=False)
        codes = [w.code for w in summary.warnings]
        assert "ROLLING_NO_SYNC" in codes

    def test_rolling_with_all_companions_no_warning(self) -> None:
        summary = _default_summary(rolling_grid=True, planner=True, account_sync=True)
        rolling_codes = [w.code for w in summary.warnings if "ROLLING" in w.code]
        assert rolling_codes == []

    def test_all_flags_visible_in_summary(self) -> None:
        summary = _default_summary(
            rolling_grid=True,
            planner=True,
            account_sync=True,
        )
        output = summary.format_summary()
        assert "rolling_grid" in output
        assert "planner" in output
        assert "account_sync" in output


# ---------------------------------------------------------------------------
# T5: unsafe partial mode combination warns
# ---------------------------------------------------------------------------


class TestUnsafePartialModeCombination:
    def test_grid_v2_no_sync_futures_blocks(self) -> None:
        summary = _default_summary(
            grid_v2=True,
            grid_v2_symbol="BTCUSDT",
            account_sync=False,
            exchange_port="futures",
        )
        blockers = [w for w in summary.warnings if w.severity == "BLOCK"]
        assert any(w.code == "GRID_V2_NO_SYNC_FUTURES" for w in blockers)
        assert summary.has_blockers

    def test_grid_v2_no_symbol_blocks(self) -> None:
        summary = _default_summary(grid_v2=True, grid_v2_symbol="")
        blockers = [w for w in summary.warnings if w.severity == "BLOCK"]
        assert any(w.code == "GRID_V2_NO_SYMBOL" for w in blockers)

    def test_planner_without_sync_warned(self) -> None:
        summary = _default_summary(planner=True, account_sync=False)
        codes = [w.code for w in summary.warnings]
        assert "PLANNER_NO_SYNC" in codes

    def test_safe_config_no_warnings(self) -> None:
        """Fully consistent config produces no warnings."""
        summary = _default_summary()
        assert summary.warnings == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_startup_summary_deterministic(self) -> None:
        """Same inputs produce identical summary output."""
        kwargs = {
            "mode": "live_trade",
            "armed": True,
            "mainnet": True,
            "exchange_port": "futures",
            "ha_enabled": False,
            "grid_v2": True,
            "grid_v2_symbol": "BTCUSDT",
            "rolling_grid": True,
            "planner": True,
            "account_sync": True,
            "preflight_passed": True,
            "skip_launch_guard": False,
            "launch_guard_status": "clean",
            "launch_guard_reason": "all_clean",
        }
        s1 = build_startup_contract_summary(**kwargs)  # type: ignore[arg-type]
        s2 = build_startup_contract_summary(**kwargs)  # type: ignore[arg-type]
        assert s1.format_summary() == s2.format_summary()

    def test_detect_unsafe_combos_deterministic(self) -> None:
        flags = RuntimeModeFlags(
            rolling_grid=True,
            planner=False,
            account_sync=False,
        )
        w1 = detect_unsafe_combos(flags)
        w2 = detect_unsafe_combos(flags)
        assert [(w.code, w.message) for w in w1] == [(w.code, w.message) for w in w2]


# ---------------------------------------------------------------------------
# Format output structure
# ---------------------------------------------------------------------------


class TestFormatOutput:
    def test_summary_contains_all_sections(self) -> None:
        summary = _default_summary(rolling_grid=True, planner=False)
        output = summary.format_summary()
        assert "STARTUP CONTRACT SUMMARY" in output
        assert "EFFECTIVE RUNTIME MODE" in output
        assert "STARTUP GATES" in output
        assert "WARNINGS" in output
        assert "VERDICT" in output

    def test_no_warnings_section_when_clean(self) -> None:
        summary = _default_summary()
        output = summary.format_summary()
        assert "WARNINGS" not in output
        assert "VERDICT: OK" in output

    def test_blocked_verdict_on_gate_fail(self) -> None:
        summary = _default_summary(preflight_passed=False)
        output = summary.format_summary()
        assert "VERDICT: BLOCKED" in output

    def test_blocked_verdict_on_block_warning(self) -> None:
        summary = _default_summary(
            grid_v2=True,
            grid_v2_symbol="",
        )
        output = summary.format_summary()
        assert "VERDICT: BLOCKED" in output
