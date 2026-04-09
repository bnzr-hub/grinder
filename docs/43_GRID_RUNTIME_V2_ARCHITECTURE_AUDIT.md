# 43 - Grid Runtime V2 Architecture Audit

**Status:** Candidate architecture memo  
**Date:** 2026-04-08  
**Audience:** operator / runtime / grid / live-trading maintainers  
**Purpose:** evaluate whether the current grid runtime should be simplified into a cleaner exchange-backed desired-state model, and identify the risks, conflicts, migration path, and expected gains.

> Note:
> This document captures the broad architectural direction.
> After deeper review, the currently recommended implementation path is the
> incremental plan in
> [44_INCREMENTAL_GRID_SIMPLIFICATION_PLAN.md](./44_INCREMENTAL_GRID_SIMPLIFICATION_PLAN.md),
> not a large one-shot Grid Runtime V2 rewrite.

---

## 1. Executive Summary

### Short answer

Yes, there is a strong case for a **targeted redesign of the grid runtime layer**.

Not a full rewrite of the whole system.
Not a rollback to snapshot-only logic.
Not a strategy redesign.

The right target is:

- keep existing risk / selector / orchestration / leverage / policy logic
- keep rolling-window behavior and current functional contract
- **replace the fragile runtime maintenance layer** with a cleaner, exchange-backed desired-state model

### Why

The current live stack already proved the following:

- autonomous truth path works
- fail-closed admission/orchestration works
- 5x leverage enforcement works
- single-symbol live trading works
- multi-symbol activation works
- controller/orchestration seams were fixable without architectural rollback

The remaining instability is concentrated in a narrower layer:

- `PositionLedger`
- `EventLedger`
- sync reconciliation
- grid_v2 repair / re-register / rebuild behavior
- local runtime state drift under real fills and snapshot lag

This is a good sign:

- the architecture as a whole is not fundamentally broken
- but the **order/position maintenance layer is too complicated and too stateful**

### Proposed direction

Move toward a **WS-first, exchange-backed state mirror** and compute grid topology as a **desired-state diff**:

1. `ORDER_TRADE_UPDATE` + `ACCOUNT_UPDATE` remain the fast path
2. REST snapshot remains bootstrap / reconciliation / recovery path
3. unify these into one canonical exchange-backed runtime state
4. compute desired entry/exit topology from that state
5. diff desired vs actual exchange orders
6. cancel extra / place missing / amend mismatched in bounded batches

This keeps functionality while reducing the amount of fragile repair logic.

---

## 2. Scope of This Audit

This memo is about the runtime grid-maintenance layer only.

It is **not** proposing changes to:

- selector / ranking
- autonomous admission
- portfolio risk budgets
- day risk / trailing profit lock
- leverage semantics
- execution-plane orphan safety
- top-level autonomous orchestration

It is specifically about:

- how a live symbol tracks actual open orders and actual position
- how it decides which orders must exist now
- how it recovers from fills, snapshot lag, and event gaps

---

## 3. Current System: What Already Exists

### 3.1 Functional contract that should be preserved

Current intended live behavior already has a coherent product shape:

- two-sided rolling-window grid
- bounded entry levels
- separate exit orders
- one-sided inventory
- inventory caps and force-reduce / forced-flat ladders
- fill-driven rolling / replenishment
- live per-symbol engines

The important current contract line is documented in:

- [27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md](./27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md)

That is the functionality we want to preserve.

### 3.2 Existing runtime state sources

Today runtime state is effectively assembled from multiple overlapping sources:

- user-data WS order events (`ORDER_TRADE_UPDATE`)
- user-data WS position events (`ACCOUNT_UPDATE`)
- account snapshot open orders
- account snapshot positions
- local `EventLedger`
- local `PositionLedger`
- grid_v2 state machine snapshot
- adapter / bridge registries
- repair / reconciliation heuristics

This gives the system a lot of information, but also creates several overlapping truth models.

### 3.3 Current runtime design strengths

The current design does have real strengths:

- WS-first event path exists
- snapshot bootstrap/reconciliation exists
- there is already a grid_v2 state machine
- invariants exist in [state.py](/home/benya/Project/grinder/src/grinder/grid_v2/state.py)
- separate exit-repair and sync-reconcile components already exist
- rolling behavior is already defined and partially proven live

So this is not a blank slate.
The redesign can reuse substantial parts of the current stack.

---

## 4. Core Problem Statement

