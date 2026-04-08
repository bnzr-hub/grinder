"""Tests for Exit Topology Repair (PR-6, ADR-105).

Adversarial tests for deterministic exit topology convergence:
1. Healthy topology → no actions
2. Extra exits → deterministic cancels
3. Missing exits → deterministic restores
4. Mixed drift → cancel + place in stable order
5. Budget-constrained topology → legal target only
6. Post -2022 repair latch → converges to target
7. Partial fill topology recompute
8. Deferred placement path is explicit
9. Symbol/direction isolation
10. Observability contract for repair result fields
"""

from __future__ import annotations

from decimal import Decimal

from grinder.core import OrderSide
from grinder.grid_v2.exit_repair import (
    DesiredExit,
    RepairTrigger,
    compute_desired_exits,
    compute_exit_topology_repair,
)
from grinder.grid_v2.state import ExitOrder, ExitOrderStatus


def _exit(
    eid: str = "exit-1",
    lid: str = "lot-1",
    side: OrderSide = OrderSide.SELL,
    price: str = "50250",
    qty: str = "0.001",
    status: ExitOrderStatus = ExitOrderStatus.OPEN,
) -> ExitOrder:
    return ExitOrder(
        exit_order_id=eid,
        lot_id=lid,
        side=side,
        price=Decimal(price),
        qty=Decimal(qty),
        status=status,
    )


def _desired(
    eid: str = "exit-1",
    lid: str = "lot-1",
    side: OrderSide = OrderSide.SELL,
    price: str = "50250",
    qty: str = "0.001",
    cid: str | None = "g-X-1",
) -> DesiredExit:
    return DesiredExit(
        exit_order_id=eid,
        lot_id=lid,
        side=side,
        price=Decimal(price),
        qty=Decimal(qty),
        registry_cid=cid,
    )


class TestComputeDesiredExits:
    """compute_desired_exits from SM state."""

    def test_open_exits_included(self) -> None:
        exits = [_exit("exit-1"), _exit("exit-2", lid="lot-2")]
        registry = {"exit-1": "g-X-1", "exit-2": "g-X-2"}
        desired = compute_desired_exits(exits, registry.get)
        assert len(desired) == 2

    def test_filled_exits_excluded(self) -> None:
        exits = [
            _exit("exit-1", status=ExitOrderStatus.OPEN),
            _exit("exit-2", lid="lot-2", status=ExitOrderStatus.FILLED),
        ]
        registry = {"exit-1": "g-X-1", "exit-2": "g-X-2"}
        desired = compute_desired_exits(exits, registry.get)
        assert len(desired) == 1
        assert desired[0].exit_order_id == "exit-1"

    def test_budget_constrains_desired(self) -> None:
        """Budget=0.002, two exits of 0.001 each → both fit."""
        exits = [
            _exit("exit-1", qty="0.001"),
            _exit("exit-2", lid="lot-2", qty="0.001"),
        ]
        registry = {"exit-1": "g-X-1", "exit-2": "g-X-2"}
        desired = compute_desired_exits(exits, registry.get, Decimal("0.002"))
        assert len(desired) == 2

    def test_budget_truncates_excess(self) -> None:
        """Budget=0.001, two exits of 0.001 → only first fits."""
        exits = [
            _exit("exit-1", qty="0.001"),
            _exit("exit-2", lid="lot-2", qty="0.001"),
        ]
        registry = {"exit-1": "g-X-1", "exit-2": "g-X-2"}
        desired = compute_desired_exits(exits, registry.get, Decimal("0.001"))
        assert len(desired) == 1

    def test_unregistered_exit_has_none_cid(self) -> None:
        exits = [_exit("exit-1")]
        desired = compute_desired_exits(exits, lambda _eid: None)
        assert len(desired) == 1
        assert desired[0].registry_cid is None


class TestHealthyTopology:
    """Actual matches desired → no repair actions."""

    def test_no_actions_when_converged(self) -> None:
        desired = [_desired("exit-1", cid="g-X-1"), _desired("exit-2", lid="lot-2", cid="g-X-2")]
        actual = {"g-X-1", "g-X-2"}
        result = compute_exit_topology_repair(desired, actual)
        assert result.is_converged
        assert len(result.actions) == 0
        assert result.extra_count == 0
        assert result.missing_count == 0
        assert result.deferred_count == 0


class TestExtraExits:
    """Extra exits on exchange → deterministic cancels."""

    def test_extra_exits_cancelled(self) -> None:
        desired = [_desired("exit-1", cid="g-X-1")]
        actual = {"g-X-1", "g-X-2", "g-X-3"}
        result = compute_exit_topology_repair(desired, actual)
        assert result.extra_count == 2
        cancel_actions = [a for a in result.actions if a.action_type == "CANCEL"]
        assert len(cancel_actions) == 2
        # Sorted by CID
        assert cancel_actions[0].cid == "g-X-2"
        assert cancel_actions[1].cid == "g-X-3"


