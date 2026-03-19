"""Live HTTP integration tests for /healthz and /metrics endpoints.

Tests that the live server (scripts/run_live.py) correctly serves:
- GET /healthz -> JSON with required keys, status="ok"
- GET /metrics -> Prometheus text format with required patterns

Uses REQUIRED_HEALTHZ_KEYS and REQUIRED_METRICS_PATTERNS from live_contract.py
as the single source of truth.

Run with: PYTHONPATH=src pytest tests/integration -v
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

# Skip entire module if redis not installed (collection won't fail)
pytest.importorskip("redis", reason="redis not installed")

from grinder.observability import REQUIRED_HEALTHZ_KEYS, REQUIRED_METRICS_PATTERNS

if TYPE_CHECKING:
    from collections.abc import Generator

# Type alias for subprocess with bytes streams
PopenBytes = subprocess.Popen[bytes]


def _can_open_local_inet_socket() -> bool:
    """Return False when environment forbids AF_INET socket creation/bind."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
    except PermissionError:
        return False
    return True


_SOCKETS_AVAILABLE = _can_open_local_inet_socket()

# Mark all tests in this module as integration tests.
# Skip via marker (instead of module-level pytest.skip) so file-level runs exit 0.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _SOCKETS_AVAILABLE,
        reason="AF_INET sockets are blocked in this environment; skipping live HTTP integration tests",
    ),
]


