# 26 - Rolling Infinite Grid Specification

**Status:** draft
**Date:** 2026-03-09
**ADR:** ADR-085
**Supersedes:** portions of doc-25 SS 25.5 Step 2 (symmetric bilateral grid centered on mid_price)

---

## 1. Problem Statement

LiveGridPlannerV1 (doc-25) builds a **symmetric bilateral grid centered on `mid_price`**.
Each tick, the planner recomputes all desired levels from the current mid. When price moves:

1. All levels shift: full `GRID_SHIFT` (10 CANCEL + 10 PLACE = 20 actions for N=5).
2. Budget burns fast: Run #18 exhausted 100-order budget in ~2 minutes (5 shifts x 20 actions).
3. Fills are lost: price moves, grid recenters, filled level's neighbors vanish before exploitation.
4. Anti-churn and converge-first (PR-P0-RACE-1) mitigate but don't solve the root cause.

**Root cause:** the grid is anchored to a moving target (`mid_price`), so every price
movement triggers a full rebuild. Grid fills don't advance the grid -- they get swallowed
by the next recenter.

### Desired behavior

A **rolling infinite ladder** where:

- Grid fills advance the ladder in the fill direction.
- The sell-side frontier follows the buy-side down (and vice versa).
- Level count and spacing are preserved.
- Each fill triggers minimal grid actions (1 CANCEL + 1 PLACE = 2, inner level reserved for TP per INV-9), not a full rebuild.

---

## 2. Design Goals

| ID | Goal |
|----|------|
| G1 | Grid fills shift the ladder discretely, not recenter on mid |
| G2 | Uniform spacing maintained at all times |
| G3 | Constant level count (`N_buy + N_sell`) absent risk/budget/exchange blocks |
| G4 | Minimal actions per fill: O(1), not O(N) |
| G5 | TP fills restore grid depth without shifting ladder |
| G6 | Deterministic restart recovery |
| G7 | Backward compatible (existing env vars, safe-by-default via feature flag) |

---

## 3. Formal Invariants

### INV-1: Cardinality

At any point, the desired ladder has exactly `N_buy` BUY levels and `N_sell` SELL levels,
unless blocked by risk gate, budget exhaustion, or exchange rejection.

### INV-2: Spacing

For any side, adjacent desired levels differ by exactly `step_price` (computed once per
session from the anchor). Formally:

```
|price[i+1] - price[i]| == step_price    for all adjacent i on same side
```

### INV-3: Rolling Shift

After `k` net grid BUY fills (without intervening grid SELL fills):

- `net_offset = -k`
- Effective center shifted down by `k * step_price`
- Entire desired ladder shifted down by `k * step_price`

Symmetrically, `k` net grid SELL fills produce `net_offset = +k`, ladder shifted up.

### INV-4: Frontier Replenish

New orders are placed at the **frontier** of the appropriate side (farthest from center),
not via mid-price recenter. The filled level's slot is consumed; a new slot opens at the
opposite frontier.

### INV-5: No Global Rebuild on Fill

An ordinary grid fill generates at most `1 CANCEL + 1 grid PLACE = 2 grid actions`
(inner level reserved for TP per INV-9; cycle layer places TP separately).
Full `GRID_SHIFT` (cancel-all + place-all) is NEVER triggered by a fill event alone.

### INV-6: TP Isolation

TP fills (`strategy_id="tp"`) do NOT shift the ladder (`net_offset` unchanged).
After TP fill, the planner restores missing grid levels via normal `GRID_FILL`
(planner diff). No separate replenish mechanism needed.

### INV-7: Bidirectional Symmetry

BUY fill path and SELL fill path are exact mirrors. Every rule that applies to
"BUY fill shifts down" has an equivalent "SELL fill shifts up".

### INV-8: Restart Recovery

After restart or AccountSync reconnect:

1. `ladder_anchor = current mid_price`
2. `net_offset = 0`
3. `step_price = round_to_tick(ladder_anchor * spacing_bps / 10000)`
4. Planner diffs desired grid against exchange orders.
5. Orphaned orders cancelled via `GRID_TRIM`; missing levels placed via `GRID_FILL`.
6. Rolling state begins fresh from new anchor.

### INV-9: TP Slot Exclusion (ADR-086)

Two-layer protection prevents TP/grid overlap at the same price:

