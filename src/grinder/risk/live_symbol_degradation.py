"""LiveSymbolDegradationController — degrade already-live symbols on risk state change.

Evaluates active symbols against current day risk state and applies
a degradation ladder:

  STOP_NEW_RISK  → graceful-exit-only
  STOP_FOR_DAY   → graceful-exit-only
  FORCE_REDUCE   → graceful-exit-only + finalize deactivation (cancel + close)

Idempotent: tracks degraded/force-reduced symbols to avoid repeated requests.
Prunes symbols no longer live. Resets on day-session rollover.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.risk.day_risk_manager import DayRiskMode

logger = logging.getLogger(__name__)


class DegradationAction(Enum):
    """Target action for a degraded live symbol."""

    GRACEFUL_EXIT_ONLY = "GRACEFUL_EXIT_ONLY"
    FORCE_REDUCE = "FORCE_REDUCE"


class DegradationReason(Enum):
    """Why a live symbol was degraded."""

    DAY_STOP_NEW_RISK = "DAY_STOP_NEW_RISK"
    DAY_FORCE_REDUCE = "DAY_FORCE_REDUCE"
    DAY_STOP_FOR_DAY = "DAY_STOP_FOR_DAY"


def _day_mode_to_action(mode: DayRiskMode) -> tuple[DegradationAction, DegradationReason] | None:
    """Map DayRiskMode to (action, reason). Returns None if no degradation needed."""
    from grinder.risk.day_risk_manager import DayRiskMode as M  # noqa: PLC0415

    _MAP: dict[M, tuple[DegradationAction, DegradationReason]] = {
        M.STOP_NEW_RISK: (
            DegradationAction.GRACEFUL_EXIT_ONLY,
            DegradationReason.DAY_STOP_NEW_RISK,
        ),
        M.STOP_FOR_DAY: (DegradationAction.GRACEFUL_EXIT_ONLY, DegradationReason.DAY_STOP_FOR_DAY),
        M.FORCE_REDUCE: (DegradationAction.FORCE_REDUCE, DegradationReason.DAY_FORCE_REDUCE),
    }
    return _MAP.get(mode)


class LiveSymbolDegradationController:
    """Manages degradation transitions for already-live symbols.

    Degradation ladder:
    - GRACEFUL_EXIT_ONLY: no new entries, wait for natural exit
    - FORCE_REDUCE: graceful exit + finalize deactivation (cancel + close)

    Delegates to existing host methods — no direct engine control.
    Tracks both levels separately for proper escalation.
    """

    def __init__(self) -> None:
        self._degraded: set[str] = set()  # graceful-exit requested
        self._force_reduced: set[str] = set()  # finalize-deactivation requested

    @property
    def degraded_symbols(self) -> frozenset[str]:
        """Symbols with graceful-exit-only requested."""
        return frozenset(self._degraded)

    @property
    def force_reduced_symbols(self) -> frozenset[str]:
        """Symbols with force-reduce (finalize deactivation) requested."""
        return frozenset(self._force_reduced)

    def evaluate(
        self,
        live_symbols: frozenset[str],
        day_mode: DayRiskMode,
        graceful_exit_fn: object,
        deactivate_fn: object | None = None,
    ) -> list[str]:
        """Evaluate live symbols and apply degradation ladder.

        Args:
            live_symbols: Currently active engine symbols.
            day_mode: Current DayRiskMode from DayRiskManager.
            graceful_exit_fn: Callable(symbol) → GracefulExitResult.
            deactivate_fn: Callable(symbol) → bool. For FORCE_REDUCE.

        Returns:
            List of symbols newly degraded/force-reduced this cycle.
        """
        # Prune symbols no longer live (deactivated/re-activatable)
        stale = self._degraded - live_symbols
        if stale:
            self._degraded -= stale
        stale_fr = self._force_reduced - live_symbols
        if stale_fr:
            self._force_reduced -= stale_fr
        if stale or stale_fr:
            logger.debug(
                "LIVE_SYMBOL_DEGRADATION_PRUNED degraded=%s force_reduced=%s",
                ",".join(sorted(stale)) or "none",
                ",".join(sorted(stale_fr)) or "none",
            )

        action_pair = _day_mode_to_action(day_mode)
        if action_pair is None:
            return []

        action, reason = action_pair
        newly_acted: list[str] = []

        for symbol in sorted(live_symbols):
            if action == DegradationAction.GRACEFUL_EXIT_ONLY:
                if symbol in self._degraded:
                    continue
                if self._request_graceful_exit(symbol, reason, graceful_exit_fn):
                    newly_acted.append(symbol)

            elif action == DegradationAction.FORCE_REDUCE:
                # Step 1: graceful exit first (if not already done)
                if symbol not in self._degraded:
                    self._request_graceful_exit(symbol, reason, graceful_exit_fn)

                # Step 2: finalize deactivation (if not already done)
                if symbol not in self._force_reduced and self._request_force_reduce(
                    symbol, reason, deactivate_fn
                ):
                        newly_acted.append(symbol)

        return newly_acted

    def _request_graceful_exit(self, symbol: str, reason: DegradationReason, fn: object) -> bool:
        """Request graceful exit. Returns True if latched successfully."""
        try:
            result = fn(symbol)  # type: ignore[operator]
            result_val = result.value if hasattr(result, "value") else str(result)
            logger.info(
                "LIVE_SYMBOL_DEGRADED symbol=%s target=GRACEFUL_EXIT_ONLY reason=%s exit_result=%s",
                symbol,
                reason.value,
                result_val,
            )
            if hasattr(result, "value") and result.value == "FAILED":
                logger.warning(
                    "LIVE_SYMBOL_DEGRADATION_RETRY symbol=%s reason=exit_failed",
                    symbol,
                )
                return False
        except Exception as e:
            logger.error(
                "LIVE_SYMBOL_DEGRADATION_FAILED symbol=%s reason=%s error=%s",
                symbol,
                reason.value,
                e,
            )
            return False

        self._degraded.add(symbol)
        return True

    def _request_force_reduce(
        self, symbol: str, reason: DegradationReason, fn: object | None
    ) -> bool:
        """Request force-reduce (finalize deactivation). Returns True if latched."""
        if fn is None:
            logger.warning(
                "LIVE_SYMBOL_FORCE_REDUCE_SKIPPED symbol=%s reason=no_deactivate_fn",
                symbol,
            )
            return False

        try:
            ok = fn(symbol)  # type: ignore[operator]
            logger.info(
                "LIVE_SYMBOL_FORCE_REDUCE symbol=%s reason=%s ok=%s",
                symbol,
                reason.value,
                ok,
            )
            if not ok:
                logger.warning(
                    "LIVE_SYMBOL_FORCE_REDUCE_RETRY symbol=%s reason=deactivation_failed",
                    symbol,
                )
                return False
        except Exception as e:
            logger.error(
                "LIVE_SYMBOL_FORCE_REDUCE_FAILED symbol=%s reason=%s error=%s",
                symbol,
                reason.value,
                e,
            )
            return False

        self._force_reduced.add(symbol)
        return True

    def reset(self) -> None:
        """Reset degradation state (e.g. on new day session)."""
        if self._degraded or self._force_reduced:
            logger.info(
                "LIVE_SYMBOL_DEGRADATION_RESET degraded=%s force_reduced=%s",
                ",".join(sorted(self._degraded)) or "none",
                ",".join(sorted(self._force_reduced)) or "none",
            )
        self._degraded.clear()
        self._force_reduced.clear()
