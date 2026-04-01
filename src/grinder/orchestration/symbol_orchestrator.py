"""SymbolOrchestrator: top-level owner for multi-symbol rotation (PR-C1b, ADR-128).

SSOT: docs/38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md Phase C1 ownership model.

Wires TuningCache → candidate filtering → RotationController → action intents.
Pure logic: no I/O, no engine start/stop, no exchange writes.
Deterministic: same (candidates, cache state, facts) -> same decision.

The orchestrator owns:
- Tuned-symbol intake from TuningCache
- Candidate filtering (only valid TUNED entries admitted)
- Handoff to RotationController
- Decision visibility (admitted, skipped, actions)

The orchestrator does NOT own:
- Engine lifecycle execution (deferred to future runtime wiring)
- Exchange writes
- Grid logic
- EventLedger internals
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from grinder.rotation.controller import (
    RotationController,
    SymbolFacts,
)

if TYPE_CHECKING:
    from grinder.rotation.controller import RotationAction
    from grinder.tuning.cache import TuningCache


class SkipReason(Enum):
    """Why a candidate symbol was excluded from the desired set.

    Note: TuningCache.get() returns None for both miss and expiry,
    so CACHE_MISS covers both cases. If expiry needs to be distinguished
    in a future phase, extend TuningCache to expose that signal.
    """

    CACHE_MISS = "CACHE_MISS"
    NOT_TUNED = "NOT_TUNED"


@dataclass(frozen=True)
class OrchestratorDecision:
    """Result of one orchestrator reconcile cycle.

    Attributes:
        admitted: Symbols that passed cache validation, in input order.
        skipped: Map of symbol -> reason for exclusion.
        actions: RotationAction intents from the controller.
    """

    admitted: list[str] = field(default_factory=list)
    skipped: dict[str, SkipReason] = field(default_factory=dict)
    actions: list[RotationAction] = field(default_factory=list)


@dataclass
class SymbolOrchestrator:
    """Top-level orchestration owner for multi-symbol rotation.

    Reads TuningCache, filters candidates to valid TUNED entries,
    hands ordered desired symbols to RotationController, and returns
    a decision with admitted/skipped symbols and action intents.

    Pure logic — caller executes returned actions externally.
    """

    cache: TuningCache
    controller: RotationController = field(default_factory=RotationController)

    def reconcile(
        self,
        candidates: list[str],
        facts: dict[str, SymbolFacts] | None = None,
    ) -> OrchestratorDecision:
        """Run one orchestration cycle.

        Args:
            candidates: Ordered list of candidate symbols (ranking from caller).
            facts: Runtime facts per symbol (is_flat, etc.). Defaults to empty.

        Returns:
            OrchestratorDecision with admitted/skipped symbols and controller actions.
        """
        effective_facts = facts or {}
        admitted: list[str] = []
        skipped: dict[str, SkipReason] = {}

        for symbol in candidates:
            result = self.cache.get(symbol)
            if result is None:
                skipped[symbol] = SkipReason.CACHE_MISS
                continue

            from grinder.tuning.solver import TuningStatus  # noqa: PLC0415

            if result.status != TuningStatus.TUNED:
                skipped[symbol] = SkipReason.NOT_TUNED
                continue

            admitted.append(symbol)

        actions = self.controller.reconcile(admitted, effective_facts)

        return OrchestratorDecision(
            admitted=admitted,
            skipped=skipped,
            actions=actions,
        )
