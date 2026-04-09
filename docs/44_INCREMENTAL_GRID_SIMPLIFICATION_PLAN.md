# 44 - Incremental Grid Simplification Plan

**Status:** Completed incremental execution plan  
**Date:** 2026-04-09  
**Audience:** operator / runtime / grid / live-trading maintainers  
**Purpose:** convert the broad grid-runtime redesign discussion into a lower-risk, production-safe execution plan.

---

## 1. Executive Summary

### Short answer

We should **not** do a full grid runtime rewrite right now.

We **should** simplify the grid runtime incrementally, targeting the exact places where live canaries failed:

- position truth convergence
- exit topology maintenance
- inflight order handling
- entry replenishment after fills

### Why this is the recommended path

The current system is already good enough in many major areas:

- autonomous risk path works
- fail-closed orchestration works
- leverage enforcement works
- single-symbol live trading works
- multi-symbol activation works
- controller/orchestration wiring is now much cleaner

The remaining pain is narrower:

- transient off-grid exits under burst fills
- transient off-grid entries under burst fills / reseed transitions
- flat reseed churn / repeated full-grid rebuilds
- soft inventory / notional caps under already-resting order bursts
- weak observability when topology is healthy-but-lagged vs genuinely broken

This means:

- we do not need an architectural revolution yet
- we need a sequence of targeted runtime simplifications

### Recommendation

Proceed with **5-8 narrow PRs** that simplify the runtime in production-safe slices:

1. position truth convergence
2. exit-order desired-state diff / repair hardening
3. explicit inflight-aware reconciliation
4. price-aware alignment for off-grid orders
5. flat reseed anti-churn + burst hardening
6. retire old repair paths gradually

This yields most of the expected benefit with far less delivery risk than a full V2 rewrite.

---

## 2. Why the Broad V2 Idea Is Not the Right Immediate Plan

The broad `Grid Runtime V2` direction is still useful as a north star, but it is too large as the next engineering move.

### Main reasons

1. **Lot-vs-net-position is still unresolved**

This is the key design question:

- are exits tracked per lot?
- or is exit maintenance based on net position?

Until that is answered, a full desired-state runtime design remains under-specified.

2. **Inflight order state does not disappear**

Even under a desired-state diff model, you still need local inflight tracking:

- place sent, not visible yet in snapshot
- no WS update yet
- planner must not place the same order again

So a redesign does not remove this complexity; it must absorb it explicitly.

3. **Rate limits remain a real operational constraint**

Naive desired-state diff can produce:

- repeated cancel/place storms
- volatile churn under fast markets
- reconnect/recovery bursts

This must be solved incrementally and measured on real canaries.

4. **Current bugs are already concrete and local**

Today we already know where real production pain is:

- ledger hydration
- exit repair
- rebuild after fills

These can be improved directly without waiting for a theoretical clean-sheet system.

---

## 3. Correct Restatement of the Goal

The goal is **not**:

- rewrite the whole grid runtime
- abandon WS-first logic
- switch to snapshot-only truth
- remove rolling behavior

The goal **is**:

- keep strategy behavior
- keep rolling-window semantics
- keep one-symbol-per-engine model
- but make order/position maintenance **simpler, more explicit, and easier to recover**

---

## 4. What We Want to Preserve

These are not up for redesign right now:

- rolling window entry behavior
- one-sided inventory
- per-lot exit semantics, unless explicitly revisited later
- `5 / 15 / 16 / 20` ladder
- force-reduce / forced-flat behavior
- autonomous risk sizing
- multi-symbol orchestration
- leverage handling

The simplification target is only the runtime maintenance mechanism.

---

## 5. What We Already Learned from Live Canaries

### Proven-good layers

Live canaries already proved:

- risk path is credible
- activation path is credible
- leverage enforcement is real
- rolling can work under healthy conditions
- multi-symbol activation itself is possible

### Proven-painful layers

Live canaries also proved:

