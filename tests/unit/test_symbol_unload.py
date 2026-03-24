"""Tests for per-symbol position unload controller v1."""

from __future__ import annotations

import pytest

from grinder.risk.symbol_unload import (
    SymbolUnloadController,
    UnloadConfig,
    UnloadStatus,
)


def _cfg(**kw: object) -> UnloadConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "step_pct": 0.20,
        "min_step_qty": 0.001,
        "step_cooldown_s": 5.0,
        "max_steps_per_hour": 60,
        "order_ttl_s": 30.0,
        "max_reprices_per_step": 3,
        "reprice_max_bps": 30,
        "exit_only_required": True,
    }
    defaults.update(kw)
    return UnloadConfig(**defaults)  # type: ignore[arg-type]


class TestUnloadConfig:
    def test_default_disabled(self) -> None:
        cfg = UnloadConfig()
        assert cfg.enabled is False

    def test_invalid_step_pct_raises(self) -> None:
        with pytest.raises(ValueError, match="step_pct"):
            UnloadConfig(step_pct=0)

    def test_invalid_step_pct_over_1_raises(self) -> None:
        with pytest.raises(ValueError, match="step_pct"):
            UnloadConfig(step_pct=1.5)

    def test_negative_cooldown_raises(self) -> None:
        with pytest.raises(ValueError, match="step_cooldown_s"):
            UnloadConfig(step_cooldown_s=-1)


class TestExitOnlyPrecondition:
    """Test 1: unload only works when activated (EXIT_ONLY state)."""

    def test_inactive_produces_no_step(self) -> None:
        ctrl = SymbolUnloadController(_cfg())
        # Not activated — should produce nothing
        step = ctrl.try_step("SYM", 10.0, now=0.0)
        assert step is None
        assert ctrl.get_status("SYM") == UnloadStatus.INACTIVE


class TestLongPositionUnload:
    """Test 2: LONG position → SELL reduce-only."""

    def test_long_produces_sell(self) -> None:
        ctrl = SymbolUnloadController(_cfg())
        ctrl.activate("SYM", now=0.0)
        step = ctrl.try_step("SYM", 10.0, now=1.0)
        assert step is not None
        assert step.side == "SELL"
        assert step.qty == pytest.approx(2.0)  # 20% of 10


class TestShortPositionUnload:
    """Test 3: SHORT position → BUY reduce-only."""

    def test_short_produces_buy(self) -> None:
        ctrl = SymbolUnloadController(_cfg())
        ctrl.activate("SYM", now=0.0)
        step = ctrl.try_step("SYM", -10.0, now=1.0)
        assert step is not None
        assert step.side == "BUY"
        assert step.qty == pytest.approx(2.0)


class TestStepQtyQuantization:
    """Test 4: step qty respects min_step_qty and doesn't overshoot."""

    def test_min_step_qty(self) -> None:
        ctrl = SymbolUnloadController(_cfg(min_step_qty=5.0, step_pct=0.10))
        ctrl.activate("SYM", now=0.0)
        # 10% of 10 = 1.0, but min = 5.0
        step = ctrl.try_step("SYM", 10.0, now=1.0)
        assert step is not None
        assert step.qty == pytest.approx(5.0)

    def test_no_overshoot(self) -> None:
        ctrl = SymbolUnloadController(_cfg(step_pct=0.50))
        ctrl.activate("SYM", now=0.0)
        # 50% of 3 = 1.5, but remaining is only 3
        step = ctrl.try_step("SYM", 3.0, now=1.0)
        assert step is not None
        assert step.qty <= 3.0


class TestCooldown:
    """Test 5: cooldown prevents rapid repeated steps."""

    def test_cooldown_blocks(self) -> None:
        ctrl = SymbolUnloadController(_cfg(step_cooldown_s=10.0))
        ctrl.activate("SYM", now=0.0)
        step1 = ctrl.try_step("SYM", 10.0, now=1.0)
        assert step1 is not None
        # Too soon
        step2 = ctrl.try_step("SYM", 8.0, now=5.0)
        assert step2 is None
        # After cooldown
        step3 = ctrl.try_step("SYM", 8.0, now=12.0)
        assert step3 is not None


