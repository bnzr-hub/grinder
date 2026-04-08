# 41. Autonomous Risk Manager v1 Spec

**Status:** CANDIDATE — architecture + rollout plan. No implementation changes.

**Depends on existing runtime:** selector V2, adaptive spacing, execution-plane rotation, EventLedger Phase 2 parity, PositionLedger Phase 3 first consumer, bootstrap two-stage prefilter, bounded tuning refresh.

---

## 1. Purpose

Define the first **production-shaped autonomous capital/risk manager** for the live multi-symbol runtime.

The system must be able to operate without manual intervention from a small set of operator inputs:

- account capital
- maximum concurrent symbols
- maximum leverage
- daily loss limit
- daily profit-lock policy
- per-symbol risk budget range
- minimum entry levels per symbol
- exchange constraints
- market-derived spacing / volatility

This document is the architectural SSOT for:

- day/session risk state
- portfolio risk budget allocation
- per-symbol campaign risk
- risk-driven grid admissibility
- risk-driven order sizing
- symbol degradation / unload policy

This document does **not** redefine:

- selector feature computation
- grid_v2 internal branch mechanics
- EventLedger / PositionLedger internals
- exchange connector contracts

It integrates them under a single risk authority layer.

---

## 2. Problem statement

The autonomous runtime is already strong operationally:

- symbols are continuously selected and rotated
- active symbols can be gracefully exited
- fills are event-first
- position truth has a trusted read path
- tuning refresh prevents post-bootstrap decay

However, capital allocation is still fragmented across multiple mechanisms:

- tuning solver computes legal `order_size`
- selector/prefilter uses `max_notional_per_order`
- live engine uses `max_position_usd`
- portfolio risk gates enforce notional and DD caps
- symbol risk manager can escalate to `CAPPED` / `EXIT_ONLY`

These protections are useful, but they do **not** yet form a single autonomous risk program.

Missing today:

- daily profit-lock / trailing giveback stop
- account-wide day/session modes
- portfolio allocator for up to 5 concurrent symbols
- formal per-symbol campaign risk budget
- risk-driven derivation of order size / grid depth
- deterministic `NO_GO` when exchange minimums break the risk model

Without this layer, multi-symbol expansion would rely on several independent controls instead of one coherent risk authority.

---

## 3. Current factual baseline

### 3.1 Implemented now

The following runtime primitives already exist and should be reused:

- **Continuous selection + rotation**
  - `RotationController` with `top_k`, `max_changes_per_cycle`, `min_hold_cycles`
  - graceful deactivation path for non-flat symbols
- **Graceful exit control**
  - active symbols can be moved to `graceful_exit_only`
  - new entries blocked, exits remain allowed
- **Per-symbol risk escalation**
  - `NORMAL -> CAPPED -> EXIT_ONLY`
- **Staged unload**
  - reduce-only unload controller for `EXIT_ONLY`
- **Portfolio/symbol risk gate**
  - risk base freshness
  - symbol notional cap
  - portfolio gross/net caps
  - symbol/portfolio DD ladder
- **Emergency exit**
  - cancel + reduce-only MARKET flatten path
- **Reduce-only budget guard**
  - prevents over-closing and exit overbooking
- **Adaptive spacing**
  - spacing is already volatility-aware
- **Position truth**
  - trusted `PositionLedger` with snapshot fallback
- **Tuning**
  - legal symbol tuning from exchange constraints
  - periodic refresh after startup

### 3.2 Not implemented now

The following are **not** yet first-class autonomous runtime capabilities:

- day/session profit-lock manager
- day stop / resume policy
- portfolio-wide live risk allocator across up to 5 symbols
- risk-budget-driven grid admissibility
- risk-budget-driven level count / order size
- unified coordination between day mode, symbol mode, and portfolio budget

---

## 4. Design goals

### 4.1 Primary goals

1. The system must make risk decisions automatically without operator micromanagement.
2. Risk must be allocated from a single authority layer, not inferred independently by multiple modules.
3. The runtime must support up to **5** concurrent symbol campaigns safely.
4. Maximum leverage must be respected as a hard cap, but must not become the sizing target.
5. Exchange minimums must never force silent risk inflation.
6. Existing active-engine correctness guarantees must be preserved.

### 4.2 Non-goals

1. No hot-swap of full live grid geometry inside already-running engines in v1.
2. No hedge-mode-specific portfolio optimizer in v1.
3. No ML-based risk budgeting in v1.
4. No correlation matrix / beta-adjusted allocator in v1.