- position truth can stay divergent too long
- exit repair can enter `BLOCKED`
- some symbols degrade into partial topology maintenance
- recovery is harder to reason about than it should be

### What has now been fixed already

The following failure classes were reproduced live and then fixed incrementally:

- orphan deadlock in multi-symbol runtime
- persistent `POSITION_MISSING_IN_LEDGER`
- stale exit repair `REREGISTERED ... BLOCKED`
- semantic ambiguity around `15 / 16 / 20`
- transient false missing exits caused by inflight lag

This validates the incremental path itself:

- the runtime can be improved safely in production slices
- live canaries are catching the right classes of defects
- a full rewrite is still not justified

### What remains after those fixes

The live runtime is healthier, but still not fully "clockwork":

- entries and exits can still be temporarily off-grid under burst conditions
- flat reseed can churn too aggressively
- already-resting entry orders can still overshoot soft caps
- logs still make it too hard to distinguish normal inflight lag from real topology problems

This is exactly the kind of problem that benefits from incremental simplification.

---

## 6. Core Design Decision That Must Stay Explicit

### Lot-level exits vs net-position exits

This is the central unresolved design choice.

Current grid_v2 is much closer to:

- **per-lot exit ownership**

That means:

- each filled entry creates a lot
- each lot owns one exit
- exit pricing depends on entry price and step

### Why this matters

If you move to net-position-only exit logic, you simplify runtime a lot, but you also change product behavior:

- different exit shape
- different attribution of fills
- different recovery semantics

That is not just an implementation choice.
It is a product decision.

### Current recommendation

Do **not** reopen that product decision yet.

For the next phase:

- keep per-lot semantics
- simplify maintenance around them
- revisit net-position exits only if incremental simplification still fails

---

## 7. Practical Runtime Simplification Strategy

### Principle

Do not jump directly to a universal desired-state engine.

Instead:

- introduce desired-state diff **where the current pain is sharpest**
- validate it live
- only then extend it

### Why exit-side first

Exit maintenance is:

- critical for safety
- directly implicated in recent failures
- easier to formalize than the full entry + exit + rolling system

For exits, the rule is very clear:

- for each open lot, there should be one correct exit order

That is a clean target for a diff-based subsystem.

### Why entry-side second

Entry replenishment is more coupled to:

- rolling window movement
- branch state
- bounded side counts
- anti-churn behavior

So it should come after exit-side maintenance is stable.

### Industrial patterns worth explicitly adopting

The following patterns are genuinely useful and fit this incremental plan.

1. **Periodic reconcile as convergence guarantee**

- do not rely only on WS fill events
- keep a periodic reconcile cycle that compares desired vs effective actual state
- this is the safety net for delayed or missed exchange visibility

This does **not** imply snapshot-only truth.
The correct model remains:

- WS-first for fast reaction
- snapshot/reconcile for convergence

2. **Inflight tracking with bounded age / expiry**

- place sent but not visible yet must count as effectively present
- cancel sent but not reflected yet must count as effectively absent
- inflight state should not live forever

This is required for both entries and exits.

3. **Price-aware matching for off-grid orders**

It is not enough to know whether an order exists.
We also need to know whether it stands on the correct grid level.

Useful distinctions:

- exact match
- fuzzy-valid match (within tolerance)
- mispriced/off-grid match
- orphan
- missing

This matters because `geometry.py` already has useful matching logic, while `sync_reconciler.py` is still closer to exact-key diff.

4. **Priority ordering for corrective actions**

Not all diff actions are equal.
Under budget pressure, priority should be:

- dangerous/wrong exits first
- missing exits second
- mispriced exits next
- missing entries after that
- entry cleanup last

This keeps safety ahead of revenue and cleanup.

5. **Keep batch seed and batch flat-reseed**

This is a hard operational requirement.

We must preserve:

- startup seed placement as a batch
- reseed after full return to `FLAT` as a batch

The runtime may simplify maintenance, but must not regress to slow one-by-one reseeding after flat.