1. **Primary (INV-9b): TP inclusion in planner's open_orders.** Engine passes all
   grinder orders (including TP) to planner. Planner's `_match_orders_by_price()`
   matches TP to desired level → no grid PLACE at TP price. Planner skips
   CANCEL/REPLACE for TP orders (managed by cycle layer). This is the cross-tick
   protection — works regardless of when TP appeared relative to fill.

2. **Secondary: Persistent reservation.** On fill ticks, planner reserves innermost
   opposite-side slots for TP orders. Reservation persists across ticks until
   age-out (`max_reservation_age`, default 50). This is defense-in-depth for the
   REST-lag gap between fill detection and TP visibility in exchange snapshot.

**Formula:**

```
desired_grid_count = (N - sell_reservation) + (N - buy_reservation)
```

Where:
- `sell_reservation = count of BUY fills this tick` (clamped to `[0, N]`)
- `buy_reservation = count of SELL fills this tick` (clamped to `[0, N]`)
- Steady tick (no fills): `sell_reservation = buy_reservation = 0`, `desired_grid_count = 2*N`

**Slot state model:**

| State | Definition |
|-------|-----------|
| `grid` | Occupied by `strategy_id="d"` |
| `tp` | Occupied by `strategy_id="tp"` |
| `reserved` | Planner withholds for TP, persists until age-out |
| `vacant` | No order at this level |

Ownership applies only to occupied slots (`grid` or `tp`). `reserved` and `vacant` are
transient non-ownership states.

**Reservation lifecycle (MUST rules):**

1. Reservation is planner-local. Stored in `LiveGridPlannerV1._tp_slot_reservations`.
   Not shared with cycle layer or engine.
2. Reservation persists across ticks until `max_reservation_age` (default 50 plan()
   calls). Age-only clearance — TP visibility does NOT clear reservation (avoids
   multi-fill false-clear, Contract 2a: existing TP from prior fill would trigger
   premature clearing of new reservation for different slot).
3. Primary ownership SSOT = TP inclusion in open_orders + price matching. Planner sees
   TP in exchange snapshot → matches to desired level → no grid PLACE at that price.
4. Reservation does not create TP. It only withholds grid placement. TP generation
   is cycle layer's responsibility.

**Convergence target:**

```
steady_state_open_count_target = 2 * N    (grid + tp)
```

This is a convergence target, NOT an absolute runtime invariant. Transient deviation
within one reconciliation cycle is expected (async propagation, rejected PLACEs,
delayed CANCELs).

**Saturation policy (fill_count > N same side):**

Reservation clamps at `min(fill_count, N)` per side. Planner never produces negative
level count. Cycle layer generates one TP per detected fill regardless. If fills > N,
cycle layer may generate more TPs than grid capacity on that side. Excess TPs
self-heal via TP TTL. Known limitation, same category as ADR-085 exchange-side
non-trade cancels.

**Defense-in-depth (`_filter_tp_grid_overlap`):**

Same-tick anomaly guard in engine.py. Expected fire count: ZERO. Firing in production
is bug evidence requiring investigation. Uses `reduce_only` as structural discriminator
(TP=True, grid=False). Fail-open when planner config unavailable. Does NOT protect
cross-tick overlap (handled by TP inclusion + persistent reservation above).

**Tie-break:** Within epsilon, closest `price_diff_bps` to desired level wins. On
exact tie, first in exchange snapshot iteration order wins.

**`diff_extra_tp` (INV-9b follow-up, ADR-086):**

`GridPlanResult.diff_extra_tp` counts TP orders in `diff.extra_orders`. Convergence
guard (ADR-084) uses `non_tp_extras = diff_extra - diff_extra_tp`. TP extras are
intentional (planner does not cancel them — cycle layer owns TP lifecycle) and must
not block grid PLACEs.

**Convergence guard operational fixes (ADR-087, BUG-3/BUG-4):**

1. **BUG-3 (GRID_SHIFT_DEFERRED churn):** Inflight latch log throttled to once per
   latch cycle. Pure shifts (`cancel_count >= place_count`) skip re-latch after
   convergence clear. Net-new PLACEs still latch.
2. **BUG-4 (CANCEL_2011 spin):** `_cancel_failed_ids` blacklist prevents re-cancelling
   orders that already returned failure. Cleared on AccountSync refresh.

---

## 4. State Model

### 4.1 Rolling Ladder State (per symbol, in-memory, volatile)

