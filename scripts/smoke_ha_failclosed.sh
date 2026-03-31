#!/usr/bin/env bash
# GRINDER HA Fail-Closed Smoke Test
#
# Validates that when HA is enabled but Redis is unreachable:
# - /readyz returns HTTP 503 (not 200)
# - Response body shows ha_enabled=true, ready=false, ha_role != "active"
#
# Usage:
#   bash scripts/smoke_ha_failclosed.sh
#
# Exit codes:
#   0 - All checks passed
#   1 - Assertion failure

set -euo pipefail

METRICS_PORT=9098
FIXTURE="/tmp/smoke_ha_fixture.jsonl"
DEAD_REDIS="redis://127.0.0.1:6390/0"
PID=""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { echo -e "${GREEN}PASS${NC}: $1"; }
fail() { echo -e "${RED}FAIL${NC}: $1"; FAILURES=$((FAILURES + 1)); }

FAILURES=0

cleanup() {
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null
        wait "$PID" 2>/dev/null || true
    fi
    rm -f "$FIXTURE" /tmp/smoke_ha_readyz.json
}
trap cleanup EXIT

echo "=== GRINDER HA Fail-Closed Smoke Test ==="
echo ""

# Create minimal fixture
cat > "$FIXTURE" <<'EOF'
{"s":"BTCUSDT","b":"97000.00","B":"1.5","a":"97001.00","A":"2.0"}
{"s":"BTCUSDT","b":"97002.00","B":"1.2","a":"97003.00","A":"1.8"}
{"s":"BTCUSDT","b":"97004.00","B":"1.0","a":"97005.00","A":"1.5"}
EOF

# Start trading loop with HA enabled + dead Redis
GRINDER_HA_ENABLED=1 \
GRINDER_REDIS_URL="$DEAD_REDIS" \
python3 -m scripts.run_trading \
    --max-orders-per-run 1 \
    --symbols BTCUSDT \
    --fixture "$FIXTURE" \
    --duration-s 10 \
    --metrics-port "$METRICS_PORT" \
    > /tmp/smoke_ha_stdout.txt 2>&1 &
PID=$!

echo "Started PID=$PID (HA=1, Redis=$DEAD_REDIS, port=$METRICS_PORT)"
echo "Waiting 4s for startup..."
sleep 4

# Gate 1: /readyz HTTP code must be 503
READYZ_CODE=$(curl -s -o /tmp/smoke_ha_readyz.json -w "%{http_code}" "http://localhost:${METRICS_PORT}/readyz" 2>/dev/null || echo "000")

if [[ "$READYZ_CODE" == "503" ]]; then
    pass "/readyz HTTP code = 503"
else
    fail "/readyz HTTP code = $READYZ_CODE (expected 503)"
fi

# Gate 2: body assertions
if [[ -f /tmp/smoke_ha_readyz.json ]]; then
    BODY=$(cat /tmp/smoke_ha_readyz.json)
    echo "  Body: $BODY"

    if echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['ha_enabled']==True" 2>/dev/null; then
        pass "ha_enabled = true"
    else
        fail "ha_enabled != true"
    fi

    if echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['ready']==False" 2>/dev/null; then
        pass "ready = false"
    else
        fail "ready != false"
    fi

    if echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['ha_role']!='active'" 2>/dev/null; then
        HA_ROLE=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['ha_role'])" 2>/dev/null || echo "?")
        pass "ha_role = $HA_ROLE (not active)"
    else
        fail "ha_role = active (expected non-active)"
    fi
else
    fail "/readyz body not received"
fi

# Cleanup
kill "$PID" 2>/dev/null
wait "$PID" 2>/dev/null || true
PID=""

echo ""
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${GREEN}=== ALL CHECKS PASSED ===${NC}"
    exit 0
else
    echo -e "${RED}=== $FAILURES CHECK(S) FAILED ===${NC}"
    exit 1
fi
