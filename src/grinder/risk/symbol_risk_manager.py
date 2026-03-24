"""Per-symbol risk manager v1 (grid_v2-first).

Monitors symbol-local risk signals and escalates mode:
    NORMAL -> CAPPED -> EXIT_ONLY

CAPPED: blocks INCREASE_RISK for the symbol.
EXIT_ONLY: blocks all new entries; only exits/cancels/reduce-only allowed.

De-escalation requires cooldown + conditions back in safe range.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

logger = logging.getLogger(__name__)


class SymbolRiskState(Enum):
    """Per-symbol risk escalation state."""

    NORMAL = "NORMAL"
    CAPPED = "CAPPED"
    EXIT_ONLY = "EXIT_ONLY"


@dataclass
class SymbolRiskConfig:
    """Configuration for per-symbol risk manager."""

    enabled: bool = False
    max_notional_usd: float = 0.0  # 0 = disabled
    max_position_qty: float = 0.0  # 0 = disabled
    max_consecutive_losses: int = 0  # 0 = disabled
    cooldown_s: float = 120.0
    exit_only_ttl_s: float = 300.0
    applies_to_grid_v2_only: bool = True

    def __post_init__(self) -> None:
        if self.cooldown_s < 0:
            raise ValueError(f"cooldown_s must be >= 0, got {self.cooldown_s}")
        if self.exit_only_ttl_s < 0:
            raise ValueError(f"exit_only_ttl_s must be >= 0, got {self.exit_only_ttl_s}")
        if self.max_consecutive_losses < 0:
            raise ValueError(
                f"max_consecutive_losses must be >= 0, got {self.max_consecutive_losses}"
            )


@dataclass
class SymbolRiskSnapshot:
    """Current risk measurements for a symbol."""

    position_notional_usd: float = 0.0
    position_qty: float = 0.0
    consecutive_losses: int = 0


@dataclass
class _SymbolState:
    """Internal per-symbol tracking state."""

    state: SymbolRiskState = SymbolRiskState.NORMAL
    escalated_at: float = 0.0
    last_transition_at: float = 0.0


@dataclass
class SymbolRiskDecision:
    """Result of risk evaluation for a symbol."""

    state: SymbolRiskState
    escalation_reason: str = ""  # non-empty if just escalated
    deescalation_reason: str = ""  # non-empty if just de-escalated


class SymbolRiskManager:
    """Per-symbol risk controller.

    Thread-unsafe: designed to be called from single engine tick loop.
    """

    def __init__(self, config: SymbolRiskConfig) -> None:
        self._config = config
        self._states: dict[str, _SymbolState] = {}

    @property
    def config(self) -> SymbolRiskConfig:
        return self._config

    def get_state(self, symbol: str) -> SymbolRiskState:
        """Get current risk state for symbol."""
        s = self._states.get(symbol)
        return s.state if s else SymbolRiskState.NORMAL

    def evaluate(
        self,
        symbol: str,
        snapshot: SymbolRiskSnapshot,
        now: float | None = None,
    ) -> SymbolRiskDecision:
        """Evaluate risk for symbol and update state.

        Returns decision with current state and any transition reasons.
        """
        if not self._config.enabled:
            return SymbolRiskDecision(state=SymbolRiskState.NORMAL)

        if now is None:
            now = time.monotonic()

        ss = self._states.get(symbol)
        if ss is None:
            ss = _SymbolState()
            self._states[symbol] = ss

        # Check escalation triggers
        escalation_reason = self._check_escalation(snapshot)

        if escalation_reason:
            target = self._escalation_target(ss.state, escalation_reason)
            if target != ss.state:
                logger.warning(
                    "SYMBOL_RISK_ESCALATED symbol=%s from=%s to=%s reason=%s "
                    "notional=%.2f qty=%.4f losses=%d",
                    symbol,
                    ss.state.value,
                    target.value,
                    escalation_reason,
                    snapshot.position_notional_usd,
                    snapshot.position_qty,
                    snapshot.consecutive_losses,
                )
                ss.state = target
                ss.escalated_at = now
                ss.last_transition_at = now
                return SymbolRiskDecision(state=ss.state, escalation_reason=escalation_reason)
        elif ss.state != SymbolRiskState.NORMAL:
            # Check de-escalation
            deesc_reason = self._check_deescalation(ss, snapshot, now)
            if deesc_reason:
                old = ss.state
                ss.state = self._deescalation_target(ss.state)
                ss.last_transition_at = now
                if ss.state == SymbolRiskState.NORMAL:
                    ss.escalated_at = 0.0
                logger.info(
                    "SYMBOL_RISK_DEESCALATED symbol=%s from=%s to=%s reason=%s",
                    symbol,
                    old.value,
                    ss.state.value,
                    deesc_reason,
                )
                return SymbolRiskDecision(state=ss.state, deescalation_reason=deesc_reason)

        return SymbolRiskDecision(state=ss.state)

    def record_lot_closed(
        self, symbol: str, entry_price: Decimal, exit_price: Decimal, side: str
    ) -> None:
        """Record a closed lot for consecutive loss tracking.

        Loss definition (v1):
        - LONG: exit_price < entry_price
        - SHORT: exit_price > entry_price
        """
        is_loss = (side == "LONG" and exit_price < entry_price) or (
            side == "SHORT" and exit_price > entry_price
        )
        ss = self._states.get(symbol)
        if ss is None:
            ss = _SymbolState()
            self._states[symbol] = ss

        # We don't track consecutive losses in _SymbolState directly —
        # the caller (engine) tracks via SymbolRiskSnapshot.consecutive_losses.
        # This method is a hook for future direct tracking if needed.
        if is_loss:
            logger.debug(
                "SYMBOL_RISK_LOT_LOSS symbol=%s side=%s entry=%s exit=%s",
                symbol,
                side,
                entry_price,
                exit_price,
            )

    def _check_escalation(self, snap: SymbolRiskSnapshot) -> str:
        """Return escalation reason or empty string."""
        cfg = self._config
        if cfg.max_notional_usd > 0 and snap.position_notional_usd > cfg.max_notional_usd:
            return "NOTIONAL_EXCEEDED"
        if cfg.max_position_qty > 0 and snap.position_qty > cfg.max_position_qty:
            return "QTY_EXCEEDED"
        if cfg.max_consecutive_losses > 0 and snap.consecutive_losses >= cfg.max_consecutive_losses:
            return "CONSECUTIVE_LOSSES"
        return ""

    @staticmethod
    def _escalation_target(current: SymbolRiskState, reason: str) -> SymbolRiskState:
        """Determine target state on escalation (monotonic upward)."""
        if reason == "CONSECUTIVE_LOSSES":
            return SymbolRiskState.EXIT_ONLY
        if current == SymbolRiskState.NORMAL:
            return SymbolRiskState.CAPPED
        if current == SymbolRiskState.CAPPED:
            return SymbolRiskState.EXIT_ONLY
        return current  # already EXIT_ONLY

    def _check_deescalation(self, ss: _SymbolState, snap: SymbolRiskSnapshot, now: float) -> str:
        """Return de-escalation reason or empty string."""
        cfg = self._config

        # EXIT_ONLY TTL must pass before any de-escalation from EXIT_ONLY
        if ss.state == SymbolRiskState.EXIT_ONLY and (now - ss.escalated_at) < cfg.exit_only_ttl_s:
            return ""

        # Cooldown since last transition
        if (now - ss.last_transition_at) < cfg.cooldown_s:
            return ""

        # All triggers must be back in safe range
        if cfg.max_notional_usd > 0 and snap.position_notional_usd > cfg.max_notional_usd:
            return ""
        if cfg.max_position_qty > 0 and snap.position_qty > cfg.max_position_qty:
            return ""
        if cfg.max_consecutive_losses > 0 and snap.consecutive_losses >= cfg.max_consecutive_losses:
            return ""

        return "CONDITIONS_SAFE"

    @staticmethod
    def _deescalation_target(current: SymbolRiskState) -> SymbolRiskState:
        """Step down one level."""
        if current == SymbolRiskState.EXIT_ONLY:
            return SymbolRiskState.CAPPED
        if current == SymbolRiskState.CAPPED:
            return SymbolRiskState.NORMAL
        return SymbolRiskState.NORMAL