class TestMaxStepsPerHour:
    """Test 6: hourly cap enforced."""

    def test_hourly_cap(self) -> None:
        ctrl = SymbolUnloadController(_cfg(max_steps_per_hour=2, step_cooldown_s=0))
        ctrl.activate("SYM", now=0.0)
        step1 = ctrl.try_step("SYM", 10.0, now=1.0)
        assert step1 is not None
        step2 = ctrl.try_step("SYM", 8.0, now=2.0)
        assert step2 is not None
        # Third blocked
        step3 = ctrl.try_step("SYM", 6.0, now=3.0)
        assert step3 is None
        # After 1 hour resets
        step4 = ctrl.try_step("SYM", 6.0, now=3605.0)
        assert step4 is not None


class TestFailurePath:
    """Test 7: failure pauses unload, stays EXIT_ONLY."""

    def test_consecutive_failures_pause(self) -> None:
        ctrl = SymbolUnloadController(_cfg())
        ctrl.activate("SYM", now=0.0)
        assert ctrl.get_status("SYM") == UnloadStatus.ACTIVE
        ctrl.record_step_failure("SYM")
        ctrl.record_step_failure("SYM")
        assert ctrl.get_status("SYM") == UnloadStatus.ACTIVE  # not yet
        ctrl.record_step_failure("SYM")
        assert ctrl.get_status("SYM") == UnloadStatus.PAUSED

    def test_success_resets_failures(self) -> None:
        ctrl = SymbolUnloadController(_cfg())
        ctrl.activate("SYM", now=0.0)
        ctrl.record_step_failure("SYM")
        ctrl.record_step_failure("SYM")
        ctrl.record_step_success("SYM")
        ctrl.record_step_failure("SYM")
        ctrl.record_step_failure("SYM")
        ctrl.record_step_failure("SYM")
        # 3rd failure after reset
        assert ctrl.get_status("SYM") == UnloadStatus.PAUSED


class TestFlatCompletion:
    """Test 8: flat position completes unload."""

    def test_flat_completes(self) -> None:
        ctrl = SymbolUnloadController(_cfg())
        ctrl.activate("SYM", now=0.0)
        step = ctrl.try_step("SYM", 0.0, now=1.0)
        assert step is None
        assert ctrl.get_status("SYM") == UnloadStatus.COMPLETE


class TestDeterminism:
    """Test 9: deterministic on repeated identical snapshots."""

    def test_same_inputs_same_outputs(self) -> None:
        results = []
        for _ in range(3):
            ctrl = SymbolUnloadController(_cfg(step_cooldown_s=0))
            ctrl.activate("SYM", now=0.0)
            steps = []
            for i in range(5):
                step = ctrl.try_step("SYM", 10.0 - i * 2.0, now=float(i + 1))
                steps.append((step.side if step else None, step.qty if step else None))
            results.append(steps)
        assert results[0] == results[1] == results[2]


class TestRepriceLogic:
    """Test reprice/TTL for pending orders."""

    def test_reprice_after_ttl(self) -> None:
        ctrl = SymbolUnloadController(_cfg(order_ttl_s=30.0, max_reprices_per_step=2))
        ctrl.activate("SYM", now=0.0)
        # Before TTL
        assert not ctrl.should_reprice("SYM", order_placed_at=10.0, now=20.0)
        # After TTL
        assert ctrl.should_reprice("SYM", order_placed_at=10.0, now=45.0)
        # Second reprice ok
        assert ctrl.should_reprice("SYM", order_placed_at=45.0, now=80.0)
        # Third blocked (max=2)
        assert not ctrl.should_reprice("SYM", order_placed_at=80.0, now=115.0)


class TestDisabled:
    """Test: disabled controller is inert."""

    def test_disabled_no_steps(self) -> None:
        ctrl = SymbolUnloadController(UnloadConfig(enabled=False))
        ctrl.activate("SYM", now=0.0)
        step = ctrl.try_step("SYM", 10.0, now=1.0)
        assert step is None
