# 38. Autonomous Multi-Symbol Implementation Plan

**Status:** ACTIVE — phased execution plan. No implementation changes in this document.

**Parent spec:** `37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md` (architectural SSOT)

---

## 1. Purpose

This document converts the architectural spec (doc 37) into an actionable, PR-sized implementation plan.

It answers:
- what we build first and what depends on what
- what stays shadow-only before activation
- what safety gates must be proven before each escalation
- what must not be changed prematurely
- how we avoid losing the current stable single-symbol baseline

**Relationship to other documents:**
- `37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md` — defines the *what* (target architecture, state machine, invariants). This plan defines the *how* and *when*.
- `36_MULTI_SYMBOL_ELIGIBILITY_ML_INTEGRATION_V1.md` — Phases 1-3b implementation detail (ShadowSelector, ActiveSelector, ML scoring). Already delivered. This plan builds on top.
- `STATE.md` — current implementation truth. This plan must not contradict it.
- `POST_LAUNCH_ROADMAP.md` — P1 hardening (DONE) and P2 backlog. This plan is a structured subset of P2.

---

## 2. Current baseline

### Implemented and stable (build-on, do not break)

| Component | Location | Notes |
|-----------|----------|-------|
| Grid_v2 state machine | `src/grinder/grid_v2/state.py` | Pure SM, 78 tests. Single-symbol. |
| Grid_v2 adapter | `src/grinder/grid_v2/adapter.py` | CID scheme, fill translation. Single-symbol. |
| Grid_v2 bridge | `src/grinder/grid_v2/bridge.py` | Wires SM + adapter into execution pipeline. Single-symbol. |
| Grid_v2 shadow | `src/grinder/grid_v2/shadow.py` | Isolated shadow runner. Single-symbol. |
| LiveEngineV0 | `src/grinder/live/engine.py` | All orchestration. `_grid_v2_symbol` = one symbol per engine. |
| EventLedger Phase 2 | `src/grinder/account/event_ledger.py` | Trusted read model, degraded boundary. |
| Sync reconciler | `src/grinder/live/engine.py` | Three-layer (theoretical/effective/actual). |
| ShadowSelector | `src/grinder/selection/shadow_selector.py` | Phase 1 delivered. 17 tests. |
| ActiveSelector | `src/grinder/selection/active_selector.py` | Phase 2 delivered. 14 tests. Hysteresis, hold timers. |
| ConstraintProvider | `src/grinder/execution/constraint_provider.py` | Loads `step_size`, `min_qty`, `tick_size` from exchangeInfo. **Missing: `minNotional`.** |
| BinanceFuturesPort | `src/grinder/execution/binance_futures_port.py` | Mainnet-ready. `refresh_ts_offset()` for -1021. |
| Risk gates | `src/grinder/risk/` | Per-symbol inventory, loss, consecutive loss guard. |
| PortfolioRiskManager | `src/grinder/risk/` | Primitives (gross/net caps, concentration). **Not full autonomous allocator.** |
| Fatal abort cleanup | `scripts/run_trading.py` | `finalize_and_cleanup()` on ANY post-start exit. |
| Preflight | `src/grinder/live/preflight.py` | DNS, clock sync, WS bootstrap, config consistency. |
| One-sided grid mode | `src/grinder/grid_v2/` | Cancels opposite entries on branch. |
| Reduce-only budget guard v2 | `src/grinder/live/engine.py` | Direction-aware, reservation model. |
| Burst churn suppression | `src/grinder/live/engine.py` | One-shot flag, reset on new fill. |

### Partially implemented

| Component | What exists | What's missing |
|-----------|------------|----------------|
| Grid_v2 multi-symbol | `_grid_v2_symbol` (one per engine) | Multi-grid requires per-symbol bridges or multiple engines |
| Symbol constraints | ConstraintProvider: `step_size`, `min_qty`, `tick_size` | `minNotional` not parsed from exchangeInfo |
| Active selector | Operator universe + hysteresis | No autonomous discovery from full exchange universe |
| Graceful exit on rotation | `graceful_exit_only` blocks new entries | No explicit flatten-on-deactivation policy; no resource release |
| Portfolio risk | Gross/net caps, concentration primitives | No position-budget allocator for multi-symbol |

### Not implemented yet

| Component | Description |
|-----------|-------------|
| Autonomous universe discovery | Auto-scan exchangeInfo for tradeable symbols |
| Symbol tuning solver | Compute legal grid config from constraints + price + risk |
| Rotation controller | State machine for symbol lifecycle transitions |
| Per-symbol grid bridge manager | Multiple bridges per engine or multiple engines |
| Multi-symbol cleanup orchestration | Coordinated deactivation across symbols |

