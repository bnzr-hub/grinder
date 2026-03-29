# Runbook 35: Grid V2 Shadow Verification (doc-27 PR5)

Run grid_v2 in shadow mode alongside legacy engine to validate behavior
before PR6 live ceremony.

## Prerequisites

- No live risk: shadow mode does not dispatch orders.
- `GRINDER_GRID_V2_ENABLED` must be OFF (shadow is mutually exclusive with primary).

## Environment Variables

```bash
export GRINDER_GRID_V2_SHADOW=true
export GRINDER_GRID_V2_SYMBOL=BTCUSDT
# Optional: tune grid config (defaults are safe)
# export GRINDER_GRID_V2_STEP_PCT=0.0025
# export GRINDER_GRID_V2_ENTRY_LEVELS=5
# export GRINDER_GRID_V2_ORDER_SIZE=0.001
# export GRINDER_GRID_V2_MAX_INV_LEVELS=5
# export GRINDER_GRID_V2_MAX_INV_NOTIONAL=1000
```

## Procedure

### 1. Enable shadow mode

Set the env vars above in your runtime config. Start the engine normally.

### 2. Monitor logs

Shadow emits structured log lines:

```
GRID_V2_SHADOW_STARTUP_OK symbol=BTCUSDT mode=fresh
GRID_V2_SHADOW_TICK symbol=BTCUSDT tick=1 shadow=0 legacy=3 divergence=count_mismatch
GRID_V2_SHADOW_DIVERGENCE symbol=BTCUSDT detail=shadow=0 legacy=3
```

Divergence types:
- `none` — shadow and legacy agree
- `count_mismatch` — different action counts
- `type_mismatch` — same count, different action types
- `shadow_blocked` — shadow startup failed or not complete

### 3. Run smoke proof

```bash
bash scripts/smoke_grid_v2_shadow.sh
```

Artifacts saved to `.artifacts/grid_v2_shadow/<ts>/`.

### 4. Evaluate results

- Shadow divergence is **expected** in the first phase (grid_v2 and legacy use
  different strategies).
- Focus on: shadow startup succeeds, no runtime errors, no crash/hang.
- Shadow errors should appear in logs as `GRID_V2_SHADOW_ERROR` but must not
  affect live dispatch.

## Rollback

Disable shadow: `GRINDER_GRID_V2_SHADOW=false` (or unset). No other changes needed.

## Related

- Spec: `docs/27_TWO_SIDED_ROLLING_WINDOW_GRID_SPEC.md` section 24
- ADR-091 in `docs/DECISIONS.md`
- Smoke script: `scripts/smoke_grid_v2_shadow.sh`
