# DOCS INDEX — Grinder

Where to find information:

- **Implementation truth:** `docs/STATE.md`
- **Decisions:** `docs/DECISIONS.md`
- **Plan + progress (active):** `docs/POST_LAUNCH_ROADMAP.md`

---

## Documentation Taxonomy (SSOT-first)

Use this classification before relying on any doc in reviews/implementation:

- **ACTIVE (operational SSOT):**
  - `docs/STATE.md`
  - `docs/DECISIONS.md`
  - `docs/POST_LAUNCH_ROADMAP.md`
  - `docs/27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md` (grid_v2 contract line)
- **CANDIDATE (valuable ideas, not binding contracts yet):**
  - Feature/spec docs that describe future work unless explicitly marked implemented in `STATE.md`
- **HISTORICAL (context preserved, not current contract):**
  - `docs/ROADMAP.md` (historical milestone snapshot)
  - `docs/GAPS.md` (historical gap snapshot)
  - legacy branches/spec lines superseded in `STATE.md`/`DECISIONS.md`

If documents conflict, trust order is:
`STATE.md` -> `DECISIONS.md` -> active roadmap/spec.

---

## Core orientation
- `docs/00_PRODUCT.md` — goals, scope, milestone checklists (target spec)
- `docs/STATE.md` — what is implemented *right now*
- `docs/DECISIONS.md` — why key choices were made
- `docs/POST_LAUNCH_ROADMAP.md` — current post-launch priorities and backlog
- `docs/ROADMAP.md` — historical milestone tracker (reference only)

---

## Glossary & data
- `docs/01_GLOSSARY.md`
- `docs/02_DATA_SOURCES.md`
- `docs/05_FEATURE_CATALOG.md`
- `docs/15_CONSTANTS.md`

---

## Architecture & system behavior
- `docs/03_ARCHITECTURE.md`
- `docs/08_STATE_MACHINE.md`

---

## Pipeline specs (target behavior)
- `docs/04_PREFILTER_SPEC.md`
- `docs/06_TOXICITY_SPEC.md`
- `docs/07_GRID_POLICY_LIBRARY.md`
- `docs/09_EXECUTION_SPEC.md`
- `docs/10_RISK_SPEC.md`
- `docs/11_BACKTEST_PROTOCOL.md`
- `docs/12_ML_SPEC.md`
- `docs/18_FEATURE_STORE_SPEC.md` — ML training data governance (M8-04)
- `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md` — meta-controller (regime, step, reset)
- `docs/13_OBSERVABILITY.md`
- `docs/14_GITHUB_WORKFLOW.md`

---

## Contracts & hooks
- `docs/22_POLICY_CONTRACT.md` — GridPolicy + GridPlan interface contract (TRD-2)
- `docs/23_NATR_CONTRACT.md` — NATR(14) computation contract
- `docs/24_NATR_SPACING_HOOK.md` — NATR-to-spacing hook (TRD-3b)
- `docs/25_LIVE_GRID_PLANNER_SPEC.md` — LiveGridPlanner (exchange-truth reconciliation)

---

## Smart Grid (versioned specs)
- `docs/smart_grid/README.md` — overview + version matrix
- `docs/smart_grid/ROADMAP.md` — feature roadmap by version
- `docs/smart_grid/SPEC_V1_0.md` — v1.0 base (L1-only, deterministic regime)
- `docs/smart_grid/SPEC_V1_1.md` — v1.1 (+FeatureEngine, NATR/ATR)
- `docs/smart_grid/SPEC_V1_2.md` — v1.2 (+AdaptiveGridPolicy, dynamic sizing)
- `docs/smart_grid/SPEC_V1_3.md` — v1.3 (+Top-K v1, feature-based selection)
- `docs/smart_grid/SPEC_V2_0.md` — v2.0 (L2-aware, partial fills, DD allocator)
- `docs/smart_grid/SPEC_V3_0.md` — v3.0 (multi-venue, full production)

---

## Live Trading
- `src/grinder/live/` — Live trading modules
  - **Write-path (ADR-036):**
    - `config.py` — LiveEngineConfig (arming, mode, kill-switch, whitelist)
    - `engine.py` — LiveEngineV0 (safety gates, intent classification, hardening chain)
  - **Read-path (ADR-037):**
    - `types.py` — LiveFeaturesUpdate, WsMessage, BookTickerData, LiveFeedStats
    - `feed.py` — LiveFeed pipeline (WS → Snapshot → FeatureEngine → features)
