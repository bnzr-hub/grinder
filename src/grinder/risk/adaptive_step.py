"""Adaptive step controller v1 (volatility-aware grid spacing).

Computes effective grid step based on volatility input (NATR).
Bounded by min/max clamps, with hysteresis and cooldown to prevent oscillation.

v1 contract:
- Effective step applies to new rolling entries only (no mass recenter).
- fail-open: on missing/invalid vol → freeze_last or fallback_base.
- Deterministic: same inputs → same effective step.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class StepFailMode(Enum):
    FREEZE_LAST = "freeze_last"
    FALLBACK_BASE = "fallback_base"


@dataclass
class AdaptiveStepConfig:
    """Configuration for adaptive step controller."""

    enabled: bool = False
    step_min_pct: float = 0.0020
    step_max_pct: float = 0.0100
    step_base_pct: float = 0.0025  # fallback/base
    vol_ref_bps: float = 100.0  # normalization anchor
    update_cooldown_s: float = 60.0
    hysteresis_bps: float = 10.0
    fail_mode: StepFailMode = StepFailMode.FREEZE_LAST

    def __post_init__(self) -> None:
        if self.step_min_pct <= 0:
            raise ValueError(f"step_min_pct must be > 0, got {self.step_min_pct}")
        if self.step_max_pct < self.step_min_pct:
            raise ValueError(
                f"step_max_pct ({self.step_max_pct}) must be >= step_min_pct ({self.step_min_pct})"
            )
        if self.step_base_pct <= 0:
            raise ValueError(f"step_base_pct must be > 0, got {self.step_base_pct}")
        if self.vol_ref_bps <= 0:
            raise ValueError(f"vol_ref_bps must be > 0, got {self.vol_ref_bps}")
        if self.update_cooldown_s < 0:
            raise ValueError(f"update_cooldown_s must be >= 0, got {self.update_cooldown_s}")
        if self.hysteresis_bps < 0:
            raise ValueError(f"hysteresis_bps must be >= 0, got {self.hysteresis_bps}")


@dataclass
class StepUpdate:
    """Result of step evaluation."""

    effective_step_pct: float
    changed: bool = False
    reason: str = ""  # why it changed/didn't change


@dataclass
class _SymbolStepState:
    """Internal per-symbol step tracking."""

    current_step_pct: float = 0.0
    last_update_at: float = -999999.0
    last_vol_bps: float = 0.0


class AdaptiveStepController:
    """Per-symbol volatility-aware step controller.

    Thread-unsafe: designed for single engine tick loop.
    """

    def __init__(self, config: AdaptiveStepConfig) -> None:
        self._config = config
        self._states: dict[str, _SymbolStepState] = {}

    @property
    def config(self) -> AdaptiveStepConfig:
        return self._config

    def get_effective_step(self, symbol: str) -> float:
        """Get current effective step for symbol."""
        ss = self._states.get(symbol)
        if ss is None or ss.current_step_pct <= 0:
            return self._config.step_base_pct
        return ss.current_step_pct

    def evaluate(
        self,
        symbol: str,
        vol_bps: float | None,
        now: float | None = None,
    ) -> StepUpdate:
        """Evaluate and potentially update effective step.

        Args:
            vol_bps: Volatility in basis points (e.g., NATR_14_5m * 10000).
                     None = unavailable.

        Returns:
            StepUpdate with current effective step and change info.
        """
        if not self._config.enabled:
            return StepUpdate(effective_step_pct=self._config.step_base_pct)

        if now is None:
            now = time.monotonic()

        ss = self._states.get(symbol)
        if ss is None:
            ss = _SymbolStepState(current_step_pct=self._config.step_base_pct)
            self._states[symbol] = ss

        # Handle missing/invalid vol input
        if vol_bps is None or vol_bps <= 0:
            return self._handle_vol_missing(symbol, ss)

        ss.last_vol_bps = vol_bps

        # Cooldown check
        if (now - ss.last_update_at) < self._config.update_cooldown_s:
            return StepUpdate(
                effective_step_pct=ss.current_step_pct,
                reason="COOLDOWN",
            )

        # Compute candidate step: base * (vol / vol_ref)
        # Monotonic linear scaling — wider step in high vol, narrower in calm
        ratio = vol_bps / self._config.vol_ref_bps
        candidate = self._config.step_base_pct * ratio

        # Clamp to [min, max]
        candidate = max(self._config.step_min_pct, min(self._config.step_max_pct, candidate))

        # Hysteresis: only update if change exceeds threshold
        diff_bps = abs(candidate - ss.current_step_pct) * 10000
        if diff_bps < self._config.hysteresis_bps:
            return StepUpdate(
                effective_step_pct=ss.current_step_pct,
                reason="HYSTERESIS",
            )

        # Apply update
        old = ss.current_step_pct
        ss.current_step_pct = candidate
        ss.last_update_at = now

        logger.info(
            "GRID_V2_STEP_UPDATE_APPLIED symbol=%s old=%.6f new=%.6f vol_bps=%.1f ratio=%.3f",
            symbol,
            old,
            candidate,
            vol_bps,
            ratio,
        )

        return StepUpdate(
            effective_step_pct=candidate,
            changed=True,
            reason="VOL_UPDATE",
        )

    def _handle_vol_missing(self, symbol: str, ss: _SymbolStepState) -> StepUpdate:
        """Handle missing volatility input based on fail mode."""
        cfg = self._config
        if cfg.fail_mode == StepFailMode.FREEZE_LAST:
            effective = ss.current_step_pct if ss.current_step_pct > 0 else cfg.step_base_pct
            logger.debug(
                "GRID_V2_STEP_UPDATE_FAIL_FROZEN symbol=%s step=%.6f",
                symbol,
                effective,
            )
            return StepUpdate(effective_step_pct=effective, reason="FAIL_FROZEN")

        # FALLBACK_BASE
        logger.debug(
            "GRID_V2_STEP_UPDATE_FAIL_BASE_FALLBACK symbol=%s step=%.6f",
            symbol,
            cfg.step_base_pct,
        )
        return StepUpdate(effective_step_pct=cfg.step_base_pct, reason="FAIL_BASE_FALLBACK")
