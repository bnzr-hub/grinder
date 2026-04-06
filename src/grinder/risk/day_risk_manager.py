"""DayRiskManager — day/session risk state for autonomous runtime.

Implements day-level PnL tracking and mode transitions per spec §9:
- NORMAL → DEFENSIVE at +3% day PnL
- DEFENSIVE → STOP_FOR_DAY on profit giveback breach
- Any → STOP_FOR_DAY on -10% day loss
- STOP_FOR_DAY is latched within a session (reset on new day)

Shadow-only in PR-B: no execution behavior change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

logger = logging.getLogger(__name__)


class DayRiskMode(Enum):
    """Day-level operating mode (spec §9.1)."""

    NORMAL = "NORMAL"
    DEFENSIVE = "DEFENSIVE"
    STOP_NEW_RISK = "STOP_NEW_RISK"
    FORCE_REDUCE = "FORCE_REDUCE"
    STOP_FOR_DAY = "STOP_FOR_DAY"


@dataclass(frozen=True)
class DayRiskConfig:
    """Day risk thresholds (spec §6.1)."""

    profit_lock_trigger_pct: Decimal = Decimal("3.0")
    profit_giveback_pct: Decimal = Decimal("1.0")
    daily_loss_limit_pct: Decimal = Decimal("10.0")


@dataclass(frozen=True)
class DayRiskState:
    """Snapshot of day risk state after an update."""

    mode: DayRiskMode
    equity_day_start: Decimal
    equity_day_peak: Decimal
    equity_current: Decimal
    day_pnl_pct: Decimal
    day_peak_pnl_pct: Decimal
    profit_lock_floor_pct: Decimal
    session_key: str


_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


class DayRiskManager:
    """Deterministic day-level risk state machine.

    Pure logic — no I/O. Call update(equity, session_key) each cycle.
    Resets automatically when session_key changes (new day boundary).
    Ignores non-positive equity until the first valid positive equity arrives.
    """

    def __init__(self, config: DayRiskConfig | None = None) -> None:
        self._config = config or DayRiskConfig()
        self._reset_state()
        self._session_key: str = ""

    @property
    def mode(self) -> DayRiskMode:
        return self._mode

    def is_stop_for_day(self) -> bool:
        return self._mode == DayRiskMode.STOP_FOR_DAY

    def is_defensive(self) -> bool:
        return self._mode == DayRiskMode.DEFENSIVE

    def reset_for_new_day(self, equity_start: Decimal | None = None) -> None:
        """Explicitly reset for a new trading day/session.

        Called by runtime on UTC day boundary or by session_key change.
        """
        self._reset_state()
        if equity_start is not None and equity_start > _ZERO:
            self._equity_day_start = equity_start
            self._equity_day_peak = equity_start
        logger.info(
            "DAY_RISK_RESET equity_start=%s",
            self._equity_day_start or "pending",
        )

    def update(self, equity: Decimal, session_key: str = "") -> DayRiskState:
        """Advance state with current account equity. Returns snapshot.

        Args:
            equity: Current account equity.
            session_key: Day/session identifier (e.g. "2026-04-06"). When this
                changes, the manager resets for a new day automatically.
        """
        # Auto-rollover on session key change
        if session_key and session_key != self._session_key:
            if self._session_key:  # not the very first call
                logger.info(
                    "DAY_RISK_SESSION_ROLLOVER old=%s new=%s",
                    self._session_key,
                    session_key,
                )
                self.reset_for_new_day(equity_start=equity if equity > _ZERO else None)
            self._session_key = session_key

        # Skip non-positive equity — wait for valid data
        if equity <= _ZERO:
            logger.debug("DAY_RISK_UPDATE_SKIPPED reason=non_positive_equity")
            return self._snapshot(equity)

        # Initialize on first valid positive equity
        if self._equity_day_start is None:
            self._equity_day_start = equity
            self._equity_day_peak = equity
            logger.info("DAY_RISK_INITIALIZED equity_start=%s session=%s", equity, session_key)

        # Latched — no further transitions within this session
        if self._mode == DayRiskMode.STOP_FOR_DAY:
            return self._snapshot(equity)

        # Update high-water mark
        self._equity_day_peak = max(self._equity_day_peak, equity)

        # Compute derived fields
        day_pnl_pct = (equity - self._equity_day_start) / self._equity_day_start * _HUNDRED
        day_peak_pnl_pct = (
            (self._equity_day_peak - self._equity_day_start) / self._equity_day_start * _HUNDRED
        )
        cfg = self._config

        # Hard loss stop — check first (highest priority)
        if day_pnl_pct <= -cfg.daily_loss_limit_pct:
            self._transition(DayRiskMode.STOP_FOR_DAY, reason="loss_limit", pnl=day_pnl_pct)
            return self._snapshot(equity)

        # Defensive trigger (before profit lock check — must enter DEFENSIVE first)
        if day_pnl_pct >= cfg.profit_lock_trigger_pct and self._mode == DayRiskMode.NORMAL:
            self._transition(DayRiskMode.DEFENSIVE, pnl=day_pnl_pct)

        # Profit lock path (only after DEFENSIVE has been entered)
        if day_peak_pnl_pct >= cfg.profit_lock_trigger_pct and self._profit_lock_armed:
            floor = max(cfg.profit_lock_trigger_pct, day_peak_pnl_pct - cfg.profit_giveback_pct)
            if day_pnl_pct <= floor:
                self._transition(
                    DayRiskMode.STOP_FOR_DAY, reason="profit_lock", pnl=day_pnl_pct, floor=floor
                )
                return self._snapshot(equity)

        # Arm profit lock after first DEFENSIVE transition
        if day_peak_pnl_pct >= cfg.profit_lock_trigger_pct and not self._profit_lock_armed:
            self._profit_lock_armed = True
            floor = max(cfg.profit_lock_trigger_pct, day_peak_pnl_pct - cfg.profit_giveback_pct)
            logger.info(
                "DAY_PROFIT_LOCK_ARMED floor_pct=%s peak_pnl_pct=%s", floor, day_peak_pnl_pct
            )

        return self._snapshot(equity)

    def _reset_state(self) -> None:
        """Reset all day state to initial values."""
        self._mode = DayRiskMode.NORMAL
        self._equity_day_start: Decimal | None = None
        self._equity_day_peak: Decimal = _ZERO
        self._profit_lock_armed = False

    def _transition(self, new_mode: DayRiskMode, **kwargs: object) -> None:
        old = self._mode
        self._mode = new_mode
        if new_mode == DayRiskMode.STOP_FOR_DAY:
            logger.info(
                "DAY_STOP_FOR_DAY_TRIGGERED reason=%s day_pnl_pct=%s",
                kwargs.get("reason", "unknown"),
                kwargs.get("pnl", "?"),
            )
        logger.info("DAY_RISK_MODE_CHANGED old=%s new=%s", old.value, new_mode.value)

    def _snapshot(self, equity: Decimal) -> DayRiskState:
        start = self._equity_day_start or _ZERO
        if start <= _ZERO:
            return DayRiskState(
                mode=self._mode,
                equity_day_start=start,
                equity_day_peak=self._equity_day_peak,
                equity_current=equity,
                day_pnl_pct=_ZERO,
                day_peak_pnl_pct=_ZERO,
                profit_lock_floor_pct=_ZERO,
                session_key=self._session_key,
            )
        day_pnl_pct = (equity - start) / start * _HUNDRED
        day_peak_pnl_pct = (self._equity_day_peak - start) / start * _HUNDRED
        cfg = self._config
        floor = (
            max(cfg.profit_lock_trigger_pct, day_peak_pnl_pct - cfg.profit_giveback_pct)
            if day_peak_pnl_pct >= cfg.profit_lock_trigger_pct
            else _ZERO
        )
        return DayRiskState(
            mode=self._mode,
            equity_day_start=start,
            equity_day_peak=self._equity_day_peak,
            equity_current=equity,
            day_pnl_pct=day_pnl_pct,
            day_peak_pnl_pct=day_peak_pnl_pct,
            profit_lock_floor_pct=floor,
            session_key=self._session_key,
        )
