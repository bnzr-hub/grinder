"""Tests for AutonomousLoop (PR-D2, ADR-132).

Covers:
- Full cycle happy path
- Operator universe override
- Only tuned symbols selected
- Universe fetch failure retention
- Orchestrator error stop-the-line
- Actions propagated
- Determinism
- Empty universe
- Stopped loop refuses cycles
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
        operator_universe_override=override or frozenset(),
    )
    return AutonomousLoop(
        universe_provider=provider,
        orchestrator=orch,
        config=config,
    )


class TestFullCycleHappyPath:
    """Complete cycle: discover → filter → orchestrate → report."""

    def test_cycle_produces_report(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            cache.put(s, _tuned(s))

        loop = _make_loop(cache=cache)
        report = loop.run_cycle()

        assert report.cycle == 1
        assert len(report.discovered) == 3
        assert len(report.admitted) == 3
        assert len(report.skipped) == 0
        assert len(report.actions) == 3  # 3 ACTIVATE
        assert report.error is None

    def test_actions_are_activate(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        for s in ["BTCUSDT", "ETHUSDT"]:
            cache.put(s, _tuned(s))

        loop = _make_loop(cache=cache, top_k=2, max_changes=2)
        report = loop.run_cycle()

        activate = [a for a in report.actions if a.kind == RotationActionKind.ACTIVATE]
        assert len(activate) == 2


class TestOperatorOverride:
    """Operator universe override restricts discovery."""

    def test_override_restricts(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            cache.put(s, _tuned(s))

        loop = _make_loop(
            cache=cache,
            override=frozenset({"BTCUSDT", "ETHUSDT"}),
        )
        report = loop.run_cycle()

        assert set(report.discovered) == {"BTCUSDT", "ETHUSDT"}
        assert "SOLUSDT" not in report.admitted


class TestOnlyTunedSelected:
    """Symbols without valid TUNED cache entry are skipped."""

    def test_cache_miss_skipped(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))
        # ETHUSDT and SOLUSDT not cached

        loop = _make_loop(cache=cache)
        report = loop.run_cycle()

        assert report.admitted == ["BTCUSDT"]
        assert "ETHUSDT" in report.skipped
        assert "SOLUSDT" in report.skipped


class TestUniverseFetchFailure:
    """Universe fetch failure retains previous via provider fail-safe."""

    def test_failure_returns_empty_first_time(self) -> None:
        cache = TuningCache(ttl_s=300.0)

        def failing_fetcher(url: str, timeout: int) -> dict:  # type: ignore[type-arg]
            raise ConnectionError("offline")

        loop = _make_loop(cache=cache, fetcher=failing_fetcher)
        report = loop.run_cycle()

        assert report.discovered == []
        assert report.admitted == []
        assert report.error is None  # universe failure is fail-safe, not error


class TestOrchestratorError:
    """Orchestrator error triggers stop-the-line."""

    def test_stop_on_error(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        loop = _make_loop(cache=cache)

        # Sabotage orchestrator
        def exploding_reconcile(*args: object, **kwargs: object) -> None:
            raise RuntimeError("controller exploded")

        loop.orchestrator.reconcile = exploding_reconcile  # type: ignore[assignment]

        report = loop.run_cycle()

        assert report.error is not None
        assert "orchestrator_error" in report.error
        assert loop.stopped is True

    def test_stopped_loop_refuses_cycles(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        loop = _make_loop(cache=cache)
        loop.stop("test_stop")

        report = loop.run_cycle()
        assert report.error is not None
        assert "STOPPED" in report.error


class TestActionsPropagate:
    """Controller actions appear in cycle report."""

    def test_activate_in_report(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        loop = _make_loop(cache=cache, top_k=1, max_changes=1)
        report = loop.run_cycle()

        assert len(report.actions) == 1
        assert report.actions[0].kind == RotationActionKind.ACTIVATE
        assert report.actions[0].symbol == "BTCUSDT"


class TestDeterminism:
    """Same inputs produce identical cycle reports."""

    def test_repeated_cycles_on_fresh_loop(self) -> None:
        def run() -> CycleReport:
            cache = TuningCache(ttl_s=300.0)
            cache.put("BTCUSDT", _tuned("BTCUSDT"))
            cache.put("ETHUSDT", _tuned("ETHUSDT"))
            loop = _make_loop(cache=cache, top_k=2, max_changes=2)
            return loop.run_cycle()

        r1 = run()
        r2 = run()
        assert r1.discovered == r2.discovered
        assert r1.admitted == r2.admitted
        assert r1.skipped == r2.skipped
        assert len(r1.actions) == len(r2.actions)


class TestEmptyUniverse:
    """Empty universe produces empty report, no crash."""

    def test_empty(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        loop = _make_loop(
            cache=cache,
            fetcher=lambda _url, _timeout: {"symbols": []},
        )
        report = loop.run_cycle()

        assert report.discovered == []
        assert report.admitted == []
        assert report.actions == []
        assert report.error is None


class TestCycleCounter:
    """Cycle counter increments monotonically."""

    def test_increments(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        loop = _make_loop(cache=cache)

        r1 = loop.run_cycle()
        r2 = loop.run_cycle()
        r3 = loop.run_cycle()

        assert r1.cycle == 1
        assert r2.cycle == 2
        assert r3.cycle == 3
        assert loop.cycle == 3


class TestExternalStop:
    """External stop halts the loop."""

    def test_stop_and_resume_blocked(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        loop = _make_loop(cache=cache)

        loop.run_cycle()
        loop.stop("operator_requested")

        report = loop.run_cycle()
        assert report.error is not None
        assert loop.stopped is True
        assert loop.stop_reason == "operator_requested"
