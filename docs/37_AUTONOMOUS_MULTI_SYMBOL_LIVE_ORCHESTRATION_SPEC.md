# 37. Autonomous Multi-Symbol Live Orchestration Spec

**Status:** CANDIDATE — docs + contracts only. No implementation changes.

**Supersedes operationally:** `36_MULTI_SYMBOL_ELIGIBILITY_ML_INTEGRATION_V1.md` (which remains the source for Phases 1–3b implementation detail; this document adds the orchestration layer on top).

---

## 1. Purpose and scope

Define how the system operates **fully autonomously 24/7** across multiple symbols:

- builds candidate universe
- prefilters and scores
- selects Top-K active symbols
- trades them via grid_v2
- rotates symbols in/out based on continuous scoring
- handles deactivation, cleanup, and replacement safely
- operates as a single integrated loop without manual restarts

This document is the **architectural SSOT** for the orchestration layer. It does NOT redefine grid_v2, risk, or EventLedger internals — it integrates them.

---

## 2. Current factual baseline

### Implemented now

| Component | Location | Status |
|-----------|----------|--------|
| Multi-symbol `--symbols` CLI | `run_trading.py` | DONE — static operator list |
| PaperEngine per-symbol state | `src/grinder/paper/engine.py` | DONE |
| Shadow Selector (Phase 1) | `src/grinder/selection/shadow_selector.py` | DONE — 17 tests |
| Active Selector (Phase 2) | `src/grinder/selection/active_selector.py` | DONE — 14 tests |
| ML-Assisted Scoring (Phase 3/3b) | via ShadowSelector + ONNX | DONE — 19 tests |
| Hysteresis controls | ActiveSelector | DONE — MIN_HOLD, ENTER/EXIT thresholds, MAX_CHANGES |
| Graceful exit only | ActiveSelector + engine | DONE — grid_v2-aware, no forced unwind |
| ConstraintProvider | `src/grinder/execution/constraint_provider.py` | DONE — loads all Binance symbols |
| Per-symbol risk (inventory, loss) | `src/grinder/risk/` | DONE |
| Portfolio-level risk | PortfolioRiskManager | PARTIAL — primitives exist (gross/net caps, concentration), not full autonomous allocator |
| Risk-saturated mode (ADR-102) | engine.py | DONE |
| Reduce-only budget guard v2 | ADR-104 | DONE |
| Exit topology repair | ADR-105 | DONE |
| EventLedger trusted read | ADR-109 Phase 2 | DONE |
| Degraded recovery boundary | ADR-109 Phase 2 PR-3 | DONE |
| Fatal abort cleanup | ADR-121 | DONE |
| Prefilter spec (hard gates + scoring) | `docs/04_PREFILTER_SPEC.md` | DONE (spec) |
| SOR (CANCEL_REPLACE/BLOCK/NOOP) | `docs/14_SMART_ORDER_ROUTER_SPEC.md` | DONE (AMEND deferred) |
| Launch readiness preflight | `scripts/launch_readiness.py` | DONE |
| Consecutive loss guard | per-symbol persistent | DONE |

### Implemented partially

| Component | What exists | What's missing |
|-----------|------------|----------------|
| Grid_v2 multi-symbol | Single `_grid_v2_symbol` per engine | One engine instance = one grid symbol; multi-grid requires multiple engines or symbol-scoped grid |
| Symbol constraints | ConstraintProvider loads tick/step/minQty from exchangeInfo | Missing: `minNotional` not parsed; no auto-tuning solver |
| Active selector | Operator universe + hysteresis | No autonomous discovery from full exchange universe |
| Cleanup on rotation | `graceful_exit_only` blocks new entries | No explicit flatten-on-deactivation policy |

### Not implemented yet

| Component | Description |
|-----------|-------------|
| Autonomous universe discovery | Auto-scan Binance exchangeInfo for new tradeable symbols |
| Symbol auto-tuning solver | Compute legal grid config from constraints + price + risk |
| Continuous rotation loop | Integrated onboard → trade → deactivate → replace cycle |
| Per-symbol grid_v2 engine | Multiple grid bridges per engine instance |
| Multi-venue routing | Bybit, OKX, COIN-M (deferred P2, ADR-066) |