### Explicitly out of scope for this plan

- Multi-venue routing (Bybit, OKX, COIN-M) — deferred per ADR-066
- ML policy integration (signal → grid params) — P2 separate track
- SOR AMEND support — requires order state tracking
- Full backtest walk-forward engine — P2 separate track

---

## 3. Target outcome

When this plan is fully executed, the system will:

1. **Discover** tradeable USDT-M perpetual symbols from Binance exchangeInfo automatically
2. **Prefilter** candidates through hard gates (spread, volume, OI, blacklist)
3. **Tune** each eligible symbol: compute a legal grid_v2 config or declare NO_GO with reason
4. **Score and select** Top-K among TUNED symbols with hysteresis and churn budget
5. **Trade** active symbols via grid_v2 (one bridge per symbol)
6. **Rotate** symbols in/out based on continuous scoring: graceful exit for non-flat, cooldown before re-entry
7. **Deactivate** safely: block entries, wait for FLAT, verify clean, release resources
8. **Operate continuously** 24/7 without manual restarts or symbol list updates

The plan is phased so that each step adds observable value while preserving the stable single-symbol baseline.

---

## 4. Phased implementation map

```
Phase B1: Extend metadata (minNotional)
    │
    ▼
Phase B2: Tuning solver (shadow mode)
    │
    ▼
Phase B3: Tuning observability + readiness cache
    │
    ▼
Phase C1: Active rotation with operator universe
    │
    ▼
Phase C2: Safe symbol lifecycle / deactivation
    │
    ▼
Phase D: Autonomous discovery + continuous loop
```

### Phase B1: Extend metadata snapshot

**Goal:** ConstraintProvider exposes `minNotional` so the tuning solver (Phase B2) can validate order sizing.

**What changes:**
- `SymbolConstraints` gains `min_notional: Decimal` field
- `parse_exchange_info()` parses `MIN_NOTIONAL` or `NOTIONAL` filter from exchangeInfo
- Existing cache format extended (backward-compatible: missing field defaults to `Decimal("0")`)

**What stays unchanged:**
- All existing grid_v2 code (reads `step_size`, `min_qty`, `tick_size` — no change)
- LiveEngineV0 orchestration (single-symbol)
- All selectors, risk gates, EventLedger

**Safety:** Pure additive. No runtime behavior change.

### Phase B2: Tuning solver in shadow

**Goal:** Given a symbol and its constraints + current price, compute a legal grid_v2 config or declare NO_GO. Shadow-only: logs results, does not affect trading.

**What changes:**
- New module: `src/grinder/tuning/solver.py`
  - `TuningResult(symbol, status, order_size, tick_size, step_size, no_go_reason, config)`
  - `TuningSolver.solve(symbol, constraints, price, risk_config, grid_config) -> TuningResult`
  - Deterministic: same inputs → same output
- Wired into `run_trading.py` startup (or post-preflight): logs which symbols would be tradeable
- No runtime behavior change. No dispatch mutation.

**What stays unchanged:**
- LiveEngineV0 orchestration
- Grid_v2 bridge (still single-symbol)
- All selectors, risk gates

**Safety:** Read-only computation. No exchange writes. No state mutation.

### Phase B3: Tuning observability + readiness cache

**Goal:** Operator can see tuning results in metrics and logs. Tuned configs cached for selector consumption.

**What changes:**
- Tuning solver emits log signals: `SYMBOL_TUNED`, `SYMBOL_NO_GO` (per doc 37 Section 10)
- Prometheus metrics: `grinder_tuning_result_total{status}`, `grinder_tuning_no_go_total{reason}`
- `TuningCache`: in-memory dict of `symbol -> TuningResult` with TTL
- Selector can optionally filter input to TUNED-only symbols (shadow: log what would change)

**What stays unchanged:**
- LiveEngineV0 dispatch loop
- Actual active set mutation (still operator-controlled)

**Safety:** Observability-only additions. No dispatch change.

### Phase C1: Active rotation with operator universe

**Goal:** System auto-tunes configs for operator-provided symbols and rotates the active set. Grid_v2 remains single-symbol per engine instance.

**Ownership model (Phase C):**

A new `SymbolOrchestrator` (separate from `LiveEngineV0`) is the sole owner of:
- **Ranking:** calls tuning cache + ActiveSelector
- **Active set:** decides which symbols are ACTIVE, GRACEFUL_EXIT_ONLY, COOLDOWN
- **Deactivation decision:** triggers graceful exit on removal
- **Per-symbol engine lifecycle:** starts/stops per-symbol engine instances

