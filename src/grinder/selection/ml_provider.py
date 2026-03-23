"""ML adjust provider for selector scoring — ONNX regime model integration.

Translates OnnxMlModel predictions into per-symbol adjust_bps for selector scoring.

Regime → adjust mapping (deterministic, integer):
    HIGH → +adjust_base_bps  (volatile = good for grid)
    MID  → 0
    LOW  → -adjust_base_bps  (calm = bad for grid)

adjust_base_bps defaults to ml_adjust_max_bps / 2 (half the cap as base signal).
Final bounding is handled by the selector's clamping logic.

Fail-open: any error → empty dict (fallback to baseline scoring in selector).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grinder.features.types import FeatureSnapshot
    from grinder.ml.onnx.model import OnnxMlModel

logger = logging.getLogger(__name__)

# Regime → sign mapping (deterministic)
_REGIME_SIGN: dict[str, int] = {
    "HIGH": 1,
    "MID": 0,
    "LOW": -1,
}


def build_ml_adjust_provider(
    model: OnnxMlModel,
    feature_cache: dict[str, FeatureSnapshot],
    adjust_base_bps: int,
) -> Any:
    """Build a closure that returns per-symbol ML adjust_bps.

    Args:
        model: Loaded OnnxMlModel instance.
        feature_cache: Reference to selector's feature cache (mutable, updated each tick).
        adjust_base_bps: Base adjustment magnitude in bps. HIGH → +base, LOW → -base.

    Returns:
        Callable[[], dict[str, int]] suitable for ShadowSelector/ActiveSelector.
    """

    def _provider() -> dict[str, int]:
        adjusts: dict[str, int] = {}
        for symbol, feat in feature_cache.items():
            policy_features = _feature_to_policy(feat)
            signal = model.predict(feat.ts, symbol, policy_features)
            if signal is None:
                continue
            sign = _REGIME_SIGN.get(signal.predicted_regime, 0)
            adjusts[symbol] = sign * adjust_base_bps
        return adjusts

    return _provider


def _feature_to_policy(feat: FeatureSnapshot) -> dict[str, Any]:
    """Convert FeatureSnapshot to policy_features dict for ONNX model.

    Maps existing feature fields to the model's expected input names.
    Uses integer values for determinism.
    """
    return {
        "natr_bps": feat.natr_bps,
        "spread_bps": feat.spread_bps,
        "range_score": feat.range_score,
        "net_return_bps": feat.net_return_bps,
        "imbalance_l1_bps": feat.imbalance_l1_bps,
        "warmup_bars": feat.warmup_bars,
    }
