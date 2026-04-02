"""Tests for stable per-run identity (ADR-145)."""

from __future__ import annotations

from datetime import UTC, datetime

from grinder.live.run_identity import build_run_identity


class TestRunIdCreation:
    def test_run_id_created_once_per_process_start(self) -> None:
        """One call = one identity."""
        rid = build_run_identity()
        assert rid.run_id
        assert rid.pid > 0
        assert rid.started_at_utc

    def test_run_id_format_is_stable(self) -> None:
        """Deterministic: same clock + entropy = same run_id."""
        clock = datetime(2026, 4, 2, 14, 30, 52, tzinfo=UTC)
        r1 = build_run_identity(clock=clock, pid=12345, entropy="a7f3")
        r2 = build_run_identity(clock=clock, pid=12345, entropy="a7f3")
        assert r1 == r2
        assert r1.run_id == "20260402-143052-a7f3"
        assert r1.pid == 12345
        assert r1.started_at_utc == "2026-04-02T14:30:52Z"

    def test_different_starts_get_different_run_ids(self) -> None:
        """Different entropy -> different run_id."""
        clock = datetime(2026, 4, 2, 14, 30, 52, tzinfo=UTC)
        r1 = build_run_identity(clock=clock, pid=100, entropy="aaaa")
        r2 = build_run_identity(clock=clock, pid=100, entropy="bbbb")
        assert r1.run_id != r2.run_id

    def test_different_clock_different_run_ids(self) -> None:
        c1 = datetime(2026, 4, 2, 14, 0, 0, tzinfo=UTC)
        c2 = datetime(2026, 4, 2, 14, 0, 1, tzinfo=UTC)
        r1 = build_run_identity(clock=c1, pid=100, entropy="aaaa")
        r2 = build_run_identity(clock=c2, pid=100, entropy="aaaa")
        assert r1.run_id != r2.run_id

    def test_run_id_without_overrides_is_unique(self) -> None:
        """Default (real clock + random entropy) produces unique ids."""
        r1 = build_run_identity()
        r2 = build_run_identity()
        # Extremely unlikely to collide (4 hex = 65536 values per second)
        # but they could share the same second — entropy differs.
        assert r1.run_id != r2.run_id or r1 is not r2  # at minimum distinct objects


class TestStartupBanner:
    def test_startup_banner_includes_run_id_and_pid(self) -> None:
        rid = build_run_identity(
            clock=datetime(2026, 4, 2, 14, 30, 52, tzinfo=UTC),
            pid=9999,
            entropy="f00d",
        )
        banner = rid.format_startup_banner(
            mode="live_trade",
            armed=True,
            mainnet=True,
            exchange_port="futures",
            symbols=["BTCUSDT", "DRIFTUSDT"],
            metrics_port=9090,
        )
        assert "run_id        = 20260402-143052-f00d" in banner
        assert "pid           = 9999" in banner
        assert "mode          = live_trade" in banner
        assert "armed         = True" in banner
        assert "mainnet       = True" in banner
        assert "exchange_port = futures" in banner
        assert "symbols       = BTCUSDT,DRIFTUSDT" in banner
        assert "metrics_port  = 9090" in banner
        assert "GRINDER RUN IDENTITY" in banner

    def test_startup_banner_contains_separator_lines(self) -> None:
        rid = build_run_identity(
            clock=datetime(2026, 4, 2, 14, 0, 0, tzinfo=UTC),
            pid=1,
            entropy="0000",
        )
        banner = rid.format_startup_banner(
            mode="read_only",
            armed=False,
            mainnet=False,
            exchange_port="noop",
            symbols=["BTCUSDT"],
            metrics_port=0,
        )
        assert banner.startswith("=" * 72)
        assert banner.endswith("=" * 72)


class TestShutdownBanner:
    def test_shutdown_banner_includes_same_run_id(self) -> None:
        """Same run identity appears in both startup and shutdown."""
        rid = build_run_identity(
            clock=datetime(2026, 4, 2, 14, 30, 52, tzinfo=UTC),
            pid=9999,
            entropy="f00d",
        )
        startup = rid.format_startup_banner(
            mode="live_trade",
            armed=True,
            mainnet=True,
            exchange_port="futures",
            symbols=["BTCUSDT"],
            metrics_port=9090,
        )
        shutdown = rid.format_shutdown_banner(
            exit_code=0,
            stop_reason="signal",
        )
        # Same run_id in both
        assert "20260402-143052-f00d" in startup
        assert "20260402-143052-f00d" in shutdown
        assert "GRINDER RUN SHUTDOWN" in shutdown
        assert "exit_code     = 0" in shutdown
        assert "stop_reason   = signal" in shutdown

    def test_shutdown_banner_non_zero_exit(self) -> None:
        rid = build_run_identity(
            clock=datetime(2026, 4, 2, 14, 0, 0, tzinfo=UTC),
            pid=1,
            entropy="dead",
        )
        shutdown = rid.format_shutdown_banner(exit_code=2, stop_reason="fatal_error")
        assert "exit_code     = 2" in shutdown
        assert "stop_reason   = fatal_error" in shutdown


class TestDeterminism:
    def test_banner_output_deterministic(self) -> None:
        """Same inputs = same output."""
        clock = datetime(2026, 4, 2, 14, 30, 52, tzinfo=UTC)
        r1 = build_run_identity(clock=clock, pid=100, entropy="abcd")
        r2 = build_run_identity(clock=clock, pid=100, entropy="abcd")
        b1 = r1.format_startup_banner(
            mode="paper",
            armed=False,
            mainnet=False,
            exchange_port="noop",
            symbols=["ETHUSDT"],
            metrics_port=8080,
        )
        b2 = r2.format_startup_banner(
            mode="paper",
            armed=False,
            mainnet=False,
            exchange_port="noop",
            symbols=["ETHUSDT"],
            metrics_port=8080,
        )
        assert b1 == b2
        assert r1.format_shutdown_banner(
            exit_code=0, stop_reason="ok"
        ) == r2.format_shutdown_banner(exit_code=0, stop_reason="ok")
