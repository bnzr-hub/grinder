"""Tests for AutonomousLoop (PR-D2, ADR-132).

Covers:
- Full cycle happy path with all stages
- Prefilter stage filters candidates
- Ranker stage orders candidates
- Prefilter failure degrades safely
- Ranker failure degrades safely
- Operator universe override
- Only tuned symbols selected
- Universe fetch failure retention
- Orchestrator error stop-the-line
- Actions propagated
- Determinism
- Empty universe
- Cycle counter
- External stop
- Continuous run_forever with max_cycles
- run_forever respects stop
"""

from __future__ import annotations

from decimal import Decimal

from grinder.orchestration.autonomous_loop import (
    AutonomousLoop,
    AutonomousLoopConfig,
    CycleReport,
)
from grinder.orchestration.symbol_orchestrator import SymbolOrchestrator
from grinder.orchestration.universe_provider import (
    UniverseProvider,
    UniverseProviderConfig,
)
from grinder.rotation.controller import (
    RotationActionKind,
    RotationConfig,
    RotationController,
)
from grinder.tuning.cache import TuningCache
from grinder.tuning.solver import TuningResult, TuningStatus

FIXTURE = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "status": "TRADING",
        },
        {
            "symbol": "ETHUSDT",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "status": "TRADING",
        },
        {
            "symbol": "SOLUSDT",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "status": "TRADING",
        },
    ]
}


def _tuned(symbol: str) -> TuningResult:
    return TuningResult(
        symbol=symbol,
        status=TuningStatus.TUNED,
        order_size=Decimal("0.001"),
        tick_size=Decimal("0.10"),
        step_size=Decimal("0.001"),
        max_inventory_levels=5,
    )


def _make_loop(
    cache: TuningCache | None = None,
    top_k: int = 3,
    max_changes: int = 3,
    override: frozenset[str] | None = None,
    fetcher: object = None,
    prefilter_fn: object = None,
    ranker_fn: object = None,
) -> AutonomousLoop:
    c = cache or TuningCache(ttl_s=300.0)
    ctrl = RotationController(
        config=RotationConfig(
            top_k=top_k,
            max_changes_per_cycle=max_changes,
            min_hold_cycles=0,
        )
    )
    orch = SymbolOrchestrator(cache=c, controller=ctrl)
    provider = UniverseProvider(
        config=UniverseProviderConfig(),
        fetcher=fetcher or (lambda _url, _timeout: FIXTURE),
    )
    config = AutonomousLoopConfig(
        cycle_interval_s=0.01,
        operator_universe_override=override or frozenset(),
    )
    kwargs: dict[str, object] = {}
    if prefilter_fn is not None:
        kwargs["prefilter_fn"] = prefilter_fn
    if ranker_fn is not None:
        kwargs["ranker_fn"] = ranker_fn
    return AutonomousLoop(
        universe_provider=provider,
        orchestrator=orch,
        config=config,
        **kwargs,  # type: ignore[arg-type]
    )


