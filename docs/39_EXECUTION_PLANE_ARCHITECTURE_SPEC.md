# 39. Execution-Plane Architecture Spec

**Status:** CANDIDATE — architecture and contracts only. No implementation changes.

**Parent:** `37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md` (control-plane), `38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md` (completed through D2).

---

## 1. Purpose and scope

Define how the system **executes** control-plane action intents against real per-symbol engines on a live exchange.

The control-plane (docs 37/38, PRs #522–#533) produces deterministic decisions:
- ACTIVATE symbol
- REQUEST_GRACEFUL_EXIT symbol
- FINALIZE_DEACTIVATION symbol

This spec defines what happens when those intents become real:
- engines start
- orders flow
- cleanup runs
- state converges

### What this spec covers

- Engine lifecycle management (start, stop, health)
- Activation and deactivation ceremonies against real engines
- Desired-vs-actual reconciliation
- Mismatch detection and quarantine
- Stop-the-line execution semantics
- Operator controls for runtime intervention

### What this spec does NOT cover

- Control-plane selection/ranking logic (doc 37)
- Tuning solver internals (ADR-124)
- Grid_v2 state machine internals (doc 27)
- Multi-venue routing (deferred, ADR-066)

---

## 2. Ownership boundaries

### Control-plane (existing, complete)

| Owner | Responsibility |
|-------|---------------|
| `AutonomousLoop` | Cycle cadence, stage orchestration |
| `UniverseProvider` | Raw symbol discovery |
| `SymbolOrchestrator` | TuningCache filter, candidate admission |
| `RotationController` | Active-set transitions, bounded changes, hold timers |

**Output:** `list[RotationAction]` — symbolic intents.

### Execution-plane (this spec, not yet implemented)

| Owner | Responsibility |
|-------|---------------|
| `ExecutionCoordinator` | Translates action intents into engine lifecycle operations |
| `EngineRegistry` | Tracks per-symbol engine instances and their states |
| `ActivationCeremony` | Safe startup of a new per-symbol engine |
| `DeactivationCeremony` | Safe shutdown with cleanup verification |

**Input:** `list[RotationAction]` from control-plane.
**Output:** Engine state changes on exchange.

### LiveEngineV0 (unchanged)

- Remains a **single-symbol engine**
- Does NOT know about rotation, other symbols, or the active set
- The execution-plane spawns one `LiveEngineV0` per active symbol
- The execution-plane manages their lifecycle externally

### Hard boundary

The control-plane NEVER executes exchange operations directly.
The execution-plane NEVER makes selection/ranking decisions.
`LiveEngineV0` NEVER manages its own lifecycle across symbols.

---

## 3. Core execution entities

### 3.1 EngineRegistry

Tracks all per-symbol engine instances and their current execution state.

**Responsibilities:**
- Register/deregister engine handles
- Provide current state per symbol
- Detect orphan engines (running but not in desired set)
- Detect missing engines (desired but not running)

**Contract:** `get_state(symbol) -> EngineState`, `list_present() -> list[EngineHandle]` (ACTIVATING, ACTIVE, GRACEFUL_EXIT, SHUTTING_DOWN)

### 3.2 EngineHandle

Opaque reference to one per-symbol engine instance.

**Contains:**
- symbol
- engine instance reference
- current state
- activation timestamp
- last health check timestamp

### 3.3 EngineState (per-symbol execution lifecycle)

```
ABSENT          — no engine exists for this symbol
ACTIVATING      — activation ceremony in progress
ACTIVE          — engine running, trading normally
GRACEFUL_EXIT   — engine running, no new entries, exits only
SHUTTING_DOWN   — deactivation ceremony in progress
STOPPED         — engine stopped cleanly, resources released
FAILED          — engine crashed or activation failed
QUARANTINED     — engine isolated due to mismatch or invariant violation
```

**Allowed transitions:**

| From | To | Trigger |
|------|----|---------|
| ABSENT | ACTIVATING | ACTIVATE action intent |
| ACTIVATING | ACTIVE | Activation ceremony success |
| ACTIVATING | FAILED | Activation ceremony failure/timeout |
| ACTIVE | GRACEFUL_EXIT | REQUEST_GRACEFUL_EXIT intent |
| GRACEFUL_EXIT | SHUTTING_DOWN | Position flat + no orders (finalize gate) |
| SHUTTING_DOWN | STOPPED | Cleanup verification passed |
| SHUTTING_DOWN | FAILED | Cleanup verification failed/timeout |
| STOPPED | ABSENT | Resources released |
| FAILED | QUARANTINED | Operator review required |
| QUARANTINED | ABSENT | Operator manual recovery + clean verify |
| ANY | QUARANTINED | Severe invariant violation detected |

### 3.4 ExecutionCoordinator

Top-level execution owner. Consumes action intents from control-plane,
executes them against EngineRegistry.

**Contract:**
```
execute_actions(actions: list[RotationAction]) -> ExecutionReport
```

**Responsibilities:**
- Map ACTIVATE → start activation ceremony
- Map REQUEST_GRACEFUL_EXIT → signal engine graceful mode
- Map FINALIZE_DEACTIVATION → start deactivation ceremony
- Detect and handle execution mismatches
- Enforce stop-the-line rules

---

## 4. Ceremonies

### 4.1 Activation ceremony

**Purpose:** Safely start a new per-symbol engine and verify it reaches ACTIVE state.

**Sequence:**
1. Verify exchange state is clean for symbol (`exchange_state verify`)
2. Build `LiveEngineV0` with symbol-specific config (from TuningResult)
3. Start engine (WS connection, preflight checks)
4. Verify engine health (first snapshot received, no startup errors)
5. Register engine as ACTIVE in EngineRegistry
6. If any step fails: mark FAILED, do not retry automatically

**Timeout:** Configurable activation timeout (e.g., 60s). If not ACTIVE within timeout → FAILED.

**Evidence:** Log `ENGINE_ACTIVATION_STARTED`, `ENGINE_ACTIVATION_COMPLETED`, `ENGINE_ACTIVATION_FAILED`.

### 4.2 Deactivation ceremony

**Purpose:** Safely stop a per-symbol engine and verify clean exchange state.

**Prerequisites:** Control-plane has already moved symbol through GRACEFUL_EXIT. Deactivation finalize gate (`can_finalize_deactivation`) returns `can_finalize=True`.

**Sequence:**
1. Signal engine to stop accepting new snapshots
2. Wait for engine graceful shutdown (or timeout)
3. Run `exchange_state cleanup` for symbol
4. Run `exchange_state verify` for symbol
5. If clean: deregister engine, mark STOPPED
6. If dirty: retry cleanup (bounded retries), then FAILED if still dirty

**Timeout:** Configurable deactivation timeout (e.g., 120s). If not STOPPED within timeout → FAILED.

**Evidence:** Log `ENGINE_DEACTIVATION_STARTED`, `ENGINE_DEACTIVATION_COMPLETED`, `ENGINE_DEACTIVATION_FAILED`.

### 4.3 Mismatch recovery

**Purpose:** Handle divergence between desired and actual engine state.

**Categories:**
- **Missing engine:** Desired ACTIVE but no engine running → trigger activation
- **Orphan engine:** Engine running but not in desired set → trigger deactivation
- **State mismatch:** Engine in wrong state (e.g., ACTIVE but should be GRACEFUL_EXIT) → correct state
- **Health failure:** Engine ACTIVE but not producing ticks → quarantine

**Recovery policy:** Bounded retries, then quarantine on failure.

---

## 5. Desired-vs-actual reconciliation

Each execution cycle compares:
- **Desired symbol set:** `set[str]` — symbols that should currently have a live engine. Derived from control-plane active set (the caller resolves RotationAction intents into this set before calling the reconciler).
- **Actual state:** From EngineRegistry (what engines are actually running and in which state)

**Reconciliation output:**
- Actions needed to converge (start, stop, signal)
- Mismatches detected (with severity)
- Quarantine recommendations

**Reconciliation frequency:** Every control-plane cycle (piggybacks on `AutonomousLoop.run_cycle`).

**Fail-safe:** If reconciliation fails, retain current engine state. Do not speculatively start/stop engines on error.

---

## 6. Stop-the-line semantics

### Execution-level stop-the-line (complements control-plane STL from doc 38)

| # | Trigger | Action |
|---|---------|--------|
| STL-E1 | Engine activation fails repeatedly (>N attempts) | Quarantine symbol, alert operator |
| STL-E2 | Deactivation cleanup fails (dirty state after retries) | Quarantine symbol, DO NOT auto-remediate |
| STL-E3 | Orphan engine detected (running but no desired-state entry) | Halt new activations, alert operator |
| STL-E4 | Duplicate engine for same symbol | Halt all execution, alert operator |
| STL-E5 | Reconciliation mismatch count exceeds threshold | Halt new activations until mismatches resolve |
| STL-E6 | Engine health check failure (no ticks for >N seconds) | Quarantine specific engine |

**Key principle:** Stop-the-line halts *new execution actions*. Running engines continue their grid_v2 lifecycle. The worst outcome of an execution halt is that the active set stays frozen, which is safe.

---

## 7. Operator controls

| Control | Effect |
|---------|--------|
| Pause execution | No new activations/deactivations. Running engines continue. |
| Force deactivate symbol | Immediate deactivation ceremony for specific symbol. |
| Quarantine symbol | Move engine to QUARANTINED. No trading. |
| Release quarantine | Operator acknowledges issue resolved. Verify clean state. Return to ABSENT. |
| Resume execution | Re-enable activation/deactivation after pause. |
| Emergency stop all | Trigger deactivation ceremony for all active engines. |

**All operator controls must be auditable:** Log `OPERATOR_CONTROL action=... symbol=... operator=...`.

---

## 8. Safety guarantees

**S1:** No engine activation without clean exchange state verification.

**S2:** No engine deactivation without cleanup verification (flat + no orders).

**S3:** No silent engine killing — all shutdowns go through deactivation ceremony.

**S4:** No duplicate engines for the same symbol.

**S5:** No bypass of the finalize gate (`can_finalize_deactivation`).

**S6:** Orchestrator/control-plane errors do not propagate to execution-plane (separate error domains).

**S7:** Execution-plane errors do not change control-plane decisions (separate error domains).

**S8:** All engine state transitions are logged and auditable.

---

## 9. Observability

### Required signals

| Signal | When |
|--------|------|
| `ENGINE_ACTIVATION_STARTED symbol=X` | Activation ceremony begins |
| `ENGINE_ACTIVATION_COMPLETED symbol=X` | Engine reached ACTIVE |
| `ENGINE_ACTIVATION_FAILED symbol=X reason=R` | Activation failed |
| `ENGINE_DEACTIVATION_STARTED symbol=X` | Deactivation ceremony begins |
| `ENGINE_DEACTIVATION_COMPLETED symbol=X` | Engine reached STOPPED, exchange clean |
| `ENGINE_DEACTIVATION_FAILED symbol=X reason=R` | Deactivation failed |
| `ENGINE_QUARANTINED symbol=X reason=R` | Engine moved to quarantine |
| `ENGINE_HEALTH_CHECK symbol=X status=OK/FAILED` | Periodic health check |
| `EXECUTION_RECONCILE_COMPLETED desired=N actual=M mismatches=K` | Reconciliation summary |
| `EXECUTION_STOP_THE_LINE rule=STL-EX` | Execution halted |
| `OPERATOR_CONTROL action=X symbol=Y` | Operator intervention |

---

## 10. Cross-references

| Document | Relationship |
|----------|-------------|
| [37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md](37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md) | Control-plane architecture — this spec implements its action intents |
| [38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md](38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md) | Control-plane plan (completed) — this spec is the next workstream |
| [40_EXECUTION_PLANE_IMPLEMENTATION_PLAN.md](40_EXECUTION_PLANE_IMPLEMENTATION_PLAN.md) | Phased implementation plan for this spec |
| [27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md](27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md) | Grid_v2 internals — execution-plane wraps, does not modify |
| [15_ACCOUNT_SYNC_SPEC.md](15_ACCOUNT_SYNC_SPEC.md) | Account sync — execution-plane uses for health/verification |