`LiveEngineV0` remains a single-symbol engine. It does NOT know about rotation, other symbols, or the active set. The orchestrator spawns one engine per active symbol and manages their lifecycle externally.

```
SymbolOrchestrator (new)
  ├── RotationController (state machine per symbol)
  ├── TuningCache (from Phase B)
  ├── ActiveSelector (existing)
  └── per-symbol engine instances:
        ├── LiveEngineV0 for BTCUSDT
        ├── LiveEngineV0 for ETHUSDT
        └── ...
```

**What changes:**
- `src/grinder/rotation/` — `SymbolLifecycle` (pure SM) + `RotationController` (reconcile/action-intent core). Landed in C1a.
- `src/grinder/orchestration/` — `SymbolOrchestrator` (engine lifecycle, wiring). Lands in C1b.
- ActiveSelector receives only TUNED symbols as input (not raw universe)
- Rotation controller: owns symbol state machine (doc 37 Section 5)
- Orchestrator on activation: spawn engine instance with tuned config
- Orchestrator on removal (FLAT): stop engine, release resources
- Orchestrator on removal (non-FLAT): signal engine `graceful_exit_only`, wait for FLAT, then stop
- Operator still provides `--symbols` universe

**What stays unchanged:**
- `LiveEngineV0` internals (no rotation logic added to engine.py)
- Universe source (operator CLI, not auto-discovery)
- Single-symbol grid_v2 per engine instance

**Safety:** Rotation bounded by `MAX_CHANGES_PER_CYCLE`. Hysteresis prevents churn. `graceful_exit_only` protects open inventory. Engine isolation: crash of one symbol engine does not affect others.

### Phase C2: Safe symbol lifecycle / deactivation orchestration

**Goal:** Prove the full deactivation path: entry-block → exit-only → FLAT verification → resource release → COOLDOWN.

**What changes:**
- Deactivation sequence formalized and tested (unit + bounded live)
- Cleanup verification: no orphan orders, no orphan position after deactivation
- Cooldown timer before symbol can re-enter ELIGIBLE
- Resource release: grid bridge teardown, EventLedger scope clearing

**What stays unchanged:**
- Single-symbol grid_v2 per engine (deactivation operates on one symbol at a time)

**Safety:** Deactivation is the highest-risk transition. Must be proven with bounded live runs before Phase D.

### Phase D: Autonomous discovery + continuous loop

**Goal:** Full 24/7 autonomous operation. Universe auto-discovered from exchangeInfo. No `--symbols` required.

**What changes:**
- UniverseProvider: periodic exchangeInfo fetch, USDT-M perpetual filter, blacklist exclusion
- Continuous loop: discover → prefilter → tune → score → select → trade → deactivate → loop
- Per-symbol grid_v2 bridges (architecture fork decision required — see Section 8)
- Operator can still override with `--symbols` (restricts universe to subset)

**Dependencies:** Phases B1-B3, C1, C2 all proven. Architecture fork decided.

**Safety:** Full rotation active. All 8 safety invariants from doc 37 enforced. Stop-the-line rules active.

---

## 5. PR-by-PR plan

### PR-B1: Extend ConstraintProvider with minNotional

**Title:** `feat(constraints): parse minNotional from exchangeInfo`
**Goal:** `SymbolConstraints` exposes `min_notional` for downstream tuning.
**Files touched:**
- `src/grinder/execution/constraint_provider.py` — add `parse_min_notional_filter()`, extend `parse_exchange_info()`
- `src/grinder/execution/engine.py` — add `min_notional: Decimal = Decimal("0")` to `SymbolConstraints`
- `tests/unit/test_constraint_provider.py` — new tests for MIN_NOTIONAL parsing, missing filter, backward compat
**Dependencies:** None (first PR).
**Tests required:**
- Parse MIN_NOTIONAL filter from fixture exchangeInfo
- Parse NOTIONAL filter (alternate filter name)
- Missing filter → `min_notional = Decimal("0")` (backward compat)
- Existing cache without min_notional field loads without error
- Round-trip: parse → cache → load preserves min_notional
**Observability:** None (pure data extension).
**Proof bundle:** pytest, ruff check, ruff format, mypy.
**Must stay unchanged:** All existing ConstraintProvider consumers. Grid_v2 code. LiveEngineV0.

### PR-B2: Tuning solver core

