#!/usr/bin/env python3
"""Autonomous multi-symbol control-plane runner with execution scaffold.

Assembles and runs the autonomous orchestration loop. Execution ceremonies
are registry-level placeholders — real per-symbol engine lifecycle (LiveEngineV0
start/stop) requires bridging to run_trading infrastructure (future work).

Control-plane (real):
  UniverseProvider → prefilter → tuning → ranking → SymbolOrchestrator → AutonomousLoop

Execution-plane (scaffold — registry transitions only, no real engines):
  EngineRegistry → ExecutionCoordinator → placeholder ceremony bindings

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
    return p


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

    # Real ceremony bindings (placeholder implementations for now —
    # full per-symbol engine lifecycle requires run_trading integration)
    def activate_fn(symbol: str) -> bool:
        logger.info("CEREMONY_ACTIVATE symbol=%s", symbol)
        registry.register(symbol)
        from grinder.execution_plane.registry import EngineState  # noqa: PLC0415

        registry.transition(symbol, EngineState.ACTIVE, reason="ceremony_activate")
        return True

    def graceful_exit_fn(symbol: str) -> bool:
        logger.info("CEREMONY_GRACEFUL_EXIT symbol=%s", symbol)
        import contextlib  # noqa: PLC0415

        from grinder.execution_plane.registry import EngineState  # noqa: PLC0415

        with contextlib.suppress(Exception):
            registry.transition(symbol, EngineState.GRACEFUL_EXIT, reason="ceremony_graceful_exit")
        return True

    def deactivate_fn(symbol: str) -> bool:
        logger.info("CEREMONY_DEACTIVATE symbol=%s", symbol)
        import contextlib  # noqa: PLC0415

        from grinder.execution_plane.registry import EngineState  # noqa: PLC0415

        with contextlib.suppress(Exception):
            registry.transition(symbol, EngineState.SHUTTING_DOWN, reason="ceremony_deactivate")
            registry.transition(symbol, EngineState.STOPPED, reason="ceremony_deactivate_done")
        return True

    coordinator = ExecutionCoordinator(
        activate_fn=activate_fn,
        graceful_exit_fn=graceful_exit_fn,
        deactivate_fn=deactivate_fn,
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

    print(
        f"\nGRINDER AUTONOMOUS SYSTEM starting."
        f"\n  pid={os.getpid()}"
        f"\n  mode={exec_mode} {ack_status}"
        f"\n  execution_ceremonies=scaffold (registry transitions only, no real engines)"
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


if __name__ == "__main__":
    main()
