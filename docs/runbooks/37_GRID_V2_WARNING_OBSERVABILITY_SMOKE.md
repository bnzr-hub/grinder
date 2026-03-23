# Runbook 37: Grid V2 Warning Observability Smoke

Purpose: prove that warning observability for Grid V2 is live in runtime:
- warning counters are exposed in `/metrics`
- unknown user-data warnings include explicit `event_type`
- live run still exits cleanly

Scope:
- Observability-only validation (no strategy mutation)
- control-window evidence capture

---

## Profile used

- Symbol: `PIPPINUSDT`
- Port: futures mainnet (`--exchange-port futures --mainnet`)
- Mode: `live_trade`, armed
- Metrics: `--metrics-port 19092`

Key env:
- `GRINDER_GRID_V2_ENABLED=true`
- `GRINDER_GRID_V2_SYMBOL=PIPPINUSDT`
- `GRINDER_GRID_V2_ENTRY_LEVELS=5`
- `GRINDER_GRID_V2_STEP_PCT=0.0025`
- `GRINDER_GRID_V2_ORDER_SIZE=60`
- `GRINDER_GRID_V2_TICK_SIZE=0.0001`

---

## Runtime Proof A (2026-03-23, 3-minute smoke)

Artifact:
- `/tmp/grinder_smoke_metrics_3m.log`

### 1) Metrics contain new counters

```bash
curl -sS http://127.0.0.1:19092/metrics | rg "grinder_live_grid_v2_integrity_mismatch_pending_total|grinder_live_grid_v2_rejected_fill_cleaned_total|grinder_user_data_unknown_events_total"
```

Expected metrics families:
- `grinder_user_data_unknown_events_total{event_type=...}`
- `grinder_live_grid_v2_integrity_mismatch_pending_total{sym=...}`
- `grinder_live_grid_v2_rejected_fill_cleaned_total{sym=...,source=...,reason=...}`

### 2) Unknown event warning includes explicit type

Example line from log:

```text
... WARNING grinder.execution.futures_events unknown_event_type event_type=TRADE_LITE
```

### 3) Run finishes clean

Expected end markers in log:
- `Duration (180s) reached ...`
- `Cleanup-on-exit completed successfully`
- `EXCHANGE_STATE_VERIFY ... status=CLEAN orders=0 position=FLAT`

---

## Runtime Proof B (2026-03-23, 15-minute control window) — PASS

Artifact:
- `/tmp/grinder_stable_window_15m.log`

Observed:
- Duration completed by timer (`900s`) with cleanup-on-exit success.
- End state: `EXCHANGE_STATE_VERIFY symbol=PIPPINUSDT status=CLEAN orders=0 position=FLAT`.
- Warning/error event counts from log:
  - `unknown_event_type event_type=...`: `9`
  - `GRID_V2_INTEGRITY_MISMATCH_PENDING`: `0`
  - `GRID_V2_REJECTED_FILL_CLEANED`: `0`
  - `GRID_V2_EXIT_FILL_ORPHAN`: `0`
  - `GRID_V2_INTEGRITY_REPAIR_TRIGGER`: `0`
  - `GRID_V2_INTEGRITY_FATAL`: `0`
  - `Traceback`: `0`

Interpretation:
- Warning observability contract is healthy for this control window.
- Alert thresholds were not breached:
  - `GridV2IntegrityMismatchPendingBurst` (>5/15m)
  - `GridV2RejectedFillCleanedBurst` (>3/15m)

---

## Runtime Proof C (2026-03-23, 15-minute live control window) — PASS

Artifact:
- `/tmp/grinder_control_15m.log`

Observed:
- Duration completed by timer (`900s`) after `13890` ticks.
- End state: `EXCHANGE_STATE_VERIFY symbol=PIPPINUSDT status=CLEAN orders=0 position=FLAT`.
- Cleanup-on-exit completed successfully.
- Warning/event counts from log:
  - `GRID_V2_FILL_PROCESSED`: `10`
  - `unknown_event_type event_type=...`: `10` (all classified as `TRADE_LITE`)
  - `GRID_V2_INTEGRITY_MISMATCH_PENDING`: `1` (transient)
  - `GRID_V2_REJECTED_FILL_CLEANED`: `1` (self-healed)
  - `GRID_V2_EXIT_FILL_ORPHAN`: `0`
  - `GRID_V2_INTEGRITY_REPAIR_TRIGGER`: `0`
  - `GRID_V2_INTEGRITY_FATAL`: `0`
  - `Traceback`: `0`

Interpretation:
- Trading path remained stable under active fills.
- One transient mismatch and one rejected-fill cleanup were observed but remained below alert burst thresholds.
- No fatal integrity path and no orphan exits in this window.

---

## Exit Criteria

Smoke PASS when all true:

1. Both Grid V2 warning counters are present in `/metrics`.
2. Unknown user-data warnings include `event_type`.
3. No integrity/reject/orphan bursts in control window.
4. Run ends clean (`orders=0`, `position=FLAT`).