**Title:** `feat(tuning): deterministic symbol tuning solver`
**Goal:** Pure function: constraints + price + config → TuningResult (TUNED or NO_GO with reason).
**Files touched:**
- `src/grinder/tuning/__init__.py` (new)
- `src/grinder/tuning/solver.py` (new) — `TuningSolver`, `TuningResult`, `TuningConfig`
- `tests/unit/test_tuning_solver.py` (new) — deterministic tests
**Dependencies:** PR-B1 (needs `min_notional` in SymbolConstraints).
**Tests required:**
- TUNED: legal config computed for typical symbol (BTCUSDT-like, PIPPINUSDT-like)
- NO_GO: `NOTIONAL_TOO_LOW` (tiny symbol, high min_notional)
- NO_GO: `POSITION_EXCEEDS_CAP` (large order_size * max_inv > budget)
- NO_GO: `TICK_SIZE_UNAVAILABLE`
- NO_GO: `BLACKLISTED`
- Determinism: same inputs → same TuningResult
- Edge: price exactly at notional boundary
- Edge: step_size larger than min_qty_for_notional
**Observability:** None (pure computation, no metrics yet).
**Proof bundle:** pytest, ruff, mypy. Show TuningResult for at least 3 symbols.
**Must stay unchanged:** No LiveEngineV0 changes. No selector changes. No grid_v2 changes.

### PR-B3a: Tuning solver wiring (shadow startup)

**Title:** `feat(tuning): wire tuning solver into startup (shadow log only)`
**Goal:** On startup, run tuning solver for all `--symbols` and log results. No dispatch change.
**Files touched:**
- `scripts/run_trading.py` — call solver after preflight, log TuningResult per symbol
- `src/grinder/tuning/solver.py` — accept ConstraintProvider + price source
**Dependencies:** PR-B2.
**Tests required:**
- Startup with 3 symbols → 3 TuningResult log lines
- One NO_GO symbol → logged but not fatal
- No symbols → no solver invocation (no crash)
**Observability:** Log signals: `SYMBOL_TUNED`, `SYMBOL_NO_GO` with reason.
**Proof bundle:** pytest, ruff, mypy. Show startup log with tuning results.
**Must stay unchanged:** Runtime dispatch loop. Grid_v2 behavior. Selector behavior.

### PR-B3b: Tuning metrics + TuningCache

**Title:** `feat(tuning): tuning metrics and in-memory readiness cache`
**Goal:** Tuning results exported as Prometheus metrics. Cached for selector consumption.
**Files touched:**
- `src/grinder/tuning/cache.py` (new) — `TuningCache` (dict + TTL)
- `src/grinder/tuning/metrics.py` (new) — Prometheus counters/gauges
- `src/grinder/observability/metrics_contract.py` — register new metric names
- `tests/unit/test_tuning_cache.py` (new)
- `tests/unit/test_tuning_metrics.py` (new)
**Dependencies:** PR-B3a.
**Tests required:**
- Cache stores and retrieves TuningResult
- Cache TTL expiry
- Metrics increment on TUNED / NO_GO
- Metrics contract: new metric names in SSOT
**Observability:** `grinder_tuning_result_total{status}`, `grinder_tuning_no_go_total{reason}`.
**Proof bundle:** pytest, ruff, mypy. Show metrics output.
**Must stay unchanged:** Dispatch loop. Active selector behavior.

### PR-C1a: Rotation controller core

**Title:** `feat(rotation): symbol lifecycle state machine + rotation controller`
**Goal:** Pure rotation controller with reconcile/action-intent semantics (doc 37 Sections 5-6). No engine wiring.
**Files touched:**
- `src/grinder/rotation/__init__.py` (new)
- `src/grinder/rotation/state_machine.py` (new) — `SymbolState` (9 states), `SymbolLifecycle`, `VALID_TRANSITIONS`, `InvalidTransitionError`
- `src/grinder/rotation/controller.py` (new) — `RotationController` (reconcile desired set, bounded changes, graceful exit, action intents)
- `tests/unit/test_rotation_state_machine.py` (new)
- `tests/unit/test_rotation_controller.py` (new)
**Dependencies:** None (pure logic).
**Tests required:**
- All 9 states, all valid/invalid transitions, reason codes, determinism
- Controller: activation path, graceful exit for non-flat, finalize on flat, cooldown blocks re-entry, bounded changes, min hold cycles, no churn, top_k respected, replacement deferred for non-flat
**Observability:** None (pure logic, metrics added in wiring PR).
**Proof bundle:** pytest, ruff, mypy.
**Must stay unchanged:** No engine changes. No selector changes.

