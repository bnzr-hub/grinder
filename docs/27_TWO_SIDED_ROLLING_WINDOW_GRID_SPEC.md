# 27 - Two-Sided Rolling Window Grid v1 Specification

**Status:** Proposed
**Date:** 2026-03-17
**Supersedes conceptually:** `docs/26_ROLLING_INFINITE_GRID_SPEC.md` (ADR-085) as target architecture.
Doc-26 remains as legacy/experimental reference. No code changes in this PR.

---

## 1. Goal

Build a **two-sided rolling-window grid** with:

* bounded number of active **entry** levels per side
* separate **inventory ledger** tracking open lots
* separate **exit order** management (outside entry window)
* one-sided inventory mode (no mixed long+short)
* full recenter **only in flat**
* deterministic, testable state machine

### Why replace the old model

The rolling/infinite-grid line (doc-26, ADR-085) grew organically through 11+ sub-versions (V1A-V1G),
accumulating complexity that is hard to reason about, hard to verify with live proof, and hard to extend.
Key problems:

1. **No entry/exit separation.** Grid orders and TP/exit orders share the same planner space,
   causing ownership conflicts (TP slot exclusion, `diff_extra_tp`, cross-tick overlap).
2. **No explicit inventory model.** Fill detection relies on heuristics (disappearance, inflight CID tracking),
   not on a proper lot ledger. Reconciliation is fragile.
3. **Unbounded outward growth.** The "infinite ladder" concept has no inherent window bound.
   Budget exhaustion and order churn are managed via bolted-on guards (freeze, anti-churn, convergence).
4. **Recenter while exposed.** The old model recenters on `mid_price` continuously,
   losing grid position relative to open fills.

The new model starts from a clean state machine with explicit separation of concerns.

---

## 2. Non-goals for v1

NOT supported in v1:

* simultaneous `long + short` inventory (mixed/hedge mode)
* infinite outward grid growth
* mid-driven continuous recenter while inventory open
* adaptive spacing / ML / regime-aware spacing
* partial close optimization beyond explicit lot accounting
* cross-symbol portfolio coordination

---

## 3. Core definitions

### 3.1 Entry order

An order that **opens** a new inventory lot.

* `BUY entry` below the current working zone
* `SELL entry` above the current working zone

Entry orders belong to the rolling window.

### 3.2 Exit order

An order that **closes** an already-open lot.

* for `LONG lot` -> `SELL exit` one step above entry price
* for `SHORT lot` -> `BUY exit` one step below entry price

Exit orders are **NOT part of the entry window**. They are owned by the inventory/exit ledger.

### 3.3 Rolling window

A bounded set of active entry levels:

* `N_buy` levels below reference
* `N_sell` levels above reference

When market moves, the window **rolls** (not expands):

* adds a new far level in the direction of movement
* removes the far level from the opposite side

### 3.4 Inventory lot

A tracked position opened by an entry fill. Has exactly one paired exit order (or pending exit intent).

### 3.5 Branch mode

The current inventory direction: `FLAT`, `LONG_BRANCH`, or `SHORT_BRANCH`.

### 3.6 Reference price

The center around which the entry window is built. Updated only on flat recenter.

### 3.7 Flat recenter

Full normalization of the entry window around a new reference price.
Allowed **only when inventory is empty** (mode == FLAT).

---

## 4. Hard invariants

Violation of any invariant = bug.

### I1. Bounded entry window

Active entry orders are always bounded:

* `len(buy_entries) <= N_buy`
* `len(sell_entries) <= N_sell`

### I2. Exit separation

Rolling/recenter logic **never** cancels or moves exit orders of open lots.

### I3. One lot -> one exit

Every open inventory lot has exactly one paired exit order or pending-exit intent.

### I4. One-sided inventory only

At any given time, the system is in exactly one mode:

* `FLAT` (no inventory)
* `LONG_BRANCH` (one or more long lots, zero short lots)
* `SHORT_BRANCH` (one or more short lots, zero long lots)

Mixed inventory is forbidden.

### I5. Full recenter only in flat

Full entry window recenter is permitted only when inventory is empty.

### I6. Exchange truth reconciliation

Local order and position state must be reconstructable from exchange/account snapshot without ambiguity.

### I7. Deterministic transitions

Given the same input event sequence, the state machine must produce the same output.

### I8. No duplicate inventory from same fill

A single fill event must create at most one inventory lot.
Reconciliation must be idempotent.

---

## 5. Strategy parameters

Minimum required parameter set:

| Parameter | Type | Description |
|-----------|------|-------------|
| `grid_step_pct` | Decimal | Grid step size, e.g. `0.5%` |
| `entry_levels_per_side` | int | Number of entry levels per side |
| `order_size_mode` | enum | `fixed` (v1 only) |
| `order_size` | Decimal | Size per order |
| `max_inventory_levels` | int | Hard cap on open lots |
| `max_inventory_notional_usd` | Decimal | Hard cap on total inventory notional |
| `max_open_entries_total` | int | Max entry orders on exchange |
| `recenter_policy` | enum | `flat_only` (v1 only) |
| `inventory_mode` | enum | `one_sided_only` (v1 only) |

Recommended v1 starting values:

* `grid_step_pct = 0.5%`
* `entry_levels_per_side = 8..12`
* `order_size_mode = fixed`
* `inventory_mode = one_sided_only`
* `recenter_policy = flat_only`

---

## 6. State model

### 6.1 Top-level mode

```text
Mode = FLAT | LONG_BRANCH | SHORT_BRANCH
```

### 6.2 Entry window state

```text
EntryWindow:
  reference_price: Decimal
  buy_entry_prices: list[Decimal]
  sell_entry_prices: list[Decimal]
  levels_per_side: int
  step_pct: Decimal
```

### 6.3 Inventory lot

```text
InventoryLot:
  lot_id: str
  side: LONG | SHORT
  entry_price: Decimal
  qty: Decimal
  opened_at_ts: int
  source_entry_order_id: str
  exit_price: Decimal
  exit_order_id: str | None
  status: OPEN | CLOSING | CLOSED
```

### 6.4 Inventory ledger

```text
InventoryLedger:
  open_lots: list[InventoryLot]
  closed_lots: list[InventoryLot]   # optional archive / runtime history
```

### 6.5 Exit order

```text
ExitOrder:
  exit_order_id: str
  lot_id: str
  side: BUY | SELL
  price: Decimal
  qty: Decimal
  status: OPEN | FILLED | CANCELED | REJECTED
```

### 6.6 Strategy runtime state

```text
GridRuntimeState:
  mode: FLAT | LONG_BRANCH | SHORT_BRANCH
  entry_window: EntryWindow
  inventory_ledger: InventoryLedger
  exit_orders: dict[exit_order_id, ExitOrder]
  pending_actions: list[ActionIntent]
  last_exchange_snapshot_ts: int | None
  last_recenter_ts: int | None
```

---

## 7. Order ownership contract

SSOT for order ownership.

### Entry window owns:

* buy-entry orders
* sell-entry orders

### Inventory / exit ledger owns:

* paired exit orders for open lots

### Rolling controller may:

* add/remove entry orders
* **never** cancel valid open exit orders (except emergency/operator cleanup)

### Exit manager may:

* create exit for a newly opened lot
* mark exit filled/canceled/rejected
* **never** mutate entry window (except by emitting domain event that a lot closed)

---

## 8. Mode semantics

### 8.1 FLAT

