"""Tests for PR-2 dynamic tuning admission in TuningRefresher (ADR-183 PR-2).

PR-2 completes the dynamic bootstrap path started in PR-1 (#672). Where
PR-1 only discovered and logged the post-start eligible subset, PR-2
actually tunes those symbols and, on success, merges them into the
shared ``AutonomousTuningState`` under a single atomic ``replace()``.

The **sharp edge** is state consistency: after commit, every symbol
present in ``state.candidates`` must also appear in every map field
(``tuned_results``, ``tuned_sizes``, ``natr_map``, ``v1_features``,
``v2_features``). There must be no path where a newly discovered
symbol can become partially visible — e.g. present in ``candidates``
but missing from ``v2_features``, or the reverse.

These tests exercise the full ``_run_one_cycle`` flow with external
dependencies (feature fetchers, constraint provider, price fetcher,
universe provider, bridge, cache) replaced by fakes, so the behavior
is deterministic and the atomic merge invariant can be asserted
structurally on the committed state snapshot.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from grinder.tuning.autonomous_state import AutonomousTuningState
from grinder.tuning.refresher import TuningRefresher
from grinder.tuning.solver import TuningResult, TuningStatus

if TYPE_CHECKING:
    from collections.abc import Callable


def _make_tuned_result(symbol: str, price: str = "100") -> TuningResult:
    """Build a TuningResult in TUNED status with plausible values."""
    return TuningResult(
        symbol=symbol,
        status=TuningStatus.TUNED,
        order_size=Decimal("1"),
        max_position_notional_usd=Decimal(price) * Decimal("5"),
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.001"),
        reason=None,
    )


def _make_rejected_result(symbol: str, reason_code: Any = None) -> TuningResult:
    """Build a TuningResult that failed tuning (no order_size)."""
    return TuningResult(
        symbol=symbol,
        status=TuningStatus.NO_GO,
        order_size=None,
        max_position_notional_usd=None,
        tick_size=None,
        step_size=None,
        reason=reason_code,
    )


class _FakeFeature:
    """Minimal stand-in for a v1/v2 selector feature object."""

    def __init__(self, natr: Decimal = Decimal("1.5")) -> None:
        self.natr_14_5m = natr
        self.quote_volume_last_12x5m = Decimal("5000000")


def _make_refresher(
    *,
    initial_candidates: list[str] | None = None,
    initial_tuned_results: dict[str, Any] | None = None,
    initial_tuned_sizes: dict[str, str] | None = None,
    initial_natr_map: dict[str, Decimal] | None = None,
    initial_v1_features: dict[str, Any] | None = None,
    initial_v2_features: dict[str, Any] | None = None,
    dynamic_selection: list[str] | None = None,
    coarse_select_fn: Callable[..., list[str]] | None = None,
    prefilter_fn: Callable[..., list[str]] | None = None,
    universe_provider: Any = None,
) -> tuple[TuningRefresher, AutonomousTuningState]:
    """Build a refresher with enough wiring to exercise _run_one_cycle end-to-end."""
    state = AutonomousTuningState(
        candidates=list(initial_candidates or []),
        tuned_results=dict(initial_tuned_results or {}),
        tuned_sizes=dict(initial_tuned_sizes or {}),
        natr_map=dict(initial_natr_map or {}),
        v1_features=dict(initial_v1_features or {}),
        v2_features=dict(initial_v2_features or {}),
    )

    bridge = MagicMock()
    bridge.last_known_risk_base = Decimal("1000")
    bridge.last_known_gross_exposure = Decimal("0")

    registry = MagicMock()
    registry.list_present.return_value = []
    registry.get_state.return_value = None

    refresher = TuningRefresher(
        state=state,
        cache=MagicMock(),
        bridge=bridge,
        registry=registry,
        args=argparse.Namespace(mainnet=False),
        universe_provider=universe_provider,
        coarse_select_fn=coarse_select_fn,
        prefilter_fn=prefilter_fn,
    )
    if dynamic_selection is not None:
        refresher._last_dynamic_candidates = list(dynamic_selection)
    return refresher, state


class _CycleFakes:
    """Bundle of patches that replace external refresh-cycle dependencies."""

    def __init__(
        self,
        *,
        existing_candidates: list[str],
        dynamic_candidates: list[str],
        existing_v1: dict[str, _FakeFeature] | None = None,
        dynamic_v1: dict[str, _FakeFeature] | None = None,
        dynamic_retune_results: dict[str, TuningResult] | None = None,
        existing_retune_results: dict[str, TuningResult] | None = None,
    ) -> None:
        self.existing_candidates = existing_candidates
        self.dynamic_candidates = dynamic_candidates
        self.existing_v1 = existing_v1 or {s: _FakeFeature() for s in existing_candidates}
        self.dynamic_v1 = dynamic_v1 or {s: _FakeFeature() for s in dynamic_candidates}
        self.existing_retune_results = existing_retune_results or {
            s: _make_tuned_result(s) for s in existing_candidates
        }
        self.dynamic_retune_results = dynamic_retune_results or {
            s: _make_tuned_result(s) for s in dynamic_candidates
        }
        self.v2_calls: list[list[str]] = []

    def _fake_fetch_v1(
        self,
        symbols: list[str],
        *,
        mainnet: bool,  # noqa: ARG002
    ) -> dict[str, Any]:
        # Two routes: existing-candidates fetch or dynamic-candidates fetch.
        sset = set(symbols)
        if sset <= set(self.existing_v1.keys()):
            return dict(self.existing_v1)
        if sset <= set(self.dynamic_v1.keys()):
            return dict(self.dynamic_v1)
        # Mixed or unknown — merge what we have
        merged = {}
        for s in symbols:
            if s in self.existing_v1:
                merged[s] = self.existing_v1[s]
            elif s in self.dynamic_v1:
                merged[s] = self.dynamic_v1[s]
        return merged

    def _fake_fetch_v2(
        self,
        symbols: list[str],
        *,
        tuning_order_sizes: dict[str, Decimal],  # noqa: ARG002
        max_notional_per_order: Any,  # noqa: ARG002
        mainnet: bool,  # noqa: ARG002
    ) -> dict[str, Any]:
        self.v2_calls.append(list(symbols))
        return {s: _FakeFeature() for s in symbols}

    def _fake_retune(
        self,
        candidates: list[str],
        natr_map: dict[str, Decimal],  # noqa: ARG002
        symbol_risk_budget: Decimal,  # noqa: ARG002
    ) -> tuple[
        dict[str, TuningResult],
        dict[str, str],
        dict[str, Decimal],
        list[tuple[str, TuningResult]],
    ]:
        # Route by symbol membership — existing vs dynamic
        tuned_results: dict[str, TuningResult] = {}
        tuned_sizes: dict[str, str] = {}
        order_sizes: dict[str, Decimal] = {}
        all_results: list[tuple[str, TuningResult]] = []
        for sym in candidates:
            if sym in self.existing_retune_results:
                r = self.existing_retune_results[sym]
            elif sym in self.dynamic_retune_results:
                r = self.dynamic_retune_results[sym]
            else:
                r = _make_rejected_result(sym)
            all_results.append((sym, r))
            if r.status == TuningStatus.TUNED and r.order_size is not None:
                tuned_results[sym] = r
                tuned_sizes[sym] = str(r.order_size)
                order_sizes[sym] = r.order_size
        return tuned_results, tuned_sizes, order_sizes, all_results

    def install(self, refresher: TuningRefresher) -> dict[str, Any]:  # noqa: ARG002
        """Return a dict of patch objects; caller uses with contextlib.ExitStack."""
        return {
            "fetch_v1": patch(
                "grinder.selector.feature_provider.fetch_selection_features",
                side_effect=self._fake_fetch_v1,
            ),
            "fetch_v2": patch(
                "grinder.selector.feature_provider.fetch_selection_features_v2",
                side_effect=self._fake_fetch_v2,
            ),
            "retune": patch.object(
                TuningRefresher,
                "_retune_symbols",
                side_effect=self._fake_retune,
            ),
            "risk_budget": patch.object(
                TuningRefresher,
                "_derive_symbol_risk_budget",
                return_value=Decimal("100"),
            ),
            "update_equity": patch.object(TuningRefresher, "_update_equity"),
            "update_bridge": patch.object(TuningRefresher, "_update_bridge"),
        }


def _run_cycle(refresher: TuningRefresher, fakes: _CycleFakes) -> None:
    """Run _run_one_cycle with all external deps patched to the fakes."""
    import contextlib  # noqa: PLC0415

    with contextlib.ExitStack() as stack:
        for p in fakes.install(refresher).values():
            stack.enter_context(p)
        refresher._run_one_cycle()


class TestAtomicVisibilityInvariant:
    """The load-bearing invariant: no torn snapshot after commit.

    For every symbol in ``state.candidates``, that symbol must also be in
    ``tuned_results``, ``tuned_sizes``, ``natr_map``, ``v1_features``, and
    ``v2_features``. Conversely, no symbol may appear in any map without
    being in ``candidates``. This test is what protects against a future
    refactor reintroducing a partial-merge seam.
    """

    def _assert_state_coherent(self, state: AutonomousTuningState) -> None:
        cand_set = set(state.candidates)
        # Every candidate must be in every map
        for sym in cand_set:
            assert sym in state.tuned_results, (
                f"atomic-merge violation: {sym} in candidates but missing from tuned_results"
            )
            assert sym in state.tuned_sizes, (
                f"atomic-merge violation: {sym} in candidates but missing from tuned_sizes"
            )
            assert sym in state.natr_map, (
                f"atomic-merge violation: {sym} in candidates but missing from natr_map"
            )
            assert sym in state.v1_features, (
                f"atomic-merge violation: {sym} in candidates but missing from v1_features"
            )
            assert sym in state.v2_features, (
                f"atomic-merge violation: {sym} in candidates but missing from v2_features"
            )
        # Every map key must be in candidates (no leftover after merge)
        for sym in state.tuned_results:
            assert sym in cand_set, (
                f"atomic-merge violation: {sym} in tuned_results but not in candidates"
            )
        for sym in state.tuned_sizes:
            assert sym in cand_set, (
                f"atomic-merge violation: {sym} in tuned_sizes but not in candidates"
            )
        for sym in state.natr_map:
            assert sym in cand_set, (
                f"atomic-merge violation: {sym} in natr_map but not in candidates"
            )
        for sym in state.v1_features:
            assert sym in cand_set, (
                f"atomic-merge violation: {sym} in v1_features but not in candidates"
            )
        for sym in state.v2_features:
            assert sym in cand_set, (
                f"atomic-merge violation: {sym} in v2_features but not in candidates"
            )

    def test_fresh_admission_from_empty_is_coherent(self) -> None:
        """Empty initial state + one dynamic symbol tuned successfully →
        coherent single-symbol snapshot."""
        refresher, state = _make_refresher(
            initial_candidates=[],
            dynamic_selection=["NEWUSDT"],
        )
        fakes = _CycleFakes(existing_candidates=[], dynamic_candidates=["NEWUSDT"])
        _run_cycle(refresher, fakes)

        assert state.candidates == ["NEWUSDT"]
        self._assert_state_coherent(state)

    def test_dynamic_merge_over_existing_is_coherent(self) -> None:
        """Existing tuned candidates + new dynamic admissions → merged
        coherent snapshot."""
        refresher, state = _make_refresher(
            initial_candidates=["BTCUSDT"],
            initial_tuned_results={"BTCUSDT": _make_tuned_result("BTCUSDT")},
            initial_tuned_sizes={"BTCUSDT": "1"},
            initial_natr_map={"BTCUSDT": Decimal("1.5")},
            initial_v1_features={"BTCUSDT": _FakeFeature()},
            initial_v2_features={"BTCUSDT": _FakeFeature()},
            dynamic_selection=["ETHUSDT", "SOLUSDT"],
        )
        fakes = _CycleFakes(
            existing_candidates=["BTCUSDT"],
            dynamic_candidates=["ETHUSDT", "SOLUSDT"],
        )
        _run_cycle(refresher, fakes)

        assert set(state.candidates) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        # Order: existing first, then dynamic in discovery order
        assert state.candidates == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        self._assert_state_coherent(state)

    def test_mixed_tuning_results_only_successful_merge(self) -> None:
        """5 dynamic candidates attempted, only 2 tune successfully →
        only the 2 successful ones appear in state, the 3 failed ones
        are absent from EVERY map field. Atomic by construction."""
        refresher, state = _make_refresher(
            initial_candidates=["BTCUSDT"],
            initial_tuned_results={"BTCUSDT": _make_tuned_result("BTCUSDT")},
            initial_tuned_sizes={"BTCUSDT": "1"},
            initial_natr_map={"BTCUSDT": Decimal("1.5")},
            initial_v1_features={"BTCUSDT": _FakeFeature()},
            initial_v2_features={"BTCUSDT": _FakeFeature()},
            dynamic_selection=["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"],
        )
        fakes = _CycleFakes(
            existing_candidates=["BTCUSDT"],
            dynamic_candidates=["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"],
            dynamic_retune_results={
                "AUSDT": _make_tuned_result("AUSDT"),
                "BUSDT": _make_rejected_result("BUSDT"),
                "CUSDT": _make_tuned_result("CUSDT"),
                "DUSDT": _make_rejected_result("DUSDT"),
                "EUSDT": _make_rejected_result("EUSDT"),
            },
        )
        _run_cycle(refresher, fakes)

        # Only the 2 successful ones got merged
        assert set(state.candidates) == {"BTCUSDT", "AUSDT", "CUSDT"}
        # Failed dynamic symbols are in NONE of the maps
        for failed in ("BUSDT", "DUSDT", "EUSDT"):
            assert failed not in state.candidates
            assert failed not in state.tuned_results
            assert failed not in state.tuned_sizes
            assert failed not in state.natr_map
            assert failed not in state.v1_features
            assert failed not in state.v2_features
        self._assert_state_coherent(state)

    def test_all_dynamic_tuning_fails_no_admission(self) -> None:
        """All 3 dynamic candidates fail tuning → state is exactly as
        before for existing candidates, no map touched by failures.
        """
        refresher, state = _make_refresher(
            initial_candidates=["BTCUSDT"],
            initial_tuned_results={"BTCUSDT": _make_tuned_result("BTCUSDT")},
            initial_tuned_sizes={"BTCUSDT": "1"},
            initial_natr_map={"BTCUSDT": Decimal("1.5")},
            initial_v1_features={"BTCUSDT": _FakeFeature()},
            initial_v2_features={"BTCUSDT": _FakeFeature()},
            dynamic_selection=["AUSDT", "BUSDT", "CUSDT"],
        )
        fakes = _CycleFakes(
            existing_candidates=["BTCUSDT"],
            dynamic_candidates=["AUSDT", "BUSDT", "CUSDT"],
            dynamic_retune_results={
                "AUSDT": _make_rejected_result("AUSDT"),
                "BUSDT": _make_rejected_result("BUSDT"),
                "CUSDT": _make_rejected_result("CUSDT"),
            },
        )
        _run_cycle(refresher, fakes)

        assert state.candidates == ["BTCUSDT"]
        self._assert_state_coherent(state)


class TestDynamicAdmissionOrdering:
    """Newly admitted symbols are appended in discovery-rank order."""

    def test_discovery_order_is_preserved(self) -> None:
        refresher, state = _make_refresher(
            initial_candidates=["A"],
            initial_tuned_results={"A": _make_tuned_result("A")},
            initial_tuned_sizes={"A": "1"},
            initial_natr_map={"A": Decimal("1.5")},
            initial_v1_features={"A": _FakeFeature()},
            initial_v2_features={"A": _FakeFeature()},
            dynamic_selection=["C", "B", "D"],  # specific order
        )
        fakes = _CycleFakes(
            existing_candidates=["A"],
            dynamic_candidates=["C", "B", "D"],
        )
        _run_cycle(refresher, fakes)

        # Existing first, then dynamic in PR-1 order (C, B, D)
        assert state.candidates == ["A", "C", "B", "D"]


class TestNoDynamicCandidatesBackwardsCompat:
    """Empty `_last_dynamic_candidates` → legacy refresh path unchanged."""

    def test_empty_dynamic_keeps_candidates_unchanged(self) -> None:
        refresher, state = _make_refresher(
            initial_candidates=["BTCUSDT", "ETHUSDT"],
            initial_tuned_results={
                "BTCUSDT": _make_tuned_result("BTCUSDT"),
                "ETHUSDT": _make_tuned_result("ETHUSDT"),
            },
            initial_tuned_sizes={"BTCUSDT": "1", "ETHUSDT": "1"},
            initial_natr_map={"BTCUSDT": Decimal("1.5"), "ETHUSDT": Decimal("1.5")},
            initial_v1_features={"BTCUSDT": _FakeFeature(), "ETHUSDT": _FakeFeature()},
            initial_v2_features={"BTCUSDT": _FakeFeature(), "ETHUSDT": _FakeFeature()},
            dynamic_selection=[],  # no new admissions
        )
        fakes = _CycleFakes(
            existing_candidates=["BTCUSDT", "ETHUSDT"],
            dynamic_candidates=[],
        )
        _run_cycle(refresher, fakes)

        # Candidates list is exactly the original two, order preserved
        assert state.candidates == ["BTCUSDT", "ETHUSDT"]

    def test_empty_candidates_and_empty_dynamic_early_return(self) -> None:
        refresher, state = _make_refresher(
            initial_candidates=[],
            dynamic_selection=[],
        )
        fakes = _CycleFakes(existing_candidates=[], dynamic_candidates=[])
        _run_cycle(refresher, fakes)

        # Nothing changed — version stayed at 0
        assert state.version == 0
        assert state.candidates == []


class TestDynamicTuneDirect:
    """Unit test `_tune_dynamic_candidates` in isolation."""

    def test_successful_dynamic_tuning_returns_coherent_maps(self) -> None:
        refresher, _ = _make_refresher(initial_candidates=[])
        fakes = _CycleFakes(
            existing_candidates=[],
            dynamic_candidates=["AUSDT", "BUSDT"],
        )
        with (
            patch(
                "grinder.selector.feature_provider.fetch_selection_features",
                side_effect=fakes._fake_fetch_v1,
            ),
            patch(
                "grinder.selector.feature_provider.fetch_selection_features_v2",
                side_effect=fakes._fake_fetch_v2,
            ),
            patch.object(
                TuningRefresher,
                "_retune_symbols",
                side_effect=fakes._fake_retune,
            ),
        ):
            result = refresher._tune_dynamic_candidates(
                ["AUSDT", "BUSDT"],
                Decimal("100"),
                mainnet=False,
            )

        (
            tuned_results,
            tuned_sizes,
            _order_sizes,
            natr_map,
            v1_features,
            v2_features,
            _all_results,
        ) = result

        # All five map fields share the same key set
        assert set(tuned_results.keys()) == {"AUSDT", "BUSDT"}
        assert set(tuned_sizes.keys()) == {"AUSDT", "BUSDT"}
        assert set(_order_sizes.keys()) == {"AUSDT", "BUSDT"}
        assert set(natr_map.keys()) == {"AUSDT", "BUSDT"}
        assert set(v1_features.keys()) == {"AUSDT", "BUSDT"}
        assert set(v2_features.keys()) == {"AUSDT", "BUSDT"}

    def test_failed_dynamic_tuning_scrubs_maps(self) -> None:
        refresher, _ = _make_refresher(initial_candidates=[])
        fakes = _CycleFakes(
            existing_candidates=[],
            dynamic_candidates=["AUSDT", "BUSDT"],
            dynamic_retune_results={
                "AUSDT": _make_tuned_result("AUSDT"),
                "BUSDT": _make_rejected_result("BUSDT"),
            },
        )
        with (
            patch(
                "grinder.selector.feature_provider.fetch_selection_features",
                side_effect=fakes._fake_fetch_v1,
            ),
            patch(
                "grinder.selector.feature_provider.fetch_selection_features_v2",
                side_effect=fakes._fake_fetch_v2,
            ),
            patch.object(
                TuningRefresher,
                "_retune_symbols",
                side_effect=fakes._fake_retune,
            ),
        ):
            result = refresher._tune_dynamic_candidates(
                ["AUSDT", "BUSDT"],
                Decimal("100"),
                mainnet=False,
            )

        (
            _tuned_results,
            tuned_sizes,
            _order_sizes,
            natr_map,
            v1_features,
            v2_features,
            all_results,
        ) = result

        # Only the successful symbol is present in the map-shaped outputs
        assert set(tuned_sizes.keys()) == {"AUSDT"}
        assert set(natr_map.keys()) == {"AUSDT"}
        assert set(v1_features.keys()) == {"AUSDT"}
        assert set(v2_features.keys()) == {"AUSDT"}
        # But all_results includes both (for cache bookkeeping)
        assert {s for s, _ in all_results} == {"AUSDT", "BUSDT"}

    def test_empty_input_returns_empty_maps(self) -> None:
        refresher, _ = _make_refresher(initial_candidates=[])
        result = refresher._tune_dynamic_candidates([], Decimal("100"), mainnet=False)
        # Every slot is empty
        for slot in result[:6]:
            assert slot == {}
        assert result[6] == []


class TestDynamicTuningFailOpen:
    """A crash inside dynamic tuning must not prevent the existing refresh
    from committing its normal snapshot.
    """

    def test_dynamic_tuning_exception_preserves_existing_refresh(self) -> None:
        refresher, state = _make_refresher(
            initial_candidates=["BTCUSDT"],
            initial_tuned_results={"BTCUSDT": _make_tuned_result("BTCUSDT")},
            initial_tuned_sizes={"BTCUSDT": "1"},
            initial_natr_map={"BTCUSDT": Decimal("1.5")},
            initial_v1_features={"BTCUSDT": _FakeFeature()},
            initial_v2_features={"BTCUSDT": _FakeFeature()},
            dynamic_selection=["BOOMUSDT"],
        )
        fakes = _CycleFakes(
            existing_candidates=["BTCUSDT"],
            dynamic_candidates=["BOOMUSDT"],
        )

        def _crashing_tune(*_a: object, **_k: object) -> None:
            raise RuntimeError("dynamic tuning solver crashed")

        import contextlib  # noqa: PLC0415

        with contextlib.ExitStack() as stack:
            for p in fakes.install(refresher).values():
                stack.enter_context(p)
            stack.enter_context(
                patch.object(
                    refresher,
                    "_tune_dynamic_candidates",
                    side_effect=_crashing_tune,
                )
            )
            refresher._run_one_cycle()

        # Existing refresh committed normally — BOOMUSDT absent from state
        assert state.candidates == ["BTCUSDT"]
        assert "BOOMUSDT" not in state.tuned_results
        # Coherent snapshot invariant still holds
        for sym in state.candidates:
            assert sym in state.tuned_results
            assert sym in state.tuned_sizes
            assert sym in state.natr_map
            assert sym in state.v1_features
            assert sym in state.v2_features


class TestSingleAtomicReplaceCall:
    """The commit must happen via a single ``state.replace(...)`` call so
    readers never observe a torn intermediate snapshot.
    """

    def test_dynamic_admission_does_not_mutate_state_mid_cycle(self) -> None:
        refresher, state = _make_refresher(
            initial_candidates=["BTCUSDT"],
            initial_tuned_results={"BTCUSDT": _make_tuned_result("BTCUSDT")},
            initial_tuned_sizes={"BTCUSDT": "1"},
            initial_natr_map={"BTCUSDT": Decimal("1.5")},
            initial_v1_features={"BTCUSDT": _FakeFeature()},
            initial_v2_features={"BTCUSDT": _FakeFeature()},
            dynamic_selection=["NEWUSDT"],
        )
        fakes = _CycleFakes(
            existing_candidates=["BTCUSDT"],
            dynamic_candidates=["NEWUSDT"],
        )

        replace_calls: list[dict[str, Any]] = []
        original_replace = state.replace

        def _spying_replace(**kwargs: Any) -> None:
            replace_calls.append(dict(kwargs))
            original_replace(**kwargs)

        with patch.object(state, "replace", side_effect=_spying_replace):
            _run_cycle(refresher, fakes)

        # Exactly one replace call for the whole merged snapshot
        assert len(replace_calls) == 1
        call = replace_calls[0]
        # The single call must contain the merged view including NEWUSDT
        assert "NEWUSDT" in call["tuned_results"]
        assert "NEWUSDT" in call["tuned_sizes"]
        assert "NEWUSDT" in call["natr_map"]
        assert "NEWUSDT" in call["v1_features"]
        assert "NEWUSDT" in call["v2_features"]
        assert call["candidates"] is not None
        assert "NEWUSDT" in call["candidates"]


class TestStateVersionAdvances:
    """Every successful commit bumps ``state.version``."""

    def test_version_bumped_after_dynamic_admission(self) -> None:
        refresher, state = _make_refresher(
            initial_candidates=["BTCUSDT"],
            initial_tuned_results={"BTCUSDT": _make_tuned_result("BTCUSDT")},
            initial_tuned_sizes={"BTCUSDT": "1"},
            initial_natr_map={"BTCUSDT": Decimal("1.5")},
            initial_v1_features={"BTCUSDT": _FakeFeature()},
            initial_v2_features={"BTCUSDT": _FakeFeature()},
            dynamic_selection=["NEWUSDT"],
        )
        original_version = state.version
        fakes = _CycleFakes(
            existing_candidates=["BTCUSDT"],
            dynamic_candidates=["NEWUSDT"],
        )
        _run_cycle(refresher, fakes)

        assert state.version == original_version + 1
