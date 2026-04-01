"""Tests for deactivation finalize gate (PR-C2a, ADR-129).

Covers:
- Non-flat + open orders: blocked
- Non-flat + no orders: blocked
- Flat + open orders: blocked
- Flat + no orders: finalizable
- Dirty then clean progression
- Determinism
- Reason codes explicit
- Symbol preserved in decision
- Integration with controller removal path
"""

from __future__ import annotations

from grinder.orchestration.deactivation import (
    BlockReason,
    DeactivationFacts,
    can_finalize_deactivation,
)


class TestNonFlatOpenOrdersBlocked:
    """Non-flat position with open orders cannot finalize."""

    def test_blocked(self) -> None:
        facts = DeactivationFacts(is_flat=False, open_orders_count=3)
        decision = can_finalize_deactivation("BTCUSDT", facts)

        assert decision.can_finalize is False
        assert decision.block_reason == BlockReason.POSITION_NOT_FLAT_AND_ORDERS_REMAIN


class TestNonFlatNoOrdersBlocked:
    """Non-flat position without orders still cannot finalize."""

    def test_blocked(self) -> None:
        facts = DeactivationFacts(is_flat=False, open_orders_count=0)
        decision = can_finalize_deactivation("BTCUSDT", facts)

        assert decision.can_finalize is False
        assert decision.block_reason == BlockReason.POSITION_NOT_FLAT


class TestFlatWithOpenOrdersBlocked:
    """Flat position but lingering orders cannot finalize."""

    def test_blocked(self) -> None:
        facts = DeactivationFacts(is_flat=True, open_orders_count=2)
        decision = can_finalize_deactivation("BTCUSDT", facts)

        assert decision.can_finalize is False
        assert decision.block_reason == BlockReason.OPEN_ORDERS_REMAIN


class TestFlatNoOrdersFinalizable:
    """Flat position with no orders: clean state, finalize allowed."""

    def test_finalizable(self) -> None:
        facts = DeactivationFacts(is_flat=True, open_orders_count=0)
        decision = can_finalize_deactivation("BTCUSDT", facts)

        assert decision.can_finalize is True
        assert decision.block_reason is None


class TestDirtyThenClean:
    """Finalize gate reflects current facts, not history."""

    def test_dirty_then_clean(self) -> None:
        dirty = DeactivationFacts(is_flat=False, open_orders_count=1)
        d1 = can_finalize_deactivation("BTCUSDT", dirty)
        assert d1.can_finalize is False

        clean = DeactivationFacts(is_flat=True, open_orders_count=0)
        d2 = can_finalize_deactivation("BTCUSDT", clean)
        assert d2.can_finalize is True


class TestDeterminism:
    """Same inputs produce identical decisions."""

    def test_same_facts_same_decision(self) -> None:
        facts = DeactivationFacts(is_flat=True, open_orders_count=0)
        d1 = can_finalize_deactivation("BTCUSDT", facts)
        d2 = can_finalize_deactivation("BTCUSDT", facts)
        assert d1 == d2

    def test_blocked_deterministic(self) -> None:
        facts = DeactivationFacts(is_flat=False, open_orders_count=5)
        d1 = can_finalize_deactivation("X", facts)
        d2 = can_finalize_deactivation("X", facts)
        assert d1 == d2


class TestReasonCodesExplicit:
    """All blocked outcomes have explicit reason codes."""

    def test_all_block_reasons_reachable(self) -> None:
        d1 = can_finalize_deactivation("A", DeactivationFacts(is_flat=False, open_orders_count=0))
        assert d1.block_reason == BlockReason.POSITION_NOT_FLAT

        d2 = can_finalize_deactivation("B", DeactivationFacts(is_flat=True, open_orders_count=1))
        assert d2.block_reason == BlockReason.OPEN_ORDERS_REMAIN

        d3 = can_finalize_deactivation("C", DeactivationFacts(is_flat=False, open_orders_count=1))
        assert d3.block_reason == BlockReason.POSITION_NOT_FLAT_AND_ORDERS_REMAIN

    def test_finalizable_has_no_reason(self) -> None:
        d = can_finalize_deactivation("A", DeactivationFacts(is_flat=True, open_orders_count=0))
        assert d.block_reason is None


class TestSymbolPreserved:
    """Decision carries through the input symbol."""

    def test_symbol_in_decision(self) -> None:
        d = can_finalize_deactivation(
            "ETHUSDT", DeactivationFacts(is_flat=True, open_orders_count=0)
        )
        assert d.symbol == "ETHUSDT"


class TestControllerIntegration:
    """Controller removal + deactivation finalize gate work together."""

    def test_controller_removal_clean_allows_finalize(self) -> None:
        """After controller requests graceful exit, clean facts allow finalize."""
        from grinder.rotation.controller import (  # noqa: PLC0415
            RotationConfig,
            RotationController,
            SymbolFacts,
        )

        ctrl = RotationController(
            config=RotationConfig(top_k=1, max_changes_per_cycle=2, min_hold_cycles=0)
        )
        ctrl.reconcile(["BTCUSDT"], {})
        ctrl.apply_activation("BTCUSDT")

        actions = ctrl.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=True)})
        assert any(a.symbol == "BTCUSDT" for a in actions)

        facts = DeactivationFacts(is_flat=True, open_orders_count=0)
        decision = can_finalize_deactivation("BTCUSDT", facts)
        assert decision.can_finalize is True

    def test_controller_removal_dirty_blocks_finalize(self) -> None:
        """After controller requests graceful exit, dirty facts block finalize."""
        from grinder.rotation.controller import (  # noqa: PLC0415
            RotationConfig,
            RotationController,
            SymbolFacts,
        )

        ctrl = RotationController(
            config=RotationConfig(top_k=1, max_changes_per_cycle=2, min_hold_cycles=0)
        )
        ctrl.reconcile(["BTCUSDT"], {})
        ctrl.apply_activation("BTCUSDT")

        ctrl.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=False)})

        facts = DeactivationFacts(is_flat=False, open_orders_count=2)
        decision = can_finalize_deactivation("BTCUSDT", facts)
        assert decision.can_finalize is False
        assert decision.block_reason == BlockReason.POSITION_NOT_FLAT_AND_ORDERS_REMAIN