```python
@dataclass
class _RollingLadderState:
    anchor_price: Decimal     # Mid price when grid was first built this session
    step_price: Decimal       # round_to_tick(anchor_price * spacing_bps / 10000)
    net_offset: int = 0       # +1 per grid SELL fill, -1 per grid BUY fill
```

**Lifecycle:**

- Created on first planner tick (when grid is first built).
- Updated on each grid fill event (`net_offset` only).
- Destroyed on restart (volatile, not persisted).

### 4.2 Effective Center

```
effective_center = anchor_price + net_offset * step_price
```

This is the center around which desired levels are computed. It moves discretely
by `+/- step_price` on each grid fill, NOT continuously with `mid_price`.

### 4.3 SSOT for Ladder State

The `_RollingLadderState` is the **sole source of truth** for desired level computation.
The exchange (open orders via AccountSync) is the source of truth for actual state.
The planner diffs desired vs actual.

`mid_price` is NOT used for level computation after initialization. It is still used for:

- Initial anchor computation (first tick).
- Feature engine / NATR calculation.
- Adaptive spacing computation (if enabled).
- Display / logging.

---

## 5. Level Computation Formulas

### 5.1 Desired Levels

```
BUY[i]  = round_to_tick(effective_center - i * step_price),   i = 1..N_buy
SELL[i] = round_to_tick(effective_center + i * step_price),    i = 1..N_sell
```

Where:

- `effective_center = anchor_price + net_offset * step_price`
- `round_to_tick(p) = floor(p / tick_size) * tick_size`

### 5.2 Step Price Computation

`step_price` is computed ONCE when the ladder is initialized:

```
step_price = round_to_tick(anchor_price * effective_spacing_bps / 10000)
```

Where `effective_spacing_bps` is:

- **Static:** `base_spacing_bps` (config)
- **Adaptive:** `max(natr_bps * step_alpha / 100, step_min_bps)` (if NATR enabled and fresh)

`step_price` does NOT change after initialization, even if NATR changes. This
preserves INV-2 (uniform spacing). Step refresh happens only on re-anchor events
(restart, or future manual re-anchor).

### 5.3 Numeric Example

BTCUSDT, `anchor_price = 66000`, `spacing_bps = 10`, `tick_size = 0.10`, N=3:

```
step_price = round_to_tick(66000 * 10 / 10000) = round_to_tick(66.0) = 66.0
```

Initial grid (`net_offset = 0`, `effective_center = 66000`):

```
SELL: 66066.0 (i=1)  66132.0 (i=2)  66198.0 (i=3)
BUY:  65934.0 (i=1)  65868.0 (i=2)  65802.0 (i=3)
```

After 1 BUY fill at 65934 (`net_offset = -1`, `effective_center = 65934`):

```
SELL: 66000.0 (i=1)  66066.0 (i=2)  66132.0 (i=3)
BUY:  65868.0 (i=1)  65802.0 (i=2)  65736.0 (i=3)
```

---

## 6. Action Generation Rules

### 6.1 Grid BUY Fill

**Trigger:** Grid order (`strategy_id="d"`, side=BUY) disappears from exchange
(detected via AccountSync snapshot diff, not in `_pending_cancels`).

**State update:**

```
net_offset -= 1     # effective_center moves down by step_price
```

**Planner diff (next tick):**

Using the example from 5.3 (center was 66000, now 65934 after BUY fill at 65934):

| Desired | Exchange | Result |
|---------|----------|--------|
| SELL 66132 | SELL 66132 | MATCH |
| SELL 66066 | SELL 66066 | MATCH |
| SELL 66000 | -- | MISSING: PLACE |
| -- | SELL 66198 | EXTRA: CANCEL |
| BUY 65868 | BUY 65868 | MATCH |
| BUY 65802 | BUY 65802 | MATCH |
| BUY 65736 | -- | MISSING: PLACE |

**Grid actions:** 1 CANCEL + 1 PLACE = 2 grid actions.
(INV-9: inner SELL at 66000 is reserved for TP — planner does not PLACE it. Cycle layer
places TP SELL separately.)

### 6.2 Grid SELL Fill

**Mirror of 6.1:**

```
net_offset += 1     # effective_center moves up by step_price
```

**Grid actions:** CANCEL lowest BUY + PLACE new SELL at top frontier. 2 grid actions.
(INV-9: inner BUY reserved for TP — planner does not PLACE it.)

### 6.3 Multiple Grid Fills (Same Snapshot)