---

## 5. Core design principles

### 5.1 Risk budget is the SSOT

The primary control variable is **risk budget**, not notional.

Correct hierarchy:

`day mode -> portfolio budget -> symbol budget -> grid admissibility -> order size -> notional`

### 5.2 Leverage is a capacity ceiling, not a risk target

`max_leverage=5` means:

- the system may use leverage to run several symbols simultaneously
- the system may **not** treat 5x as permission to maximize exposure

Correct interpretation:

- leverage answers: "Can the portfolio carry this safe exposure?"
- risk budget answers: "How much exposure is safe in the first place?"

### 5.3 Exchange minimums must not inflate risk silently

If a symbol requires a minimum legal order size that exceeds the allowed risk budget, the symbol is `NO_GO`.

The runtime must not "fix" this by enlarging size until it fits exchange rules.

### 5.4 Rotation and risk must cooperate

Ranking/selection decides **which** symbols are desirable.
Risk manager decides:

- whether a new symbol may be admitted
- how much budget it may receive
- whether an active symbol should be reduced or exited

### 5.5 Active engine safety beats tuning freshness

In v1, refreshed tuning/risk parameters apply to:

- new activations
- new entries on symbols where entry flow is centrally gated

They do not require full live hot-reconfiguration of existing engine geometry.

---

## 6. System inputs

### 6.1 Hard operator-configured limits

- `max_active_symbols = 5`
- `max_leverage = 5`
- `daily_loss_limit_pct = 10%`
- `daily_profit_lock_trigger_pct = 3%`
- `daily_profit_giveback_pct = 1%`
- `min_entry_levels = 15`

### 6.2 Dynamic policy inputs

- market regime (`GOOD`, `NEUTRAL`, `TOXIC`)
- selector score / rank
- current equity
- day PnL and day peak
- open symbol campaigns
- symbol volatility (`NATR`)
- adaptive spacing (`step_pct`)
- exchange symbol constraints

### 6.3 Derived policy parameters

Recommended v1 defaults:

- `symbol_risk_pct_good = 6%`
- `symbol_risk_pct_neutral = 3%`
- `symbol_risk_pct_toxic = 2%`
- `portfolio_live_risk_cap_pct = 6%`

Note:

- `daily_loss_limit_pct = 10%` is a **hard circuit breaker**
- `portfolio_live_risk_cap_pct = 6%` is the **maximum simultaneous deployed risk**
- the live deployed risk cap should remain below the daily hard stop

---

## 7. Control-plane architecture

```
┌───────────────────────────────────────────────────────────────────┐
│                 AUTONOMOUS RISK MANAGER v1                        │
├───────────────────────────────────────────────────────────────────┤
│  1. DayRiskManager                                                │
│     - day start equity                                            │
│     - day peak equity                                             │
│     - day mode / stop-for-day                                     │
│                                                                   │
│  2. PortfolioBudgetAllocator                                      │
│     - max 5 active symbols                                        │
│     - portfolio live risk cap                                     │
│     - gross exposure / leverage ceiling                           │
│                                                                   │
│  3. SymbolCampaignRiskPlanner                                     │
│     - per-symbol budget                                           │
│     - symbol mode: NORMAL / DEFENSIVE / EXIT_ONLY / UNLOAD        │
│     - degradation response                                        │
│                                                                   │
│  4. GridRiskSizer                                                 │
│     - min entry levels                                            │
│     - adverse-move estimate                                       │
│     - max symbol notional                                         │
│     - order size                                                  │
│     - NO_GO if legal minimums break risk budget                   │
│                                                                   │
│  5. Execution Integration                                         │
│     - admission gating                                            │
│     - graceful exit                                               │
│     - staged unload                                               │
│     - emergency flatten                                           │
└───────────────────────────────────────────────────────────────────┘
```

---

## 8. Authority boundaries

### 8.1 DayRiskManager

Owns:

- day start / peak equity
- daily PnL state
- day operating mode
- stop-for-day decision

Does **not** own:

- symbol selection
- exchange execution
- grid internals

### 8.2 PortfolioBudgetAllocator

Owns:

- number of concurrently active symbols
- live portfolio risk budget
- per-symbol budget ceilings
- leverage / gross exposure capacity check

Does **not** own:

- symbol scoring
- symbol-specific exit topology

