"""Tests for ADR-179 smart TTL refinement (2026-04-11).

Smart TTL asks the same ``compute_effective_entry_keys`` helper the reconciler
uses and skips aged entries that still occupy a valid effective grid slot.
Out-of-set aged entries (orphans, post-transition stale topology) are still
retired exactly as before — ADR-179's original protection is preserved.

Invariant under test: **age alone is not enough**. The decision to retire an
aged entry depends on whether it still corresponds to a current effective
``(side, price)``.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.engine import (
    _GRID_V2_STALE_SKIP_LOG_KEEPALIVE_GENS,
    LiveActionStatus,
)

if TYPE_CHECKING:
    import pytest


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    return LiveEngineV0(paper, port, LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE))


def _parsed_entry(ts: int) -> MagicMock:
    """Return a mock grid_v2 CID parse result for an ENTRY at given timestamp."""
    m = MagicMock()
    m.kind.value = "ENTRY"
    m.ts = ts
    return m


def _order(cid: str, side: str, price: str, order_ts: int = 1000) -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=cid,
        symbol="BTCUSDT",
        side=side,
        order_type="LIMIT",
        price=Decimal(price),
        qty=Decimal("0.001"),
        filled_qty=Decimal("0"),
        reduce_only=False,
        status="NEW",
        ts=order_ts,
    )


def _snap(orders: list[OpenOrderSnap], ts: int = 2000) -> AccountSnapshot:
    return AccountSnapshot(positions=(), open_orders=tuple(orders), ts=ts, source="test")


def _sync_result(orders: list[OpenOrderSnap], ts: int = 2000) -> MagicMock:
    result = MagicMock()
    result.snapshot = _snap(orders, ts)
    result.mismatches = []
    result.error = None
    return result


def _recon_empty() -> MagicMock:
    return MagicMock(
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
        inventory_headroom=None,
        inflight_entry_places=0,
        inflight_entry_cancels=0,
        inflight_exit_places=0,
        inflight_exit_cancels=0,
    )


def _setup_engine(
    engine: LiveEngineV0,
    cid_ts_by_cid: dict[str, int],
) -> MagicMock:
    """Wire engine just enough to exercise the ADR-179 stale-retire block.

    ``cid_ts_by_cid`` maps each CID to the parsed CID timestamp that the
    mocked ``bridge.adapter.parse_cid`` will return. The engine computes
    ``age_s = int(time.time()) - parsed.ts`` so callers control aged vs
    fresh by picking ``ts`` relative to ``time.time()``.
    """
    engine._grid_v2_enabled = True
    engine._grid_v2_symbol = "BTCUSDT"
    engine._grid_v2_started = True
    engine._grid_v2_awaiting_sync = False
    engine._sync_reconciler_enabled = True
    engine._grid_v2_pending_cancels = {}
    engine._grid_v2_pending_place_cids = {}
    engine._grid_v2_seen_on_exchange = set()
    engine._risk_base_enabled = False
    engine._symbol_risk_manager = MagicMock()
    engine._symbol_risk_manager.config.enabled = False
    # Stub out position-drift reconstruct (method, not attribute — bypass mypy
    # with setattr, mirroring how engine methods are stubbed in other
    # grid_v2 sync tests).
    setattr(  # noqa: B010
        engine,
        "_grid_v2_sync_reconstruct_on_position_drift",
        MagicMock(),
    )

    syncer = MagicMock()
    engine._account_syncer = syncer

    bridge = MagicMock()
    bridge.state_machine = MagicMock()
    bridge.state_machine.mode.value = "FLAT"
    bridge.state_machine.snapshot.open_lots = ()
    bridge.reconstruction_ok = True
    bridge._config.max_inventory_levels = 10
    bridge.adapter.registry.all_entry_cids = frozenset(cid_ts_by_cid.keys())
    bridge.adapter.registry.all_exit_cids = frozenset()

    def _parse_cid(cid: str) -> MagicMock | None:
        ts = cid_ts_by_cid.get(cid)
        if ts is None:
            return None
        return _parsed_entry(ts)

    bridge.adapter.parse_cid.side_effect = _parse_cid
    engine._grid_v2_bridge = bridge
    return syncer


def _run_tick(
    engine: LiveEngineV0,
    syncer: MagicMock,
    orders: list[OpenOrderSnap],
    effective_keys: frozenset[tuple[OrderSide, Decimal]],
) -> list[str]:
    """Run one sync tick and return CANCEL ``order_id``s that reached dispatch."""
    syncer.sync.return_value = _sync_result(orders)
    dispatched_cancels: list[str] = []

    def _capture(action: ExecutionAction, _ts_ms: int) -> MagicMock:
        if action.action_type == ActionType.CANCEL and action.order_id:
            dispatched_cancels.append(action.order_id)
        status_mock = MagicMock()
        status_mock.status = LiveActionStatus.EXECUTED
        return status_mock

    with (
        patch(
            "grinder.grid_v2.sync_reconciler.compute_effective_entry_keys",
            return_value=effective_keys,
        ),
        patch(
            "grinder.grid_v2.sync_reconciler.reconcile_grid_state",
            return_value=_recon_empty(),
        ),
        patch.object(engine, "_process_action", side_effect=_capture),
    ):
        engine._tick_account_sync()

    return dispatched_cancels


class TestADR179SmartTTL:
    """Smart TTL: aged entries on effective slots are spared; orphans still go."""

    def test_aged_in_set_entry_is_not_retired(self) -> None:
        """An aged entry still on an effective ``(side, price)`` must survive.

        Before smart TTL: this entry was cancelled unconditionally every 600s
        even though its slot was still desired, producing a ~1 sync-cycle
        coverage gap with no safety benefit. After smart TTL: no cancel.
        """
        engine = _make_engine()
        now = int(time.time())
        cid = "g-healthy-aged"
        syncer = _setup_engine(engine, {cid: now - 700})  # age > 600s

        order = _order(cid, "BUY", "50000")
        effective = frozenset({(OrderSide.BUY, Decimal("50000"))})

        dispatched = _run_tick(engine, syncer, [order], effective)

        assert cid not in dispatched, (
            "Smart TTL regression: aged entry still on effective slot was "
            "retired. Age alone must not be enough when the slot is valid."
        )

    def test_aged_out_of_set_entry_is_still_retired(self) -> None:
        """Orphan/out-of-set aged entries are still retired (ADR-179 intent)."""
        engine = _make_engine()
        now = int(time.time())
        cid = "g-orphan-aged"
        syncer = _setup_engine(engine, {cid: now - 700})

        order = _order(cid, "BUY", "49000")
        effective: frozenset[tuple[OrderSide, Decimal]] = frozenset()

        dispatched = _run_tick(engine, syncer, [order], effective)

        assert dispatched == [cid], "Out-of-set aged entry must still be retired by ADR-179."

    def test_mixed_in_set_and_out_of_set_only_orphan_retired(self) -> None:
        """Selectivity check: in-set survives, out-of-set goes. No cross-leakage."""
        engine = _make_engine()
        now = int(time.time())
        healthy = "g-healthy"
        orphan = "g-orphan"
        syncer = _setup_engine(
            engine,
            {healthy: now - 700, orphan: now - 700},
        )

        orders = [
            _order(healthy, "BUY", "50000"),
            _order(orphan, "SELL", "49900"),
        ]
        effective = frozenset({(OrderSide.BUY, Decimal("50000"))})

        dispatched = _run_tick(engine, syncer, orders, effective)

        assert dispatched == [orphan], (
            f"Mixed case: expected only {orphan!r} retired, got {dispatched!r}"
        )
        assert healthy not in dispatched

    def test_non_aged_entry_is_never_retired(self) -> None:
        """Fresh entry (age <= TTL) is not touched by ADR-179, regardless of fit.

        ADR-179 is strictly age-gated. Entries below the TTL remain the
        reconciler's responsibility even when out-of-set.
        """
        engine = _make_engine()
        now = int(time.time())
        cid = "g-fresh"
        syncer = _setup_engine(engine, {cid: now - 10})  # well below TTL

        order = _order(cid, "BUY", "49000")  # off-grid but fresh
        effective: frozenset[tuple[OrderSide, Decimal]] = frozenset()

        dispatched = _run_tick(engine, syncer, [order], effective)

        assert dispatched == [], "Fresh entry was retired by ADR-179; age gate violated."

    def test_truly_stale_far_from_grid_seed_still_retired(self) -> None:
        """Regression guard for ADR-179's original MAGMA/RAVE incident class.

        A 30-minute-old seed entry sitting far from the current effective
        grid (e.g. after significant price movement) is exactly what
        ADR-179 was added to catch. Smart TTL must not re-open this.
        """
        engine = _make_engine()
        now = int(time.time())
        cid = "g-seed-stale"
        syncer = _setup_engine(engine, {cid: now - 1800})  # 30 min old

        order = _order(cid, "SELL", "55000")  # far from current grid
        effective = frozenset(
            {
                (OrderSide.BUY, Decimal("50000")),
                (OrderSide.SELL, Decimal("50100")),
            }
        )

        dispatched = _run_tick(engine, syncer, [order], effective)

        assert dispatched == [cid], (
            "Regression: truly stale far-from-grid seed entry was NOT "
            "retired. ADR-179's original protection against delayed burst "
            "fills on orphan seeds must remain intact."
        )


def _stale_log_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Filter caplog to GRID_V2_STALE_ENTRIES_RETIRED records only."""
    return [
        r
        for r in caplog.records
        if r.name == "grinder.live.engine" and "GRID_V2_STALE_ENTRIES_RETIRED" in r.getMessage()
    ]


