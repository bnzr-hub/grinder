"""Tests for grid_v2 sync-driven reconciler (ADR-096).

Tests A-H from the PR brief:
A) Deterministic diff — same inputs → identical output.
B) Cancel-before-place ordering.
C) Shadow invariance — no dispatch change.
D) Empty/no-mismatch scenario.
E) Inventory full → no desired entries.
F) Budget cap respected.
G) Branch-mode exit reconciliation.
H) Deterministic across repeated calls.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
from grinder.execution.types import ActionType
from grinder.grid_v2.state import BranchMode, ExitOrderStatus
from grinder.grid_v2.sync_reconciler import reconcile_grid_state


def _make_bridge(
    *,
    mode: BranchMode = BranchMode.FLAT,
    buy_prices: tuple[Decimal, ...] = (),
    sell_prices: tuple[Decimal, ...] = (),
    exit_orders: tuple[object, ...] = (),
    open_lots: tuple[object, ...] = (),
    max_inv: int = 80,
    order_size: Decimal = Decimal("100"),
    tick_size: Decimal = Decimal("0.0001"),
    ref_price: Decimal = Decimal("50000"),
    step_pct: Decimal = Decimal("0.01"),
    levels: int = 3,
) -> MagicMock:
    """Build a mock GridV2Bridge with desired SM state."""
    bridge = MagicMock()
    sm = MagicMock()
    sm.mode = mode
    sm.snapshot.entry_window.buy_entry_prices = buy_prices
    sm.snapshot.entry_window.sell_entry_prices = sell_prices
    sm.snapshot.entry_window.reference_price = ref_price
    sm.snapshot.exit_orders = exit_orders
    sm.snapshot.open_lots = open_lots
    bridge.state_machine = sm
    bridge.reconstruction_ok = True
    bridge._config.max_inventory_levels = max_inv
    bridge._config.order_size = order_size
    bridge._config.price_tick_size = tick_size
    bridge._config.grid_step_pct = step_pct
    bridge._config.entry_levels_per_side = levels
    bridge._quantize_price = lambda p, _s: p

    # Adapter: parse_cid returns mock with kind
    def _parse(cid: str) -> MagicMock | None:
        if cid.startswith("entry_"):
            m = MagicMock()
            m.kind.value = "ENTRY"
            return m
        if cid.startswith("exit_"):
            m = MagicMock()
            m.kind.value = "EXIT"
            return m
        return None

    bridge.adapter.parse_cid = _parse
    bridge.adapter.registry.cid_for_exit = lambda eid: f"exit_{eid}"
    bridge.adapter.registry.cid_for_entry = lambda _side, _price: None  # no existing CIDs
    # Make isinstance check work
    bridge.__class__ = type("GridV2Bridge", (), {})
    return bridge


def _make_snapshot(
    *,
    symbol: str = "BTCUSDT",
    entry_orders: dict[tuple[str, str], str] | None = None,
    exit_cids: list[str] | None = None,
) -> AccountSnapshot:
    """Build AccountSnapshot with open orders.

    entry_orders: {(side_str, price_str): cid, ...}
    exit_cids: [cid, ...]
    """
    orders: list[OpenOrderSnap] = []
    if entry_orders:
        for (side, price), cid in entry_orders.items():
            orders.append(
                OpenOrderSnap(
                    order_id=cid,
                    symbol=symbol,
                    side=side,
                    order_type="LIMIT",
                    price=Decimal(price),
                    qty=Decimal("100"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000,
                )
            )
    if exit_cids:
        for cid in exit_cids:
            orders.append(
                OpenOrderSnap(
                    order_id=cid,
                    symbol=symbol,
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50000"),
                    qty=Decimal("100"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000,
                )
            )
    return AccountSnapshot(
        positions=(),
        open_orders=tuple(orders),
        ts=1000,
        source="test",
    )


def _make_exit_order(exit_order_id: str, status: str = "OPEN") -> MagicMock:
    eo = MagicMock()
    eo.exit_order_id = exit_order_id
    eo.status = ExitOrderStatus(status)
    return eo


class TestReconcilerDeterministicDiff:
    """Test A: same inputs → identical output."""

    def test_identical_results_on_repeated_calls(self) -> None:
        """Same inputs → identical output every time."""
        bridge = _make_bridge(
            buy_prices=(Decimal("99.00"), Decimal("98.00")),
            sell_prices=(Decimal("101.00"), Decimal("102.00")),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=2,
        )
        snap = _make_snapshot(
            entry_orders={("BUY", "99.00"): "entry_buy1"},
        )
        # Missing: BUY@98, SELL@101, SELL@102. Extra: none.
        results = []
        for _ in range(3):
            r = reconcile_grid_state(snap, "BTCUSDT", bridge)
            results.append(r)
        for i in range(1, 3):
            assert results[i].actions == results[0].actions
            assert results[i].missing_entries == results[0].missing_entries

    def test_no_mismatch_returns_empty(self) -> None:
        """All SM-desired entries present on exchange → 0 missing."""
        bridge = _make_bridge(
            buy_prices=(Decimal("99.00"), Decimal("98.00")),
            sell_prices=(Decimal("101.00"), Decimal("102.00")),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=2,
        )
        snap = _make_snapshot(
            entry_orders={
                ("BUY", "99.00"): "entry_b1",
                ("BUY", "98.00"): "entry_b2",
                ("SELL", "101.00"): "entry_s1",
                ("SELL", "102.00"): "entry_s2",
            },
        )
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.missing_entries == 0
        assert r.extra_entries == 0
        assert len(r.actions) == 0


class TestCancelBeforePlaceOrdering:
    """Test B: extras and missing mixed → all CANCEL first, then PLACE."""

    def test_cancel_before_place(self) -> None:
        """Extra entry cancelled before missing placed."""
        bridge = _make_bridge(
            buy_prices=(Decimal("99.00"),),
            sell_prices=(Decimal("101.00"),),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=1,
        )
        # Actual: SELL@105 (extra, not at geometry level), missing BUY@99 + SELL@101
        snap = _make_snapshot(
            entry_orders={("SELL", "105.00"): "entry_extra"},
        )
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        # Should have at least 1 CANCEL + some PLACEs
        cancels = [a for a in r.actions if a.action_type == ActionType.CANCEL]
        places = [a for a in r.actions if a.action_type == ActionType.PLACE]
        assert len(cancels) >= 1
        assert len(places) >= 1
        # CANCEL before PLACE
        first_place_idx = next(
            i for i, a in enumerate(r.actions) if a.action_type == ActionType.PLACE
        )
        last_cancel_idx = max(
            i for i, a in enumerate(r.actions) if a.action_type == ActionType.CANCEL
        )
        assert last_cancel_idx < first_place_idx


class TestShadowInvariance:
    """Test C: reconciler output is pure — no side effects on bridge/SM."""

    def test_bridge_state_unchanged(self) -> None:
        bridge = _make_bridge(
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=2,
        )
        snap = _make_snapshot(
            entry_orders={("BUY", "48.00"): "entry_wrong"},
        )
        sm_before = bridge.state_machine.mode
        _ = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert bridge.state_machine.mode == sm_before
        # No dispatch/mutation methods called on bridge
        bridge.on_fill.assert_not_called()
        bridge.on_cancel_ack.assert_not_called()


class TestInventoryFull:
    """Test E: inventory cap → no desired entries."""

    def test_inventory_full_no_place_entries(self) -> None:
        lots = [MagicMock() for _ in range(80)]
        bridge = _make_bridge(
            mode=BranchMode.LONG_BRANCH,
            open_lots=tuple(lots),
            max_inv=80,
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=3,
        )
        snap = _make_snapshot()  # no orders on exchange
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.desired_entry_count == 0
        assert r.missing_entries == 0
        # No PLACE actions
        assert all(a.action_type != ActionType.PLACE for a in r.actions)


class TestBudgetCap:
    """Test F: budget cap respected."""

    def test_max_actions_limits_output(self) -> None:
        """Budget cap: max_actions=3 limits output to 3."""
        bridge = _make_bridge(
            buy_prices=tuple(Decimal(str(100 - i)) for i in range(1, 6)),
            sell_prices=tuple(Decimal(str(100 + i)) for i in range(1, 6)),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=5,
        )
        snap = _make_snapshot()
        r = reconcile_grid_state(snap, "BTCUSDT", bridge, max_actions=3)
        assert len(r.actions) == 3


class TestExitReconciliation:
    """Test G: branch-mode exit reconciliation."""

    def test_missing_exit_not_placed_extra_exit_cancelled(self) -> None:
        """Reconciler cancels extra exits but doesn't PLACE exits."""
        eo = _make_exit_order("lot_1")
        bridge = _make_bridge(
            mode=BranchMode.LONG_BRANCH,
            exit_orders=(eo,),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=2,
        )
        # Extra exit on exchange that shouldn't be there
        snap = _make_snapshot(exit_cids=["exit_unknown"])
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        cancel_actions = [a for a in r.actions if a.action_type == ActionType.CANCEL]
        assert any(a.order_id == "exit_unknown" for a in cancel_actions)