- `src/grinder/connectors/binance_ws.py` — BinanceWsConnector (bookTicker stream)
- `src/grinder/connectors/binance_user_data_ws.py` — FuturesUserDataWsConnector (user-data stream)
- `src/grinder/execution/futures_events.py` — FuturesOrderEvent, FuturesPositionEvent, UserDataEvent
- `src/grinder/reconcile/` — Reconciliation module (LC-09b, LC-10, LC-11, LC-12, LC-13, LC-14a, LC-14b)
  - `types.py` — ExpectedOrder, ObservedOrder, Mismatch, MismatchType
  - `expected_state.py` — ExpectedStateStore (ring buffer + TTL)
  - `observed_state.py` — ObservedStateStore (stream + REST)
  - `engine.py` — ReconcileEngine (mismatch detection)
  - `metrics.py` — ReconcileMetrics (Prometheus export)
  - `snapshot_client.py` — SnapshotClient (REST polling)
  - `config.py` — ReconcileConfig, RemediationAction
  - `remediation.py` — RemediationExecutor (active remediation, LC-10)
  - `runner.py` — ReconcileRunner (wiring + routing policy, LC-11)
  - `identity.py` — OrderIdentityConfig, generate/parse client_order_id (LC-12)
  - `audit.py` — AuditWriter, AuditEvent (JSONL audit trail, LC-11b)
  - `price_getter.py` — PriceGetter (REST price fetch, LC-14b)
- `src/grinder/live/reconcile_loop.py` — ReconcileLoop for periodic reconciliation (LC-14a, LC-14b)
- See: `docs/STATE.md` §Live Trading, `docs/DECISIONS.md` ADR-036/ADR-037/ADR-041-050

---

## Operations
- `docs/HOW_TO_OPERATE.md` — operator's guide (includes Release Checklist v1)
- `docs/OBSERVABILITY_STACK.md` — Prometheus + Grafana setup
- `docs/runbooks/` — operational runbooks:
  - `01_STARTUP_SHUTDOWN.md` — starting/stopping the system
  - `02_HEALTH_TRIAGE.md` — quick health checks
  - `03_METRICS_DASHBOARDS.md` — Prometheus metrics and Grafana
  - `04_KILL_SWITCH.md` — kill-switch events and recovery
  - `05_SOAK_GATE.md` — running soak tests
  - `06_ALERT_RESPONSE.md` — responding to alerts
  - `07_HA_OPERATIONS.md` — HA deployment, failover, rolling restart
  - `08_SMOKE_TEST_TESTNET.md` — testnet smoke test procedure
  - `09_MAINNET_TRADE_SMOKE.md` — Spot mainnet smoke test procedure (LC-08b)
  - `10_FUTURES_MAINNET_TRADE_SMOKE.md` — Futures USDT-M mainnet smoke test (LC-08b-F)
  - `11_RECONCILIATION_TRIAGE.md` — Reconciliation mismatch triage (LC-09b)
  - `12_ACTIVE_REMEDIATION.md` — Active remediation operations (LC-10)
  - `13_OPERATOR_CEREMONY.md` — Operator ceremony for safe enablement (LC-11)
  - `14_RECONCILE_E2E_SMOKE.md` — E2E reconcile→remediate smoke test (LC-13)
  - `15_ENABLEMENT_CEREMONY.md` — Staged enablement ceremony for ReconcileLoop (LC-15a)
  - `16_RECONCILE_ALERTS_SLOS.md` — Reconcile alerts, SLOs, and triage (LC-15b)
  - `17_LIVE_TRADE_CONNECTOR_ENABLEMENT.md` — LIVE_TRADE connector enablement (LC-22)
- `scripts/smoke_live_testnet.py` — Spot smoke test script (testnet/mainnet)
- `scripts/smoke_futures_mainnet.py` — Futures USDT-M smoke test script (mainnet)
- `scripts/smoke_reconcile_e2e.py` — E2E reconcile→remediate smoke harness (LC-13)
- `scripts/smoke_live_reconcile_loop.py` — ReconcileLoop wiring smoke test (LC-14a)
- `scripts/smoke_live_reconcile_loop_real_sources.py` — ReconcileLoop real sources smoke test (LC-14b)
- `scripts/smoke_enablement_ceremony.py` — Enablement ceremony smoke test (LC-15a)
- `scripts/smoke_real_sources_detect_only.py` — Credentialed real-source smoke test (LC-17)
- `scripts/smoke_metrics_contract.py` — Metrics contract validation smoke (LC-16)
- `scripts/smoke_lc22_live_trade.py` — LIVE_TRADE connector smoke test (LC-22)
- `monitoring/grafana/dashboards/grinder_overview.json` — Grafana dashboard: Overview (LC-16)
- `monitoring/grafana/dashboards/grinder_reconcile.json` — Grafana dashboard: Reconcile & Remediation (LC-16)

---

## Spec vs reality
- Specs define **target behavior**.
- `STATE.md` defines **current behavior**.
- If a spec conflicts with `STATE.md`, treat the spec as *planned* unless `STATE.md` says implemented.