### PR-C1b: Symbol orchestrator wiring

**Title:** `feat(orchestration): symbol orchestrator with rotation controller`
**Goal:** `SymbolOrchestrator` owns per-symbol engine lifecycle. Uses `RotationController` (from C1a) for state transitions. `LiveEngineV0` is not modified.
**Files touched:**
- `src/grinder/orchestration/__init__.py` (new)
- `src/grinder/orchestration/orchestrator.py` (new) — `SymbolOrchestrator` (spawns/stops per-symbol engines, executes RotationAction intents)
- `scripts/run_trading.py` — optional `--orchestrator` mode (feature-flagged, default OFF)
- `tests/unit/test_symbol_orchestrator.py` (new)
**Dependencies:** PR-C1a, PR-B3b.
**Tests required:**
- Orchestrator spawns engine on TUNED → ACTIVE transition
- Orchestrator stops engine on COOLDOWN transition (after FLAT)
- Orchestrator signals graceful_exit_only on ACTIVE → GRACEFUL_EXIT_ONLY
- Controller feeds only TUNED symbols to scorer
- Symbol transitions through full lifecycle
- `MAX_CHANGES_PER_CYCLE` enforced
- `MIN_HOLD_CYCLES` enforced
- Fail-safe: controller error → retain previous active set, no engine start/stop
- Engine isolation: one engine failure does not affect orchestrator or other engines
**Observability:** `SELECTOR_CYCLE_COMPLETED active=N eligible=M scored=L` (extend existing signal). `ORCHESTRATOR_ENGINE_STARTED symbol=X`, `ORCHESTRATOR_ENGINE_STOPPED symbol=X`.
**Proof bundle:** pytest, ruff, mypy. Show orchestrator cycle with tuning filter + engine lifecycle.
**Must stay unchanged:** `LiveEngineV0` internals (no rotation logic in engine.py). Grid_v2 single-symbol behavior. Existing selector metrics.

### PR-C2a: Graceful deactivation sequence

**Title:** `feat(orchestration): graceful symbol deactivation with cleanup verification`
**Goal:** When orchestrator removes a symbol: signal engine graceful_exit_only → FLAT wait → stop engine → cleanup verify → COOLDOWN.
**Files touched:**
- `src/grinder/orchestration/deactivation.py` (new) — `DeactivationSequence`
- `src/grinder/orchestration/orchestrator.py` — deactivation hook on symbol removal
- `tests/unit/test_deactivation_sequence.py` (new)
**Dependencies:** PR-C1b.
**Tests required:**
- Non-flat symbol → GRACEFUL_EXIT_ONLY (entries blocked, exits maintained)
- Flat symbol → immediate COOLDOWN
- Cleanup verification: no orphan orders after FLAT
- Resource release: bridge teardown after deactivation
- Cooldown timer prevents premature re-entry
**Observability:** `SYMBOL_DEACTIVATED`, `SYMBOL_GRACEFUL_EXIT_ONLY`, `SYMBOL_COOLDOWN_ENTERED`.
**Proof bundle:** pytest, ruff, mypy. Show deactivation log sequence.
**Must stay unchanged:** Single-symbol grid_v2 invariants. EventLedger trust boundary.

### PR-C2b: Bounded live deactivation proof

**Title:** `test(rotation): bounded live deactivation ceremony`
**Goal:** Prove deactivation works on real exchange with bounded run.
**Files touched:**
- `scripts/smoke_deactivation_ceremony.py` (new) — bounded test: activate → fill → remove → verify FLAT → verify clean
- `docs/runbooks/` — deactivation triage runbook
**Dependencies:** PR-C2a.
**Tests required:**
- Ceremony: single symbol, activate, get at least 1 fill, trigger removal, wait for FLAT, verify no orphan orders
- Evidence: exchange state before/after deactivation
**Observability:** Full log trace of deactivation sequence.
**Proof bundle:** Ceremony log output, exchange state screenshots, no orphan orders.
**Must stay unchanged:** All existing live trading behavior for non-deactivating symbols.

### PR-D1: Universe provider