---

## 3. Target autonomous loop

```
┌─────────────────────────────────────────────────────┐
│               CONTINUOUS ORCHESTRATION LOOP          │
│                                                     │
│  1. Universe Discovery (venue-level only)            │
│     - Fetch exchangeInfo (all symbols)              │
│     - Filter by: USDT-M perpetual, TRADING status,  │
│       not in global blacklist                       │
│     - NO liquidity/volume/spread here (Step 2)      │
│                                                     │
│  2. Hard Prefilter                                   │
│     - Spread, volume, trade count, OI, blacklist    │
│     - Output: eligible candidates                   │
│                                                     │
│  3. Scoring + Ranking                               │
│     - Top-K v1: range + liquidity - toxicity - trend│
│     - Optional ML adjustment (bounded)              │
│     - Output: ranked list with scores               │
│                                                     │
│  4. Active Set Selection                            │
│     - Hysteresis: enter vs stay thresholds          │
│     - Hold timer: MIN_HOLD_CYCLES before removal    │
│     - Change budget: MAX_CHANGES_PER_CYCLE          │
│     - Output: active_set, added, removed, deferred  │
│                                                     │
│  5. Symbol Onboarding (for newly added)             │
│     - Load constraints (tick, step, notional, etc.) │
│     - Compute legal grid config                     │
│     - Validate: TUNED or NO_GO                      │
│                                                     │
│  6. Live Trading                                    │
│     - Grid_v2 per active symbol                     │
│     - Risk gates per-symbol + portfolio             │
│     - EventLedger + sync reconciler                 │
│                                                     │
│  7. Deactivation (for removed symbols)              │
│     - Set graceful_exit_only                        │
│     - Block new entries, maintain exits              │
│     - Wait for FLAT + verify clean                  │
│     - Release grid resources                        │
│                                                     │
│  8. Loop back to Step 1                             │
│     (cycle interval: SELECTOR_CYCLE_S, default 60s) │
└─────────────────────────────────────────────────────┘
```

---

## 4. Required runtime components

### 4.1 Universe provider (NEW)

Periodically fetches Binance exchangeInfo and produces a raw candidate list. Filters: USDT-M perpetual, status=TRADING, not in global blacklist.

**Input:** Binance REST `/fapi/v1/exchangeInfo`
**Output:** `list[str]` — raw symbol candidates
**Refresh:** every `UNIVERSE_REFRESH_S` (default: 300s)

### 4.2 Symbol tuning solver (NEW)

Given a symbol and its constraints, computes a legal grid_v2 config or declares NO_GO.

**Input:**
- `tickSize`, `stepSize`, `minQty` (from current ConstraintProvider)
- `minNotional` (**NOT yet in ConstraintProvider** — must be added before Phase B; sourced from exchangeInfo `NOTIONAL` or `MIN_NOTIONAL` filter)
- Current price (from market data)
- Risk caps (`MAX_POSITION_USD`, `MAX_NOTIONAL_PCT`)
- Grid geometry knobs (`ENTRY_LEVELS`, `STEP_PCT`, `MAX_INV_LEVELS`)

**Phase B prerequisite:** Extend `ConstraintProvider` (or add a new `SymbolMetadataProvider`) to expose `minNotional` from exchangeInfo filters. Current `ConstraintProvider` only parses `step_size`, `min_qty`, and `tick_size`.

**Output:**
- `TunedConfig(order_size, tick_size, step_size, ...)` or `NO_GO(reason)`

**Decision logic:**
1. `min_qty_for_notional = ceil(min_notional / price)`
2. `order_size = max(min_qty_for_notional, step_size)`
3. Check: `order_size * price * max_inv_levels <= MAX_POSITION_USD`
4. Check: tick alignment, step alignment
5. If any check fails → NO_GO with reason

