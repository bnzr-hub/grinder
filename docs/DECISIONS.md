# DECISIONS (ADR Log)

Этот файл фиксирует **почему** были приняты важные решения. Цель — избежать “памяти на словах” и дрейфа архитектуры.

## Формат записи

- **ID:** ADR-XXX
- **Date:** YYYY-MM-DD
- **Status:** proposed | accepted | superseded
- **Context:** почему возник вопрос
- **Decision:** что выбрали
- **Consequences:** что это означает для кода/процесса
- **Alternatives:** что рассматривали

---

## ADR-001 — Детерминизм replay обязателен
- **Date:** 2026-01-30
- **Status:** accepted
- **Context:** изменения в policy/risk/execution легко ломают воспроизводимость бэктестов.
- **Decision:** любой PR, затрагивающий replay/fixtures/policy/risk/execution, обязан проходить `scripts.verify_replay_determinism` и прикладывать proof.
- **Consequences:** флак-тесты запрещены; случайность только с фиксированными seed.
- **Alternatives:** “доверять на слово” / “проверять вручную”.

## ADR-002 — README/pyproject не опережают реализацию
- **Date:** 2026-01-30
- **Status:** accepted
- **Context:** несостыковки документации и кода ломают онбординг и доверие.
- **Decision:** любые команды/CLI/пути в README и entrypoints в `pyproject.toml` должны быть реально работоспособны. Всё остальное помечать как Planned.
- **Consequences:** прежде чем добавить новый entrypoint — добавить модуль+тест.
- **Alternatives:** "дописать позже".

## ADR-003 — Domain contracts as SSOT
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** M1 vertical slice требует единых контрактов данных для pipeline (Snapshot → Policy → Decision → Execution). Без SSOT контрактов легко получить рассинхрон между компонентами.
- **Decision:**
  - Контракты живут в `src/grinder/contracts.py`
  - Все контракты — frozen dataclasses (immutable)
  - Все контракты имеют `to_dict()`/`from_dict()` для JSON-сериализации
  - JSON-сериализация детерминистична (`sort_keys=True`)
  - Breaking changes требуют: (1) обновления fixtures, (2) bump версии, (3) обновления STATE.md
- **Consequences:**
  - Prefilter/Policy/Execution импортируют контракты из `contracts.py`
  - Тесты проверяют roundtrip сериализации
  - Golden fixtures в `tests/fixtures/contracts/` служат regression-тестами
- **Alternatives:** "протоколы/интерфейсы без конкретных типов" — отвергнуто, т.к. ломает детерминизм и type safety.

## ADR-004 — Adaptive Controller is rule-based MVP with explicit resets
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** grid parameters and mode must adapt to regime shifts; silent drift and implicit resets cause churn and non-determinism.
- **Decision:** introduce an Adaptive Controller contract that selects market regime, computes adaptive step, and emits explicit auto-reset actions with reason-codes (SSOT: `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md`).
- **Consequences:** `GridPlan` contract expands (regime/width/reset/reason_codes); observability must expose regime/step/reset; implementation PRs must include baseline backtests.
- **Alternatives:** black-box ML controller; implicit resets hidden in execution; keeping static spacing.

## ADR-005 — Unicode policy for docs
- **Date:** 2026-01-31 (updated 2026-02-01)
- **Status:** accepted
- **Context:** GitHub flags "hidden or bidirectional Unicode" in PRs (CVE-2021-42574 Trojan Source protection). Need clear policy on allowed vs dangerous chars, and documented mitigation for the warning.
- **Decision:**
  - **Forbidden (security risk):** bidi controls (U+202A-E, U+2066-9), zero-width (U+200B-D, U+FEFF), soft hyphen (U+00AD), category `Cf` (Format) except allowed below
  - **Allowed (non-security):** box-drawing (U+2500-257F) for diagrams, Cyrillic for Russian text in docs, em-dash (U+2014) for typography
  - Use `scripts/check_unicode.py` to verify before merge (scans for forbidden bidi/zero-width chars)
  - **GitHub warning mitigation:** The repo-wide scan (`scripts/check_unicode.py`) is the authoritative security gate, NOT the GitHub UI warning. GitHub may show "hidden or bidirectional Unicode" warning due to:
    - Em-dashes (U+2014) in documentation — allowed, not a security risk
    - Cyrillic text in `docs/STATE.md` — allowed, Russian documentation
    - Box-drawing characters — allowed for diagrams
  - PRs are mergeable when: (1) `scripts/check_unicode.py` passes, (2) all CI checks green
  - **Enforced in CI:** `.github/workflows/ci.yml` → step "Unicode security scan"
- **Consequences:** PRs must pass Unicode scan; GitHub warning is informational, not blocking, when scan passes.
- **Alternatives:** replace all non-ASCII with ASCII — rejected for readability and i18n support.

## ADR-006 — Fixture event format: SNAPSHOT replaces BOOK_TICKER
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** M1 end-to-end replay requires events that match the `Snapshot` domain contract (`src/grinder/contracts.py`). The old `BOOK_TICKER` format used `bid`/`ask` (floats) and lacked `last_price`/`last_qty` fields required by `Snapshot.from_dict()`. ReplayEngine needs typed Snapshot objects to compute `mid_price` and `spread_bps` for prefilter and policy.
- **Decision:**
  - Fixture events use `type: "SNAPSHOT"` with fields matching `Snapshot` contract:
    - `ts`, `symbol`, `bid_price`, `ask_price`, `bid_qty`, `ask_qty`, `last_price`, `last_qty`
  - All price/quantity fields are string-encoded Decimals (not floats) for determinism
  - Old `BOOK_TICKER` format is **not supported** by ReplayEngine
  - Expected digest for `tests/fixtures/sample_day` is now `453ebd0f655e4920` (was `9397b7200c09a7d2` with old format)
- **Consequences:**
  - Existing fixtures must be migrated to SNAPSHOT format
  - Digest values in `config.json` must be updated after migration
  - ReplayEngine only processes `type: "SNAPSHOT"` events (other types ignored)
- **Alternatives:**
  - Support both formats with adapter — rejected for complexity and non-determinism risk
  - Keep BOOK_TICKER and adapt Snapshot contract — rejected because BOOK_TICKER lacks required fields

## ADR-007 — Gating metrics contract
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** Gating decisions (allow/block) need observability for production monitoring and debugging. Metric names and label keys become implicit contracts once Grafana dashboards and alerts depend on them.
- **Decision:**
  - Stable enums for gate identifiers (`GateName`) and block reasons (`GateReason`)
  - Metric names are constants: `grinder_gating_allowed_total`, `grinder_gating_blocked_total`
  - Label keys are constants: `gate`, `reason`
  - Contract tests in `tests/unit/test_gating_contracts.py` fail if any of these change
  - Breaking changes require: (1) update contract tests, (2) add ADR entry, (3) update dashboards
- **Consequences:**
  - Adding new gates/reasons is safe (append-only)
  - Renaming or removing existing values is a breaking change
  - Dashboards can rely on stable label values
- **Alternatives:**
  - Free-text reasons — rejected for label cardinality explosion
  - No contract tests — rejected because silent breaks are worse than loud failures

## ADR-008 — Paper trading output schema v1
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** Paper trading needs stable output schema for: (1) replay determinism verification, (2) downstream analysis tools, (3) dashboard integration. Adding fields without versioning risks silent breaking changes.
- **Decision:**
  - Introduce `SCHEMA_VERSION = "v1"` constant in `src/grinder/paper/engine.py`
  - `PaperResult` includes `schema_version` field in serialized output
  - **PaperOutput v1 contract** (required keys): `ts`, `symbol`, `prefilter_result`, `gating_result`, `plan`, `actions`, `events`, `blocked_by_gating`, `fills`, `pnl_snapshot`
  - **PaperResult v1 contract** (required keys): `schema_version`, `fixture_path`, `outputs`, `digest`, `events_processed`, `events_gated`, `orders_placed`, `orders_blocked`, `total_fills`, `final_positions`, `total_realized_pnl`, `total_unrealized_pnl`, `errors`
  - All monetary values serialized as strings (Decimal → str)
  - Contract tests in `tests/unit/test_paper_contracts.py` fail on breaking changes
  - Canonical digests locked: `sample_day` = `66b29a4e92192f8f`, `sample_day_allowed` = `ec223bce78d7926f`, `sample_day_toxic` = `66d57776b7be4797`
- **Consequences:**
  - Adding new fields is safe (append-only)
  - Removing/renaming existing fields is breaking change requiring version bump
  - Downstream tools can rely on field presence
  - Digests change when output structure changes (intentional)
- **Alternatives:**
  - No versioning — rejected because silent breaks are worse
  - Semantic versioning — deferred, simple "v1" sufficient for now

## ADR-009 — ToxicityGate v0 with per-symbol price tracking
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** Markets can become "toxic" during periods of stress (flash crashes, spread spikes). Trading during toxic conditions leads to adverse selection and losses. Need a gate to block orders when market conditions are dangerous.
- **Decision:**
  - Introduce `ToxicityGate` class in `src/grinder/gating/toxicity_gate.py`
  - **Spread spike detection**: Block when `spread_bps > max_spread_bps` (default 50 bps)
  - **Price impact detection**: Block when price moves > `max_price_impact_bps` (default 500 bps = 5%) within `lookback_window_ms` (default 5000ms)
  - Threshold set high (500 bps) to avoid triggering on normal volatility (existing fixtures have up to 300 bps moves)
  - Price history is tracked **per-symbol** (not global) to avoid false positives across different assets
  - ToxicityGate is checked **first** in gating pipeline (fail-fast on market conditions)
  - Extend `GateName` with `TOXICITY_GATE`, `GateReason` with `SPREAD_SPIKE` and `PRICE_IMPACT_HIGH`
  - New fixture `sample_day_toxic` tests price impact blocking (600 bps move)
  - Toxicity details NOT included in allow result to preserve backward compatibility with existing digests
- **Consequences:**
  - Paper engine gating order: toxicity → rate limit → risk gate
  - Existing fixture digests UNCHANGED: `sample_day` = `66b29a4e92192f8f`, `sample_day_allowed` = `ec223bce78d7926f`
  - New fixture: `sample_day_toxic` = `66d57776b7be4797`
  - `/metrics` now includes `toxicity_gate` label values
- **Alternatives:**
  - Global price history across symbols — rejected because comparing BTC to ETH prices causes false positives
  - ML-based toxicity detection — deferred for v1, rule-based is deterministic and sufficient for v0
  - Lower threshold (100 bps) — rejected because it would trigger on existing "happy path" fixtures
  - Include toxicity details in allow result — rejected to preserve backward compatibility with existing digests

## ADR-010 — Top-K Prefilter v0 with volatility scoring
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** Need to select a candidate set of symbols (Top-K) from a multisymbol stream for grid trading. Selection must be deterministic to ensure replay reproducibility.
- **Decision:**
  - Introduce `TopKSelector` class in `src/grinder/prefilter/topk.py`
  - **Scoring method:** Volatility proxy — sum of absolute mid-price returns in integer basis points over a window
    - `score_bps = int(Σ |(mid_t - mid_{t-1}) / mid_{t-1}| * 10000)` (quantized to int for determinism)
  - **Tie-breakers (deterministic):**
    1. Higher score first
    2. Lexicographic symbol ascending
  - **Parameters:**
    - `K` = 3 (default, configurable)
    - `window_size` = 10 events per symbol (default, configurable)
  - Top-K runs **before** policy/execution/gating in paper trading pipeline
  - Top-K results included in `PaperResult` (`topk_selected_symbols`, `topk_k`, `topk_scores`) and `FixtureResult` in backtest report
  - **NOT** included in paper digest computation to preserve backward compatibility with existing canonicals
  - New fixture `sample_day_multisymbol` with 5 symbols tests Top-K filtering
- **Consequences:**
  - Existing fixture digests UNCHANGED (all have ≤3 symbols, K=3 selects all)
    - `sample_day` = `66b29a4e92192f8f`
    - `sample_day_allowed` = `ec223bce78d7926f`
    - `sample_day_toxic` = `66d57776b7be4797`
  - New fixture: `sample_day_multisymbol` = `7c4f4b07ec7b391f`
  - Backtest report now includes `topk_selected_symbols` and `topk_k` per fixture
  - Report digest changes due to new fixture: `cd622b8e55764b5b`
- **Alternatives:**
  - Activity proxy (event count) — rejected for being less "market-y"
  - ML-based scoring — deferred for v1, rule-based is deterministic and sufficient for v0
  - Include Top-K in digest — rejected to preserve backward compatibility

## ADR-011 — Adaptive Controller v0 with rule-based parameter adjustment
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** Grid spacing must adapt to changing market conditions. Static spacing leads to suboptimal performance: too tight in volatile markets (fills too fast, adverse selection), too wide in calm markets (misses opportunities). Need a controller that adjusts policy parameters based on recent market conditions.
- **Decision:**
  - Introduce `AdaptiveController` class in `src/grinder/controller/adaptive.py`
  - **Controller modes:**
    - `BASE` — Normal operation, no adjustment (spacing_multiplier = 1.0)
    - `WIDEN` — High volatility, widen grid (spacing_multiplier = 1.5)
    - `TIGHTEN` — Low volatility, tighten grid (spacing_multiplier = 0.8)
    - `PAUSE` — Wide spread, no new orders
  - **Decision reasons:**
    - `NORMAL` — Metrics within normal thresholds
    - `HIGH_VOL` — Volatility above threshold (> 300 bps)
    - `LOW_VOL` — Volatility below threshold (< 50 bps)
    - `WIDE_SPREAD` — Spread above threshold (> 50 bps)
  - **Priority order:** PAUSE > WIDEN > TIGHTEN > BASE
  - **Window-based metrics:**
    - `vol_bps` = sum of absolute mid-price returns in integer bps over window
    - `spread_bps_max` = maximum spread observed in window
    - Window size default = 10 events per symbol
  - **Determinism:** All metrics use integer basis points (no floats)
  - **Opt-in:** Controller disabled by default (`controller_enabled=False`) to preserve backward compatibility
  - New fixture `sample_day_controller` tests all three volatility modes
- **Consequences:**
  - Controller is wired into paper engine after prefilter/Top-K, before policy evaluation
  - When enabled, controller applies spacing_multiplier to base spacing
  - Controller decisions recorded in `PaperResult.controller_decisions` (NOT in digest)
  - Existing fixture digests UNCHANGED (controller disabled by default):
    - `sample_day` = `66b29a4e92192f8f`
    - `sample_day_allowed` = `ec223bce78d7926f`
    - `sample_day_toxic` = `66d57776b7be4797`
    - `sample_day_multisymbol` = `7c4f4b07ec7b391f`
  - New fixture: `sample_day_controller` = `f3a0a321c39cc411`
- **Alternatives:**
  - EMA-based adaptive step (as per spec 16) — deferred for v1, rule-based is simpler and sufficient for v0
  - ML-based regime detection — rejected for determinism concerns
  - Controller always-on — rejected to preserve backward compatibility with existing digests

## ADR-012 — DataConnector abstract base class with narrow contract
- **Date:** 2026-02-01
- **Status:** accepted
- **Context:** Need a data connector interface for ingesting market data from various sources (live WebSocket, mock fixtures, replay files). The connector must support production hardening (timeouts, retries, idempotency) while remaining simple for testing.
- **Decision:**
  - Introduce `DataConnector` abstract base class in `src/grinder/connectors/data_connector.py`
  - **Narrow contract** (only essential methods):
    - `connect()` — establish connection
    - `close()` — release resources
    - `iter_snapshots()` — async iterator yielding `Snapshot`
    - `reconnect()` — resume from last position
  - **State machine:** DISCONNECTED → CONNECTING → CONNECTED → (RECONNECTING) → CLOSED
  - **Hardening configurations:**
    - `RetryConfig`: exponential backoff with cap (base_delay_ms, backoff_multiplier, max_delay_ms)
    - `TimeoutConfig`: connection and read timeouts
  - **Idempotency:** `last_seen_ts` property for duplicate detection; snapshots with ts ≤ last_seen_ts are skipped
  - **Assumption:** Timestamps in a single stream are monotonically increasing (per-stream, not per-symbol). Multi-symbol streams share one `last_seen_ts` guard. This is valid for fixture-based testing; live connectors may need per-symbol cursors if exchange delivers out-of-order across symbols.
  - **First implementation:** `BinanceWsMockConnector` reads from fixture files for testing
- **Consequences:**
  - New connectors must implement `DataConnector` interface
  - Idempotency is connector's responsibility (via `last_seen_ts` guard)
  - Mock connector enables deterministic testing without network
  - Retry/timeout configs are present but actual retry logic is implementation-specific
- **Alternatives:**
  - Callback-based interface — rejected for complexity and testing difficulty
  - Pull-based (get_next_snapshot) — rejected because async iterator is more Pythonic
  - Global connector registry — rejected as premature abstraction

## ADR-013 — DrawdownGuard and KillSwitch for equity protection
- **Date:** 2026-02-01
- **Status:** accepted
- **Context:** Need risk controls to halt trading when equity drawdown exceeds threshold. Essential for protecting capital in adverse market conditions. Must be deterministic for replay testing.
- **Decision:**
  - **Equity definition:**
    - `equity = initial_capital + total_realized_pnl + total_unrealized_pnl`
    - Sampled per snapshot (after fills are applied)
    - `initial_capital` is configurable parameter (default 10000 USD)
  - **DrawdownGuard** (`src/grinder/risk/drawdown.py`):
    - Tracks high-water mark (HWM), initialized to `initial_capital`
    - Drawdown computed as: `(HWM - equity) / HWM * 100`
    - Configurable threshold (`max_drawdown_pct`, default 5%)
    - **Latching:** once triggered, stays triggered until explicit reset
  - **KillSwitch** (`src/grinder/risk/kill_switch.py`):
    - Simple latch: once triggered, stays triggered
    - **Idempotent:** triggering twice is a no-op (returns existing state)
    - **Reasons:** `DRAWDOWN_LIMIT`, `MANUAL`, `ERROR`
    - **Reset semantics:** does NOT auto-reset within a run; requires new engine run or explicit `reset()` call
  - **PaperEngine integration:**
    - Optional: `kill_switch_enabled` parameter (default False for backward compatibility)
    - When kill-switch is triggered, blocks all trading with `KILL_SWITCH_ACTIVE` gating reason
    - **No auto-liquidation:** positions are NOT force-closed when kill-switch trips (default behavior)
    - State exposed in `PaperOutput` (per-snapshot) and `PaperResult` (final)
  - **New gating reasons added:**
    - `KILL_SWITCH_ACTIVE` — trading blocked because kill-switch is triggered
    - `DRAWDOWN_LIMIT_EXCEEDED` — for future use (currently triggers kill-switch instead)
- **Consequences:**
  - Trading halts deterministically when drawdown exceeds threshold
  - Trigger point is deterministic (exact snapshot where threshold crossed)
  - Backward compatible: disabled by default
  - No automatic position management (manual intervention required after trip)
- **Alternatives:**
  - Auto-liquidation on trip — rejected for simplicity; can be added as opt-in feature later
  - Soft warning before hard stop — rejected; keep v0 simple
  - Per-symbol drawdown tracking — rejected; track total portfolio equity for v0

## ADR-014 — Soak Gate as CI release gate
- **Date:** 2026-02-01
- **Status:** accepted
- **Context:** Need CI gate to catch performance regressions and determinism issues before merge. Synthetic soak (nightly) exists but doesn't use real PaperEngine execution. Need fixture-based soak that validates actual behavior.
- **Decision:**
  - **Soak runner** (`scripts/run_soak_fixtures.py`):
    - Runs PaperEngine on all registered fixtures multiple times (default 3 runs)
    - Collects metrics: latency (p50, p99), RSS memory, fill_rate, errors, digest stability
    - Outputs JSON report compatible with `check_soak_thresholds.py`
  - **Gating vs informational metrics:**
    - **Gating (causes failure):** `errors_total`, `all_digests_stable`, `fill_rate` range
    - **Informational (logged but not gated):** `decision_latency_p99_ms`, `order_latency_p99_ms`, `rss_mb_max`, `event_queue_depth_max`
    - Latency/memory are NOT gated because CI runners have variable performance
  - **Determinism check:**
    - Each fixture runs N times, all N digests must match
    - `all_digests_stable: true` required for pass
    - Any digest mismatch = hard failure
  - **Why 3 runs per fixture:**
    - Balances coverage vs CI time
    - Enough to detect non-determinism
    - Not so many that CI becomes slow
  - **Fill rate semantics:**
    - Computed as: `total_fills / total_orders_placed`
    - Value between 0 and 1
    - If no orders placed, fill_rate = 1.0 (nothing to fill)
    - Threshold: min 0.4, max 1.0 (baseline)
  - **Deterministic gate script** (`scripts/check_soak_gate.py`):
    - Reads thresholds from `monitoring/soak_thresholds.yml` (SSOT for thresholds)
    - Gates ONLY deterministic metrics: `all_digests_stable`, `errors_total`, `fill_rate`, `events_dropped`
    - Logs latency/memory as informational (not gated)
    - Usage: `python -m scripts.check_soak_gate --report report.json --thresholds monitoring/soak_thresholds.yml --mode baseline`
  - **CI workflow** (`.github/workflows/soak_gate.yml`):
    - Triggers on PRs touching `src/**`, `scripts/**`, `tests/fixtures/**`, `monitoring/soak_thresholds.yml`
    - Runs fixture-based soak with 3 runs per fixture
    - Calls `check_soak_gate.py` for deterministic-only validation
    - Uploads JSON report as artifact for inspection
  - **Nightly synthetic soak** (`.github/workflows/nightly_soak.yml`):
    - Uses `check_soak_thresholds.py` with full threshold validation (latency/memory included)
    - Appropriate for controlled environment with consistent runner performance
- **Consequences:**
  - PRs that break determinism will be blocked
  - PRs that cause errors will be blocked
  - Latency/memory regressions are visible in PR artifacts but not blocking (CI variance)
  - Nightly soak provides full threshold validation in stable environment
- **Alternatives:**
  - Gate on latency/memory — rejected; too flaky in CI
  - Only nightly soak — rejected; too slow feedback loop
  - Single run — rejected; doesn't catch non-determinism