class TestMissingExits:
    """Missing exits → deterministic restores."""

    def test_missing_exits_placed(self) -> None:
        desired = [
            _desired("exit-1", cid="g-X-1"),
            _desired("exit-2", lid="lot-2", cid="g-X-2"),
        ]
        actual = {"g-X-1"}  # g-X-2 missing
        result = compute_exit_topology_repair(desired, actual)
        assert result.missing_count == 1
        place_actions = [a for a in result.actions if a.action_type == "PLACE"]
        assert len(place_actions) == 1
        assert place_actions[0].exit_order_id == "exit-2"
        assert place_actions[0].cid == "g-X-2"


class TestMixedDrift:
    """Cancel + place in stable deterministic order."""

    def test_cancel_before_place(self) -> None:
        desired = [_desired("exit-1", cid="g-X-1")]
        actual = {"g-X-2"}  # X-1 missing, X-2 extra
        result = compute_exit_topology_repair(desired, actual)
        assert result.extra_count == 1
        assert result.missing_count == 1
        # Cancel first, then place
        assert result.actions[0].action_type == "CANCEL"
        assert result.actions[0].cid == "g-X-2"
        assert result.actions[1].action_type == "PLACE"
        assert result.actions[1].exit_order_id == "exit-1"


class TestBudgetConstrainedTopology:
    """Budget limits desired exits → legal target only."""

    def test_budget_zero_desires_nothing(self) -> None:
        exits = [_exit("exit-1", qty="0.001")]
        desired = compute_desired_exits(exits, lambda _: "g-X-1", Decimal("0"))
        assert len(desired) == 0

    def test_budget_partial_keeps_first(self) -> None:
        exits = [
            _exit("exit-1", qty="0.001"),
            _exit("exit-2", lid="lot-2", qty="0.001"),
            _exit("exit-3", lid="lot-3", qty="0.001"),
        ]
        registry = {"exit-1": "g-X-1", "exit-2": "g-X-2", "exit-3": "g-X-3"}
        desired = compute_desired_exits(exits, registry.get, Decimal("0.002"))
        assert len(desired) == 2
        assert desired[0].exit_order_id == "exit-1"
        assert desired[1].exit_order_id == "exit-2"


class TestDeferredPlacement:
    """Exits not yet registered → DEFERRED (explicit, not silent)."""

    def test_unregistered_exit_deferred(self) -> None:
        desired = [_desired("exit-1", cid=None)]
        actual: set[str] = set()
        result = compute_exit_topology_repair(desired, actual)
        assert result.deferred_count == 1
        assert not result.is_converged
        deferred = [a for a in result.actions if a.action_type == "DEFERRED"]
        assert len(deferred) == 1
        assert deferred[0].exit_order_id == "exit-1"


class TestObservabilityContract:
    """ExitTopologyResult has all expected fields."""

    def test_result_fields(self) -> None:
        desired = [_desired("exit-1", cid="g-X-1")]
        result = compute_exit_topology_repair(desired, set(), RepairTrigger.REJECT_RECOVERY)
        assert hasattr(result, "desired_exit_count")
        assert hasattr(result, "actual_exit_count")
        assert hasattr(result, "extra_count")
        assert hasattr(result, "missing_count")
        assert hasattr(result, "deferred_count")
        assert hasattr(result, "is_converged")
        assert hasattr(result, "trigger")
        assert result.trigger == RepairTrigger.REJECT_RECOVERY

    def test_trigger_values_stable(self) -> None:
        assert RepairTrigger.SYNC_DRIFT.value == "SYNC_DRIFT"
        assert RepairTrigger.BUDGET_OVERRUN.value == "BUDGET_OVERRUN"
        assert RepairTrigger.REJECT_RECOVERY.value == "REJECT_RECOVERY"
        assert RepairTrigger.PARTIAL_FILL_RECOMPUTE.value == "PARTIAL_FILL_RECOMPUTE"


