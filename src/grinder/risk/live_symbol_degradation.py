"""LiveSymbolDegradationController — degrade already-live symbols on risk state change.

Evaluates active symbols against current day risk state and applies
graceful-exit-only when conditions degrade:

  STOP_NEW_RISK  → graceful-exit-only
  STOP_FOR_DAY   → graceful-exit-only
  FORCE_REDUCE   → graceful-exit-only (staged unload requires engine-level integration)

Idempotent: tracks degraded symbols to avoid repeated requests.
Prunes symbols no longer live. Resets on day-session rollover.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.risk.day_risk_manager import DayRiskMode

logger = logging.getLogger(__name__)


class DegradationReason(Enum):
    """Why a live symbol was degraded."""

    DAY_STOP_NEW_RISK = "DAY_STOP_NEW_RISK"
    DAY_FORCE_REDUCE = "DAY_FORCE_REDUCE"
    DAY_STOP_FOR_DAY = "DAY_STOP_FOR_DAY"


def _day_mode_to_reason(mode: DayRiskMode) -> DegradationReason | None:
    """Map DayRiskMode to degradation reason. Returns None if no degradation needed."""
    from grinder.risk.day_risk_manager import DayRiskMode as M  # noqa: PLC0415

    _MAP: dict[M, DegradationReason] = {
        M.STOP_NEW_RISK: DegradationReason.DAY_STOP_NEW_RISK,
        M.STOP_FOR_DAY: DegradationReason.DAY_STOP_FOR_DAY,
        M.FORCE_REDUCE: DegradationReason.DAY_FORCE_REDUCE,
    }
    return _MAP.get(mode)


class LiveSymbolDegradationController:
    """Manages graceful-exit-only transitions for already-live symbols.

    All degradation triggers currently map to graceful-exit-only.
    Staged unload for FORCE_REDUCE requires engine-level SymbolUnloadController
    integration — deferred to a later PR.

    Delegates to existing host.request_graceful_exit() — no direct engine control.
    """

    def __init__(self) -> None:
        self._degraded: set[str] = set()

    @property
    def degraded_symbols(self) -> frozenset[str]:
        """Symbols with graceful-exit-only requested."""
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
            List of symbols newly degraded this cycle.
        """
        # Prune symbols no longer live (deactivated/re-activatable)
        stale = self._degraded - live_symbols
        if stale:
            self._degraded -= stale
            logger.debug(
                "LIVE_SYMBOL_DEGRADATION_PRUNED symbols=%s",
                ",".join(sorted(stale)),
            )

        reason = _day_mode_to_reason(day_mode)
        if reason is None:
            return []

        newly_degraded: list[str] = []
        for symbol in sorted(live_symbols):
            if symbol in self._degraded:
                continue

            # Request graceful exit via host — only latch on success
            try:
                result = graceful_exit_fn(symbol)  # type: ignore[operator]
                result_val = result.value if hasattr(result, "value") else str(result)
                logger.info(
                    "LIVE_SYMBOL_DEGRADED symbol=%s target=GRACEFUL_EXIT_ONLY"
                    " reason=%s exit_result=%s",
                    symbol,
                    reason.value,
                    result_val,
                )
                # Only latch if exit was accepted (SUCCESS or NOT_APPLICABLE)
                # FAILED → leave out of _degraded so next cycle retries
                if hasattr(result, "value") and result.value == "FAILED":
                    logger.warning(
                        "LIVE_SYMBOL_DEGRADATION_RETRY symbol=%s reason=exit_failed",
                        symbol,
                    )
                    continue
            except Exception as e:
                logger.error(
                    "LIVE_SYMBOL_DEGRADATION_FAILED symbol=%s reason=%s error=%s",
                    symbol,
                    reason.value,
                    e,
                )
                continue  # don't latch — retry next cycle

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