### 8.3 SymbolCampaignRiskPlanner

Owns:

- whether a symbol may open new risk
- whether a symbol should remain active
- whether it should degrade to `graceful_exit_only`
- whether staged unload should begin

Does **not** own:

- per-order routing
- exchange cleanup mechanics

### 8.4 GridRiskSizer

Owns:

- risk admissibility of a symbol campaign
- risk-driven max notional
- risk-driven level count and order size
- `NO_GO` if risk model and exchange minimums conflict

Does **not** own:

- exchange execution
- rank/selection

---

## 9. Day risk state machine

### 9.1 States

- `NORMAL`
- `DEFENSIVE`
- `STOP_NEW_RISK`
- `FORCE_REDUCE`
- `STOP_FOR_DAY`

### 9.2 Inputs

- `equity_day_start`
- `equity_current`
- `equity_day_peak`

Derived:

- `day_pnl_pct = (equity_current - equity_day_start) / equity_day_start`
- `day_peak_pnl_pct = (equity_day_peak - equity_day_start) / equity_day_start`
- `drawdown_from_peak_pct = day_peak_pnl_pct - day_pnl_pct`

### 9.3 Recommended policy

#### NORMAL

- active while `day_pnl_pct < +3%`
- normal symbol risk caps apply

#### DEFENSIVE

Enter when:

- `day_pnl_pct >= +3%`

Behavior:

- continue trading
- reduce new symbol risk budgets
- lower number of fresh activations if needed

#### STOP_FOR_DAY by profit lock

Define:

- `profit_lock_floor_pct = max(3%, day_peak_pnl_pct - 1%)`

Enter `STOP_FOR_DAY` when:

- `day_peak_pnl_pct >= +3%`
- and `day_pnl_pct <= profit_lock_floor_pct`

Example:

- day reaches `+4%`
- lock floor becomes `+3%`
- if PnL falls back to `+3%`, stop the day

#### STOP_FOR_DAY by loss

Enter when:

- `day_pnl_pct <= -10%`

#### FORCE_REDUCE

Optional intermediate state before `STOP_FOR_DAY` if:

- portfolio risk is too high
- or hard day stop is being approached rapidly

Behavior:

- no new risk
- active symbols move toward unload / flatten

### 9.4 Day-mode consequences

| Mode | New activations | New entries | Existing exits | Forced reduction |
|------|------------------|-------------|----------------|------------------|
| NORMAL | allowed | allowed | allowed | no |
| DEFENSIVE | limited | reduced | allowed | no |
| STOP_NEW_RISK | blocked | blocked | allowed | no |
| FORCE_REDUCE | blocked | blocked | allowed | yes |
| STOP_FOR_DAY | blocked | blocked | allowed | yes / flatten policy |

---

## 10. Symbol campaign state machine

### 10.1 States

- `NORMAL`
- `DEFENSIVE`
- `GRACEFUL_EXIT_ONLY`
- `EXIT_ONLY`
- `UNLOADING`
- `CLOSED`

### 10.2 Entry into degraded states

#### GRACEFUL_EXIT_ONLY

Trigger examples:

- symbol dropped out of desired set
- symbol quality degraded materially
- day mode disallows new risk but open position may unwind naturally

Behavior:

- block new entries
- keep exit mechanics active

#### EXIT_ONLY

Trigger examples:

- symbol risk manager escalation
- per-symbol risk budget violation
- consecutive-loss policy breach
- portfolio/day mode requires hard de-risking

Behavior:

- no new risk
- reduce-only only
- unload controller may activate

#### UNLOADING

Trigger examples:

- day/portfolio risk state requires active reduction
- symbol in `EXIT_ONLY` with non-flat position

Behavior:

- staged reduce-only exits
- bounded rate/cooldown

---

## 11. Portfolio budget model

### 11.1 Hard limits

- `active_symbol_count <= 5`
- `gross_exposure_usd <= equity * 5`

### 11.2 Live deployed portfolio risk

Define:

- `daily_loss_limit_pct = 10%`
- `portfolio_live_risk_cap_pct = 6%`

The live deployed risk cap is the maximum simultaneous risk that may be allocated across active symbols.

This is intentionally lower than the daily hard stop.

### 11.3 Per-symbol budget cap

Each symbol receives:

`symbol_budget_pct = min(regime_symbol_cap_pct, residual_portfolio_risk_pct_adjusted)`

where:

