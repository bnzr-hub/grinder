#!/usr/bin/env python3
"""Autonomous multi-symbol control-plane runner with real engine host.

Assembles and runs the autonomous orchestration loop. Execution ceremonies
are wired to AutonomousEngineHost (ADR-147/148) — real per-symbol engine
lifecycle via injectable factory/stop/cleanup callables.

Control-plane (real):
  UniverseProvider → prefilter → tuning → ranking → SymbolOrchestrator → AutonomousLoop

Execution-plane (real via host):
  EngineRegistry ↔ AutonomousEngineHost → ExecutionCoordinator

Usage:
    # Shadow-only (default — no engine execution)
    python3 -m scripts.run_autonomous --symbols BTCUSDT,ETHUSDT

    # With execution enabled (requires explicit ACK)
    python3 -m scripts.run_autonomous --symbols BTCUSDT,ETHUSDT \
        --execution-enabled --execution-ack

    # Universe override (restrict discovery)
    python3 -m scripts.run_autonomous --symbols BTCUSDT

    # Custom cycle interval
    python3 -m scripts.run_autonomous --cycle-interval-s 30

Env vars:
    Standard trading env vars apply (BINANCE_API_KEY, etc.)
    No hidden flags required — all config via CLI args.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.request
from decimal import Decimal
from typing import Any

logger = logging.getLogger("autonomous")

_CONSTRAINT_CACHE_TTL_HOURLY_S = 3600
_ZERO = Decimal("0")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Grinder autonomous multi-symbol runtime")
    p.add_argument(
        "--symbols", default="", help="Comma-separated symbol override (empty=auto-discover)"
    )
    p.add_argument("--blacklist", default="", help="Comma-separated blacklist")
    p.add_argument("--cycle-interval-s", type=float, default=60.0, help="Seconds between cycles")
    p.add_argument("--top-k", type=int, default=3, help="Max simultaneous active symbols")
    p.add_argument("--max-changes-per-cycle", type=int, default=1, help="Bounded rotation changes")
    p.add_argument(
        "--execution-enabled",
        action="store_true",
        help="Enable execution-plane (default: shadow-only)",
    )
    p.add_argument("--execution-ack", action="store_true", help="Operator ACK for execution")
    p.add_argument("--max-cycles", type=int, default=None, help="Stop after N cycles (for testing)")
    p.add_argument(
        "--exchange-port",
        default="noop",
        choices=["noop", "futures"],
        help="Exchange port for engine threads: noop (default) or futures (requires API keys).",
    )
    p.add_argument("--mainnet", action="store_true", help="Use mainnet (default: testnet)")
    p.add_argument("--armed", action="store_true", help="Arm engines for write operations")
    p.add_argument(
        "--max-notional-per-order",
        default="100",
        help="Max notional per order in USD (default 100).",
    )
    p.add_argument(
        "--max-orders-per-run",
        type=int,
        default=500,
        help="Max orders per engine instance (default 500).",
    )
    return p


# ---------------------------------------------------------------------------
# Cold-start tuning bootstrap (ADR-152)
# ---------------------------------------------------------------------------


def _current_utc_session_key() -> str:
    """Return current UTC date as session key for day rollover."""
    import datetime  # noqa: PLC0415

    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")


def _fetch_price_rest(symbol: str, testnet: bool = True) -> Decimal | None:
    """Fetch current price from Binance Futures REST API (no auth needed)."""
    from decimal import Decimal  # noqa: PLC0415

    base = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
    url = f"{base}/fapi/v1/ticker/price?symbol={symbol}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return Decimal(data["price"])
    except Exception as e:
        logger.warning("BOOTSTRAP_PRICE_FETCH_FAILED symbol=%s error=%s", symbol, e)
        return None


def _fetch_quote_volume_24h_map(testnet: bool = True) -> dict[str, Decimal]:
    """Fetch 24h quote volume map for all futures symbols.

    Used only for bootstrap subset ranking: a single cheap bulk call lets us
    prefer liquid symbols over the provider's alphabetical discovery order.
    Fail-open: callers should fall back to original order on any exception.
    """
    base = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
    url = f"{base}/fapi/v1/ticker/24hr"
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read())

    volume_map: dict[str, Decimal] = {}
    if not isinstance(data, list):
        return volume_map

    for entry in data:
        if not isinstance(entry, dict):
            continue
        symbol = entry.get("symbol")
        quote_volume = entry.get("quoteVolume")
        if not isinstance(symbol, str) or quote_volume is None:
            continue
        try:
            volume_map[symbol] = Decimal(str(quote_volume))
        except Exception:
            continue
    return volume_map


def _select_bootstrap_subset(
    discovered: list[str],
    limit: int,
    *,
    testnet: bool,
) -> list[str]:
    """Select a bounded startup subset for bootstrap tuning.

    Prefers symbols with higher 24h quote volume so the bootstrap-tuned set is
    operationally useful, instead of taking the first alphabetical slice.
    Fail-open: if volume ranking fails, preserve the provider order.
    """
    if limit <= 0 or not discovered:
        return []

    try:
        volume_map = _fetch_quote_volume_24h_map(testnet=testnet)
    except Exception as e:
        logger.warning(
            "BOOTSTRAP_UNIVERSE_VOLUME_RANK_FAILED error=%s — falling back to discovery order",
            e,
        )
        return discovered[:limit]

    ranked = sorted(
        discovered,
        key=lambda sym: (-volume_map.get(sym, Decimal("0")), sym),
    )
    subset = ranked[:limit]
    if subset:
        logger.info(
            "BOOTSTRAP_UNIVERSE_RANKED_BY_24H_VOLUME limit=%d top=%s",
            limit,
            ",".join(subset[:5]),
        )
    return subset


def _bootstrap_tuning_cache(
    symbols: list[str],
    cache: Any,
    args: argparse.Namespace,
    *,
    natr_map: dict[str, Decimal] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Populate TuningCache with REST-fetched prices + constraint solver.

    Runs once at startup before the autonomous loop starts.
    Fail-open: if price fetch or solver fails, symbol stays un-tuned.

    Returns: (sizes, results) where sizes is symbol→order_size string,
    results is symbol→TuningResult for successfully tuned symbols.
    """
    from grinder.execution.constraint_provider import (  # noqa: PLC0415
        ConstraintProvider,
        ConstraintProviderConfig,
    )
    from grinder.observability.latency_telemetry import PhaseTimer, log_bootstrap  # noqa: PLC0415
    from grinder.tuning.solver import TuningSolverConfig, solve  # noqa: PLC0415
    from scripts.http_measured_client import RequestsHttpClient  # noqa: PLC0415

    bootstrap_timer = PhaseTimer()
    testnet = not getattr(args, "mainnet", False)
    logger.info("BOOTSTRAP_TUNING_START symbols=%s testnet=%s", symbols, testnet)

    # Refresh constraints at most once per hour. If the cache is stale, fetch fresh
    # exchangeInfo and update the cache; if fetch fails, ConstraintProvider still
    # fails open to the stale cache.
    exchange_info_url = (
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo"
        if testnet
        else "https://fapi.binance.com/fapi/v1/exchangeInfo"
    )
    provider = ConstraintProvider(
        http_client=RequestsHttpClient(port_name="constraint_fetch"),
        config=ConstraintProviderConfig(
            cache_ttl_seconds=_CONSTRAINT_CACHE_TTL_HOURLY_S,
            allow_fetch=True,
            exchange_info_url=exchange_info_url,
        ),
    )
    constraints = provider.get_constraints()
    if not constraints:
        logger.warning("BOOTSTRAP_TUNING_NO_CONSTRAINTS — solver will use zero constraints")

    # Wire solver config from bridge/runtime ladder geometry (same SSOT)
    from grinder.runtime.live_engine_bridge import BridgeConfig  # noqa: PLC0415
    from grinder.selector.spacing import compute_adaptive_spacing_bps  # noqa: PLC0415

    bridge_cfg = BridgeConfig()  # defaults match what bridge actually uses
    _static_spacing_pct = Decimal(str(bridge_cfg.spacing_bps)) / Decimal("10000")
    _natr = natr_map or {}
    tuned_sizes: dict[str, str] = {}
    tuned_results: dict[str, Any] = {}
    tuned_count = 0
    for symbol in symbols:
        price = _fetch_price_rest(symbol, testnet=testnet)
        if price is None or price <= 0:
            logger.warning("BOOTSTRAP_TUNING_NO_PRICE symbol=%s", symbol)
            continue

        sc = constraints.get(symbol) if constraints else None
        if sc is None:
            logger.warning("BOOTSTRAP_TUNING_NO_CONSTRAINTS symbol=%s — skipped", symbol)
            continue

        # Per-symbol adaptive spacing from NATR (fall back to static if unavailable)
        natr_val = _natr.get(symbol)
        if natr_val is not None:
            adaptive_bps = compute_adaptive_spacing_bps(natr_val)
            spacing_pct = adaptive_bps / Decimal("10000")
        else:
            spacing_pct = _static_spacing_pct

        config = TuningSolverConfig(
            entry_levels_per_side=bridge_cfg.levels,
            spacing_pct=spacing_pct,
        )
        result = solve(symbol, sc, price, config)
        cache.put(symbol, result)

        from grinder.tuning.solver import TuningStatus  # noqa: PLC0415

        if result.status == TuningStatus.TUNED:
            tuned_count += 1
            tuned_sizes[symbol] = str(result.order_size)
            tuned_results[symbol] = result
            logger.info(
                "BOOTSTRAP_TUNED symbol=%s price=%s order_size=%s spacing_pct=%s",
                symbol,
                price,
                result.order_size,
                spacing_pct,
            )
        else:
            logger.info(
                "BOOTSTRAP_NO_GO symbol=%s price=%s reason=%s",
                symbol,
                price,
                result.reason.value if result.reason else "UNKNOWN",
            )

    logger.info("BOOTSTRAP_TUNING_COMPLETE tuned=%d total=%d", tuned_count, len(symbols))
    log_bootstrap(bootstrap_timer.elapsed_ms(), len(symbols), tuned_count)
    return tuned_sizes, tuned_results


