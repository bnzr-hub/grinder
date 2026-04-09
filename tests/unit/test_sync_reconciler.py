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
from grinder.core import OrderSide
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


# --- Tests: Inflight-aware exit reconciliation ---


class TestInflightExitReconciliation:
    """Exit diff must account for pending place/cancel to avoid false missing/extra."""

    def test_pending_place_suppresses_false_missing_exit(self) -> None:
        """Exit dispatched but not visible → not counted as missing."""
        bridge = _make_bridge(
            exit_orders=(_make_exit_order("eo1"),),
        )
        # Exit not on exchange yet
        snap = _make_snapshot(exit_cids=[])
        # But pending place has its CID
        r = reconcile_grid_state(
            snap,
            "BTCUSDT",
            bridge,
            pending_exit_place_cids=frozenset({"exit_eo1"}),
        )
        assert r.missing_exits == 0

    def test_pending_cancel_suppresses_false_extra_exit(self) -> None:
        """Exit cancel sent but still visible → not counted as extra."""
        bridge = _make_bridge(exit_orders=())
        # Exit still on exchange but cancel is pending
        snap = _make_snapshot(exit_cids=["exit_stale"])
        r = reconcile_grid_state(
            snap,
            "BTCUSDT",
            bridge,
            pending_exit_cancel_cids=frozenset({"exit_stale"}),
        )
        assert r.extra_exits == 0

    def test_true_missing_exit_still_detected(self) -> None:
        """No snapshot exit, no pending place → genuinely missing."""
        bridge = _make_bridge(
            exit_orders=(_make_exit_order("eo1"),),
        )
        snap = _make_snapshot(exit_cids=[])
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.missing_exits == 1

    def test_true_extra_exit_still_detected(self) -> None:
        """Snapshot has exit, no desired lot → genuinely extra."""
        bridge = _make_bridge(exit_orders=())
        snap = _make_snapshot(exit_cids=["exit_orphan"])
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.extra_exits == 1

    def test_backwards_compatible_without_pending(self) -> None:
        """Without pending_exit args, behavior is unchanged."""
        bridge = _make_bridge(
            exit_orders=(_make_exit_order("eo1"),),
        )
        snap = _make_snapshot(exit_cids=[])
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        # No pending args → old behavior: missing = 1
        assert r.missing_exits == 1

    def test_unfiltered_entry_cid_contaminates_exit_diff(self) -> None:
        """Caller MUST filter pending CIDs by EXIT kind before passing.

        If an entry CID leaks into pending_exit_place_cids, the reconciler
        will misclassify it as an extra exit. This test documents the
        contract: filtering is the caller's responsibility (engine.py).
        """
        bridge = _make_bridge(
            exit_orders=(_make_exit_order("eo1"),),
        )
        snap = _make_snapshot(exit_cids=[])
        # Unfiltered entry CID passed as pending exit → contaminates diff
        r = reconcile_grid_state(
            snap,
            "BTCUSDT",
            bridge,
            pending_exit_place_cids=frozenset({"entry_e0"}),
        )
        # entry CID misclassified as extra exit — proves caller must filter
        assert r.extra_exits == 1

    def test_filtered_exit_only_cids_clean(self) -> None:
        """With properly filtered exit-only CIDs, no contamination."""
        bridge = _make_bridge(
            exit_orders=(_make_exit_order("eo1"),),
        )
        snap = _make_snapshot(exit_cids=[])
        # Only exit CID in pending → correct behavior
        r = reconcile_grid_state(
            snap,
            "BTCUSDT",
            bridge,
            pending_exit_place_cids=frozenset({"exit_eo1"}),
        )
        assert r.missing_exits == 0
        assert r.extra_exits == 0


# --- Tests: Inflight-aware entry reconciliation ---


