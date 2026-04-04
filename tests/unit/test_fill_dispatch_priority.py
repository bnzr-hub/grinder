"""Tests for fill-to-branch dispatch priority optimization."""

from __future__ import annotations

from decimal import Decimal

from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.engine import _reorder_fill_actions


def _place_exit(cid: str = "x1") -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="DRIFTUSDT",
        side=None,
        price=Decimal("0.0409"),
        quantity=Decimal("124"),
        client_order_id=cid,
        reason="grid_v2_PLACE_EXIT",
        reduce_only=True,
    )


def _place_entry(cid: str = "e1") -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="DRIFTUSDT",
        side=None,
        price=Decimal("0.0415"),
        quantity=Decimal("124"),
        client_order_id=cid,
        reason="grid_v2_PLACE_ENTRY",
        reduce_only=False,
    )


def _cancel(cid: str = "c1") -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.CANCEL,
        order_id=cid,
        symbol="DRIFTUSDT",
        reason="grid_v2_CANCEL_ENTRY",
    )


class TestReorderFillActions:
    """Priority: exit → cancel → entry (safe branch dispatch order)."""

    def test_exit_before_cancel(self) -> None:
        actions = [_cancel("c1"), _place_exit("x1")]
        reordered = _reorder_fill_actions(actions)
        assert reordered[0].reduce_only is True
        assert reordered[1].action_type == ActionType.CANCEL

    def test_exit_before_entry(self) -> None:
        actions = [_place_entry("e1"), _place_exit("x1")]
        reordered = _reorder_fill_actions(actions)
        assert reordered[0].reduce_only is True
        assert reordered[1].reduce_only is False

    def test_cancel_before_entry(self) -> None:
        """Opposite-side cancels dispatch before new entries."""
        actions = [_place_entry("e1"), _cancel("c1")]
        reordered = _reorder_fill_actions(actions)
        assert reordered[0].action_type == ActionType.CANCEL
        assert reordered[1].action_type == ActionType.PLACE
        assert reordered[1].reduce_only is False

    def test_full_fill_sequence(self) -> None:
        """Full fill sequence: exit → cancel1 → cancel2 → entry."""
        actions = [
            _place_entry("e1"),
            _cancel("c1"),
            _cancel("c2"),
            _place_exit("x1"),
        ]
        reordered = _reorder_fill_actions(actions)
        assert reordered[0].client_order_id == "x1"  # exit first
        assert reordered[1].action_type == ActionType.CANCEL  # cancels second
        assert reordered[2].action_type == ActionType.CANCEL
        assert reordered[3].client_order_id == "e1"  # entry last

    def test_empty_list(self) -> None:
        assert _reorder_fill_actions([]) == []

    def test_single_cancel(self) -> None:
        actions = [_cancel("c1")]
        assert _reorder_fill_actions(actions) == actions

    def test_stable_within_priority(self) -> None:
        """Same-priority actions preserve relative order."""
        actions = [_cancel("c1"), _cancel("c2"), _cancel("c3")]
        reordered = _reorder_fill_actions(actions)
        cids = [a.order_id for a in reordered]
        assert cids == ["c1", "c2", "c3"]
