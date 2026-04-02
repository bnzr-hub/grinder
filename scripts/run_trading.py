#!/usr/bin/env python3
"""Run GRINDER trading loop.

Usage:
    python -m scripts.run_trading --symbols BTCUSDT,ETHUSDT --metrics-port 9090 [--mainnet]

Rehearsal knobs (safe with NoOpExchangePort):
    --armed                 Arm engine gates (lets actions reach fill-prob gate)
    --paper-size-per-level  Override PaperEngine size_per_level (Decimal, e.g. 0.001)
    --paper-spacing-bps     Override grid spacing in bps (default 10.0, lower = tighter grid)
    --paper-levels          Override grid levels per side (default 5)
    --paper-cooldown-ms     Override per-symbol cooldown in ms (default 100)

Exchange port selection:
    --exchange-port noop        Default, no real orders (NoOpExchangePort)
    --exchange-port futures     BinanceFuturesPort (USDT-M). Requires 5 safety gates:
                                1. mode=live_trade  2. --armed  3. ALLOW_MAINNET_TRADE=1
                                4. GRINDER_REAL_PORT_ACK=YES_I_REALLY_WANT_MAINNET
                                5. BINANCE_API_KEY + BINANCE_API_SECRET set
    --max-notional-per-order    Max notional per order in USD (default 100, rehearsal cap)
    --max-orders-per-run        Max orders per port instance (default 500).
                                Values >1 require GRINDER_MAX_ORDERS_ACK=YES_I_ACCEPT_MULTI_ORDER.

HA mode (GRINDER_HA_ENABLED=true):
    - Starts LeaderElector for single-active coordination
    - /readyz returns 200 only when loop_ready AND role==ACTIVE
    - Snapshot processing skipped when not ACTIVE (fail-closed)
    - Elector failure → role stays UNKNOWN → /readyz=503

Env vars:
    GRINDER_TRADING_MODE        read_only (default) | paper | live_trade
    GRINDER_TRADING_LOOP_ACK    Must be YES_I_KNOW for paper/live_trade
    GRINDER_FILL_MODEL_DIR      Path to fill model directory (enables fill-prob gate)
    ALLOW_MAINNET_TRADE         Existing guard (enforced by connector for live_trade)
    GRINDER_REAL_PORT_ACK       Must be YES_I_REALLY_WANT_MAINNET for --exchange-port futures
    GRINDER_MAX_ORDERS_ACK      Must be YES_I_ACCEPT_MULTI_ORDER for --max-orders-per-run >1
    GRINDER_HA_ENABLED          true|1|yes to enable HA leader election
    BINANCE_API_KEY             Required for --exchange-port futures
    BINANCE_API_SECRET          Required for --exchange-port futures

Safety:
    - Default mode is read_only (no write ops).
    - paper / live_trade require explicit ACK env.
    - Default exchange port is NoOp — no real orders placed.
    - --exchange-port futures requires ALL 5 safety gates to pass.
    - --armed only affects the gate chain inside LiveEngineV0._process_action().
      With NoOpExchangePort, arming has zero real-world effect.

Fixture mode (--fixture):
    Pass a JSONL file (one bookTicker JSON object per line) to run
    with canned data instead of a real WebSocket connection.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from grinder.connectors.binance_user_data_ws import (
    BINANCE_FUTURES_MAINNET_URL as USER_DATA_MAINNET_URL,
)
from grinder.connectors.binance_user_data_ws import (
    BINANCE_FUTURES_TESTNET_URL as USER_DATA_TESTNET_URL,
)
from grinder.connectors.binance_user_data_ws import (
    FuturesUserDataWsConnector,
    ListenKeyConfig,
    ListenKeyManager,
    UserDataWsConfig,
)
from grinder.connectors.binance_ws import (
    BINANCE_WS_FUTURES_MAINNET,
    FakeWsTransport,
)
from grinder.connectors.live_connector import (
    LiveConnectorConfig,
    LiveConnectorV0,
    SafeMode,
)
from grinder.env_parse import parse_bool, parse_int
from grinder.execution.binance_futures_port import (
    BINANCE_FUTURES_MAINNET_URL,
    BinanceFuturesPort,
    BinanceFuturesPortConfig,
)
from grinder.execution.constraint_provider import (
    ConstraintProvider,
    ConstraintProviderConfig,
)
from grinder.execution.port import ExchangePort, NoOpExchangePort
from grinder.execution.port_metrics import get_port_metrics
from grinder.features.engine import FeatureEngine, FeatureEngineConfig
from grinder.gating.metrics import get_gating_metrics
from grinder.ha.leader import LeaderElector, LeaderElectorConfig
from grinder.ha.role import HARole, get_ha_state
from grinder.live.config import LiveEngineConfig
from grinder.live.cycle_layer import LiveCycleConfig, LiveCycleLayerV1
from grinder.live.engine import LiveEngineV0
from grinder.ml.fill_model_loader import load_fill_model_v0
from grinder.net.fixture_guard import install_fixture_network_guard
from grinder.observability import (
    build_healthz_body,
    build_metrics_body,
    set_ready_fn,
    set_start_time,
)
from grinder.paper.engine import PaperEngine
from scripts.http_measured_client import RequestsHttpClient, build_measured_client

if TYPE_CHECKING:
    from collections.abc import Callable

    from grinder.execution.engine import SymbolConstraints
    from grinder.live.grid_planner import LiveGridPlannerV1

# Module-level readiness flags.
_loop_ready = False
_ha_enabled = False


def is_trading_ready() -> bool:
    """Check if trading loop is ready.

    Ready requires loop_ready=True AND (HA disabled OR role==ACTIVE).
    """
    if not _loop_ready:
        return False
    if _ha_enabled:
        return get_ha_state().role == HARole.ACTIVE
    return True


def reset_trading_state() -> None:
    """Reset module-level trading state (for test cleanup)."""
    global _loop_ready, _ha_enabled  # noqa: PLW0603
    _loop_ready = False
    _ha_enabled = False


class TradingHealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for health checks and metrics.

    Endpoints:
        /healthz - Always 200 if process alive (liveness)
        /readyz  - 200 if loop_ready AND (HA disabled OR ACTIVE), 503 otherwise
        /metrics - Prometheus metrics
    """

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/healthz":
            self._send_health()
        elif self.path == "/readyz":
            self._send_ready()
        elif self.path == "/metrics":
            self._send_metrics()
        else:
            self.send_error(404)

    def _send_health(self) -> None:
        """Send health check response (always 200 if alive)."""
        body = build_healthz_body()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def _send_ready(self) -> None:
        """Send readiness check (200 if ready, 503 otherwise)."""
        ready = is_trading_ready()
        body = json.dumps(
            {
                "ready": ready,
                "loop_ready": _loop_ready,
                "ha_enabled": _ha_enabled,
                "ha_role": get_ha_state().role.value if _ha_enabled else "n/a",
                "mode": "trading_loop",
            }
        )
        self.send_response(200 if ready else 503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def _send_metrics(self) -> None:
        """Send Prometheus metrics."""
        body = build_metrics_body()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default logging."""
        pass


class _ReusableHTTPServer(HTTPServer):
    """HTTPServer with SO_REUSEADDR set before bind."""

    allow_reuse_address = True


def run_server(port: int) -> HTTPServer:
    """Start HTTP server in background thread with SO_REUSEADDR."""
    set_start_time(time.time())
    server = _ReusableHTTPServer(("0.0.0.0", port), TradingHealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def validate_env() -> SafeMode:
    """Validate trading mode and ACK env vars.

    Returns:
        SafeMode enum value.

    Raises:
        SystemExit: If mode is invalid or ACK is missing for paper/live_trade.
    """
    mode_str = os.environ.get("GRINDER_TRADING_MODE", "read_only").lower()
    try:
        mode = SafeMode(mode_str)
    except ValueError:
        print(
            f"ERROR: GRINDER_TRADING_MODE={mode_str!r} invalid. "
            "Must be: read_only, paper, live_trade"
        )
        sys.exit(1)

    if mode in (SafeMode.PAPER, SafeMode.LIVE_TRADE):
        ack = os.environ.get("GRINDER_TRADING_LOOP_ACK", "")
        if ack != "YES_I_KNOW":
            print(f"ERROR: GRINDER_TRADING_LOOP_ACK must be YES_I_KNOW for mode={mode.value}")
            sys.exit(1)

    if mode == SafeMode.LIVE_TRADE:
        print("  WARNING: live_trade mode requires ALLOW_MAINNET_TRADE=1 (enforced by connector)")

    return mode


def is_ha_enabled() -> bool:
    """Check if HA mode is enabled via environment."""
    return os.environ.get("GRINDER_HA_ENABLED", "").lower() in ("true", "1", "yes")


def start_ha_elector() -> LeaderElector | None:
    """Start HA leader election if enabled.

    Fail-closed: if elector fails to start, role stays UNKNOWN → /readyz=503.

    Returns:
        LeaderElector instance if started, None otherwise.
    """
    if not is_ha_enabled():
        print("  HA mode: DISABLED (set GRINDER_HA_ENABLED=true to enable)")
        return None

    print("  HA mode: ENABLED")
    try:
        config = LeaderElectorConfig()
        print(f"    Redis URL: {config.redis_url}")
        print(f"    Lock TTL: {config.lock_ttl_ms}ms")
        print(f"    Instance ID: {config.instance_id}")
        elector = LeaderElector(config)
        elector.start()
        print("    LeaderElector started")
        return elector
    except Exception as e:
        print(f"    WARNING: Failed to start LeaderElector: {e}")
        print("    Running without HA (role stays UNKNOWN → /readyz=503)")
        return None


def validate_real_port_gates(mode: SafeMode, armed: bool) -> None:
    """Validate all 5 safety gates for real exchange port.

    Gates:
        1. mode == LIVE_TRADE
        2. armed == True
        3. ALLOW_MAINNET_TRADE=1
        4. GRINDER_REAL_PORT_ACK=YES_I_REALLY_WANT_MAINNET
        5. BINANCE_API_KEY + BINANCE_API_SECRET set (checked in build_exchange_port)

    Raises:
        SystemExit: If any gate fails.
    """
    if mode != SafeMode.LIVE_TRADE:
        print(f"ERROR: --exchange-port futures requires mode=live_trade (got {mode.value})")
        sys.exit(1)

    if not armed:
        print("ERROR: --exchange-port futures requires --armed")
        sys.exit(1)

    allow_mainnet = os.environ.get("ALLOW_MAINNET_TRADE", "").lower() in ("1", "true", "yes")
    if not allow_mainnet:
        print("ERROR: --exchange-port futures requires ALLOW_MAINNET_TRADE=1")
        sys.exit(1)

    real_ack = os.environ.get("GRINDER_REAL_PORT_ACK", "")
    if real_ack != "YES_I_REALLY_WANT_MAINNET":
        print(
            "ERROR: --exchange-port futures requires GRINDER_REAL_PORT_ACK=YES_I_REALLY_WANT_MAINNET"
        )
        sys.exit(1)


def validate_max_orders_ack(max_orders: int) -> None:
    """Validate ACK env var for multi-order mode.

    Raises:
        SystemExit: If max_orders > 1 and ACK env var is missing.
    """
    if max_orders <= 1:
        return
    ack = os.environ.get("GRINDER_MAX_ORDERS_ACK", "")
    if ack != "YES_I_ACCEPT_MULTI_ORDER":
        print(
            f"ERROR: --max-orders-per-run {max_orders} requires "
            "GRINDER_MAX_ORDERS_ACK=YES_I_ACCEPT_MULTI_ORDER"
        )
        sys.exit(1)


def build_exchange_port(
    port_name: str,
    mode: SafeMode,
    armed: bool,
    symbols: list[str],
    max_notional: Decimal,
    max_orders_per_run: int = 500,
) -> ExchangePort:
    """Build exchange port by name.

    Args:
        port_name: "noop" or "futures".
        mode: SafeMode for config.
        armed: Whether engine is armed.
        symbols: Trading symbols (used as whitelist for futures).
        max_notional: Max notional per order (for futures config).
        max_orders_per_run: Max orders per port instance (default 500).

    Returns:
        ExchangePort instance.

    Raises:
        SystemExit: If gates fail or API keys missing for futures.
    """
    if port_name == "noop":
        return NoOpExchangePort()

    if port_name == "futures":
        validate_real_port_gates(mode, armed)

        api_key = os.environ.get("BINANCE_API_KEY", "").strip()
        api_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
        if not api_key or not api_secret:
            print("ERROR: --exchange-port futures requires BINANCE_API_KEY and BINANCE_API_SECRET")
            sys.exit(1)

        inner = RequestsHttpClient(port_name="futures")
        http_client = build_measured_client(inner)

        config = BinanceFuturesPortConfig(
            mode=mode,
            base_url=BINANCE_FUTURES_MAINNET_URL,
            api_key=api_key,
            api_secret=api_secret,
            symbol_whitelist=symbols,
            allow_mainnet=True,
            max_notional_per_order=max_notional,
            max_orders_per_run=max_orders_per_run,
        )

        # Compute signed clock offset vs Binance server (WSL2 clock drift workaround).
        # Positive: local clock is ahead; negative: local clock is behind.
        ts_offset_ms = 0
        try:
            resp = urllib.request.urlopen(f"{BINANCE_FUTURES_MAINNET_URL}/fapi/v1/time", timeout=5)
            server_ts = json.loads(resp.read())["serverTime"]
            local_ts = int(time.time() * 1000)
            ts_offset_ms = local_ts - server_ts
            if ts_offset_ms > 0:
                print(f"  CLOCK_OFFSET_MS={ts_offset_ms} (local ahead of Binance)")
            elif ts_offset_ms < 0:
                print(f"  CLOCK_OFFSET_MS={ts_offset_ms} (local behind Binance)")
        except Exception:
            pass  # fail-open: offset stays 0

        return BinanceFuturesPort(
            http_client=http_client, config=config, _ts_offset_ms=ts_offset_ms
        )  # type: ignore[return-value]

    print(f"ERROR: Unknown exchange port: {port_name!r}. Must be: noop, futures")
    sys.exit(1)


def build_connector(
    symbols: list[str],
    mode: SafeMode,
    fixture_path: str | None,
    *,
    use_testnet: bool = True,
    exchange_port: str = "noop",
) -> LiveConnectorV0:
    """Build LiveConnectorV0 with optional fixture transport.

    Args:
        symbols: List of trading symbols.
        mode: SafeMode for the connector.
        fixture_path: Optional path to JSONL fixture file.
        use_testnet: Use testnet WS endpoint (default True for safety).
        exchange_port: Exchange port name ("noop" or "futures").
            When "futures", uses fstream.binance.com for market data.

    Returns:
        Configured LiveConnectorV0 instance.
    """
    ws_transport = None
    if fixture_path:
        with Path(fixture_path).open() as f:
            messages = [line.strip() for line in f if line.strip()]
        ws_transport = FakeWsTransport(messages=messages, delay_ms=100)

    # Explicit ws_url only for futures mainnet (fstream).
    # Testnet and spot mainnet are handled by BinanceWsConfig via use_testnet flag.
    ws_url: str | None = None
    if not use_testnet and exchange_port == "futures":
        ws_url = BINANCE_WS_FUTURES_MAINNET

    net_label = "testnet" if use_testnet else ("futures" if exchange_port == "futures" else "spot")
    effective = ws_url or ("testnet" if use_testnet else "spot-mainnet")
    print(f"  Market data WS: {effective} ({net_label})")

    config = LiveConnectorConfig(
        mode=mode,
        symbols=symbols,
        ws_transport=ws_transport,
        ws_url=ws_url,
        use_testnet=use_testnet,
    )
    return LiveConnectorV0(config=config)


def _build_user_data_connector_or_none(
    *,
    symbols: list[str],
    mode: SafeMode,
    exchange_port: str,
    fixture_path: str | None,
    use_testnet: bool,
) -> FuturesUserDataWsConnector | None:
    """Build optional user-data connector for immediate fill/cancel events.

    Enabled only in live futures mode (non-fixture) and can be toggled via
    GRINDER_USER_DATA_IMMEDIATE_ENABLED (default: true).
    """
    enabled = parse_bool("GRINDER_USER_DATA_IMMEDIATE_ENABLED", default=True, strict=False)
    if not enabled:
        print("  User-data immediate path: disabled (GRINDER_USER_DATA_IMMEDIATE_ENABLED=false)")
        return None
    if mode != SafeMode.LIVE_TRADE or exchange_port != "futures" or fixture_path is not None:
        return None

    api_key = os.environ.get("BINANCE_API_KEY", "").strip()
    if not api_key:
        print("  User-data immediate path: disabled (missing BINANCE_API_KEY)")
        return None

    base_url = USER_DATA_TESTNET_URL if use_testnet else USER_DATA_MAINNET_URL
    symbol_filter = symbols[0] if len(symbols) == 1 else None
    from grinder.connectors.data_connector import TimeoutConfig  # noqa: PLC0415

    ws_cfg = UserDataWsConfig(
        base_url=base_url,
        api_key=api_key,
        use_testnet=use_testnet,
        symbol_filter=symbol_filter,
        timeout=TimeoutConfig(connect_timeout_ms=10000),
    )
    lk_cfg = ListenKeyConfig(base_url=base_url, api_key=api_key)
    lk_mgr = ListenKeyManager(RequestsHttpClient(port_name="user_data"), lk_cfg)
    print(
        f"  User-data immediate path: enabled "
        f"(symbol_filter={symbol_filter or 'none'}, net={'testnet' if use_testnet else 'mainnet'})"
    )
    return FuturesUserDataWsConnector(config=ws_cfg, listen_key_manager=lk_mgr)


def _load_symbol_constraints() -> dict[str, SymbolConstraints] | None:
    """Load symbol constraints from exchange info (fail-open).

    Tries local cache first, then Binance Futures API.
    Returns None if both fail (constraints will be skipped).
    """
    provider = ConstraintProvider(
        config=ConstraintProviderConfig(allow_fetch=False),
    )
    constraints = provider.get_constraints()
    if constraints:
        return constraints

    # Try API fetch (requires network)
    try:
        http = RequestsHttpClient(port_name="constraint_fetch")
        api_provider = ConstraintProvider(
            http_client=http,
            config=ConstraintProviderConfig(allow_fetch=True),
        )
        constraints = api_provider.get_constraints()
        if constraints:
            return constraints
    except Exception as e:
        print(f"  Constraint fetch failed (fail-open): {e}")

    return None


def _run_startup_tuning_shadow(
    symbols: list[str],
    constraints: dict[str, SymbolConstraints] | None,
) -> None:
    """Run shadow tuning at startup and log outcomes.

    Shadow-only: no dispatch mutation, no selector change, no new network I/O.
    Prices are not available at this startup stage — every symbol will get
    SYMBOL_NO_GO reason=PRICE_UNAVAILABLE. This is the correct B3a behavior:
    visibility into constraint readiness without introducing new data acquisition.

    Results are recorded into TuningCache and TuningMetrics (B3b) for
    downstream observability. Neither affects runtime decisions.

    Fail-open: errors are logged but never block startup.
    """
    from grinder.tuning.cache import get_tuning_cache  # noqa: PLC0415
    from grinder.tuning.metrics import get_tuning_metrics  # noqa: PLC0415
    from grinder.tuning.shadow import run_tuning_shadow  # noqa: PLC0415
    from grinder.tuning.solver import TuningSolverConfig  # noqa: PLC0415

    try:
        # Use env var if set; otherwise high sentinel (consistent with engine's "no cap" semantics)
        raw_cap = os.environ.get("GRINDER_MAX_POSITION_USD")
        max_pos_usd = Decimal(raw_cap) if raw_cap else Decimal("999999999")
        max_inv = int(os.environ.get("GRINDER_GRID_V2_MAX_INV_LEVELS", "5"))

        config = TuningSolverConfig(
            max_position_usd=max_pos_usd,
            max_inventory_levels=max_inv,
        )

        # No price source at startup in B3a — pass empty prices.
        # Solver emits PRICE_UNAVAILABLE for each symbol (visible, non-fatal).
        results = run_tuning_shadow(symbols, constraints, {}, config)

        # Record into cache and metrics (B3b — shadow observability only).
        # Cache gauge values (cache_size, expired_total) are synced live
        # by MetricsBuilder._build_tuning_metrics() on each /metrics scrape.
        cache = get_tuning_cache()
        metrics = get_tuning_metrics()
        for r in results:
            cache.put(r.symbol, r)
            metrics.record_result(r)
    except Exception as e:
        logging.getLogger(__name__).warning("TUNING_SHADOW_SKIPPED error=%s", e)


FuturesPreflightStatus = Literal[
    "skipped",
    "passed",
    "constraints_unavailable",
    "symbol_missing",
]


@dataclass(frozen=True)
class FuturesPreflightResult:
    """Result of futures preflight validation."""

    status: FuturesPreflightStatus
    missing_symbols: tuple[str, ...] = ()


GridV2PreflightStatus = Literal[
    "skipped",
    "passed",
    "account_sync_disabled",
]

PippinOrderSizeLockStatus = Literal[
    "skipped",
    "passed",
    "mismatch",
    "invalid",
]


# ---------------------------------------------------------------------------
# Launch Guard v2: preflight → (optional cleanup) → verify → start
# ---------------------------------------------------------------------------

LaunchGuardStatus = Literal[
    "skipped",
    "verify_clean",
    "verify_dirty_no_cleanup",
    "cleanup_then_clean",
    "cleanup_then_still_dirty",
    "verify_error",
    "recovery_non_flat_skip_cleanup",
    "recovery_snapshot_unstable",
]


@dataclass(frozen=True)
class LaunchGuardResult:
    """Result of the launch-guard sequence."""

    status: LaunchGuardStatus
    reason: str
    orders: int = 0
    position: str = "FLAT"


def _snapshot_is_stable(symbol: str) -> bool:
    """Take two snapshots ~1s apart; return True if orders+position match."""
    import time  # noqa: PLC0415

    from scripts.exchange_state import cmd_verify_programmatic  # noqa: PLC0415

    try:
        _, orders1, position1 = cmd_verify_programmatic(symbol)
    except Exception:
        return False
    time.sleep(1)
    try:
        _, orders2, position2 = cmd_verify_programmatic(symbol)
    except Exception:
        return False
    return orders1 == orders2 and position1 == position2


def evaluate_launch_guard(  # noqa: PLR0911
    *,
    exchange_port: str,
    mainnet: bool,
    armed: bool,
    fixture_path: str | None,
    pre_cleanup: bool,
    symbols: list[str],
) -> LaunchGuardResult:
    """Pure launch-guard evaluation (calls exchange_state helpers).

    Sequence: check → recovery policy → (optional cleanup) → verify.
    Only active for futures + mainnet + armed + no-fixture (live mainnet path).

    Recovery policy (crash-safe):
    - position != FLAT → skip cleanup, allow startup for reconstruction
    - position == FLAT + dirty → require stable snapshots before cleanup
    """
    if exchange_port != "futures" or not mainnet or not armed or fixture_path is not None:
        return LaunchGuardResult(status="skipped", reason="not_live_mainnet_futures")

    from scripts.exchange_state import cmd_verify_programmatic  # noqa: PLC0415

    cleanup_performed = False
    recovery_non_flat = False
    for symbol in symbols:
        try:
            ok, orders, position = cmd_verify_programmatic(symbol)
        except Exception as exc:
            return LaunchGuardResult(
                status="verify_error",
                reason=f"symbol={symbol} verify failed: {exc}",
            )
        if ok:
            continue

        # Dirty state detected — apply recovery policy
        is_non_flat = position != "FLAT"

        if is_non_flat:
            # Non-flat position: NEVER cleanup (would close position).
            # Allow startup so grid_v2 reconstruction can recover.
            print(
                f"  RECOVERY_NON_FLAT_SKIP_CLEANUP symbol={symbol} "
                f"orders={orders} position={position}"
            )
            recovery_non_flat = True
            continue

        # FLAT + dirty (orders exist but no position)
        if not pre_cleanup:
            return LaunchGuardResult(
                status="verify_dirty_no_cleanup",
                reason=f"symbol={symbol} not clean, --pre-cleanup not enabled",
                orders=orders,
                position=position,
            )

        # Before cleanup: require stable snapshots (guard against pending fills)
        if not _snapshot_is_stable(symbol):
            return LaunchGuardResult(
                status="recovery_snapshot_unstable",
                reason=(
                    f"symbol={symbol} snapshots unstable (orders/position changed between reads), "
                    f"cleanup unsafe"
                ),
                orders=orders,
                position=position,
            )

        # Cleanup safe: FLAT + stable snapshots
        from scripts.exchange_state import cmd_cleanup  # noqa: PLC0415

        try:
            cmd_cleanup(symbol)
        except Exception as exc:
            return LaunchGuardResult(
                status="verify_error",
                reason=f"symbol={symbol} cleanup failed: {exc}",
            )
        cleanup_performed = True

        # Re-verify after cleanup
        try:
            ok2, orders2, position2 = cmd_verify_programmatic(symbol)
        except Exception as exc:
            return LaunchGuardResult(
                status="verify_error",
                reason=f"symbol={symbol} post-cleanup verify failed: {exc}",
            )
        if not ok2:
            return LaunchGuardResult(
                status="cleanup_then_still_dirty",
                reason=f"symbol={symbol} still dirty after cleanup",
                orders=orders2,
                position=position2,
            )

    if recovery_non_flat:
        return LaunchGuardResult(
            status="recovery_non_flat_skip_cleanup",
            reason="non-flat position detected, cleanup skipped for reconstruction",
        )
    return LaunchGuardResult(
        status="cleanup_then_clean" if cleanup_performed else "verify_clean",
        reason="all symbols clean",
    )


@dataclass(frozen=True)
class GridV2PreflightResult:
    """Result of grid_v2 runtime preflight validation."""

    status: GridV2PreflightStatus


@dataclass(frozen=True)
class PippinOrderSizeLockResult:
    """Result of optional PIPPINUSDT order-size lock preflight."""

    status: PippinOrderSizeLockStatus
    expected: str = "80"
    actual: str = ""


def evaluate_futures_preflight(
    symbols: list[str],
    exchange_port: str,
    fixture_path: str | None,
    constraints: dict[str, SymbolConstraints] | None,
) -> FuturesPreflightResult:
    """Pure futures preflight validation.

    Returns a structured result so callers can decide whether to exit, log, or skip.
    """
    if exchange_port != "futures" or fixture_path is not None:
        return FuturesPreflightResult(status="skipped")

    if constraints is None:
        return FuturesPreflightResult(status="constraints_unavailable")

    missing = tuple(s for s in symbols if s not in constraints)
    if missing:
        return FuturesPreflightResult(status="symbol_missing", missing_symbols=missing)

    return FuturesPreflightResult(status="passed")


def evaluate_grid_v2_account_sync_preflight(
    exchange_port: str,
    fixture_path: str | None,
    grid_v2_enabled: bool,
    account_sync_enabled: bool,
) -> GridV2PreflightResult:
    """Pure grid_v2 preflight validation.

    grid_v2 runtime requires account-sync truth to recover from missed user-data
    events and keep fill/cancel reconciliation deterministic in futures live mode.
    """
    if not grid_v2_enabled or exchange_port != "futures" or fixture_path is not None:
        return GridV2PreflightResult(status="skipped")
    if not account_sync_enabled:
        return GridV2PreflightResult(status="account_sync_disabled")
    return GridV2PreflightResult(status="passed")


def evaluate_pippin_order_size_lock_preflight(
    *,
    exchange_port: str,
    fixture_path: str | None,
    lock_enabled: bool,
    grid_v2_enabled: bool,
    grid_v2_symbol: str,
    order_size_raw: str,
) -> PippinOrderSizeLockResult:
    """Optional fail-closed profile lock for PIPPINUSDT order size.

    Active only when:
    - GRINDER_PIPPIN_ORDER_SIZE_LOCK_ENABLED=1
    - futures mode
    - no fixture
    - grid_v2 enabled for PIPPINUSDT
    """
    if (
        not lock_enabled
        or exchange_port != "futures"
        or fixture_path is not None
        or not grid_v2_enabled
        or grid_v2_symbol != "PIPPINUSDT"
    ):
        return PippinOrderSizeLockResult(status="skipped")

    try:
        order_size = Decimal(order_size_raw)
    except Exception:
        return PippinOrderSizeLockResult(
            status="invalid",
            expected="80",
            actual=order_size_raw,
        )

    if order_size != Decimal("80"):
        return PippinOrderSizeLockResult(
            status="mismatch",
            expected="80",
            actual=order_size_raw,
        )
    return PippinOrderSizeLockResult(status="passed", expected="80", actual=order_size_raw)


def _validate_futures_preflight_or_exit(
    symbols: list[str],
    exchange_port: str,
    fixture_path: str | None,
) -> None:
    """Validate symbols exist on futures venue. Fail-closed.

    No-op unless exchange_port=="futures" and fixture_path is None.
    Exits with code 1 if constraints unavailable or symbols missing.
    """
    constraints = None
    if exchange_port == "futures" and fixture_path is None:
        constraints = _load_symbol_constraints()
    result = evaluate_futures_preflight(
        symbols,
        exchange_port,
        fixture_path,
        constraints,
    )
    if result.status in {"skipped", "passed"}:
        return
    if result.status == "constraints_unavailable":
        print(
            "ERROR: Cannot load futures exchangeInfo for symbol validation. "
            "Futures mode requires symbol constraints to verify WS venue compatibility. "
            "Check var/cache/exchange_info_futures.json or network access."
        )
        sys.exit(1)
    if result.status == "symbol_missing":
        print(
            f"ERROR: symbols {list(result.missing_symbols)} not found in futures exchangeInfo. "
            f"Cannot subscribe to futures WS for unknown symbols. "
            f"Available: {len(constraints or {})} symbols."
        )
        sys.exit(1)


def _validate_grid_v2_account_sync_or_exit(
    exchange_port: str,
    fixture_path: str | None,
) -> None:
    """Fail-closed: primary grid_v2 in futures mode requires AccountSync."""
    result = evaluate_grid_v2_account_sync_preflight(
        exchange_port=exchange_port,
        fixture_path=fixture_path,
        grid_v2_enabled=parse_bool("GRINDER_GRID_V2_ENABLED", default=False, strict=False),
        account_sync_enabled=parse_bool(
            "GRINDER_ACCOUNT_SYNC_ENABLED", default=False, strict=False
        ),
    )
    if result.status in {"skipped", "passed"}:
        return
    print(
        "ERROR: GRINDER_GRID_V2_ENABLED=true with --exchange-port futures requires "
        "GRINDER_ACCOUNT_SYNC_ENABLED=1. Without account-sync, grid_v2 may miss "
        "fill/cancel reconciliation and stop updating order placement."
    )
    sys.exit(1)


def _validate_pippin_order_size_lock_or_exit(
    exchange_port: str,
    fixture_path: str | None,
) -> None:
    """Optional fail-closed launcher lock for PIPPINUSDT profile."""
    result = evaluate_pippin_order_size_lock_preflight(
        exchange_port=exchange_port,
        fixture_path=fixture_path,
        lock_enabled=parse_bool(
            "GRINDER_PIPPIN_ORDER_SIZE_LOCK_ENABLED", default=False, strict=False
        ),
        grid_v2_enabled=parse_bool("GRINDER_GRID_V2_ENABLED", default=False, strict=False),
        grid_v2_symbol=os.environ.get("GRINDER_GRID_V2_SYMBOL", ""),
        order_size_raw=os.environ.get("GRINDER_GRID_V2_ORDER_SIZE", ""),
    )
    if result.status in {"skipped", "passed"}:
        return
    print(
        "ERROR: PIPPIN profile lock FAILED: GRINDER_GRID_V2_ORDER_SIZE must be 80 "
        "when GRINDER_GRID_V2_SYMBOL=PIPPINUSDT and lock is enabled "
        f"(actual={result.actual!r})."
    )
    sys.exit(1)


def _build_grid_planners(
    symbols: list[str],
    symbol_constraints: dict[str, SymbolConstraints] | None,
    paper_kwargs: dict[str, Any],
) -> dict[str, LiveGridPlannerV1] | None:
    """Build per-symbol LiveGridPlannerV1 instances (PR-L2).

    Returns None if GRINDER_LIVE_PLANNER_ENABLED is not set.
    tick_size fail-safe: if unavailable for a symbol, planner gets tick_size=None
    and will produce 0 actions + WARN log (doc-25 invariant).
    """
    if not parse_bool("GRINDER_LIVE_PLANNER_ENABLED", default=False) or not symbols:
        return None

    from grinder.live.grid_planner import LiveGridConfig, LiveGridPlannerV1  # noqa: PLC0415

    # PR-VERIF-KNOBS-1: verification knobs for live planner
    adaptive_enabled = parse_bool("GRINDER_LIVE_ADAPTIVE_SPACING_ENABLED", default=True)
    max_level_dist_raw = int(os.environ.get("GRINDER_LIVE_MAX_LEVEL_DISTANCE_BPS", "0"))
    max_level_distance_bps: int | None = max_level_dist_raw if max_level_dist_raw > 0 else None

    planners: dict[str, LiveGridPlannerV1] = {}
    for sym in symbols:
        tick = None
        if symbol_constraints and sym in symbol_constraints:
            tick = symbol_constraints[sym].tick_size
        cfg = LiveGridConfig(
            base_spacing_bps=paper_kwargs.get("spacing_bps", 10.0),
            levels=paper_kwargs.get("levels", 5),
            size_per_level=paper_kwargs.get("size_per_level", Decimal("0.01")),
            tick_size=tick,
            adaptive_enabled=adaptive_enabled,
            max_level_distance_bps=max_level_distance_bps,
        )
        planners[sym] = LiveGridPlannerV1(cfg)
    syms_str = ", ".join(f"{s}(tick={planners[s]._config.tick_size})" for s in symbols)
    print(
        f"  LiveGridPlanner enabled: {syms_str}"
        f" adaptive={adaptive_enabled} max_level_dist_bps={max_level_distance_bps}"
    )
    return planners


def _build_cycle_layer(  # noqa: PLR0912, PLR0915
    symbols: list[str],
    symbol_constraints: dict[str, SymbolConstraints] | None,
    paper_kwargs: dict[str, Any],
) -> LiveCycleLayerV1 | None:
    """Build LiveCycleLayerV1 if enabled (PR-INV-3).

    Returns None if GRINDER_LIVE_CYCLE_ENABLED is not set.
    V1: single tick_size for all symbols. Fail-closed on tick_size mismatch.
    """
    if not parse_bool("GRINDER_LIVE_CYCLE_ENABLED", default=False):
        return None

    first_tick = None
    tick_mismatch = False
    for sym in symbols:
        tick = None
        if symbol_constraints and sym in symbol_constraints:
            tick = symbol_constraints[sym].tick_size
        if first_tick is None:
            first_tick = tick
        elif tick != first_tick:
            tick_mismatch = True

    if tick_mismatch:
        print(
            "  WARNING: LiveCycleLayer disabled -- tick_size differs across symbols (V1 limitation)"
        )
        return None

    # PR-INV-3b: TP TTL from env var (None/0 = disabled, default 300000 = 5min)
    tp_ttl_ms: int | None = 300_000
    raw_ttl = os.environ.get("GRINDER_TP_TTL_MS", "").strip()
    if raw_ttl:
        try:
            parsed_ttl = int(raw_ttl)
            tp_ttl_ms = parsed_ttl if parsed_ttl > 0 else None
        except ValueError:
            print(f"  WARNING: invalid GRINDER_TP_TTL_MS={raw_ttl!r}, using default 300000ms")

    # PR-INV-4: Replenish after fill (safe-by-default)
    replenish_enabled = parse_bool("GRINDER_LIVE_REPLENISH_ENABLED", default=False)
    replenish_max_levels = 0
    raw_max = os.environ.get("GRINDER_REPLENISH_MAX_LEVELS", "").strip()
    if raw_max:
        try:
            replenish_max_levels = int(raw_max)
        except ValueError:
            print(f"  WARNING: invalid GRINDER_REPLENISH_MAX_LEVELS={raw_max!r}, using 0")

    # PR-TP-RENEW: Auto-renew TP on expiry when position open (safe-by-default)
    tp_renew_enabled = parse_bool("GRINDER_TP_RENEW_ENABLED", default=False)
    tp_renew_cooldown_ms = 60_000
    raw_cooldown = os.environ.get("GRINDER_TP_RENEW_COOLDOWN_MS", "").strip()
    if raw_cooldown:
        try:
            tp_renew_cooldown_ms = int(raw_cooldown)
        except ValueError:
            print(f"  WARNING: invalid GRINDER_TP_RENEW_COOLDOWN_MS={raw_cooldown!r}, using 60000")
    tp_renew_max_attempts = 3
    raw_attempts = os.environ.get("GRINDER_TP_RENEW_MAX_ATTEMPTS", "").strip()
    if raw_attempts:
        try:
            tp_renew_max_attempts = max(1, int(raw_attempts))
        except ValueError:
            print(f"  WARNING: invalid GRINDER_TP_RENEW_MAX_ATTEMPTS={raw_attempts!r}, using 3")

    # PR-TP-PARTIAL: TP qty mode
    tp_qty_mode = os.environ.get("GRINDER_TP_QTY_MODE", "full").strip().lower()
    if tp_qty_mode not in ("full", "one_level", "pct"):
        print(f"  WARNING: invalid GRINDER_TP_QTY_MODE={tp_qty_mode!r}, using 'full'")
        tp_qty_mode = "full"
    tp_qty_pct = 100
    raw_pct = os.environ.get("GRINDER_TP_QTY_PCT", "").strip()
    if raw_pct:
        try:
            tp_qty_pct = max(1, min(100, int(raw_pct)))
        except ValueError:
            print(f"  WARNING: invalid GRINDER_TP_QTY_PCT={raw_pct!r}, using 100")

    # Extract per_level_qty and step_size from paper_kwargs and symbol_constraints
    raw_plq = paper_kwargs.get("size_per_level")
    per_level_qty: Decimal | None = Decimal(str(raw_plq)) if raw_plq is not None else None
    first_step: Decimal | None = None
    for sym in symbols:
        if symbol_constraints and sym in symbol_constraints:
            first_step = symbol_constraints[sym].step_size
            break

    cfg = LiveCycleConfig(
        spacing_bps=paper_kwargs.get("spacing_bps", 10.0),
        tick_size=first_tick,
        tp_ttl_ms=tp_ttl_ms,
        replenish_enabled=replenish_enabled,
        replenish_max_levels=replenish_max_levels,
        tp_renew_enabled=tp_renew_enabled,
        tp_renew_cooldown_ms=tp_renew_cooldown_ms,
        tp_renew_max_attempts=tp_renew_max_attempts,
        tp_qty_mode=tp_qty_mode,
        tp_qty_pct=tp_qty_pct,
        per_level_qty=per_level_qty,
        step_size=first_step,
    )
    layer = LiveCycleLayerV1(cfg)
    ttl_str = f"{cfg.tp_ttl_ms}ms" if cfg.tp_ttl_ms else "disabled"
    replenish_str = f"max_levels={replenish_max_levels}" if replenish_enabled else "disabled"
    renew_str = (
        f"cooldown={tp_renew_cooldown_ms}ms max_attempts={tp_renew_max_attempts}"
        if tp_renew_enabled
        else "disabled"
    )
    qty_str = tp_qty_mode
    if tp_qty_mode == "pct":
        qty_str = f"pct={tp_qty_pct}%"
    print(
        f"  LiveCycleLayer enabled: spacing={cfg.spacing_bps}bps tick={first_tick}"
        f" tp_ttl={ttl_str} replenish={replenish_str} tp_renew={renew_str}"
        f" tp_qty={qty_str}"
    )
    return layer


def _parse_max_position_usd() -> float | None:
    """Parse GRINDER_MAX_POSITION_USD env var (PR-INV-1)."""
    raw = os.environ.get("GRINDER_MAX_POSITION_USD", "").strip()
    if not raw:
        return None
    try:
        cap = float(raw)
        print(f"  Max position cap: ${cap:.2f}")
        return cap
    except ValueError:
        print(f"  WARNING: invalid GRINDER_MAX_POSITION_USD={raw!r}, cap disabled")
        return None


def build_engine(  # noqa: PLR0912, PLR0915
    mode: SafeMode,
    *,
    armed: bool = False,
    paper_size_per_level: Decimal | None = None,
    paper_spacing_bps: float | None = None,
    paper_levels: int | None = None,
    paper_cooldown_ms: int | None = None,
    exchange_port: ExchangePort | None = None,
    symbols: list[str] | None = None,
) -> LiveEngineV0:
    """Build LiveEngineV0 with configurable ExchangePort.

    If exchange_port is None, defaults to NoOpExchangePort (no real orders).

    If GRINDER_FILL_MODEL_DIR is set, loads FillModelV0 for fill probability
    gating (fail-open: load error -> None -> gate skipped).

    Loads symbol constraints (tick_size, step_size) from exchange info
    for price/qty rounding (fail-open: if unavailable, uses price_precision only).

    Args:
        mode: SafeMode for engine config.
        armed: Arm engine gate chain (lets actions flow to fill-prob gate).
            Safe with NoOpExchangePort — zero real-world effect.
        paper_size_per_level: Override PaperEngine size_per_level.
            Default PaperEngine uses 100 (base asset units), which exceeds
            notional gating limits at current BTC prices. Use e.g. 0.001
            for rehearsal to get actions through gating.
        paper_spacing_bps: Override PaperEngine grid spacing (default 10.0 bps).
        paper_levels: Override PaperEngine grid levels per side (default 5).
        paper_cooldown_ms: Override PaperEngine per-symbol cooldown (default 100ms).
        exchange_port: ExchangePort to use. Defaults to NoOpExchangePort.
        symbols: Trading symbols for per-symbol planner creation (PR-L2).

    Returns:
        Configured LiveEngineV0 instance (gauge set to 1 after init).
    """
    # Load symbol constraints for tick_size rounding (fail-open)
    symbol_constraints = _load_symbol_constraints()
    constraints_enabled = symbol_constraints is not None
    if constraints_enabled and symbol_constraints is not None:
        print(f"  Symbol constraints loaded: {len(symbol_constraints)} symbols")
    else:
        print("  Symbol constraints not available (fail-open, using price_precision only)")

    paper_kwargs: dict[str, object] = {
        "constraints_enabled": constraints_enabled,
        "symbol_constraints": symbol_constraints,
    }
    if paper_size_per_level is not None:
        paper_kwargs["size_per_level"] = paper_size_per_level
    if paper_spacing_bps is not None:
        paper_kwargs["spacing_bps"] = paper_spacing_bps
    if paper_levels is not None:
        paper_kwargs["levels"] = paper_levels
    if paper_cooldown_ms is not None:
        paper_kwargs["cooldown_ms"] = paper_cooldown_ms
    paper_engine = PaperEngine(**paper_kwargs)  # type: ignore[arg-type]
    port = exchange_port if exchange_port is not None else NoOpExchangePort()
    config = LiveEngineConfig(armed=armed, mode=mode, max_position_usd=_parse_max_position_usd())

    fill_model = None
    model_dir = os.environ.get("GRINDER_FILL_MODEL_DIR", "").strip()
    if model_dir:
        fill_model = load_fill_model_v0(model_dir)
        if fill_model is not None:
            print(
                f"  Fill model loaded: {len(fill_model.bins)} bins, prior={fill_model.global_prior_bps} bps"
            )
        else:
            print("  Fill model load FAILED (fail-open, gate skipped)")

    # FSM + guards (opt-in via GRINDER_FSM_ENABLED, default=false → zero behavior change, PR-A1)
    fsm_driver = None
    drawdown_guard = None
    toxicity_gate = None
    fsm_enabled = os.environ.get("GRINDER_FSM_ENABLED", "").lower() in ("true", "1", "yes")
    if fsm_enabled:
        from grinder.gating.toxicity_gate import ToxicityGate  # noqa: PLC0415
        from grinder.live.fsm_driver import FsmDriver  # noqa: PLC0415
        from grinder.live.fsm_orchestrator import FsmConfig, OrchestratorFSM  # noqa: PLC0415
        from grinder.risk.drawdown_guard_v1 import (  # noqa: PLC0415
            DrawdownGuardV1,
            DrawdownGuardV1Config,
        )

        fsm = OrchestratorFSM(config=FsmConfig())
        fsm_driver = FsmDriver(fsm)
        dd_config = DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.20"))
        drawdown_guard = DrawdownGuardV1(dd_config)
        toxicity_gate = ToxicityGate()
        print("  FSM enabled (feed_stale + toxicity wired)")

    # Account syncer (opt-in via GRINDER_ACCOUNT_SYNC_ENABLED, Launch-15)
    account_syncer = None
    if parse_bool("GRINDER_ACCOUNT_SYNC_ENABLED", default=False):
        from grinder.account.syncer import AccountSyncer  # noqa: PLC0415

        account_syncer = AccountSyncer(port)
        print("  AccountSyncer enabled")

    # FeatureEngine for NATR/volatility (PR-L0, feeds future LiveGridPlanner)
    feature_engine = FeatureEngine(
        FeatureEngineConfig(bar_interval_ms=60_000, atr_period=14, max_bars=1000)
    )
    print("  FeatureEngine enabled (bar_interval=60s, atr_period=14)")

    # PR-ROLL-1b: reduce-only enforcement toggle
    ro_enforcement = parse_bool("GRINDER_LIVE_REDUCE_ONLY_ENFORCEMENT", default=True)
    print(f"  Reduce-only enforcement: {'enabled' if ro_enforcement else 'disabled'}")

    # Live grid planner (opt-in via GRINDER_LIVE_PLANNER_ENABLED, PR-L2)
    grid_planners = _build_grid_planners(symbols or [], symbol_constraints, paper_kwargs)

    # Live cycle layer (opt-in via GRINDER_LIVE_CYCLE_ENABLED, PR-INV-3)
    cycle_layer = _build_cycle_layer(symbols or [], symbol_constraints, paper_kwargs)

    # Doc-36: shared weight parser for Phase 1 (shadow) and Phase 2 (active) selectors
    def _parse_weight(env_var: str, default: float) -> float:
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            print(f"  WARNING: invalid {env_var}={raw!r}, using default {default}")
            return default

    # Doc-36: load ONNX model for ML-assisted selector scoring (shared by shadow + active)
    _ml_onnx_model = None
    _ml_selector_enabled = parse_bool(
        "GRINDER_SYMBOL_SELECTOR_ML_ENABLED", default=False, strict=False
    )
    _ml_model_dir = os.environ.get("GRINDER_ML_REGIME_MODEL_DIR", "").strip()
    if _ml_selector_enabled and _ml_model_dir:
        try:
            from grinder.ml.onnx.model import OnnxMlModel  # noqa: PLC0415

            _ml_onnx_model = OnnxMlModel.load_from_dir(_ml_model_dir)
            print(f"  Selector ONNX model loaded: {_ml_model_dir}")
        except Exception as exc:
            print(f"  Selector ONNX model load FAILED (fail-open): {exc}")
    elif _ml_selector_enabled:
        print("  Selector ML enabled but GRINDER_ML_REGIME_MODEL_DIR not set — baseline fallback")

    # Doc-36 Phase 1: shadow selector (observability only, no dispatch mutation)
    shadow_selector = None
    if parse_bool("GRINDER_SYMBOL_SELECTOR_SHADOW", default=False, strict=False):
        from grinder.selection.shadow_selector import (  # noqa: PLC0415
            ShadowSelector,
            ShadowSelectorConfig,
        )

        selector_config = ShadowSelectorConfig(
            enabled=True,
            k=parse_int("GRINDER_SYMBOL_SELECTOR_K", default=3, strict=False) or 3,
            cycle_s=parse_int("GRINDER_SYMBOL_SELECTOR_CYCLE_S", default=60, strict=False) or 60,
            min_natr_bps=parse_int(
                "GRINDER_SYMBOL_SELECTOR_MIN_NATR_BPS", default=100, strict=False
            )
            or 100,
            trend_hard_gate_bps=parse_int(
                "GRINDER_SYMBOL_SELECTOR_TREND_HARD_GATE_BPS", default=0, strict=False
            )
            or 0,
            range_weight_w=_parse_weight("GRINDER_SYMBOL_SELECTOR_RANGE_WEIGHT_W", 1.0),
            liquidity_weight_w=_parse_weight("GRINDER_SYMBOL_SELECTOR_LIQUIDITY_WEIGHT_W", 1.0),
            toxicity_penalty_w=_parse_weight("GRINDER_SYMBOL_SELECTOR_TOXICITY_PENALTY_W", 1.0),
            trend_penalty_w=_parse_weight("GRINDER_SYMBOL_SELECTOR_TREND_PENALTY_W", 1.0),
            ml_enabled=parse_bool(
                "GRINDER_SYMBOL_SELECTOR_ML_ENABLED", default=False, strict=False
            ),
            ml_adjust_max_bps=parse_int(
                "GRINDER_SYMBOL_SELECTOR_ML_ADJUST_MAX_BPS", default=0, strict=False
            )
            or 0,
        )
        _shadow_provider = None
        if selector_config.ml_enabled and _ml_onnx_model is not None:
            # Deferred: provider wired after init to capture feature_cache ref
            pass
        shadow_selector = ShadowSelector(selector_config)
        if selector_config.ml_enabled and _ml_onnx_model is not None:
            from grinder.selection.ml_provider import (  # noqa: PLC0415
                build_ml_adjust_provider,
            )

            _shadow_provider = build_ml_adjust_provider(
                _ml_onnx_model,
                shadow_selector._feature_cache,
                adjust_base_bps=max(1, selector_config.ml_adjust_max_bps // 2),
            )
            shadow_selector._ml_adjust_provider = _shadow_provider
            print(
                f"  Shadow selector ML provider WIRED: max_adjust={selector_config.ml_adjust_max_bps}bps"
            )
        elif selector_config.ml_enabled:
            print("  Shadow selector ML enabled but model unavailable — baseline fallback")
        print(
            f"  Shadow selector enabled: k={selector_config.k} "
            f"cycle_s={selector_config.cycle_s} min_natr={selector_config.min_natr_bps}bps"
        )

    # Doc-36 Phase 2: active selector (controlled activation, operator universe only)
    active_selector = None
    if parse_bool("GRINDER_SYMBOL_SELECTOR_ENABLED", default=False, strict=False):
        from grinder.selection.active_selector import (  # noqa: PLC0415
            ActiveSelector,
            ActiveSelectorConfig,
        )

        active_config = ActiveSelectorConfig(
            enabled=True,
            k=parse_int("GRINDER_SYMBOL_SELECTOR_K", default=3, strict=False) or 3,
            cycle_s=parse_int("GRINDER_SYMBOL_SELECTOR_CYCLE_S", default=60, strict=False) or 60,
            min_natr_bps=parse_int(
                "GRINDER_SYMBOL_SELECTOR_MIN_NATR_BPS", default=100, strict=False
            )
            or 100,
            trend_hard_gate_bps=parse_int(
                "GRINDER_SYMBOL_SELECTOR_TREND_HARD_GATE_BPS", default=0, strict=False
            )
            or 0,
            range_weight_w=_parse_weight("GRINDER_SYMBOL_SELECTOR_RANGE_WEIGHT_W", 1.0),
            liquidity_weight_w=_parse_weight("GRINDER_SYMBOL_SELECTOR_LIQUIDITY_WEIGHT_W", 1.0),
            toxicity_penalty_w=_parse_weight("GRINDER_SYMBOL_SELECTOR_TOXICITY_PENALTY_W", 1.0),
            trend_penalty_w=_parse_weight("GRINDER_SYMBOL_SELECTOR_TREND_PENALTY_W", 1.0),
            min_hold_cycles=parse_int(
                "GRINDER_SYMBOL_SELECTOR_MIN_HOLD_CYCLES", default=5, strict=False
            )
            or 5,
            max_changes_per_cycle=parse_int(
                "GRINDER_SYMBOL_SELECTOR_MAX_CHANGES_PER_CYCLE", default=1, strict=False
            )
            or 1,
            enter_threshold_bps=parse_int(
                "GRINDER_SYMBOL_SELECTOR_ENTER_THRESHOLD_BPS", default=0, strict=False
            )
            or 0,
            exit_threshold_bps=parse_int(
                "GRINDER_SYMBOL_SELECTOR_EXIT_THRESHOLD_BPS", default=0, strict=False
            )
            or 0,
            ml_enabled=parse_bool(
                "GRINDER_SYMBOL_SELECTOR_ML_ENABLED", default=False, strict=False
            ),
            ml_adjust_max_bps=parse_int(
                "GRINDER_SYMBOL_SELECTOR_ML_ADJUST_MAX_BPS", default=0, strict=False
            )
            or 0,
        )
        _active_provider = None
        if active_config.ml_enabled and _ml_onnx_model is not None:
            # Deferred: provider wired after init
            pass
        active_selector = ActiveSelector(active_config, initial_active=set(symbols or []))
        if active_config.ml_enabled and _ml_onnx_model is not None:
            from grinder.selection.ml_provider import (  # noqa: PLC0415
                build_ml_adjust_provider,
            )

            _active_provider = build_ml_adjust_provider(
                _ml_onnx_model,
                active_selector._shadow._feature_cache,
                adjust_base_bps=max(1, active_config.ml_adjust_max_bps // 2),
            )
            active_selector._shadow._ml_adjust_provider = _active_provider
            print(
                f"  Active selector ML provider WIRED: max_adjust={active_config.ml_adjust_max_bps}bps"
            )
        elif active_config.ml_enabled:
            print("  Active selector ML enabled but model unavailable — baseline fallback")
            if active_config.ml_adjust_max_bps <= 0:
                print("  WARNING: ML_ADJUST_MAX_BPS=0, ML adjust effectively disabled")
        print(
            f"  Active selector enabled: k={active_config.k} "
            f"hold={active_config.min_hold_cycles} max_chg={active_config.max_changes_per_cycle}"
        )

    return LiveEngineV0(
        paper_engine=paper_engine,
        exchange_port=port,
        config=config,
        fill_model=fill_model,
        fsm_driver=fsm_driver,
        drawdown_guard=drawdown_guard,
        toxicity_gate=toxicity_gate,
        account_syncer=account_syncer,
        feature_engine=feature_engine,
        grid_planners=grid_planners,
        cycle_layer=cycle_layer,
        shadow_selector=shadow_selector,
        active_selector=active_selector,
        operator_symbols=symbols,
    )


async def run_user_data_loop(
    make_conn: Callable[[], Any],
    on_event: Callable[[Any], None],
    shutdown: asyncio.Event,
    *,
    max_retries: int = 5,
) -> None:
    """Retry loop for user-data WS: fresh connector per attempt, shutdown-aware.

    Args:
        make_conn: Factory that creates a fresh connector (with connect/iter_events/close).
        on_event: Callback for each received event.
        shutdown: Event to signal graceful stop. Wakes up backoff sleeps immediately.
        max_retries: Maximum outer restart attempts.
    """
    for attempt in range(max_retries):
        if shutdown.is_set():
            break
        conn = make_conn()
        try:
            await conn.connect()
            async for event in conn.iter_events():
                if shutdown.is_set():
                    return
                on_event(event)
        except Exception as e:
            if shutdown.is_set():
                break
            delay = min(2**attempt * 3, 30)
            print(
                f"  User-data loop attempt {attempt + 1}/{max_retries} "
                f"ended: {e}. Restart in {delay}s"
            )
            if attempt < max_retries - 1:
                # Shutdown-aware sleep: wake immediately on shutdown signal
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=delay)
                    return  # shutdown fired during backoff
                except TimeoutError:
                    pass  # backoff expired, continue retry
        else:
            break  # clean exit
        finally:
            with contextlib.suppress(Exception):
                await conn.close()


async def trading_loop(
    connector: LiveConnectorV0,
    engine: LiveEngineV0,
    shutdown: asyncio.Event,
    duration_s: int,
    user_data_connector_factory: Callable[[], FuturesUserDataWsConnector] | None = None,
) -> str:
    """Run the trading loop: connector -> engine.process_snapshot().

    Sets module-level _loop_ready flag after connector.connect() succeeds.
    Resets _loop_ready in finally block.

    When HA is enabled, skips snapshot processing if role != ACTIVE (fail-closed).

    Args:
        connector: Connected LiveConnectorV0.
        engine: Initialized LiveEngineV0.
        shutdown: Event to signal graceful stop.
        duration_s: Max duration (0 = infinite).
    """
    global _loop_ready  # noqa: PLW0603

    user_data_task: asyncio.Task[None] | None = None
    await connector.connect()
    if user_data_connector_factory is not None:
        user_data_task = asyncio.create_task(
            run_user_data_loop(
                make_conn=user_data_connector_factory,
                on_event=engine.process_user_data_event,
                shutdown=shutdown,
            )
        )
    _loop_ready = True
    print("  /readyz now returning 200 (if HA permits)")
    start = time.time()
    tick_count = 0
    ha_skip_count = 0
    stop_reason = "stream_ended"
    try:
        async for snapshot in connector.iter_snapshots():
            if shutdown.is_set():
                stop_reason = "shutdown_requested"
                break
            if duration_s > 0 and (time.time() - start) >= duration_s:
                print(f"\nDuration ({duration_s}s) reached after {tick_count} ticks.")
                stop_reason = "duration_reached"
                break
            # HA gating: skip processing when not ACTIVE
            if _ha_enabled and get_ha_state().role != HARole.ACTIVE:
                ha_skip_count += 1
                if ha_skip_count % 100 == 1:
                    print(f"  HA: not ACTIVE, skipping snapshot (total skipped: {ha_skip_count})")
                continue
            engine.process_snapshot(snapshot)
            tick_count += 1
            if tick_count % 100 == 0:
                print(f"  Processed {tick_count} ticks ({snapshot.symbol})")
    finally:
        _loop_ready = False
        if user_data_task is not None and not user_data_task.done():
            user_data_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await user_data_task
        await connector.close()
        print(f"  Trading loop stopped. Total ticks: {tick_count}, HA skips: {ha_skip_count}")
    return stop_reason


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(description="Run GRINDER trading loop")
    parser.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT")
    parser.add_argument("--duration-s", type=int, default=0)
    parser.add_argument("--metrics-port", type=int, default=9090)
    parser.add_argument(
        "--fixture",
        type=str,
        default=None,
        help="Path to JSONL fixture (one bookTicker JSON per line)",
    )
    parser.add_argument(
        "--mainnet",
        action="store_true",
        default=False,
        help="Use mainnet WS endpoint instead of testnet (safe for read_only)",
    )
    parser.add_argument(
        "--armed",
        action="store_true",
        default=False,
        help="Arm engine gate chain (lets actions reach fill-prob gate). Safe with NoOpExchangePort.",
    )
    parser.add_argument(
        "--paper-size-per-level",
        type=str,
        default=None,
        help="Override PaperEngine size_per_level (Decimal, e.g. 0.001). "
        "Default 100 exceeds notional limits at current BTC prices.",
    )
    parser.add_argument(
        "--exchange-port",
        type=str,
        default="noop",
        choices=["noop", "futures"],
        help="Exchange port: noop (default, no orders) or futures (BinanceFuturesPort, 5 gates).",
    )
    parser.add_argument(
        "--max-notional-per-order",
        type=str,
        default="100",
        help="Max notional per order in USD (default 100, rehearsal cap). Used with --exchange-port futures.",
    )
    parser.add_argument(
        "--max-orders-per-run",
        type=int,
        default=500,
        help="Max orders per run (default 500). "
        "Values >1 require GRINDER_MAX_ORDERS_ACK=YES_I_ACCEPT_MULTI_ORDER.",
    )
    parser.add_argument(
        "--paper-spacing-bps",
        type=float,
        default=None,
        help="Override PaperEngine grid spacing in basis points (default 10.0). "
        "Lower values = tighter grid = more frequent cancel/replace.",
    )
    parser.add_argument(
        "--paper-levels",
        type=int,
        default=None,
        help="Override PaperEngine grid levels per side (default 5). Total orders = 2 * levels.",
    )
    parser.add_argument(
        "--paper-cooldown-ms",
        type=int,
        default=None,
        help="Override PaperEngine per-symbol cooldown in milliseconds (default 100).",
    )
    parser.add_argument(
        "--cleanup-on-exit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "After trading loop stops, auto-run exchange cleanup for each symbol "
            "(futures + live_trade + --armed + --mainnet only). "
            "Default: enabled. Use --no-cleanup-on-exit to disable."
        ),
    )
    parser.add_argument(
        "--pre-cleanup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Before start, if exchange state is DIRTY, run cleanup first. "
            "Only applies to futures + mainnet + armed. "
            "Default: enabled. Use --no-pre-cleanup to disable."
        ),
    )
    parser.add_argument(
        "--skip-launch-guard",
        action="store_true",
        default=False,
        help=(
            "Skip the launch guard verify step. NOT recommended for mainnet. "
            "Use only for testnet/paper/debugging."
        ),
    )
    return parser


async def _drain_pending_tasks() -> None:
    """Cancel and await all pending tasks (safety net for clean shutdown)."""
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _configure_logging() -> None:
    """Configure root logger from GRINDER_LOG_LEVEL env var.

    Ownership: run_trading.py is the canonical entrypoint for the trading loop.
    It owns logging setup because no other code path should configure the root
    logger — library modules use ``logging.getLogger(__name__)`` and inherit.
    ``force=True`` ensures this config wins even if a library import triggered
    a default ``lastResort`` handler, which is acceptable because run_trading.py
    is always the top-level process owner.
    """
    raw = os.environ.get("GRINDER_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, raw, None)
    if not isinstance(level, int):
        print(f"  WARNING: invalid GRINDER_LOG_LEVEL={raw!r}, falling back to INFO")
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
        force=True,
    )


def evaluate_cleanup_on_exit_policy(
    *,
    cleanup_on_exit: bool,
    mode: SafeMode,
    exchange_port: str,
    armed: bool,
    mainnet: bool,
    fixture_path: str | None,
    stop_reason: str,
) -> tuple[bool, str]:
    """Decide whether post-run cleanup should execute.

    Cleanup runs on:
    - normal duration_reached (planned stop)
    - any fatal abort that happened after live trading started
      (stream_ended, shutdown_requested, or any non-"not_started" reason)

    Cleanup is skipped only when:
    - cleanup_on_exit is False
    - mode/port/armed/mainnet/fixture preconditions fail
    - loop never started (stop_reason == "not_started")
    """
    enabled = False
    reason = "disabled"
    if cleanup_on_exit:
        enabled = True
        reason = "enabled"
        if mode != SafeMode.LIVE_TRADE:
            enabled = False
            reason = "not_live_trade"
        elif exchange_port != "futures":
            enabled = False
            reason = "not_futures_port"
        elif not armed:
            enabled = False
            reason = "not_armed"
        elif not mainnet:
            enabled = False
            reason = "not_mainnet"
        elif fixture_path:
            enabled = False
            reason = "fixture_mode"
        elif stop_reason == "not_started":
            enabled = False
            reason = "loop_never_started"
    return (enabled, reason)


def finalize_and_cleanup(
    *,
    cleanup_on_exit: bool,
    mode: SafeMode,
    exchange_port: str,
    armed: bool,
    mainnet: bool,
    fixture_path: str | None,
    stop_reason: str,
    symbols: list[str],
    cleanup_fn: Callable[[list[str]], int] | None = None,
) -> int:
    """Evaluate cleanup policy and run cleanup if needed.

    Returns exit code contribution (0 = ok, 3 = cleanup had failures).
    """
    should_cleanup, cleanup_reason = evaluate_cleanup_on_exit_policy(
        cleanup_on_exit=cleanup_on_exit,
        mode=mode,
        exchange_port=exchange_port,
        armed=armed,
        mainnet=mainnet,
        fixture_path=fixture_path,
        stop_reason=stop_reason,
    )
    if should_cleanup:
        cleanup_type = "PLANNED" if stop_reason == "duration_reached" else "ABORT"
        print(f"TRADING_{cleanup_type}_CLEANUP_STARTED stop_reason={stop_reason}")
        run_cleanup = cleanup_fn or _run_cleanup_on_exit
        failures = run_cleanup(symbols)
        print(
            f"TRADING_{cleanup_type}_CLEANUP_COMPLETED "
            f"failures={failures} stop_reason={stop_reason}"
        )
        return 3 if failures > 0 else 0
    if cleanup_on_exit:
        print(f"  Cleanup-on-exit skipped: {cleanup_reason}")
    return 0


def _run_cleanup_on_exit(
    symbols: list[str],
    *,
    run_cmd: Any = subprocess.run,
    executable: str = sys.executable,
) -> int:
    """Run exchange_state cleanup for each symbol with post-verify. Returns failed count."""
    repo_root = Path(__file__).resolve().parents[1]
    failures = 0
    for symbol in symbols:
        print(f"  Cleanup-on-exit: cleaning {symbol} ...")
        env = os.environ.copy()
        env["ALLOW_MAINNET_TRADE"] = "1"
        env.setdefault("PYTHONPATH", ".")
        # Cleanup subprocess with timeout (prevent indefinite hang)
        try:
            result = run_cmd(
                [executable, "-m", "scripts.exchange_state", "cleanup", symbol],
                cwd=repo_root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            failures += 1
            print(f"  Cleanup-on-exit TIMEOUT symbol={symbol}")
            continue
        if result.stdout:
            print(result.stdout.strip())
        if result.returncode != 0:
            failures += 1
            print(f"  Cleanup-on-exit FAILED symbol={symbol} rc={result.returncode}")
            if result.stderr:
                print(result.stderr.strip())
            continue
        # Post-cleanup re-verify from parent process (Gap 1/9 fix)
        try:
            verify_result = run_cmd(
                [executable, "-m", "scripts.exchange_state", "verify", symbol],
                cwd=repo_root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if verify_result.returncode != 0:
                failures += 1
                print(f"  Cleanup-on-exit POST_VERIFY_DIRTY symbol={symbol}")
                if verify_result.stdout:
                    print(verify_result.stdout.strip())
            else:
                print(f"  Cleanup-on-exit VERIFIED_CLEAN symbol={symbol}")
        except subprocess.TimeoutExpired:
            failures += 1
            print(f"  Cleanup-on-exit POST_VERIFY_TIMEOUT symbol={symbol}")
    return failures


def main() -> None:  # noqa: PLR0912, PLR0915
    global _ha_enabled  # noqa: PLW0603

    args = build_parser().parse_args()

    # ADR-089: native logging config — must be before any engine/connector construction.
    _configure_logging()

    # Fixture network airgap (PR-NETLOCK-1) — must be before ANY network-touching code
    if args.fixture:
        install_fixture_network_guard()

    mode = validate_env()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    use_testnet = not args.mainnet
    max_notional = Decimal(args.max_notional_per_order)

    paper_size: Decimal | None = None
    if args.paper_size_per_level is not None:
        paper_size = Decimal(args.paper_size_per_level)

    # HA lifecycle
    _ha_enabled = is_ha_enabled()
    elector = start_ha_elector()

    # Exchange port
    max_orders = args.max_orders_per_run
    if max_orders > 1:
        validate_max_orders_ack(max_orders)
    port = build_exchange_port(
        args.exchange_port, mode, args.armed, symbols, max_notional, max_orders
    )

    # Boot summary
    print(
        f"GRINDER TRADING LOOP | mode={mode.value} symbols={symbols} "
        f"port={args.exchange_port} armed={args.armed} "
        f"ha={_ha_enabled} net={'mainnet' if args.mainnet else 'testnet'} "
        f"max_notional={max_notional}"
    )
    if paper_size is not None:
        print(f"  Paper size_per_level: {paper_size}")
    if args.paper_spacing_bps is not None:
        print(f"  Paper spacing_bps: {args.paper_spacing_bps}")
    if args.paper_levels is not None:
        print(f"  Paper levels: {args.paper_levels}")
    if args.paper_cooldown_ms is not None:
        print(f"  Paper cooldown_ms: {args.paper_cooldown_ms}")
    if args.exchange_port == "futures":
        print(
            f"  FUTURES_PORT_CONFIG_OK max_orders_per_run={max_orders} "
            f"max_notional_per_order={max_notional}"
        )
    if args.fixture:
        print(f"  Fixture: {args.fixture}")
        print("  Network guard: ACTIVE (external connections blocked)")

    # Build read-only syncer for preflight (before engine build)
    preflight_syncer = None
    if args.armed and args.mainnet and parse_bool("GRINDER_ACCOUNT_SYNC_ENABLED", default=False):
        from grinder.account.syncer import AccountSyncer  # noqa: PLC0415

        preflight_syncer = AccountSyncer(port)

    # Load symbol constraints BEFORE preflight so check_symbol_metadata
    # can verify the target symbol exists on exchange.
    preflight_constraints = _load_symbol_constraints()
    if preflight_constraints:
        print(f"  Preflight constraints loaded: {len(preflight_constraints)} symbols")
    else:
        print("  WARNING: Preflight constraints unavailable (metadata check will fail-closed)")

    # Create a constraint holder for preflight's check_symbol_metadata
    class _PreflightConstraintHolder:
        def get_symbol_constraints(self) -> dict[str, SymbolConstraints] | None:
            return preflight_constraints

    preflight_port = _PreflightConstraintHolder() if preflight_constraints else port

    # Live Preflight Gate (PR-2): fail-closed before trading loop
    from grinder.live.preflight import run_preflight  # noqa: PLC0415

    preflight_report = run_preflight(
        armed=args.armed,
        mainnet=args.mainnet,
        mode_value=mode.value,
        syncer=preflight_syncer,
        port=preflight_port,
        symbols=symbols,
        env_acks={
            "ALLOW_MAINNET_TRADE": os.environ.get("ALLOW_MAINNET_TRADE") == "1",
            "GRINDER_REAL_PORT_ACK": os.environ.get("GRINDER_REAL_PORT_ACK")
            in (
                "YES_I_REALLY_WANT_MAINNET",
                "1",
                "true",
            ),
        },
    )
    preflight_report.print_report()
    if not preflight_report.passed:
        print("LIVE_PREFLIGHT BLOCKED: armed run cannot start. Fix blockers above.")
        sys.exit(2)

    # Shadow tuning: log SYMBOL_TUNED / SYMBOL_NO_GO per symbol (PR-B3a).
    # Pure startup visibility — no dispatch change, no selector change.
    _run_startup_tuning_shadow(symbols, preflight_constraints)

    server: HTTPServer | None = None
    try:
        server = run_server(args.metrics_port)
    except OSError as e:
        print(f"ERROR: Cannot bind metrics port {args.metrics_port}: {e}")
        sys.exit(1)
    print(f"  Health endpoint: http://localhost:{args.metrics_port}/healthz")

    # Register atexit handler to release server port on ANY exit path.
    # This covers sys.exit() calls in validators, launch guard, etc.
    import atexit  # noqa: PLC0415

    def _release_server_on_exit() -> None:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass

    atexit.register(_release_server_on_exit)

    engine = build_engine(
        mode,
        armed=args.armed,
        paper_size_per_level=paper_size,
        paper_spacing_bps=args.paper_spacing_bps,
        paper_levels=args.paper_levels,
        paper_cooldown_ms=args.paper_cooldown_ms,
        exchange_port=port,
        symbols=symbols,
    )
    print("  Engine initialized: grinder_live_engine_initialized=1")

    # Pre-populate zero-value gating metrics for Prometheus visibility
    get_gating_metrics().initialize_zero_series()

    # Pre-populate zero-value port order attempt metrics (PR-FUT-1)
    get_port_metrics().initialize_zero_series(args.exchange_port)

    # Pre-populate zero-value tuning metrics for Prometheus visibility (PR-B3b)
    from grinder.tuning.metrics import get_tuning_metrics  # noqa: PLC0415

    get_tuning_metrics().initialize_zero_series()

    # Register readyz callback so /metrics emits grinder_readyz_ready gauge (PR-ALERTS-0)
    set_ready_fn(is_trading_ready)

    # Futures preflight: validate symbols exist on futures venue (fail-closed).
    _validate_futures_preflight_or_exit(symbols, args.exchange_port, args.fixture)
    _validate_grid_v2_account_sync_or_exit(args.exchange_port, args.fixture)
    _validate_pippin_order_size_lock_or_exit(args.exchange_port, args.fixture)

    # Launch guard v2: verify exchange state clean before start (fail-closed).
    if not args.skip_launch_guard:
        guard_result = evaluate_launch_guard(
            exchange_port=args.exchange_port,
            mainnet=args.mainnet,
            armed=args.armed,
            fixture_path=args.fixture,
            pre_cleanup=args.pre_cleanup,
            symbols=symbols,
        )
        print(f"  LAUNCH_GUARD status={guard_result.status} reason={guard_result.reason}")
        if guard_result.status in ("verify_dirty_no_cleanup", "cleanup_then_still_dirty"):
            print(
                f"ERROR: Launch guard FAILED — exchange state not clean. "
                f"orders={guard_result.orders} position={guard_result.position}. "
                f"Run 'python3 -m scripts.exchange_state cleanup <SYMBOL>' or use --pre-cleanup."
            )
            sys.exit(1)
        if guard_result.status == "verify_error":
            print(f"ERROR: Launch guard verify error — {guard_result.reason}. Cannot start safely.")
            sys.exit(1)
        if guard_result.status == "recovery_snapshot_unstable":
            print(
                f"ERROR: Launch guard FAILED — snapshots unstable, cleanup unsafe. "
                f"orders={guard_result.orders} position={guard_result.position}. "
                f"Wait for pending fills to settle and retry."
            )
            sys.exit(1)
        if guard_result.status == "recovery_non_flat_skip_cleanup":
            print(
                "  RECOVERY_MODE_ACTIVE — non-flat position detected, cleanup skipped. "
                "Startup reconstruction will recover grid state."
            )
    else:
        print("  LAUNCH_GUARD status=skipped reason=--skip-launch-guard")

    connector = build_connector(
        symbols,
        mode,
        args.fixture,
        use_testnet=use_testnet,
        exchange_port=args.exchange_port,
    )

    # Factory for user-data connector: creates a fresh instance per retry attempt.
    # Closed connectors cannot reconnect, so each retry needs a new object.
    def _make_user_data_connector() -> FuturesUserDataWsConnector | None:
        return _build_user_data_connector_or_none(
            symbols=symbols,
            mode=mode,
            exchange_port=args.exchange_port,
            fixture_path=args.fixture,
            use_testnet=use_testnet,
        )

    # Probe once to check if user-data is enabled (prints status message)
    user_data_connector = _make_user_data_connector()

    # Async loop with signal handling
    loop = asyncio.new_event_loop()
    shutdown = asyncio.Event()

    def handle_signal(*_: object) -> None:
        loop.call_soon_threadsafe(shutdown.set)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # SIGUSR1: trigger graceful-exit-only for all symbols (E3 ceremony support)
    def handle_graceful_exit_signal(*_: object) -> None:
        for sym in symbols:
            if engine.force_graceful_exit(sym):
                print(f"  FORCE_GRACEFUL_EXIT symbol={sym}")

    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, handle_graceful_exit_signal)

    print(
        f"\nGRINDER TRADING LOOP running. pid={os.getpid()} "
        f"metrics_port={args.metrics_port} Press Ctrl+C to stop."
    )
    exit_code = 0
    loop_stop_reason = "not_started"
    try:
        loop_stop_reason = loop.run_until_complete(
            trading_loop(
                connector,
                engine,
                shutdown,
                args.duration_s,
                user_data_connector_factory=(
                    _make_user_data_connector  # type: ignore[arg-type]
                    if user_data_connector is not None
                    else None
                ),
            )
        )
    except Exception as exc:
        print(f"GRINDER TRADING LOOP FATAL: {exc}")
        exit_code = 2
    finally:
        # Shutdown async resources (loop always exists at this point)
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(_drain_pending_tasks())
            loop.close()
        except Exception as e:
            print(f"  WARNING: async shutdown error: {e}")

        # Cleanup exchange state (most critical — must always attempt)
        try:
            cleanup_exit = finalize_and_cleanup(
                cleanup_on_exit=args.cleanup_on_exit,
                mode=mode,
                exchange_port=args.exchange_port,
                armed=args.armed,
                mainnet=args.mainnet,
                fixture_path=args.fixture,
                stop_reason=loop_stop_reason,
                symbols=symbols,
            )
            if cleanup_exit > 0 and exit_code == 0:
                exit_code = cleanup_exit
        except Exception as e:
            print(f"  ERROR: cleanup failed with exception: {e}")
            if exit_code == 0:
                exit_code = 3

        # Stop LeaderElector (wrapped — must not block server shutdown)
        try:
            if elector is not None:
                print("  Stopping LeaderElector...")
                elector.stop()
        except Exception as e:
            print(f"  WARNING: LeaderElector stop error: {e}")

        # Shutdown HTTP server and release port.
        # atexit handler is also registered as safety net, but explicit
        # shutdown here is preferred for deterministic ordering.
        try:
            if server is not None:
                server.shutdown()
                server.server_close()
                server = None  # prevent atexit double-close
        except Exception as e:
            print(f"  WARNING: server shutdown error: {e}")

        print(f"GRINDER TRADING LOOP stopped. pid={os.getpid()} exit_code={exit_code}")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