def _apply_bootstrap_prefilter(
    coarse_symbols: list[str],
    *,
    limit: int = 30,
    mainnet: bool = True,
    blacklist: frozenset[str] = frozenset(),
) -> list[str]:
    """Apply live-tradability gates to coarse bootstrap slice.

    Fetches selector features for the coarse set, then filters by:
    - rolling 12x5m volume floor ($2M)
    - NATR(14, 5m) floor (1.0%)
    - adaptive spacing tradability (>= 50 bps)
    - blacklist

    Returns top `limit` survivors sorted by 24h volume (from features).
    Fail-open: returns coarse_symbols[:limit] if feature fetch fails.
    """
    if not coarse_symbols:
        return []

    try:
        from grinder.selector.feature_provider import fetch_selection_features  # noqa: PLC0415
        from grinder.selector.prefilter import (  # noqa: PLC0415
            DEFAULT_NATR_5M_MIN,
            DEFAULT_VOLUME_LAST_12X5M_MIN,
        )
        from grinder.selector.spacing import (  # noqa: PLC0415
            DEFAULT_MIN_SPACING_BPS,
            compute_adaptive_spacing_bps,
        )

        features = fetch_selection_features(coarse_symbols, mainnet=mainnet)
    except Exception as e:
        logger.warning(
            "BOOTSTRAP_PREFILTER_FEATURE_FETCH_FAILED error=%s — using coarse slice",
            e,
        )
        return coarse_symbols[:limit]

    survivors: list[tuple[Decimal, str]] = []
    skip_counts: dict[str, int] = {}

    for sym in coarse_symbols:
        if sym in blacklist:
            skip_counts["BLACKLISTED"] = skip_counts.get("BLACKLISTED", 0) + 1
            continue
        feat = features.get(sym)
        if feat is None:
            skip_counts["NO_FEATURES"] = skip_counts.get("NO_FEATURES", 0) + 1
            continue
        if feat.quote_volume_last_12x5m < DEFAULT_VOLUME_LAST_12X5M_MIN:
            skip_counts["LOW_VOLUME"] = skip_counts.get("LOW_VOLUME", 0) + 1
            continue
        if feat.natr_14_5m < DEFAULT_NATR_5M_MIN:
            skip_counts["LOW_NATR"] = skip_counts.get("LOW_NATR", 0) + 1
            continue
        spacing = compute_adaptive_spacing_bps(feat.natr_14_5m)
        if spacing < DEFAULT_MIN_SPACING_BPS:
            skip_counts["LOW_SPACING"] = skip_counts.get("LOW_SPACING", 0) + 1
            continue
        survivors.append((feat.quote_volume_last_12x5m, sym))

    # Sort by rolling volume descending, take top limit
    survivors.sort(key=lambda t: (-t[0], t[1]))
    result = [sym for _vol, sym in survivors[:limit]]

    logger.info(
        "BOOTSTRAP_PREFILTER eligible=%d skipped=%d skip_reasons=%s",
        len(result),
        sum(skip_counts.values()),
        skip_counts or "none",
    )

    # Fail-open: if zero survivors, fall back to coarse slice
    if not result:
        logger.warning(
            "BOOTSTRAP_PREFILTER_EMPTY_FALLBACK count=%d reason=no_survivors",
            min(limit, len(coarse_symbols)),
        )
        return coarse_symbols[:limit]

    return result


