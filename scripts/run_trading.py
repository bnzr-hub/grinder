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
    --max-orders-per-run        Max orders per port instance (default 1, canary safety cap).
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
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from grinder.connectors.binance_ws import BINANCE_WS_MAINNET, FakeWsTransport
from grinder.connectors.live_connector import (
    LiveConnectorConfig,
    LiveConnectorV0,
    SafeMode,
)
from grinder.env_parse import parse_bool
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


def run_server(port: int) -> HTTPServer:
    """Start HTTP server in background thread."""
    set_start_time(time.time())
    server = HTTPServer(("0.0.0.0", port), TradingHealthHandler)
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
    max_orders_per_run: int = 1,
) -> ExchangePort:
    """Build exchange port by name.

    Args:
        port_name: "noop" or "futures".
        mode: SafeMode for config.
        armed: Whether engine is armed.
        symbols: Trading symbols (used as whitelist for futures).
        max_notional: Max notional per order (for futures config).
        max_orders_per_run: Max orders per port instance (default 1).

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

        # Compute clock offset vs Binance server (WSL2 clock drift workaround)
        ts_offset_ms = 0
        try:
            resp = urllib.request.urlopen(f"{BINANCE_FUTURES_MAINNET_URL}/fapi/v1/time", timeout=5)
            server_ts = json.loads(resp.read())["serverTime"]
            local_ts = int(time.time() * 1000)
            ts_offset_ms = max(0, local_ts - server_ts)
            if ts_offset_ms > 0:
                print(f"  CLOCK_OFFSET_MS={ts_offset_ms} (local ahead of Binance)")
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
) -> LiveConnectorV0:
    """Build LiveConnectorV0 with optional fixture transport.

    Args:
        symbols: List of trading symbols.
        mode: SafeMode for the connector.
        fixture_path: Optional path to JSONL fixture file.
        use_testnet: Use testnet WS endpoint (default True for safety).

    Returns:
        Configured LiveConnectorV0 instance.
    """
    ws_transport = None
    if fixture_path:
        with Path(fixture_path).open() as f:
            messages = [line.strip() for line in f if line.strip()]
        ws_transport = FakeWsTransport(messages=messages, delay_ms=100)

    ws_url = BINANCE_WS_MAINNET if not use_testnet else "wss://testnet.binance.vision/ws"

    config = LiveConnectorConfig(
        mode=mode,
        symbols=symbols,
        ws_transport=ws_transport,
        ws_url=ws_url,
        use_testnet=use_testnet,
    )
    return LiveConnectorV0(config=config)


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


def build_engine(  # noqa: PLR0915
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
    )


async def trading_loop(
    connector: LiveConnectorV0,
    engine: LiveEngineV0,
    shutdown: asyncio.Event,
    duration_s: int,
) -> None:
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
    await connector.connect()
    _loop_ready = True
    print("  /readyz now returning 200 (if HA permits)")
    start = time.time()
    tick_count = 0
    ha_skip_count = 0
    try:
        async for snapshot in connector.iter_snapshots():
            if shutdown.is_set():
                break
            if duration_s > 0 and (time.time() - start) >= duration_s:
                print(f"\nDuration ({duration_s}s) reached after {tick_count} ticks.")
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
        await connector.close()
        print(f"  Trading loop stopped. Total ticks: {tick_count}, HA skips: {ha_skip_count}")


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
        default=1,
        help="Max orders per run (default 1, canary safety cap). "
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


def main() -> None:  # noqa: PLR0915
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

    server = run_server(args.metrics_port)
    print(f"  Health endpoint: http://localhost:{args.metrics_port}/healthz")

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

    # Register readyz callback so /metrics emits grinder_readyz_ready gauge (PR-ALERTS-0)
    set_ready_fn(is_trading_ready)

    connector = build_connector(symbols, mode, args.fixture, use_testnet=use_testnet)

    # Async loop with signal handling
    loop = asyncio.new_event_loop()
    shutdown = asyncio.Event()

    def handle_signal(*_: object) -> None:
        loop.call_soon_threadsafe(shutdown.set)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("\nGRINDER TRADING LOOP running. Press Ctrl+C to stop.")
    exit_code = 0
    try:
        loop.run_until_complete(trading_loop(connector, engine, shutdown, args.duration_s))
    except Exception as exc:
        print(f"GRINDER TRADING LOOP FATAL: {exc}")
        exit_code = 2
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(_drain_pending_tasks())
        loop.close()
        if elector is not None:
            print("  Stopping LeaderElector...")
            elector.stop()
        server.shutdown()
        print("GRINDER TRADING LOOP stopped.")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
