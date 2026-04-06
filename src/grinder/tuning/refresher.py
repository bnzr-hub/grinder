"""Periodic tuning refresh for autonomous mode (ADR-162).

Background daemon thread that re-tunes the bounded bootstrap candidate set
and atomically replaces AutonomousTuningState. Keeps TuningCache warm,
selector features fresh, and bridge config updated for future activations.

Does NOT hot-swap geometry of already-running engines.
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_REFRESH_INTERVAL_S = 240.0


class TuningRefresher:
    """Background tuning refresh daemon thread.

    Periodically retunes the bootstrap candidate set and updates:
    - TuningCache entries
    - AutonomousTuningState (selector features + tuning results)
    - Bridge per-symbol config (for future activations, not active engines)
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

        candidates = self._state.candidates
        if not candidates:
            return

        timer = PhaseTimer()
        mainnet = getattr(self._args, "mainnet", False)
        max_notional = Decimal(str(self._args.max_notional_per_order))

        logger.info("TUNING_REFRESH_START candidates=%d", len(candidates))

        # Fetch features
        v1_features = fetch_selection_features(candidates, mainnet=mainnet)
        natr_map: dict[str, Decimal] = {sym: f.natr_14_5m for sym, f in v1_features.items()}

        # Stage tuning results (no cache writes yet)
        tuned_results, tuned_sizes, tuning_order_sizes, all_results = self._retune_symbols(
            candidates, natr_map
        )

        # Fetch V2 features with real order sizes
        v2_features: dict[str, Any] = {}
        if tuned_sizes:
            v2_features = fetch_selection_features_v2(
                list(tuned_sizes.keys()),
                tuning_order_sizes=tuning_order_sizes,
                max_notional_per_order=max_notional,
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

    def _retune_symbols(
        self,
        candidates: list[str],
        natr_map: dict[str, Decimal],
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, Decimal], list[tuple[str, Any]]]:
        """Run tuning solver for candidates. Returns (results, sizes, order_sizes, all_results).

        Does NOT commit to TuningCache — caller commits after full cycle succeeds.
        """
        from grinder.execution.constraint_provider import (  # noqa: PLC0415
            ConstraintProvider,
            ConstraintProviderConfig,
        )
        from grinder.runtime.live_engine_bridge import BridgeConfig  # noqa: PLC0415
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

        bridge_cfg = BridgeConfig()
        static_spacing_pct = Decimal(str(bridge_cfg.spacing_bps)) / Decimal("10000")

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

            natr_val = natr_map.get(symbol)
            if natr_val is not None:
                spacing_pct = compute_adaptive_spacing_bps(natr_val) / Decimal("10000")
            else:
                spacing_pct = static_spacing_pct

            config = TuningSolverConfig(
                entry_levels_per_side=bridge_cfg.levels,
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
            natr_val = natr_map.get(sym)
            if natr_val is not None:
                self._bridge.set_symbol_spacing(sym, compute_adaptive_spacing_bps(natr_val))

    def _update_equity(self) -> None:
        """Fetch account equity and gross exposure, push to bridge. Fail-open."""
        testnet = not getattr(self._args, "mainnet", False)
        equity = self._fetch_equity(testnet)
        if equity is not None and equity > 0:
            self._bridge.update_equity(equity)
        gross = self._fetch_gross_exposure(testnet)
        if gross is not None:
            self._bridge.update_gross_exposure(gross)

    @staticmethod
    def _fetch_equity(testnet: bool) -> Decimal | None:
        """Fetch USDT margin balance from REST. Fail-open, no engine dependency."""
        import hashlib  # noqa: PLC0415
        import hmac  # noqa: PLC0415
        import json  # noqa: PLC0415
        import os  # noqa: PLC0415
        import time  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        api_key = os.environ.get("BINANCE_API_KEY", "").strip()
        api_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
        if not api_key or not api_secret:
            return None

        base = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        ts = int(time.time() * 1000)
        query = f"timestamp={ts}&recvWindow=10000"
        sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"{base}/fapi/v2/balance?{query}&signature={sig}"
        try:
            req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                for asset in data:
                    if asset.get("asset") == "USDT":
                        # Compute equity from wallet + unrealized PnL,
                        # matching totalMarginBalance semantics.
                        wallet = Decimal(str(asset.get("crossWalletBalance", "0")))
                        upnl = Decimal(str(asset.get("crossUnPnl", "0")))
                        return wallet + upnl
        except Exception:
            pass
        return None

    @staticmethod
    def _fetch_gross_exposure(testnet: bool) -> Decimal | None:
        """Fetch gross position exposure from REST. Fail-open.

        Returns sum(abs(notional)) across all open positions.
        Uses /fapi/v2/positionRisk — same auth as equity fetch.
        """
        import hashlib  # noqa: PLC0415
        import hmac  # noqa: PLC0415
        import json  # noqa: PLC0415
        import os  # noqa: PLC0415
        import time  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        api_key = os.environ.get("BINANCE_API_KEY", "").strip()
        api_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
        if not api_key or not api_secret:
            return None

        base = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        ts = int(time.time() * 1000)
        query = f"timestamp={ts}&recvWindow=10000"
        sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"{base}/fapi/v2/positionRisk?{query}&signature={sig}"
        try:
            req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                gross = Decimal("0")
                for pos in data:
                    notional = pos.get("notional", "0")
                    try:
                        gross += abs(Decimal(str(notional)))
                    except Exception:
                        continue
                return gross
        except Exception:
            pass
        return None

    @staticmethod
    def _fetch_price(symbol: str, testnet: bool) -> Decimal | None:
        """Fetch current price from REST. Fail-open."""
        import json  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        base = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        url = f"{base}/fapi/v1/ticker/price?symbol={symbol}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
                return Decimal(data["price"])
        except Exception:
            return None
