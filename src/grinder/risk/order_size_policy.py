"""Grid v2 order-size policy (flat-only, deterministic core + optional ML adjust).

The policy computes a target order size from:
- base size
- volatility (NATR bps)
- effective grid step (bps)
- risk headroom ratio
- optional bounded ML multiplier

Policy itself is pure and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderSizePolicyConfig:
    enabled: bool = False
    flat_only: bool = True
    update_cooldown_s: float = 60.0
    delta_threshold_pct: float = 12.0
    base_size: float = 0.0
    min_size: float = 0.0
    max_size: float = 0.0
    natr_ref_bps: float = 100.0
    step_ref_bps: float = 25.0
    vol_k: float = 1.0
    step_k: float = 0.5
    ml_enabled: bool = False
    ml_adjust_max_pct: float = 80.0

    def __post_init__(self) -> None:
        if self.update_cooldown_s < 0:
            raise ValueError("update_cooldown_s must be >= 0")
        if self.delta_threshold_pct < 0:
            raise ValueError("delta_threshold_pct must be >= 0")
        if self.base_size <= 0:
            raise ValueError("base_size must be > 0")
        if self.min_size <= 0:
            raise ValueError("min_size must be > 0")
        if self.max_size < self.min_size:
            raise ValueError("max_size must be >= min_size")
        if self.natr_ref_bps <= 0:
            raise ValueError("natr_ref_bps must be > 0")
        if self.step_ref_bps <= 0:
            raise ValueError("step_ref_bps must be > 0")
        if self.vol_k < 0:
            raise ValueError("vol_k must be >= 0")
        if self.step_k < 0:
            raise ValueError("step_k must be >= 0")
        if self.ml_adjust_max_pct < 0:
            raise ValueError("ml_adjust_max_pct must be >= 0")


@dataclass(frozen=True)
class OrderSizeInputs:
    natr_bps: float | None
    step_bps: float
    risk_headroom_ratio: float
    ml_adjust_pct: float = 0.0


@dataclass(frozen=True)
class OrderSizeResult:
    target_size: float
    changed: bool
    delta_pct: float
    det_size: float
    ml_multiplier: float
    reason: str


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def compute_target_order_size(
    current_size: float,
    config: OrderSizePolicyConfig,
    inputs: OrderSizeInputs,
) -> OrderSizeResult:
    """Compute target size and whether update threshold is exceeded."""
    natr = inputs.natr_bps if inputs.natr_bps and inputs.natr_bps > 0 else config.natr_ref_bps
    vol_ratio = config.natr_ref_bps / max(natr, 1e-9)
    vol_factor = _clamp(vol_ratio, 0.25, 4.0) ** config.vol_k

    step_ratio = inputs.step_bps / config.step_ref_bps if config.step_ref_bps > 0 else 1.0
    step_factor = _clamp(step_ratio, 0.5, 2.0) ** config.step_k

    risk_factor = _clamp(inputs.risk_headroom_ratio, 0.1, 1.0)
    det_size = config.base_size * vol_factor * step_factor * risk_factor

    if config.ml_enabled and config.ml_adjust_max_pct > 0:
        bounded_ml = _clamp(
            inputs.ml_adjust_pct, -config.ml_adjust_max_pct, config.ml_adjust_max_pct
        )
        ml_mult = 1.0 + (bounded_ml / 100.0)
    else:
        ml_mult = 1.0

    target = det_size * ml_mult
    target = _clamp(target, config.min_size, config.max_size)

    delta_pct = 100.0 if current_size <= 0 else abs(target - current_size) / current_size * 100.0
    changed = delta_pct >= config.delta_threshold_pct

    return OrderSizeResult(
        target_size=target,
        changed=changed,
        delta_pct=delta_pct,
        det_size=det_size,
        ml_multiplier=ml_mult,
        reason="DELTA_THRESHOLD_MET" if changed else "DELTA_BELOW_THRESHOLD",
    )