**Title:** `feat(universe): autonomous symbol discovery from exchangeInfo`
**Goal:** Periodically fetch exchangeInfo and produce raw candidate list.
**Files touched:**
- `src/grinder/universe/__init__.py` (new)
- `src/grinder/universe/provider.py` (new) — `UniverseProvider`
- `tests/unit/test_universe_provider.py` (new)
**Dependencies:** PR-B1 (extended ConstraintProvider for metadata).
**Tests required:**
- Fetch produces list of USDT-M perpetual symbols
- Non-TRADING status excluded
- Blacklist applied
- Refresh respects `UNIVERSE_REFRESH_S` interval
- Fetch failure → retain previous universe (fail-safe)
**Observability:** `SYMBOL_DISCOVERED count=N`.
**Proof bundle:** pytest, ruff, mypy. Show discovered universe for testnet.
**Must stay unchanged:** Everything. Pure additive module.

### PR-D2: Continuous loop integration

**Title:** `feat(orchestration): continuous autonomous multi-symbol loop`
**Goal:** Wire universe provider + tuning + rotation + trading into single continuous loop.
**Files touched:**
- `src/grinder/orchestration/loop.py` (new) — `OrchestrationLoop`
- `scripts/run_trading.py` — add `--autonomous` flag to enable continuous loop
- Integration with existing per-symbol engine instances or bridge manager (architecture fork decision)
**Dependencies:** All prior PRs. Architecture fork decision (Section 8).
**Tests required:**
- End-to-end: discover → prefilter → tune → score → select → activate → trade
- Rotation: symbol drops below threshold → graceful exit → replacement activated
- Safety: all 8 invariants from doc 37 Section 8 enforced
- Stop-the-line: churn budget exceeded → loop pauses rotation
**Observability:** Full signal set from doc 37 Section 10.
**Proof bundle:** pytest, ruff, mypy. Bounded live run with 2+ symbols.
**Must stay unchanged:** Single-symbol `--symbols X` mode must still work exactly as today.

---

## 6. Safety gates between phases

### Gate B→C: Tuning proven before rotation

| Requirement | Proof |
|-------------|-------|
| ConstraintProvider exposes minNotional for all USDT-M symbols | Unit test + cache validation |
| Tuning solver produces correct TUNED/NO_GO for known symbols | Unit test: BTCUSDT (TUNED), AIOTUSDT (NO_GO notional) |
| Shadow tuning logs visible in startup | Startup log showing all symbol results |
| No false NO_GO for currently-traded symbols | Verify against live ceremony symbols (BTCUSDT, PIPPINUSDT, BEATUSDT, ZBTUSDT) |
| No runtime behavior change from Phase B | Diff: no engine.py dispatch changes |

### Gate C1→C2: Rotation proven before deactivation

| Requirement | Proof |
|-------------|-------|
| ActiveSelector receives only TUNED symbols | Unit test: non-TUNED symbols excluded |
| Rotation controller transitions follow state machine | Unit test: all valid/invalid transitions |
| `MAX_CHANGES_PER_CYCLE` enforced | Unit test: excess changes deferred |
| `MIN_HOLD_CYCLES` enforced | Unit test: premature removal blocked |
| Fail-safe on controller error | Unit test: previous set retained |

### Gate C2→D: Deactivation proven before autonomy

| Requirement | Proof |
|-------------|-------|
| Graceful deactivation sequence completes end-to-end | Bounded live ceremony (PR-C2b) |
| No orphan orders after deactivation | Exchange state verification |
| No orphan position after deactivation | Exchange state verification |
| Cooldown prevents immediate re-entry | Unit test |
| Resource release (bridge teardown) verified | Unit test |
| Deactivation under fill pressure works | Live ceremony with active fills |

### Gate D activation: Full autonomy readiness

| Requirement | Proof |
|-------------|-------|
| Universe provider produces stable candidate list | Shadow run: same universe on repeated fetches |
| Tuning solver handles full universe without crash/timeout | Shadow run: solve 200+ symbols |
| Rotation handles multi-symbol active set | Bounded live run: 2+ symbols simultaneously |
| Deactivation works during rotation | Live run: trigger removal while trading |
| Stop-the-line rules functional | Inject churn budget violation, verify pause |
| All 8 safety invariants pass | Invariant checker integrated into loop |

---