def find_free_port() -> int:
    """Find a free port for the test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port: int = s.getsockname()[1]
        return port


def wait_for_server(base_url: str, timeout: float = 10.0, interval: float = 0.2) -> bool:
    """Poll server until it responds or timeout.

    Args:
        base_url: Base URL of the server (e.g., "http://127.0.0.1:9090")
        timeout: Maximum time to wait in seconds
        interval: Time between poll attempts in seconds

    Returns:
        True if server is ready, False if timeout reached
    """
    healthz_url = f"{base_url}/healthz"
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        try:
            req = Request(healthz_url, method="GET")
            with urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (URLError, OSError, TimeoutError):
            pass
        time.sleep(interval)

    return False


def terminate_process(proc: PopenBytes, timeout: float = 3.0) -> None:
    """Terminate process gracefully, then force kill if needed.

    Args:
        proc: The subprocess to terminate
        timeout: Time to wait for graceful shutdown before SIGKILL
    """
    if proc.poll() is not None:
        return  # Already dead

    # Try graceful termination first
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Force kill
        proc.kill()
        # Wait with suppressed timeout - process may be stuck in uninterruptible state
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)


class TestLiveHTTPContracts:
    """Integration tests for live server HTTP endpoints."""

    @pytest.fixture
    def live_server(self) -> Generator[tuple[PopenBytes, str], None, None]:
        """Start live server and yield (process, base_url).

        Automatically cleans up the server after the test.
        Uses DEVNULL for stdout and temp file for stderr to avoid pipe deadlock.
        """
        port = find_free_port()
        base_url = f"http://127.0.0.1:{port}"

        # Set up environment - prepend src to PYTHONPATH (don't overwrite)
        existing_pythonpath = os.environ.get("PYTHONPATH", "")
        env = os.environ.copy()
        env["PYTHONPATH"] = f"src{os.pathsep}{existing_pythonpath}".rstrip(os.pathsep)

        # Use temp file for stderr to avoid pipe deadlock while capturing on failure
        # We intentionally don't use context manager because file must stay open for subprocess
        stderr_file = tempfile.NamedTemporaryFile(mode="w+b", delete=False)  # noqa: SIM115
        stderr_path = Path(stderr_file.name)

        try:
            # Start server with short duration (will be terminated anyway)
            # Use DEVNULL for stdout to avoid pipe buffer deadlock
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "scripts.run_live",
                    "--metrics-port",
                    str(port),
                    "--duration-s",
                    "60",  # Will be terminated before this
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                cwd=Path.cwd(),
            )

            try:
                # Wait for server to be ready
                if not wait_for_server(base_url, timeout=10.0):
                    # Server didn't start - get stderr for debugging
                    terminate_process(proc)
                    stderr_file.flush()
                    stderr_content = stderr_path.read_text(errors="replace")
                    # Tail last 200 lines
                    stderr_lines = stderr_content.splitlines()[-200:]
                    pytest.fail(
                        f"Server failed to start within timeout.\n"
                        f"stderr (last 200 lines):\n{chr(10).join(stderr_lines)}"
                    )

                yield proc, base_url
            finally:
                # Clean up server
                terminate_process(proc)
        finally:
            # Clean up temp file
            stderr_file.close()
            with contextlib.suppress(OSError):
                stderr_path.unlink()

    def test_healthz_returns_valid_json(self, live_server: tuple[PopenBytes, str]) -> None:
        """Test that /healthz returns valid JSON with correct content-type."""
        _, base_url = live_server
        url = f"{base_url}/healthz"

        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            # Check status code
            assert resp.status == 200

            # Check content-type header
            content_type = resp.headers.get("Content-Type", "")
            assert "application/json" in content_type, (
                f"Expected application/json, got {content_type}"
            )

            # Check body is valid JSON
            body = resp.read().decode()
            data = json.loads(body)
            assert isinstance(data, dict)

    def test_healthz_has_required_keys(self, live_server: tuple[PopenBytes, str]) -> None:
        """Test that /healthz contains all required keys from contract."""
        _, base_url = live_server
        url = f"{base_url}/healthz"

        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            data = json.loads(body)

            for key in REQUIRED_HEALTHZ_KEYS:
                assert key in data, f"Missing required key: {key}"

    def test_healthz_status_is_ok(self, live_server: tuple[PopenBytes, str]) -> None:
        """Test that /healthz status value is 'ok'."""
        _, base_url = live_server
        url = f"{base_url}/healthz"

        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            data = json.loads(body)

            assert data.get("status") == "ok", f"Expected status='ok', got {data.get('status')}"

    def test_healthz_uptime_is_valid(self, live_server: tuple[PopenBytes, str]) -> None:
        """Test that /healthz uptime_s is a non-negative number."""
        _, base_url = live_server
        url = f"{base_url}/healthz"

        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            data = json.loads(body)

            uptime = data.get("uptime_s")
            assert isinstance(uptime, (int, float)), (
                f"uptime_s should be numeric, got {type(uptime)}"
            )
            assert uptime >= 0, f"uptime_s should be non-negative, got {uptime}"

    def test_metrics_returns_text_format(self, live_server: tuple[PopenBytes, str]) -> None:
        """Test that /metrics returns text/plain with Prometheus format."""
        _, base_url = live_server
        url = f"{base_url}/metrics"

        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            # Check status code
            assert resp.status == 200

            # Check content-type header
            content_type = resp.headers.get("Content-Type", "")
            assert "text/plain" in content_type, f"Expected text/plain, got {content_type}"

            # Check body has newlines (multi-line format)
            body = resp.read().decode()
            lines = body.split("\n")
            assert len(lines) > 1, "Metrics should be multi-line"

    def test_metrics_has_required_patterns(self, live_server: tuple[PopenBytes, str]) -> None:
        """Test that /metrics contains all required patterns from contract."""
        _, base_url = live_server
        url = f"{base_url}/metrics"

        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            body = resp.read().decode()

            for pattern in REQUIRED_METRICS_PATTERNS:
                assert pattern in body, f"Missing required pattern: {pattern}"

    def test_metrics_grinder_up_is_one(self, live_server: tuple[PopenBytes, str]) -> None:
        """Test that grinder_up gauge is 1 (running)."""
        _, base_url = live_server
        url = f"{base_url}/metrics"

        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            assert "grinder_up 1" in body, "grinder_up should be 1"

    def test_metrics_has_help_lines(self, live_server: tuple[PopenBytes, str]) -> None:
        """Test that /metrics contains at least one HELP line."""
        _, base_url = live_server
        url = f"{base_url}/metrics"

        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            lines = body.split("\n")

            help_lines = [line for line in lines if line.startswith("# HELP")]
            assert len(help_lines) > 0, "Metrics should contain at least one # HELP line"

    def test_server_cleanup(self, live_server: tuple[PopenBytes, str]) -> None:
        """Test that server process is properly terminated after test."""
        proc, _ = live_server

        # Server should still be running at this point
        assert proc.poll() is None, "Server should be running during test"

        # After this test, the fixture cleanup will terminate the server
        # The fixture's finally block handles this
