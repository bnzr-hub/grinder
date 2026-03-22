# 36 — Multi-Symbol + Eligibility + ML Integration v1

> Status: PHASE 0 CONTRACT BASELINE (docs-only, code wiring not started)
> Scope: integrate multi-symbol live orchestration with deterministic symbol eligibility and ML-aware scoring.

## 1) Why this exists

We already have:
- multi-symbol runtime (`--symbols`) in `scripts/run_trading.py`,
- futures venue preflight (symbol exists in futures exchangeInfo),
- Top-K selection implementations (`prefilter/topk.py`, `selection/topk_v1.py`),
- ML signal contract (`MlSignalSnapshot`) and inference pipeline.

What is missing is a single live orchestration path that combines these into one safe operator workflow.

## 2) Current factual baseline

### 2.1 Multi-symbol (implemented)
- `--symbols` supports comma-separated symbols in live runner.
- Futures preflight is fail-closed for missing symbols.

### 2.2 Eligibility / Top-K (implemented, mostly paper/backtest wiring)
- Top-K v0: volatility-proxy deterministic selector.
- Top-K v1: gated score (`range + liquidity - toxicity - trend`) with deterministic tie-break.
- Hard gates already defined (toxicity/spread/thin-book/warmup).
- Additional Phase 0 floor (binding, configurable): `NATR_14_5m >= min_natr_bps` (default `100 bps` = `1%`).

### 2.3 ML (implemented as signal layer, limited policy consumption)
- `MlSignalSnapshot` and ONNX inference flow exist.
- ML policy integration is partially planned in docs (`signal -> policy params` not fully universal in live paths).

### 2.4 Existing formula sources (must reuse, not reinvent)
- Top-K v1 score shape is already specified in smart-grid SSOT:
  - `score = range + liquidity - toxicity_penalty - trend_penalty`
  - Source: `docs/17_ADAPTIVE_SMART_GRID_V1.md` (§17.14), mirrored in `docs/smart_grid/SPEC_V1_2.md`.
- Prefilter signal fields and components are already specified:
  - `range_score`, `liquidity_score`, `tox_penalty`, `trend_strength`
  - Source: `docs/04_PREFILTER_SPEC.md`.
- ML signal contract and rollout controls are already specified:
  - `MlSignalSnapshot`, shadow/active rollout, fail-open behavior
  - Source: `docs/12_ML_SPEC.md` + runbooks `18/19`.

Phase 1/2/3 must consume these existing definitions unless an explicit SSOT amendment is merged first.

## 3) Target v1 behavior (live)

For each selection cycle:
1. Build candidate universe from operator-provided symbol universe.
2. Apply hard eligibility gates (venue + constraints + microstructure gates).
3. Rank eligible symbols via Top-K v1 deterministic scoring.
4. Optionally apply ML score adjustment in bounded range (shadow first).
5. Produce active symbol set for live trading loop.
6. Enforce hysteresis/hold timers to avoid churn.

Outputs:
- `active_symbols`
- `excluded_symbols` with reason codes
- deterministic score table (for audit/proof).

### 3.1 Hysteresis contract (required for Phase 2)

Selection churn is treated as a safety risk. Minimum controls:

- `min_hold_cycles`: once a symbol enters active set, it cannot be removed before N selection cycles.
- `max_changes_per_cycle`: upper bound on add/remove operations per cycle.
- `enter_threshold` / `exit_threshold` (hysteresis band): symbol must beat stronger threshold to enter than to remain.
- deterministic tie-break remains `(-score, symbol)`.

## 4) Operational priority alignment (short horizon)

This track must not bypass current operational hardening priorities.

### Gate A (must be complete first)
- launch guard for live runs:
  `preflight -> (optional cleanup) -> verify -> start`

### Gate B (required during rollout)
- stable live validation windows with strict proof bundle:
  - clean start proof,
  - selection output proof,
  - end-state verify proof.

### Gate C (safety scope control)
- watchdog hardening remains targeted only to observed failures.
- no broad speculative self-healing framework as part of this track.

### Gate D (grid_v2 branch safety)
- Eligibility changes must not orphan open inventory/exit protection.
- No forced emergency unwind in this track.
- Mid-branch ineligibility uses graceful degrade policy (see 5.4).

## 5) Proposed rollout phases

### Phase 0 — Docs + contracts only
- Finalize candidate universe contract, reason codes, metrics schema.
- No runtime behavior change.

### Phase 1 — Shadow selector in live runner
- Compute eligibility/Top-K each cycle but do not change dispatch universe.
- Emit metrics + logs + proof artifacts only.
- Flagged by `GRINDER_SYMBOL_SELECTOR_SHADOW=1` (proposal).

### Phase 2 — Controlled activation
- Feature-flagged activation for a bounded subset.
- Hysteresis and max symbol-change-per-cycle guard enabled.
- Flagged by `GRINDER_SYMBOL_SELECTOR_ENABLED=1` (proposal).

### Phase 3 — ML-assisted scoring (shadow then active)
- Add bounded ML adjustment term after baseline Top-K v1 score.
- Keep fail-open fallback to non-ML score.
- Flagged by `GRINDER_SYMBOL_SELECTOR_ML_ENABLED=1` (proposal; shadow-first rollout).

### 5.4 Grid_v2 interaction policy (P0 decision)

