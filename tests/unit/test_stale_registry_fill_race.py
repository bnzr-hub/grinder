"""Tests for stale-registry cleaning vs fill-detection race.

Proves:
1. Entry absent for 1 sync is NOT cleaned (fill detection gets a chance)
2. Entry absent for 2 consecutive syncs IS cleaned (truly stale)
3. Entry that reappears between syncs is NOT cleaned
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
from grinder.connectors.live_connector import SafeMode
from grinder.live import LiveEngineConfig, LiveEngineV0


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    return LiveEngineV0(paper, port, LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE))


def _snap(cids: list[str], ts: int = 1000) -> AccountSnapshot:
    orders = tuple(
        OpenOrderSnap(
            order_id=cid,
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            price=Decimal("50000"),
            qty=Decimal("0.001"),
            filled_qty=Decimal("0"),
            reduce_only=False,
            status="NEW",
            ts=ts,
        )
        for cid in cids
    )
    return AccountSnapshot(positions=(), open_orders=orders, ts=ts, source="test")


def _sync_result(cids: list[str], ts: int = 2000) -> MagicMock:
    result = MagicMock()
    result.snapshot = _snap(cids, ts)
    result.mismatches = []
    result.error = None
    return result


def _setup_engine(engine: LiveEngineV0) -> MagicMock:
    engine._grid_v2_enabled = True
    engine._grid_v2_symbol = "BTCUSDT"
    engine._grid_v2_started = True
    engine._sync_reconciler_enabled = True
    syncer = MagicMock()
    engine._account_syncer = syncer
    engine._risk_base_enabled = False
    engine._symbol_risk_manager = MagicMock()
    engine._symbol_risk_manager.config.enabled = False
    engine._grid_v2_sync_reconstruct_on_position_drift = MagicMock()

    # Mock bridge with registry
    bridge = MagicMock()
    bridge.state_machine = MagicMock()
    bridge.state_machine.mode.value = "FLAT"
    bridge.state_machine.snapshot.open_lots = ()
    bridge.reconstruction_ok = True
    bridge._config.max_inventory_levels = 10
    bridge.adapter.registry.all_entry_cids = frozenset({"g-e0", "g-e1", "g-e2"})
    # ADR-180: mark as seen for stale-cleanup eligibility
    engine._grid_v2_seen_on_exchange = {"g-e0", "g-e1", "g-e2"}
    bridge.adapter.registry.all_exit_cids = frozenset()
    engine._grid_v2_bridge = bridge
    engine._grid_v2_pending_place_cids = {}
    engine._grid_v2_pending_cancels = {}
    return syncer


class TestTwoCycleStaleRegistryCleaning:
    """Stale-registry cleaning requires 2 consecutive absences."""

    def test_single_absence_does_not_clean(self) -> None:
        """First sync: g-e0 absent from exchange → NOT cleaned yet."""
        engine = _make_engine()
        syncer = _setup_engine(engine)

        # Snapshot has g-e1, g-e2 but NOT g-e0
        syncer.sync.return_value = _sync_result(["g-e1", "g-e2"], ts=2000)

        with patch("grinder.grid_v2.sync_reconciler.reconcile_grid_state") as mock_recon:
            mock_recon.return_value = MagicMock(
                actions=(),
                would_cancel=0,
                would_place=0,
                desired_entry_count=0,
                theoretical_desired_entry_count=0,
                actual_entry_count=0,
                actual_exit_count=0,
                missing_entries=0,
                extra_entries=0,
                missing_exits=0,
                extra_exits=0,
                projection_mode=MagicMock(value="UNCONSTRAINED"),
                legal_entry_capacity=None,
            )
            engine._tick_account_sync()

        # g-e0 should NOT be cleaned after just 1 absence
        engine._grid_v2_bridge.adapter.confirm_cancel_entry.assert_not_called()

    def test_two_consecutive_absences_cleans(self) -> None:
        """Two syncs: g-e0 absent both times → cleaned on second."""
        engine = _make_engine()
        syncer = _setup_engine(engine)

        mock_recon_result = MagicMock(
            actions=(),
            would_cancel=0,
            would_place=0,
            desired_entry_count=0,
            theoretical_desired_entry_count=0,
            actual_entry_count=0,
            actual_exit_count=0,
            missing_entries=0,
            extra_entries=0,
            missing_exits=0,
            extra_exits=0,
            projection_mode=MagicMock(value="UNCONSTRAINED"),
            legal_entry_capacity=None,
        )

        with patch(
            "grinder.grid_v2.sync_reconciler.reconcile_grid_state",
            return_value=mock_recon_result,
        ):
            # Sync 1: g-e0 absent
            syncer.sync.return_value = _sync_result(["g-e1", "g-e2"], ts=2000)
            engine._tick_account_sync()

            # Sync 2: g-e0 still absent
            syncer.sync.return_value = _sync_result(["g-e1", "g-e2"], ts=3000)
            engine._tick_account_sync()

        # g-e0 cleaned on second absence
        engine._grid_v2_bridge.adapter.confirm_cancel_entry.assert_called_once_with("g-e0")

    def test_reappearance_resets_tracking(self) -> None:
        """g-e0 absent, then reappears, then absent again → NOT cleaned."""
        engine = _make_engine()
        syncer = _setup_engine(engine)

        mock_recon_result = MagicMock(
            actions=(),
            would_cancel=0,
            would_place=0,
            desired_entry_count=0,
            theoretical_desired_entry_count=0,
            actual_entry_count=0,
            actual_exit_count=0,
            missing_entries=0,
            extra_entries=0,
            missing_exits=0,
            extra_exits=0,
            projection_mode=MagicMock(value="UNCONSTRAINED"),
            legal_entry_capacity=None,
        )

        with patch(
            "grinder.grid_v2.sync_reconciler.reconcile_grid_state",
            return_value=mock_recon_result,
        ):
            # Sync 1: g-e0 absent
            syncer.sync.return_value = _sync_result(["g-e1", "g-e2"], ts=2000)
            engine._tick_account_sync()

            # Sync 2: g-e0 reappears!
            syncer.sync.return_value = _sync_result(["g-e0", "g-e1", "g-e2"], ts=3000)
            engine._tick_account_sync()

            # Sync 3: g-e0 absent again — but only 1 consecutive absence
            syncer.sync.return_value = _sync_result(["g-e1", "g-e2"], ts=4000)
            engine._tick_account_sync()

        # NOT cleaned: reappearance reset the consecutive counter
        engine._grid_v2_bridge.adapter.confirm_cancel_entry.assert_not_called()


class TestFillSurvivesFirstAbsence:
    """Real engine-path: filled entry survives first absence and is processed as fill."""

    def test_filled_entry_not_cleaned_on_first_sync(self) -> None:
        """Entry disappears from snapshot (filled). First sync: NOT cleaned.
        Fill detection on next tick can still find it in registry."""
        engine = _make_engine()
        syncer = _setup_engine(engine)

        # Registry has g-e0 (the fill candidate)
        bridge = engine._grid_v2_bridge
        assert "g-e0" in bridge.adapter.registry.all_entry_cids

        # Sync 1: g-e0 absent from snapshot (it was filled on exchange)
        syncer.sync.return_value = _sync_result(["g-e1", "g-e2"], ts=2000)

        with patch("grinder.grid_v2.sync_reconciler.reconcile_grid_state") as mock_recon:
            mock_recon.return_value = MagicMock(
                actions=(),
                would_cancel=0,
                would_place=0,
                desired_entry_count=0,
                theoretical_desired_entry_count=0,
                actual_entry_count=0,
                actual_exit_count=0,
                missing_entries=0,
                extra_entries=0,
                missing_exits=0,
                extra_exits=0,
                projection_mode=MagicMock(value="UNCONSTRAINED"),
                legal_entry_capacity=None,
            )
            engine._tick_account_sync()

        # g-e0 NOT cleaned — still in registry for fill detection
        bridge.adapter.confirm_cancel_entry.assert_not_called()
        # Registry still contains g-e0 (fill detection can find it)
        assert "g-e0" in bridge.adapter.registry.all_entry_cids

    def test_fill_detection_uses_registry_before_stale_cleaning(self) -> None:
        """Full sequence: entry absent → first sync preserves it →
        fill detection finds it → fill processed → second sync cleans."""
        engine = _make_engine()
        syncer = _setup_engine(engine)

        bridge = engine._grid_v2_bridge

        # Sync 1: g-e0 absent (filled)
        syncer.sync.return_value = _sync_result(["g-e1", "g-e2"], ts=2000)

        with patch("grinder.grid_v2.sync_reconciler.reconcile_grid_state") as mock_recon:
            mock_recon.return_value = MagicMock(
                actions=(),
                would_cancel=0,
                would_place=0,
                desired_entry_count=0,
                theoretical_desired_entry_count=0,
                actual_entry_count=0,
                actual_exit_count=0,
                missing_entries=0,
                extra_entries=0,
                missing_exits=0,
                extra_exits=0,
                projection_mode=MagicMock(value="UNCONSTRAINED"),
                legal_entry_capacity=None,
            )
            engine._tick_account_sync()

        # After first sync: g-e0 still in registry
        assert "g-e0" in bridge.adapter.registry.all_entry_cids

        # Between syncs: fill detection would run in process_snapshot
        # and find g-e0 in registry → process it as a fill.
        # After fill processing, g-e0 is removed from registry by
        # bridge.adapter.confirm_entry_fill(). Simulate this:
        bridge.adapter.registry.all_entry_cids = frozenset({"g-e1", "g-e2"})

        # Sync 2: g-e0 still absent, but now also gone from registry
        # (fill detection already processed it). Stale cleaning finds
        # nothing new to clean.
        syncer.sync.return_value = _sync_result(["g-e1", "g-e2"], ts=3000)

        with patch("grinder.grid_v2.sync_reconciler.reconcile_grid_state") as mock_recon2:
            mock_recon2.return_value = MagicMock(
                actions=(),
                would_cancel=0,
                would_place=0,
                desired_entry_count=0,
                theoretical_desired_entry_count=0,
                actual_entry_count=0,
                actual_exit_count=0,
                missing_entries=0,
                extra_entries=0,
                missing_exits=0,
                extra_exits=0,
                projection_mode=MagicMock(value="UNCONSTRAINED"),
                legal_entry_capacity=None,
            )
            engine._tick_account_sync()

        # g-e0 was already removed by fill detection, NOT by stale cleaning.
        # Stale cleaning never touched it.
        bridge.adapter.confirm_cancel_entry.assert_not_called()


class TestRealFillPathBeatsStaleClean:
    """Real _grid_v2_process_fills consumes the entry before stale cleaning."""

    def test_process_fills_finds_entry_after_grace_cycle(self) -> None:
        """Sync 1: entry absent, grace. Between syncs: real fill detection
        via _grid_v2_process_fills finds it. Sync 2: nothing left to clean."""

        engine = _make_engine()
        syncer = _setup_engine(engine)
        bridge = engine._grid_v2_bridge

        # Use proper CID format so parse_client_order_id works
        fill_cid = "g_g_BTCUSDT_e0_1000_0"
        stay_cids = ["g_g_BTCUSDT_e1_1000_0", "g_g_BTCUSDT_e2_1000_0"]
        bridge.adapter.registry.all_entry_cids = frozenset({fill_cid, *stay_cids})
        bridge.adapter.registry.all_exit_cids = frozenset()
        # Mark CIDs as fill-eligible (simulating they were live on exchange)
        engine._grid_v2_fill_eligible_cids.update({fill_cid, *stay_cids})

        # Make bridge.on_fill return a non-rejected result
        bridge.on_fill = MagicMock(
            return_value=MagicMock(
                rejected=False,
                execution_actions=[],
                transition=MagicMock(
                    snapshot=MagicMock(open_lots=[MagicMock(qty=Decimal("0.001"))]),
                ),
            )
        )
        entry_reg = MagicMock()
        entry_reg.side = MagicMock(value="BUY")
        entry_reg.price = Decimal("50000")
        bridge.adapter.registry.lookup_entry = MagicMock(return_value=entry_reg)
        bridge.adapter.registry.lookup_exit = MagicMock(return_value=None)

        # Sync 1: fill_cid absent from snapshot → grace cycle
        syncer.sync.return_value = _sync_result(stay_cids, ts=2000)

        mock_recon_result = MagicMock(
            actions=(),
            would_cancel=0,
            would_place=0,
            desired_entry_count=0,
            theoretical_desired_entry_count=0,
            actual_entry_count=0,
            actual_exit_count=0,
            missing_entries=0,
            extra_entries=0,
            missing_exits=0,
            extra_exits=0,
            projection_mode=MagicMock(value="UNCONSTRAINED"),
            legal_entry_capacity=None,
        )
        with patch(
            "grinder.grid_v2.sync_reconciler.reconcile_grid_state",
            return_value=mock_recon_result,
        ):
            engine._tick_account_sync()

        # fill_cid still in registry (grace cycle preserved it)
        bridge.adapter.confirm_cancel_entry.assert_not_called()

        # Between syncs: real fill detection via _grid_v2_process_fills.
        # Snapshot shows stay_cids only. fill_cid is in registry but
        # NOT on exchange → detected as fill.
        engine._last_account_snapshot = _snap(stay_cids, ts=2000)
        engine._grid_v2_process_fills("BTCUSDT", 2500)

        # bridge.on_fill was called for the disappeared fill_cid
        assert bridge.on_fill.called
        actual_cid = bridge.on_fill.call_args[0][0]
        assert actual_cid == fill_cid

        # After fill processing, registry no longer has fill_cid
        bridge.adapter.registry.all_entry_cids = frozenset(stay_cids)

        # Sync 2: fill_cid gone from registry (fill consumed it)
        syncer.sync.return_value = _sync_result(stay_cids, ts=3000)
        with patch(
            "grinder.grid_v2.sync_reconciler.reconcile_grid_state",
            return_value=mock_recon_result,
        ):
            engine._tick_account_sync()

        # Stale cleaning never touched fill_cid
        bridge.adapter.confirm_cancel_entry.assert_not_called()


class TestNeverSeenFallbackSurvivesAdr181:
    """ADR-181 regression guard: bounded never-seen cleanup must still work
    for genuinely never-visible, never-filled entries after the new fill-consumed
    termination logic was added.
    """

    def test_true_never_seen_cid_still_cleaned_after_max_gens(self) -> None:
        """A CID that was placed, never appeared in any snapshot, and never
        received a fill event must still be cleaned by the bounded fallback
        after _GRID_V2_NEVER_SEEN_MAX_GENS sync cycles."""
        from grinder.live.engine import _GRID_V2_NEVER_SEEN_MAX_GENS  # noqa: PLC0415

        engine = _make_engine()
        syncer = _setup_engine(engine)
        bridge = engine._grid_v2_bridge

        # Replace the default seen CIDs with a single never-seen CID.
        never_seen_cid = "g_g_BTCUSDT_e0_1000_0"
        bridge.adapter.registry.all_entry_cids = frozenset({never_seen_cid})
        engine._grid_v2_seen_on_exchange = set()  # never appeared
        engine._grid_v2_entry_reg_gen = {never_seen_cid: 0}
        # Advance account_sync_generation so (gen+1 - reg_gen) >= threshold.
        engine._account_sync_generation = _GRID_V2_NEVER_SEEN_MAX_GENS

        # Snapshot never contains the CID.
        syncer.sync.return_value = _sync_result([], ts=2000)

        mock_recon_result = MagicMock(
            actions=(),
            would_cancel=0,
            would_place=0,
            desired_entry_count=0,
            theoretical_desired_entry_count=0,
            actual_entry_count=0,
            actual_exit_count=0,
            missing_entries=0,
            extra_entries=0,
            missing_exits=0,
            extra_exits=0,
            projection_mode=MagicMock(value="UNCONSTRAINED"),
            legal_entry_capacity=None,
        )
        with patch(
            "grinder.grid_v2.sync_reconciler.reconcile_grid_state",
            return_value=mock_recon_result,
        ):
            engine._tick_account_sync()

        # Bounded fallback must fire on the genuinely never-seen CID.
        bridge.adapter.confirm_cancel_entry.assert_called_once_with(never_seen_cid)

    def test_fill_consumed_cid_is_not_touched_by_never_seen_fallback(self) -> None:
        """ADR-181 core invariant: after process_user_data_event handles a fill
        for a CID (popping it from _grid_v2_entry_reg_gen), the never-seen fallback
        loop must not later fire on that CID even if max_gens has elapsed."""
        from grinder.live.engine import _GRID_V2_NEVER_SEEN_MAX_GENS  # noqa: PLC0415

        engine = _make_engine()
        syncer = _setup_engine(engine)
        bridge = engine._grid_v2_bridge

        filled_cid = "g_g_BTCUSDT_e0_1000_0"
        bridge.adapter.registry.all_entry_cids = frozenset({filled_cid})
        engine._grid_v2_seen_on_exchange = set()  # never appeared on snapshot
        # Simulate the post-ADR-181 state after a user-data fill:
        # entry_reg_gen has been popped by process_user_data_event.
        engine._grid_v2_entry_reg_gen = {}
        engine._grid_v2_user_fill_seen.add(filled_cid)
        engine._account_sync_generation = _GRID_V2_NEVER_SEEN_MAX_GENS * 2

        syncer.sync.return_value = _sync_result([], ts=2000)
        mock_recon_result = MagicMock(
            actions=(),
            would_cancel=0,
            would_place=0,
            desired_entry_count=0,
            theoretical_desired_entry_count=0,
            actual_entry_count=0,
            actual_exit_count=0,
            missing_entries=0,
            extra_entries=0,
            missing_exits=0,
            extra_exits=0,
            projection_mode=MagicMock(value="UNCONSTRAINED"),
            legal_entry_capacity=None,
        )
        with patch(
            "grinder.grid_v2.sync_reconciler.reconcile_grid_state",
            return_value=mock_recon_result,
        ):
            engine._tick_account_sync()

        # The fill-consumed CID must NOT be cleaned by the never-seen fallback,
        # because ADR-181 removed its entry from _grid_v2_entry_reg_gen at fill
        # time, making reg_gen is None → condition `reg_gen is not None` fails.
        bridge.adapter.confirm_cancel_entry.assert_not_called()
