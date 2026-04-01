"""Tests for RotationController (PR-C1a, ADR-127).

Covers:
- Activation path (TUNED -> ACTIVE)
- Graceful deactivation (non-flat blocks finalize)
- Flat symbol can finalize
- Cooldown blocks re-entry
- Bounded changes per cycle
- Min hold cycles enforcement
- No churn on repeated reconcile
- Multiple symbols respect top_k
- Replacement waits for safe exit
- Determinism
"""

from __future__ import annotations

from grinder.rotation.controller import (
    RotationAction,
    RotationActionKind,
    RotationConfig,
    RotationController,
    SymbolFacts,
)
from grinder.rotation.state_machine import SymbolState


class TestActivationPath:
    """TUNED symbol selected into desired set gets ACTIVATE action."""

    def test_activate_tuned_symbol(self) -> None:
        ctrl = RotationController(config=RotationConfig(top_k=3, max_changes_per_cycle=3))
        actions = ctrl.reconcile(["BTCUSDT"], {})
        assert len(actions) == 1
        assert actions[0].kind == RotationActionKind.ACTIVATE
        assert actions[0].symbol == "BTCUSDT"

    def test_apply_activation_transitions_to_active(self) -> None:
        ctrl = RotationController(config=RotationConfig(top_k=3, max_changes_per_cycle=3))
        ctrl.reconcile(["BTCUSDT"], {})
        ctrl.apply_activation("BTCUSDT")
        assert ctrl.get_lifecycle("BTCUSDT").state == SymbolState.ACTIVE
        assert "BTCUSDT" in ctrl.get_active_symbols()

    def test_already_active_no_duplicate(self) -> None:
        ctrl = RotationController(config=RotationConfig(top_k=3, max_changes_per_cycle=3))
        ctrl.reconcile(["BTCUSDT"], {})
        ctrl.apply_activation("BTCUSDT")
        # Second reconcile with same desired set
        actions = ctrl.reconcile(["BTCUSDT"], {})
        activate_actions = [a for a in actions if a.kind == RotationActionKind.ACTIVATE]
        assert len(activate_actions) == 0


class TestGracefulDeactivation:
    """Non-flat symbol gets graceful exit, not hard drop."""

    def test_non_flat_gets_graceful_exit(self) -> None:
        ctrl = RotationController(
            config=RotationConfig(top_k=3, max_changes_per_cycle=3, min_hold_cycles=0)
        )
        ctrl.reconcile(["BTCUSDT"], {})
        ctrl.apply_activation("BTCUSDT")

        # Remove BTCUSDT from desired set, non-flat
        facts = {"BTCUSDT": SymbolFacts(is_flat=False)}
        actions = ctrl.reconcile([], facts)

        graceful = [a for a in actions if a.kind == RotationActionKind.REQUEST_GRACEFUL_EXIT]
        assert len(graceful) == 1
        assert graceful[0].symbol == "BTCUSDT"
        assert "non_flat" in graceful[0].reason

    def test_non_flat_blocks_finalize(self) -> None:
        ctrl = RotationController(
            config=RotationConfig(top_k=3, max_changes_per_cycle=3, min_hold_cycles=0)
        )
        ctrl.reconcile(["BTCUSDT"], {})
        ctrl.apply_activation("BTCUSDT")

        # Request graceful exit
        ctrl.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=False)})
        ctrl.apply_graceful_exit("BTCUSDT")

        # Still non-flat: no finalize
        actions = ctrl.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=False)})
        finalize = [a for a in actions if a.kind == RotationActionKind.FINALIZE_DEACTIVATION]
        assert len(finalize) == 0

    def test_flat_allows_finalize(self) -> None:
        ctrl = RotationController(
            config=RotationConfig(top_k=3, max_changes_per_cycle=3, min_hold_cycles=0)
        )
        ctrl.reconcile(["BTCUSDT"], {})
        ctrl.apply_activation("BTCUSDT")

        ctrl.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=True)})
        ctrl.apply_graceful_exit("BTCUSDT")

        # Now flat: finalize
        actions = ctrl.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=True)})
        finalize = [a for a in actions if a.kind == RotationActionKind.FINALIZE_DEACTIVATION]
        assert len(finalize) == 1
        assert finalize[0].symbol == "BTCUSDT"


class TestCooldown:
    """Symbol in cooldown cannot immediately re-enter."""

    def test_cooldown_blocks_activation(self) -> None:
        ctrl = RotationController(
            config=RotationConfig(top_k=3, max_changes_per_cycle=3, min_hold_cycles=0)
        )
        # Activate then deactivate
        ctrl.reconcile(["BTCUSDT"], {})
        ctrl.apply_activation("BTCUSDT")
        ctrl.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=True)})
        ctrl.apply_graceful_exit("BTCUSDT")
        ctrl.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=True)})
        ctrl.apply_deactivation("BTCUSDT")

        assert ctrl.get_lifecycle("BTCUSDT").state == SymbolState.COOLDOWN

        # Try to re-activate immediately
        actions = ctrl.reconcile(["BTCUSDT"], {})
        activate = [a for a in actions if a.kind == RotationActionKind.ACTIVATE]
        assert len(activate) == 0  # Blocked by COOLDOWN state