### 4.3 Grid bridge manager (NEW or extended)

Manages per-symbol grid_v2 bridges. Currently the engine has a single `_grid_v2_bridge`. For multi-symbol grid operation, this needs to become symbol-scoped.

**Options:**
- A: Multiple engine instances (one per symbol) behind an orchestrator
- B: Symbol-scoped bridge dict inside a single engine
- Decision deferred to implementation PR.

### 4.4 Rotation controller (NEW)

Owns the state machine for symbol lifecycle transitions. Calls selector, tuning solver, and bridge manager.

---

## 5. Symbol state machine

```
         DISCOVERED
             │
             ▼
      PREFILTER_BLOCKED ──────────────────┐
             │ (passes prefilter)          │ (fails prefilter)
             ▼                             │
          ELIGIBLE                         │
             │                             │
             ▼                             │
        TUNING_CHECK                       │
           │    │                          │
           │    └── NO_GO ─────────────────┤
           ▼                               │
          TUNED                            │
             │                             │
             ▼ (selected into Top-K)       │
          ACTIVE                           │
             │                             │
             ▼ (dropped from Top-K         │
                 but position non-flat)    │
     GRACEFUL_EXIT_ONLY                    │
             │                             │
             ▼ (position = FLAT)           │
         COOLDOWN ─────────────────────────┘
             │ (cooldown expired)
             ▼
          ELIGIBLE (re-enters scoring)
```

**State descriptions:**

| State | Meaning | Entries allowed | Exits allowed |
|-------|---------|-----------------|---------------|
| DISCOVERED | Found in universe | No | No |
| PREFILTER_BLOCKED | Failed hard filter | No | No |
| ELIGIBLE | Passed prefilter, not yet tuned | No | No |
| TUNING_CHECK | Constraint validation in progress | No | No |
| NO_GO | Constraints prevent legal config | No | No |
| TUNED | Legal config computed | No (not yet selected) | No |
| ACTIVE | In Top-K, trading | Yes | Yes |
| GRACEFUL_EXIT_ONLY | Dropped from Top-K, non-flat | No | Yes |
| COOLDOWN | Just deactivated, hold before re-entry | No | No |

---

## 6. Selection and replacement policy

### 6.1 Scoring cycle

- **Frequency:** every `SELECTOR_CYCLE_S` (default 60s)
- **Algorithm:** Top-K v1 (`range + liquidity - toxicity - trend`)
- **Optional:** ML adjustment (bounded by `ML_ADJUST_MAX_BPS`)
- **Deterministic:** same inputs → same scores → same ranking

### 6.2 Hysteresis

- `ENTER_THRESHOLD_BPS`: score must exceed N-th place by this margin to enter
- `EXIT_THRESHOLD_BPS`: score must drop below N-th place by this margin to be removed
- `MIN_HOLD_CYCLES`: symbol cannot be removed within first N cycles after activation
- **Purpose:** prevent rapid churn in active set

### 6.3 Change budget

- `MAX_CHANGES_PER_CYCLE`: maximum add + remove operations per selection cycle (default 1)
- If more changes are desirable, they are **deferred** to the next cycle

### 6.4 Replacement rules

| Condition | Action |
|-----------|--------|
| Better candidate available, current symbol flat | Replace (add new, remove current) |
| Better candidate available, current symbol non-flat | Defer removal; set `graceful_exit_only` |
| Current symbol drops below exit threshold, flat | Remove and add best available |
| Current symbol drops below exit threshold, non-flat | `graceful_exit_only`, wait for flat |
| Active set size < K and eligible candidates exist | Add without removing |
| No better candidates | No change (stability) |

### 6.5 When replacement is forbidden

- During active fill processing for the symbol
- When health mode is not HEALTHY
- When exchange connectivity is degraded
- During the first `MIN_HOLD_CYCLES` after activation

---

## 7. Onboarding and tuning contract

### 7.1 Required inputs

