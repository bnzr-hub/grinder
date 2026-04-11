"""Tests for dynamic bootstrap discovery in TuningRefresher (PR-1).

PR-1 is **discovery-only**: the refresher identifies newly eligible post-
startup symbols via the same coarse + prefilter semantics as cold bootstrap,
logs them, and stores the selected list on ``self._last_dynamic_candidates``.
It does NOT tune them, does NOT merge them into shared state, and does NOT
affect active engines. Merging is the job of PR-2.

Invariants under test:
  - discovery is a no-op when any dependency (universe_provider / helpers)
    is missing (backwards compatible default)
  - already-tracked candidates are never rediscovered as "new"
  - the selected list is bounded by ``dynamic_bootstrap_max_new_per_cycle``
  - universe fetch failures are fail-open (no crash, empty result)
  - prefilter rank order is preserved in the selected bounded slice
  - ``AutonomousTuningState.candidates`` is never mutated
"""

from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from grinder.tuning.autonomous_state import AutonomousTuningState
from grinder.tuning.refresher import TuningRefresher

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


def _make_refresher(
    *,
    initial_candidates: list[str],
    universe_provider: Any = None,
    coarse_select_fn: Callable[..., list[str]] | None = None,
    prefilter_fn: Callable[..., list[str]] | None = None,
    max_new: int = 5,
    blacklist: frozenset[str] = frozenset(),
) -> tuple[TuningRefresher, AutonomousTuningState]:
    """Build a refresher wired for discovery tests. All other deps mocked."""
    state = AutonomousTuningState(
        candidates=list(initial_candidates),
        tuned_results={sym: MagicMock() for sym in initial_candidates},
        tuned_sizes=dict.fromkeys(initial_candidates, "1"),
    )
    refresher = TuningRefresher(
        state=state,
        cache=MagicMock(),
        bridge=MagicMock(),
        registry=MagicMock(),
        args=argparse.Namespace(mainnet=False),
        universe_provider=universe_provider,
        blacklist=blacklist,
        coarse_select_fn=coarse_select_fn,
        prefilter_fn=prefilter_fn,
        dynamic_bootstrap_max_new_per_cycle=max_new,
    )
    return refresher, state


class TestDiscoveryDisabled:
    """Discovery is a no-op when plumbing is not wired (backwards compat)."""

    def test_no_universe_provider_returns_empty(self) -> None:
        refresher, state = _make_refresher(
            initial_candidates=["BTCUSDT"],
            universe_provider=None,
            coarse_select_fn=lambda *_a, **_k: ["ETHUSDT"],
            prefilter_fn=lambda *_a, **_k: ["ETHUSDT"],
        )
        result = refresher._discover_new_candidates()
        assert result == []
        assert refresher._last_dynamic_candidates == []
        # State is untouched
        assert list(state.candidates) == ["BTCUSDT"]

    def test_no_coarse_fn_returns_empty(self) -> None:
        refresher, _ = _make_refresher(
            initial_candidates=["BTCUSDT"],
            universe_provider=MagicMock(get_candidates=lambda: ["ETHUSDT"]),
            coarse_select_fn=None,
            prefilter_fn=lambda *_a, **_k: ["ETHUSDT"],
        )
        assert refresher._discover_new_candidates() == []

    def test_no_prefilter_fn_returns_empty(self) -> None:
        refresher, _ = _make_refresher(
            initial_candidates=["BTCUSDT"],
            universe_provider=MagicMock(get_candidates=lambda: ["ETHUSDT"]),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=None,
        )
        assert refresher._discover_new_candidates() == []


