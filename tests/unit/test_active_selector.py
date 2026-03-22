"""Tests for doc-36 Phase 2 active selector.

Covers (from brief):
A) Activation gate (ENABLED=0 vs ENABLED=1)
B) Hysteresis (min_hold_cycles, enter/exit threshold)
C) Change budget (max_changes_per_cycle)
D) Grid-v2 safety (graceful_exit_only for non-flat)
E) Fail-safe (exception → previous stable set)
F) Determinism (repeated runs identical)
G) No auto-scan (never introduces outside operator universe)
H) Metrics contract
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from grinder.connectors.live_connector import SafeMode
from grinder.execution.port import NoOpExchangePort
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.engine import BlockReason
from grinder.selection.active_selector import (
    ActiveSelector,
    ActiveSelectorConfig,
    get_active_selector_metrics,
    reset_active_selector_metrics,
)
from grinder.selection.shadow_selector import reset_selector_metrics


@dataclass
class _FakeFeatureSnapshot:
    ts: int
    symbol: str
    mid_price: Decimal
    spread_bps: int
    imbalance_l1_bps: int
    thin_l1: Decimal
    natr_bps: int
    atr: Decimal | None
    sum_abs_returns_bps: int
    net_return_bps: int
    range_score: int
    warmup_bars: int


def _make_feat(
    symbol: str,
    *,
    range_score: int = 500,
    natr_bps: int = 200,
    spread_bps: int = 10,
    net_return_bps: int = 50,
) -> _FakeFeatureSnapshot:
    return _FakeFeatureSnapshot(
        ts=1000,
        symbol=symbol,
        mid_price=Decimal("100"),
        spread_bps=spread_bps,
        imbalance_l1_bps=0,
        thin_l1=Decimal("10.0"),
        natr_bps=natr_bps,
        atr=Decimal("1.0"),
        sum_abs_returns_bps=1000,
        net_return_bps=net_return_bps,
        range_score=range_score,
        warmup_bars=20,
    )


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_selector_metrics()
    reset_active_selector_metrics()


def _base_config(**overrides: object) -> ActiveSelectorConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "k": 2,
        "cycle_s": 0,
        "min_hold_cycles": 0,
        "max_changes_per_cycle": 10,
        "enter_threshold_bps": 0,
        "exit_threshold_bps": 0,
    }
    defaults.update(overrides)
    return ActiveSelectorConfig(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------
# A) Activation gate
# ---------------------------------------------------------------


class TestActivationGate:
    def test_disabled_returns_none(self) -> None:
        config = _base_config(enabled=False)
        sel = ActiveSelector(config)
        sel.update_features(_make_feat("AAA"))  # type: ignore[arg-type]
        assert sel.maybe_run(1000, ["AAA"]) is None

    def test_enabled_returns_result(self) -> None:
        config = _base_config()
        sel = ActiveSelector(config, initial_active={"AAA", "BBB", "CCC"})
        for sym in ("AAA", "BBB", "CCC"):
            sel.update_features(_make_feat(sym))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB", "CCC"])
        assert result is not None
        assert len(result.active_set) > 0


# ---------------------------------------------------------------
# B) Hysteresis
# ---------------------------------------------------------------


class TestHysteresis:
    def test_min_hold_prevents_removal(self) -> None:
        config = _base_config(k=1, min_hold_cycles=3, max_changes_per_cycle=10)
        sel = ActiveSelector(config, initial_active={"AAA", "BBB"})
        # AAA scores higher → BBB should be removed, but min_hold blocks it
        for cycle in range(3):
            for sym in ("AAA", "BBB"):
                sel.update_features(_make_feat(sym, range_score=500 if sym == "AAA" else 100))  # type: ignore[arg-type]
            result = sel.maybe_run(cycle * 1000, ["AAA", "BBB"])
            assert result is not None
            # BBB should still be in active set due to min_hold
            assert "BBB" in result.active_set

    def test_removal_after_hold_expires(self) -> None:
        config = _base_config(k=1, min_hold_cycles=2, max_changes_per_cycle=10)
        sel = ActiveSelector(config, initial_active={"AAA", "BBB"})
        results = []
        for cycle in range(4):
            for sym in ("AAA", "BBB"):
                sel.update_features(_make_feat(sym, range_score=500 if sym == "AAA" else 100))  # type: ignore[arg-type]
            r = sel.maybe_run(cycle * 1000, ["AAA", "BBB"])
            assert r is not None
            results.append(r)
        # After enough cycles, BBB should be removed
        assert "BBB" not in results[-1].active_set

    def test_enter_threshold(self) -> None:
        config = _base_config(k=3, enter_threshold_bps=2000)
        sel = ActiveSelector(config, initial_active=set())
        # All symbols score ~1000 (< 2000 threshold) → none added
        for sym in ("AAA", "BBB"):
            sel.update_features(_make_feat(sym, range_score=50))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB"])
        assert result is not None
        assert len(result.added) == 0


# ---------------------------------------------------------------
# C) Change budget
# ---------------------------------------------------------------


class TestChangeBudget:
    def test_budget_limits_adds(self) -> None:
        config = _base_config(k=5, max_changes_per_cycle=1)
        sel = ActiveSelector(config, initial_active=set())
        for sym in ("AAA", "BBB", "CCC"):
            sel.update_features(_make_feat(sym))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB", "CCC"])
        assert result is not None
        # Only 1 add allowed per cycle
        assert len(result.added) <= 1

    def test_budget_limits_removes(self) -> None:
        config = _base_config(k=0, max_changes_per_cycle=1)
        sel = ActiveSelector(config, initial_active={"AAA", "BBB", "CCC"})
        for sym in ("AAA", "BBB", "CCC"):
            sel.update_features(_make_feat(sym))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB", "CCC"])
        assert result is not None
        # Only 1 remove per cycle (k=0 means target is empty)
        assert len(result.removed) <= 1


# ---------------------------------------------------------------
# D) Grid-v2 safety (graceful_exit_only)
# ---------------------------------------------------------------


class TestGracefulExitOnly:
    def test_non_flat_symbol_deferred(self) -> None:
        config = _base_config(k=1, max_changes_per_cycle=10)
        sel = ActiveSelector(config, initial_active={"AAA", "BBB"})
        # AAA better → BBB should be removed, but BBB is non-flat
        for sym in ("AAA", "BBB"):
            sel.update_features(_make_feat(sym, range_score=500 if sym == "AAA" else 100))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB"], non_flat_symbols={"BBB"})
        assert result is not None
        assert "BBB" in result.deferred
        assert "BBB" in result.graceful_exit_only

    def test_deferred_removed_after_flat(self) -> None:
        config = _base_config(k=1, max_changes_per_cycle=10)
        sel = ActiveSelector(config, initial_active={"AAA", "BBB"})
        # Cycle 1: BBB non-flat → deferred
        for sym in ("AAA", "BBB"):
            sel.update_features(_make_feat(sym, range_score=500 if sym == "AAA" else 100))  # type: ignore[arg-type]
        sel.maybe_run(1000, ["AAA", "BBB"], non_flat_symbols={"BBB"})
        # Cycle 2: BBB now flat → removed
        for sym in ("AAA", "BBB"):
            sel.update_features(_make_feat(sym, range_score=500 if sym == "AAA" else 100))  # type: ignore[arg-type]
        result = sel.maybe_run(2000, ["AAA", "BBB"], non_flat_symbols=set())
        assert result is not None
        assert "BBB" not in result.active_set
        assert "BBB" not in result.graceful_exit_only

    def test_dispatch_blocked_for_graceful_exit(self) -> None:
        config = _base_config(k=1, max_changes_per_cycle=10)
        sel = ActiveSelector(config, initial_active={"AAA", "BBB"})
        for sym in ("AAA", "BBB"):
            sel.update_features(_make_feat(sym, range_score=500 if sym == "AAA" else 100))  # type: ignore[arg-type]
        sel.maybe_run(1000, ["AAA", "BBB"], non_flat_symbols={"BBB"})
        assert not sel.is_dispatch_allowed("BBB")
        assert sel.is_dispatch_allowed("AAA")


# ---------------------------------------------------------------
# E) Fail-safe
# ---------------------------------------------------------------


class TestFailSafe:
    def test_exception_preserves_previous_set(self) -> None:
        config = _base_config()
        sel = ActiveSelector(config, initial_active={"AAA"})
        sel.update_features(_make_feat("AAA"))  # type: ignore[arg-type]
        # Corrupt internal state to trigger exception in _apply_transitions
        sel._shadow._feature_cache = None  # type: ignore[assignment]
        # First tick populates shadow cooldown; second triggers _run which fails
        result = sel.maybe_run(1000, ["AAA"])
        assert result is not None
        # Active set preserved
        assert "AAA" in result.active_set
        assert result.added == []
        assert result.removed == []


# ---------------------------------------------------------------
# F) Determinism
# ---------------------------------------------------------------


class TestDeterminism:
    def test_repeated_runs_identical(self) -> None:
        results = []
        for _ in range(3):
            reset_selector_metrics()
            reset_active_selector_metrics()
            config = _base_config(k=2, max_changes_per_cycle=10)
            sel = ActiveSelector(config, initial_active={"AAA", "BBB", "CCC"})
            for sym in ("AAA", "BBB", "CCC"):
                sel.update_features(
                    _make_feat(sym, range_score={"AAA": 500, "BBB": 300, "CCC": 100}[sym])  # type: ignore[arg-type]
                )
            r = sel.maybe_run(1000, ["AAA", "BBB", "CCC"])
            assert r is not None
            results.append(sorted(r.active_set))
        assert results[0] == results[1] == results[2]


# ---------------------------------------------------------------
# G) No auto-scan
# ---------------------------------------------------------------


class TestNoAutoScan:
    def test_active_set_never_exceeds_universe(self) -> None:
        config = _base_config(k=5, max_changes_per_cycle=10)
        sel = ActiveSelector(config, initial_active={"AAA", "BBB", "ROGUE"})
        for sym in ("AAA", "BBB"):
            sel.update_features(_make_feat(sym))  # type: ignore[arg-type]
        result = sel.maybe_run(1000, ["AAA", "BBB"])
        assert result is not None
        # ROGUE was in initial_active but not in operator universe → removed
        assert "ROGUE" not in result.active_set
        assert result.active_set <= {"AAA", "BBB"}


# ---------------------------------------------------------------
# H) Metrics contract
# ---------------------------------------------------------------


class TestMetricsContract:
    def test_metrics_emitted(self) -> None:
        config = _base_config()
        sel = ActiveSelector(config, initial_active={"AAA"})
        sel.update_features(_make_feat("AAA"))  # type: ignore[arg-type]
        sel.update_features(_make_feat("BBB"))  # type: ignore[arg-type]
        sel.maybe_run(1000, ["AAA", "BBB"])
        metrics = get_active_selector_metrics()
        assert metrics.active_set_size > 0
        lines = metrics.to_prometheus_lines()
        assert any("grinder_selector_active_set_size" in line for line in lines)
        assert any("grinder_selector_transition_total" in line for line in lines)


# ---------------------------------------------------------------
# I) Engine dispatch gating
# ---------------------------------------------------------------


class TestEngineDispatchGating:
    def test_selector_blocks_increase_risk_for_excluded_symbol(self) -> None:
        """Symbol not in active set → INCREASE_RISK blocked at engine gate."""
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])

        active_config = _base_config(k=1, max_changes_per_cycle=10)
        # Active set = only AAA
        selector = ActiveSelector(active_config, initial_active={"AAA"})

        engine = LiveEngineV0(
            paper_engine=paper,
            exchange_port=NoOpExchangePort(),
            config=config,
            active_selector=selector,
            operator_symbols=["AAA", "BBB"],
        )

        # Try to place an entry for BBB (not in active set)
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BBB",
        )
        result = engine._process_action(action, 1000)
        assert result.block_reason == BlockReason.SELECTOR_BLOCKED

    def test_selector_allows_cancel_for_excluded_symbol(self) -> None:
        """CANCEL is allowed even for symbols not in active set."""
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])

        active_config = _base_config(k=1, max_changes_per_cycle=10)
        selector = ActiveSelector(active_config, initial_active={"AAA"})

        engine = LiveEngineV0(
            paper_engine=paper,
            exchange_port=NoOpExchangePort(),
            config=config,
            active_selector=selector,
            operator_symbols=["AAA", "BBB"],
        )

        # CANCEL for BBB should NOT be blocked by selector
        action = ExecutionAction(
            action_type=ActionType.CANCEL,
            symbol="BBB",
            order_id="test-cancel-1",
        )
        result = engine._process_action(action, 1000)
        # Should not be SELECTOR_BLOCKED (cancel is reduce/cancel intent)
        assert result.block_reason != BlockReason.SELECTOR_BLOCKED


# ---------------------------------------------------------------
# J) build_engine with SHADOW=0 + ENABLED=1
# ---------------------------------------------------------------


class TestBuildEnginePhase2Only:
    def test_enabled_without_shadow_no_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ENABLED=1 + SHADOW=0 must not crash with UnboundLocalError."""
        monkeypatch.setenv("GRINDER_SYMBOL_SELECTOR_ENABLED", "1")
        monkeypatch.setenv("GRINDER_SYMBOL_SELECTOR_SHADOW", "0")
        monkeypatch.setenv("GRINDER_TRADING_MODE", "read_only")

        from scripts.run_trading import build_engine  # noqa: PLC0415

        engine = build_engine(
            SafeMode.READ_ONLY,
            armed=False,
            symbols=["BTCUSDT"],
        )
        assert engine._active_selector is not None
        assert engine._shadow_selector is None
