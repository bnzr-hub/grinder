# Runbook 38: Grid V2 Launch Readiness

## Purpose
Operator-facing readiness checklist for grid_v2 live verification runs.
Covers all subsystems from the 8-PR production program (ADR-100 through ADR-107).

## Pre-Start Ceremony

### 0. Launch Readiness Report
```bash
PYTHONPATH=src python -m scripts.launch_readiness --symbol BTCUSDT
```
Prints GO/NO-GO verdict with config, risk, safety, and subsystem checks.
Fix all FAIL items before proceeding.

### 1. Preflight Gate (ADR-101)
```bash
# Preflight runs automatically on armed mainnet start.
# Hard failure = exit code 2, run does not start.
# Checks: DNS, exchange time sync, WS bootstrap, account sync, symbol metadata, config consistency.
```

**Go:** All 6 checks PASS.
**No-Go:** Any hard FAIL.

### 2. Config Verification
```bash
# Required env vars:
GRINDER_GRID_V2_ENABLED=1
GRINDER_GRID_V2_SYMBOL=BTCUSDT
GRINDER_GRID_V2_TICK_SIZE=0.10
GRINDER_GRID_V2_RESEED_ON_FLAT=1
GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW=0
GRINDER_GRID_V2_NETOFF_ENABLED=0
GRINDER_GRID_V2_SYNC_RECONCILER_ENABLED=1
GRINDER_GRID_V2_SYNC_RECONCILER_PRIMARY=1
GRINDER_GRID_V2_SYNC_RECONCILER_SHADOW=0
GRINDER_SYMBOL_RISK_MAX_NOTIONAL_PCT=0.80
GRINDER_MAX_POSITION_USD=<safety fence>
GRINDER_RISK_SATURATION_THRESHOLD=3
```

### 3. Health Gate Modes (ADR-100)
| Mode | Write Policy | Cancel | Reduce-Only |
|------|-------------|--------|-------------|
| HEALTHY | allowed | allowed | allowed |
| DEGRADED_SYNC | allowed | allowed | allowed |
| DEGRADED_WS | allowed | allowed | allowed |
| STALE_TRUTH | **blocked** | allowed | allowed |
| PAUSED_UNSAFE | **blocked** | allowed | **blocked** |

## In-Run Watchpoints

### Key Events to Monitor
| Event | Severity | Meaning |
|-------|----------|---------|
| `GRID_V2_NO_ACTION reason=ACTUAL_MATCHES_EFFECTIVE_TARGET` | Normal | Healthy steady state |
| `GRID_V2_NO_ACTION reason=RISK_SATURATED_TARGET_ZERO` | Warning | Risk cap blocking all entries |
| `GRID_V2_NO_ACTION reason=EFFECTIVE_TARGET_PARTIAL_MATCHED` | Info | Partial capacity matched |
| `GRID_V2_ENTRY_SUPPRESSED reason=EFFECTIVE_TARGET_ZERO` | Warning | All entries suppressed by risk |
| `GRID_V2_EXIT_SUPPRESSED reason=PENDING_REPAIR_AFTER_REJECT` | Warning | -2022 repair pending |
| `GRID_V2_EXIT_SUPPRESSED reason=REDUCE_ONLY_BUDGET_EXCEEDED` | Warning | Exit over-budget blocked |
| `GRID_V2_HEALTH_BLOCK reason=STALE_TRUTH` | **Blocker** | Truth source stale, writes blocked |
| `GRID_V2_HEALTH_BLOCK reason=PAUSED_UNSAFE` | **Blocker** | Multiple sources stale |
| `GRID_V2_REDUCE_ONLY_REPAIR_START` | Info | Budget repair in progress |
| `GRID_V2_REDUCE_ONLY_REPAIR_CONVERGED` | Info | Budget repair succeeded |
| `GRID_V2_REDUCE_ONLY_REPAIR_DEFERRED` | Warning | Repair cancel failed, retrying |
| `GRID_V2_EXIT_TOPOLOGY_REPAIR_START` | Info | Exit topology repair in progress |
| `GRID_V2_EXIT_TOPOLOGY_REPAIR_CONVERGED` | Info | Topology converged |
| `GRID_V2_EXIT_TOPOLOGY_REPAIR_INCOMPLETE` | Warning | Topology repair failed |
| `GRID_V2_RISK_SATURATED_ENTER` | Warning | Symbol entered risk saturation |
| `GRID_V2_RISK_SATURATED_EXIT` | Info | Saturation cleared |

