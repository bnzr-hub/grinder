#!/usr/bin/env bash
# smoke_grid_v2_shadow.sh — Smoke proof for grid_v2 shadow verification (doc-27 PR5).
#
# Validates that shadow mode:
#   A) grid_v2 OFF baseline: no shadow runner created
#   B) grid_v2 shadow ON + primary OFF: shadow runs, 0 executed writes
#   C) grid_v2 primary ON + shadow ON: mutual exclusion, shadow not created
#   D) Shadow tests pass with -k "shadow"
#
# Usage:
#   bash scripts/smoke_grid_v2_shadow.sh
#
# Saves artifacts to .artifacts/grid_v2_shadow/<ts>/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

TS=$(date +%Y%m%dT%H%M%S)
ARTIFACT_DIR=".artifacts/grid_v2_shadow/${TS}"
mkdir -p "$ARTIFACT_DIR"

PASS=0
FAIL=0

pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); }

echo "=== Grid V2 Shadow Verification Smoke Test ==="
echo "Artifact dir: $ARTIFACT_DIR"
echo ""

# --- Gate A: shadow tests pass ---
echo "Gate A: Running shadow-specific pytest..."
if PYTHONPATH=src python3 -m pytest tests/unit/test_grid_v2_shadow.py -vv \
    --tb=short 2>&1 | tee "$ARTIFACT_DIR/gate_a_shadow_tests.txt" | tail -3; then
    pass "Gate A: all shadow tests pass"
else
    fail "Gate A: shadow tests failed"
fi
echo ""

# --- Gate B: full grid_v2 test suite ---
echo "Gate B: Running full grid_v2 test suite..."
if PYTHONPATH=src python3 -m pytest tests/unit/test_grid_v2_bridge.py tests/unit/test_grid_v2_shadow.py -q \
    2>&1 | tee "$ARTIFACT_DIR/gate_b_full_grid_v2.txt" | tail -3; then
    pass "Gate B: full grid_v2 suite passes"
else
    fail "Gate B: full grid_v2 suite failed"
fi
echo ""

# --- Gate C: ruff check ---
echo "Gate C: Ruff lint check..."
if python3 -m ruff check src/grinder/grid_v2/shadow.py src/grinder/live/engine.py \
    2>&1 | tee "$ARTIFACT_DIR/gate_c_ruff.txt"; then
    pass "Gate C: ruff clean"
else
    fail "Gate C: ruff lint errors"
fi
echo ""

# --- Gate D: ruff format ---
echo "Gate D: Ruff format check..."
if python3 -m ruff format --check src/grinder/grid_v2/shadow.py src/grinder/live/engine.py \
    2>&1 | tee "$ARTIFACT_DIR/gate_d_format.txt"; then
    pass "Gate D: format clean"
else
    fail "Gate D: format errors"
fi
echo ""

# --- Gate E: mypy on shadow module ---
echo "Gate E: mypy on shadow module..."
if python3 -m mypy src/grinder/grid_v2/shadow.py --no-error-summary \
    2>&1 | tee "$ARTIFACT_DIR/gate_e_mypy.txt"; then
    pass "Gate E: mypy clean"
else
    fail "Gate E: mypy errors"
fi
echo ""

# --- Summary ---
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
echo "Artifacts: $ARTIFACT_DIR/"

# Generate sha256sums
(cd "$ARTIFACT_DIR" && sha256sum ./* > sha256sums.txt 2>/dev/null || true)

# Write summary
cat > "$ARTIFACT_DIR/summary.txt" <<EOF
Grid V2 Shadow Verification Smoke Test
Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Commit: $(git rev-parse HEAD 2>/dev/null || echo "unknown")
Passed: $PASS
Failed: $FAIL
EOF

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
