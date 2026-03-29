# Runbook 30: Account Sync Fire Drill (Launch-15)

Deterministic fire drill for the AccountSyncer mismatch detection pipeline.
Exercises all 4 mismatch rules + metrics contract against the REAL `AccountSyncer.sync()` code path.

**SSOT:** `docs/15_ACCOUNT_SYNC_SPEC.md` (Sec 15.9)

---

## 1. Overview

The account sync fire drill proves that:

1. **Clean sync** works end-to-end (positions + orders + metrics recorded)
2. **Mismatch detection** catches: `duplicate_key`, `negative_qty`, `orphan_order`, `ts_regression`
3. **Metrics** are wired correctly to Prometheus via `MetricsBuilder`
4. **Invariants** hold: monotonic ts guard rejects regression, last_ts not updated on regression

No API keys needed. No network calls. Pure CPU, ~2 seconds.

---

## 2. Running the drill

**Recommended** (via ops entrypoint):
```bash
bash scripts/ops_fill_triage.sh account-sync-drill
```

**Standalone:**
```bash
bash scripts/fire_drill_account_sync.sh
```

**With custom artifact directory:**
```bash
GRINDER_ARTIFACT_DIR=/tmp/evidence bash scripts/fire_drill_account_sync.sh
```

---

## 3. What each drill proves

| Drill | Scenario | Assertions |
|-------|----------|------------|
| **A** | Clean sync (happy path) | `ok=True`, 0 mismatches, `last_ts` updated, metrics recorded (positions, orders, pending_notional) |
| **B** | Duplicate key + negative qty | `ok=False`, 2 mismatches (`duplicate_key`, `negative_qty`), mismatch counters incremented |
| **C** | Orphan order | `ok=False`, 1 mismatch (`orphan_order`), only unknown order flagged, known order NOT flagged |
| **D** | Timestamp regression | `ok=False`, `ts_regression` detected, `last_ts` NOT updated (regression rejected, stays at previous value). **ADR-108:** After 3 consecutive bounded regressions (<10s), the 4th is tolerated: `ok=True`, `last_ts` advanced, log `ACCOUNT_SYNC_TS_REGRESSION_TOLERATED`. Large regressions (>10s) always block. |
| **E** | Metrics contract smoke | All account sync patterns from `REQUIRED_METRICS_PATTERNS` present in `MetricsBuilder` output |

---

## 4. Expected output

```
=== Results: 34 passed, 0 failed, 0 skipped ===
```

Key PASS markers to grep:
```
drill_a_PROVEN: clean sync
drill_b_PROVEN: duplicate_key + negative_qty detected
drill_c_PROVEN: orphan_ord_x flagged, known_ord_1 not flagged
drill_d_PROVEN: ts_regression detected
drill_e_PROVEN: all N account sync patterns present
```

---

## 5. Verifying artifacts

```bash
# From the evidence directory printed in output:
cd .artifacts/account_sync_fire_drill/<timestamp>/

# Verify integrity
sha256sum -c sha256sums.txt

# Read summary
cat summary.txt
```

---

## 6. Artifact layout

```
.artifacts/account_sync_fire_drill/<YYYYMMDDTHHMMSSZ>/
  drill_a_log.txt          # Clean sync: ok, metrics
  drill_a_metrics.txt      # Prometheus text (positions, orders, pending_notional)
  drill_b_log.txt          # Mismatch: duplicate_key + negative_qty
  drill_b_metrics.txt      # Prometheus text (mismatch counters)
  drill_c_log.txt          # Orphan: orphan_ord_x flagged
  drill_c_metrics.txt      # Prometheus text (orphan_order counter)
  drill_d_log.txt          # Regression: ts_regression, last_ts unchanged
  drill_d_metrics.txt      # Prometheus text (ts_regression counter)
  drill_e_metrics.txt      # Full MetricsBuilder output (contract smoke)
  summary.txt              # Copy/paste evidence block
  sha256sums.txt           # Full 64-char sha256 of all artifact files
```

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: grinder` | PYTHONPATH not set | Run via `scripts/fire_drill_account_sync.sh` (sets it automatically) |
| Drill E fails with missing patterns | `REQUIRED_METRICS_PATTERNS` out of sync | Check `src/grinder/observability/live_contract.py` includes account_sync patterns |
| `.artifacts/` in git status | `.gitignore` broken | Ensure `.artifacts/` is in `.gitignore` |

---

## 8. Evidence commands (for PR proof)

```bash
# Run drill and capture output
bash scripts/ops_fill_triage.sh account-sync-drill --no-status 2>&1 | tee /tmp/drill.log

# Grep for EVIDENCE_REF line
grep '^EVIDENCE_REF' /tmp/drill.log

# Verify sha256sums
cd <evidence_dir> && sha256sum -c sha256sums.txt

# Copy evidence into PR body
cat <evidence_dir>/summary.txt
cat <evidence_dir>/sha256sums.txt
```

---

## AccountSyncMismatchSpike

**Severity:** Warning | **Category:** correctness | **`for`:** 3m

**Meaning:** One or more account sync mismatches detected in the last 5 minutes.
The `rule` label indicates the mismatch type: `duplicate_key`, `ts_regression`,
`negative_qty`, or `orphan_order`.

**Impact:** Internal state and exchange state have diverged. Depending on the rule:

| Rule | Risk |
|------|------|
| `orphan_order` | Order on exchange not tracked — may fill without risk checks |
| `duplicate_key` | Data corruption in fetch/parse layer |
| `negative_qty` | Invalid exchange response or parse bug |
| `ts_regression` | Clock skew or stale API cache |

**PromQL:**
```promql
sum(increase(grinder_account_sync_mismatches_total{rule!="none"}[5m])) > 0
```

**Triage Steps:**

1. Identify mismatch rules:
   ```bash
   curl -s localhost:9090/metrics | grep grinder_account_sync_mismatches_total
   ```

2. Run the fire drill to verify detection pipeline:
   ```bash
   bash scripts/ops_fill_triage.sh account-sync-drill
   ```

3. Check evidence artifacts for details:
   ```bash
   ls -lt .artifacts/account_sync/ | head -5
   cat .artifacts/account_sync/<latest>/mismatches.json
   ```

4. If `orphan_order`: check if manual order was placed outside the system
5. If `ts_regression`: investigate exchange API latency or clock drift

**Resolution:**
- `orphan_order`: cancel the orphan order on exchange, or add to tracked set
- `duplicate_key` / `negative_qty`: file a bug — invariant violation
- `ts_regression`: usually transient from Binance cached snapshots. **ADR-108:** After 3 consecutive bounded regressions (<10s delta), tolerance kicks in automatically — `ACCOUNT_SYNC_TS_REGRESSION_TOLERATED` log. If regression >10s or persistent after tolerance, check NTP sync or exchange API health.

---

## See also

- [Account Sync runbook](29_ACCOUNT_SYNC.md) -- enablement, metrics, evidence artifacts
- [Evidence Index](00_EVIDENCE_INDEX.md) -- full evidence matrix
- [Ops Quickstart](00_OPS_QUICKSTART.md) -- one-command examples
