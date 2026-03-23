# Runbook 37: Grid V2 Warning Observability Smoke

Purpose: prove that Grid V2 warning observability wiring is live and actionable without mutating dispatch behavior.

Scope:
- Validate warning counters are exposed in `/metrics`.
- Validate warning logs include explicit classification payloads.
- Capture a stable control window proof (no warning bursts).

Status: smoke-verified on `main` (2026-03-23, 15-minute control window).

---

## Preconditions

- Futures mainnet profile configured for `PIPPINUSDT`.
- Metrics endpoint reachable (default `:19092` for this ceremony).
- Launch guard defaults enabled (`--pre-cleanup`, `--cleanup-on-exit`).

---

## Procedure (Control Window)

Run a bounded live window (example: 15 minutes), then collect:

1. Runtime log (`run.log`)
2. `/metrics` snapshot from the same run
3. Final exchange state verification

Minimum required checks:

- `GRID_V2_INTEGRITY_MISMATCH_PENDING` count over window
- `GRID_V2_REJECTED_FILL_CLEANED` count over window
- `GRID_V2_EXIT_FILL_ORPHAN` count over window
- `unknown_event_type event_type=...` structured warnings present (classification quality)
- End-state cleanliness (`orders=0`, `position=FLAT`)

---

## 2026-03-23 Control Window (15m) — PASS

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

Metrics wiring confirmed:
- `grinder_live_grid_v2_integrity_mismatch_pending_total{...}`
- `grinder_live_grid_v2_rejected_fill_cleaned_total{...}`

Log quality confirmed:
- `unknown_event_type` warning includes explicit `event_type=<name>`.

Verdict:
- Observability contract is healthy for this control window.
- Alert thresholds from `monitoring/alert_rules.yml` were not breached in this run.

---

## Exit Criteria

Smoke is considered PASS when all are true:

1. Metrics for both warning counters are present in `/metrics`.
2. Warning logs are classified (event type included).
3. No integrity/reject/orphan bursts in control window.
4. Run ends clean (`orders=0`, `position=FLAT`).
