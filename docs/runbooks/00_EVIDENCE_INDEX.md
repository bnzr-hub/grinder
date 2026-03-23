# Runbook 00: Evidence Index (Launch-11)

One-table reference: what you want to prove, which script to run, and where to find the artifacts.

**Recommended**: use the unified entrypoint `bash scripts/ops_fill_triage.sh <mode>` for all fill and connector evidence. See [Ops Quickstart](00_OPS_QUICKSTART.md).

See also: [Ops Quickstart](00_OPS_QUICKSTART.md) | [Fill Tracker Triage](26_FILL_TRACKER_TRIAGE.md)

---

## Evidence matrix

| Scenario | What you prove | Script | Artifact dir | Key files | Copy/paste proof |
|----------|---------------|--------|-------------|-----------|-----------------|
| Fill wiring (local) | Metric labels + wiring are sane, no forbidden labels | `scripts/smoke_fill_ingest.sh` | *(no persistent artifacts)* | terminal output | `=== Results: N passed, 0 failed ===` |
| Staging enablement | Gates A/B/C pass, real Binance reads work, cursor persists across restart | `scripts/smoke_fill_ingest_staging.sh` | `.artifacts/fill_ingest_staging/<ts>/` | `gate_a_metrics.txt`, `gate_b_metrics.txt`, `gate_c_metrics.txt`, `cursor_after_run1.json`, `cursor_after_run2.json` | evidence block in terminal output |
| Alert inputs (non-monotonic rejection) | `rejected_non_monotonic` counter increments, cursor unchanged, log marker present | `scripts/fire_drill_fill_alerts.sh` | `.artifacts/fill_alert_fire_drill/<ts>/` | `drill_a_metrics.txt`, `drill_a_log.txt`, `cursor_before_drill_a.json`, `cursor_after_drill_a.json` | `summary.txt` |
| Alert inputs (cursor stuck) | `cursor_save error` counter increments, `cursor_age_seconds` grows over time | `scripts/fire_drill_fill_alerts.sh` | `.artifacts/fill_alert_fire_drill/<ts>/` | `drill_b_metrics_1.txt`, `drill_b_metrics_2.txt`, `drill_b_log.txt`, `cursor_drill_b.json` | `summary.txt` |
| Kill-switch + enforcement | Kill-switch trips, gauge=1, INCREASE_RISK blocked, CANCEL allowed, idempotent latch | `scripts/fire_drill_risk_killswitch_drawdown.sh` | `.artifacts/risk_fire_drill/<ts>/` | `drill_a_metrics.txt`, `drill_a_log.txt` | `summary.txt` |
| Drawdown guard | DrawdownGuardV1 blocks INCREASE_RISK in DRAWDOWN, allows REDUCE_RISK/CANCEL, state latched | `scripts/fire_drill_risk_killswitch_drawdown.sh` | `.artifacts/risk_fire_drill/<ts>/` | `drill_b_metrics.txt`, `drill_b_log.txt` | `summary.txt` |
| Budget per-run cap | Per-run notional cap blocks execution, block reason + metrics correct | `scripts/fire_drill_reconcile_budget_limits.sh` | `.artifacts/budget_fire_drill/<ts>/` | `drill_a_metrics.txt`, `drill_a_log.txt`, `drill_a_state.json` | `summary.txt` |
| Budget per-day cap | Per-day notional cap blocks across run boundaries, UTC day key, state persisted | `scripts/fire_drill_reconcile_budget_limits.sh` | `.artifacts/budget_fire_drill/<ts>/` | `drill_b_metrics.txt`, `drill_b_log.txt`, `drill_b_state.json` | `summary.txt` |
| Execution intent gates | NOT_ARMED blocks all, kill-switch blocks non-CANCEL, drawdown blocks INCREASE_RISK, all-pass reaches port | `scripts/fire_drill_execution_intents.sh` | `.artifacts/execution_fire_drill/<ts>/` | `drill_a_*.txt`, `drill_b_*.txt`, `drill_c_*.txt`, `drill_d_*.txt` | `summary.txt` |
| Market data connector | L2 parse validation, DQ staleness/gaps/outliers, symbol whitelist | `scripts/ops_fill_triage.sh connector-market-data` | `.artifacts/connector_market_data_fire_drill/<ts>/` | `drill_a_*.txt` .. `drill_e_*.txt` | `summary.txt` + `sha256sums.txt` |
| Exchange port boundary | Gate chain (5 gates), idempotency cache, retry classification (transient vs fatal) | `scripts/ops_fill_triage.sh connector-exchange-port` | `.artifacts/connector_exchange_port_fire_drill/<ts>/` | `drill_a_log.txt` .. `drill_f_log.txt`, `drill_e_metrics.txt`, `drill_f_metrics.txt` | `summary.txt` + `sha256sums.txt` |
| SOR fire drill | Router decisions (CANCEL_REPLACE/BLOCK/NOOP), metrics wiring, contract smoke | `scripts/ops_fill_triage.sh sor-fire-drill` | `.artifacts/sor_fire_drill/<ts>/` | `drill_a_*.txt` .. `drill_d_*.txt` | `summary.txt` + `sha256sums.txt` |
| Account sync evidence | Positions + open orders snapshot, mismatch detection, metrics | `GRINDER_ACCOUNT_SYNC_EVIDENCE=1` (env-gated) | `.artifacts/account_sync/<ts>/` | `account_snapshot.json`, `positions.json`, `open_orders.json`, `mismatches.json` | `summary.txt` + `sha256sums.txt` |
| Account sync fire drill | Mismatch rules (duplicate_key, negative_qty, orphan_order, ts_regression), metrics wiring, contract smoke | `scripts/ops_fill_triage.sh account-sync-drill` | `.artifacts/account_sync_fire_drill/<ts>/` | `drill_a_*.txt` .. `drill_e_*.txt` | `summary.txt` + `sha256sums.txt` |
| Fill probability evidence | Why an order was blocked/shadowed by fill prob gate — features, model metadata, threshold vs probability | `GRINDER_FILL_PROB_EVIDENCE=1` (env-gated) | `.artifacts/fill_prob/` | `{ts_ms}_{verdict}_{symbol}.json` + `.sha256` | JSON evidence artifact |
| Grid V2 shadow verification | Shadow runner tests pass, ruff/mypy clean, shadow isolated from dispatch | `scripts/smoke_grid_v2_shadow.sh` | `.artifacts/grid_v2_shadow/<ts>/` | `gate_a_shadow_tests.txt`, `gate_b_full_grid_v2.txt`, `gate_c_ruff.txt`, `gate_d_format.txt`, `gate_e_mypy.txt` | `summary.txt` + `sha256sums.txt` |
| Grid V2 warning observability smoke | Warning counters and classified warning logs are wired; control window stability proof | `python3 scripts/run_trading.py ... --duration-s 900` + log/metrics scrape | `/tmp/grinder_stable_window_15m.log` | run log + `/metrics` snapshot + final exchange verify | stable-window summary in Runbook 37 |

