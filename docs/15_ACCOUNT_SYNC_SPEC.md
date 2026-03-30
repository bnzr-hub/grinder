# 15 -- AccountSyncer Spec (Positions + Open Orders)

> **Status:** DONE (Launch-15 shipped, mainnet-ready as of PR #331 @ `d7b778f`)
> **Last updated:** 2026-03-02
> **SSOT:** This document. Referenced from `docs/POST_LAUNCH_ROADMAP.md` (Launch-15 section).
> **Prerequisite:** `docs/14_SMART_ORDER_ROUTER_SPEC.md` (SOR needs execution reality to route).
> **Variant:** 2 (positions + open orders). Variant 1 (positions-only) was rejected as insufficient for operational use.

---

## 15.1 Problem Statement

The system currently operates "blind" with respect to exchange state:

1. **No position truth:** Internal position tracking (via `Ledger`) is derived from simulated
   fills in paper mode. In live mode there is no comparison against the exchange's actual
   position. Risk gates, drawdown guard, and kill-switch decisions depend on accurate
   position data.

2. **No order truth:** `ExecutionEngine` tracks orders internally, but the exchange may have
   rejected, expired, or partially filled orders without notifying us. SOR
   (Launch-14) needs accurate `existing` order state to route correctly.

3. **No mismatch detection:** If internal state diverges from exchange state (e.g., manual
   intervention, network partition, partial fill race), there is no mechanism to detect or
   alert on the discrepancy.

**AccountSyncer** periodically fetches positions + open orders from the exchange,
produces a deterministic `AccountSnapshot`, detects mismatches, emits evidence
artifacts, and exposes metrics for alerting.

---

## 15.2 Scope

### In scope (Launch-15)

- Read-only sync: fetch positions + open orders from exchange, compare with internal state.
- Deterministic snapshot rendering: stable sort, stable Decimal formatting, sha256.
- Mismatch detection: sanity rules (duplicate keys, ts regression, negative qty, orphan orders).
- Evidence artifacts: `AccountSnapshot` -> JSON -> sha256 -> artifact file (safe-by-default, env-gated).
- Metrics: freshness, mismatch count, open orders count, sync errors.
- Feature flag: `account_sync_enabled` (default OFF), `GRINDER_ACCOUNT_SYNC_ENABLED` env var.
- Fire drill: exercise all mismatch scenarios with evidence artifacts.

### Out of scope (deferred)

- Write-path remediation (auto-cancel orphan orders, auto-close positions) -- P2.
- Balance sync (USDT margin balance) -- P2.
- Multi-venue sync -- deferred to M9.
- Full RoundTrip accounting (open->close PnL attribution) -- P2 (depends on fill tracking maturity).

---

## 15.3 Data Contracts

### 15.3.1 PositionSnap

Frozen dataclass representing a single exchange position at a point in time.

```python
@dataclass(frozen=True)
class PositionSnap:
    symbol: str              # e.g. "BTCUSDT"
    side: str                # "LONG" | "SHORT" | "BOTH" (Binance hedge mode)
    qty: Decimal             # Absolute quantity (always >= 0)
    entry_price: Decimal     # Average entry price
    mark_price: Decimal      # Current mark price (for uPnL calc)
    unrealized_pnl: Decimal  # Exchange-reported uPnL
    leverage: int            # Current leverage setting
    ts: int                  # Unix ms when fetched from exchange
```

**Serialization rules:**
- `Decimal` fields rendered via `str()` (no scientific notation, no trailing zeros beyond precision).
- `ts` as integer (Unix ms).
- `side` as uppercase string.

### 15.3.2 OpenOrderSnap

Frozen dataclass representing a single open order on the exchange.

```python
@dataclass(frozen=True)
class OpenOrderSnap:
    order_id: str            # Exchange order ID
    symbol: str              # e.g. "BTCUSDT"
    side: str                # "BUY" | "SELL"
    order_type: str          # "LIMIT" | "MARKET" | "STOP" | ...
    price: Decimal           # Limit price (0 for market orders)
    qty: Decimal             # Original quantity
    filled_qty: Decimal      # Already filled quantity
    reduce_only: bool        # Whether reduce-only flag is set
    status: str              # "NEW" | "PARTIALLY_FILLED"
    ts: int                  # Unix ms when fetched from exchange
```

### 15.3.3 AccountSnapshot

Top-level container: positions + open orders at a consistent point in time.

```python
@dataclass(frozen=True)
class AccountSnapshot:
    positions: tuple[PositionSnap, ...]     # Canonical order (see 15.4)
    open_orders: tuple[OpenOrderSnap, ...]  # Canonical order (see 15.4)
    ts: int                                  # Snapshot timestamp (max of component ts values)
    source: str                              # "exchange" | "test" | "fire_drill"
```

**Invariant:** `positions` and `open_orders` are tuples (immutable) in canonical sort order.

---

## 15.4 Canonical Ordering

Deterministic ordering is required for sha256 stability and diff correctness.

### Positions

Sorted by `(symbol, side)` -- both ascending, lexicographic.

```
BTCUSDT/LONG < BTCUSDT/SHORT < ETHUSDT/LONG
```

### Open Orders

Sorted by `(symbol, side, order_type, price, qty, order_id)` -- all ascending.

- `price` and `qty` compared as `Decimal` (numeric order).
- `order_id` as string (lexicographic) -- tiebreaker for identical price/qty.

```
BTCUSDT/BUY/LIMIT/49000/0.01/id_1 < BTCUSDT/BUY/LIMIT/49500/0.01/id_2 < BTCUSDT/SELL/LIMIT/51000/0.01/id_3
```

---

## 15.5 Invariants

These **must** hold at all times. Tests enforce them.

### I1: Deterministic serialization

```
render(snapshot_1) == render(snapshot_2)  iff  snapshot_1 == snapshot_2
```

Same inputs always produce byte-identical JSON output. Canonical sort order + stable
Decimal formatting guarantee this.

### I2: Round-trip equality

```
snapshot == load(render(snapshot))
```

Serialize to JSON, deserialize back, assert equality. This ensures no data loss or
precision drift in the serialization layer.

### I3: Sha256 stability

```
sha256(render(snapshot)) is stable across runs
```

Given identical inputs, sha256 is identical. No timestamps, random IDs, or dict
ordering instability in the render path.

### I4: Safe-by-default (read-only)

AccountSyncer **never** writes to the exchange. It only reads positions and orders.
Mismatch detection produces alerts and evidence, not remediation actions.

### I5: Monotonic timestamp guard

```
new_snapshot.ts >= last_snapshot.ts
```

Reject snapshots with timestamps older than the last accepted snapshot. This prevents
state rollback from stale API responses or clock skew.

**Exception:** Empty snapshots (`ts=0`, no positions, no open orders) are exempt from
this check. An empty account is a normal state transition (e.g., all orders filled or
expired), not a data regression. When an empty snapshot is accepted, `last_ts` resets
to 0, enabling clean re-entry when the account becomes non-empty again.

### I6: No duplicate keys

Position keys `(symbol, side)` and order keys `(order_id)` must be unique within a
snapshot. Duplicates indicate a bug in the fetch/parse layer and must be flagged as a
mismatch.

---

## 15.6 Artifact Scheme

### Evidence directory

```
${GRINDER_ARTIFACT_DIR:-.artifacts}/account_sync/<YYYYMMDDTHHMMSSZ>/
```

### Artifact files

```
account_snapshot.json     # Full AccountSnapshot (canonical JSON)
positions.json            # Positions only (canonical JSON)
open_orders.json          # Open orders only (canonical JSON)
mismatches.json           # Detected mismatches (if any)
summary.txt              # Human-readable evidence block
sha256sums.txt           # sha256sum of all artifact files
```

### Env gate

Evidence writing is gated by `GRINDER_ACCOUNT_SYNC_EVIDENCE` env var (truthy = enabled).
Follows the same pattern as `GRINDER_FSM_EVIDENCE` in `fsm_evidence.py`.

No files are written unless the env var is set. This is the safe-by-default path.

---

## 15.7 Metrics

All metrics follow existing patterns from `MetricsBuilder` / `REQUIRED_METRICS_PATTERNS`.

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grinder_account_sync_last_ts` | gauge | — | Unix ms of last exchange data update (max `updateTime` across positions/orders) — DATA FRESHNESS |
| `grinder_account_sync_last_wall_ts` | gauge | — | Unix ms wall-clock when last sync completed without error — LIVENESS |
| `grinder_account_sync_age_seconds` | gauge | — | Seconds since last successful sync completion (wall-clock liveness) |
| `grinder_account_sync_data_age_seconds` | gauge | — | Seconds since last exchange data update (data freshness) |
| `grinder_account_sync_errors_total` | counter | `reason` | Sync errors by reason (timeout, auth, parse) |
| `grinder_account_sync_mismatches_total` | counter | `rule` | Mismatches detected by rule (duplicate_key, ts_regression, negative_qty, orphan_order) |
| `grinder_account_sync_positions_count` | gauge | — | Number of positions in last snapshot |
| `grinder_account_sync_open_orders_count` | gauge | — | Number of open orders in last snapshot |
| `grinder_account_sync_pending_notional` | gauge | — | Total notional value of open orders (price * remaining_qty) |

### Mismatch rules (for `mismatches_total` counter)

| Rule | Description |
|------|-------------|
| `duplicate_key` | Two positions with same `(symbol, side)` or two orders with same `order_id` |
| `ts_regression` | Snapshot timestamp older than previously accepted snapshot |
| `negative_qty` | Position qty or order qty is negative |
| `orphan_order` | Order exists on exchange but not tracked by `ExecutionEngine` |

---

## 15.8 Port Interface Extensions

`ExchangePort` already has `fetch_open_orders(symbol)`. Launch-15 adds:

```python
class ExchangePort(Protocol):
    # ... existing methods ...

    def fetch_positions(self, symbol: str) -> list[PositionSnap]:
        """Fetch current positions for a symbol from the exchange."""
        ...

    def fetch_account_snapshot(self) -> AccountSnapshot:
        """Fetch full account snapshot (all positions + all open orders).

        This is the preferred method for sync -- single consistent read.
        """
        ...
```

`NoOpExchangePort` will return empty lists (no positions, no orders) -- safe for
replay/paper mode.

---

## 15.9 PR Breakdown

### PR0 (this PR): Spec + SSOT pointers

- **NEW:** `docs/15_ACCOUNT_SYNC_SPEC.md` (this file)
- **MOD:** `docs/STATE.md` -- Launch-15 pointer
- **MOD:** `docs/POST_LAUNCH_ROADMAP.md` -- Launch-15 status update

### PR1: Core contracts + deterministic render + tests

- **NEW:** `src/grinder/account/contracts.py` -- `PositionSnap`, `OpenOrderSnap`, `AccountSnapshot`
- **NEW:** `src/grinder/account/render.py` -- `render_snapshot()`, `load_snapshot()`, `snapshot_sha256()`
- **NEW:** `src/grinder/account/metrics.py` -- `AccountSyncMetrics` (counters + gauges)
- **MOD:** `src/grinder/observability/metrics_builder.py` -- wire account sync metrics
- **MOD:** `src/grinder/observability/live_contract.py` -- add account sync patterns to `REQUIRED_METRICS_PATTERNS`
- **NEW:** `tests/unit/test_account_contracts.py` -- round-trip, sha256 stability, canonical ordering
- **NEW:** `tests/unit/test_account_render.py` -- render/load/sha256 determinism
- **NEW:** `tests/unit/test_account_metrics.py` -- metrics contract tests

### PR2: Port wiring + syncer + mismatch detection + evidence

- **NEW:** `src/grinder/account/syncer.py` -- `AccountSyncer` (fetch + compare + detect mismatches)
- **NEW:** `src/grinder/account/evidence.py` -- evidence artifact writer (env-gated)
- **MOD:** `src/grinder/execution/port.py` -- add `fetch_positions()`, `fetch_account_snapshot()` to `ExchangePort`
- **MOD:** `src/grinder/live/config.py` -- add `account_sync_enabled: bool = False`
- **MOD:** `src/grinder/live/engine.py` -- wire syncer into live loop (gated by feature flag)
- **NEW:** `docs/runbooks/29_ACCOUNT_SYNC.md` -- enablement, running, verifying, troubleshooting
- **MOD:** `docs/runbooks/00_EVIDENCE_INDEX.md` -- add account sync row
- **MOD:** `scripts/gen_evidence_index.py` -- add `account_sync` entry
- **NEW:** `tests/unit/test_account_syncer.py` -- mismatch detection, invariant enforcement
- **NEW:** `tests/unit/test_account_evidence.py` -- evidence artifact tests

### PR3: Fire drill + evidence + runbook + ops entrypoint

- **NEW:** `scripts/fire_drill_account_sync.sh` -- 3-4 scenarios (happy path, mismatch, orphan order, ts regression)
- **MOD:** `scripts/ops_fill_triage.sh` -- add `account-sync-drill` mode
- **NEW:** `docs/runbooks/30_ACCOUNT_SYNC_FIRE_DRILL.md`
- **MOD:** `docs/runbooks/00_EVIDENCE_INDEX.md` -- add fire drill row
- **MOD:** `scripts/gen_evidence_index.py` -- add fire drill entry

### PR4 (#329): Port protocol -- add `fetch_account_snapshot()` to ExchangePort

- **MOD:** `src/grinder/execution/port.py` -- add `fetch_account_snapshot()` method to `ExchangePort` protocol
- **MOD:** `src/grinder/execution/noop_port.py` -- stub returning empty snapshot
- **MOD:** `tests/unit/test_exchange_port.py` -- protocol conformance test

### PR5 (#330): Sync interval throttle

- **MOD:** `src/grinder/live/engine.py` -- 5s minimum between sync calls via `snapshot.ts`
- **NEW:** `tests/unit/test_engine_account_sync_throttle.py` -- 6 tests (first-tick, interval, error, zero-ts, backwards-ts)
- Design: negative init (`-5000`) guarantees first tick always syncs. Uses `snapshot.ts` (deterministic, not wall-clock).

### PR6 (#331): BinanceFuturesPort.fetch_account_snapshot()

- **MOD:** `src/grinder/execution/binance_futures_port.py` -- real implementation: 2 REST calls (`/fapi/v2/positionRisk` + `/fapi/v1/openOrders`), parse to `PositionSnap`/`OpenOrderSnap`, `build_account_snapshot()`
- **MOD:** `src/grinder/execution/binance_port.py` -- `NoopHttpClient.positions_response` + routing
- **MOD:** `tests/unit/test_binance_futures_port.py` -- 6 tests (dry-run, parsing, empty, call count, ts, reduceOnly string)
- Safe `reduceOnly` parsing: Binance may send `"false"` as string -- `_parse_reduce_only()` handles both bool and string.
- `dry_run=True` returns empty snapshot with 0 HTTP calls.

---

## 15.10 Resolved Questions

1. **Sync frequency:** 5-second interval throttle (PR #330). Uses `snapshot.ts` for deterministic timing. Configurable via `_account_sync_interval_ms` (default 5000).

2. **Partial fill race:** Flagged as mismatch with severity=INFO (not ERROR). Detected by the orphan-order mismatch rule.

3. **Hedge mode:** Handled via `positionSide` field from Binance ("BOTH"/"LONG"/"SHORT"). Maps directly to `PositionSnap.side`.

4. **Batch vs per-symbol fetch:** Batch (`fetch_account_snapshot()` with no symbol filter) is the canonical path. Fetches all positions and all orders in 2 REST calls.

5. **RoundTrip accounting scope:** Deferred to P2 as planned. Launch-15 only tracks position/order truth vs exchange.

## 15.11 Operational Notes

### Liveness vs freshness (PR-339)

Two distinct concerns, two distinct metric families:

| Concern | Metric | Source | Alert |
|---------|--------|--------|-------|
| **Liveness** — is sync running? | `last_wall_ts` / `age_seconds` | Wall-clock set on every successful `record_sync()` call | `AccountSyncStale` (>120s) |
| **Data freshness** — is exchange data changing? | `last_ts` / `data_age_seconds` | Max `updateTime` from exchange positions/orders | `AccountSyncDataStale` (>300s) |

**Why the split:** When a single unchanged open order sits on Binance, `snapshot.ts` (max of order `updateTime` values) is frozen at order creation time. The old `age_seconds` (derived from `last_ts`) grew unbounded — triggering false `AccountSyncStale` alerts even though sync was running successfully every 5 seconds. Separating liveness (wall-clock) from freshness (data timestamp) eliminates this false-positive class.

### Edge case: empty account (`snapshot.ts=0`)

When the account has no positions and no open orders, `build_account_snapshot()` computes `ts = max([])` which defaults to `0`. However, `AccountSyncMetrics.record_sync()` still sets `last_wall_ts` to wall-clock ms, so liveness metrics (`age_seconds`) always reflect sync health. `last_ts` remains 0 until the first non-empty snapshot.

**ts_regression exemption:** Empty snapshots (`ts=0`, no positions, no orders) do not
trigger the I5 monotonic timestamp guard. This prevents false `ts_regression` mismatches
when transitioning from non-empty to empty (e.g., all orders filled or expired). The
syncer resets `last_ts` to 0 on empty snapshots, enabling clean re-entry.

**Bounded ts_regression tolerance (ADR-108):** Binance can return cached account snapshots
with slightly older timestamps after fill events. To prevent indefinitely starving the
reconciler, the syncer tolerates small bounded regressions after repeated occurrences:

- Regression > `TS_REGRESSION_TOLERANCE_MS` (10s): always hard-blocked as mismatch.
- Regression <= tolerance, first 3 consecutive occurrences: flagged as mismatch, `last_ts` frozen.
- Regression <= tolerance, 4th+ consecutive occurrence: **tolerated** — snapshot accepted, `last_ts` advanced to snapshot.ts, streak reset. Log: `ACCOUNT_SYNC_TS_REGRESSION_TOLERATED`.
- Normal snapshot (no regression): streak counter reset.

This ensures cached-snapshot behavior does not permanently wedge downstream consumers
while large/unsafe regressions still fail-closed.

**Alert guard:** `AccountSyncDataStale` only fires when the account has active orders or
positions (`open_orders_count > 0 or positions_count > 0`). Empty accounts have nothing
to be "stale" about; sync liveness is covered by `AccountSyncStale`.

**Semantic contract:**
- `grinder_account_sync_last_wall_ts` = wall-clock ms of last successful `record_sync()` call. Never 0 after at least one successful sync.
- `grinder_account_sync_last_ts` = max exchange `updateTime` across positions/orders (data freshness). May be 0 for empty accounts.
- `grinder_account_sync_age_seconds` = seconds since `last_wall_ts` (liveness).
- `grinder_account_sync_data_age_seconds` = seconds since `last_ts` (data freshness).

Sync liveness can also be verified via HTTP request counters:

```bash
# Verify sync is ticking (expect +2 every 5 seconds: positionRisk + openOrders)
curl -s localhost:9092/metrics | grep 'grinder_http_requests_total.*op="get_positions"'
curl -s localhost:9092/metrics | grep 'grinder_http_requests_total.*op="get_open_orders"'
```

### EventLedger trust authority boundary (ADR-109 Phase 2)

EventLedger provides a local event-derived order state from Binance user-data
stream (ORDER_TRADE_UPDATE events). In healthy mode, it serves as the trusted
read model for open-order visibility. Authority is bounded:

**Healthy mode (trusted):**
- `is_trusted = bootstrapped AND last_convergence_ok AND NOT trust_revoked`
- Engine order-visibility paths prefer ledger over snapshot
- Fills/cancels detected faster via WS→ledger than REST snapshot polling

**Degraded mode (trust revoked):**
- Any non-HEALTHY health mode transition (DEGRADED_WS, DEGRADED_SYNC,
  STALE_TRUTH, PAUSED_UNSAFE) immediately revokes trust
- All order-visibility paths fall back to snapshot
- Log signal: `EVENT_LEDGER_TRUST_REVOKED reason=health_mode=<mode>`

**Recovery:**
- Trust restored only when BOTH conditions met:
  1. Health mode returns to HEALTHY
  2. Next sync comparison converges (0 divergences)
- Log signal: `EVENT_LEDGER_TRUST_RESTORED`
- No silent re-enable without explicit convergence proof

**Snapshot role in Phase 2:**
- Bootstrap source (hydrate ledger on every sync)
- Audit/drift detection (compare_with_snapshot)
- Recovery source (authoritative when ledger is not trusted)
- Still used by reconciler for actual entry/exit computation
