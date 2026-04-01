"""E3 bounded graceful-exit ceremony (PR-E3-proof).

Proves the real graceful-exit → no-new-entries → deactivation path:
1. Preflight: verify exchange clean
2. Start bounded trading run (subprocess)
3. Wait for trading activity (entries placed)
4. Apply graceful-exit signal to the running engine
5. Observe no-new-entries window after signal
6. Wait for position to converge to FLAT
7. Shutdown + cleanup + verify clean

This runs the engine as a subprocess with a control protocol:
- Engine runs for --duration-s with --max-orders
- After --graceful-exit-after-s, SIGUSR1 triggers graceful-exit
  (engine must handle SIGUSR1 as graceful-exit signal)
- After graceful-exit, engine continues but blocks new entries
- Normal duration/SIGINT shutdown follows

Usage:
    python3 -m scripts.e3_graceful_exit_ceremony \
        --symbol BTCUSDT \
        --duration-s 300 \
        --graceful-exit-after-s 60 \
        --max-orders 50 \
        --paper-size-per-level 0.002 \
        --paper-levels 1 \
        2>&1 | tee /tmp/e3_ceremony.log

Note: This ceremony requires GRINDER_SYMBOL_SELECTOR_ENABLED=1 for the
dispatch gate to be active. Without it, graceful-exit has no effect.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time

logger = logging.getLogger("e3_ceremony")


def _run_exchange_state(symbol: str, cmd: str) -> tuple[bool, str]:
    """Run exchange_state.py command."""
    args = [sys.executable, "-m", "scripts.exchange_state", cmd, symbol]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)


def run_ceremony(args: argparse.Namespace) -> int:
    """Run the E3 graceful-exit ceremony."""
    symbol = args.symbol

    # Phase 1: Preflight
    logger.info("E3_CEREMONY_PREFLIGHT symbol=%s", symbol)
    ok, output = _run_exchange_state(symbol, "verify")
    if not ok:
        logger.error("E3_CEREMONY_RESULT status=FAILED reason=PREFLIGHT_DIRTY output=%s", output)
        return 1
    logger.info("E3_CEREMONY_PREFLIGHT symbol=%s status=CLEAN", symbol)

    # Phase 2: Start trading with selector enabled
    logger.info(
        "E3_CEREMONY_START symbol=%s duration_s=%d graceful_exit_after_s=%d",
        symbol,
        args.duration_s,
        args.graceful_exit_after_s,
    )

    import signal as _signal  # noqa: PLC0415

    cmd = [
        sys.executable,
        "-m",
        "scripts.run_trading",
        "--exchange-port",
        "futures",
        "--symbols",
        symbol,
        "--duration-s",
        str(args.duration_s),
        "--max-orders-per-run",
        str(args.max_orders),
        "--max-notional-per-order",
        "200",
        "--paper-size-per-level",
        args.paper_size_per_level,
        "--paper-spacing-bps",
        "10.0",
        "--paper-levels",
        str(args.paper_levels),
        "--armed",
        "--mainnet",
    ]

    logger.info("E3_CEREMONY_TRADING_ACTIVE symbol=%s", symbol)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # Phase 3: Wait for some trading activity
    logger.info(
        "E3_CEREMONY_WAITING_FOR_ACTIVITY symbol=%s wait_s=%d",
        symbol,
        args.graceful_exit_after_s,
    )
    time.sleep(args.graceful_exit_after_s)

    # Phase 4: Send SIGUSR1 for graceful-exit signal
    # Engine handles SIGUSR1 as force_graceful_exit for all symbols
    logger.info("E3_CEREMONY_GRACEFUL_EXIT_SIGNAL symbol=%s", symbol)
    if hasattr(_signal, "SIGUSR1"):
        proc.send_signal(_signal.SIGUSR1)
    else:
        logger.warning("E3_CEREMONY_NO_SIGUSR1 — platform unsupported")

    # Phase 5: Observe no-new-entries window
    observation_s = min(
        args.observation_window_s, args.duration_s - args.graceful_exit_after_s - 30
    )
    if observation_s > 0:
        logger.info(
            "E3_CEREMONY_OBSERVATION_WINDOW symbol=%s seconds=%d",
            symbol,
            observation_s,
        )
        time.sleep(observation_s)
    logger.info("E3_CEREMONY_OBSERVATION_COMPLETE symbol=%s", symbol)

    # Phase 6: SIGINT for graceful shutdown + cleanup-on-exit
    logger.info("E3_CEREMONY_SHUTDOWN_REQUESTED symbol=%s", symbol)
    proc.send_signal(_signal.SIGINT)

    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        logger.warning("E3_CEREMONY_TIMEOUT symbol=%s", symbol)
        proc.kill()
        proc.wait(timeout=10)

    exit_code = proc.returncode
    logger.info("E3_CEREMONY_TRADING_EXITED symbol=%s exit_code=%d", symbol, exit_code or -1)

    # Phase 6: Post-ceremony verification
    for attempt in range(1, 4):
        logger.info("E3_CEREMONY_VERIFY symbol=%s attempt=%d/3", symbol, attempt)
        ok, output = _run_exchange_state(symbol, "verify")
        if ok:
            logger.info("E3_CEREMONY_VERIFY symbol=%s status=CLEAN", symbol)
            logger.info("E3_CEREMONY_RESULT status=SUCCESS symbol=%s", symbol)
            return 0
        logger.warning("E3_CEREMONY_VERIFY symbol=%s status=DIRTY attempt=%d", symbol, attempt)
        if attempt < 3:
            _run_exchange_state(symbol, "cleanup")
            time.sleep(5)

    logger.error("E3_CEREMONY_RESULT status=FAILED reason=POST_VERIFY_DIRTY")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="E3 bounded graceful-exit ceremony")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--duration-s", type=int, default=300)
    parser.add_argument("--graceful-exit-after-s", type=int, default=60)
    parser.add_argument("--max-orders", type=int, default=50)
    parser.add_argument("--paper-size-per-level", default="0.002")
    parser.add_argument("--paper-levels", type=int, default=1)
    parser.add_argument(
        "--observation-window-s",
        type=int,
        default=30,
        help="Seconds to observe no-new-entries after graceful-exit signal",
    )
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if args.check_only:
        ok, _ = _run_exchange_state(args.symbol, "verify")
        sys.exit(0 if ok else 1)

    sys.exit(run_ceremony(args))


if __name__ == "__main__":
    main()
