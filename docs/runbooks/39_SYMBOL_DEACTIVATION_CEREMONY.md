# Runbook 39: Bounded Shutdown Cleanup Ceremony

**Purpose:** Bounded live proof that trading + graceful shutdown + cleanup-on-exit reaches clean exchange state. Proves the shutdown/cleanup path — full graceful_exit_only deactivation proof is a future iteration.
**ADR:** ADR-130 (PR-C2b)
**SSOT:** `scripts/deactivation_ceremony.py`

---

## Prerequisites

1. Binance API keys configured (`BINANCE_API_KEY`, `BINANCE_API_SECRET`)
2. `ALLOW_MAINNET_TRADE=1`
3. `GRINDER_TRADING_MODE=live_trade`
4. `GRINDER_TRADING_LOOP_ACK=YES_I_KNOW`
5. `GRINDER_REAL_PORT_ACK=YES_I_REALLY_WANT_MAINNET`
6. `GRINDER_MAX_ORDERS_ACK=YES_I_ACCEPT_MULTI_ORDER`
7. Target symbol has sufficient liquidity and notional
8. Exchange state is clean before ceremony start

---

## Quick Check (Pre-flight Only)

```bash
python3 -m scripts.deactivation_ceremony --symbol BTCUSDT --check-only
```

Expected: `DEACTIVATION_CEREMONY_PREFLIGHT symbol=BTCUSDT status=CLEAN`

---

## Full Ceremony

### Step 1: Launch

```bash
python3 -m scripts.deactivation_ceremony \
    --symbol BTCUSDT \
    --duration-s 300 \
    --deactivation-after-s 60 \
    --max-orders 50 \
    --paper-size-per-level 0.002 \
    --paper-levels 1 \
    2>&1 | tee /tmp/deactivation_ceremony.log
```

### Step 2: What Happens

| Phase | Duration | What |
|-------|----------|------|
| Pre-flight | ~5s | Verify exchange state is clean |
| Trading | 60s | Normal grid trading |
| Shutdown | Remaining | SIGINT → graceful shutdown → cleanup-on-exit |
| Post-verify | ~15s | Verify clean state (with retries + cleanup fallback) |

### Step 3: Success Criteria

```
DEACTIVATION_CEREMONY_RESULT status=SUCCESS symbol=BTCUSDT
```

Post-verify must show:
```
EXCHANGE_STATE_VERIFY symbol=BTCUSDT status=CLEAN orders=0 position=FLAT
```

### Step 4: Evidence Extraction

```bash
# Key signals
grep "DEACTIVATION_CEREMONY_" /tmp/deactivation_ceremony.log

# Final result
grep "DEACTIVATION_CEREMONY_RESULT" /tmp/deactivation_ceremony.log

# Exchange state
python3 -m scripts.exchange_state verify BTCUSDT
```

---

## Failure Handling

| Failure | Action |
|---------|--------|
| `PREFLIGHT_DIRTY` | Run `exchange_state cleanup BTCUSDT`, then retry |
| `TRADING_PHASE_ERROR` | Check logs for exchange errors; verify state manually |
| `POST_VERIFY_DIRTY` | Script attempts cleanup + retry (3 attempts). If still dirty: manual `exchange_state cleanup` |
| `TIMEOUT` | Trading process killed; verify exchange state manually |

---

## Manual Verification

If ceremony fails or for independent verification:

```bash
# Check orders + position
python3 -m scripts.exchange_state check BTCUSDT

# Hard verify (exit code 0 = clean)
python3 -m scripts.exchange_state verify BTCUSDT

# Force cleanup
python3 -m scripts.exchange_state cleanup BTCUSDT
```

---

## Stop Conditions

- **STOP if** exchange state remains dirty after 3 cleanup attempts
- **STOP if** position cannot be closed (liquidity issue, exchange error)
- **STOP if** unexpected error codes in trading logs
- **DO NOT** retry the full ceremony without first verifying clean state

---

## Evidence Checklist (for PR proof)

- [ ] Pre-flight: `status=CLEAN`
- [ ] Trading phase: at least 1 order placed (visible in logs)
- [ ] Deactivation requested: `DEACTIVATION_CEREMONY_DEACTIVATION_REQUESTED`
- [ ] Cleanup-on-exit ran: visible in trading logs
- [ ] Post-verify: `status=CLEAN`
- [ ] Final result: `DEACTIVATION_CEREMONY_RESULT status=SUCCESS`
- [ ] Manual `exchange_state verify` confirms clean state

---

## Proof Record: 2026-04-01 (BTCUSDT @ `611d520`)

**Result:** SUCCESS

```
DEACTIVATION_CEREMONY_PREFLIGHT symbol=BTCUSDT status=CLEAN
DEACTIVATION_CEREMONY_TRADING_ACTIVE symbol=BTCUSDT
DEACTIVATION_CEREMONY_WAITING_FOR_DEACTIVATION symbol=BTCUSDT wait_s=60
DEACTIVATION_CEREMONY_DEACTIVATION_REQUESTED symbol=BTCUSDT
DEACTIVATION_CEREMONY_TRADING_EXITED symbol=BTCUSDT exit_code=-1
DEACTIVATION_CEREMONY_VERIFY symbol=BTCUSDT status=CLEAN
DEACTIVATION_CEREMONY_RESULT status=SUCCESS symbol=BTCUSDT
```

**Final exchange state:**
```
EXCHANGE_STATE_VERIFY symbol=BTCUSDT status=CLEAN orders=0 position=FLAT
```

**Notes:** Trading exit code -1 is expected (SIGINT). No cleanup retries needed. No anomalies.