### Industrial patterns we should NOT jump to yet

These may become useful later, but are not the right immediate move:

- full reconciler-only authority in one jump
- anchor snap / aggressive grid recentering on volatility spikes
- generation/epoch anti-churn machinery before simpler fixes are exhausted

They are ideas for later, not the next production-safe slice.

---

## 8. Detailed Risk Audit

### 8.1 Risk: we accidentally build two runtime authorities

If we add new diff-based logic while leaving old repair logic fully active, we can end up with:

- legacy repair trying to patch topology
- new diff layer trying to enforce topology
- contradictory actions

**Mitigation**

- introduce one new authority at a time
- clearly define which path owns exit maintenance during each phase
- disable or narrow superseded repair code as soon as parity is proven

### 8.2 Risk: inflight order duplication

This is the invisible but critical problem.

Scenario:

- place sent
- exchange has not surfaced it yet
- WS event not yet received
- snapshot still stale
- diff sees missing order and wants to place again

**Mitigation**

- keep explicit inflight tracking as a first-class component
- desired-state diff must operate on:
  - actual open orders
  - plus pending local intents

### 8.3 Risk: rate-limit and churn amplification

Diff can overreact if every small mismatch turns into `CANCEL + PLACE`.

**Mitigation**

- bounded action batches
- cooldowns / debounce
- distinguish meaningful mismatch from transient pending state
- never full-reconcile blindly on every small drift

### 8.4 Risk: reduce-only / budget guards still block

Replacing repair with diff does not magically eliminate:

- reduce-only invalidity
- insufficient remaining position
- notional constraints

These same constraints can block desired actions repeatedly.

**Mitigation**

- make block reasons explicit
- prefer recomputation from fresh truth over stale blind retry
- classify mismatch as:
  - actionable now
  - deferred
  - structurally invalid

### 8.5 Risk: partial fills and lot fragmentation

Partial fills are one of the hardest realities.

The simplification plan must not assume:

- fills always align perfectly with one level
- all lots are neat and complete

**Mitigation**

- keep explicit lot semantics
- build exit diff around actual open lots
- make residual qty handling a tested contract

### 8.6 Risk: healthy symbols regress while fixing bad ones

Some symbols already behave fine.

**Mitigation**

- every PR must preserve healthy-path behavior
- replay both:
  - failing symbol path
  - healthy symbol path
- use shadow comparisons when feasible

### 8.7 Risk: team gets trapped in “V2 is coming” mode

This is a real process risk:

- production keeps running on old code
- current bugs stay unfixed
- all energy goes into the future architecture

**Mitigation**

- each PR must improve live runtime on its own
- no long-lived “waiting for V2” branch

### 8.8 Risk: off-grid orders are treated as extra + missing instead of one correction

An order can exist but still be wrong:

- wrong price
- wrong level
- stale geometry after drift/reseed

If the reconciler only uses exact-key presence checks, this becomes:

- one extra order
- one missing order

That is too coarse.

**Mitigation**

- add price-aware matching
- treat wrong-price cases as one correction unit
- prefer paired correction planning over unrelated cancel/place noise

Note: this is not true exchange atomicity.
It is a **paired correction unit** in planning/dispatch semantics.

### 8.9 Risk: burst overshoot despite "correct" inventory counting

The runtime already counts open lots and desired entries.
That alone is not enough to guarantee a hard cap, because:

- orders can already be resting on exchange
- fills can arrive faster than those orders can be canceled

So current caps are still partly prospective / soft under burst conditions.

**Mitigation**

- explicit burst hardening
- headroom or outer-level proactive cancel
- do not assume that "we track lot count" implies "we cannot overshoot"

---

## 9. Completed Execution Chain

This plan is now complete.

### Completed foundation slices

These validated the direction of the plan:

- position truth convergence
- exit budget priority fix
- inflight-aware exit reconciliation
- semantics cleanup around `15 / 16 / 20`

