#!/usr/bin/env bash
# GRINDER Clean Shutdown Smoke Test (PR-OBS-2)
#
# Validates that fixture-mode runs produce zero async warnings:
# - 0 "Task was destroyed but it is pending!"
# - 0 "Read timeout, attempting reconnect"
# - 0 "pending!"
#
# Usage:
#   bash scripts/smoke_no_task_destroyed.sh
#
# Exit codes:
#   0 - All checks passed (zero warnings)
#   1 - Assertion failure (warnings found)

set -euo pipefail

METRICS_PORT=9103
FIXTURE="/tmp/smoke_shutdown_fixture.jsonl"
LOG="/tmp/smoke_shutdown.log"
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
}
trap cleanup EXIT

echo "=== GRINDER Clean Shutdown Smoke Test ==="
echo ""

# Create minimal fixture (3 ticks)
cat > "$FIXTURE" <<'EOF'
{"s":"BTCUSDT","b":"97000.00","B":"1.5","a":"97001.00","A":"2.0"}
{"s":"BTCUSDT","b":"97002.00","B":"1.2","a":"97003.00","A":"1.8"}
{"s":"BTCUSDT","b":"97004.00","B":"1.0","a":"97005.00","A":"1.5"}
EOF

# Run trading loop with fixture + duration (synchronous — wait for exit)
python3 -m scripts.run_trading \
    --max-orders-per-run 1 \
    --symbols BTCUSDT \
    --fixture "$FIXTURE" \
    --duration-s 4 \
    --metrics-port "$METRICS_PORT" \
    > "$LOG" 2>&1 || true

echo "Process exited. Checking log for warnings..."
echo ""

# Gate 1: zero "Task was destroyed"
COUNT_DESTROYED=$(grep -c "Task was destroyed" "$LOG" || true)
if [[ "$COUNT_DESTROYED" == "0" ]]; then
    pass_msg "\"Task was destroyed\" count = 0"
else
    fail_msg "\"Task was destroyed\" count = $COUNT_DESTROYED (expected 0)"
    grep "Task was destroyed" "$LOG" || true
fi

# Gate 2: zero "pending!"
COUNT_PENDING=$(grep -c "pending!" "$LOG" || true)
if [[ "$COUNT_PENDING" == "0" ]]; then
    pass_msg "\"pending!\" count = 0"
else
    fail_msg "\"pending!\" count = $COUNT_PENDING (expected 0)"
    grep "pending!" "$LOG" || true
fi

# Gate 3: zero "Read timeout, attempting reconnect"
COUNT_TIMEOUT=$(grep -c "Read timeout" "$LOG" || true)
if [[ "$COUNT_TIMEOUT" == "0" ]]; then
    pass_msg "\"Read timeout\" count = 0"
else
    fail_msg "\"Read timeout\" count = $COUNT_TIMEOUT (expected 0)"
    grep "Read timeout" "$LOG" || true
fi

# Gate 4: process exited with "GRINDER TRADING LOOP stopped."
if grep -q "GRINDER TRADING LOOP stopped" "$LOG"; then
    pass_msg "Clean shutdown message present"
else
    fail_msg "Missing \"GRINDER TRADING LOOP stopped.\" in output"
fi

echo ""
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${GREEN}=== ALL CHECKS PASSED ===${NC}"
    exit 0
else
    echo -e "${RED}=== $FAILURES CHECK(S) FAILED ===${NC}"
    exit 1
fi