| Input | Source | Used for |
|-------|--------|----------|
| `tickSize` | exchangeInfo / ConstraintProvider | Price quantization |
| `stepSize` | exchangeInfo / ConstraintProvider | Quantity rounding |
| `minQty` | exchangeInfo / ConstraintProvider | Minimum order size |
| `minNotional` | exchangeInfo / ConstraintProvider | Minimum order value |
| Current price | Market data snapshot | Notional calculation |
| `MAX_POSITION_USD` | Risk config | Max total exposure |
| `MAX_INV_LEVELS` | Grid config | Inventory depth |
| `ENTRY_LEVELS` | Grid config | Visible ladder width |
| `STEP_PCT` | Grid config | Grid spacing |

### 7.2 Tuning solver output

```python
@dataclass
class TuningResult:
    symbol: str
    status: Literal["TUNED", "NO_GO"]
    order_size: Decimal | None       # Legal qty per order
    tick_size: Decimal | None        # Price tick
    step_size: Decimal | None        # Qty step
    no_go_reason: str | None         # If NO_GO: human-readable reason
    config: GridV2Config | None      # Full legal config if TUNED
```

### 7.3 NO_GO reasons

| Reason | Description |
|--------|-------------|
| `NOTIONAL_TOO_LOW` | `order_size * price < minNotional` at any viable qty |
| `POSITION_EXCEEDS_CAP` | `order_size * price * max_inv > MAX_POSITION_USD` |
| `TICK_SIZE_UNAVAILABLE` | Cannot determine tick size from constraints |
| `LEVERAGE_LIMIT` | Exchange leverage limits prevent the required position size |
| `BLACKLISTED` | Symbol is on the operator blacklist |

---

## 8. Safety invariants

**I1:** No unprotected inventory during rotation. A symbol with open lots MUST go through `GRACEFUL_EXIT_ONLY` before deactivation.

**I2:** No active-set churn beyond budget. `MAX_CHANGES_PER_CYCLE` is the hard cap.

**I3:** No new entries for symbols in `GRACEFUL_EXIT_ONLY`. Only exits and cancels.

**I4:** No symbol activation without valid tuned config. `NO_GO` symbols cannot enter `ACTIVE`.

**I5:** Deterministic selection from same inputs. Score ties broken by `(-score, symbol)`.

**I6:** Operator-auditable reason codes for every exclude, remove, add, defer, and no-go.

**I7:** Cleanup on any exit. Fatal abort triggers fail-safe cleanup (ADR-121).

**I8:** EventLedger trust boundary respected. Degraded mode falls back to snapshot (ADR-109).

---

## 9. Failure handling and degrade policy

| Failure | Response |
|---------|----------|
| Universe fetch fails | Keep previous universe; log warning |
| Prefilter exception | Skip cycle; retain previous active set |
| Selector exception | Retain previous active set (fail-safe, existing behavior) |
| Tuning solver NO_GO | Symbol stays in ELIGIBLE/NO_GO; not activated |
| Symbol metadata stale | Use cached constraints; flag warning |
| WS disconnect | Health gate degrades; EventLedger trust revoked |
| Account sync failure | Health degrades; timestamp offset refreshed on -1021 |
| Symbol invalid mid-run | `graceful_exit_only` → wait for FLAT → deactivate |
| Cleanup failure on abort | Log `TRADING_ABORT_CLEANUP_COMPLETED failures=N`; operator must verify |

---

## 10. Observability and operator audit trail

### Required signals