* inventory empty
* no required open exits
* entry window active on both sides
* recenter allowed

### 8.2 LONG_BRANCH

* at least one open long lot
* no short lots allowed
* each long lot has paired sell exit
* entry window stays two-sided but rolls with each accepted buy fill
* opposite-side sell-entry fills are rejected while mode remains `LONG_BRANCH` (`BRANCH_INCOMPATIBLE`)

### 8.3 SHORT_BRANCH

* at least one open short lot
* no long lots allowed
* each short lot has paired buy exit
* entry window stays two-sided but rolls with each accepted sell fill
* opposite-side buy-entry fills are rejected while mode remains `SHORT_BRANCH` (`BRANCH_INCOMPATIBLE`)

---

## 9. Initial placement rules

On init in `FLAT`, for reference price `P` and step `S`:

* place `BUY entry` at: `P - 1*S`, `P - 2*S`, ..., `P - N*S`
* place `SELL entry` at: `P + 1*S`, `P + 2*S`, ..., `P + N*S`

No exit orders exist in flat.

---

## 10. Event model

Minimum required domain events:

```text
Event =
  MarketTick(mid_price, ts)
  EntryFilled(order_id, side, price, qty, ts)
  ExitFilled(exit_order_id, lot_id, price, qty, ts)
  OrderCanceled(order_id, ts)
  OrderRejected(order_id, reason, ts)
  AccountSnapshotRefreshed(snapshot, ts)
  EmergencyStopTriggered(ts)
  OperatorCleanup(ts)
  RecenterRequested(ts)
```

---

## 11. Transition rules

### 11.1 FLAT + EntryFilled(BUY)

Preconditions: mode == FLAT, filled order is valid buy-entry.

Actions:
1. Create new `LONG lot` in inventory ledger
2. Create paired `SELL exit` at `entry_price + 1 step`
3. Remove filled buy-entry from window
4. Add new farthest buy-entry one step lower than current lowest
5. Remove one farthest sell-entry level (opposite-edge trim)
6. mode -> `LONG_BRANCH`

Post-condition: entry window remains bounded and two-sided; opposite side is trimmed by one level.

### 11.2 FLAT + EntryFilled(SELL)

Preconditions: mode == FLAT, filled order is valid sell-entry.

Actions:
1. Create new `SHORT lot`
2. Create paired `BUY exit` at `entry_price - 1 step`
3. Remove filled sell-entry from window
4. Add new farthest sell-entry one step higher than current highest
5. Remove one farthest buy-entry level (opposite-edge trim)
6. mode -> `SHORT_BRANCH`

Post-condition: entry window remains bounded and two-sided; opposite side is trimmed by one level.

### 11.3 LONG_BRANCH + EntryFilled(BUY)

Preconditions: mode == LONG_BRANCH, no short lots exist, filled order is valid buy-entry.

Actions:
1. Create another `LONG lot`
2. Create paired `SELL exit` one step above its entry
3. Remove filled buy-entry from window
4. Add new farthest buy-entry one step lower than current lowest
5. Remove one farthest sell-entry level (opposite-edge trim)
6. mode remains `LONG_BRANCH`

Note: rolling trim applies on every accepted branch-compatible entry fill.

### 11.4 SHORT_BRANCH + EntryFilled(SELL)

Symmetric to 11.3, including one-level opposite-edge trim per accepted fill.

### 11.5 LONG_BRANCH + ExitFilled(SELL)

Preconditions: exit belongs to an open long lot.

Actions:
1. Mark corresponding long lot `CLOSED`
2. Remove/close corresponding exit order
3. If open long lots remain: mode remains `LONG_BRANCH` and emit one `PLACE_ENTRY(SELL)` to restore the trimmed far edge
4. If no open lots remain: mode -> `FLAT`; by default entry window is immediately reseeded
   symmetrically around the preserved reference price (`reseed_on_flat=True`), and may be
   preserved as-is when `reseed_on_flat=False`

### 11.6 SHORT_BRANCH + ExitFilled(BUY)

Symmetric to 11.5, restoring one `PLACE_ENTRY(BUY)` while short lots remain open.

---

## 12. Architectural decisions (v1 choices)

These are binding for v1 implementation.

### D1. Active branch mode: one-sided inventory, two-sided rolling entries

**Choice: YES.**
When branch is active, inventory remains one-sided, but entry window remains two-sided and bounded.
Each accepted branch-compatible fill rolls the window and trims one farthest level on the opposite side.
Opposite-side entry fills are rejected by branch gate while branch mode is active.

In `LONG_BRANCH`: accepted fills are `BUY` only.
In `SHORT_BRANCH`: accepted fills are `SELL` only.

**Rationale:** preserves rolling two-sided market coverage while keeping mixed inventory forbidden.

### D2. Full recenter: flat only

**Choice: YES.**
Recenter allowed only when mode == FLAT and inventory empty.

### D3. Mixed inventory: forbidden in v1

**Choice: YES.**
No simultaneous long + short lots. One-sided inventory only.

### D4. Exit ownership: exits outside rolling window

**Choice: YES.**
Exit orders are managed by the inventory/exit ledger, not the entry window.
Rolling/recenter logic never touches exits.

### D5. Flat normalization: configurable (default soft recenter)

**Choice: Option B -- soft recenter.**
When branch fully unwinds to FLAT, the default behavior is to reseed the entry
window to the standard symmetric shape around the preserved reference price.
Optional preserve mode skips full recenter on unwind and keeps rolling continuity.
Optional explicit `RecenterRequested` may still rebuild it symmetrically around a new reference.

**Rationale:** reseed remains safe default against drift; preserve mode is an
operator switch for continuity-sensitive live runs.

---

## 13. Recenter policy

### Allowed

Full recenter only if ALL conditions met:

* mode == FLAT
* inventory_ledger.open_lots is empty
* no unresolved exit orders remain

### Forbidden

Recenter forbidden if ANY condition true:

* any lot is open
* any required exit is still active
* emergency cleanup not completed

---

## 14. Window maintenance during active branch

### During LONG_BRANCH

* maintain buy-entry chain below (up to `N_buy` levels)
* maintain sell-entry chain above (opposite side, trimmed by rolling)
* sell-exit orders above for open long lots
* opposite-side fill attempts are rejected by branch gate (`BRANCH_INCOMPATIBLE`)

### During SHORT_BRANCH

* maintain sell-entry chain above (up to `N_sell` levels)
* maintain buy-entry chain below (opposite side, trimmed by rolling)
* buy-exit orders below for open short lots
* opposite-side fill attempts are rejected by branch gate (`BRANCH_INCOMPATIBLE`)

---

## 15. Reconciliation contract

Source of truth after exchange refresh:

* open orders from exchange snapshot
* actual position snapshot
* executed fills / order status if available

### Required reconciliation outcomes

For every known order: `OPEN | FILLED | CANCELED | REJECTED | MISSING_UNCERTAIN`

### Rules

1. Entry fill creates lot only once (idempotent).
2. Exit fill closes lot only once (idempotent).
3. Missing exit order for open lot = **critical inconsistency** -> immediate repair attempt.
4. Missing entry order without fill evidence must not silently mutate inventory.
5. Emergency reconciliation may flatten to restore consistency.
6. Restart/reload must reconstruct state from snapshot without ambiguity.

---

## 16. Risk rules

Mandatory constraints:

| Rule | Description |
|------|-------------|
| `max_inventory_levels` | Hard cap on open lot count |
| `max_inventory_notional_usd` | Hard cap on total inventory notional |
| `max_open_orders_total` | Hard cap on all orders on exchange |
| `max_new_orders_per_cycle` | Rate limit on order placement |
| `max_position_per_side` | Position size cap |
| emergency flatten | Flatten all on invariant breach |
| operator cleanup | Manual cleanup path (check/cleanup/verify) |

### Hard stop behavior

If any invariant breaks:

1. Stop placing new entries
2. Keep or restore exits if possible
3. Optionally emergency flatten depending on severity

---

## 17. Failure handling

### 17.1 Entry place failed

* do not create inventory lot
* keep window consistent
* may retry per execution policy

### 17.2 Exit place failed

* **critical severity** -- lot remains open but unprotected
* trigger immediate repair attempt
* repeated failure may escalate to emergency action

### 17.3 Snapshot lag / stale data

* never create duplicate inventory from same fill
* never drop exits due to window roll
* reconciliation must be idempotent

### 17.4 Missing entry order in reconciliation

An entry order that the engine expects to be OPEN but is absent from the exchange snapshot:

* **without fill evidence:** do NOT create an inventory lot. Mark order as `MISSING_UNCERTAIN`.
  Log warning. May re-place entry if window position still valid.
* **with fill evidence (trade record or position delta):** create inventory lot (idempotent).
  Treat as normal fill path.
* **never assume fill without evidence** -- silent lot creation from missing orders is forbidden (I8).

### 17.5 Missing exit order for open lot

An exit order that the engine expects to be OPEN but is absent from the exchange snapshot,
while its paired inventory lot is still OPEN:

* **critical severity** -- lot is open and unprotected.
* trigger immediate exit re-placement at the lot's exit price.
* if re-placement fails: escalate to operator alert. Repeated failure may trigger emergency flatten.
* this is the highest-priority reconciliation repair (a lot without an exit is unbounded risk).

### 17.6 Uncertain/missing order after restart

On engine restart, reconstruct state from exchange snapshot:

* for every open order on exchange: classify as entry or exit by CID prefix / metadata.
* for every open lot inferred from position: verify paired exit exists. If missing, repair (17.5).
* for orders that cannot be classified: mark `MISSING_UNCERTAIN`, do NOT cancel or assume ownership.
  Operator must resolve via cleanup path.
* **invariant:** restart must not create phantom lots or orphan exits. Reconstruction must be
  fully deterministic from the snapshot (I6, I7).

---

## 18. Minimal acceptance tests

Implementation cannot be merged without these tests passing.

### State machine tests

| # | Test |
|---|------|
| 1 | init flat window symmetrical |
| 2 | first BUY fill creates LONG lot + paired SELL exit |
| 3 | first SELL fill creates SHORT lot + paired BUY exit |
| 4 | long continuation adds lot + new lower entry + trims upper far edge |
| 5 | short continuation symmetric |
| 6 | long exit closes correct lot only |
| 7 | short exit closes correct lot only |
| 8 | full unwind returns to FLAT |
| 9 | flat recenter allowed |
| 10 | recenter blocked while inventory open |
| 11 | mixed inventory forbidden |
| 12 | exits never canceled by rolling controller |

### Reconciliation tests

| # | Test |
|---|------|
| 13 | duplicate fill event does not duplicate lot |
| 14 | missing snapshot update does not double-open inventory |
| 15 | exit fill closes one and only one lot |
| 16 | restart/reload reconstructs state consistently from snapshot |

### Risk tests

| # | Test |
|---|------|
| 17 | max inventory levels enforced |
| 18 | max inventory notional enforced |
| 19 | emergency stop blocks new entries |
| 20 | emergency cleanup produces flat + no open orders |

---

## 19. Live acceptance criteria

Before live-approval, must prove:

* no duplicate lot creation
* no lost exit orders
* no mixed inventory
* bounded active entry count
* successful flat normalization after full unwind
* cleanup/verify returns `status=CLEAN`

---

## 20. Implementation plan (PR stages)

### PR1 -- Spec only (this PR)

* add this spec doc
* mark old grid model legacy/experimental in STATE/DECISIONS
* no code changes, no behavior changes

### PR2 -- Pure state machine

* no exchange interaction
* deterministic unit tests
* full transition coverage (all 20 acceptance tests)

### PR3 -- Reconciliation adapter

* map snapshot/fills/orders into domain events
* replay tests

### PR4 -- Execution integration

* entry/exit order placement via exchange port
* repair paths for failed exits
* safety gates (risk rules)

### PR5 -- Paper/live shadow verification [DONE]

* `GridV2ShadowRunner` runs alongside legacy engine on same snapshots
* Isolated bridge (not shared with primary), no dispatch side effects
* `ShadowResult` with divergence detection (count/type mismatch)
* Mutual exclusion: shadow only when primary OFF
* Fail-open: shadow errors logged, never propagated
* Section 24 (24.1-24.5) added to spec
* 26 tests across 13 classes, all passing

### PR6 -- Small live ceremony [DONE]

* 1 level, 0.002 BTC, BTCUSDT futures, 180s canary run
* `GRID_V2_BRIDGE_STARTUP_OK` confirmed, 2 seed PLACE_ENTRY executed (BUY+SELL)
* `GRID_V2_AWAITING_SYNC_CLEARED` after first account sync
* 16167 ticks, 0 errors, 0 false fills, position FLAT throughout
* Post-cleanup: `EXCHANGE_STATE_VERIFY status=CLEAN`
* Price quantization fix: `price_tick_size` in GridV2Config, quantized in bridge
* Awaiting-sync fix: skip fill detection until first account sync after fresh start

---

## 21. PR2 contract clarifications

The subsections below close contract gaps discovered during PR2 design review.
They are normative for the pure state machine implementation.

### 21.1 Flat normalization contract

When the last open lot is closed (rules 11.5/11.6), the state machine transitions to
`mode = FLAT` and, by default, reseeds the entry window to the standard symmetric
create_initial shape around the preserved reference price.

If `reseed_on_flat=False`, unwind to FLAT emits no `RECENTER`/`RECENTER_REPLACE`
actions. Rolling restore semantics still apply, so missing side levels may be
replenished via `EXIT_RESTORE` to keep the ladder symmetric without recentering.

The state machine does not hold market data and does not need a separate
recenter request to restore symmetric coverage after full unwind.
The caller (execution/reconciliation layer, PR3+) may send explicit
`RecenterRequested(new_reference_price, ts)` to normalize around a new reference.

This is the PR2 interpretation of D5 ("soft recenter") as default behavior.
Runtime may opt into preserve mode (`reseed_on_flat=False`) where explicit
`RecenterRequested` remains the only recenter trigger.

### 21.2 OperatorCleanup semantics

`OperatorCleanup` is a **full state reset** event. Its semantics in the pure
state machine:

1. All `open_lots` → status `CLOSED` (each gets a new closed copy)
2. All `OPEN` exit orders → status `CANCELED`
3. Entry window → cleared (empty buy/sell tuples, reference preserved)
4. Mode → `FLAT`
5. `emergency_stopped` → `False`
6. Emits individual `CANCEL_ENTRY` for each entry price + individual `CANCEL_EXIT`
   for each open exit order

**Difference from EmergencyStopTriggered:**

| | EmergencyStop | OperatorCleanup |
|---|---|---|
| open lots | preserved (still open) | all closed |
| exit orders | preserved (I2: exits protect lots) | all canceled |
| entry orders | canceled (no new exposure) | canceled |
| mode | unchanged | FLAT |
| emergency_stopped | True | False |
| purpose | freeze (stop bleeding) | full reset (clean slate) |