### Completed incremental slices

The following runtime slices were implemented and merged on `main`:

- entry-side inflight-aware reconciliation
- flat reseed anti-churn cooldown
- inventory headroom burst protection
- grid observability cleanup

### What this means

`doc-44` is no longer an active backlog.

It should now be read as:

- the completed stabilization plan
- the justification for why incremental runtime hardening was the right path
- the closure record for the `#642`-`#649` grid-maintenance chain

### What comes next

The next phase is tracked separately in:

- `docs/45_GRID_RUNTIME_PHASE2_PLAN.md`

That next phase is **not** a continuation of the same stabilization backlog.
It is the follow-up improvement phase focused on:

- price-aware reconciliation for off-grid orders
- corrective-action priority
- more explicit inflight lifetime semantics

Goal:

- recognize orders that exist but stand on the wrong grid level
- add fuzzy / tolerance-aware matching
- classify wrong-price cases as correction candidates, not naive `extra + missing`

### PR-3: Flat reseed anti-churn

Goal:

- reduce repeated full reseeds around flat oscillation
- keep batch reseed semantics

### PR-4: Burst hardening for inventory/notional caps

Goal:

- reduce overshoot caused by already-resting entry orders
- strengthen runtime behavior under fast one-sided fill cascades

### PR-5: Observability cleanup

Goal:

- make suppression reasons explicit
- make inflight-vs-broken topology easier to distinguish from logs

### PR-6: Remove superseded repair logic

Goal:

- retire the now-obsolete patchwork paths
- reduce runtime ambiguity and hidden interactions

### PR-7: Optional broader cleanup

Only after live validation of the above:

- revisit whether broader `RuntimeSymbolState` consolidation is still needed

---

## 10. Expected Wins from the Incremental Path

If the sequence above works, we should get:

- much more reliable exit maintenance
- much more reliable entry maintenance
- clearer recovery after fills
- fewer off-grid orders that temporarily "exist but stand wrong"
- less dependence on obscure repair paths
- fewer permanent divergence states
- less reseed churn
- cleaner logs and easier debugging

And importantly:

- every intermediate stage is valuable on its own

This is much better for production than a large rewrite.

---

## 11. What We Are Deliberately Not Solving Yet

To keep this tractable, we are not deciding yet:

- whether net-position exits should replace per-lot exits
- whether a full `RuntimeSymbolState` authority should replace all current subcomponents
- whether grid runtime should be rewritten around a new planner entirely

Those can be revisited later if incremental simplification proves insufficient.

---

## 12. Success Criteria

This plan is successful if, after 2-3 live canary iterations:

- no persistent `POSITION_MISSING_IN_LEDGER`
- no blocked exit repair leading to degraded protection
- exit side does not repeatedly show transient missing coverage under burst
- entry replenishment continues correctly after exit fills
- entry side does not repeatedly drift off-grid under burst/reseed lag
- startup seed remains batch
- flat reseed remains batch
- healthy symbols remain healthy
- multi-symbol live runtime stays stable

If these are achieved, a large rewrite is unnecessary.

If these are **not** achieved after the incremental plan, then a broader runtime redesign becomes justified.

---

## 13. Estimated Scope

Very rough effort estimate:

- incremental path: **5-8 PRs**
- each PR is testable and can be validated in live canaries

Compared with:

- broad Grid Runtime V2: likely **30-50 PRs**
- much larger production risk
- much higher coordination cost

This is a major reason to prefer the incremental path now.

---

## 14. Final Recommendation

The broad architectural instinct was good:

- simplify runtime
- reduce repair magic
- make grid behavior easier to reason about

But the **recommended execution path** is narrower:

- do not rewrite the whole runtime now
- simplify the most painful seams one by one
- start with exits
- preserve current product semantics
- validate every step live

**Bottom line:** the likely best path is not “Grid Runtime V2 now”, but “incremental grid simplification with exit-first desired-state diff and explicit inflight handling.” 
