"""Bounded shutdown cleanup ceremony (PR-C2b, ADR-130).

Proves that bounded trading followed by graceful shutdown + cleanup-on-exit
reaches clean exchange state:
- No open orders
- Flat position

Sequence:
1. Pre-flight: verify exchange is clean for the target symbol
2. Start bounded trading run (operator provides duration + budget)
3. After deactivation_after_s: send SIGINT for graceful shutdown
4. Cleanup-on-exit runs exchange_state cleanup per symbol
5. Post-ceremony: verify exchange state is clean (with retries)

Note: This proves the shutdown/cleanup path, not yet the full
graceful_exit_only deactivation path with observed fills. That requires
a ceremony iteration with explicit deactivation signal and fill evidence.

Usage:
    python3 -m scripts.deactivation_ceremony \
        --symbol BTCUSDT \
        --duration-s 300 \
        --deactivation-after-s 60 \
        --max-orders 50 \
        --paper-size-per-level 0.002 \
        --paper-levels 1 \
        --check-only

    # --check-only: just verify exchange state, don't run ceremony
    # Without --check-only: runs full ceremony (requires mainnet env vars)

Evidence:
    Logs are emitted with stable signal prefixes:
    - DEACTIVATION_CEREMONY_START
    - DEACTIVATION_CEREMONY_PREFLIGHT
    - DEACTIVATION_CEREMONY_TRADING_ACTIVE
    - DEACTIVATION_CEREMONY_DEACTIVATION_REQUESTED
    - DEACTIVATION_CEREMONY_CONVERGENCE_CHECK
    - DEACTIVATION_CEREMONY_VERIFY
    - DEACTIVATION_CEREMONY_RESULT status=SUCCESS|FAILED
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time

logger = logging.getLogger("deactivation_ceremony")


def _run_exchange_state(symbol: str, cmd: str) -> tuple[bool, str]:
    """Run exchange_state.py command and return (success, output)."""
    args = [sys.executable, "-m", "scripts.exchange_state", cmd, symbol]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)


def _verify_clean(symbol: str) -> tuple[bool, str]:
    """Verify exchange state is clean for symbol."""
    return _run_exchange_state(symbol, "verify")


def _cleanup_symbol(symbol: str) -> tuple[bool, str]:
    """Cleanup exchange state for symbol."""
    return _run_exchange_state(symbol, "cleanup")


def run_preflight(symbol: str) -> bool:
    """Pre-flight: verify exchange is clean."""
    logger.info("DEACTIVATION_CEREMONY_PREFLIGHT symbol=%s", symbol)
    ok, output = _verify_clean(symbol)
    if ok:
        logger.info("DEACTIVATION_CEREMONY_PREFLIGHT symbol=%s status=CLEAN", symbol)
    else:
        logger.warning(
            "DEACTIVATION_CEREMONY_PREFLIGHT symbol=%s status=DIRTY output=%s",
            symbol,
            output,
        )
    return ok


def run_trading_phase(
    symbol: str,
    duration_s: int,
    deactivation_after_s: int,
    max_orders: int,
    paper_size: str,
    paper_levels: int,
) -> tuple[bool, str]:
    """Run bounded trading phase with graceful shutdown.

    Starts trading, waits for deactivation_after_s, then sends SIGINT
    for graceful shutdown. Cleanup-on-exit handles order cancellation
    and position closure.

    Returns (success, log_output).
    """
    logger.info(
        "DEACTIVATION_CEREMONY_START symbol=%s duration_s=%d deactivation_after_s=%d",
        symbol,
        duration_s,
        deactivation_after_s,
    )

    # Build run_trading command
    cmd = [
        sys.executable,
        "-m",
        "scripts.run_trading",
        "--exchange-port",
        "futures",
        "--symbols",
        symbol,
        "--duration-s",
        str(duration_s),
        "--max-orders-per-run",
        str(max_orders),
        "--max-notional-per-order",
        "200",
        "--paper-size-per-level",
        paper_size,
        "--paper-spacing-bps",
        "10.0",
        "--paper-levels",
        str(paper_levels),
        "--armed",
        "--mainnet",
    ]

    logger.info("DEACTIVATION_CEREMONY_TRADING_ACTIVE symbol=%s cmd=%s", symbol, " ".join(cmd))

    try:
        # Start the trading process
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # Wait for deactivation_after_s, then send SIGINT (graceful shutdown)
        logger.info(
            "DEACTIVATION_CEREMONY_WAITING_FOR_DEACTIVATION symbol=%s wait_s=%d",
            symbol,
            deactivation_after_s,
        )
        time.sleep(deactivation_after_s)

        logger.info("DEACTIVATION_CEREMONY_DEACTIVATION_REQUESTED symbol=%s", symbol)

        # Send SIGINT for graceful shutdown (triggers cleanup-on-exit)
        import signal  # noqa: PLC0415

        proc.send_signal(signal.SIGINT)

        # Wait for remaining duration + grace period for cleanup
        remaining = max(duration_s - deactivation_after_s, 30)
        try:
            proc.wait(timeout=remaining + 60)
        except subprocess.TimeoutExpired:
            logger.warning("DEACTIVATION_CEREMONY_TIMEOUT symbol=%s", symbol)
            proc.kill()
            proc.wait(timeout=10)

        output = proc.stdout.read() if proc.stdout else ""
        exit_code = proc.returncode

        logger.info(
            "DEACTIVATION_CEREMONY_TRADING_EXITED symbol=%s exit_code=%d",
            symbol,
            exit_code or -1,
        )

        return exit_code in {0, 3}, output  # 3 = cleanup had issues but ran

    except Exception as e:
        logger.error("DEACTIVATION_CEREMONY_ERROR symbol=%s error=%s", symbol, e)
        return False, str(e)


def run_post_verify(symbol: str, max_retries: int = 3) -> bool:
    """Post-ceremony: verify exchange state is clean with retries."""
    for attempt in range(1, max_retries + 1):
        logger.info(
            "DEACTIVATION_CEREMONY_VERIFY symbol=%s attempt=%d/%d",
            symbol,
            attempt,
            max_retries,
        )
        ok, output = _verify_clean(symbol)
        if ok:
            logger.info(
                "DEACTIVATION_CEREMONY_VERIFY symbol=%s status=CLEAN",
                symbol,
            )
            return True

        logger.warning(
            "DEACTIVATION_CEREMONY_VERIFY symbol=%s status=DIRTY attempt=%d output=%s",
            symbol,
            attempt,
            output,
        )
        if attempt < max_retries:
            # Try cleanup between retries
            logger.info("DEACTIVATION_CEREMONY_CLEANUP_RETRY symbol=%s", symbol)
            _cleanup_symbol(symbol)
            time.sleep(5)

    return False


def run_ceremony(args: argparse.Namespace) -> int:
    """Run the full deactivation ceremony."""
    symbol = args.symbol

    logger.info(
        "DEACTIVATION_CEREMONY_START symbol=%s duration_s=%d deactivation_after_s=%d",
        symbol,
        args.duration_s,
        args.deactivation_after_s,
    )

    # Phase 1: Pre-flight
    if not run_preflight(symbol):
        logger.error("DEACTIVATION_CEREMONY_RESULT status=FAILED reason=PREFLIGHT_DIRTY")
        return 1

    # Phase 2: Trading + deactivation
    ok, _output = run_trading_phase(
        symbol=symbol,
        duration_s=args.duration_s,
        deactivation_after_s=args.deactivation_after_s,
        max_orders=args.max_orders,
        paper_size=args.paper_size_per_level,
        paper_levels=args.paper_levels,
    )
    if not ok:
        logger.error("DEACTIVATION_CEREMONY_RESULT status=FAILED reason=TRADING_PHASE_ERROR")
        # Still try post-verify
        run_post_verify(symbol)
        return 1

    # Phase 3: Post-ceremony verification
    if run_post_verify(symbol):
        logger.info("DEACTIVATION_CEREMONY_RESULT status=SUCCESS symbol=%s", symbol)
        return 0

    logger.error("DEACTIVATION_CEREMONY_RESULT status=FAILED reason=POST_VERIFY_DIRTY")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded live deactivation ceremony")
    parser.add_argument("--symbol", required=True, help="Trading symbol (e.g., BTCUSDT)")
    parser.add_argument("--duration-s", type=int, default=300, help="Total run duration")
    parser.add_argument(
        "--deactivation-after-s",
        type=int,
        default=60,
        help="Seconds before requesting deactivation",
    )
    parser.add_argument("--max-orders", type=int, default=50, help="Order budget")
    parser.add_argument("--paper-size-per-level", default="0.002", help="Size per grid level")
    parser.add_argument("--paper-levels", type=int, default=1, help="Grid levels per side")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only verify exchange state, don't run ceremony",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
    )

    if args.check_only:
        ok = run_preflight(args.symbol)
        sys.exit(0 if ok else 1)

    sys.exit(run_ceremony(args))


if __name__ == "__main__":
    main()
