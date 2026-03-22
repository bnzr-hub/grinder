"""Active symbol selector — Phase 2 (controlled activation, operator universe only).

Extends Phase 1 shadow scoring with:
- Hysteresis (min_hold_cycles, enter/exit threshold band)
- Bounded transitions (max_changes_per_cycle)
- Graceful exit only for grid_v2 non-flat symbols
- Fail-safe fallback to previous stable set on error

Config (env):
    GRINDER_SYMBOL_SELECTOR_ENABLED             (default 0)
    GRINDER_SYMBOL_SELECTOR_K                   (default 3)
    GRINDER_SYMBOL_SELECTOR_CYCLE_S             (default 60)
    GRINDER_SYMBOL_SELECTOR_MIN_HOLD_CYCLES     (default 5)
    GRINDER_SYMBOL_SELECTOR_MAX_CHANGES_PER_CYCLE (default 1)
    GRINDER_SYMBOL_SELECTOR_ENTER_THRESHOLD_BPS (default 0)
    GRINDER_SYMBOL_SELECTOR_EXIT_THRESHOLD_BPS  (default 0)
    + all Phase 1 scoring/gate env vars

See: docs/36_MULTI_SYMBOL_ELIGIBILITY_ML_INTEGRATION_V1.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from grinder.selection.shadow_selector import (
    ShadowSelector,
    ShadowSelectorConfig,
    ShadowSelectorResult,
    get_selector_metrics,
)

if TYPE_CHECKING:
    from grinder.features.types import FeatureSnapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActiveSelectorConfig:
    """Active selector configuration (Phase 2)."""

    enabled: bool = False
    # Scoring config (delegated to ShadowSelector)
    k: int = 3
    cycle_s: int = 60
    min_natr_bps: int = 100
    trend_hard_gate_bps: int = 0
    range_weight_w: float = 1.0
    liquidity_weight_w: float = 1.0
    toxicity_penalty_w: float = 1.0
    trend_penalty_w: float = 1.0
    # Hysteresis
    min_hold_cycles: int = 5
    max_changes_per_cycle: int = 1
    enter_threshold_bps: int = 0
    exit_threshold_bps: int = 0

    def to_shadow_config(self) -> ShadowSelectorConfig:
        """Build Phase 1 shadow config for scoring delegation."""
        return ShadowSelectorConfig(
            enabled=True,
            k=self.k,
            cycle_s=self.cycle_s,
            min_natr_bps=self.min_natr_bps,
            trend_hard_gate_bps=self.trend_hard_gate_bps,
            range_weight_w=self.range_weight_w,
            liquidity_weight_w=self.liquidity_weight_w,
            toxicity_penalty_w=self.toxicity_penalty_w,
            trend_penalty_w=self.trend_penalty_w,
        )


# ---------------------------------------------------------------------------
# Metrics (Phase 2 additions)
# ---------------------------------------------------------------------------

METRIC_ACTIVE_SET_SIZE = "grinder_selector_active_set_size"
METRIC_TRANSITION = "grinder_selector_transition_total"
METRIC_GRACEFUL_EXIT = "grinder_selector_graceful_exit_only_gauge"


@dataclass
class ActiveSelectorMetrics:
    """Phase 2 active selector metrics."""

    active_set_size: int = 0
    transition_total: dict[tuple[str, str], int] = field(default_factory=dict)
    """Counter by (kind, result). kind: add/remove/defer. result: ok/held/budget."""
    graceful_exit_symbols: set[str] = field(default_factory=set)

    def record_transition(self, kind: str, result: str) -> None:
        key = (kind, result)
        self.transition_total[key] = self.transition_total.get(key, 0) + 1

    def to_prometheus_lines(self) -> list[str]:
        lines: list[str] = []

        lines.append(f"# HELP {METRIC_ACTIVE_SET_SIZE} Current active symbol count")
        lines.append(f"# TYPE {METRIC_ACTIVE_SET_SIZE} gauge")
        lines.append(f"{METRIC_ACTIVE_SET_SIZE} {self.active_set_size}")

        lines.append(f"# HELP {METRIC_TRANSITION} Transition events by kind and result")
        lines.append(f"# TYPE {METRIC_TRANSITION} counter")
        for (kind, result), count in sorted(self.transition_total.items()):
            lines.append(f'{METRIC_TRANSITION}{{kind="{kind}",result="{result}"}} {count}')

        lines.append(f"# HELP {METRIC_GRACEFUL_EXIT} Count of symbols in graceful-exit-only mode")
        lines.append(f"# TYPE {METRIC_GRACEFUL_EXIT} gauge")
        lines.append(f"{METRIC_GRACEFUL_EXIT} {len(self.graceful_exit_symbols)}")

        return lines


_ACTIVE_METRICS: ActiveSelectorMetrics | None = None


def get_active_selector_metrics() -> ActiveSelectorMetrics:
    global _ACTIVE_METRICS  # noqa: PLW0603
    if _ACTIVE_METRICS is None:
        _ACTIVE_METRICS = ActiveSelectorMetrics()
    return _ACTIVE_METRICS


def reset_active_selector_metrics() -> None:
    global _ACTIVE_METRICS  # noqa: PLW0603
    _ACTIVE_METRICS = None


# ---------------------------------------------------------------------------
# Active Selector
# ---------------------------------------------------------------------------


@dataclass
class ActiveSelectorResult:
    """Result of an active selector cycle."""

    active_set: set[str]
    added: list[str]
    removed: list[str]
    deferred: list[str]
    graceful_exit_only: set[str]
    shadow_result: ShadowSelectorResult


class ActiveSelector:
    """Active symbol selector (Phase 2, controlled activation).

    Wraps ShadowSelector scoring + adds hysteresis, change budget,
    graceful_exit_only for grid_v2 non-flat symbols.

    Active set mutation is bounded and deterministic.
    On runtime error: fail-safe to previous stable active set.
    """

    def __init__(
        self,
        config: ActiveSelectorConfig,
        initial_active: set[str] | None = None,
    ) -> None:
        self._config = config
        self._shadow = ShadowSelector(config.to_shadow_config())
        self._active_set: set[str] = set(initial_active or set())
        self._graceful_exit_only: set[str] = set()
        self._hold_cycles: dict[str, int] = {}  # symbol -> cycles in active set
        self._cycle_count: int = 0
        self._metrics = get_active_selector_metrics()

    @property
    def config(self) -> ActiveSelectorConfig:
        return self._config

    @property
    def active_set(self) -> set[str]:
        return set(self._active_set)

    @property
    def graceful_exit_only(self) -> set[str]:
        return set(self._graceful_exit_only)

    def update_features(self, snapshot: FeatureSnapshot) -> None:
        """Update cached features for a symbol."""
        self._shadow.update_features(snapshot)

    def maybe_run(
        self,
        now_ts_ms: int,
        operator_universe: list[str],
        non_flat_symbols: set[str] | None = None,
    ) -> ActiveSelectorResult | None:
        """Run selector if cooldown elapsed.

        Args:
            now_ts_ms: Current timestamp in ms.
            operator_universe: Operator-configured symbols.
            non_flat_symbols: Symbols with non-flat grid_v2 inventory (for graceful_exit_only).
        """
        if not self._config.enabled:
            return None

        try:
            # Delegate cooldown check to shadow
            shadow_result = self._shadow.maybe_run(now_ts_ms, operator_universe)
            if shadow_result is None:
                return None

            return self._apply_transitions(
                shadow_result, operator_universe, non_flat_symbols or set()
            )
        except Exception:
            logger.exception("SELECTOR_ACTIVE_ERROR — fail-safe to previous active set")
            metrics = get_selector_metrics()
            metrics.record_cycle("active", "error_failsafe")
            return ActiveSelectorResult(
                active_set=set(self._active_set),
                added=[],
                removed=[],
                deferred=[],
                graceful_exit_only=set(self._graceful_exit_only),
                shadow_result=None,  # type: ignore[arg-type]
            )

    def _apply_transitions(  # noqa: PLR0912, PLR0915
        self,
        shadow_result: ShadowSelectorResult,
        operator_universe: list[str],
        non_flat_symbols: set[str],
    ) -> ActiveSelectorResult:
        """Apply bounded transitions with hysteresis and graceful_exit_only."""
        self._cycle_count += 1
        metrics = self._metrics
        universe_set = set(operator_universe)

        # Target set from scoring
        target = set(shadow_result.selection.selected)

        # Score lookup for threshold checks
        score_by_symbol: dict[str, int] = {}
        for s in shadow_result.selection.scores:
            score_by_symbol[s.symbol] = s.score

        # --- Compute desired adds/removes ---
        desired_add = sorted(target - self._active_set)
        desired_remove = sorted(self._active_set - target)

        # --- Apply enter threshold ---
        if self._config.enter_threshold_bps > 0:
            desired_add = [
                sym
                for sym in desired_add
                if score_by_symbol.get(sym, 0) >= self._config.enter_threshold_bps
            ]

        # --- Apply exit threshold (symbol stays if score above exit threshold) ---
        if self._config.exit_threshold_bps > 0:
            desired_remove = [
                sym
                for sym in desired_remove
                if score_by_symbol.get(sym, 0) < self._config.exit_threshold_bps
            ]

        # --- Apply min_hold_cycles ---
        held: list[str] = []
        final_remove: list[str] = []
        for sym in desired_remove:
            cycles = self._hold_cycles.get(sym, 0)
            if cycles < self._config.min_hold_cycles:
                held.append(sym)
                metrics.record_transition("remove", "held")
            else:
                final_remove.append(sym)

        # --- Apply graceful_exit_only for non-flat symbols ---
        deferred: list[str] = []
        safe_remove: list[str] = []
        for sym in final_remove:
            if sym in non_flat_symbols:
                deferred.append(sym)
                self._graceful_exit_only.add(sym)
                metrics.record_transition("remove", "defer_graceful")
            else:
                safe_remove.append(sym)

        # --- Apply change budget ---
        budget = self._config.max_changes_per_cycle
        actual_add: list[str] = []
        actual_remove: list[str] = []
        changes_used = 0

        for sym in safe_remove:
            if changes_used >= budget:
                metrics.record_transition("remove", "budget_exhausted")
                break
            actual_remove.append(sym)
            changes_used += 1
            metrics.record_transition("remove", "ok")

        for sym in desired_add:
            if changes_used >= budget:
                metrics.record_transition("add", "budget_exhausted")
                break
            actual_add.append(sym)
            changes_used += 1
            metrics.record_transition("add", "ok")

        # --- Mutate active set ---
        for sym in actual_remove:
            self._active_set.discard(sym)
            self._hold_cycles.pop(sym, None)

        for sym in actual_add:
            self._active_set.add(sym)
            self._hold_cycles[sym] = 0

        # Increment hold cycles for all active symbols
        for sym in list(self._active_set):
            self._hold_cycles[sym] = self._hold_cycles.get(sym, 0) + 1

        # Clean up graceful_exit_only for symbols that became flat
        for sym in list(self._graceful_exit_only):
            if sym not in non_flat_symbols:
                self._graceful_exit_only.discard(sym)
                # Now safe to remove from active set if still desired
                if sym not in target and sym in self._active_set:
                    self._active_set.discard(sym)
                    self._hold_cycles.pop(sym, None)
                    actual_remove.append(sym)
                    metrics.record_transition("remove", "ok_post_flat")

        # Safety: never include symbols outside operator universe
        self._active_set &= universe_set

        # Update metrics
        metrics.active_set_size = len(self._active_set)
        metrics.graceful_exit_symbols = set(self._graceful_exit_only)

        selector_metrics = get_selector_metrics()
        selector_metrics.record_cycle("active", "ok")

        logger.info(
            "SELECTOR_ACTIVE_CYCLE active=%d added=%s removed=%s deferred=%s "
            "graceful_exit=%s held=%s budget_used=%d/%d",
            len(self._active_set),
            actual_add,
            actual_remove,
            deferred,
            sorted(self._graceful_exit_only),
            held,
            changes_used,
            budget,
        )

        return ActiveSelectorResult(
            active_set=set(self._active_set),
            added=actual_add,
            removed=actual_remove,
            deferred=deferred,
            graceful_exit_only=set(self._graceful_exit_only),
            shadow_result=shadow_result,
        )

    def is_dispatch_allowed(self, symbol: str) -> bool:
        """Check if new entries are allowed for symbol.

        Returns False for symbols in graceful_exit_only or not in active set.
        Exit protection/reconciliation should continue regardless.
        """
        if symbol in self._graceful_exit_only:
            return False
        if not self._active_set:
            # No active set configured yet — allow all (fail-open)
            return True
        return symbol in self._active_set

    def to_dict(self) -> dict[str, Any]:
        """Serialize state for diagnostics."""
        return {
            "enabled": self._config.enabled,
            "active_set": sorted(self._active_set),
            "graceful_exit_only": sorted(self._graceful_exit_only),
            "hold_cycles": dict(sorted(self._hold_cycles.items())),
            "cycle_count": self._cycle_count,
        }
