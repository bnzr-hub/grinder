from typing import Any, cast

from grinder.risk.order_size_policy import (
    OrderSizeInputs,
    OrderSizePolicyConfig,
    compute_target_order_size,
)


def _cfg(**kw: float | bool) -> OrderSizePolicyConfig:
    base: dict[str, float | bool] = {
        "enabled": True,
        "flat_only": True,
        "update_cooldown_s": 60.0,
        "delta_threshold_pct": 10.0,
        "base_size": 100.0,
        "min_size": 20.0,
        "max_size": 400.0,
        "natr_ref_bps": 100.0,
        "step_ref_bps": 25.0,
        "vol_k": 1.0,
        "step_k": 0.5,
        "ml_enabled": False,
        "ml_adjust_max_pct": 80.0,
    }
    base.update(kw)
    return OrderSizePolicyConfig(**cast("dict[str, Any]", base))


def test_high_natr_reduces_size() -> None:
    cfg = _cfg()
    low_vol = compute_target_order_size(
        100.0,
        cfg,
        OrderSizeInputs(natr_bps=50, step_bps=25, risk_headroom_ratio=1.0),
    )
    high_vol = compute_target_order_size(
        100.0,
        cfg,
        OrderSizeInputs(natr_bps=300, step_bps=25, risk_headroom_ratio=1.0),
    )
    assert high_vol.target_size < low_vol.target_size


def test_larger_step_increases_size() -> None:
    cfg = _cfg()
    narrow = compute_target_order_size(
        100.0,
        cfg,
        OrderSizeInputs(natr_bps=100, step_bps=25, risk_headroom_ratio=1.0),
    )
    wide = compute_target_order_size(
        100.0,
        cfg,
        OrderSizeInputs(natr_bps=100, step_bps=100, risk_headroom_ratio=1.0),
    )
    assert wide.target_size > narrow.target_size


def test_ml_adjust_bounded() -> None:
    cfg = _cfg(ml_enabled=True, ml_adjust_max_pct=50.0)
    out = compute_target_order_size(
        100.0,
        cfg,
        OrderSizeInputs(
            natr_bps=100,
            step_bps=25,
            risk_headroom_ratio=1.0,
            ml_adjust_pct=200.0,
        ),
    )
    # capped at +50%
    assert out.ml_multiplier == 1.5


def test_delta_threshold_controls_change_flag() -> None:
    cfg = _cfg(delta_threshold_pct=20.0, base_size=100.0)
    out = compute_target_order_size(
        100.0,
        cfg,
        OrderSizeInputs(natr_bps=110, step_bps=25, risk_headroom_ratio=1.0),
    )
    assert not out.changed
