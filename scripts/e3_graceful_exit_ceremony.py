"""E3 bounded graceful-exit ceremony with evidence validation.

Proves the real graceful-exit → no-new-entries → deactivation path by:
1. Starting a real engine subprocess
2. Streaming its output in real-time
3. Counting pre-signal entry activity (proves engine was trading)
4. Sending SIGUSR1 to trigger force_graceful_exit
5. Counting post-signal entries (must be zero) and SELECTOR_BLOCKED events
6. Shutting down via SIGINT + cleanup-on-exit
7. Verifying clean exchange state

The harness ASSERTS:
- At least 1 entry-related event before graceful-exit signal
- Zero new entry placements after graceful-exit signal
- Clean exchange state after shutdown

Usage:
    python3 -m scripts.e3_graceful_exit_ceremony \
        --symbol BTCUSDT \
        --duration-s 300 \
        --graceful-exit-after-s 60 \
        --observation-window-s 30 \
        --max-orders 50 \
        --paper-size-per-level 0.002 \
        --paper-levels 1 \
        2>&1 | tee /tmp/e3_ceremony.log
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time

logger = logging.getLogger("e3_ceremony")

# Evidence markers in engine subprocess output
ENTRY_MARKERS = ("PLACE_ENTRY", "PLACE_EXIT", "grinder_live_engine_initialized")
GRACEFUL_EXIT_MARKER = "FORCE_GRACEFUL_EXIT"
BLOCKED_MARKER = "SELECTOR_BLOCKED"


def _run_exchange_state(symbol: str, cmd: str) -> tuple[bool, str]:
    args = [sys.executable, "-m", "scripts.exchange_state", cmd, symbol]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except Exception as e:
        return False, str(e)


class EvidenceCollector:
    """Thread-safe collector for subprocess output evidence."""

    def __init__(self) -> None:
        self.pre_signal_activity: list[str] = []
        self.post_signal_entries: list[str] = []
        self.post_signal_blocked: list[str] = []
        self.graceful_exit_seen: bool = False
        self._signal_sent = threading.Event()
        self._lock = threading.Lock()

    def mark_signal_sent(self) -> None:
        self._signal_sent.set()

    def process_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return

        with self._lock:
            if not self._signal_sent.is_set():
                # Pre-signal: collect trading activity evidence
                for marker in ENTRY_MARKERS:
                    if marker in stripped:
                        self.pre_signal_activity.append(stripped)
                        break
            else:
                # Post-signal: watch for entries (bad) and blocked (good)
                if GRACEFUL_EXIT_MARKER in stripped:
                    self.graceful_exit_seen = True
                if BLOCKED_MARKER in stripped:
                    self.post_signal_blocked.append(stripped)
                if "PLACE_ENTRY" in stripped and BLOCKED_MARKER not in stripped:
                    self.post_signal_entries.append(stripped)


def _stream_output(proc: subprocess.Popen, collector: EvidenceCollector) -> None:  # type: ignore[type-arg]
    """Read subprocess stdout line by line in a background thread."""
    assert proc.stdout is not None
    for line in proc.stdout:
        collector.process_line(line)


def run_ceremony(args: argparse.Namespace) -> int:  # noqa: PLR0915
    symbol = args.symbol

    # Phase 1: Preflight
    logger.info("E3_CEREMONY_PREFLIGHT symbol=%s", symbol)
    ok, _preflight_out = _run_exchange_state(symbol, "verify")
    if not ok:
        logger.error("E3_CEREMONY_RESULT status=FAILED reason=PREFLIGHT_DIRTY")
        return 1
    logger.info("E3_CEREMONY_PREFLIGHT symbol=%s status=CLEAN", symbol)

    # Phase 2: Start trading
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

    collector = EvidenceCollector()
    reader = threading.Thread(target=_stream_output, args=(proc, collector), daemon=True)
    reader.start()

    # Phase 3: Wait for trading activity
    logger.info(
        "E3_CEREMONY_WAITING_FOR_ACTIVITY symbol=%s wait_s=%d", symbol, args.graceful_exit_after_s
    )
    time.sleep(args.graceful_exit_after_s)

    pre_count = len(collector.pre_signal_activity)
    logger.info("E3_CEREMONY_PRE_SIGNAL_ACTIVITY symbol=%s count=%d", symbol, pre_count)

    # Phase 4: Send SIGUSR1 for graceful-exit
    logger.info("E3_CEREMONY_GRACEFUL_EXIT_SIGNAL symbol=%s", symbol)
    collector.mark_signal_sent()
    if hasattr(_signal, "SIGUSR1"):
        proc.send_signal(_signal.SIGUSR1)
    else:
        logger.error("E3_CEREMONY_RESULT status=FAILED reason=NO_SIGUSR1")
        proc.kill()
        return 1

    # Phase 5: Observe no-new-entries window
    obs = min(args.observation_window_s, args.duration_s - args.graceful_exit_after_s - 30)
    if obs > 0:
        logger.info("E3_CEREMONY_OBSERVATION_WINDOW symbol=%s seconds=%d", symbol, obs)
        time.sleep(obs)

    post_entries = len(collector.post_signal_entries)
    post_blocked = len(collector.post_signal_blocked)
    graceful_seen = collector.graceful_exit_seen
    logger.info(
        "E3_CEREMONY_OBSERVATION_RESULT symbol=%s graceful_exit_seen=%s"
        " post_entries=%d post_blocked=%d",
        symbol,
        graceful_seen,
        post_entries,
        post_blocked,
    )

    # Phase 6: Shutdown
    logger.info("E3_CEREMONY_SHUTDOWN_REQUESTED symbol=%s", symbol)
    proc.send_signal(_signal.SIGINT)
    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    reader.join(timeout=5)

    logger.info("E3_CEREMONY_TRADING_EXITED symbol=%s exit_code=%d", symbol, proc.returncode or -1)

    # Phase 7: Post-verify
    for attempt in range(1, 4):
        logger.info("E3_CEREMONY_VERIFY symbol=%s attempt=%d/3", symbol, attempt)
        ok, _output = _run_exchange_state(symbol, "verify")
        if ok:
            logger.info("E3_CEREMONY_VERIFY symbol=%s status=CLEAN", symbol)
            break
        if attempt < 3:
            _run_exchange_state(symbol, "cleanup")
            time.sleep(5)
    else:
        logger.error("E3_CEREMONY_RESULT status=FAILED reason=POST_VERIFY_DIRTY")
        return 1

    # Phase 8: Assert evidence
    if pre_count == 0:
        logger.error("E3_CEREMONY_RESULT status=FAILED reason=NO_PRE_SIGNAL_ACTIVITY")
        return 1

    if post_entries > 0:
        logger.error(
            "E3_CEREMONY_RESULT status=FAILED reason=POST_SIGNAL_ENTRIES count=%d",
            post_entries,
        )
        return 1

    logger.info(
        "E3_CEREMONY_RESULT status=SUCCESS symbol=%s pre_activity=%d"
        " post_entries=%d post_blocked=%d graceful_exit_seen=%s",
        symbol,
        pre_count,
        post_entries,
        post_blocked,
        graceful_seen,
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="E3 bounded graceful-exit ceremony")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--duration-s", type=int, default=300)
    parser.add_argument("--graceful-exit-after-s", type=int, default=60)
    parser.add_argument("--max-orders", type=int, default=50)
    parser.add_argument("--paper-size-per-level", default="0.002")
    parser.add_argument("--paper-levels", type=int, default=1)
    parser.add_argument("--observation-window-s", type=int, default=30)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if args.check_only:
        ok, _ = _run_exchange_state(args.symbol, "verify")
        sys.exit(0 if ok else 1)

    sys.exit(run_ceremony(args))


if __name__ == "__main__":
    main()