def _fetch_natr_map(symbols: list[str], *, mainnet: bool) -> dict[str, Decimal]:
    """Fetch NATR map from selector features for adaptive spacing. Fail-open."""
    try:
        from grinder.selector.feature_provider import fetch_selection_features  # noqa: PLC0415

        feats = fetch_selection_features(symbols, mainnet=mainnet)
        return {sym: f.natr_14_5m for sym, f in feats.items()}
    except Exception as e:
        logger.warning("BOOTSTRAP_NATR_FETCH_FAILED error=%s — using static spacing", e)
        return {}


# ---------------------------------------------------------------------------
# Engine lifecycle bridge (real LiveEngineV0 via background threads)
# ---------------------------------------------------------------------------


def _build_engine_bridge(args: argparse.Namespace) -> Any:
    """Build the LiveEngineBridge with config from CLI args."""
    from grinder.runtime.live_engine_bridge import BridgeConfig, LiveEngineBridge  # noqa: PLC0415

    mode = "live_trade" if args.armed and args.exchange_port == "futures" else "read_only"
    ws_transport = getattr(args, "_ws_transport", None)
    return LiveEngineBridge(
        config=BridgeConfig(
            mode=mode,
            armed=args.armed,
            use_testnet=not args.mainnet,
            exchange_port=args.exchange_port,
            enable_user_data=args.exchange_port == "futures" and args.armed,
            max_notional_per_order=args.max_notional_per_order,
            max_orders_per_run=args.max_orders_per_run,
            ws_transport=ws_transport,
        )
    )