## 7. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| R1 | Selector churn — frequent add/remove cycling | Medium | High (order waste, fill disruption) | Hysteresis band + `MAX_CHANGES_PER_CYCLE` + `MIN_HOLD_CYCLES`. Shadow verify churn rate before activation. |
| R2 | Invalid tuning — solver computes config that fails on exchange | Medium | High (-4164 notional, -2022 reduce_only) | Validate against known symbols. Live ceremony per new symbol before production. |
| R3 | Grid_v2 single-symbol ownership conflict | High | High (state corruption) | Architecture fork must be decided before Phase D. Single-symbol-per-engine is safe but resource-heavy. Symbol-scoped bridges need careful isolation proof. |
| R4 | Dirty deactivation — orders/position left on exchange after removal | Medium | Critical (unmanaged exposure) | Explicit FLAT verification + exchange state check. Stop-the-line on orphan detection. |
| R5 | Persistence/restart ambiguity — symbol states lost on restart | Medium | Medium (duplicate activation, missed deactivation) | Phase B-C: stateless (re-derive on startup). Phase D: decide persistence model. |
| R6 | Portfolio budget conflict — multiple symbols compete for margin | Medium | High (margin call, liquidation) | Per-symbol `MAX_POSITION_USD` + portfolio-level gross cap. Tuning solver accounts for portfolio budget. |
| R7 | Docs/code drift — plan says X but code does Y | High | Medium (false safety claims) | Every PR updates `STATE.md`. Review checks STATE against code. |
| R8 | minNotional changes mid-run — exchange updates constraints | Low | Medium (NO_GO for active symbol) | Periodic constraint refresh + graceful deactivation on constraint violation. |
| R9 | Tuning solver too conservative — NO_GO for viable symbols | Medium | Low (missed opportunity, not safety) | Shadow analysis: compare solver output vs manual tuning for known-good symbols. |
| R10 | WS/sync overhead with multiple symbols | Medium | Medium (latency, rate limits) | Phase D design must account for per-symbol WS streams and sync intervals. Rate-limit budget per symbol. |

---

## 8. Architecture forks / deferred decisions

### Fork 1: Multi-symbol execution model

**Options:**
- **A: Multiple engine instances (one per symbol) behind a SymbolOrchestrator.**
  - Pros: Clean isolation. Existing engine code unchanged. Crash of one symbol doesn't affect others.
  - Cons: Resource overhead (each engine has full WS + sync + EventLedger stack). Harder portfolio-level coordination.
- **B: Symbol-scoped bridge dict inside a single engine.**
  - Pros: Shared infrastructure (WS, sync, portfolio risk). Lower resource overhead.
  - Cons: Requires significant engine refactoring. State isolation must be proven. Higher blast radius on bugs.

**Decision for Phase C:** Option A. `SymbolOrchestrator` owns active set and spawns per-symbol `LiveEngineV0` instances. `LiveEngineV0` is not modified for rotation logic.

**Open for Phase D:** Re-evaluate Option B if resource overhead from Option A becomes a constraint at higher K values (e.g., K=10+). Decision point: before PR-D2.

### Fork 2: Persistence model for symbol state

**Options:**
- **A: Stateless (re-derive on startup).**
  - Tuning solver re-runs on startup. Active set derived from exchange state (open positions → ACTIVE, no position → ELIGIBLE).
  - Pros: No state file to corrupt. Simple restart semantics.
  - Cons: Loses cooldown timers. May re-activate recently-deactivated symbols.
- **B: Persistent state file (JSON/SQLite).**
  - Pros: Preserves cooldowns, hold timers, deactivation history.
  - Cons: State file corruption risk. Schema migration needed on upgrades.

**Decision point:** Before Phase D. Phase B-C can use Option A (stateless).

### Fork 3: Tuning solver determinism

**Decision:** Deterministic first. Solver uses fixed config knobs (not learned/adaptive). Adaptive tuning (price-responsive sizing) is Phase D+ at earliest.

### Fork 4: Owner of tuning cache

**Options:**
- A: Rotation controller owns the cache (tight coupling, simpler)
- B: Separate TuningCache service (loose coupling, testable)

**Decision point:** PR-B3b. Lean toward B (separate cache) for testability.

### Fork 5: Portfolio budget allocator

**Current state:** `MAX_POSITION_USD` is per-symbol, set by operator. No cross-symbol allocation.

**For Phase C:** Operator sets per-symbol caps. Total exposure bounded by portfolio gross cap.

**For Phase D:** May need dynamic allocator that distributes budget across active symbols based on score/risk. Deferred until Phase D design.

---

## 9. Validation plan

### Phase B: Unit + shadow proof

| Evidence type | What to show |
|---------------|-------------|
| Unit tests | Constraint parsing, solver determinism, NO_GO reasons, cache TTL |
| Shadow startup log | Tuning results for all `--symbols` on testnet |
| Metrics export | `grinder_tuning_result_total`, `grinder_tuning_no_go_total` visible in `/metrics` |
| Known-symbol validation | BTCUSDT → TUNED, AIOTUSDT → NO_GO (notional), results match manual calculation |