class TestReplayDeterminism:
    """Test H: deterministic across repeated calls with same input."""

    def test_action_order_stable(self) -> None:
        """Deterministic: same input → same actions, CANCEL before PLACE."""
        # ref=100, levels=3 → desired BUY@99,98,97 SELL@101,102,103
        bridge = _make_bridge(
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=3,
        )
        snap = _make_snapshot(
            entry_orders={
                ("BUY", "47.00"): "entry_extra_buy",
                ("SELL", "120.00"): "entry_extra_sell",
            },
        )
        r1 = reconcile_grid_state(snap, "BTCUSDT", bridge)
        r2 = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r1.actions == r2.actions
        first_place_idx = next(
            (i for i, a in enumerate(r1.actions) if a.action_type == ActionType.PLACE),
            len(r1.actions),
        )
        last_cancel_idx = max(
            (i for i, a in enumerate(r1.actions) if a.action_type == ActionType.CANCEL),
            default=-1,
        )
        assert last_cancel_idx < first_place_idx


class TestDuplicateEntrySkip:
    """P0 fix: reconciler skips PLACE when adapter registry already has CID for slot."""

    def test_skip_place_when_registry_has_cid(self) -> None:
        """Skip PLACE if registry already has CID for that slot."""
        # ref=100, levels=1 → desired BUY@99, SELL@101
        bridge = _make_bridge(
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=1,
        )
        # Registry already has CIDs for both slots
        bridge.adapter.registry.cid_for_entry = lambda _side, _price: "existing_cid"
        snap = _make_snapshot()  # no orders on exchange
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        place_actions = [a for a in r.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 0

    def test_place_when_registry_empty(self) -> None:
        """Place when registry has no CID for slot."""
        bridge = _make_bridge(
            buy_prices=(Decimal("99.00"),),
            sell_prices=(Decimal("101.00"),),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=1,
        )
        bridge.adapter.registry.cid_for_entry = lambda _side, _price: None
        snap = _make_snapshot()
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        place_actions = [a for a in r.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 2  # BUY@99 + SELL@101