---

## Artifact details

### Local smoke (`smoke_fill_ingest.sh`)

No persistent artifacts. All assertions are inline in terminal output. Run and check exit code.

### Staging smoke (`smoke_fill_ingest_staging.sh`)

```
.artifacts/fill_ingest_staging/<YYYYMMDDTHHMMSS>/
  gate_a_metrics.txt          # Prometheus text (Gate A: OFF, FakePort)
  gate_b_metrics.txt          # Prometheus text (Gate B: ON, real Binance)
  gate_c_metrics.txt          # Prometheus text (Gate C: restart persistence)
  cursor_after_run1.json      # Cursor state after first real run
  cursor_before_restart.json  # Cursor snapshot before restart (monotonicity check)
  cursor_after_run2.json      # Cursor state after restart run
```

Gate B/C artifacts only present when `BINANCE_API_KEY` and `BINANCE_API_SECRET` are set.

### Fire drill (`fire_drill_fill_alerts.sh`)

```
.artifacts/fill_alert_fire_drill/<YYYYMMDDTHHMMSS>/
  cursor_before_drill_a.json  # Cursor before non-monotonic save attempt
  cursor_after_drill_a.json   # Cursor after (should match before)
  cursor_drill_a.json         # Working cursor file used during Drill A
  drill_a_metrics.txt         # Prometheus metrics after Drill A
  drill_a_log.txt             # Captured stderr (FILL_CURSOR_REJECTED_NON_MONOTONIC)
  cursor_drill_b.json         # Cursor used for Drill B
  drill_b_metrics_1.txt       # Scrape 1: after initial successful save
  drill_b_metrics_2.txt       # Scrape 2: after failed saves + time passage
  drill_b_log.txt             # Captured stderr from Drill B
  summary.txt                 # Copy/paste evidence block with exact metric lines
  sha256sums.txt              # Full 64-char sha256 of all artifact files
```