### 4.1 What is actually failing

Recent live canaries showed that the remaining instability is not in high-level orchestration.
It is in the symbol-local runtime maintenance loop:

- persistent `POSITION_MISSING_IN_LEDGER`
- repeated `POSITION_LEDGER_SHADOW_DIVERGENCE`
- `GRID_V2_EXIT_TOPOLOGY_REPAIR_REREGISTERED ... status=BLOCKED`
- symbol-specific cases where entry/exit topology stopped rebuilding cleanly

### 4.2 What these failures suggest

These failures are a symptom of a deeper problem:

- too much runtime meaning is encoded in layered local state
- too much recovery depends on special-case repair paths
- too many state transitions are inferred indirectly instead of recomputed from one canonical mirror

This is the critical distinction:

- the strategy logic is not necessarily too complicated
- the **runtime maintenance mechanism** is

### 4.3 What is not the problem

This is not evidence that:

- rolling windows are a bad idea
- exits should be removed
- WS should be abandoned
- the whole live system should be rewritten

The issue is more specific:

- local runtime state has become too difficult to reconcile cleanly under live asynchronous conditions

---

## 5. Why “Just Trust Snapshot” Is Not the Answer

This is a crucial point.

The proposal is **not**:

- “revert to snapshot-only truth”

That would recreate older problems:

- slower reaction to fills
- stale view between syncs
- worse behavior under fast volatility
- more lag between exchange truth and planner behavior

Snapshot-only was explicitly avoided for good reasons.

### Correct target

The correct target is:

- **WS-first live truth**
- snapshot as bootstrap / audit / reconciliation / recovery
- one unified exchange-backed state mirror

So the desired architecture is:

- not `snapshot = truth`
- but `exchange-confirmed state = truth`

Where:

- WS updates it quickly
- snapshot repairs or bootstraps it conservatively

---

## 6. Proposed Architecture Direction: Grid Runtime V2

### 6.1 Canonical runtime state

Per symbol, keep one canonical exchange-backed mirror:

```text
RuntimeSymbolState
  position
  open_orders
  open_lots
  mode / branch
  force_reduce / forced_flat flags
  pending local actions
  trust / recovery status
```

This is the only state the planner should need.

### 6.2 Data-source hierarchy

Preferred model:

- WS = primary mutating source
- snapshot = reconcile / bootstrap source
- local state = exchange-backed mirror, not an independent authority

This avoids both extremes:

- not snapshot-only
- not ledger-only

### 6.3 Planner model

The planner should be closer to:

```text
desired_topology = f(runtime_symbol_state, market_inputs, policy)
```

Then:

```text
actions = diff(actual_exchange_orders, desired_topology)
```

This is the main simplification.

### 6.4 Rolling behavior under V2

Rolling absolutely survives.

It becomes simpler:

- a fill changes position / lot state
- new state changes desired topology
- diff creates the minimal needed actions

So rolling remains a feature, but stops being a special repair-heavy path.

### 6.5 Exit behavior under V2

Exit maintenance should not depend on fragile “repair because local state is weird” logic.
Instead:

- lots define required exits
- planner computes which exits must exist
- diff enforces them

That is much easier to reason about than ad hoc exit topology repair.

---

## 7. Expected Gains

### 7.1 Operational gains

If implemented correctly, this should yield:

- faster and more predictable response after fills
- less silent topology drift
- easier recovery after volatility bursts
- easier reasoning about “what should be on the exchange right now”
- fewer permanent divergence states

### 7.2 Debuggability gains

Today many failures look like:

- repair blocked
- trust revoked
- topology incomplete
- ledger divergence

In a cleaner desired-state model, the core debug question becomes:

- what orders should exist?
- what orders actually exist?
- what diff prevented convergence?

This is much easier to inspect and test.

### 7.3 Testing gains

A desired-state planner is easier to test than a repair-heavy state machine, because:

- pure inputs -> pure expected topology
- diff engine -> deterministic actions
- fewer long chains of hidden local state

### 7.4 Live resilience gains

Under fast markets, the goal is not “no divergence ever”.
The goal is:

- divergence happens
- state reconverges quickly and deterministically

This architecture is better aligned with that goal.

---

## 8. Preserved Functional Contract

The following behavior should remain intact:

- rolling-window entry logic
- paired exit logic
- bounded window
- one-sided inventory
- `max_inventory_levels`
- force-reduce and forced-flat thresholds
- existing risk caps
- per-symbol sizing logic
- leverage handling
- autonomous orchestration

