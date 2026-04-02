"""Stable per-run identity for operator-facing logs and triage.

Every process start generates one RunIdentity that remains stable
for the full process lifetime. Printed in startup/shutdown banners
so operators can unambiguously correlate log output to a single run.

SSOT: this module.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class RunIdentity:
    """Immutable per-run identity."""

    run_id: str
    pid: int
    started_at_utc: str  # ISO 8601, always UTC

    def format_startup_banner(
        self,
        *,
        mode: str,
        armed: bool,
        mainnet: bool,
        exchange_port: str,
        symbols: list[str],
        metrics_port: int,
    ) -> str:
        """Format the startup banner as a single copyable block."""
        lines = [
            "=" * 72,
            "GRINDER RUN IDENTITY",
            "=" * 72,
            f"  run_id        = {self.run_id}",
            f"  pid           = {self.pid}",
            f"  started_at    = {self.started_at_utc}",
            f"  mode          = {mode}",
            f"  armed         = {armed}",
            f"  mainnet       = {mainnet}",
            f"  exchange_port = {exchange_port}",
            f"  symbols       = {','.join(symbols)}",
            f"  metrics_port  = {metrics_port}",
            "=" * 72,
        ]
        return "\n".join(lines)

    def format_shutdown_banner(
        self,
        *,
        exit_code: int,
        stop_reason: str,
    ) -> str:
        """Format the shutdown banner."""
        lines = [
            "=" * 72,
            "GRINDER RUN SHUTDOWN",
            "=" * 72,
            f"  run_id        = {self.run_id}",
            f"  pid           = {self.pid}",
            f"  exit_code     = {exit_code}",
            f"  stop_reason   = {stop_reason}",
            "=" * 72,
        ]
        return "\n".join(lines)


def build_run_identity(
    *,
    clock: datetime | None = None,
    pid: int | None = None,
    entropy: str | None = None,
) -> RunIdentity:
    """Create a new RunIdentity.

    Args:
        clock: Override UTC timestamp (for testing determinism).
        pid: Override PID (for testing).
        entropy: Override random suffix (for testing determinism).

    Run ID format: ``YYYYMMDD-HHMMSS-<4hex>``
    Example: ``20260402-143052-a7f3``
    """
    now = clock or datetime.now(UTC)
    _pid = pid if pid is not None else os.getpid()
    suffix = entropy or secrets.token_hex(2)
    run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{suffix}"
    started_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return RunIdentity(run_id=run_id, pid=_pid, started_at_utc=started_at)
