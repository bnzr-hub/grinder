# Documentation vs Implementation Gaps

> **Status:** HISTORICAL GAP SNAPSHOT (reference, needs periodic re-sync)
>
> This file is kept for context and idea capture.
> For authoritative current state and priorities, use:
> - `docs/STATE.md`
> - `docs/DECISIONS.md`
> - `docs/POST_LAUNCH_ROADMAP.md`
>
> If a row here conflicts with `STATE.md`, treat `STATE.md` as the source of truth.

> SSOT index of where specs describe target-state behavior beyond current code.
>
> Last updated: 2026-03-02 (main @ d7b778f)

This file tracks the delta between documented specifications and actual implementation.
Each spec may describe both current reality and planned features — this index clarifies which is which.

---

## Gap Index

| Component | Spec | Code | Status | Priority | Owner | Notes / Tracking | Exit criteria |
|-----------|------|------|--------|----------|-------|------------------|---------------|
| Toxicity formulas (VPIN, Kyle, Amihud, OFI, liquidation surge) | `docs/06_TOXICITY_SPEC.md` | `src/grinder/gating/toxicity_gate.py` | **PARTIAL** | **P2** | Trading | v0: spread_spike + price_impact only; composite scoring planned post-launch | 5 formulas + composite score implemented; unit tests; determinism fixture; Prometheus metrics per component |
| Grid policy library (Trend, LiqCatcher, Funding, VolBreakout, MeanRev) | `docs/07_GRID_POLICY_LIBRARY.md` | `src/grinder/policies/grid/` | **PARTIAL** | **P2** | Trading | 2 of ~6 policies: Static + Adaptive; others are target state | >=2 new policies + router/selection; tests; determinism fixture on policy switching |
| Backtest engine (walk-forward, cost model, queue modeling) | `docs/11_BACKTEST_PROTOCOL.md` | `src/grinder/backtest/cli.py` | **DEFERRED** | **P2** | Research | 29-line stub delegates to replay; full engine deferred to M9 | CLI not delegating to replay; fee/slippage model + walk-forward + OOS report artifact; deterministic mode |
| State machine orchestrator (formal FSM, state persistence) | `docs/08_STATE_MACHINE.md` | `src/grinder/live/fsm_orchestrator.py`, `fsm_driver.py` | **DONE** | **P1** | Core | Launch-13: OrchestratorFSM (381 lines) + FsmDriver (183 lines) + evidence + metrics. Wired to LiveEngineV0. A-series: numeric inputs (A1-A4). PR-B: contract tests. | All exit criteria met (FSM orchestrator, driver, evidence, metrics, contract tests) |
| Smart order routing (amend vs cancel-replace, batching) | `docs/09_EXECUTION_SPEC.md` §9.3 | `src/grinder/execution/smart_order_router.py` | **DONE** | **P1** | Execution | Launch-14: SmartOrderRouter (474 lines) with NOOP/AMEND/CANCEL_REPLACE/BLOCK decision matrix. Wired to LiveEngineV0 `route()`. Fill-prob enforcement gate (Track C). | All exit criteria met (SOR, decision matrix, metrics, fill-prob gate) |
| Fill tracking (FillTracker, RoundTrip, AccountSyncer) | `docs/09_EXECUTION_SPEC.md` §9.6-9.7 | `src/grinder/execution/`, `src/grinder/account/syncer.py` | **DONE** | **P1** | Execution | Launch-06: FillTracker MVP + FillMetrics + wiring. Launch-15: AccountSyncer (247 lines) replaces planned PositionSyncer — positions + open orders sync, mismatch detection, metrics. Wired to LiveEngineV0. PR-A4: position_notional_usd from AccountSyncer. Mainnet pipeline: PR #329 (port protocol), #330 (5s throttle), #331 (BinanceFuturesPort.fetch_account_snapshot). | All exit criteria met (FillTracker, AccountSyncer, position reconcile, metrics) |
| Latency / retry (LatencyMonitor, OrderRetryPolicy) | `docs/09_EXECUTION_SPEC.md` §9.8-9.9 | `src/grinder/net/`, `src/grinder/observability/latency_metrics.py` | **DONE** | **P1** | Execution | PR1: HttpRetryPolicy, DeadlinePolicy, MeasuredHttpClient, HTTP metrics. PR2: MeasuredSyncHttpClient wired into all 12 BinanceFuturesPort call sites; env config (LATENCY_RETRY_ENABLED, HTTP_MAX_ATTEMPTS_*, HTTP_DEADLINE_*_MS); safe-by-default pass-through. PR3: 4 alert rules (2 page + 2 ticket) + triage runbook 24 + validator (op= allowlist + forbidden labels). | Smoke: set LATENCY_RETRY_ENABLED=1, observe grinder_http_latency_ms histogram populated |
| Fill probability model (`estimate_fill_probability()`) | `docs/09_EXECUTION_SPEC.md` §9.4 | `src/grinder/ml/fill_model_v0.py` | **DONE** | **P2** | Research | Track C (PR-C1 through C9): FillModelV0 trained + calibrated + wired to SOR enforcement gate. Auto-threshold from eval report (PR-C9). Controlled rollout with circuit breaker (PR-C8). Evidence artifacts + explainability (PR-C6). Remaining P2: drift detection, online features. | Core exit criteria met (model wired, enforced, calibrated). P2: drift + online features |
| Portfolio risk (beta-adjusted exposure, concentration) | `docs/10_RISK_SPEC.md` §10.5 | `src/grinder/risk/` | **PARTIAL** | **P2** | Risk | DrawdownGuard v1 + KillSwitch done; PortfolioRiskManager not built | PortfolioRiskManager with concentration + per-asset DD; tests; determinism fixture on risk decisions |
| Consecutive loss limit | `docs/10_RISK_SPEC.md` | `src/grinder/risk/consecutive_loss_guard.py`, `src/grinder/risk/consecutive_loss_wiring.py` | **PARTIAL** | **P2** | Risk | PR-C3/C3b/C3c/C3d: guard library + live wiring + per-symbol + persistent state (65+ tests). Metrics wired. NOT wired to LiveEngineV0/FSM (only in reconcile loop). | Wire to LiveEngineV0 FSM input; DEGRADED action on trip |
| ML training pipeline (sklearn→ONNX, walk-forward datasets) | `docs/12_ML_SPEC.md` §12.9 | `scripts/train_regime_model.py` | **PARTIAL** | **P2** | ML | Script exists but training logic is stub; ONNX infra fully done (M8) | Real train pipeline + dataset build; ONNX conversion; reproducible artifacts; eval report |
| ML policy integration (signal → grid param adjustment) | `docs/12_ML_SPEC.md` §12.5 | `src/grinder/ml/` | **PLANNED** | **P2** | ML | MlSignalSnapshot computed but not consumed by AdaptiveGridPolicy | AdaptiveGridPolicy reads MlSignalSnapshot (guarded); canary in SHADOW; tests; determinism fixture |
| ML drift detection / monitoring | `docs/12_ML_SPEC.md` §12.11 | — | **PLANNED** | **P2** | ML | Not implemented | Drift metrics + alert; baseline snapshot; runbook triage |
| Production trading loop (DataConnector → LiveEngineV0) | `docs/03_ARCHITECTURE.md` §3.6 | `scripts/run_trading.py` | **DONE** | **P2** | Core | PR-P2-LOOP-0 through LOOP-3: `run_trading.py` (752 lines). HA-gated (LeaderElector + ACTIVE-only). Selectable ExchangePort (`--exchange-port futures` for BinanceFuturesPort). `--mainnet`, `--armed`, `--paper-size-per-level` knobs. Graceful shutdown. Fixture mode. C3 canary PASSED on mainnet. | All exit criteria met (real port, HA gating, Runbook 32 ceremony) |
| Feature store (offline repo, versioning, lineage) | `docs/12_ML_SPEC.md` §12.8 / `docs/18_FEATURE_STORE_SPEC.md` | `scripts/build_dataset.py`, `scripts/verify_dataset.py` | **PARTIAL** | **P2** | Data | Dataset artifact pipeline done (M8-04); FeatureStore module/service planned | FeatureStore module/interface (read/write/list); lineage/versioning; tests; runbook |
| Advanced features (OFI, CVD, VAMP, multi-timeframe) | `docs/05_FEATURE_CATALOG.md` | `src/grinder/features/` | **PARTIAL** | **P2** | Features | ~10 core features done; momentum, OFI, CVD, multi-TF not built | OFI/CVD/VAMP + multi-TF aggregators added; tests; determinism fixture |
| Multi-venue (Bybit, OKX, COIN-M) | `docs/02_DATA_SOURCES.md` | `src/grinder/execution/` | **DEFERRED** | **P2** | Execution | Binance USDT-M only; deferred to M9 post-launch (ADR-066) | New port + e2e smoke + budget/kill-switch compatibility; ADR/ROADMAP entry criteria met |
| Data quality (GapDetector, outlier filtering) | `docs/02_DATA_SOURCES.md` | `src/grinder/data/quality.py`, `src/grinder/data/quality_metrics.py`, `src/grinder/data/quality_engine.py` | **DONE** | **P1** | Data | PR1: detect-only classes + metrics. PR2: wired into LiveFeed. PR3: dq_blocking gate + 3 block reasons. PR4 (Launch-04): alerts + triage runbook + validator. | All exit criteria met |