### Risk fire drill (`fire_drill_risk_killswitch_drawdown.sh`)

```
.artifacts/risk_fire_drill/<YYYYMMDDTHHMMSS>/
  drill_a_metrics.txt      # Full Prometheus text after kill-switch trip
  drill_a_log.txt          # Captured stderr (trip, gate, idempotent markers)
  drill_b_metrics.txt      # Full Prometheus text after drawdown trigger
  drill_b_log.txt          # Captured stderr (state transitions, intent decisions)
  summary.txt              # Copy/paste evidence block with exact metric lines
  sha256sums.txt           # Full 64-char sha256 of all artifact files
```

### Budget fire drill (`fire_drill_reconcile_budget_limits.sh`)

```
.artifacts/budget_fire_drill/<YYYYMMDDTHHMMSS>/
  drill_a_metrics.txt      # Full Prometheus text after per-run cap block
  drill_a_log.txt          # Captured stderr (budget checks, block decisions)
  drill_a_state.json       # BudgetTracker state file (persistence proof)
  drill_b_metrics.txt      # Full Prometheus text after per-day cap block
  drill_b_log.txt          # Captured stderr (cross-run blocking, day key)
  drill_b_state.json       # BudgetTracker state file (cross-run persistence)
  summary.txt              # Copy/paste evidence block with exact metric lines
  sha256sums.txt           # Full 64-char sha256 of all artifact files
```

### Execution fire drill (`fire_drill_execution_intents.sh`)

```
.artifacts/execution_fire_drill/<YYYYMMDDTHHMMSS>/
  drill_a_metrics.txt      # Prometheus text (NOT_ARMED state)
  drill_a_log.txt          # Captured stderr (all 4 action types blocked)
  drill_b_metrics.txt      # Prometheus text (kill-switch ON)
  drill_b_log.txt          # Captured stderr (PLACE/REPLACE blocked, CANCEL through)
  drill_c_metrics.txt      # Prometheus text (drawdown active)
  drill_c_log.txt          # Captured stderr (intent blocking + classify_intent proof)
  drill_d_metrics.txt      # Prometheus text (clean state, all gates pass)
  drill_d_log.txt          # Captured stderr (port calls recorded)
  summary.txt              # Copy/paste evidence block with gate decisions
  sha256sums.txt           # Full 64-char sha256 of all artifact files
```

### Market data connector fire drill

**Command**: `bash scripts/ops_fill_triage.sh connector-market-data`

**Expected PASS markers** (grep to verify):
```
=== Results: 21 passed, 0 failed, 0 skipped ===
  PASSED: connector-market-data
  evidence_dir: .artifacts/connector_market_data_fire_drill/<ts>
```

**Files to attach**: `summary.txt`, `sha256sums.txt`