### 21.3 Deterministic ordering contract

All collection fields in `GridV2Snapshot` have a fixed ordering guarantee:

| Field | Order | Rationale |
|-------|-------|-----------|
| `buy_entry_prices` | descending (closest to ref first) | natural for "next to fill" priority |
| `sell_entry_prices` | ascending (closest to ref first) | natural for "next to fill" priority |
| `open_lots` | chronological (oldest first, by `opened_at_ts`) | FIFO for exit pairing |
| `closed_lots` | chronological (oldest first) | audit trail order |
| `exit_orders` | same order as their paired lots | 1:1 correspondence |
| `actions` (in TransitionResult) | `PLACE_EXIT` → `CANCEL_ENTRY` → `PLACE_ENTRY` (`CANCEL_EXIT` before `CANCEL_ENTRY` where applicable) | external intents only; internal state mutations not represented |

### 21.4 Idempotency contract

| Event | Duplicate condition | Behavior |
|-------|-------------------|----------|
| `EntryFilled(order_id=X)` | lot with `source_entry_order_id=X` already exists (open or closed) | rejected, snapshot unchanged |
| `ExitFilled(exit_order_id=X)` | exit order with id=X already has status `FILLED` | rejected, snapshot unchanged |
| `ExitFilled` after `OperatorCleanup` | lot already `CLOSED` | rejected, snapshot unchanged |
| `RecenterRequested` with same reference | mode=FLAT, no lots | accepted — **snapshot-idempotent** (same window produced), but **action stream is NOT a no-op** (emits CANCEL/PLACE) |
| `EmergencyStopTriggered` repeated | already `emergency_stopped=True` | accepted, no-op (snapshot unchanged, no actions) |
| `OperatorCleanup` repeated | FLAT + no open lots + no open exits + empty window + not emergency_stopped | accepted, no-op (snapshot unchanged, no actions) |

### 21.5 ExitFilled pair-integrity contract

`ExitFilled(exit_order_id=X, lot_id=Y)` must satisfy ALL three conditions:

1. Lot with `lot_id=Y` exists in `open_lots`
2. Exit order with `exit_order_id=X` exists in `exit_orders`
3. The exit order's `lot_id` field == `Y` AND the lot's `exit_order_id` field == `X`

If any condition fails, the event is **rejected, snapshot unchanged**.

| Failure | Reason |
|---------|--------|
| lot_id not found | `UNKNOWN_LOT_ID` |
| exit_order_id not found | `UNKNOWN_EXIT_ORDER_ID` |
| exit_order.lot_id != event.lot_id | `EXIT_LOT_MISMATCH` |
| lot.exit_order_id != event.exit_order_id | `LOT_EXIT_MISMATCH` |
| lot.status == CLOSED | `LOT_ALREADY_CLOSED` (idempotent, per 21.4) |
| exit_order.status == FILLED | `EXIT_ALREADY_FILLED` (idempotent, per 21.4) |

### 21.6 EntryFilled active-entry validation

`EntryFilled(side=S, price=P)` is accepted only if `P` exists in the
**currently active entry window** for side `S`:

* `BUY` fill: `P` must be in `entry_window.buy_entry_prices`
* `SELL` fill: `P` must be in `entry_window.sell_entry_prices`

If `P` is not in the active window → **reject, snapshot unchanged**,
reason = `PRICE_NOT_IN_ACTIVE_WINDOW`.

No "synthetic fill acceptance" in PR2. Only fills matching active entries
are valid. Stale/reconciled fills are deferred to PR3.

### 21.7 OperatorCleanup lot closure ordering

Lots closed by `OperatorCleanup` are appended to `closed_lots` in their
existing `open_lots` chronological order (oldest first). This preserves
the audit trail invariant from 21.3.

**Snapshot-only limitation:** `GridV2Snapshot` alone does NOT distinguish
cleanup-closed lots from exit-closed lots. Both have `status=CLOSED`.
Distinguishing requires caller/event history. No new enum value in PR2.

### 21.8 RecenterRequested full window replacement

`RecenterRequested(new_reference_price=P)` performs a **full replacement**
of the entry window:

1. If any entries remain in the old window: emit `CANCEL_ENTRY(side, price)` for each old entry first.
2. `buy_entry_prices` and `sell_entry_prices` are **completely rebuilt**
   from `P` using the standard symmetric placement (section 9).
3. No incremental merge or reuse of old entries.
4. Emit one `PLACE_ENTRY` per new level in the rebuilt window.

**Action sequence:** `[CANCEL_ENTRY for old entries, ..., PLACE_ENTRY for new entries, ...]`
If old window is empty: only `PLACE_ENTRY` emitted.

The entry window after recenter is identical to `create_initial()` output
for the same reference price.

### 21.9 Action identity contract (PR2)

**CANCEL_ENTRY identity = `(side, price)`.** No exchange CID or order ID.
CID mapping deferred to PR3.

**CANCEL_EXIT identity = `order_id` field = exit_order_id from state machine's ExitOrder.**
CID mapping to exchange deferred to PR3.

**PLACE_EXIT identity = `lot_id` + `side` + `price` + `qty`.**

**PLACE_ENTRY identity = `side` + `price` + `qty`.**

### 21.10 Projected notional formula (risk guard)

`projected_inventory_notional_usd = sum(lot.qty * lot.entry_price for lot in open_lots) + event.qty * event.price`

If `projected > config.max_inventory_notional_usd` → reject EntryFilled.
All arithmetic uses Decimal. No rounding. Comparison is strict `>`.

### 21.11 ActionIntent reason contract

Closed set of `reason` strings for `ActionIntent`:

| Kind | reason | When |
|------|--------|------|
| `PLACE_EXIT` | `"PAIRED_EXIT_FOR_LOT"` | exit order for new lot (11.1-11.4) |
| `PLACE_ENTRY` | `"FILL_REPLACEMENT"` | new farthest entry replacing filled level |
| `PLACE_ENTRY` | `"EXIT_RESTORE"` | restore trimmed far-edge entry after exit fill |
| `PLACE_ENTRY` | `"RECENTER"` | entry from recenter rebuild |
| `CANCEL_ENTRY` | `"ROLLING_TRIM"` | opposite-side far-edge trim on accepted entry fill |
| `CANCEL_ENTRY` | `"EMERGENCY_STOP"` | entry canceled by emergency stop |
| `CANCEL_ENTRY` | `"OPERATOR_CLEANUP"` | entry canceled by cleanup |
| `CANCEL_ENTRY` | `"RECENTER_REPLACE"` | old entry removed before recenter rebuild |
| `CANCEL_EXIT` | `"OPERATOR_CLEANUP"` | exit canceled by cleanup |

`TransitionResult.reject_reason` closed set:

| reject_reason | When |
|---------------|------|
| `"PRICE_NOT_IN_ACTIVE_WINDOW"` | EntryFilled at unknown/inactive price (21.6) |
| `"MAX_INVENTORY_LEVELS"` | at lot count cap |
| `"MAX_INVENTORY_NOTIONAL_USD"` | at notional cap (21.10) |
| `"DUPLICATE_ENTRY_FILL"` | same order_id already sourced a lot (I8) |
| `"BRANCH_INCOMPATIBLE"` | BUY in SHORT or SELL in LONG (I4) |
| `"UNKNOWN_LOT_ID"` | ExitFilled for nonexistent lot (21.5) |
| `"UNKNOWN_EXIT_ORDER_ID"` | ExitFilled for nonexistent exit (21.5) |
| `"EXIT_LOT_MISMATCH"` | exit.lot_id != event.lot_id (21.5) |
| `"LOT_EXIT_MISMATCH"` | lot.exit_order_id != event.exit_order_id (21.5) |
| `"LOT_ALREADY_CLOSED"` | lot already CLOSED (21.4) |
| `"EXIT_ALREADY_FILLED"` | exit already FILLED (21.4) |
| `"RECENTER_NOT_FLAT"` | recenter when mode != FLAT |
| `"RECENTER_LOTS_OPEN"` | recenter with open lots |
| `"RECENTER_EXITS_OPEN"` | recenter with open exits |
| `"EMERGENCY_STOPPED"` | event blocked by emergency stop |
| `"INVALID_QUANTITY"` | EntryFilled or ExitFilled with qty <= 0 |

### 21.12 Config validation contract

`GridV2Config.__post_init__` must raise `ValueError` if:

| Condition | Error |
|-----------|-------|
| `grid_step_pct <= 0` | "grid_step_pct must be positive" |
| `entry_levels_per_side <= 0` | "entry_levels_per_side must be positive" |
| `order_size <= 0` | "order_size must be positive" |
| `max_inventory_levels <= 0` | "max_inventory_levels must be positive" |
| `max_inventory_notional_usd <= 0` | "max_inventory_notional_usd must be positive" |

Fail-closed: invalid config = `ValueError` at construction time. Single SSOT: `__post_init__`.

### 21.13 ExitFilled.qty contract (PR2)

`ExitFilled.qty` must be `> 0`. If `<= 0` → reject with `INVALID_QUANTITY`.

In PR2, all closes are full closes. **`event.qty` is ignored for closure semantics
and is NOT stored anywhere in snapshot state.** The state machine closes the lot
using `lot.qty` as the authoritative quantity. The closed lot retains its original
`lot.qty` unchanged.

`event.qty` validation against `lot.qty` is deferred to PR3 (reconciliation scope).
The lot is always fully closed regardless of `event.qty` value (as long as > 0).

### 21.14 OperatorCleanup economic semantics

`OperatorCleanup` is a **state reset**, not an economic event.

* Lots closed by cleanup have `status=CLOSED` but **no corresponding `ExitFilled` event**.
* Cleanup-closed lots do NOT represent realized PnL.
* The `closed_lots` tuple is an **operational audit trail**, not a PnL source.
* Realized PnL tracking (if needed) must use only lots closed by `ExitFilled` events.
* In PR2, there is no PnL tracking. This contract exists to prevent future misuse.

### 21.15 Constructor snapshot validation contract

`GridV2StateMachine.__init__(config, snapshot)` validates snapshot invariants
**immediately at construction time**. If the snapshot violates any invariant,
the constructor raises `GridV2InvariantError`.

Validated invariants:

| Check | Invariant |
|-------|-----------|
| `len(entry_window.buy_entry_prices) <= config.entry_levels_per_side` | I1 (bounded window) |
| `len(entry_window.sell_entry_prices) <= config.entry_levels_per_side` | I1 |
| every open lot has a paired OPEN exit order in `exit_orders` | I3 |
| mode == FLAT implies no open lots | I4 (branch-mode consistency) |
| mode == LONG_BRANCH implies all open lots are LONG side | I4 |
| mode == SHORT_BRANCH implies all open lots are SHORT side | I4 |

This is the same `_check_invariants()` used after every non-rejected transition.
Fail-closed: invalid snapshot = `GridV2InvariantError` at construction time.

**Single validation path:** `create_initial()` constructs its snapshot and then
passes it through `__init__()` (which calls `_check_invariants()`). There is
exactly one code path for snapshot validation — `__init__` — and all construction
routes go through it. No bypass.

### 21.16 Emergency stop event gate contract

When `emergency_stopped == True`, the state machine applies the following event gate:

| Event | Behavior | Rationale |
|-------|----------|-----------|
| `EntryFilled` | **rejected** (`EMERGENCY_STOPPED`) | no new exposure |
| `RecenterRequested` | **rejected** (`EMERGENCY_STOPPED`) | no window rebuild while frozen |
| `ExitFilled` | **accepted and applied** | exits must close lots even under emergency; I2 |
| `OperatorCleanup` | **accepted and applied** | full reset is always allowed |
| `EmergencyStopTriggered` | **no-op** (already stopped) | idempotent per 21.4 |

This gate is checked **before** event-specific dispatch. The key design
constraint: emergency stop freezes new exposure but never blocks position
reduction. Open lots with paired exits must remain closeable.

---

## 22. PR3 contract clarifications (reconciliation adapter)

### 22.1 CID scheme contract

Single strategy_id `g`.

**Strategy reservation**: `g` is reserved exclusively for grid_v2 in this
repository. No other module or strategy may use `g` as a strategy_id.
This reservation MUST be recorded in `reconcile/identity.py` module docstring.

Entry/exit type encoded in `level_id` prefix:

| Order type | level_id | Example CID | Length |
|-----------|----------|-------------|--------|
| Entry | `e{seq}` | `grinder_g_BTCUSDT_e999_1710000000_0` | 35 |
| Exit | `x{seq}` | `grinder_g_SHIBUSDT_x999_1710000000_0` | 36 |

Reuses `generate_client_order_id()` and `parse_client_order_id()` from
`reconcile/identity.py` unchanged. `is_ours()` works with
`OrderIdentityConfig(strategy_id="g")`.

**Symbol-scoped ownership**: The adapter is instantiated per symbol. CID
ownership requires BOTH strategy_id=`g` AND symbol matching the adapter's
configured symbol. A `BTCUSDT` adapter treats `grinder_g_ETHUSDT_e0_...`
as foreign (not ours), even though the strategy_id matches. This prevents
cross-symbol confusion in multi-symbol runtime.

**Length budget**:

```
Fixed overhead = 24 chars  (grinder_ + g + 4 separators + 10-digit ts + 1-digit seq)
Variable budget = symbol + level_id <= 12 chars

Symbol len | Max level_id | Max numeric seq | Entries per kind
7 (BTCUSDT)  | 5 chars (e9999) | 9999           | 10,000
8 (SHIBUSDT) | 4 chars (e999)  | 999            | 1,000
```

CID seq field always `0` in `generate_client_order_id(... seq=0)`.
Uniqueness comes from monotonic counter in `level_id`, not from seq field.

**Seq overflow**: adapter raises `ValueError` if counter exceeds max for
the configured symbol length. Fail-closed.

### 22.2 Order registry contract

Bidirectional mapping between exchange CIDs and state machine entities:

| Direction | Key | Value |
|-----------|-----|-------|
| CID -> entry | `cid` | `(side, price)` |
| CID -> exit | `cid` | `(exit_order_id, lot_id)` |
| entry -> CID | `(side, price)` | `cid` |
| exit -> CID | `exit_order_id` | `cid` |

**Invariants**:
- No duplicate CIDs (register raises `ValueError`)
- No duplicate reverse keys: `(side, price)` for entries, `exit_order_id`
  for exits (register raises `ValueError`). Prevents silent overwrite of
  the reverse map that would orphan older registrations.
- Remove cleans both directions
- Registry is the adapter's only mutable state

