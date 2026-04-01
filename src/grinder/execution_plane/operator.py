"""Operator controls for execution-plane (PR-E5, ADR-138).

SSOT: docs/39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md Section 7.

Explicit operator commands: pause, resume, quarantine, release, force-deactivate.
All auditable — every action produces an AuditRecord.
Pure state management: does not execute engine operations directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class OperatorAction(Enum):
    """Operator control action types."""

    PAUSE = "PAUSE"
    RESUME = "RESUME"
    QUARANTINE = "QUARANTINE"
    RELEASE_QUARANTINE = "RELEASE_QUARANTINE"
    FORCE_DEACTIVATE = "FORCE_DEACTIVATE"


@dataclass(frozen=True)
class AuditRecord:
    """Immutable record of an operator action.

    Attributes:
        action: What the operator did.
        symbol: Affected symbol (empty for global actions like pause/resume).
        operator: Who did it (identifier string).
        reason: Why.
    """

    action: OperatorAction
    symbol: str = ""
    operator: str = "operator"
    reason: str = ""


@dataclass
class OperatorControls:
    """Execution-plane operator control surface.

    Manages pause state, quarantine set, and force-deactivation intents.
    All actions auditable.
    """

    _paused: bool = False
    _quarantined: set[str] = field(default_factory=set)
    _force_deactivate_pending: set[str] = field(default_factory=set)
    _audit_log: list[AuditRecord] = field(default_factory=list)

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def quarantined_symbols(self) -> frozenset[str]:
        return frozenset(self._quarantined)

    @property
    def force_deactivate_pending(self) -> frozenset[str]:
        return frozenset(self._force_deactivate_pending)

    @property
    def audit_log(self) -> list[AuditRecord]:
        return list(self._audit_log)

    def pause(self, operator: str = "operator", reason: str = "") -> AuditRecord:
        """Pause execution. No new activations/deactivations while paused."""
        self._paused = True
        record = AuditRecord(action=OperatorAction.PAUSE, operator=operator, reason=reason)
        self._audit_log.append(record)
        logger.info("OPERATOR_CONTROL action=PAUSE operator=%s reason=%s", operator, reason)
        return record

    def resume(self, operator: str = "operator", reason: str = "") -> AuditRecord:
        """Resume execution after pause."""
        self._paused = False
        record = AuditRecord(action=OperatorAction.RESUME, operator=operator, reason=reason)
        self._audit_log.append(record)
        logger.info("OPERATOR_CONTROL action=RESUME operator=%s reason=%s", operator, reason)
        return record

    def quarantine(self, symbol: str, operator: str = "operator", reason: str = "") -> AuditRecord:
        """Quarantine a symbol. Blocks activation and corrective actions for this symbol."""
        self._quarantined.add(symbol)
        record = AuditRecord(
            action=OperatorAction.QUARANTINE,
            symbol=symbol,
            operator=operator,
            reason=reason,
        )
        self._audit_log.append(record)
        logger.info(
            "OPERATOR_CONTROL action=QUARANTINE symbol=%s operator=%s reason=%s",
            symbol,
            operator,
            reason,
        )
        return record

    def release_quarantine(
        self, symbol: str, operator: str = "operator", reason: str = ""
    ) -> AuditRecord:
        """Release quarantine for a symbol. Does NOT auto-activate."""
        self._quarantined.discard(symbol)
        record = AuditRecord(
            action=OperatorAction.RELEASE_QUARANTINE,
            symbol=symbol,
            operator=operator,
            reason=reason,
        )
        self._audit_log.append(record)
        logger.info(
            "OPERATOR_CONTROL action=RELEASE_QUARANTINE symbol=%s operator=%s reason=%s",
            symbol,
            operator,
            reason,
        )
        return record

    def force_deactivate(
        self, symbol: str, operator: str = "operator", reason: str = ""
    ) -> AuditRecord:
        """Request force deactivation for a symbol.

        Does not immediately kill the engine. Adds to pending set for
        the coordinator to process via the normal deactivation ceremony path.
        """
        self._force_deactivate_pending.add(symbol)
        record = AuditRecord(
            action=OperatorAction.FORCE_DEACTIVATE,
            symbol=symbol,
            operator=operator,
            reason=reason,
        )
        self._audit_log.append(record)
        logger.info(
            "OPERATOR_CONTROL action=FORCE_DEACTIVATE symbol=%s operator=%s reason=%s",
            symbol,
            operator,
            reason,
        )
        return record

    def clear_force_deactivate(self, symbol: str) -> None:
        """Clear force-deactivate intent after it has been processed."""
        self._force_deactivate_pending.discard(symbol)

    def is_activation_allowed(self, symbol: str) -> bool:
        """Check if activation is allowed for a symbol.

        Blocked if paused or symbol is quarantined.
        """
        if self._paused:
            return False
        return symbol not in self._quarantined

    def reset(self) -> None:
        """Reset all state (for testing)."""
        self._paused = False
        self._quarantined.clear()
        self._force_deactivate_pending.clear()
        self._audit_log.clear()