- `regime_symbol_cap_pct` is `1% / 2% / 3%` by market regime
- `residual_portfolio_risk_pct_adjusted` accounts for:
  - already allocated live risk
  - number of remaining symbol slots
  - concentration guard

### 11.4 Concentration policy

Recommended v1:

- no single symbol may receive more than `40%` of currently deployable live risk budget
- no new symbol may be activated if doing so would exceed:
  - portfolio live risk cap
  - leverage/gross capacity
  - concentration cap

---

## 12. Risk-driven grid admissibility

### 12.1 Inputs

- `equity`
- `symbol_budget_pct`
- `step_pct`
- `natr_pct`
- `min_entry_levels`
- exchange constraints

### 12.2 Adverse move model

For v1, define:

- `entry_span_pct = min_entry_levels * step_pct`
- `tail_buffer_pct = max(2 * step_pct, 0.75 * natr_pct)`
- `adverse_move_pct = entry_span_pct + tail_buffer_pct`

This represents:

- the full ladder being filled
- plus additional move beyond the deepest entry

### 12.3 Symbol risk budget

`symbol_risk_usd = equity * symbol_budget_pct`

### 12.4 Safe max symbol notional

`max_symbol_notional_by_risk = symbol_risk_usd / adverse_move_pct`

### 12.5 Capacity clamps

Clamp by:

- symbol/portfolio risk gates
- deployed portfolio risk
- gross exposure cap from leverage
- optional symbol concentration cap

Then:

`max_symbol_notional_usd = min(all active clamps)`

### 12.6 Minimum levels and order size

For baseline uniform sizing:

- `effective_levels = min_entry_levels`
- `order_notional_usd = max_symbol_notional_usd / effective_levels`
- `order_qty = order_notional_usd / price`

Then quantize by exchange step size.

### 12.7 Exchange minimum conflict

If quantized `order_qty` is below exchange minimum legal size:

1. attempt to reduce levels, but never below `min_entry_levels`
2. if still below minimum:
   - return `NO_GO_EXCHANGE_MIN_GT_RISK_BUDGET`

The system must not increase `order_size` above the risk-consistent value just to satisfy exchange minimums.

---

## 13. Leverage policy

### 13.1 Maximum leverage

`max_leverage = 5` is a hard upper bound.

### 13.2 Correct use of leverage

Leverage is used to allow multiple safe symbol campaigns to coexist.

It is **not** used to justify larger risk allocation.

### 13.3 Capacity check

Before admitting a new symbol:

- compute resulting portfolio gross exposure after activation
- ensure:
  - `gross_exposure_after <= equity * max_leverage`

If not:

- reduce symbol budget
- or refuse activation

### 13.4 Up to 5 symbols means "up to", not "always 5"

The runtime may run:

- 1 symbol
- 2 symbols
- 5 symbols

depending on:

- available portfolio risk budget
- leverage capacity
- exchange minimum constraints
- current market quality

It must not force occupancy of all 5 slots.

---

## 14. Integration with existing runtime

### 14.1 Selection and rotation

The current `RotationController` remains valid.

New requirement:

- risk manager must filter or re-prioritize admissible symbols **before** activation
- risk/degradation state must be allowed to push active symbols into:
  - `GRACEFUL_EXIT_ONLY`
  - `EXIT_ONLY`
  - staged unload

### 14.2 Live engine gates

Existing engine-level safety gates remain hard enforcement:

- not armed
- kill switch
- risk base unavailable
- portfolio risk blocks
- drawdown guard
- symbol risk state
- active selector / graceful exit only

The new risk manager should sit **above** them as the planner/allocator.

### 14.3 Position truth

The allocator must use account-wide truth based on:

- trusted `PositionLedger` when available
- snapshot fallback otherwise

This is especially important once multiple symbols are active simultaneously.

### 14.4 Bridge update boundary

Current bridge refresh semantics update config for future activations.

v1 must not require full hot-swap of active engine geometry.

If risk state changes while a symbol is already active:

- block fresh entries
- or stage unload
- but do not assume arbitrary mid-flight geometry mutation is safe

---

## 15. Required new components

### 15.1 `DayRiskManager`

Responsibilities:

- track day start equity
- track day peak equity
- compute day PnL and giveback
- emit day mode

### 15.2 `PortfolioBudgetAllocator`

Responsibilities:

- maintain live allocated risk
- admit/reject new symbols
- allocate per-symbol budgets
- enforce max 5 active symbols
- enforce leverage/gross capacity

