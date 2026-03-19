"""Integration test: fixture network guard activates in run_trading.py subprocess."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "trading_loop" / "bookticker_5tick.jsonl"
RUN_TRADING = REPO_ROOT / "scripts" / "run_trading.py"


def _can_open_local_inet_socket() -> bool:
    """Return False when environment forbids AF_INET socket creation/bind."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
    except PermissionError:
        return False
    return True


_SOCKETS_AVAILABLE = _can_open_local_inet_socket()

pytestmark = pytest.mark.skipif(
    not _SOCKETS_AVAILABLE,
    reason="AF_INET sockets are blocked in this environment; skipping fixture subprocess integration test",
)


def test_fixture_mode_activates_network_guard() -> None:
    """run_trading.py --fixture prints 'Network guard: ACTIVE' and exits cleanly."""
    assert FIXTURE_PATH.exists(), f"Fixture not found: {FIXTURE_PATH}"
    assert RUN_TRADING.exists(), f"Script not found: {RUN_TRADING}"

    result = subprocess.run(
        [
            sys.executable,
            str(RUN_TRADING),
            "--symbols",
            "BTCUSDT",
            "--metrics-port",
            "0",
            "--fixture",
            str(FIXTURE_PATH),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(Path.home()),
            # src/ for grinder.*; repo root for scripts.* (run_trading imports scripts.http_measured_client)
            "PYTHONPATH": f"{REPO_ROOT / 'src'}{':' + str(REPO_ROOT)}",
            "GRINDER_TRADING_MODE": "read_only",
        },
    )

    combined = result.stdout + result.stderr
    assert "Network guard: ACTIVE" in combined, (
        f"Expected 'Network guard: ACTIVE' in output.\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}\n"
        f"returncode={result.returncode}"
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}.\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
