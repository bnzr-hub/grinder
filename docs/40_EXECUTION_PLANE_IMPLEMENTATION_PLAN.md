# 40. Execution-Plane Implementation Plan

**Status:** ACTIVE — phased execution plan. No implementation changes in this document.

**Parent spec:** `39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md`

---

## 1. Purpose

This document converts the execution-plane architecture spec (doc 39) into an actionable, PR-sized implementation plan.

It answers:
- what we build first and what depends on what
- what stays isolated before integration
- what safety gates must be proven before each escalation
- what must not be changed prematurely

**Relationship to completed work:**
- `38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md` — control-plane (Phases A–D, all DONE). This plan builds the layer that executes its outputs.
- `39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md` — defines the *what*. This plan defines the *how* and *when*.

---

## 2. Current baseline

### Implemented and stable (build-on, do not break)

| Component | Location | Notes |
|-----------|----------|-------|
| AutonomousLoop | `src/grinder/orchestration/autonomous_loop.py` | Produces RotationAction intents per cycle |
| SymbolOrchestrator | `src/grinder/orchestration/symbol_orchestrator.py` | TuningCache filter + controller handoff |
| RotationController | `src/grinder/rotation/controller.py` | Bounded changes, hold timers, graceful exit |
| DeactivationGate | `src/grinder/orchestration/deactivation.py` | `can_finalize_deactivation()` finalize gate |
| UniverseProvider | `src/grinder/orchestration/universe_provider.py` | Venue-level discovery with refresh/fail-safe |
| LiveEngineV0 | `src/grinder/live/engine.py` | Single-symbol engine. Must stay unchanged. |
| exchange_state.py | `scripts/exchange_state.py` | check/cleanup/verify per symbol |
| cleanup-on-exit | `scripts/run_trading.py` | Per-symbol subprocess cleanup |

### Not implemented yet

| Component | Description |
|-----------|-------------|
| EngineRegistry | Per-symbol engine instance tracking |
| ExecutionCoordinator | Action intent → engine lifecycle |
| ActivationCeremony | Safe per-symbol engine startup |
| DeactivationCeremony | Safe per-symbol engine shutdown with cleanup verification |
| Desired-vs-actual reconciliation | Compare control-plane intent with engine reality |
| Operator runtime controls | Pause, force-deactivate, quarantine, resume |

---

## 3. Target outcome

When this plan is fully executed, the system will:

1. **Accept** action intents from the autonomous loop (ACTIVATE, REQUEST_GRACEFUL_EXIT, FINALIZE_DEACTIVATION)
2. **Start** per-symbol `LiveEngineV0` instances via activation ceremony
3. **Stop** per-symbol engines via deactivation ceremony with cleanup verification
4. **Reconcile** desired-vs-actual engine state every cycle
5. **Quarantine** engines on mismatch or invariant violation
6. **Expose** operator controls for pause, force-deactivate, resume
7. **Operate** continuously alongside the autonomous loop

---

## 4. Phased implementation map

```
Phase E1: Engine registry + execution state model
    │
    ▼
Phase E2: Activation ceremony
    │
    ▼
Phase E3: Deactivation ceremony (real per-symbol engines)
    │
    ▼
Phase E4: Desired-vs-actual reconciliation loop
    │
    ▼
Phase E5: Stop-the-line, quarantine, operator controls
    │
    ▼
Phase E6: Integration into autonomous loop
```

### Phase E1: Engine registry and execution state model

**Goal:** Track per-symbol engine instances and their lifecycle state. Pure data model — no engine start/stop.

**What changes:**
- `EngineState` enum (8 states from doc 39 Section 3.3)
- `EngineHandle` dataclass (symbol, state, timestamps)
- `EngineRegistry` (register, deregister, get_state, list_active, detect mismatches)
- Transition validation (only allowed transitions per doc 39 table)

**What stays unchanged:** Everything. Pure additive module.

**Safety:** No engine operations. Data model only.

### Phase E2: Activation ceremony

**Goal:** Safely start a per-symbol `LiveEngineV0` and verify it reaches ACTIVE state.

**What changes:**
- `ActivationCeremony` — builds and starts one LiveEngineV0 per symbol
- Pre-activation: exchange_state verify (clean gate)
- Post-activation: health check (first snapshot received)
- Timeout handling: FAILED on timeout
- EngineRegistry integration: ABSENT → ACTIVATING → ACTIVE/FAILED

**What stays unchanged:** LiveEngineV0 internals. Grid_v2. Selector.

**Safety:** Activation is fail-closed: any failure → FAILED, no retry without operator/reconciler decision.

### Phase E3: Deactivation ceremony (real engines)

**Goal:** Safely stop a per-symbol engine with cleanup verification.

**What changes:**
- `DeactivationCeremony` — stop engine, cleanup, verify
- Uses existing `can_finalize_deactivation()` gate
- Uses existing `exchange_state cleanup/verify` infrastructure
- Bounded retries on cleanup failure
- EngineRegistry integration: GRACEFUL_EXIT → SHUTTING_DOWN → STOPPED/FAILED

