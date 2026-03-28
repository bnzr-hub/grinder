"""Grid V2 Launch Readiness Check (ADR-107).

Reproducible pre-launch readiness report for grid_v2 live verification runs.
Calls the real run_preflight() gate + validates config, prints go/no-go.

Usage:
    PYTHONPATH=src python -m scripts.launch_readiness [--symbol BTCUSDT]

For armed mainnet readiness, set:
    GRINDER_ARMED=1 GRINDER_MAINNET=1 ALLOW_MAINNET_TRADE=1 GRINDER_REAL_PORT_ACK=1
"""

from __future__ import annotations

import argparse
import os
import sys

from grinder.live.preflight import CheckStatus, run_preflight


def _check(name: str, passed: bool, detail: str = "") -> bool:
    status = "PASS" if passed else "FAIL"
    suffix = f" ({detail})" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    return passed


def _run_config_checks() -> bool:
    """Check required env vars."""
    print("1. CONFIG CHECKS")
    ok = True
    required = [
        ("GRINDER_GRID_V2_ENABLED", "1"),
        ("GRINDER_GRID_V2_SYMBOL", None),
        ("GRINDER_GRID_V2_TICK_SIZE", None),
        ("GRINDER_GRID_V2_SYNC_RECONCILER_ENABLED", "1"),
        ("GRINDER_GRID_V2_SYNC_RECONCILER_PRIMARY", "1"),
        ("GRINDER_MAX_POSITION_USD", None),
        ("GRINDER_SYMBOL_RISK_MAX_NOTIONAL_PCT", None),
    ]
    for var, expected in required:
        val = os.environ.get(var, "")
        passed = val == expected if expected is not None else bool(val)
        ok &= _check(var, passed, val or "<not set>")

    ok &= _check(
        "GRINDER_GRID_V2_NETOFF_ENABLED disabled",
        os.environ.get("GRINDER_GRID_V2_NETOFF_ENABLED", "0") == "0",
        os.environ.get("GRINDER_GRID_V2_NETOFF_ENABLED", "<not set>"),
    )
    ok &= _check(
        "GRINDER_GRID_V2_RESEED_ON_FLAT",
        os.environ.get("GRINDER_GRID_V2_RESEED_ON_FLAT", "0") == "1",
        os.environ.get("GRINDER_GRID_V2_RESEED_ON_FLAT", "<not set>"),
    )
    print()
    return ok


def _run_preflight_gate(
    symbol: str,
    *,
    syncer: object | None = None,
    port: object | None = None,
) -> bool:
    """Run the real preflight gate and surface its report.

    Uses the same run_preflight() as the real startup ceremony.
    Accepts syncer/port for real startup wiring.
    """
    print("2. PREFLIGHT GATE (run_preflight)")
    armed = os.environ.get("GRINDER_ARMED", "0") == "1"
    mainnet = os.environ.get("GRINDER_MAINNET", "0") == "1"
    mode = os.environ.get("GRINDER_MODE", "live_trade")
    env_acks = {
        "ALLOW_MAINNET_TRADE": os.environ.get("ALLOW_MAINNET_TRADE", "0") == "1",
        "GRINDER_REAL_PORT_ACK": os.environ.get("GRINDER_REAL_PORT_ACK", "0") == "1",
    }

    report = run_preflight(
        armed=armed,
        mainnet=mainnet,
        mode_value=mode,
        syncer=syncer,
        port=port,
        symbols=[symbol],
        env_acks=env_acks,
    )

    for c in report.checks:
        _check(c.name, c.status != CheckStatus.FAIL, c.detail)

    if not report.passed:
        for f in report.hard_failures:
            print(f"  BLOCKER: {f.name} — {f.detail}")

    print()
    return report.passed


def _print_watchpoints() -> None:
    print("3. KEY WATCHPOINTS (monitor during run)")
    print("  Blockers:")
    print("    - GRID_V2_HEALTH_BLOCK reason=PAUSED_UNSAFE")
    print("    - Repeated GRID_V2_EXIT_TOPOLOGY_REPAIR_INCOMPLETE")
    print("    - Repeated GRID_V2_REDUCE_ONLY_REPAIR_DEFERRED")
    print("  Warnings (watch, don't stop):")
    print("    - GRID_V2_NO_ACTION reason=RISK_SATURATED_TARGET_ZERO")
    print("    - Single GRID_V2_REDUCE_ONLY_REPAIR_START → CONVERGED")
    print()


def run_readiness_check(
    symbol: str = "BTCUSDT",
    *,
    syncer: object | None = None,
    port: object | None = None,
) -> bool:
    """Run all readiness checks and print report. Returns True if GO."""
    print("=" * 60)
    print(f"GRID V2 LAUNCH READINESS REPORT (symbol={symbol})")
    print("=" * 60)
    print()

    all_pass = True
    all_pass &= _run_config_checks()
    all_pass &= _run_preflight_gate(symbol, syncer=syncer, port=port)
    _print_watchpoints()

    print("=" * 60)
    verdict = "GO" if all_pass else "NO-GO"
    print(f"VERDICT: {verdict}")
    if not all_pass:
        print("Fix failing checks before proceeding.")
    print("=" * 60)

    return all_pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Grid V2 Launch Readiness Check")
    parser.add_argument("--symbol", default="BTCUSDT", help="Target symbol")
    args = parser.parse_args()

    ok = run_readiness_check(args.symbol)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
