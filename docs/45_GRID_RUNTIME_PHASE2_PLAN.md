# 45 - Grid Runtime Phase 2 Plan

**Status:** Active next-phase implementation plan  
**Date:** 2026-04-09  
**Audience:** operator / runtime / grid / live-trading maintainers  
**Purpose:** capture what remains after `doc-44` is complete, using both live canary evidence and the follow-up industrial-pattern review.

---

## 1. Executive Summary

`doc-44` is complete.

The runtime is now much healthier:

- control-plane deadlocks were removed
- position truth convergence was fixed
- stale blocked exit repair was fixed
- inflight-aware exit and entry reconciliation were added
- flat reseed churn was reduced
- burst headroom protection was added
- observability was improved

This means the next phase is **not** "fix basic runtime survival."

The next phase is:

- make reconciliation more geometry-aware
- make action selection more safety-prioritized
- make inflight handling more explicit and bounded
- decide which larger architectural patterns are worth adopting next, and which should still wait

Current baseline:

- `main @ 79d9453`
- `doc-44` execution chain completed through `#649`

---

## 2. Reference Point

This phase is based on:

- [44_INCREMENTAL_GRID_SIMPLIFICATION_PLAN.md](/home/benya/Project/grinder/docs/44_INCREMENTAL_GRID_SIMPLIFICATION_PLAN.md)
- live canary evidence gathered during the `#640`-`#649` stabilization chain
- follow-up engineering review of industrial production patterns for grid trading runtimes

---

## 3. What Phase 1 Already Delivered

The following were the core outcomes of `doc-44` execution:

- position truth convergence
- exit budget priority repair
- inflight-aware exit reconciliation
- inflight-aware entry reconciliation
- flat reseed anti-churn cooldown
- inventory headroom burst protection
- explicit topology observability
- semantics cleanup around `15 / 16 / 20`

This closed the main canary-discovered failure classes:

- orphan deadlock
- persistent `POSITION_MISSING_IN_LEDGER`
- stale exit repair `BLOCKED`

---

## 4. Comparison Against The Follow-Up Design Review

Below is the honest mapping of "what the colleague suggested" vs "what is already implemented."

### 4.1 Desired-state reconciliation

**Status:** Partially implemented

Implemented:

- periodic reconcile exists
- entry and exit diffing exist
- inflight-aware effective actual state exists

Not yet true:

- reconciler is not the single authority for all maintenance
- legacy integrity / repair paths still exist alongside reconcile

### 4.2 Inflight tracking with explicit TTL contract

**Status:** Partially implemented

Implemented:

- pending place / pending cancel tracking
- inflight-aware reconcile behavior for entries and exits

Not yet true:

- there is no single, explicit inflight TTL contract that says:
  - "pending counts as effective truth until age N"
  - "after age N, expire and re-evaluate"

### 4.3 Generation / epoch anti-churn

**Status:** Not implemented

Current system uses:

- cooldowns
- bounded reconcile
- headroom

It does **not** yet use generation-based anti-churn.

### 4.4 Priority queue for corrective actions

**Status:** Not implemented as a unified concept

There is deterministic ordering, but not yet a formal safety-first priority queue that guarantees:

1. dangerous exits first
2. missing exits second
3. mispriced exits next
4. missing entries after that
5. cleanup last

### 4.5 Anchor snap / snap-to-price

**Status:** Not implemented

And this is still considered **optional / product-affecting**, not an obvious next bugfix.

### 4.6 Two-phase reconciliation

**Status:** Partially present in spirit, not formalized

The runtime already behaves more "exit-first" than "entry-first," but there is no explicit fast-safety / slow-revenue reconcile split yet.

### 4.7 Price-aware off-grid alignment

**Status:** PR-1 open

Implemented:

- `geometry.py` has fuzzy matching
- legacy integrity repair path in `engine.py` uses it for entry geometry repair
- PR `#650` brings fuzzy entry matching into `sync_reconciler.py`

Still not fully implemented:

- exit repair still focuses on presence / absence more than wrong-price correction
- there is no unified correction model for "this order exists but stands on the wrong grid level"
- entry-side price-aware correction still needs review, merge, and live validation

This is the single most important unresolved item from the review.

### 4.8 What phase 2 should not overreach into

The follow-up review also suggested larger patterns that are valid ideas,
but are deliberately **not** immediate PR-1 work:

- reconciler-only authority in one jump
- generation/epoch anti-churn machinery
- anchor snap / snap-to-price behavior
- full planner/runtime replacement

Those stay behind stronger evidence gates.

