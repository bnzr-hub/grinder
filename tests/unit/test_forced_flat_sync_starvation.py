"""Tests: account sync must stay alive in forced-flat terminal state.

Post-#675 canary observed STALE_TRUTH degradation after emergency
forced-flat because the short-circuit at process_snapshot() returned
before reaching _tick_account_sync(). This starved the health gate's
last_sync_success_ts, causing irreversible HEALTHY → DEGRADED_SYNC
(+15 s) → STALE_TRUTH (+30 s).

The fix adds a sync tick inside the forced-flat block. These tests
lock that invariant.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from grinder.account.syncer import AccountSyncer, SyncResult
from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.live import LiveEngineConfig, LiveEngineV0


def _snap(ts: int, symbol: str = "BTCUSDT") -> Snapshot:
    return Snapshot(
        ts=ts,
        symbol=symbol,
        bid_price=Decimal("50000"),
        ask_price=Decimal("50001"),
        bid_qty=Decimal("1"),
        ask_qty=Decimal("1"),
        last_price=Decimal("50000.5"),
        last_qty=Decimal("0.5"),
    )


def _make_engine(syncer: AccountSyncer | None = None) -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    config = LiveEngineConfig(
        armed=True,
        mode=SafeMode.LIVE_TRADE,
        symbol_whitelist=["BTCUSDT"],
    )
    engine = LiveEngineV0(paper, port, config, account_syncer=syncer)
    engine._account_sync_env_override = True
    engine._account_sync_interval_ms = 1000  # 1 second base interval
    return engine


class TestForcedFlatSyncStarvation:
    """Account sync must continue running after forced-flat latch is set."""

    def test_sync_runs_after_forced_flat(self) -> None:
        """With _forced_flat_requested=True and _forced_flat_executed=True,
        process_snapshot must still call _tick_account_sync when the
        elapsed time exceeds the sync interval."""
        syncer = MagicMock(spec=AccountSyncer)
        syncer.sync.return_value = SyncResult(snapshot=None, mismatches=[])
        engine = _make_engine(syncer)

        # Simulate post-emergency state: forced-flat already completed.
        engine._forced_flat_requested = True
        engine._forced_flat_executed = True
        # Set last sync attempt far in the past so elapsed > interval.
        engine._account_sync_last_attempt_ms = 0

        # Process a snapshot — should trigger sync despite forced-flat.
        engine.process_snapshot(_snap(ts=100_000))

        syncer.sync.assert_called_once()

    def test_health_stays_fresh_after_forced_flat(self) -> None:
        """last_sync_success_ts must update on successful sync, even in
        forced-flat terminal state. Without this, health degrades to
        STALE_TRUTH after 30 s."""
        syncer = MagicMock(spec=AccountSyncer)
        syncer.sync.return_value = SyncResult(snapshot=None, mismatches=[])
        engine = _make_engine(syncer)

        engine._forced_flat_requested = True
        engine._forced_flat_executed = True
        engine._account_sync_last_attempt_ms = 0

        ts_before = engine._health_input.last_sync_success_ts

        engine.process_snapshot(_snap(ts=100_000))

        ts_after = engine._health_input.last_sync_success_ts
        assert ts_after > ts_before, f"last_sync_success_ts not updated: {ts_before} → {ts_after}"

    def test_no_stale_truth_after_forced_flat(self) -> None:
        """Continuous forced-flat ticks must not degrade health to
        STALE_TRUTH, as long as sync succeeds."""
        syncer = MagicMock(spec=AccountSyncer)
        syncer.sync.return_value = SyncResult(snapshot=None, mismatches=[])
        engine = _make_engine(syncer)

        engine._forced_flat_requested = True
        engine._forced_flat_executed = True
        engine._account_sync_last_attempt_ms = 0

        from grinder.live.health_gate import LiveHealthMode  # noqa: PLC0415

        # Tick repeatedly with advancing timestamps.
        for i in range(10):
            engine.process_snapshot(_snap(ts=100_000 + i * 5000))

        assert engine._health_mode != LiveHealthMode.STALE_TRUTH, (
            f"Health degraded to {engine._health_mode.value} despite sync running"
        )

    def test_forced_flat_still_suppresses_actions(self) -> None:
        """Forced-flat must still return empty live_actions — the fix
        only keeps sync alive, it does NOT resume trading."""
        syncer = MagicMock(spec=AccountSyncer)
        syncer.sync.return_value = SyncResult(snapshot=None, mismatches=[])
        engine = _make_engine(syncer)

        engine._forced_flat_requested = True
        engine._forced_flat_executed = True
        engine._account_sync_last_attempt_ms = 0

        output = engine.process_snapshot(_snap(ts=100_000))

        assert output.live_actions == [], "Forced-flat must suppress all trading actions"

    def test_sync_respects_interval_in_forced_flat(self) -> None:
        """Sync must not fire on EVERY tick — it should still respect
        the scheduling interval (no sync spam)."""
        syncer = MagicMock(spec=AccountSyncer)
        syncer.sync.return_value = SyncResult(snapshot=None, mismatches=[])
        engine = _make_engine(syncer)

        engine._forced_flat_requested = True
        engine._forced_flat_executed = True
        engine._account_sync_last_attempt_ms = 100_000
        engine._account_sync_interval_ms = 5000

        # First tick: elapsed = 0, should NOT sync.
        engine.process_snapshot(_snap(ts=100_000))
        assert syncer.sync.call_count == 0

        # Second tick: elapsed = 3000 < 5000, should NOT sync.
        engine.process_snapshot(_snap(ts=103_000))
        assert syncer.sync.call_count == 0

        # Third tick: elapsed = 5000 >= 5000, should sync.
        engine.process_snapshot(_snap(ts=105_000))
        assert syncer.sync.call_count == 1