This is a runtime-maintenance redesign, not a strategy rewrite.

---

## 9. Deep Risk Audit

This section is the most important part of the memo.

### 9.1 Risk: accidental rollback to snapshot-only latency

**Description**

A bad implementation could accidentally make snapshot polling the main driver again.

**Why it matters**

- slower response to fills
- poor behavior under volatility
- visible lag in rolling/rebuild

**Mitigation**

- keep WS as primary state update path
- use snapshot only to bootstrap or repair missed state
- planner runs on unified exchange-backed state, not directly from raw snapshot every tick

**Conclusion**

This risk is real, but avoidable if V2 is implemented as WS-first.

---

### 9.2 Risk: too-frequent cancel/place churn

**Description**

If V2 recomputes desired topology every cycle and naively diffs it, it may over-cancel and over-place.

**Why it matters**

- exchange churn
- rate-limit pressure
- thrash around small market moves
- more live noise than today

**Mitigation**

- preserve bounded diff semantics
- keep anti-churn / debounce / cooldown where meaningful
- distinguish:
  - real topology gap
  - harmless transient pending-local-action state
- do not full-rebuild on every minor mismatch

**Conclusion**

The answer is not “never recompute”.
The answer is “recompute cleanly, execute minimally”.

---

### 9.3 Risk: partial fills and non-round lot state

**Description**

Exchange reality may include:

- partial fills
- partially closed lots
- quantities not perfectly aligned to one idealized level

If V2 assumes too much geometric purity, it will still fail live.

**Mitigation**

- treat lots and positions explicitly
- planner must tolerate partial residuals
- desired exits should derive from actual current lots / net position, not only idealized full-level assumptions

**Conclusion**

This is one of the hardest real requirements.
V2 must be explicit here.

---

### 9.4 Risk: reduce-only invalidity and notional rounding

**Description**

Even if desired topology is conceptually correct, exchange submission can fail because of:

- tick-size rounding
- step-size rounding
- min notional
- reduce-only conflicts
- stale remaining position size

**Mitigation**

- planner computes exchange-legal candidate orders
- diff/executor validates against constraints before submission
- keep bounded fallback behavior when an order is no longer legal
- instrument rejection reasons clearly

**Conclusion**

V2 does not eliminate these constraints, but can make them easier to reason about.

---

### 9.5 Risk: conflicting truth between open lots and net position

**Description**

One of the hardest consistency problems is:

- lot ledger says one thing
- net position says another

**Mitigation**

Need an explicit policy:

- either lots are derived/recoverable from exchange-confirmed fill history
- or degraded mode enters a bounded “position truth but lot ambiguity” recovery path

What should not happen:

- infinite degraded mode
- permanent `POSITION_MISSING_IN_LEDGER`
- silent planner confusion

**Conclusion**

This is a key design decision that must be explicit in V2.

---

### 9.6 Risk: preserving too much legacy repair logic

**Description**

If V2 is implemented incrementally but leaves most old repair logic in place, complexity may increase instead of decrease.

**Mitigation**

- define which old repair paths become obsolete
- remove or quarantine them after equivalent V2 path is stable
- do not run two contradictory topology authorities indefinitely

**Conclusion**

This is one of the biggest migration risks.

---

### 9.7 Risk: losing proven behavior in healthy symbols

**Description**

Some symbols already behave well.
A redesign can accidentally regress these healthy paths.

**Mitigation**

- reproduce both:
  - bad path
  - healthy path
- require parity on healthy cases before switching authority
- run shadow mode before cutover

**Conclusion**

Healthy-path preservation must be a review bar, not an afterthought.

---

### 9.8 Risk: hidden coupling with force-reduce / forced-flat

**Description**

Current runtime behavior around adverse levels may depend on the exact current topology/ledger logic.

**Mitigation**

- treat risk modes as planner inputs
- define desired topology under:
  - normal
  - force-reduce
  - forced-flat
- test these separately

**Conclusion**

V2 should simplify runtime, but not flatten risk semantics.

---

### 9.9 Risk: implementation complexity larger than expected

**Description**

A targeted redesign can quietly become a rewrite.

**Mitigation**

- phase the work
- keep initial scope narrow
- prove one seam at a time
- do not replace every layer simultaneously

**Conclusion**

This must be treated as a staged migration, not a heroic rewrite.

---

