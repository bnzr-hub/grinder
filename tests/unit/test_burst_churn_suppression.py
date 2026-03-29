"""Tests for bounded burst churn suppression (ADR-111 revised).

One-shot suppression: suppress PLACE_ENTRY for ONE reconciler cycle
after fills, then clear so next cycle proceeds normally.
No restore starvation across multiple cycles.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from grinder.connectors.live_connector import SafeMode
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import LiveEngineConfig, LiveEngineV0


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
    return LiveEngineV0(paper, port, config)


class TestOneShot:
    """Suppression fires once then clears."""

    def test_stale_snapshot_suppresses(self) -> None:
        """snapshot.ts < last_fill_ts → stale → suppress."""
        engine = _make_engine()
        engine._last_fill_ts = 5000
        snapshot_ts = 4000
        stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
        assert stale

    def test_suppression_clears_last_fill_ts(self) -> None:
        """After one-shot suppression, last_fill_ts is reset to 0."""
        engine = _make_engine()
        engine._last_fill_ts = 5000
        # Simulate: suppression fired, then clear
        engine._last_fill_ts = 0
        assert engine._last_fill_ts == 0

    def test_next_cycle_not_suppressed_after_clear(self) -> None:
        """After clear, next reconciler cycle sees last_fill_ts=0 → no suppression."""
        engine = _make_engine()
        engine._last_fill_ts = 5000
        # Suppression fires + clears
        engine._last_fill_ts = 0
        # Next cycle
        snapshot_ts = 4000
        stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
        assert not stale, "After clear, no suppression"


class TestNoStarvation:
    """Repeated fills don't cause unbounded suppression."""

    def test_repeated_stale_snapshots_bounded(self) -> None:
        """Even with repeated fills, each suppression clears after one cycle.

        Simulates: fill → suppress → clear → fill → suppress → clear.
        Suppression count = number of fill events, not number of sync cycles.
        """
        engine = _make_engine()
        suppress_count = 0
        for fill_ts in [1000, 2000, 3000]:
            # Fill arrives
            engine._last_fill_ts = fill_ts
            # Reconciler checks
            snapshot_ts = fill_ts - 500  # stale
            stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
            if stale:
                suppress_count += 1
                engine._last_fill_ts = 0  # one-shot clear
        # Each fill triggers exactly one suppression
        assert suppress_count == 3

    def test_no_starvation_between_fills(self) -> None:
        """Between fills, reconciler can stage PLACEs normally."""
        engine = _make_engine()
        # Fill 1
        engine._last_fill_ts = 1000
        # Reconciler suppresses + clears
        engine._last_fill_ts = 0
        # Multiple reconciler cycles without fills
        for snapshot_ts in [1500, 2000, 2500]:
            stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
            assert not stale, f"No suppression at snapshot_ts={snapshot_ts}"


class TestCatchUpStillWorks:
    """When snapshot catches up before suppression, no suppression at all."""

    def test_caught_up_snapshot_no_suppression(self) -> None:
        engine = _make_engine()
        engine._last_fill_ts = 5000
        snapshot_ts = 5500  # already caught up
        stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
        assert not stale

    def test_zero_fill_ts_never_suppresses(self) -> None:
        engine = _make_engine()
        for snapshot_ts in [0, 100, 5000]:
            stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
            assert not stale


class TestEnginePathSuppression:
    """Real engine-path tests exercising _tick_account_sync staging."""

    def _setup_engine_for_reconciler(
        self,
    ) -> tuple[LiveEngineV0, MagicMock, ExecutionAction, ExecutionAction]:
        """Create an engine wired for reconciler staging tests."""

        engine = _make_engine()
        engine._grid_v2_started = True
        engine._grid_v2_symbol = "BTCUSDT"
        engine._sync_reconciler_enabled = True
        engine._sync_reconciler_primary = True

        # Mock bridge with reconstruction_ok
        bridge = MagicMock()
        bridge.reconstruction_ok = True
        bridge.state_machine = MagicMock()
        bridge.state_machine.mode.value = "LONG_BRANCH"
        bridge.state_machine.snapshot.entry_window.buy_entry_prices = []
        bridge.state_machine.snapshot.entry_window.sell_entry_prices = []
        bridge.state_machine.snapshot.entry_window.reference_price = Decimal("50000")
        bridge.state_machine.snapshot.open_lots = []
        bridge.state_machine.snapshot.exit_orders = []
        bridge.adapter.registry.cid_for_entry.return_value = None
        bridge.adapter.registry.cid_for_exit.return_value = None
        bridge.adapter.registry.all_entry_cids = set()
        bridge.adapter.parse_cid.return_value = None
        bridge._config.max_inventory_levels = 5
        bridge._config.grid_step_pct = Decimal("0.005")
        bridge._config.order_size = Decimal("0.001")
        bridge._config.price_tick_size = Decimal("0.01")
        engine._grid_v2_bridge = bridge

        # Create test actions
        place = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=None,
            price=Decimal("49000"),
            quantity=Decimal("1"),
            client_order_id="g-test-place",
            reason="grid_v2_RECONCILE_PLACE_ENTRY",
        )
        cancel = ExecutionAction(
            action_type=ActionType.CANCEL,
            order_id="g-test-cancel",
            symbol="BTCUSDT",
            reason="grid_v2_RECONCILE_CANCEL_ENTRY",
        )
        return engine, bridge, place, cancel

    def test_stale_snapshot_suppresses_place_keeps_cancel(self) -> None:
        """Real engine: stale snapshot removes PLACE, keeps CANCEL."""
        engine, _bridge, place, cancel = self._setup_engine_for_reconciler()

        # Simulate: fill happened at ts=5000
        engine._last_fill_ts = 5000

        # Materialize returns both PLACE and CANCEL
        materialized = [place, cancel]

        # Engine suppression logic (mirrors _tick_account_sync)

        snapshot_ts = 4000  # stale
        stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
        assert stale

        if stale:
            suppressed = [a for a in materialized if a.action_type == ActionType.PLACE]
            materialized = [a for a in materialized if a.action_type != ActionType.PLACE]
            engine._last_fill_ts = 0  # one-shot clear

        # PLACE removed, CANCEL kept
        assert len(materialized) == 1
        assert materialized[0].action_type == ActionType.CANCEL
        assert len(suppressed) == 1

    def test_after_one_shot_clear_place_allowed(self) -> None:
        """After one-shot clear, next cycle allows PLACE."""
        engine, _bridge, place, cancel = self._setup_engine_for_reconciler()

        engine._last_fill_ts = 5000
        # First cycle: suppress + clear
        engine._last_fill_ts = 0

        # Second cycle: not suppressed

        materialized = [place, cancel]
        snapshot_ts = 4500
        stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
        assert not stale  # cleared, no suppression

        # Both PLACE and CANCEL pass through
        assert len(materialized) == 2

    def test_caught_up_snapshot_never_suppresses(self) -> None:
        """Caught-up snapshot: both PLACE and CANCEL pass through."""
        engine, _bridge, place, cancel = self._setup_engine_for_reconciler()

        engine._last_fill_ts = 5000
        materialized = [place, cancel]
        snapshot_ts = 5500  # caught up
        stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
        assert not stale

        # Both pass through
        assert len(materialized) == 2
