"""Rotation controller: reconcile desired active set with current state (PR-C1a, ADR-127).

SSOT: docs/37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md Sections 5-6.
      docs/38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md Phase C1 ownership model.

Pure logic: no I/O, no logging, no metrics, no exchange interaction.
Deterministic: same inputs -> same actions.
Caller responsible for executing actions and updating runtime state.

The controller owns:
- Active-set intent (which symbols should be ACTIVE)
- Bounded change budget (MAX_CHANGES_PER_CYCLE)
- Hold timer enforcement (MIN_HOLD_CYCLES)
- Graceful deactivation path (non-flat -> GRACEFUL_EXIT_ONLY)
- No-churn guarantee (repeated reconcile on same inputs = no actions)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from grinder.rotation.state_machine import SymbolLifecycle, SymbolState


class RotationActionKind(Enum):
    """Controller action intent types."""

    ACTIVATE = "ACTIVATE"
    REQUEST_GRACEFUL_EXIT = "REQUEST_GRACEFUL_EXIT"
    FINALIZE_DEACTIVATION = "FINALIZE_DEACTIVATION"
    ENTER_COOLDOWN = "ENTER_COOLDOWN"


@dataclass(frozen=True)
class RotationAction:
    """Symbolic action intent from the controller.

    Not a side effect — caller executes these externally.
    """

    kind: RotationActionKind
    symbol: str
    reason: str = ""


@dataclass(frozen=True)
class RotationConfig:
    """Configuration for rotation controller.

    Attributes:
        top_k: Maximum number of simultaneously active symbols.
        max_changes_per_cycle: Maximum add + remove operations per reconcile cycle.
        min_hold_cycles: Minimum cycles a symbol must stay ACTIVE before removal.
    """

    top_k: int = 3
    max_changes_per_cycle: int = 1
    min_hold_cycles: int = 5


@dataclass(frozen=True)
class SymbolFacts:
    """Runtime facts about a symbol needed for controller decisions.

    Attributes:
        is_flat: Whether the symbol's position is flat (no open inventory).
    """

    is_flat: bool = True


@dataclass
class RotationController:
    """Controller that reconciles desired active set with current symbol states.

    Owns per-symbol SymbolLifecycle instances. Produces RotationAction
    intents that the caller (orchestrator) executes externally.

    Deterministic: same (lifecycles, desired_symbols, facts, cycle) -> same actions.
    """

    config: RotationConfig = field(default_factory=RotationConfig)

    _lifecycles: dict[str, SymbolLifecycle] = field(default_factory=dict)
    _activation_cycle: dict[str, int] = field(default_factory=dict)
    _cycle: int = 0

    def get_lifecycle(self, symbol: str) -> SymbolLifecycle:
        """Get or create lifecycle for a symbol."""
        if symbol not in self._lifecycles:
            self._lifecycles[symbol] = SymbolLifecycle(symbol=symbol, state=SymbolState.TUNED)
        return self._lifecycles[symbol]

    def get_active_symbols(self) -> list[str]:
        """Return symbols currently in ACTIVE state."""
        return sorted(s for s, lc in self._lifecycles.items() if lc.state == SymbolState.ACTIVE)

    def get_graceful_exit_symbols(self) -> list[str]:
        """Return symbols currently in GRACEFUL_EXIT_ONLY state."""
        return sorted(
            s for s, lc in self._lifecycles.items() if lc.state == SymbolState.GRACEFUL_EXIT_ONLY
        )

    def reconcile(
        self,
        desired_symbols: list[str],
        facts: dict[str, SymbolFacts],
    ) -> list[RotationAction]:
        """Reconcile desired active set with current state.

        Produces bounded action intents. Same inputs -> same actions.

        Args:
            desired_symbols: Ranked list of symbols that should be active
                (already filtered to TUNED-only by caller). Order = priority.
            facts: Runtime facts per symbol (is_flat, etc.).

        Returns:
            List of RotationAction intents for the caller to execute.
        """
        self._cycle += 1
        actions: list[RotationAction] = []

        desired_set = set(desired_symbols[: self.config.top_k])
        current_active = {s for s, lc in self._lifecycles.items() if lc.state == SymbolState.ACTIVE}
        current_graceful = {
            s for s, lc in self._lifecycles.items() if lc.state == SymbolState.GRACEFUL_EXIT_ONLY
        }

        changes_used = self._reconcile_removals(actions, current_active - desired_set, facts)
        self._reconcile_graceful_completions(actions, current_graceful, facts)
        self._reconcile_additions(actions, desired_symbols, current_active, changes_used)

        return actions

    def _reconcile_removals(
        self,
        actions: list[RotationAction],
        to_remove: set[str],
        facts: dict[str, SymbolFacts],
    ) -> int:
        """Phase 1: Handle removals (active symbols no longer desired)."""
        changes_used = 0
        for symbol in sorted(to_remove):
            if changes_used >= self.config.max_changes_per_cycle:
                break
            activated_at = self._activation_cycle.get(symbol, 0)
            if (self._cycle - activated_at) < self.config.min_hold_cycles:
                continue
            sf = facts.get(symbol, SymbolFacts())
            reason = (
                "dropped_from_desired_set_flat"
                if sf.is_flat
                else "dropped_from_desired_set_non_flat"
            )
            actions.append(
                RotationAction(
                    kind=RotationActionKind.REQUEST_GRACEFUL_EXIT,
                    symbol=symbol,
                    reason=reason,
                )
            )
            changes_used += 1
        return changes_used

    def _reconcile_graceful_completions(
        self,
        actions: list[RotationAction],
        graceful_symbols: set[str],
        facts: dict[str, SymbolFacts],
    ) -> None:
        """Phase 2: Finalize deactivation for flat graceful-exit symbols."""
        for symbol in sorted(graceful_symbols):
            sf = facts.get(symbol, SymbolFacts())
            if sf.is_flat:
                actions.append(
                    RotationAction(
                        kind=RotationActionKind.FINALIZE_DEACTIVATION,
                        symbol=symbol,
                        reason="position_flat",
                    )
                )

    def _reconcile_additions(
        self,
        actions: list[RotationAction],
        desired_symbols: list[str],
        current_active: set[str],
        changes_used: int,
    ) -> None:
        """Phase 3: Activate desired TUNED symbols not yet active."""
        active_after_removals = len(current_active) - sum(
            1
            for a in actions
            if a.kind == RotationActionKind.REQUEST_GRACEFUL_EXIT and a.symbol in current_active
        )

        for symbol in desired_symbols[: self.config.top_k]:
            if changes_used >= self.config.max_changes_per_cycle:
                break
            if active_after_removals >= self.config.top_k:
                break
            lc = self.get_lifecycle(symbol)
            if lc.state in (
                SymbolState.ACTIVE,
                SymbolState.GRACEFUL_EXIT_ONLY,
                SymbolState.COOLDOWN,
            ):
                continue
            if lc.state == SymbolState.TUNED:
                actions.append(
                    RotationAction(
                        kind=RotationActionKind.ACTIVATE,
                        symbol=symbol,
                        reason="selected_into_desired_set",
                    )
                )
                changes_used += 1
                active_after_removals += 1

    def apply_activation(self, symbol: str) -> None:
        """Record that a symbol has been activated.

        Called by orchestrator after executing ACTIVATE action.
        """
        from grinder.rotation.state_machine import TransitionReason  # noqa: PLC0415

        lc = self.get_lifecycle(symbol)
        lc.transition(SymbolState.ACTIVE, TransitionReason.SELECTED_INTO_TOP_K)
        self._activation_cycle[symbol] = self._cycle

    def apply_graceful_exit(self, symbol: str) -> None:
        """Record that a symbol has entered graceful exit.

        Called by orchestrator after executing REQUEST_GRACEFUL_EXIT action.
        """
        from grinder.rotation.state_machine import TransitionReason  # noqa: PLC0415

        lc = self.get_lifecycle(symbol)
        lc.transition(SymbolState.GRACEFUL_EXIT_ONLY, TransitionReason.DROPPED_FROM_TOP_K)

    def apply_deactivation(self, symbol: str) -> None:
        """Record that a symbol has been fully deactivated.

        Called by orchestrator after executing FINALIZE_DEACTIVATION action.
        """
        from grinder.rotation.state_machine import TransitionReason  # noqa: PLC0415

        lc = self.get_lifecycle(symbol)
        lc.transition(SymbolState.COOLDOWN, TransitionReason.POSITION_FLAT)
        self._activation_cycle.pop(symbol, None)

    def reset(self) -> None:
        """Reset all state (for testing)."""
        self._lifecycles.clear()
        self._activation_cycle.clear()
        self._cycle = 0
