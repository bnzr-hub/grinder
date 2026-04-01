"""Tests for SymbolOrchestrator (PR-C1b, ADR-128).

Covers:
- Tuned symbols admitted through cache
- Cache miss excluded with reason
- Expired cache excluded
- NO_GO cached result excluded
- Order preserved after filtering
- Controller actions pass through
- Top-K respected via controller
- Determinism
- No side effects on cache
- Empty candidates
"""

from __future__ import annotations

from decimal import Decimal

from grinder.orchestration.symbol_orchestrator import (
    OrchestratorDecision,
    SkipReason,
    SymbolOrchestrator,
)
from grinder.rotation.controller import (
    RotationActionKind,
    RotationConfig,
    RotationController,
    SymbolFacts,
)
from grinder.tuning.cache import TuningCache
from grinder.tuning.solver import NoGoReason, TuningResult, TuningStatus


def _tuned(symbol: str) -> TuningResult:
    return TuningResult(
        symbol=symbol,
        status=TuningStatus.TUNED,
        order_size=Decimal("0.001"),
        tick_size=Decimal("0.10"),
        step_size=Decimal("0.001"),
        max_inventory_levels=5,
    )


def _no_go(symbol: str) -> TuningResult:
    return TuningResult(
        symbol=symbol,
        status=TuningStatus.NO_GO,
        reason=NoGoReason.PRICE_UNAVAILABLE,
    )


def _make_orchestrator(
    cache: TuningCache | None = None,
    top_k: int = 3,
    max_changes: int = 3,
) -> SymbolOrchestrator:
    c = cache or TuningCache(ttl_s=300.0)
    ctrl = RotationController(
        config=RotationConfig(
            top_k=top_k,
            max_changes_per_cycle=max_changes,
            min_hold_cycles=0,
        )
    )
    return SymbolOrchestrator(cache=c, controller=ctrl)


class TestTunedAdmitted:
    """Only TUNED symbols pass through cache filter."""

    def test_tuned_admitted(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))
        cache.put("ETHUSDT", _tuned("ETHUSDT"))

        orch = _make_orchestrator(cache)
        decision = orch.reconcile(["BTCUSDT", "ETHUSDT"])

        assert decision.admitted == ["BTCUSDT", "ETHUSDT"]
        assert decision.skipped == {}


class TestCacheMissExcluded:
    """Missing symbols get CACHE_MISS skip reason."""

    def test_missing_symbol(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        orch = _make_orchestrator(cache)
        decision = orch.reconcile(["BTCUSDT", "UNKNOWN"])

        assert decision.admitted == ["BTCUSDT"]
        assert decision.skipped == {"UNKNOWN": SkipReason.CACHE_MISS}


class TestExpiredCacheExcluded:
    """Expired cache entries are excluded."""

    def test_expired_entry(self) -> None:
        t = [0.0]
        cache = TuningCache(ttl_s=10.0, _clock=lambda: t[0])
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        t[0] = 20.0  # past TTL

        orch = _make_orchestrator(cache)
        decision = orch.reconcile(["BTCUSDT"])

        assert decision.admitted == []
        assert "BTCUSDT" in decision.skipped


class TestNoGoExcluded:
    """NO_GO cached results are excluded with NOT_TUNED reason."""

    def test_no_go_symbol(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))
        cache.put("BADUSDT", _no_go("BADUSDT"))

        orch = _make_orchestrator(cache)
        decision = orch.reconcile(["BTCUSDT", "BADUSDT"])

        assert decision.admitted == ["BTCUSDT"]
        assert decision.skipped == {"BADUSDT": SkipReason.NOT_TUNED}


class TestOrderPreserved:
    """Input ranking order preserved among admitted symbols."""

    def test_order_maintained(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        for s in ["C", "A", "B"]:
            cache.put(s, _tuned(s))

        orch = _make_orchestrator(cache)
        decision = orch.reconcile(["C", "A", "B"])

        assert decision.admitted == ["C", "A", "B"]


class TestControllerActionsPassThrough:
    """Orchestrator surfaces actions from RotationController."""

    def test_activate_action(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        orch = _make_orchestrator(cache)
        decision = orch.reconcile(["BTCUSDT"])

        assert len(decision.actions) == 1
        assert decision.actions[0].kind == RotationActionKind.ACTIVATE
        assert decision.actions[0].symbol == "BTCUSDT"

    def test_graceful_exit_action(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        orch = _make_orchestrator(cache, top_k=1, max_changes=2)
        # Activate BTCUSDT
        orch.reconcile(["BTCUSDT"])
        orch.controller.apply_activation("BTCUSDT")

        # Now remove BTCUSDT from desired set
        decision = orch.reconcile([], {"BTCUSDT": SymbolFacts(is_flat=False)})
        graceful = [
            a for a in decision.actions if a.kind == RotationActionKind.REQUEST_GRACEFUL_EXIT
        ]
        assert len(graceful) == 1


class TestTopKRespected:
    """Top-K limit enforced via controller."""

    def test_top_k_limits_activations(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        for s in ["A", "B", "C", "D"]:
            cache.put(s, _tuned(s))

        orch = _make_orchestrator(cache, top_k=2, max_changes=5)
        decision = orch.reconcile(["A", "B", "C", "D"])

        activate = [a for a in decision.actions if a.kind == RotationActionKind.ACTIVATE]
        assert len(activate) == 2


class TestDeterminism:
    """Same inputs produce identical decisions."""

    def test_repeated_reconcile(self) -> None:
        def run() -> OrchestratorDecision:
            cache = TuningCache(ttl_s=300.0)
            cache.put("A", _tuned("A"))
            cache.put("B", _tuned("B"))
            orch = _make_orchestrator(cache, top_k=2, max_changes=2)
            return orch.reconcile(["A", "B", "C"])

        d1 = run()
        d2 = run()
        assert d1.admitted == d2.admitted
        assert d1.skipped == d2.skipped
        assert len(d1.actions) == len(d2.actions)


class TestNoSideEffectsOnCache:
    """Orchestrator reads cache but does not mutate entries."""

    def test_cache_unchanged(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))

        orch = _make_orchestrator(cache)
        orch.reconcile(["BTCUSDT"])
        orch.reconcile(["BTCUSDT"])

        # Cache still has the entry
        assert cache.get("BTCUSDT") is not None
        assert cache.size == 1


class TestEmptyCandidates:
    """Empty candidate list produces empty decision, no crash."""

    def test_empty(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        orch = _make_orchestrator(cache)
        decision = orch.reconcile([])

        assert decision.admitted == []
        assert decision.skipped == {}
        assert decision.actions == []


class TestMixedCandidates:
    """Mix of tuned, no-go, and missing symbols."""

    def test_mixed(self) -> None:
        cache = TuningCache(ttl_s=300.0)
        cache.put("BTCUSDT", _tuned("BTCUSDT"))
        cache.put("BADUSDT", _no_go("BADUSDT"))
        # UNKNOWN not in cache

        orch = _make_orchestrator(cache)
        decision = orch.reconcile(["BTCUSDT", "BADUSDT", "UNKNOWN"])

        assert decision.admitted == ["BTCUSDT"]
        assert decision.skipped == {
            "BADUSDT": SkipReason.NOT_TUNED,
            "UNKNOWN": SkipReason.CACHE_MISS,
        }
        assert len(decision.actions) == 1
        assert decision.actions[0].kind == RotationActionKind.ACTIVATE
