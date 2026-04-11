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

import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.engine import LiveActionStatus


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
