"""Tests for adaptive step controller v1."""

from __future__ import annotations

import pytest

from grinder.risk.adaptive_step import (
    AdaptiveStepConfig,
    AdaptiveStepController,
    StepFailMode,
)


def _cfg(**kw: object) -> AdaptiveStepConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "step_min_pct": 0.0020,
        "step_max_pct": 0.0100,
        "step_base_pct": 0.0025,
        "vol_ref_bps": 100.0,
        "update_cooldown_s": 10.0,
        "hysteresis_bps": 5.0,
        "fail_mode": StepFailMode.FREEZE_LAST,
    }
    defaults.update(kw)
    return AdaptiveStepConfig(**defaults)  # type: ignore[arg-type]


class TestAdaptiveStepConfig:
    def test_default_disabled(self) -> None:
        cfg = AdaptiveStepConfig()
        assert cfg.enabled is False

    def test_invalid_min_raises(self) -> None:
        with pytest.raises(ValueError, match="step_min_pct"):
            AdaptiveStepConfig(step_min_pct=0)

    def test_max_less_than_min_raises(self) -> None:
        with pytest.raises(ValueError, match="step_max_pct"):
            AdaptiveStepConfig(step_min_pct=0.01, step_max_pct=0.005)

    def test_negative_cooldown_raises(self) -> None:
        with pytest.raises(ValueError, match="update_cooldown_s"):
            AdaptiveStepConfig(update_cooldown_s=-1)


class TestDisabled:
    """Test 1: enabled=false -> behavior unchanged."""

    def test_disabled_returns_base(self) -> None:
        ctrl = AdaptiveStepController(AdaptiveStepConfig(enabled=False))
        result = ctrl.evaluate("SYM", 200.0, now=0.0)
        assert result.effective_step_pct == pytest.approx(0.0025)
        assert not result.changed


class TestVolUp:
    """Test 2: vol up -> step widens within clamp."""

    def test_high_vol_widens_step(self) -> None:
        ctrl = AdaptiveStepController(_cfg())
        # vol = 200 bps, ref = 100 bps -> ratio = 2.0 -> candidate = 0.005
        result = ctrl.evaluate("SYM", 200.0, now=0.0)
        assert result.effective_step_pct == pytest.approx(0.005)
        assert result.changed
        assert result.reason == "VOL_UPDATE"


class TestVolDown:
    """Test 3: vol down -> step narrows within clamp."""

    def test_low_vol_narrows_step(self) -> None:
        ctrl = AdaptiveStepController(_cfg())
        # vol = 50 bps, ref = 100 -> ratio = 0.5 -> candidate = 0.00125
        # But clamped to min = 0.0020
        result = ctrl.evaluate("SYM", 50.0, now=0.0)
        assert result.effective_step_pct == pytest.approx(0.0020)
        assert result.changed


class TestClamp:
    """Test 4: clamp min/max enforced."""

    def test_clamp_max(self) -> None:
        ctrl = AdaptiveStepController(_cfg())
        # vol = 500 bps -> ratio = 5.0 -> candidate = 0.0125 > max 0.01
        result = ctrl.evaluate("SYM", 500.0, now=0.0)
        assert result.effective_step_pct == pytest.approx(0.01)

    def test_clamp_min(self) -> None:
        ctrl = AdaptiveStepController(_cfg())
        # vol = 10 bps -> ratio = 0.1 -> candidate = 0.00025 < min 0.002
        result = ctrl.evaluate("SYM", 10.0, now=0.0)
        assert result.effective_step_pct == pytest.approx(0.002)


class TestCooldown:
    """Test 5: cooldown blocks frequent updates."""

    def test_cooldown_blocks(self) -> None:
        ctrl = AdaptiveStepController(_cfg(update_cooldown_s=60.0))
        r1 = ctrl.evaluate("SYM", 200.0, now=0.0)
        assert r1.changed
        # Too soon
        r2 = ctrl.evaluate("SYM", 300.0, now=30.0)
        assert not r2.changed
        assert r2.reason == "COOLDOWN"
        # After cooldown
        r3 = ctrl.evaluate("SYM", 300.0, now=65.0)
        assert r3.changed


class TestHysteresis:
    """Test 6: hysteresis blocks tiny oscillations."""

    def test_small_change_blocked(self) -> None:
        ctrl = AdaptiveStepController(_cfg(hysteresis_bps=20.0))
        # First set a baseline
        ctrl.evaluate("SYM", 200.0, now=0.0)  # step = 0.005
        # Small vol change: 205 bps -> candidate = 0.005125
        # diff = 0.000125 = 1.25 bps < hysteresis 20 bps
        r = ctrl.evaluate("SYM", 205.0, now=15.0)
        assert not r.changed
        assert r.reason == "HYSTERESIS"

    def test_large_change_passes(self) -> None:
        ctrl = AdaptiveStepController(_cfg(hysteresis_bps=20.0))
        ctrl.evaluate("SYM", 200.0, now=0.0)  # step = 0.005
        # Big vol change: 300 bps -> candidate = 0.0075
        # diff = 0.0025 = 25 bps > hysteresis 20 bps
        r = ctrl.evaluate("SYM", 300.0, now=15.0)
        assert r.changed


class TestMissingVol:
    """Test 7/8: missing vol input -> freeze_last / fallback_base."""

    def test_freeze_last_default(self) -> None:
        ctrl = AdaptiveStepController(_cfg(fail_mode=StepFailMode.FREEZE_LAST))
        # Set a value first
        ctrl.evaluate("SYM", 200.0, now=0.0)
        # Now vol missing
        r = ctrl.evaluate("SYM", None, now=15.0)
        assert r.effective_step_pct == pytest.approx(0.005)
        assert r.reason == "FAIL_FROZEN"

    def test_fallback_base(self) -> None:
        ctrl = AdaptiveStepController(_cfg(fail_mode=StepFailMode.FALLBACK_BASE))
        ctrl.evaluate("SYM", 200.0, now=0.0)
        r = ctrl.evaluate("SYM", None, now=15.0)
        assert r.effective_step_pct == pytest.approx(0.0025)
        assert r.reason == "FAIL_BASE_FALLBACK"

    def test_zero_vol_treated_as_missing(self) -> None:
        ctrl = AdaptiveStepController(_cfg(fail_mode=StepFailMode.FREEZE_LAST))
        ctrl.evaluate("SYM", 200.0, now=0.0)
        r = ctrl.evaluate("SYM", 0.0, now=15.0)
        assert r.reason == "FAIL_FROZEN"


class TestDeterminism:
    """Test 9: deterministic on replayed identical inputs."""

    def test_same_inputs_same_outputs(self) -> None:
        results = []
        for _ in range(3):
            ctrl = AdaptiveStepController(_cfg(update_cooldown_s=0, hysteresis_bps=0))
            steps = []
            for i, vol in enumerate([100, 150, 200, 100, 50]):
                r = ctrl.evaluate("SYM", float(vol), now=float(i))
                steps.append((r.effective_step_pct, r.changed))
            results.append(steps)
        assert results[0] == results[1] == results[2]


class TestNoRegression:
    """Test 10: no regression in recovery pathways."""

    def test_get_effective_step_without_evaluate(self) -> None:
        ctrl = AdaptiveStepController(_cfg())
        # Before any evaluate, returns base
        assert ctrl.get_effective_step("SYM") == pytest.approx(0.0025)
