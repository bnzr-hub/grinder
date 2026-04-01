"""Deactivation verification: cleanup gate for symbol removal (PR-C2a, ADR-129).

SSOT: docs/38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md Phase C2.

Pure logic: no I/O, no exchange interaction, no engine commands.
Deterministic: same facts -> same decision.

A symbol can only be finalized as deactivated when ALL clean conditions pass:
- Position is flat (no open inventory)
- No open orders remain on exchange

The caller (orchestrator) is responsible for gathering these facts from
runtime systems and executing the decision externally.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DeactivationStage(Enum):
    """Stages in the deactivation sub-protocol."""

    NONE = "NONE"
    REQUESTED = "REQUESTED"
    GRACEFUL_EXIT_ONLY = "GRACEFUL_EXIT_ONLY"
    VERIFYING_CLEAN = "VERIFYING_CLEAN"
    COMPLETE = "COMPLETE"


class BlockReason(Enum):
    """Why deactivation finalization is blocked."""

    POSITION_NOT_FLAT = "POSITION_NOT_FLAT"
    OPEN_ORDERS_REMAIN = "OPEN_ORDERS_REMAIN"
    POSITION_NOT_FLAT_AND_ORDERS_REMAIN = "POSITION_NOT_FLAT_AND_ORDERS_REMAIN"


@dataclass(frozen=True)
class DeactivationFacts:
    """Runtime facts needed for deactivation verification.

    Attributes:
        is_flat: Whether the symbol's position is flat (no open inventory).
        open_orders_count: Number of open orders remaining on exchange.
    """

    is_flat: bool
    open_orders_count: int


@dataclass(frozen=True)
class DeactivationDecision:
    """Result of deactivation verification for one symbol.

    Attributes:
        symbol: Trading pair being deactivated.
        stage: Current deactivation stage.
        can_finalize: Whether cleanup verification passed.
        block_reason: If cannot finalize, why. None if can_finalize.
    """

    symbol: str
    stage: DeactivationStage
    can_finalize: bool
    block_reason: BlockReason | None = None


def evaluate_deactivation(
    symbol: str,
    facts: DeactivationFacts,
    current_stage: DeactivationStage = DeactivationStage.GRACEFUL_EXIT_ONLY,
) -> DeactivationDecision:
    """Evaluate whether a symbol can be finalized as deactivated.

    Pure function. Same inputs -> same decision.

    Args:
        symbol: Trading pair being deactivated.
        facts: Current runtime facts about the symbol.
        current_stage: Current deactivation stage (default: GRACEFUL_EXIT_ONLY,
            the typical stage when this is called).

    Returns:
        DeactivationDecision indicating whether finalization is legal.
    """
    if not facts.is_flat and facts.open_orders_count > 0:
        return DeactivationDecision(
            symbol=symbol,
            stage=current_stage,
            can_finalize=False,
            block_reason=BlockReason.POSITION_NOT_FLAT_AND_ORDERS_REMAIN,
        )

    if not facts.is_flat:
        return DeactivationDecision(
            symbol=symbol,
            stage=current_stage,
            can_finalize=False,
            block_reason=BlockReason.POSITION_NOT_FLAT,
        )

    if facts.open_orders_count > 0:
        return DeactivationDecision(
            symbol=symbol,
            stage=current_stage,
            can_finalize=False,
            block_reason=BlockReason.OPEN_ORDERS_REMAIN,
        )

    return DeactivationDecision(
        symbol=symbol,
        stage=DeactivationStage.COMPLETE,
        can_finalize=True,
    )
