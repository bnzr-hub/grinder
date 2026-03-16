# STATE -- Current Implementation Status

Goal: document **what actually works now** (not what we wish worked). Update in every PR if something changed.

Next steps and progress tracker: `docs/ROADMAP.md`.
Post-launch roadmap (P1 hardening + P2 backlog): `docs/POST_LAUNCH_ROADMAP.md`.
**Launch readiness SSOT:** `docs/LAUNCH_PLAN.md` — definition of done, release gates, remaining work.

## Launch readiness

All 15 launch criteria (D1–D15) are **DONE**. See `docs/LAUNCH_PLAN.md` for details.

Next actions are **operational** (not code):
- C3: Canary (1 symbol, real BinanceFuturesPort) — DONE (2026-02-28, BTCUSDT futures, PR #310 evidence)
- C4: Full rollout (BTCUSDT+ETHUSDT, ACTIVE) — IN PROGRESS (accumulative 24h)

**Note on H-labels:** Throughout this document, labels like `(H1)`, `(H2)`, etc. are informal shorthand for connector hardening ADRs:
- H1 = ADR-024 (Timeouts + Clean Shutdown)
- H2 = ADR-025 (Retry Utilities)
- H3 = ADR-026 (Idempotency)
- H4 = ADR-027 (Circuit Breaker)
- H5 = ADR-028 (Observability Metrics)

These are **not** a formal checklist. For canonical status, see the ADRs in `docs/DECISIONS.md`.

## Works now
- `grinder --help` / `grinder-paper --help` / `grinder-backtest --help` -- CLI entrypoints work.
- `python3 -m scripts.run_live` starts `/healthz` and `/metrics`:
  - `/healthz`: JSON health check (status, uptime)
  - `/metrics`: Prometheus format including system metrics + gating metrics
- `python3 -m scripts.run_soak` generates synthetic soak metrics JSON.
- **Test fixtures** (`tests/fixtures/`):
  - `sample_day/`: BTC/ETH prices, orders blocked by gating (notional too high)
    - Replay digest: `1119be13d7a22e8a`
    - Paper digest (v1): `66b29a4e92192f8f`
  - `sample_day_allowed/`: Low-price assets (~$1), orders pass prefilter + gating
    - Replay digest: `808820491cefb710`
    - Paper digest (v1): `ec223bce78d7926f`
  - `sample_day_toxic/`: Low-price assets with 6% price jump triggering toxicity gate
    - Paper digest (v1): `66d57776b7be4797`
    - Tests PRICE_IMPACT_HIGH blocking (600 bps > 500 bps threshold)
  - `sample_day_multisymbol/`: 5 symbols to test Top-K prefilter selection (K=3)
    - Paper digest (v1): `7c4f4b07ec7b391f`
    - Expected Top-K selection: AAAUSDT, BBBUSDT, CCCUSDT (highest volatility)
    - DDDUSDT, EEEUSDT filtered out (lower volatility scores)
  - `sample_day_controller/`: 3 symbols to test Adaptive Controller modes
    - Paper digest (v1, controller enabled): `f3a0a321c39cc411`
    - WIDENUSDT: triggers WIDEN mode (high volatility)
    - TIGHTENUSDT: triggers TIGHTEN mode (low volatility)
    - BASEUSDT: triggers BASE mode (normal volatility)
  - `sample_day_topk_v1/`: 6 symbols to test Top-K v1 feature-based selection
    - Paper digest (v1, topk_v1 enabled): `5b73848493cf2cf9`
    - Selected: LOWUSDT (rank 1), HIGHUSDT (rank 2), MEDUSDT (rank 3)
    - Gate-excluded: THINUSDT (THIN_BOOK)
    - Not selected: TRENDUSDT, WIDEUSDT (lower scores)
  - Fixture format: SNAPSHOT events (see ADR-006 for migration from BOOK_TICKER)
  - Schema version: v1 (see ADR-008)
  - **Fixture network airgap** (PR-NETLOCK-1): When `--fixture` is passed to `run_trading.py`, all non-loopback socket connections (`connect`, `connect_ex`, `create_connection`) are blocked at Python level. Prevents accidental HTTP/WS to Binance even with `--exchange-port futures`, API keys, and `ALLOW_MAINNET_TRADE=1`. See ADR-075.
- **End-to-end replay**:
  - CLI: `grinder replay --fixture <path> [-v] [--out <path>]`
  - Script: `python3 -m scripts.run_replay --fixture <path> [-v] [--out <path>]`
  - Determinism check: `python3 -m scripts.verify_replay_determinism --fixture <path>`
  - Output format: `Replay completed. Events processed: N\nOutput digest: <16-char-hex>`
- `python3 -m scripts.secret_guard` checks repo for secret leaks.
- `python3 -m scripts.check_unicode` scans docs for dangerous Unicode (bidi, zero-width). See ADR-005.
- Docker build + healthcheck work (Dockerfile uses `urllib.request` instead of `curl`).
- Grafana provisioning: `monitoring/grafana/provisioning/` contains datasource + dashboard.
- Branch protection on `main`: all PRs require 5 green checks.
- **Domain contracts** (`src/grinder/contracts.py`): Snapshot, Position, PolicyContext, OrderIntent, Decision -- typed, frozen, JSON-serializable. See ADR-003.
- **Prefilter v0** (`src/grinder/prefilter/`):
  - **Hard gates:** rule-based gates returning ALLOW/BLOCK + reason
  - **Top-K selector v0** (`TopKSelector`): selects top K symbols from multisymbol stream
    - Scoring: volatility proxy -- sum of absolute mid-price returns in basis points
    - Tie-breakers: higher score first, then lexicographic symbol ascending (deterministic)
    - Default K=3, window_size=10 events per symbol
    - See ADR-010 for design decisions
  - **Limitations:** no adaptive scoring, no stability controls
- **GridPolicy v0** (`src/grinder/policies/grid/static.py`): StaticGridPolicy producing symmetric bilateral grids. GridPlan includes: regime, width_bps, reset_action, reason_codes. Limitations: no adaptive step, no inventory skew, no regime switching.
- **Sizing Units SSOT (ADR-018):**
  - `GridPlan.size_schedule` is ALWAYS **base asset quantity** (e.g., BTC, ETH), NOT notional (USD)
  - `notional_to_qty(notional, price, precision)` utility in `src/grinder/policies/base.py` for explicit conversion
  - Formula: `qty = notional / price`, rounded down to precision
  - Example: `notional_to_qty(Decimal("500"), Decimal("50000"))` -> `Decimal("0.01")` (0.01 BTC)
  - All code interpreting `size_schedule` MUST treat values as base asset quantity
- **Execution stub v0** (`src/grinder/execution/`): ExchangePort protocol + NoOpExchangePort stub, ExecutionEngine with reconcile logic (PAUSE/EMERGENCY -> cancel all, HARD reset -> rebuild grid, SOFT reset -> replace non-conforming, NONE -> reconcile). Deterministic order ID generation. ExecutionMetrics for observability. Limitations: no live exchange writes, no rate limiting, no error recovery.
- **Replay engine v0** (`src/grinder/replay/`):
  - **Responsibilities:** Load fixture -> parse SNAPSHOT events -> apply prefilter gates -> evaluate policy -> execute via ExecutionEngine -> compute deterministic digest
  - **Components:** `ReplayEngine` (orchestrator), `ReplayOutput` (per-tick output), `ReplayResult` (full run result)
  - **Pipeline:** `Snapshot` -> `hard_filter()` -> `StaticGridPolicy.evaluate()` -> `ExecutionEngine.evaluate()` -> `ReplayOutput`
  - **Digest:** SHA256 of JSON-serialized outputs, truncated to 16 hex chars
  - **Expected digest:** `453ebd0f655e4920` for `tests/fixtures/sample_day`
  - **Limitations:** single policy (StaticGridPolicy), no custom feature injection, volume/OI assumed sufficient for replay
- **Gating v0** (`src/grinder/gating/`):
  - `RateLimiter`: sliding window rate limit (max orders/minute) + cooldown between orders
  - `RiskGate`: per-symbol and total notional limits + daily loss limit
  - `ToxicityGate`: detects adverse market conditions (spread spike, price impact)
    - `SPREAD_SPIKE`: blocks when spread_bps exceeds threshold (default 50 bps)
    - `PRICE_IMPACT_HIGH`: blocks when price moves too fast (default 500 bps over 5s)
    - Per-symbol price history tracking
  - `GatingResult`: standardized result type with allowed/blocked + reason + details
  - `GateName`: stable enum for gate identifiers (`rate_limiter`, `risk_gate`, `toxicity_gate`)
  - `GateReason`: stable enum for block reasons (metric labels)
  - **Metrics** (`GatingMetrics`):
    - `grinder_gating_allowed_total{gate=...}`: counter of allowed decisions
    - `grinder_gating_blocked_total{gate=...,reason=...}`: counter of blocked decisions
    - Export: `to_prometheus_lines()` for /metrics endpoint
  - **Contract tests**: `tests/unit/test_gating_contracts.py` fails if reason codes or metric labels change
  - **Limitations:** no circuit breakers, no position-level checks, PnL tracking is simulated
- **Paper trading v1** (`src/grinder/paper/`):
  - CLI: `grinder paper --fixture <path> [-v] [--out <path>]`
  - **Pipeline:** `Snapshot` -> `Top-K filter` -> `hard_filter()` -> `gating check (toxicity -> rate limit -> risk)` -> `StaticGridPolicy.evaluate()` -> `ExecutionEngine.evaluate()` -> `simulate_fills()` -> `Ledger.apply_fills()` -> `PaperOutput`
  - **Top-K prefilter:** Two-pass processing -- first scan for volatility scores, then filter to top K symbols
  - **Gating gates:** toxicity (spread spike, price impact) + rate limit (orders/minute, cooldown) + risk limits (notional, daily loss)
  - **Fill simulation v1.1 (crossing/touch model):** BUY fills if `mid_price <= limit_price`, SELL fills if `mid_price >= limit_price` (deterministic, see ADR-016)
  - **Tick-delay fills v0.1 (LC-03):**
    - Orders stay OPEN for N ticks before fill-eligible (configurable via `fill_after_ticks`)
    - `fill_after_ticks=0` (default): instant/crossing behavior (backward compatible)
    - `fill_after_ticks=1+`: fill on tick N after placement (if price crosses)
    - Order lifecycle: `PLACE -> OPEN -> (N ticks) -> FILLED`
    - Cancel before fill prevents filling
    - Deterministic: same inputs -> same fills (no randomness)
    - Added `placed_tick` to `OrderRecord` for tracking
    - Uses per-symbol `tick_counter` in `ExecutionState`
    - 18 unit tests in `tests/unit/test_paper_realism.py`
    - See ADR-034
  - **CycleEngine v1** (`src/grinder/paper/cycle_engine.py`):
    - Converts fills to TP + replenishment intents (Sec 17.12.2)
    - BUY fill -> SELL TP at `p_fill * (1 + step_pct)` for same qty
    - SELL fill -> BUY TP at `p_fill * (1 - step_pct)` for same qty
    - Replenishment: same-side order further out (only if `adds_allowed=True`)
    - Deterministic intent IDs: `cycle_{type}_{fill_id}_{side}_{price}`
    - Opt-in: `cycle_enabled=False` default (backward compat)
    - Intents NOT included in digest (backward compat)
    - See ADR-017
  - **Feature Engine v1** (`src/grinder/features/`):
    - Deterministic mid-bar OHLC construction from snapshot stream
    - **Bar building:** floor-aligned boundaries, no synthesized bars for gaps
    - **ATR/NATR (Sec 17.5.2):** True Range + period-based averaging (default 14)
    - **L1 features (Sec 17.5.3):** imbalance_l1_bps, thin_l1, spread_bps
    - **Range/trend (Sec 17.5.5):** sum_abs_returns_bps, net_return_bps, range_score
    - **Warmup handling:** features return 0/None until period+1 bars complete
    - **Determinism:** all calcs use Decimal, outputs as integer bps or Decimal
    - **Unit tests:** 83 tests (test_bar_builder.py, test_indicators.py, test_feature_engine.py)
    - **PaperEngine integration (ASM-P1-02):**
      - `feature_engine_enabled=False` default (backward compat)
      - `PaperOutput.features: dict | None` -- computed features per snapshot
      - Features NOT in digest (backward compat, Variant A)
      - Canonical digests unchanged when features enabled
      - 9 integration tests in `test_paper.py::TestFeatureEngineIntegration`
    - See ADR-019
  - **Position tracking:** Per-symbol qty + avg_entry_price via `Ledger` class
  - **PnL tracking:** Realized (on close), Unrealized (mark-to-market), Total
  - **Output schema v1:** `PaperResult` includes `schema_version`, `total_fills`, `final_positions`, `total_realized_pnl`, `total_unrealized_pnl`, `topk_selected_symbols`, `topk_k`, `topk_scores`
  - **Contract tests:** `tests/unit/test_paper_contracts.py` (27 tests) verify schema stability
  - Output format: `Paper trading completed. Events processed: N\nOutput digest: <16-char-hex>`
  - Deterministic digest for fixture-based runs
  - **Canonical digests (v1.2):** `sample_day` = `66b29a4e92192f8f`, `sample_day_allowed` = `887812017a697c8a`, `sample_day_toxic` = `97c8b0d37cae3ce3`, `sample_day_multisymbol` = `b605159aac9c0aac`
  - **Limitations:** no live feed, no real orders, no slippage, no partial fills
- **Adaptive Controller v0** (`src/grinder/controller/`):
  - Rule-based controller that adjusts policy parameters based on recent market conditions
  - **Controller modes:**
    - `BASE` -- Normal operation, no adjustment (spacing_multiplier = 1.0)
    - `WIDEN` -- High volatility (> 300 bps), widen grid (spacing_multiplier = 1.5)
    - `TIGHTEN` -- Low volatility (< 50 bps), tighten grid (spacing_multiplier = 0.8)
    - `PAUSE` -- Wide spread (> 50 bps), no new orders
  - **Priority order:** PAUSE > WIDEN > TIGHTEN > BASE
  - **Window-based metrics:** vol_bps (sum of abs mid returns), spread_bps_max (max spread in window)
  - **Determinism:** All metrics use integer basis points (no floats)
  - **Opt-in:** Disabled by default (`controller_enabled=False`) to preserve backward compatibility
  - **Integration:** Runs after Top-K selection, before policy evaluation
  - **Test fixture:** `sample_day_controller` with 3 symbols triggering WIDEN/TIGHTEN/BASE modes
    - Paper digest (v1, controller enabled): `f3a0a321c39cc411`
  - **Contract tests:** `tests/unit/test_controller.py` (20 tests), `tests/unit/test_backtest.py::TestControllerContract` (6 tests)
  - See ADR-011 for design decisions
  - **Limitations:** no EMA-based adaptive step, no trend detection, no DRAWDOWN mode
- **Backtest protocol v1** (`scripts/run_backtest.py`):
  - CLI: `python3 -m scripts.run_backtest [--out <path>] [--quiet]`
  - Runs paper trading on registered fixtures and generates JSON report
  - **Registered fixtures:** `sample_day`, `sample_day_allowed`, `sample_day_toxic`, `sample_day_multisymbol`
  - **Report schema v1:** `report_schema_version`, `paper_schema_version`, `fixtures_run`, `fixtures_passed`, `fixtures_failed`, `all_digests_match`, `results`, `report_digest`
  - **Per-fixture result:** `fixture_path`, `schema_version`, `paper_digest`, `expected_paper_digest`, `digest_match`, `total_fills`, `final_positions`, `total_realized_pnl`, `total_unrealized_pnl`, `events_processed`, `orders_placed`, `orders_blocked`, `errors`, `topk_selected_symbols`, `topk_k`
  - **Top-K output:** Each fixture result includes symbols selected by Top-K prefilter and the K value used (see ADR-010)
  - **Digest validation:** Compares paper_digest against expected_paper_digest in fixture config.json
  - **Exit code:** 0 if all fixtures pass, 1 if any fail or digest mismatch
  - **Contract tests:** `tests/unit/test_backtest.py` verifies schema stability, determinism, and Top-K fields
  - **Limitations:** no custom fixture list (hardcoded), no parallel execution
- **Observability v0** (`src/grinder/observability/`):
  - `MetricsBuilder`: consolidates all metrics into Prometheus format
  - `build_metrics_output()`: convenience function for /metrics endpoint
  - **Exported via `/metrics`**: system metrics (grinder_up, grinder_uptime_seconds) + gating metrics
  - **Contract tests**: `tests/unit/test_observability.py` verifies metric names and labels
  - **Live runtime contract** (`src/grinder/observability/live_contract.py`):
    - Pure functions for testable HTTP responses (no network required)
    - `build_healthz_body()`: returns JSON with `status`, `uptime_s`
    - `build_readyz_body()`: returns tuple of (JSON body, is_ready bool) based on HA role
    - `build_metrics_body()`: returns Prometheus format metrics
    - `REQUIRED_HEALTHZ_KEYS`: stable keys that must appear in /healthz response
    - `REQUIRED_READYZ_KEYS`: stable keys that must appear in /readyz response (`ready`, `role`)
    - `REQUIRED_METRICS_PATTERNS`: stable patterns that must appear in /metrics response
    - **Contract:**
      - `GET /healthz`: status 200, content-type `application/json`, body includes `{"status": "ok", "uptime_s": <float>}`
      - `GET /readyz`: status 200 if ACTIVE, 503 if STANDBY/UNKNOWN, content-type `application/json`, body includes `{"ready": true/false, "role": "<role>"}`
      - `GET /metrics`: status 200, content-type `text/plain`, body includes `grinder_up 1`, `grinder_uptime_seconds`, `grinder_ha_role`, gating metrics
    - **Contract tests**: `tests/unit/test_live_contracts.py` verifies response structure and required patterns
- **Observability stack v0** (`docker-compose.observability.yml`):
  - Docker Compose stack: grinder + Prometheus + Grafana
  - **Commands:**
    - Start: `docker compose -f docker-compose.observability.yml up --build -d`
    - Status: `docker compose -f docker-compose.observability.yml ps`
    - Stop: `docker compose -f docker-compose.observability.yml down -v`
  - **Ports:** grinder:9090, Prometheus:9091, Grafana:3000
  - **Grafana:** http://localhost:3000 (admin/admin), anonymous read access enabled
  - **Dashboard:** GRINDER Overview (auto-provisioned) with status, uptime, gating, kill-switch metrics
  - **Alert rules:** GrinderDown, GrinderTargetDown, HighGatingBlocks, ToxicityTriggers, KillSwitchTripped
  - **Smoke test:** `bash scripts/docker_smoke_observability.sh` validates full stack health
  - **CI:** `docker_smoke.yml` runs smoke test on PRs touching Dockerfile/compose/monitoring/src/scripts
  - See `docs/OBSERVABILITY_STACK.md` for full documentation
- **Metrics Contract v1** (exported via `/metrics`):
  - **System metrics:**
    - `grinder_up` (gauge): 1 if running, 0 if down
    - `grinder_uptime_seconds` (gauge): uptime in seconds since start
  - **Gating metrics:**
    - `grinder_gating_allowed_total{gate}` (counter): allowed decisions by gate name
    - `grinder_gating_blocked_total{gate,reason}` (counter): blocked decisions by gate and reason
    - Gate names: `rate_limiter`, `risk_gate`, `toxicity_gate`
    - Block reasons: `RATE_LIMIT_EXCEEDED`, `COOLDOWN_ACTIVE`, `NOTIONAL_LIMIT`, `DAILY_LOSS_LIMIT`, `SPREAD_SPIKE`, `PRICE_IMPACT_HIGH`, `KILL_SWITCH_ACTIVE`
  - **Risk metrics:**
    - `grinder_kill_switch_triggered` (gauge): 1 if kill-switch is active, 0 otherwise
    - `grinder_kill_switch_trips_total{reason}` (counter): total trips by reason
    - `grinder_drawdown_pct` (gauge): current drawdown percentage (0-100)
    - `grinder_high_water_mark` (gauge): current equity high-water mark
  - **HA metrics:**
    - `grinder_ha_role{role}` (gauge): 1 for current role (active/standby/unknown)
    - `grinder_ha_is_leader` (gauge): 1 if this instance is leader (LC-20), 0 otherwise
  - **Connector metrics (H5 Observability):**
    - `grinder_connector_retries_total{op, reason}` (counter): retry events by operation and reason
    - `grinder_idempotency_hits_total{op}` (counter): idempotency cache hits
    - `grinder_idempotency_conflicts_total{op}` (counter): idempotency conflicts (INFLIGHT duplicates)
    - `grinder_idempotency_misses_total{op}` (counter): idempotency cache misses
    - `grinder_circuit_state{op, state}` (gauge): circuit breaker state (1 for current, 0 for others)
    - `grinder_circuit_rejected_total{op}` (counter): calls rejected by OPEN circuit
    - `grinder_circuit_trips_total{op, reason}` (counter): circuit trips (CLOSED -> OPEN)
    - Labels: `op` = operation name, `reason` = transient/timeout/other/threshold, `state` = closed/open/half_open
    - See ADR-028 for design decisions
  - **Contract tests:** `tests/unit/test_live_contracts.py`, `tests/unit/test_observability.py`, `tests/unit/test_connector_metrics.py`
  - **SSOT:** This section is the canonical list of exported metrics
- **Determinism Gate v1** (`scripts/verify_determinism_suite.py`):
  - CI gate that catches silent drift across all fixtures and backtest
  - **Checks performed:**
    - For each fixture: run replay twice, assert identical digest
    - For each fixture: run paper twice, assert identical digest
    - For each fixture: assert digests match expected values in `config.json`
    - Run backtest twice, assert identical `report_digest`
  - **CLI:** `python3 -m scripts.verify_determinism_suite [-v] [-q]`
  - **Exit codes:** 0 if all pass, 1 on any mismatch/drift
  - **CI:** `determinism_suite.yml` runs on PRs touching `src/**`, `scripts/**`, `tests/**`, `docs/DECISIONS.md`, `docs/STATE.md`
  - **Fixture discovery:** auto-discovers fixtures with `config.json` under `tests/fixtures/`
  - **Output:** per-fixture summary table + final PASS/FAIL verdict
- **Live HTTP Integration Tests** (`tests/integration/test_live_http.py`):
  - End-to-end tests that spawn `scripts/run_live.py` and validate HTTP contracts
  - **Endpoints tested:**
    - `GET /healthz` - JSON with required keys, `status="ok"`, `uptime_s >= 0`
    - `GET /metrics` - Prometheus text format with all `REQUIRED_METRICS_PATTERNS`
  - **Run:** `PYTHONPATH=src pytest tests/integration -v`
  - **Marker:** `@pytest.mark.integration` (run with `-m integration` to filter)
  - **Dependencies:** Uses `REQUIRED_HEALTHZ_KEYS` and `REQUIRED_METRICS_PATTERNS` from `live_contract.py`
  - **Included in CI:** runs as part of standard `pytest` invocation
- **DataConnector v1** (`src/grinder/connectors/data_connector.py`):
  - Abstract base class (ABC) defining narrow contract: `connect()`, `close()`, `iter_snapshots()`, `reconnect()`
  - **ConnectorState:** DISCONNECTED -> CONNECTING -> CONNECTED -> RECONNECTING -> CLOSED
  - **RetryConfig:** Exponential backoff with cap (`base_delay_ms`, `backoff_multiplier`, `max_delay_ms`)
  - **TimeoutConfig (extended):**
    - `connect_timeout_ms` (5000ms default): timeout for initial connection
    - `read_timeout_ms` (10000ms default): timeout for reading next snapshot
    - `write_timeout_ms` (5000ms default): timeout for write operations
    - `close_timeout_ms` (5000ms default): timeout for graceful shutdown
  - **Error hierarchy** (`src/grinder/connectors/errors.py`):
    - `ConnectorError` -- base exception for all connector errors
    - `ConnectorTimeoutError(op, timeout_ms)` -- timeout during connect/read/write/close
    - `ConnectorClosedError(op)` -- operation attempted on closed connector
    - `ConnectorIOError` -- base for I/O errors
    - `ConnectorTransientError` -- retryable errors (network, 5xx, 429)
    - `ConnectorNonRetryableError` -- non-retryable errors (4xx, auth, validation)
  - **Retry utilities (H2)** (`src/grinder/connectors/retries.py`):
    - `RetryPolicy` -- frozen dataclass: `max_attempts`, `base_delay_ms`, `max_delay_ms`, `backoff_multiplier`, `retry_on_timeout`
    - `RetryStats` -- tracks `attempts`, `retries`, `total_delay_ms`, `last_error`, `errors`
    - `is_retryable(error, policy)` -- classifies errors as retryable/non-retryable
    - `retry_with_policy(op_name, operation, policy, sleep_func, on_retry)` -- async retry wrapper with exponential backoff
    - `sleep_func` parameter enables bounded-time testing (no real sleeps)
  - **Idempotency utilities (H3)** (`src/grinder/connectors/idempotency.py`):
    - `IdempotencyStatus` -- enum: `INFLIGHT`, `DONE`, `FAILED`
    - `IdempotencyEntry` -- dataclass: key, status, op_name, fingerprint, timestamps, result
    - `IdempotencyStore` -- protocol for pluggable storage
    - `InMemoryIdempotencyStore` -- thread-safe in-memory implementation with injectable clock
    - `compute_idempotency_key(scope, op, **params)` -- deterministic key from canonical payload (ts excluded from key)
    - `IdempotencyConflictError` -- fast-fail on INFLIGHT duplicates
  - **IdempotentExchangePort (H3)** (`src/grinder/execution/idempotent_port.py`):
    - Wraps `ExchangePort` with idempotency guarantees for place/cancel/replace
    - Same request with same key returns cached result (DONE)
    - Concurrent duplicates fail fast with `IdempotencyConflictError` (INFLIGHT)
    - FAILED entries allow retry (overwritable)
    - Integrates with H2 retries: key created once, all retries use same key -> 1 side-effect
    - Stats tracking: `place_calls`, `place_cached`, `place_executed`, `place_conflicts`
  - **Circuit Breaker (H4)** (`src/grinder/connectors/circuit_breaker.py`):
    - `CircuitState` -- enum: `CLOSED`, `OPEN`, `HALF_OPEN`
    - `CircuitBreakerConfig` -- failure_threshold, open_interval_s, half_open_probe_count, success_threshold, trip_on
    - `CircuitBreaker` -- per-operation circuit breaker with injectable clock
    - `before_call(op)` / `allow(op)` -- fast-fail when OPEN, limited probes in HALF_OPEN
    - `record_success(op)` / `record_failure(op, reason)` -- state transitions
    - `CircuitOpenError` -- non-retryable error raised when circuit is OPEN
    - `default_trip_on` -- trips on `ConnectorTransientError`, `ConnectorTimeoutError`
    - Per-op isolation: place can be OPEN while cancel stays CLOSED
    - **Status: Wired into IdempotentExchangePort** (H4-02)
    - Integration order: breaker.before_call -> idempotency -> execute -> record_success/failure
  - **Timeout utilities** (`src/grinder/connectors/timeouts.py`):
    - `wait_for_with_op(coro, timeout_ms, op)` -- wraps `asyncio.wait_for` with `ConnectorTimeoutError`
    - `cancel_tasks_with_timeout(tasks, timeout_ms)` -- clean task cancellation
    - `create_named_task(coro, name, tasks_set)` -- tracked task creation
  - **Idempotency:** `last_seen_ts` property for duplicate detection
  - See ADR-012 (design), ADR-024 (H1 hardening), ADR-025 (H2 retries), ADR-026 (H3 idempotency), ADR-027 (H4 circuit breaker)
- **BinanceWsMockConnector v1** (`src/grinder/connectors/binance_ws_mock.py`):
  - Mock connector that reads from fixture files (events.jsonl) and emits `Snapshot`
  - **Features:**
    - Fixture loading from `events.jsonl` or `events.json`
    - Configurable read delay for simulating real-time
    - Symbol filtering (`symbols` parameter)
    - Idempotency via timestamp tracking (skips duplicate/old timestamps)
    - Reconnect with position preservation (`last_seen_ts`)
    - Statistics tracking (`MockConnectorStats`)
  - **Timeout enforcement (H1):**
    - Connect timeout via `wait_for_with_op()` -- raises `ConnectorTimeoutError` on timeout
    - Read timeout during iteration -- raises `ConnectorTimeoutError` if read_delay exceeds timeout
    - Stats track `timeouts` count and `errors` list
  - **Clean shutdown (H1):**
    - Task tracking via `self._tasks` set with named prefix
    - `close()` cancels all tasks via `cancel_tasks_with_timeout()`
    - Waits for completion within `close_timeout_ms`
    - Clears events, cursor; sets state to CLOSED
    - Stats track `tasks_cancelled` and `tasks_force_killed`
    - **No zombie guarantee:** all tasks awaited or force-killed
  - **Usage:**
    ```python
    connector = BinanceWsMockConnector(Path("tests/fixtures/sample_day"))
    await connector.connect()
    async for snapshot in connector.iter_snapshots():
        print(f"Got {snapshot.symbol} @ {snapshot.mid_price}")
    await connector.close()
    ```
  - **Transient failure simulation (H2):**
    - `TransientFailureConfig(connect_failures, read_failures, failure_message)` for testing retry logic
    - `connect_failures`: N connect attempts fail with `ConnectorTransientError` before success
    - `read_failures`: N read operations fail before success
    - Stats track `transient_failures_injected` count
  - **Unit tests:** `tests/unit/test_data_connector.py` (47 tests), `tests/unit/test_retries.py` (35 tests)
  - **Integration tests:** `tests/integration/test_connector_integration.py` (8 tests)
  - **Limitations:** no live WebSocket
  - See ADR-024 (H1 hardening), ADR-025 (H2 retries)
- **LiveConnectorV0** (`src/grinder/connectors/live_connector.py`):
  - Live WebSocket connector with SafeMode enforcement (read_only/paper/live_trade)
  - **Features:**
    - `SafeMode` enum: `READ_ONLY` (default), `PAPER`, `LIVE_TRADE`
    - Default URL: Binance testnet (safe by design)
    - `assert_mode(required_mode)` -- raises `ConnectorNonRetryableError` if insufficient (non-retryable by design)
    - Extends `DataConnector` ABC: `connect()`, `close()`, `stream_ticks()`, `subscribe()`, `reconnect()`
    - H2/H4/H5 hardening: retries, circuit breaker, metrics integration
  - **Configuration** (`LiveConnectorConfig`):
    - `mode`: SafeMode (default: READ_ONLY)
    - `symbols`: List of symbols to subscribe
    - `ws_url`: WebSocket URL (default: testnet)
    - `timeout_config`, `retry_policy`, `circuit_breaker_config`
  - **Bounded-time testing:**
    - Injectable `clock` and `sleep_func` parameters
    - Tests complete in milliseconds with FakeClock/FakeSleep
  - **V1 scope (LC-21: real WebSocket):**
    - `stream_ticks()` wired to `BinanceWsConnector` for L1 bookTicker data
    - Yields `Snapshot` objects with bid/ask/ts from real WebSocket frames
    - Idempotency via `last_seen_ts` check (no duplicate yields)
    - Contract verified, hardening wired, tests pass
    - **Metrics:** `grinder_ws_connected`, `grinder_ws_reconnect_total`, `grinder_ticks_received_total`, `grinder_last_tick_ts`
    - **Testing:** `FakeWsTransport` injectable for bounded-time tests (delay_ms=2 for unique timestamps)
  - **Paper write-path (PAPER mode only):**
    - `place_order(symbol, side, price, quantity)` -> `OrderResult` (instant fill v0)
    - `cancel_order(order_id)` -> `OrderResult` (error if filled)
    - `replace_order(order_id, new_price, new_quantity)` -> `OrderResult` (cancel+new)
    - Deterministic order IDs: `PAPER_{seq:08d}`
    - No network calls -- pure in-memory simulation via `PaperExecutionAdapter`
  - **LIVE_TRADE write-path (LC-22):**
    - `place_order(symbol, side, price, quantity)` -> delegates to `BinanceFuturesPort`
    - `cancel_order(order_id)` -> delegates to `BinanceFuturesPort`
    - `replace_order(order_id, new_price, new_quantity)` -> delegates to `BinanceFuturesPort`
    - **3 safety gates + required futures_port:** ALL must pass for real trades:
      1. `armed=True` (explicit arming in config)
      2. `mode=LIVE_TRADE` (explicit mode)
      3. `ALLOW_MAINNET_TRADE=1` env var (external safeguard)
      - Plus: `futures_port` must be configured (required dependency, not a safety gate)
    - Any check failure -> `ConnectorNonRetryableError` with actionable message
    - See ADR-056 for design decisions, runbook 17 for enablement procedure
  - **Unit tests:** `tests/unit/test_live_connector.py` (43 tests)
  - **Integration tests:** `tests/integration/test_live_connector_integration.py` (6 tests)
  - See ADR-029 (live connector v0), ADR-030 (paper write-path v0)
- **PaperExecutionAdapter** (`src/grinder/connectors/paper_execution.py`):
  - In-memory order execution backend for PAPER mode
  - **Features:**
    - Deterministic order ID generation (`{prefix}_{seq:08d}`)
    - V0 semantics: instant fill on place, cancel+new on replace
    - Injectable `clock` for deterministic timestamps
    - No persistence, no network calls
  - **Types:**
    - `OrderRequest`: Place/replace input (frozen dataclass)
    - `OrderResult`: Operation result snapshot (frozen dataclass)
    - `PaperOrder`: Mutable internal order record
    - `OrderType`: LIMIT, MARKET
    - `PaperOrderError`: Non-retryable error for order failures
  - **How to verify paper mode:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_paper_execution.py tests/integration/test_paper_write_path.py -v
    ```
  - **Unit tests:** `tests/unit/test_paper_execution.py` (21 tests)
  - **Integration tests:** `tests/integration/test_paper_write_path.py` (17 tests)
  - See ADR-030 for design decisions
- **BinanceExchangePort v0.2** (`src/grinder/execution/binance_port.py`):
  - Live exchange port implementing ExchangePort protocol for Binance Spot (LC-04, LC-08b)
  - **Safety by design:**
    - `SafeMode.READ_ONLY` (default): blocks ALL write operations -> 0 risk
    - `SafeMode.LIVE_TRADE` required for real API calls (explicit opt-in)
    - Default URL: `https://testnet.binance.vision` (testnet)
  - **Mainnet guards (ADR-039, LC-08b):**
    - `allow_mainnet=False` by default (must explicitly opt-in)
    - `ALLOW_MAINNET_TRADE=1` env var REQUIRED for mainnet
    - `symbol_whitelist` REQUIRED for mainnet (non-empty)
    - `max_notional_per_order` REQUIRED for mainnet
    - `max_orders_per_run=1` default (single order per run)
    - `max_open_orders=1` default (single concurrent order)
  - **Injectable HTTP client:**
    - `HttpClient` protocol for HTTP operations
    - `NoopHttpClient` for mock transport testing (records calls but no real HTTP)
    - `dry_run=True` config: returns synthetic results WITHOUT calling http_client (0 calls)
    - Enables deterministic testing without external dependencies
  - **Symbol whitelist:**
    - `symbol_whitelist` config parameter
    - Blocks trades for unlisted symbols (empty = all allowed for testnet, REQUIRED for mainnet)
  - **Error mapping:**
    - 5xx -> `ConnectorTransientError` (retryable)
    - 429 -> `ConnectorTransientError` (rate limit)
    - 418 -> `ConnectorNonRetryableError` (IP ban)
    - 4xx -> `ConnectorNonRetryableError` (client error)
  - **H2/H3/H4 integration:**
    - Wrap with `IdempotentExchangePort` for idempotency + circuit breaker
    - Replace = cancel + place with shared idempotency key (safe under retries)
  - **Operations:**
    - `place_order()`: POST /api/v3/order (validates notional + order count)
    - `cancel_order()`: DELETE /api/v3/order
    - `replace_order()`: cancel + place
    - `fetch_open_orders()`: GET /api/v3/openOrders
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_binance_port.py -v
    ```
  - **Unit tests:** `tests/unit/test_binance_port.py` (42 tests)
    - Dry-run tests prove NoopHttpClient makes 0 HTTP calls
    - SafeMode tests prove READ_ONLY blocks writes
    - Mainnet guard tests prove env var, whitelist, notional limits enforced
    - Error mapping tests prove correct classification
    - Idempotency integration tests prove caching works
  - **Limitations (v0.2):**
    - HTTP REST only (no WebSocket streaming)
    - Spot only (no futures/margin)
    - Real AiohttpClient not implemented (only protocol)
  - See ADR-035, ADR-039 for design decisions
- **BinanceFuturesPort v0.1** (`src/grinder/execution/binance_futures_port.py`):
  - Live exchange port implementing ExchangePort protocol for Binance Futures USDT-M (LC-08b-F)
  - **Target execution venue:** `fapi.binance.com` (Futures USDT-M mainnet)
  - **Safety by design:**
    - `SafeMode.READ_ONLY` (default): blocks ALL write operations -> 0 risk
    - `SafeMode.LIVE_TRADE` required for real API calls (explicit opt-in)
    - Default URL: `https://testnet.binancefuture.com` (testnet)
  - **Mainnet guards (ADR-040, LC-08b-F):**
    - `allow_mainnet=False` by default (must explicitly opt-in)
    - `ALLOW_MAINNET_TRADE=1` env var REQUIRED for mainnet
    - `symbol_whitelist` REQUIRED for mainnet (non-empty)
    - `max_notional_per_order` REQUIRED for mainnet
    - `max_orders_per_run=1` default (single order per run)
    - `target_leverage=3` default (reduces margin req; safe: far-from-market, cancelled)
  - **Futures-specific features:**
    - `set_leverage()`: Set leverage for symbol (1-125x)
    - `get_leverage()`: Get current leverage
    - `get_position_mode()`: Check hedge vs one-way mode
    - `get_positions()`: Get open positions
    - `close_position()`: Close position with reduceOnly market order
    - `place_market_order()`: Market order for position cleanup
  - **Operations:**
    - `place_order()`: POST /fapi/v1/order (with reduceOnly support)
    - `cancel_order()`: DELETE /fapi/v1/order
    - `cancel_order_by_binance_id()`: Cancel by numeric order ID
    - `cancel_all_orders()`: DELETE /fapi/v1/allOpenOrders
    - `fetch_open_orders()`: GET /fapi/v1/openOrders
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_binance_futures_port.py -v
    ```
  - **Unit tests:** `tests/unit/test_binance_futures_port.py` (30 tests)
    - Dry-run tests prove 0 HTTP calls
    - SafeMode tests prove READ_ONLY blocks writes
    - Mainnet guard tests prove all guards enforced
    - Leverage validation tests
  - See ADR-040 for design decisions
- **LiveEngineV0** (`src/grinder/live/engine.py`):
  - Live write-path wiring from PaperEngine to ExchangePort (LC-05)
  - **Arming model (two-layer safety):**
    - `armed=False` (default): blocks ALL writes before reaching port
    - `mode=SafeMode.LIVE_TRADE` required at port level
    - Both required for actual writes: `armed=True AND mode=LIVE_TRADE`
  - **Intent classification:**
    - PLACE/REPLACE -> INCREASE_RISK (blocked in DRAWDOWN)
    - CANCEL -> CANCEL (always allowed)
  - **Safety gate ordering:**
    1. Arming check
    2. Mode check
    3. Kill-switch check (blocks INCREASE_RISK, allows CANCEL)
    4. Symbol whitelist check
    5. DrawdownGuardV1.allow(intent)
    6. Execute via exchange_port
  - **Hardening chain:**
    - H3: IdempotentExchangePort for idempotency
    - H4: CircuitBreaker for fast-fail
    - H2: RetryPolicy for transient errors
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_live_engine.py -v
    ```
  - **Unit tests:** `tests/unit/test_live_engine.py` (16 tests)
    - Safety/arming tests prove armed=False blocks writes
    - Drawdown tests prove INCREASE_RISK blocked in DRAWDOWN
    - Idempotency tests prove duplicate->cached
    - Circuit breaker tests prove OPEN->reject
  - See ADR-036 for design decisions
- **LiveFeed v0** (`src/grinder/live/feed.py`):
  - Live read-path pipeline: WS -> Snapshot -> FeatureEngine -> features (LC-06)
  - **Architecture:** DataConnector -> Snapshot -> FeatureEngine -> LiveFeaturesUpdate
  - **Hard read-only constraint:**
    - `feed.py` MUST NOT import from `grinder.execution.*`
    - Enforced by `test_feed_py_has_no_execution_imports` (AST parsing)
    - Violation = CI failure
  - **BinanceWsConnector** (`src/grinder/connectors/binance_ws.py`):
    - Implements `DataConnector` ABC with `iter_snapshots()` async iterator
    - Parses bookTicker JSON -> Snapshot objects
    - Idempotency via `last_seen_ts` tracking
    - Auto-reconnect with exponential backoff
    - Testable via `WsTransport` ABC injection (FakeWsTransport for tests)
  - **LiveFeed pipeline:**
    - Symbol filtering (optional)
    - FeatureEngine integration (BarBuilder -> indicators)
    - Yields `LiveFeaturesUpdate` with computed features
    - Warmup detection (`is_warmed_up` when bars >= warmup_bars)
  - **Key types:**
    - `LiveFeaturesUpdate`: ts, symbol, features, bar_completed, is_warmed_up, latency_ms
    - `WsMessage`: Raw WebSocket message wrapper
    - `BookTickerData`: Parsed Binance bookTicker fields
    - `LiveFeedStats`: Ticks/bars/errors tracking
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_live_feed.py -v
    ```
  - **Unit tests:** `tests/unit/test_live_feed.py` (21 tests)
    - P0 hard-block tests prove 0 execution imports
    - FakeWsTransport tests prove testable WS behavior
    - Connector tests prove yields snapshots, skips duplicates
    - LiveFeed tests prove features computed correctly
    - Golden output tests prove determinism
  - **Fixtures:** `tests/fixtures/ws/bookticker_btcusdt.json`
  - See ADR-037 for design decisions
- **FuturesUserDataWsConnector v0.1** (`src/grinder/connectors/binance_user_data_ws.py`):
  - WebSocket connector for Binance Futures USDT-M user-data stream (LC-09a)
  - **Features:**
    - ListenKey lifecycle: create (POST), keepalive (PUT), close (DELETE)
    - Auto-keepalive every 30 seconds (configurable)
    - Auto-reconnect with exponential backoff
    - Event normalization: ORDER_TRADE_UPDATE -> FuturesOrderEvent, ACCOUNT_UPDATE -> FuturesPositionEvent
    - Unknown events yield as UNKNOWN with raw_data (don't crash)
  - **Event types** (`src/grinder/execution/futures_events.py`):
    - `FuturesOrderEvent`: Normalized order update (ts, symbol, order_id, client_order_id, side, status, price, qty, executed_qty, avg_price)
    - `FuturesPositionEvent`: Normalized position update (ts, symbol, position_amt, entry_price, unrealized_pnl)
    - `UserDataEvent`: Tagged union wrapper with event_type discriminator
    - `BINANCE_STATUS_MAP`: Binance status -> OrderState mapping (NEW->OPEN, CANCELED->CANCELLED)
  - **ListenKeyManager** (`ListenKeyManager`):
    - HTTP operations via injectable `HttpClient`
    - 401 -> `ConnectorNonRetryableError` (invalid API key)
    - 5xx -> `ConnectorTransientError` (retryable)
  - **Testing:**
    - `FakeListenKeyManager`: Mock for listenKey operations
    - `FakeWsTransport`: Reused from binance_ws.py for WS testing
    - Injectable clock for keepalive timing tests
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_futures_events.py tests/unit/test_listen_key_manager.py tests/unit/test_user_data_ws.py -v
    ```
  - **Unit tests:**
    - `tests/unit/test_futures_events.py` (41 tests): serialization, parsing, lifecycle golden tests
    - `tests/unit/test_listen_key_manager.py` (17 tests): HTTP operations, error handling
    - `tests/unit/test_user_data_ws.py` (21 tests): connection, events, stats
  - **Fixtures:**
    - `tests/fixtures/user_data/order_lifecycle.jsonl`: NEW -> PARTIALLY_FILLED -> FILLED
    - `tests/fixtures/user_data/position_lifecycle.jsonl`: 0 -> position -> 0
  - **Limitations (v0.1):**
    - No reconciliation logic (LC-09b scope)
    - No REST snapshot fallback
    - No active actions (cancel-all, flatten)
    - No metrics/counters for stream health
  - See ADR-041 for design decisions
- **Reconciliation v0.1 (Passive)** (`src/grinder/reconcile/`):
  - Detects mismatches between expected state (what we sent) and observed state (stream + REST) for Binance Futures USDT-M (LC-09b)
  - **Passive only:** logs + metrics + action_plan text -- no actual remediation actions
  - **Mismatch types** (`MismatchType` enum):
    - `ORDER_MISSING_ON_EXCHANGE`: Expected OPEN order not found after grace period
    - `ORDER_EXISTS_UNEXPECTED`: Order on exchange (grinder_ prefix) not in expected state
    - `ORDER_STATUS_DIVERGENCE`: Expected vs observed status differs
    - `POSITION_NONZERO_UNEXPECTED`: Position != 0 when expected = 0
  - **Components:**
    - `ExpectedStateStore`: Ring buffer (max_orders=200) + TTL (24h) eviction, injectable clock
    - `ObservedStateStore`: Updated from FuturesOrderEvent/FuturesPositionEvent + REST snapshots
    - `ReconcileEngine`: Compares expected vs observed, emits Mismatch list
    - `ReconcileMetrics`: Prometheus-compatible counters/gauges
    - `SnapshotClient`: REST polling with retry/backoff on 429/5xx
  - **Metrics:**
    - `grinder_reconcile_mismatch_total{type="..."}`: Counter by mismatch type
    - `grinder_reconcile_last_snapshot_age_seconds`: Gauge for REST snapshot staleness
    - `grinder_reconcile_runs_total`: Counter for reconcile runs
  - **Configuration** (`ReconcileConfig`):
    - `order_grace_period_ms=5000`: Delay before ORDER_MISSING fires
    - `snapshot_interval_sec=60`: REST snapshot polling interval
    - `expected_max_orders=200`: Ring buffer limit
    - `expected_ttl_ms=86400000`: 24h TTL for expected orders
    - `symbol_filter`: Optional symbol filter
    - `enabled=True`: Feature flag
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_reconcile*.py tests/unit/test_expected_state.py tests/unit/test_observed_state.py tests/unit/test_snapshot_client.py -v
    ```
  - **Unit tests:**
    - `tests/unit/test_reconcile_types.py` (22 tests): serialization roundtrips
    - `tests/unit/test_expected_state.py` (16 tests): ring buffer, TTL eviction
    - `tests/unit/test_observed_state.py` (15 tests): stream/REST updates
    - `tests/unit/test_reconcile_engine.py` (13 tests): mismatch detection
    - `tests/unit/test_snapshot_client.py` (16 tests): retry logic
  - **Fixtures:**
    - `tests/fixtures/reconcile/expected_orders.jsonl`: Sample expected orders
    - `tests/fixtures/reconcile/rest_open_orders.json`: GET /openOrders response
    - `tests/fixtures/reconcile/rest_position_risk.json`: GET /positionRisk response
    - `tests/fixtures/reconcile/mismatch_scenarios.jsonl`: Test scenarios
  - **Limitations (v0.1):**
    - No integration with LiveEngineV0 event loop
  - **Note:** HA leader election for remediation loop was added in Active Remediation v0.2 (LC-20). See below.
  - See ADR-042 for design decisions
- **Active Remediation v0.2** (`src/grinder/reconcile/remediation.py`):
  - Extends passive reconciliation (LC-09b) with active actions (LC-10)
  - **Actions:**
    - `cancel_all`: Cancel unexpected grinder_ prefixed orders via `port.cancel_order()`
    - `flatten`: Close unexpected positions via `port.place_market_order(reduce_only=True)`
  - **Staged Rollout Modes (LC-18):**
    - `DETECT_ONLY`: Detect mismatches, no planning (0 port calls) -- **default**
    - `PLAN_ONLY`: Plan remediation, increment planned metrics (0 port calls)
    - `BLOCKED`: Plan + block by gates, increment blocked metrics (0 port calls)
    - `EXECUTE_CANCEL_ALL`: Execute only cancel_all actions
    - `EXECUTE_FLATTEN`: Execute only flatten actions (includes cancel budget check)
  - **Safety Gates (LC-10 + LC-18):**
    1. Mode check (DETECT_ONLY/PLAN_ONLY/BLOCKED -> early exit)
    2. Strategy allowlist (uses LC-12 `parse_client_order_id()`)
    3. Symbol remediation allowlist (optional)
    4. Budget: `max_calls_per_run`, `max_notional_per_run`
    5. Budget: `max_calls_per_day`, `max_notional_per_day`
    6. `action != NONE` (config)
    7. `dry_run == False` (config)
    8. `allow_active_remediation == True` (config)
    9. `armed == True` (from LiveEngine)
    10. `ALLOW_MAINNET_TRADE=1` (env var)
    11. Cooldown elapsed since last action
    12. Symbol in whitelist
    13. grinder_ prefix for cancel (protects manual orders)
    14. Notional <= cap for flatten (limits exposure)
  - **Budget Tracking (LC-18):** (`src/grinder/reconcile/budget.py`)
    - `BudgetState`: Tracks calls/notional today + this run
    - `BudgetTracker`: Enforces limits, daily reset at midnight UTC
    - JSON persistence (optional): `budget_state_path` config field
  - **Kill-switch semantics:** Remediation ALLOWED (reduces risk)
  - **Default behavior:** DETECT_ONLY mode (no planning, no execution)
  - **New types:**
    - `RemediationMode`: Enum (5 modes for staged rollout)
    - `RemediationBlockReason`: Enum (27 values for why blocked -- includes NOT_LEADER for LC-20)
    - `RemediationStatus`: Enum (PLANNED, EXECUTED, BLOCKED, FAILED)
    - `RemediationResult`: Frozen dataclass for remediation outcome
    - `RemediationExecutor`: Class with `can_execute()`, `remediate_cancel()`, `remediate_flatten()`
    - `BudgetState`, `BudgetTracker`: Budget enforcement (LC-18)
  - **New metrics:**
    - `grinder_reconcile_action_planned_total{action}`: Dry-run plans
    - `grinder_reconcile_action_executed_total{action}`: Real executions
    - `grinder_reconcile_action_blocked_total{reason}`: Blocked actions
    - `grinder_reconcile_budget_calls_used_day`: Daily call count
    - `grinder_reconcile_budget_notional_used_day`: Daily notional USDT
    - `grinder_reconcile_budget_calls_remaining_day`: Remaining daily calls
    - `grinder_reconcile_budget_notional_remaining_day`: Remaining daily notional
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_remediation.py -v
    ```
  - **Unit tests:** `tests/unit/test_remediation.py` (48 tests, including LC-20 HA tests)
  - **HA Leader-Only Remediation (LC-20):**
    - Only `HARole.ACTIVE` instances can execute remediation
    - Non-leader (STANDBY, UNKNOWN) returns BLOCKED status with `NOT_LEADER` reason
    - Appears in `action_blocked_total{reason="not_leader"}` metric (NOT planned)
    - Gate 0 (HA role check) happens before all other gates
    - `grinder_ha_is_leader` metric: 1=leader, 0=follower
    - See ADR-054 for design decisions
  - **CI Smoke Test (ha-mismatch-smoke):**
    - `scripts/docker_smoke_ha_mismatch.sh` verifies leader-only execution in Docker
    - Leader: `status=planned`, `action_planned_total{action="cancel_all"} >= 1`
    - Follower: `status=blocked`, `action_blocked_total{reason="not_leader"} >= 1`
    - Uses FakePort (no real HTTP calls to Binance)
    - **Diagnostics debug knob (local only):**
      - Set `SMOKE_FORCE_FAIL=1` to force an early exit after the HA stack starts, to verify that `dump_diagnostics()` prints `docker compose ps`, container logs, and HA metrics.
      - CI does **not** use `SMOKE_FORCE_FAIL`; it is intended for manual troubleshooting only.
  - **Limitations (v0.2):**
    - Not integrated with LiveEngineV0 event loop
    - No automatic strategy recovery
  - See ADR-043 (LC-10), ADR-052 (LC-18), ADR-054 (LC-20) for design decisions
- **Remediation Wiring v0.1** (`src/grinder/reconcile/runner.py`):
  - Orchestrates: ReconcileEngine -> ReconcileRunner -> RemediationExecutor (LC-11)
  - **ReconcileRunner:**
    - `run()` -> `ReconcileRunReport` with full audit trail
    - Routes mismatches via ROUTING_POLICY (SSOT constants)
  - **Routing Policy (frozenset constants):**
    - `ORDER_EXISTS_UNEXPECTED` -> CANCEL
    - `ORDER_STATUS_DIVERGENCE` -> CANCEL (if not terminal)
    - `POSITION_NONZERO_UNEXPECTED` -> FLATTEN
    - `ORDER_MISSING_ON_EXCHANGE` -> NO ACTION (v0.1)
  - **Terminal statuses (skip cancel):** FILLED, CANCELLED, REJECTED, EXPIRED
  - **Actionable statuses (allow cancel):** OPEN, PARTIALLY_FILLED
  - **Bounded execution:**
    - One action type per run (cancel OR flatten, whichever comes first)
    - Respects executor's max_orders_per_action / max_symbols_per_action
  - **New types:**
    - `ReconcileRunReport`: Frozen dataclass (ts_start, ts_end, cancel_results, flatten_results, skipped_*)
  - **New metrics:**
    - `grinder_reconcile_runs_with_mismatch_total`: Counter for runs with mismatches
    - `grinder_reconcile_runs_with_remediation_total{action}`: Counter for runs with executed actions
    - `grinder_reconcile_last_remediation_ts_ms`: Gauge for last remediation timestamp
  - **Audit logging:**
    - `RECONCILE_RUN`: Run completion with summary stats
    - `REMEDIATE_SKIP`: Skipped mismatch with reason
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_reconcile_runner.py -v
    ```
  - **Unit tests:** `tests/unit/test_reconcile_runner.py` (34 tests)
  - **Limitations (v0.1):**
    - One action type per run (no mixed cancel/flatten)
    - ORDER_MISSING_ON_EXCHANGE -> alert only, no retry
    - Not integrated with LiveEngineV0 event loop
  - **Runbook:** `docs/runbooks/13_OPERATOR_CEREMONY.md`
  - See ADR-044 for design decisions
- **Configurable Order Identity v0.1** (`src/grinder/reconcile/identity.py`):
  - Central config for order identity: prefix + strategy allowlist (LC-12)
  - **OrderIdentityConfig:**
    - `prefix`: Order ID prefix (default: "grinder_")
    - `strategy_id`: Strategy identifier (default: "default")
    - `allowed_strategies`: Set of allowed strategy IDs for remediation
    - `require_strategy_allowlist`: If True, strategy must be in allowlist
    - `allow_legacy_format`: Allow legacy format (env: `ALLOW_LEGACY_ORDER_ID=1`)
  - **clientOrderId Formats:**
    - v1: `{prefix}{strategy_id}_{symbol}_{level_id}_{ts}_{seq}`
    - Legacy: `grinder_{symbol}_{level_id}_{ts}_{seq}` (no strategy_id)
  - **Core functions:**
    - `parse_client_order_id(cid)`: Parse v1 or legacy format
    - `is_ours(cid, config)`: Check ownership via prefix + strategy allowlist
    - `generate_client_order_id(config, ...)`: Create v1 format
  - **Integration points:**
    - `BinanceFuturesPort.place_order()`: Uses `generate_client_order_id()`
    - `BinancePort.place_order()`: Uses `generate_client_order_id()`
    - `ReconcileEngine._check_unexpected_orders()`: Uses `is_ours()`
    - `RemediationExecutor.can_execute()` Gate 8: Uses `is_ours()`
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_identity.py -v
    ```
  - **Unit tests:** `tests/unit/test_identity.py` (44 tests)
  - See ADR-045 for design decisions
- **Audit JSONL v0.1** (`src/grinder/reconcile/audit.py`):
  - Append-only JSONL audit trail for reconcile/remediation runs (LC-11b)
  - **Opt-in:** Disabled by default, enable via `GRINDER_AUDIT_ENABLED=1`
  - **Event types:**
    - `RECONCILE_RUN`: Summary at end of each reconcile run
    - `REMEDIATE_ATTEMPT`: Individual remediation attempt (future)
    - `REMEDIATE_RESULT`: Result of remediation (future)
  - **Safety guarantees:**
    - Redaction of secrets by default (api_key, token, password, etc.)
    - Bounded file size: rotation at 100MB or 100k events
    - Fail-open: continues on write error (logs warning)
  - **Configuration:**
    ```python
    AuditConfig(
        enabled=False,              # Opt-in
        path="audit/reconcile.jsonl",
        max_bytes=100_000_000,      # 100 MB
        redact=True,                # Redact secrets
        fail_open=True,             # Continue on error
    )
    ```
  - **Integration:** Pass `AuditWriter` to `ReconcileRunner.audit_writer`
  - **How to enable:**
    ```bash
    GRINDER_AUDIT_ENABLED=1 GRINDER_AUDIT_PATH=/path/to/audit.jsonl \
        PYTHONPATH=src python3 -m your_app
    ```
  - **Unit tests:** `tests/unit/test_audit.py` (33 tests)
  - See ADR-046 for design decisions
- **E2E Reconcile Smoke Harness** (`scripts/smoke_reconcile_e2e.py`):
  - End-to-end smoke test for reconcile->remediate flow (LC-13)
  - **3 P0 scenarios:**
    - `order`: Unexpected order -> CANCEL -> validates cancel routing
    - `position`: Unexpected position -> FLATTEN -> validates flatten routing
    - `mixed`: Both mismatches -> priority routing (order wins)
  - **Default: DRY-RUN mode**
    - Zero port calls in dry-run
    - Safe to run without credentials or env vars
  - **Live mode gates (all 5 required):**
    - `--confirm LIVE_REMEDIATE` CLI flag
    - `RECONCILE_DRY_RUN=0` env var
    - `RECONCILE_ALLOW_ACTIVE=1` env var
    - `ARMED=1` env var
    - `ALLOW_MAINNET_TRADE=1` env var
  - **Audit integration:** Writes RECONCILE_RUN events when `GRINDER_AUDIT_ENABLED=1`
  - **How to run:**
    ```bash
    # Dry-run (default, safe)
    PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e

    # With audit
    GRINDER_AUDIT_ENABLED=1 PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e
    ```
  - See ADR-047 for design decisions

- **Live Reconcile Runner** (`scripts/run_live_reconcile.py`):
  - Operator entrypoint for LC-18 staged rollout of active remediation
  - **Environment variables:**
    - `REMEDIATION_MODE`: detect_only|plan_only|blocked|execute_cancel_all|execute_flatten (default: detect_only)
    - `ALLOW_MAINNET_TRADE`: Must be exactly "1" for execute modes (not "true" or "yes")
    - `REMEDIATION_STRATEGY_ALLOWLIST`: CSV strategy IDs (empty = allow all)
    - `REMEDIATION_SYMBOL_ALLOWLIST`: CSV symbols (empty = allow all)
    - `MAX_CALLS_PER_DAY`, `MAX_NOTIONAL_PER_DAY`: Daily budget limits
    - `MAX_CALLS_PER_RUN`, `MAX_NOTIONAL_PER_RUN`: Per-run budget limits
    - `FLATTEN_MAX_NOTIONAL_PER_CALL`: Max notional for single flatten
    - `BUDGET_STATE_PATH`: Path to persist daily budget (optional)
  - **CLI flags:** `--duration`, `--interval-ms`, `--metrics-port`, `--audit-out`, `--symbols`
  - **Exit codes:** 0=success, 2=config error, 3=runtime error
  - **Safety:** Execute mode without `ALLOW_MAINNET_TRADE=1` -> exit 2 (config error)
  - **How to run:**
    ```bash
    # Stage A: Detect-only (default, safest)
    PYTHONPATH=src python3 -m scripts.run_live_reconcile --duration 60

    # Stage D: Execute cancel-only (requires ALLOW_MAINNET_TRADE=1)
    REMEDIATION_MODE=execute_cancel_all ALLOW_MAINNET_TRADE=1 \
    PYTHONPATH=src python3 -m scripts.run_live_reconcile --duration 60

    # Stage E: Execute flatten (requires ALLOW_MAINNET_TRADE=1)
    REMEDIATION_MODE=execute_flatten ALLOW_MAINNET_TRADE=1 \
    REMEDIATION_STRATEGY_ALLOWLIST="default" \
    REMEDIATION_SYMBOL_ALLOWLIST="BTCUSDT" \
    MAX_CALLS_PER_DAY=1 MAX_CALLS_PER_RUN=1 \
    MAX_NOTIONAL_PER_DAY=150 MAX_NOTIONAL_PER_RUN=150 \
    FLATTEN_MAX_NOTIONAL_PER_CALL=150 \
    PYTHONPATH=src python3 -m scripts.run_live_reconcile --duration 60
    ```
  - **E2E Validation (Mainnet):**
    - **Stage D E2E:** Verified on Binance Futures mainnet -- grinder_ orders cancelled (PR #102)
    - **Stage E E2E:** Verified on Binance Futures mainnet -- position flattened (PR #103)
    - **BTCUSDT Limitation:** Binance Futures min notional is high; `scripts/place_test_position.py` has
      P0 safety guard that fails if min notional exceeds $20 cap (prevents accidental large positions)
    - **Non-BTC Micro-Test (M4.3):** For lower-notional testing, see Runbook 13 "Stage E: Non-BTC Symbol
      Micro-Test" -- includes exchangeInfo procedure to verify min-notional before testing
  - **Unit tests:** `tests/unit/test_run_live_reconcile.py` (27 tests)
  - See ADR-052 for LC-18 design decisions
  - **Artifact Run-Directory (M4.1):**
    - Set `GRINDER_ARTIFACTS_DIR` to enable structured artifact storage
    - Creates: `$GRINDER_ARTIFACTS_DIR/YYYY-MM-DD/run_<ts>/` per run
    - Fixed filenames: `stdout.log`, `audit.jsonl`, `metrics.prom`, `metrics_summary.json`, `budget_state.json`
    - TTL cleanup: `GRINDER_ARTIFACT_TTL_DAYS` (default: 14) deletes old run-dirs at startup
    - Backward compatible: explicit `--audit-out`/`--metrics-out` take precedence
    - **Usage:**
      ```bash
      GRINDER_ARTIFACTS_DIR=/var/lib/grinder/artifacts \
      PYTHONPATH=src python3 -m scripts.run_live_reconcile --duration 60
      # Creates: /var/lib/grinder/artifacts/2026-02-07/run_1707307200000/{stdout.log,...}
      ```
    - **Post-run artifact bundle:**
      ```bash
      ls -la $GRINDER_ARTIFACTS_DIR/$(date +%Y-%m-%d)/run_*/
      ```
  - **Budget State Lifecycle (M4.2):**
    - `BUDGET_STATE_PATH`: Path to persist daily budget (optional)
    - `BUDGET_STATE_STALE_HOURS`: Hours before stale warning (default: 24)
    - `--reset-budget-state`: CLI flag to delete budget file before start
    - **First run (clean):** Use `--reset-budget-state` to start fresh
    - **Multi-run (persist):** Omit flag to preserve budget across runs
    - **Stale warning:** Logs warning if file mtime > 24h (configurable)
    - **Usage:**
      ```bash
      # First run - clean budget
      BUDGET_STATE_PATH=/var/lib/grinder/budget.json \
      PYTHONPATH=src python3 -m scripts.run_live_reconcile --reset-budget-state --duration 60

      # Multi-run - preserve budget
      BUDGET_STATE_PATH=/var/lib/grinder/budget.json \
      PYTHONPATH=src python3 -m scripts.run_live_reconcile --duration 60
      ```
    - **Unit tests:** `tests/unit/test_budget_lifecycle.py`
- **ReconcileLoop for LiveEngine** (`src/grinder/live/reconcile_loop.py`):
  - Periodic background loop for reconciliation in LiveEngine (LC-14a, LC-14b)
  - **Threading pattern:** Daemon thread with `threading.Event` for graceful shutdown
  - **Configuration:**
    - `RECONCILE_ENABLED` env var (default: False)
    - `RECONCILE_INTERVAL_MS` env var (default: 30000ms)
    - `require_active_role` option for HA integration
    - `detect_only` option for hard enforcement (LC-14b)
  - **Statistics:**
    - `runs_total`, `runs_skipped_role`, `runs_with_mismatch`, `runs_with_error`
    - `last_run_ts_ms`, `last_report` (thread-safe access)
  - **Safety:**
    - Disabled by default (opt-in via env var)
    - Minimum interval 1000ms enforced
    - Errors logged, loop continues
    - HA-aware (skips when not ACTIVE)
    - **detect_only enforcer (LC-14b):** Refuses to start if runner can execute actions
  - **Smoke scripts:**
    - `scripts/smoke_live_reconcile_loop.py` -- FakePort pattern (zero HTTP calls)
    - `scripts/smoke_live_reconcile_loop_real_sources.py` -- Real Binance REST (LC-14b)
  - **Unit tests:** `tests/unit/test_reconcile_loop.py` (23 tests)
  - **How to run:**
    ```bash
    # FakePort smoke (no network)
    PYTHONPATH=src python3 -m scripts.smoke_live_reconcile_loop --duration 15
    # Real sources smoke (LC-14b)
    PYTHONPATH=src python3 -m scripts.smoke_live_reconcile_loop_real_sources
    ```
  - See ADR-048, ADR-049 for design decisions
- **PriceGetter** (`src/grinder/reconcile/price_getter.py`):
  - Fetches current market price from Binance Futures REST API (LC-14b)
  - Uses HttpClient protocol (same as SnapshotClient)
  - 1-second cache TTL to reduce API calls
  - Returns `Decimal | None` for safe handling
  - **Endpoint:** `GET /fapi/v1/ticker/price`
  - See ADR-049 for design decisions
- **Enablement Ceremony v0.1** (LC-15a):
  - 5-stage procedure for safe ReconcileLoop enablement in production
  - **Stages:** Baseline -> Detect-only -> Plan-only -> Blocked -> Live
  - Each stage has explicit pass criteria and rollback steps
  - **Runbook:** `docs/runbooks/15_ENABLEMENT_CEREMONY.md`
  - **Smoke script:** `scripts/smoke_enablement_ceremony.py`
  - **Rollback:** Single command (`RECONCILE_ENABLED=0` or `RECONCILE_ACTION=none`)
  - See ADR-050 for design decisions
- **Reconcile Observability v0.1** (LC-15b):
  - Metrics contract + alerts + SLOs for reconciliation
  - **Metrics contract integration:**
    - Reconcile metrics added to `REQUIRED_METRICS_PATTERNS` in `live_contract.py`
    - MetricsBuilder includes reconcile metrics via `_build_reconcile_metrics()`
    - Contract tests in `tests/unit/test_live_contracts.py::TestReconcileMetricsContract`
    - **Scope:** LC-15b added **26 reconcile-specific patterns** (subset of 71 total patterns in contract)
  - **Prometheus alert rules (`monitoring/alert_rules.yml`):**
    - `ReconcileLoopDown`: warning -- Loop not running when expected
    - `ReconcileSnapshotStale`: warning -- Snapshot age > 120s
    - `ReconcileMismatchSpike`: warning -- High mismatch rate
    - `ReconcileRemediationExecuted`: critical -- REAL action executed
    - `ReconcileRemediationPlanned`: info -- Dry-run action planned
    - `ReconcileRemediationBlocked`: info -- Action blocked by gates
    - `ReconcileMismatchNoBlocks`: warning -- Mismatches but no remediation
    - **Scope:** LC-15b added **7 reconcile alerts** in `grinder_reconcile` group (subset of 16 total alerts)
  - **Service Level Objectives:**
    - Loop Availability: 99.9% (runs_total > 0 per 5-min window)
    - Snapshot Freshness: 99% (age < 120s)
    - Execution Budget: < 10/day (action_executed_total)
  - **Runbook:** `docs/runbooks/16_RECONCILE_ALERTS_SLOS.md`
  - See ADR-051 for design decisions

- **Credentialed Real-Source Smoke v0.1** (LC-17):
  - Smoke test with real Binance Futures USDT-M credentials (detect-only)
  - **Script:** `scripts/smoke_real_sources_detect_only.py`
  - **Sources tested:**
    - REST snapshot (orders/positions via SnapshotClient)
    - Price getter (BTCUSDT price fetch)
    - User-data WS (listenKey establishment)
  - **Safety guarantees:**
    - Uses FakePort (no real order execution ever)
    - `detect_only=True` enforced at ReconcileLoopConfig
    - `action=NONE` enforced at ReconcileConfig
    - `armed=False` enforced at RemediationExecutor
  - **Audit JSONL output:** `--audit-out` flag for artifact generation
  - **Exit codes:** 0=success, 1=detect-only violation, 2=config error, 3=connection error
  - **Usage:**
    ```bash
    # Dry-run (no credentials)
    PYTHONPATH=src python3 -m scripts.smoke_real_sources_detect_only --dry-run

    # With mainnet credentials
    BINANCE_API_KEY=xxx BINANCE_SECRET=xxx \
    PYTHONPATH=src python3 -m scripts.smoke_real_sources_detect_only \
        --duration 60 --audit-out /tmp/audit.jsonl
    ```

- **Observability Hardening v0.1** (LC-16):
  - Production-grade observability with automated validation
  - **Grafana Dashboards:**
    - `monitoring/grafana/dashboards/grinder_overview.json` -- System health, HA, gating, risk
    - `monitoring/grafana/dashboards/grinder_reconcile.json` -- Reconcile loop, mismatches, remediation
  - **Promtool CI:** Alert rules validated in `.github/workflows/promtool.yml`
  - **Metrics Contract Smoke:** `scripts/smoke_metrics_contract.py`
    - Validates /metrics against `REQUIRED_METRICS_PATTERNS` (60+ patterns)
    - Checks for `FORBIDDEN_METRIC_LABELS` (high-cardinality labels)
    - Exit codes: 0=valid, 1=validation failed, 2=connection error
  - **Usage:**
    ```bash
    # Validate against live service
    python3 -m scripts.smoke_metrics_contract --url http://localhost:9090/metrics

    # Validate from file
    python3 -m scripts.smoke_metrics_contract --file /tmp/metrics.txt -v
    ```

- **Live Smoke Harness** (`scripts/smoke_live_testnet.py`):
  - Smoke test harness for Binance (testnet or mainnet): place micro order -> cancel (LC-07, LC-08b)
  - **Safe-by-construction guards:**
    - `--dry-run` by default (no real HTTP calls)
    - Requires `--confirm TESTNET` for testnet orders
    - Requires `--confirm MAINNET_TRADE` for mainnet orders (LC-08b)
    - Requires `ARMED=1` env var for any real trades
    - Testnet: requires `ALLOW_TESTNET_TRADE=1` env var
    - Mainnet: requires `ALLOW_MAINNET_TRADE=1` env var + guards (ADR-039)
    - Kill-switch blocks PLACE/REPLACE, allows CANCEL
  - **Mainnet guards (ADR-039):**
    - `--max-notional` argument (default: $50) caps each order
    - `symbol_whitelist` required (non-empty)
    - `max_orders_per_run=1` (single order per run)
    - All guards enforced at config validation time
  - **E2E execution status:**
    - Harness is READY and tested in dry-run + kill-switch modes
    - Real E2E run is OPERATOR-DEPENDENT (requires API credentials)
  - **Failure paths:**
    - Missing keys -> clear exit 1 + message
    - Missing env var -> clear error message
    - Notional exceeds limit -> clear error message
    - Kill-switch active -> PLACE blocked (expected), CANCEL allowed
  - **How to verify:**
    ```bash
    # Dry-run (default) -- no credentials needed
    PYTHONPATH=src python3 -m scripts.smoke_live_testnet

    # Kill-switch test -- no credentials needed
    PYTHONPATH=src python3 -m scripts.smoke_live_testnet --kill-switch

    # Real testnet order
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_TESTNET_TRADE=1 \
        PYTHONPATH=src python3 -m scripts.smoke_live_testnet --confirm TESTNET

    # Real mainnet order (budgeted)
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_MAINNET_TRADE=1 \
        PYTHONPATH=src python3 -m scripts.smoke_live_testnet --confirm MAINNET_TRADE
    ```
  - **Runbooks:**
    - `docs/runbooks/08_SMOKE_TEST_TESTNET.md` -- testnet procedure
    - `docs/runbooks/09_MAINNET_TRADE_SMOKE.md` -- mainnet procedure (LC-08b)
  - See ADR-038, ADR-039 for design decisions
- **Futures Smoke Harness** (`scripts/smoke_futures_mainnet.py`):
  - Smoke test harness for Binance Futures USDT-M mainnet (LC-08b-F)
  - **Target execution venue:** `fapi.binance.com` (Futures USDT-M)
  - **Safe-by-construction guards (9 layers):**
    - `--dry-run` by default (no real HTTP calls)
    - Requires `--confirm FUTURES_MAINNET_TRADE` for real orders
    - Requires `ARMED=1` env var
    - Requires `ALLOW_MAINNET_TRADE=1` env var
    - `symbol_whitelist` required (non-empty)
    - `max_notional_per_order` required (default: $125, above Binance $100 min)
    - `max_orders_per_run=1` (single order per run)
    - `target_leverage=3` (reduces margin req; safe: far-from-market, cancelled)
    - Position cleanup on fill
  - **7-step procedure:**
    1. Get account info (position mode)
    2. Set leverage to target (default: 1x)
    3. Check existing position
    4. Place limit order (far from market)
    5. Cancel order
    6. Check and close any position (if filled)
    7. Final position verification (should be 0)
  - **How to verify:**
    ```bash
    # Dry-run (default) -- no credentials needed
    PYTHONPATH=src python3 -m scripts.smoke_futures_mainnet

    # Real futures mainnet order (budgeted)
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_MAINNET_TRADE=1 \
        PYTHONPATH=src python3 -m scripts.smoke_futures_mainnet --confirm FUTURES_MAINNET_TRADE
    ```
  - **Runbook:** `docs/runbooks/10_FUTURES_MAINNET_TRADE_SMOKE.md`
  - See ADR-040 for design decisions
- **DrawdownGuard v0** (`src/grinder/risk/drawdown.py`):
  - Tracks equity high-water mark (HWM)
  - Computes drawdown: `(HWM - equity) / HWM`
  - Configurable threshold (`max_drawdown_pct`, default 5%)
  - Latching behavior: once triggered, stays triggered until reset
  - **Equity definition:** `equity = initial_capital + total_realized_pnl + total_unrealized_pnl`
  - **HWM initialization:** First equity sample (starts at `initial_capital`)
  - See ADR-013 for design decisions
- **AutoSizer v1** (`src/grinder/sizing/auto_sizer.py`):
  - Risk-budget-based position sizing for grid policies (ASM-P2-01)
  - **Core formula:** `qty_per_level = (equity * dd_budget) / (n_levels * price * adverse_move)`
  - **Risk guarantee:** worst_case_loss <= equity * dd_budget
  - **Sizing modes:** UNIFORM (default), PYRAMID, INVERSE_PYRAMID
  - **Inputs:** equity, dd_budget (e.g., 0.20), adverse_move (e.g., 0.25), grid_shape, price
  - **Output:** SizeSchedule with qty_per_level[], risk_utilization, worst_case_loss
  - **Integration:** AdaptiveGridPolicy (opt-in via `auto_sizing_enabled=True`)
  - **Backward compat:** When disabled, uses legacy uniform `size_per_level`
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_auto_sizer.py tests/unit/test_adaptive_policy.py::TestAutoSizingIntegration -v
    ```
  - **Unit tests:** `tests/unit/test_auto_sizer.py` (36 tests)
  - **Integration tests:** `tests/unit/test_adaptive_policy.py::TestAutoSizingIntegration` (5 tests)
  - See ADR-031 for design decisions
- **DdAllocator v1** (`src/grinder/sizing/dd_allocator.py`):
  - Portfolio-to-symbol DD budget distribution (ASM-P2-02)
  - **Inputs:** equity, portfolio_dd_budget, candidates[] (symbol, tier, weight, enabled)
  - **Output:** AllocationResult with per-symbol dd_budget fractions and residual
  - **Algorithm:** `risk_weight = user_weight / tier_factor`, normalize, distribute, ROUND_DOWN
  - **Tier factors:** LOW=1.0, MED=1.5, HIGH=2.0 (higher = less budget)
  - **Invariants (all tested):**
    1. Non-negativity: all budgets >= 0
    2. Conservation: sum(budgets) + residual == portfolio_budget
    3. Determinism: same inputs -> same outputs
    4. Monotonicity: larger budget -> no decrease
    5. Tier ordering: HIGH <= MED <= LOW (at equal weights)
  - **Residual policy:** ROUND_DOWN residual stays in cash reserve
  - **Integration:** Output feeds into AdaptiveGridConfig.dd_budget -> AutoSizer
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_dd_allocator.py tests/unit/test_adaptive_policy.py::TestDdAllocatorIntegration -v
    ```
  - **Unit tests:** `tests/unit/test_dd_allocator.py` (28 tests)
  - **Integration tests:** `tests/unit/test_adaptive_policy.py::TestDdAllocatorIntegration` (3 tests)
  - See ADR-032 for design decisions
- **DrawdownGuardV1** (`src/grinder/risk/drawdown_guard_v1.py`):
  - Intent-based DD guard for portfolio and per-symbol risk enforcement (ASM-P2-03)
  - **GuardState:** `NORMAL` (all intents allowed) | `DRAWDOWN` (reduce-only)
  - **OrderIntent:** `INCREASE_RISK` | `REDUCE_RISK` | `CANCEL`
  - **Transition triggers:**
    - Portfolio DD >= portfolio_dd_limit, OR
    - Symbol loss >= symbol_dd_budget
  - **No auto-recovery:** Once in DRAWDOWN, stays until explicit `reset()` (deterministic)
  - **Allow decision table:**
    | State | Intent | Allowed | Reason |
    |-------|--------|---------|--------|
    | NORMAL | * | Yes | NORMAL_STATE |
    | DRAWDOWN | INCREASE_RISK | No | DD_PORTFOLIO_BREACH / DD_SYMBOL_BREACH |
    | DRAWDOWN | REDUCE_RISK | Yes | REDUCE_RISK_ALLOWED |
    | DRAWDOWN | CANCEL | Yes | CANCEL_ALWAYS_ALLOWED |
  - **PaperEngine wiring:** `src/grinder/paper/engine.py` Step 3.5 (lines 717-767)
    - Enabled via `dd_guard_v1_enabled=True` in constructor
    - Wiring point: after gating check, before execution
    - Blocks INCREASE_RISK orders when in DRAWDOWN state
  - **Reduce-only path (P2-04b):**
    - `PaperEngine.flatten_position(symbol, price, ts)` -- closes entire position
    - Checks `guard.allow(REDUCE_RISK, symbol)` before executing
    - Deterministic: same inputs -> same fill output
    - Allows emergency exit when DRAWDOWN is triggered
  - **Reset hook (P2-04c):**
    - `PaperEngine.reset_dd_guard_v1()` -- returns guard to NORMAL state
    - Use case: new session/day start
    - Also called by `PaperEngine.reset()` (general reset)
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_drawdown_guard_v1.py -v
    ```
  - **Unit tests:** `tests/unit/test_drawdown_guard_v1.py` (55 tests: 43 guard/wiring + 6 reduce-only + 6 reset)
  - See ADR-033 for design decisions
- **KillSwitch v0** (`src/grinder/risk/kill_switch.py`):
  - Simple emergency halt latch for trading
  - **Idempotent:** triggering twice is a no-op
  - **Reasons:** `DRAWDOWN_LIMIT`, `MANUAL`, `ERROR`
  - **Reset semantics:** does NOT auto-reset; requires explicit `reset()` call
  - **PaperEngine integration:**
    - When triggered, blocks all trading with `KILL_SWITCH_ACTIVE` gating reason
    - Does NOT auto-liquidate positions (configurable in future)
    - Surfaces state in `PaperResult` and `PaperOutput`
  - **Unit tests:** `tests/unit/test_risk.py` (25 tests)
  - **Integration tests:** `tests/integration/test_kill_switch_integration.py` (9 tests)
  - See ADR-013 for design decisions
- **KillSwitch + EmergencyExit integrated semantics** (PR-B, main @ 9a98648):
  - **Trigger gate**: EmergencyExit fires ONLY when FSM state == EMERGENCY (engine.py:436-443). Outside EMERGENCY = no-op.
  - **Latch**: `_emergency_exit_executed` flag (engine.py:440,558). Second trigger = no-op. One execution per engine lifetime.
  - **State transition**: EMERGENCY -> executor fires -> AccountSyncer measures position_notional_usd (confirmed, not override) -> if notional < $10 threshold AND not kill_switch AND not dd_breached -> PAUSED (fsm_orchestrator.py:367-381).
  - **Recovery conditions** (all must be true): `position_notional_usd is not None`, `position_notional_usd < position_notional_threshold_usd (10.0)`, `kill_switch_active == False`, `drawdown_pct < drawdown_threshold_pct (0.20)`.
  - **INIT protection**: _check_emergency skipped when state == INIT (fsm_orchestrator.py:231). INIT + kill_switch -> stays INIT.
  - **KillSwitch SSOT**: `LiveEngineConfig.kill_switch_active` (config.py:33). No separate env var — config is the single source.
  - **KillSwitch priority in FSM**: Priority 1 in _check_emergency (fsm_orchestrator.py:250). kill_switch_active -> EMERGENCY from any non-INIT state.
  - **KillSwitch effect on trading ops**: Gate 3 (engine.py:652-664). PLACE/REPLACE blocked (`BlockReason.KILL_SWITCH_ACTIVE`), CANCEL allowed.
  - **Metrics**: `grinder_emergency_exit_enabled` (gauge), `grinder_emergency_exit_total{result}` (counter), `grinder_emergency_exit_orders_cancelled_total` (counter), `grinder_emergency_exit_positions_closed_total` (counter). See emergency_exit_metrics.py.
  - **Contract tests**: `tests/unit/test_killswitch_emergency_semantics.py` (8 tests covering trigger gate, latch, state transition, metrics, kill switch priority/effect, INIT edge case, recovery-blocked-by-killswitch).
- **Soak Gate v0** (`scripts/run_soak_fixtures.py`, `.github/workflows/soak_gate.yml`):
  - Fixture-based soak runner for CI release gate
  - **Metrics collected:**
    - Processing latency (p50, p99)
    - Memory usage (RSS max)
    - Fill rate
    - Error counts
    - Digest stability (determinism check)
  - **Threshold policy:** `monitoring/soak_thresholds.yml`
    - Baseline mode: strict thresholds for normal operation
    - Overload mode: relaxed thresholds for stress testing
  - **CI integration:**
    - `soak_gate.yml`: runs on PRs touching src/scripts/tests/monitoring
    - `nightly_soak.yml`: runs daily with synthetic soak (existing)
  - **Commands:**
    - `python3 -m scripts.run_soak_fixtures --output report.json` -- run soak test
    - `python3 -m scripts.check_soak_gate --report report.json --thresholds monitoring/soak_thresholds.yml --mode baseline` -- PR gate (deterministic only)
    - `python3 -m scripts.check_soak_thresholds --baseline report.json --overload report.json --thresholds monitoring/soak_thresholds.yml` -- nightly gate (full)
  - **Unit tests:** `tests/unit/test_soak_thresholds.py`
  - **Test fixtures:** Uses registered fixtures including `sample_day_drawdown` for kill-switch
- **Operations v0** (`docs/runbooks/`, `docs/HOW_TO_OPERATE.md`):
  - Runbooks for common operational tasks:
    - [Startup/Shutdown](runbooks/01_STARTUP_SHUTDOWN.md): docker compose up/down
    - [Health Triage](runbooks/02_HEALTH_TRIAGE.md): quick diagnostics via /healthz
    - [Metrics & Dashboards](runbooks/03_METRICS_DASHBOARDS.md): Prometheus + Grafana
    - [Kill-Switch](runbooks/04_KILL_SWITCH.md): detection, diagnosis, recovery
    - [Soak Gate](runbooks/05_SOAK_GATE.md): running soak tests
    - [Alert Response](runbooks/06_ALERT_RESPONSE.md): responding to Prometheus alerts
    - [HA Operations](runbooks/07_HA_OPERATIONS.md): high availability deployment and failover
  - Operator's guide: [HOW_TO_OPERATE.md](HOW_TO_OPERATE.md)
  - **Quick reference:**
    - Health: `curl -fsS http://localhost:9090/healthz`
    - Metrics: `curl -fsS http://localhost:9090/metrics`
    - Start stack: `docker compose -f docker-compose.observability.yml up --build -d`
    - Stop stack: `docker compose -f docker-compose.observability.yml down -v`
  - **Limitations:** No Kubernetes, no automated runbook execution
- **HA v0** (`src/grinder/ha/`, `docker-compose.ha.yml`):
  - Single-host redundancy with Redis lease-lock coordination
  - **Components:**
    - `HARole` enum: `ACTIVE`, `STANDBY`, `UNKNOWN`
    - `HAState`: thread-safe state container for current role
    - `LeaderElector`: Redis-based lease lock manager with TTL-based coordination
  - **Leader election:**
    - Lock TTL: 10 seconds (configurable via `GRINDER_HA_LOCK_TTL_MS`)
    - Renewal interval: 3 seconds (configurable via `GRINDER_HA_RENEW_INTERVAL_MS`)
    - Lock key: `grinder:leader:lock`
  - **Fail-safe semantics (critical for single-active safety):**
    - Lock renewal failure -> immediately become STANDBY
    - Redis unavailable -> all instances become STANDBY
    - STANDBY/UNKNOWN -> `/readyz` returns 503 (not ready for traffic)
    - Only ACTIVE -> `/readyz` returns 200 (ready)
    - **Unit tests:** `tests/unit/test_ha.py::TestFailSafeSemantics` validates these guarantees
  - **Observability:**
    - `/readyz` endpoint: 200 for ACTIVE, 503 for STANDBY/UNKNOWN
    - `/healthz` endpoint: 200 always (liveness, not readiness)
    - `grinder_ha_role{role}` metric: gauge with all roles (current=1, others=0)
  - **Deployment:** `docker-compose.ha.yml` with Redis 7.2 + 2 grinder instances
  - **Environment variables:**
    - `GRINDER_REDIS_URL`: Redis connection URL (default: redis://localhost:6379/0)
    - `GRINDER_HA_ENABLED`: Enable HA mode (default: false)
    - `GRINDER_HA_LOCK_TTL_MS`: Lock TTL in milliseconds (default: 10000)
    - `GRINDER_HA_RENEW_INTERVAL_MS`: Lock renewal interval in milliseconds (default: 3000)
  - **Commands:**
    - Start HA stack: `docker compose -f docker-compose.ha.yml up --build -d`
    - Failover test: `docker stop grinder_live_1` -> grinder_live_2 becomes ACTIVE within ~10s
  - **Contract tests:**
    - `tests/unit/test_live_contracts.py`: /readyz response structure
    - `tests/unit/test_ha.py`: HA role, state management, fail-safe semantics (17 tests)
  - See ADR-015 for design decisions
  - **Limitations:** Single-host only (Redis is SPOF), no protection against host/VM failure (v1 scope)
- **HA Runbooks + Release Procedure v1** (PR-M3-012):
  - **HA Operations runbook** (`docs/runbooks/07_HA_OPERATIONS.md`):
    - Rolling restart procedure (zero-downtime HTTP)
    - One node down triage and recovery
    - Redis down/flapping troubleshooting
    - Failover testing procedures
  - **Release Checklist v1** (`docs/HOW_TO_OPERATE.md#deploying-a-release`):
    - Pre-flight: CI gates (checks, soak-gate, docker-smoke, ha-smoke, determinism)
    - Deployment steps: single-instance and HA rolling restart
    - Post-deployment verification checklist
    - Rollback plan with exact commands
  - **Navigation updated:** `docs/DOCS_INDEX.md`, `docs/runbooks/README.md`
- **Inventory cap + position-aware intent** (PR-INV-1):
  - Gate 5 (`MAX_POSITION_EXCEEDED`): blocks INCREASE_RISK when total position notional >= cap
  - `GRINDER_MAX_POSITION_USD` env var (None = disabled, safe-by-default)
  - Position-aware `classify_intent()`: SELL when LONG = REDUCE_RISK, BUY when SHORT = REDUCE_RISK
  - Hedge-mode only; one-way mode (`BOTH`) = fail-closed (all PLACE = INCREASE_RISK)
  - Gate chain re-numbered: 8 gates (was 7). Doc-20 updated.
- **Planner cancel-only mode** (PR-INV-2):
  - `LiveGridPlannerV1.plan(suppress_increase=True)`: filters out PLACE/REPLACE, keeps CANCEL only
  - Wired in `LiveEngineV0._plan_grid()`: enabled when FSM state != ACTIVE
  - Prevents risk accumulation in PAUSED/DEGRADED/THROTTLED/EMERGENCY
  - Doc-25 updated (§25.8 Suppress Increase Mode section)
- **Live fill -> TP layer** (PR-INV-3):
  - `LiveCycleLayerV1`: fill detection via AccountSync snapshot diff
  - TP namespace: `grinder_tp_` clientOrderId (`strategy_id="tp"`, SSOT: `identity.py`)
  - TP `reduce_only=True` (semantic invariant for Binance, prevents position increase)
  - Planner filter: `is_tp_order()` from `reconcile.identity` excludes TPs from grid diff
  - TP classified REDUCE_RISK -> passes Gate 5/6/7 even at cap/drawdown/paused
  - TP goes through Gate 8 (fill-prob) -- documented invariant, may be skipped in future PR
  - Contract: `ExecutionAction` gains `reduce_only` + `client_order_id` fields
  - `ExchangePort.place_order()` extended: `reduce_only` + `client_order_id` params (5 impls)
  - `IdempotentExchangePort`: includes `reduce_only` + `client_order_id` in idempotency key
  - `GRINDER_LIVE_CYCLE_ENABLED=0` (safe-by-default)
  - V1: single tick_size for all symbols. Multi-symbol tick mismatch -> cycle layer disabled.
  - Contract: non-numeric `level_id` (e.g., "cleanup") -> TP `level_id=0`
  - Contract: planner filters TPs by `is_tp_order()` -- essential because TP clientOrderIds parse as valid grinder orders
- **TP expiry + observability** (PR-INV-3b):
  - TP orders older than `GRINDER_TP_TTL_MS` (default 300s) are auto-cancelled (reason `TP_EXPIRED`)
  - `LiveCycleConfig.tp_ttl_ms`: `int | None` (None/0 = disabled)
  - `_tp_created_ts`: tracks TP creation timestamps; cleaned up when TP disappears from open_orders
  - Fail-closed: only cancels TPs tracked in `_tp_created_ts` (we created them)
  - `CycleMetrics` singleton (`cycle_metrics.py`): `grinder_cycle_tp_generated_total`, `grinder_cycle_tp_expired_total`, `grinder_cycle_fill_candidates_total`
  - Fill candidate outcomes: `tp_generated`, `skipped_pending_cancel`, `skipped_tp_order`, `skipped_dedup`
  - Wired into `MetricsBuilder` for Prometheus `/metrics` endpoint
- **Replenish after fill** (PR-INV-4):
  - After grid order fill, restores grid level at next level further from center
  - Replenish = INCREASE_RISK: blocked by Gate 5 max-position + INV-2 suppress_increase
  - Safe-by-default: `GRINDER_LIVE_REPLENISH_ENABLED=0`, `GRINDER_REPLENISH_MAX_LEVELS=0`
  - Fail-closed: non-numeric `level_id` -> skip replenish; `level+1 > max_levels` -> skip; `mid_price <= 0` -> skip
  - Price: `mid_price * (1 +/- level * spacing_bps/10000)`, tick-rounded
  - Replenish uses grid identity (`strategy_id="d"`), not TP namespace
  - Metric: `grinder_cycle_replenish_generated_total{symbol}`
- **TP slot takeover** (PR-ROLL-2):
  - When TP created, cancel farthest same-side grid order to keep total opposite-side count constant
  - LONG fill → TP SELL + cancel max-price SELL grid
  - SHORT fill → TP BUY + cancel min-price BUY grid
  - Multi-fill: each TP claims a different grid order (`claimed_for_takeover` dedup)
  - Cancelled order registered in `_pending_cancels` (prevents false fill detection on next tick)
  - Only targets grid orders (`strategy_id="d"`); TP orders never eligible
  - Log: `TP_SLOT_TAKEOVER` / `TP_SLOT_TAKEOVER_SKIP`
- **TP_CLOSE atomicity** (PR-P0-TP-CLOSE-ATOMIC):
  - TP_SLOT_TAKEOVER CANCEL only executes if paired TP_CLOSE PLACE succeeded (linked by `correlation_id` on ExecutionAction)
  - Prevents grid shrinkage on `-4118 ReduceOnly Order Failed` race condition
  - Failed TP PLACEs retried 3x (10s cooldown, `-4118` only) via engine retry queue
  - Known limitation: successful retry does not fire compensating CANCEL. Extra grid order may be reconciled by planner on unfreeze under existing planner behavior; not guaranteed by this PR. Follow-up live evidence required
  - Non-goal: no Prometheus counters - logs only (`TP_SLOT_TAKEOVER_SKIPPED`, `TP_CLOSE_RETRY_QUEUED`, `_OK`, `_EXHAUSTED`). Counters deferred to observability PR
- **Converge-first planner + budget-burn stop** (PR-P0-RACE-1, ADR-084):
  - Three guards scoped to planner/grid-shift path ONLY (cycle layer TP/replenish unaffected):
    1. Sync-gated planner: skip planner until AccountSync refreshes after dispatch (`_account_sync_generation` counter). BUG-3: log throttled (once per latch cycle), pure shifts skip re-latch.
    2. Cancel-first on extras: if `non_tp_extras > 0` (INV-9b: `diff_extra - diff_extra_tp`), only CANCEL actions pass (PLACEs deferred). TP extras are intentional.
    3. Budget pre-check: if `orders_remaining() <= 0` or PLACEs > budget, entire shift deferred (no partial execution)
  - Convergence = `non_tp_extras == 0` after fresh AccountSync (INV-9b: TP extras do not block)
  - Inflight latch: 30s timeout safety valve (clears sync-wait but cancel-only still applies if extras present). BUG-3: no re-latch for pure shifts after convergence clear.
  - `ExchangePort.orders_remaining() -> int | None`: clean public API (NoOp returns None = unlimited)
  - Env: `GRINDER_LIVE_CONVERGE_FIRST` (bool, default `True`, safe-by-default)
  - Logs: `GRID_SHIFT_DEFERRED`, `PLACEMENT_DEFERRED`, `INFLIGHT_GENERATION_TIMEOUT`, `ORDER_BUDGET_EXHAUSTED`, `ORDER_BUDGET_NEAR_EXHAUSTION`
  - Run #18 motivation: budget usage drops from 100+ (exhausted in 2min) to ~36 (stable grid)
- **Verification knobs** (PR-VERIF-KNOBS-1):
  - `GRINDER_LIVE_ADAPTIVE_SPACING_ENABLED` (bool, default True): disables NATR-driven spacing when False
  - `GRINDER_LIVE_MAX_LEVEL_DISTANCE_BPS` (int, default 0=None): cap max distance from mid; levels beyond are skipped (not clamped)
  - Wired into `LiveGridConfig.adaptive_enabled` and `LiveGridConfig.max_level_distance_bps`
  - Cap logic in `_build_desired_grid()`: reduces `desired_count` naturally
  - Backward-compatible: defaults preserve existing behavior
- **Rolling Infinite Grid** (ADR-085):
  - **Status:** V1F implemented -- anchor/initial placement contract (INV-10, ADR-088). Live verification pending.
  - **Spec:** `docs/26_ROLLING_INFINITE_GRID_SPEC.md`
  - **Problem:** Current mid-anchored grid rebuilds entirely on price movement (20 actions/shift, budget burn). Grid fills don't advance the ladder.
  - **Solution:** Rolling infinite ladder where grid fills shift `effective_center` by `+/- step_price`. Fill produces 2 grid actions (1 CANCEL + 1 PLACE, inner level reserved for TP) instead of full rebuild.
  - **Key invariants:** cardinality (N_buy + N_sell constant), spacing (uniform step_price), rolling shift (net_offset tracks fill direction), no global rebuild on fill, TP isolation (TP fills don't shift), TP slot exclusion (INV-9).
  - **State model:** `_RollingLadderState(anchor_price, step_price, net_offset)` -- volatile, re-anchor on restart.
  - **Feature flag:** `GRINDER_LIVE_ROLLING_GRID` (bool, default `False`, safe-by-default). Wired in engine.
  - **V1A (planner):** `_RollingLadderState`, `_build_rolling_grid()`, `_match_orders_by_price()`, `plan(rolling_mode=True)`. Planner-only, 18 tests.
  - **V1B (engine wiring):** `GRINDER_LIVE_ROLLING_GRID` env var gates rolling mode. Engine-owned fill detection (`_prev_rolling_orders`, `_rolling_pending_cancels`). Freeze/replenish/anti-churn bypassed. 17 engine integration tests (35 total).
  - **V1C (TP slot ownership, ADR-086, INV-9):** Fill-tick reservation prevents planner from placing grid at TP price on same tick.
    - **Live verification exposed cross-tick overlap** (run4): TP SELL@70843.30 + grid SELL@70843.40 coexisted 45 sync cycles. Root cause: engine filtered TP orders from planner's `open_orders` (PR-INV-3), so planner couldn't see or match TPs.
  - **V1D (cross-tick TP slot protection, ADR-086 update, INV-9b):** Two-layer fix:
    1. **Primary:** TP orders included in planner's `open_orders` (removed `is_tp_order` filter from engine). Planner matches TP to desired level → no grid PLACE at TP price. Planner skips CANCEL/REPLACE for TP orders (managed by cycle layer).
    2. **Secondary:** Reservation persists across ticks until age-out (`max_reservation_age=50`). Age-only clearance avoids multi-fill false-clear (Contract 2a). Defense-in-depth for REST-lag gap between fill detection and TP visibility.
    - **INV-9 formula:** `desired_grid_count = (N - sell_reservation) + (N - buy_reservation)` on fill ticks.
    - **Slot state model:** `{grid, tp, reserved, vacant}`. Ownership applies only to occupied slots.
    - **Convergence target:** `grid + tp = 2*N` (not absolute runtime invariant — transient deviation expected).
    - **Saturation:** reservation clamps at `min(fill_count, N)` per side. Excess TPs self-heal via TTL.
    - **Defense-in-depth:** `_filter_tp_grid_overlap()` in engine. Same-tick anomaly guard (expected fire count: ZERO). Uses `reduce_only` discriminator. Fail-open when config missing.
    - **`diff_extra_tp` (INV-9b follow-up):** `GridPlanResult.diff_extra_tp` counts TP orders in extras. Convergence guard uses `non_tp_extras = diff_extra - diff_extra_tp` — TP extras are intentional (not cancelled), only grid extras block PLACEs. T43 validates.
    - 22 slot ownership tests (T21-T42) + T43, 67 rolling grid tests total.
  - **V1E (convergence guard + cancel-spin fixes, BUG-3/BUG-4):** Live run8 exposed two operational bugs:
    - **BUG-3 (GRID_SHIFT_DEFERRED churn):** Inflight latch re-created on every sync cycle. Mechanism: PLACE → latch(gen=N) → sync → latch clears → planner SHIFT → new latch(gen=N+1) → repeat. Each latch blocks ~100 WS ticks. Fix: (a) log throttle — `GRID_SHIFT_DEFERRED` logged once per latch cycle, not every tick; (b) no re-latch for pure shifts (cancel_count >= place_count) after convergence clear. Net-new PLACEs still latch.
    - **BUG-4 (CANCEL_2011 spin):** Stale snapshot shows already-cancelled order → planner generates CANCEL → Binance returns -2011 → repeats every tick until sync refresh. Fix: `_cancel_failed_ids` blacklist. Failed CANCELs recorded; blacklisted order_ids skipped in action loop. Cleared on AccountSync refresh (`_account_sync_generation` increment).
    - **Logs:** `CANCEL_FAILED_IDS_CLEARED count={} gen={}` (on sync refresh). `BlockReason.CANCEL_ALREADY_FAILED` (SKIPPED status).
    - 4 BUG-3 tests (log throttle, reset after sync, pure shift no-relatch, net-new relatch) + 5 BUG-4 tests (populated, cleared on sync, persists without sync, error path, empty no-log).
  - **V1F (anchor/initial placement contract, ADR-088, INV-10):** Live runs (run9-run13) exposed grid asymmetry after external cleanup. Root cause: no re-anchor mechanism when exchange empties while engine is running.
    - **ANCHOR_RESET:** 5-condition contract (rolling_state_exists, no_grinder_orders, no_fresh_inflight_latch, position_flat, no_pending_cancels). Same-tick: `reset_rolling_state()` → `_plan_grid()` reinits from fresh `mid_price`.
    - **no_fresh_inflight_latch:** inflight blocks only while `account_sync_generation <= inflight.sync_gen`. Stale inflight does not block reset after sync refresh. Live verification exposed stranded inflight bug: `_apply_convergence_guards()` returned early on `actions=[]` without clearing stale inflight latch. Fix: (a) clear stale inflight on empty-actions path when sync has refreshed; (b) ANCHOR_RESET condition uses staleness check instead of presence check.
    - **Part A safety invariant:** `_apply_convergence_guards()` is only called from the live planner path (engine.py line 806), not the `budget_dead`/`grid_frozen` path. `actions=[]` in this context genuinely means "planner found no diff", not suppression.
    - **SSOT:** `anchor_price = snapshot.mid_price` (raw Decimal, not tick-rounded).
    - **Two-layer cleanup:** planner-owned (anchor, step, offset, reservations) + engine-owned (prev_orders, pending_cancels, throttle keys).
    - **BLOCKED throttle:** `ANCHOR_RESET_BLOCKED` logged once per reason per episode. Three blocked reasons: POSITION_OPEN, POSITION_UNKNOWN, PENDING_CANCELS.
    - **PLANNER_ACTIONS_SUMMARY:** rolling mode appends `ec=` and `anchor=` to log.
    - **ADR-089 log events:** `ROLLING_STEADY_STATE` (DEBUG, throttled 1/100 ticks, when actions=0 in rolling mode), `INFLIGHT_STALE_CLEARED` (INFO, when stale inflight cleared on convergence), `CANCEL_SKIP_ALREADY_FAILED` (DEBUG, when cancel skipped due to prior -2011). Unit-test-proven (18 tests). Not reachable in fixture mode; intended for live forensics. `INFLIGHT_STALE_CLEARED` is additionally **live-proven** (minimum ceremony 2026-03-15, sync_gen=2 > inflight_gen=1).
    - **ADR-089 operator tool:** `scripts/exchange_state.py` — check/cleanup/verify exchange state. Pre-flight and post-run canonical commands. Read-only commands (check/verify) bypass `ALLOW_MAINNET_TRADE` config gate via `setdefault`; `SafeMode.READ_ONLY` is the write guard.
    - **Runbook:** `docs/runbooks/34_ROLLING_LIVE_VERIFICATION.md` — canonical ceremony for rolling live verification.
    - **Minimum live acceptance: PASSED** (2026-03-15). Proven: ANCHOR_INIT, order placement (2x POST 200 OK), 0 errors, clean shutdown (300s, 3073 ticks), post-run status=CLEAN. Full rolling-path acceptance (ANCHOR_RESET, fill→TP, ROLLING_FILL_OFFSET) remains open.
    - 19 anchor contract tests (T44-T62), 95 rolling grid tests total.
  - **V1G (inflight fill detection, ADR-090, INV-11):** Live ceremony A2 (2026-03-15) exposed P0 safety bug: 13 BUY orders in 60s, 11 filled instantly, $1,571 accumulated with zero fills detected. Root cause: disappearance heuristic requires order to appear in at least one REST snapshot — orders filling within one sync interval (~5s) are structurally invisible.
    - **Inflight CID tracking:** dispatched PLACE CIDs tracked in `_inflight_placed_cids`. After sync refresh, CID absent from REST = inflight fill detected. CID present = survived, handed to disappearance heuristic.
    - **Reconciliation contract:** every dispatched PLACE increments `_unreconciled_place_count` once; first post-dispatch sync decrements exactly once, regardless of survival or inflight fill.
    - **Guard 4:** unreconciled placement cap (`levels * 2`). Defense-in-depth — fires only if both fill detection paths fail simultaneously.
    - **New log events:** `INFLIGHT_FILL_DETECTED` (INFO, operational path), `PLACEMENT_CAPPED` (WARNING, anomaly).
    - **ANCHOR_RESET cleanup:** `_inflight_placed_cids` filtered + `_unreconciled_place_count` popped alongside existing state cleanup.
    - 12 inflight fill detection tests (T63-T74), 116 rolling grid tests total.
  - **Fill detection:** Rolling fill = disappeared order with `strategy_id="d"` (grid) AND not in pending cancels. TP (`strategy_id="tp"`) and all non-grid strategies do NOT shift offset. Two detection paths:
    1. **Inflight CID reconciliation (ADR-090):** dispatched PLACE CID tracked in `_inflight_placed_cids`. First post-dispatch REST sync resolves fate: CID present = survived (handed to disappearance heuristic); CID absent = inflight fill (emits `INFLIGHT_FILL_DETECTED`). Closes the instant-fill blind spot.
    2. **Disappearance heuristic:** order in `_prev_rolling_orders` but not in current REST snapshot = fill. Covers fills that happen after the order appeared in at least one snapshot.
    - Known limitation: exchange-side non-trade cancels of grid orders (ADL, margin) treated as fills. Resets on restart.
  - **Unreconciled placement cap (ADR-090, Guard 4):** `_unreconciled_place_count[symbol][side]` tracks PLACEs awaiting reconciliation. Incremented on dispatch, decremented on first post-dispatch sync (both survived + filled outcomes). Cap = `levels * 2`. Fires `PLACEMENT_CAPPED` (WARNING) when threshold reached — defense-in-depth for broken fill detection.
  - **Obsoletes in rolling mode:** cycle-layer replenish, TP_FILL_REPLENISH, mid-driven GRID_SHIFT, anti-churn, grid freeze.
  - **Migration:** spec -> V1A planner -> V1B engine -> V1C slot ownership -> V1D cross-tick fix -> V1E convergence guards -> V1F anchor contract -> live verification -> cleanup.

- **AccountSync visibility instrumentation** (PR-P0-2):
  - `GRINDER_ACCOUNT_SYNC_DEBUG_OPEN_ORDERS=1` (default 0): one flag enables both raw logging + correlation
  - Port: logs raw openOrders response summary (count + symbols_sample + first 5 entries with clientOrderId/status/timeInForce)
  - Engine: correlates recent_places (last 20 successful PLACEs by sent clientOrderId + wall-clock ts) with open_orders; logs parsable_grinder_ids count for hypothesis narrowing
  - `grinder_port_cancel_unknown_total{port}` counter for -2011 errors (no symbol label)
  - CANCEL -2011 diagnostic log: order_id + parsed identity (before map_binance_error raises)
  - Pure helper `correlate_recent_places()` in `place_tracker.py` for testability
  - DRY: `_PORT_NAME = "futures"` class constant replaces hardcoded strings in BinanceFuturesPort
  - Debug logs emitted at WARNING level to be visible without `logging.basicConfig()` (historical — run_trading.py now has native logging config via ADR-089; WARNING promotion kept for backward compat when debug active)
  - Debug-only, default off, no behavioral changes
  - P0-2b: on missing orders, `GET /fapi/v1/order` by origClientOrderId (bounded by `GRINDER_ACCOUNT_SYNC_DEBUG_LOOKUP_LIMIT`, default 5, dedup per clientOrderId, set cap 100)
  - Logs: `ORDER_LOOKUP clientOrderId=... status=FILLED/CANCELED/...` or `ORDER_LOOKUP_NOT_FOUND clientOrderId=... code=-2013/-2011` or `ORDER_LOOKUP_ERROR clientOrderId=... status=429/...`
  - `OPEN_ORDERS_DROP prev_count=X now_count=0 recent=N` log when orders disappear between polls
  - Metric: `grinder_port_order_lookup_total{port,outcome}` (outcome: found/not_found/error)
  - Outcome classification: `not_found` only for Binance code -2013/-2011; all other failures = `error`
  - P0-2d: cancel source tagging - determine WHO cancels orders
  - `CANCEL_INTENT order_id=... symbol=... reason=...` log before each cancel execution (env-gated)
  - `CANCEL_OK order_id=...` log on successful cancel from Binance (env-gated)
  - Metric: `grinder_port_cancel_ok_total{port}` - successful cancel counter
  - `PLANNER_ACTIONS_SUMMARY` promoted to WARNING when debug active (visible without logging.basicConfig)
- **Grid freeze in position** (`GRINDER_LIVE_FREEZE_GRID_WHEN_IN_POSITION`, default `false`):
  - When symbol has non-zero position (`abs(qty) > 0`), grid planner is skipped — no GRID_SHIFT cancel/replace churn
  - Replenish actions from cycle layer are also filtered out when frozen
  - TP reduce-only actions (`TP_CLOSE`, `TP_EXPIRY`) still pass through — cycle layer stays alive
  - Log: `GRID_FREEZE_IN_POSITION symbol=X — skipping planner + replenish`
  - When position returns to zero, planner resumes normal grid actions
  - Root cause fix for order churn: diagnostic proved planner GRID_SHIFT cancels all orders within seconds of placement
- **Grid shift anti-churn** (`GRINDER_LIVE_GRID_SHIFT_MIN_MOVE_BPS`, default `0` = disabled):
  - Suppresses GRID_SHIFT (cancel+replace) actions when mid hasn't moved enough from anchor
  - Anchor = mid at last allowed GRID_SHIFT (or first grid placement)
  - GRID_FILL (missing orders) and GRID_TRIM (extra orders) always pass through
  - Log: `GRID_SHIFT_SUPPRESSED symbol=X move=Y bps < threshold=Z bps`
  - Recommended: `50` bps for mainnet (prevents sub-cent noise from triggering full grid rebuild)
  - **Unfreeze anchor reset** (PR-ANTI-CHURN-2): when position closes to 0 (freeze→unfreeze transition),
    anchor is cleared so planner can recenter grid immediately without waiting for 50bps move.
    One-shot: anchor re-set on first tick after unfreeze, then anti-churn resumes normally.
    Log: `GRID_UNFREEZE symbol=X — anchor reset, planner will recenter grid`
- **TP auto-renew on expiry** (`GRINDER_TP_RENEW_ENABLED`, default `false`):
  - When TP expires (TTL) and position is still open (pos_qty != 0), cancel old TP + place new TP at same price
  - Invariant: position open = TP must exist (auto-healed on expiry instead of abandoned)
  - Source price preserved: renew uses original `fill_price` formula, not current mid
  - 3 anti-churn guards: cooldown (`GRINDER_TP_RENEW_COOLDOWN_MS`, default 60s), inflight latch (per symbol), retry budget (`GRINDER_TP_RENEW_MAX_ATTEMPTS`, default 3 then degrade to plain cancel)
  - **Atomic renew** (PR-P0-TP-RENEW-ATOMIC): PLACE new TP first, CANCEL old TP second.
    If PLACE is blocked by gates (MAX_POSITION, FSM, etc.), CANCEL is skipped and old TP stays alive.
    Log: `TP_RENEW_CANCEL_SKIPPED symbol=X -- PLACE was blocked, keeping old TP alive`
  - **reduce_only intent** (PR-P0-REDUCEONLY-INTENT): `reduce_only=True` actions always classify
    as `REDUCE_RISK` (regardless of pos_sign). Bypasses Gate 5 (MAX_POSITION) and Gate 7 (FSM).
    P0 invariant: TP must always be placeable when position is open.
  - Metrics: `grinder_cycle_tp_renew_total{symbol,outcome}` (started/renewed/failed/cooldown/inflight), `grinder_tp_active_gauge{symbol}` (0/1)
  - Log: `TP_RENEW_STARTED`, `TP_RENEWED`, `TP_RENEW_FAILED`, `TP_RENEW_COOLDOWN`, `TP_RENEW_INFLIGHT`, `TP_RENEW_DEGRADED`
  - Safe-by-default: disabled unless explicitly enabled
  - Renew actions pass through grid freeze filter (only REPLENISH is blocked)
  - **Overalloc guard** (PR-P0-TP-RENEW-OVERALLOC-GUARD): prevents temporary reduceOnly over-allocation
    during renew when multiple TPs exist. Decision: if `tp_sum + new_qty > pos_abs` AND other TPs
    exist, use CANCEL-first order (safe — other TPs cover the gap). Single TP stays PLACE-first
    (PR-367 engine guard handles -2022 rejection). Fixes Run #16 bug where Binance auto-expired
    a different TP during renew over-allocation.
- **Replenish on TP fill** (`GRINDER_LIVE_REPLENISH_ON_TP_FILL`, default `false`):
  - Detects TP fill events via position magnitude decrease (prev > 0 → cur < prev, or short symmetric)
  - On TP fill (position still open): **inward** re-entry + **outward** opposite (PR-ROLL-3b)
  - LONG: BUY above highest_buy (inward), SELL above highest_sell (outward)
  - SHORT: SELL below lowest_sell (inward), BUY below lowest_buy (outward)
  - Guard: mid-cross -- skip inward order if price crosses mid (prevents overlap)
  - Anchors: stores lowest BUY / highest SELL per symbol when position is flat; uses anchors as fallback when no orders on a side
  - Uses grid identity (`strategy_id="d"`, `level_id=0`), not TP namespace
  - Reason: `TP_FILL_REPLENISH` (distinct from `REPLENISH`; passes through grid freeze filter)
  - Safe-by-default: disabled unless explicitly enabled
  - No replenish when position fully closed (pos_qty == 0) or when no anchors available
  - Log: `TP_FILL_REPLENISH symbol=X pos_qty=Y buy=Z sell=W`
- **Reduce-only enforcement** (`GRINDER_LIVE_REDUCE_ONLY_ENFORCEMENT`, default `true`):
  - When enabled and position open, opposite-side PLACE/REPLACE orders forced `reduce_only=True`
  - LONG -> all SELL `reduce_only=True`, SHORT -> all BUY `reduce_only=True`
  - Flat/unknown position -> no enforcement (fail-open)
  - Prevents unintended position reversal (e.g. grid SELL opening SHORT after TP closes LONG)
  - Set `GRINDER_LIVE_REDUCE_ONLY_ENFORCEMENT=0` for two-sided MM mode (grid can open/reverse)
  - TP orders always keep `reduce_only=True` regardless of this flag (set by CycleLayer)
  - Metric: `grinder_live_reduce_only_enforced_total{sym,side,reason}`
  - Log: `REDUCE_ONLY_ENFORCED` (warning level, only when flag actually changed)
- **Partial TP qty mode** (`GRINDER_TP_QTY_MODE`, default `full`):
  - Modes: `full` (fill qty, existing behavior), `one_level` (per_level_qty), `pct` (pos_qty * pct / 100)
  - `GRINDER_TP_QTY_PCT=50` — percentage of position for pct mode (int 1-100, default 100)
  - Invariants: tp_qty <= abs(pos_qty), tp_qty >= step_size, rounded to step_size (ROUND_DOWN)
  - When partial qty < step_size: tp_qty=0, TP skipped (metric: `tp_qty_too_small`, log: `TP_SKIPPED_QTY_TOO_SMALL`)
  - Fallback to full only for missing config (per_level_qty=None, pos_qty=None)
  - TP renew preserves original qty from exchange order (no recalculation)
  - All arithmetic Decimal-only (no floats)
- **Order budget exhaustion latch** (automatic, no env var):
  - When port returns "Order count limit reached", latch activates: `ORDER_BUDGET_LATCH`
  - Planner suppressed for remaining run (no new PLACE/CANCEL generation)
  - Eliminates 80k+ spam lines when budget exhausted
  - Log: `ORDER_BUDGET_EXHAUSTED symbol=X — planner suppressed`

## Partially implemented
- Package structure `src/grinder/*` (core, protocols/interfaces) -- scaffolding.
- Documentation in `docs/*` -- SSOT for architecture/specs (must match implementation).

## Known gaps / mismatches

> **See also:** [`docs/GAPS.md`](GAPS.md) — SSOT index of spec-vs-code gaps with status and tracking.

- **End-to-end strategy loop** (signal -> plan -> execute continuously) not yet formalized as a production rollout procedure. Execution pipeline verified (Stage D/E mainnet cancel_all/flatten), but continuous autonomous loop is not yet opped.
- **Backtest in Docker fails:** Running `scripts/run_backtest.py` inside a Docker container fails with `ModuleNotFoundError: No module named 'scripts'`. Fixture determinism checks pass; CI passes because it runs in a properly configured Python environment.
- Adaptive Grid Controller v1+ (EMA-based adaptive step, trend detection, DRAWDOWN mode, auto-reset) -- **not implemented**; see `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md` (Planned). Controller v0 implemented with rule-based modes (see ADR-011).
- **Binance API integration:** Execution HTTP endpoints verified (Stage D/E mainnet). Market data WS connector wired (LC-21). Full autonomous market data loop -- TBD.

## Process / governance
- PR template with mandatory `## Proof` section.
- CI guard (`pr_body_guard.yml`) blocks PRs without Proof Bundle.
- CLAUDE.md + DECISIONS.md + STATE.md -- governance docs.

## Planned next
- [DONE] Single-venue launch readiness (Launch-01: runbook 21, smoke_launch_readiness.sh, CI job).
- [DONE] ACTIVE enablement ceremony (Launch-02: runbook 22, code-verified env vars with Source: annotations).
- [DONE] Gap triage: GAPS.md updated with Priority/Owner/Exit criteria columns (PR #175).
- [DONE] Data quality v0 (Launch-03 PR1): GapDetector + OutlierFilter + 3 Prometheus counters (detect-only).
- [DONE] Data quality wiring (Launch-03 PR2): DataQualityEngine wired into LiveFeed._process_snapshot (dq_enabled gated, metrics-only).
- [DONE] Data quality gating (Launch-03 PR3): dq_blocking flag + 3 block reasons (stale/gap/outlier) in remediation gate; DataQualityVerdict; safe-by-default.
- [DONE] Data quality alerts + triage (Launch-04): 4 DQ alert rules (stale/gap/outlier/blocking); triage runbook 23; alert rules validator.
- [DONE] HTTP retry policy + latency metrics (Launch-05 PR1): HttpRetryPolicy, DeadlinePolicy (per-op budgets), MeasuredHttpClient (httpx wrapper), HTTP metrics (requests/retries/fail counters + latency_ms histogram). Safe-by-default (retries disabled, enabled=False). No behavior change.
- [DONE] HTTP measured client wiring (Launch-05 PR2): MeasuredSyncHttpClient wrapping sync HttpClient; op= annotations on all 12 BinanceFuturesPort call sites; env config (LATENCY_RETRY_ENABLED, HTTP_MAX_ATTEMPTS_*, HTTP_DEADLINE_*_MS); safe-by-default (disabled pass-through).
- [DONE] HTTP latency/retry alerts + triage (Launch-05 PR3): 4 alert rules (2 page + 2 ticket); triage runbook 24; validator update (op= allowlist + forbidden labels). Zero changes to src/.
- [DONE] Latency/retry enablement ceremony (Launch-05b): Runbook 25 — operator procedure for safely enabling LATENCY_RETRY_ENABLED=1 with conservative config, observation window, rollback, evidence bundle. Zero changes to src/.
- [DONE] HTTP probe loop (Launch-05c): Shared measured-client factory + HTTP probe in run_live.py for observable grinder_http_* metrics. No API keys required (public endpoints only). Enables RB25 in STAGING.
- [DONE] FillTracker MVP (Launch-06 PR1): In-memory FillTracker + FillMetrics (counters with source/side/liquidity labels) + metrics contract + RB26 triage runbook. Detect-only scaffold; no execution wiring.
- [DONE] FillTracker wiring (Launch-06 PR2): Wired into reconcile loop via `FILL_INGEST_ENABLED=1`. Reads `userTrades` from Binance, ingests into FillTracker, pushes to FillMetrics. Persistent cursor file (`FILL_CURSOR_PATH`). Still detect-only (read-only, no place/cancel). `OP_GET_USER_TRADES` added to ops taxonomy.
- [DONE] Fill health alerts + enablement (Launch-06 PR3): 5 health metrics (ingest polls, enabled gauge, ingest errors, cursor load/save) + 5 alert rules (FillIngestDisabled, FillIngestNoPolls, FillCursorSaveErrors, FillParseErrors, FillIngestHttpErrors). RB26 updated with Gate 0/1/2 + rollback procedure. Smoke script for local validation.
- [DONE] Staging dry-run validation (Launch-06 PR4): Staging smoke script (3-gate: OFF/ON/restart-persistence) for real Binance reads. RB26 expanded with safety invariants, quiet market semantics, companion scripts. Zero src/ changes.
- [DONE] Artifact assertions + write-op grep hardening (Launch-06 PR5): metrics-out artifact must contain grinder_fill_* lines (automated). RB26 write-op grep expanded with diff-level + repo-level patterns. Zero src/ changes.
- [DONE] Cursor stuck detection + monotonicity (Launch-06 PR6): `cursor_last_save_ts` / `cursor_age_seconds` gauges for stuck detection. Monotonicity guard rejects backward cursor writes using tuple `(trade_id, ts_ms)`. Cursor saved every poll (not just on new trades) — prevents false-positive FillCursorStuck in quiet markets. 2 new alerts (FillCursorStuck, FillCursorNonMonotonicRejected). RB26 updated with NoPolls/QuietMarket/CursorStuck triage table + thresholds. 8 contract patterns. Zero write-op changes.
- [DONE] Launch-13 (P1): Centralized FSM Orchestrator — COMPLETE (main @ `7793045`).
  - PR0 (#211) — Spec/ADR (SSOT wiring)
  - PR1 (#213) — Pure FSM core + 74 deterministic tests (merged @ `897ca8b`)
  - PR2 (#214) — Driver + metrics + Gate 7 (merged @ `89e329a`)
  - PR3 (#215) — Real loop wiring + runtime signals (merged @ `232d07b`)
  - PR4 (#216) — Operator override normalization + runbook (merged @ `6c37baf`)
  - PR5 (#217) — Deterministic evidence artifacts (merged @ `7793045`)
  - PR-A1: Wire real FSM inputs (feed_stale + toxicity_level):
    - feed_stale: per-symbol snapshot gap detection (GRINDER_FEED_STALE_MS, default 5000ms)
    - toxicity_level: ToxicityGate mapping (SPREAD_SPIKE→MID, PRICE_IMPACT_HIGH→HIGH)
    - run_trading.py: FSM+DrawdownGuard+ToxicityGate (GRINDER_FSM_ENABLED, default false)
    - Deferred: OrchestratorInputs type alignment (bool/enum → float/int) → PR-A2
  - PR-A2a: OrchestratorInputs numeric type alignment:
    - feed_stale: bool → feed_gap_ms: int (FSM owns threshold via FsmConfig)
    - toxicity_level: str → spread_bps: float + toxicity_score_bps: float
    - Thresholds moved from engine to FsmConfig (feed_stale_threshold_ms, spread_spike_threshold_bps, toxicity_high_threshold_bps)
    - ToxicityGate.price_impact_bps() typed method (no dict-fishing)
    - Evidence v1 backward compat: render_evidence_text() maps numerics to v1 names via FsmConfig
  - PR-A2b: fsm_evidence_v2 (BREAKING CHANGE):
    - ARTIFACT_VERSION bumped: fsm_evidence_v1 → fsm_evidence_v2
    - Removed v1 signal fields: feed_stale (bool), toxicity_level (str "LOW"/"MID"/"HIGH")
    - Added v2 signal fields: feed_gap_ms (int), spread_bps (float), toxicity_score_bps (float)
    - Unchanged fields (at PR-A2b): kill_switch_active, drawdown_breached, operator_override
    - Note: drawdown_breached replaced by drawdown_pct (PR-A3), position_reduced replaced by position_notional_usd (PR-A4)
    - Migration: any parser matching feed_stale= or toxicity_level= must switch to feed_gap_ms=/spread_bps=/toxicity_score_bps=
    - CANON_TEXT + CANON_SHA256 updated in test_fsm_evidence.py
    - Deferred: drawdown_pct (PR-A3), position_notional (PR-A4)
    - Dropped: regime (PR-A5) — no FSM consumer; regime lives in policy artifacts (GridPlan.regime)
  - PR-A3: drawdown_breached → drawdown_pct (numeric DD fraction):
    - OrchestratorInputs: drawdown_breached: bool → drawdown_pct: float (0.20 = 20%)
    - FsmConfig: added drawdown_threshold_pct: float = 0.20 (was DrawdownGuardV1Config default)
    - FSM helper _is_dd_breached(inp) owns threshold decision (>= comparison)
    - DrawdownGuardV1: added current_drawdown_pct property (float accessor)
    - Engine: passes guard.current_drawdown_pct instead of guard.is_drawdown
    - Evidence v2 signal: drawdown_breached → drawdown_pct (CANON_TEXT + SHA256 updated)
    - Dropped: regime (PR-A5) — no FSM consumer; see GridPlan.regime for policy-level regime
  - PR-A4: position_reduced → position_notional_usd (live position notional):
    - OrchestratorInputs: position_reduced: bool → position_notional_usd: float | None (Σ|qty|×mark_price USDT; None = unknown)
    - FsmConfig: added position_notional_threshold_usd: float = 10.0 (recovery threshold, not exchange MIN_NOTIONAL)
    - FSM helper _is_position_large(inp): None or >= threshold → True (blocks EMERGENCY recovery)
    - Source: AccountSyncer only (pure live measurement, no override/latch)
    - None = unknown → conservatively blocks recovery (safe default)
    - Evidence v2 signal: position_reduced → position_notional_usd (CANON_TEXT + SHA256 updated)
  - A-series COMPLETE (PR-A1 → A2a → A2b → A3 → A4). PR-A5 (regime) dropped: no FSM consumer.
    Regime is policy-internal (GridPlan.regime via classify_regime). Future FSM regime-aware
    transitions require separate ADR + spec + tests.
- [DONE] Launch-14 (P1): SmartOrderRouter (existing=None scope) — COMPLETE (main @ `e5b177c`).
  - PR0 (#219) — Spec/decision matrix + invariants (merged @ `8ff7339`)
  - PR1 (#220) — Router core + table-driven tests (merged @ `d98008d`)
  - PR2 (#221) — LiveEngine wiring + SOR metrics (merged @ `045e5c7`)
  - PR3 (#222) — Fire drill + evidence + runbook (merged @ `e5b177c`)
  - Note: AMEND deferred (requires order state tracking, not in scope for existing=None).
- [DONE] Launch-15 (P1): AccountSyncer (Positions + Open Orders) — COMPLETE (main @ `ac3cc36`).
  - PR0 (#224) — Spec/SSOT (merged @ `05662a6`)
  - PR1 (#225) — Core contracts + render + metrics (merged @ `754da32`)
  - PR2 (#226) — Port wiring + syncer + mismatch detection + evidence (merged @ `1e64c24`)
  - PR3 (#227) — Fire drill + evidence + runbook + ops entrypoint (merged @ `ac3cc36`)
  - PR4 (#329) — ExchangePort protocol: add `fetch_account_snapshot()` (merged @ `2e5ed7f`)
  - PR5 (#330) — Sync interval throttle: 5s minimum via `snapshot.ts` (merged @ `420e12f`)
  - PR6 (#331) — BinanceFuturesPort: real `fetch_account_snapshot()`, 2 REST calls (merged @ `d7b778f`)
- [DONE] P2 triage PR1: alert coverage for Launch-13/14/15 metrics + runbook wiring. 7 alert rules (FsmBadStateTooLong, FsmActionBlockedSpike, SorBlockedSpike, SorNoopSpike, AccountSyncStale, AccountSyncErrors, AccountSyncMismatchSpike) + triage wiring in RB02. Zero src/ changes.
- [DONE] P2 triage PR3: env parsing unified. New `src/grinder/env_parse.py` SSOT with parse_bool/parse_int/parse_csv/parse_enum. Migrated 5 triage-flow call sites (fsm_evidence, account evidence, live engine SOR/sync/override, reconcile_loop). strict=True raises ConfigError, strict=False warns+default.
- [DONE] P2 triage PR2: observability quick panels + triage discoverability (Launch-13/14/15). Added FSM/SOR/AccountSync panel definitions to OBSERVABILITY_STACK.md + consolidated "Observability Quick Check" in RB02. Zero src/ changes.
- [DONE] Track C PR-C1: Fill Dataset v1. New `src/grinder/ml/fill_dataset.py` with FillOutcomeRow (21 fields), RoundtripTracker, build_fill_dataset_v1(). CLI: `scripts/build_fill_dataset_v1.py`. Artifact: `ml/datasets/fill_outcomes/v1/data.parquet + manifest.json`. 24 tests. ADR-068.
- [DONE] Track C PR-C2: Fill Probability Model v0. New `src/grinder/ml/fill_model_v0.py` — pure-Python calibrated bin-count model. FillModelFeaturesV0 (4 entry-side features, no leakage). FillModelV0.train/predict/save/load. CLI: `scripts/train_fill_model_v0.py`. Artifact: `model.json + manifest.json`. 12 tests. ADR-069.
- [DONE] Track C PR-C3: Consecutive Loss Guard v1. New `src/grinder/risk/consecutive_loss_guard.py` — pure-logic state machine (library only, not wired to live pipeline). ConsecutiveLossConfig (enabled, threshold, action). update(outcome) tracks streaks, trips at threshold. 24 tests. ADR-070. Alerts/runbook/observability deferred to PR-C3b (wiring).
- [DONE] Track C PR-C3b: Wire ConsecutiveLossGuard into live pipeline. New `src/grinder/risk/consecutive_loss_wiring.py` — ConsecutiveLossService wired into `scripts/run_live_reconcile.py` (only). PAUSE-only action via GRINDER_OPERATOR_OVERRIDE. Metrics: `grinder_risk_consecutive_losses` (gauge), `grinder_risk_consecutive_loss_trips_total` (counter). 2 alert rules, runbook, observability panel. 19 tests. ADR-070 updated. Remaining: per-symbol tracking, persistent state.
- [DONE] Track C PR-C3c: Per-symbol guards + persistent state. Per-symbol independent streak tracking (`dict[symbol, ConsecutiveLossGuard]`). Persistent state via JSON + sha256 sidecar (`GRINDER_CONSEC_LOSS_STATE_PATH`). Monotonicity guard, strict `from_dict()` validation. Metrics: count=max across symbols, trips=sum. RoundtripTracker NOT persisted (limitation). 16 new tests (35 total in wiring). ADR-070 updated. Remaining: DEGRADED action channel, RoundtripTracker persistence, FillModelV0 integration.
- [DONE] Track C PR-C3d: RoundtripTracker persistence + restart recovery. `to_state_dict()`/`from_state_dict()` on RoundtripTracker (Decimal->str, strict validation). State file v1->v2 with backward compat. Entry before restart + exit after restart = closed roundtrip. 9 new tests (3 tracker + 6 wiring, 41 total wiring). ADR-070 updated. Remaining: DEGRADED action channel, FillModelV0 integration.
- [DONE] Track C PR-C4a: FillModelV0 shadow loader + metrics. New `src/grinder/ml/fill_model_loader.py` — loader with SHA256 verify, fail-open, online feature extraction. Shadow integration in `scripts/run_live_reconcile.py` (env-gated via `GRINDER_FILL_MODEL_ENABLED`, NO decision impact). Metrics: `grinder_ml_fill_prob_bps_last` (gauge), `grinder_ml_fill_model_loaded` (gauge). 14 tests. Remaining: SOR enforcement gate (PR-C5), evidence artifacts (PR-C4b).
- [DONE] Track C PR-C5: Fill probability SOR enforcement gate. New `src/grinder/execution/fill_prob_gate.py` — pure gate function `check_fill_prob()` with ALLOW/BLOCK/SHADOW verdicts. Wired as Gate 8 in `LiveEngineV0._process_action()` (PLACE/REPLACE only). Env vars: `GRINDER_FILL_MODEL_ENFORCE` (default off), `GRINDER_FILL_PROB_MIN_BPS` (default 2500). Fail-open when model=None. Metrics: `grinder_router_fill_prob_blocks_total` (counter), `grinder_router_fill_prob_enforce_enabled` (gauge). ADR-071. Remaining: level filtering (PR-C5b), evidence artifacts (PR-C4b), calibration (PR-C6).
- [DONE] Track C PR-C6: Fill probability evidence artifacts. New `src/grinder/execution/fill_prob_evidence.py` — env-gated evidence writer for BLOCK/SHADOW events. JSON + sha256 sidecar in `{GRINDER_ARTIFACT_DIR}/fill_prob/`. Structured log `FILL_PROB_EVIDENCE` always on BLOCK, on SHADOW only when evidence env ON. Payload: verdict, prob_bps, threshold_bps, enforce, features, action metadata, model metadata. Atomic writes, deterministic format, fail-open. 20 tests. ADR-071a.
- [DONE] Track C PR-C7: FillModelV0 threshold calibration & offline evaluation. New `src/grinder/ml/fill_model_eval.py` — evaluation library with threshold sweep (101 points, 0..10000 bps), cost-weighted scoring (`TP + cost_ratio * TN`, default ratio 2.0), calibration diagnostics (per-bin predicted vs actual, max error, well-calibrated flag). CLI: `scripts/eval_fill_model_v0.py`. Artifact: `eval_report.json + manifest.json` (deterministic, sha256 sidecar). 23 tests. ADR-072.
- [DONE] Track C PR-C8: Controlled rollout for fill probability enforcement. Three deliverables: (1) `FillProbCircuitBreaker` in `fill_prob_gate.py` — rolling-window block rate tracker, trips when >50% blocks in 5-min window, fail-open, shadow-safe (zero overhead in shadow mode). Wired into `LiveEngineV0._check_fill_prob()` — CB check before gate, bypass on trip. Metric: `grinder_router_fill_prob_cb_trips_total`. Alert: `FillProbCircuitBreakerTripped`. (2) `scripts/preflight_fill_prob.py` — validates model, eval report, calibration, threshold match, evidence artifacts before enforcement enablement. (3) Runbook `docs/runbooks/31_FILL_PROB_ROLLOUT.md` — operator ceremony for enablement, monitoring, rollback. ADR-073.
- [DONE] Track C PR-C9: Auto-threshold from eval report. New `src/grinder/ml/threshold_resolver.py` — reads recommended_threshold_bps from eval report with 3-layer validation (sha256, schema_version, model provenance). Two modes: recommend-only (default, log+metric) and auto-apply (opt-in via GRINDER_FILL_PROB_AUTO_THRESHOLD=1). Fail-open. Metric: `grinder_router_fill_prob_auto_threshold_bps` (gauge). Evidence artifact: `threshold_resolution_{ts}.json` gated on GRINDER_ARTIFACT_DIR. Preflight: `--auto-threshold` flag. ADR-074.
- [DONE] Track C PR-A1: Auto-threshold production ceremony. Runbook 31 updated with phased rollout (Phase 0 preconditions → Phase 1 recommend-only 24-48h → Phase 2 go/no-go → Phase 3 auto-apply → rollback). Env var reference table, monitoring checklist, gauge interpretation guide. Preflight now always prints threshold summary (configured/recommended/effective/mode).
- [DONE] Track C PR-B1: Hardening eval_report sanity. `resolve_threshold_result()` returns `ResolveResult` with stable reason_codes. Old `resolve_threshold()` preserved (backward compat). Opt-in freshness gate via `GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS` (**behavioral control**, ADR-074a). Unified log events `FILL_PROB_THRESHOLD_RESOLUTION_FAILED`/`OK` in engine (resolver is pure, no logging). Preflight shows `reason_code` on failure.
- [DONE] Track C PR-C1: Canary by instance runbook section. Docs-only. Runbook 31 updated with canary-vs-control topology (disjoint symbol sets, separate `GRINDER_ARTIFACT_DIR`), CLI and docker-compose launch examples, side-by-side monitoring commands, go/no-go criteria, promote workflow, and rollback checklist. No code changes.
- [DONE] Track C PR-C2: Canary by symbol allowlist. `GRINDER_FILL_PROB_ENFORCE_SYMBOLS` env var (CSV, uppercase-normalized, parsed via `parse_csv` SSOT). When set, only listed symbols go through fill-prob gate; others bypass (ALLOW). `frozenset` in engine, checked in `_check_fill_prob()` before gate. Gauge `grinder_router_fill_prob_enforce_allowlist_enabled`. 8 tests. Runbook 31 updated with allowlist section.
- [DONE] Track C PR-C3: Runbook 31 decision tree + triage table for canary (instance vs allowlist). Docs-only. Decision tree for choosing canary strategy, migration checklist (instance→allowlist), failure modes table with 9 symptom→cause→action rows.
- [DONE] PR-R1: Mainnet rollout ceremony for fill-prob enforcement. New Runbook 32 with phased ceremony (Phase 0 preconditions → Phase 1 shadow baseline 24-48h → Phase 2 canary allowlist → Phase 3 expand → Phase 4 full enforcement → Phase 5 optional auto-apply). Rollback playbook (R1 emergency, R2 step-down, R3 auto-threshold). Cross-references from Runbook 31, HOW_TO_OPERATE.md, runbooks/README.md.
- [DONE] PR-C4: Engine init gauge + trading loop gap documentation. New gauge `grinder_live_engine_initialized` (0/1) in SorMetrics, set at end of `LiveEngineV0.__init__`. Added to metrics contract. Documented production trading loop gap in GAPS.md (PLANNED, P2): `run_live.py` is observability shell only, no code path instantiates engine. Runbook 32 updated with runtime precondition note.
- [DONE] PR-P2-LOOP-0: Minimal trading loop entrypoint (`scripts/run_trading.py`). Wires LiveConnectorV0 → LiveEngineV0.process_snapshot(). Modes: read_only/paper/live_trade (affect gating only; NoOpExchangePort = no real orders). Safety: paper/live_trade require GRINDER_TRADING_LOOP_ACK=YES_I_KNOW. `grinder_live_engine_initialized`=1 after startup. /readyz=200 after engine+connector ready. Fixture mode via --fixture (JSONL).
- [DONE] OBS-4: Alert contract enforcement in `scripts/verify_alert_rules.py`. Checks 6-10 added: required labels (component, category), enum validation (severity/component/category/dashboard_uid), dashboard_uid required for critical/page, Go-template prohibition in constant fields. 19 new tests (53 total). Real `alert_rules.yml` golden-positive test. Zero src/ changes.
- Expand tests to >50% coverage.
- Adaptive Controller v1 (EMA-based adaptive step, trend detection, DRAWDOWN mode).
- ~~Live Connector v1~~ [DONE] Done (LC-21: stream_ticks wired to BinanceWsConnector).
- (Deferred) Multi-venue M9 -- post-launch (see ADR-066).

## Smart Grid Spec Version

| Spec | Location | Status | Proof Anchor |
|------|----------|--------|--------------|
| v1.0 | `docs/smart_grid/SPEC_V1_0.md` | [DONE] Implemented | `sample_day`, `sample_day_allowed` fixtures; ADR-019..021 |
| v1.1 | `docs/smart_grid/SPEC_V1_1.md` | [DONE] Implemented | FeatureEngine in `sample_day_adaptive`; ADR-019 |
| v1.2 | `docs/smart_grid/SPEC_V1_2.md` | [DONE] Implemented | `sample_day_adaptive` digest `1b8af993a8435ee6`; ADR-022 |
| v1.3 | `docs/smart_grid/SPEC_V1_3.md` | [DONE] Implemented | `sample_day_topk_v1` digest `5b73848493cf2cf9`; ADR-023 |
| v2.0 | `docs/smart_grid/SPEC_V2_0.md` | [DONE] Implemented | M7-03..M7-09 code+ADRs+fixtures (PR #137) |
| v3.0 | `docs/smart_grid/SPEC_V3_0.md` | [PLANNED] Planned | -- |

**Verification:** `python3 -m scripts.verify_determinism_suite` (11/11 fixtures PASS)
**Current target:** `docs/smart_grid/SPEC_V1_3.md`
**Roadmap:** `docs/smart_grid/ROADMAP.md`

---

## Planned (spec exists, not implemented)

### Adaptive Smart Grid v2.0+ (`docs/smart_grid/SPEC_V2_0.md`)
Comprehensive adaptive grid system design:
- **Regime-driven behavior:** RANGE / TREND / VOL_SHOCK / THIN_BOOK / TOXIC / PAUSED / EMERGENCY
- **Auto-sizing:** dynamic step, width, levels, and size schedule from market features
- **L1/L2-aware:** microstructure features from L1 (spread, imbalance) and optional L2 (depth, impact)
- **Risk budgeting:** DD budgets, inventory caps, leverage caps, auto-allocation
- **Top-K 3-5:** symbol selection for tradable chop + safe liquidity
- **Deterministic:** fixture-based testing with stable digests
- **Cross-reference:** See `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md` for meta-controller contracts (regime, step, reset)
- **Partial implementation status:**
  - [DONE] **Feature Engine v1 (ASM-P1-01):** Mid-bar OHLC + ATR/NATR + L1 microstructure (see ADR-019)
  - [DONE] **PaperEngine integration (ASM-P1-02):** Integrated, features NOT in digest (backward compat)
  - [DONE] **Policy receives features (ASM-P1-03):** Plumbing complete -- features passed to policy when enabled, StaticGridPolicy ignores (backward compat), digests unchanged (see ADR-020)
  - [DONE] **Regime classifier (ASM-P1-04):** Deterministic precedence-based classifier -- EMERGENCY > TOXIC > THIN_BOOK > VOL_SHOCK > TREND > RANGE (see ADR-021)
  - [DONE] **AdaptiveGridPolicy v1 (ASM-P1-05):** Dynamic grid sizing from NATR + regime (see ADR-022)
    - **Opt-in:** `adaptive_policy_enabled=False` default (backward compat with existing digests)
    - **L1-only:** Uses natr_bps, spread_bps, thin_l1, range_score from FeatureEngine
    - **Adapts:** step_bps (volatility-scaled), width_bps (X_stress model), levels (ceil(width/step))
    - **Formulas:** step=max(5, 0.3*NATR*regime_mult), width=clamp(2.0*NATR*sqrt(H/TF), 20, 500), levels=clamp(ceil(width/step), 2, 20)
    - **Regime multipliers:** RANGE=1.0, VOL_SHOCK=1.5, THIN_BOOK=2.0, TREND asymmetric (1.3x on against-trend side)
    - **Units:** All thresholds/multipliers as integer bps (x100 scale) for determinism
    - **Auto-sizing:** Available via ASM-P2-01 (opt-in `auto_sizing_enabled=True`)
    - **L2 gating (M7-03):** Optional L2-based entry blocking (opt-in `l2_gating_enabled=False` default)
      - Insufficient depth: blocks entries when order book depth exhausted
      - Impact threshold: blocks entries when VWAP slippage >= `l2_impact_threshold_bps` (default 200)
      - See ADR-057
    - **DD budget ratio (M7-04):** Optional DD budget ratio application (policy-level, single-symbol)
      - `dd_budget_ratio: Decimal | None` parameter in `evaluate()`
      - ratio=0: blocks all new entries (reduce-only allowed)
      - 0 < ratio < 1: scales size_schedule by ratio
      - ratio=1 or None: no scaling (v1 behavior)
      - See ADR-058
    - **Qty constraints (M7-05):** Execution-layer qty rounding with stepSize/minQty
      - `SymbolConstraints(step_size, min_qty)` per-symbol configuration
      - `floor_to_step()`: deterministic floor rounding to lot size
      - Orders with rounded_qty < min_qty: skipped with `ORDER_SKIPPED` event
      - Reason code: `EXEC_QTY_BELOW_MIN_QTY`
      - See ADR-059
    - **ConstraintProvider (M7-06):** Automated loading from Binance exchangeInfo
      - `ConstraintProvider` class with cache + API fetch
      - `load_constraints_from_file()` for offline mode
      - `scripts/fetch_exchange_info.py` CLI for ops
      - LOT_SIZE filter parsing for stepSize/minQty
      - Cache location: `var/cache/exchange_info_futures.json`
      - See ADR-060
    - **ExecutionEngineConfig (M7-07):** Wiring ConstraintProvider with explicit enablement
      - `ExecutionEngineConfig(constraints_enabled=False)` -- default OFF for safety
      - `constraint_provider` parameter for lazy loading
      - Symbol constraints only applied when `constraints_enabled=True`
      - Backward compatible: existing code unchanged
      - See ADR-061
    - **Execution L2 Guard (M7-09):** Last-mile L2 safety checks at execution layer
      - `ExecutionEngineConfig.l2_execution_guard_enabled=False` -- default OFF for safety
      - `l2_features: dict[str, L2FeatureSnapshot]` input for L2 data
      - Guards: staleness (`EXEC_L2_STALE`), insufficient depth (`EXEC_L2_INSUFFICIENT_DEPTH_*`), high impact (`EXEC_L2_IMPACT_*_HIGH`)
      - Runs BEFORE qty constraints, only on PLACE actions
      - Pass-through when features missing (safe rollout)
      - See ADR-062
    - **ConstraintProvider TTL/Refresh (M7-08):** TTL and controlled refresh for exchangeInfo cache
      - `ConstraintProviderConfig.cache_ttl_seconds=86400` -- 24h default TTL
      - `allow_fetch=True/False` -- gate for API fetch (False in offline/replay)
      - Fallback chain: fresh cache -> API -> stale cache -> empty dict
      - No I/O at init (lazy loading preserves determinism)
      - See ADR-063
    - **Fixture:** `sample_day_adaptive` -- paper digest `1b8af993a8435ee6`
  - [DONE] **Top-K v1 (ASM-P1-06):** Feature-based symbol selection (see ADR-023)
    - **Opt-in:** `topk_v1_enabled=False` default (backward compat with existing digests)
    - **Requires:** `feature_engine_enabled=True` (needs FeatureEngine for range_score, spread_bps, thin_l1, net_return_bps)
    - **Scoring:** `score = range + liquidity - toxicity_penalty - trend_penalty`
    - **Hard gates:** TOXICITY_BLOCKED, SPREAD_TOO_WIDE, THIN_BOOK, WARMUP_INSUFFICIENT
    - **Tie-breaking:** Deterministic by `(-score, symbol)` for stable ordering
    - **Config:** `TopKConfigV1(k=3, spread_max_bps=100, thin_l1_min=1.0, warmup_min=10)`
    - **Output:** `topk_v1_selected_symbols`, `topk_v1_scores`, `topk_v1_gate_excluded`
    - **Fixture:** `sample_day_topk_v1` -- 6 symbols, paper digest `5b73848493cf2cf9`
    - **NOT included:** real-time re-selection (selects once after warmup), adaptive scoring weights

### ML Integration (`docs/12_ML_SPEC.md`)
- **Spec:** `docs/12_ML_SPEC.md` -- SSOT for ML contracts
- **Code:** `src/grinder/ml/` -- MlSignalSnapshot, ONNX inference, training pipeline, registry, feature store
- **Status:** M8 complete (PR #134-#170)
- **Progress:**
  - [DONE] **M8-00 (docs-only):** ML Specification with I/O contracts
    - Input: `FeatureSnapshot` (L1+volatility) + `L2FeatureSnapshot` (order book)
    - Output: `MlSignalSnapshot` (regime probabilities, spacing multiplier)
    - Determinism: 8 MUST / 7 MUST NOT invariants
    - Artifacts: `manifest.json` + SHA256 checksums
    - Enablement: `ml_enabled=False` default (safe rollout)
    - See ADR-064
  - [DONE] **M8-01 (stub):** MlSignalSnapshot contract + time-indexed signal selection
    - `MlSignalSnapshot` dataclass in `src/grinder/ml/__init__.py` (PR #140)
    - Time-indexed lookup: `_get_ml_signal(symbol, ts_ms)` with bisect O(log n) (PR #141)
    - Safe-by-default: `ml_enabled=True` + no signal.json = baseline digest
    - SSOT rule: max(signal.ts_ms) where signal.ts_ms <= snapshot.ts_ms
    - Digest-locked fixtures: `sample_day_ml_multisignal_basic`, `sample_day_ml_multisignal_no_prior` (PR #142)
    - 26 unit tests (14 contract + 12 selection)
  - [DONE] **M8-02 (ONNX):** Complete
    - [DONE] **M8-02a:** Artifact plumbing (types, loader, config fields, 19 tests)
    - [DONE] **M8-02b:** Shadow mode (OnnxMlModel, vectorize, soft-fail, 19 tests)
    - [DONE] **M8-02c:** Active inference mode (ADR-065)
      - [DONE] M8-02c-1: Config guards + 15 ADR-065 tests (PR #147)
      - [DONE] M8-02c-2: Observability: gauge, reason codes, metrics (PR #148)
      - [DONE] M8-02c-3: Structured logs + SSOT docs (PR #149)
    - [DONE] **M8-02d:** Latency histogram + SLO alerts (PR #151)
    - [DONE] **M8-02e:** Grafana dashboards (PR #154)
  - [DONE] **M8-03 (Training & Registry):** Complete
    - [DONE] **M8-03a:** Artifact pack v1.1 + build CLI (PR #150)
    - [DONE] **M8-03b-1:** Training pipeline MVP (PR #152)
    - [DONE] **M8-03b-2:** Runtime integration + determinism tests (PR #153)
    - [DONE] **M8-03c-1a:** Registry spec + runbook (PR #155)
    - [DONE] **M8-03c-1b:** Registry implementation (PR #157)
    - [DONE] **M8-03c-2:** PaperEngine config wiring (PR #158)
    - [DONE] **M8-03c-3:** Promotion CLI + history[] audit trail (PR #159)
  - **M8-04 (Feature Store):** Complete
    - [DONE] **M8-04 spec:** Feature store specification (`docs/18_FEATURE_STORE_SPEC.md`) (PR #165)
    - [DONE] **M8-04a:** Dataset manifest verification CLI (`scripts/verify_dataset.py`) + tests + fixture (PR #166)
    - [DONE] **M8-04b:** Dataset builder CLI (`scripts/build_dataset.py`) + tests (PR #167)
    - [DONE] **M8-04c:** Train integration with dataset manifest (`--dataset-manifest`, dataset_id traceability) (PR #168)
    - [DONE] **M8-04d:** ACTIVE promotion requires verified dataset artifact (`verify_dataset_for_promotion`, fail-closed guard in `promote_ml_model.py`) (PR #169)
    - [DONE] **M8-04e:** Operator runbook (`docs/runbooks/20_FEATURE_STORE_DATASETS.md`) + golden dataset integration test

### Multi-venue
- **Current:** Binance Futures USDT-M only
- **Planned:** COIN-M, other CEXs (see ROADMAP M9)
- **Status:** [DEFERRED] Post-launch (see ADR-066). Focus on single-venue launch readiness first.