| Signal | When |
|--------|------|
| `SYMBOL_DISCOVERED count=N` | Universe refresh found N symbols |
| `SYMBOL_PREFILTER_BLOCKED symbol=X reason=Y` | Symbol failed hard filter |
| `SYMBOL_ELIGIBLE symbol=X` | Symbol passed prefilter |
| `SYMBOL_TUNED symbol=X order_size=N tick=T` | Legal config computed |
| `SYMBOL_NO_GO symbol=X reason=Y` | Cannot trade this symbol |
| `SYMBOL_ACTIVATED symbol=X` | Added to active set |
| `SYMBOL_DEACTIVATED symbol=X` | Removed from active set (was flat) |
| `SYMBOL_GRACEFUL_EXIT_ONLY symbol=X` | Non-flat, blocking new entries |
| `SYMBOL_ROTATION_DEFERRED symbol=X reason=Y` | Change budget exhausted or non-flat |
| `SYMBOL_COOLDOWN_ENTERED symbol=X` | Post-deactivation hold period |
| `SELECTOR_CYCLE_COMPLETED active=N eligible=M scored=L` | Selection cycle summary |

### Existing signals preserved

All existing signals from `13_OBSERVABILITY.md` remain unchanged: health mode, EventLedger trust lifecycle, fill processing, repair, cleanup.

---

## 11. Rollout phases

### Phase A: Docs + contracts (THIS PR)

- This spec document
- State machine definition
- Tuning solver contract
- No implementation changes

### Phase B: Onboarding snapshot + tuning in shadow

- Implement universe provider (read-only)
- Implement tuning solver (shadow mode — log TunedConfig / NO_GO)
- Wire into existing `run_trading.py` startup
- Operator can see which symbols would be tradeable
- **No runtime behavior change**

### Phase C: Active set rotation with operator universe

- Enable ActiveSelector with auto-tuned configs
- Operator still provides base universe via `--symbols`
- System auto-tunes configs per symbol
- Rotation via hysteresis + graceful_exit_only
- **Grid_v2 still single-symbol per run** (or one engine per symbol in multi-process)

### Phase D: Full autonomous continuous multi-symbol loop

- Universe provider auto-discovers from exchangeInfo
- No `--symbols` required (operator can still override)
- Continuous loop: discover → filter → score → activate → trade → rotate
- Per-symbol grid_v2 bridges (either multi-process or symbol-scoped)
- **Full 24/7 autonomous operation**

---

## 12. Open questions / deferred items

| Question | Status |
|----------|--------|
| Single process with per-symbol bridges vs multi-process? | Deferred to Phase C/D design |
| Should tuning solver be deterministic or adaptive? | Deterministic first, adaptive later |
| How to handle symbols that oscillate between TUNED and NO_GO? | Cooldown + hysteresis should prevent churn |
| Maximum active set size? | K=3 default, operator-configurable |
| Multi-venue (Bybit, OKX, COIN-M)? | Deferred P2 (ADR-066) |
| SOR AMEND support? | Deferred (requires order state tracking) |
| ML policy integration (signal → grid params)? | Deferred P2 |

---

## Cross-references

| Document | Relationship |
|----------|-------------|
| [36_MULTI_SYMBOL_ELIGIBILITY_ML_INTEGRATION_V1.md](36_MULTI_SYMBOL_ELIGIBILITY_ML_INTEGRATION_V1.md) | Phases 1–3b implementation detail; this doc adds orchestration layer |
| [04_PREFILTER_SPEC.md](04_PREFILTER_SPEC.md) | Hard filter + scoring spec (Step 2–3 of the loop) |
| [03_ARCHITECTURE.md](03_ARCHITECTURE.md) | System architecture, data flow, latency budget |
| [10_RISK_SPEC.md](10_RISK_SPEC.md) | Per-symbol + portfolio risk controls |
| [27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md](27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md) | Grid_v2 state machine, safety invariants |
| [14_SMART_ORDER_ROUTER_SPEC.md](14_SMART_ORDER_ROUTER_SPEC.md) | SOR decision matrix, constraint validation |
| [15_ACCOUNT_SYNC_SPEC.md](15_ACCOUNT_SYNC_SPEC.md) | Account sync, EventLedger trust boundary |
| [13_OBSERVABILITY.md](13_OBSERVABILITY.md) | Metrics, log signals, operator audit trail |
| [STATE.md](STATE.md) | Current implementation truth |
| [POST_LAUNCH_ROADMAP.md](POST_LAUNCH_ROADMAP.md) | P2 backlog items |