### 22.3 Registry mutation policy

**Core principle: translation is pure. Translation never mutates registry.**

`translate_fill()` and `resolve_actions(CANCEL_*)` are read-only operations
on the registry. They return CIDs / translated events but never register or
deregister anything.

**Exhaustive list of registry mutation operations:**

| Operation | Method | When called |
|-----------|--------|-------------|
| Register entry | `resolve_actions(PLACE_ENTRY)` | action resolution creates new entry CID |
| Register exit | `resolve_actions(PLACE_EXIT)` | action resolution creates new exit CID |
| Register entries (batch) | `seed_entry_window()` | initial window setup |
| Remove entry (fill confirmed) | `confirm_entry_fill(cid)` | caller, after SM.apply(EntryFilled) succeeds |
| Remove exit (fill confirmed) | `confirm_exit_fill(cid)` | caller, after SM.apply(ExitFilled) succeeds |
| Remove entry (cancel confirmed) | `confirm_cancel_entry(cid)` | caller, after exchange ack |
| Remove exit (cancel confirmed) | `confirm_cancel_exit(cid)` | caller, after exchange ack |
| Full reset + rebuild | `reconstruct_snapshot()` | restart path |

No other code path may mutate the registry.

**Rationale**: Without caller confirmation, early removal causes:
- Late fills at "removed" CIDs -> false `ValueError` on `translate_fill`
- False `ENTRY/EXIT_MISSING_ON_EXCHANGE` noise in reconciliation
- Reverse map `(side, price) -> cid` stale or blocked
- Registry stops being truth layer between state machine and exchange

### 22.4 Fill translation contract

| Input CID | Registry lookup | Result |
|-----------|----------------|--------|
| Not ours (foreign) | -- | `None` (ignored) |
| Entry CID, in registry | found | `TranslatedFill(EntryFilled(...))` |
| Exit CID, in registry | found | `TranslatedFill(ExitFilled(...))` |
| Our CID, NOT in registry | not found | `ValueError` (stale fill) |

**No registry mutation.** Caller must call `confirm_entry_fill(cid)` or
`confirm_exit_fill(cid)` after successful SM application.

`EntryFilled.order_id` = the CID itself. This flows through to state machine:
`lot_id = f"lot-{cid}"`, `exit_order_id = f"exit-{cid}"`.

**Duplicate fill semantics**: The adapter is stateless with respect to fills.
Calling `translate_fill` with the same CID twice produces the same event
both times (as long as the registration exists). Deduplication is the state
machine's responsibility (I8 / `DUPLICATE_ENTRY_FILL`, `EXIT_ALREADY_FILLED`).
The adapter MUST NOT silently suppress duplicates.

### 22.5 Action resolution contract

| ActionIntentKind | Adapter action | Registry side effect |
|-----------------|----------------|---------------------|
| `PLACE_ENTRY` | generate entry CID | **register** `cid -> (side, price)` |
| `PLACE_EXIT` | generate exit CID, derive IDs | **register** `cid -> (exit_order_id, lot_id)` |
| `CANCEL_ENTRY` | look up CID by `(side, price)` | **no mutation** (returns CID only) |
| `CANCEL_EXIT` | look up CID by `exit_order_id` | **no mutation** (returns CID only) |

`CANCEL_*` raises `ValueError` if CID not found in registry.

**Exit ID derivation**: `action.lot_id = "lot-{X}"` ->
`exit_order_id = "exit-{X}"` where `X = lot_id.removeprefix("lot-")`.
Matches state machine convention in `state.py`.

### 22.6 Reconciliation detection contract

| Mismatch kind | Severity | Condition |
|--------------|----------|-----------|
| `ENTRY_MISSING_ON_EXCHANGE` | WARNING | entry CID in registry but not on exchange |
| `EXIT_MISSING_ON_EXCHANGE` | CRITICAL | exit CID in registry but not on exchange (17.5) |
| `UNEXPECTED_ORDER` | WARNING | our CID on exchange but not in registry |

Non-g CIDs on exchange are ignored (not our orders).

### 22.7 Snapshot reconstruction contract (restart path)

**Inputs**: exchange open orders + position quantity + reference price + timestamp.

**Algorithm**:

1. Parse each order CID -> classify as entry (e-prefix) or exit (x-prefix)
2. Skip non-g CIDs (not ours)
3. Entry orders -> rebuild entry window (buy descending, sell ascending)
4. Each exit order -> infer one open lot:
   - Exit SELL -> lot is LONG, `entry_price = exit_price / (1 + grid_step_pct)`
   - Exit BUY -> lot is SHORT, `entry_price = exit_price / (1 - grid_step_pct)`
5. Mode: FLAT (no lots), LONG_BRANCH (all long), SHORT_BRANCH (all short)
6. Cross-validate against position (fail-closed, see 22.8)
7. Validate through `GridV2StateMachine.__init__()` (21.15 single validation path)
8. Reset registry (clear) and register all discovered CIDs
9. Recover seq counters (see 22.10)

### 22.8 Reconstruction fail-closed rules

Every rule is `ValueError`. No silent degradation, no truncation.

| # | Condition | Error |
|---|-----------|-------|
| F1 | Mixed lot sides (some LONG, some SHORT) | I4 violation |
| F2 | Position non-flat (!= 0) but no exit orders found | unprotected position |
| F3 | Position flat (== 0) but exit orders exist | inconsistent state |
| F4 | Position sign mismatches lot direction | direction mismatch |
| F5 | Duplicate exit orders for same inferred lot | ambiguous lot mapping |
| F6 | Duplicate entry orders for same (side, price) | registry one-to-one violation |
| F7 | Entry count for either side exceeds config.entry_levels_per_side | window overflow |
| F8 | All order CIDs truly unparseable (non-empty input, zero classified, zero parseable by base identity parser). Cross-symbol g-CIDs that parse but are filtered by symbol scope do NOT trigger F8. | unclassifiable orders |

**Position quantity divergence**: `abs(position_qty)` vs
`sum(lot.qty for lot in inferred_lots)` is **explicitly ignored in PR3**.
The adapter validates direction only, not magnitude. Quantity reconciliation
is deferred to PR4+. No warning emitted (no warning channel in return type).

**Return type**: `GridV2Snapshot` only. No warnings, no degraded mode.
Reconstruction either succeeds (valid snapshot) or raises `ValueError`.

### 22.9 Adapter lifecycle

```
create_initial(config, ref, ts) -> sm
adapter.seed_entry_window(sm.snapshot.entry_window, ts) -> initial CIDs
    |
[exchange fill] -> adapter.translate_fill(...) -> EntryFilled/ExitFilled
    |
sm.apply(event) -> TransitionResult (if not rejected)
    |
adapter.confirm_entry_fill(cid) / confirm_exit_fill(cid)
    |
adapter.resolve_actions(result.actions, ts) -> CIDs for execution (PR4)
    |
[exchange confirms cancel] -> adapter.confirm_cancel_entry/exit(cid)
    |
[periodically] adapter.reconcile(sm.snapshot, exchange_open_cids, ts)
    |
[restart] adapter.reconstruct_snapshot(...) -> new snapshot + registry rebuilt
```

### 22.10 Restart seq recovery contract

On `reconstruct_snapshot()`, the adapter:

1. Clears registry (full reset)
2. Registers all discovered entry and exit CIDs
3. Scans all discovered CIDs, parses the numeric suffix from level_id
4. Sets `_entry_seq = max(parsed_entry_indices) + 1` (or 0 if none)
5. Sets `_exit_seq = max(parsed_exit_indices) + 1` (or 0 if none)

