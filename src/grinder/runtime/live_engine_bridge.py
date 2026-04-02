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

    def graceful_exit(self, symbol: str, engine_ref: Any) -> bool:
        """Signal graceful exit on a running engine."""
        handle: EngineHandle = engine_ref
        engine = handle.engine_ref
        if engine is None:
            logger.warning("BRIDGE_GRACEFUL_EXIT_NO_ENGINE symbol=%s", symbol)
            return False
        try:
            result = engine.force_graceful_exit(symbol)
            logger.info("BRIDGE_GRACEFUL_EXIT symbol=%s result=%s", symbol, result)
            return bool(result)
        except Exception as e:
            logger.error("BRIDGE_GRACEFUL_EXIT_ERROR symbol=%s error=%s", symbol, e)
            return False

    def cleanup(self, symbol: str, engine_ref: Any) -> bool:  # noqa: ARG002
        """Cleanup after engine stop. Currently no-op beyond logging."""
        logger.info("BRIDGE_ENGINE_CLEANUP symbol=%s", symbol)
        return True

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
        from grinder.paper.engine import PaperEngine  # noqa: PLC0415

        cfg = self._config
        mode = SafeMode(cfg.mode)

        paper = PaperEngine(
            spacing_bps=cfg.spacing_bps,
            levels=cfg.levels,
            size_per_level=Decimal(cfg.size_per_level),
        )
        port = self._build_port(symbol, mode)
        engine_config = LiveEngineConfig(
            armed=cfg.armed,
            mode=mode,
        )
        engine = LiveEngineV0(
            paper_engine=paper,
            exchange_port=port,
            config=engine_config,
            operator_symbols=[symbol],
        )

        # Propagate engine ref to the handle so graceful_exit can reach it.
        if handle_ref is not None and handle_ref[0] is not None:
            handle_ref[0].engine_ref = engine

        connector_config = LiveConnectorConfig(
            mode=mode,
            symbols=[symbol],
            use_testnet=cfg.use_testnet,
            ws_transport=cfg.ws_transport,
        )
        connector = LiveConnectorV0(config=connector_config)

        # Signal readiness AFTER connector.connect() succeeds.
        # If connect fails, engine_ready never fires → factory fail-closed.
        try:
            await connector.connect()
            logger.info("BRIDGE_ENGINE_CONNECTED symbol=%s mode=%s", symbol, mode.value)
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
