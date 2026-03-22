"""Shadow symbol selector — Phase 1 (observability only, no dispatch mutation).

Runs Top-K v1 scoring on accumulated FeatureSnapshots with timestamp-based cooldown.
Emits metrics and logs only; NEVER modifies the active dispatch universe.

Config (env):
    GRINDER_SYMBOL_SELECTOR_SHADOW          (default 0)
    GRINDER_SYMBOL_SELECTOR_K               (default 3)
    GRINDER_SYMBOL_SELECTOR_CYCLE_S         (default 60)
    GRINDER_SYMBOL_SELECTOR_MIN_NATR_BPS    (default 100)
    GRINDER_SYMBOL_SELECTOR_TREND_HARD_GATE_BPS (default 0 = disabled)
    GRINDER_SYMBOL_SELECTOR_RANGE_WEIGHT_W      (default 1.0)
    GRINDER_SYMBOL_SELECTOR_LIQUIDITY_WEIGHT_W  (default 1.0)
    GRINDER_SYMBOL_SELECTOR_TOXICITY_PENALTY_W  (default 1.0)
    GRINDER_SYMBOL_SELECTOR_TREND_PENALTY_W     (default 1.0)

See: docs/36_MULTI_SYMBOL_ELIGIBILITY_ML_INTEGRATION_V1.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from grinder.selection.topk_v1 import (
    SelectionCandidate,
    SelectionResult,
    TopKConfigV1,
    select_topk_v1,
)

if TYPE_CHECKING:
    from grinder.features.types import FeatureSnapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowSelectorConfig:
    """Shadow selector configuration (from env vars)."""

    enabled: bool = False
    k: int = 3
    cycle_s: int = 60
    min_natr_bps: int = 100
    trend_hard_gate_bps: int = 0  # 0 = disabled
    range_weight_w: float = 1.0
    liquidity_weight_w: float = 1.0
    toxicity_penalty_w: float = 1.0
    trend_penalty_w: float = 1.0

    def to_topk_config(self) -> TopKConfigV1:
        """Convert to TopKConfigV1 for scoring."""
        return TopKConfigV1(
            k=self.k,
            w_range=int(self.range_weight_w * 100),
            w_liquidity=int(self.liquidity_weight_w * 100),
            w_toxicity=int(self.toxicity_penalty_w * 100),
            w_trend=int(self.trend_penalty_w * 100),
        )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

# Metric name constants (stable contracts)
METRIC_SELECTOR_CYCLE = "grinder_selector_cycle_total"
METRIC_SELECTOR_CANDIDATE_COUNT = "grinder_selector_candidate_count"
METRIC_SELECTOR_EXCLUDED = "grinder_selector_excluded_total"
METRIC_SELECTOR_CHURN = "grinder_selector_churn_total"
METRIC_SELECTOR_SCORE_BPS = "grinder_selector_score_bps"


@dataclass
class SelectorMetrics:
    """Shadow selector metrics collector."""

    cycle_total: dict[tuple[str, str], int] = field(default_factory=dict)
    """Counter by (mode, result)."""

    candidate_count: dict[str, int] = field(default_factory=dict)
    """Last gauge by stage: input/eligible/selected."""

    excluded_total: dict[str, int] = field(default_factory=dict)
    """Counter by reason code."""

    churn_total: dict[str, int] = field(default_factory=dict)
    """Counter by kind: would_add/would_remove."""

    score_bps: dict[tuple[str, str], int] = field(default_factory=dict)
    """Last gauge by (symbol, component). Capped to top-K + 2 near-cutoff."""

    def record_cycle(self, mode: str, result: str) -> None:
        key = (mode, result)
        self.cycle_total[key] = self.cycle_total.get(key, 0) + 1

    def set_candidate_count(self, stage: str, count: int) -> None:
        self.candidate_count[stage] = count

    def record_excluded(self, reason: str) -> None:
        self.excluded_total[reason] = self.excluded_total.get(reason, 0) + 1

    def record_churn(self, kind: str) -> None:
        self.churn_total[kind] = self.churn_total.get(kind, 0) + 1

    def set_score(self, symbol: str, component: str, value: int) -> None:
        self.score_bps[(symbol, component)] = value

    def to_prometheus_lines(self) -> list[str]:
        """Export metrics in Prometheus text format."""
        lines: list[str] = []

        lines.append(f"# HELP {METRIC_SELECTOR_CYCLE} Total selector cycles")
        lines.append(f"# TYPE {METRIC_SELECTOR_CYCLE} counter")
        for (mode, result), count in sorted(self.cycle_total.items()):
            lines.append(f'{METRIC_SELECTOR_CYCLE}{{mode="{mode}",result="{result}"}} {count}')

        lines.append(f"# HELP {METRIC_SELECTOR_CANDIDATE_COUNT} Candidates per stage")
        lines.append(f"# TYPE {METRIC_SELECTOR_CANDIDATE_COUNT} gauge")
        for stage, count in sorted(self.candidate_count.items()):
            lines.append(f'{METRIC_SELECTOR_CANDIDATE_COUNT}{{stage="{stage}"}} {count}')

        lines.append(f"# HELP {METRIC_SELECTOR_EXCLUDED} Excluded by reason")
        lines.append(f"# TYPE {METRIC_SELECTOR_EXCLUDED} counter")
        for reason, count in sorted(self.excluded_total.items()):
            lines.append(f'{METRIC_SELECTOR_EXCLUDED}{{reason="{reason}"}} {count}')

        lines.append(f"# HELP {METRIC_SELECTOR_CHURN} Hypothetical churn by kind")
        lines.append(f"# TYPE {METRIC_SELECTOR_CHURN} counter")
        for kind, count in sorted(self.churn_total.items()):
            lines.append(f'{METRIC_SELECTOR_CHURN}{{kind="{kind}"}} {count}')

        lines.append(f"# HELP {METRIC_SELECTOR_SCORE_BPS} Score components in bps")
        lines.append(f"# TYPE {METRIC_SELECTOR_SCORE_BPS} gauge")
        for (symbol, component), value in sorted(self.score_bps.items()):
            lines.append(
                f'{METRIC_SELECTOR_SCORE_BPS}{{symbol="{symbol}",component="{component}"}} {value}'
            )

        return lines


_SELECTOR_METRICS: SelectorMetrics | None = None


def get_selector_metrics() -> SelectorMetrics:
    """Get or create the singleton selector metrics instance."""
    global _SELECTOR_METRICS  # noqa: PLW0603
    if _SELECTOR_METRICS is None:
        _SELECTOR_METRICS = SelectorMetrics()
    return _SELECTOR_METRICS


def reset_selector_metrics() -> None:
    """Reset selector metrics (for testing)."""
    global _SELECTOR_METRICS  # noqa: PLW0603
    _SELECTOR_METRICS = None


# ---------------------------------------------------------------------------
# Shadow Selector
# ---------------------------------------------------------------------------

# Cardinality cap: emit score components for top-K + 2 near-cutoff symbols.
_SCORE_EMIT_EXTRA = 2


@dataclass
class ShadowSelectorResult:
    """Result of a shadow selector cycle."""

    selection: SelectionResult
    would_add: list[str]
    would_remove: list[str]
    excluded_reasons: dict[str, list[str]]


class ShadowSelector:
    """Shadow symbol selector (Phase 1, observability only).

    Accumulates FeatureSnapshots per symbol, runs Top-K v1 scoring
    on timestamp-based cooldown. Emits metrics/logs only.

    NEVER modifies the active dispatch universe.
    """

    def __init__(self, config: ShadowSelectorConfig) -> None:
        self._config = config
        self._topk_config = config.to_topk_config()
        self._feature_cache: dict[str, FeatureSnapshot] = {}
        self._last_run_ts_ms: int = -(config.cycle_s * 1000)  # ensures first tick runs
        self._last_selected: set[str] = set()
        self._metrics = get_selector_metrics()

    @property
    def config(self) -> ShadowSelectorConfig:
        return self._config

    @property
    def last_selected(self) -> set[str]:
        return set(self._last_selected)

    def update_features(self, snapshot: FeatureSnapshot) -> None:
        """Update cached features for a symbol (called every tick)."""
        self._feature_cache[snapshot.symbol] = snapshot

    def maybe_run(
        self, now_ts_ms: int, operator_universe: list[str]
    ) -> ShadowSelectorResult | None:
        """Run selector if cooldown elapsed. Returns None if skipped.

        Args:
            now_ts_ms: Current timestamp in ms.
            operator_universe: Operator-configured symbol list.
        """
        if not self._config.enabled:
            return None

        cycle_ms = self._config.cycle_s * 1000
        if now_ts_ms - self._last_run_ts_ms < cycle_ms:
            return None

        self._last_run_ts_ms = now_ts_ms
        return self._run(operator_universe)

    def _run(self, operator_universe: list[str]) -> ShadowSelectorResult:  # noqa: PLR0912
        """Execute one selector cycle."""
        metrics = self._metrics

        # Build candidates from cached features
        candidates: list[SelectionCandidate] = []
        excluded_reasons: dict[str, list[str]] = {}

        universe_set = set(operator_universe)

        for symbol in sorted(universe_set):
            feat = self._feature_cache.get(symbol)
            if feat is None:
                excluded_reasons[symbol] = ["WARMUP_INCOMPLETE"]
                metrics.record_excluded("WARMUP_INCOMPLETE")
                continue

            # Pre-scoring hard gates from doc-36
            reasons: list[str] = []

            if feat.natr_bps < self._config.min_natr_bps:
                reasons.append("NATR_BELOW_MIN")

            if (
                self._config.trend_hard_gate_bps > 0
                and abs(feat.net_return_bps) > self._config.trend_hard_gate_bps
            ):
                reasons.append("TREND_TOO_STRONG")

            if reasons:
                excluded_reasons[symbol] = reasons
                for r in reasons:
                    metrics.record_excluded(r)
                continue

            candidates.append(
                SelectionCandidate(
                    symbol=symbol,
                    range_score=feat.range_score,
                    spread_bps=feat.spread_bps,
                    thin_l1=feat.thin_l1,
                    net_return_bps=feat.net_return_bps,
                    warmup_bars=feat.warmup_bars,
                    toxicity_blocked=False,
                )
            )

        # Run topk_v1
        result = select_topk_v1(candidates, self._topk_config)

        # Collect additional exclusions from topk_v1 gates
        for score in result.scores:
            if score.gates_failed:
                excluded_reasons[score.symbol] = list(score.gates_failed)
                for gate in score.gates_failed:
                    metrics.record_excluded(gate)

        # Compute hypothetical churn
        new_selected = set(result.selected)
        would_add = sorted(new_selected - self._last_selected)
        would_remove = sorted(self._last_selected - new_selected)
        self._last_selected = new_selected

        # Record metrics
        metrics.set_candidate_count("input", len(universe_set))
        metrics.set_candidate_count("eligible", len(candidates) - result.gate_excluded)
        metrics.set_candidate_count("selected", len(result.selected))

        for _ in would_add:
            metrics.record_churn("would_add")
        for _ in would_remove:
            metrics.record_churn("would_remove")

        # Score components — capped to top-K + near-cutoff
        # Clear old score entries to prevent stale symbols
        metrics.score_bps.clear()
        emit_cap = self._config.k + _SCORE_EMIT_EXTRA
        emitted = 0
        for score in result.scores:
            if emitted >= emit_cap:
                break
            if score.gates_failed:
                continue
            metrics.set_score(score.symbol, "total", score.score)
            metrics.set_score(score.symbol, "range", score.range_component)
            metrics.set_score(score.symbol, "liquidity", score.liquidity_component)
            metrics.set_score(score.symbol, "toxicity_penalty", score.toxicity_penalty)
            metrics.set_score(score.symbol, "trend_penalty", score.trend_penalty)
            emitted += 1

        cycle_result = "ok"
        metrics.record_cycle("shadow", cycle_result)

        logger.info(
            "SELECTOR_SHADOW_CYCLE input=%d eligible=%d selected=%d "
            "would_add=%d would_remove=%d selected=%s",
            len(universe_set),
            len(candidates) - result.gate_excluded,
            len(result.selected),
            len(would_add),
            len(would_remove),
            result.selected,
        )

        return ShadowSelectorResult(
            selection=result,
            would_add=would_add,
            would_remove=would_remove,
            excluded_reasons=excluded_reasons,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize last state for diagnostics."""
        return {
            "enabled": self._config.enabled,
            "last_run_ts_ms": self._last_run_ts_ms,
            "last_selected": sorted(self._last_selected),
            "feature_cache_symbols": sorted(self._feature_cache.keys()),
        }