This prevents CID collision with still-open orders on the exchange.

**Overflow policy**: If `max_seen + 1` exceeds the symbol-dependent limit,
`reconstruct_snapshot` raises `ValueError`. Fail-closed.

### 22.11 Stale fill handling (PR3 scope)

When a fill arrives for a CID in our registry, the adapter translates it.
The state machine may reject it (`PRICE_NOT_IN_ACTIVE_WINDOW`).
The adapter does **not** force acceptance. Detection only.

When a fill arrives for a CID that is ours but NOT in registry,
`translate_fill` raises `ValueError`. Remediation deferred to PR4+.

---

## 23. PR4: Runtime bridge contract

`GridV2Bridge` wires `GridV2Adapter` + `GridV2StateMachine` into the
live engine's action generation phase. No direct exchange I/O — produces
`ExecutionAction` instances for the existing dispatch pipeline.

Symbol-scoped: one bridge per trading symbol.

### 23.1 Binding runtime contract (strict call order)

Every exchange fill MUST be processed in this exact order:

1. Exchange fact received (fill or cancel ack)
2. `adapter.translate_fill(cid, side, price, qty, ts)` — read-only on registry
3. `sm.apply(translated.event)` → `TransitionResult`
4. **Only if not rejected**: `adapter.confirm_entry_fill(cid)` or `confirm_exit_fill(cid)`
5. `adapter.resolve_actions(result.actions, ts)` → `ResolvedAction` tuples
6. Bridge converts to `ExecutionAction` tuples for dispatch pipeline
7. **Only after exchange cancel ack**: `adapter.confirm_cancel_entry/exit(cid)`

No other code path may mutate the adapter registry.
No dispatch before successful startup reconstruction.

### 23.2 Startup fail-closed contract

The bridge blocks all operations (`on_fill`, `on_cancel_ack`, `reconcile`)
until startup reconstruction succeeds. Two startup paths:

| Path | Method | When |
|------|--------|------|
| Fresh start | `startup_fresh(ref_price, ts)` | No exchange orders, first run |
| Reconstruction | `startup(exchange_orders, pos_qty, ref_price, ts)` | Restart with existing orders |

`startup()` catches `ValueError` from adapter reconstruction and sets
`failed_reason`. All subsequent calls raise `RuntimeError` with
`GRID_V2_DISPATCH_BLOCKED`.

`startup_fresh()` creates initial state machine, seeds entry window via
adapter, and returns `ExecutionAction` tuples for initial PLACE_ENTRY orders.

### 23.3 Fill lifecycle

`on_fill(cid, side, price, qty, ts)` → `FillResult`:

| Input | Outcome |
|-------|---------|
| Foreign CID (not ours) | `FillResult(translated=None, rejected=False)` — ignored |
| Entry CID, SM accepts | Confirm fill, resolve actions (PLACE_EXIT + window shifts), return ExecutionActions |
| Exit CID, SM accepts | Confirm fill, resolve actions (window shifts), return ExecutionActions |
| Any CID, SM rejects | `FillResult(rejected=True, reject_reason=...)` — no confirm, no actions |

Bridge contract is intentionally local: on reject, bridge does not mutate registry.
Engine-level disappeared-fill handling may apply additional cleanup policy (23.9).

### 23.4 Cancel ack lifecycle

`on_cancel_ack(cid)` → `CancelAckResult`:

Called only after exchange confirms the cancel. Removes CID from registry.
Foreign/unknown CIDs return `removed=False`.

### 23.5 Reconciliation

`reconcile(exchange_open_cids, ts)` → detection only, no mutation.
Delegates to `adapter.reconcile()`. Logs mismatches at WARNING/CRITICAL.
Returns empty tuple (no remediation actions in PR4).

### 23.6 ExecutionAction conversion

| ResolvedAction kind | ExecutionAction |
|--------------------|-----------------|
| `PLACE_ENTRY` | `ActionType.PLACE`, `reduce_only=False`, reason=`grid_v2_PLACE_ENTRY` |
| `PLACE_EXIT` | `ActionType.PLACE`, `reduce_only=True`, reason=`grid_v2_PLACE_EXIT` |
| `CANCEL_ENTRY` | `ActionType.CANCEL`, `order_id=cid`, reason=`grid_v2_CANCEL_ENTRY` |
| `CANCEL_EXIT` | `ActionType.CANCEL`, `order_id=cid`, reason=`grid_v2_CANCEL_EXIT` |

### 23.7 Stable log reason codes

| Code | Meaning |
|------|---------|
| `GRID_V2_RECONSTRUCTION_OK` | Startup reconstruction succeeded |
| `GRID_V2_RECONSTRUCTION_FAILED` | Startup reconstruction failed (fail-closed) |
| `GRID_V2_BRIDGE_STARTUP_OK` | Fresh startup completed |
| `GRID_V2_FILL_PROCESSED` | Fill processed successfully |
| `GRID_V2_FILL_REJECTED` | Fill rejected by state machine |
| `GRID_V2_FILL_FOREIGN` | Fill CID not ours (ignored) |
| `GRID_V2_DISPATCH_BLOCKED` | Operation blocked (reconstruction not done/failed) |

### 23.8 Bridge data types

```python
@dataclass(frozen=True)
class FillResult:
    translated: TranslatedFill | None
    transition: TransitionResult | None
    resolved_actions: tuple[ResolvedAction, ...]
    execution_actions: tuple[ExecutionAction, ...]
    rejected: bool
    reject_reason: str | None

@dataclass(frozen=True)
class CancelAckResult:
    cid: str
    kind: GridV2OrderKind
    removed: bool
```

### 23.9 Runtime switch contract (engine integration)

`LiveEngineV0` reads two env vars at `__init__`:

- `GRINDER_GRID_V2_ENABLED` (bool, default `False`) — master switch
- `GRINDER_GRID_V2_SYMBOL` (string, required when enabled) — symbol scope

**Fail-closed**: `GRINDER_GRID_V2_ENABLED=True` + empty/missing symbol →
`ValueError` at engine init. No silent degradation.

**Config env vars** (read at init, used by `GridV2Config`):

| Env var | Default | Maps to |
|---------|---------|---------|
| `GRINDER_GRID_V2_STEP_PCT` | `0.005` | `grid_step_pct` |
| `GRINDER_GRID_V2_ENTRY_LEVELS` | `3` | `entry_levels_per_side` |
| `GRINDER_GRID_V2_ORDER_SIZE` | `0.001` | `order_size` |
| `GRINDER_GRID_V2_MAX_INV_LEVELS` | `3` | `max_inventory_levels` |
| `GRINDER_GRID_V2_MAX_INV_NOTIONAL` | `1000` | `max_inventory_notional_usd` |
| `GRINDER_GRID_V2_RESEED_ON_FLAT` | `false` | `reseed_on_flat` |
| `GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW` | `true` | `reseed_on_flat_only_on_skew` |

**When disabled** (`GRINDER_GRID_V2_ENABLED=False`, default):
- `_grid_v2_bridge` is `None`
- All legacy code paths (planner, paper engine, cycle layer) unchanged
- Zero behavioral impact