**What stays unchanged:** Deactivation finalize gate. exchange_state.py tool.

**Safety:** Deactivation must pass cleanup verification. Dirty state → FAILED, not STOPPED.

### Phase E4: Desired-vs-actual reconciliation

**Goal:** Every cycle, compare what control-plane wants with what engines are actually doing. Produce convergence actions.

**What changes:**
- `ExecutionReconciler` — diff desired vs EngineRegistry
- Detect: missing engines, orphan engines, state mismatches
- Produce corrective actions (start missing, stop orphan, fix state)
- Bounded: max corrective actions per cycle

**What stays unchanged:** Control-plane decisions. RotationController.

**Safety:** Reconciliation is conservative: never start/stop more engines than mismatch count warrants.

### Phase E5: Stop-the-line, quarantine, operator controls

**Goal:** Operator can pause/resume execution, quarantine individual engines, force-deactivate.

**What changes:**
- Stop-the-line rules (STL-E1 through STL-E6 from doc 39)
- Quarantine state in EngineRegistry
- Operator control API (pause, resume, force-deactivate, release quarantine)
- Audit logging for all operator actions

**What stays unchanged:** Control-plane autonomy (pause only affects execution).

**Safety:** All operator controls are auditable. No silent state changes.

### Phase E6: Integration into autonomous loop

**Goal:** Wire ExecutionCoordinator into AutonomousLoop so action intents are executed automatically.

**What changes:**
- `AutonomousLoop` gains optional `ExecutionCoordinator`
- After `run_cycle()` produces actions, coordinator executes them
- Feature-flagged: `execution_enabled` (default OFF)
- Operator ACK required to enable

**What stays unchanged:** Control-plane cycle logic. AutonomousLoop still works without execution (shadow mode).

**Safety:** Execution is opt-in. Default behavior remains control-plane-only.

---

## 5. PR-by-PR plan

### PR-E1: Engine registry and state model

**Title:** `feat(execution): engine registry and execution state model`
**Goal:** Pure data model for per-symbol engine tracking.
**Files touched:**
- `src/grinder/execution_plane/__init__.py` (new)
- `src/grinder/execution_plane/registry.py` (new)
- `tests/unit/test_engine_registry.py` (new)
**Tests required:**
- All 8 engine states defined
- All allowed transitions succeed
- Invalid transitions rejected
- Register/deregister lifecycle
- Detect missing/orphan engines
- Determinism
**Must stay unchanged:** No engine changes. No orchestration changes.

### PR-E2: Activation ceremony

**Title:** `feat(execution): per-symbol engine activation ceremony`
**Goal:** Build, start, and health-check one LiveEngineV0 per symbol.
**Files touched:**
- `src/grinder/execution_plane/activation.py` (new)
- `tests/unit/test_activation_ceremony.py` (new)
**Dependencies:** PR-E1.
**Tests required:**
- Clean exchange → activation succeeds
- Dirty exchange → activation blocked
- Startup error → FAILED state
- Timeout → FAILED state
- Registry updated on success/failure
**Must stay unchanged:** LiveEngineV0 internals.

### PR-E3: Deactivation ceremony

**Title:** `feat(execution): per-symbol engine deactivation with cleanup verification`
**Goal:** Stop engine, cleanup exchange, verify clean state.
**Files touched:**
- `src/grinder/execution_plane/deactivation.py` (new)
- `tests/unit/test_engine_deactivation.py` (new)
**Dependencies:** PR-E2.
**Tests required:**
- Clean shutdown → STOPPED
- Cleanup failure → retry → STOPPED or FAILED
- Finalize gate blocks if not clean
- Registry updated
- Bounded live deactivation ceremony
**Must stay unchanged:** Deactivation finalize gate. exchange_state.py.

### PR-E4: Desired-vs-actual reconciliation

**Title:** `feat(execution): desired-vs-actual engine reconciliation`
**Goal:** Compare control-plane desired state with EngineRegistry actual state.
**Files touched:**
- `src/grinder/execution_plane/reconciler.py` (new)
- `tests/unit/test_execution_reconciler.py` (new)
**Dependencies:** PR-E3.
**Tests required:**
- Missing engine → activation action
- Orphan engine → deactivation action
- State mismatch → corrective action
- Bounded corrective actions per cycle
- No action when converged
**Must stay unchanged:** Control-plane decisions.

### PR-E5: Stop-the-line and operator controls

**Title:** `feat(execution): stop-the-line, quarantine, and operator controls`
**Goal:** Safety controls for runtime execution.
**Files touched:**
- `src/grinder/execution_plane/safety.py` (new)
- `src/grinder/execution_plane/operator.py` (new)
- `tests/unit/test_execution_safety.py` (new)
**Dependencies:** PR-E4.
**Tests required:**
- Each STL-E rule triggers correctly
- Quarantine blocks further operations on symbol
- Operator pause/resume works
- Operator force-deactivate works
- All controls are auditable (log assertions)
**Must stay unchanged:** Control-plane stop-the-line rules (Tier 1/2 from doc 38).