class TestADR179SkipOnlyLogThrottle:
    """Skip-only `count=0 skipped_in_set=N` logs are throttled to log-on-change
    plus periodic keepalive. Real retire (`count > 0`) is unthrottled and
    refreshes the throttle state.
    """

    def _run_skip_only(
        self,
        engine: LiveEngineV0,
        syncer: MagicMock,
        order: OpenOrderSnap,
        effective: frozenset[tuple[OrderSide, Decimal]],
    ) -> None:
        """Run one tick that produces a skip-only condition (no real retire)."""
        _run_tick(engine, syncer, [order], effective)

    def test_repeated_steady_state_logs_only_on_first_cycle(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Steady state: same skipped_in_set N cycles in a row → 1 log line."""
        engine = _make_engine()
        now = int(time.time())
        cid = "g-healthy"
        syncer = _setup_engine(engine, {cid: now - 700})
        order = _order(cid, "BUY", "50000")
        effective = frozenset({(OrderSide.BUY, Decimal("50000"))})

        with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
            for _ in range(5):
                self._run_skip_only(engine, syncer, order, effective)

        records = _stale_log_records(caplog)
        assert len(records) == 1, (
            f"Expected exactly 1 skip-only log over 5 steady-state cycles, "
            f"got {len(records)}: {[r.getMessage() for r in records]}"
        )
        assert "skipped_in_set=1" in records[0].getMessage()
        assert "count=0" in records[0].getMessage()

    def test_change_in_skipped_count_re_emits_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """When skipped_in_set changes (e.g. another aged in-set entry appears),
        the throttle releases and the new count is logged immediately."""
        engine = _make_engine()
        now = int(time.time())
        cid_a = "g-healthy-a"
        cid_b = "g-healthy-b"
        syncer = _setup_engine(
            engine,
            {cid_a: now - 700, cid_b: now - 700},
        )
        order_a = _order(cid_a, "BUY", "50000")
        order_b = _order(cid_b, "SELL", "50100")
        effective = frozenset(
            {
                (OrderSide.BUY, Decimal("50000")),
                (OrderSide.SELL, Decimal("50100")),
            }
        )

        with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
            # Tick 1: 1 aged in-set
            _run_tick(engine, syncer, [order_a], effective)
            # Tick 2-3: same condition — throttled
            _run_tick(engine, syncer, [order_a], effective)
            _run_tick(engine, syncer, [order_a], effective)
            # Tick 4: 2 aged in-set — changed → must re-emit
            _run_tick(engine, syncer, [order_a, order_b], effective)
            # Tick 5: still 2 — throttled again
            _run_tick(engine, syncer, [order_a, order_b], effective)

        records = _stale_log_records(caplog)
        messages = [r.getMessage() for r in records]
        assert len(records) == 2, (
            f"Expected 2 skip-only logs (tick 1 + tick 4), got {len(records)}: {messages}"
        )
        assert "skipped_in_set=1" in messages[0]
        assert "skipped_in_set=2" in messages[1]

    def test_keepalive_emits_after_threshold_cycles(self, caplog: pytest.LogCaptureFixture) -> None:
        """After KEEPALIVE_GENS cycles in unchanged steady state, a heartbeat
        log fires so operators can confirm the path is still active."""
        engine = _make_engine()
        now = int(time.time())
        cid = "g-healthy"
        syncer = _setup_engine(engine, {cid: now - 700})
        order = _order(cid, "BUY", "50000")
        effective = frozenset({(OrderSide.BUY, Decimal("50000"))})

        with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
            for _ in range(_GRID_V2_STALE_SKIP_LOG_KEEPALIVE_GENS + 1):
                _run_tick(engine, syncer, [order], effective)

        records = _stale_log_records(caplog)
        assert len(records) == 2, (
            f"Expected exactly 2 logs over {_GRID_V2_STALE_SKIP_LOG_KEEPALIVE_GENS + 1} "
            f"cycles (tick 1 + keepalive), got {len(records)}"
        )
        for r in records:
            assert "skipped_in_set=1" in r.getMessage()

    def test_real_retire_path_is_unthrottled(self, caplog: pytest.LogCaptureFixture) -> None:
        """`count > 0` real retire logs immediately every cycle, regardless of
        prior skip-only throttle state. The throttle must not gate retire logs.
        """
        engine = _make_engine()
        now = int(time.time())
        orphan = "g-orphan"
        syncer = _setup_engine(engine, {orphan: now - 700})
        order = _order(orphan, "SELL", "49000")
        effective: frozenset[tuple[OrderSide, Decimal]] = frozenset()

        with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
            for _ in range(3):
                # Re-add the orphan each tick because dispatch removes it from
                # pending_cancels — keep simulating "fresh aged orphan present"
                # to assert each cycle still logs.
                engine._grid_v2_pending_cancels.clear()
                _run_tick(engine, syncer, [order], effective)

        records = _stale_log_records(caplog)
        assert len(records) == 3, (
            f"Real retire path is throttled: expected 3 logs, got {len(records)}"
        )
        for r in records:
            msg = r.getMessage()
            assert "count=1" in msg
            assert "skipped_in_set=0" in msg

    def test_real_retire_resets_skip_throttle_state(self, caplog: pytest.LogCaptureFixture) -> None:
        """After a real retire, the next skip-only event must log even if
        the count matches the prior skip-only count, because the registry
        just changed and operators need to see the new context.
        """
        engine = _make_engine()
        now = int(time.time())
        healthy = "g-healthy"
        orphan = "g-orphan"
        syncer = _setup_engine(
            engine,
            {healthy: now - 700, orphan: now - 700},
        )
        order_healthy = _order(healthy, "BUY", "50000")
        order_orphan = _order(orphan, "SELL", "49000")
        effective = frozenset({(OrderSide.BUY, Decimal("50000"))})

        with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
            # Tick 1: skip-only (1 in-set, no orphan yet) → log emitted
            _run_tick(engine, syncer, [order_healthy], effective)
            # Tick 2: same condition → throttled
            _run_tick(engine, syncer, [order_healthy], effective)
            # Tick 3: orphan appears → real retire fires (count=1) AND
            # in-set still skipped (skipped_in_set=1). Real retire path runs.
            engine._grid_v2_pending_cancels.clear()
            _run_tick(engine, syncer, [order_healthy, order_orphan], effective)
            # Tick 4: orphan gone, only healthy → skip-only with same count=1.
            # Must log because retire reset the throttle state.
            _run_tick(engine, syncer, [order_healthy], effective)

        records = _stale_log_records(caplog)
        messages = [r.getMessage() for r in records]
        assert len(records) == 3, (
            f"Expected 3 logs (tick 1 skip + tick 3 retire + tick 4 reset-skip), "
            f"got {len(records)}: {messages}"
        )
        assert "count=0" in messages[0] and "skipped_in_set=1" in messages[0]
        assert "count=1" in messages[1] and "skipped_in_set=1" in messages[1]
        assert "count=0" in messages[2] and "skipped_in_set=1" in messages[2]
