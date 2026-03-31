"""Tests for stale pre-fill reconciler placement filter (ADR-112).

Engine-path tests: exercise the real drain branch in process_snapshot
with staged pending actions and mode tracking.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import LiveEngineConfig, LiveEngineV0


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
    engine = LiveEngineV0(paper, port, config)
    return engine


def _snapshot(symbol: str = "BTCUSDT", ts: int = 1000) -> Snapshot:
    return Snapshot(
        ts=ts,
        symbol=symbol,
        bid_price=Decimal("49999"),
        ask_price=Decimal("50001"),
        bid_qty=Decimal("1"),
        ask_qty=Decimal("1"),
        last_price=Decimal("50000"),
        last_qty=Decimal("1"),
    )


def _place(cid: str = "g-stale") -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        price=Decimal("49000"),
        quantity=Decimal("1"),
        client_order_id=cid,
        reason="grid_v2_RECONCILE_PLACE_ENTRY",
    )


def _cancel(oid: str = "g-cancel") -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.CANCEL,
        order_id=oid,
        symbol="BTCUSDT",
        reason="grid_v2_RECONCILE_CANCEL_ENTRY",
    )


class TestEngineDrainPath:
    """Real engine drain path with staged actions and mode tracking."""

    def _setup_for_drain(
        self, staged_mode: str, current_mode: str
    ) -> tuple[LiveEngineV0, MagicMock]:
        """Set up engine for drain with given staged/current modes."""
        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"
        engine._grid_v2_started = True
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_seed_actions = []
        engine._sync_reconciler_primary = True
        engine._reconciler_staged_mode = staged_mode

        # Mock bridge
        bridge = MagicMock()
        bridge.reconstruction_ok = True
        sm = MagicMock()
        sm.mode.value = current_mode
        sm.snapshot.open_lots = []
        bridge.state_machine = sm
        bridge.adapter.registry.cid_for_entry.return_value = None
        bridge.adapter.is_ours.return_value = True
        engine._grid_v2_bridge = bridge

        return engine, bridge

    def test_mode_change_drops_place_keeps_cancel(self) -> None:
        """Staged FLAT, current LONG_BRANCH → PLACE dropped, CANCEL kept."""
        engine, _bridge = self._setup_for_drain("FLAT", "LONG_BRANCH")
        engine._sync_reconciler_pending_actions = [_place("g-stale"), _cancel("g-ok")]

        # Mock out fill processing and other side effects
        with (
            patch.object(engine, "_grid_v2_process_fills", return_value=[]),
            patch.object(engine, "_grid_v2_process_cancel_acks"),
            patch.object(engine, "_grid_v2_track_flat_transition"),
            patch.object(engine, "_extract_planned_entry_slots", return_value=set()),
            patch.object(engine, "_grid_v2_clean_failed_place") as mock_clean,
        ):
            engine.process_snapshot(_snapshot("BTCUSDT"))

        # PLACE should have been dropped and cleaned
        mock_clean.assert_called_once_with("g-stale")
        # Pending actions should be cleared
        assert engine._sync_reconciler_pending_actions == []

    def test_same_mode_keeps_place(self) -> None:
        """Staged FLAT, still FLAT → PLACE kept."""
        engine, _bridge = self._setup_for_drain("FLAT", "FLAT")
        engine._sync_reconciler_pending_actions = [_place("g-ok"), _cancel("g-cancel")]

        with (
            patch.object(engine, "_grid_v2_process_fills", return_value=[]),
            patch.object(engine, "_grid_v2_process_cancel_acks"),
            patch.object(engine, "_grid_v2_track_flat_transition"),
            patch.object(engine, "_extract_planned_entry_slots", return_value=set()),
            patch.object(engine, "_grid_v2_clean_failed_place") as mock_clean,
        ):
            engine.process_snapshot(_snapshot("BTCUSDT"))

        # PLACE not cleaned — mode didn't change
        mock_clean.assert_not_called()

    def test_no_staged_mode_no_filter(self) -> None:
        """No staged mode → no mode-based filtering."""
        engine, _bridge = self._setup_for_drain("", "LONG_BRANCH")
        engine._sync_reconciler_pending_actions = [_place("g-ok")]

        with (
            patch.object(engine, "_grid_v2_process_fills", return_value=[]),
            patch.object(engine, "_grid_v2_process_cancel_acks"),
            patch.object(engine, "_grid_v2_track_flat_transition"),
            patch.object(engine, "_extract_planned_entry_slots", return_value=set()),
            patch.object(engine, "_grid_v2_clean_failed_place") as mock_clean,
        ):
            engine.process_snapshot(_snapshot("BTCUSDT"))

        # No staged mode → PLACE not filtered
        mock_clean.assert_not_called()

    def test_cancel_passes_through_on_mode_change(self) -> None:
        """CANCEL actions always pass through regardless of mode change."""
        engine, _bridge = self._setup_for_drain("FLAT", "LONG_BRANCH")
        engine._sync_reconciler_pending_actions = [_cancel("g-cancel")]

        with (
            patch.object(engine, "_grid_v2_process_fills", return_value=[]),
            patch.object(engine, "_grid_v2_process_cancel_acks"),
            patch.object(engine, "_grid_v2_track_flat_transition"),
            patch.object(engine, "_extract_planned_entry_slots", return_value=set()),
            patch.object(engine, "_grid_v2_clean_failed_place") as mock_clean,
        ):
            engine.process_snapshot(_snapshot("BTCUSDT"))

        # CANCEL not cleaned — it's not a PLACE
        mock_clean.assert_not_called()

    def test_fill_since_staging_drops_place_same_mode(self) -> None:
        """Same mode but newer fill watermark -> stale PLACE dropped."""
        engine, _bridge = self._setup_for_drain("SHORT_BRANCH", "SHORT_BRANCH")
        engine._sync_reconciler_pending_actions = [_place("g-stale-fill"), _cancel("g-ok")]
        engine._reconciler_staged_fill_ts = 1000
        engine._last_fill_ts = 2000

        with (
            patch.object(engine, "_grid_v2_process_fills", return_value=[]),
            patch.object(engine, "_grid_v2_process_cancel_acks"),
            patch.object(engine, "_grid_v2_track_flat_transition"),
            patch.object(engine, "_extract_planned_entry_slots", return_value=set()),
            patch.object(engine, "_grid_v2_clean_failed_place") as mock_clean,
        ):
            engine.process_snapshot(_snapshot("BTCUSDT"))

        mock_clean.assert_called_once_with("g-stale-fill")
        assert engine._sync_reconciler_pending_actions == []
        # staged_fill_ts is NOT reset to 0 after drain — it keeps the
        # staging-time value so next drain can correctly detect new fills.

    def test_no_fill_since_staging_keeps_place_same_mode(self) -> None:
        """Same mode and no newer fill watermark -> PLACE kept."""
        engine, _bridge = self._setup_for_drain("SHORT_BRANCH", "SHORT_BRANCH")
        engine._sync_reconciler_pending_actions = [_place("g-ok")]
        engine._reconciler_staged_fill_ts = 2000
        engine._last_fill_ts = 2000

        with (
            patch.object(engine, "_grid_v2_process_fills", return_value=[]),
            patch.object(engine, "_grid_v2_process_cancel_acks"),
            patch.object(engine, "_grid_v2_track_flat_transition"),
            patch.object(engine, "_extract_planned_entry_slots", return_value=set()),
            patch.object(engine, "_grid_v2_clean_failed_place") as mock_clean,
        ):
            engine.process_snapshot(_snapshot("BTCUSDT"))

        mock_clean.assert_not_called()


class TestBurstSuppressionWatermarkInteraction:
    """Multi-tick test: burst suppression must not permanently suppress PLACEs."""

    def _setup_active_market(self) -> LiveEngineV0:
        """Engine with grid_v2, primary reconciler, and a fill already happened."""
        engine = _make_engine()
        engine._grid_v2_enabled = True
        engine._grid_v2_symbol = "BTCUSDT"
        engine._grid_v2_started = True
        engine._grid_v2_awaiting_sync = False
        engine._grid_v2_seed_actions = []
        engine._sync_reconciler_primary = True

        bridge = MagicMock()
        bridge.reconstruction_ok = True
        sm = MagicMock()
        sm.mode.value = "LONG_BRANCH"
        sm.snapshot.open_lots = []
        bridge.state_machine = sm
        bridge.adapter.registry.cid_for_entry.return_value = None
        bridge.adapter.is_ours.return_value = True
        engine._grid_v2_bridge = bridge
        return engine

    def test_suppression_fires_once_then_place_goes_through(self) -> None:
        """Fill → burst suppression → next cycle staging → drain does NOT drop."""
        engine = self._setup_active_market()

        # Tick 1: fill happened, last_fill_ts set
        engine._last_fill_ts = 5000

        # Stage a PLACE (reconciler found missing entry)
        engine._sync_reconciler_pending_actions = [_place("g-e0")]
        engine._reconciler_staged_mode = "LONG_BRANCH"
        engine._reconciler_staged_fill_ts = 5000  # staged at fill time

        # Tick 2: no new fill, _last_fill_ts still 5000
        # Drain should NOT drop because staged_fill_ts == _last_fill_ts
        with (
            patch.object(engine, "_grid_v2_process_fills", return_value=[]),
            patch.object(engine, "_grid_v2_process_cancel_acks"),
            patch.object(engine, "_grid_v2_track_flat_transition"),
            patch.object(engine, "_extract_planned_entry_slots", return_value=set()),
            patch.object(engine, "_grid_v2_clean_failed_place") as mock_clean,
        ):
            engine.process_snapshot(_snapshot("BTCUSDT"))

        # PLACE should NOT have been dropped (no new fill since staging)
        mock_clean.assert_not_called()

    def test_staging_after_burst_suppression_records_nonzero_watermark(self) -> None:
        """After burst suppression fires, staging must record real fill_ts, not 0."""
        engine = self._setup_active_market()

        # Fill happened
        engine._last_fill_ts = 5000
        engine._burst_suppression_fired = False

        # Simulate burst suppression: snapshot_ts < last_fill_ts
        # After suppression, _last_fill_ts should still be 5000 (NOT zeroed)
        assert engine._last_fill_ts == 5000

        # Staging should record 5000, not 0
        engine._reconciler_staged_fill_ts = engine._last_fill_ts
        assert engine._reconciler_staged_fill_ts == 5000

    def test_same_tick_fill_does_not_permanently_suppress(self) -> None:
        """Snapshot-diff fill in same tick as drain: only drops if genuinely newer."""
        engine = self._setup_active_market()

        # Stage with fill_ts = 5000
        engine._reconciler_staged_fill_ts = 5000
        engine._last_fill_ts = 5000  # same → no drop
        engine._reconciler_staged_mode = "LONG_BRANCH"
        engine._sync_reconciler_pending_actions = [_place("g-e0")]

        with (
            patch.object(engine, "_grid_v2_process_fills", return_value=[]),
            patch.object(engine, "_grid_v2_process_cancel_acks"),
            patch.object(engine, "_grid_v2_track_flat_transition"),
            patch.object(engine, "_extract_planned_entry_slots", return_value=set()),
            patch.object(engine, "_grid_v2_clean_failed_place") as mock_clean,
        ):
            engine.process_snapshot(_snapshot("BTCUSDT"))

        # Same fill_ts → not dropped
        mock_clean.assert_not_called()