---

## Status Legend

| Status | Meaning |
|--------|---------|
| **DONE** | Spec matches code — no gap |
| **PARTIAL** | Core functionality implemented; spec describes additional planned features |
| **PLANNED** | Spec exists; no implementation yet |
| **DEFERRED** | Explicitly deferred to a future milestone (with ADR or roadmap reference) |

### Priority Legend

| Priority | Meaning |
|----------|---------|
| **P0** | Launch blocker — must fix before first ACTIVE window |
| **P1** | Launch hardening — do ASAP post-launch / before widening scope |
| **P2** | Post-launch / target state |

### Launch-Blockers Checklist (P0)

All P0 items must be true before the first ACTIVE window on mainnet:

- [x] Operator procedure for enabling ACTIVE (Runbook 22) + ceremony artifacts
- [x] E2e smoke validating `/healthz`, `/metrics` SSOT contract, `/readyz`, graceful stop (Launch-01)
- [x] Rollback in <5 min with verifiable metric signals (RB22 §8)
- [x] Kill-switch drill + documented reset path (restart only) (RB22 §7)
- [x] Hard mainnet write gate (`ALLOW_MAINNET_TRADE`) blocks execute without flag (4 code locations)
- [x] Budget/limits per day and per run enforced in code (5 env vars, `run_live_reconcile.py:207-232`)
- [x] Observability: key metrics visible during ACTIVE hold period (RB22 §6 watchlist)