## 10. Conflict Audit Against Current Contracts

### 10.1 Conflict with doc-27 rolling spec

No direct conflict.

V2 can preserve doc-27 behavior and only change runtime implementation strategy.

### 10.2 Conflict with doc-25 planner spec

There is conceptual overlap.

Doc-25 already states a useful idea:

- exchange open orders as source of truth
- desired grid
- diff to actions

But current live runtime has grown beyond the simple assumptions of doc-25.

V2 should treat doc-25 as a useful precursor, not as sufficient current SSOT.

### 10.3 Conflict with current ledgers

Potential conflict:

- if ledgers remain fully authoritative while V2 mirror also becomes authoritative

That would create split-brain.

So V2 must explicitly decide:

- ledgers become implementation detail of the exchange-backed mirror
or
- ledgers remain primary state, but exposed through one canonical mirror API

What should not happen:

- multiple parallel authorities visible to planner logic

### 10.4 Conflict with current repair modules

Current modules like:

- sync reconciler
- exit repair
- bridge repair/re-register

may become partially redundant or need to be redefined as:

- diff-layer helper
- recovery helper
- bounded repair executor

If left untouched, they may fight V2.

---

## 11. What Should Be Reused vs Replaced

### Reuse

- grid_v2 strategic contract from doc-27
- rolling-window semantics
- one-symbol-per-engine runtime model
- WS event ingestion
- account snapshot sync
- risk ladders and caps
- execution plane
- autonomous orchestration

### Likely simplify or replace

- direct planner dependence on fragmented local state
- implicit repair-based exit rebuild logic
- long-lived divergence/trust states that do not converge
- too many special-case topology recovery branches

### Keep but demote

- `PositionLedger`
- `EventLedger`

These likely remain useful, but as internal components of a canonical state mirror, not as independent competing truths.

---

## 12. Migration Strategy

### Phase 0: current bug stabilization

Before any large redesign:

- close immediate ledger hydration bugs
- close immediate exit-repair blockers
- keep current live path stable enough to compare against

### Phase 1: define canonical `RuntimeSymbolState`

Introduce one explicit state object that the planner consumes.

Do not change strategy behavior yet.
Only clarify state ownership and data flow.

### Phase 2: add desired-topology planner in shadow

For live symbols:

- compute current legacy behavior
- compute new desired topology in shadow
- compare
- emit structured diff diagnostics

Do not execute new actions yet.

### Phase 3: enable V2 diff engine for selected symbols / modes

Start with:

- simplest healthy symbol paths
- bounded action set
- clear rollback path

### Phase 4: retire redundant repair logic

Once V2 proves stable:

- remove or sharply narrow old repair paths
- reduce hidden state interactions

---

## 13. Success Criteria

This redesign is worth doing only if it produces clear measurable wins.

### Minimum technical wins

- no persistent `POSITION_MISSING_IN_LEDGER`
- no silent exit-topology stalls
- no symbol stuck with obviously incomplete opposing-side protection
- no permanent degraded state from ordinary fill/snapshot ordering

### Stronger operational wins

- clearer logs
- easier explanation of why an order exists or does not exist
- smaller recovery surface after volatility bursts
- easier reproduction in tests

### Strongest success case

In live volatility:

- symbol fills
- topology changes
- system converges quickly
- no hidden repair drama
- operator can explain state from exchange truth + planner rules

---

## 14. Recommendation

### Recommendation: Yes, pursue Grid Runtime V2

But with the following boundaries:

- do **not** rewrite the whole autonomous stack
- do **not** regress to snapshot-only
- do **not** remove rolling behavior
- do **not** attempt a one-shot rewrite

### Recommended objective

Build a **WS-first, exchange-backed desired-state runtime** for grid maintenance.

### Why this is worth it

Because the expected gains are substantial:

- cleaner runtime logic
- fewer long-lived divergence bugs
- easier live debugging
- preserved strategy behavior
- better recovery under real volatility

And because the current pain is precisely in the layer this redesign targets.

---

## 15. Final Verdict

The redesign is justified.

Not because “everything is broken”.
Not because current live trading is impossible.
Not because the strategy needs to change.

It is justified because:

- most of the system is now operationally credible
- the remaining fragility is concentrated in runtime state maintenance
- the proposed exchange-backed desired-state model attacks exactly that failure surface
- it can preserve functionality while reducing complexity

**Bottom line:** this is a high-value architectural cleanup, not an aesthetic rewrite.
