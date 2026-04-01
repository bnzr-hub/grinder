# Runbook 41: E3 Graceful-Exit Proof Ceremony

**Purpose:** Prove that graceful-exit signal blocks new entries on a real running engine, and that the full deactivation path reaches clean exchange state.

**SSOT:** `scripts/e3_graceful_exit_ceremony.py`

---

## Prerequisites

1. Binance API keys (`BINANCE_API_KEY`, `BINANCE_API_SECRET`)
2. `ALLOW_MAINNET_TRADE=1`
3. `GRINDER_TRADING_MODE=live_trade`
4. `GRINDER_TRADING_LOOP_ACK=YES_I_KNOW`
5. `GRINDER_REAL_PORT_ACK=YES_I_REALLY_WANT_MAINNET`
6. `GRINDER_MAX_ORDERS_ACK=YES_I_ACCEPT_MULTI_ORDER`
7. `GRINDER_SYMBOL_SELECTOR_ENABLED=1` (required for dispatch gate)
8. Exchange state clean for target symbol

---

## Ceremony Sequence

| Phase | What | Signal |
|-------|------|--------|
| Preflight | Verify exchange clean | `E3_CEREMONY_PREFLIGHT status=CLEAN` |
| Trading | Bounded run, entries placed | `E3_CEREMONY_TRADING_ACTIVE` |
| Graceful exit | SIGUSR1 → `force_graceful_exit()` | `FORCE_GRACEFUL_EXIT symbol=X` |
| Observation | No new entries in window | `E3_CEREMONY_OBSERVATION_COMPLETE` |
| Shutdown | SIGINT → cleanup-on-exit | `E3_CEREMONY_SHUTDOWN_REQUESTED` |
| Verify | Clean exchange state | `E3_CEREMONY_VERIFY status=CLEAN` |

---

## Commands

### Quick check
```bash
python3 -m scripts.e3_graceful_exit_ceremony --symbol BTCUSDT --check-only
```

### Full ceremony
```bash
python3 -m scripts.e3_graceful_exit_ceremony \
    --symbol BTCUSDT \
    --duration-s 300 \
    --graceful-exit-after-s 60 \
    --observation-window-s 30 \
    --max-orders 50 \
    --paper-size-per-level 0.002 \
    --paper-levels 1 \
    2>&1 | tee /tmp/e3_ceremony.log
```

### Evidence extraction
```bash
grep "E3_CEREMONY_\|FORCE_GRACEFUL_EXIT\|SELECTOR_BLOCKED" /tmp/e3_ceremony.log
python3 -m scripts.exchange_state verify BTCUSDT
python3 -m scripts.exchange_state check BTCUSDT
```

---

## Success Criteria

1. Preflight: `status=CLEAN`
2. Trading active: engine placed at least 1 order before graceful-exit
3. Graceful exit: `FORCE_GRACEFUL_EXIT symbol=BTCUSDT` visible in subprocess output
4. No new entries: `SELECTOR_BLOCKED` entries visible after graceful-exit signal (or zero new entry placements in observation window)
5. Post-verify: `status=CLEAN orders=0 position=FLAT`
6. Final result: `E3_CEREMONY_RESULT status=SUCCESS`

---

## What counts as "no new entries" evidence

After `FORCE_GRACEFUL_EXIT`:
- Any `SELECTOR_BLOCKED` log lines confirm the gate is active
- Zero new `PLACE_ENTRY` actions after the graceful-exit timestamp
- Existing exits, cancels, and grid_v2 internal actions may continue (that's correct behavior)

---

## Failure Handling

| Failure | Action |
|---------|--------|
| Preflight dirty | `exchange_state cleanup`, retry |
| No SIGUSR1 (Windows) | Ceremony not supported on this platform |
| Post-verify dirty | Script retries cleanup 3x, then FAILED |
| Timeout | Process killed, verify manually |
