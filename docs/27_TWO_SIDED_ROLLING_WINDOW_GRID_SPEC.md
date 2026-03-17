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
* **opposite-side sell-entry orders suppressed** (strict one-sided branch)
* only buy-entries below + sell-exits above

### 8.3 SHORT_BRANCH

* at least one open short lot
* no long lots allowed
* each short lot has paired buy exit
* **opposite-side buy-entry orders suppressed** (strict one-sided branch)
* only sell-entries above + buy-exits below

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
5. Remove farthest sell-entry from opposite side
6. mode -> `LONG_BRANCH`

### 11.2 FLAT + EntryFilled(SELL)

Preconditions: mode == FLAT, filled order is valid sell-entry.

Actions:
1. Create new `SHORT lot`
2. Create paired `BUY exit` at `entry_price - 1 step`
3. Remove filled sell-entry from window
4. Add new farthest sell-entry one step higher than current highest
5. Remove farthest buy-entry from opposite side
6. mode -> `SHORT_BRANCH`

### 11.3 LONG_BRANCH + EntryFilled(BUY)

Preconditions: no short lots exist, filled order is valid buy-entry.

Actions:
1. Create another `LONG lot`
2. Create paired `SELL exit` one step above its entry
3. Add new farthest buy-entry lower
4. Remove farthest sell-entry upper (if any remain from window)
5. mode remains `LONG_BRANCH`

### 11.4 SHORT_BRANCH + EntryFilled(SELL)

Symmetric to 11.3.

### 11.5 LONG_BRANCH + ExitFilled(SELL)

Preconditions: exit belongs to an open long lot.

Actions:
1. Mark corresponding long lot `CLOSED`
2. Remove/close corresponding exit order
3. If open long lots remain: mode remains `LONG_BRANCH`
4. If no open lots remain: mode -> `FLAT`, trigger flat normalization

### 11.6 SHORT_BRANCH + ExitFilled(BUY)

Symmetric to 11.5.

---

## 12. Architectural decisions (v1 choices)

These are binding for v1 implementation.

### D1. Active branch mode: strict one-sided branch

**Choice: YES.**
When branch is active, only branch-compatible entries remain.
Opposite-side entry orders that would open forbidden inventory are **suppressed**.

In `LONG_BRANCH`: only buy-entries below + sell-exits above.
In `SHORT_BRANCH`: only sell-entries above + buy-exits below.

**Rationale:** simplest to reason about, safest, eliminates mixed-inventory edge cases entirely.

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

### D5. Flat normalization: soft recenter in flat (Option B)

**Choice: Option B -- soft recenter.**
When branch fully unwinds to FLAT, rebuild entry window symmetrically around current mid/reference.

**Rationale:** avoids drift accumulation from asymmetric unwind sequences.
Cleaner operationally than keep-as-is (Option A).

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
* sell-exit orders above for open long lots
* **no sell-entry orders** (would open short inventory)

### During SHORT_BRANCH

* maintain sell-entry chain above (up to `N_sell` levels)
* buy-exit orders below for open short lots
* **no buy-entry orders** (would open long inventory)

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

### PR5 -- Paper/live shadow verification

* run alongside existing engine
* no real-risk ceremony yet

### PR6 -- Small live ceremony

* minimal size (1-2 levels, small qty)
* strict proof bundle (same format as ADR-090 ceremonies)
