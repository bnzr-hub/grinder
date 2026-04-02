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
import logging
import os
import signal
import sys
import time
from typing import Any

logger = logging.getLogger("autonomous")


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
            max_notional_per_order=args.max_notional_per_order,
            max_orders_per_run=args.max_orders_per_run,
            ws_transport=ws_transport,
        )
    )


# ---------------------------------------------------------------------------
# Runtime assembly
# ---------------------------------------------------------------------------


def build_runtime(args: argparse.Namespace) -> dict:  # type: ignore[type-arg]
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
    from grinder.tuning.cache import TuningCache  # noqa: PLC0415

    # Parse symbols/blacklist
    symbols_override = frozenset(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    blacklist = frozenset(s.strip().upper() for s in args.blacklist.split(",") if s.strip())

    # Control-plane components
    universe_config = UniverseProviderConfig(blacklist=blacklist)
    universe_provider = UniverseProvider(config=universe_config)

    tuning_cache = TuningCache(ttl_s=300.0)

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

    # Assemble autonomous loop with execution integration
    loop = AutonomousLoop(
        universe_provider=universe_provider,
        orchestrator=orchestrator,
        config=loop_config,
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
    }


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

    # Signal handling
    def handle_stop(*_: object) -> None:
        loop.stop("signal_received")

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    print("\nGRINDER AUTONOMOUS SYSTEM running. Press Ctrl+C to stop.\n")

    # Run
    try:
        reports = loop.run_forever(
            clock=time.monotonic,
            sleep_fn=time.sleep,
            max_cycles=args.max_cycles,
        )
        print(f"\nGRINDER AUTONOMOUS SYSTEM finished. cycles={len(reports)} pid={os.getpid()}")
    except Exception as e:
        logger.error("AUTONOMOUS_SYSTEM_FATAL error=%s", e)
        sys.exit(1)
    finally:
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
