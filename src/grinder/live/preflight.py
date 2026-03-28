"""Live Preflight Gate: pre-start checks before armed trading loop.

Evaluates environment viability. Fails closed on hard blockers.
No trading writes — read-only probes only.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

BINANCE_FUTURES_HOST = "fapi.binance.com"
BINANCE_FUTURES_TIME_URL = "https://fapi.binance.com/fapi/v1/time"


class CheckStatus(Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class PreflightCheck:
    """Result of a single preflight check."""

    name: str
    status: CheckStatus
    detail: str = ""


@dataclass
class PreflightConfig:
    """Thresholds for preflight checks."""

    clock_drift_warn_ms: int = 500
    clock_drift_fail_ms: int = 1500
    dns_timeout_s: float = 5.0
    sync_timeout_s: float = 10.0


@dataclass
class PreflightReport:
    """Aggregated preflight result."""

    checks: list[PreflightCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.status != CheckStatus.FAIL for c in self.checks)

    @property
    def hard_failures(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == CheckStatus.FAIL]

    def print_report(self) -> None:
        overall = "PASS" if self.passed else "FAIL"
        print(f"LIVE_PREFLIGHT status={overall}")
        for c in self.checks:
            print(f"  CHECK {c.name} status={c.status.value} {c.detail}")
        if not self.passed:
            for f in self.hard_failures:
                print(f"  BLOCKER: {f.name} — {f.detail}")


def check_dns_resolution(host: str = BINANCE_FUTURES_HOST, timeout: float = 5.0) -> PreflightCheck:
    """Verify DNS resolves for exchange host."""
    try:
        socket.setdefaulttimeout(timeout)
        addr = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        if addr:
            return PreflightCheck(
                name="dns_resolution",
                status=CheckStatus.PASS,
                detail=f"host={host} resolved={addr[0][4][0]}",
            )
        return PreflightCheck(
            name="dns_resolution",
            status=CheckStatus.FAIL,
            detail=f"host={host} no_addresses",
        )
    except Exception as e:
        return PreflightCheck(
            name="dns_resolution",
            status=CheckStatus.FAIL,
            detail=f"host={host} error={e!s}",
        )


def check_exchange_time_sync(
    config: PreflightConfig,
) -> PreflightCheck:
    """Fetch exchange time and compute clock offset."""
    try:
        req = urllib.request.Request(BINANCE_FUTURES_TIME_URL)
        with urllib.request.urlopen(req, timeout=config.dns_timeout_s) as resp:
            server_ts = json.loads(resp.read())["serverTime"]
        local_ts = int(time.time() * 1000)
        offset_ms = local_ts - server_ts

        if abs(offset_ms) > config.clock_drift_fail_ms:
            return PreflightCheck(
                name="exchange_time_sync",
                status=CheckStatus.FAIL,
                detail=f"offset_ms={offset_ms} threshold_ms={config.clock_drift_fail_ms}",
            )
        if abs(offset_ms) > config.clock_drift_warn_ms:
            return PreflightCheck(
                name="exchange_time_sync",
                status=CheckStatus.WARN,
                detail=f"offset_ms={offset_ms} warn_ms={config.clock_drift_warn_ms}",
            )
        return PreflightCheck(
            name="exchange_time_sync",
            status=CheckStatus.PASS,
            detail=f"offset_ms={offset_ms}",
        )
    except Exception as e:
        return PreflightCheck(
            name="exchange_time_sync",
            status=CheckStatus.FAIL,
            detail=f"error={e!s}",
        )


def check_account_sync_read(
    syncer: object | None,
    _timeout_s: float = 10.0,
) -> PreflightCheck:
    """Perform read-only account sync to verify connectivity."""
    if syncer is None:
        return PreflightCheck(
            name="account_sync_read",
            status=CheckStatus.PASS,
            detail="syncer=None (sync disabled)",
        )
    try:
        result = syncer.sync()  # type: ignore[attr-defined]
        if result.error is not None:
            return PreflightCheck(
                name="account_sync_read",
                status=CheckStatus.FAIL,
                detail=f"error={result.error}",
            )
        return PreflightCheck(
            name="account_sync_read",
            status=CheckStatus.PASS,
            detail="sync_ok",
        )
    except Exception as e:
        return PreflightCheck(
            name="account_sync_read",
            status=CheckStatus.FAIL,
            detail=f"error={e!s}",
        )


def check_symbol_metadata(
    port: Any,
    symbols: list[str],
) -> PreflightCheck:
    """Verify symbol constraints/metadata loadable for requested symbols.

    Fail-closed: if metadata unavailable or symbol missing, FAIL.
    """
    if not symbols:
        return PreflightCheck(
            name="symbol_metadata_read",
            status=CheckStatus.FAIL,
            detail="no_symbols_specified",
        )
    try:
        # Try to get constraints from port
        constraints = None
        if hasattr(port, "get_symbol_constraints"):
            constraints = port.get_symbol_constraints()
        elif hasattr(port, "_constraint_provider"):
            cp = getattr(port, "_constraint_provider", None)
            if cp is not None and hasattr(cp, "_constraints"):
                constraints = getattr(cp, "_constraints", None)

        if constraints is None:
            return PreflightCheck(
                name="symbol_metadata_read",
                status=CheckStatus.FAIL,
                detail=f"metadata_unavailable symbols={symbols}",
            )

        # Verify each requested symbol exists in constraints
        missing = [s for s in symbols if s not in constraints]
        if missing:
            return PreflightCheck(
                name="symbol_metadata_read",
                status=CheckStatus.FAIL,
                detail=f"missing_symbols={missing} available={len(constraints)}",
            )
        return PreflightCheck(
            name="symbol_metadata_read",
            status=CheckStatus.PASS,
            detail=f"symbols={symbols} constraints={len(constraints)}",
        )
    except Exception as e:
        return PreflightCheck(
            name="symbol_metadata_read",
            status=CheckStatus.FAIL,
            detail=f"error={e!s}",
        )


def check_ws_bootstrap(
    timeout_s: float = 5.0,
) -> PreflightCheck:
    """Verify WS endpoint is reachable with bounded connection probe.

    Read-only: attempts TCP connect to Binance WS endpoint, no subscription.
    """
    ws_host = "fstream.binance.com"
    ws_port = 443
    try:
        sock = socket.create_connection((ws_host, ws_port), timeout=timeout_s)
        sock.close()
        return PreflightCheck(
            name="ws_bootstrap",
            status=CheckStatus.PASS,
            detail=f"host={ws_host} port={ws_port}",
        )
    except Exception as e:
        return PreflightCheck(
            name="ws_bootstrap",
            status=CheckStatus.FAIL,
            detail=f"host={ws_host} error={e!s}",
        )


def check_config_consistency(
    armed: bool,
    mainnet: bool,
    mode_value: str,
    env_acks: dict[str, bool] | None = None,
) -> PreflightCheck:
    """Check for contradictory or unsafe config combinations."""
    issues: list[str] = []

    if armed and mainnet and mode_value != "live_trade":
        issues.append(f"armed+mainnet but mode={mode_value} (expected live_trade)")

    if env_acks:
        if armed and mainnet and not env_acks.get("ALLOW_MAINNET_TRADE", False):
            issues.append("armed+mainnet but ALLOW_MAINNET_TRADE not set")
        if armed and mainnet and not env_acks.get("GRINDER_REAL_PORT_ACK", False):
            issues.append("armed+mainnet but GRINDER_REAL_PORT_ACK not set")

    if issues:
        return PreflightCheck(
            name="config_consistency",
            status=CheckStatus.FAIL,
            detail="; ".join(issues),
        )
    return PreflightCheck(
        name="config_consistency",
        status=CheckStatus.PASS,
        detail="ok",
    )


def run_preflight(
    *,
    armed: bool,
    mainnet: bool,
    mode_value: str,
    syncer: object | None = None,
    port: object | None = None,
    symbols: list[str] | None = None,
    config: PreflightConfig | None = None,
    env_acks: dict[str, bool] | None = None,
) -> PreflightReport:
    """Run all preflight checks and return aggregated report.

    Read-only. No trading writes.
    """
    cfg = config or PreflightConfig()
    report = PreflightReport()

    # Only run full preflight for armed mainnet
    if not (armed and mainnet):
        report.checks.append(
            PreflightCheck(
                name="preflight_scope",
                status=CheckStatus.PASS,
                detail="skipped (not armed+mainnet)",
            )
        )
        return report

    report.checks.append(check_dns_resolution(timeout=cfg.dns_timeout_s))
    report.checks.append(check_exchange_time_sync(cfg))
    report.checks.append(check_ws_bootstrap(timeout_s=cfg.dns_timeout_s))
    report.checks.append(check_account_sync_read(syncer, cfg.sync_timeout_s))
    report.checks.append(check_symbol_metadata(port, symbols or []))
    report.checks.append(check_config_consistency(armed, mainnet, mode_value, env_acks))

    return report
