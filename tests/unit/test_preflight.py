"""Tests for Live Preflight Gate (PR-2 of production program).

Adversarial tests for pre-start environment checks.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from grinder.live.preflight import (
    CheckStatus,
    PreflightConfig,
    PreflightReport,
    check_account_sync_read,
    check_config_consistency,
    check_dns_resolution,
    check_exchange_time_sync,
    check_symbol_metadata,
    check_ws_bootstrap,
    run_preflight,
)


class TestDnsResolution:
    def test_pass_on_resolve(self) -> None:
        r = check_dns_resolution("fapi.binance.com", timeout=5.0)
        # May pass or fail depending on test env — just verify structure
        assert r.name == "dns_resolution"
        assert r.status in (CheckStatus.PASS, CheckStatus.FAIL)

    def test_fail_on_bad_host(self) -> None:
        r = check_dns_resolution("this.host.does.not.exist.invalid", timeout=2.0)
        assert r.status == CheckStatus.FAIL
        assert r.name == "dns_resolution"


class TestExchangeTimeSync:
    def test_pass_on_small_offset(self) -> None:
        with patch("grinder.live.preflight.urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.read.return_value = (
                b'{"serverTime": ' + str(int(time.time() * 1000)).encode() + b"}"
            )
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_url.return_value = mock_resp

            r = check_exchange_time_sync(PreflightConfig(clock_drift_fail_ms=2000))
            assert r.status in (CheckStatus.PASS, CheckStatus.WARN)

    def test_fail_on_large_offset(self) -> None:
        with patch("grinder.live.preflight.urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"serverTime": 1000}'  # way off
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_url.return_value = mock_resp

            r = check_exchange_time_sync(PreflightConfig(clock_drift_fail_ms=1500))
            assert r.status == CheckStatus.FAIL

    def test_fail_on_connection_error(self) -> None:
        with patch(
            "grinder.live.preflight.urllib.request.urlopen",
            side_effect=ConnectionError("no network"),
        ):
            r = check_exchange_time_sync(PreflightConfig())
            assert r.status == CheckStatus.FAIL


class TestAccountSyncRead:
    def test_pass_on_successful_sync(self) -> None:
        syncer = MagicMock()
        syncer.sync.return_value = MagicMock(error=None)
        r = check_account_sync_read(syncer)
        assert r.status == CheckStatus.PASS

    def test_fail_on_sync_error(self) -> None:
        syncer = MagicMock()
        syncer.sync.return_value = MagicMock(error="DNS failure")
        r = check_account_sync_read(syncer)
        assert r.status == CheckStatus.FAIL

    def test_fail_on_exception(self) -> None:
        syncer = MagicMock()
        syncer.sync.side_effect = RuntimeError("connection refused")
        r = check_account_sync_read(syncer)
        assert r.status == CheckStatus.FAIL

    def test_pass_when_syncer_none(self) -> None:
        r = check_account_sync_read(None)
        assert r.status == CheckStatus.PASS


class TestConfigConsistency:
    def test_pass_normal_config(self) -> None:
        r = check_config_consistency(
            armed=True,
            mainnet=True,
            mode_value="live_trade",
            env_acks={"ALLOW_MAINNET_TRADE": True, "GRINDER_REAL_PORT_ACK": True},
        )
        assert r.status == CheckStatus.PASS

    def test_fail_wrong_mode(self) -> None:
        r = check_config_consistency(
            armed=True,
            mainnet=True,
            mode_value="read_only",
        )
        assert r.status == CheckStatus.FAIL
        assert "mode=read_only" in r.detail

    def test_fail_missing_ack(self) -> None:
        r = check_config_consistency(
            armed=True,
            mainnet=True,
            mode_value="live_trade",
            env_acks={"ALLOW_MAINNET_TRADE": False, "GRINDER_REAL_PORT_ACK": True},
        )
        assert r.status == CheckStatus.FAIL
        assert "ALLOW_MAINNET_TRADE" in r.detail


class TestRunPreflight:
    def test_skip_for_non_armed(self) -> None:
        report = run_preflight(armed=False, mainnet=True, mode_value="live_trade")
        assert report.passed
        assert report.checks[0].name == "preflight_scope"

    def test_skip_for_non_mainnet(self) -> None:
        report = run_preflight(armed=True, mainnet=False, mode_value="live_trade")
        assert report.passed

    def test_report_passed_property(self) -> None:
        report = PreflightReport()
        report.checks.append(MagicMock(status=CheckStatus.PASS, name="a", detail=""))
        assert report.passed

    def test_report_failed_property(self) -> None:
        report = PreflightReport()
        report.checks.append(MagicMock(status=CheckStatus.FAIL, name="a", detail="bad"))
        assert not report.passed
        assert len(report.hard_failures) == 1

    def test_no_write_side_effects(self) -> None:
        """Preflight must be read-only: no PLACE/CANCEL dispatched."""
        port = MagicMock()
        syncer = MagicMock()
        syncer.sync.return_value = MagicMock(error=None)

        with (
            patch("grinder.live.preflight.check_dns_resolution") as mock_dns,
            patch("grinder.live.preflight.check_exchange_time_sync") as mock_time,
        ):
            mock_dns.return_value = MagicMock(
                name="dns_resolution", status=CheckStatus.PASS, detail=""
            )
            mock_time.return_value = MagicMock(
                name="exchange_time_sync", status=CheckStatus.PASS, detail=""
            )
            run_preflight(
                armed=True,
                mainnet=True,
                mode_value="live_trade",
                syncer=syncer,
                port=port,
                symbols=["BTCUSDT"],
                env_acks={"ALLOW_MAINNET_TRADE": True, "GRINDER_REAL_PORT_ACK": True},
            )

        # Port should NOT have place/cancel called
        port.place_order.assert_not_called()
        port.cancel_order.assert_not_called()


class TestSymbolMetadataReal:
    """Symbol metadata must validate real constraints."""

    def test_pass_when_symbol_in_constraints(self) -> None:

        port = MagicMock()
        port.get_symbol_constraints.return_value = {"BTCUSDT": {}, "ETHUSDT": {}}
        r = check_symbol_metadata(port, ["BTCUSDT"])
        assert r.status == CheckStatus.PASS

    def test_fail_when_symbol_missing(self) -> None:

        port = MagicMock()
        port.get_symbol_constraints.return_value = {"BTCUSDT": {}}
        r = check_symbol_metadata(port, ["PIPPINUSDT"])
        assert r.status == CheckStatus.FAIL
        assert "PIPPINUSDT" in r.detail

    def test_fail_when_metadata_unavailable(self) -> None:

        port = MagicMock(spec=[])  # no attributes at all
        r = check_symbol_metadata(port, ["BTCUSDT"])
        assert r.status == CheckStatus.FAIL
        assert "unavailable" in r.detail

    def test_fail_when_no_symbols(self) -> None:

        port = MagicMock()
        r = check_symbol_metadata(port, [])
        assert r.status == CheckStatus.FAIL


class TestWsBootstrap:
    """WS bootstrap check."""

    def test_pass_on_reachable(self) -> None:

        with patch("grinder.live.preflight.socket.create_connection") as mock_conn:
            mock_sock = MagicMock()
            mock_conn.return_value = mock_sock
            r = check_ws_bootstrap(timeout_s=2.0)
            assert r.status == CheckStatus.PASS
            mock_sock.close.assert_called_once()

    def test_fail_on_connection_error(self) -> None:

        with patch(
            "grinder.live.preflight.socket.create_connection",
            side_effect=ConnectionRefusedError("refused"),
        ):
            r = check_ws_bootstrap(timeout_s=2.0)
            assert r.status == CheckStatus.FAIL

    def test_fail_on_timeout(self) -> None:

        with patch(
            "grinder.live.preflight.socket.create_connection",
            side_effect=TimeoutError("timeout"),
        ):
            r = check_ws_bootstrap(timeout_s=1.0)
            assert r.status == CheckStatus.FAIL


class TestRealSyncerWired:
    """Account sync check with real syncer behavior."""

    def test_failing_syncer_blocks(self) -> None:
        """Real syncer returning error → FAIL."""
        syncer = MagicMock()
        syncer.sync.return_value = MagicMock(
            error="ConnectorTransientError: Temporary failure in name resolution"
        )
        r = check_account_sync_read(syncer)
        assert r.status == CheckStatus.FAIL
        assert "name resolution" in r.detail

    def test_syncer_exception_blocks(self) -> None:
        """Syncer raising exception → FAIL."""
        syncer = MagicMock()
        syncer.sync.side_effect = ConnectionError("connection refused")
        r = check_account_sync_read(syncer)
        assert r.status == CheckStatus.FAIL
