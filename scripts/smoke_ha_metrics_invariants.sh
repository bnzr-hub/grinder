#!/usr/bin/env bash
# GRINDER HA Dead-Redis Metrics Invariant Smoke (PR-OBS-3)
#
# Validates that with HA enabled + unreachable Redis:
# - /readyz returns HTTP 503
# - grinder_readyz_callback_registered = 1
# - grinder_readyz_ready = 0
# - Process still runs and shuts down cleanly
#
# Usage:
#   bash scripts/smoke_ha_metrics_invariants.sh
#
# Exit codes:
#   0 - All checks passed
#   1 - Assertion failure

set -euo pipefail

METRICS_PORT=9105
FIXTURE="/tmp/smoke_ha_fixture.jsonl"
LOG="/tmp/smoke_ha.log"
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

echo "=== GRINDER HA Dead-Redis Metrics Invariant Smoke ==="
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

# Start trading loop with HA enabled + dead Redis (port 6390 — nothing there)
PYTHONUNBUFFERED=1 \
GRINDER_HA_ENABLED=true \
GRINDER_REDIS_URL="redis://127.0.0.1:6390/0" \
python3 -m scripts.run_trading \
    --max-orders-per-run 1 \
    --symbols BTCUSDT \
    --fixture "$FIXTURE" \
    --duration-s 8 \
    --metrics-port "$METRICS_PORT" \
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

# Wait a bit more for loop_ready to be set
sleep 2

# Gate 1: /readyz returns HTTP 503
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${METRICS_PORT}/readyz")
if [[ "$HTTP_CODE" == "503" ]]; then
    pass_msg "/readyz HTTP status = 503 (not ready, HA standby)"
else
    fail_msg "/readyz HTTP status = $HTTP_CODE (expected 503)"
fi

# Gate 2: /readyz body shows ha_enabled=true, ha_role != active
READYZ_BODY=$(curl -fsS "http://localhost:${METRICS_PORT}/readyz" 2>/dev/null || curl -s "http://localhost:${METRICS_PORT}/readyz")
echo "  readyz body: $READYZ_BODY"
if echo "$READYZ_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['ha_enabled']==True" 2>/dev/null; then
    pass_msg "readyz body ha_enabled = true"
else
    fail_msg "readyz body ha_enabled != true"
fi

if echo "$READYZ_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['ready']==False" 2>/dev/null; then
    pass_msg "readyz body ready = false"
else
    fail_msg "readyz body ready != false"
fi

# Gate 3: /metrics contains grinder_readyz_callback_registered 1
METRICS=$(curl -fsS "http://localhost:${METRICS_PORT}/metrics")
CB_REG=$(echo "$METRICS" | grep "^grinder_readyz_callback_registered " | awk '{print $2}')
if [[ "$CB_REG" == "1" ]]; then
    pass_msg "grinder_readyz_callback_registered = 1"
else
    fail_msg "grinder_readyz_callback_registered = $CB_REG (expected 1)"
fi

# Gate 4: /metrics contains grinder_readyz_ready 0
READY_VAL=$(echo "$METRICS" | grep "^grinder_readyz_ready " | awk '{print $2}')
if [[ "$READY_VAL" == "0" ]]; then
    pass_msg "grinder_readyz_ready = 0"
else
    fail_msg "grinder_readyz_ready = $READY_VAL (expected 0)"
fi

# Wait for process to finish naturally (fixture exhaustion + duration)
echo ""
echo "Waiting for process to exit..."
wait "$PID" 2>/dev/null || true
PID=""

# Gate 5: clean shutdown
if grep -q "GRINDER TRADING LOOP stopped" "$LOG"; then
    pass_msg "Clean shutdown message present"
else
    fail_msg "Missing \"GRINDER TRADING LOOP stopped.\" in output"
fi

# Gate 6: zero "Task was destroyed"
COUNT_DESTROYED=$(grep -c "Task was destroyed" "$LOG" || true)
if [[ "$COUNT_DESTROYED" == "0" ]]; then
    pass_msg "\"Task was destroyed\" count = 0"
else
    fail_msg "\"Task was destroyed\" count = $COUNT_DESTROYED (expected 0)"
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
