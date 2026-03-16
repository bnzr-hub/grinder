# Runbook 34: Rolling Live Verification Ceremony

**Purpose:** Canonical procedure for live verification of the rolling infinite grid path.
Covers pre-flight, launch, evidence collection, expected blockers, cleanup, and acceptance.

**ADR:** ADR-089 (operational hardening for rolling live path).

**Operator tool:** `scripts/exchange_state.py` — pre-flight check, cleanup, verify.

---

## 1. Pre-flight

### 1a. Exchange state check

```bash
# Read-only: show open orders + position for target symbol
python3 -m scripts.exchange_state check BTCUSDT
```

Expected output for clean state:
```
EXCHANGE_STATE_CHECK symbol=BTCUSDT
  open_orders=0
  position: FLAT
  summary: orders=0 position=FLAT
```

If orders or position exist, run cleanup (section 5) before proceeding.

### 1b. Exchange state verify (hard gate)

```bash
# Assert 0 orders + flat position. Exit code 1 if not clean.
python3 -m scripts.exchange_state verify BTCUSDT
```

Expected:
```
EXCHANGE_STATE_VERIFY symbol=BTCUSDT status=CLEAN orders=0 position=FLAT
```

Do **not** proceed to launch if verify returns exit code 1.

### 1c. Environment

All env vars must be set before launch. Load from `.env` if present:

```bash
# .env loading is optional — vars can be exported manually instead.
# If using .env, source it explicitly:
set -a && source .env && set +a
```

Required env vars:

| Var | Value | Purpose |
|-----|-------|---------|
| `GRINDER_TRADING_MODE` | `live_trade` | Enable live trading |
| `GRINDER_TRADING_LOOP_ACK` | `YES_I_KNOW` | Safety ACK |
| `GRINDER_REAL_PORT_ACK` | `YES_I_REALLY_WANT_MAINNET` | Mainnet safety ACK |
| `GRINDER_MAX_ORDERS_ACK` | `YES_I_ACCEPT_MULTI_ORDER` | Multi-order safety ACK |
| `ALLOW_MAINNET_TRADE` | `1` | Connector mainnet guard |
| `GRINDER_LIVE_ROLLING_GRID` | `true` | Enable rolling grid mode |
| `GRINDER_LIVE_PLANNER_ENABLED` | `true` | Enable grid planner |
| `GRINDER_ACCOUNT_SYNC_ENABLED` | `true` | Enable account sync |
| `GRINDER_LIVE_CYCLE_ENABLED` | `true` | Enable TP cycle layer |
| `GRINDER_LOG_LEVEL` | `INFO` | Native logging (ADR-089) |
| `BINANCE_API_KEY` | `<key>` | Binance credentials |
| `BINANCE_API_SECRET` | `<secret>` | Binance credentials |

### 1d. Port availability

```bash
ss -tlnp | grep 9090 || echo "Port 9090 free"
```

---

## 2. Launch

### Canonical launch command

```bash
python3 -m scripts.run_trading \
  --exchange-port futures \
  --symbols BTCUSDT \
  --duration-s 300 \
  --metrics-port 9090 \
  --max-notional-per-order 200 \
  --max-orders-per-run 50 \
  --armed \
  --mainnet \
  --paper-size-per-level 0.002 \
  --paper-spacing-bps 10.0 \
  --paper-levels 1 \
  2>&1 | tee /tmp/grinder_live_run.log
```

With ADR-089 native logging, all INFO/WARNING/ERROR logs go to stderr.
`2>&1 | tee` captures both stdout (boot summary) and stderr (structured logs).
No external logging wrapper needed.

For DEBUG-level visibility: `export GRINDER_LOG_LEVEL=DEBUG` before launch.

---

## 3. Evidence Grep Pack

### Key events (run after engine stops)

```bash
LOG=/tmp/grinder_live_run.log

# Event counts
grep -c "ANCHOR_INIT"                 "$LOG" || echo "0"
grep -c "ANCHOR_RESET[^_]"           "$LOG" || echo "0"
grep -c "ANCHOR_RESET_BLOCKED"       "$LOG" || echo "0"
grep -c "ROLLING_FILL_OFFSET"        "$LOG" || echo "0"
grep -c "ROLLING_STEADY_STATE"       "$LOG" || echo "0"
grep -c "INFLIGHT_STALE_CLEARED"     "$LOG" || echo "0"
grep -c "INFLIGHT_GENERATION_TIMEOUT" "$LOG" || echo "0"
grep -c "GRID_SHIFT_DEFERRED"        "$LOG" || echo "0"
grep -c "CANCEL_SKIP_ALREADY_FAILED" "$LOG" || echo "0"

# Line-numbered key events
grep -n "ANCHOR_INIT\|ANCHOR_RESET\|ROLLING_FILL_OFFSET\|INFLIGHT_STALE_CLEARED\|ROLLING_STEADY_STATE" "$LOG"
```