### Blocker Events (Require Immediate Attention)
- `GRID_V2_HEALTH_BLOCK reason=PAUSED_UNSAFE` — all writes blocked
- Repeated `GRID_V2_EXIT_TOPOLOGY_REPAIR_INCOMPLETE` — topology not converging
- Repeated `GRID_V2_REDUCE_ONLY_REPAIR_DEFERRED` — budget repair not converging
- Any Binance `-2022` after repair should have converged

### Acceptable Degraded Events (Watch, Don't Stop)
- Occasional `GRID_V2_HEALTH_BLOCK reason=STALE_TRUTH` followed by recovery
- `GRID_V2_NO_ACTION reason=RISK_SATURATED_TARGET_ZERO` (expected near cap)
- Single `GRID_V2_REDUCE_ONLY_REPAIR_START` → `CONVERGED` cycle

## Go / No-Go Conditions

### Go
- [ ] Preflight PASS (all 6 checks)
- [ ] No `PAUSED_UNSAFE` health mode at start
- [ ] Symbol config valid (tick size, step, levels)
- [ ] No unresolved repair latch (`_reduce_only_pending_repair` empty)
- [ ] Risk envelope configured (`GRINDER_SYMBOL_RISK_MAX_NOTIONAL_PCT`, `GRINDER_MAX_POSITION_USD`)
- [ ] Sync reconciler enabled and primary

### No-Go
- Preflight hard fail on any check
- `PAUSED_UNSAFE` at startup
- Repeated `-2022` without convergence
- Persistent `GRID_V2_EXIT_TOPOLOGY_REPAIR_INCOMPLETE`
- DNS/time drift instability (preflight clock check fails)
- Unexplained `RISK_SATURATED_ENTER` without corresponding cap pressure

## Verification Success Criteria (Canary Run)

A short canary run (e.g., 180s, 1 level, min size) succeeds if:
- [ ] No repeated `-2022` rejects
- [ ] No churn when actual == effective (zero-action steady state)
- [ ] No hidden suppressed paths without reason code
- [ ] Exit topology converges when drifted
- [ ] No unsafe writes under degraded truth
- [ ] Cleanup CLEAN at shutdown (no orphan orders)
- [ ] `GRID_V2_NO_ACTION reason=ACTUAL_MATCHES_EFFECTIVE_TARGET` appears (healthy steady state)

## Cleanup / Recovery

### Normal Shutdown
```bash
# CTRL+C or SIGTERM → engine runs cleanup sequence:
# 1. Cancels all open grid_v2 orders (entries + exits)
# 2. Logs GRID_V2_CLEANUP_COMPLETE with order counts
# 3. Exits cleanly
```

### Emergency Stop
```bash
# Option 1: Kill switch env var (blocks PLACE/REPLACE, allows CANCEL)
# See runbook 04_KILL_SWITCH.md

# Option 2: Manual cancel via operator CLI
PYTHONPATH=src python -m scripts.exchange_state --symbol BTCUSDT --cancel-all
```

### Post-Incident Verification
```bash
# 1. Check for orphan orders
PYTHONPATH=src python -m scripts.exchange_state --symbol BTCUSDT

# 2. Verify position state (expect FLAT after cleanup)
PYTHONPATH=src python -m scripts.exchange_state --symbol BTCUSDT --positions

# 3. Review unresolved events in logs:
#    grep for: GRID_V2_REDUCE_ONLY_REPAIR_DEFERRED
#              GRID_V2_EXIT_TOPOLOGY_REPAIR_INCOMPLETE
#              GRID_V2_HEALTH_BLOCK reason=PAUSED_UNSAFE

# 4. On next restart, stale repair latches clear automatically
#    (no manual flag reset needed)
```

### Cross-reference
- Kill switch ceremony: `docs/runbooks/04_KILL_SWITCH.md`
- Account sync triage: `docs/runbooks/29_ACCOUNT_SYNC.md`
- Grid V2 shadow verification: `docs/runbooks/35_GRID_V2_SHADOW_VERIFICATION.md`