# ---------------------------------------------------------------------------
# Runtime assembly
# ---------------------------------------------------------------------------


def _build_v2_selector(
    state: Any,  # AutonomousTuningState — read at call time, not construction time
    tuning_cache: Any,
    blacklist: frozenset[str],
    max_notional_per_order: str = "100",
) -> tuple[Any, Any]:
    """Build V2 prefilter and ranker closures backed by dynamic shared state.

    Closures read from AutonomousTuningState on every invocation so that
    periodic tuning refresh is automatically visible to selector.
    """
    from decimal import Decimal as _D  # noqa: PLC0415

    from grinder.selector.prefilter import prefilter_v1  # noqa: PLC0415
    from grinder.selector.ranker import rank_v1, rank_v2  # noqa: PLC0415

    _max_notional = _D(max_notional_per_order)

    def prefilter(candidates: list[str]) -> list[str]:
        eligible, _skipped = prefilter_v1(
            candidates,
            tuning_results={
                sym: tuning_cache.get(sym)
                for sym in candidates
                if tuning_cache.get(sym) is not None
            },
            features=state.v1_features,  # dynamic read
            blacklist=blacklist,
            max_notional_per_order=_max_notional,
        )
        return eligible

    def ranker(candidates: list[str]) -> list[str]:
        scored = rank_v2(candidates, state.v2_features)  # dynamic read
        if scored:
            return [s.symbol for s in scored]
        scored_v1 = rank_v1(candidates, state.v1_features)  # dynamic fallback
        if scored_v1:
            logger.warning(
                "SELECTOR_V2_FALLBACK_TO_V1 candidates=%d reason=no_v2_features",
                len(candidates),
            )
        return [s.symbol for s in scored_v1]

    return prefilter, ranker


def _propagate_tuning_to_bridge(
    bridge: Any,
    tuned_sizes: dict[str, str],
    tuned_results: dict[str, Any],
    natr_map: dict[str, Decimal],
) -> None:
    """Propagate tuning-resolved per-symbol config to bridge."""
    from grinder.selector.spacing import compute_adaptive_spacing_bps  # noqa: PLC0415

    for sym, size in tuned_sizes.items():
        bridge.set_symbol_size(sym, size)
        result = tuned_results.get(sym)
        if result and result.tick_size and result.step_size:
            bridge.set_symbol_grid_config(
                sym,
                tick_size=str(result.tick_size),
                step_size=str(result.step_size),
            )
        natr_val = natr_map.get(sym)
        if natr_val is not None:
            bridge.set_symbol_spacing(sym, compute_adaptive_spacing_bps(natr_val))


def _build_tuning_state_and_selector(
    tuned_results: dict[str, Any],
    tuned_sizes: dict[str, str],
    natr_map: dict[str, Decimal],
    symbols_override: frozenset[str],
    mainnet: bool,
    args: Any,
    tuning_cache: Any,
    blacklist: frozenset[str],
    bridge: Any,
    registry: Any,
) -> tuple[Any, Any, Any, Any]:
    """Build shared tuning state, refresher, and selector closures (ADR-162)."""
    from grinder.tuning.autonomous_state import AutonomousTuningState  # noqa: PLC0415
    from grinder.tuning.refresher import TuningRefresher  # noqa: PLC0415

    candidates = list(tuned_results.keys()) or (
        sorted(symbols_override) if symbols_override else []
    )
    v1_features, v2_features = _fetch_initial_selector_features(
        tuned_results, mainnet, args.max_notional_per_order
    )

    state = AutonomousTuningState(
        candidates=candidates,
        tuned_results=tuned_results,
        tuned_sizes=tuned_sizes,
        natr_map=natr_map,
        v1_features=v1_features,
        v2_features=v2_features,
    )

    prefilter, ranker = _build_v2_selector(
        state, tuning_cache, blacklist, max_notional_per_order=args.max_notional_per_order
    )

    refresher = TuningRefresher(
        state=state, cache=tuning_cache, bridge=bridge, registry=registry, args=args
    )

    return state, refresher, prefilter, ranker