---

## 5. Core Constraints For Phase 2

The following must remain true:

- WS-first live behavior remains
- snapshot/reconcile remains the convergence path, not the only truth source
- per-lot exit semantics remain
- rolling-window behavior remains
- startup seed remains batch
- reseed after full `FLAT` remains batch
- no big-bang full rewrite

---

## 6. The Main Unfinished Problem

After `doc-44`, the most important remaining runtime weakness is:

### Orders that exist, but are wrong

The system is now much better at:

- missing orders
- stale orders
- inflight lag

But it is still weaker than it should be at:

- entry exists, but not on the right grid price
- exit exists, but not on the right lot-derived price
- mispriced order being treated as `extra + missing` instead of one correction

This is where the next phase should focus first.

---

## 7. Recommended Phase 2 Sequence

### PR-1: Price-aware reconciliation for entries

**Status:** PR open (`#650`)

Goal:

- integrate `match_entries_with_tolerance(...)` into `sync_reconciler.py`
- distinguish:
  - exact match
  - fuzzy-valid match
  - mispriced entry
  - truly missing entry
  - truly extra entry

Why first:

- the matching logic already exists
- it is already proven useful in the legacy integrity path
- moving it into reconcile is a high-leverage improvement

Acceptance criteria:

- off-grid entry orders are no longer treated only as `extra + missing`
- fuzzy-valid entries are left alone
- no duplicate churn is introduced
- batch seed / batch flat-reseed behavior remains unchanged

### PR-2: Price-aware reconciliation for exits

Goal:

- for each open lot, compare actual exit price vs expected exit price with tolerance
- treat wrong-price exits as correction candidates, not only presence/absence issues

Acceptance criteria:

- wrong-price exits are explicitly detectable
- correction planning for exits becomes geometry-aware
- per-lot exit semantics remain unchanged

### PR-3: Unified corrective-action priority queue

Goal:

- formalize action priority across reconcile/repair paths

Recommended order:

1. cancel dangerous/wrong exits
2. place missing exits
3. correct mispriced exits
4. place missing entries
5. cancel stale entries

Acceptance criteria:

- safety-critical fixes win under bounded action budgets
- cleanup no longer crowds out protection work

### PR-4: Explicit inflight TTL contract

Goal:

- make inflight behavior explicit and bounded

Desired semantics:

- pending place counts as effectively present until visible or expired
- pending cancel counts as effectively absent until reflected or expired
- expired inflight returns to normal evaluation

Acceptance criteria:

- inflight lifetime becomes easy to reason about
- stale pending state no longer depends on scattered local cleanup rules

### PR-5: Formal two-phase reconciliation

Goal:

- split maintenance into:
  - fast safety reconcile
  - slower revenue / cleanup reconcile

Why later:

- useful, but lower leverage than price-aware matching and priority

Acceptance criteria:

- exits are protected even under heavy churn
- entry maintenance remains productive without stealing safety budget

---

## 8. Optional Later Work

These are not part of the recommended immediate phase unless new canaries justify them:

### 8.1 Generation / epoch anti-churn

Useful if:

- cooldown-based anti-churn still leaves too much cancel/place churn

Not needed yet if simpler fixes are enough.

### 8.2 Anchor snap / snap-to-price

Useful if:

- canaries show the grid remains too far from market for too long after extreme volatility

But this is a strategy/product decision, not just a maintenance fix.

### 8.3 Reconciler-only authority

Potential long-term simplification, but not the next step.

Only revisit if:

- price-aware reconciliation
- priority queue
- inflight TTL

still leave too much duplicated repair logic around.

---

## 9. What We Are Deliberately Not Doing Yet

To keep this phase safe and production-shaped, we are not yet:

- switching to net-position exits
- doing a full planner/runtime rewrite
- forcing an anchor snap model into strategy behavior
- replacing all repair paths in one jump

---

## 10. Success Criteria

This phase is successful if future live canaries show:

- off-grid orders are rare and corrected deterministically
- wrong-price orders are no longer misclassified as simple missing/extra noise
- bounded action budgets prioritize safety correctly
- inflight lag is easier to reason about and does not create long-lived ambiguity

If those outcomes are achieved, the runtime moves from "stabilized" toward "production-clean."

---

## 11. Bottom Line

`doc-44` proved that incremental stabilization works.

The next phase should **not** restart a broad V2 discussion.

The right next move is narrower:

- make reconciliation geometry-aware
- prioritize corrective actions properly
- formalize inflight lifetime rules

That gives the highest expected value without reopening a full rewrite.
