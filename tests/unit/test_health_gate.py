"""Tests for Live Health Gate (PR-1 of production program).

Adversarial tests for truth-source degradation and write policy.
"""

from __future__ import annotations

from grinder.live.health_gate import (
    LiveHealthConfig,
    LiveHealthInput,
    LiveHealthMode,
    evaluate_health,
)


def _cfg(**kw: object) -> LiveHealthConfig:
    return LiveHealthConfig(**kw)  # type: ignore[arg-type]


def _inputs(**kw: object) -> LiveHealthInput:
    return LiveHealthInput(**kw)  # type: ignore[arg-type]


NOW = 1000.0


class TestHealthyMode:
    """HEALTHY when all sources fresh."""

    def test_all_fresh(self) -> None:
        r = evaluate_health(
            _inputs(last_sync_success_ts=NOW - 5, last_ws_message_ts=NOW - 1),
            _cfg(),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.HEALTHY
        assert r.write_allowed is True

    def test_init_no_sync_yet(self) -> None:
        """Before first sync, health is HEALTHY (init phase)."""
        r = evaluate_health(_inputs(), _cfg(), now=NOW)
        assert r.mode == LiveHealthMode.HEALTHY
        assert r.write_allowed is True
        assert r.reason == "init_no_sync_yet"


class TestDegradedSync:
    """DEGRADED_SYNC when sync soft-stale or consecutive failures."""

    def test_sync_soft_stale(self) -> None:
        r = evaluate_health(
            _inputs(last_sync_success_ts=NOW - 20, last_ws_message_ts=NOW - 1),
            _cfg(sync_soft_stale_s=15),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.DEGRADED_SYNC
        assert r.write_allowed is True  # still allowed in degraded sync

    def test_consecutive_sync_failures(self) -> None:
        r = evaluate_health(
            _inputs(
                last_sync_success_ts=NOW - 5,
                last_ws_message_ts=NOW - 1,
                consecutive_sync_failures=3,
            ),
            _cfg(sync_fail_soft_count=3),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.DEGRADED_SYNC


class TestDegradedWs:
    """DEGRADED_WS when WS disconnected but sync still fresh."""

    def test_ws_disconnected_sync_fresh(self) -> None:
        r = evaluate_health(
            _inputs(
                last_sync_success_ts=NOW - 5,
                ws_connected=False,
                last_ws_message_ts=NOW - 15,
            ),
            _cfg(),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.DEGRADED_WS
        assert r.write_allowed is True

    def test_ws_stale(self) -> None:
        r = evaluate_health(
            _inputs(
                last_sync_success_ts=NOW - 5,
                ws_connected=True,
                last_ws_message_ts=NOW - 15,
            ),
            _cfg(ws_stale_s=10),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.DEGRADED_WS


class TestStaleTruth:
    """STALE_TRUTH when sync hard-stale or clock drift storm."""

    def test_sync_hard_stale(self) -> None:
        r = evaluate_health(
            _inputs(last_sync_success_ts=NOW - 35, last_ws_message_ts=NOW - 1),
            _cfg(sync_hard_stale_s=30),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.STALE_TRUTH
        assert r.write_allowed is False
        assert r.cancel_allowed is True
        assert r.reduce_only_allowed is True

    def test_clock_drift_storm(self) -> None:
        r = evaluate_health(
            _inputs(
                last_sync_success_ts=NOW - 5,
                last_ws_message_ts=NOW - 1,
                clock_drift_errors_recent=3,
                last_clock_drift_error_ts=NOW - 10,
            ),
            _cfg(),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.STALE_TRUTH
        assert r.write_allowed is False

    def test_normal_writes_blocked_in_stale_truth(self) -> None:
        """Key invariant: no normal PLACE in STALE_TRUTH."""
        r = evaluate_health(
            _inputs(last_sync_success_ts=NOW - 40, last_ws_message_ts=NOW - 1),
            _cfg(sync_hard_stale_s=30),
            now=NOW,
        )
        assert r.write_allowed is False
        assert r.reduce_only_allowed is True


class TestPausedUnsafe:
    """PAUSED_UNSAFE when multiple truth sources hard-stale."""

    def test_both_sync_and_ws_hard_stale(self) -> None:
        r = evaluate_health(
            _inputs(last_sync_success_ts=NOW - 70, last_ws_message_ts=NOW - 70),
            _cfg(combined_hard_stale_s=60),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.PAUSED_UNSAFE
        assert r.write_allowed is False
        assert r.reduce_only_allowed is False

    def test_many_consecutive_sync_failures(self) -> None:
        r = evaluate_health(
            _inputs(
                last_sync_success_ts=NOW - 5,
                last_ws_message_ts=NOW - 1,
                consecutive_sync_failures=6,
            ),
            _cfg(sync_fail_hard_count=6),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.PAUSED_UNSAFE
        assert r.write_allowed is False
        assert r.reduce_only_allowed is False

    def test_cancel_always_allowed(self) -> None:
        """Even in PAUSED_UNSAFE, cancel is allowed."""
        r = evaluate_health(
            _inputs(last_sync_success_ts=NOW - 70, last_ws_message_ts=NOW - 70),
            _cfg(combined_hard_stale_s=60),
            now=NOW,
        )
        assert r.cancel_allowed is True


class TestRecovery:
    """Recovery from degraded to healthy."""

    def test_recovery_after_sync_success(self) -> None:
        """After sync recovers, mode returns to HEALTHY."""
        # Stale state first
        r1 = evaluate_health(
            _inputs(last_sync_success_ts=NOW - 40, last_ws_message_ts=NOW - 1),
            _cfg(sync_hard_stale_s=30),
            now=NOW,
        )
        assert r1.mode == LiveHealthMode.STALE_TRUTH

        # Then: fresh sync
        r2 = evaluate_health(
            _inputs(last_sync_success_ts=NOW - 2, last_ws_message_ts=NOW - 1),
            _cfg(sync_hard_stale_s=30),
            now=NOW,
        )
        assert r2.mode == LiveHealthMode.HEALTHY

    def test_no_premature_recovery(self) -> None:
        """Still degraded if sync is soft-stale, even if WS fresh."""
        r = evaluate_health(
            _inputs(
                last_sync_success_ts=NOW - 18,
                last_ws_message_ts=NOW - 1,
            ),
            _cfg(sync_soft_stale_s=15),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.DEGRADED_SYNC


class TestDnsStorm:
    """DNS/connectivity errors drive mode transitions."""

    def test_dns_storm_triggers_stale_truth(self) -> None:
        """3+ recent DNS errors → STALE_TRUTH, writes blocked."""
        r = evaluate_health(
            _inputs(
                last_sync_success_ts=NOW - 5,
                last_ws_message_ts=NOW - 1,
                dns_errors_recent=3,
                last_dns_error_ts=NOW - 5,
            ),
            _cfg(),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.STALE_TRUTH
        assert r.write_allowed is False

    def test_old_dns_errors_no_effect(self) -> None:
        """DNS errors > 30s ago don't affect mode."""
        r = evaluate_health(
            _inputs(
                last_sync_success_ts=NOW - 5,
                last_ws_message_ts=NOW - 1,
                dns_errors_recent=5,
                last_dns_error_ts=NOW - 40,
            ),
            _cfg(),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.HEALTHY

    def test_few_dns_errors_no_effect(self) -> None:
        """1-2 DNS errors don't trigger storm."""
        r = evaluate_health(
            _inputs(
                last_sync_success_ts=NOW - 5,
                last_ws_message_ts=NOW - 1,
                dns_errors_recent=2,
                last_dns_error_ts=NOW - 5,
            ),
            _cfg(),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.HEALTHY


class TestWsFromConnector:
    """WS state must come from connector, not snapshot arrival."""

    def test_ws_disconnected_while_processing(self) -> None:
        """WS marked disconnected → DEGRADED_WS even if engine ticks."""
        r = evaluate_health(
            _inputs(
                last_sync_success_ts=NOW - 5,
                ws_connected=False,
                last_ws_message_ts=NOW - 20,
            ),
            _cfg(ws_stale_s=10),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.DEGRADED_WS

    def test_ws_stale_message_ts(self) -> None:
        """WS connected but no messages for 15s → DEGRADED_WS."""
        r = evaluate_health(
            _inputs(
                last_sync_success_ts=NOW - 5,
                ws_connected=True,
                last_ws_message_ts=NOW - 15,
            ),
            _cfg(ws_stale_s=10),
            now=NOW,
        )
        assert r.mode == LiveHealthMode.DEGRADED_WS