**Result: all P0 items closed by Launch-01 (PR #173) and Launch-02 (PR #174).**

Post-launch P0 blocker (RISK-EE-1) closed by PR #316:
- [x] Emergency Exit Executor (§10.6): cancel + MARKET reduce_only + bounded verify + FSM EMERGENCY → PAUSED

No remaining P0 gaps in the index — all P1 items now DONE; remaining gaps are P2 (target state).

---

## Fully Implemented (no gap)

These specs match their implementation — not listed in the gap table above:

- **Adaptive Grid Policy v1/v2** — `docs/17_ADAPTIVE_SMART_GRID_V1.md` ↔ `src/grinder/policies/grid/adaptive.py` (734 lines)
- **Prefilter / Top-K** — `docs/04_PREFILTER_SPEC.md` ↔ `src/grinder/prefilter/`
- **ML ONNX contracts (M8-01/02)** — `docs/12_ML_SPEC.md` §12.2 ↔ `src/grinder/ml/onnx/` (1,817 lines)
- **ML model registry & promotion** — `docs/12_ML_SPEC.md` §12.10 ↔ `ml/registry/` + `scripts/promote_ml_model.py`
- **Constraint Provider (M7-05)** — `docs/09_EXECUTION_SPEC.md` ↔ `src/grinder/execution/constraint_provider.py`
- **Drawdown Guard v1** — `docs/10_RISK_SPEC.md` §10.4 ↔ `src/grinder/risk/drawdown_guard_v1.py`
- **Auto-Sizer / DD Allocator** — `docs/10_RISK_SPEC.md` ↔ `src/grinder/sizing/`
- **GitHub Workflow** — `docs/14_GITHUB_WORKFLOW.md` ↔ `.github/workflows/`
- **Emergency Exit Executor** — `docs/10_RISK_SPEC.md` §10.6 ↔ `src/grinder/risk/emergency_exit.py` (190 lines) + port + metrics. Wired to FSM EMERGENCY. RISK-EE-1 (#316).
- **Account Syncer** — `docs/15_ACCOUNT_SYNC_SPEC.md` ↔ `src/grinder/account/syncer.py` (247 lines). Positions + open orders. Launch-15 (#224-228). Mainnet pipeline: #329 (port protocol), #330 (5s throttle), #331 (BinanceFuturesPort.fetch_account_snapshot).
- **NATR volatility contract** — `docs/23_NATR_CONTRACT.md` ↔ `src/grinder/features/natr.py`. TRD-3a (#304).
- **NATR-spacing hook** — `docs/24_NATR_SPACING_HOOK.md` ↔ `src/grinder/policies/grid/adaptive.py` `compute_step_bps()`. TRD-3b (#305).
- **Safety envelope (gate ordering)** — `docs/20_SAFETY_ENVELOPE.md` ↔ `src/grinder/live/engine.py` gate pipeline. TRD-1 (#301).
- **Policy contract (GridPolicy/GridPlan)** — `docs/22_POLICY_CONTRACT.md` ↔ `src/grinder/policies/grid/`. TRD-2 (#302).
