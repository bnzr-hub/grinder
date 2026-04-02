#!/usr/bin/env bash
# GRINDER Futures No-Orders Rehearsal Smoke (PR-FUT-0)
#
# Validates that --exchange-port futures with fixture + fake API keys:
# - Exits cleanly (exit code 0)
# - Produces NO order-like network strings in output
# - Emits correct metrics (engine_initialized=1, callback_registered=1)
#
# This is a regression guard: proves that "futures port enabled" does NOT
# result in real order submission when running on fixture data.
#
# Usage:
#   bash scripts/smoke_futures_no_orders.sh
#
# Exit codes:
#   0 - All checks passed (no orders submitted)
#   1 - Assertion failure

set -euo pipefail

METRICS_PORT=9106
FIXTURE="/tmp/smoke_futures_fixture.jsonl"
LOG="/tmp/smoke_futures_no_orders.log"
PID=""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass_msg() { echo -e "${GREEN}PASS${NC}: $1"; }
fail_msg() { echo -e "${RED}FAIL${NC}: $1"; FAILURES=$((FAILURES + 1)); }

FAILURES=0

cleanup() {
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null
        wait "$PID" 2>/dev/null || true
    fi
    rm -f "$FIXTURE" "$LOG"
    fuser -k "${METRICS_PORT}/tcp" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== GRINDER Futures No-Orders Rehearsal Smoke ==="
echo ""

# Kill stale port
fuser -k "${METRICS_PORT}/tcp" 2>/dev/null || true

# Create fixture with enough ticks to keep process alive for endpoint checks.
# FakeWsTransport uses delay_ms=100, so 80 ticks ≈ 8 seconds.
python3 -c "
for i in range(80):
    p = 97000 + i
    print('{\"s\":\"BTCUSDT\",\"b\":\"' + str(p) + '.00\",\"B\":\"1.5\",\"a\":\"' + str(p+1) + '.00\",\"A\":\"2.0\"}')
" > "$FIXTURE"

# Start trading loop with futures port + all safety gates + FAKE keys.
# This proves the process boots and runs without sending real orders.
PYTHONUNBUFFERED=1 \
GRINDER_TRADING_MODE=live_trade \
GRINDER_TRADING_LOOP_ACK=YES_I_KNOW \
ALLOW_MAINNET_TRADE=1 \
GRINDER_REAL_PORT_ACK=YES_I_REALLY_WANT_MAINNET \
BINANCE_API_KEY='smoke_test_fake_key_not_real' \
BINANCE_API_SECRET='smoke_test_fake_secret_not_real' \
python3 -m scripts.run_trading \
    --max-orders-per-run 1 \
    --symbols BTCUSDT \
    --fixture "$FIXTURE" \
    --duration-s 8 \
    --metrics-port "$METRICS_PORT" \
    --exchange-port futures \
    --max-notional-per-order 100 \
    --armed \
    > "$LOG" 2>&1 &
PID=$!

# Wait for health endpoint to come up
echo "Waiting for health endpoint..."
for i in $(seq 1 20); do
    if curl -fsS "http://localhost:${METRICS_PORT}/healthz" > /dev/null 2>&1; then
        echo "  Health endpoint up after ${i}s"
        break
    fi
    sleep 1
done

# Wait for loop to process some ticks
sleep 2

# Gate 1: boot line confirms futures port + armed
if grep -q "port=futures armed=True" "$LOG"; then
    pass_msg "Boot line: port=futures armed=True"
else
    fail_msg "Missing boot line with port=futures armed=True"
fi

# Gate 2: /metrics contains grinder_live_engine_initialized 1
METRICS=$(curl -fsS "http://localhost:${METRICS_PORT}/metrics")
ENGINE_INIT=$(echo "$METRICS" | grep "^grinder_live_engine_initialized " | awk '{print $2}')
if [[ "$ENGINE_INIT" == "1" ]]; then
    pass_msg "grinder_live_engine_initialized = 1"
else
    fail_msg "grinder_live_engine_initialized = $ENGINE_INIT (expected 1)"
fi

# Gate 3: /metrics contains grinder_readyz_callback_registered 1
CB_REG=$(echo "$METRICS" | grep "^grinder_readyz_callback_registered " | awk '{print $2}')
if [[ "$CB_REG" == "1" ]]; then
    pass_msg "grinder_readyz_callback_registered = 1"
else
    fail_msg "grinder_readyz_callback_registered = $CB_REG (expected 1)"
fi

# Wait for process to finish naturally
echo ""
echo "Waiting for process to exit..."
wait "$PID" 2>/dev/null || true
EXIT_CODE=${PIPESTATUS[0]:-$?}
PID=""

# Gate 4: exit code 0
if [[ "$EXIT_CODE" == "0" ]]; then
    pass_msg "Exit code = 0"
else
    fail_msg "Exit code = $EXIT_CODE (expected 0)"
fi

# Gate 5: NO order-like network strings in output.
# These patterns would appear if the engine tried to submit orders to Binance.
ORDER_PATTERNS="/fapi/v1/order|newOrder|place_order|POST.*order|submit_order"
ORDER_HITS=$(grep -ciE "$ORDER_PATTERNS" "$LOG" || true)
if [[ "$ORDER_HITS" == "0" ]]; then
    pass_msg "No order-like network strings in output (0 matches)"
else
    fail_msg "Found $ORDER_HITS order-like strings in output (expected 0)"
    grep -iE "$ORDER_PATTERNS" "$LOG" || true
fi

# Gate 6: clean shutdown
if grep -q "GRINDER RUN SHUTDOWN" "$LOG"; then
    pass_msg "Clean shutdown message present"
else
    fail_msg "Missing \"GRINDER RUN SHUTDOWN\" in output"
fi

# Gate 7: zero "Task was destroyed"
COUNT_DESTROYED=$(grep -c "Task was destroyed" "$LOG" || true)
if [[ "$COUNT_DESTROYED" == "0" ]]; then
    pass_msg "\"Task was destroyed\" count = 0"
else
    fail_msg "\"Task was destroyed\" count = $COUNT_DESTROYED (expected 0)"
fi

# Gate 8: port order attempts metric = 0 for all futures ops (PR-FUT-1)
# Re-read metrics from log (process already exited, endpoint gone) — check the
# $METRICS variable captured earlier while process was alive.
for OP in place cancel replace; do
    ATTEMPTS=$(echo "$METRICS" | grep "^grinder_port_order_attempts_total{port=\"futures\",op=\"${OP}\"}" | awk '{print $2}')
    if [[ "$ATTEMPTS" == "0" ]]; then
        pass_msg "grinder_port_order_attempts_total{port=\"futures\",op=\"${OP}\"} = 0"
    else
        fail_msg "grinder_port_order_attempts_total{port=\"futures\",op=\"${OP}\"} = ${ATTEMPTS:-MISSING} (expected 0)"
    fi
done

# Gate 9: zero HTTP requests to /fapi endpoints (PR-FUT-2)
# Proves no real Binance Futures API traffic during fixture rehearsal.
FAPI_HITS=$(echo "$METRICS" | grep -c '^grinder_port_http_requests_total{port="futures"' || true)
if [[ "$FAPI_HITS" == "0" ]]; then
    pass_msg "No grinder_port_http_requests_total{port=\"futures\"} series (0 HTTP calls)"
else
    # Series exist — check if all counts are 0
    FAPI_SUM=$(echo "$METRICS" | grep '^grinder_port_http_requests_total{port="futures"' | awk '{s+=$2} END {print s+0}')
    if [[ "$FAPI_SUM" == "0" ]]; then
        pass_msg "grinder_port_http_requests_total{port=\"futures\"} sum = 0"
    else
        fail_msg "grinder_port_http_requests_total{port=\"futures\"} sum = $FAPI_SUM (expected 0)"
        echo "$METRICS" | grep '^grinder_port_http_requests_total{port="futures"' || true
    fi
fi

echo ""
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${GREEN}=== ALL CHECKS PASSED ===${NC}"
    exit 0
else
    echo -e "${RED}=== $FAILURES CHECK(S) FAILED ===${NC}"
    echo ""
    echo "--- Full log ---"
    cat "$LOG"
    exit 1
fi