### PR-E6: Autonomous loop integration

**Title:** `feat(execution): wire execution-plane into autonomous loop`
**Goal:** AutonomousLoop can optionally execute action intents.
**Files touched:**
- `src/grinder/orchestration/autonomous_loop.py` (modified)
- `src/grinder/execution_plane/coordinator.py` (new)
- `tests/unit/test_execution_integration.py` (new)
**Dependencies:** PR-E5.
**Tests required:**
- Execution disabled (default) → shadow-only, no engine operations
- Execution enabled → actions executed via coordinator
- Execution error → stop-the-line, engines preserved
- Feature flag + operator ACK gate
**Must stay unchanged:** AutonomousLoop control-plane logic.

---

## 6. Safety gates between phases

### Gate E1→E2: State model proven

| Requirement | Proof |
|-------------|-------|
| All 8 states and transitions tested | Unit tests |
| Registry handles register/deregister cleanly | Unit tests |
| No coupling to engine internals | Import check |

### Gate E2→E3: Activation proven

| Requirement | Proof |
|-------------|-------|
| Activation ceremony starts real engine | Bounded live test |
| Failed activation → FAILED state, clean recovery | Unit + live test |
| Exchange pre-check blocks dirty activation | Unit test |

### Gate E3→E4: Deactivation proven

| Requirement | Proof |
|-------------|-------|
| Deactivation ceremony reaches clean exchange state | Bounded live test |
| Failed cleanup → FAILED, not STOPPED | Unit test |
| Finalize gate enforced | Unit test |

### Gate E4→E5: Reconciliation proven

| Requirement | Proof |
|-------------|-------|
| Missing/orphan engines detected | Unit tests |
| Corrective actions bounded | Unit tests |
| No action when converged | Unit test |

### Gate E5→E6: Safety controls proven

| Requirement | Proof |
|-------------|-------|
| Stop-the-line rules trigger correctly | Unit tests |
| Quarantine isolates engine | Unit tests |
| Operator controls are auditable | Log assertion tests |

---

## 7. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| R1 | Engine startup race (two engines for same symbol) | Medium | Critical | EngineRegistry duplicate detection + STL-E4 |
| R2 | Deactivation cleanup fails (dirty exchange) | Medium | High | Bounded retries, then FAILED + quarantine |
| R3 | WS connection per engine overwhelms Binance rate limit | Medium | High | Connection pool or staggered activation |
| R4 | Engine crash leaves orphan orders | Medium | High | Health check + deactivation ceremony cleanup |
| R5 | Control-plane and execution-plane disagree on state | Medium | Medium | Reconciliation loop with mismatch alerts |
| R6 | Operator accidentally force-deactivates wrong symbol | Low | High | Confirmation gate + audit log |

---

## 8. Architecture forks / deferred decisions

### Fork: Engine process model

**Current decision:** Per-symbol engines run as in-process coroutines (same process as orchestrator). If resource pressure requires it, future iteration may move to subprocess per engine.

### Fork: Connection sharing

**Deferred:** Whether per-symbol engines share a WS connection or each has its own. Depends on Binance rate limits at higher K values.

### Fork: Persistence

**Deferred:** Whether EngineRegistry persists across restarts. Phase E1–E4 use stateless re-derivation (same as control-plane).

---

## 9. Validation plan

### Phase E1–E2: Unit + bounded live

| Evidence | What to show |
|----------|-------------|
| Unit tests | State model, registry, activation ceremony logic |
| Bounded live | One symbol activated, health-checked, verified |

### Phase E3–E4: Unit + bounded live + multi-symbol

| Evidence | What to show |
|----------|-------------|
| Unit tests | Deactivation, reconciliation, mismatch detection |
| Bounded live | One symbol activated → deactivated → exchange clean |
| Multi-symbol | Two symbols: both activated, one deactivated, other unaffected |

### Phase E5–E6: Full integration

| Evidence | What to show |
|----------|-------------|
| Unit tests | Safety controls, operator actions, audit |
| Integration | Autonomous loop with execution enabled, bounded multi-symbol run |
| Operator | Pause/resume/force-deactivate demonstrated |

---

## Cross-references

| Document | Relationship |
|----------|-------------|
| [39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md](39_EXECUTION_PLANE_ARCHITECTURE_SPEC.md) | Architecture SSOT — this plan implements it |
| [37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md](37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md) | Control-plane SSOT — execution-plane consumes its outputs |
| [38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md](38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md) | Control-plane plan (completed) — this plan is the sequel |
| [27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md](27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md) | Grid_v2 — execution-plane wraps, does not modify |
| [STATE.md](STATE.md) | Current implementation truth |
