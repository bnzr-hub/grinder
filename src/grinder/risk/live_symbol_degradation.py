"""LiveSymbolDegradationController — degrade already-live symbols on risk state change.

Evaluates active symbols against current day risk state and requests
graceful-exit-only when conditions degrade. Does NOT force-close, does NOT
modify engine geometry, does NOT trigger staged unload (that's PR 2).

Day-mode-driven triggers (PR 1):
- STOP_NEW_RISK  → graceful-exit-only
- FORCE_REDUCE   → graceful-exit-only (unload wiring deferred)
- STOP_FOR_DAY   → graceful-exit-only

Idempotent: tracks already-degraded symbols to avoid repeated requests/logs.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.risk.day_risk_manager import DayRiskMode

logger = logging.getLogger(__name__)

# Day modes that require degradation of already-live symbols
_DEGRADE_MODES: frozenset[str] = frozenset(
    {
        "STOP_NEW_RISK",
        "FORCE_REDUCE",
        "STOP_FOR_DAY",
    }
)


class DegradationReason(Enum):
    """Why a live symbol was degraded."""

    DAY_STOP_NEW_RISK = "DAY_STOP_NEW_RISK"
    DAY_FORCE_REDUCE = "DAY_FORCE_REDUCE"
    DAY_STOP_FOR_DAY = "DAY_STOP_FOR_DAY"


def _day_mode_to_reason(mode: DayRiskMode) -> DegradationReason | None:
    """Map DayRiskMode to degradation reason. Returns None if no degradation needed."""
    _MAP = {
        "STOP_NEW_RISK": DegradationReason.DAY_STOP_NEW_RISK,
        "FORCE_REDUCE": DegradationReason.DAY_FORCE_REDUCE,
        "STOP_FOR_DAY": DegradationReason.DAY_STOP_FOR_DAY,
    }
    return _MAP.get(mode.value)


class LiveSymbolDegradationController:
    """Manages graceful-exit-only transitions for already-live symbols.

    Pure orchestration — delegates actual exit to host.request_graceful_exit().
    Tracks degraded symbols for idempotency (no repeated requests/logs).
    """

    def __init__(self) -> None:
        self._degraded: set[str] = set()

    @property
    def degraded_symbols(self) -> frozenset[str]:
        """Currently degraded symbols (for observability)."""
        return frozenset(self._degraded)

    def evaluate(
        self,
        live_symbols: frozenset[str],
        day_mode: DayRiskMode,
        graceful_exit_fn: object,
    ) -> list[str]:
        """Evaluate live symbols and degrade if day mode requires it.

        Args:
            live_symbols: Currently active engine symbols.
            day_mode: Current DayRiskMode from DayRiskManager.
            graceful_exit_fn: Callable(symbol) → GracefulExitResult.

        Returns:
            List of symbols newly degraded this cycle (for logging/testing).
        """
        reason = _day_mode_to_reason(day_mode)
        if reason is None:
            return []

        newly_degraded: list[str] = []
        for symbol in sorted(live_symbols):
            if symbol in self._degraded:
                continue

            # Request graceful exit via host
            try:
                result = graceful_exit_fn(symbol)  # type: ignore[operator]
                logger.info(
                    "LIVE_SYMBOL_DEGRADED symbol=%s target=GRACEFUL_EXIT_ONLY"
                    " reason=%s exit_result=%s",
                    symbol,
                    reason.value,
                    result.value if hasattr(result, "value") else str(result),
                )
            except Exception as e:
                logger.error(
                    "LIVE_SYMBOL_DEGRADATION_FAILED symbol=%s reason=%s error=%s",
                    symbol,
                    reason.value,
                    e,
                )

            self._degraded.add(symbol)
            newly_degraded.append(symbol)

        return newly_degraded

    def reset(self) -> None:
        """Reset degradation state (e.g. on new day session)."""
        if self._degraded:
            logger.info(
                "LIVE_SYMBOL_DEGRADATION_RESET previously_degraded=%s",
                ",".join(sorted(self._degraded)),
            )
        self._degraded.clear()