```
.artifacts/connector_market_data_fire_drill/<YYYYMMDDTHHMMSS>/
  drill_a_log.txt          # L2ParseError rejects malformed payloads
  drill_b_log.txt          # DQ staleness detection (stale_total counter)
  drill_b_metrics.txt      # Prometheus text (stale counter)
  drill_c_log.txt          # Symbol whitelist filtering
  drill_d_log.txt          # Gap detection + outlier detection
  drill_d_metrics.txt      # Prometheus text (gap + outlier counters)
  drill_e_log.txt          # Happy path: clean DQ + valid L2 parse
  drill_e_metrics.txt      # Prometheus text (happy path zeroes)
  summary.txt              # Copy/paste evidence block
  sha256sums.txt           # Full 64-char sha256 of all artifact files
```

### Exchange port boundary fire drill

**Command**: `bash scripts/ops_fill_triage.sh connector-exchange-port`

**Expected PASS markers** (grep to verify):
```
=== Results: 40 passed, 0 failed, 0 skipped ===
  PASSED: connector-exchange-port
  evidence_dir: .artifacts/connector_exchange_port_fire_drill/<ts>
```

**Files to attach**: `summary.txt`, `sha256sums.txt`

```
.artifacts/connector_exchange_port_fire_drill/<YYYYMMDDTHHMMSS>/
  drill_a_log.txt          # NOT_ARMED blocks all (gate 1, 0 port calls)
  drill_b_log.txt          # Kill-switch blocks PLACE, allows CANCEL (gate 3)
  drill_c_log.txt          # Drawdown blocks INCREASE_RISK, CANCEL+NOOP safe (gate 5)
  drill_d_log.txt          # Symbol whitelist blocks, retry counters = 0 (gate 4)
  drill_e_log.txt          # Idempotency cache prevents duplicate port calls
  drill_e_metrics.txt      # Prometheus text (idempotency hits/misses)
  drill_f_log.txt          # Retry classification: transient vs fatal
  drill_f_metrics.txt      # Prometheus text (retry counters)
  summary.txt              # Copy/paste evidence block with code_path markers
  sha256sums.txt           # Full 64-char sha256 of all artifact files
```

### SOR fire drill (`fire_drill_sor.sh`)

**Command**: `bash scripts/ops_fill_triage.sh sor-fire-drill`

**Expected PASS markers** (grep to verify):
```
=== Results: 20 passed, 0 failed, 0 skipped ===
  PASSED: sor-fire-drill
  evidence_dir: .artifacts/sor_fire_drill/<ts>
```

**Files to attach**: `summary.txt`, `sha256sums.txt`

Artifact root can be overridden via `GRINDER_ARTIFACT_DIR` (default: `.artifacts`).

```
.artifacts/sor_fire_drill/<YYYYMMDDTHHMMSS>/
  drill_a_log.txt          # CANCEL_REPLACE: happy path, port called
  drill_a_metrics.txt      # Prometheus text (decision counter)
  drill_b_log.txt          # BLOCK: spread crossing, ROUTER_BLOCKED
  drill_b_metrics.txt      # Prometheus text (block counter)
  drill_c_log.txt          # NOOP: budget exhausted (router-only, direct route())
  drill_c_metrics.txt      # Prometheus text (noop counter)
  drill_d_metrics.txt      # Full MetricsBuilder output (contract smoke)
  summary.txt              # Copy/paste evidence block
  sha256sums.txt           # Full 64-char sha256 of all artifact files
```

### Account sync evidence

**Enablement**: `GRINDER_ACCOUNT_SYNC_EVIDENCE=1` (env-gated, off by default)

Evidence is written automatically during live sync cycles when the env var is set.
See [Account Sync runbook](29_ACCOUNT_SYNC.md) for full details.

```
.artifacts/account_sync/<YYYYMMDDTHHMMSSZ>/
  account_snapshot.json     # Full AccountSnapshot (canonical JSON)
  positions.json            # Positions only
  open_orders.json          # Open orders only
  mismatches.json           # Detected mismatches (empty array if clean)
  summary.txt               # Human-readable evidence block
  sha256sums.txt            # sha256 of all artifact files
```

### Account sync fire drill (`fire_drill_account_sync.sh`)

**Command**: `bash scripts/ops_fill_triage.sh account-sync-drill`