class TestInflightEntryReconciliation:
    """Entry diff must account for pending place/cancel to avoid false missing/extra."""

    def test_pending_entry_place_suppresses_false_missing(self) -> None:
        """Entry dispatched but not visible → not counted as missing."""
        from grinder.core import OrderSide  # noqa: PLC0415

        bridge = _make_bridge(
            buy_prices=(Decimal("99.00"),),
            sell_prices=(Decimal("101.00"),),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=1,
        )
        # No entries on exchange
        snap = _make_snapshot()
        # But pending place has BUY@99
        r = reconcile_grid_state(
            snap,
            "BTCUSDT",
            bridge,
            pending_entry_place_keys=frozenset({(OrderSide.BUY, Decimal("99.00"))}),
        )
        # BUY@99 should not be missing (it's pending)
        # SELL@101 is still missing
        assert r.missing_entries == 1  # only SELL@101

    def test_pending_entry_cancel_suppresses_false_extra(self) -> None:
        """Entry cancel sent but still visible → not counted as extra."""
        from grinder.core import OrderSide  # noqa: PLC0415

        bridge = _make_bridge(
            buy_prices=(),
            sell_prices=(),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=0,
        )
        # Old entry still on exchange
        snap = _make_snapshot(
            entry_orders={("BUY", "99.00"): "entry_old"},
        )
        # Cancel pending for it
        r = reconcile_grid_state(
            snap,
            "BTCUSDT",
            bridge,
            pending_entry_cancel_keys=frozenset({(OrderSide.BUY, Decimal("99.00"))}),
        )
        assert r.extra_entries == 0

    def test_true_missing_entry_still_detected(self) -> None:
        """No snapshot entry, no pending → genuinely missing."""
        bridge = _make_bridge(
            buy_prices=(Decimal("99.00"),),
            sell_prices=(),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=1,
        )
        snap = _make_snapshot()
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.missing_entries == 1

    def test_true_extra_entry_still_detected(self) -> None:
        """Snapshot has entry, not desired → genuinely extra."""
        bridge = _make_bridge(
            buy_prices=(),
            sell_prices=(),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=0,
        )
        snap = _make_snapshot(
            entry_orders={("BUY", "99.00"): "entry_stale"},
        )
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.extra_entries == 1

    def test_pending_exit_does_not_contaminate_entry_diff(self) -> None:
        """Pending exit CID must not affect entry missing/extra."""
        bridge = _make_bridge(
            buy_prices=(Decimal("99.00"),),
            sell_prices=(),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=1,
        )
        snap = _make_snapshot()
        # Pending exit should not suppress entry missing
        r = reconcile_grid_state(
            snap,
            "BTCUSDT",
            bridge,
            pending_exit_place_cids=frozenset({"exit_x0"}),
        )
        assert r.missing_entries == 1  # entry still missing

    def test_backwards_compatible_without_pending_entry(self) -> None:
        """Without pending_entry args, old behavior preserved."""
        bridge = _make_bridge(
            buy_prices=(Decimal("99.00"),),
            sell_prices=(),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=1,
        )
        snap = _make_snapshot()
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.missing_entries == 1


# --- Tests: Inventory headroom burst protection ---