## ADR-015 — HA v0 with Redis lease-lock for single-active safety
- **Date:** 2026-02-01
- **Status:** accepted
- **Context:** Production deployments need high availability to avoid single-point-of-failure. Full multi-node HA is complex; v0 focuses on single-host redundancy with single-active safety to prevent split-brain scenarios.
- **Decision:**
  - **Architecture:** Single-host redundancy with 2+ instances + Redis for coordination
  - **Leader election:** TTL-based lease lock using Redis SET with NX/XX and PX options
    - Lock TTL: 10 seconds
    - Renewal interval: 3 seconds (< TTL to prevent expiry during normal operation)
    - Lock key: `grinder:leader:lock`
  - **HA roles:**
    - `ACTIVE` — Instance holds the lock, actively trading
    - `STANDBY` — Instance is healthy but waiting to acquire lock
    - `UNKNOWN` — Initial state before first lock attempt
  - **Fail-safe semantics:**
    - If lock renewal fails → immediately become STANDBY (fail-safe)
    - Redis unavailable → all instances become STANDBY
    - Only ACTIVE instance executes trading logic
  - **Observability:**
    - `/readyz` endpoint: Returns 200 for ACTIVE, 503 for STANDBY/UNKNOWN
    - `/healthz` endpoint: Always 200 if process is alive (liveness, not readiness)
    - `grinder_ha_role{role}` metric: Gauge with all roles, current=1, others=0
  - **Deployment:** `docker-compose.ha.yml` with Redis 7.2 + 2 grinder instances
  - **Environment variables:**
    - `GRINDER_REDIS_URL` — Redis connection URL (default: redis://localhost:6379/0)
    - `GRINDER_HA_ENABLED` — Enable HA mode (default: false)
    - `GRINDER_HA_LOCK_TTL_MS` — Lock TTL in ms (default: 10000)
    - `GRINDER_HA_RENEW_INTERVAL_MS` — Renewal interval in ms (default: 3000)
- **Consequences:**
  - Single-active safety: Only one instance is ACTIVE at any time
  - Failover on leader crash: ~10s (lock TTL expiry)
  - Redis is SPOF for coordination (acceptable for single-host deployment)
  - No protection against host/VM failure (that's HA v1 scope)
- **Alternatives:**
  - Docs-only (no code) — rejected; need working HA for production readiness
  - File-based lock — rejected; not atomic, no TTL support
  - Active-active with partitioning — rejected; too complex for v0
  - Full Raft/Paxos consensus — rejected; overkill for single-host

## ADR-016 — Fill model v1: crossing/touch with determinism constraints
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** Paper trading v0 used "instant fill" model where all PLACE orders fill immediately at their limit price regardless of market state. This is unrealistic: real limit orders fill only when price reaches the limit. Need a more realistic fill model while preserving determinism for replay.
- **Decision:**
  - **Crossing/touch fill model (v1):**
    - LIMIT BUY fills if `mid_price <= limit_price` (price came down to our buy level)
    - LIMIT SELL fills if `mid_price >= limit_price` (price came up to our sell level)
    - Orders that don't cross/touch the mid price do NOT fill
  - **No partial fills:** v1 fills are all-or-nothing (max 1 fill per order)
  - **Determinism:** Fill simulation uses only `mid_price` and `limit_price` (both Decimal), no randomness
  - **Backward compatibility:**
    - `fill_mode="crossing"` (default) — new crossing/touch model
    - `fill_mode="instant"` — legacy instant-fill for backward compat
  - **Implementation:** `src/grinder/paper/fills.py::simulate_fills()` function
  - **Tests:** `tests/unit/test_fills.py` with crossing/touch and instant mode coverage
- **Consequences:**
  - Fixture digests updated (orders that don't cross now produce 0 fills):
    - `sample_day` = `66b29a4e92192f8f` (unchanged — blocked by gating, 0 fills)
    - `sample_day_allowed` = `3ecf49cd03db1b07` (was `ec223bce78d7926f`)
    - `sample_day_toxic` = `a31ead72fc1f197e` (was `66d57776b7be4797`)
    - `sample_day_multisymbol` = `22acba5cb8b81ab4` (was `7c4f4b07ec7b391f`)
    - `sample_day_controller` = `f3a0a321c39cc411` (unchanged — 0 fills with controller)
  - Fills are now more realistic: buy orders only fill when price drops, sell orders only fill when price rises
  - This is foundation for ASM (Adaptive Smart Grid v1) per §17.21 roadmap
- **Alternatives:**
  - Keep instant fill — rejected; too unrealistic for meaningful backtests
  - Probabilistic fill based on queue position — rejected; breaks determinism
  - Slippage model — deferred to v2; crossing/touch is sufficient for v1

## ADR-017 — CycleEngine v1: fill → TP + replenishment
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** Grid trading requires automatic cycle management: when a fill occurs, a take-profit (TP) order should be placed on the opposite side, and optionally a replenishment order to maintain grid depth. This is specified in §17.12.2 of the ASM v1 spec.
- **Decision:**
  - **CycleEngine module:** `src/grinder/paper/cycle_engine.py`
  - **TP generation logic:**
    - BUY fill at `p_fill` with `qty` → SELL TP at `p_fill * (1 + step_pct)` for same `qty`
    - SELL fill at `p_fill` with `qty` → BUY TP at `p_fill * (1 - step_pct)` for same `qty`
  - **Replenishment logic:**
    - If `adds_allowed=True`: place new order further out (same side as fill, opposite direction)
    - If `adds_allowed=False`: only TP orders, no new risk added
  - **Determinism constraints:**
    - Intent IDs: `cycle_{type}_{source_fill_id}_{side}_{price}`
    - Ordering: fills processed in order; TP before replenishment for each fill
    - Price/quantity: Decimal with configurable precision, ROUND_DOWN
  - **Integration with PaperEngine:**
    - `cycle_enabled` parameter (default False for backward compat)
    - `cycle_step_pct` parameter (default 0.001 = 10 bps)
    - `cycle_intents` field in PaperOutput (NOT included in digest for backward compat)
  - **adds_allowed determination (v1):**
    - False if controller mode is PAUSE
    - False if kill-switch is triggered
    - True otherwise
- **Consequences:**
  - CycleEngine generates CycleIntent objects representing desired TP/replenishment orders
  - Intents are recorded in output for observability but don't affect canonical digests
  - Foundation for full grid cycling in ASM v1 (next: intents → actual order placement)
  - Backward compatible: existing digests unchanged when cycle_enabled=False
- **Alternatives:**
  - Immediate order placement from fills — rejected; need intent layer for gating/validation
  - Fixed TP distance — rejected; step_pct allows adaptive sizing per controller
  - Martingale sizing — rejected; bounded replenishment is safer per §17.11.3

## ADR-018 — Sizing units SSOT: size_schedule is base quantity, NOT notional
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** `GridPlan.size_schedule` had ambiguous unit semantics. The `StaticGridPolicy` docstring incorrectly stated "quote currency (USD)" while execution engine treated it as base asset quantity. This ambiguity could lead to order sizing bugs (e.g., placing $0.01 orders instead of 0.01 BTC orders).
- **Decision:**
  - **SSOT:** `GridPlan.size_schedule` is ALWAYS interpreted as **base asset quantity** (e.g., BTC, ETH), NOT notional (USD)
  - **Documentation updates:**
    - `GridPlan` docstring: explicit Units section referencing this ADR
    - `StaticGridPolicy.size_per_level` docstring: fixed to "base asset quantity"
    - `ExecutionEngine._compute_grid_levels`: already correct ("quantity per level")
  - **Conversion utility:** `notional_to_qty(notional, price, precision)` in `src/grinder/policies/base.py`
    - Formula: `qty = notional / price`, rounded down to precision
    - Use this when configuring size_schedule from notional budget
  - **Reference:** docs/smart_grid/SPEC_V1_0.md §17.12.4 (canonical)
- **Consequences:**
  - All code interpreting `size_schedule` MUST treat values as base asset quantity
  - To configure from notional (USD) budget, explicitly convert: `notional_to_qty(500, 50000) = 0.01`
  - No backward compat impact: execution engine already uses qty interpretation
- **Alternatives:**
  - Allow both units with a flag — rejected; too error-prone, single SSOT is safer
  - Default to notional — rejected; qty is more intuitive for trading (you buy/sell qty, not USD)
  - No conversion utility — rejected; explicit helper prevents division-order bugs

## ADR-019 — Feature Engine v1: deterministic mid-bar OHLC + L1 microstructure
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** ASM v1 (Adaptive Smart Grid) requires computed market features for dynamic policy adjustment. Per §17.5, the policy needs:
  - **Volatility:** NATR (Normalized ATR) for adaptive grid spacing
  - **L1 microstructure:** imbalance, thin_l1 for order placement decisions
  - **Range/trend:** range_score for regime detection (choppy vs trending)
  Features must be deterministic for replay fidelity.
- **Decision:**
  - **Module structure:** `src/grinder/features/` with:
    - `bar.py` — MidBar frozen dataclass + BarBuilder for OHLC construction
    - `indicators.py` — compute_atr, compute_natr_bps, compute_imbalance_l1_bps, compute_thin_l1, compute_range_trend
    - `types.py` — FeatureSnapshot frozen dataclass with all computed features
    - `engine.py` — FeatureEngine orchestrator (maintains per-symbol state)
  - **Bar building (§17.5.1):**
    - Bar boundaries: floor division on bar_interval_ms (default 60s)
    - No synthesized bars for gaps (correct behavior)
    - MidBar: bar_ts, open, high, low, close, tick_count
  - **ATR/NATR (§17.5.2):**
    - True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    - ATR = SMA of TRs over period (default 14)
    - NATR = ATR / close, in integer bps for determinism
  - **L1 features (§17.5.3):**
    - imbalance_l1_bps = (bid_qty - ask_qty) / (bid_qty + ask_qty + eps) * 10000
    - thin_l1 = min(bid_qty, ask_qty)
    - spread_bps = (ask - bid) / mid * 10000
  - **Range/trend (§17.5.5):**
    - sum_abs_returns_bps = sum of |return_i| over horizon
    - net_return_bps = |p_end/p_start - 1|
    - range_score = sum_abs_returns_bps / (net_return_bps + 1)
    - High range_score = choppy; low = trending
  - **Warmup handling:**
    - ATR/NATR return 0/None until period+1 bars complete
    - FeatureSnapshot.is_warmed_up = warmup_bars >= 15
  - **Determinism guarantees:**
    - All intermediate calcs use Decimal
    - Final outputs: integer bps or Decimal (no floats)
    - Same snapshot sequence → identical features
  - **Backward compatibility:**
    - FeatureEngine is standalone; not yet integrated into PaperEngine
    - No changes to existing digests
    - Integration deferred to ASM-P1-02
- **Consequences:**
  - FeatureSnapshot can be passed to policy via to_policy_features()
  - Unit tests: test_bar_builder.py (25 tests), test_indicators.py (32 tests), test_feature_engine.py (26 tests)
  - Memory bounded: max_bars=1000 per symbol
  - Foundation for ASM v1 policy adaptation (§17.6, §17.7)
- **Alternatives:**
  - Use floating point — rejected; breaks determinism
  - External TA library (TA-Lib) — rejected; Decimal precision lost, extra dependency
  - Time-weighted VWAP bars — deferred to v2; mid-bars sufficient for v1
  - Volume bars — deferred; tick-based sufficient for initial ASM

## ADR-020 — Policy features plumbing (ASM-P1-03)
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** FeatureEngine computes market features (ADR-019), and PaperEngine integrates it (ASM-P1-02). The next step is to pass features to the policy for adaptive behavior. Per ASM v1 spec §17.6, policies need access to volatility (NATR), L1 microstructure (imbalance, thin_l1), and range metrics for dynamic parameter adjustment. However, this must be done without changing existing behavior (StaticGridPolicy ignores extra features).
- **Decision:**
  - **Plumbing approach:**
    - `FeatureSnapshot.to_policy_features()` returns typed dict with: mid_price, spread_bps, imbalance_l1_bps, natr_bps, thin_l1, range_score, net_return_bps, sum_abs_returns_bps, is_warmed_up
    - PaperEngine merges these features into `policy_features` dict when `feature_engine_enabled=True`
    - Policy receives full features dict via existing `evaluate(features: dict[str, Any])` interface
  - **Backward compatibility:**
    - `StaticGridPolicy` only uses `mid_price` from features dict, ignores extra keys
    - When `feature_engine_enabled=False` (default), policy receives only `{"mid_price": ...}` (existing behavior)
    - Canonical digests unchanged — features affect policy input but StaticGridPolicy ignores them
  - **No behavior change:**
    - This PR is pure plumbing — StaticGridPolicy behavior is identical with or without extra features
    - Future policies (e.g., AdaptiveGridPolicy) will use the additional features
  - **Testing:**
    - `test_policy_receives_features_when_enabled`: verifies full features dict passed to policy
    - `test_policy_receives_only_mid_price_when_disabled`: verifies minimal dict when disabled
    - `test_static_grid_policy_ignores_extra_features`: verifies StaticGridPolicy works with extra keys
    - `test_digests_unchanged_with_policy_features`: regression test for digest stability
- **Consequences:**
  - Policy interface unchanged (`GridPolicy.evaluate(features: dict[str, Any])` already accepts generic dict)
  - Foundation for ASM v1 adaptive policies that will use features
  - Determinism maintained — same inputs produce same outputs
- **Alternatives:**
  - Add explicit features parameter to policy — rejected; dict is flexible for future features
  - Pass FeatureSnapshot directly — rejected; dict is more generic and allows policy to not depend on features module
  - Change StaticGridPolicy to use features — deferred to AdaptiveGridPolicy (ASM-P1-05)

## ADR-021 — Deterministic regime classifier v1 (ASM-P1-04)
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** ASM v1 requires regime-driven policy behavior (§17.3). Before implementing adaptive policies, we need a deterministic regime classifier that classifies market conditions from FeatureSnapshot and gating state. The regime determines which policy behavior to activate (e.g., pause trading in EMERGENCY, widen grids in VOL_SHOCK, tighten in RANGE).
- **Decision:**
  - **Module:** `src/grinder/controller/regime.py` with:
    - `Regime` enum: RANGE, TREND_UP, TREND_DOWN, VOL_SHOCK, THIN_BOOK, TOXIC, PAUSED, EMERGENCY
    - `RegimeReason` enum: reason codes for each classification
    - `RegimeConfig` frozen dataclass: threshold configuration (no magic numbers)
    - `RegimeDecision` frozen dataclass: classified regime + reason + confidence + features_used
    - `classify_regime()` function: precedence-based classification
  - **Precedence order (first match wins):**
    1. kill_switch_active → EMERGENCY
    2. toxicity_result blocked → TOXIC
    3. thin_l1 < threshold OR spread_bps > threshold → THIN_BOOK
    4. natr_bps > vol_shock_threshold → VOL_SHOCK
    5. |net_return_bps| > trend_threshold AND range_score <= choppy_max → TREND_UP/DOWN
    6. else → RANGE
  - **Thresholds (configurable via RegimeConfig):**
    - `thin_l1_qty=0.1` — minimum L1 depth on thin side
    - `spread_thin_bps=100` — spread threshold for THIN_BOOK
    - `vol_shock_natr_bps=500` — NATR threshold for VOL_SHOCK
    - `trend_net_return_bps=200` — net return threshold for trend
    - `trend_range_score_max=3` — max range_score for trending (vs choppy)
  - **Determinism guarantees:**
    - All thresholds are integer bps (no floats)
    - Same inputs → same regime classification
    - Confidence is integer 0-100
  - **Boundary conditions (strict > for thresholds):**
    - At threshold → NOT triggered (need to exceed threshold)
    - Exception: range_score uses <= for max (at or below is trending)
- **Consequences:**
  - Regime classifier is standalone; can be used independently of policy
  - Future policies will use `classify_regime()` to determine behavior
  - Unit tests: 28 tests in `test_regime.py` covering all precedence branches + boundary cases
  - No changes to existing digests — classifier is not yet integrated into PaperEngine
- **Alternatives:**
  - ML-based regime detection — deferred to v2; rule-based is deterministic and interpretable
  - Continuous regime probability — rejected; discrete regimes are simpler for policy logic
  - Integrate directly into AdaptiveGridPolicy — rejected; separation of concerns, easier to test

## ADR-022 — AdaptiveGridPolicy v1 (L1-only step/width/levels, deterministic)
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** ASM v1 requires dynamic grid parameters based on market conditions (§17.8-17.10). StaticGridPolicy uses fixed spacing/levels which doesn't adapt to volatility changes. We need an adaptive policy that computes step/width/levels from NATR and regime while maintaining determinism.
- **Decision:**
  - **Module:** `src/grinder/policies/grid/adaptive.py` with:
    - `AdaptiveGridConfig`: threshold configuration (integer bps for determinism)
    - `AdaptiveGridPolicy`: policy class with `evaluate()` method
    - Helper functions: `compute_step_bps()`, `compute_width_bps()`, `compute_levels()`
  - **Step computation (§17.9):**
    - `step_bps = max(step_min_bps, alpha * NATR * regime_mult)`
    - `alpha` stored as integer (30 = 0.30) for determinism
    - Regime multipliers: RANGE=1.0, VOL_SHOCK=1.5, THIN_BOOK=2.0
  - **Width/X_stress computation (§17.8):**
    - `sigma_H = NATR * sqrt(H / TF)` (horizon volatility)
    - `X_stress = clamp(k_tail * sigma_H, X_min, X_cap)`
    - Asymmetric in TREND: more width on against-trend side
  - **Levels computation (§17.10):**
    - `levels = ceil(width / step)`, clamped to [levels_min, levels_max]
  - **Regime integration:**
    - Uses `classify_regime()` from ADR-021 for market condition detection
    - EMERGENCY/TOXIC/PAUSED → pause plan (levels=0, reset=HARD)
    - VOL_SHOCK → wider step
    - TREND_UP/DOWN → asymmetric width
  - **PaperEngine integration:**
    - `adaptive_policy_enabled` flag (default False for backward compat)
    - When enabled, uses AdaptiveGridPolicy instead of StaticGridPolicy
    - Requires `feature_engine_enabled=True` for features
  - **Sizing:** Legacy (fixed size_per_level from config) — auto-sizing deferred to P1-05b
  - **Determinism guarantees:**
    - All thresholds and multipliers are integer bps
    - Integer arithmetic for all intermediate calculations
    - Same inputs → same GridPlan
- **Consequences:**
  - AdaptiveGridPolicy produces different GridPlan than StaticGridPolicy
  - New fixture `sample_day_adaptive` with separate digest (existing fixtures unchanged)
  - Unit tests: 23 tests in `test_adaptive_policy.py` for step/width/levels + boundaries
  - Existing digests unchanged (adaptive_policy_enabled=False by default)
- **Alternatives:**
  - Float arithmetic — rejected; breaks determinism
  - Single unified policy — rejected; StaticGridPolicy useful for comparison/debugging
  - Auto-sizing in v1 — deferred to P1-05b to keep PR scope manageable

## ADR-023 — Top-K v1: Feature-Based Symbol Selection (ASM-P1-06)
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** ASM v1 requires selecting the best K symbols for trading based on market characteristics. The existing Top-K v0 uses simple price volatility which doesn't consider range quality, liquidity, toxicity, or trend strength. We need a feature-based selection that integrates with FeatureEngine outputs.
- **Decision:**
  - **Module:** `src/grinder/selection/topk_v1.py` with:
    - `TopKConfigV1`: configuration (k, spread_max_bps, thin_l1_min, warmup_min, weights)
    - `SelectionCandidate`: input data structure (symbol, range_score, spread_bps, thin_l1, net_return_bps, warmup_bars, toxicity_blocked)
    - `SymbolScoreV1`: output with score components and gate status
    - `SelectionResult`: result with selected symbols and all scores
    - `select_topk_v1()`: main selection function
  - **Scoring formula (integer-only for determinism):**
    - `score = range_component + liquidity_component - toxicity_penalty - trend_penalty`
    - `range_component = w_range * range_score / 100`
    - `liquidity_component = ilog10(thin_l1 + 1) * liq_scale * w_liquidity / 100`
      - `ilog10(x) = len(str(x)) - 1` (floor of log10, deterministic via digit counting)
      - No float operations — fully portable across platforms/compilers
    - `toxicity_penalty = w_toxicity * 100` (fixed penalty when blocked)
    - `trend_penalty = w_trend * abs(net_return_bps) / 100`
  - **Hard gates (exclude before scoring):**
    - `TOXICITY_BLOCKED`: toxicity_blocked=True from ToxicityGate
    - `SPREAD_TOO_WIDE`: spread_bps > spread_max_bps
    - `THIN_BOOK`: thin_l1 < thin_l1_min
    - `WARMUP_INSUFFICIENT`: warmup_bars < warmup_min
  - **Tie-breaking:** deterministic sort by `(-score, symbol)` for stable ordering
  - **PaperEngine integration:**
    - `topk_v1_enabled` flag (default False for backward compat)
    - `topk_v1_config` parameter for configuration
    - First pass: feed all events to FeatureEngine for warmup
    - Build candidates from cached features + toxicity state
    - Run selection, filter symbols
    - Symbols not in Top-K get `not_in_topk=True` in output
  - **FeatureEngine extension:**
    - Added `_latest_snapshots` cache for per-symbol feature tracking
    - `get_latest_snapshot(symbol)` and `get_all_latest_snapshots()` methods
  - **Determinism guarantees:**
    - All weights and thresholds are integers
    - Integer-only arithmetic throughout scoring (no float operations)
    - Liquidity uses `ilog10` (digit counting) instead of `math.log10` to avoid float
    - Deterministic tie-breaking by `(-score, symbol)` for stable ordering
    - Same inputs → identical selection across all platforms
- **Consequences:**
  - New fixture `sample_day_topk_v1` with 6 symbols demonstrating selection
  - When enabled, replaces v0 volatility-based selection
  - Gate-excluded symbols marked in scores (gates_failed list)
  - PaperResult includes `topk_v1_selected_symbols`, `topk_v1_scores`, `topk_v1_gate_excluded`
  - PaperOutput includes `not_in_topk`, `topk_v1_rank` fields
  - 20 unit tests in `test_topk_v1.py`
- **Alternatives:**
  - Float weights — rejected; breaks determinism
  - `math.log10` for liquidity — rejected; float not bit-identical across platforms/libc
  - Single-pass selection — rejected; need FeatureEngine warmup first
  - Real-time re-selection — deferred; current approach selects once after warmup

## ADR-024 — Connector Hardening v1: Timeouts + Error Hierarchy + Clean Shutdown (PR-H1)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** DataConnector and BinanceWsMockConnector need production-grade hardening before live deployment. Per M3 roadmap, this includes explicit timeouts, typed error hierarchy, and clean shutdown semantics to prevent zombie tasks.
- **Decision:**
  - **TimeoutConfig extended:** Added `write_timeout_ms` (5000ms default) and `close_timeout_ms` (5000ms default) alongside existing `connect_timeout_ms` and `read_timeout_ms`
  - **Error hierarchy:** Created `src/grinder/connectors/errors.py` with:
    - `ConnectorError` — base exception for all connector errors
    - `ConnectorTimeoutError(op, timeout_ms)` — timeout during connect/read/write/close
    - `ConnectorClosedError(op)` — operation attempted on closed connector
    - `ConnectorIOError` — base for I/O errors
    - `ConnectorTransientError` — retryable errors (scaffolding for H2)
    - `ConnectorNonRetryableError` — non-retryable errors (scaffolding for H2/H4)
  - **Timeout utilities:** Created `src/grinder/connectors/timeouts.py` with:
    - `wait_for_with_op(coro, timeout_ms, op)` — wraps `asyncio.wait_for` with `ConnectorTimeoutError`
    - `cancel_tasks_with_timeout(tasks, timeout_ms)` — clean task cancellation with timeout
    - `create_named_task(coro, name, tasks_set)` — tracked task creation with auto-removal on completion
  - **Task tracking:** BinanceWsMockConnector tracks background tasks in `self._tasks` set with named prefix for clean shutdown
  - **Clean shutdown:** `close()` cancels all tracked tasks via `cancel_tasks_with_timeout()`, waits for completion within `close_timeout_ms`, clears events and cursor
  - **Stats tracking:** `MockConnectorStats` extended with `tasks_cancelled` and `tasks_force_killed` counters
- **Consequences:**
  - All timeout-related errors raise `ConnectorTimeoutError` (not `asyncio.TimeoutError`)
  - Closed connector operations raise `ConnectorClosedError` (not generic `RuntimeError`)
  - No zombie tasks after `close()` — all tasks cancelled and awaited
  - Tests: 47 tests in `test_data_connector.py` (was 28), including `TestConnectorTimeouts`, `TestConnectorCleanShutdown`, `TestConnectorErrorHierarchy`
  - Error hierarchy prepares for H2 (retries) and H4 (circuit breaker)
- **Alternatives:**
  - Per-call timeout parameters — rejected; TimeoutConfig as connector attribute is cleaner and consistent
  - Raw `asyncio.TimeoutError` — rejected; typed errors enable better error handling upstream
  - Fire-and-forget task cancellation — rejected; must await to ensure clean shutdown

## ADR-025 — Connector Hardening v2: Retry Utilities (PR-H2)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Following H1 (Timeouts + Error Hierarchy), H2 adds retry-with-backoff utilities for transient connector failures. Per M3 hardening roadmap, retry logic must be centralized, testable, and support deterministic testing (no real sleeps in unit tests).
- **Decision:**
  - **RetryPolicy** (`src/grinder/connectors/retries.py`): frozen dataclass configuring retry behavior:
    - `max_attempts` (default 3): total attempts (1 = no retries)
    - `base_delay_ms` (default 100): initial delay between retries
    - `max_delay_ms` (default 5000): maximum delay cap for backoff
    - `backoff_multiplier` (default 2.0): exponential backoff factor
    - `retry_on_timeout` (default True): whether to retry `ConnectorTimeoutError`
    - `compute_delay_ms(attempt)`: computes delay for given attempt with exponential backoff and cap
  - **RetryStats**: tracks `attempts`, `retries`, `total_delay_ms`, `last_error`, `errors` list
  - **is_retryable(error, policy)**: error classification function:
    - `ConnectorTransientError` → always retryable
    - `ConnectorTimeoutError` → retryable if `policy.retry_on_timeout`
    - `ConnectorNonRetryableError`, `ConnectorClosedError` → never retryable
    - Unknown exceptions → fail fast (not retried)
  - **retry_with_policy()**: async utility wrapping operations with retry logic:
    - `sleep_func` parameter for injecting fake sleep in tests (bounded-time testing)
    - `on_retry` callback for observability/logging
    - Returns `tuple[T, RetryStats]` for telemetry
  - **TransientFailureConfig**: mock connector configuration for simulating failures:
    - `connect_failures`: N connect attempts fail before success
    - `read_failures`: N read operations fail before success
    - `failure_message`: custom message for simulated errors
  - **BinanceWsMockConnector updated**: accepts `transient_failure_config` parameter, injects failures in `_do_connect()` and `iter_snapshots()`, tracks via `stats.transient_failures_injected`
- **Consequences:**
  - Retry logic is centralized and reusable for any connector
  - Tests are bounded-time (no real sleeps) via `sleep_func` injection
  - Mock connector can simulate transient failures for testing retry behavior
  - Tests: 35 new tests in `test_retries.py` covering policy validation, error classification, retry behavior, and mock integration
  - Prepares for H3 (idempotency) and H4 (circuit breaker)
- **Alternatives:**
  - Inline retry loops in connector methods — rejected; centralized utility is cleaner and testable
  - Real sleeps in tests — rejected; bounded-time tests are faster and deterministic
  - Retry all exceptions — rejected; fail fast on unknown errors prevents masking bugs

## ADR-026 — Connector Hardening v3: Idempotency (PR-H3)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Following H2 (Retries), H3 adds idempotency guarantees for write operations (place/cancel/amend). Without idempotency, retries can cause duplicate side-effects (double orders, double cancels). Per M3 hardening roadmap, write-path must be safe for retries.
- **Decision:**
  - **Idempotency Key Format**: `{scope}:{op}:{sha256_hex[:32]}` — deterministic hash of canonical payload, NOT random UUID
    - Key components for place: `symbol`, `side`, `price`, `quantity`, `level_id`
    - Key components for cancel: `order_id`
    - Key components for replace: `order_id`, `price`, `quantity`
    - **EXCLUDED from key**: `ts` (timestamp), nonces, random values — same intent at different times MUST produce same key
    - Canonicalization: JSON with sorted keys, Decimal normalized, None excluded
  - **IdempotencyStatus enum**: `INFLIGHT`, `DONE`, `FAILED`
  - **IdempotencyEntry** (`src/grinder/connectors/idempotency.py`): dataclass tracking:
    - `key`, `status`, `op_name`, `request_fingerprint`
    - `created_at`, `expires_at`, `result`, `error_code`
  - **IdempotencyStore protocol**: pluggable interface for storage:
    - `get(key)` → entry or None (expired entries return None)
    - `put_if_absent(key, entry, ttl_s)` → True if stored, False if exists
    - `mark_done(key, result)` → update status and extend TTL
    - `mark_failed(key, error_code)` → mark for retry
    - `purge_expired(now)` → clean up old entries
  - **InMemoryIdempotencyStore**: thread-safe in-memory implementation:
    - Injectable clock for testing (`_clock` parameter)
    - FAILED entries can be overwritten (retry allowed)
    - Default TTLs: 300s for INFLIGHT, 86400s for DONE
  - **IdempotentExchangePort** (`src/grinder/execution/idempotent_port.py`): wrapper for ExchangePort:
    - Wraps `place_order`, `cancel_order`, `replace_order` with idempotency checks
    - Flow: check store → if DONE return cached → if INFLIGHT raise conflict → put_if_absent → execute → mark_done
    - `fetch_open_orders` passes through (no idempotency for reads)
    - Stats tracking: `place_calls`, `place_cached`, `place_executed`, `place_conflicts`
  - **IdempotencyConflictError**: fast-fail on INFLIGHT duplicates (non-retryable)
  - **Integration with retries**: key created ONCE before retries, all retry attempts use same key → at most 1 side-effect
- **Consequences:**
  - Write operations are safe for retries — duplicate requests return cached result
  - Concurrent duplicate requests get deterministic behavior (fast-fail)
  - FAILED operations allow retry (entry overwritten)
  - Tests: 32 tests in `test_idempotency.py` covering key generation, store operations, port behavior, retry integration
  - Prepares for H4 (circuit breaker) and production Redis store
- **Alternatives:**
  - Random UUID keys — rejected; would break idempotency on retry (new key = new operation)
  - Wait-for-INFLIGHT pattern — rejected; fast-fail is simpler and avoids deadlocks in v1
  - Retry INFLIGHT conflicts — rejected; conflicts indicate concurrent duplicates, should fail fast

## ADR-027 — Connector Hardening v4: Circuit Breaker (PR-H4)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Following H2 (Retries) and H3 (Idempotency), H4 adds circuit breaker pattern to prevent cascading failures. Without circuit breaker, retries can hammer a degraded upstream indefinitely ("thundering herd"). Per M3 hardening roadmap, write-path must have fail-fast protection.
- **Decision:**
  - **Circuit States**: `CLOSED`, `OPEN`, `HALF_OPEN`
    - CLOSED: normal operation, failures counted toward threshold
    - OPEN: fast-fail all requests, cooldown timer running
    - HALF_OPEN: allow limited probes, success → CLOSED, failure → OPEN
  - **Per-operation tracking**: each operation (place/cancel/replace) has independent circuit state
    - Rationale: one operation failing (e.g., place) shouldn't block unrelated operations (e.g., cancel)
  - **CircuitBreakerConfig** (`src/grinder/connectors/circuit_breaker.py`):
    - `failure_threshold`: consecutive failures to trip OPEN (default: 5)
    - `open_interval_s`: seconds to stay OPEN before HALF_OPEN (default: 30)
    - `half_open_probe_count`: max probes allowed in HALF_OPEN (default: 1)
    - `success_threshold`: consecutive successes to close (default: 1)
    - `trip_on`: callable to determine if error counts as breaker-worthy
  - **trip_on semantics**:
    - `default_trip_on` counts: `ConnectorTransientError`, `ConnectorTimeoutError`
    - Does NOT count: `ConnectorNonRetryableError`, `IdempotencyConflictError`, `CircuitOpenError`
    - Rationale: only upstream failures should trip breaker, not client-side errors
  - **CircuitBreaker methods**:
    - `allow(op_name) -> bool`: check if operation allowed
    - `before_call(op_name)`: raises `CircuitOpenError` if not allowed
    - `record_success(op_name)`: count success, may close circuit
    - `record_failure(op_name, reason)`: count failure, may open circuit
    - `state(op_name) -> CircuitState`: get current state
  - **CircuitOpenError**: non-retryable error raised when circuit is OPEN
  - **Injectable clock**: `clock` parameter for deterministic testing
  - **Integration order** for write-path (implemented in IdempotentExchangePort):
    1. `breaker.before_call(op)` — fast-fail if OPEN
    2. Idempotency check (DONE → return, INFLIGHT → conflict)
    3. Execute operation
    4. `breaker.record_success(op)` or `breaker.record_failure(op, reason)` based on outcome
  - Rationale: breaker sits BEFORE idempotency to prevent any interaction with degraded upstream
  - **H4-01**: CircuitBreaker module implemented as library
  - **H4-02**: Wired into IdempotentExchangePort with optional breaker/trip_on parameters
- **Consequences:**
  - Degraded upstream triggers fast-fail, not retry storms
  - Per-operation isolation prevents one bad endpoint from blocking all operations
  - HALF_OPEN probes allow automatic recovery detection
  - Tests: 20+ tests in `test_circuit_breaker.py` covering state transitions, fast-fail, per-op isolation
  - Prepares for H5 (observability metrics for circuit state)
- **Alternatives:**
  - Global circuit (not per-op) — rejected; too coarse, one bad endpoint shouldn't block all
  - No HALF_OPEN, manual reset only — rejected; auto-recovery is essential for hands-off operation
  - Retry OPEN calls with backoff — rejected; defeats purpose of circuit breaker, use HALF_OPEN probes instead

## ADR-028 — Connector Observability v1: H2/H3/H4 Metrics (PR-H5)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Following H2 (Retries), H3 (Idempotency), and H4 (Circuit Breaker), we need Prometheus metrics to observe these components in production. Without metrics, operators cannot detect retry storms, idempotency conflicts, or circuit breaker trips until they cause visible failures.
- **Decision:**
  - **Metric naming convention:**
    - Prefix: `grinder_` (consistent with existing metrics)
    - Counter metrics: `_total` suffix
    - Gauge metrics: no suffix (state)
  - **H2 Retry metrics:**
    - `grinder_connector_retries_total{op, reason}` (counter): total retry events
    - Labels: `op` = operation name (place, cancel, replace, etc.), `reason` = transient, timeout, other
  - **H3 Idempotency metrics:**
    - `grinder_idempotency_hits_total{op}` (counter): cached result returned (no upstream call)
    - `grinder_idempotency_conflicts_total{op}` (counter): INFLIGHT duplicate rejected
    - `grinder_idempotency_misses_total{op}` (counter): new key, upstream call made
  - **H4 Circuit Breaker metrics:**
    - `grinder_circuit_state{op, state}` (gauge): 1 for current state, 0 for others
    - States: closed, open, half_open
    - `grinder_circuit_rejected_total{op}` (counter): calls rejected due to OPEN circuit
    - `grinder_circuit_trips_total{op, reason}` (counter): circuit trips (CLOSED → OPEN)
    - Reason: threshold (consecutive failures exceeded)
  - **Cardinality rules (low cardinality only):**
    - `op` — operation name only (place, cancel, replace, test_op, etc.)
    - `reason` — enum values only (transient, timeout, other, threshold)
    - `state` — enum values only (closed, open, half_open)
    - **EXCLUDED from labels:** symbol, order_id, idempotency_key, timestamps, client_id
    - Rationale: high-cardinality labels cause Prometheus scrape timeouts and memory issues
  - **Module:** `src/grinder/connectors/metrics.py` with:
    - `ConnectorMetrics` — dataclass tracking all counters/gauges
    - `to_prometheus_lines()` — Prometheus text format output
    - `get_connector_metrics()` / `reset_connector_metrics()` — global singleton
    - `CircuitMetricState` — enum for state gauge (CLOSED, OPEN, HALF_OPEN)
  - **Integration points:**
    - `retries.py` — records retry metric after each retry event
    - `idempotent_port.py` — records hit/conflict/miss metrics
    - `circuit_breaker.py` — records state/rejected/trip metrics
    - `metrics_builder.py` — includes connector metrics in `/metrics` output
    - `live_contract.py` — updated REQUIRED_METRICS_PATTERNS
  - **Compatibility policy:**
    - Metric names and label keys are stable contracts (same as gating metrics)
    - Adding new label values is safe (append-only)
    - Removing/renaming metrics or labels is a breaking change requiring ADR
    - Dashboards/alerts can rely on these metric names
- **Consequences:**
  - Operators can monitor retry rates, idempotency behavior, and circuit breaker health
  - Alerts can trigger on: high retry rate, frequent conflicts, circuit trips
  - Low cardinality ensures Prometheus stability at scale
  - Unit tests: 45+ tests in `test_connector_metrics.py` covering all metric types
- **Alternatives:**
  - High-cardinality labels (symbol, order_id) — rejected; Prometheus cardinality explosion
  - Separate metrics endpoints — rejected; single `/metrics` is simpler and standard
  - Histogram for retry delays — deferred; counters sufficient for v1

## ADR-029 — Live Connector v0: SafeMode + Hardening (M3-LC-01)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Production deployment requires a live WebSocket connector for real-time market data from Binance. Connector must integrate existing hardening (H2 retries, H4 circuit breaker, H5 metrics) while enforcing safety constraints to prevent accidental live trading.
- **Decision:**
  - **SafeMode enum** (`src/grinder/connectors/live_connector.py`):
    - `READ_ONLY` (default): Only read market data, no trading operations
    - `PAPER`: Read data + simulated trading (paper mode)
    - `LIVE_TRADE`: Full trading capability (requires explicit opt-in)
  - **Safe defaults:**
    - Default mode is `READ_ONLY` — must explicitly opt into trading
    - Default URL is Binance testnet (`wss://testnet.binance.vision/ws`) — must explicitly configure mainnet
    - Method `assert_mode(required_mode)` — raises `ConnectorNonRetryableError` if current mode is insufficient
  - **LiveConnectorV0** extends `DataConnector` ABC:
    - `connect()`: establish WebSocket connection with H2 retries and H4 circuit breaker
    - `close()`: clean shutdown with task cancellation
    - `stream_ticks()` / `iter_snapshots()`: yield `Snapshot` objects (DataConnector compliance)
    - `subscribe(symbols)`: add symbols to subscription
    - `reconnect()`: reconnect after failure, preserving `last_seen_ts` for idempotency
  - **H2/H4/H5 integration:**
    - `RetryPolicy` for transient connection failures (configurable attempts, backoff)
    - `CircuitBreaker` for fast-fail when upstream is degraded
    - `get_connector_metrics()` integration for observability
  - **Bounded-time testing:**
    - Injectable `clock` parameter for deterministic time control
    - Injectable `sleep_func` parameter for instant test execution
    - FakeClock + FakeSleep utilities in tests
  - **Configuration** (`LiveConnectorConfig`):
    - `mode`: SafeMode (default: READ_ONLY)
    - `symbols`: List of symbols to subscribe
    - `ws_url`: WebSocket URL (default: testnet)
    - `timeout_config`: TimeoutConfig for connect/read/close
    - `retry_policy`: RetryPolicy for connection retries
    - `circuit_breaker_config`: CircuitBreakerConfig for fast-fail
  - **Statistics** (`LiveConnectorStats`):
    - `ticks_received`, `connection_attempts`, `reconnections`, `retries`, `circuit_trips`, `timeouts`, `errors`
  - **V0 scope** (mock implementation):
    - `stream_ticks()` yields nothing (placeholder for real WebSocket integration)
    - Contract verified, hardening wired, integration tests pass
    - Real WebSocket integration deferred to v1
- **Consequences:**
  - Live connector is safe by default — must explicitly opt into trading
  - Existing hardening (H2/H4/H5) reused without duplication
  - Bounded-time tests complete in milliseconds, no flaky waits
  - Unit tests: 31 tests in `test_live_connector.py`
  - Integration tests: 6 tests in `test_live_connector_integration.py`
- **Alternatives:**
  - No SafeMode, rely on URL config only — rejected; too easy to accidentally trade on mainnet
  - Separate read/write connectors — rejected; unnecessary complexity for v0
  - Real WebSocket in v0 — rejected; contract-first approach, real integration in v1

## ADR-030 — Paper Write-Path v0: Simulated Trading (M3-LC-02)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Paper trading mode (`SafeMode.PAPER`) was defined in ADR-029 but had no write operations implemented. Need to add `place_order`, `cancel_order`, `replace_order` to `LiveConnectorV0` while maintaining safety guarantees (no network calls, deterministic behavior).
- **Decision:**
  - **PaperExecutionAdapter** (`src/grinder/connectors/paper_execution.py`):
    - In-memory order storage (no persistence)
    - Deterministic order ID generation: `{prefix}_{seq:08d}` (e.g., `PAPER_00000001`)
    - Injectable `clock` for deterministic timestamps
    - No network calls — pure simulation
  - **V0 Order Semantics (instant fill):**
    - `place_order`: Creates order → immediately FILLED (no market simulation)
    - `cancel_order`: If OPEN/PENDING → CANCELLED; if FILLED → `PaperOrderError`; if CANCELLED → idempotent return
    - `replace_order`: Cancel old order + place new order (cancel+new pattern), returns NEW order_id
  - **SafeMode Enforcement:**
    - `READ_ONLY`: All write operations raise `ConnectorNonRetryableError` (non-retryable by design)
    - `PAPER`: Write operations delegated to `PaperExecutionAdapter`
    - `LIVE_TRADE`: Not implemented in v0, raises `ConnectorNonRetryableError`
  - **Wire-up in LiveConnectorV0:**
    - `paper_adapter` property — `PaperExecutionAdapter | None`, initialized only in PAPER mode
    - `place_order(symbol, side, price, quantity, client_order_id)` → `OrderResult`
    - `cancel_order(order_id)` → `OrderResult`
    - `replace_order(order_id, new_price, new_quantity)` → `OrderResult`
    - All methods check `ConnectorState.CLOSED` first
  - **Types:**
    - `OrderRequest`: Frozen dataclass for place/replace input
    - `OrderResult`: Frozen dataclass for operation result (snapshot of order state)
    - `PaperOrder`: Mutable internal order record
    - `OrderType`: Enum (LIMIT, MARKET)
    - `PaperOrderError`: Non-retryable error for paper order failures
  - **No H2/H4 Wiring for Paper:**
    - Paper execution is synchronous and never fails transiently
    - Retries and circuit breaker not needed for in-memory simulation
    - Errors are logical (invalid state) not transient (network)
- **Mode → Operations → Backend Table:**
  | Mode        | stream_ticks | place_order | cancel_order | replace_order | Backend           |
  |-------------|--------------|-------------|--------------|---------------|-------------------|
  | READ_ONLY   | ✓            | ✗           | ✗            | ✗             | N/A               |
  | PAPER       | ✓            | ✓           | ✓            | ✓             | PaperExecutionAdapter |
  | LIVE_TRADE  | ✓            | ✗ (v0)      | ✗ (v0)       | ✗ (v0)        | Not implemented   |
- **Consequences:**
  - Paper mode provides full order lifecycle without exchange dependency
  - Order IDs are deterministic for replay/testing
  - No network calls in PAPER mode — tests are fast and reliable
  - Unit tests: 21 tests in `test_paper_execution.py`
  - Integration tests: 17 tests in `test_paper_write_path.py`
- **Out of Scope (v0):**
  - Real trading (testnet/mainnet) — deferred to M3-LC-03
  - Partial fills / slippage / L2 market simulation — deferred
  - PnL calculation and commission modeling — deferred
  - Persistent order storage — deferred
- **Alternatives:**
  - Shared adapter for PAPER and LIVE_TRADE — rejected; different requirements (mock vs real API)
  - Random order IDs — rejected; breaks determinism for replay
  - Async paper operations — rejected; unnecessary complexity for in-memory simulation


## ADR-031 — Auto-Sizing v1: Risk-Budget-Based Position Sizing (ASM-P2-01)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Grid policies used uniform `size_per_level` without risk awareness. Position sizes were manually configured and didn't adapt to account equity, drawdown limits, or adverse market scenarios. This made it hard to ensure portfolio-level risk stayed within bounds.
- **Decision:**
  - **AutoSizer Module** (`src/grinder/sizing/auto_sizer.py`):
    - Pure function computing size_schedule from risk parameters
    - Inputs: `equity`, `dd_budget`, `adverse_move`, `grid_shape`, `price`
    - Output: `SizeSchedule` with `qty_per_level[]` and risk metrics
  - **Core Formula:**
    ```
    max_loss_usd = equity * dd_budget
    total_qty_allowed = max_loss_usd / (price * adverse_move)
    qty_per_level = total_qty_allowed / n_levels  (for uniform mode)
    ```
    - Worst-case assumption: All levels fill on one side, price moves by `adverse_move`
    - Always rounds DOWN to stay within risk budget
  - **Sizing Modes:**
    - `UNIFORM`: Equal quantity at each level (default)
    - `PYRAMID`: Larger quantities at outer levels
    - `INVERSE_PYRAMID`: Smaller quantities at outer levels
  - **Integration with AdaptiveGridPolicy:**
    - `auto_sizing_enabled: bool` flag in config (default False for backward compat)
    - When enabled, `_compute_size_schedule()` uses AutoSizer instead of legacy uniform sizing
    - Falls back to legacy if equity/dd_budget/adverse_move are missing
  - **Determinism:**
    - Pure function: same inputs → same outputs
    - Integer bps for risk parameters
    - Decimal arithmetic with explicit rounding
- **Risk Bound Guarantee:**
  ```
  worst_case_loss = sum(qty_i * price) * adverse_move
  constraint: worst_case_loss <= equity * dd_budget
  ```
  - Due to ROUND_DOWN, actual utilization is always <= 100%
  - `risk_utilization` metric tracks efficiency (should be ~90-99%)
- **Configuration:**
  ```python
  AdaptiveGridConfig(
      auto_sizing_enabled=True,
      equity=Decimal("10000"),          # Account equity
      dd_budget=Decimal("0.20"),        # 20% max drawdown
      adverse_move=Decimal("0.25"),     # 25% worst-case price move
      auto_sizer_config=AutoSizerConfig(
          sizing_mode=SizingMode.UNIFORM,
          min_qty=Decimal("0.0001"),    # Exchange minimum
          qty_precision=8,
      ),
  )
  ```
- **Consequences:**
  - Size schedules automatically adapt to account equity and risk parameters
  - Risk bound is enforced: worst-case loss stays within dd_budget
  - Backward compatible: legacy uniform sizing when auto_sizing_enabled=False
  - Unit tests: 36 tests in `test_auto_sizer.py`
  - Integration tests: 5 tests in `test_adaptive_policy.py::TestAutoSizingIntegration`
- **Out of Scope (v1):**
  - Multi-symbol DD allocation (deferred to ASM-P2-02)
  - Dynamic equity tracking (equity is static config, not live P&L)
  - Position-aware sizing (doesn't account for existing inventory)
  - L2 depth-aware sizing
- **Alternatives:**
  - Kelly criterion sizing — rejected; requires probability estimates we don't have
  - VaR-based sizing — rejected; needs distribution assumptions
  - Fixed notional sizing — rejected; doesn't scale with account size

## ADR-032 — DD Allocator v1: Portfolio-to-Symbol Budget Distribution (ASM-P2-02)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** ASM-P2-01 (AutoSizer) computes per-symbol size schedules from `dd_budget`, but doesn't address how to distribute a portfolio-level drawdown budget across multiple symbols. Manual allocation is error-prone and doesn't account for relative risk (volatility tiers).
- **Decision:**
  - **DdAllocator Module** (`src/grinder/sizing/dd_allocator.py`):
    - Pure function distributing portfolio DD budget across symbols
    - Inputs: `equity`, `portfolio_dd_budget`, `candidates[]` (symbol, tier, weight, enabled)
    - Output: `AllocationResult` with per-symbol budgets and residual
  - **Algorithm:**
    1. Filter to enabled symbols only
    2. Compute `risk_weight = user_weight / tier_factor` for each symbol
    3. Normalize weights to sum to 1.0
    4. Multiply normalized weights by `portfolio_budget_usd`
    5. ROUND_DOWN to `budget_precision` decimal places
    6. Residual goes to cash reserve (not reallocated)
  - **Tier Factors (v1):**
    - `LOW`: 1.0 (lowest risk, gets most budget)
    - `MED`: 1.5
    - `HIGH`: 2.0 (highest risk, gets least budget)
    - Higher factor = higher risk = smaller budget allocation
  - **Invariants (must always hold):**
    1. **Non-negativity:** All budgets >= 0
    2. **Conservation:** `sum(budgets) + residual == portfolio_budget` (exact with Decimal)
    3. **Determinism:** Same inputs → same outputs (sorted by symbol)
    4. **Monotonicity:** Larger portfolio budget → no symbol budget decreases
    5. **Tier ordering:** At equal weights, HIGH <= MED <= LOW budget
  - **Integration with AutoSizer:**
    - DdAllocator output: `allocations[symbol]` = per-symbol dd_budget fraction
    - Feed to AdaptiveGridConfig: `dd_budget=allocations[symbol]`
    - AutoSizer then computes size_schedule using this per-symbol budget
  - **Residual Policy:**
    - ROUND_DOWN creates small residual (< sum of rounding errors)
    - Residual stays in `residual_usd` field (cash reserve)
    - Not reallocated to avoid complexity; caller can add to lowest-risk symbol if desired
- **Configuration:**
  ```python
  allocator = DdAllocator(DdAllocatorConfig(
      tier_factors={
          RiskTier.LOW: Decimal("1.0"),
          RiskTier.MED: Decimal("1.5"),
          RiskTier.HIGH: Decimal("2.0"),
      },
      budget_precision=2,      # Round to cents
      min_budget_usd=Decimal("1.0"),  # Below this = 0
  ))

  result = allocator.allocate(
      equity=Decimal("100000"),
      portfolio_dd_budget=Decimal("0.20"),  # 20% total
      candidates=[
          SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH),
          SymbolCandidate(symbol="ETHUSDT", tier=RiskTier.MED),
          SymbolCandidate(symbol="BNBUSDT", tier=RiskTier.LOW),
      ],
  )
  # result.allocations = {"BNBUSDT": 0.0869..., "BTCUSDT": 0.0434..., "ETHUSDT": 0.0580...}
  ```
- **Example (3 symbols, equal weights, default tiers):**
  | Symbol | Tier | Factor | Risk Weight | Normalized | Budget USD |
  |--------|------|--------|-------------|------------|------------|
  | BNBUSDT | LOW | 1.0 | 1.0 | 0.4348 | $8,695.65 |
  | ETHUSDT | MED | 1.5 | 0.667 | 0.2899 | $5,797.10 |
  | BTCUSDT | HIGH | 2.0 | 0.5 | 0.2174 | $4,347.83 |
  | **Total** | | | | 1.0 | $18,840.58 |
  | Residual | | | | | $1,159.42 |
- **Consequences:**
  - Portfolio-level risk is automatically distributed to symbols
  - Higher-risk symbols get smaller budgets (conservative)
  - Custom weights allow overriding tier-based allocation
  - Unit tests: 28 tests in `test_dd_allocator.py` (all 5 invariants covered)
  - Integration tests: 3 tests in `test_adaptive_policy.py::TestDdAllocatorIntegration`
- **Out of Scope (v1):**
  - Dynamic correlation-aware allocation (Markowitz-style)
  - Real-time volatility estimation for tier assignment
  - Learning/EMA adaptation of tier factors
  - Residual reallocation strategies
- **Alternatives:**
  - Equal allocation — rejected; ignores risk differences
  - Inverse-volatility weighting — rejected; requires vol estimates we may not have
  - Risk parity — rejected; needs covariance matrix

## ADR-033 — Drawdown Guard Wiring v1: Intent-Based Risk Blocking (ASM-P2-03)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** ASM-P2-01/02 provide auto-sizing and DD allocation, but there's no mechanism to enforce risk limits at runtime. When portfolio or symbol DD exceeds limits, the system should block risk-increasing orders while allowing risk-reducing ones. This requires a deterministic guard that can be wired into the policy/execution pipeline.
- **Decision:**
  - **DrawdownGuardV1 Module** (`src/grinder/risk/drawdown_guard_v1.py`):
    - Tracks DD at portfolio level AND per-symbol level
    - GuardState: `NORMAL` | `DRAWDOWN`
    - OrderIntent: `INCREASE_RISK` | `REDUCE_RISK` | `CANCEL`
    - Transition NORMAL → DRAWDOWN when:
      - Portfolio DD >= portfolio_dd_limit, OR
      - Symbol loss >= symbol_dd_budget
  - **Intent Classification (v1 rules):**
    - `INCREASE_RISK`: New positions, grid entries, orders that increase exposure
    - `REDUCE_RISK`: Closes, exits, flatten intents that decrease exposure
    - `CANCEL`: Cancellation of existing orders
  - **Allow Decision Logic:**
    | State | Intent | Allowed | Reason |
    |-------|--------|---------|--------|
    | NORMAL | INCREASE_RISK | ✓ | NORMAL_STATE |
    | NORMAL | REDUCE_RISK | ✓ | NORMAL_STATE |
    | NORMAL | CANCEL | ✓ | CANCEL_ALWAYS_ALLOWED |
    | DRAWDOWN | INCREASE_RISK | ✗ | DD_PORTFOLIO_BREACH or DD_SYMBOL_BREACH |
    | DRAWDOWN | REDUCE_RISK | ✓ | REDUCE_RISK_ALLOWED |
    | DRAWDOWN | CANCEL | ✓ | CANCEL_ALWAYS_ALLOWED |
  - **No Auto-Recovery:**
    - Once in DRAWDOWN, stays there until explicit `reset()` call
    - Prevents flapping and ensures deterministic replay behavior
    - Reset intended for new session/day start only
  - **Global DRAWDOWN State (P2-04a):**
    - Guard state is GLOBAL, not per-symbol
    - When ANY symbol breaches its DD budget → entire guard transitions to DRAWDOWN
    - In DRAWDOWN, INCREASE_RISK is blocked for ALL symbols, not just the breached one
    - Rationale: Portfolio risk is correlated; if one symbol is losing, reducing exposure everywhere is prudent
    - Example: BTCUSDT breaches $1000 budget → ETHUSDT INCREASE_RISK also blocked with reason `DD_SYMBOL_BREACH`
  - **Reduce-Only Semantics (P2-04b):**
    - In DRAWDOWN, `REDUCE_RISK` intent is always allowed
    - Reduce-only action: `PaperEngine.flatten_position(symbol, price, ts)`
    - Closes entire position at given price (no partial reduce in v0)
    - If LONG → generates SELL fill; if SHORT → generates BUY fill
    - Deterministic: same inputs → same fill output
    - Guards checked: `guard.allow(REDUCE_RISK, symbol)` → always allowed in any state
    - Use case: emergency position exit when DD limit breached
  - **Reset Hook (P2-04c):**
    - `PaperEngine.reset_dd_guard_v1()` — returns guard to NORMAL state
    - Use case: new session/day start
    - Transition: DRAWDOWN → NORMAL (or NORMAL → NORMAL, safe no-op)
    - After reset: `INCREASE_RISK` is allowed again
    - Also called by `PaperEngine.reset()` (general reset)
    - Returns: `{reset: bool, state_before: str, state_after: str, reason: str}`
  - **Reason Codes (stable, low-cardinality):**
    - `NORMAL_STATE`: All intents allowed in normal operation
    - `REDUCE_RISK_ALLOWED`: Reduce-only allowed in DRAWDOWN
    - `CANCEL_ALWAYS_ALLOWED`: Cancels always permitted
    - `DD_PORTFOLIO_BREACH`: Blocked due to portfolio DD limit
    - `DD_SYMBOL_BREACH`: Blocked due to symbol DD limit
  - **Wiring Point:** `src/grinder/paper/engine.py` Step 3.5 (lines 717-767)
    - Guard sits BETWEEN gating check AND execution (BEFORE `ExecutionEngine.evaluate()`)
    - Location: After `if not gating_result.allowed: return ...` block
    - Flow: gating → **DD guard check** → execution
    - On each snapshot:
      1. Compute current equity from ledger (initial_capital + realized + unrealized)
      2. Compute symbol losses (negative total PnL → positive loss value)
      3. Call `guard.update(equity_start, equity_current, symbol_losses)`
      4. If plan has entry levels, call `guard.allow(OrderIntent.INCREASE_RISK, symbol)`
      5. If blocked, return early with `blocked_by_dd_guard_v1=True`
    - Enabled via `dd_guard_v1_enabled=True` in PaperEngine constructor
  - **Loss Calculation (v1):**
    - Uses realized PnL (simpler, deterministic)
    - Portfolio DD = (equity_start - equity_current) / equity_start
    - Symbol loss = absolute USD loss (positive value)
- **Configuration:**
  ```python
  guard = DrawdownGuardV1(DrawdownGuardV1Config(
      portfolio_dd_limit=Decimal("0.20"),  # 20%
      symbol_dd_budgets={
          "BTCUSDT": Decimal("1000"),
          "ETHUSDT": Decimal("500"),
      },
  ))

  # On each tick
  guard.update(
      equity_current=equity,
      equity_start=session_start_equity,
      symbol_losses=ledger.get_realized_losses(),
  )

  # Before placing order
  decision = guard.allow(OrderIntent.INCREASE_RISK, symbol="BTCUSDT")
  if not decision.allowed:
      logger.warning("Blocked by DD guard: %s", decision.reason.value)
      return  # Skip order
  ```
- **Consequences:**
  - Risk limits are enforced deterministically at runtime
  - Policy can't accidentally increase risk beyond limits
  - Reduce-only orders always pass (allows position unwinding)
  - Unit tests: 39 tests in `test_drawdown_guard_v1.py` (34 guard + 5 wiring)
  - All 5 invariants from v0 DrawdownGuard preserved
  - Wiring integration tests verify blocking behavior in PaperEngine
- **Out of Scope (v1):**
  - Auto-recovery / hysteresis / cooldown
  - Partial degradation states (WARN, DEGRADED)
  - Mark-to-market PnL (uses realized only)
  - Kill-switch integration (separate from DD guard)
- **Alternatives:**
  - Auto-recovery with cooldown — rejected; non-deterministic, risk of flapping
  - Single state (just block all in DD) — rejected; need reduce-only for position exit
  - Probabilistic blocking — rejected; breaks determinism

---

## ADR-034: Paper Realism v0.1 — Tick-Delay Fills (LC-03)

- **Status:** Accepted
- **Context:**
  - Paper trading previously used instant fills (v0) or immediate crossing fills (v1)
  - Real exchanges have latency: orders stay OPEN before being matched
  - Instant fills make backtesting overly optimistic (no adverse selection modeling)
  - Need deterministic fill model that's more realistic without randomness
- **Decision:**
  - Implement **tick-delay fill model** in `PaperEngine`
  - New parameter: `fill_after_ticks: int = 0` (0 = current behavior, 1+ = delay)
  - Order lifecycle: `PLACE → OPEN → (N ticks) → FILLED` (if price crosses)
  - Cancel semantics: Cancel OPEN order before fill-eligible prevents fill
  - Replace semantics: Replace OPEN order = cancel + place new (both get new tick count)
- **Fill Rule (tick-count model):**
  ```
  Order placed at tick T fills at tick T + N (where N = fill_after_ticks)
  IF price crossing condition is met:
    - BUY fills if mid_price <= limit_price
    - SELL fills if mid_price >= limit_price
  ```
- **Order State Tracking:**
  - Added `placed_tick: int` to `OrderRecord` (tracks tick when placed)
  - Each symbol has its own `tick_counter` in `ExecutionState`
  - Orders in `FILLED` state are not re-checked for fills
  - Orders in `CANCELLED` state are skipped
- **Implementation:**
  - `check_pending_fills()` in `fills.py` — checks existing OPEN orders
  - `_update_orders_to_filled()` in `engine.py` — transitions order state
  - `_snapshot_counter` in `PaperEngine` — global counter (for debugging)
- **Determinism:**
  - Same inputs → same fills (no randomness, no wall-clock)
  - Fill order is deterministic (sorted by order_id)
  - Tick count is discrete (integer), not time-based
- **Backward Compatibility:**
  - `fill_after_ticks=0` preserves existing behavior (instant/crossing)
  - Default is 0 to avoid breaking existing fixtures/digests
  - `placed_tick` defaults to 0 in `OrderRecord.from_dict()` for old data
- **Configuration Example:**
  ```python
  engine = PaperEngine(
      fill_after_ticks=1,  # Fill on next tick after placement
      fill_mode="crossing",  # Price must still cross for fill
  )
  ```
- **Consequences:**
  - More realistic simulation (orders don't fill instantly)
  - Cancel-before-fill is now possible (order management testing)
  - Grid reconciliation works correctly with OPEN orders
  - 18 unit tests in `test_paper_realism.py`
  - Determinism preserved (replay produces identical results)
- **Out of Scope (v0.1):**
  - Partial fills
  - Slippage / fees
  - Order book depth (L2) simulation
  - Probabilistic models (random delays)
  - Time-based delays (uses tick count, not milliseconds)
- **Future Extensions:**
  - v0.2: Price-sensitive delay (further orders = longer delay)
  - v0.3: Partial fills based on available liquidity
  - v1.0: L2-based fill simulation

## ADR-035: BinanceExchangePort v0.1 — Live Write-Path (LC-04)
- **Date:** 2026-02-04
- **Status:** Accepted
- **Context:**
  - Paper trading uses NoOpExchangePort (no real exchange calls)
  - Need real Binance Spot API integration for live trading
  - Must be impossible to accidentally trade real money (safety by default)
  - Must integrate with existing H2/H3/H4/H5 hardening (retries, idempotency, circuit breaker, metrics)
  - DoD v2 requires: testnet only in v0.1, symbol whitelist, injectable HTTP client for testing
- **Decision:**
  - **BinanceExchangePort** implements `ExchangePort` protocol (`src/grinder/execution/binance_port.py`)
  - **SafeMode enforcement:**
    - `SafeMode.READ_ONLY` (default): blocks all write operations → 0 risk
    - `SafeMode.PAPER`: blocks write operations (use PaperExecutionAdapter instead)
    - `SafeMode.LIVE_TRADE`: explicit opt-in required for real API calls
    - Mode validation happens BEFORE any HTTP call
  - **Mainnet forbidden in v0.1:**
    - Config rejects any URL containing `api.binance.com`
    - Default URL: `https://testnet.binance.vision` (safe by design)
    - Raises `ConnectorNonRetryableError` if mainnet URL detected
  - **Injectable HTTP client:**
    - `HttpClient` protocol for HTTP operations
    - `NoopHttpClient` for dry-run testing (0 real HTTP calls)
    - Allows proving dry-run mode makes no network I/O
  - **Symbol whitelist:**
    - `symbol_whitelist` config parameter
    - Blocks trades for symbols not in list (empty = all allowed)
    - Raises `ConnectorNonRetryableError` if symbol blocked
  - **Error mapping:**
    - 5xx → `ConnectorTransientError` (retryable)
    - 429 → `ConnectorTransientError` (rate limit, retryable)
    - 418 → `ConnectorNonRetryableError` (IP ban, not retryable)
    - 4xx → `ConnectorNonRetryableError` (client error, not retryable)
    - Binance -1000 series → `ConnectorTransientError` (WAF, overload)
    - Binance -1100/-2000 series → `ConnectorNonRetryableError` (validation)
  - **H3 idempotency via IdempotentExchangePort wrapper:**
    - Wrap `BinanceExchangePort` with `IdempotentExchangePort` for production use
    - Replace = cancel + place with shared idempotency key
    - Safe under retries: same request key → 1 side-effect
  - **H4 circuit breaker via IdempotentExchangePort:**
    - Optional `breaker` parameter in wrapper
    - Fast-fail when upstream degraded (OPEN state)
- **Implementation:**
  - `BinanceExchangePort.place_order()`: POST /api/v3/order
  - `BinanceExchangePort.cancel_order()`: DELETE /api/v3/order
  - `BinanceExchangePort.replace_order()`: cancel + place (with contextlib.suppress)
  - `BinanceExchangePort.fetch_open_orders()`: GET /api/v3/openOrders
  - Order ID format: `grinder_{symbol}_{level_id}_{ts}_{counter}`
  - HMAC-SHA256 signing for authenticated endpoints
- **Testing:**
  - 28 unit tests in `test_binance_port.py`
  - Dry-run tests prove `NoopHttpClient` makes 0 HTTP calls
  - SafeMode tests prove READ_ONLY/PAPER block writes
  - Mainnet tests prove api.binance.com is rejected
  - Whitelist tests prove unlisted symbols are blocked
  - Error mapping tests prove correct classification
  - Idempotency integration tests prove caching works
  - Circuit breaker integration tests prove fast-fail works
- **Consequences:**
  - Live trading possible with explicit `SafeMode.LIVE_TRADE` opt-in
  - Cannot accidentally trade on mainnet (forbidden in v0.1)
  - Injectable HTTP client enables deterministic testing
  - Integrates cleanly with existing H2/H3/H4/H5 stack
- **SafeMode vs KillSwitch (Clarification):**
  - **SafeMode** is a static, per-run configuration that controls whether the port CAN make HTTP calls
    - Set at construction time, doesn't change during a run
    - `READ_ONLY` → 0 writes allowed (safe by default)
    - `LIVE_TRADE` → writes allowed (explicit opt-in)
  - **KillSwitch** is a dynamic, runtime latch that blocks trading when triggered (ADR-013)
    - Checked at orchestrator level (PaperEngine), NOT built into BinanceExchangePort
    - Can trip mid-run (e.g., drawdown exceeded)
    - Once triggered, stays triggered until explicit reset
  - **Difference from RISK_SPEC.md "arming":**
    - RISK_SPEC.md describes KillSwitch with `armed` flag (must arm before trigger works)
    - Current implementation is simpler: `trip()` always works, `is_triggered` is the guard
    - SafeMode does NOT replace arming - they're orthogonal:
      - SafeMode = "can the port make API calls at all?"
      - KillSwitch = "should we block trading right now?"
  - **Integration pattern:**
    - Orchestrator checks `kill_switch.is_triggered` BEFORE calling `port.place_order()`
    - If triggered, skip the call (0 HTTP calls, no SafeMode check needed)
    - If not triggered, SafeMode validation happens inside the port
- **dry_run mode:**
  - `BinanceExchangePortConfig.dry_run=True` returns synthetic results WITHOUT calling http_client
  - Distinct from NoopHttpClient (which still receives calls for recording)
  - `dry_run=True` guarantees 0 `http_client.request()` calls
- **Out of Scope (v0.1):**
  - WebSocket streaming (uses HTTP REST only)
  - Mainnet support (testnet only)
  - Futures/margin trading (spot only)
  - Real AiohttpClient implementation (only protocol defined)
  - Rate limiting (handled by H4 circuit breaker)

## ADR-036 — LiveEngineV0: Live Write-Path Wiring (LC-05)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** We have individual components (BinanceExchangePort, IdempotentExchangePort, DrawdownGuardV1, KillSwitch) but no orchestration layer that wires them together for live trading. PaperEngine handles paper mode but shouldn't be polluted with live I/O concerns.
- **Decision:**
  - **New module:** `grinder.live` with `LiveEngineV0` class
  - **Architecture:** Thin wrapper around PaperEngine that forwards execution to real ExchangePort
  - **Arming model (two-layer safety):**
    - `LiveEngineConfig.armed: bool = False` — master switch, blocks ALL writes when False
    - `LiveEngineConfig.mode: SafeMode = READ_ONLY` — secondary check via port
    - Both `armed=True` AND `mode=LIVE_TRADE` required for actual writes
    - Engine arming is checked BEFORE port SafeMode (faster rejection)
  - **Intent classification:**
    - `ActionType.CANCEL` → `OrderIntent.CANCEL` (always allowed)
    - `ActionType.PLACE` → `OrderIntent.INCREASE_RISK` (blocked in DRAWDOWN)
    - `ActionType.REPLACE` → `OrderIntent.INCREASE_RISK` (blocked in DRAWDOWN)
    - `ActionType.NOOP` → `OrderIntent.CANCEL` (safe, skipped)
  - **Kill-switch semantics:**
    - `kill_switch_active=True` → blocks PLACE/REPLACE, allows CANCEL
    - Enables "reduce only" mode to exit positions when triggered
  - **Safety gate ordering (checked in this order):**
    1. Arming check (`armed=False` → blocked)
    2. Mode check (`mode≠LIVE_TRADE` → blocked)
    3. Kill-switch check (if active, blocks INCREASE_RISK)
    4. Symbol whitelist check
    5. DrawdownGuardV1.allow(intent) check
    6. Execute via exchange_port
  - **Hardening chain (H2/H3/H4):**
    - H3: IdempotentExchangePort wraps base port for idempotency
    - H4: CircuitBreaker integrated into IdempotentExchangePort for fast-fail
    - H2: RetryPolicy for transient errors (exponential backoff)
    - Chain: `LiveEngineV0 → IdempotentExchangePort(H3+H4) → BinanceExchangePort`
  - **Error handling:**
    - `ConnectorNonRetryableError` → fail immediately (no retries)
    - `ConnectorTransientError` → retry with backoff
    - `CircuitOpenError` → fail immediately (breaker OPEN)
    - Other `ConnectorError` → check `is_retryable()` to decide
- **Implementation:**
  - `src/grinder/live/config.py`: `LiveEngineConfig` dataclass
  - `src/grinder/live/engine.py`: `LiveEngineV0` class
  - `LiveEngineV0.process_snapshot(snapshot)` → `LiveEngineOutput`
  - `LiveAction` dataclass tracks status, block_reason, attempts
  - `BlockReason` enum for gate-specific rejection codes
- **Testing:**
  - 16 unit tests in `test_live_engine.py`
  - A) Safety/arming (4): armed=False, mode=READ_ONLY, kill-switch, whitelist
  - B) Drawdown guard (3): NORMAL allows, DRAWDOWN blocks INCREASE_RISK, allows CANCEL
  - C) Idempotency+retry (3): duplicate→cached, transient→retry, non-retryable→no-retry
  - D) Circuit breaker (2): trip→reject, half-open→close
- **Consequences:**
  - Live trading possible with explicit `armed=True` + `mode=LIVE_TRADE`
  - By default nothing writes (`armed=False`)
  - DrawdownGuardV1 blocks risk-increasing actions in DRAWDOWN state
  - Idempotency ensures 1 side-effect even under retries
  - Circuit breaker fast-fails when upstream degraded
- **Out of Scope (v0):**
  - Reconciliation via `fetch_open_orders()` (deferred to LC-06)
  - Multi-symbol support (single-symbol focus)
  - Persistent state recovery (deferred)
  - Real E2E testnet testing (requires LC-07 runbook)
  - Engine-level metrics (H5) beyond existing port metrics

## ADR-038 — Testnet Smoke Harness (LC-07)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Need an E2E smoke test for Binance Testnet to verify live trading connectivity and order flow. Must be safe-by-construction: cannot accidentally trade on mainnet or place real orders without explicit opt-in.
- **Decision:**
  - **Safe-by-construction guards:**
    - `--dry-run` by default (no real HTTP calls, simulated place/cancel)
    - Requires `--confirm TESTNET` for real orders
    - Mainnet FORBIDDEN (blocked in BinanceExchangePort)
    - Requires `ARMED=1` + `ALLOW_TESTNET_TRADE=1` env vars for real trades
    - Kill-switch blocks PLACE/REPLACE, allows CANCEL
  - **Script:** `scripts/smoke_live_testnet.py`
    - `RequestsHttpClient`: Real HTTP via requests library
    - `SmokeResult`: Tracks simulated vs real outcomes
    - Clear output: `** SIMULATED - No real HTTP calls made **` in dry-run
    - Order ID prefixed with `SIM_` in dry-run mode
  - **Runbook:** `docs/runbooks/08_SMOKE_TEST_TESTNET.md`
    - Step-by-step procedure for testnet smoke test
    - Failure scenarios and resolution
    - Operator checklist
  - **Kill-switch extension:** `docs/runbooks/04_KILL_SWITCH.md`
    - Added kill-switch behavior table (PLACE blocked, CANCEL allowed)
    - Testnet verification procedure
- **E2E Run Status:**
  - Smoke harness is READY and tested in dry-run mode
  - Real E2E run is OPERATOR-DEPENDENT (requires Binance testnet credentials)
  - Binance testnet may require KYC verification for API key generation
  - Real E2E run NOT executed as part of this PR
- **Verification (dry-run):**
  ```bash
  PYTHONPATH=src python -m scripts.smoke_live_testnet  # dry-run
  PYTHONPATH=src python -m scripts.smoke_live_testnet --kill-switch  # kill-switch test
  ```
- **Consequences:**
  - Operators can verify testnet connectivity when they have credentials
  - Dry-run mode proves script logic works without external dependencies
  - Mainnet protection hardcoded at BinanceExchangePort level
  - Kill-switch behavior documented and testable
- **Out of Scope (v0.1):**
  - Mainnet trading (superseded by ADR-039)
  - Automated CI execution of real testnet orders (no credentials in CI)
  - Fill verification (order is far-from-market, should not fill)

---

## ADR-039 — Mainnet Trade Smoke v0.1 (LC-08b)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Testnet unavailable due to KYC requirements. Mainnet trading is available with a dedicated test budget. Need safe-by-construction guards to enable mainnet smoke testing without risk of accidental large trades.
- **Decision:**
  - **Multi-layer safety guards:**
    1. `allow_mainnet=False` by default (must explicitly opt-in in config)
    2. `ALLOW_MAINNET_TRADE=1` env var required (prevents accidental mainnet)
    3. `symbol_whitelist` REQUIRED for mainnet (no wildcard trading)
    4. `max_notional_per_order` REQUIRED for mainnet (caps each order notional)
    5. `max_orders_per_run=1` default (single order per script run)
    6. `max_open_orders=1` default (single concurrent order)
    7. `ARMED=1` env var required (same as testnet)
  - **BinanceExchangePort changes:**
    - Conditional mainnet allow (was: unconditional block)
    - `is_mainnet()` method for URL detection
    - `_validate_notional()` enforces notional limit
    - `_validate_order_count()` enforces order count limit
    - `reset()` clears order count for new runs
  - **Smoke script changes:**
    - `--confirm MAINNET_TRADE` flag for mainnet mode
    - `--max-notional` argument (default: $50)
    - Clear banner: `*** LIVE MAINNET MODE ***`
    - Output shows `is_mainnet: True`, `base_url: api.binance.com`
  - **Guard validation order (fail-fast):**
    1. Config validation (allow_mainnet, env var, whitelist, max_notional)
    2. Per-order validation (notional limit, order count)
  - **Runbook:** `docs/runbooks/09_MAINNET_TRADE_SMOKE.md`
    - Prerequisites (credentials, test budget, env vars)
    - Step-by-step procedure
    - Verification checklist
    - Emergency procedures
- **Test coverage:**
  - `TestMainnetGuards` class (7 tests) in `tests/unit/test_binance_port.py`
  - Tests: env var required, whitelist required, max_notional required, limit enforcement
- **Verification:**
  ```bash
  # Dry-run (default)
  PYTHONPATH=src python3 -m scripts.smoke_live_testnet

  # Real mainnet order (budgeted)
  BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_MAINNET_TRADE=1 \
      PYTHONPATH=src python3 -m scripts.smoke_live_testnet --confirm MAINNET_TRADE
  ```
- **Consequences:**
  - Mainnet smoke testing enabled with strict guardrails
  - 7+ layers of safety prevent accidental large trades
  - Order count limits prevent runaway scripts
  - Notional limits cap worst-case per-order loss
  - All guards verified by unit tests
- **Out of Scope (v0.1):**
  - Multi-symbol mainnet trading (single symbol per run)
  - Automated mainnet E2E in CI (manual operator runs only)
  - Fill verification (order placed far from market)

## ADR-040 — Futures USDT-M Mainnet Smoke v0.1 (LC-08b-F)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Target execution venue is Binance Futures USDT-M (`fapi.binance.com`), not Spot. ADR-039 implemented Spot mainnet smoke (`api.binance.com`), but this does not validate the actual execution path. Need futures-specific port and smoke harness.
- **Decision:**
  - **New module:** `src/grinder/execution/binance_futures_port.py`
    - `BinanceFuturesPortConfig`: Configuration with futures-specific guards
    - `BinanceFuturesPort`: Exchange port implementing futures API
    - Base URL: `https://fapi.binance.com` (mainnet), `https://testnet.binancefuture.com` (testnet)
  - **Futures-specific safety guards:**
    - Same 7 layers as ADR-039 (allow_mainnet, env var, whitelist, notional, order count)
    - `target_leverage`: Enforce leverage setting (default: 1x = no leverage)
    - Position mode logging (hedge vs one-way)
    - Margin type logging (isolated vs cross)
  - **Position cleanup on fill:**
    - After order placement + cancel, check for residual position
    - If position exists → close with market order (`reduceOnly=True`)
    - Final verification: position should be 0
  - **Smoke script:** `scripts/smoke_futures_mainnet.py`
    - `--confirm FUTURES_MAINNET_TRADE` flag for live mode
    - 7-step procedure: account info → leverage → position check → order → cancel → cleanup → verify
    - Clear output: leverage, position mode, order details, cleanup status
  - **API endpoints:**
    - `POST /fapi/v1/order` (place order)
    - `DELETE /fapi/v1/order` (cancel order)
    - `POST /fapi/v1/leverage` (set leverage)
    - `GET /fapi/v2/positionRisk` (check position)
    - `GET /fapi/v2/account` (account info)
    - `GET /fapi/v1/positionSide/dual` (position mode)
- **Test coverage:**
  - `tests/unit/test_binance_futures_port.py` (30 tests)
  - Dry-run tests (0 HTTP calls)
  - SafeMode enforcement tests
  - Mainnet guard tests
  - Notional/order count limit tests
  - Leverage validation tests
- **Consequences:**
  - Futures USDT-M execution path now validated
  - Same guardrails as Spot (ADR-039)
  - Leverage enforced at 1x by default (no margin amplification)
  - Position cleanup ensures no residual exposure
- **Runbook:** `docs/runbooks/10_FUTURES_MAINNET_TRADE_SMOKE.md`
- **Spot vs Futures:**
  - ADR-039 (LC-08b): Spot mainnet smoke → validates Spot path
  - ADR-040 (LC-08b-F): Futures mainnet smoke → validates Futures USDT-M path
  - Target production venue: Futures USDT-M

---

## ADR-037 — LiveFeed: Live Read-Path Pipeline (LC-06)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** LiveEngineV0 (ADR-036) handles write-path (order execution), but we need a read-only data pipeline to convert Binance WebSocket bookTicker stream into FeatureSnapshot objects for the policy layer. This must be strictly read-only with ZERO imports from execution module.
- **Decision:**
  - **New modules:**
    - `grinder.connectors.binance_ws`: WebSocket client for Binance bookTicker stream
    - `grinder.live.types`: LiveFeaturesUpdate, WsMessage, BookTickerData, LiveFeedStats
    - `grinder.live.feed`: LiveFeed pipeline orchestrator
  - **Architecture:** DataConnector → Snapshot → FeatureEngine → LiveFeaturesUpdate
  - **Hard read-only constraint:**
    - `feed.py` MUST NOT import from `grinder.execution.*`
    - Enforced by `test_feed_py_has_no_execution_imports` using AST parsing
    - Violation = CI failure
  - **BinanceWsConnector:**
    - Implements `DataConnector` ABC with `iter_snapshots()` async iterator
    - Parses bookTicker JSON → Snapshot objects
    - Idempotency via `last_seen_ts` tracking (skips old/duplicate)
    - Auto-reconnect with exponential backoff
    - Testable via `WsTransport` ABC injection
  - **FakeWsTransport:**
    - Pre-loaded messages queue for testing
    - Simulated delays (`delay_ms`)
    - Error injection (`error_after=N`)
    - Injectable clock for deterministic timestamps
  - **LiveFeed pipeline:**
    - Receives Snapshots from DataConnector
    - Filters by configured symbols
    - Feeds through FeatureEngine (BarBuilder → indicators)
    - Yields LiveFeaturesUpdate with computed features
    - Tracks stats (ticks, bars, errors, latency)
  - **LiveFeaturesUpdate:**
    - `ts`: Snapshot timestamp
    - `symbol`: Trading symbol
    - `features`: FeatureSnapshot from engine
    - `bar_completed`: Whether a new bar was completed
    - `bars_available`: Count of completed bars
    - `is_warmed_up`: Whether enough bars for full feature computation
    - `latency_ms`: Processing latency
  - **Configuration:**
    - `LiveFeedConfig`: symbols filter, feature_config, warmup_bars
    - `BinanceWsConfig`: symbols, use_testnet, timeout, retry
- **Implementation:**
  - `src/grinder/connectors/binance_ws.py`: WsTransport, BinanceWsConnector
  - `src/grinder/live/types.py`: LiveFeaturesUpdate, WsMessage, BookTickerData
  - `src/grinder/live/feed.py`: LiveFeed, LiveFeedConfig, LiveFeedRunner
  - `tests/unit/test_live_feed.py`: 21 tests
  - `tests/fixtures/ws/bookticker_btcusdt.jsonl`: Golden fixture
- **Testing:**
  - **P0 Hard-block tests (2):** AST check for 0 execution imports in feed.py and types.py
  - **FakeWsTransport tests (3):** Message ordering, not-connected error, error injection
  - **BinanceWsConnector tests (4):** Connect/subscribe, yields snapshots, skip subscription response, idempotency
  - **LiveFeed tests (7):** Process snapshot, bars tracking, symbol filtering, warmup detection, stats, reset
  - **Determinism tests (2):** Same input → same output, golden fixture SHA256 match
  - **LiveFeaturesUpdate tests (1):** to_dict serialization
- **Consequences:**
  - Live data pipeline ready for integration with LiveEngineV0
  - Strictly read-only (no accidental trades from data plane)
  - Deterministic testing with fake WS transport
  - Golden fixtures enable regression detection
  - FeatureEngine produces features for policy evaluation
- **Out of Scope (v0):**
  - Real WebSocket connection to mainnet (testnet only)
  - Multi-symbol concurrent streaming (single pipeline)
  - WebSocket heartbeat/ping handling (delegated to websockets library)
  - Persistence of WS messages for replay (deferred)

---

## ADR-041 — Futures User-Data Stream v0.1 (LC-09a)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Reconciliation between expected order state (from execution) and actual state (from exchange) requires real-time user-data stream. Binance Futures USDT-M uses a listenKey-based WebSocket for ORDER_TRADE_UPDATE and ACCOUNT_UPDATE events. Need event normalization, listenKey lifecycle management, and deterministic testing.
- **Decision:**
  - **New types** (`src/grinder/execution/futures_events.py`):
    - `FuturesOrderEvent`: Normalized order update (ts, symbol, order_id, client_order_id, side, status, price, qty, executed_qty, avg_price)
    - `FuturesPositionEvent`: Normalized position update (ts, symbol, position_amt, entry_price, unrealized_pnl)
    - `UserDataEventType`: Enum for ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, UNKNOWN
    - `UserDataEvent`: Tagged union wrapper for event dispatch
    - `BINANCE_STATUS_MAP`: Binance status → OrderState mapping (NEW→OPEN, CANCELED→CANCELLED, etc.)
  - **ListenKey lifecycle** (`src/grinder/connectors/binance_user_data_ws.py`):
    - `ListenKeyConfig`: API base URL, API key, timeout
    - `ListenKeyManager`: HTTP operations for listenKey (POST create, PUT keepalive, DELETE close)
    - 401 → `ConnectorNonRetryableError` (invalid API key)
    - 5xx → `ConnectorTransientError` (retryable)
  - **WebSocket connector** (`FuturesUserDataWsConnector`):
    - Lifecycle: connect (create listenKey → WS connect → start keepalive), close (cancel keepalive → WS close → delete listenKey)
    - `iter_events()`: AsyncIterator yielding `UserDataEvent`
    - Auto-keepalive: PUT every 30 seconds (configurable)
    - Auto-reconnect: exponential backoff on transient errors
    - Unknown events: yield as UNKNOWN with raw_data (don't crash)
    - listenKeyExpired: log warning, trigger reconnect
  - **Testing infrastructure**:
    - `FakeListenKeyManager`: Injectable mock for listenKey operations
    - Reuses `FakeWsTransport` from binance_ws.py for WS testing
    - Injectable clock for keepalive timing tests
  - **Fixtures** (`tests/fixtures/user_data/`):
    - `order_lifecycle.jsonl`: NEW → PARTIALLY_FILLED → FILLED
    - `position_lifecycle.jsonl`: 0 → position → 0
    - Golden tests verify deterministic normalization (same input → same output)
- **API message format (Binance reference)**:
  - ORDER_TRADE_UPDATE: `o.s`→symbol, `o.c`→client_order_id, `o.X`→status, `o.i`→order_id, etc.
  - ACCOUNT_UPDATE: `a.P[]`→positions array, `a.P[].s`→symbol, `a.P[].pa`→position_amt, etc.
- **Test coverage:**
  - `tests/unit/test_futures_events.py` (41 tests): serialization, parsing, lifecycle golden tests
  - `tests/unit/test_listen_key_manager.py` (17 tests): HTTP operations, error handling
  - `tests/unit/test_user_data_ws.py` (21 tests): connection, events, stats, FakeListenKeyManager
- **Consequences:**
  - User-data stream infrastructure ready for reconciliation integration (LC-09b)
  - Event normalization provides stable types for strategy logic
  - Deterministic testing without network dependencies
  - listenKey lifecycle managed automatically (no manual keepalive needed)
- **Out of Scope (LC-09b):**
  - Reconciliation logic (comparing expected vs observed state)
  - REST snapshot fallback
  - Active actions (cancel-all, flatten on mismatch)
  - Metrics/counters for stream health

---

## ADR-042 — Passive Reconciliation v0.1 (LC-09b)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Need to detect mismatches between expected state (what we sent to exchange) and observed state (from user-data stream + REST snapshots) for Binance Futures USDT-M. v0.1 is **passive only**: logs + metrics + action plan text. No actual remediation actions.
- **Decision:**
  - **New types** (`src/grinder/reconcile/types.py`):
    - `ExpectedOrder`: Frozen dataclass for order we expect on exchange (client_order_id, symbol, side, order_type, price, orig_qty, ts_created, expected_status)
    - `ExpectedPosition`: Frozen dataclass for expected position (symbol, expected_position_amt, ts_updated)
    - `ObservedOrder`: Frozen dataclass for order seen via stream/REST (includes order_id, executed_qty, avg_price, source)
    - `ObservedPosition`: Frozen dataclass for observed position (position_amt, entry_price, unrealized_pnl, source)
    - `MismatchType`: Enum with 4 stable values (metric labels):
      - `ORDER_MISSING_ON_EXCHANGE`: Expected OPEN order not found after grace period
      - `ORDER_EXISTS_UNEXPECTED`: Order on exchange (grinder_ prefix) not in expected state
      - `ORDER_STATUS_DIVERGENCE`: Expected vs observed status differs
      - `POSITION_NONZERO_UNEXPECTED`: Position != 0 when expected = 0
    - `Mismatch`: Frozen dataclass for detected mismatch (type, symbol, client_order_id, expected, observed, ts_detected, action_plan)
  - **Configuration** (`src/grinder/reconcile/config.py`):
    - `ReconcileConfig`: order_grace_period_ms (5s), snapshot_interval_sec (60s), expected_max_orders (200), expected_ttl_ms (24h), symbol_filter, enabled
  - **State stores**:
    - `ExpectedStateStore` (`src/grinder/reconcile/expected_state.py`):
      - Ring buffer (max_orders=200) with OrderedDict for FIFO eviction
      - TTL eviction (24h) - terminal orders evicted first
      - Methods: record_order, mark_filled, mark_cancelled, get_active_orders, get_all_orders
      - Injectable clock for deterministic testing
    - `ObservedStateStore` (`src/grinder/reconcile/observed_state.py`):
      - Updated from FuturesOrderEvent/FuturesPositionEvent (stream) and REST snapshots
      - Methods: update_from_order_event, update_from_position_event, update_from_rest_orders, update_from_rest_positions
      - Tracks last_snapshot_ts for staleness detection
  - **Reconciliation engine** (`src/grinder/reconcile/engine.py`):
    - `ReconcileEngine`: Compares expected vs observed state
    - `reconcile()` returns `list[Mismatch]`, logs warnings (RECONCILE_MISMATCH), updates metrics
    - Grace period: ORDER_MISSING only fires after order_grace_period_ms (prevents false positives during network lag)
    - Only detects grinder_ prefixed orders (ignores third-party orders)
    - **Passive only**: action_plan is text describing what v1.0 *would* do
  - **Metrics** (`src/grinder/reconcile/metrics.py`):
    - `grinder_reconcile_mismatch_total{type="..."}`: Counter by mismatch type
    - `grinder_reconcile_last_snapshot_age_seconds`: Gauge for REST snapshot staleness
    - `grinder_reconcile_runs_total`: Counter for reconcile runs
    - Thread-safe via GIL, Prometheus text export via `to_prometheus_lines()`
    - Global singleton: `get_reconcile_metrics()`, `reset_reconcile_metrics()`
  - **Snapshot client** (`src/grinder/reconcile/snapshot_client.py`):
    - `SnapshotClient`: Periodic REST polling of /fapi/v1/openOrders and /fapi/v2/positionRisk
    - Retry with exponential backoff on 429/5xx (max_retries=3, base_delay=1s, max_delay=10s)
    - Injectable HttpClient for testing
    - `fetch_snapshot()` updates ObservedStateStore
    - `should_fetch()` checks if interval elapsed
- **Test coverage:**
  - `tests/unit/test_reconcile_types.py` (22 tests): serialization roundtrips, MismatchType contract
  - `tests/unit/test_expected_state.py` (16 tests): ring buffer, TTL eviction, mark_filled/cancelled
  - `tests/unit/test_observed_state.py` (15 tests): stream/REST updates, symbol filtering
  - `tests/unit/test_reconcile_engine.py` (13 tests): all 4 mismatch types, grace period, metrics
  - `tests/unit/test_snapshot_client.py` (16 tests): retry logic, backoff, fetch_snapshot
  - Total: 82 tests, all passing
- **Fixtures** (`tests/fixtures/reconcile/`):
  - `expected_orders.jsonl`: Sample expected orders
  - `rest_open_orders.json`: GET /openOrders response
  - `rest_position_risk.json`: GET /positionRisk response
  - `mismatch_scenarios.jsonl`: Test scenarios for golden tests
- **Consequences:**
  - Passive reconciliation infrastructure ready for integration
  - Mismatch detection available via ReconcileEngine.reconcile()
  - Metrics enable alerting on reconciliation issues
  - Memory bounded by ring buffer + TTL eviction
  - Deterministic testing with injectable clocks
- **Out of Scope (LC-10):**
  - Automatic remediation actions (cancel-all, flatten) → implemented in ADR-043
  - `RECONCILE_ACTION=cancel_all` execution → implemented in ADR-043
  - Multi-symbol reconcile optimization
  - HA leader election for reconcile loop
  - Integration with LiveEngineV0 event loop

---

## ADR-043 — Active Remediation v0.1 (LC-10)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Passive reconciliation (ADR-042) detects mismatches but takes no action. Need active remediation to cancel unexpected orders and flatten unexpected positions, but with strict safety gates to prevent accidental execution.
- **Decision:**
  - **Actions:**
    - `cancel_all`: Cancel unexpected grinder_ prefixed orders
    - `flatten`: Close unexpected positions with reduceOnly market orders
  - **9 Safety Gates (ALL must pass for real execution):**
    | # | Gate | Config/Env | Default |
    |---|------|------------|---------|
    | 1 | action != none | `action` | `none` |
    | 2 | dry_run == False | `dry_run` | `True` |
    | 3 | allow_active_remediation | `allow_active_remediation` | `False` |
    | 4 | armed == True | passed from LiveEngine | `False` |
    | 5 | ALLOW_MAINNET_TRADE=1 | env var | not set |
    | 6 | cooldown elapsed | `cooldown_seconds` | 60s |
    | 7 | symbol in whitelist | `symbol_whitelist` | required |
    | 8 | grinder_ prefix (cancel) | hardcoded | required |
    | 9 | notional <= limit (flatten) | `max_flatten_notional_usdt` | 500 |
  - **Additional limits:**
    - `max_orders_per_action=10`: Max cancels per reconcile run
    - `max_symbols_per_action=3`: Max symbols per reconcile run
    - `require_whitelist=True`: Require non-empty symbol whitelist
  - **Kill-switch semantics:** Remediation ALLOWED under kill-switch (reduces risk exposure)
  - **Default behavior:** dry-run only — plans but doesn't execute
  - **grinder_ prefix:** Required for cancel operations (protects manual orders placed outside grinder)
  - **Notional cap:** Required for flatten operations (limits exposure per remediation)
  - **New types** (`src/grinder/reconcile/remediation.py`):
    - `RemediationBlockReason`: Enum with 13 stable values for why remediation was blocked
    - `RemediationStatus`: Enum (PLANNED, EXECUTED, BLOCKED, FAILED)
    - `RemediationResult`: Frozen dataclass for remediation outcome
    - `RemediationExecutor`: Class implementing safety gates and remediation logic
  - **New metrics:**
    - `grinder_reconcile_action_planned_total{action}`: Counter for dry-run plans
    - `grinder_reconcile_action_executed_total{action}`: Counter for real executions
    - `grinder_reconcile_action_blocked_total{reason}`: Counter for blocked actions
  - **New config fields** (`ReconcileConfig`):
    - `action: RemediationAction = NONE`
    - `dry_run: bool = True`
    - `allow_active_remediation: bool = False`
    - `max_orders_per_action: int = 10`
    - `max_symbols_per_action: int = 3`
    - `cooldown_seconds: int = 60`
    - `max_flatten_notional_usdt: Decimal = 500`
    - `require_whitelist: bool = True`
- **Test coverage:**
  - `tests/unit/test_remediation.py` (28 tests):
    - 9 safety gate tests (one per gate)
    - 4 execution tests (cancel, flatten, max_orders, max_symbols)
    - 2 kill-switch tests (allows cancel, allows flatten)
    - 3 metrics tests (planned, executed, blocked counters)
    - Contract tests for enum values and constants
- **Consequences:**
  - Active remediation available with explicit opt-in
  - 9 layers of safety prevent accidental execution
  - grinder_ prefix protects manual orders from accidental cancel
  - Notional cap limits worst-case exposure per flatten
  - Deterministic testing via injectable port
- **Runbook:** `docs/runbooks/12_ACTIVE_REMEDIATION.md`
- **Out of Scope (v0.1):**
  - Smart order replacement/modification
  - Automatic strategy recovery after remediation
  - Multi-venue / COIN-M support
  - HA orchestration for remediation loop
  - ML-based decision making

---

## ADR-044 — Remediation Wiring + Routing Policy (LC-11)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** ADR-043 introduced RemediationExecutor with 9 safety gates, but it operates on individual orders/positions. Need an orchestration layer to wire ReconcileEngine (detects mismatches) → RemediationExecutor (takes actions), with clear routing policy (mismatch type → action mapping).
- **Decision:**
  - **ReconcileRunner** (`src/grinder/reconcile/runner.py`): Orchestrates reconciliation flow:
    1. Call `engine.reconcile()` → `list[Mismatch]`
    2. Route each mismatch via ROUTING_POLICY to action
    3. Execute via `executor.remediate_cancel/flatten()`
    4. Return `ReconcileRunReport` with full audit trail
  - **Routing Policy (SSOT):**
    | Mismatch Type | Action | Notes |
    |---------------|--------|-------|
    | `ORDER_EXISTS_UNEXPECTED` | CANCEL | grinder_ prefix required (Gate 8) |
    | `ORDER_STATUS_DIVERGENCE` | CANCEL | Only if not terminal status |
    | `POSITION_NONZERO_UNEXPECTED` | FLATTEN | Notional cap applies (Gate 9) |
    | `ORDER_MISSING_ON_EXCHANGE` | NO ACTION | v0.1: alert only, no retry |
  - **Terminal statuses (skip cancel):** FILLED, CANCELLED, REJECTED, EXPIRED
  - **Actionable statuses (allow cancel):** OPEN, PARTIALLY_FILLED
  - **Bounded execution:**
    - One action type per run (cancel OR flatten, determined by priority)
    - Respects executor's max_orders_per_action / max_symbols_per_action
  - **Deterministic ordering:**
    - Mismatches are sorted before processing for predictable action-type selection
    - Sort key: priority → symbol → client_order_id
    - Priority (lower = processed first): ORDER_EXISTS=10, ORDER_STATUS_DIVERGENCE=20, ORDER_MISSING=90, POSITION_NONZERO=100
    - Cancel always wins over flatten when both exist (cancel has lower priority numbers)
    - Ensures same result regardless of ReconcileEngine's detection order
  - **Routing constants (frozenset for performance):**
    ```python
    ORDER_MISMATCHES_FOR_CANCEL = frozenset({
        MismatchType.ORDER_EXISTS_UNEXPECTED,
        MismatchType.ORDER_STATUS_DIVERGENCE,
    })
    POSITION_MISMATCHES_FOR_FLATTEN = frozenset({
        MismatchType.POSITION_NONZERO_UNEXPECTED,
    })
    NO_ACTION_MISMATCHES = frozenset({
        MismatchType.ORDER_MISSING_ON_EXCHANGE,
    })
    TERMINAL_STATUSES = frozenset({
        OrderState.FILLED, OrderState.CANCELLED,
        OrderState.REJECTED, OrderState.EXPIRED,
    })
    ACTIONABLE_STATUSES = frozenset({
        OrderState.OPEN, OrderState.PARTIALLY_FILLED,
    })
    ```
  - **ReconcileRunReport:** Frozen dataclass with:
    - `ts_start`, `ts_end`: Run timestamps
    - `mismatches_detected`: Total from engine
    - `cancel_results`, `flatten_results`: Tuples of RemediationResult
    - `skipped_terminal`, `skipped_no_action`: Skip counts
    - Properties: `total_actions`, `executed_count`, `planned_count`, `blocked_count`
  - **New metrics:**
    - `grinder_reconcile_runs_with_mismatch_total`: Counter for runs that detected mismatches
    - `grinder_reconcile_runs_with_remediation_total{action}`: Counter for runs with executed actions
    - `grinder_reconcile_last_remediation_ts_ms`: Gauge for last remediation timestamp
  - **Audit logging:**
    - `RECONCILE_RUN`: Run completion with summary stats
    - `REMEDIATE_SKIP`: Skipped mismatch with reason
- **Test coverage:**
  - `tests/unit/test_reconcile_runner.py` (39 tests):
    - Routing policy constants tests
    - Routing behavior tests (4 mismatch types)
    - One action type per run tests
    - Terminal status skip tests
    - Metrics tests
    - ReconcileRunReport tests
    - Determinism tests (4 tests for priority-based ordering)
    - Edge cases
- **Consequences:**
  - Full wiring: ReconcileEngine → ReconcileRunner → RemediationExecutor
  - Routing policy is explicit SSOT (constants at module level)
  - One action type per run prevents mixed cancel/flatten in same cycle
  - Terminal orders are not attempted to cancel
  - Audit trail via structured logs + metrics
- **Runbook:** `docs/runbooks/13_OPERATOR_CEREMONY.md`
- **Out of Scope:**
  - Multi-action-type per run (cancel AND flatten)
  - Smart retry for ORDER_MISSING_ON_EXCHANGE
  - Integration with LiveEngineV0 event loop (separate task)

---

## ADR-045 — Configurable Order Identity (LC-12)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** The hardcoded `grinder_` prefix in multiple places made it impossible to:
  1. Run multiple strategy instances with isolated order ownership
  2. Identify which strategy placed an order
  3. Selectively remediate orders by strategy allowlist
  
  Gate 8 in ADR-043 checked `startswith("grinder_")` but couldn't distinguish between strategies.

- **Decision:**
  - **OrderIdentityConfig** (`src/grinder/reconcile/identity.py`): Central config for order identity:
    ```python
    @dataclass
    class OrderIdentityConfig:
        prefix: str = "grinder_"           # Order ID prefix
        strategy_id: str = "default"       # Strategy identifier
        allowed_strategies: set[str] = {}  # Allowlist for remediation
        require_strategy_allowlist: bool = True
        allow_legacy_format: bool = False  # Env: ALLOW_LEGACY_ORDER_ID=1
        identity_format_version: int = 1
    ```
  - **clientOrderId Format v1:** `{prefix}{strategy_id}_{symbol}_{level_id}_{ts_seconds}_{seq}`
    - Example: `grinder_d_BTCUSDT_1_1704067200_1`
    - Timestamp truncated from millis to seconds (Binance 36-char limit)
  - **Legacy Format:** `grinder_{symbol}_{level_id}_{ts}_{seq}` (no strategy_id)
    - Supported via `allow_legacy_format=True` or `ALLOW_LEGACY_ORDER_ID=1` env var
    - Parsed strategy_id: `__legacy__` (internal marker)
  - **ParsedOrderId:** Frozen dataclass with parsed components:
    - `prefix`, `strategy_id`, `symbol`, `level_id`, `ts`, `seq`, `is_legacy`
  - **Core functions:**
    - `parse_client_order_id(cid) -> ParsedOrderId | None`: Parse any format
    - `is_ours(cid, config) -> bool`: Check ownership via prefix + strategy allowlist
    - `generate_client_order_id(config, symbol, level_id, ts, seq) -> str`: Create v1 format
  - **Singleton pattern:**
    - `get_default_identity_config()`: Returns singleton (lazy init)
    - `set_default_identity_config(config)`: Set at startup
    - `reset_default_identity_config()`: For testing
  - **Integration points updated:**
    - `BinanceFuturesPort.place_order()`: Uses `generate_client_order_id()`
    - `BinanceFuturesPort.place_market_order()`: Uses `generate_client_order_id()`
    - `BinancePort.place_order()`: Uses `generate_client_order_id()`
    - `ReconcileEngine._check_unexpected_orders()`: Uses `is_ours()`
    - `RemediationExecutor.can_execute()` Gate 8: Uses `is_ours()`
  - **Strategy allowlist semantics:**
    - If `allowed_strategies` empty at init → defaults to `{strategy_id}`
    - Legacy orders (`__legacy__`) allowed only if `allow_legacy_format=True`
    - `require_strategy_allowlist=False` → accept any strategy
  - **Backward compatibility:**
    - v1 format includes strategy_id: `grinder_d_BTCUSDT_...` (differs from legacy `grinder_BTCUSDT_...`)
    - Legacy format parsing enabled via env var (`ALLOW_LEGACY_ORDER_ID=1`) or config flag
    - GRINDER_PREFIX constant preserved in remediation.py for backward compat

- **Test coverage:**
  - `tests/unit/test_identity.py` (44 tests):
    - Config validation tests
    - V1 format parsing tests
    - Legacy format parsing tests
    - `is_ours()` allowlist tests
    - Generation tests
    - Security edge cases
    - Singleton pattern tests

- **Consequences:**
  - Orders can be identified by prefix + strategy
  - Multiple strategies can coexist with isolated remediation scope
  - Gate 8 now checks allowlist, not just prefix
  - Legacy orders can be migrated gradually
  - Parser reads both v1 and legacy formats; generation always produces v1

- **Out of Scope (v0.1):**
  - Strategy-specific config loading from file
  - Dynamic allowlist updates at runtime
  - Multi-prefix support (only one prefix per config)
  - Strategy registry with capabilities/permissions

## ADR-046 — Audit JSONL for Reconcile/Remediation (LC-11b)

- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Reconciliation and remediation runs need a deterministic audit trail for:
  1. Post-mortem analysis of state mismatches
  2. Compliance/regulatory evidence
  3. Debugging and reproducibility

  Requirements:
  - Append-only JSONL format (one event per line)
  - No secrets in output (redaction by default)
  - Bounded file size with rotation
  - Opt-in (disabled by default)
  - Deterministic serialization (sorted keys)

- **Decision:**
  - **AuditConfig** (`src/grinder/reconcile/audit.py`): Configuration for audit logging:
    ```python
    @dataclass
    class AuditConfig:
        enabled: bool = False              # Opt-in
        path: str = "audit/reconcile.jsonl"
        max_bytes: int = 100_000_000       # 100 MB
        max_events_per_file: int = 100_000
        flush_every: int = 1               # Immediate flush
        fsync: bool = False                # No fsync for performance
        redact: bool = True                # Redact secrets
        fail_open: bool = True             # Continue on write error
    ```
  - **AuditEventType** enum:
    - `RECONCILE_RUN`: Summary of reconcile run
    - `REMEDIATE_ATTEMPT`: Individual remediation attempt
    - `REMEDIATE_RESULT`: Result of remediation (planned/executed/blocked/failed)
  - **AuditEvent** frozen dataclass:
    - `ts_ms`: Timestamp in milliseconds
    - `event_type`: Event type enum
    - `run_id`: Unique run identifier (format: `{ts_ms}_{seq}`)
    - `schema_version`: Schema version (default: 1)
    - `mode`: "dry_run" or "live"
    - `action`: Action type (none/cancel_all/flatten)
    - `status`: Event status (for REMEDIATE_* events)
    - `block_reason`: Why blocked (if applicable)
    - `symbols`: Bounded list of symbols (max 10)
    - `mismatch_counts`: Counts by mismatch type
    - `details`: Additional details dict
  - **AuditWriter** class:
    - Append-only writes to JSONL file
    - Creates directories if needed
    - Rotation when `max_bytes` or `max_events_per_file` exceeded
    - Redaction of sensitive fields (api_key, secret, token, etc.)
    - Context manager support
    - Injectable clock and run_id factory for testing
  - **Environment variables:**
    - `GRINDER_AUDIT_ENABLED=1`: Enable audit logging
    - `GRINDER_AUDIT_PATH=/path/to/file.jsonl`: Override audit path
  - **Integration:**
    - `ReconcileRunner` accepts optional `audit_writer` field
    - Writes `RECONCILE_RUN` event at end of each run
    - Collects mismatch counts and symbols during run
  - **Redaction:** Fields containing these patterns are replaced with `[REDACTED]`:
    - `api_key`, `api_secret`, `secret`, `password`, `token`, `signature`, `x-mbx-apikey`, `authorization`
  - **Failure policy:** `fail_open=True` (default) continues on write error, logs warning

- **Test coverage:**
  - `tests/unit/test_audit.py` (33 tests):
    - AuditConfig: defaults, env var overrides
    - AuditEvent: serialization, schema, immutability
    - AuditWriter: append-only, rotation, redaction, context manager
    - Factory functions: event creation
    - Determinism: same inputs → same outputs
    - Runner integration: audit event written, no-audit works

- **Consequences:**
  - Audit trail available for post-mortems when enabled
  - No secrets in audit files (redaction default)
  - Bounded file size prevents disk exhaustion
  - Opt-in: no impact when disabled
  - Deterministic output enables diff-based testing

- **Out of Scope (v0.1):**
  - REMEDIATE_ATTEMPT/RESULT events in runner (only RECONCILE_RUN)
  - Centralized storage (S3, Loki)
  - Encryption/signing of audit files
  - Audit metrics (planned for P1)

## ADR-047 — E2E Reconcile→Remediate Smoke Harness (LC-13)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** Active remediation (LC-10/LC-11) has 9 safety gates but no end-to-end test that exercises the full `detect → route → remediate` flow in a controlled environment. Unit tests validate individual components; we need a higher-level smoke test that:
  1. Validates the complete flow without real HTTP/WS calls
  2. Confirms dry-run mode produces zero port calls
  3. Ensures live mode is protected by 5 explicit gates
  4. Verifies audit integration when enabled

- **Decision:**
  - **New script:** `scripts/smoke_reconcile_e2e.py` — E2E smoke harness for reconciliation + remediation

  - **FakePort pattern:** Duck-typed port that records calls without executing:
    ```python
    @dataclass
    class FakePort:
        calls: list[dict[str, Any]] = field(default_factory=list)

        def cancel_order(self, symbol: str, client_order_id: str) -> dict:
            self.calls.append({"action": "cancel_order", ...})
            return {"status": "CANCELED", ...}

        def market_order(self, symbol: str, side: str, quantity: Decimal) -> dict:
            self.calls.append({"action": "market_order", ...})
            return {"status": "FILLED", ...}
    ```

  - **3 P0 scenarios:**
    - `order`: Unexpected order → CANCEL → validates cancel routing
    - `position`: Unexpected position → FLATTEN → validates flatten routing
    - `mixed`: Both mismatches → priority routing (order wins with CANCEL_ALL action)

  - **Default mode: DRY-RUN**
    - Zero port calls in dry-run mode
    - Plans recorded via `planned_count`
    - Safe to run without any env vars

  - **Live mode gating (5 gates):**
    - `--confirm LIVE_REMEDIATE` — explicit CLI flag
    - `RECONCILE_DRY_RUN=0` — config override
    - `RECONCILE_ALLOW_ACTIVE=1` — allow active remediation
    - `ARMED=1` — executor armed
    - `ALLOW_MAINNET_TRADE=1` — mainnet trade env var

    All 5 must pass; any failure blocks live mode with explicit error message.

  - **Audit integration:**
    - When `GRINDER_AUDIT_ENABLED=1`, writes RECONCILE_RUN events to audit file
    - Default path: `audit/reconcile.jsonl`
    - Events include mismatch counts, action, mode, symbols

  - **Output format:** Clear tabular output for each scenario:
    ```
    --- Scenario: order [PASS] ---
      Mismatches detected:  1
      Expected action:      cancel
      Actual action:        cancel
      Port calls:           0
      Planned count:        1
      Executed count:       0
    ```

- **Usage:**
  ```bash
  # Dry-run (safe, default)
  PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e

  # With audit enabled
  GRINDER_AUDIT_ENABLED=1 PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e

  # Live mode (requires all 5 gates)
  RECONCILE_DRY_RUN=0 RECONCILE_ALLOW_ACTIVE=1 ARMED=1 ALLOW_MAINNET_TRADE=1 \
    PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e --confirm LIVE_REMEDIATE
  ```

- **Consequences:**
  - E2E validation of reconcile→remediate flow without real exchange calls
  - Dry-run safety verified (zero port calls assertion)
  - Live mode protected by explicit 5-gate ceremony
  - Audit integration validated in harness

- **Out of Scope (v0.1):**
  - Real mainnet execution in smoke test
  - COIN-M futures support
  - Concurrent scenario execution
  - Performance benchmarking

## ADR-048 — ReconcileLoop Wiring for LiveEngine (LC-14a)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** ReconcileRunner exists but is not integrated into the live trading loop. We need a periodic background loop that:
  1. Runs ReconcileRunner on a configurable interval
  2. Respects HA role (only runs when ACTIVE)
  3. Operates in detect-only mode by default
  4. Provides thread-safe statistics for monitoring
  5. Follows existing threading patterns (LeaderElector)

- **Decision:**
  - **New module:** `src/grinder/live/reconcile_loop.py` — ReconcileLoop component

  - **Threading pattern (from LeaderElector):**
    - Daemon thread for background execution
    - `threading.Event` for graceful shutdown
    - Interruptible wait via `Event.wait(timeout=interval)`

  - **Configuration (ReconcileLoopConfig):**
    ```python
    @dataclass
    class ReconcileLoopConfig:
        enabled: bool  # env: RECONCILE_ENABLED (default: False)
        interval_ms: int  # env: RECONCILE_INTERVAL_MS (default: 30000)
        require_active_role: bool = True  # Check HA role before running
    ```

  - **Statistics (ReconcileLoopStats):**
    ```python
    @dataclass
    class ReconcileLoopStats:
        runs_total: int
        runs_skipped_role: int
        runs_with_mismatch: int
        runs_with_error: int
        last_run_ts_ms: int
        last_report: ReconcileRunReport | None
    ```

  - **HA integration:**
    - When `require_active_role=True` (default), checks HA role before each run
    - Skips run if role != ACTIVE, increments `runs_skipped_role`
    - Optional dependency: works without HA module installed

  - **Error handling:**
    - Exceptions in runner logged, loop continues
    - `runs_with_error` counter incremented
    - Never crashes the loop thread

  - **Safety guarantees:**
    - Default `enabled=False` — must opt-in via env var
    - Default interval 30s — prevents rapid-fire reconciliation
    - Minimum interval 1000ms — enforced by validation
    - Detect-only by default (via ReconcileConfig)

- **Usage:**
  ```python
  # In LiveEngine initialization
  loop = ReconcileLoop(
      runner=reconcile_runner,
      config=ReconcileLoopConfig(enabled=True, interval_ms=30000),
  )
  loop.start()  # Starts background thread
  # ... later
  loop.stop()   # Graceful shutdown
  ```

  ```bash
  # Enable via environment
  RECONCILE_ENABLED=1 RECONCILE_INTERVAL_MS=30000 python3 -m grinder.live

  # Smoke test
  PYTHONPATH=src python3 -m scripts.smoke_live_reconcile_loop --duration 15
  ```

- **Smoke test script:** `scripts/smoke_live_reconcile_loop.py`
  - Uses FakePort (no real HTTP calls)
  - Demonstrates loop start/stop lifecycle
  - Verifies detect-only mode (zero port calls)
  - Supports `--inject-mismatch` for mismatch detection

- **Unit tests:** `tests/unit/test_reconcile_loop.py` (18 tests)
  - Config: defaults, env vars, validation
  - Lifecycle: start/stop, idempotent
  - Stats: initial, after runs, thread-safe
  - HA: skip when not ACTIVE, run when ACTIVE
  - Error handling: exceptions don't crash loop

- **Consequences:**
  - ReconcileRunner can now run as periodic background task
  - HA-aware reconciliation prevents split-brain execution
  - Statistics enable monitoring and alerting
  - Thread-safe design prevents race conditions
  - Follows established patterns from LeaderElector

- **Out of Scope (v0.1):**
  - Dynamic interval adjustment
  - Backoff on repeated errors
  - Metrics export (Prometheus)
  - Integration with LiveEngine start/stop

## ADR-049 — Real Sources Wiring for ReconcileLoop (LC-14b)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** ReconcileLoop (LC-14a) uses mock data sources. Production deployment requires:
  1. Real price data from Binance REST API (for notional calculation)
  2. Hard enforcement of detect-only mode (refuse to start if runner can execute)
  3. Proof of zero execution side-effects with real sources

- **Decision:**

  - **PriceGetter module:** `src/grinder/reconcile/price_getter.py`
    - Fetches current market price from Binance Futures REST API (`/fapi/v1/ticker/price`)
    - Uses HttpClient protocol (same as SnapshotClient)
    - 1-second cache TTL to reduce API calls
    - Returns `Decimal | None` for safe handling of unavailable prices
    ```python
    @dataclass
    class PriceGetter:
        http_client: HttpClient
        config: PriceGetterConfig

        def get_price(self, symbol: str) -> Decimal | None:
            # Fetches from REST with caching
    ```

  - **detect_only enforcer in ReconcileLoopConfig:**
    ```python
    @dataclass
    class ReconcileLoopConfig:
        # Existing fields...
        detect_only: bool = True  # LC-14b: Hard enforcer
    ```

    - Hard check on `start()`: refuses to run if runner can execute actions
    - Condition for detect-only: `action=NONE OR (dry_run=True AND allow_active_remediation=False)`
    - Raises `RuntimeError` if detect-only violated

  - **Smoke test with real sources:** `scripts/smoke_live_reconcile_loop_real_sources.py`
    - Uses RequestsHttpClient for real Binance REST calls
    - Tests PriceGetter with live market data
    - FakePort records all execution calls
    - Verifies zero port calls at end (detect-only proof)
    ```python
    # Execution verification
    if len(fake_port.calls) > 0:
        raise AssertionError(f"DETECT-ONLY VIOLATED: {fake_port.calls}")
    print("✓ DETECT-ONLY MODE VERIFIED: Zero port calls")
    ```

- **Consequences:**
  - ReconcileLoop enforces detect-only mode at startup
  - PriceGetter enables notional calculation for flatten safety gate
  - Smoke test provides reproducible proof of no execution side-effects
  - Ready for production deployment with real market data

- **Safety Guarantees:**
  - detect_only=True default refuses to start if runner can execute
  - FakePort in smoke test catches any execution attempts
  - No API credentials required for price fetch (public endpoint)

- **Out of Scope (v0.1):**
  - WS user-data stream integration (FuturesUserDataWsConnector wiring)
  - REST snapshot fallback (SnapshotClient wiring to ObservedStateStore)
  - WebSocket price streaming (using REST for simplicity)

## ADR-050 — Operator Ceremony: Staged Enablement + Rollback (LC-15a)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** ReconcileLoop with remediation is production-ready but enabling it requires careful staged rollout. Operators need:
  1. Clear step-by-step procedure with verification at each stage
  2. Explicit rollback steps that work immediately
  3. Documented failure modes and expected behavior
  4. Proof that each stage doesn't execute until explicitly enabled

- **Decision:**

  - **5-stage enablement ceremony:**
    | Stage | Name | Execution | Verification |
    |-------|------|-----------|--------------|
    | 0 | Baseline | Disabled | Loop not running |
    | 1 | Detect-only | None | Port calls = 0, runs increasing |
    | 2 | Plan-only | Planned | action_planned > 0, executed = 0 |
    | 3 | Blocked | Blocked | action_blocked > 0 with reason |
    | 4 | Live | Executed | action_executed > 0 when mismatch |

  - **Each stage has explicit pass criteria** before proceeding:
    - Minimum runtime (10 minutes for stages 1-2)
    - Metrics verification
    - Log/audit verification
    - Zero unexpected execution calls

  - **Rollback is single-command:**
    ```bash
    # Any of these stops execution immediately:
    export RECONCILE_ENABLED=0    # Disable loop entirely
    export RECONCILE_ACTION=none  # Disable remediation
    export ARMED=0                # Block at armed gate
    ```

  - **Kill-switch semantics:** Remediation ALLOWED under kill-switch (ADR-043):
    - Cancel/flatten reduce risk
    - New trades blocked, cleanup allowed
    - Documented in ceremony runbook

  - **Smoke script for ceremony:** `scripts/smoke_enablement_ceremony.py`
    - Runs mini-stages A/B/C/D locally
    - Default: 0 execution calls
    - Verifies stage transitions work correctly
    - Optional `--inject-mismatch` for testing

- **Consequences:**
  - Operators have reproducible, auditable enablement procedure
  - Each stage can be verified before proceeding
  - Rollback is instant and reliable
  - Smoke script proves ceremony works without live execution

- **Related:**
  - Runbook: `docs/runbooks/15_ENABLEMENT_CEREMONY.md`
  - ADR-048: ReconcileLoop Wiring
  - ADR-049: Real Sources Wiring
  - ADR-043: Active Remediation (kill-switch semantics)

## ADR-051 — Reconcile Alerts, Metrics Contract, and SLOs (LC-15b)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** ReconcileLoop and remediation are production-ready but lack observability primitives:
  1. Metrics are exported but not integrated into `/metrics` endpoint contract
  2. No Prometheus alert rules for reconcile-specific failure modes
  3. No defined SLOs for loop availability, snapshot freshness, execution budget

- **Decision:**

  - **Metrics contract integration:**
    - Added reconcile metrics to `REQUIRED_METRICS_PATTERNS` in `live_contract.py`
    - MetricsBuilder now includes reconcile metrics via `_build_reconcile_metrics()`
    - Contract tests verify all reconcile metrics are present with correct labels
    ```python
    # Series-level patterns (enforce label schema)
    'grinder_reconcile_mismatch_total{type=',
    'grinder_reconcile_action_planned_total{action=',
    'grinder_reconcile_action_executed_total{action=',
    'grinder_reconcile_runs_with_remediation_total{action=',
    ```

  - **Prometheus alert rules (`monitoring/alert_rules.yml`):**
    | Alert | Severity | Condition |
    |-------|----------|-----------|
    | ReconcileLoopDown | warning | No runs for 5 min while grinder_up=1 |
    | ReconcileSnapshotStale | warning | last_snapshot_age > 120s for 2 min |
    | ReconcileMismatchSpike | warning | Mismatch rate > 0.1/sec for 3 min |
    | ReconcileRemediationExecuted | critical | Any real execution (increase > 0) |
    | ReconcileRemediationPlanned | info | Dry-run action planned |
    | ReconcileRemediationBlocked | info | Action blocked by gates |
    | ReconcileMismatchNoBlocks | warning | Mismatches but no remediation |

  - **Service Level Objectives:**
    | SLO | Target | Metric |
    |-----|--------|--------|
    | Loop Availability | 99.9% | runs_total > 0 per 5-min window |
    | Snapshot Freshness | 99% | age < 120s |
    | Execution Budget | < 10/day | action_executed_total daily increase |

  - **Runbook:** `docs/runbooks/16_RECONCILE_ALERTS_SLOS.md`
    - Triage procedures for each alert
    - Grafana dashboard queries
    - Emergency rollback steps
    - SLO burn rate monitoring

- **Consequences:**
  - Reconcile metrics are now part of the enforced `/metrics` contract
  - Prometheus alerts catch reconcile-specific failure modes
  - SLOs enable data-driven operational decisions
  - Operators have clear triage procedures for each alert

- **Related:**
  - ADR-048: ReconcileLoop Wiring
  - ADR-049: Real Sources Wiring
  - ADR-050: Operator Ceremony
  - Runbook: `docs/runbooks/16_RECONCILE_ALERTS_SLOS.md`

## ADR-052 — Remediation Safety Extensions (LC-18)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** Active remediation (LC-10) is functional but requires additional safety layers for production:
  1. Strategy allowlist: Only remediate orders from specific strategies (uses LC-12 identity)
  2. Per-run and per-day budget limits: Cap calls and notional exposure
  3. Staged rollout modes: DETECT_ONLY → PLAN_ONLY → BLOCKED → EXECUTE_CANCEL_ALL → EXECUTE_FLATTEN
  4. Budget persistence: Track daily usage across restarts

- **Decision:**

  - **RemediationMode enum** (`config.py`):
    ```python
    class RemediationMode(Enum):
        DETECT_ONLY = "detect_only"      # Detect mismatches, no planning (0 calls)
        PLAN_ONLY = "plan_only"          # Plan remediation, increment planned metrics (0 calls)
        BLOCKED = "blocked"              # Plan + block by gates, increment blocked metrics (0 calls)
        EXECUTE_CANCEL_ALL = "execute_cancel_all"  # Execute only cancel_all actions
        EXECUTE_FLATTEN = "execute_flatten"        # Execute only flatten actions
    ```

  - **Budget config fields** (`ReconcileConfig`):
    ```python
    remediation_mode: RemediationMode = RemediationMode.DETECT_ONLY
    remediation_strategy_allowlist: set[str] = field(default_factory=set)
    remediation_symbol_allowlist: set[str] = field(default_factory=set)
    max_calls_per_day: int = 100
    max_notional_per_day: Decimal = Decimal("5000")
    max_calls_per_run: int = 10
    max_notional_per_run: Decimal = Decimal("1000")
    flatten_max_notional_per_call: Decimal = Decimal("500")
    budget_state_path: str | None = None  # JSON persistence
    ```

  - **BudgetTracker** (`budget.py`):
    - Tracks calls_today, notional_today with daily reset at midnight UTC
    - Tracks calls_this_run, notional_this_run with per-run reset
    - Persists daily state to JSON file (optional)
    - `can_execute(notional) → (allowed, block_reason)` checks both limits
    - `record_execution(notional)` updates counters

  - **Extended block reasons** (`RemediationBlockReason`):
    ```python
    # Mode-based reasons
    MODE_DETECT_ONLY = "mode_detect_only"
    MODE_PLAN_ONLY = "mode_plan_only"
    MODE_BLOCKED = "mode_blocked"
    MODE_CANCEL_ONLY = "mode_cancel_only"
    MODE_FLATTEN_ONLY = "mode_flatten_only"

    # Budget reasons
    MAX_CALLS_PER_RUN = "max_calls_per_run"
    MAX_NOTIONAL_PER_RUN = "max_notional_per_run"
    MAX_CALLS_PER_DAY = "max_calls_per_day"
    MAX_NOTIONAL_PER_DAY = "max_notional_per_day"

    # Allowlist reasons
    STRATEGY_NOT_ALLOWED = "strategy_not_allowed"
    SYMBOL_NOT_IN_REMEDIATION_ALLOWLIST = "symbol_not_in_remediation_allowlist"
    ```

  - **Budget metrics** (`ReconcileMetrics`):
    ```python
    budget_calls_used_day: int
    budget_notional_used_day: Decimal
    budget_calls_remaining_day: int
    budget_notional_remaining_day: Decimal
    ```

  - **Gate ordering in `can_execute()`:**
    1. Mode check (DETECT_ONLY/PLAN_ONLY/BLOCKED early exit)
    2. Strategy allowlist (uses `parse_client_order_id()` from LC-12)
    3. Symbol remediation allowlist (optional)
    4. Budget per-run limits
    5. Budget per-day limits
    6. Original LC-10 gates (action, dry_run, allow_active, armed, env_var, cooldown, whitelist)

  - **Mode semantics:**
    - DETECT_ONLY: Returns PLANNED with MODE_DETECT_ONLY (0 port calls)
    - PLAN_ONLY: Returns PLANNED with MODE_PLAN_ONLY (0 port calls)
    - BLOCKED: Returns PLANNED with MODE_BLOCKED (0 port calls, but logs as blocked)
    - EXECUTE_CANCEL_ALL: Allows only cancel_all actions (blocks flatten with MODE_CANCEL_ONLY)
    - EXECUTE_FLATTEN: Allows only flatten actions (blocks cancel with MODE_FLATTEN_ONLY)

- **Test coverage:**
  - `tests/unit/test_remediation.py` (41 tests total, 13 new for LC-18):
    - Mode semantics: detect_only, plan_only, blocked, execute_cancel_all, execute_flatten
    - Strategy allowlist: allowed, blocked, empty=allow all
    - Budget gates: max_calls_per_run, max_notional_per_run, reset_between_runs

- **Consequences:**
  - Staged rollout: Operators can safely progress through modes
  - Budget enforcement: Daily exposure limited even with bugs
  - Strategy isolation: Only specified strategies can trigger remediation
  - No breaking changes: Default mode is DETECT_ONLY (safe)

- **Related:**
  - ADR-043: Active Remediation v0.1
  - ADR-045: Configurable Order Identity (LC-12)
  - Runbook: `docs/runbooks/12_ACTIVE_REMEDIATION.md` (update pending)

## ADR-053 — ROADMAP Structure: Post-Stage-E (M4/M5/M6)
- **Date:** 2026-02-07
- **Status:** accepted
- **Context:**
  After Stage D/E E2E mainnet verification (PR #102–#104), the original "M3 — Production v1.0" placeholder
  in ROADMAP.md no longer reflects reality. The LC-* series (LC-04 through LC-18) delivered live
  reconciliation with active remediation, which was the actual production-readiness work. The old M3
  items (connector hardening, risk controls, HA, monitoring) are either completed via LC-* deliverables
  (see STATE.md for details) or need to be replanned as separate milestones (M4/M5/M6).

- **Decision:**
  - Rename completed work to "M3 — Live Reconciliation" and mark as ✅ Done
  - Add "Section 0) Current Main Truth State" at top of ROADMAP.md for quick reference
  - Define new milestones M4/M5/M6 for remaining production hardening:
    - M4: Ops Hardening (artifacts hygiene, budget policy, low-notional symbols)
    - M5: Observability Polish (dashboards, alerts, SLOs)
    - M6: HA / Leader Election (LC-20)
  - Each milestone has: Deliverables, Acceptance/DoD, Required Proof

- **Consequences:**
  - ROADMAP.md now matches STATE.md (no SSOT conflict)
  - Clear separation: "what's done" vs "what's planned"
  - Each planned milestone has verifiable acceptance criteria
  - Historical M1/M2 DoD preserved for reference

- **Alternatives:**
  - Keep old M3 and mark items as done/skipped: rejected — creates confusion about what "M3" means
  - Rename to M3.1/M3.2: rejected — harder to track than clean M4/M5/M6 numbering

## ADR-054 — HA Leader-Only Remediation (LC-20)

- **Status:** Accepted
- **Context:**
  RemediationExecutor can execute cancel/flatten actions, but in an HA deployment with multiple
  instances (active + standby), we must ensure only ONE instance executes remediation. Otherwise:
  - Both instances might try to cancel the same order (race condition)
  - Both might try to flatten the same position (doubled trade)
  - Budget tracking would be split across instances

- **Decision:**
  1. Add `NOT_LEADER = "not_leader"` to `RemediationBlockReason` enum
  2. Add HA role check as Gate 0 in `can_execute()` — before all other gates
  3. Only `HARole.ACTIVE` instances can execute remediation
  4. Non-leader instances (STANDBY, UNKNOWN) return `BLOCKED` status with `NOT_LEADER` reason
  5. Add `grinder_ha_is_leader` metric (1=leader, 0=follower) for dashboards/alerts
  6. NOT_LEADER is a "blocking reason" — increments `action_blocked_total{reason="not_leader"}`

- **Gate Order (updated):**
  - Gate 0: HA role == ACTIVE (LC-20)
  - Gate 0a: Mode allows action (LC-18)
  - Gate 0b: Budget limits (LC-18)
  - Gate 0c-0d: Allowlists (LC-18)
  - Gates 1-9: Original safety gates (LC-10)

- **Consequences:**
  - HA deployments are safe: only leader executes remediation
  - Followers are explicitly blocked (visible in blocked metric)
  - Budget tracking remains correct (only leader updates state file)
  - If leader fails over, new leader takes over remediation responsibility
  - `grinder_ha_is_leader` provides simple alerting target

- **Metrics:**
  - `grinder_ha_is_leader` (gauge): 1 if this instance is leader, 0 otherwise
  - `grinder_reconcile_action_blocked_total{reason="not_leader"}`: incremented when non-leader attempts remediation
  - `grinder_reconcile_action_planned_total`: NOT incremented for NOT_LEADER (it's a block, not a plan)

- **Testing:**
  - Unit tests: TestHALeaderOnlyRemediation (7 tests)
  - HA smoke test: Verifies only leader has is_leader=1
  - Integration: docker-compose.ha.yml with 2 instances


## ADR-055 — LC-21: L1 WebSocket Integration for LiveConnectorV0

- **Date:** 2026-02-08
- **Status:** accepted
- **Context:**
  LiveConnectorV0 had `stream_ticks()` as a placeholder that yielded nothing. For production
  market data ingestion, we need real L1 bookTicker data from Binance WebSocket.

- **Decision:**
  1. Wire `stream_ticks()` to `BinanceWsConnector.iter_snapshots()` for real L1 data
  2. L1 (bookTicker) first, not L2 (depth):
     - L1 is sufficient for grid trading (bid/ask spread awareness)
     - Lower bandwidth, simpler parsing
     - L2 depth can be added if orderbook visibility required
  3. Idempotency via `last_seen_ts` check — no duplicate yields
  4. Injectable `ws_transport` in `LiveConnectorConfig` for testing without real WS
  5. Use `FakeWsTransport(delay_ms=2)` to ensure unique timestamps for idempotency tests
  6. Add 4 WebSocket metrics:
     - `grinder_ws_connected` (gauge): 1=connected, 0=disconnected
     - `grinder_ws_reconnect_total` (counter): reconnection events
     - `grinder_ticks_received_total` (counter): ticks per connector (ADR-028 compliant)
     - `grinder_last_tick_ts` (gauge): last tick timestamp per connector

- **Testing without flaky:**
  - `FakeWsTransport` injectable via `ws_transport` config field
  - `delay_ms=2` ensures real-time timestamp differences for idempotency
  - No network calls in unit tests
  - Bounded-time execution (no real sleeps)

- **Consequences:**
  - `stream_ticks()` now yields real `Snapshot` objects from Binance bookTicker stream
  - SafeMode still enforced (READ_ONLY default)
  - No LIVE_TRADE enablement (out of scope for LC-21)
  - Unit tests increased from 31 to 35 (+4 for stream_ticks with WS)

- **Related:**
  - ADR-029: LiveConnectorV0 design
  - ADR-030: Paper write-path v0

## ADR-056 — LC-22: LIVE_TRADE Write-Path for LiveConnectorV0

- **Date:** 2026-02-08
- **Status:** accepted
- **Context:**
  LiveConnectorV0 had write operations (`place_order`, `cancel_order`, `replace_order`)
  wired only to PaperExecutionAdapter for PAPER mode. For production trading, we need
  these operations to delegate to BinanceFuturesPort when in LIVE_TRADE mode.

- **Decision:**
  1. Add 3 safety gates + required `futures_port` for LIVE_TRADE writes (lowest level, can't be bypassed):
     - Gate 1: `armed=True` in config (explicit arming)
     - Gate 2: `mode=LIVE_TRADE` (explicit mode selection)
     - Gate 3: `ALLOW_MAINNET_TRADE=1` env var (external safeguard)
     - Required: `futures_port` must be configured (injectable dependency, not a safety gate)
  2. All 3 gates + futures_port must pass; any failure → `ConnectorNonRetryableError` with actionable message
  3. Injectable `futures_port` via `LiveConnectorConfig.futures_port` for testing
  4. PAPER mode unchanged (uses PaperExecutionAdapter, ignores armed flag)
  5. READ_ONLY mode unchanged (blocks all writes)

- **Gate rationale:**
  - Gate 1 (`armed`): Prevents accidental trading from config typo
  - Gate 2 (`mode`): Explicit intent declaration in code
  - Gate 3 (`ALLOW_MAINNET_TRADE`): External safeguard, can be managed at infra level
  - `futures_port` (required dependency): Ensures trading infrastructure is wired

- **Testing:**
  - `FakeFuturesPort` records calls without network
  - 4 negative-path tests (3 gates + futures_port check)
  - 4 positive-path tests (place/cancel/replace delegation + edge case)
  - All tests use monkeypatch for env var control

- **Consequences:**
  - LIVE_TRADE mode now routes writes to BinanceFuturesPort
  - 3 safety gates + required futures_port prevent accidental mainnet trading
  - Unit tests increased from 35 to 43 (+8 for LIVE_TRADE gates)
  - Mainnet smoke requires `ALLOW_MAINNET_TRADE=1 PYTHONPATH=src pytest tests/smoke/...`

- **Related:**
  - ADR-029: LiveConnectorV0 design
  - ADR-030: Paper write-path v0
  - ADR-055: LC-21 L1 WebSocket integration
  - ADR-040: Futures USDT-M mainnet smoke (for ALLOW_MAINNET_TRADE pattern)

---

## ADR-057 — L2 Gating Semantics (M7-03)

**Status:** Accepted
**Date:** 2026-02-09
**Deciders:** Core team
**Scope:** AdaptiveGridPolicy L2 gating for entry blocking

### Context

M7-02 added L2FeatureSnapshot with computed L2 features (impact, wall scores, insufficient depth flags). M7-03 wires these features into AdaptiveGridPolicy to gate entries based on order book conditions.

### Decision

1. **Feature flag:** `AdaptiveGridConfig.l2_gating_enabled: bool = False` (default OFF)
   - Preserves v1 behavior when disabled or when l2_features=None
   - Enables incremental rollout without breaking existing deployments

2. **Blocking semantics (entries only, not exits):**
   - **Blocking = no NEW entry orders** on the affected side
   - Existing positions are unaffected: reduce-only orders and exits are still allowed
   - Policy produces `GridPlan` (entry intents); execution layer handles order placement
   - When buy-side blocked: `levels_down=0` (no new buy entries)
   - When sell-side blocked: `levels_up=0` (no new sell entries)
   - Full pause (both sides blocked): `ResetAction.HARD` triggered

3. **Insufficient depth gate (hard block):**
   - When `impact_buy_topN_insufficient_depth == 1`: block buy-side entries (levels_down=0)
   - When `impact_sell_topN_insufficient_depth == 1`: block sell-side entries (levels_up=0)
   - Rationale: insufficient depth means we cannot reliably compute VWAP slippage

4. **Impact threshold gate (soft block):**
   - `l2_impact_threshold_bps: int = 200` (default)
   - When `impact_buy_topN_bps >= threshold`: block buy-side entries
   - When `impact_sell_topN_bps >= threshold`: block sell-side entries
   - **Threshold rationale (200 bps):**
     - Based on typical grid step of 50-100 bps: 2x step = 200 bps slippage makes entry unprofitable
     - Empirical observation: BTCUSDT normal depth gives 0-5 bps impact for 0.003 BTC qty_ref
     - 200 bps is 40-200x normal, indicating severely degraded liquidity
     - Conservative default; can be tuned per-symbol/venue in production

5. **Priority:** Insufficient depth takes precedence (checked first)
   - Reason codes reflect the root cause (e.g., `L2_INSUFFICIENT_DEPTH_BUY`)
   - Avoids duplicate reason codes when both conditions apply

6. **Reason codes (SSOT contract):**

| Code | Meaning | When Emitted |
|------|---------|--------------|
| `L2_INSUFFICIENT_DEPTH_BUY` | Buy-side depth exhausted; cannot compute impact | `impact_buy_topN_insufficient_depth == 1` |
| `L2_INSUFFICIENT_DEPTH_SELL` | Sell-side depth exhausted; cannot compute impact | `impact_sell_topN_insufficient_depth == 1` |
| `L2_IMPACT_BUY_HIGH` | Buy-side VWAP slippage exceeds threshold | `impact_buy_topN_bps >= threshold` (and not insufficient) |
| `L2_IMPACT_SELL_HIGH` | Sell-side VWAP slippage exceeds threshold | `impact_sell_topN_bps >= threshold` (and not insufficient) |
| `L2_BLOCK_BUY` | Summary: buy-side blocked (any L2 reason) | Added when `block_buy_side=True` |
| `L2_BLOCK_SELL` | Summary: sell-side blocked (any L2 reason) | Added when `block_sell_side=True` |

### Consequences

- v1 behavior unchanged when `l2_gating_enabled=False` or `l2_features=None`
- Grid can now adapt to order book liquidity conditions
- Entry blocking is deterministic and auditable via reason codes
- Existing positions unaffected (reduce-only allowed)
- Tests cover all 4 fixture scenarios (normal, ultra_thin, wall_bid, thin_insufficient)

### Related

- SPEC_V2_0.md §B.2: L2 feature definitions
- ADR-022: AdaptiveGridPolicy v1
- ADR-032: DdAllocator

## ADR-058 — M7-04: Apply DD budget ratio in AdaptiveGridPolicy (single-symbol, policy-level)

**Status:** Accepted
**Date:** 2026-02-09
**Deciders:** Core team
**Scope:** AdaptiveGridPolicy DD budget ratio wiring for single-symbol sizing

### Context

We have a portfolio-level DD allocator (`DdAllocator.allocate()`), which outputs per-symbol
`dd_budget_ratio` in range [0..1]. The allocator operates at the portfolio/orchestration layer.

We need Smart Grid v2.0 to respect per-symbol DD budget constraints without coupling
`AdaptiveGridPolicy` to portfolio selection/TopK orchestration.

### Decision

1. **Policy input contract:**
   - `AdaptiveGridPolicy.evaluate()` accepts an optional, pre-computed `dd_budget_ratio: Decimal | None`
   - Expected range: [0..1], validated at entry (ValueError if out of range)
   - The policy does NOT call DdAllocator directly and does not know about portfolios/candidates
   - The orchestration layer (or tests) computes allocations and passes the per-symbol ratio

2. **Default behavior unchanged:**
   - When `dd_budget_ratio is None`: no DD scaling is applied (v1-equivalent behavior)

3. **Semantics (SSOT):**
   - `dd_budget_ratio is None` → no DD scaling (v1 behavior)
   - `dd_budget_ratio == 1` → no scaling
   - `0 < dd_budget_ratio < 1` → scale per-level entry sizing:
     - `qty_per_level = qty_per_level * dd_budget_ratio` (exact Decimal multiplication)
     - **No rounding in policy** — symbol-specific lot size rounding at execution layer
   - `dd_budget_ratio == 0` → **block NEW entries** on both sides:
     - `levels_up = 0`, `levels_down = 0`
     - reduce-only / existing-position management is unaffected
     - `ResetAction.HARD` triggered

4. **Determinism:**
   - Uses `Decimal` for dd_budget_ratio and sizing computations
   - Policy outputs exact scaled values (no precision loss)
   - Lot size rounding deferred to execution layer where stepSize is known
   - No floats, no locale-dependent formatting

5. **Reason codes (SSOT table):**

| Code | Condition | Effect |
|------|-----------|--------|
| `DD_SCALE_APPLIED` | 0 < ratio < 1 | qty scaled (exact Decimal) |
| `DD_BLOCK_ENTRIES` | ratio == 0 | levels_up=0 and levels_down=0 |

Note: No reason code emitted for ratio=None or ratio=1 (unchanged behavior).

### Non-goals

- Portfolio orchestration / TopK selection is out of scope
- Changing DD allocator math is out of scope
- Live L2 streaming and execution walk-the-book are out of scope

### Consequences

- v1 behavior unchanged when `dd_budget_ratio is None`
- Policy remains pure and testable (no knowledge of portfolio layer)
- DD budget constraints can be applied without architectural coupling
- Entry blocking is deterministic and auditable via reason codes

### Related

- ADR-032: DdAllocator
- ADR-057: L2 gating semantics
- ADR-022: AdaptiveGridPolicy v1

## ADR-059 — M7-05: Execution-layer qty rounding with stepSize/minQty

**Status:** Accepted
**Date:** 2026-02-10
**Deciders:** Core team
**Scope:** ExecutionEngine qty constraints for symbol-specific lot size rounding and minimum qty validation

### Context

ADR-058 established that policy outputs exact Decimal quantities without rounding, deferring
symbol-specific lot size constraints to the execution layer. This ADR defines how execution
layer applies those constraints.

Exchange-specific constraints:
- `stepSize` (lot size): qty must be a multiple of this value (e.g., 0.001 for BTC)
- `minQty`: minimum order quantity enforced by exchange (e.g., 0.001 for BTC)

Orders with qty < minQty are rejected by the exchange. We must handle this gracefully.

### Decision

1. **Single point of application:**
   - `ExecutionEngine._apply_qty_constraints(qty, symbol)` is the ONLY place where
     step_size rounding and min_qty validation occurs
   - Called immediately before `port.place_order()`

2. **Constraint provider:**
   - `SymbolConstraints` dataclass: `step_size: Decimal`, `min_qty: Decimal`
   - Passed to `ExecutionEngine.__init__` as `symbol_constraints: dict[str, SymbolConstraints]`
   - Empty dict or missing symbol = no constraints applied (pass-through)

3. **Rounding semantics:**
   - `floor_to_step(qty, step_size)` — always floor (ROUND_DOWN), never round up
   - Deterministic: uses Decimal arithmetic only, no floats

4. **Validation semantics:**
   - If `rounded_qty < min_qty`: skip order, emit `ORDER_SKIPPED` event
   - Reason code: `EXEC_QTY_BELOW_MIN_QTY`
   - Order is NOT placed to port

5. **Event emission:**
   - `ORDER_SKIPPED` event with details:
     - `reason`: `EXEC_QTY_BELOW_MIN_QTY`
     - `level_id`, `side`, `original_qty`, `rounded_qty`

### Reason codes (SSOT table)

| Code | Condition | Effect |
|------|-----------|--------|
| `EXEC_QTY_BELOW_MIN_QTY` | rounded_qty < min_qty | Order skipped, not placed |

### Non-goals

- Dynamic constraint fetching from exchange (constraints are passed at engine init)
- Price step rounding (price rounding uses existing `_round_price()` method)
- Retry logic for rejected orders

### Consequences

- DD→qty→execution chain is complete: policy scales qty, execution rounds and validates
- Orders below minQty are gracefully skipped with audit trail
- No hidden precision contracts in policy layer
- Deterministic: same constraints + same input = same output

### Related

- ADR-058: DD budget ratio (policy-level, no rounding)
- ADR-032: DdAllocator

## ADR-060 — M7-06: ConstraintProvider for symbol step_size/min_qty

**Status:** Accepted
**Date:** 2026-02-10
**Deciders:** Core team
**Scope:** Automated loading of symbol constraints from Binance exchangeInfo

### Context

ADR-059 introduced `ExecutionEngine.symbol_constraints` but left it empty by default.
To enable qty rounding and minQty validation in production, we need to populate
constraints from the exchange.

Sources of truth for Binance Futures USDT-M:
- REST endpoint: `GET /fapi/v1/exchangeInfo`
- Filter type: `LOT_SIZE` contains `stepSize` and `minQty`

### Decision

1. **ConstraintProvider class:**
   - Loads constraints from Binance exchangeInfo
   - Supports offline mode via local JSON cache
   - Parses `LOT_SIZE` filter for each symbol

2. **Data sources (priority order):**
   - Local JSON cache (`var/cache/exchange_info_futures.json`)
   - Binance REST API (if cache stale or missing)
   - Stale cache fallback (if API unavailable)

3. **Caching strategy:**
   - Cache TTL: 24 hours (configurable)
   - Cache file is deterministic JSON (sorted keys, indent=2)
   - Cache can be pre-populated via CLI script

4. **Parsing semantics:**
   - All values parsed as strings to Decimal (determinism)
   - Missing LOT_SIZE filter = no constraints for symbol
   - Invalid filter data = log warning, skip symbol

5. **Missing constraints behavior:**
   - If symbol not in constraints dict: pass-through (no rounding)
   - This matches ADR-059 default behavior
   - Safe for backward compatibility

6. **CLI script:**
   - `scripts/fetch_exchange_info.py` for ops/runbook
   - Fetches from API and saves to cache
   - Shows parsed constraints summary

### API Contract

```python
from grinder.execution.constraint_provider import (
    ConstraintProvider,
    load_constraints_from_file,
)

# From cache file (offline)
constraints = load_constraints_from_file(Path("var/cache/exchange_info_futures.json"))

# From provider (with optional API fetch)
provider = ConstraintProvider(http_client=client, config=config)
constraints = provider.get_constraints()

# Wire into engine
engine = ExecutionEngine(port=port, symbol_constraints=constraints)
```

### Non-goals

- COIN-M support (only USDT-M)
- Auto-refresh in background
- Multi-venue abstraction
- Dynamic constraint updates during runtime

### Consequences

- Constraints can be populated from exchange without manual configuration
- Offline mode supported for testing and air-gapped environments
- Cache prevents excessive API calls
- Deterministic parsing ensures replay consistency

### Related

- ADR-059: ExecutionEngine qty constraints
- ADR-058: DD budget ratio (policy-level)

## ADR-061 — M7-07: ExecutionEngineConfig with constraints_enabled flag

**Status:** Accepted
**Date:** 2026-02-10
**Deciders:** Core team
**Scope:** Wiring ConstraintProvider to ExecutionEngine with explicit enablement

### Context

ADR-059/060 introduced symbol constraints and ConstraintProvider, but constraints
were applied automatically whenever `symbol_constraints` dict was non-empty.

For safe rollout, we need:
1. An explicit config flag to enable/disable constraint application
2. Lazy loading of constraints from provider
3. Backward compatibility (default = disabled)

### Decision

1. **ExecutionEngineConfig dataclass:**
   ```python
   @dataclass(frozen=True)
   class ExecutionEngineConfig:
       constraints_enabled: bool = False  # Default OFF for safety
   ```

2. **Engine accepts config and provider:**
   ```python
   engine = ExecutionEngine(
       port=port,
       config=ExecutionEngineConfig(constraints_enabled=True),
       constraint_provider=provider,  # Lazy loading
   )
   ```

3. **Constraint application logic:**
   - If `config.constraints_enabled=False`: pass-through (no rounding, no minQty check)
   - If `config.constraints_enabled=True`: apply floor_to_step + minQty validation
   - Constraints lazy-loaded from provider on first use

4. **Priority order:**
   - `symbol_constraints` dict (direct) takes precedence over provider
   - Allows test fixtures to override provider

5. **Backward compatibility:**
   - Default config = `constraints_enabled=False`
   - Existing code without config = no behavior change
   - M7-05/06 tests updated to pass `config=ExecutionEngineConfig(constraints_enabled=True)`

### API Contract

```python
from grinder.execution import (
    ExecutionEngine,
    ExecutionEngineConfig,
    ConstraintProvider,
)

# Explicit enablement (recommended for production)
config = ExecutionEngineConfig(constraints_enabled=True)
provider = ConstraintProvider.from_cache(cache_path)
engine = ExecutionEngine(port=port, config=config, constraint_provider=provider)

# Direct constraints (for tests)
engine = ExecutionEngine(
    port=port,
    symbol_constraints=constraints,
    config=ExecutionEngineConfig(constraints_enabled=True),
)

# Disabled (default, backward compatible)
engine = ExecutionEngine(port=port)  # No constraints applied
```

### Non-goals

- Runtime toggle of constraints_enabled
- Per-symbol enable/disable
- Hot-reload of constraints

### Consequences

- Safe rollout: constraints can be loaded but not applied until explicitly enabled
- Lazy loading: provider not called until first order placement
- Backward compatible: existing code works unchanged
- Tests must explicitly enable constraints to test constraint behavior

### Related

- ADR-059: ExecutionEngine qty constraints (implementation)
- ADR-060: ConstraintProvider (loading)
- ADR-058: DD budget ratio (policy-level)

## ADR-062 — M7-09: Execution L2 Guard (last-mile safety)

- **Date:** 2026-02-10
- **Status:** accepted
- **PR:** #132
- **Related:** M7-09, SPEC_V2_0.md §B

### Context

After M7-03 (policy L2 gating), we have policy-level guards that skip entries based on L2 conditions.
However, between policy evaluation and order placement, market conditions can change.
We need a "last-mile safety" guard at the execution layer to catch:

1. Stale L2 data (snapshot too old)
2. Insufficient depth (would exhaust orderbook)
3. High slippage impact (adverse conditions)

This is NOT a replacement for policy gating — it's a final safety net at execution time.

### Decision

1. **New config fields in ExecutionEngineConfig:**
   ```python
   @dataclass(frozen=True)
   class ExecutionEngineConfig:
       # M7-07 fields...
       constraints_enabled: bool = False

       # M7-09 L2 guard fields
       l2_execution_guard_enabled: bool = False  # Default OFF
       l2_execution_max_age_ms: int = 1500       # Staleness threshold
       l2_execution_impact_threshold_bps: int = 50  # Impact threshold
   ```

2. **L2 features input:**
   ```python
   engine = ExecutionEngine(
       port=port,
       config=config,
       l2_features={"BTCUSDT": l2_snapshot},  # dict[str, L2FeatureSnapshot]
   )
   ```

3. **Guard logic in `_apply_l2_guard()`:**
   - Only applies to PLACE actions
   - Runs BEFORE qty constraints
   - Checks in order:
     a. Staleness: `now_ts - snapshot.ts_ms > max_age_ms` → `EXEC_L2_STALE`
     b. Insufficient depth: `impact_*_topN_insufficient_depth == 1` → `EXEC_L2_INSUFFICIENT_DEPTH_BUY/SELL`
     c. High impact: `impact_*_topN_bps >= threshold` → `EXEC_L2_IMPACT_BUY_HIGH/SELL_HIGH`

4. **Pass-through behavior (safe rollout):**
   - `l2_execution_guard_enabled=False` → no checks, pass-through
   - `l2_features=None` → no checks, pass-through
   - Symbol not in l2_features → no checks, pass-through

5. **Reason codes (SSOT):**
   | Code | Meaning |
   |------|---------|
   | `EXEC_L2_STALE` | L2 snapshot older than max_age_ms |
   | `EXEC_L2_INSUFFICIENT_DEPTH_BUY` | Buy side depth exhausted |
   | `EXEC_L2_INSUFFICIENT_DEPTH_SELL` | Sell side depth exhausted |
   | `EXEC_L2_IMPACT_BUY_HIGH` | Buy impact >= threshold |
   | `EXEC_L2_IMPACT_SELL_HIGH` | Sell impact >= threshold |

### API Contract

```python
from grinder.execution import ExecutionEngine, ExecutionEngineConfig
from grinder.features.l2_types import L2FeatureSnapshot

# Enable L2 guard
config = ExecutionEngineConfig(
    l2_execution_guard_enabled=True,
    l2_execution_max_age_ms=1500,
    l2_execution_impact_threshold_bps=50,
)

# Provide L2 features per symbol
l2_features = {"BTCUSDT": latest_snapshot}

engine = ExecutionEngine(port=port, config=config, l2_features=l2_features)
```

### Non-goals

- L2 streaming/websocket wiring (separate concern)
- Dynamic threshold adjustment
- Multi-symbol orchestration
- "Smart" qty/price adjustments based on L2 (only block/skip)

### Consequences

- Default OFF: no behavior change until explicitly enabled
- Determinism preserved: guard uses integer comparisons only
- No I/O at init: L2 features are passed in, not fetched
- Single point of application: guard in `_apply_actions()` before qty constraints
- Side-aware: checks appropriate impact/depth based on order side

### Related

- ADR-059: ExecutionEngine qty constraints
- ADR-061: ExecutionEngineConfig with constraints_enabled
- M7-03: Policy L2 gating

## ADR-063 — M7-08: ConstraintProvider TTL and Refresh Policy

- **Date:** 2026-02-10
- **Status:** accepted
- **PR:** #133
- **Related:** M7-06 (ADR-060), M7-07 (ADR-061)

### Context

ConstraintProvider loads symbol constraints (step_size, min_qty) from Binance exchangeInfo.
For production use, we need:

1. **TTL semantics**: Cache should expire after a configurable time
2. **Controlled refresh**: API fetch should be gated by explicit flag
3. **Graceful degradation**: Fallback chain when API is unavailable
4. **No I/O at init**: Preserve determinism and fast startup

### Decision

1. **ConstraintProviderConfig with TTL:**
   ```python
   @dataclass(frozen=True)
   class ConstraintProviderConfig:
       cache_dir: Path = Path("var/cache")
       cache_file: str = "exchange_info_futures.json"
       cache_ttl_seconds: int = 86400  # 24 hours
       allow_fetch: bool = True
       exchange_info_url: str = BINANCE_FUTURES_EXCHANGE_INFO_URL
   ```

2. **Staleness check:**
   - Cache is stale when `now - mtime(cache_file) > cache_ttl_seconds`
   - Uses `Path.stat().st_mtime` for file modification time
   - TTL only affects "decision to fetch", not stale fallback usage

3. **Refresh policy (get_constraints flow):**
   ```
   1. If in-memory cache exists and not force_refresh → return it
   2. If file cache is fresh (within TTL) → load and return
   3. If allow_fetch=True and http_client available:
      a. Try API fetch
      b. On success: save to cache, return constraints
      c. On failure: log warning, continue to fallback
   4. Fallback: try loading stale cache (if exists)
   5. Final fallback: return empty dict {}
   ```

4. **No auto-refresh on init:**
   - `__init__` does no I/O
   - Constraints loaded lazily on `get_constraints()` call
   - Preserves determinism in replay (constraints_enabled=False by default)

5. **Logging (debug/info):**
   - `cache_hit_fresh`: "Loaded N constraints from cache"
   - `cache_stale_api_success`: "Fetched N constraints from API"
   - `cache_stale_api_fail`: "API fetch failed, using stale cache"
   - `no_constraints`: "No constraints available"

### API Contract

```python
# Production: fetch enabled, 24h TTL
config = ConstraintProviderConfig(
    cache_ttl_seconds=86400,
    allow_fetch=True,
)
provider = ConstraintProvider(http_client=client, config=config)

# Offline/replay: fetch disabled, use cache only
provider = ConstraintProvider.from_cache(Path("var/cache/exchange_info.json"))
# Equivalent to: allow_fetch=False, uses existing cache

# Force refresh (ops/CLI)
constraints = provider.get_constraints(force_refresh=True)
```

### Non-goals

- Auto-refresh on background thread
- Per-symbol TTL
- Cache invalidation signals
- Retry/backoff logic (single attempt only)

### Consequences

- **Safe by default**: `allow_fetch=False` in offline mode, `constraints_enabled=False` means no constraint impact
- **Determinism preserved**: No network I/O in replay path
- **Graceful degradation**: Stale cache is better than no constraints
- **Testable**: All network paths mockable via `allow_fetch=False` + fixtures

### Fallback Chain Summary

| Cache State | allow_fetch | API Result | Outcome |
|-------------|-------------|------------|---------|
| Fresh | any | - | Use fresh cache |
| Stale | True | Success | Use API, update cache |
| Stale | True | Failure | Use stale cache |
| Stale | False | - | Use stale cache |
| Missing | True | Success | Use API, create cache |
| Missing | True | Failure | Empty dict |
| Missing | False | - | Empty dict |

### Related

- ADR-060: ConstraintProvider initial implementation
- ADR-061: ExecutionEngineConfig constraints_enabled gate
- ADR-059: ExecutionEngine qty constraints

---

## ADR-064 — M8-00: ML Integration Specification

- **Date:** 2026-02-12
- **Status:** accepted
- **Milestone:** M8-00 (docs-only)
- **Artifact:** docs/12_ML_SPEC.md

### Context

M8 introduces ML-based regime classification to improve parameter calibration.
Before implementing code, we need clear contracts for:

1. **I/O contracts**: What features go in, what signals come out
2. **Determinism**: How to preserve replay reproducibility with ML inference
3. **Artifacts**: How to version and validate model files
4. **Enablement**: How to safely roll out ML without breaking existing behavior

### Decision

1. **Input contract**: ML models receive `FeatureSnapshot` (L1+volatility) and optionally `L2FeatureSnapshot` (order book depth). Both are existing SSOT types.

2. **Output contract**: New `MlSignalSnapshot` type with:
   - Regime probabilities in integer bps (sum = 10000)
   - Predicted regime ("LOW"/"MID"/"HIGH")
   - Spacing multiplier as x1000 integer
   - Model version + hash for traceability
   - Feature importance for interpretability

3. **Determinism invariants**:
   - 8 MUST rules (e.g., same inputs → same outputs, fixed random seeds)
   - 7 MUST NOT anti-patterns (e.g., no random dropout, no clock-based features)
   - Verification script: `verify_ml_determinism`

4. **Artifact scheme**:
   - Directory: `var/models/<model_name>/`
   - Manifest: `manifest.json` with SHA256 checksums for all files
   - Model format: ONNX for portability
   - Semantic versioning: MAJOR.MINOR.PATCH

5. **Enablement model**:
   - `ml.enabled=False` by default (safe rollout)
   - 4-level gate hierarchy: global → artifact → runtime → output
   - Fallback options: neutral, previous, error
   - Shadow mode support for A/B comparison

### M8 Milestone Plan

| Sub-milestone | Scope | Deliverables |
|--------------|-------|--------------|
| M8-00 | Docs-only | ML spec (this ADR), I/O contracts, determinism rules |
| M8-01 | Stub | MlSignalSnapshot type, MlModelPort protocol, NeutralMlModel stub |
| M8-02 | ONNX | OnnxMlModel implementation, artifact loader, shadow mode |

### Non-goals (M8)

- Training pipeline (out of scope, offline/manual)
- Hyperparameter tuning automation
- Real-time model updates
- Multi-model ensemble

### Consequences

- **Safe by default**: ML disabled means zero impact on existing behavior
- **Determinism preserved**: All inference paths use fixed seeds and integer outputs
- **Testable**: NeutralMlModel stub enables unit testing without real model
- **Traceable**: Every ML output includes model version + hash

### Related

- docs/12_ML_SPEC.md (SSOT for ML contracts)
- src/grinder/features/types.py (FeatureSnapshot input)
- src/grinder/features/l2_types.py (L2FeatureSnapshot input)

## ADR-065 — M8-02c: Shadow → Active Inference Transition

- **Date:** 2026-02-14
- **Status:** proposed
- **Context:** M8-02b delivered shadow mode — ONNX inference runs but does NOT affect trading decisions or digests. Before enabling "active" mode (where inference results affect policy), we need explicit safety gates to prevent accidental activation and ensure instant rollback.

### State Machine

```
┌─────────────────────────────────────────────────────────────────┐
│                     ML Inference Modes                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌─────┐    ml_infer_enabled    ┌────────┐    ml_active_     ┌────────┐
│   │ OFF │ ──────────────────────▶│ SHADOW │ ──────enabled────▶│ ACTIVE │
│   └─────┘                        └────────┘                    └────────┘
│      ▲                               │                             │
│      │                               │                             │
│      │       kill-switch OR          │       kill-switch OR        │
│      │       config change           │       config change         │
│      └───────────────────────────────┴─────────────────────────────┘
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

| Mode | `ml_infer_enabled` | `ml_shadow_mode` | `ml_active_enabled` | Affects Decisions |
|------|-------------------|------------------|---------------------|-------------------|
| OFF | `False` | `False` | `False` | No |
| SHADOW | `True` | `True` | `False` | No (log only) |
| ACTIVE | `True` | `False` | `True` | **YES** |

---

### Decision

#### 1. Two-key activation (2-man rule)

ACTIVE mode requires BOTH `ml_infer_enabled=True` AND `ml_active_enabled=True`.
Missing either key → `ConfigError` (no silent degradation).

#### 2. Kill-switch priority (highest)

Kill-switch is evaluated **per snapshot** (every decision tick), not just at startup.
Takes effect immediately without process restart.

| Source | Check |
|--------|-------|
| Env var | `ML_KILL_SWITCH=1` |
| Config | `ml_kill_switch=True` |

Kill-switch checked BEFORE any inference logic. If set → force OFF mode (no error, just skip inference).

#### 3. Explicit acknowledgment for ACTIVE

Config must include exact string:
```
ml_active_ack = "I_UNDERSTAND_THIS_AFFECTS_TRADING"
```
Any other value or missing → `ConfigError` with message:
```
ACTIVE mode requires explicit acknowledgment. Set ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING"
```

#### 4. Environment allowlist

| Config | Env var | Semantics |
|--------|---------|-----------|
| `ml_active_allowed_envs` | `GRINDER_ENV` | List of allowed environments |

**Default behavior:**
- `ml_active_allowed_envs=[]` (empty) → **allowed everywhere** (two-key + ack still required)
- `ml_active_allowed_envs=["prod"]` → ACTIVE only if `GRINDER_ENV=prod`

If set and mismatch → `ConfigError`:
```
ACTIVE mode not allowed in environment 'dev'. Allowed: ['prod']
```

#### 5. Failure modes: Shadow vs Active

| Mode | Failure Type | Behavior |
|------|--------------|----------|
| SHADOW | ONNX load fails | **Soft-fail**: log warning, continue without ML |
| SHADOW | Inference error | **Soft-fail**: log warning, return None |
| SHADOW | Invalid manifest | **Soft-fail**: log warning, skip inference |
| ACTIVE | ONNX load fails | **Fail-closed**: `ConfigError` at startup |
| ACTIVE | Inference error | **Fail-closed**: log error, do NOT affect policy |
| ACTIVE | Invalid manifest | **Fail-closed**: `ConfigError` at startup |

**Principle:** ACTIVE mode is fail-closed. Any error → do NOT mutate policy. Shadow can continue degraded.

---

### Truth Table: ACTIVE Mode Activation

ACTIVE inference affects policy **only if ALL conditions are true**:

| # | Condition | Required |
|---|-----------|----------|
| 1 | `ML_KILL_SWITCH` env var is NOT `1` | ✓ |
| 2 | `ml_kill_switch` config is NOT `True` | ✓ |
| 3 | `ml_infer_enabled = True` | ✓ |
| 4 | `ml_active_enabled = True` | ✓ |
| 5 | `ml_active_ack = "I_UNDERSTAND_THIS_AFFECTS_TRADING"` | ✓ |
| 6 | `ONNX_AVAILABLE = True` | ✓ |
| 7 | `onnx_artifact_dir` is set and manifest valid | ✓ |
| 8 | If `ml_active_allowed_envs` non-empty → `GRINDER_ENV` ∈ allowlist; if empty → allow all | ✓ |

**Any violation → ACTIVE disabled.** Guards G1-G7 raise `ConfigError` at startup; kill-switch forces OFF silently.

---

### Config Guards

| # | Condition | Action |
|---|-----------|--------|
| K1 | `ML_KILL_SWITCH=1` env var | Force OFF (silent, per-snapshot) |
| K2 | `ml_kill_switch=True` config | Force OFF (silent, per-snapshot) |
| G1 | `ml_infer_enabled=True` + `!ONNX_AVAILABLE` | `ConfigError` |
| G2 | `ml_infer_enabled=True` + `!ml_shadow_mode` + `!ml_active_enabled` | `ConfigError` (ambiguous) |
| G3 | `ml_active_enabled=True` + `!ml_infer_enabled` | `ConfigError` |
| G4 | `ml_active_enabled=True` + `ml_active_ack` missing/wrong | `ConfigError` |
| G5 | `ml_active_enabled=True` + `onnx_artifact_dir=None` | `ConfigError` |
| G6 | `ml_active_enabled=True` + env not in `ml_active_allowed_envs` (if set) | `ConfigError` |
| G7 | `ml_active_enabled=True` + manifest invalid/missing | `ConfigError` |

---

### Observability Requirements

#### Metrics (required for ACTIVE)

| Metric | Type | Labels | Purpose |
|--------|------|--------|---------|
| `grinder_ml_mode` | Gauge | `mode=OFF\|SHADOW\|ACTIVE` | Current mode |
| `grinder_ml_active_on` | Gauge | — | 0/1 flag for alerts |
| `grinder_ml_inference_total` | Counter | `mode`, `result=ok\|error\|skip` | Inference count |
| `grinder_ml_inference_latency_ms` | Histogram | `mode` | Latency |
| `grinder_ml_active_decision_impact_total` | Counter | — | Decisions affected by ML |

#### Logs (required)

| Event | When | Fields |
|-------|------|--------|
| `ML_MODE_INIT` | Startup | `mode`, `config_hash` |
| `ML_ACTIVE_ENABLED` | Enter ACTIVE | `artifact_sha`, `env` |
| `ML_ACTIVE_DISABLED` | Exit ACTIVE | `reason`, `trigger` |
| `ML_ACTIVE_BLOCKED` | ACTIVE denied | `reason=wrong_ack\|kill_switch\|artifact_invalid\|env_not_allowed\|onnx_missing\|missing_flag` |
| `ML_KILL_SWITCH_TRIGGERED` | Kill-switch activates | `source=env\|config` |

---

### Rollback Paths

| Action | Effect | Latency |
|--------|--------|---------|
| Set `ML_KILL_SWITCH=1` | Force OFF | **Immediate** (next snapshot) |
| Set `ml_kill_switch=True` | Force OFF | **Immediate** (next snapshot) |
| Set `ml_active_enabled=False` | Downgrade to SHADOW | Immediate (hot-reload) or restart |
| Remove `ml_active_ack` | Downgrade to SHADOW | Restart required |

---

### Consequences

- **ACTIVE mode is never accidental:** requires explicit `ml_active_enabled=True` + `ml_active_ack` + passing all guards
- **Instant rollback:** kill-switch evaluated per-snapshot, no restart needed
- **Fail-closed for ACTIVE:** any error → do NOT mutate policy
- **Audit trail:** all mode changes logged with timestamp + reason
- **Determinism preserved:** same config → same mode → same behavior
- **Alerting:** `grinder_ml_active_on=1` gauge enables "unexpected ACTIVE" alerts

### Alternatives Considered

1. **Single flag for ACTIVE** — rejected: too easy to enable accidentally
2. **ML affects decisions by default** — rejected: violates safe-by-default
3. **Soft-fail for ACTIVE errors** — rejected: ACTIVE must be fail-closed
4. **File-based kill-switch only** — rejected: env var is faster for k8s
5. **Empty allowlist = blocked everywhere** — rejected: two-key + ack sufficient

---

### Test Requirements (M8-02c-1)

| # | Test | Description |
|---|------|-------------|
| T1 | `test_active_requires_both_flags` | `ml_active_enabled=True` without `ml_infer_enabled=True` → ConfigError |
| T2 | `test_active_requires_ack` | `ml_active_enabled=True` without ack → ConfigError |
| T3 | `test_active_wrong_ack_string` | `ml_active_ack="yes"` → ConfigError |
| T4 | `test_kill_switch_env_overrides_all` | `ML_KILL_SWITCH=1` → mode OFF regardless of config |
| T5 | `test_kill_switch_config_overrides_all` | `ml_kill_switch=True` → mode OFF |
| T6 | `test_kill_switch_per_snapshot` | Kill-switch activates mid-run → next snapshot is OFF |
| T7 | `test_default_config_is_off` | Empty/default config → mode OFF |
| T8 | `test_shadow_soft_fail_continues` | ONNX load error in SHADOW → continues without ML |
| T9 | `test_active_load_fail_raises` | ONNX load error in ACTIVE → ConfigError |
| T10 | `test_active_inference_error_no_policy_mutation` | Inference error → policy_features unchanged |
| T11 | `test_shadow_does_not_affect_decisions` | Shadow predictions logged but not used |
| T12 | `test_active_never_mutates_without_all_keys` | Missing any key → policy_features unchanged |
| T13 | `test_env_allowlist_blocks_wrong_env` | `ml_active_allowed_envs=["prod"]` + `GRINDER_ENV=dev` → ConfigError |
| T14 | `test_env_allowlist_empty_allows_all` | `ml_active_allowed_envs=[]` → ACTIVE allowed |
| T15 | `test_manifest_invalid_blocks_active` | Invalid manifest SHA → ConfigError for ACTIVE |

---

### Implementation PRs

| PR | Scope | Status |
|----|-------|--------|
| M8-02c-0 | This ADR + DECISIONS.md update | proposed |
| M8-02c-1 | Guards + tests (no inference logic) | blocked on M8-02c-0 |
| M8-02c-2 | Active inference implementation | blocked on M8-02c-1 |

### Related

- ADR-064 (M8-00: ML Integration Specification)
- docs/12_ML_SPEC.md (SSOT for ML contracts)

## ADR-066 -- Defer Multi-venue until Post-launch

- **Date:** 2026-02-17
- **Status:** accepted
- **Context:** M8 (ML Integration + Feature Store) is complete (PR #134-#170). The next visible roadmap item is M9 (Multi-venue). Multi-venue introduces significant complexity across data connectors, execution semantics, risk controls, and observability. Single-venue production rollout is the higher-priority path.
- **Decision:** We defer M9 (Multi-venue) until after stable single-venue production rollout. Post-M8 focus shifts to single-venue launch readiness: rollout procedure (shadow -> staging -> active), e2e smoke, and operator runbooks.
- **Entry criteria to start M9:**
  - Single-venue rollout completed (shadow -> staging -> active) with a documented runbook.
  - SLOs met for N days with no unresolved P0 incidents.
  - Execution + risk controls validated in live ops (kill-switch, budgets, reconciliation).
- **Consequences:**
  - Short-term focus shifts to single-venue launch readiness (ops, rollout, e2e smoke).
  - Multi-venue work is out of scope for pre-launch milestones unless explicitly re-prioritized.
- **Alternatives:** "Start M9 immediately after M8" -- rejected because multi-venue surface area is too broad without validated single-venue ops.

## ADR-067 -- Unified env parsing with strict-by-default contract

- **Date:** 2026-02-21
- **Status:** accepted
- **Context:** Six divergent boolean-parsing patterns across triage-flow modules (fsm_evidence, account evidence, live engine, reconcile_loop). Each defined its own truthy/falsey sets with inconsistent coverage (some missing "on"/"off", some `== "1"` only). Unknown values were silently swallowed everywhere, making operator misconfiguration invisible.
- **Decision:** New SSOT module `src/grinder/env_parse.py` with `parse_bool`, `parse_int`, `parse_csv`, `parse_enum`. Canonical truthy: `{1, true, yes, on}`. Canonical falsey: `{0, false, no, off, ""}`. All case-insensitive, whitespace-stripped. `strict=True` is the default: unknown/invalid values raise `ConfigError`. `strict=False` logs a warning and returns the default.
- **Usage rules:**
  - New code should use `strict=True` (the default) unless there is a documented reason to tolerate invalid values.
  - Migrated legacy call sites use `strict=False` to preserve safe-by-default behavior without breaking existing deployments.
  - Callers must NOT define their own truthy/falsey sets -- use `env_parse` functions.
- **Consequences:** Invalid env values are now visible (either crash or warning log). Future env flags get consistent parsing for free. Migration of remaining non-triage call sites is P2.
- **Alternatives:** "strict=False default" -- rejected because it perpetuates silent misconfiguration; better to be loud-by-default and opt into leniency explicitly.

## ADR-068 -- Fill outcome dataset v1 (Track C, PR-C1)

- **Date:** 2026-02-21
- **Status:** accepted
- **Context:** P2 backlog items #4 (fill probability model) and #6 (consecutive loss limit) both depend on a structured dataset of completed fill roundtrips. No such dataset exists; fills are tracked in-memory by FillTracker but never persisted as roundtrip outcomes.
- **Decision:** New module `src/grinder/ml/fill_dataset.py` with:
  - `FillOutcomeRow` frozen dataclass (21 fields: identification, entry, exit, PnL, context, metadata).
  - `RoundtripTracker` that groups fills by (symbol, direction), emits a row when position transitions from non-zero back to zero.
  - `build_fill_dataset_v1()` writes `data.parquet` + `manifest.json` with deterministic settings (`write_statistics=False`, `compression="snappy"`).
  - Deterministic `row_id` = `sha1(symbol|direction|entry_ts|exit_ts|entry_price|exit_price|qty)`.
  - CLI: `scripts/build_fill_dataset_v1.py --fixture <path> --out-dir <path>`.
  - Artifact: `ml/datasets/fill_outcomes/v1/manifest.json` + `data.parquet`.
  - Manifest schema: `fill_outcomes_v1` (distinct from Feature Store v1 `feature_order_hash` schema).

- **Field schema (21 columns, deterministic column order in `FILL_OUTCOME_COLUMNS`):**

  | # | Field | Parquet Type | Description |
  |---|-------|-------------|-------------|
  | 1 | `row_id` | string | sha1 of canonical pipe-separated key fields |
  | 2 | `symbol` | string | Trading pair, e.g. "BTCUSDT" |
  | 3 | `direction` | string | "long" or "short" |
  | 4 | `entry_ts` | int64 | First entry fill timestamp (ms) |
  | 5 | `entry_price` | float64 | Weighted average entry price |
  | 6 | `entry_qty` | float64 | Total entry quantity |
  | 7 | `entry_fee` | float64 | Sum of fees on entry fills |
  | 8 | `entry_fill_count` | int32 | Number of fills that built the position |
  | 9 | `exit_ts` | int64 | Last exit fill timestamp (ms) |
  | 10 | `exit_price` | float64 | Weighted average exit price |
  | 11 | `exit_qty` | float64 | Total exit quantity |
  | 12 | `exit_fee` | float64 | Sum of fees on exit fills |
  | 13 | `exit_fill_count` | int32 | Number of fills that closed the position |
  | 14 | `realized_pnl` | float64 | (exit_price - entry_price) * qty * direction_sign |
  | 15 | `net_pnl` | float64 | realized_pnl - entry_fee - exit_fee |
  | 16 | `pnl_bps` | int32 | net_pnl / notional * 10000, rounded |
  | 17 | `holding_time_ms` | int64 | exit_ts - entry_ts |
  | 18 | `notional` | float64 | entry_price * entry_qty |
  | 19 | `outcome` | string | "win", "loss", or "breakeven" (based on net_pnl) |
  | 20 | `source` | string | "paper" (only value in v1) |
  | 21 | `dataset_version` | string | "v1" |

- **Determinism invariants:**
  - `row_id` = `sha1(f"{symbol}|{direction}|{entry_ts}|{exit_ts}|{entry_price}|{exit_price}|{qty}")`. Same canonical inputs → same hex digest, always.
  - Parquet: `write_statistics=False` + `compression="snappy"` → byte-identical `data.parquet` for identical input rows.
  - `manifest.sha256["data.parquet"]` = sha256 of the parquet file only. `created_at_utc` is **not** included in the sha256 — it lives in manifest metadata only. Therefore `data.parquet` digest is deterministic regardless of build time.
  - Manifest JSON uses `json.dumps(manifest, indent=2, sort_keys=True) + "\n"` for stable byte order. Full manifest byte-identity requires pinning `created_at_utc` (the CLI accepts `--created-at-utc` for this; tests always pin it).
  - Row order in parquet matches the order rows are emitted by `RoundtripTracker` (fill-sequence order, no re-sorting). Stable because `RoundtripTracker.record()` emits rows one-at-a-time in fill arrival order.

- **Out of scope (deferred to v2 or later):**
  - Live exchange fills (v1 is paper-fill only; `source="paper"`).
  - Slippage or partial-fill modeling (paper fills are all-or-nothing).
  - Per-fill detail rows (v1 aggregates to roundtrip level only).
  - Automatic dataset rebuild from FillTracker events (v1 requires explicit CLI invocation).
  - Feature Store v1 integration (`feature_order_hash`, labels) — different schema lineage.

- **Versioning plan:** Schema changes that add columns are additive (v1 consumers ignore unknown columns). Schema changes that remove, rename, or retype columns require a new `schema_version` ("fill_outcomes_v2") and a new `build_fill_dataset_v2()` entry point. The `dataset_version` field in each row records which builder wrote it.

- **Consequences:** PR-C2 (fill probability model) and PR-C3 (consecutive loss limit) have a stable SSOT to build on. pyarrow is in `[dev]` extras (tests) and `[ml]` extras (production). Dataset is paper-fill only for now; live fills deferred.
- **Alternatives:** "Reuse Feature Store v1 manifest" -- rejected because fill outcomes have different column semantics (no feature_order, no labels). "Store as CSV" -- rejected because parquet provides type safety, compression, and columnar access.

## ADR-069 -- Fill probability model v0 (Track C, PR-C2)

- **Date:** 2026-02-21
- **Status:** accepted
- **Context:** PR-C1 (#232) shipped the `fill_outcomes_v1` dataset. P2 backlog item #4 (fill probability model) needs a baseline estimator. v0 must be pure-Python (no sklearn), shadow-only (not wired to execution), and deterministic.
- **Decision:** New module `src/grinder/ml/fill_model_v0.py` with:
  - `FillModelV0` class: calibrated bin-count model, pure Python, no ML deps.
  - `FillModelFeaturesV0` TypedDict: SSOT feature contract (4 fields, all int/str).
  - `extract_features(row) -> FillModelFeaturesV0`: entry-side feature extraction.
  - `FillModelV0.train(rows)`, `.predict(features) -> int` (0..10000 bps), `.save()`, `.load()`.
  - CLI: `scripts/train_fill_model_v0.py --dataset <dir> --out-dir <dir>` (requires pyarrow).
  - Artifact: `model.json` + `manifest.json` with sha256 integrity.

- **Feature contract (`FillModelFeaturesV0` — SSOT):**

  | Field | Type | Source | Leakage-safe |
  |-------|------|--------|-------------|
  | `direction` | str ("long"/"short") | `FillOutcomeRow.direction` | Yes — known at entry |
  | `notional_bucket` | int (0..4) | `quantize(entry_price * entry_qty)` | Yes — known at entry |
  | `entry_fill_count` | int (1..3) | `FillOutcomeRow.entry_fill_count`, clamped | Yes — known at close |
  | `holding_ms_bucket` | int (0..4) | `quantize(holding_time_ms)` | Yes — known at close |

  **Leakage policy:** `pnl_bps`, `net_pnl`, `realized_pnl`, `exit_price` are NEVER used as features. They are outcome data only. The label is `outcome == "win"` → 1, else → 0. `holding_ms_bucket` is available only for retroactive analysis on completed roundtrips; online prediction would need a replacement feature.

- **Bucket boundaries (fixed for v0):**
  - Notional: 100, 500, 1000, 5000 (quote currency units)
  - Holding ms: 1000, 10000, 60000, 300000
  - Fill count: 1, 2, 3+ (clamped)

- **Model design:**
  - Composite bin key: `"{direction}|{notional_bucket}|{entry_fill_count}|{holding_ms_bucket}"`
  - Per-bin: `wins * 10000 // total` (integer arithmetic, no floats)
  - Global prior: same formula across all rows (fallback for unseen bins)
  - Empty dataset: returns 5000 bps (50% neutral)

- **Determinism invariants:**
  - `model.json`: `json.dumps(to_dict(), indent=2, sort_keys=True) + "\n"` — all values int/str, no floats
  - `manifest.sha256["model.json"]` = sha256 of model.json bytes
  - Integer-only arithmetic: `(wins * 10000 + total // 2) // total` avoids float rounding drift
  - `created_at_utc` is manifest metadata only, not in sha256

- **Out of scope (deferred):**
  - ~~Wiring into execution / risk / grid level filtering~~ (partially addressed: shadow wiring in PR-C4a; enforcement deferred to PR-C5)
  - Online learning or incremental updates
  - Drift detection or monitoring metrics
  - sklearn/numpy dependency (v0 is pure Python)
  - ONNX format (v0 uses JSON; ONNX deferred to v1+)

- **Consequences:** PR-C3 (consecutive loss limit) can consume model predictions. No new deps (pyarrow already in `[dev]`/`[ml]`; core module is pure Python). `docs/GAPS.md` fill probability row updated PLANNED → PARTIAL.
- **Alternatives:** "Use sklearn logistic regression" — rejected for v0 because it adds heavy dependency and makes core untestable without `[ml]` extras. "Train on pnl_bps as feature" — rejected due to label leakage (pnl_bps is outcome data, not entry-side).

### PR-C4a update: shadow loader + metrics (2026-02-22)

- **Scope:** Wire FillModelV0 into live reconcile pipeline as shadow-only (observe, no decision impact).
- **New module:** `src/grinder/ml/fill_model_loader.py`
  - `load_fill_model_v0(model_dir)` → `FillModelV0 | None` (fail-open on any error)
  - `extract_online_features()` → `FillModelFeaturesV0` from live pipeline fields (entry-side only, `entry_fill_count=1` conservative, `holding_ms=0` at planning time)
  - Module-level shadow metrics state: `set_fill_model_metrics()` / `get_fill_model_metrics()` / `fill_model_metrics_to_prometheus_lines()`
- **Env vars:** `GRINDER_FILL_MODEL_ENABLED` (default off), `GRINDER_FILL_MODEL_DIR` (model directory path)
- **Metrics:** `grinder_ml_fill_prob_bps_last` (gauge, last computed fill probability 0..10000 bps), `grinder_ml_fill_model_loaded` (gauge, 0/1). No `symbol=` label (FORBIDDEN_METRIC_LABELS contract).
- **Shadow-only guarantee:** `prob_bps` is logged at DEBUG level and emitted as metrics. It is NEVER referenced in any SOR, execution, or decision path.
- **Fail-open:** Missing dir, SHA256 mismatch, bad JSON → model=None, metrics default to 0, no crash.
- **Remaining:** ~~SOR enforcement gate (PR-C5)~~ shipped (ADR-071), ~~evidence artifacts (PR-C4b)~~ shipped (ADR-071a).

## ADR-070 -- Consecutive loss guard v1 (Track C, PR-C3)

- **Date:** 2026-02-21
- **Status:** accepted
- **Context:** P2 backlog gap "Consecutive loss limit" in `docs/GAPS.md`. No implementation existed. The risk module has `DrawdownGuardV1` (equity-based) and `KillSwitch` (emergency latch), but nothing tracks consecutive losing roundtrips. A streak of losses may indicate regime shift, adverse selection, or execution issues — warrants automatic pause for investigation.
- **Decision:** New module `src/grinder/risk/consecutive_loss_guard.py` with:
  - `ConsecutiveLossGuard` class: pure-logic state machine, no I/O, no metrics emission.
  - `ConsecutiveLossConfig` (frozen dataclass): `enabled`, `threshold` (int >= 1), `action` (PAUSE/DEGRADED).
  - `ConsecutiveLossState` (frozen dataclass): `count`, `tripped`, `last_row_id`, `last_ts_ms`.
  - `update(outcome, *, row_id, ts_ms) -> bool`: returns True on first trip.
  - `reset()`: clears count and tripped.

- **Outcome classification:**

  | Outcome | Guard action | Rationale |
  |---------|-------------|-----------|
  | `"loss"` | count++ | Adverse event; streak extends |
  | `"win"` | count = 0, tripped = False | Streak broken; reset |
  | `"breakeven"` | count = 0, tripped = False | Not a loss; streak broken |
  | unknown | no-op | Defensive; don't crash on unexpected values |

- **FSM wiring (minimal for v1):**
  - Guard is pure logic; caller sets `operator_override="PAUSE"` when tripped.
  - Leverages existing FSM `OPERATOR_PAUSE` transition. No new FSM states or fields.
  - Safe-by-default: `enabled=False` means zero effect on trading.

- **Determinism:** Same sequence of `update()` calls → same state. No timestamps in logic (ts_ms is metadata only). No floats. Frozen dataclass state.

- **Out of scope (deferred to PR-C3b):**
  - Live pipeline wiring (RoundtripTracker → guard in reconcile loop).
  - Metrics emission: `grinder_risk_consecutive_losses` (gauge), `grinder_risk_consecutive_loss_trips_total` (counter).
  - Alert rules: `ConsecutiveLossTrip`, `ConsecutiveLossesHigh`.
  - Runbook and observability panel sections.
  - Persistent state across restarts (count resets on restart).
  - Per-symbol tracking (v1 tracks global streak only).
  - Integration with FillModelV0 predictions.

- **Consequences:** Risk module now has three guards: DrawdownGuardV1 (equity), KillSwitch (emergency), ConsecutiveLossGuard (loss streaks). This PR ships the library only (pure logic + tests). Wiring, metrics, alerts, and runbook are deferred to PR-C3b to avoid SSOT breach (alerts implying active protection when no wiring exists). `docs/GAPS.md` consecutive loss row updated PLANNED → PARTIAL.
- **Alternatives:** "Wire directly into FSM as new OrchestratorInputs field" — rejected for v1 to avoid modifying FSM contract + all callers. `operator_override="PAUSE"` achieves the same effect with zero FSM changes. "Auto-reset after cooldown" — rejected because loss streaks warrant human investigation before resuming.

- **Update (PR-C3b): Live pipeline wiring shipped.**
  - Integration point: `scripts/run_live_reconcile.py` only. Guard is wired into `_fetch_and_ingest_fills()` — after `ingest_fills()`, same Binance trades are fed through `ConsecutiveLossService` → `RoundtripTracker` → `ConsecutiveLossGuard`. Other entrypoints do NOT activate this guard.
  - Action: PAUSE-only. `GRINDER_OPERATOR_OVERRIDE` supports `{"PAUSE", "EMERGENCY"}` — DEGRADED has no real channel and is not faked. Future softer actions require a new FSM input.
  - Env vars: `GRINDER_CONSEC_LOSS_ENABLED` (bool, default False), `GRINDER_CONSEC_LOSS_THRESHOLD` (int, default 5, min 1), `GRINDER_CONSEC_LOSS_EVIDENCE` (bool, default False).
  - Metrics: `grinder_risk_consecutive_losses` (gauge), `grinder_risk_consecutive_loss_trips_total` (counter). Dedicated `set_consecutive_loss_metrics()` setter in `metrics_builder.py` (not RiskMetricsState).
  - Alerts: `ConsecutiveLossTrip` (critical, on trip), `ConsecutiveLossesHigh` (warning, early-warning at >=3).
  - Dedup: service maintains own `_last_trade_id` cursor; input sorted by trade ID before processing.
  - Remaining out of scope: per-symbol tracking (v1 tracks global streak), persistent state across restarts (count resets), FillModelV0 integration.

- **Update (PR-C3c): Per-symbol guards + persistent state shipped.**
  - Per-symbol independent streaks: `dict[str, ConsecutiveLossGuard]` keyed by symbol. Guards are lazily created on first trade for each symbol. One symbol tripping doesn't reset others.
  - **Metrics are aggregates only (no per-symbol labels in Prometheus).** `grinder_risk_consecutive_losses` = max(count) across all symbol guards. `grinder_risk_consecutive_loss_trips_total` = sum of all trips. `FORBIDDEN_METRIC_LABELS` includes `symbol=`. **Per-symbol detail is available only in evidence artifacts and structured logs** -- not in Prometheus.
  - Persistent state: JSON + sha256 sidecar via `GRINDER_CONSEC_LOSS_STATE_PATH`. State file format `consecutive_loss_state_v1` with `PersistedServiceState` envelope. Atomic write (tmp + replace). Monotonicity guard rejects backward cursor writes.
  - `ConsecutiveLossState.from_dict()`: strict validation (isinstance checks, no coercion). Rejects negative count, string tripped, invalid types with ValueError.
  - `ConsecutiveLossGuard.from_state()`: factory to restore guard from persisted state.
  - RoundtripTracker is NOT persisted (too complex — `_OpenPosition` with Decimal accumulators per (symbol, direction) pair). On restart, tracker rebuilds from new trades. In-flight roundtrips at restart time are lost. Explicit `CONSEC_LOSS_TRACKER_NOT_RESTORED` warning emitted on state load.
  - Remaining out of scope: DEGRADED action (needs FSM input channel), FillModelV0 integration, RoundtripTracker persistence.

- **Update (PR-C3d): RoundtripTracker persistence shipped.**
  - `RoundtripTracker.to_state_dict()` / `from_state_dict()`: serialize/deserialize open positions. Decimals stored as strings (no float precision loss). Position keys as `"symbol|direction"` for JSON compatibility. Strict validation in `from_state_dict()` (isinstance checks, no coercion).
  - State file format upgraded: `consecutive_loss_state_v1` -> `consecutive_loss_state_v2`. New `tracker` field in `PersistedServiceState` envelope.
  - Backward compatibility: v1 files accepted by `from_dict()` with `tracker=None`. Service logs `CONSEC_LOSS_TRACKER_NOT_IN_STATE` (info) and starts with fresh tracker. v2 files restore tracker open positions.
  - Restart recovery: entry fill before restart + exit fill after restart = closed roundtrip. Guard is updated. Partial exits also survive restart.
  - Tracker restore failure: if `from_state_dict()` raises ValueError (corrupt tracker data), service logs `CONSEC_LOSS_TRACKER_RESTORE_FAILED` and continues with fresh tracker. Guards and dedup cursor are still restored.
  - Remaining out of scope: DEGRADED action (needs FSM input channel), FillModelV0 integration.

## ADR-071 -- Fill probability SOR enforcement gate (Track C, PR-C5)

- **Date:** 2026-02-22
- **Status:** accepted
- **Context:** PR-C4a shipped shadow metrics for FillModelV0 (predict + observe, no decision impact). Next step: use predictions to gate risk-increasing orders at SOR level. Low fill probability orders waste budget and increase adverse selection exposure.
- **Decision:** New module `src/grinder/execution/fill_prob_gate.py` with pure function `check_fill_prob()`:
  - **Inputs:** model (or None), features, threshold_bps, enforce flag.
  - **Outputs:** `FillProbResult(verdict, prob_bps, threshold_bps, enforce)`.
  - **Verdicts:**
    - `ALLOW`: model=None (fail-open) OR enforce=True + prob >= threshold.
    - `BLOCK`: enforce=True + prob < threshold.
    - `SHADOW`: enforce=False (prediction computed, never blocks).
  - **Integration:** Gate 8 in `LiveEngineV0._process_action()`, after FSM gate (Gate 7), before SOR routing. Only applies to PLACE/REPLACE actions. CANCEL/NOOP bypass entirely.
  - **Env vars:** `GRINDER_FILL_MODEL_ENFORCE` (bool, default false), `GRINDER_FILL_PROB_MIN_BPS` (int, default 2500, range 0..10000). Read once at engine init.
  - **Fail-open:** model=None → gate skipped entirely (no overhead, no log spam).
  - **Default OFF:** `GRINDER_FILL_MODEL_ENFORCE=0` means shadow mode (SHADOW verdict, never blocks).
- **Metrics:** `grinder_router_fill_prob_blocks_total` (counter), `grinder_router_fill_prob_enforce_enabled` (gauge 0/1). In `SorMetrics` (same singleton as SOR decision metrics). No `symbol=` label (FORBIDDEN_METRIC_LABELS contract).
- **Block reason:** `BlockReason.FILL_PROB_LOW` in `LiveEngineV0`.
- **Consequences:** Orders with fill probability below 25% (2500 bps) can be blocked when enforcement is enabled. Shadow mode allows operators to observe gate behavior before enabling. Model predictions are entry-side only (direction + notional bucket, conservative defaults: fill_count=1, holding_ms=0).
- **Alternatives:** "Gate at level filtering" — deferred to PR-C5b (more nuanced, per-level filtering). "Embed in SOR route() function" — rejected because route() is pure/stateless and shouldn't depend on ML model.

## ADR-071a -- Fill probability evidence artifacts (Track C, PR-C6)

- **Date:** 2026-02-22
- **Status:** accepted
- **Context:** PR-C5 added the fill probability gate (BLOCK/SHADOW/ALLOW). On BLOCK, operators need to diagnose *why* an order was blocked — features, model metadata, threshold vs actual probability. Without evidence, the only signal is a counter increment and a log line.
- **Decision:** New module `src/grinder/execution/fill_prob_evidence.py`:
  - **Evidence artifacts** (JSON + sha256 sidecar) written on BLOCK and SHADOW events.
  - **Env-gated** via `GRINDER_FILL_PROB_EVIDENCE` (default OFF, safe-by-default).
  - **Atomic writes** (tmp + os.replace), same pattern as `fsm_evidence.py`.
  - **Deterministic format**: `sort_keys=True`, `indent=2`, trailing newline.
  - **Structured logging**: `FILL_PROB_EVIDENCE` log always emitted on BLOCK (not env-gated). On SHADOW, log emitted only when evidence env is ON.
  - **Payload contract** (`fill_prob_evidence_v1`): verdict, prob_bps, threshold_bps, enforce, features (direction, notional_bucket, entry_fill_count, holding_ms_bucket), action metadata (symbol, side, price, qty, action_type), model metadata (n_bins, n_train_rows, global_prior_bps — null if model unavailable).
  - **File naming**: `{ts_ms}_{verdict}_{symbol}.json` in `{GRINDER_ARTIFACT_DIR}/fill_prob/`.
  - **Fail-open**: OSError on write → log warning, return None, no crash.
  - **No new metrics**: evidence goes to artifacts + structured logs only.
- **Consequences:** Operators can diagnose blocked orders immediately. SHADOW artifacts enable offline analysis of near-miss events before enabling enforcement. Zero runtime cost when evidence is disabled (default).

## ADR-072 -- FillModelV0 threshold calibration & offline evaluation (Track C, PR-C7)

- **Date:** 2026-02-22
- **Status:** accepted
- **Context:** PR-C5 shipped fill probability gate with a hardcoded default threshold of 2500 bps (25%). This value was chosen conservatively but has no empirical basis. Before enabling enforcement (`GRINDER_FILL_MODEL_ENFORCE=1`), operators need evidence-based threshold selection with quantified trade-offs (false positives = good orders blocked, false negatives = bad orders allowed).
- **Decision:** New evaluation library `src/grinder/ml/fill_model_eval.py` + CLI script `scripts/eval_fill_model_v0.py`:
  - **Input:** fill_outcomes_v1 dataset (same as training), trained model artifact.
  - **Evaluation:** For each roundtrip, predict fill probability using entry-side features. Compare prediction against actual outcome (win/loss/breakeven). Label: outcome == "win" → positive, else → negative (breakeven is negative).
  - **Threshold sweep:** Evaluate every threshold from 0 to 10000 bps in steps of 100 bps. At each threshold: ALLOW if prob_bps >= threshold, BLOCK if prob_bps < threshold. Confusion matrix: TP (wins allowed), FP (non-wins allowed), TN (non-wins blocked), FN (wins blocked). Precision, recall, F1, block rate computed at each point.
  - **Cost-weighted score formula:** `cost_score = TP + cost_ratio * TN`. Higher is better. Each TP (win correctly allowed) contributes 1. Each TN (loss correctly blocked) contributes cost_ratio. Default cost_ratio=2.0 means "blocking a loss is 2× as valuable as allowing a win". Recommended threshold = threshold with highest cost_score. Tie-break: lowest threshold (most conservative).
  - **Calibration check:** Per observed bin (not per model bin): `actual_bps = (wins * 10000 + total // 2) // total` (same integer formula as training). `predicted_bps = model.bins[key]` (or global_prior for unseen). `error_bps = abs(predicted - actual)`. Empty bins (0 samples in evaluation data) skipped. `calibration_well_calibrated = (max_error < 500)`.
  - **Output:** Deterministic JSON report artifact (`eval_report.json` + `manifest.json` with sha256). Report includes: threshold sweep table, recommended threshold, calibration diagnostics, dataset/model metadata, provenance sha256s.
  - **Human-readable summary:** Printed to stdout — recommended threshold, block rate at that threshold, precision/recall, calibration status. Machine-parseable exit code (0 = success).
  - **Pure offline:** No runtime dependencies, no env vars, no side effects beyond writing the report artifact. Uses same dataset loader pattern as `train_fill_model_v0.py`.
  - **No new library deps:** Pure Python (integer arithmetic for confusion matrix, float only for precision/recall/F1/cost_score display). pyarrow required (already in `[dev]` and `[ml]` extras).
- **Report schema** (`fill_model_eval_v0`):
  - `schema_version: "fill_model_eval_v0"`
  - `dataset_path: str`, `model_path: str` — paths used
  - `dataset_manifest_sha256: str`, `model_manifest_sha256: str` — provenance
  - `n_rows: int`, `n_wins: int`, `n_losses: int`, `n_breakeven: int`
  - `global_prior_bps: int` — model's global prior
  - `cost_ratio: float`, `sweep_step_bps: int` — parameters used
  - `threshold_sweep: list[{threshold_bps, tp, fp, tn, fn, precision_pct, recall_pct, f1_pct, block_rate_pct, cost_score}]`
  - `recommended_threshold_bps: int` — threshold with best cost_score (tie-break: lowest)
  - `calibration: list[{bin_key, predicted_bps, actual_bps, n_samples, error_bps}]` — sorted lexicographic by bin_key
  - `calibration_max_error_bps: int`, `calibration_well_calibrated: bool`
- **Consequences:** Operators get an evidence-based threshold recommendation before enabling enforcement. The report artifact is auditable (sha256 sidecar). Calibration check reveals if the model needs retraining. Cost ratio is tunable for different risk appetites.
- **Alternatives:** "Use cross-validation" — deferred (dataset too small for k-fold, in-sample eval is appropriate for v0 calibrated bin-count model where predictions are historical rates). "Integrate into training script" — rejected (eval should be independently runnable on any model+dataset pair, not coupled to training).

## ADR-073 -- Controlled rollout for fill probability enforcement (Track C, PR-C8)

- **Date:** 2026-02-22
- **Status:** accepted
- **Context:** PR-C5 shipped the fill probability gate (default OFF). PR-C6 shipped evidence artifacts. PR-C7 shipped offline evaluation with threshold calibration. Operators now need a safe, controlled path to flip enforcement ON in production. Key risks: (1) over-blocking kills revenue (false negatives = good orders blocked), (2) model staleness or miscalibration causes wrong blocks, (3) no automatic safety net if block rate spikes unexpectedly.
- **Decision:** Three deliverables for safe rollout:
  - **1. Block rate circuit breaker** in `src/grinder/execution/fill_prob_gate.py`:
    - New `FillProbCircuitBreaker` class: tracks block count and total decisions in a rolling window.
    - Config via env vars: `GRINDER_FILL_PROB_CB_WINDOW_SECONDS` (default 300 = 5 min), `GRINDER_FILL_PROB_CB_MAX_BLOCK_RATE_PCT` (default 50 = 50%).
    - When block rate in the window exceeds max, circuit breaker trips: all subsequent checks return ALLOW (bypass) until window rolls over and rate drops below threshold.
    - On trip: emits structured log `FILL_PROB_CIRCUIT_BREAKER_TRIPPED` with block_count, total_count, block_rate_pct, window_seconds. Counter metric `grinder_router_fill_prob_cb_trips_total`.
    - **Safe-by-default**: circuit breaker only activates when enforcement is ON. In shadow mode, no effect.
    - **Fail-open**: any error in circuit breaker logic → ALLOW.
    - **No env var needed to enable**: circuit breaker is always active when enforce=True. Operators tune max_block_rate_pct (set to 100 to effectively disable).
  - **2. Pre-flight check script** `scripts/preflight_fill_prob.py`:
    - Validates all prerequisites before operator flips `GRINDER_FILL_MODEL_ENFORCE=1`.
    - Checks: (a) model directory exists and loads successfully, (b) eval report exists, (c) calibration is well-calibrated (max_error < 500 bps), (d) recommended threshold from eval matches configured `GRINDER_FILL_PROB_MIN_BPS` (or warns on mismatch), (e) shadow mode has been running (evidence artifacts exist in artifact dir).
    - Exit 0 = all checks pass, safe to enable. Exit 1 = at least one check failed.
    - Human-readable output with PASS/FAIL per check.
    - Pure offline — reads files only, no live connection required.
  - **3. Rollout runbook** `docs/runbooks/05_FILL_PROB_ROLLOUT.md`:
    - Step-by-step operator guide for enabling enforcement.
    - Pre-flight: run preflight script. Review eval report. Confirm threshold.
    - Enablement: set env vars, restart service, verify metrics.
    - Monitoring: watch `grinder_router_fill_prob_blocks_total` rate, `grinder_router_fill_prob_cb_trips_total`, evidence artifacts.
    - Rollback: set `GRINDER_FILL_MODEL_ENFORCE=0`, restart.
    - Circuit breaker tuning: adjust `GRINDER_FILL_PROB_CB_MAX_BLOCK_RATE_PCT`.
- **Integration:** Circuit breaker wired into `LiveEngineV0._check_fill_prob()` — called before `check_fill_prob()`. If tripped, short-circuits to ALLOW without calling the gate.
- **Metrics:** `grinder_router_fill_prob_cb_trips_total` (counter) in SorMetrics. No new gauge (cb state is transient, not worth a gauge — structured log + counter sufficient).
- **Alert rule:** `FillProbCircuitBreakerTripped` — fires if `grinder_router_fill_prob_cb_trips_total` increases by > 0 in 5 minutes. Severity: warning. Links to rollout runbook.
- **Consequences:** Operators get automatic protection against runaway blocking. Pre-flight script prevents premature enablement. Runbook provides repeatable ceremony. Circuit breaker is zero-overhead in shadow mode.
- **Alternatives:** "Percentage-based canary (enforce on N% of decisions)" — rejected (adds non-determinism, harder to reason about, complicates evidence analysis). "Symbol-based canary" — operators can already achieve this by running separate instances per symbol with different env vars. "Automatic threshold adjustment" — deferred to future PR (requires online calibration loop, too complex for v0 rollout).

## ADR-074: Auto-threshold from eval report (PR-C9) — accepted

- **Status:** accepted
- **Context:** Before PR-C9, operators had to manually copy `recommended_threshold_bps` from the eval report into `GRINDER_FILL_PROB_MIN_BPS`. This is error-prone and creates a risk of stale thresholds (model retrained, eval re-run, but env var not updated). ADR-073 deferred "Automatic threshold adjustment" as too complex — but the offline variant (read from eval report at startup) is straightforward and safe.
- **Decision:** Add `src/grinder/ml/threshold_resolver.py` — a pure-offline module that reads `eval_report.json`, validates sha256/schema/model-provenance, and extracts `recommended_threshold_bps`. Two modes:
  - **Recommend-only** (default): resolves threshold, emits structured log + gauge metric, but does NOT override `GRINDER_FILL_PROB_MIN_BPS`. Operator sees mismatch in logs/metrics.
  - **Auto-apply** (opt-in via `GRINDER_FILL_PROB_AUTO_THRESHOLD=1`): resolves threshold AND overrides `_fill_prob_min_bps` in engine. Only if eval validations pass.
- **Env vars:**
  - `GRINDER_FILL_PROB_EVAL_DIR` (str, default unset): path to eval report directory. If unset → zero eval reads, no threshold resolution, no metric change. This is the kill switch for the feature.
  - `GRINDER_FILL_PROB_AUTO_THRESHOLD` (bool, default `0`): if `1` and eval resolution succeeds → override configured threshold. If `0` → recommend-only mode (log only).
- **3-layer validation:**
  1. SHA256 integrity of `eval_report.json` against `manifest.json` in eval dir.
  2. `schema_version` must equal `fill_model_eval_v0`.
  3. Model provenance: `model_manifest_sha256` in eval report must match SHA256 of current `model_dir/manifest.json`. Prevents using eval from a different model version.
- **Fail-open:** Any validation failure → `resolve_threshold()` returns `None` → engine uses configured `GRINDER_FILL_PROB_MIN_BPS` → structured log `THRESHOLD_RESOLVE_FAILED reason=<reason>`. Never crashes, never uses unvalidated data.
- **Startup log:** `THRESHOLD_RESOLVED mode=<mode> recommended_bps=<N> configured_bps=<M> effective_bps=<E> reason=<reason>`. Operator always sees which threshold is actually active.
- **Metric:** `grinder_router_fill_prob_auto_threshold_bps` (gauge, default 0). Shows resolved threshold (0 = disabled/failed). Operator can alert on mismatch vs configured.
- **Evidence artifact:** `threshold_resolution_{ts}.json + .sha256` in `{GRINDER_ARTIFACT_DIR}/fill_prob/`. Gated on `GRINDER_ARTIFACT_DIR` being set (NOT `GRINDER_FILL_PROB_EVIDENCE` — this is a boot-time config artifact, not a runtime event). Contains: mode, recommended_bps, configured_bps, effective_bps, eval_sha256, model_manifest_sha256.
- **Preflight integration:** `scripts/preflight_fill_prob.py` gains `--auto-threshold` flag. When passed, runs `resolve_threshold()` against the provided model + eval dirs. PASS = resolution succeeds, FAIL = resolution fails.
- **Consequences:** Operators can safely auto-apply thresholds from eval reports with full provenance tracking. Default mode (recommend-only) is zero-risk — only logs. Auto-apply requires explicit opt-in and passes all 3 validation layers. Preflight catches mismatches before deployment.
- **Alternatives:** "Hot-reload eval report at runtime" — rejected (adds complexity, non-deterministic behavior mid-session). "Single env var combining eval_dir + auto-apply" — rejected (violates separation of concerns, harder to audit). "Gate evidence on GRINDER_FILL_PROB_EVIDENCE" — rejected (C6 evidence is runtime BLOCK/SHADOW events, threshold resolution is boot-time config — different lifecycle).

### ADR-074a: Eval report freshness check (PR-B1) — accepted

- **Status:** accepted (addendum to ADR-074)
- **Context:** Stale eval reports (e.g., from a model retrain weeks ago) could silently recommend outdated thresholds. Operator needs a way to catch this. Additionally, `resolve_threshold()` returned bare `None` on failure — callers couldn't distinguish between different failure reasons for triage.
- **Decision:**
  - Add `resolve_threshold_result()` returning `ResolveResult(resolution, reason_code, detail)` with stable reason codes: `ok`, `missing_field`, `bad_type`, `out_of_range`, `sha256_mismatch`, `schema_unsupported`, `model_provenance_mismatch`, `timestamp_future`, `timestamp_too_old`, `parse_error`, `env_invalid`.
  - Old `resolve_threshold()` preserved as backward-compatible thin wrapper (returns `.resolution`).
  - Resolver is a **pure validation function** — no logging. Callers (engine, preflight) own all logging via unified events `FILL_PROB_THRESHOLD_RESOLUTION_FAILED` / `FILL_PROB_THRESHOLD_RESOLUTION_OK`.
  - Opt-in freshness gate via `GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS` (**behavioral control**):
    - Unset = disabled (safe default). No freshness checks.
    - When enabled, checks `ts_ms`, `created_at`, or `generated_at` in eval report.
    - No timestamp in report → skip (pass). Timestamp presence is not required.
    - Future timestamps (>5 min tolerance) → fail-open with `timestamp_future`.
    - Stale timestamps (> max_age) → fail-open with `timestamp_too_old`.
    - Bad env var value (non-numeric) → `env_invalid`, fail-open.
  - This is a behavioral control: it changes which eval reports the resolver accepts. Operators should validate in recommend-only mode before enabling.
- **Consequences:** Triage-friendly structured errors. Operators can set a staleness limit to catch forgotten eval reruns. All failures are fail-open (no crash, no threshold override).

### ADR-075: Fixture network airgap (PR-NETLOCK-1) — accepted

- **Status:** accepted
- **Context:** Fixture mode (`--fixture`) replaces WebSocket data with `FakeWsTransport` and typically uses `NoOpExchangePort`. But if `--exchange-port futures` is passed, `BinanceFuturesPort` could attempt real HTTP calls. Multiple env-var guards (`ALLOW_MAINNET_TRADE`, `GRINDER_REAL_PORT_ACK`, `SafeMode`) are independent of fixture mode — misconfiguration could leak real network traffic during what should be a hermetic run.
- **Decision:**
  - New module `src/grinder/net/fixture_guard.py` monkey-patches `socket.socket.connect`, `socket.socket.connect_ex`, and `socket.create_connection` when `--fixture` is active.
  - All connections to non-loopback addresses raise `FixtureNetworkBlockedError` (fail-fast).
  - Loopback allowed: `ip.is_loopback == True` (`127.0.0.0/8`, `::1`) + literal `"localhost"`. Permits metrics server and Redis HA.
  - Guard installed once at startup in `scripts/run_trading.py`, before HA elector, exchange port, or connector initialization.
  - Socket-level patch catches all Python network libraries (httpx, websockets, aiohttp, urllib, raw socket).
- **Consequences:** Fixture runs are provably air-gapped. Any code path attempting external network I/O during fixture replay crashes immediately with clear error message. Existing smokes unaffected (use `--fixture` + localhost only). Integration test runs `run_trading.py` as a subprocess and sets `PYTHONPATH` to include both `src/` (for `grinder.*`) and repo root (for `scripts.*`, since `run_trading.py` imports `scripts.http_measured_client`).
- **Alternatives:** "Library-level guard (patch httpx.Client)" — rejected (doesn't catch websockets, raw sockets, or future libraries). "DNS-level block" — rejected (OS-dependent, can't block IP-literal connections). "Warn-only mode" — rejected (silent leaks are worse than crashes).

## ADR-076 — Safety Envelope: Normative Gate Ordering Contract (TRD-1)
- **Date:** 2026-02-28
- **Status:** accepted
- **Context:** The engine gate chain in `LiveEngineV0._process_action()` has grown to 7 sequential safety gates (arming, mode, kill-switch, whitelist, drawdown, FSM, fill-prob). Gate ordering is safety-critical: fail-fast behavior ensures the cheapest/most-decisive check runs first, and operators rely on deterministic BlockReason codes for triage. However, the ordering was implicit in code — no normative specification or contract tests existed to prevent accidental reordering.
- **Decision:**
  - Declare gate ordering **normative** (not just implementation detail).
  - Gate chain: 1. armed → 2. mode → 3. kill-switch → 4. whitelist → 5. max-position → 6. drawdown → 7. FSM → 8. fill-prob → (SOR routing, not a safety gate) → execute.
  - Dry-run contract formula: **writes impossible unless** `armed=True AND mode=LIVE_TRADE AND exchange_port=futures`. Defaults: `armed=False`, `mode=READ_ONLY`, `exchange_port=noop`.
  - ConsecutiveLossGuard is documented as an **indirect** gate: CLG trips → sets `GRINDER_OPERATOR_OVERRIDE=PAUSE` → FSM transitions to PAUSE → Gate 7 blocks.
  - Contract tests (`tests/unit/test_safety_envelope.py`) freeze ordering via semantic behavior (observed BlockReason + spy guards proving later gates not called), not brittle line-number assertions.
  - SSOT document: `docs/20_SAFETY_ENVELOPE.md`.
- **Consequences:**
  - Adding a new gate requires updating: (1) engine code, (2) `docs/20_SAFETY_ENVELOPE.md` gate table, (3) contract tests, (4) this ADR. This friction is intentional — gate changes are safety-critical and must be conscious.
  - Reordering existing gates will break contract tests (correct behavior — ordering is normative).
  - Smoke test (`scripts/smoke_futures_no_orders.sh`) complements contract tests: it proves zero network I/O end-to-end with all gates open on fixture data. It does NOT prove that kill-switch/drawdown correctly block at runtime (that is the contract tests' job).
- **Rationale for ordering:** (1) Fail-fast: cheapest checks first (bool flag < model inference). (2) Determinism: same input always hits the same first blocking gate. (3) Operator expectations: `armed=False` is the master kill — it always wins.

## ADR-077 — Policy Interface Contract: GridPolicy + GridPlan Normative Lockdown (TRD-2)
- **Date:** 2026-02-28
- **Status:** accepted
- **Context:** `GridPolicy` ABC and `GridPlan` dataclass form the contract between policy evaluation and the execution engine. Two concrete policies (`StaticGridPolicy`, `AdaptiveGridPolicy`) depend on this contract. However, the field set, structural invariants, and protocol requirements were implicit in code — no normative specification or contract tests prevented accidental breakage (field addition/removal, type changes, invariant violations).
- **Decision:**
  - Declare `GridPlan` field set **normative**: exactly 11 fields (`mode`, `center_price`, `spacing_bps`, `levels_up`, `levels_down`, `size_schedule`, `skew_bps`, `regime`, `width_bps`, `reset_action`, `reason_codes`).
  - Declare structural invariants normative: `spacing_bps > 0`, `levels >= 0`, `center_price > 0`, non-empty `size_schedule` when grid has levels, non-negative sizes.
  - Declare determinism normative: same features → identical GridPlan (required by ADR-001 replay).
  - Contract tests (`tests/unit/test_policy_contract.py`) freeze the interface via field enumeration, type assertions, invariant checks, and determinism proofs.
  - SSOT document: `docs/22_POLICY_CONTRACT.md`.
- **Consequences:**
  - Adding or removing a `GridPlan` field is a **breaking change** to the policy-engine contract. It requires updating: (1) `base.py`, (2) `docs/22_POLICY_CONTRACT.md` field table, (3) contract tests (update `GRIDPLAN_EXPECTED_FIELDS` frozenset), (4) this ADR. This friction is intentional — field changes must be conscious and reviewed.
  - Contract tests do NOT verify adaptive math correctness or regime classification — those are covered by implementation-specific tests. The contract tests verify the **interface**, not the **behavior**.
  - `AdaptiveGridPolicy.evaluate()` extends the base signature with extra kwargs (`kill_switch_active`, `toxicity_result`, `l2_features`, `dd_budget_ratio`). These are NOT part of the ABC contract — they are adaptive-specific extension points.

## ADR-078 — NATR(14) Volatility Feature Contract: natr_bps as SSOT (TRD-3a)
- **Date:** 2026-02-28
- **Status:** accepted
- **Context:** `compute_natr_bps()` in `src/grinder/features/indicators.py` computes Normalized ATR(14) as integer basis points (x10000 encoding). The result is stored in `FeatureSnapshot.natr_bps` and consumed by `AdaptiveGridPolicy` for step/width calculations. However, the encoding, invariants, and warmup behavior were implicit in code — no normative specification or contract tests existed. A proposal to add a second NATR field (`natr_14_x1000`, x1000 encoding) was considered but rejected to avoid drift between redundant representations of the same metric.
- **Decision:**
  - Declare `natr_bps` (x10000 integer bps) as the **single source of truth** for NATR volatility in the feature pipeline.
  - Encoding formula: `int((ATR(14) / close * 10000).quantize(Decimal("1")))`.
  - No dual encoding: consumers needing different scales (x1000, float ratio) must derive from `natr_bps` at the call site.
  - Structural invariants: non-negative, 0 during warmup (< period+1 bars), 0 on zero close.
  - Determinism: same bars → identical int result (Decimal arithmetic, no float).
  - Contract tests (`tests/unit/test_natr_contract.py`) freeze the encoding, invariants, warmup boundary, and determinism.
  - SSOT document: `docs/23_NATR_CONTRACT.md`.
- **Consequences:**
  - Changing the encoding (e.g., from x10000 to x1000) is a **breaking change**. It requires updating: (1) `indicators.py`, (2) `docs/23_NATR_CONTRACT.md`, (3) contract tests (golden fixture value), (4) all consumers (AdaptiveGridPolicy step/width math), (5) this ADR.
  - Adding a second NATR field to `FeatureSnapshot` requires justification via a new ADR — the default position is one field per metric.
  - Contract tests do NOT verify ATR algorithm correctness (SMA vs EMA) — that is covered by `tests/unit/test_indicators.py::TestATR`.
- **Alternatives rejected:** Adding `natr_14_x1000` (x1000 encoding) alongside `natr_bps` — rejected because two fields representing the same metric with different scales creates drift risk and consumer confusion.

## ADR-082: ExecutionAction + ExchangePort contract extension (PR-INV-3)

- **Status:** Accepted
- **Context:** TP orders need `reduce_only` flag (Binance semantics to prevent position increase) and pre-generated `clientOrderId` (grinder_tp_ namespace for cycle layer identification). `ExecutionAction` had neither field, and `ExchangePort.place_order()` did not accept `reduce_only` or `client_order_id` override.
- **Decision:**
  - `ExecutionAction`: add `reduce_only: bool = False`, `client_order_id: str | None = None`
  - `ExchangePort.place_order()`: add `reduce_only: bool = False`, `client_order_id: str | None = None`
  - All 5 implementations updated: Protocol, NoOp, BinanceFutures, Binance, Idempotent
  - `IdempotentExchangePort` includes both `reduce_only` + `client_order_id` in idempotency key to prevent TP order collapse
  - TP identification: `is_tp_order()` helper in `reconcile/identity.py` checks `strategy_id == "tp"` (NOT `reduce_only` flag)
- **Backward compatibility:** New fields have defaults; old serialized `ExecutionAction`s deserialize correctly via `.get()` with defaults.
- **Consequences:**
  - Replay digests changed (new fields in `to_dict()` affect hash): `sample_day_allowed` = `887812017a697c8a`, `sample_day_toxic` = `97c8b0d37cae3ce3`, `sample_day_multisymbol` = `b605159aac9c0aac`. `sample_day` unchanged (0 fills).
- **SSOT:** `src/grinder/execution/types.py` (ExecutionAction), `src/grinder/execution/port.py` (Protocol)

## ADR-083: TP_CLOSE + TP_SLOT_TAKEOVER atomicity (PR-P0-TP-CLOSE-ATOMIC)

- **Date:** 2026-03-08
- **Status:** Accepted
- **Context:** TP_CLOSE PLACE and TP_SLOT_TAKEOVER CANCEL were independent actions. If PLACE failed (`-4118 ReduceOnly`), CANCEL still executed, shrinking grid without TP protection (observed in Run #17).
- **Decision:**
  1. Atomicity enforced in **engine** (not cycle_layer) via `correlation_id` field on `ExecutionAction`. Both PLACE and CANCEL share the same correlation_id (= TP client_order_id). Engine tracks success per correlation_id.
  2. Retry: only `-4118` is retryable (reconvergence after race-duplicate orders). Max 3 attempts after initial failure, 10s cooldown. Error code extracted via regex parser from the deterministic format `"Binance error {code}: {msg}"` (`binance_port.py:250`).
  3. After successful retry, no compensating CANCEL. Extra grid order may be reconciled by planner on unfreeze under existing planner behavior; not guaranteed by this PR. Follow-up live evidence required.
  4. Non-goal: no Prometheus counters (logs only). Counters deferred to observability PR.
- **Consequences:**
  - ExecutionAction schema gains `correlation_id` field - replay digests change (precedent: ADR-082 added `reduce_only` + `client_order_id`, same mechanism).
  - Missing correlation_id on new cycle_layer actions = generation bug (tested).
  - Engine atomicity pattern reusable for future paired action scenarios.
- **Alternatives rejected:**
  - Positional pairing (PLACE always before CANCEL): implicit contract, fragile on refactoring.
  - Per-symbol tracking: breaks on multi-fill same symbol.
  - Atomicity in cycle_layer: cycle_layer has no execution feedback.
  - Immediate retry (blocking sleep): `-4118` resolves after account sync (~30s), not after milliseconds.
- **SSOT:** `src/grinder/execution/types.py` (ExecutionAction.correlation_id), `src/grinder/live/engine.py` (atomicity guard + retry queue)

## ADR-084: Converge-first planner + budget-burn stop (PR-P0-RACE-1)

- **Date:** 2026-03-08
- **Status:** Accepted
- **Context:** Run #18 (main @ `d3e950d`): Grid shift churn burned 100-order budget in ~2min. `mismatch=10` triggered 20 actions (10 CANCEL + 10 PLACE) per shift. AccountSync REST lag (5s throttle) caused planner to see stale state and re-emit PLACEs, inflating open orders to count=16 (6 stale + 10 new). 10,561 `ORDER_BUDGET_EXHAUSTED` log lines, zero fills.
- **Convergence contract:**
  - Definition: `GridPlanResult.diff_extra == 0` after fresh AccountSync.
  - Identity: `_account_sync_generation` counter (monotonic int, incremented per successful sync). Inflight shift records sync_gen at dispatch.
  - Convergence check: sync_gen advanced AND extra==0 → converged.
  - Why extra-based (not order-ID tracking): planner already computes `diff_extra` via level matching. Reusing this avoids new identity infrastructure. Extra>0 reliably indicates stale orders because level matching is unique per side.
- **Decision:**
  1. Sync-gated planner: skip planner until AccountSync refreshes after dispatch.
  2. Cancel-first on extras: if `diff_extra > 0`, only CANCEL actions pass.
  3. Budget pre-check: if `orders_remaining() <= 0` OR PLACEs > budget, entire shift deferred.
  4. All guards scoped to planner path only. Cycle layer (TP/replenish) unaffected.
  5. No partial shift execution (asymmetric grid → more churn). Explicit non-goal.
- **Env:** `GRINDER_LIVE_CONVERGE_FIRST` (bool, default `True`, safe-by-default).
- **Fail-open:** `CONVERGE_FIRST=0` disables all guards. `orders_remaining()=None` disables budget guard. Timeout (30s) prevents permanent latch.
- **Known limitation:** timeout after 30s allows planner to proceed without proven convergence. Cancel-only filter still applies if extras present, so churn risk is bounded but not eliminated.
- **Consequences:**
  - `_plan_grid()` return type changed from `list[ExecutionAction]` to `GridPlanResult` (internal, no serialization, no digest impact).
  - `ExchangePort` protocol gains `orders_remaining() -> int | None` method.
  - Run #18 scenario: budget usage drops from 100+ (exhausted) to ~36 (stable grid).
- **SSOT:** `src/grinder/live/engine.py` (`_apply_convergence_guards`, `_InflightShift`), `src/grinder/execution/port.py` (`orders_remaining`)

## ADR-085: Rolling Infinite Grid (PR-ROLLING-INFINITE-GRID-SPEC)

- **Date:** 2026-03-09
- **Status:** Phase B implemented (planner + engine wiring complete, ready for live verification)
- **Context:** LiveGridPlannerV1 (doc-25) rebuilds the entire grid on every mid-price movement: 20 actions per shift for N=5. Grid fills don't advance the ladder -- they get swallowed by the next recenter. Run #18 burned 100-order budget in 2 minutes; PR-P0-RACE-1 mitigated budget burn but didn't solve the root cause (grid anchored to moving mid_price). Run #19 confirmed guards work (131 `GRID_SHIFT_DEFERRED` in 900s) but the planner still constantly wants to rebuild.
- **Decision:** Replace mid-anchored grid with rolling infinite ladder:
  1. Grid fills shift `effective_center` by `+/- step_price` (discrete, not continuous). `step_price` fixed per session.
  2. No mid-price tracking for level computation -- grid anchored to rolling state (`anchor_price + net_offset * step_price`).
  3. Fill produces 3 actions (1 CANCEL + 2 PLACE), not full rebuild (O(1) vs O(N)).
  4. TP fills don't shift ladder -- planner diff restores grid levels naturally.
  5. Price-based order matching (not level_id) -- prevents false mismatches after shift.
  6. Volatile state (in-memory), re-anchor on restart (exchange-truth reconciliation).
  7. Replenish mechanisms (PR-INV-4, TP_FILL_REPLENISH) become obsolete -- planner diff handles all level restoration.
- **Feature flag:** `GRINDER_LIVE_ROLLING_GRID` (bool, default `False`, safe-by-default).
- **Spec:** `docs/26_ROLLING_INFINITE_GRID_SPEC.md`
- **Migration:** spec PR -> Phase A (planner, this) -> Phase B (engine wiring) -> live verification -> cleanup.
- **Implementation Phase A** (PR-ROLLING-GRID-V1A):
  - `_RollingLadderState` dataclass in `grid_planner.py` (anchor_price, step_price, net_offset)
  - Additive level formula: `ec +/- i * step_price` (vs multiplicative in doc-25)
  - Price-based order matching: `_match_orders_by_price()` (replaces level_id matching in rolling mode)
  - `plan(rolling_mode=True)` new code path -- NOT called by engine yet (no env var, no behavioral change)
  - Phase A does NOT change runtime behavior. Engine does not call rolling mode.
- **Implementation Phase B** (PR-ROLLING-GRID-V1B):
  - `GRINDER_LIVE_ROLLING_GRID` env flag wired in `engine.py` `__init__`, gating rolling mode in `process_snapshot()`.
  - Rolling fill detection: engine-owned snapshot diff (`_prev_rolling_orders`, `_rolling_pending_cancels`) detects grid order disappearances before planner runs. Each fill calls `planner.apply_fill_offset()` to shift `effective_center`.
  - Fill classification: disappearance heuristic (not trade evidence check). Rolling fill = disappeared order with `strategy_id="d"` (grid) AND not in pending cancels. TP orders (`strategy_id="tp"`) and all non-grid strategies do NOT shift offset. **Limitation:** exchange-side non-trade cancels of grid orders (ADL, margin liquidation) treated as fills (rare, offset resets on restart).
  - Freeze disabled in rolling mode: planner's additive formula (`ec +/- i * step_price`) is bounded (no mid-driven rebuilds), replacing freeze as safety mechanism.
  - Replenish bypassed: both cycle-layer REPLENISH and TP_FILL_REPLENISH filtered in rolling mode. Planner diff is sole level restoration path.
  - Anti-churn (`_filter_grid_shift`) bypassed: no mid-driven shifts to suppress.
  - Pending cancel TTL: 30s, matching `cycle_layer._CANCEL_TTL_MS`. Both mechanisms agree on fill/cancel classification.
  - Convergence guards: unchanged, still apply in rolling mode.
  - 17 engine integration tests in `test_rolling_grid.py` (35 total with V1A planner tests).
- **Open questions:** adaptive spacing refresh, max offset threshold, cleanup of mid-anchored path.
- **SSOT:** `docs/26_ROLLING_INFINITE_GRID_SPEC.md`

## ADR-086: TP/Grid Slot Ownership — INV-9 (PR-INV-9)

- **Date:** 2026-03-13
- **Status:** accepted (updated 2026-03-14: INV-9b cross-tick fix)
- **Context:** Live rolling mode verification exposed a contract breach: after a BUY fill at 67768.30, both a TP SELL at 67836.00 and a grid SELL at 67836.20 coexisted on exchange. Root cause: one-tick race between planner (step 2 in tick pipeline) and cycle layer (step 3). Planner builds grid levels BEFORE cycle layer generates TP, so planner places a grid SELL at approximately the same price where cycle layer will place TP SELL. TP_SLOT_TAKEOVER removes the farthest SELL, but planner independently placed a new inner SELL into the TP zone on the same tick.
- **INV-9 (V1C) fixed fill-tick overlap only.** Cross-tick overlap exposed by live run4: TP SELL@70843.30 + grid SELL@70843.40 coexisted 45 sync cycles. Root cause: engine filtered TP orders from planner's `open_orders` (PR-INV-3 `not is_tp_order` filter), so planner could never see or match TPs to desired levels, placing grid orders at TP prices on subsequent ticks.
- **Decision (INV-9b, V1D):** Two-layer cross-tick slot protection:
  1. **Primary: TP inclusion in planner's open_orders.** Engine passes all grinder orders (including TP) to planner. Planner's `_match_orders_by_price()` matches TP to desired level by price → no grid PLACE at TP price. Planner skips CANCEL/REPLACE for TP orders (`is_tp_order` check in `_generate_actions`); TP lifecycle managed by cycle layer.
  2. **Secondary: Persistent reservation with age-only clearance.** Reservation created in `apply_fill_offset()`, consumed in `_build_rolling_grid()`. Persists across ticks until `max_reservation_age` (default 50 plan() calls). Age-only clearance avoids multi-fill false-clear when existing TP from prior fill triggers premature clearing of new reservation (Contract 2a).
  3. **Reservation does not create TP.** It only withholds grid placement. TP generation is cycle layer's responsibility.
- **Invariant INV-9 (TP Slot Exclusion):** `desired_grid_count = (N - sell_reservation) + (N - buy_reservation)` on fill ticks. Steady tick: `2 * N`.
- **Slot state model:** `{grid, tp, reserved, vacant}`. Ownership applies only to occupied slots (`grid` or `tp`). `reserved` and `vacant` are transient non-ownership states.
- **Convergence target:** `steady_state_open_count_target = 2 * N` (grid + tp). This is a convergence target, NOT an absolute runtime invariant. Transient deviation within one reconciliation cycle is expected.
- **Saturation policy (fill_count > N same side):** Reservation clamps at `min(fill_count, N)` per side. Cycle layer generates one TP per fill regardless. Excess TPs self-heal via TP TTL. Known limitation, same category as ADR-085 exchange-side non-trade cancels.
- **Defense-in-depth:** `_filter_tp_grid_overlap()` in engine.py. Same-tick anomaly guard, expected fire count ZERO. Uses `reduce_only` as structural discriminator (TP=True, grid=False). Fail-open when planner config unavailable. Firing in production = bug evidence. Does NOT protect cross-tick overlap (that's handled by TP inclusion + persistent reservation).
- **Consequences:**
  - INV-5 updated: fill generates 1 CANCEL + 1 grid PLACE (inner SELL/BUY reserved for TP). Was 1 CANCEL + 2 PLACE.
  - T28 (applied_grid + applied_tp = 2*N) is a post-action application model invariant for unit tests only, NOT a live exchange guarantee.
  - Overlap guard suppression preserves cardinality (grid -1 + TP +1 = same total). Self-heals if TP fails.
  - TP orders included in planner matching (INV-9b, engine change). `grinder_tp_*` parses as `prefix="grinder_"`, matches `startswith("grinder")`. Previously excluded by engine filter (PR-INV-3); now included.
  - Planner skips CANCEL/REPLACE for TP orders in `_generate_actions()` (`is_tp_order` guard on extra_orders and mismatch_orders). TP lifecycle remains cycle-layer-owned.
  - Tie-break for slot matching within epsilon: closest `price_diff_bps` wins; on exact tie, first in exchange snapshot iteration order wins.
  - Reservation age-out (max_reservation_age) is defense for REST-lag gap. After age-out, slot returns to grid. If TP appears after age-out, it matches desired level via primary mechanism.
- **Alternatives considered:**
  - Unified allocator: adds cross-layer coupling, single point of failure.
  - Cycle layer owns all slots: contradicts planner-as-SSOT for grid levels.
  - Post-merge dedup only: doesn't prevent the planner from generating the conflicting PLACE in the first place.
  - TP-visibility reservation clearing: clears reservation when TP visible in open_orders. Rejected: false-clears in multi-fill case (existing TP from prior fill triggers clearing of new reservation for different slot, Contract 2a).
- **`diff_extra_tp` follow-up (INV-9b):** Live run6 exposed convergence deadlock: TP in extras → `diff_extra > 0` → Guard 2 blocked all PLACEs → 50-tick spin. Fix: `GridPlanResult.diff_extra_tp` counts TP orders in extras. Convergence guard uses `non_tp_extras = diff_extra - diff_extra_tp`. TP extras are intentional (not cancelled). T43 validates.
- **SSOT:** `docs/26_ROLLING_INFINITE_GRID_SPEC.md` section INV-9

## ADR-087: Convergence guard fixes — BUG-3 + BUG-4

- **Date:** 2026-03-14
- **Status:** accepted
- **Context:** Live run8 (commit 44ce8c2, INV-9b) exposed two operational bugs:
  - **BUG-3 (GRID_SHIFT_DEFERRED churn):** 8,116 GRID_SHIFT_DEFERRED entries in one run. Mechanism: PLACE → latch(gen=N) → sync → latch clears → planner SHIFT → new latch(gen=N+1) → repeat. Each latch cycle blocks ~100 WS ticks (~5s sync interval). Root cause: inflight latch re-created after every convergence clear, even for pure price-shift actions (1 CANCEL + 1 PLACE).
  - **BUG-4 (CANCEL_2011 spin):** 30 CANCEL_2011 errors in one run. Stale snapshot shows already-cancelled order → planner generates CANCEL → Binance returns -2011 → repeats every tick until next AccountSync refresh.
- **Decision:**
  - **BUG-3 fix (a): Log throttle.** `GRID_SHIFT_DEFERRED` logged at INFO once per latch cycle (was WARNING every WS tick). `_inflight_deferred_logged: set[str]` tracks which symbols have logged for current latch. Cleared on latch clear/timeout.
  - **BUG-3 fix (b): No re-latch for pure shifts.** After convergence clears latch, pure shifts (`cancel_count >= place_count AND place_count > 0`) skip re-latch. Net-new PLACEs (initial placement, fill recovery where `cancel_count < place_count`) still latch. `convergence_cleared` flag tracks whether latch was just cleared in current guard invocation.
  - **BUG-4 fix: Cancel-failed blacklist.** `_cancel_failed_ids: set[str]` stores order_ids where CANCEL returned failure. Action processing loop skips CANCELs for blacklisted order_ids (`LiveActionStatus.SKIPPED`, `BlockReason.CANCEL_ALREADY_FAILED`). Blacklist cleared on AccountSync refresh (`_account_sync_generation` increment) — fresh snapshot replaces stale state.
- **Consequences:**
  - GRID_SHIFT_DEFERRED log volume: O(100/latch) → O(1/latch). Run8 would have had ~80 entries instead of 8,116.
  - Pure shifts (mid-price drift) no longer cause cascading latch → defer → clear → relatch cycles.
  - CANCEL_2011 retries: O(N*ticks_until_sync) → O(1). First failure blacklists; subsequent skipped.
  - BUG-3 fix risk: brief stale-snapshot window (1 sync cycle) where planner may generate redundant PLACE for shifted slot. Self-heals next sync (extra detected → CANCEL).
  - BUG-4 fix risk: premature blacklist if CANCEL fails for transient reason (non -2011). Self-heals on sync refresh (blacklist cleared).

## ADR-088: Anchor / Initial Placement Contract — INV-10

- **Date:** 2026-03-14
- **Status:** accepted
- **Context:** Live rolling mode verification (run9-run13) exposed grid placement asymmetry after external cleanup. Root cause: initial placement contract not formalized. Two modes mixed — initial placement vs live rolling. When an operator externally cancels all orders, the grid rebuilds from a stale anchor (`mid_price` at first init), appearing asymmetric from the current market price. Rolling mode never re-anchors by design (fill-driven offsets only). No mechanism existed to detect "exchange truly empty" and re-init.
- **Decision:**
  - **SSOT for anchor:** `anchor_price = snapshot.mid_price = (bid_price + ask_price) / 2`. Raw `Decimal`, NOT tick-rounded. Stored in `_RollingLadderState.anchor_price`.
  - **`ANCHOR_INIT` log:** Emitted in `plan()` when `init_rolling_state()` is first called for a symbol. Format: `ANCHOR_INIT symbol={} anchor={} spacing_bps={}`.
  - **`reset_rolling_state(symbol)`:** Planner method clears planner-owned state (anchor, step, net_offset, tp_slot_reservations). Next `plan()` re-inits from fresh `mid_price`.
  - **ANCHOR_RESET (engine):** 5-condition contract — rolling_state_exists AND no_grinder_orders AND no_fresh_inflight_latch AND position_flat AND no_pending_cancels_for_symbol. Same-tick: reset before `_plan_grid()` in same `process_snapshot()` call.
  - **no_fresh_inflight_latch:** inflight blocks only while `account_sync_generation <= inflight.sync_gen`. Stale inflight (sync refreshed since dispatch) does not block reset — REST is the source of truth after sync refresh. Safety: Binance order matching < 1s, sync interval = 5s → REST after refresh reliably reflects exchange state.
  - **Two-layer cleanup:** Planner-owned (anchor, step, net_offset, reservations) + Engine-owned (`_prev_rolling_orders`, pending cancels for symbol, `_inflight_deferred_logged`, throttle keys).
  - **ANCHOR_RESET_BLOCKED throttle:** `_anchor_reset_blocked_logged: set[str]` keyed by `"{symbol}:{reason}"`. Reason-level throttle (not count-level). Cleared when state changes (orders reappear, reset succeeds, inflight active).
  - **PLANNER_ACTIONS_SUMMARY extension:** Rolling mode appends `mid={} ec={} anchor={}`. Non-rolling unchanged (`mid={}` only).
- **Consequences:**
  - External cleanup (operator cancels all orders, margin call, ADL) triggers automatic re-anchor to current market mid on same tick.
  - `ANCHOR_RESET_BLOCKED` with `reason=POSITION_OPEN` prevents re-anchor when position exists (operator must close position or TP must fill first).
  - `ANCHOR_RESET_BLOCKED` with `reason=POSITION_UNKNOWN` prevents re-anchor when AccountSync is unavailable (defensive — avoids re-anchor without position confirmation).
  - `ANCHOR_RESET_BLOCKED` with `reason=PENDING_CANCELS` prevents re-anchor when cancel confirmations are pending (transient — self-heals via 30s TTL).
  - Throttle prevents log spam: each BLOCKED reason logged once per episode, re-fires after state change.
  - External-cleanup recovery path: operator can force re-anchor by closing position + cancelling all orders. Engine detects empty+flat → ANCHOR_RESET → fresh grid.
  - Saturation edge case (fills > N same side): reservation clamps at `min(fill_count, N)`, excess TPs self-heal via TTL (unchanged from ADR-086).
  - 19 tests (T44-T62): anchor=raw_mid, symmetry, stability, same-tick reset, inflight guard, orders guard, fill+cleanup+reanchor, position_open blocked, prev_orders cleared, pending_cancels cleared, mixed-symbol isolation, throttle behavior, pending_cancels→expiry→reset, position_unknown, stale inflight clearance (T59), stale-vs-fresh boundary (T60/T61), initial placement safety (T62).
  - **INV-10 follow-up (stranded inflight fix):** Live verification exposed that `_apply_convergence_guards()` returned early on `actions=[]` without clearing stale `_inflight_shift` latch, making ANCHOR_RESET unreachable in steady state. Fix: (a) clear stale inflight on empty-actions path when `account_sync_generation > inflight.sync_gen`; (b) ANCHOR_RESET condition uses staleness check (`account_sync_generation <= inflight.sync_gen`) instead of presence check (`symbol not in _inflight_shift`). Part A safety invariant: `_apply_convergence_guards()` is only called from the live planner path (engine.py line 806), not the `budget_dead`/`grid_frozen` path — `actions=[]` genuinely means "planner found no diff", not suppression.

## ADR-089: Operational Hardening for Rolling Live Path

- **Context:** Live verification of INV-10 exposed operational gaps: (1) `run_trading.py` had no logging config — Python defaulted to WARNING, making all INFO/DEBUG engine logs invisible without an external wrapper; (2) key rolling-path state transitions had no explicit log events; (3) no canonical ceremony/runbook for rolling live verification.
- **Decision:**
  - **Native logging:** `run_trading.py` calls `logging.basicConfig(level=GRINDER_LOG_LEVEL, force=True)` early in `main()`, before engine/connector construction. `GRINDER_LOG_LEVEL` env var (already documented in DEV.md, .env.example, docker-compose.yml) is now wired. Default: `INFO`. Invalid value: falls back to `INFO` with print warning. Ownership: `run_trading.py` is the canonical process entrypoint and owns root logger setup; library modules use `getLogger(__name__)` and inherit. `force=True` overrides any `lastResort` handler from library imports.
  - **New log events (observability only, no behavioral changes):**
    - `ROLLING_STEADY_STATE symbol=X tick=N desired=D actual=A` — DEBUG, emitted in `_plan_grid()` when rolling mode produces 0 actions. Throttled: 1 per 100 zero-action ticks per symbol. Counter resets when actions resume.
    - `INFLIGHT_STALE_CLEARED symbol=X sync_gen=N inflight_gen=M` — INFO, emitted in `_apply_convergence_guards()` when stale inflight latch is cleared on empty-actions path.
    - `CANCEL_SKIP_ALREADY_FAILED symbol=X order_id=Y` — DEBUG, emitted in `process_snapshot()` when a CANCEL is skipped because the order_id is in `_cancel_failed_ids` blacklist.
    - **Coverage:** All three events are unit-test-proven (18 tests: positive + negative + boundary). They require the real live planner path (account sync, exchange orders, -2011 errors) and are not reachable in fixture mode. Intended for live forensics. `INFLIGHT_STALE_CLEARED` is additionally **live-proven** (minimum ceremony 2026-03-15, sync_gen=2 > inflight_gen=1).
  - **Cleanup contract:** `scripts/exchange_state.py` — canonical operator tool for pre-flight check, cleanup (cancel all + close position), and verify (assert 0 orders + flat, exit 1 if dirty). Documented in `docs/runbooks/34_ROLLING_LIVE_VERIFICATION.md`.
  - **Runbook:** `docs/runbooks/34_ROLLING_LIVE_VERIFICATION.md` — 6 sections: pre-flight (exchange_state check/verify), launch (canonical command), evidence grep pack, blocked states, cleanup (exchange_state cleanup), acceptance criteria.
- **Consequences:**
  - External logging wrapper (`/tmp/run_live_with_logging.py`) is no longer needed. `python3 -m scripts.run_trading` is the canonical launch path.
  - All INFO-level engine logs are visible by default. DEBUG logs require `GRINDER_LOG_LEVEL=DEBUG`.
  - No behavioral changes: new log events are purely observational. No new metrics. No policy changes.
  - No replay/determinism impact: log events do not affect engine state or action generation.
  - `force=True` in `logging.basicConfig()` is safe: `run_trading.py` is always the top-level process owner. Library modules use `getLogger(__name__)` and inherit. The only effect is that INFO (previously invisible under Python default WARNING) is now visible.
  - **Operator tool bugfix (5939b07):** `_build_port()` read-only path required `allow_mainnet=True` + `ALLOW_MAINNET_TRADE=1` env var to satisfy `BinanceFuturesPortConfig.__post_init__` validation, even for read-only commands. Fix: always `allow_mainnet=True`, `os.environ.setdefault("ALLOW_MAINNET_TRADE", "1")` for read-only path. `SafeMode.READ_ONLY` remains the actual write guard.
  - **Minimum live acceptance: PASSED** (2026-03-15). Proven: ANCHOR_INIT, placement (2x POST 200 OK), GRID_SHIFT_DEFERRED, INFLIGHT_STALE_CLEARED (live-proven), 0 errors, clean shutdown (300s/3073 ticks), cleanup→verify status=CLEAN. Full rolling-path acceptance (ANCHOR_RESET, fill→TP, ROLLING_FILL_OFFSET) remains open.

## ADR-090 — Inflight Fill Detection (P0 Incident Fix)
- **Date:** 2026-03-15
- **Status:** accepted
- **Context:** Live ceremony A2 (2026-03-15) exposed a P0 safety bug. The rolling grid engine placed 13 BUY orders in 60 seconds, 11 of which filled instantly, accumulating 0.022 BTC long (~$1,571 notional) with zero fills detected (`ROLLING_FILL_OFFSET=0`). Root cause: `_detect_grid_fills_for_rolling()` uses a disappearance heuristic — an order must appear in at least one REST snapshot to enter `_prev_rolling_orders`. An order that fills within one REST sync interval (~5s) is never tracked, so its disappearance is never detected. The inflight latch delays planner for one sync cycle, but once sync refreshes, planner sees `actual < desired` and places another BUY at the same price. Repeat every ~5s.
- **Decision:** Two-part fix:
  1. **Inflight CID fill tracking (root cause fix):** Track dispatched PLACE orders by `client_order_id` in `_inflight_placed_cids`. After sync refresh, check if CID survived in REST snapshot. Missing = inflight fill (emit `INFLIGHT_FILL_DETECTED`, shift offset). Surviving = hand off to normal disappearance heuristic. Uses `all_open_ids` (unfiltered set of order IDs) for CID survival check — inflight reconciliation is identity-based, not strategy-based.
  2. **Unreconciled placement hard cap (defense-in-depth):** Track PLACEs awaiting reconciliation in `_unreconciled_place_count[symbol][side]`. Incremented on dispatch, decremented on first post-dispatch sync (both survived + filled). Cap = `levels * 2`. When reached, suppress PLACEs on that side (`PLACEMENT_CAPPED`, WARNING). Side-selective — capping BUY does not block SELL.
- **Reconciliation contract:** An order is "unreconciled" from PLACE dispatch until first post-dispatch REST sync. Two outcomes (mutually exclusive, exhaustive): **Survived** (CID in REST → decrement counter) or **Inflight fill** (CID absent → decrement counter + emit fill). Invariant: every dispatched PLACE increments unreconciled once; first post-dispatch sync decrements exactly once, regardless of outcome.
- **CID identity contract:** `client_order_id` flows unchanged through: port generation → Binance `newClientOrderId` → REST `clientOrderId` → `OpenOrderSnap.order_id` → `all_open_ids` key. No transform path exists.
- **Why existing guards failed:**
  - Inflight latch: delays 1 sync cycle, does not verify fill.
  - Budget pre-check: `max_orders_per_run=50` is too high for safety bound.
  - Max position cap: env var not set → disabled.
  - Drawdown guard: PnL hadn't crossed threshold.
  - Disappearance heuristic: the root cause — structurally blind to sub-sync fills.
- **Cap value justification (`levels * 2`):** Initial placement dispatches `levels` orders per side. After reconciliation, all are either survived or filled → counter returns to 0. The cap fires only if `levels` consecutive PLACEs are dispatched with ZERO reconciliation — both disappearance heuristic and inflight CID tracking failed. With `levels=1` (incident config), cap=2 would have stopped the loop after 2 undetected fills instead of 11.
- **New state:** `_InflightPlacedOrder(symbol, side, sync_gen)` dataclass. `_inflight_placed_cids: dict[str, _InflightPlacedOrder]`. `_unreconciled_place_count: dict[str, dict[str, int]]`.
- **New log events:** `INFLIGHT_FILL_DETECTED` (INFO — operational path, not anomaly), `PLACEMENT_CAPPED` (WARNING — anomaly, fill detection itself is broken).
- **ANCHOR_RESET cleanup:** `_inflight_placed_cids` filtered by symbol + `_unreconciled_place_count` popped alongside existing state cleanup (6 state maps total).
- **Intentional coupling:** Guard 4 reads `planner._config.levels` — frozen `@dataclass`, single scalar, follows precedent at engine.py `_filter_tp_grid_overlap()`. Documented for future refactoring.
- **Consequences:**
  - Instant fills are now a supported operational path, not an undetectable gap.
  - Disappearance heuristic remains the primary fill detection path for orders that survive at least one REST snapshot.
  - Inflight CID tracking is the first-fill path for orders that fill within the sync interval.
  - 12 new tests (T63-T74): inflight BUY/SELL fill, multi-cycle no-churn, survived order, early-check, cap fires, side isolation, reconciliation decrements, ANCHOR_RESET cleanup, healthy init, healthy replacement.
  - Incident verdict: FAILED. No further fill ceremonies until this PR merges and is live-verified.