**Expected PASS markers** (grep to verify):
```
=== Results: 34 passed, 0 failed, 0 skipped ===
  PASSED: account-sync-drill
  evidence_dir: .artifacts/account_sync_fire_drill/<ts>
```

**Files to attach**: `summary.txt`, `sha256sums.txt`

Artifact root can be overridden via `GRINDER_ARTIFACT_DIR` (default: `.artifacts`).

```
.artifacts/account_sync_fire_drill/<YYYYMMDDTHHMMSSZ>/
  drill_a_log.txt          # Clean sync: happy path, metrics recorded
  drill_a_metrics.txt      # Prometheus text (positions, orders, pending_notional)
  drill_b_log.txt          # Mismatch: duplicate_key + negative_qty
  drill_b_metrics.txt      # Prometheus text (mismatch counters)
  drill_c_log.txt          # Orphan: orphan_ord_x flagged, known_ord_1 not
  drill_c_metrics.txt      # Prometheus text (orphan_order counter)
  drill_d_log.txt          # Regression: ts_regression, last_ts unchanged
  drill_d_metrics.txt      # Prometheus text (ts_regression counter)
  drill_e_metrics.txt      # Full MetricsBuilder output (contract smoke)
  summary.txt              # Copy/paste evidence block
  sha256sums.txt           # Full 64-char sha256 of all artifact files
```

### Fill probability evidence

**Enablement**: `GRINDER_FILL_PROB_EVIDENCE=1` (env-gated, off by default)

Evidence is written automatically during live engine processing when an order is BLOCKED or SHADOW-predicted by the fill probability gate.

```
.artifacts/fill_prob/
  {ts_ms}_BLOCK_{symbol}.json     # Evidence for blocked order
  {ts_ms}_BLOCK_{symbol}.sha256   # SHA256 sidecar
  {ts_ms}_SHADOW_{symbol}.json    # Evidence for shadow-predicted order
  {ts_ms}_SHADOW_{symbol}.sha256  # SHA256 sidecar
```

**JSON payload** (`fill_prob_evidence_v1`):
- `verdict`: BLOCK or SHADOW
- `prob_bps`: predicted fill probability (0..10000)
- `threshold_bps`: configured minimum threshold
- `enforce`: whether enforcement was active
- `features`: direction, notional_bucket, entry_fill_count, holding_ms_bucket
- `action`: symbol, side, price, qty, action_type
- `model`: n_bins, n_train_rows, global_prior_bps (null if model unavailable)

**Structured log**: `FILL_PROB_EVIDENCE` always emitted on BLOCK (not env-gated). On SHADOW, only when `GRINDER_FILL_PROB_EVIDENCE=1`.

---

## CI artifact

Every CI run (push to main + PRs) uploads `docs/runbooks/00_EVIDENCE_INDEX.json` as the `evidence-index-json` artifact.

To download from a specific run:
```bash
gh run download <run_id> -n evidence-index-json
cat 00_EVIDENCE_INDEX.json | jq '.entries[] | select(.mode=="connector-market-data")'
```

---

## Notes

- `.artifacts/` is gitignored. Do not commit evidence files.
- Each run creates a timestamped subdirectory. Old runs are not auto-deleted.
- `sha256sums.txt` (fire drill) and inline sha256/bytes (staging smoke) provide integrity proof.
- All scripts exit non-zero on any failure.
- **Unified entrypoint (recommended)**: `bash scripts/ops_fill_triage.sh <mode>` covers fill modes (`local`, `staging`, `fire-drill`) and connector modes (`connector-market-data`, `connector-exchange-port`).
- **Risk triage wrapper**: `bash scripts/ops_risk_triage.sh <mode>` runs the right risk script (`killswitch-drawdown` or `budget-limits`).
- **Execution triage wrapper**: `bash scripts/ops_exec_triage.sh <mode>` runs the execution intent fire drill (`exec-fire-drill`).
- **Connector triage wrapper (standalone)**: `bash scripts/ops_connector_triage.sh <mode>` — same drills, but prefer the unified entrypoint above.
- See [Ops Quickstart](00_OPS_QUICKSTART.md) for one-command examples.
