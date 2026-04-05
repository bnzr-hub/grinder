"""Bridge between AutonomousEngineHost and real LiveEngineV0 lifecycle (ADR-149).

Creates, runs, and stops real per-symbol engine instances in background threads.
Each symbol gets its own: async event loop + connector + engine + trading loop.

The bridge provides injectable callables that AutonomousEngineHost uses
as factory/stop/graceful_exit/cleanup operations.

SSOT: this module for per-symbol engine lifecycle bridge.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EngineHandle:
    """Handle to a running per-symbol engine in a background thread.

    The handle owns the thread + shutdown event. The engine and connector
    live inside the thread's async loop.
    """

    symbol: str
    thread: threading.Thread
    shutdown_event: threading.Event
    engine_ref: Any = None  # LiveEngineV0 (set after startup inside thread)
    port_ref: Any = None  # ExchangePort (set during engine creation for shutdown cleanup)
    started_at: float = 0.0
    error: str | None = None

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()


@dataclass(frozen=True)
class BridgeConfig:
    """Configuration for LiveEngineBridge.

    All fields have safe defaults (read_only + NoOp port).
    Production requires explicit ``exchange_port="futures"`` +
    ``armed=True`` + ``mode="live_trade"`` + API keys in env.

    Exchange port modes:
    - ``noop`` (default): NoOpExchangePort, no real orders
    - ``futures``: BinanceFuturesPort, requires API keys + safety gates
    """

    mode: str = "read_only"  # SafeMode value
    armed: bool = False
    use_testnet: bool = True
    exchange_port: str = "noop"  # "noop" or "futures"
    spacing_bps: float = 10.0
    levels: int = 5
    size_per_level: str = "0.001"
    max_notional_per_order: str = "100"
    max_orders_per_run: int = 500
    shutdown_timeout_s: float = 30.0
    ws_transport: Any = None  # Injectable WsTransport for testing (None = real WebSocket)


class LiveEngineBridge:
    """Creates and manages real per-symbol engine threads.

    Provides callables compatible with AutonomousEngineHost:
    - factory(symbol) -> EngineHandle
    - stop(symbol, handle) -> bool
    - graceful_exit(symbol, handle) -> bool
    - cleanup(symbol, handle) -> bool
    """

    def __init__(self, config: BridgeConfig | None = None) -> None:
        self._config = config or BridgeConfig()
        # Per-symbol tuned order sizes. Set by bootstrap before activation.
        # Key: symbol, Value: order_size string (e.g. "124", "0.002").
        self._symbol_sizes: dict[str, str] = {}
        # Per-symbol grid_v2 config from tuning (tick_size, step_size, etc.)
        self._symbol_grid_config: dict[str, dict[str, str]] = {}
        # Per-symbol adaptive spacing in bps (from NATR via compute_adaptive_spacing_bps).
        self._symbol_spacings: dict[str, Decimal] = {}
        # Serialize engine construction so env propagation is thread-safe.
        # grid_v2 config is passed via process-global os.environ; this lock
        # prevents overlapping engine __init__ calls from reading each other's
        # symbol/size/tick values.
        self._engine_construction_lock = threading.Lock()

    def set_symbol_size(self, symbol: str, order_size: str) -> None:
        """Register tuning-resolved order size for a symbol."""
        self._symbol_sizes[symbol] = order_size
        logger.info("BRIDGE_SYMBOL_SIZE_SET symbol=%s order_size=%s", symbol, order_size)

    def set_symbol_spacing(self, symbol: str, spacing_bps: Decimal) -> None:
        """Register tuning-resolved adaptive spacing for a symbol."""
        self._symbol_spacings[symbol] = spacing_bps
        logger.info("BRIDGE_SYMBOL_SPACING_SET symbol=%s spacing_bps=%s", symbol, spacing_bps)

    def set_symbol_grid_config(
        self,
        symbol: str,
        tick_size: str,
        step_size: str,
    ) -> None:
        """Register tuning-resolved grid_v2 config for a symbol."""
        self._symbol_grid_config[symbol] = {
            "tick_size": tick_size,
            "step_size": step_size,
        }
        logger.info(
            "BRIDGE_SYMBOL_GRID_CONFIG symbol=%s tick_size=%s step_size=%s",
            symbol,
            tick_size,
            step_size,
        )

    # Env vars set by _propagate_grid_v2_env (for save/restore)
    _GRID_V2_ENV_KEYS: tuple[str, ...] = (
        "GRINDER_GRID_V2_ENABLED",
        "GRINDER_GRID_V2_SYMBOL",
        "GRINDER_GRID_V2_ORDER_SIZE",
        "GRINDER_GRID_V2_RESEED_ON_FLAT",
        "GRINDER_GRID_V2_SYNC_RECONCILER_ENABLED",
        "GRINDER_GRID_V2_SYNC_RECONCILER_PRIMARY",
        "GRINDER_GRID_V2_SYNC_RECONCILER_SHADOW",
        "GRINDER_GRID_V2_STEP_PCT",
        "GRINDER_GRID_V2_ENTRY_LEVELS",
        "GRINDER_GRID_V2_TICK_SIZE",
        "GRINDER_ACCOUNT_SYNC_ENABLED",
    )

    def _propagate_grid_v2_env(self, symbol: str, effective_size: str, cfg: BridgeConfig) -> None:
        """Set GRINDER_GRID_V2_* env vars so LiveEngineV0 activates grid_v2.

        Called before engine construction. Engine reads these in __init__.
        Only activates grid_v2 if the symbol has a tuned size (autonomous path).
        """
        if symbol not in self._symbol_sizes:
            return

        os.environ["GRINDER_GRID_V2_ENABLED"] = "1"
        os.environ["GRINDER_GRID_V2_SYMBOL"] = symbol
        os.environ["GRINDER_GRID_V2_ORDER_SIZE"] = effective_size
        os.environ["GRINDER_GRID_V2_RESEED_ON_FLAT"] = "1"
        os.environ["GRINDER_GRID_V2_SYNC_RECONCILER_ENABLED"] = "1"
        os.environ["GRINDER_GRID_V2_SYNC_RECONCILER_PRIMARY"] = "1"
        os.environ["GRINDER_GRID_V2_SYNC_RECONCILER_SHADOW"] = "0"
        # Account sync: required for grid_v2 startup (needs _last_account_snapshot)
        os.environ["GRINDER_ACCOUNT_SYNC_ENABLED"] = "1"
        # Spacing: per-symbol adaptive if available, else bridge config fallback
        effective_spacing_bps = self._symbol_spacings.get(symbol, Decimal(str(cfg.spacing_bps)))
        spacing_pct = effective_spacing_bps / Decimal("10000")
        os.environ["GRINDER_GRID_V2_STEP_PCT"] = str(spacing_pct)
        os.environ["GRINDER_GRID_V2_ENTRY_LEVELS"] = str(cfg.levels)
        # Tick size from tuning result (required by engine)
        grid_cfg = self._symbol_grid_config.get(symbol)
        if grid_cfg:
            os.environ["GRINDER_GRID_V2_TICK_SIZE"] = grid_cfg["tick_size"]
        logger.info(
            "BRIDGE_GRID_V2_ENV symbol=%s enabled=1 order_size=%s "
            "step_pct=%s levels=%s tick_size=%s",
            symbol,
            effective_size,
            spacing_pct,
            cfg.levels,
            grid_cfg["tick_size"] if grid_cfg else "NOT_SET",
        )

    def _build_account_syncer(self, symbol: str, port: Any) -> Any:
        """Create AccountSyncer if symbol has a tuned order size.

        grid_v2 startup requires _last_account_snapshot, which is only
        populated when an AccountSyncer is present and account sync is enabled.
        Returns None for symbols not in _symbol_sizes (safe fallback).
        """
        if symbol not in self._symbol_sizes:
            return None
        from grinder.account.syncer import AccountSyncer  # noqa: PLC0415

        logger.info("BRIDGE_ACCOUNT_SYNCER_CREATED symbol=%s", symbol)
        return AccountSyncer(port)

    def _restore_grid_v2_env(self, saved: dict[str, str | None]) -> None:
        """Restore env vars to pre-propagation state."""
        for key, original in saved.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

    def factory(self, symbol: str) -> EngineHandle:
        """Create and start a real engine for one symbol in a background thread."""
        shutdown_event = threading.Event()
        engine_ready = threading.Event()
        handle_ref: list[EngineHandle | None] = [None]
        handle = EngineHandle(
            symbol=symbol,
            thread=threading.Thread(
                target=self._run_engine_thread,
                args=(symbol, shutdown_event, handle_ref),
                kwargs={"engine_ready": engine_ready},
                name=f"engine-{symbol}",
                daemon=True,
            ),
            shutdown_event=shutdown_event,
            started_at=time.monotonic(),
        )
        handle_ref[0] = handle
        handle.thread.start()
        # Wait for engine construction — fail closed if not ready in time.
        ready = engine_ready.wait(timeout=10.0)
        if not ready or handle.engine_ref is None:
            logger.error(
                "BRIDGE_ENGINE_STARTUP_FAILED symbol=%s ready=%s alive=%s engine_ref=%s",
                symbol,
                ready,
                handle.thread.is_alive(),
                handle.engine_ref is not None,
            )
            handle.shutdown_event.set()
            handle.thread.join(timeout=5.0)
            raise RuntimeError(f"Engine startup failed for {symbol}: ready={ready}")
        logger.info("BRIDGE_ENGINE_STARTED symbol=%s thread=%s", symbol, handle.thread.name)
        return handle

    def stop(self, symbol: str, engine_ref: Any) -> bool:
        """Signal shutdown and wait for engine thread to stop."""
        handle: EngineHandle = engine_ref
        handle.shutdown_event.set()
        handle.thread.join(timeout=self._config.shutdown_timeout_s)
        if handle.thread.is_alive():
            logger.error(
                "BRIDGE_ENGINE_STOP_TIMEOUT symbol=%s timeout_s=%s",
                symbol,
                self._config.shutdown_timeout_s,
            )
            return False
        logger.info("BRIDGE_ENGINE_STOPPED symbol=%s", symbol)
        return True

    def graceful_exit(self, symbol: str, engine_ref: Any) -> Any:
        """Signal graceful exit on a running engine.

        Returns GracefulExitResult: SUCCESS, NOT_APPLICABLE, or FAILED.
        """
        from grinder.runtime.autonomous_host import GracefulExitResult  # noqa: PLC0415

        handle: EngineHandle = engine_ref
        engine = handle.engine_ref
        if engine is None:
            logger.warning("BRIDGE_GRACEFUL_EXIT_NO_ENGINE symbol=%s", symbol)
            return GracefulExitResult.FAILED
        try:
            ok = engine.force_graceful_exit(symbol)
            logger.info("BRIDGE_GRACEFUL_EXIT symbol=%s result=%s", symbol, ok)
            if ok:
                return GracefulExitResult.SUCCESS
            # force_graceful_exit returns False when no position / nothing to exit.
            # This is benign — NOT_APPLICABLE, not FAILED.
            return GracefulExitResult.NOT_APPLICABLE
        except Exception as e:
            logger.error("BRIDGE_GRACEFUL_EXIT_ERROR symbol=%s error=%s", symbol, e)
            return GracefulExitResult.FAILED

    def cleanup(self, symbol: str, engine_ref: Any) -> bool:
        """Cancel all open orders and close any position for symbol.

        Called after engine thread is stopped. Uses the port reference
        stored on the handle to perform real exchange writes.
        Safe to call when already flat/no-orders (idempotent).
        """
        handle: EngineHandle = engine_ref
        port = handle.port_ref
        if port is None or not hasattr(port, "fetch_positions_raw"):
            logger.info(
                "BRIDGE_ENGINE_CLEANUP symbol=%s port=%s (no-op)",
                symbol,
                type(port).__name__ if port else "None",
            )
            return True

        from grinder.observability.latency_telemetry import (  # noqa: PLC0415
            PhaseTimer,
            log_shutdown,
        )

        cleanup_timer = PhaseTimer()
        ok = True
        # Step 1: cancel all open orders
        cancel_timer = PhaseTimer()
        try:
            cancelled = port.cancel_all_orders(symbol)
            logger.info("BRIDGE_CLEANUP_CANCEL_ALL symbol=%s cancelled=%s", symbol, cancelled)
        except Exception as e:
            logger.error("BRIDGE_CLEANUP_CANCEL_FAILED symbol=%s error=%s", symbol, e)
            ok = False
        cancel_ms = cancel_timer.elapsed_ms()

        # Step 2: close any open position
        close_timer = PhaseTimer()
        try:
            positions = port.fetch_positions_raw(symbol)
            pos_qty = Decimal("0")
            for p in positions:
                qty = Decimal(str(p.get("positionAmt", "0")))
                if qty != 0:
                    pos_qty = qty
            if pos_qty != 0:
                order_id = port.close_position(symbol)
                logger.info(
                    "BRIDGE_CLEANUP_CLOSE_POSITION symbol=%s qty=%s order_id=%s",
                    symbol,
                    pos_qty,
                    order_id,
                )
            else:
                logger.info("BRIDGE_CLEANUP_POSITION_FLAT symbol=%s", symbol)
        except Exception as e:
            logger.error("BRIDGE_CLEANUP_CLOSE_FAILED symbol=%s error=%s", symbol, e)
            ok = False
        close_ms = close_timer.elapsed_ms()

        log_shutdown(symbol, cancel_ms, close_ms, cleanup_timer.elapsed_ms())
        logger.info("BRIDGE_ENGINE_CLEANUP symbol=%s ok=%s", symbol, ok)
        return ok

    def _build_port(self, symbol: str, mode: Any) -> Any:
        """Build exchange port based on config.

        Returns NoOpExchangePort (safe default) or BinanceFuturesPort.
        Raises RuntimeError if futures port requirements are not met.
        """
        import os  # noqa: PLC0415

        cfg = self._config
        if cfg.exchange_port == "noop":
            from grinder.execution.port import NoOpExchangePort  # noqa: PLC0415

            return NoOpExchangePort()

        if cfg.exchange_port == "futures":
            from grinder.execution.binance_futures_port import (  # noqa: PLC0415
                BinanceFuturesPort,
                BinanceFuturesPortConfig,
            )

            api_key = os.environ.get("BINANCE_API_KEY", "").strip()
            api_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
            if not api_key or not api_secret:
                raise RuntimeError(
                    f"exchange_port=futures requires BINANCE_API_KEY and BINANCE_API_SECRET "
                    f"for symbol={symbol}"
                )

            base_url = (
                "https://testnet.binancefuture.com"
                if cfg.use_testnet
                else "https://fapi.binance.com"
            )
            port_config = BinanceFuturesPortConfig(
                mode=mode,
                base_url=base_url,
                api_key=api_key,
                api_secret=api_secret,
                symbol_whitelist=[symbol],
                allow_mainnet=not cfg.use_testnet,
                max_notional_per_order=Decimal(cfg.max_notional_per_order),
                max_orders_per_run=cfg.max_orders_per_run,
            )
            # Use the scripts-level HTTP client factory (same as run_trading.py)
            from scripts.http_measured_client import RequestsHttpClient  # noqa: PLC0415

            http_client = RequestsHttpClient(port_name=f"bridge-{symbol}")
            logger.info(
                "BRIDGE_PORT_FUTURES symbol=%s testnet=%s armed=%s",
                symbol,
                cfg.use_testnet,
                cfg.armed,
            )
            return BinanceFuturesPort(http_client=http_client, config=port_config)

        raise RuntimeError(f"Unknown exchange_port={cfg.exchange_port!r}")

    def _run_engine_thread(
        self,
        symbol: str,
        shutdown_event: threading.Event,
        handle_ref: list,  # type: ignore[type-arg]
        *,
        engine_ready: threading.Event | None = None,
    ) -> None:
        """Thread entry point: create engine + connector, run trading loop."""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    self._run_engine_async(
                        symbol, shutdown_event, handle_ref, engine_ready=engine_ready
                    )
                )
            finally:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
        except Exception as e:
            logger.error("BRIDGE_ENGINE_THREAD_FATAL symbol=%s error=%s", symbol, e)

    async def _run_engine_async(
        self,
        symbol: str,
        shutdown_event: threading.Event,
        handle_ref: list | None = None,  # type: ignore[type-arg]
        *,
        engine_ready: threading.Event | None = None,
    ) -> None:
        """Async engine lifecycle: connect → process snapshots → shutdown."""
        from grinder.connectors.live_connector import (  # noqa: PLC0415
            LiveConnectorConfig,
            LiveConnectorV0,
            SafeMode,
        )
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415
        from grinder.observability.latency_telemetry import (  # noqa: PLC0415
            PhaseTimer,
            log_engine_startup,
        )
        from grinder.paper.engine import PaperEngine  # noqa: PLC0415

        startup_timer = PhaseTimer()
        cfg = self._config
        mode = SafeMode(cfg.mode)

        # Per-symbol tuned size takes precedence over bridge default
        effective_size = self._symbol_sizes.get(symbol, cfg.size_per_level)
        logger.info(
            "BRIDGE_ENGINE_SIZE symbol=%s size=%s source=%s",
            symbol,
            effective_size,
            "tuning" if symbol in self._symbol_sizes else "default",
        )

        # Engine construction under lock: grid_v2 config is passed via
        # process-global os.environ, so overlapping constructions would race.
        # Lock serializes; try/finally guarantees env restore even on failure.
        with self._engine_construction_lock:
            saved_env = {k: os.environ.get(k) for k in self._GRID_V2_ENV_KEYS}
            self._propagate_grid_v2_env(symbol, effective_size, cfg)
            try:
                paper = PaperEngine(
                    spacing_bps=cfg.spacing_bps,
                    levels=cfg.levels,
                    size_per_level=Decimal(effective_size),
                )
                port = self._build_port(symbol, mode)
                engine_config = LiveEngineConfig(
                    armed=cfg.armed,
                    mode=mode,
                )
                # AccountSyncer: grid_v2 needs _last_account_snapshot for startup.
                account_syncer = self._build_account_syncer(symbol, port)
                engine = LiveEngineV0(
                    paper_engine=paper,
                    exchange_port=port,
                    config=engine_config,
                    operator_symbols=[symbol],
                    account_syncer=account_syncer,
                )
            finally:
                self._restore_grid_v2_env(saved_env)
        engine_ms = startup_timer.elapsed_ms()

        # Propagate engine + port refs to the handle for graceful_exit and cleanup.
        if handle_ref is not None and handle_ref[0] is not None:
            handle_ref[0].engine_ref = engine
            handle_ref[0].port_ref = port

        # Futures WS URL: testnet and mainnet use different endpoints than spot.
        ws_url = (
            (
                "wss://stream.binancefuture.com/ws"
                if cfg.use_testnet
                else "wss://fstream.binance.com/ws"
            )
            if cfg.exchange_port == "futures"
            else None
        )
        connector_config = LiveConnectorConfig(
            mode=mode,
            symbols=[symbol],
            use_testnet=cfg.use_testnet,
            ws_transport=cfg.ws_transport,
            ws_url=ws_url,
        )
        connector = LiveConnectorV0(config=connector_config)

        # Signal readiness AFTER connector.connect() succeeds.
        # If connect fails, engine_ready never fires → factory fail-closed.
        try:
            await connector.connect()
            logger.info("BRIDGE_ENGINE_CONNECTED symbol=%s mode=%s", symbol, mode.value)
            log_engine_startup(
                symbol,
                engine_ms,
                startup_timer.elapsed_ms() - engine_ms,
                startup_timer.elapsed_ms(),
            )
            if engine_ready is not None:
                engine_ready.set()
            async for snapshot in connector.iter_snapshots():
                if shutdown_event.is_set():
                    break
                engine.process_snapshot(snapshot)
        except Exception as e:
            logger.error("BRIDGE_ENGINE_LOOP_ERROR symbol=%s error=%s", symbol, e)
        finally:
            import contextlib  # noqa: PLC0415

            with contextlib.suppress(Exception):
                await connector.close()
            logger.info("BRIDGE_ENGINE_LOOP_ENDED symbol=%s", symbol)