### 15.3 `GridRiskSizer`

Responsibilities:

- translate symbol risk budget into:
  - max symbol notional
  - level count
  - order size
  - `NO_GO`

### 15.4 `AutonomousRiskCoordinator`

Responsibilities:

- integrate day mode + portfolio budget + symbol degradation + sizing
- publish effective per-symbol risk plan to the orchestrator/runtime

---

## 16. Observability and evidence

Must expose:

- `DAY_RISK_MODE_CHANGED`
- `DAY_PROFIT_LOCK_ARMED`
- `DAY_STOP_FOR_DAY_TRIGGERED`
- `PORTFOLIO_RISK_BUDGET_ALLOCATED`
- `SYMBOL_RISK_BUDGET_ASSIGNED`
- `SYMBOL_RISK_NO_GO`
- `SYMBOL_RISK_GRACEFUL_EXIT`
- `SYMBOL_RISK_EXIT_ONLY`
- `SYMBOL_UNLOAD_STARTED`
- `GRID_RISK_SIZER_RESULT`

Metrics should include:

- current day mode
- current day pnl %
- current day peak pnl %
- deployed live risk %
- active symbols count
- gross exposure %
- leverage utilization
- per-symbol allocated risk %

---

## 17. Key risks and conflicts

### 17.1 Split-brain risk authority

Current system has several independent sizing/risk controls.

Mitigation:

- the new risk manager must become the planner/allocator SSOT
- existing engine gates stay as hard enforcement only

### 17.2 Active-engine geometry hot-swap risk

Bridge refresh currently targets future activations.

Mitigation:

- v1 uses entry suppression / graceful exit / unload
- no full arbitrary live geometry replacement required

### 17.3 Multi-symbol account-wide truth

Day/portfolio allocator must be based on shared account truth, not per-engine local assumptions.

Mitigation:

- portfolio allocator reads account-wide positions/exposure from the shared truth path

### 17.4 Exchange minimum conflict

Small-budget symbols may become illegal to trade.

Mitigation:

- explicit `NO_GO` instead of silent risk inflation

### 17.5 Correlation blind spot

v1 does not include full correlation/beta model.

Mitigation:

- use conservative live portfolio risk cap
- concentration caps
- maximum 5 active symbols

---

## 18. Rollout plan

### PR-A: Spec + contracts

- docs only
- introduce config/contracts for:
  - day mode
  - symbol budget
  - sizing result

### PR-B: DayRiskManager

- track day start/peak
- implement mode transitions
- no live consumer switch yet

### PR-C: PortfolioBudgetAllocator

- max 5 active symbols
- leverage/gross capacity
- per-symbol budget assignment
- shadow mode first

### PR-D: GridRiskSizer

- compute risk-driven symbol admissibility
- compute max symbol notional / order size / `NO_GO`
- shadow compare against current tuning/notional path

### PR-E: Admission gating

- allocator participates in activation decisions
- no live engine hot-swap

### PR-F: Symbol degradation policy

- graceful exit on score/risk deterioration
- staged unload when required

### PR-G: Day stop / profit-lock enforcement

- `STOP_NEW_RISK`
- `STOP_FOR_DAY`
- optional `FORCE_REDUCE`

### PR-H: Final cutover

- risk budget becomes authoritative planner input for:
  - symbol admission
  - symbol sizing
  - campaign admissibility

---

## 19. Acceptance criteria

The v1 autonomous risk manager is complete only when all of the following are true:

1. Runtime can trade up to 5 concurrent symbols without exceeding portfolio risk or leverage caps.
2. Daily loss hard stop works deterministically.
3. Daily profit-lock / giveback stop works deterministically.
4. Per-symbol risk budget is explicitly computed and observable.
5. Grid order size is derived from risk budget, not independently hand-tuned notional alone.
6. Symbols that fail exchange-minimum-vs-risk checks are rejected as `NO_GO`.
7. Degraded symbols can be gracefully exited or unloaded automatically.
8. Existing engine safety gates remain intact as final hard enforcement.

---

## 20. Bottom line

The autonomous runtime should not merely pick symbols and place orders.

It should:

- know when it is allowed to take risk
- know how much risk it may allocate
- know how many symbols it may carry
- know how large each campaign may be
- know when a symbol must stop growing
- know when the day is over

That is the purpose of `Autonomous Risk Manager v1`.