class TestFullCycleHappyPath:
    """Complete cycle through all stages."""

    def test_cycle_report_has_all_stages(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            cache.put(s, _tuned(s))

        loop = _make_loop(cache=cache)
        report = loop.run_cycle()

        assert report.cycle == 1
        assert len(report.discovered) == 3
        assert len(report.eligible) == 3
        assert len(report.tuned) == 3
        assert len(report.selected) == 3
        assert len(report.admitted) == 3
        assert len(report.actions) == 3
        assert report.error is None


class TestPrefilterStage:
    """Prefilter stage filters discovered candidates."""

    def test_prefilter_reduces_candidates(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        for s in ["BTCUSDT", "ETHUSDT"]:
            cache.put(s, _tuned(s))

        def keep_btc_only(symbols: list[str]) -> list[str]:
            return [s for s in symbols if s == "BTCUSDT"]

        loop = _make_loop(cache=cache, prefilter_fn=keep_btc_only)
        report = loop.run_cycle()

        assert report.discovered == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        assert report.eligible == ["BTCUSDT"]
        assert report.admitted == ["BTCUSDT"]

    def test_prefilter_failure_degrades_safely(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        def exploding_filter(_symbols: list[str]) -> list[str]:
            raise RuntimeError("filter broken")

        loop = _make_loop(cache=cache, prefilter_fn=exploding_filter)
        report = loop.run_cycle()

        # Fail-closed: prefilter failure preserves previous admitted (empty on first cycle)
        assert report.eligible == []
        assert report.error is None


class TestRankerStage:
    """Ranker stage orders eligible candidates."""

    def test_ranker_reorders(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            cache.put(s, _tuned(s))

        def reverse_rank(symbols: list[str]) -> list[str]:
            return list(reversed(symbols))

        loop = _make_loop(cache=cache, ranker_fn=reverse_rank)
        report = loop.run_cycle()

        assert report.selected == ["SOLUSDT", "ETHUSDT", "BTCUSDT"]

    def test_ranker_failure_degrades_safely(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        def exploding_ranker(_symbols: list[str]) -> list[str]:
            raise RuntimeError("ranker broken")

        loop = _make_loop(cache=cache, ranker_fn=exploding_ranker)
        report = loop.run_cycle()

        # Fail-closed: ranking failure preserves previous admitted set (empty on first cycle)
        assert report.selected == []
        assert report.error is None


class TestOperatorOverride:
    def test_override_restricts(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            cache.put(s, _tuned(s))

        loop = _make_loop(cache=cache, override=frozenset({"BTCUSDT", "ETHUSDT"}))
        report = loop.run_cycle()

        assert set(report.discovered) == {"BTCUSDT", "ETHUSDT"}


class TestTuningBeforeRanking:
    """Tuning admission happens before ranking — doc 37 invariant."""

    def test_ranker_sees_only_tuned(self) -> None:
        """Ranker receives only TUNED symbols, not raw eligible set."""
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))
        # ETHUSDT and SOLUSDT not cached

        ranked_input: list[list[str]] = []

        def capturing_ranker(symbols: list[str]) -> list[str]:
            ranked_input.append(list(symbols))
            return symbols

        loop = _make_loop(cache=cache, ranker_fn=capturing_ranker)
        loop.run_cycle()

        # Ranker should have received only BTCUSDT (the one TUNED symbol)
        assert len(ranked_input) == 1
        assert ranked_input[0] == ["BTCUSDT"]

    def test_tuned_field_is_pre_ranking(self) -> None:
        """CycleReport.tuned reflects tuning stage, not orchestrator output."""
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))
        cache.put("ETHUSDT", _tuned("ETHUSDT"))

        loop = _make_loop(cache=cache, top_k=2, max_changes=2)
        report = loop.run_cycle()

        # tuned = symbols that passed TuningCache (pre-ranking)
        assert set(report.tuned) == {"BTCUSDT", "ETHUSDT"}
        # admitted = post-orchestrator (may equal tuned when all pass)
        assert set(report.admitted) == {"BTCUSDT", "ETHUSDT"}

    def test_cache_miss_in_skipped(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        loop = _make_loop(cache=cache)
        report = loop.run_cycle()

        assert report.admitted == ["BTCUSDT"]
        assert "ETHUSDT" in report.skipped
        assert report.skipped["ETHUSDT"] == "CACHE_MISS"


class TestUniverseFetchFailure:
    def test_failure_returns_empty(self) -> None:
        cache = TuningCache(ttl_s=300.0)

        def failing_fetcher(_url: str, _timeout: int) -> dict:  # type: ignore[type-arg]
            raise ConnectionError("offline")

        loop = _make_loop(cache=cache, fetcher=failing_fetcher)
        report = loop.run_cycle()

        assert report.discovered == []
        assert report.error is None


class TestOrchestratorError:
    def test_stop_on_error(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        loop = _make_loop(cache=cache)

        def _boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("boom")

        loop.orchestrator.reconcile = _boom  # type: ignore[assignment,method-assign]

        report = loop.run_cycle()
        assert report.error is not None
        assert loop.stopped is True

    def test_stopped_loop_refuses(self) -> None:
        loop = _make_loop()
        loop.stop("test")
        report = loop.run_cycle()
        assert "STOPPED" in (report.error or "")


class TestActionsPropagate:
    def test_activate_in_report(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        loop = _make_loop(cache=cache, top_k=1, max_changes=1)
        report = loop.run_cycle()

        assert len(report.actions) == 1
        assert report.actions[0].kind == RotationActionKind.ACTIVATE


class TestDeterminism:
    def test_same_inputs(self) -> None:
        def run() -> CycleReport:
            cache = TuningCache(ttl_s=300.0)
            cache.put("BTCUSDT", _tuned("BTCUSDT"))
            cache.put("ETHUSDT", _tuned("ETHUSDT"))
            loop = _make_loop(cache=cache, top_k=2, max_changes=2)
            return loop.run_cycle()

        r1 = run()
        r2 = run()
        assert r1.admitted == r2.admitted
        assert len(r1.actions) == len(r2.actions)


class TestEmptyUniverse:
    def test_empty(self) -> None:
        loop = _make_loop(fetcher=lambda _u, _t: {"symbols": []})
        report = loop.run_cycle()
        assert report.discovered == []
        assert report.error is None


class TestCycleCounter:
    def test_increments(self) -> None:
        loop = _make_loop()
        r1 = loop.run_cycle()
        r2 = loop.run_cycle()
        assert r1.cycle == 1
        assert r2.cycle == 2


class TestExternalStop:
    def test_stop(self) -> None:
        loop = _make_loop()
        loop.run_cycle()
        loop.stop("operator")
        report = loop.run_cycle()
        assert loop.stopped
        assert "STOPPED" in (report.error or "")


class TestRunForever:
    """Continuous loop with max_cycles."""

    def test_runs_n_cycles(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        t = [0.0]
        loop = _make_loop(cache=cache)
        reports = loop.run_forever(
            clock=lambda: t[0],
            sleep_fn=lambda _s: None,
            max_cycles=3,
        )

        assert len(reports) == 3
        assert reports[0].cycle == 1
        assert reports[2].cycle == 3

    def test_stop_halts_forever(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        loop = _make_loop(cache=cache)

        cycle_count = [0]

        def stop_after_two() -> None:
            cycle_count[0] += 1
            if cycle_count[0] >= 2:
                loop.stop("enough")

        original_run = loop.run_cycle

        def counting_cycle(facts: object = None) -> CycleReport:
            report = original_run(facts)  # type: ignore[arg-type]
            stop_after_two()
            return report

        loop.run_cycle = counting_cycle  # type: ignore[method-assign]

        reports = loop.run_forever(
            clock=lambda: 0.0,
            sleep_fn=lambda _s: None,
        )

        assert len(reports) == 2
        assert loop.stopped


# --- Tests: Stage 8 execution integration via run_cycle() ---


class TestStage8ExecutionDisabled:
    """With execution deps wired but disabled, run_cycle stays shadow."""

    def test_shadow_when_disabled(self) -> None:
        from grinder.execution_plane.coordinator import ExecutionCoordinator  # noqa: PLC0415
        from grinder.execution_plane.operator import OperatorControls  # noqa: PLC0415
        from grinder.execution_plane.registry import EngineRegistry  # noqa: PLC0415

        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        loop = _make_loop(cache=cache)
        loop.execution_coordinator = ExecutionCoordinator()
        loop.execution_registry = EngineRegistry()
        loop.execution_operator = OperatorControls()
        loop.execution_enabled = False

        report = loop.run_cycle()
        assert report.execution_report is not None
        assert report.execution_report.execution_enabled is False
        assert report.execution_report.execution_attempted is False


class TestStage8ExecutionEnabledAcked:
    """With execution enabled + ACK, run_cycle attaches execution report."""

    def test_execution_runs(self) -> None:
        from grinder.execution_plane.coordinator import ExecutionCoordinator  # noqa: PLC0415
        from grinder.execution_plane.operator import OperatorControls  # noqa: PLC0415
        from grinder.execution_plane.registry import EngineRegistry  # noqa: PLC0415

        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        activated: list[str] = []
        coord = ExecutionCoordinator(
            activate_fn=lambda s: activated.append(s) or True,  # type: ignore[func-returns-value]
        )

        loop = _make_loop(cache=cache, top_k=1, max_changes=1)
        loop.execution_coordinator = coord
        loop.execution_registry = EngineRegistry()
        loop.execution_operator = OperatorControls()
        loop.execution_enabled = True
        loop.execution_acknowledged = True

        report = loop.run_cycle()
        assert report.execution_report is not None
        assert report.execution_report.execution_enabled is True
        assert report.execution_report.execution_acknowledged is True


class TestStage8SafetyBlock:
    """Safety block suppresses execution in run_cycle."""

    def test_safety_blocks_in_loop(self) -> None:
        from grinder.execution_plane.coordinator import ExecutionCoordinator  # noqa: PLC0415
        from grinder.execution_plane.operator import OperatorControls  # noqa: PLC0415
        from grinder.execution_plane.registry import EngineRegistry, EngineState  # noqa: PLC0415

        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        # Create orphan engine to trigger STL-E3
        reg = EngineRegistry()
        reg.register("ORPHAN", engine_ref="e")
        reg.transition("ORPHAN", EngineState.ACTIVE)

        loop = _make_loop(cache=cache)
        loop.execution_coordinator = ExecutionCoordinator()
        loop.execution_registry = reg
        loop.execution_operator = OperatorControls()
        loop.execution_enabled = True
        loop.execution_acknowledged = True

        report = loop.run_cycle()
        assert report.execution_report is not None
        assert "safety_block" in (report.execution_report.skipped_reason or "")


class TestStage8NoExecutionDeps:
    """Without execution deps, run_cycle has no execution_report."""

    def test_legacy_mode(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        loop = _make_loop(cache=cache)
        report = loop.run_cycle()
        assert report.execution_report is None


# --- Tests: Orphan-engine deadlock fix (execution desired set preserves controller-owned symbols) ---


class TestOrphanFixRankingChurn:
    """Live symbols must stay in execution desired set even if ranking drops them."""

    def test_ranking_churn_does_not_produce_orphan(self) -> None:
        """Activate BTCUSDT on cycle 1, then ranker drops it on cycle 2.

        Without fix: reconciler sees BTCUSDT as orphan → STL_E3 blocks.
        With fix: controller-owned BTCUSDT stays in desired → no orphan.
        Must NOT spuriously activate ETHUSDT (controller hasn't approved it).
        """
        from grinder.execution_plane.coordinator import ExecutionCoordinator  # noqa: PLC0415
        from grinder.execution_plane.operator import OperatorControls  # noqa: PLC0415
        from grinder.execution_plane.registry import EngineRegistry, EngineState  # noqa: PLC0415

        cache = TuningCache(ttl_s=300.0)
        for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            cache.put(s, _tuned(s))

        activated: list[str] = []
        coord = ExecutionCoordinator(
            activate_fn=lambda s: activated.append(s) or True,  # type: ignore[func-returns-value]
        )
        reg = EngineRegistry()

        loop = _make_loop(cache=cache, top_k=1, max_changes=1)
        loop.execution_coordinator = coord
        loop.execution_registry = reg
        loop.execution_operator = OperatorControls()
        loop.execution_enabled = True
        loop.execution_acknowledged = True

        # Cycle 1: BTCUSDT admitted and activated
        report1 = loop.run_cycle()
        assert "BTCUSDT" in report1.admitted
        # Simulate engine start: register in execution registry + apply activation
        # in controller (as the host would do after real engine startup)
        reg.register("BTCUSDT", engine_ref="e1")
        reg.transition("BTCUSDT", EngineState.ACTIVE)
        loop.orchestrator.controller.apply_activation("BTCUSDT")
        activated.clear()

        # Cycle 2: ranker now prefers ETHUSDT only.
        # BTCUSDT is non-flat so controller keeps it in GRACEFUL_EXIT_ONLY
        # (not fully deactivated in one cycle).
        from grinder.rotation.controller import SymbolFacts as SF  # noqa: PLC0415

        loop.ranker_fn = lambda syms: sorted(syms, key=lambda s: s != "ETHUSDT")
        report2 = loop.run_cycle(facts={"BTCUSDT": SF(is_flat=False)})

        # No safety_block from orphan — BTCUSDT stays controller-owned
        assert report2.execution_report is not None
        assert "safety_block" not in (report2.execution_report.skipped_reason or "")

        # Critical: no spurious activation of ETHUSDT — controller owns
        # top_k/max_changes/min_hold boundaries, not the execution plane
        assert "ETHUSDT" not in activated

    def test_graceful_exit_symbol_stays_in_desired(self) -> None:
        """Symbol in GRACEFUL_EXIT_ONLY must remain in desired, not become orphan."""
        from grinder.execution_plane.coordinator import ExecutionCoordinator  # noqa: PLC0415
        from grinder.execution_plane.operator import OperatorControls  # noqa: PLC0415
        from grinder.execution_plane.registry import EngineRegistry, EngineState  # noqa: PLC0415
        from grinder.rotation.state_machine import SymbolState, TransitionReason  # noqa: PLC0415

        cache = TuningCache(ttl_s=300.0)
        for s in ["BTCUSDT", "ETHUSDT"]:
            cache.put(s, _tuned(s))

        coord = ExecutionCoordinator(
            activate_fn=lambda _s: True,
        )
        reg = EngineRegistry()

        loop = _make_loop(cache=cache, top_k=2, max_changes=2)
        loop.execution_coordinator = coord
        loop.execution_registry = reg
        loop.execution_operator = OperatorControls()
        loop.execution_enabled = True
        loop.execution_acknowledged = True

        # Cycle 1: activate both
        loop.run_cycle()
        reg.register("BTCUSDT", engine_ref="e1")
        reg.transition("BTCUSDT", EngineState.ACTIVE)

        # Move BTCUSDT through ACTIVE → GRACEFUL_EXIT_ONLY in controller
        ctrl = loop.orchestrator.controller
        lc = ctrl.get_lifecycle("BTCUSDT")
        lc.transition(SymbolState.ACTIVE, TransitionReason.SELECTED_INTO_TOP_K)
        lc.transition(SymbolState.GRACEFUL_EXIT_ONLY, TransitionReason.DROPPED_FROM_TOP_K)

        assert "BTCUSDT" in ctrl.get_graceful_exit_symbols()

        # Cycle 2: BTCUSDT not in admitted, but in graceful exit
        report = loop.run_cycle()
        assert report.execution_report is not None
        assert "safety_block" not in (report.execution_report.skipped_reason or "")


class TestOrphanFixGenuineOrphan:
    """True unowned engines must still trigger STL_E3."""

    def test_unowned_engine_still_produces_orphan(self) -> None:
        """Engine in registry with no controller lifecycle → genuine orphan → STL_E3."""
        from grinder.execution_plane.coordinator import ExecutionCoordinator  # noqa: PLC0415
        from grinder.execution_plane.operator import OperatorControls  # noqa: PLC0415
        from grinder.execution_plane.registry import EngineRegistry, EngineState  # noqa: PLC0415

        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        reg = EngineRegistry()
        # Inject orphan: XYZUSDT in registry but never went through controller
        reg.register("XYZUSDT", engine_ref="ghost")
        reg.transition("XYZUSDT", EngineState.ACTIVE)

        loop = _make_loop(cache=cache, top_k=1, max_changes=1)
        loop.execution_coordinator = ExecutionCoordinator()
        loop.execution_registry = reg
        loop.execution_operator = OperatorControls()
        loop.execution_enabled = True
        loop.execution_acknowledged = True

        report = loop.run_cycle()
        assert report.execution_report is not None
        assert "safety_block" in (report.execution_report.skipped_reason or "")