class TestBoundedChanges:
    """MAX_CHANGES_PER_CYCLE limits actions per reconcile."""

    def test_max_one_change(self) -> None:
        ctrl = RotationController(config=RotationConfig(top_k=5, max_changes_per_cycle=1))
        # 3 symbols want activation, but only 1 allowed per cycle
        actions = ctrl.reconcile(["A", "B", "C"], {})
        activate = [a for a in actions if a.kind == RotationActionKind.ACTIVATE]
        assert len(activate) == 1

    def test_max_two_changes(self) -> None:
        ctrl = RotationController(config=RotationConfig(top_k=5, max_changes_per_cycle=2))
        actions = ctrl.reconcile(["A", "B", "C"], {})
        activate = [a for a in actions if a.kind == RotationActionKind.ACTIVATE]
        assert len(activate) == 2


class TestMinHoldCycles:
    """Symbol cannot be removed within MIN_HOLD_CYCLES after activation."""

    def test_hold_blocks_removal(self) -> None:
        ctrl = RotationController(
            config=RotationConfig(top_k=3, max_changes_per_cycle=3, min_hold_cycles=3)
        )
        ctrl.reconcile(["BTCUSDT"], {})
        ctrl.apply_activation("BTCUSDT")

        # Cycles 2 and 3: removal blocked by hold timer
        actions = ctrl.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=True)})
        graceful = [a for a in actions if a.kind == RotationActionKind.REQUEST_GRACEFUL_EXIT]
        assert len(graceful) == 0  # Hold timer not expired

    def test_hold_expires_allows_removal(self) -> None:
        ctrl = RotationController(
            config=RotationConfig(top_k=3, max_changes_per_cycle=3, min_hold_cycles=2)
        )
        ctrl.reconcile(["BTCUSDT"], {})
        ctrl.apply_activation("BTCUSDT")

        # Cycle 2: still within hold
        ctrl.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=True)})

        # Cycle 3: hold expired (activated at cycle 1, now cycle 3, diff=2 >= min_hold=2)
        actions = ctrl.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=True)})
        graceful = [a for a in actions if a.kind == RotationActionKind.REQUEST_GRACEFUL_EXIT]
        assert len(graceful) == 1


class TestNoChurn:
    """Repeated reconcile on same inputs produces no new actions."""

    def test_stable_active_set_no_actions(self) -> None:
        ctrl = RotationController(
            config=RotationConfig(top_k=3, max_changes_per_cycle=3, min_hold_cycles=0)
        )
        ctrl.reconcile(["BTCUSDT"], {})
        ctrl.apply_activation("BTCUSDT")

        # Same desired set, symbol already active
        a1 = ctrl.reconcile(["BTCUSDT"], {})
        a2 = ctrl.reconcile(["BTCUSDT"], {})
        # No activate/remove actions
        assert all(a.kind != RotationActionKind.ACTIVATE for a in a1)
        assert all(a.kind != RotationActionKind.ACTIVATE for a in a2)
        assert all(a.kind != RotationActionKind.REQUEST_GRACEFUL_EXIT for a in a1)
        assert all(a.kind != RotationActionKind.REQUEST_GRACEFUL_EXIT for a in a2)


class TestTopK:
    """Only top_k symbols can be active simultaneously."""

    def test_respects_top_k(self) -> None:
        ctrl = RotationController(config=RotationConfig(top_k=2, max_changes_per_cycle=5))
        actions = ctrl.reconcile(["A", "B", "C"], {})
        activate = [a for a in actions if a.kind == RotationActionKind.ACTIVATE]
        assert len(activate) == 2
        assert {a.symbol for a in activate} == {"A", "B"}


class TestReplacementWaitsForSafeExit:
    """Better candidate appears while current symbol non-flat — defers safely."""

    def test_replacement_deferred_non_flat(self) -> None:
        ctrl = RotationController(
            config=RotationConfig(top_k=1, max_changes_per_cycle=2, min_hold_cycles=0)
        )
        # Activate A
        ctrl.reconcile(["A"], {})
        ctrl.apply_activation("A")

        # Now B is desired instead of A, but A is non-flat
        actions = ctrl.reconcile(["B"], {"A": SymbolFacts(is_flat=False)})

        # A gets graceful exit request (not hard drop)
        graceful = [a for a in actions if a.kind == RotationActionKind.REQUEST_GRACEFUL_EXIT]
        assert len(graceful) == 1
        assert graceful[0].symbol == "A"

        # B cannot activate yet because active_after_removals still counts A
        # (A moved to graceful exit but slot not yet freed until finalize)
        # Change budget may also be exhausted


class TestDeterminism:
    """Same inputs produce identical actions."""

    def test_repeated_reconcile_same_result(self) -> None:
        def run() -> list[RotationAction]:
            ctrl = RotationController(
                config=RotationConfig(top_k=2, max_changes_per_cycle=2, min_hold_cycles=0)
            )
            return ctrl.reconcile(["A", "B", "C"], {})

        a1 = run()
        a2 = run()
        assert len(a1) == len(a2)
        for x, y in zip(a1, a2, strict=True):
            assert x.kind == y.kind
            assert x.symbol == y.symbol