### ADR-089 event notes

The three events added in ADR-089 are unit-test-proven (18 tests in `TestADR089LogEvents`).
They fire in the real live planner path under these conditions:

- `ROLLING_STEADY_STATE` (DEBUG): planner produces 0 actions in rolling mode. Requires converged grid + account sync active. Throttled 1/100 ticks.
- `INFLIGHT_STALE_CLEARED` (INFO): stale inflight latch cleared after sync refresh confirms convergence. Requires inflight set + subsequent account sync.
- `CANCEL_SKIP_ALREADY_FAILED` (DEBUG): cancel skipped for order_id with prior -2011 failure. Requires a Binance -2011 error followed by stale snapshot.

These events are **not reachable in fixture mode** (NoOp port, no account sync).
They are intended for live forensics and are exercised by the real exchange path.

### Contiguous windows

For forensic review, extract contiguous windows around key events:

```bash
# Around ANCHOR_RESET (±5 lines)
grep -n "ANCHOR_RESET[^_]" "$LOG" | while IFS=: read line _; do
  sed -n "$((line-5)),$((line+5))p" "$LOG"
  echo "---"
done
```

---

## 4. Blocked States / Expected Blockers

These are normal operational states, not errors:

| Log event | Meaning | Resolution |
|-----------|---------|------------|
| `ANCHOR_RESET_BLOCKED reason=POSITION_OPEN` | Position exists, cannot re-anchor | Position must close (TP fill or manual close) |
| `ANCHOR_RESET_BLOCKED reason=POSITION_UNKNOWN` | AccountSync unavailable | Wait for next sync cycle |
| `ANCHOR_RESET_BLOCKED reason=PENDING_CANCELS` | Cancel confirmations pending | Self-heals via 30s TTL |
| `GRID_SHIFT_DEFERRED reason=INFLIGHT_GENERATION` | Orders dispatched, waiting for sync confirmation | Self-heals on next account sync |
| `PLACEMENT_DEFERRED reason=ACCOUNT_SYNC_NOT_CONVERGED` | Extra orders detected, waiting for sync | Self-heals on next account sync |

---

## 5. Cleanup

### Canonical cleanup command

```bash
# Cancel all orders + close any open position for target symbol.
# Requires ALLOW_MAINNET_TRADE=1 (write operations).
python3 -m scripts.exchange_state cleanup BTCUSDT
```

Expected output:
```
EXCHANGE_CLEANUP symbol=BTCUSDT
  orders_before=<N>
  cancel_all_orders: done, orders_after=0
  close_position: skipped (FLAT)
  --- verify after cleanup ---
EXCHANGE_STATE_VERIFY symbol=BTCUSDT status=CLEAN orders=0 position=FLAT
```

Cleanup automatically runs verify after completing. If verify fails (exit 1),
investigate manually before proceeding.

### Post-cleanup verification (standalone)

```bash
python3 -m scripts.exchange_state verify BTCUSDT
```

### Process cleanup

```bash
# Verify no orphaned engine processes
pgrep -f "run_trading" || echo "No engine processes"

# Verify metrics port released
ss -tlnp | grep 9090 || echo "Port 9090 released"
```

### Expected final clean state

| Check | Expected | Command |
|-------|----------|---------|
| Open orders | 0 | `python3 -m scripts.exchange_state verify BTCUSDT` |
| Position | FLAT | (same command, exit code 0) |
| Engine process | Stopped | `pgrep -f "run_trading" \|\| echo "No engine processes"` |
| Metrics port | Released | `ss -tlnp \| grep 9090 \|\| echo "Port released"` |

---

## 6. Acceptance Criteria

A rolling live verification run is accepted when ALL of the following are proven:

### Minimum evidence

- [ ] `ANCHOR_INIT` fired (grid initialized from mid_price)
- [ ] Orders placed on exchange (POST order API calls in log)
- [ ] No `ERROR` lines (or all errors are explained/expected)
- [ ] Clean shutdown (exit code 0 or duration reached)
- [ ] Post-run: `python3 -m scripts.exchange_state verify BTCUSDT` → `status=CLEAN`

### Full acceptance (for ANCHOR_RESET verification)

All of the above, plus:

- [ ] `ANCHOR_RESET` fired with `reason=EXCHANGE_EMPTY_FLAT`
- [ ] Same-tick `ANCHOR_INIT` after reset (new anchor from current mid)
- [ ] Same-tick grid PLACEs from new anchor
- [ ] No `INFLIGHT_GENERATION_TIMEOUT` (inflight cleared via convergence, not timeout)
- [ ] `ANCHOR_RESET_BLOCKED` fired at least once (proves blocked path works)

### Fill path acceptance

- [ ] BUY fill → TP SELL placement observed
- [ ] SELL fill → TP BUY placement observed (if applicable to test)
- [ ] `ROLLING_FILL_OFFSET` logged with correct fill count and offset direction
