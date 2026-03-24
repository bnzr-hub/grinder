"""Per-symbol position unload controller v1.

Staged reduce-only unload while symbol is in EXIT_ONLY state.
Produces at most one reduce-only order per step, with cooldown and rate limits.

Contract:
- Precondition: symbol must be in EXIT_ONLY (from SymbolRiskManager).
- Produces reduce-only PLACE actions only.
- Never increases risk.
- On repeated failure: pauses unload, stays EXIT_ONLY.
- On flat: emits UNLOAD_COMPLETE, clears state.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class UnloadStatus(Enum):
    """Per-symbol unload state."""

    INACTIVE = "INACTIVE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"


@dataclass
class UnloadConfig:
    """Configuration for position unload controller."""

    enabled: bool = False
    step_pct: float = 0.20  # unload 20% of position per step
    min_step_qty: float = 0.0  # 0 = derive from constraints
    step_cooldown_s: float = 20.0
    max_steps_per_hour: int = 60
    order_ttl_s: float = 60.0  # cancel+reprice after this
    max_reprices_per_step: int = 3
    reprice_max_bps: int = 30  # max drift before reprice
    exit_only_required: bool = True

    def __post_init__(self) -> None:
        if self.step_pct <= 0 or self.step_pct > 1.0:
            raise ValueError(f"step_pct must be in (0, 1.0], got {self.step_pct}")
        if self.step_cooldown_s < 0:
            raise ValueError(f"step_cooldown_s must be >= 0, got {self.step_cooldown_s}")
        if self.max_steps_per_hour < 0:
            raise ValueError(f"max_steps_per_hour must be >= 0, got {self.max_steps_per_hour}")
        if self.order_ttl_s < 0:
            raise ValueError(f"order_ttl_s must be >= 0, got {self.order_ttl_s}")
        if self.max_reprices_per_step < 0:
            raise ValueError(
                f"max_reprices_per_step must be >= 0, got {self.max_reprices_per_step}"
            )


@dataclass
class UnloadStep:
    """A single unload step action request."""

    symbol: str
    side: str  # "BUY" or "SELL" (reduce direction)
    qty: float
    reason: str = "SYMBOL_UNLOAD_STEP"


@dataclass
class _SymbolUnloadState:
    """Internal per-symbol unload tracking."""

    status: UnloadStatus = UnloadStatus.INACTIVE
    steps_this_hour: int = 0
    hour_start: float = 0.0
    last_step_at: float = -999999.0  # allows immediate first step
    pending_order_placed_at: float = 0.0
    pending_reprices: int = 0
    consecutive_failures: int = 0


class SymbolUnloadController:
    """Per-symbol staged reduce-only unload.

    Thread-unsafe: designed for single engine tick loop.
    """

    _MAX_CONSECUTIVE_FAILURES = 3  # pause after this many

    def __init__(self, config: UnloadConfig) -> None:
        self._config = config
        self._states: dict[str, _SymbolUnloadState] = {}

    @property
    def config(self) -> UnloadConfig:
        return self._config

    def get_status(self, symbol: str) -> UnloadStatus:
        s = self._states.get(symbol)
        return s.status if s else UnloadStatus.INACTIVE

    def activate(self, symbol: str, now: float | None = None) -> None:
        """Start unload for symbol (called when entering EXIT_ONLY)."""
        if not self._config.enabled:
            return
        if now is None:
            now = time.monotonic()
        ss = self._states.get(symbol)
        if ss is None:
            ss = _SymbolUnloadState()
            self._states[symbol] = ss
        if ss.status in (UnloadStatus.INACTIVE, UnloadStatus.COMPLETE):
            ss.status = UnloadStatus.ACTIVE
            ss.consecutive_failures = 0
            ss.pending_reprices = 0
            logger.warning("SYMBOL_UNLOAD_STARTED symbol=%s", symbol)

    def try_step(  # noqa: PLR0911
        self,
        symbol: str,
        signed_qty: float,
        now: float | None = None,
    ) -> UnloadStep | None:
        """Evaluate whether to produce an unload step.

        Args:
            signed_qty: Positive = LONG, negative = SHORT, zero = flat.

        Returns:
            UnloadStep if action needed, None otherwise.
        """
        if not self._config.enabled:
            return None
        if now is None:
            now = time.monotonic()

        ss = self._states.get(symbol)
        if ss is None or ss.status != UnloadStatus.ACTIVE:
            return None

        # Flat check
        if abs(signed_qty) < 1e-12:
            ss.status = UnloadStatus.COMPLETE
            logger.info("SYMBOL_UNLOAD_COMPLETE symbol=%s", symbol)
            return None

        # Hourly rate limit
        if now - ss.hour_start >= 3600:
            ss.hour_start = now
            ss.steps_this_hour = 0
        if (
            self._config.max_steps_per_hour > 0
            and ss.steps_this_hour >= self._config.max_steps_per_hour
        ):
            logger.debug(
                "SYMBOL_UNLOAD_PAUSED_RATE_LIMIT symbol=%s steps=%d",
                symbol,
                ss.steps_this_hour,
            )
            return None

        # Step cooldown
        if (now - ss.last_step_at) < self._config.step_cooldown_s:
            return None

        # Compute step qty
        abs_qty = abs(signed_qty)
        step_qty = max(abs_qty * self._config.step_pct, self._config.min_step_qty)
        step_qty = min(step_qty, abs_qty)  # don't overshoot

        if step_qty <= 0:
            return None

        # Determine reduce side
        side = "SELL" if signed_qty > 0 else "BUY"

        ss.last_step_at = now
        ss.steps_this_hour += 1

        logger.info(
            "SYMBOL_UNLOAD_STEP_PLANNED symbol=%s side=%s qty=%.6f abs_pos=%.6f step_pct=%.2f",
            symbol,
            side,
            step_qty,
            abs_qty,
            self._config.step_pct,
        )

        return UnloadStep(symbol=symbol, side=side, qty=step_qty)

    def record_step_success(self, symbol: str) -> None:
        """Record successful step execution."""
        ss = self._states.get(symbol)
        if ss is not None:
            ss.consecutive_failures = 0
            ss.pending_reprices = 0

    def record_step_failure(self, symbol: str) -> None:
        """Record failed step. Pauses after too many consecutive failures."""
        ss = self._states.get(symbol)
        if ss is None:
            return
        ss.consecutive_failures += 1
        if ss.consecutive_failures >= self._MAX_CONSECUTIVE_FAILURES:
            ss.status = UnloadStatus.PAUSED
            logger.warning(
                "SYMBOL_UNLOAD_STEP_STALLED symbol=%s failures=%d -> PAUSED",
                symbol,
                ss.consecutive_failures,
            )

    def should_reprice(self, symbol: str, order_placed_at: float, now: float | None = None) -> bool:
        """Check if pending unload order should be repriced (TTL expired)."""
        if not self._config.enabled:
            return False
        if now is None:
            now = time.monotonic()
        ss = self._states.get(symbol)
        if ss is None or ss.status != UnloadStatus.ACTIVE:
            return False
        if (now - order_placed_at) < self._config.order_ttl_s:
            return False
        if ss.pending_reprices >= self._config.max_reprices_per_step:
            logger.warning(
                "SYMBOL_UNLOAD_REPRICE_EXHAUSTED symbol=%s reprices=%d",
                symbol,
                ss.pending_reprices,
            )
            self.record_step_failure(symbol)
            return False
        ss.pending_reprices += 1
        return True

    def clear(self, symbol: str) -> None:
        """Reset unload state for symbol."""
        self._states.pop(symbol, None)