class TestInventoryHeadroom:
    """Near-cap same-side entries are reduced to limit burst overshoot."""

    def test_at_cap_zero_desired_entries(self) -> None:
        """At max inventory, no desired entries (existing behavior)."""
        lots = tuple(MagicMock() for _ in range(15))
        bridge = _make_bridge(
            mode=BranchMode.LONG_BRANCH,
            buy_prices=(Decimal("99.00"), Decimal("98.00"), Decimal("97.00")),
            sell_prices=(),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=3,
            max_inv=15,
            open_lots=lots,
        )
        snap = _make_snapshot()
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.theoretical_desired_entry_count == 0

    def test_near_cap_reduces_same_side_entries(self) -> None:
        """At 14/15 lots, only 1 same-side BUY entry desired (headroom=1)."""
        lots = tuple(MagicMock() for _ in range(14))
        bridge = _make_bridge(
            mode=BranchMode.LONG_BRANCH,
            buy_prices=(Decimal("99.00"), Decimal("98.00"), Decimal("97.00")),
            sell_prices=(),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=3,
            max_inv=15,
            open_lots=lots,
        )
        snap = _make_snapshot()
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        # Headroom = 1 → only 1 BUY entry desired (closest to ref)
        assert r.theoretical_desired_entry_count == 1

    def test_far_from_cap_full_entries(self) -> None:
        """At 5/15 lots, full entry_levels_per_side desired."""
        lots = tuple(MagicMock() for _ in range(5))
        bridge = _make_bridge(
            mode=BranchMode.LONG_BRANCH,
            buy_prices=(Decimal("99.00"), Decimal("98.00"), Decimal("97.00")),
            sell_prices=(),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=3,
            max_inv=15,
            open_lots=lots,
        )
        snap = _make_snapshot()
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.theoretical_desired_entry_count == 3

    def test_flat_mode_not_affected(self) -> None:
        """FLAT mode ignores headroom (batch seed/reseed must work)."""
        lots = tuple(MagicMock() for _ in range(14))
        bridge = _make_bridge(
            mode=BranchMode.FLAT,
            buy_prices=(Decimal("99.00"), Decimal("98.00")),
            sell_prices=(Decimal("101.00"), Decimal("102.00")),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=2,
            max_inv=15,
            open_lots=lots,
        )
        snap = _make_snapshot()
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.theoretical_desired_entry_count == 4


class TestPriceAwareEntryReconciliation:
    """Off-grid entries should use fuzzy/geometry-aware matching in reconciler."""

    def test_fuzzy_valid_entry_is_kept(self) -> None:
        """Entry within one tick of desired price is treated as valid."""
        bridge = _make_bridge(
            buy_prices=(Decimal("99.00"),),
            sell_prices=(),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=1,
        )
        snap = _make_snapshot(
            entry_orders={("BUY", "99.01"): "entry_buy_fuzzy"},
        )
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.missing_entries == 0
        assert r.extra_entries == 0
        assert tuple() == r.actions

    def test_off_grid_entry_becomes_cancel_plus_replace(self) -> None:
        """Entry far from desired price is corrected, not treated as structural drift."""
        bridge = _make_bridge(
            buy_prices=(Decimal("99.00"),),
            sell_prices=(),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=1,
        )
        snap = _make_snapshot(
            entry_orders={("BUY", "98.00"): "entry_buy_wrong"},
        )
        r = reconcile_grid_state(snap, "BTCUSDT", bridge)
        assert r.missing_entries == 0
        assert r.extra_entries == 0
        assert len(r.actions) == 2
        assert r.actions[0].action_type == ActionType.CANCEL
        assert r.actions[0].order_id == "entry_buy_wrong"
        assert r.actions[0].reason == "grid_v2_RECONCILE_REPRICE_ENTRY_CANCEL"
        assert r.actions[1].action_type == ActionType.PLACE
        assert r.actions[1].side == OrderSide.BUY
        assert r.actions[1].price == Decimal("99.00")
        assert r.actions[1].reason == "grid_v2_RECONCILE_REPRICE_ENTRY_PLACE"

    def test_pending_place_suppresses_reprice_for_expected_slot(self) -> None:
        """Pending expected-slot place suppresses duplicate geometry correction."""
        bridge = _make_bridge(
            buy_prices=(Decimal("99.00"),),
            sell_prices=(),
            ref_price=Decimal("100"),
            step_pct=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            levels=1,
        )
        snap = _make_snapshot(
            entry_orders={("BUY", "98.00"): "entry_buy_wrong"},
        )
        r = reconcile_grid_state(
            snap,
            "BTCUSDT",
            bridge,
            pending_entry_place_keys=frozenset({(OrderSide.BUY, Decimal("99.00"))}),
        )
        assert r.missing_entries == 0
        assert r.extra_entries == 0
        assert tuple() == r.actions