class TestMixedSideBudgeting:
    """Per-side budgeting: SELL exits budget against LONG, BUY against SHORT."""

    def test_sell_side_budget_independent_from_buy(self) -> None:
        """SELL exits constrained by sell_budget=0.001, BUY exits by buy_budget=0.002."""
        sell_exits = [
            _exit("exit-s1", "lot-s1", OrderSide.SELL, qty="0.001"),
            _exit("exit-s2", "lot-s2", OrderSide.SELL, qty="0.001"),
        ]
        buy_exits = [
            _exit("exit-b1", "lot-b1", OrderSide.BUY, qty="0.001"),
            _exit("exit-b2", "lot-b2", OrderSide.BUY, qty="0.001"),
        ]
        registry = {
            "exit-s1": "g-S1",
            "exit-s2": "g-S2",
            "exit-b1": "g-B1",
            "exit-b2": "g-B2",
        }
        # SELL budget = 0.001 → only 1 SELL exit fits
        desired_sell = compute_desired_exits(sell_exits, registry.get, Decimal("0.001"))
        assert len(desired_sell) == 1

        # BUY budget = 0.002 → both BUY exits fit
        desired_buy = compute_desired_exits(buy_exits, registry.get, Decimal("0.002"))
        assert len(desired_buy) == 2

        # Combined: 1 SELL + 2 BUY = 3 desired
        desired = desired_sell + desired_buy
        assert len(desired) == 3

    def test_one_side_constrained_other_unconstrained(self) -> None:
        """SELL exits fully constrained (budget=0), BUY exits fully allowed."""
        sell_exits = [_exit("exit-s1", "lot-s1", OrderSide.SELL, qty="0.001")]
        buy_exits = [_exit("exit-b1", "lot-b1", OrderSide.BUY, qty="0.001")]
        registry = {"exit-s1": "g-S1", "exit-b1": "g-B1"}

        desired_sell = compute_desired_exits(sell_exits, registry.get, Decimal("0"))
        desired_buy = compute_desired_exits(buy_exits, registry.get, Decimal("0.001"))

        assert len(desired_sell) == 0  # SELL suppressed
        assert len(desired_buy) == 1  # BUY allowed


class TestBudgetPrioritizesExchangeExits:
    """Regression: budget must prioritize exits already on exchange.

    Live canary showed: 3 lots (39.2 each), position=78.4, budget=78.4.
    Oldest lot's exit was lost (unregistered). Budget was consumed by
    the unregistered exit first (SM ordering), leaving no room for it
    on exchange. The stale DEFERRED re-place hit BLOCKED.
    """

    def test_exchange_exits_get_budget_priority(self) -> None:
        """On-exchange exits consume budget before unregistered ones."""
        exits = [
            _exit("exit-1", "lot-1", qty="39.2"),  # oldest, lost/unregistered
            _exit("exit-2", "lot-2", qty="39.2"),  # on exchange
            _exit("exit-3", "lot-3", qty="39.2"),  # on exchange
        ]
        registry = {"exit-2": "g-X-2", "exit-3": "g-X-3"}  # exit-1 not registered
        actual = frozenset({"g-X-2", "g-X-3"})

        desired = compute_desired_exits(
            exits, registry.get, Decimal("78.4"), actual_exit_cids=actual,
        )

        # Budget=78.4 fits exactly 2 exits. On-exchange exits (2,3) get priority.
        # Unregistered exit-1 is cut — no stale DEFERRED that would be BLOCKED.
        assert len(desired) == 2
        desired_eids = {d.exit_order_id for d in desired}
        assert "exit-2" in desired_eids
        assert "exit-3" in desired_eids
        assert "exit-1" not in desired_eids

    def test_unregistered_exit_included_when_budget_allows(self) -> None:
        """Unregistered exit is desired when budget has room."""
        exits = [
            _exit("exit-1", "lot-1", qty="39.2"),  # unregistered
            _exit("exit-2", "lot-2", qty="39.2"),  # on exchange
        ]
        registry = {"exit-2": "g-X-2"}
        actual = frozenset({"g-X-2"})

        desired = compute_desired_exits(
            exits, registry.get, Decimal("78.4"), actual_exit_cids=actual,
        )

        # Budget=78.4 fits both. On-exchange exit-2 first, then exit-1.
        assert len(desired) == 2

    def test_no_deferred_when_budget_fully_covered(self) -> None:
        """With budget-priority, the stale exit doesn't become DEFERRED."""
        exits = [
            _exit("exit-1", "lot-1", qty="39.2"),  # unregistered
            _exit("exit-2", "lot-2", qty="39.2"),  # on exchange
            _exit("exit-3", "lot-3", qty="39.2"),  # on exchange
        ]
        registry = {"exit-2": "g-X-2", "exit-3": "g-X-3"}
        actual_cids = frozenset({"g-X-2", "g-X-3"})

        desired = compute_desired_exits(
            exits, registry.get, Decimal("78.4"), actual_exit_cids=actual_cids,
        )
        result = compute_exit_topology_repair(desired, set(actual_cids))

        # No DEFERRED actions — the stale exit was cut at budget level
        deferred = [a for a in result.actions if a.action_type == "DEFERRED"]
        assert len(deferred) == 0
        assert result.is_converged

    def test_backwards_compatible_without_actual_cids(self) -> None:
        """Without actual_exit_cids, SM ordering is preserved (old behavior)."""
        exits = [
            _exit("exit-1", "lot-1", qty="39.2"),  # unregistered, SM first
            _exit("exit-2", "lot-2", qty="39.2"),  # registered
        ]
        registry = {"exit-2": "g-X-2"}

        desired = compute_desired_exits(
            exits, registry.get, Decimal("78.4"),
        )

        # Without actual_exit_cids, SM order preserved: exit-1 first
        assert len(desired) == 2
        assert desired[0].exit_order_id == "exit-1"