### Phase C: Unit + bounded live proof

| Evidence type | What to show |
|---------------|-------------|
| Unit tests | Rotation state machine, controller transitions, deactivation sequence |
| Bounded live ceremony | Single symbol: activate → fill → remove → FLAT → clean |
| Multi-symbol live ceremony | Two symbols: both active, one removed, other unaffected |
| Churn budget test | Inject score oscillation, verify `MAX_CHANGES_PER_CYCLE` enforced |
| Exchange state verification | No orphan orders, no orphan positions after deactivation |

### Phase D: Full integration + continuous run

| Evidence type | What to show |
|---------------|-------------|
| Shadow universe run | Discover 200+ symbols, tune all, log TUNED/NO_GO breakdown |
| Multi-symbol canary | 3 symbols, 30+ minutes, at least 1 rotation |
| Stop-the-line test | Inject invariant violation, verify loop pauses |
| 24h soak test | Continuous operation, no memory leak, no orphan accumulation |
| Restart proof | Kill process, restart, verify clean re-derivation of state |

---

## 10. Stop-the-line rules

Two severity tiers with different runtime contracts:

### Tier 1: Rotation halt (orchestrator-level)

Halts rotation decisions. Active symbol engines continue their grid_v2 lifecycle normally. The worst outcome is running the current active set longer than optimal, which is safe.

| # | Trigger | Action |
|---|---------|--------|
| STL-1 | Churn exceeds `MAX_CHANGES_PER_CYCLE` (should be impossible if enforced) | Halt rotation, alert operator, log state |
| STL-2 | Symbol activated without valid TuningResult | Halt rotation, force GRACEFUL_EXIT_ONLY for invalid symbol |
| STL-3 | Dirty deactivation detected (orphan orders or position after FLAT declaration) | Halt rotation, alert operator, do NOT auto-remediate |
| STL-4 | Orphan inventory appears during rotation (position in symbol not in active set or GRACEFUL_EXIT_ONLY) | Halt rotation, alert operator |
| STL-6 | EventLedger trust revoked during rotation | Pause rotation until trust restored (existing degraded-mode behavior) |
| STL-7 | Portfolio gross exposure exceeds cap during symbol activation | Block activation, defer to next cycle |
| STL-8 | Tuning solver produces TUNED for a previously-NO_GO symbol with no constraint change | Log warning, investigate (possible price sensitivity) |

### Tier 2: Symbol emergency halt (engine-level)

Halts ALL trading for the affected symbol. This is a more severe response, justified only when continuing to trade would violate grid_v2 safety invariants. Other symbols are not affected.

| # | Trigger | Action |
|---|---------|--------|
| STL-5 | Any existing grid_v2 safety invariant violated (INV-1 through INV-8 from doc 27) for a specific symbol | Halt trading for affected symbol (engine-level kill-switch or graceful_exit_only depending on severity), alert operator. Other symbols and rotation continue. |

**Distinction:** Tier 1 is an orchestrator-level pause — "stop changing the active set." Tier 2 is an engine-level emergency — "this symbol's grid is in an unsafe state, stop placing orders for it." These are independent: a Tier 2 halt on one symbol does not pause rotation for other symbols, and a Tier 1 rotation halt does not stop trading for any symbol.

---

## Cross-references

| Document | Relationship |
|----------|-------------|
| [37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md](37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md) | Architectural SSOT — this plan implements it |
| [36_MULTI_SYMBOL_ELIGIBILITY_ML_INTEGRATION_V1.md](36_MULTI_SYMBOL_ELIGIBILITY_ML_INTEGRATION_V1.md) | Phases 1-3b detail (already delivered) — this plan builds on top |
| [STATE.md](STATE.md) | Current implementation truth — this plan must not contradict it |
| [POST_LAUNCH_ROADMAP.md](POST_LAUNCH_ROADMAP.md) | P2 backlog — this plan is a structured subset |
| [27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md](27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md) | Grid_v2 invariants (INV-1 through INV-8) — must be preserved |
| [15_ACCOUNT_SYNC_SPEC.md](15_ACCOUNT_SYNC_SPEC.md) | AccountSyncer — deactivation verification depends on it |
| [13_OBSERVABILITY.md](13_OBSERVABILITY.md) | Existing metrics — new metrics must not conflict |
| [10_RISK_SPEC.md](10_RISK_SPEC.md) | Risk limits — tuning solver must respect them |
| [DECISIONS.md](DECISIONS.md) | ADR-122 (doc 37 creation) — this plan is the follow-up |