If `k` grid BUY fills detected in one snapshot:

```
net_offset -= k
```

**Grid actions:** `k` CANCEL + `k` PLACE = `2k` grid actions. Linear in fill count.
(INV-9: `k` inner levels reserved for TPs.)

For N=5, 2 fills: 4 grid actions + 2 TP PLACEs (vs 20 for full rebuild). For N=5, 1 fill: 2 grid actions + 1 TP PLACE (vs 20).

### 6.4 TP Fill

**Trigger:** TP order (`strategy_id="tp"`) disappears from exchange.

**State update:** NONE. `net_offset` unchanged.

**Planner diff:** TP fill itself doesn't remove a grid order. However, if
`TP_SLOT_TAKEOVER` previously cancelled a grid order to make room for the TP,
that grid level is now missing.

**Actions:** 0-1 PLACE (`GRID_FILL` to restore the slot-takeover'd level). The
planner handles this naturally via diff -- no special `TP_FILL_REPLENISH` logic needed.

### 6.5 Mid-Price Drift (No Fills)

When `mid_price` moves but no fills occur:

- `net_offset` unchanged.
- `effective_center` unchanged.
- Desired levels unchanged.
- Planner diff: all match (within epsilon).
- **Actions: 0.**

This is the key behavioral change from doc-25: mid-price drift does NOT trigger
`GRID_SHIFT`. The grid is anchored to rolling ladder state, not to `mid_price`.

### 6.6 When Does GRID_SHIFT Occur?

Full grid rebuild (`GRID_SHIFT`) occurs ONLY on:

1. **Restart / re-anchor:** new anchor, new desired levels, diff against exchange.
2. **Explicit re-anchor:** future manual command or extended flat-period reset.
3. **Emergency:** if exchange state diverges beyond recovery threshold.

`GRID_SHIFT` is NEVER triggered by:

- Normal price movement.
- Grid fills.
- TP fills.

---

## 7. Tick Pipeline Order

### 7.1 Current Order (doc-25)

```
1. AccountSync tick -> snapshot
2. Grid freeze check
3. Planner (computes desired from mid_price)
4. Anti-churn filter
5. Convergence guards
6. Cycle layer (fill detection -> TP generation -> replenish)
```

Fill detection runs AFTER the planner. With rolling grid, fills must update
`net_offset` BEFORE the planner computes desired levels.

### 7.2 Required Order (rolling grid)

```
1. AccountSync tick -> snapshot
2. Grid freeze check (modified: see 8.2)
3. Fill detection (extract from cycle layer, update net_offset)
4. Planner (computes desired from effective_center)
5. Convergence guards
6. Cycle layer remainder (TP generation, TP expiry, TP renew)
```

**Key change:** fill detection (currently Phase 1 of `LiveCycleLayerV1.on_snapshot()`)
must execute before the planner. This means either:

- (a) Extract fill detection from `on_snapshot()` into a separate method called earlier.
- (b) Add a pre-planner fill scan that only updates `net_offset` (cycle layer still runs
  later for TP generation).

Decision deferred to implementation PR.

---

## 8. Interaction with Existing Mechanisms

### 8.1 Converge-First (PR-P0-RACE-1, ADR-084)

**Status: PRESERVED, unchanged.**

Rolling shifts still dispatch PLACEs and CANCELs. Convergence guards still apply:

- Guard 1 (inflight latch): wait for AccountSync after dispatch.
- Guard 2 (cancel-first on extras): if stale orders exist, cancel only.
- Guard 3 (budget pre-check): if budget insufficient, defer.
- Guard 4 (ADR-090, unreconciled placement cap): if `_unreconciled_place_count[symbol][side] >= levels * 2`, suppress PLACEs on that side. Defense-in-depth — fires only if both inflight CID tracking and disappearance heuristic fail. Side-selective (capping BUY does not block SELL). Emits `PLACEMENT_CAPPED` (WARNING).

Key improvement: rolling shifts use 3 actions per fill (vs 20 for full rebuild),
so budget burns ~7x slower.

### 8.1a Fill Detection Paths (ADR-090)

Fill detection uses two complementary paths:

1. **Inflight CID reconciliation:** dispatched PLACE CIDs tracked in `_inflight_placed_cids`.
   On first post-dispatch REST sync (`account_sync_generation > dispatch sync_gen`):
   - CID in `all_open_ids` (unfiltered) → **survived** → handed to disappearance heuristic.
   - CID absent → **inflight fill** → `INFLIGHT_FILL_DETECTED` (INFO), offset shift applied.
   This is the first-fill path for orders filling within the REST sync interval (~5s).

2. **Disappearance heuristic:** order in `_prev_rolling_orders` but absent from current
   `strategy_id="d"` snapshot → fill (unless in pending cancels). This is the primary path
   for orders surviving at least one REST snapshot.

**Reconciliation contract:** every dispatched PLACE increments `_unreconciled_place_count`
once; first post-dispatch sync decrements exactly once, regardless of survival or inflight fill.

### 8.2 Grid Freeze in Position (PR-FREEZE)

**Status: MUST BE MODIFIED.**

Current behavior: freeze ALL planner output when position open.

Problem: grid BUY fill opens a position. Freeze blocks the planner. The 3-action
rolling shift (CANCEL top SELL + PLACE inner SELL + PLACE frontier BUY) never
executes. On TP fill (position closes), grid unfreezes with stale desired levels.

**Required change:** fill-driven shifts MUST be exempt from grid freeze. The freeze
should prevent mid-price-driven rebuilds (which don't exist in rolling mode anyway)
but not fill-driven ladder shifts.

Implementation options (to decide in implementation PR):

- (a) Disable grid freeze entirely when rolling mode is active.
- (b) Keep freeze but exempt fill-driven shifts (flag: `net_offset` changed since last plan).
- (c) Apply freeze only after the fill-driven shift completes (shift first, then freeze).

### 8.3 Anti-Churn (PR-ANTI-CHURN)

**Status: LARGELY OBSOLETE for rolling mode.**

Anti-churn suppresses mid-price-driven `GRID_SHIFT` when move < 50bps. Rolling grid
doesn't produce mid-driven shifts at all. Anti-churn still applies to:

- Restart re-anchor (one-time shift).
- Explicit re-anchor events.

Fill-driven shifts bypass anti-churn (they are by definition desired behavior).

### 8.4 TP Atomicity (PR-P0-TP-CLOSE-ATOMIC, ADR-083)

**Status: PRESERVED, unchanged.**

TP PLACE + TP_SLOT_TAKEOVER CANCEL remain atomic via `correlation_id`. Rolling grid
doesn't change TP generation -- it's in the cycle layer, not the planner.

### 8.5 TP Slot Takeover (PR-ROLL-2)

**Status: PRESERVED, cooperates with rolling shift.**

When TP created after grid fill:

1. Cycle layer cancels farthest same-side grid order (`TP_SLOT_TAKEOVER`).
2. Rolling shift already wants to CANCEL that same order (it's "extra" after shift).
3. `TP_SLOT_TAKEOVER` pre-cancels the extra; planner sees fewer extras on next tick.

Net effect: `TP_SLOT_TAKEOVER` and rolling shift cooperate. No conflict, no double-cancel
(planner skips already-cancelled orders).

### 8.6 Replenish (PR-INV-4) and TP_FILL_REPLENISH (engine)

**Status: OBSOLETE in rolling mode.**

Current replenish: after grid fill, place order at `level+1` (further from center). This
was needed because the current planner recenters on mid, losing filled level context.

Rolling grid: after grid fill, planner already computes the correct shifted desired
levels. Missing levels are restored via normal `GRID_FILL` from planner diff. No
separate replenish mechanism needed.

After TP fill: the slot-takeover'd grid order is restored by planner diff (`GRID_FILL`).
No separate `TP_FILL_REPLENISH` needed.

**Migration:** replenish mechanisms should be disabled when rolling mode is active.
Formal removal deferred to cleanup PR.

### 8.7 Reduce-Only Enforcement (PR-ROLL-1)

**Status: PRESERVED, unchanged.**

Rolling grid PLACEs are still classified by intent (`INCREASE_RISK` / `REDUCE_RISK`).
Reduce-only enforcement applies to PLACEs on the opposite side when position is open.

Note: the inner SELL placed after a BUY fill (see 6.1) is `INCREASE_RISK` -- it opens
a potential short entry. This is correct: the grid is designed to capture both sides.
Risk is managed by Gate 5 (max position cap), not by blocking grid PLACEs.

---

## 9. Order Matching Strategy

### 9.1 Price-Based Matching (Change from doc-25)

**Current** (doc-25 SS 25.5 Step 3): Match exchange orders to desired levels by
`level_id` in `clientOrderId`.

**Problem:** After a rolling shift, exchange orders retain their original `level_id`
from a previous center. A SELL originally at `level_id=1` (from old center) might
now correspond to `level_id=2` (from new center). Level-id matching produces
false mismatches, triggering unnecessary CANCEL+PLACE.

**Rolling grid:** Match by **(side, price)** within `price_epsilon` tolerance.

### 9.2 Matching Algorithm

```
For each desired level (side, price):
    Find unmatched exchange order with same side and
        |exchange_price - desired_price| <= price_epsilon
    If found: MATCH (no action)
    If not found: MISSING -> PLACE

For each unmatched exchange order (after all desired matched):
    EXTRA -> CANCEL
```

### 9.3 Level ID in New Orders

New PLACEs use the level index from the desired grid (`i` in `BUY[i]` / `SELL[i]`).
This is for identification/logging purposes only -- matching does NOT depend on
`level_id`.

---

## 10. Restart / AccountSync Recovery

### 10.1 On Engine Start

1. No persisted rolling state -- ladder is volatile (in-memory).
2. First AccountSync snapshot provides `mid_price` and `open_orders`.
3. Engine initializes:
   ```
   anchor_price = mid_price
   step_price   = round_to_tick(anchor_price * effective_spacing_bps / 10000)
   net_offset   = 0
   ```
4. Planner computes desired levels from anchor.
5. Diff against exchange: CANCEL extras (`GRID_TRIM`), PLACE missing (`GRID_FILL`).

### 10.2 State Loss on Restart

After restart, `net_offset` is lost. The grid re-anchors at current `mid_price`.

**Consequence:** Some orders may be cancelled and replaced at slightly different
prices (old center != new anchor). This is a one-time cost bounded by
`N_buy + N_sell` actions.

**Acceptable because:**

- Restart is infrequent.
- One-time rebuild is bounded.
- Alternative (persisted state) adds complexity and failure modes.
- The exchange-truth reconciliation (INV-8) handles all cases cleanly.

### 10.3 AccountSync Reconnect (Without Restart)

If AccountSync loses connection and reconnects (no engine restart):

- Rolling state preserved (in-memory, engine still running).
- Next snapshot shows current exchange orders.
- Planner diffs as usual.
- No re-anchor needed.

### 10.4 Live Re-Anchor (INV-10, ADR-088)

If all grinder orders disappear while the engine is running (external cleanup,
exchange-side cancel, margin call), the grid re-anchors to current `mid_price`.

**SSOT:** `anchor_price = snapshot.mid_price = (bid_price + ask_price) / 2`.
Raw `Decimal`, NOT tick-rounded.

**5-condition contract (all must be true):**

1. `rolling_state_exists` — planner has `_RollingLadderState` for symbol.
2. `no_grinder_orders` — no orders with `parse_client_order_id() != None` and prefix `"grinder"` on exchange.
3. `no_fresh_inflight_latch` — inflight blocks only while `account_sync_generation <= inflight.sync_gen`. Stale inflight (sync refreshed since dispatch) does not block reset — REST is the source of truth after sync refresh.
4. `position_flat` — `_get_position_qty(symbol) == 0`. If `None` (AccountSync unavailable), blocked with `reason=POSITION_UNKNOWN`.
5. `no_pending_cancels_for_symbol` — `_count_pending_cancels_for_symbol(symbol) == 0`.

**Same-tick re-anchor:** `reset_rolling_state()` called BEFORE `_plan_grid()` in the same
`process_snapshot()` call. Next `plan()` sees no rolling state → re-inits from fresh `mid_price`.

**Formula:** `desired_grid_count = 2 * N` (full grid from new anchor, offset=0).

**Two-layer cleanup:**
- Planner-owned: anchor, step, net_offset, tp_slot_reservations.
- Engine-owned: `_prev_rolling_orders`, pending cancels (symbol-scoped), `_inflight_deferred_logged`, throttle keys, `_inflight_placed_cids` (ADR-090: dispatched PLACE CIDs awaiting reconciliation), `_unreconciled_place_count` (ADR-090: per-symbol per-side unreconciled PLACE counter).

**Slot state model:** `{grid, tp, reserved, vacant}` (unchanged from INV-9).

**Blocked logging:** `ANCHOR_RESET_BLOCKED` throttled via `_anchor_reset_blocked_logged: set[str]`
keyed by `"{symbol}:{reason}"`. Reason-level throttle. Cleared when state changes (orders reappear,
reset succeeds, or inflight active).

**State matrix:**

| Exchange | Position | Pending | Inflight | Result |
|----------|----------|---------|----------|--------|
| empty | flat | 0 | none/stale | ANCHOR_RESET |
| empty | flat | >0 | none/stale | BLOCKED (PENDING_CANCELS) |
| empty | open | any | none/stale | BLOCKED (POSITION_OPEN) |
| empty | unknown | any | none/stale | BLOCKED (POSITION_UNKNOWN) |
| empty | any | any | fresh | skip (inflight in progress, sync not yet refreshed) |
| orders | any | any | any | skip (normal operation) |

Inflight is **fresh** when `account_sync_generation <= inflight.sync_gen` (sync hasn't
refreshed since dispatch). Inflight is **stale** when `account_sync_generation > inflight.sync_gen`
(sync refreshed — REST is source of truth).

---

## 11. Grid Fill vs TP Fill Distinction

### 11.1 SSOT

`is_tp_order(client_order_id)` at `src/grinder/reconcile/identity.py:174`.

- Grid order: `strategy_id == "d"` (grid identity).
- TP order: `strategy_id == "tp"` (TP identity).

### 11.2 Effect on Ladder

| Event | `net_offset` | Ladder shift | Planner actions |
|-------|-------------|--------------|-----------------|
| Grid BUY fill | `-1` | Down by `step_price` | 1 CANCEL + 1 grid PLACE (INV-9: inner reserved for TP) |
| Grid SELL fill | `+1` | Up by `step_price` | 1 CANCEL + 1 grid PLACE (INV-9: inner reserved for TP) |
| TP fill | unchanged | None | 0-1 PLACE (restore slot) |

---

## 12. What Becomes Obsolete in Rolling Mode

| Mechanism | Status | Rationale |
|-----------|--------|-----------|
| GRID_SHIFT from mid drift | Eliminated | Grid doesn't track mid_price |
| Anti-churn (mid-price suppression) | Largely N/A | No mid-driven shifts to suppress |
| Grid freeze (planner suppression) | Must modify | Fill-driven shifts must pass |
| Replenish (cycle_layer) | Obsolete | Planner diff handles level restoration |
| TP_FILL_REPLENISH (engine) | Obsolete | Planner diff handles slot restoration |
| `_grid_anchor_mid` | Replaced | Rolling state replaces mid-anchor |
| Hysteresis (`rebalance_threshold`) | Reduced scope | No continuous mid tracking |

---

## 13. Test Matrix (for Implementation PR)

### Invariant Tests

| ID | Test | Invariant |
|----|------|-----------|
| T1 | After 1 BUY fill: desired has N_buy BUY + N_sell SELL levels | INV-1 |
| T2 | After 1 SELL fill: N_buy + N_sell preserved | INV-1 |
| T3 | Adjacent desired levels differ by exactly step_price | INV-2 |
| T4 | After 1 BUY fill: effective_center decreased by step_price | INV-3 |
| T5 | After k BUY fills: effective_center decreased by k * step_price | INV-3 |
| T6 | After 1 SELL fill: effective_center increased by step_price | INV-3 |
| T7 | New orders at frontier, not at center | INV-4 |
| T8 | Fill generates 1 CANCEL + 1 grid PLACE = 2 grid actions (inner reserved for TP, INV-9) | INV-5, INV-9 |
| T9 | TP fill does NOT change net_offset | INV-6 |
| T10 | BUY fill path mirrors SELL fill path | INV-7 |
| T11 | After restart: anchor=mid, offset=0, grid rebuilt | INV-8 |

### Integration Tests

| ID | Test | Verifies |
|----|------|----------|
| T12 | k fills in one snapshot: 3k actions | Multi-fill scaling |
| T13 | TP_SLOT_TAKEOVER reduces planner extras count | Cooperation |
| T14 | Convergence guards apply to fill-driven shifts | Guard compat |
| T15 | Budget check: 3 PLACEs per fill fit budget | Budget compat |
| T16 | Price-based matching: correct price, wrong level_id -> MATCH | New matching |
| T17 | Mid-price drift produces 0 planner actions | No mid-shift |
| T18 | TP fill restores slot-takeover'd level via GRID_FILL | TP restoration |

### TP Slot Ownership Tests (INV-9, ADR-086)

| ID | Test | Verifies |
|----|------|----------|
| T21 | BUY fill → desired_grid_count = 2*N - 1, SELL side has N-1 levels | Reservation |
| T22 | SELL fill → desired_grid_count = 2*N - 1, BUY side has N-1 levels | Reservation |
| T23 | Mixed (2 BUY + 1 SELL) → desired_grid_count = 2*N - 3 | Multi-fill reservation |
| T24 | Next tick (no fill): desired_grid_count = 2*N (reservation cleared) | One-tick lifecycle |
| T25 | TP on exchange matches desired level by price → no redundant grid PLACE | Exchange-truth SSOT |
| T26 | BUY fill + action merge: no grid SELL PLACE within epsilon of TP SELL PLACE | Overlap prevention |
| T27 | SELL fill + action merge: no grid BUY PLACE within epsilon of TP BUY PLACE | Overlap prevention |
| T28 | Fill tick post-action model (unit test only): applied_grid + applied_tp = 2*N | Post-action model |
| T29 | No same-side TP/grid overlap within epsilon in post-action snapshot | No overlap |
| T30 | Live-style: BUY@67768.30 → TP_SELL@67836 → grid_SELL@67836.20, overlap prevented | Integration |
| T31 | Overlap guard suppresses grid PLACE; TP absent next tick → self-heal | Self-heal |
| T32 | TP expiry → slot vacant → grid PLACE next tick | TP lifecycle |
| T33 | Saturation: 4 fills with N=3 → reservation clamps at 3, per-side >= 0 | Saturation |
| T34 | TP PLACE blocked → reserved → vacant → grid (self-heal) | Blocked TP |
| T35 | TP renew → slot stays tp, no grid overlap | TP renewal |
| T36 | Existing TP + new BUY fill (Contract 2a): old TP matched, farthest cancelled, new reserved | Contract 2a |
| T37 | Overlap guard with missing planner config → returns unchanged, no crash | Fail-open |

### Backward Compatibility

| ID | Test | Verifies |
|----|------|----------|
| T19 | `GRINDER_LIVE_ROLLING_GRID=0` (default): current behavior preserved | Feature flag |
| T20 | Rolling mode off: GRID_SHIFT still triggered by mid drift | Legacy compat |

---

## 14. Env Vars

| Var | Type | Default | Purpose |
|-----|------|---------|---------|
| `GRINDER_LIVE_ROLLING_GRID` | bool | `False` | Enable rolling infinite grid mode |

Safe-by-default: disabled. Current mid-anchored behavior (doc-25) is the default.
Requires `GRINDER_LIVE_PLANNER_ENABLED=1` to take effect.

---

## 15. Migration Path

### Phase 1: Spec PR (this PR)

- Define contract, invariants, formulas, interaction map.
- No code changes.

### Phase 2: Implementation PR

- New matching strategy (price-based).
- Rolling state tracking (`net_offset`).
- Fill detection before planner (tick pipeline reorder).
- Feature flag: `GRINDER_LIVE_ROLLING_GRID`.
- Disable replenish in rolling mode.
- Modify grid freeze for fill-driven shifts.
- Tests T1-T20.

### Phase 3: Live Verification PR

- Live trading run with rolling grid enabled.
- Forensic pack comparison: rolling vs mid-anchored.
- Budget usage, fill count, grid stability metrics.

### Phase 4: Cleanup PR (optional)

- Remove obsolete replenish mechanisms.
- Simplify anti-churn for rolling mode.
- Remove grid freeze override for rolling mode.

---

## 16. Open Questions (to resolve before Phase 2)

1. **Adaptive spacing refresh:** When NATR changes significantly, should `step_price`
   be updated? Current spec says no (fixed per session). Extended sessions may
   benefit from periodic refresh with re-anchor.

2. **Max drift from market:** If `net_offset` grows large, the grid drifts far from
   current market. Should there be a `max_offset` threshold that triggers re-anchor?

3. **Grid freeze interaction:** Should fill-driven shifts be exempt from freeze (option b),
   or should freeze be disabled entirely for rolling mode (option a)?

4. **Replenish removal timing:** Should replenish be disabled immediately when rolling
   mode is active, or kept as a fallback during Phase 2 development?

5. **Fill detection timing:** Should fill detection be extracted from cycle layer into a
   separate pre-planner phase, or should `net_offset` be computed independently?