class TestDiscoveryNewVsKnown:
    """Discovery correctly identifies new symbols and skips already-tracked ones."""

    def test_new_symbol_is_discovered(self) -> None:
        refresher, _ = _make_refresher(
            initial_candidates=["BTCUSDT"],
            universe_provider=MagicMock(get_candidates=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"]),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=lambda syms, **_k: syms,
        )
        result = refresher._discover_new_candidates()
        assert "ETHUSDT" in result
        assert "SOLUSDT" in result

    def test_already_tracked_symbol_not_rediscovered(self) -> None:
        refresher, _ = _make_refresher(
            initial_candidates=["BTCUSDT", "ETHUSDT"],
            universe_provider=MagicMock(get_candidates=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"]),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=lambda syms, **_k: syms,
        )
        result = refresher._discover_new_candidates()
        assert "BTCUSDT" not in result
        assert "ETHUSDT" not in result
        assert result == ["SOLUSDT"]

    def test_all_known_returns_empty(self) -> None:
        refresher, _ = _make_refresher(
            initial_candidates=["BTCUSDT", "ETHUSDT"],
            universe_provider=MagicMock(get_candidates=lambda: ["BTCUSDT", "ETHUSDT"]),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=lambda syms, **_k: syms,
        )
        assert refresher._discover_new_candidates() == []


class TestDiscoveryBoundedness:
    """The selected list is bounded by dynamic_bootstrap_max_new_per_cycle."""

    def test_bounded_selection_respects_max_new(self) -> None:
        many = [f"SYM{i}USDT" for i in range(20)]
        refresher, _ = _make_refresher(
            initial_candidates=[],
            universe_provider=MagicMock(get_candidates=lambda: many),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=lambda syms, **_k: syms,
            max_new=5,
        )
        result = refresher._discover_new_candidates()
        assert len(result) == 5
        # Prefilter rank order preserved — first 5 of the provided order
        assert result == [f"SYM{i}USDT" for i in range(5)]

    def test_bounded_selection_smaller_than_max_keeps_all(self) -> None:
        refresher, _ = _make_refresher(
            initial_candidates=[],
            universe_provider=MagicMock(get_candidates=lambda: ["AUSDT", "BUSDT"]),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=lambda syms, **_k: syms,
            max_new=10,
        )
        result = refresher._discover_new_candidates()
        assert result == ["AUSDT", "BUSDT"]

    def test_coarse_limit_is_passed_to_coarse_fn(self) -> None:
        coarse_calls: list[int] = []

        def _coarse(syms: list[str], limit: int, **_k: object) -> list[str]:
            coarse_calls.append(limit)
            return syms[:limit]

        refresher, _ = _make_refresher(
            initial_candidates=[],
            universe_provider=MagicMock(get_candidates=lambda: ["A", "B", "C"]),
            coarse_select_fn=_coarse,
            prefilter_fn=lambda syms, **_k: syms,
        )
        refresher._discover_new_candidates()
        # Default coarse limit == 100 (module constant)
        assert coarse_calls == [100]


class TestDiscoveryFailOpen:
    """Discovery failures must not crash the refresher."""

    def test_universe_fetch_failure_returns_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        def _boom() -> list[str]:
            raise RuntimeError("network down")

        refresher, _ = _make_refresher(
            initial_candidates=["BTCUSDT"],
            universe_provider=MagicMock(get_candidates=_boom),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=lambda syms, **_k: syms,
        )
        with caplog.at_level(logging.WARNING, logger="grinder.tuning.refresher"):
            result = refresher._discover_new_candidates()
        assert result == []
        assert refresher._last_dynamic_candidates == []
        assert any(
            "DYNAMIC_BOOTSTRAP_UNIVERSE_FETCH_FAILED" in r.getMessage() for r in caplog.records
        )

    def test_empty_universe_returns_empty(self) -> None:
        refresher, _ = _make_refresher(
            initial_candidates=[],
            universe_provider=MagicMock(get_candidates=lambda: []),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=lambda syms, **_k: syms,
        )
        assert refresher._discover_new_candidates() == []

    def test_coarse_select_failure_returns_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        def _coarse_boom(*_a: object, **_k: object) -> list[str]:
            raise RuntimeError("solver broke")

        refresher, _ = _make_refresher(
            initial_candidates=["BTCUSDT"],
            universe_provider=MagicMock(get_candidates=lambda: ["ETHUSDT"]),
            coarse_select_fn=_coarse_boom,
            prefilter_fn=lambda syms, **_k: syms,
        )
        with caplog.at_level(logging.WARNING, logger="grinder.tuning.refresher"):
            result = refresher._discover_new_candidates()
        assert result == []
        assert any(
            "DYNAMIC_BOOTSTRAP_COARSE_SELECT_FAILED" in r.getMessage() for r in caplog.records
        )

    def test_prefilter_failure_returns_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        def _prefilter_boom(*_a: object, **_k: object) -> list[str]:
            raise RuntimeError("features broke")

        refresher, _ = _make_refresher(
            initial_candidates=["BTCUSDT"],
            universe_provider=MagicMock(get_candidates=lambda: ["ETHUSDT"]),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=_prefilter_boom,
        )
        with caplog.at_level(logging.WARNING, logger="grinder.tuning.refresher"):
            result = refresher._discover_new_candidates()
        assert result == []
        assert any("DYNAMIC_BOOTSTRAP_PREFILTER_FAILED" in r.getMessage() for r in caplog.records)


class TestDiscoveryStateIsolation:
    """Discovery never mutates AutonomousTuningState."""

    def test_candidates_list_not_mutated(self) -> None:
        refresher, state = _make_refresher(
            initial_candidates=["BTCUSDT"],
            universe_provider=MagicMock(get_candidates=lambda: ["ETHUSDT"]),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=lambda syms, **_k: syms,
        )
        original_version = state.version
        refresher._discover_new_candidates()
        # Candidates list unchanged, state version unchanged
        assert list(state.candidates) == ["BTCUSDT"]
        assert state.version == original_version

    def test_tuned_results_not_mutated(self) -> None:
        refresher, state = _make_refresher(
            initial_candidates=["BTCUSDT"],
            universe_provider=MagicMock(get_candidates=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"]),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=lambda syms, **_k: syms,
        )
        original_keys = set(state.tuned_results.keys())
        refresher._discover_new_candidates()
        # New symbols are NOT added to tuned_results
        assert set(state.tuned_results.keys()) == original_keys
        assert "ETHUSDT" not in state.tuned_results
        assert "SOLUSDT" not in state.tuned_results


class TestDiscoveryBlacklist:
    """Blacklisted symbols should be filtered by the prefilter helper."""

    def test_blacklist_is_passed_to_prefilter_fn(self) -> None:
        captured_blacklist: list[frozenset[str]] = []

        def _prefilter_capture(
            syms: list[str],
            *,
            limit: int,
            mainnet: bool,
            blacklist: frozenset[str],
        ) -> list[str]:
            captured_blacklist.append(blacklist)
            return [s for s in syms if s not in blacklist][:limit]

        refresher, _ = _make_refresher(
            initial_candidates=[],
            universe_provider=MagicMock(get_candidates=lambda: ["BTCUSDT", "BANNED"]),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=_prefilter_capture,
            blacklist=frozenset({"BANNED"}),
        )
        result = refresher._discover_new_candidates()
        assert captured_blacklist == [frozenset({"BANNED"})]
        assert "BANNED" not in result
        assert "BTCUSDT" in result


class TestDiscoveryObservability:
    """Discovery emits the expected log events for operator visibility."""

    def test_result_log_has_all_stage_counts(self, caplog: pytest.LogCaptureFixture) -> None:
        refresher, _ = _make_refresher(
            initial_candidates=["BTCUSDT"],
            universe_provider=MagicMock(get_candidates=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"]),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=lambda syms, **_k: syms,
        )
        with caplog.at_level(logging.INFO, logger="grinder.tuning.refresher"):
            refresher._discover_new_candidates()
        messages = [r.getMessage() for r in caplog.records]
        assert any("DYNAMIC_BOOTSTRAP_DISCOVERY_START" in m for m in messages)
        result_line = next(m for m in messages if "DYNAMIC_BOOTSTRAP_DISCOVERY_RESULT" in m)
        assert "discovered=3" in result_line
        assert "coarse=3" in result_line
        assert "prefiltered=3" in result_line
        assert "new=2" in result_line
        assert "selected=2" in result_line
        # Selected names are also logged
        assert any("DYNAMIC_BOOTSTRAP_DISCOVERY_SELECTED" in m for m in messages)

    def test_no_selected_log_when_nothing_new(self, caplog: pytest.LogCaptureFixture) -> None:
        refresher, _ = _make_refresher(
            initial_candidates=["BTCUSDT"],
            universe_provider=MagicMock(get_candidates=lambda: ["BTCUSDT"]),
            coarse_select_fn=lambda syms, _limit, **_k: syms,
            prefilter_fn=lambda syms, **_k: syms,
        )
        with caplog.at_level(logging.INFO, logger="grinder.tuning.refresher"):
            refresher._discover_new_candidates()
        messages = [r.getMessage() for r in caplog.records]
        # Result is logged but SELECTED is only logged when non-empty
        assert any("selected=0" in m for m in messages)
        assert not any("DYNAMIC_BOOTSTRAP_DISCOVERY_SELECTED" in m for m in messages)


class TestBackwardsCompat:
    """Existing TuningRefresher construction (without discovery) still works."""

    def test_legacy_constructor_has_empty_discovery(self) -> None:
        state = AutonomousTuningState(candidates=["BTCUSDT"])
        refresher = TuningRefresher(
            state=state,
            cache=MagicMock(),
            bridge=MagicMock(),
            registry=MagicMock(),
            args=argparse.Namespace(mainnet=False),
        )
        # No discovery deps injected → empty result, stay quiet
        assert refresher._discover_new_candidates() == []
        assert refresher._last_dynamic_candidates == []


class TestBoundedDefaultsMatchBootstrap:
    """Dynamic discovery defaults align with cold bootstrap tunables."""

    def test_default_coarse_limit_is_100(self) -> None:
        from grinder.tuning.refresher import (  # noqa: PLC0415
            _DEFAULT_DYNAMIC_BOOTSTRAP_COARSE_LIMIT,
        )

        assert _DEFAULT_DYNAMIC_BOOTSTRAP_COARSE_LIMIT == 100

    def test_default_tune_limit_is_30(self) -> None:
        from grinder.tuning.refresher import (  # noqa: PLC0415
            _DEFAULT_DYNAMIC_BOOTSTRAP_TUNE_LIMIT,
        )

        assert _DEFAULT_DYNAMIC_BOOTSTRAP_TUNE_LIMIT == 30

    def test_default_max_new_is_small(self) -> None:
        from grinder.tuning.refresher import (  # noqa: PLC0415
            _DEFAULT_DYNAMIC_BOOTSTRAP_MAX_NEW_PER_CYCLE,
        )

        # Brief recommends K=5 or K=10. Keep conservative (5).
        assert _DEFAULT_DYNAMIC_BOOTSTRAP_MAX_NEW_PER_CYCLE <= 10
