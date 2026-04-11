"""Periodic tuning refresh for autonomous mode (ADR-162).

Background daemon thread that re-tunes the bounded bootstrap candidate set
and atomically replaces AutonomousTuningState. Keeps TuningCache warm,
selector features fresh, and bridge config updated for future activations.

Does NOT hot-swap geometry of already-running engines.

Dynamic bootstrap discovery (PR-1, 2026-04-11): optionally runs a bounded
coarse→prefilter scan on every refresh cycle to identify newly eligible
post-startup symbols. **PR-1 is discovery-only:** the selected list is
logged and stored on ``self._last_dynamic_candidates``, but the shared
tuning state is not mutated. Merging newly tuned symbols into
``AutonomousTuningState.candidates`` is the job of PR-2.
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_DEFAULT_REFRESH_INTERVAL_S = 240.0
_DEFAULT_DYNAMIC_BOOTSTRAP_COARSE_LIMIT = 100
_DEFAULT_DYNAMIC_BOOTSTRAP_TUNE_LIMIT = 30
_DEFAULT_DYNAMIC_BOOTSTRAP_MAX_NEW_PER_CYCLE = 5


class TuningRefresher:
    """Background tuning refresh daemon thread.

    Periodically retunes the bootstrap candidate set and updates:
    - TuningCache entries
    - AutonomousTuningState (selector features + tuning results)
    - Bridge per-symbol config (for future activations, not active engines)

    When ``universe_provider`` + ``coarse_select_fn`` + ``prefilter_fn`` are
    supplied, it also runs bounded dynamic candidate discovery on each cycle
    to identify post-startup symbols that have become prefilter-eligible.
    **Discovery-only in PR-1**: the resulting list is logged and stored on
    ``self._last_dynamic_candidates`` but never merged into shared state.
    """

    def __init__(
        self,
        *,
        state: Any,  # AutonomousTuningState
        cache: Any,  # TuningCache
        bridge: Any,  # LiveEngineBridge
        registry: Any,  # EngineRegistry
        args: Any,  # argparse.Namespace
        interval_s: float = _DEFAULT_REFRESH_INTERVAL_S,
        # --- Dynamic bootstrap discovery (PR-1) ---
        universe_provider: Any = None,
        blacklist: frozenset[str] = frozenset(),
        coarse_select_fn: Callable[..., list[str]] | None = None,
        prefilter_fn: Callable[..., list[str]] | None = None,
        dynamic_bootstrap_coarse_limit: int = _DEFAULT_DYNAMIC_BOOTSTRAP_COARSE_LIMIT,
        dynamic_bootstrap_tune_limit: int = _DEFAULT_DYNAMIC_BOOTSTRAP_TUNE_LIMIT,
        dynamic_bootstrap_max_new_per_cycle: int = _DEFAULT_DYNAMIC_BOOTSTRAP_MAX_NEW_PER_CYCLE,
    ) -> None:
        self._state = state
        self._cache = cache
        self._bridge = bridge
        self._registry = registry
        self._args = args
        self._interval_s = interval_s
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle_count = 0
        # Dynamic bootstrap discovery (PR-1)
        self._universe_provider = universe_provider
        self._blacklist = blacklist
        self._coarse_select_fn = coarse_select_fn
        self._prefilter_fn = prefilter_fn
        self._dynamic_bootstrap_coarse_limit = dynamic_bootstrap_coarse_limit
        self._dynamic_bootstrap_tune_limit = dynamic_bootstrap_tune_limit
        self._dynamic_bootstrap_max_new_per_cycle = dynamic_bootstrap_max_new_per_cycle
        # Most recent dynamic discovery result. Observational only in PR-1;
        # PR-2 will consume this to drive the tuning solver.
        self._last_dynamic_candidates: list[str] = []

    @property
    def state(self) -> Any:
        return self._state

    def start(self) -> None:
        """Start the background refresh thread."""
        self._thread = threading.Thread(
            target=self._run_loop,
            name="tuning-refresher",
            daemon=True,
        )
        self._thread.start()
        logger.info("TUNING_REFRESHER_STARTED interval_s=%.0f", self._interval_s)

    def stop(self) -> None:
        """Signal the refresh thread to stop."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            logger.info("TUNING_REFRESHER_STOPPED cycles=%d", self._cycle_count)

    def _run_loop(self) -> None:
        """Main refresh loop. Runs until stop event is set."""
        # Initial equity fetch so day risk has data before first full refresh
        import contextlib  # noqa: PLC0415

        with contextlib.suppress(Exception):
            self._update_equity()
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._interval_s)
            if self._stop_event.is_set():
                break
            try:
                self._run_one_cycle()
                self._cycle_count += 1
            except Exception as e:
                logger.warning(
                    "TUNING_REFRESH_FAILED cycle=%d error=%s",
                    self._cycle_count,
                    e,
                )

    def _run_one_cycle(self) -> None:
        """Execute one refresh cycle. Fail-open: exceptions propagate to _run_loop."""
        from grinder.observability.latency_telemetry import PhaseTimer  # noqa: PLC0415
        from grinder.selector.feature_provider import (  # noqa: PLC0415
            fetch_selection_features,
            fetch_selection_features_v2,
        )

        # Dynamic bootstrap discovery (PR-1). Isolated try/except so a
        # discovery failure cannot poison the main refresh path. Runs even
        # when the current candidate set is empty so operators can observe
        # what WOULD be picked up if PR-2 merge semantics existed.
        try:
            self._discover_new_candidates()
        except Exception as e:
            logger.warning("DYNAMIC_BOOTSTRAP_DISCOVERY_FAILED error=%s", e)

        candidates = self._state.candidates
        if not candidates:
            return

        timer = PhaseTimer()
        mainnet = getattr(self._args, "mainnet", False)

        # Refresh exchange truth first so retuning uses current capital base.
        self._update_equity()
        symbol_risk_budget = self._derive_symbol_risk_budget()
        if symbol_risk_budget is None or symbol_risk_budget <= Decimal("0"):
            logger.warning("TUNING_REFRESH_ABORT reason=no_real_risk_budget")
            return

        logger.info("TUNING_REFRESH_START candidates=%d", len(candidates))

        # Fetch features
        v1_features = fetch_selection_features(candidates, mainnet=mainnet)
        natr_map: dict[str, Decimal] = {sym: f.natr_14_5m for sym, f in v1_features.items()}

        # Stage tuning results (no cache writes yet)
        tuned_results, tuned_sizes, tuning_order_sizes, all_results = self._retune_symbols(
            candidates, natr_map, symbol_risk_budget
        )

        # Fetch V2 features with real order sizes
        v2_features: dict[str, Any] = {}
        if tuned_sizes:
            v2_features = fetch_selection_features_v2(
                list(tuned_sizes.keys()),
                tuning_order_sizes=tuning_order_sizes,
                max_notional_per_order=None,
                mainnet=mainnet,
            )

        # Commit phase: cache → state → bridge (all-or-nothing on exception)
        for sym, result in all_results:
            self._cache.put(sym, result)

        self._state.replace(
            tuned_results=tuned_results,
            tuned_sizes=tuned_sizes,
            natr_map=natr_map,
            v1_features=v1_features,
            v2_features=v2_features,
        )

        self._update_bridge(tuned_results, tuned_sizes, natr_map)
        self._update_equity()

        logger.info(
            "TUNING_REFRESH_COMPLETE tuned=%d total=%d elapsed_ms=%d version=%d",
            len(tuned_sizes),
            len(candidates),
            timer.elapsed_ms(),
            self._state.version,
        )

    def _discover_new_candidates(self) -> list[str]:
        """Bounded post-startup discovery of newly eligible symbols (PR-1).

        Runs the same two-stage filter the cold bootstrap uses:
          1. Coarse top-N by 24h quote volume
          2. Prefilter (volume floor, NATR floor, adaptive spacing, blacklist)

        Then subtracts already-tracked candidates in
        ``AutonomousTuningState.candidates`` and keeps the top-K remaining
        as the "selected" dynamic bootstrap set. The selected list is
        stored on ``self._last_dynamic_candidates`` and logged via
        ``DYNAMIC_BOOTSTRAP_DISCOVERY_RESULT``. **PR-1 does not tune or
        merge the selected symbols** — that is PR-2's job.

        Discovery is disabled (no-op) if any of the injected dependencies
        are missing: ``universe_provider``, ``coarse_select_fn``, or
        ``prefilter_fn``. This keeps backwards compatibility for tests
        that construct ``TuningRefresher`` without these plumbing args.

        Returns:
            List of newly discovered symbols not already tracked, bounded
            by ``dynamic_bootstrap_max_new_per_cycle``. Empty list when
            discovery is disabled or no new candidates found.
        """
        if (
            self._universe_provider is None
            or self._coarse_select_fn is None
            or self._prefilter_fn is None
        ):
            # Discovery plumbing not wired — stay quiet.
            return []

        logger.info("DYNAMIC_BOOTSTRAP_DISCOVERY_START")

        try:
            discovered = self._universe_provider.get_candidates()
        except Exception as e:
            logger.warning("DYNAMIC_BOOTSTRAP_UNIVERSE_FETCH_FAILED error=%s", e)
            self._last_dynamic_candidates = []
            return []

        if not discovered:
            logger.info(
                "DYNAMIC_BOOTSTRAP_DISCOVERY_RESULT discovered=0 coarse=0 "
                "prefiltered=0 new=0 selected=0"
            )
            self._last_dynamic_candidates = []
            return []

        mainnet = getattr(self._args, "mainnet", False)
        testnet = not mainnet

        # Coarse: top-N by 24h volume. Same cadence/semantics as cold bootstrap.
        try:
            coarse = self._coarse_select_fn(
                discovered,
                self._dynamic_bootstrap_coarse_limit,
                testnet=testnet,
            )
        except Exception as e:
            logger.warning("DYNAMIC_BOOTSTRAP_COARSE_SELECT_FAILED error=%s", e)
            self._last_dynamic_candidates = []
            return []

        # Prefilter: volume + NATR + spacing + blacklist gates. Same semantics.
        try:
            prefiltered = self._prefilter_fn(
                coarse,
                limit=self._dynamic_bootstrap_tune_limit,
                mainnet=mainnet,
                blacklist=self._blacklist,
            )
        except Exception as e:
            logger.warning("DYNAMIC_BOOTSTRAP_PREFILTER_FAILED error=%s", e)
            self._last_dynamic_candidates = []
            return []

        # Subtract already-tracked candidates — we only care about NEW ones.
        known = set(self._state.candidates)
        new_symbols = [sym for sym in prefiltered if sym not in known]

        # Bounded selection: keep the top-K (prefilter preserves rank order).
        selected = new_symbols[: self._dynamic_bootstrap_max_new_per_cycle]

        logger.info(
            "DYNAMIC_BOOTSTRAP_DISCOVERY_RESULT discovered=%d coarse=%d "
            "prefiltered=%d new=%d selected=%d",
            len(discovered),
            len(coarse),
            len(prefiltered),
            len(new_symbols),
            len(selected),
        )
        if selected:
            logger.info(
                "DYNAMIC_BOOTSTRAP_DISCOVERY_SELECTED symbols=%s",
                ",".join(selected),
            )

        self._last_dynamic_candidates = selected
        return selected

    def _retune_symbols(
        self,
        candidates: list[str],
        natr_map: dict[str, Decimal],
        symbol_risk_budget: Decimal,
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, Decimal], list[tuple[str, Any]]]:
        """Run tuning solver for candidates. Returns (results, sizes, order_sizes, all_results).

        Does NOT commit to TuningCache — caller commits after full cycle succeeds.
        """
        from grinder.execution.constraint_provider import (  # noqa: PLC0415
            ConstraintProvider,
            ConstraintProviderConfig,
        )
        from grinder.risk.grid_policy import DEFAULT_GRID_POLICY  # noqa: PLC0415
        from grinder.selector.spacing import compute_adaptive_spacing_bps  # noqa: PLC0415
        from grinder.tuning.solver import TuningSolverConfig, TuningStatus, solve  # noqa: PLC0415

        testnet = not getattr(self._args, "mainnet", False)
        exchange_info_url = (
            "https://testnet.binancefuture.com/fapi/v1/exchangeInfo"
            if testnet
            else "https://fapi.binance.com/fapi/v1/exchangeInfo"
        )
        try:
            from scripts.http_measured_client import RequestsHttpClient  # noqa: PLC0415

            provider = ConstraintProvider(
                http_client=RequestsHttpClient(port_name="tuning_refresh"),
                config=ConstraintProviderConfig(
                    cache_ttl_seconds=3600,
                    allow_fetch=True,
                    exchange_info_url=exchange_info_url,
                ),
            )
            constraints = provider.get_constraints()
        except Exception:
            constraints = {}

        policy = DEFAULT_GRID_POLICY

        tuned_results: dict[str, Any] = {}
        tuned_sizes: dict[str, str] = {}
        tuning_order_sizes: dict[str, Decimal] = {}
        all_results: list[tuple[str, Any]] = []

        for symbol in candidates:
            price = self._fetch_price(symbol, testnet)
            if price is None or price <= 0:
                continue
            sc = constraints.get(symbol) if constraints else None
            if sc is None:
                continue

            # Fail-closed: skip symbol if NATR unavailable
            natr_val = natr_map.get(symbol)
            if natr_val is None:
                continue
            spacing_pct = compute_adaptive_spacing_bps(natr_val) / Decimal("10000")

            config = TuningSolverConfig(
                max_position_usd=symbol_risk_budget,
                entry_levels_per_side=policy.live_entry_levels_per_side,
                max_inventory_levels=policy.max_inventory_levels,
                adverse_depth_levels=policy.adverse_depth_levels,
                spacing_pct=spacing_pct,
            )
            result = solve(symbol, sc, price, config)
            all_results.append((symbol, result))

            if result.status == TuningStatus.TUNED and result.order_size is not None:
                tuned_sizes[symbol] = str(result.order_size)
                tuned_results[symbol] = result
                tuning_order_sizes[symbol] = result.order_size

        return tuned_results, tuned_sizes, tuning_order_sizes, all_results

    def _update_bridge(
        self,
        tuned_results: dict[str, Any],
        tuned_sizes: dict[str, str],
        natr_map: dict[str, Decimal],
    ) -> None:
        """Update bridge config for symbols not currently active."""
        from grinder.selector.spacing import compute_adaptive_spacing_bps  # noqa: PLC0415

        for sym, size in tuned_sizes.items():
            # Skip symbols with active engines
            try:
                state = self._registry.get_state(sym)
                if state is not None and state.value in ("ACTIVE", "ACTIVATING", "GRACEFUL_EXIT"):
                    logger.debug("TUNING_REFRESH_BRIDGE_SKIP symbol=%s reason=%s", sym, state.value)
                    continue
            except Exception:
                pass  # fail-open: update anyway if registry check fails

            self._bridge.set_symbol_size(sym, size)
            result = tuned_results.get(sym)
            if result and result.tick_size and result.step_size:
                self._bridge.set_symbol_grid_config(
                    sym,
                    tick_size=str(result.tick_size),
                    step_size=str(result.step_size),
                )
            if result and result.max_position_notional_usd is not None:
                self._bridge.set_symbol_risk_caps(
                    sym,
                    max_inventory_notional_usd=result.max_position_notional_usd,
                    max_order_notional_usd=result.max_position_notional_usd,
                )
            natr_val = natr_map.get(sym)
            if natr_val is not None:
                self._bridge.set_symbol_spacing(sym, compute_adaptive_spacing_bps(natr_val))

    def _update_equity(self) -> None:
        """Fetch equity, risk base, and gross exposure, push to bridge. Fail-open."""
        from grinder.runtime.account_truth import (  # noqa: PLC0415
            fetch_futures_equity,
            fetch_futures_gross_exposure,
            fetch_futures_risk_base,
        )

        testnet = not getattr(self._args, "mainnet", False)
        equity = fetch_futures_equity(testnet=testnet)
        if equity is not None and equity > 0:
            self._bridge.update_equity(equity)
        risk_base = fetch_futures_risk_base(testnet=testnet)
        if risk_base is not None and risk_base > 0:
            self._bridge.update_risk_base(risk_base)
        gross = fetch_futures_gross_exposure(testnet=testnet)
        if gross is not None:
            self._bridge.update_gross_exposure(gross)

    def _derive_symbol_risk_budget(self) -> Decimal | None:
        """Derive current autonomous symbol risk budget from real bridge facts."""
        from grinder.risk.autonomous_risk_budget import (  # noqa: PLC0415
            derive_autonomous_portfolio_snapshot,
        )

        risk_base = self._bridge.last_known_risk_base
        gross = self._bridge.last_known_gross_exposure
        if risk_base is None or risk_base <= Decimal("0") or gross is None:
            return None
        active_count = len(self._registry.list_present())
        snapshot = derive_autonomous_portfolio_snapshot(
            equity=risk_base,
            gross_exposure_used_usd=gross,
            active_symbol_count=active_count,
        )
        if snapshot.per_symbol_risk_budget_usd <= Decimal("0"):
            return None
        logger.info(
            "TUNING_REFRESH_RISK_BASE risk_base=%s gross_exposure=%s active=%d "
            "per_symbol_risk_budget_usd=%s",
            risk_base,
            gross,
            active_count,
            snapshot.per_symbol_risk_budget_usd,
        )
        return snapshot.per_symbol_risk_budget_usd

    @staticmethod
    def _fetch_price(symbol: str, testnet: bool) -> Decimal | None:
        """Fetch current price from REST. Fail-open.

        Symbol is percent-encoded before URL interpolation so non-ASCII
        symbol payloads don't trip ``http.client``'s ASCII encoding on the
        HTTP request line — mirrors the cold bootstrap ``_fetch_price_rest``.
        """
        import json  # noqa: PLC0415
        import urllib.parse  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        base = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        url = f"{base}/fapi/v1/ticker/price?symbol={urllib.parse.quote(symbol, safe='')}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
                return Decimal(data["price"])
        except Exception:
            return None
