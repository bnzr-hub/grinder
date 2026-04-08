# 42. Autonomous Risk Truth Unification Remediation Spec

**Status:** ACTIVE — remediation contract for autonomous risk/sizing/runtime truth unification.

**Depends on existing runtime:** docs 37/38/39/40/41, shared `GridPolicy` SSOT (`5 / 15 / 16 / 20`), autonomous loop, tuning bootstrap, tuning refresher, execution-plane bridge, day risk manager, portfolio budget allocator, risk admission gate.

---

## 1. Purpose

This document fixes a specific class of production-dangerous defects:

- legacy capital defaults still influencing autonomous sizing
- manual notional ceilings still overriding automatic risk-derived sizing
- split-brain between bootstrap, refresher, admission, selector, and live runtime
- fail-open seams that let autonomous activation proceed without real risk facts

The goal is simple:

- autonomous sizing must derive from **real exchange/account truth**
- all autonomous layers must use the **same risk model**
- legacy/manual fallbacks must not silently reactivate in mainnet autonomous mode

This document is the remediation SSOT for the fixes below.

---

## 2. Problem summary

Audit found that the autonomous runtime is only partially on the new risk model.

Already on real truth:

- day risk uses real equity
- portfolio budget uses real equity and gross exposure
- admission uses portfolio-derived per-symbol risk budget

Still on legacy/manual paths:

- bootstrap tuning solver
- refresher retuning solver
- live runtime `GRINDER_GRID_V2_MAX_INV_NOTIONAL`
- selector/runtime `max_notional_per_order`
- several orchestration fail-open seams

That means the system can still mix:

- exchange-derived risk truth
- legacy `$1000` position budget
- manual per-order caps

This is explicitly forbidden for autonomous mainnet mode.

---

## 3. Normative target contract

### 3.1 Capital/risk source of truth

Autonomous sizing must derive from:

- real exchange equity
- real gross exposure
- current day mode
- portfolio allocator output

Autonomous sizing must **not** derive from:

- hardcoded `$1000`
- bridge/parser static defaults
- operator-specified per-order notional caps

If real capital/risk facts are unavailable, autonomous sizing must fail closed for new tuning/activation.

### 3.2 Unified sizing chain

The normative chain is:

`exchange truth -> day risk -> portfolio budget -> per-symbol risk budget -> tuning solver -> bridge/runtime -> live engine`

No parallel capital source is allowed.

### 3.3 Autonomous order-size contract

Order size in autonomous mode must be:

- risk-budget-driven
- bounded by exchange minimums
- rejected (`NO_GO`) when exchange minimums exceed the allowed risk-consistent size

Order size must not be:

- minimum legal qty by legacy default
- capped by a manual CLI notional ceiling

### 3.4 Runtime cap contract

`grid_v2` runtime must use:

- `max_inventory_levels` from shared `GridPolicy`
- `max_inventory_notional_usd` derived from the same tuning risk budget for that symbol

It must not silently default to `$1000`.

### 3.5 Exchange-port safety cap contract

Mainnet exchange ports may still require a per-order notional guard.

In autonomous mode that guard must be:

- **derived from the same symbol risk budget / tuned campaign cap**
- not manually configured via `--max-notional-per-order`

The guard exists only as a final execution safety backstop.
It is not an independent sizing authority.

### 3.6 Selector contract

Selector/prefilter/ranker must not exclude or down-rank candidates using a manual per-order notional ceiling in autonomous mode.

If execution-fit needs a cap-like input, it must be derived from the same risk model or omitted.

---

## 4. Audit findings

### 4.1 P0 blockers

1. Bootstrap solver uses legacy `$1000`
2. Refresher retuning uses legacy `$1000`
3. Live runtime `GRINDER_GRID_V2_MAX_INV_NOTIONAL` uses legacy `$1000`
4. `max_notional_per_order` is still a real manual ceiling in selection, scoring, and exchange dispatch

### 4.2 P1 seams

1. Admission ranker fail-opens before risk facts exist
2. Portfolio budget fail-opens gross exposure to `0`
3. Bootstrap prefilter fail-opens to coarse slice
4. Autonomous loop fail-opens on prefilter/ranking exceptions
5. Bridge can still fall back to default `size_per_level`

### 4.3 P2 drift seams

1. Static spacing fallback still comes from `BridgeConfig()` instead of a dedicated SSOT
2. Bridge/parser still expose old default `size_per_level` / `max_notional_per_order` values as latent rollback paths

---

## 5. Remediation requirements

### 5.1 Remove legacy capital defaults

The autonomous path must not rely on `TuningSolverConfig.max_position_usd = 1000`.

Bootstrap and refresher must pass a real risk-derived `max_position_usd` into the solver.

If a real risk-derived capital base cannot be produced, the symbol must not be tuned for activation.

### 5.2 Derive bootstrap/refresher risk budget from real facts

Bootstrap/refresher sizing must derive symbol risk budget from:

- current exchange equity
- current exchange gross exposure
- default or current day mode
- portfolio allocator

Bootstrap may use a safe startup assumption for:

- day mode = `NORMAL`
- market regime = `NEUTRAL`
- active symbol count = current runtime count or `0` on cold start

This is allowed because it is still based on real exchange capital truth, not a legacy constant.

### 5.3 Remove manual `max_notional_per_order` from autonomous sizing logic

Autonomous selector/prefilter/ranker must not depend on operator-supplied `max_notional_per_order`.

Autonomous exchange-port guards must use a derived cap from the same tuning/risk model.

### 5.4 Propagate derived runtime notional cap

Bridge must propagate a symbol-specific derived `GRINDER_GRID_V2_MAX_INV_NOTIONAL`.

Live engine must consume that value instead of defaulting to `$1000`.

### 5.5 Fail closed when truth is missing

For autonomous new-risk admission and tuning:

- missing equity must not silently fall through
- missing gross exposure must not silently become `0`
- missing tuned size must not silently start engines with a default

---

## 6. PR plan

### PR-A — Remove legacy capital/notional fallbacks

Scope:

- bootstrap solver
- refresher solver
- bridge/runtime `max_inventory_notional_usd`
- derived autonomous exchange-port cap

Success criteria:

- no legacy `$1000` in autonomous bootstrap/refresher/runtime path
- no manual `max_notional_per_order` conflict in autonomous path

### PR-B — Fail-closed autonomous orchestration

Scope:

- admission ranker
- portfolio budget facts
- bootstrap prefilter
- autonomous loop prefilter/ranking failures
- bridge untuned-size fallback

Success criteria:

- autonomous new-risk activation cannot proceed on missing risk truth or failed screening

### PR-C — Drift cleanup

Scope:

- static spacing fallback SSOT cleanup
- removal or deactivation of latent bridge/parser rollback defaults
- docs/state alignment

Success criteria:

- no hidden “old model” source remains reachable in autonomous mode

---

## 7. Review bar

Approve remediation only if:

- `P0 = 0`
- autonomous sizing derives from real exchange/account truth
- selector, admission, bootstrap, refresher, bridge, and runtime share the same model
- no manual notional ceiling remains as an autonomous sizing authority
- missing truth fails closed for new-risk paths

---

## 8. Expected outcome

After this remediation:

- autonomous mode will no longer mix old and new risk models
- tuning, selection, admission, and runtime will agree on the same symbol budget
- canaries will validate the real autonomous system, not a partially legacy one

That is the minimum bar before any further autonomous mainnet trust should be restored.
