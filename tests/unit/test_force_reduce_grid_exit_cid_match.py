"""Regression tests for FORCE_REDUCE budget-clear CID ID matching.

Post-#675 canary (2026-04-11 22:07:16 → 22:10:59) observed a catastrophic
deadlock on AIOTUSDT:

    22:07:12 GRID_ADVERSE_LEVEL_BREACHED level=16
    22:07:16 FORCE_REDUCE_EXIT_ORDERS_CLEARED  ← falsely logged
    22:07:16 SYMBOL_UNLOAD_STARTED
    22:07:16 GRID_V2_EXIT_SUPPRESSED REDUCE_ONLY_BUDGET_EXCEEDED
             open_ro=670 reserved=0 new=134 position=670 available=0
    ... 10 retries over 4 minutes, all blocked ...
    22:10:59 GRID_ADVERSE_LEVEL_BREACHED level=20  ← escalation
    22:11:00 EMERGENCY EXIT: cancel_all + MARKET order (last-line defense)

**Root cause, proven in code:**

1. ``binance_futures_port.py:644`` — ``OpenOrderSnap.order_id`` is populated
   from ``o.get("clientOrderId") or str(o.get("orderId", ""))``, so it
   holds the grinder-generated client order ID (e.g.
   ``g_g_SYMBOL_x0_TS_0``) that was sent to the exchange.

2. ``grid_v2/adapter.py:521`` — when a PLACE_EXIT is registered, the
   adapter stores two distinct strings on ``ExitRegistration``:
   - ``cid`` = the real clientOrderId sent to the exchange
   - ``exit_order_id = f"exit-{entry_order_id}"`` = a state-machine
     internal string, **never sent to the exchange**

3. ``engine.py::_get_grid_exit_order_ids`` (pre-fix) returned
   ``{reg.exit_order_id for reg in registry}`` — a set of
   ``"exit-..."`` strings.

4. ``engine.py::_count_grid_exits`` checked
   ``order.order_id in grid_exit_ids`` — comparing real clientOrderIds
   against ``"exit-..."`` strings. The intersection is **always empty**.

5. Consequence: ``_count_grid_exits`` returns 0 on the very first cycle
   of force-reduce pre-clear, the engine falsely logs
   ``FORCE_REDUCE_EXIT_ORDERS_CLEARED``, advances to
   ``_force_reduce_exits_cleared = True``, activates ``symbol_unload``,
   which immediately runs ``SYMBOL_UNLOAD_STEP`` → exchange budget
   guard rejects because existing exits still saturate position →
   retry forever until ADVERSE_LEVEL_20 emergency market close fires.

**Fix:** ``_get_grid_exit_order_ids`` now returns
``set(bridge.adapter.registry.all_exit_cids)`` — the real client order
IDs that match ``OpenOrderSnap.order_id`` populated from the exchange.

These tests lock the CID-based matching behavior so any future refactor
that reintroduces the ``exit_order_id`` vs ``cid`` confusion will fail
immediately.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
from grinder.connectors.live_connector import SafeMode
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.engine import LiveActionStatus

if TYPE_CHECKING:
    from grinder.live.engine import LiveAction


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    return LiveEngineV0(paper, port, LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE))


def _make_registry_with_exits(
    cids_and_state_ids: list[tuple[str, str]],
) -> MagicMock:
    """Build a fake adapter.registry where each exit has:
    - cid (the real clientOrderId sent to the exchange)
    - exit_order_id (state-machine internal string, NEVER sent anywhere)

    These MUST be different strings for the test to prove the fix is
    matching against the right one.
    """
    registry = MagicMock()
    registry.all_exit_cids = frozenset(cid for cid, _ in cids_and_state_ids)

    def _lookup_exit(cid: str) -> MagicMock | None:
        for c, sid in cids_and_state_ids:
            if c == cid:
                reg = MagicMock()
                reg.cid = c
                reg.exit_order_id = sid
                reg.lot_id = f"lot-{c}"
                return reg
        return None

    registry.lookup_exit.side_effect = _lookup_exit
    return registry


def _make_order_snap(
    *,
    cid: str,
    symbol: str = "ENJUSDT",
    side: str = "BUY",
    price: str = "0.0312",
    qty: str = "273",
    reduce_only: bool = True,
) -> OpenOrderSnap:
    """Exchange snapshot order: ``order_id`` holds the clientOrderId
    (this is what ``binance_futures_port.py:644`` populates in real runs)."""
    return OpenOrderSnap(
        order_id=cid,
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        price=Decimal(price),
        qty=Decimal(qty),
        filled_qty=Decimal("0"),
        reduce_only=reduce_only,
        status="NEW",
        ts=1000,
    )


def _make_snapshot(orders: list[OpenOrderSnap]) -> AccountSnapshot:
    return AccountSnapshot(
        positions=(),
        open_orders=tuple(orders),
        ts=2000,
        source="test",
    )


class TestGetGridExitOrderIds:
    """``_get_grid_exit_order_ids`` must return the real CIDs, not the
    state-machine internal ``exit_order_id`` strings."""

    def test_returns_real_cids_not_state_machine_ids(self) -> None:
        """Two exits registered: the returned set must be the set of
        CIDs, not the set of ``exit-...`` strings. This is the exact
        seam that was broken pre-fix."""
        engine = _make_engine()
        bridge = MagicMock()
        bridge.adapter.registry = _make_registry_with_exits(
            [
                ("g_g_ENJUSDT_x0_1775943976_0", "exit-g_g_ENJUSDT_e5_1775943791_0"),
                ("g_g_ENJUSDT_x1_1775943988_0", "exit-g_g_ENJUSDT_e6_1775943792_0"),
            ]
        )
        engine._grid_v2_bridge = bridge

        ids = engine._get_grid_exit_order_ids()

        # The returned set must contain the REAL CIDs (what the exchange sees)
        assert ids == {
            "g_g_ENJUSDT_x0_1775943976_0",
            "g_g_ENJUSDT_x1_1775943988_0",
        }
        # It must NOT contain the state-machine internal strings
        assert "exit-g_g_ENJUSDT_e5_1775943791_0" not in ids
        assert "exit-g_g_ENJUSDT_e6_1775943792_0" not in ids

    def test_empty_registry_returns_empty_set(self) -> None:
        engine = _make_engine()
        bridge = MagicMock()
        bridge.adapter.registry = _make_registry_with_exits([])
        engine._grid_v2_bridge = bridge
        assert engine._get_grid_exit_order_ids() == set()

    def test_no_bridge_returns_empty_set(self) -> None:
        engine = _make_engine()
        engine._grid_v2_bridge = None
        assert engine._get_grid_exit_order_ids() == set()

    def test_no_adapter_returns_empty_set(self) -> None:
        engine = _make_engine()
        bridge = MagicMock()
        bridge.adapter = None
        engine._grid_v2_bridge = bridge
        assert engine._get_grid_exit_order_ids() == set()


class TestCountGridExits:
    """``_count_grid_exits`` must find exits by CID match."""

    def test_counts_matching_cids_on_snapshot(self) -> None:
        """Snapshot has 2 reduceOnly orders with CIDs that match the
        registry. Count must be 2."""
        engine = _make_engine()
        bridge = MagicMock()
        bridge.adapter.registry = _make_registry_with_exits(
            [
                ("g_g_ENJUSDT_x0_1775943976_0", "exit-g_g_ENJUSDT_e5_1775943791_0"),
                ("g_g_ENJUSDT_x1_1775943988_0", "exit-g_g_ENJUSDT_e6_1775943792_0"),
            ]
        )
        engine._grid_v2_bridge = bridge

        acct = _make_snapshot(
            [
                _make_order_snap(cid="g_g_ENJUSDT_x0_1775943976_0"),
                _make_order_snap(cid="g_g_ENJUSDT_x1_1775943988_0"),
            ]
        )

        assert engine._count_grid_exits("ENJUSDT", acct) == 2

    def test_does_not_count_foreign_reduce_only_orders(self) -> None:
        """Manual/foreign reduceOnly orders (not in registry) must NOT
        be counted as grid-owned."""
        engine = _make_engine()
        bridge = MagicMock()
        bridge.adapter.registry = _make_registry_with_exits(
            [("g_g_ENJUSDT_x0_1775943976_0", "exit-g_g_ENJUSDT_e5_1775943791_0")]
        )
        engine._grid_v2_bridge = bridge

        acct = _make_snapshot(
            [
                _make_order_snap(cid="g_g_ENJUSDT_x0_1775943976_0"),  # ours
                _make_order_snap(cid="manual-user-exit-999"),  # foreign
            ]
        )

        assert engine._count_grid_exits("ENJUSDT", acct) == 1

    def test_regression_aiot_canary_scenario(self) -> None:
        """Direct reproduction of the 2026-04-11 AIOT canary deadlock.

        Pre-fix: 10 grid exits on exchange, registry has the 10 CIDs,
        but ``_get_grid_exit_order_ids`` returned ``exit-...`` strings
        → intersection empty → ``_count_grid_exits`` returned 0 →
        FORCE_REDUCE_EXIT_ORDERS_CLEARED falsely logged → unload stuck.

        Post-fix: returns 10 because CIDs match.
        """
        engine = _make_engine()
        cids = [f"g_g_AIOTUSDT_x{i}_177594{i:04d}_0" for i in range(10)]
        bridge = MagicMock()
        bridge.adapter.registry = _make_registry_with_exits(
            [(cid, f"exit-g_g_AIOTUSDT_e{i}_stateOnly") for i, cid in enumerate(cids)]
        )
        engine._grid_v2_bridge = bridge

        acct = _make_snapshot([_make_order_snap(cid=cid, symbol="AIOTUSDT") for cid in cids])

        # The whole deadlock was "engine thinks 0 grid exits exist"
        assert engine._count_grid_exits("AIOTUSDT", acct) == 10, (
            "AIOT canary regression: _count_grid_exits must identify all "
            "10 grid-owned exits via CID match. Pre-fix this returned 0 "
            "because the set used state-machine exit_order_id strings."
        )

    def test_wrong_symbol_not_counted(self) -> None:
        """Orders for a different symbol must not be counted even if
        their CIDs are in the registry."""
        engine = _make_engine()
        bridge = MagicMock()
        bridge.adapter.registry = _make_registry_with_exits(
            [("g_g_BTCUSDT_x0_1775943976_0", "exit-g_g_BTCUSDT_e5_..")]
        )
        engine._grid_v2_bridge = bridge

        acct = _make_snapshot(
            [_make_order_snap(cid="g_g_BTCUSDT_x0_1775943976_0", symbol="BTCUSDT")]
        )

        assert engine._count_grid_exits("ENJUSDT", acct) == 0
        assert engine._count_grid_exits("BTCUSDT", acct) == 1

    def test_non_reduce_only_not_counted(self) -> None:
        """Non-reduceOnly orders must not be counted even if CID matches
        (these are entry orders, not exits)."""
        engine = _make_engine()
        bridge = MagicMock()
        bridge.adapter.registry = _make_registry_with_exits(
            [("g_g_ENJUSDT_x0_1775943976_0", "exit-...")]
        )
        engine._grid_v2_bridge = bridge

        acct = _make_snapshot(
            [_make_order_snap(cid="g_g_ENJUSDT_x0_1775943976_0", reduce_only=False)]
        )

        assert engine._count_grid_exits("ENJUSDT", acct) == 0

    def test_fully_filled_order_not_counted(self) -> None:
        """Orders with ``filled_qty == qty`` are fully executed and
        must not be counted as 'still occupying budget'."""
        engine = _make_engine()
        bridge = MagicMock()
        bridge.adapter.registry = _make_registry_with_exits(
            [("g_g_ENJUSDT_x0_1775943976_0", "exit-...")]
        )
        engine._grid_v2_bridge = bridge

        filled_order = OpenOrderSnap(
            order_id="g_g_ENJUSDT_x0_1775943976_0",
            symbol="ENJUSDT",
            side="BUY",
            order_type="LIMIT",
            price=Decimal("0.0312"),
            qty=Decimal("273"),
            filled_qty=Decimal("273"),  # fully filled
            reduce_only=True,
            status="FILLED",
            ts=1000,
        )
        acct = _make_snapshot([filled_order])

        assert engine._count_grid_exits("ENJUSDT", acct) == 0


class TestCancelGridExitsForForceReduce:
    """``_cancel_grid_exits_for_force_reduce`` must dispatch CANCEL
    actions for grid-owned exits matched by CID."""

    def test_cancels_matched_grid_exits(self) -> None:
        engine = _make_engine()
        bridge = MagicMock()
        bridge.adapter.registry = _make_registry_with_exits(
            [
                ("g_g_ENJUSDT_x0_1775943976_0", "exit-..."),
                ("g_g_ENJUSDT_x1_1775943988_0", "exit-..."),
            ]
        )
        engine._grid_v2_bridge = bridge

        acct = _make_snapshot(
            [
                _make_order_snap(cid="g_g_ENJUSDT_x0_1775943976_0"),
                _make_order_snap(cid="g_g_ENJUSDT_x1_1775943988_0"),
            ]
        )

        dispatched_cancel_ids: list[str] = []

        def _capture(action: ExecutionAction, _ts: int) -> LiveAction:
            if action.action_type == ActionType.CANCEL and action.order_id is not None:
                dispatched_cancel_ids.append(action.order_id)
            result = MagicMock()
            result.status = LiveActionStatus.EXECUTED
            return result

        # Patch _process_action to capture cancels
        setattr(engine, "_process_action", _capture)  # noqa: B010

        count = engine._cancel_grid_exits_for_force_reduce("ENJUSDT", acct)

        assert count == 2
        assert set(dispatched_cancel_ids) == {
            "g_g_ENJUSDT_x0_1775943976_0",
            "g_g_ENJUSDT_x1_1775943988_0",
        }

    def test_does_not_cancel_foreign_reduce_only(self) -> None:
        """Foreign/manual reduceOnly orders must not be canceled."""
        engine = _make_engine()
        bridge = MagicMock()
        bridge.adapter.registry = _make_registry_with_exits(
            [("g_g_ENJUSDT_x0_1775943976_0", "exit-...")]
        )
        engine._grid_v2_bridge = bridge

        acct = _make_snapshot(
            [
                _make_order_snap(cid="g_g_ENJUSDT_x0_1775943976_0"),
                _make_order_snap(cid="manual-user-999"),  # foreign
            ]
        )

        dispatched_cancel_ids: list[str] = []

        def _capture(action: ExecutionAction, _ts: int) -> LiveAction:
            if action.action_type == ActionType.CANCEL and action.order_id is not None:
                dispatched_cancel_ids.append(action.order_id)
            result = MagicMock()
            result.status = LiveActionStatus.EXECUTED
            return result

        setattr(engine, "_process_action", _capture)  # noqa: B010

        count = engine._cancel_grid_exits_for_force_reduce("ENJUSDT", acct)

        assert count == 1
        assert dispatched_cancel_ids == ["g_g_ENJUSDT_x0_1775943976_0"]
        assert "manual-user-999" not in dispatched_cancel_ids

    def test_regression_no_cancels_under_old_exit_order_id_matching(self) -> None:
        """Falsifiability guard: this test LOCKS the post-fix behavior.

        If a future refactor reverts to matching by ``reg.exit_order_id``
        instead of ``reg.cid``, ``_get_grid_exit_order_ids`` would return
        the ``"exit-..."`` strings. Then ``order.order_id in grid_exit_ids``
        would always be False (because snapshot holds real CIDs), and
        no cancels would be dispatched. This test would fail with
        ``count == 0`` under the broken behavior.
        """
        engine = _make_engine()
        bridge = MagicMock()
        # Deliberately use VERY DIFFERENT strings for cid vs exit_order_id
        # so the test can distinguish which field the matching actually
        # uses.
        bridge.adapter.registry = _make_registry_with_exits(
            [
                (
                    "g_g_AIOTUSDT_x0_1775944919_0",
                    "TOTALLY-DIFFERENT-STATE-MACHINE-STRING",
                ),
            ]
        )
        engine._grid_v2_bridge = bridge

        acct = _make_snapshot(
            [_make_order_snap(cid="g_g_AIOTUSDT_x0_1775944919_0", symbol="AIOTUSDT")]
        )

        dispatched: list[str] = []

        def _capture(action: ExecutionAction, _ts: int) -> LiveAction:
            if action.action_type == ActionType.CANCEL and action.order_id is not None:
                dispatched.append(action.order_id)
            result = MagicMock()
            result.status = LiveActionStatus.EXECUTED
            return result

        setattr(engine, "_process_action", _capture)  # noqa: B010

        count = engine._cancel_grid_exits_for_force_reduce("AIOTUSDT", acct)

        # Post-fix: match by reg.cid → count == 1
        # If someone regresses to reg.exit_order_id → count would be 0
        # because "TOTALLY-DIFFERENT-STATE-MACHINE-STRING" never appears
        # in order.order_id (which is the real CID from the exchange).
        assert count == 1, (
            "ID matching regression: _cancel_grid_exits_for_force_reduce "
            "must use adapter.registry.all_exit_cids (real clientOrderIds), "
            "NOT ExitRegistration.exit_order_id (state-machine internal "
            "strings). See post-#675 AIOT canary deadlock."
        )
        assert dispatched == ["g_g_AIOTUSDT_x0_1775944919_0"]
