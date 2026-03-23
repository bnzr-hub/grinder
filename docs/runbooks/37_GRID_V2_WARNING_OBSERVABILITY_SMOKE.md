# Runbook 37: Grid V2 Warning Observability Smoke

Purpose: prove that post-PR427 warning observability is live in runtime:
- warning-path counters are exposed in `/metrics`
- unknown user-data warnings include explicit `event_type`
- run still exits cleanly with cleanup-on-exit

---

## Profile used

- Symbol: `PIPPINUSDT`
- Port: futures mainnet (`--exchange-port futures --mainnet`)
- Mode: `live_trade`, armed
- Duration: `180s` (control smoke)
- Metrics: `--metrics-port 19092`

Key env:
- `GRINDER_GRID_V2_ENABLED=true`
- `GRINDER_GRID_V2_SYMBOL=PIPPINUSDT`
- `GRINDER_GRID_V2_ENTRY_LEVELS=5`
- `GRINDER_GRID_V2_STEP_PCT=0.0025`
- `GRINDER_GRID_V2_ORDER_SIZE=60`
- `GRINDER_GRID_V2_TICK_SIZE=0.0001`

---

## Proof (2026-03-23)

### 1) Metrics contain new counters

Command:

```bash
curl -sS http://127.0.0.1:19092/metrics | rg "grinder_live_grid_v2_integrity_mismatch_pending_total|grinder_live_grid_v2_rejected_fill_cleaned_total|grinder_user_data_unknown_events_total"
```

Observed lines:

```text
# HELP grinder_user_data_unknown_events_total Unknown user-data events by type
# TYPE grinder_user_data_unknown_events_total counter
grinder_user_data_unknown_events_total{event_type="none"} 0
# HELP grinder_live_grid_v2_integrity_mismatch_pending_total Transient grid_v2 integrity mismatches (streak below repair threshold)
# TYPE grinder_live_grid_v2_integrity_mismatch_pending_total counter
grinder_live_grid_v2_integrity_mismatch_pending_total{sym="none"} 0
# HELP grinder_live_grid_v2_rejected_fill_cleaned_total Rejected grid_v2 fills cleaned from adapter registry
# TYPE grinder_live_grid_v2_rejected_fill_cleaned_total counter
grinder_live_grid_v2_rejected_fill_cleaned_total{sym="none",source="none",reason="none"} 0
```

### 2) Unknown event warnings include explicit type

From run log (`/tmp/grinder_smoke_metrics_3m.log`):

```text
... WARNING grinder.execution.futures_events unknown_event_type event_type=TRADE_LITE
```

### 3) Run finishes clean

From run log (`/tmp/grinder_smoke_metrics_3m.log`):

```text
Duration (180s) reached after 2125 ticks.
Cleanup-on-exit completed successfully.
EXCHANGE_STATE_VERIFY symbol=PIPPINUSDT status=CLEAN orders=0 position=FLAT
```

---

## Interpretation

- Observability additions are active in production runtime.
- No trading-path behavior change detected in this smoke.
- New alert thresholds are now actionable via:
  - `GridV2IntegrityMismatchPendingBurst` (>5/15m)
  - `GridV2RejectedFillCleanedBurst` (>3/15m)