If symbol becomes ineligible while grid_v2 has non-flat inventory:

- Enter `graceful_exit_only` for that symbol:
  - stop creating new entry intents,
  - keep/maintain exit protection and reconciliation,
  - continue until position returns FLAT.
- Remove symbol from active trading universe only after FLAT + verify clean.
- If state is ambiguous (sync mismatch / unknown position), fail closed to conservative mode:
  keep symbol managed (no new entries) until exchange-truth converges.

This avoids unprotected inventory while still honoring eligibility decisions.

## 6) Non-goals (v1)

- Multi-venue routing (still P2 deferred).
- Autonomous discovery from full exchange universe without operator base universe.
- Aggressive dynamic capital allocator redesign.

## 7) Success criteria

- Deterministic selection outputs on replay and repeated live snapshots.
- No increase in startup fail-closed incidents due to selection logic.
- No symbol-churn spikes beyond configured limits.
- Operator can explain every selected/excluded symbol via reason codes.

## 8) Proposed config contract (env, proposal only)

These names are proposed for consistency with existing `GRINDER_*` runtime controls:

- `GRINDER_SYMBOL_SELECTOR_SHADOW` (default `0`)
- `GRINDER_SYMBOL_SELECTOR_ENABLED` (default `0`)
- `GRINDER_SYMBOL_SELECTOR_K` (default `3`)
- `GRINDER_SYMBOL_SELECTOR_CYCLE_S` (default `60`)
- `GRINDER_SYMBOL_SELECTOR_MIN_HOLD_CYCLES` (default `5`)
- `GRINDER_SYMBOL_SELECTOR_MAX_CHANGES_PER_CYCLE` (default `1`)
- `GRINDER_SYMBOL_SELECTOR_ENTER_THRESHOLD_BPS` (default `0`)
- `GRINDER_SYMBOL_SELECTOR_EXIT_THRESHOLD_BPS` (default `0`)
- `GRINDER_SYMBOL_SELECTOR_ML_ENABLED` (default `0`)
- `GRINDER_SYMBOL_SELECTOR_ML_ADJUST_MAX_BPS` (default `0`, bounded adjustment cap)

Thresholds are intentionally configurable via env contract (no hard-coded runtime constants in docs).

## 9) Phase 0 binding artifacts (docs-only)

### 9.1 Reason codes (initial, deterministic)
- `NOT_IN_OPERATOR_UNIVERSE`
- `VENUE_UNAVAILABLE`
- `SYMBOL_CONSTRAINTS_MISSING`
- `WARMUP_INCOMPLETE`
- `NATR_BELOW_MIN`
- `THIN_BOOK`
- `SPREAD_SPIKE`
- `TOXICITY_HIGH`
- `TREND_TOO_STRONG`
- `MIN_HOLD_ACTIVE`
- `MAX_CHANGES_BUDGET_EXHAUSTED`
- `GRACEFUL_EXIT_ONLY_ACTIVE`

These are the initial exclusion/override reason codes for selector audit output.
Any rename/add/remove requires SSOT + contract test update in implementation PR.

### 9.2 Metrics schema (initial)
- `grinder_selector_cycle_total{mode,result}`
- `grinder_selector_candidate_count{stage}` (`input`, `eligible`, `selected`)
- `grinder_selector_excluded_total{reason}`
- `grinder_selector_churn_total{kind}` (`added`, `removed`, `deferred`)
- `grinder_selector_score_bps{symbol,component}` (`range`, `liquidity`, `toxicity`, `trend`, `ml_adjust`, `final`)
- `grinder_selector_graceful_exit_only_gauge{symbol}`

All selector metrics are observability-only in Phase 1 shadow mode (no dispatch impact).

### 9.3 Phase 0 exit criteria
- Config contract frozen (section 8).
- Initial reason-code catalog frozen (section 9.1).
- Initial metrics schema frozen (section 9.2).
- `STATE.md` + `DECISIONS.md` + `POST_LAUNCH_ROADMAP.md` synchronized to this doc.

## 10) Configurable threshold contract (Phase 0 baseline)

To support operator tuning, hard gates and scoring penalties are parameterized:

- `GRINDER_SYMBOL_SELECTOR_MIN_NATR_BPS` (default `100`) -> exclude if `NATR_14_5m < min_natr_bps` (`NATR_BELOW_MIN`)
- `GRINDER_SYMBOL_SELECTOR_TREND_PENALTY_W` (default `1.0`) -> multiplier for trend penalty component
- `GRINDER_SYMBOL_SELECTOR_TREND_HARD_GATE_BPS` (default `0`, disabled) -> if `>0`, exclude when `trend_strength_bps > threshold` (`TREND_TOO_STRONG`)
- `GRINDER_SYMBOL_SELECTOR_TOXICITY_PENALTY_W` (default `1.0`) -> multiplier for toxicity penalty component
- `GRINDER_SYMBOL_SELECTOR_LIQUIDITY_WEIGHT_W` (default `1.0`) -> multiplier for liquidity component
- `GRINDER_SYMBOL_SELECTOR_RANGE_WEIGHT_W` (default `1.0`) -> multiplier for range component

Validation policy (fail-closed in implementation phase):
- all `*_BPS` thresholds must be `>= 0`
- all penalty/weight multipliers must be finite and `>= 0`
