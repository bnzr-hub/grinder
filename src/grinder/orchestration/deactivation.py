"""Deactivation finalize gate for symbol removal (PR-C2a, ADR-129).

SSOT: docs/38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md Phase C2.

Pure logic: no I/O, no exchange interaction, no engine commands.
Deterministic: same facts -> same decision.

This is a finalization gate, not a staged protocol. The caller
(RotationController / SymbolOrchestrator) owns stage progression
(ACTIVE -> GRACEFUL_EXIT_ONLY -> COOLDOWN). This module answers one
question: "given current runtime facts, is it safe to finalize?"

Clean conditions (ALL must pass):
- Position is flat (no open inventory)
- No open orders remain on exchange
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
    """Result of deactivation finalize gate for one symbol.

    Attributes:
        symbol: Trading pair being deactivated.
        can_finalize: Whether cleanup verification passed.
        block_reason: If cannot finalize, why. None if can_finalize.
    """

    symbol: str
    can_finalize: bool
    block_reason: BlockReason | None = None


def can_finalize_deactivation(
    symbol: str,
    facts: DeactivationFacts,
) -> DeactivationDecision:
    """Check whether a symbol's deactivation can be finalized.

    Pure finalize gate. Same inputs -> same decision.
    Does NOT model stage progression — that is owned by RotationController.

    Args:
        symbol: Trading pair being deactivated.
        facts: Current runtime facts about the symbol.

    Returns:
        DeactivationDecision indicating whether finalization is legal.
    """
    if not facts.is_flat and facts.open_orders_count > 0:
        return DeactivationDecision(
            symbol=symbol,
            can_finalize=False,
            block_reason=BlockReason.POSITION_NOT_FLAT_AND_ORDERS_REMAIN,
        )

    if not facts.is_flat:
        return DeactivationDecision(
            symbol=symbol,
            can_finalize=False,
            block_reason=BlockReason.POSITION_NOT_FLAT,
        )

    if facts.open_orders_count > 0:
        return DeactivationDecision(
            symbol=symbol,
            can_finalize=False,
            block_reason=BlockReason.OPEN_ORDERS_REMAIN,
        )

    return DeactivationDecision(
        symbol=symbol,
        can_finalize=True,
    )