def _build_portfolio_allocator() -> Any:
    """Build PortfolioBudgetAllocator with default config."""
    from grinder.risk.portfolio_budget_allocator import PortfolioBudgetAllocator  # noqa: PLC0415

    return PortfolioBudgetAllocator()


def _build_day_risk_manager() -> Any:
    """Build DayRiskManager with default config."""
    from grinder.risk.day_risk_manager import DayRiskManager  # noqa: PLC0415

    return DayRiskManager()


def _build_grid_risk_sizer() -> Any:
    """Build GridRiskSizer with default config."""
    from grinder.risk.grid_risk_sizer import GridRiskSizer  # noqa: PLC0415

    return GridRiskSizer()


def _build_risk_admission_gate(
    day_risk_manager: Any,
    portfolio_allocator: Any,
    grid_sizer: Any,
) -> Any:
    """Build RiskAdmissionGate combining all risk planners."""
    from grinder.risk.risk_admission import RiskAdmissionGate  # noqa: PLC0415

    return RiskAdmissionGate(
        day_risk_manager=day_risk_manager,
        portfolio_allocator=portfolio_allocator,
        grid_sizer=grid_sizer,
    )


def _fetch_initial_selector_features(
    tuned_results: dict[str, Any],
    mainnet: bool,
    max_notional_per_order: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch initial V1+V2 selector features for bootstrap tuned symbols."""
    from grinder.selector.feature_provider import (  # noqa: PLC0415
        fetch_selection_features,
        fetch_selection_features_v2,
    )

    symbols = list(tuned_results.keys())
    if not symbols:
        return {}, {}
    v1_features = fetch_selection_features(symbols, mainnet=mainnet)
    order_sizes: dict[str, Decimal] = {}
    for sym, result in tuned_results.items():
        if result.order_size is not None:
            order_sizes[sym] = result.order_size
    v2_features = fetch_selection_features_v2(
        symbols,
        tuning_order_sizes=order_sizes,
        max_notional_per_order=Decimal(max_notional_per_order),
        mainnet=mainnet,
    )
    return v1_features, v2_features


def build_runtime(args: argparse.Namespace) -> dict:  # type: ignore[type-arg]  # noqa: PLR0915
    """Assemble the full autonomous runtime graph."""
    from grinder.execution_plane.coordinator import ExecutionCoordinator  # noqa: PLC0415
    from grinder.execution_plane.operator import OperatorControls  # noqa: PLC0415
    from grinder.execution_plane.registry import EngineRegistry  # noqa: PLC0415
    from grinder.orchestration.autonomous_loop import (  # noqa: PLC0415
        AutonomousLoop,
        AutonomousLoopConfig,
    )
    from grinder.orchestration.symbol_orchestrator import SymbolOrchestrator  # noqa: PLC0415
    from grinder.orchestration.universe_provider import (  # noqa: PLC0415
        UniverseProvider,
        UniverseProviderConfig,
    )
    from grinder.rotation.controller import RotationConfig, RotationController  # noqa: PLC0415
    from grinder.runtime.autonomous_host import AutonomousEngineHost  # noqa: PLC0415
    from grinder.runtime.live_engine_bridge import BridgeConfig  # noqa: PLC0415
    from grinder.tuning.cache import TuningCache  # noqa: PLC0415

    # Parse symbols/blacklist
    symbols_override = frozenset(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    blacklist = frozenset(s.strip().upper() for s in args.blacklist.split(",") if s.strip())

    # Control-plane components
    universe_config = UniverseProviderConfig(blacklist=blacklist)
    universe_provider = UniverseProvider(config=universe_config)

    tuning_cache = TuningCache(ttl_s=300.0)

    # Cold-start tuning bootstrap (ADR-152): fetch REST prices and run solver
    # so TuningCache is populated BEFORE the first autonomous loop cycle.
    # Without this, the loop sees CACHE_MISS for every symbol → tuned=0 → no activation.
    _tuned_sizes: dict[str, str] = {}
    _tuned_results: dict[str, Any] = {}
    _natr_map: dict[str, Decimal] = {}
    mainnet = getattr(args, "mainnet", False)
    if symbols_override:
        # Fetch NATR for adaptive spacing before bootstrap (fail-open)
        _natr_map = _fetch_natr_map(sorted(symbols_override), mainnet=mainnet)
        _tuned_sizes, _tuned_results = _bootstrap_tuning_cache(
            sorted(symbols_override), tuning_cache, args, natr_map=_natr_map
        )
    else:
        # Auto-discovery mode: two-stage bootstrap selection.
        # Stage 1: coarse slice by 24h volume (cheap, broad)
        # Stage 2: prefilter by live tradability (volume, NATR, spacing)
        _BOOTSTRAP_COARSE_LIMIT = 100
        _BOOTSTRAP_TUNE_LIMIT = 30
        testnet = not mainnet
        try:
            discovered = universe_provider.get_candidates()
        except Exception:
            logger.warning("BOOTSTRAP_UNIVERSE_DISCOVERY_FAILED — continuing without bootstrap")
            discovered = []
        coarse_subset = _select_bootstrap_subset(
            discovered,
            _BOOTSTRAP_COARSE_LIMIT,
            testnet=testnet,
        )
        # Stage 2: fetch features and apply bootstrap prefilter
        bootstrap_subset = _apply_bootstrap_prefilter(
            coarse_subset,
            limit=_BOOTSTRAP_TUNE_LIMIT,
            mainnet=mainnet,
            blacklist=blacklist,
        )
        logger.info(
            "BOOTSTRAP_UNIVERSE_DISCOVERED count=%d coarse=%d prefiltered=%d",
            len(discovered),
            len(coarse_subset),
            len(bootstrap_subset),
        )
        if bootstrap_subset:
            _natr_map = _fetch_natr_map(bootstrap_subset, mainnet=mainnet)
            _tuned_sizes, _tuned_results = _bootstrap_tuning_cache(
                bootstrap_subset, tuning_cache, args, natr_map=_natr_map
            )

    rotation_controller = RotationController(
        config=RotationConfig(
            top_k=args.top_k,
            max_changes_per_cycle=args.max_changes_per_cycle,
            min_hold_cycles=5,
        )
    )

    orchestrator = SymbolOrchestrator(cache=tuning_cache, controller=rotation_controller)

    loop_config = AutonomousLoopConfig(
        cycle_interval_s=args.cycle_interval_s,
        operator_universe_override=symbols_override,
    )

    # Execution-plane components
    registry = EngineRegistry()
    operator_controls = OperatorControls()

    # Real engine host with LiveEngineBridge (ADR-147/148/149)
    bridge = _build_engine_bridge(args)
    _propagate_tuning_to_bridge(bridge, _tuned_sizes, _tuned_results, _natr_map)
    host = AutonomousEngineHost(
        registry=registry,
        engine_factory=bridge.factory,
        engine_stop_fn=bridge.stop,
        engine_cleanup_fn=bridge.cleanup,
        graceful_exit_fn=bridge.graceful_exit,
    )

    # Coordinator with real host bindings
    coordinator = ExecutionCoordinator(
        activate_fn=host.activate,
        graceful_exit_fn=host.request_graceful_exit,
        deactivate_fn=host.finalize_deactivation,
    )

    # Shared tuning/selector state + refresher (ADR-162)
    _tuning_state, refresher, _prefilter, _ranker = _build_tuning_state_and_selector(
        _tuned_results,
        _tuned_sizes,
        _natr_map,
        symbols_override,
        mainnet,
        args,
        tuning_cache,
        blacklist,
        bridge,
        registry,
    )

    # Risk admission gate — filters ranked candidates before orchestrator
    day_risk_manager = _build_day_risk_manager()
    portfolio_allocator = _build_portfolio_allocator()
    grid_sizer = _build_grid_risk_sizer()
    admission_gate = _build_risk_admission_gate(day_risk_manager, portfolio_allocator, grid_sizer)
    # Mutable dict for sharing cycle-level risk state between facts_fn and ranker
    _risk_state: dict[str, Any] = {}

    # Build constraint provider for admission gate (same config as bootstrap)
    from grinder.execution.constraint_provider import (  # noqa: PLC0415
        ConstraintProvider,
        ConstraintProviderConfig,
    )
    from scripts.http_measured_client import RequestsHttpClient  # noqa: PLC0415

    _exchange_info_url = (
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo"
        if not mainnet
        else "https://fapi.binance.com/fapi/v1/exchangeInfo"
    )
    _constraint_provider = ConstraintProvider(
        http_client=RequestsHttpClient(port_name="admission_constraints"),
        config=ConstraintProviderConfig(
            cache_ttl_seconds=_CONSTRAINT_CACHE_TTL_HOURLY_S,
            allow_fetch=True,
            exchange_info_url=_exchange_info_url,
        ),
    )

    _bridge_cfg = BridgeConfig()
    _default_entry_levels = _bridge_cfg.levels

    def _admission_ranker(candidates: list[str]) -> list[str]:
        """Rank candidates via V2 ranker, then filter through risk admission.

        Already-live symbols bypass admission (active engines unaffected).
        Only new candidates are filtered through risk gates.
        """
        ranked: list[str] = _ranker(candidates)

        # If risk state not yet populated (first cycle before facts_fn),
        # fall through without filtering — fail-open.
        day_state = _risk_state.get("day_state")
        portfolio_snap = _risk_state.get("portfolio_snapshot")
        equity = _risk_state.get("equity")
        if day_state is None or portfolio_snap is None or equity is None:
            return ranked

        # Split: already-live symbols pass through, new candidates go through gate
        live = host.live_symbols
        new_candidates = [s for s in ranked if s not in live]

        if not new_candidates:
            return ranked  # all live — nothing to gate

        # Build price + step_pct maps for new candidates only
        prices: dict[str, Decimal] = {}
        step_pcts: dict[str, Decimal] = {}
        natr_map = _tuning_state.natr_map
        for sym in new_candidates:
            feat = _tuning_state.v1_features.get(sym)
            if feat is not None and hasattr(feat, "mid_price"):
                mid = feat.mid_price
                if mid > _ZERO:
                    prices[sym] = mid
            natr_val = natr_map.get(sym)
            if natr_val is not None:
                from grinder.selector.spacing import (  # noqa: PLC0415
                    compute_adaptive_spacing_bps,
                )

                step_pcts[sym] = compute_adaptive_spacing_bps(natr_val) / Decimal("10000")

        # Constraints from provider (cached, hourly refresh)
        try:
            constraint_map = _constraint_provider.get_constraints()
        except Exception:
            constraint_map = {}

        admitted_new, _decisions = admission_gate.filter_candidates(
            new_candidates,
            prices=prices,
            step_pcts=step_pcts,
            entry_levels=_default_entry_levels,
            constraints=constraint_map,
            day_state=day_state,
            portfolio_snapshot=portfolio_snap,
        )

        # Reconstruct ranked list: live symbols keep position, new filtered
        admitted_set = set(admitted_new)
        return [s for s in ranked if s in live or s in admitted_set]

    # Assemble autonomous loop with execution integration
    loop = AutonomousLoop(
        universe_provider=universe_provider,
        orchestrator=orchestrator,
        config=loop_config,
        prefilter_fn=_prefilter,
        ranker_fn=_admission_ranker,
        execution_coordinator=coordinator,
        execution_registry=registry,
        execution_operator=operator_controls,
        execution_enabled=args.execution_enabled,
        execution_acknowledged=args.execution_ack,
    )

    return {
        "loop": loop,
        "host": host,
        "bridge": bridge,
        "registry": registry,
        "operator": operator_controls,
        "coordinator": coordinator,
        "tuning_cache": tuning_cache,
        "universe_provider": universe_provider,
        "refresher": refresher,
        "day_risk_manager": day_risk_manager,
        "portfolio_allocator": portfolio_allocator,
        "grid_sizer": grid_sizer,
        "admission_gate": admission_gate,
        "tuning_state": _tuning_state,
        "risk_state": _risk_state,
    }


def _build_cycle_facts(runtime: dict) -> Any:  # type: ignore[type-arg]
    """Build per-cycle hook for DayRiskManager + PortfolioBudgetAllocator.

    Stores latest day_state and portfolio_snapshot into runtime["risk_state"]
    so the admission gate can read current risk state each cycle.

    Uses real gross exposure from refresher. Market regime stays NEUTRAL until
    live engines expose their controller/regime.py RegimeDecision upstream.
    """
    day_risk_manager = runtime.get("day_risk_manager")
    portfolio_allocator = runtime.get("portfolio_allocator")
    bridge = runtime["bridge"]
    host = runtime["host"]
    risk_state = runtime.get("risk_state", {})

    def _cycle_facts() -> None:
        if day_risk_manager is None:
            return
        try:
            equity = bridge.last_known_equity
            if equity is None or equity <= 0:
                return
            session_key = _current_utc_session_key()
            day_state = day_risk_manager.update(equity, session_key=session_key)
            risk_state["day_state"] = day_state
            risk_state["equity"] = equity

            if portfolio_allocator is not None:
                from grinder.risk.portfolio_budget_allocator import (  # noqa: PLC0415
                    MarketRegime,
                    PortfolioBudgetInput,
                )

                active_count = len(host.live_symbols) if hasattr(host, "live_symbols") else 0

                # Market regime: NEUTRAL until live engines expose their
                # controller/regime.py RegimeDecision upstream. The existing
                # per-symbol classifier is correct but currently lives inside
                # engine threads — no shared portfolio-level aggregation path yet.
                regime = MarketRegime.NEUTRAL

                # Real gross exposure from refresher (fail-open to 0)
                gross_exposure = bridge.last_known_gross_exposure or _ZERO

                inp = PortfolioBudgetInput(
                    equity=equity,
                    day_mode=day_state.mode,
                    market_regime=regime,
                    active_symbol_count=active_count,
                    gross_exposure_used_usd=gross_exposure,
                )
                budget = portfolio_allocator.compute(inp)
                risk_state["portfolio_snapshot"] = budget
                logger.debug(
                    "PORTFOLIO_BUDGET_STATUS mode=%s regime=%s equity=%s active=%d "
                    "slots_free=%d risk_pct=%s risk_usd=%s entries_allowed=%s "
                    "gross_used_usd=%s gross_free_usd=%s",
                    budget.day_mode.value,
                    budget.market_regime.value,
                    budget.equity,
                    budget.active_symbol_count,
                    budget.remaining_symbol_slots,
                    budget.effective_risk_pct,
                    budget.per_symbol_risk_budget_usd,
                    budget.new_entries_allowed,
                    budget.gross_exposure_used_usd,
                    budget.gross_exposure_free_usd,
                )
        except Exception:
            pass

    return _cycle_facts


def main() -> None:
    args = build_parser().parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Startup banner
    exec_mode = "EXECUTION_ENABLED" if args.execution_enabled else "SHADOW_ONLY"
    ack_status = "ACK=true" if args.execution_ack else "ACK=false"
    symbols_desc = args.symbols if args.symbols else "auto-discover"
    ceremonies = (
        "real (AutonomousEngineHost)" if args.execution_enabled else "shadow (no engine lifecycle)"
    )

    net = "mainnet" if args.mainnet else "testnet"
    print(
        f"\nGRINDER AUTONOMOUS SYSTEM starting."
        f"\n  pid={os.getpid()}"
        f"\n  mode={exec_mode} {ack_status}"
        f"\n  execution_ceremonies={ceremonies}"
        f"\n  exchange_port={args.exchange_port} net={net} armed={args.armed}"
        f"\n  symbols={symbols_desc}"
        f"\n  blacklist={args.blacklist or 'none'}"
        f"\n  top_k={args.top_k} max_changes={args.max_changes_per_cycle}"
        f"\n  cycle_interval={args.cycle_interval_s}s"
    )

    if args.execution_enabled and not args.execution_ack:
        print("  WARNING: execution enabled but not acknowledged — will run shadow-only")

    # Build runtime
    runtime = build_runtime(args)
    loop = runtime["loop"]
    host = runtime["host"]
    refresher = runtime.get("refresher")

    # Start tuning refresher (ADR-162)
    if refresher is not None:
        refresher.start()

    # Signal handling
    def handle_stop(*_: object) -> None:
        loop.stop("signal_received")

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    print("\nGRINDER AUTONOMOUS SYSTEM running. Press Ctrl+C to stop.\n")

    # Day risk + portfolio budget — shadow update each cycle
    _cycle_facts = _build_cycle_facts(runtime)

    # Run
    try:
        reports = loop.run_forever(
            facts_fn=_cycle_facts,
            clock=time.monotonic,
            sleep_fn=time.sleep,
            max_cycles=args.max_cycles,
        )
        print(f"\nGRINDER AUTONOMOUS SYSTEM finished. cycles={len(reports)} pid={os.getpid()}")
    except Exception as e:
        logger.error("AUTONOMOUS_SYSTEM_FATAL error=%s", e)
        sys.exit(1)
    finally:
        # Stop tuning refresher
        if refresher is not None:
            refresher.stop()
        # Shutdown all host-owned engines safely
        if host.live_symbols:
            logger.info("HOST_SHUTDOWN_START live_symbols=%s", sorted(host.live_symbols))
            shutdown_report = host.shutdown_all()
            if not shutdown_report.clean:
                logger.warning(
                    "HOST_SHUTDOWN_PARTIAL_FAILURE failed=%s",
                    shutdown_report.failed,
                )
        else:
            logger.info("HOST_SHUTDOWN_SKIP reason=no_live_engines")


if __name__ == "__main__":
    main()
