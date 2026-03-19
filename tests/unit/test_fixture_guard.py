"""Tests for fixture network airgap guard (PR-NETLOCK-1)."""

from __future__ import annotations

import logging
import socket
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from grinder.net.fixture_guard import (
    _LOG_LIMIT,
    FixtureNetworkBlockedError,
    install_fixture_network_guard,
    uninstall_fixture_network_guard,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


def _new_tcp_socket_or_skip() -> socket.socket:
    """Create AF_INET socket or skip when sandbox blocks socket syscalls."""
    try:
        return socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError as exc:
        pytest.skip(f"AF_INET sockets are blocked in this environment: {exc}")


@pytest.fixture(autouse=True)
def _guard_lifecycle() -> Iterator[None]:
    """Install guard before each test, uninstall after — even on crash."""
    install_fixture_network_guard()
    try:
        yield
    finally:
        uninstall_fixture_network_guard()


class TestFixtureGuardBlocking:
    """Tests that external connections are blocked."""

    def test_blocks_external_connect(self) -> None:
        """socket.socket.connect to external host raises FixtureNetworkBlockedError."""
        sock = _new_tcp_socket_or_skip()
        try:
            with pytest.raises(FixtureNetworkBlockedError, match=r"api\.binance\.com"):
                sock.connect(("api.binance.com", 443))
        finally:
            sock.close()

    def test_blocks_external_connect_ex(self) -> None:
        """socket.socket.connect_ex to external host raises FixtureNetworkBlockedError."""
        sock = _new_tcp_socket_or_skip()
        try:
            with pytest.raises(FixtureNetworkBlockedError, match=r"8\.8\.8\.8"):
                sock.connect_ex(("8.8.8.8", 53))
        finally:
            sock.close()

    def test_blocks_create_connection(self) -> None:
        """socket.create_connection to external host raises FixtureNetworkBlockedError."""
        with pytest.raises(FixtureNetworkBlockedError, match=r"example\.com"):
            socket.create_connection(("example.com", 80))


class TestFixtureGuardAllowlist:
    """Tests that loopback connections are allowed."""

    def test_allows_localhost_ipv4(self) -> None:
        """127.0.0.1 passes through to original connect (may fail with ConnectionRefused)."""
        sock = _new_tcp_socket_or_skip()
        try:
            # Should NOT raise FixtureNetworkBlockedError.
            # Will raise ConnectionRefusedError (nothing listening) — that's fine.
            with pytest.raises(ConnectionRefusedError):
                sock.connect(("127.0.0.1", 1))
        finally:
            sock.close()

    def test_allows_localhost_name(self) -> None:
        """'localhost' passes through to original connect."""
        sock = _new_tcp_socket_or_skip()
        try:
            with pytest.raises((ConnectionRefusedError, OSError)):
                sock.connect(("localhost", 1))
        finally:
            sock.close()


class TestFixtureGuardLifecycle:
    """Tests for install/uninstall lifecycle."""

    def test_uninstall_restores_original(self) -> None:
        """After uninstall, external connect is no longer blocked by guard."""
        uninstall_fixture_network_guard()
        sock = _new_tcp_socket_or_skip()
        sock.setblocking(False)
        try:
            # Without guard, connect should NOT raise FixtureNetworkBlockedError.
            # It will raise BlockingIOError (non-blocking) or OSError — not our error.
            with pytest.raises((BlockingIOError, OSError)):
                sock.connect(("8.8.8.8", 53))
            # Verify it was NOT our custom error
            try:
                sock2 = _new_tcp_socket_or_skip()
                sock2.setblocking(False)
                sock2.connect(("8.8.8.8", 53))
            except FixtureNetworkBlockedError:
                pytest.fail("Guard still active after uninstall")
            except (BlockingIOError, OSError):
                pass  # Expected — real socket behavior
            finally:
                sock2.close()
        finally:
            sock.close()
            # Re-install for autouse fixture cleanup
            install_fixture_network_guard()

    def test_idempotent_install_uninstall(self) -> None:
        """Double install + double uninstall = no crash."""
        # Guard already installed by autouse fixture
        install_fixture_network_guard()  # Second install — should be no-op
        install_fixture_network_guard()  # Third install — still no-op

        # Verify still works
        with pytest.raises(FixtureNetworkBlockedError):
            socket.create_connection(("example.com", 80))

        uninstall_fixture_network_guard()
        uninstall_fixture_network_guard()  # Double uninstall — should be no-op

        # Re-install for autouse fixture cleanup
        install_fixture_network_guard()


class TestFixtureGuardErrorMessage:
    """Tests for error message quality."""

    def test_error_message_actionable(self) -> None:
        """Error message contains the blocked host and guidance to remove --fixture."""
        sock = _new_tcp_socket_or_skip()
        try:
            with pytest.raises(FixtureNetworkBlockedError) as exc_info:
                sock.connect(("stream.binance.com", 9443))
            msg = str(exc_info.value)
            assert "stream.binance.com" in msg
            assert "without --fixture" in msg
        finally:
            sock.close()


class TestIsLocalhostEdgeCases:
    """Tests for _is_localhost edge cases via integration."""

    @patch("grinder.net.fixture_guard._original_connect", side_effect=ConnectionRefusedError)
    def test_allows_loopback_127_x(self, _mock: object) -> None:
        """127.0.0.2 (full 127.0.0.0/8 range) is allowed."""
        sock = _new_tcp_socket_or_skip()
        try:
            with pytest.raises(ConnectionRefusedError):
                sock.connect(("127.0.0.2", 1))
        finally:
            sock.close()


class TestFixtureGuardLogRateLimit:
    """Tests that blocked-connection logs are rate-limited."""

    def test_log_suppressed_after_limit(self, caplog: pytest.LogCaptureFixture) -> None:
        """After _LOG_LIMIT blocks, further warnings are suppressed."""
        with caplog.at_level(logging.WARNING, logger="grinder.net.fixture_guard"):
            for _ in range(_LOG_LIMIT + 3):
                with pytest.raises(FixtureNetworkBlockedError):
                    socket.create_connection(("example.com", 80))

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        # First _LOG_LIMIT are individual block messages, then 1 suppression message
        assert len(warning_records) == _LOG_LIMIT + 1
        assert "suppressed" in warning_records[-1].message