**When enabled** (`GRINDER_GRID_V2_ENABLED=True`):
1. Bridge constructed at engine init via `_create_grid_v2_bridge()`
2. On first tick with account sync data, `_grid_v2_try_startup()` runs:
   - Classifies exchange orders by strategy `g`
   - If grid_v2 orders exist → `bridge.startup()` (reconstruct)
   - If no grid_v2 orders + position non-flat → **fail-closed** (overexposure guard)
   - If no grid_v2 orders + position flat → `bridge.startup_fresh()` (new grid)
3. **Startup tick**: seed actions dispatched, fill/cancel-ack detection **skipped**
   (seeded CIDs not yet on exchange → registry-vs-exchange diff would be false positives)
4. On each subsequent tick where `_is_grid_v2_active(symbol)`:
   - `_grid_v2_process_cancel_acks()` detects confirmed cancels via pending-cancel set
   - `_grid_v2_process_fills()` detects fills via registry-vs-exchange diff
   - fill processing order is deterministic: EXIT CIDs first, then ENTRY CIDs
   - if a disappeared CID maps to a rejected fill, engine removes that CID from
     registry (`confirm_cancel_*`) and logs `GRID_V2_REJECTED_FILL_CLEANED`
   - Dispatched CANCEL actions tracked in `_grid_v2_pending_cancels`
   - Fill actions replace legacy planner output for that symbol
5. Legacy planner/paper engine skipped for the grid_v2 symbol (never falls through)
6. **Post-step generators bypassed**: cycle layer (TP detection) and replenish path
   are unconditionally skipped for the grid_v2 symbol — grid_v2 owns its own fill
   detection and exit placement. This applies in both active and blocked states,
   ensuring fail-closed cannot be circumvented by legacy action generators.

## 24. Shadow / Paper Verification (PR5)

Shadow mode runs grid_v2 computation alongside the legacy engine on the same
snapshots, without affecting live dispatch. Purpose: validate grid_v2 behavior
before PR6 live ceremony.

### 24.1 Shadow Runner Contract

- `GridV2ShadowRunner` owns an **isolated** `GridV2Bridge` instance (not shared
  with engine's primary bridge).
- Shadow **never** produces `ExecutionAction` objects that reach the dispatch
  pipeline. All output is `ShadowResult` (observability-only).
- Shadow errors are **fail-open**: caught and logged, never propagated to the
  live engine path.

### 24.2 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRINDER_GRID_V2_SHADOW` | `false` | Enable shadow runner |
| `GRINDER_GRID_V2_SYMBOL` | (required) | Symbol to shadow |
| `GRINDER_GRID_V2_STEP_PCT` | `0.005` | Grid step (shared with primary) |
| `GRINDER_GRID_V2_ENTRY_LEVELS` | `3` | Entry levels per side |
| `GRINDER_GRID_V2_ORDER_SIZE` | `0.001` | Order size |
| `GRINDER_GRID_V2_MAX_INV_LEVELS` | `3` | Max inventory levels |
| `GRINDER_GRID_V2_MAX_INV_NOTIONAL` | `1000` | Max inventory notional USD |

### 24.3 Mutual Exclusion

Shadow and primary grid_v2 are **mutually exclusive**:
- If `GRINDER_GRID_V2_ENABLED=true`, shadow is **not** created (even if
  `GRINDER_GRID_V2_SHADOW=true`). Primary owns the symbol.
- Shadow only runs when primary is OFF.

### 24.4 Shadow Tick Lifecycle

1. Shadow startup mirrors primary logic: reconstruct / fresh / non-flat blocked.
2. On each tick, `_grid_v2_shadow_tick()` runs **after** legacy action generation
   is complete (including cycle layer, replenish) and **before** dispatch.
3. Shadow computes what grid_v2 WOULD do (fill detection via registry-vs-exchange
   diff on shadow's isolated bridge).
4. `ShadowResult` logged with divergence classification:
   - `none`: action counts and types match.
   - `count_mismatch`: different number of actions.
   - `type_mismatch`: same count but different action types.
   - `shadow_blocked`: shadow startup failed or not complete.

### 24.5 Invariants

- **I-S1**: Shadow bridge is a separate instance. Shadow state mutations do not
  affect engine's primary path.
- **I-S2**: Shadow never injects actions into `raw_actions` or `live_actions`.
- **I-S3**: Shadow fail-closed (non-flat/no-orders) does not block legacy dispatch.
- **I-S4**: Shadow errors are caught (try/except), logged, and do not propagate.

## 25. Live Ceremony (PR6)

### 25.1 Price Quantization

Prices generated by the state machine (entry window, exit placement) are
floating-point products of `reference_price * (1 ± step_pct * i)`. These must
be quantized to exchange tick size before dispatch.

- `GridV2Config.price_tick_size` (default `0.01`, BTCUSDT = `0.10`).
- Side-aware quantization in `bridge._quantize_price(price, side)`:
  - BUY: `ROUND_DOWN` (less aggressive entry).
  - SELL: `ROUND_UP` (less aggressive exit).
  - None/unknown: `ROUND_HALF_UP` (fallback).
- Applied in `_to_execution_actions()` for all PLACE actions.
- Environment variable: `GRINDER_GRID_V2_TICK_SIZE` (**required** when
  `GRINDER_GRID_V2_ENABLED=true`; engine fails closed if missing).

### 25.2 Awaiting-Sync Gate

After `startup_fresh()`, seed CIDs are in the registry but not yet visible on
the exchange (REST snapshot hasn't refreshed). Fill detection must be skipped
until the first account sync confirms orders are visible.

- `_grid_v2_awaiting_sync` flag: set `True` on fresh start.
- `_grid_v2_pending_seed_cids`: stores CIDs from seed actions.
- Cleared by `_tick_account_sync()` only when **all** seed CIDs are present
  in the account snapshot `open_orders`. If any seed CID is missing, the flag
  stays `True` and fill detection remains skipped.
- While set: fill and cancel-ack detection are skipped (same as startup-tick
  skip, but extended until seed visibility confirmed).
- Log markers: `GRID_V2_AWAITING_SYNC_CLEARED gen=N seeds_confirmed=K`,
  `GRID_V2_AWAITING_SYNC_PENDING missing=M/K`.

### 25.3 Ceremony Results (2026-03-18)

| Parameter | Value |
|-----------|-------|
| Symbol | BTCUSDT |
| Levels | 1 per side |
| Order size | 0.002 BTC |
| Step | 0.5% |
| Tick size | 0.10 |
| Duration | 180s |
| Ticks | 16167 |
| Startup | `GRID_V2_BRIDGE_STARTUP_OK mode=FLAT seed_count=2` |
| Orders placed | 2 (BUY @ 71435.00, SELL @ 72153.00) |
| Errors | 0 |
| False fills | 0 |
| Position | FLAT throughout |
| Post-cleanup | `EXCHANGE_STATE_VERIFY status=CLEAN` |

### 25.4 Flat Reseed Modes

Flat normalization supports three runtime modes:

- `reseed_on_flat=true`: always recenter/reseed when full unwind reaches `FLAT`.
- `reseed_on_flat=false` and `reseed_on_flat_only_on_skew=false`: preserve rolled window on
  unwind; integrity repair cleans extras but does not force reseed.
- `reseed_on_flat=false` and `reseed_on_flat_only_on_skew=true` (default): reseed only when flat ladder is
  skewed (BUY/SELL imbalance or side-vs-expected mismatch), otherwise preserve.

Startup reconstruction follows the same rule: after successful reconstruction in `FLAT` with
`pos_qty == 0`, optional recenter executes only if configured mode requires it.
