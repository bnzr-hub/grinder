# GRINDER - Observability

> Metrics, logging, tracing, and alerting specifications

---

## 13.1 Observability Stack

```
┌─────────────────────────────────────────────────────────────────┐
│                    OBSERVABILITY STACK                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │   Metrics   │  │    Logs     │  │   Traces    │             │
│  │ (Prometheus)│  │ (Structured)│  │   (OTLP)    │             │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘             │
│         │                │                │                     │
│         ▼                ▼                ▼                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │   Grafana   │  │    Loki     │  │   Jaeger    │             │
│  │ Dashboards  │  │   (Store)   │  │   (Traces)  │             │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘             │
│         │                │                │                     │
│         └────────────────┴────────────────┘                     │
│                          │                                      │
│                          ▼                                      │
│                   ┌─────────────┐                               │
│                   │   Alerting  │                               │
│                   │ (PagerDuty) │                               │
│                   └─────────────┘                               │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 13.2 Metrics

### Metric Categories

| Category | Examples | Purpose |
|----------|----------|---------|
| Business | PnL, RT count, Sharpe | Trading performance |
| System | Latency, throughput, errors | System health |
| Data | Staleness, gaps, outliers | Data quality |
| Risk | DD, inventory, exposure | Risk monitoring |
| ML | Prediction accuracy, drift | Model health |

### Core Metrics

```python
from prometheus_client import Counter, Gauge, Histogram, Summary

# Business Metrics
pnl_total = Gauge(
    "grinder_pnl_total_usd",
    "Total P&L in USD",
    ["symbol", "policy"]
)

round_trips_total = Counter(
    "grinder_round_trips_total",
    "Total round-trips completed",
    ["symbol", "policy", "outcome"]  # outcome: win/loss
)

rt_pnl_bps = Histogram(
    "grinder_rt_pnl_bps",
    "Round-trip P&L in basis points",
    ["symbol", "policy"],
    buckets=[-50, -20, -10, -5, 0, 5, 10, 20, 50, 100]
)

# System Metrics
order_latency_ms = Histogram(
    "grinder_order_latency_ms",
    "Order placement latency in milliseconds",
    ["exchange", "operation"],  # operation: place/cancel/amend
    buckets=[10, 25, 50, 100, 250, 500, 1000, 2500]
)

decision_latency_ms = Histogram(
    "grinder_decision_latency_ms",
    "Policy decision latency in milliseconds",
    ["policy"],
    buckets=[1, 2, 5, 10, 25, 50, 100]
)

errors_total = Counter(
    "grinder_errors_total",
    "Total errors by type",
    ["error_type", "module"]
)

# Data Metrics
data_staleness_ms = Gauge(
    "grinder_data_staleness_ms",
    "Data staleness in milliseconds",
    ["stream", "symbol"]
)

data_gaps_total = Counter(
    "grinder_data_gaps_total",
    "Total data gaps detected",
    ["stream", "symbol"]
)

# Risk Metrics
drawdown_pct = Gauge(
    "grinder_drawdown_pct",
    "Current drawdown percentage",
    ["period"]  # session/daily/weekly
)

inventory_notional = Gauge(
    "grinder_inventory_notional_usd",
    "Inventory notional in USD",
    ["symbol", "side"]
)

toxicity_score = Gauge(
    "grinder_toxicity_score",
    "Current toxicity score",
    ["symbol"]
)
```


### Adaptive Controller Metrics (planned)

```python
regime_state = Gauge(
    "grinder_regime",
    "Current market regime (label holds the state)",
    ["symbol", "regime"]
)

grid_step_bps = Gauge(
    "grinder_grid_step_bps",
    "Current grid step in bps",
    ["symbol"]
)

grid_width_bps = Gauge(
    "grinder_grid_width_bps",
    "Current grid width in bps",
    ["symbol"]
)

reset_total = Counter(
    "grinder_reset_total",
    "Total grid resets",
    ["symbol", "type", "reason_code"]
)

gate_state = Gauge(
    "grinder_gate_state",
    "Gate state (0=OPEN,1=THROTTLE,2=PAUSE)",
    ["symbol", "state"]
)

reason_code_total = Counter(
    "grinder_reason_code_total",
    "Reason code emissions",
    ["code"]
)
```

### Custom Metrics

```python
class MetricsCollector:
    """Collect and export custom metrics."""

    def __init__(self):
        self.fill_rate = Gauge(
            "grinder_fill_rate",
            "Order fill rate",
            ["symbol", "side"]
        )
        self.spread_bps = Gauge(
            "grinder_spread_bps",
            "Current spread in bps",
            ["symbol"]
        )

    def record_fill(self, symbol: str, side: str,
                    filled: bool) -> None:
        """Record fill for rate calculation."""
        # Using moving average
        current = self.fill_rate.labels(symbol=symbol, side=side)._value.get()
        new_val = 0.95 * current + 0.05 * (1 if filled else 0)
        self.fill_rate.labels(symbol=symbol, side=side).set(new_val)

    def record_spread(self, symbol: str, spread_bps: float) -> None:
        """Record current spread."""
        self.spread_bps.labels(symbol=symbol).set(spread_bps)
```

### ML Inference Metrics (M8-02c-2, M8-02d)

ML ONNX inference metrics for SLO tracking and alerting.

```python
# ML Metrics (from grinder.ml.metrics)

# Gauge: Whether ML ACTIVE mode is enabled per snapshot
grinder_ml_active_on  # 0 or 1

# Counter: ACTIVE mode blocks by reason code
# Labels: reason (KILL_SWITCH_ENV, KILL_SWITCH_CONFIG, INFER_DISABLED,
#                 ACTIVE_DISABLED, BAD_ACK, ONNX_UNAVAILABLE,
#                 ARTIFACT_DIR_MISSING, MANIFEST_INVALID, MODEL_NOT_LOADED,
#                 ENV_NOT_ALLOWED)
grinder_ml_block_total{reason="..."}

# Counter: Successful inferences
grinder_ml_inference_total

# Counter: Inference errors (fail-closed mode)
grinder_ml_inference_errors_total

# Histogram: Inference latency in milliseconds (M8-02d)
# Labels: mode (shadow, active)
# Buckets: [1, 2, 5, 10, 25, 50, 100, 250, 500] ms
grinder_ml_inference_latency_ms{mode="..."}
```

**SLO Thresholds (M8-02d):**

| Metric | p95 Target | p99 Warning | p99.9 Critical |
|--------|------------|-------------|----------------|
| Inference latency | < 50ms | < 100ms | < 250ms |
| Error rate | - | < 5% | - |

**Alert Rules:**

- `MlInferenceLatencyHigh`: p99 > 100ms for 5m (warning)
- `MlInferenceLatencyCritical`: p99.9 > 250ms for 3m (critical)
- `MlInferenceErrorRateHigh`: error rate > 5% for 5m (warning)
- `MlActiveModePersistentlyBlocked`: ACTIVE blocked for 15m (info)
- `MlInferenceStalled`: no inferences for 10m (warning)

### Tuning Metrics (PR-B3b, ADR-126)

Symbol tuning outcome metrics. Shadow-only — no runtime gating. No `symbol=` label (FORBIDDEN_METRIC_LABELS, ADR-028). Per-symbol detail remains in structured logs (`SYMBOL_TUNED`, `SYMBOL_NO_GO`).

```python
# Counter: Tuning outcomes by status
# Labels: status (TUNED, NO_GO)
grinder_tuning_result_total{status="TUNED"}
grinder_tuning_result_total{status="NO_GO"}

# Counter: NO_GO outcomes by reason
# Labels: reason (PRICE_UNAVAILABLE, POSITION_EXCEEDS_CAP, NOTIONAL_TOO_LOW,
#                 TICK_SIZE_UNAVAILABLE, STEP_SIZE_UNAVAILABLE, BLACKLISTED)
grinder_tuning_no_go_total{reason="..."}

# Gauge: Non-expired entries in TuningCache (live on scrape)
grinder_tuning_cache_size

# Counter: Total TuningCache expirations (live on scrape)
grinder_tuning_cache_expired_total
```

**Operator interpretation:**

| Metric | What it tells the operator |
|--------|---------------------------|
| `grinder_tuning_result_total{status="TUNED"}` | How many symbols passed tuning at last startup |
| `grinder_tuning_result_total{status="NO_GO"}` | How many symbols failed tuning at last startup |
| `grinder_tuning_no_go_total{reason="..."}` | Why symbols failed — notional too low, price missing, cap exceeded, etc. |
| `grinder_tuning_cache_size` | How many tuning results are still cached (not yet expired) |
| `grinder_tuning_cache_expired_total` | How many cache entries have expired since startup |

---

## 13.3 Structured Logging

### Log Format

```python
import structlog

# Configure structlog
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()
```

### Log Events

```python
# Trading events
logger.info("order_placed",
    symbol="BTCUSDT",
    side="BUY",
    price=50000.0,
    quantity=0.1,
    order_id="abc123",
    policy="RANGE",
)

logger.info("fill_received",
    symbol="BTCUSDT",
    side="BUY",
    price=49999.5,
    quantity=0.1,
    order_id="abc123",
    is_maker=True,
    latency_ms=45,
)

logger.info("round_trip_completed",
    symbol="BTCUSDT",
    policy="RANGE",
    entry_price=49999.5,
    exit_price=50010.0,
    pnl_bps=2.1,
    hold_time_s=120,
)

# State changes
logger.info("state_transition",
    from_state="ACTIVE",
    to_state="THROTTLED",
    trigger="TOX_MID",
    toxicity_score=1.5,
)

# Risk events
logger.warning("risk_alert",
    alert_type="DRAWDOWN_WARNING",
    current_dd_pct=3.5,
    limit_dd_pct=5.0,
    action="THROTTLE",
)

logger.critical("emergency_exit",
    reason="DD_BREACH",
    dd_pct=5.2,
    positions={"BTCUSDT": 0.5, "ETHUSDT": -0.3},
)

# Errors
logger.error("order_failed",
    symbol="BTCUSDT",
    error="INSUFFICIENT_MARGIN",
    order_id="abc123",
    retry_count=2,
)
```

### Log Levels

| Level | Use Case | Examples |
|-------|----------|----------|
| DEBUG | Detailed flow | Feature values, calculations |
| INFO | Normal operations | Orders, fills, state changes |
| WARNING | Concerning but handled | High toxicity, rate limits |
| ERROR | Failures requiring attention | Order failures, data gaps |
| CRITICAL | Immediate action needed | Emergency exit, kill switch |

---

## 13.4 Distributed Tracing

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

# Setup
provider = TracerProvider()
processor = BatchSpanProcessor(OTLPSpanExporter())
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("grinder")

# Usage
class PolicyEngine:
    async def evaluate(self, symbol: str, features: dict) -> GridPlan:
        with tracer.start_as_current_span("policy_evaluate") as span:
            span.set_attribute("symbol", symbol)
            span.set_attribute("toxicity", features.get("tox_score", 0))

            # Compute features
            with tracer.start_as_current_span("compute_features"):
                enhanced_features = self._enhance_features(features)

            # Select policy
            with tracer.start_as_current_span("select_policy"):
                policy = self._select_policy(enhanced_features)
                span.set_attribute("policy", policy.name)

            # Generate plan
            with tracer.start_as_current_span("generate_plan"):
                plan = policy.evaluate(enhanced_features)

            span.set_attribute("grid_mode", plan.mode.value)
            return plan
```

---

## 13.5 Alerting

### Alert Definitions

```yaml
# alerts/grinder.yaml
groups:
  - name: grinder_critical
    rules:
      - alert: GrinderEmergencyExit
        expr: grinder_state == 6  # EMERGENCY
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "GRINDER emergency exit triggered"
          description: "System entered emergency state"

      - alert: GrinderHighDrawdown
        expr: grinder_drawdown_pct{period="daily"} > 0.08
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Daily drawdown > 8%"

  - name: grinder_warning
    rules:
      - alert: GrinderHighToxicity
        expr: grinder_toxicity_score > 2.5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High toxicity for {{ $labels.symbol }}"

      - alert: GrinderDataStale
        expr: grinder_data_staleness_ms > 5000
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "Stale data for {{ $labels.stream }}"

      - alert: GrinderHighLatency
        expr: histogram_quantile(0.99, grinder_order_latency_ms_bucket) > 500
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "P99 order latency > 500ms"

  - name: grinder_info
    rules:
      - alert: GrinderStateChange
        expr: changes(grinder_state[5m]) > 3
        for: 0m
        labels:
          severity: info
        annotations:
          summary: "Frequent state changes"
```

### Alert Routing

```python
class AlertManager:
    """Route alerts to appropriate channels."""

    def __init__(self, config: AlertConfig):
        self.config = config
        self.pagerduty = PagerDutyClient(config.pagerduty_key)
        self.slack = SlackClient(config.slack_webhook)

    async def send_alert(self, alert: Alert) -> None:
        """Send alert to appropriate channels."""

        if alert.severity == "critical":
            # PagerDuty for critical
            await self.pagerduty.trigger(
                summary=alert.message,
                source="grinder",
                severity="critical",
                custom_details=alert.context,
            )
            # Also Slack
            await self.slack.send(
                channel="#trading-alerts",
                text=f"🚨 CRITICAL: {alert.message}",
                attachments=[{"fields": alert.context}],
            )

        elif alert.severity == "warning":
            await self.slack.send(
                channel="#trading-alerts",
                text=f"⚠️ WARNING: {alert.message}",
            )

        else:
            await self.slack.send(
                channel="#trading-info",
                text=f"ℹ️ {alert.message}",
            )
```

---

## 13.6 Dashboards

### Main Dashboard Panels

| Panel | Metrics | Purpose |
|-------|---------|---------|
| P&L Chart | `pnl_total` | Track performance |
| Equity Curve | Calculated from P&L | Visualize growth |
| Drawdown | `drawdown_pct` | Risk monitoring |
| State Timeline | `state` | System status |
| Toxicity Heatmap | `toxicity_score` | Per-symbol toxicity |
| Fill Rate | `fill_rate` | Execution quality |
| Latency | `order_latency_ms` | System performance |
| Round Trips | `round_trips_total` | Activity level |

### Dashboard JSON (Grafana)

```json
{
  "dashboard": {
    "title": "GRINDER Main",
    "panels": [
      {
        "title": "Session P&L",
        "type": "stat",
        "targets": [
          {
            "expr": "sum(grinder_pnl_total_usd)",
            "legendFormat": "Total P&L"
          }
        ],
        "fieldConfig": {
          "defaults": {
            "unit": "currencyUSD",
            "thresholds": {
              "mode": "absolute",
              "steps": [
                {"color": "red", "value": -500},
                {"color": "yellow", "value": 0},
                {"color": "green", "value": 100}
              ]
            }
          }
        }
      },
      {
        "title": "Drawdown",
        "type": "gauge",
        "targets": [
          {
            "expr": "grinder_drawdown_pct{period='session'}",
            "legendFormat": "Session DD"
          }
        ],
        "fieldConfig": {
          "defaults": {
            "max": 10,
            "thresholds": {
              "steps": [
                {"color": "green", "value": 0},
                {"color": "yellow", "value": 3},
                {"color": "red", "value": 5}
              ]
            }
          }
        }
      },
      {
        "title": "System State",
        "type": "state-timeline",
        "targets": [
          {
            "expr": "grinder_state"
          }
        ]
      },
      {
        "title": "Order Latency P99",
        "type": "timeseries",
        "targets": [
          {
            "expr": "histogram_quantile(0.99, rate(grinder_order_latency_ms_bucket[5m]))"
          }
        ]
      }
    ]
  }
}
```

---

## 13.7 Health Checks

```python
from fastapi import FastAPI
from prometheus_client import generate_latest

app = FastAPI()

@app.get("/health")
async def health():
    """Kubernetes liveness probe."""
    return {"status": "healthy"}

@app.get("/ready")
async def ready():
    """Kubernetes readiness probe."""
    checks = {
        "exchange_connected": connector.is_connected(),
        "data_fresh": max(data_staleness.values()) < 5000,
        "risk_ok": not risk_manager.is_breached(),
    }
    all_ok = all(checks.values())
    return {"ready": all_ok, "checks": checks}

@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(
        generate_latest(),
        media_type="text/plain"
    )
```

---

## 13.8 Runbooks

### Runbook: High Toxicity

```markdown
## Alert: GrinderHighToxicity

### Symptoms
- Toxicity score > 2.5 for > 5 minutes
- Grid in THROTTLED or PAUSED state

### Diagnosis
1. Check which symbols are toxic: `grinder_toxicity_score > 2`
2. Check toxicity components in logs
3. Check market conditions (news, liquidations)

### Resolution
1. If isolated to 1-2 symbols: wait for decay
2. If widespread: consider manual PAUSE
3. If persistent: investigate data quality

### Escalation
- If > 30 minutes: notify trading lead
- If > 1 hour: consider manual intervention
```

### Runbook: Emergency Exit

```markdown
## Alert: GrinderEmergencyExit

### Immediate Actions
1. Check positions: `kubectl exec grinder -- grinder-cli positions`
2. Check if exit completed: all positions should be flat
3. Check P&L impact

### Diagnosis
1. Check trigger reason in logs
2. Check drawdown at time of trigger
3. Review recent trades for anomalies

### Recovery
1. Wait for manual review
2. Reset state: `kubectl exec grinder -- grinder-cli reset-emergency`
3. Gradual restart with reduced limits

### Post-Incident
- Create incident report
- Review risk limits
- Update if necessary
```

## Grid V2 Sync Reconciler Log Schema (ADR-103)

The `GRID_V2_SYNC_RECONCILER` log emits on every sync cycle with entry/exit diff:

```
GRID_V2_SYNC_RECONCILER symbol=BTCUSDT mode=FLAT
  theoretical_entries=4 effective_entries=2 actual_entries=1
  missing=1 extra=0
  desired_exits=1 actual_exits=1 missing_exits=0 extra_exits=0
  would_cancel=0 would_place=1 cycle_ms=2
  projection=RISK_CONSTRAINED_PARTIAL capacity=2 primary=True
```

| Field | Meaning |
|-------|---------|
| `theoretical_entries` | SM desired entries before risk projection |
| `effective_entries` | Legal desired entries after projection |
| `actual_entries` | Entries currently on exchange |
| `projection` | `UNCONSTRAINED`, `RISK_CONSTRAINED_PARTIAL`, `RISK_CONSTRAINED_ZERO` |
| `capacity` | Legal entry capacity from risk gate (`None` = unconstrained) |

## Grid V2 Exit Topology Repair Log Schema (ADR-105)

```
GRID_V2_EXIT_TOPOLOGY_REPAIR_START symbol=BTCUSDT trigger=SYNC_DRIFT
  desired=3 actual=4 extra=1 missing=0 deferred=0
GRID_V2_EXIT_TOPOLOGY_REPAIR_CANCEL symbol=BTCUSDT order_id=g-X-4
GRID_V2_EXIT_TOPOLOGY_REPAIR_CONVERGED symbol=BTCUSDT cancels=1 places=0 deferred=0
```

| Field | Meaning |
|-------|---------|
| `trigger` | `SYNC_DRIFT`, `REJECT_RECOVERY` (emitted); `BUDGET_OVERRUN`, `PARTIAL_FILL_RECOMPUTE` (future) |
| `desired` | Legal exit count (SM + budget constrained) |
| `actual` | EXIT CIDs on exchange |
| `extra` | On exchange but not desired (cancel) |
| `missing` | Desired but not on exchange (place) |
| `deferred` | Desired but not yet registered (log only) |

Additional outcome events:
- `GRID_V2_EXIT_TOPOLOGY_REPAIR_CONVERGED` — all repair actions succeeded, topology is legal.
- `GRID_V2_EXIT_TOPOLOGY_REPAIR_INCOMPLETE` — one or more actions failed or deferred; topology not yet converged.

## Reason Codes (ADR-106)

Stable reason codes for every major suppressed/degraded/no-op path.

### GRID_V2_NO_ACTION
Emitted when sync cycle produces zero repair/reconcile actions.
```
GRID_V2_NO_ACTION symbol=BTCUSDT reason=ACTUAL_MATCHES_EFFECTIVE_TARGET
  theoretical_entries=4 effective_entries=4 actual_entries=4 projection=UNCONSTRAINED
```

| Reason | Meaning |
|--------|---------|
| `ACTUAL_MATCHES_EFFECTIVE_TARGET` | Healthy steady-state: actual == effective |
| `RISK_SATURATED_TARGET_ZERO` | Risk-saturated: effective=0 while theoretical>0 |
| `EFFECTIVE_TARGET_PARTIAL_MATCHED` | Partial projection matched: actual == effective < theoretical |
| `AWAITING_SYNC` | Waiting for first account sync |
| `NOT_STARTED` | Grid V2 not yet started |
| `RECONSTRUCTION_PENDING` | SM reconstruction not yet complete |

### GRID_V2_ENTRY_SUPPRESSED
Emitted when projection reduces desired entries.
```
GRID_V2_ENTRY_SUPPRESSED symbol=BTCUSDT reason=EFFECTIVE_TARGET_ZERO
  theoretical=4 effective=0 projection=RISK_CONSTRAINED_ZERO capacity=0
```

| Reason | Meaning |
|--------|---------|
| `EFFECTIVE_TARGET_ZERO` | All entries suppressed (fully constrained) |
| `EFFECTIVE_TARGET_PARTIAL` | Some entries suppressed (partial capacity) |

### GRID_V2_EXIT_SUPPRESSED
Emitted when reduce-only exit is blocked.
```
GRID_V2_EXIT_SUPPRESSED symbol=BTCUSDT side=BUY reason=REDUCE_ONLY_BUDGET_EXCEEDED
GRID_V2_EXIT_SUPPRESSED symbol=BTCUSDT side=BUY reason=PENDING_REPAIR_AFTER_REJECT
```

### GRID_V2_HEALTH_BLOCK
Emitted when health gate blocks a write.
```
GRID_V2_HEALTH_BLOCK symbol=BTCUSDT reason=STALE_TRUTH action=PLACE
```

### Reason Families
| Family | Count | Emitted as | Module |
|--------|-------|------------|--------|
| NoActionReason | 6 | `GRID_V2_NO_ACTION reason=` | reason_codes.py |
| EntrySuppressionReason | 5 | `GRID_V2_ENTRY_SUPPRESSED reason=` | reason_codes.py |
| ExitSuppressionReason | 4 | `GRID_V2_EXIT_SUPPRESSED reason=` | reason_codes.py |
| HealthBlockReason | 4 | `GRID_V2_HEALTH_BLOCK reason=` | reason_codes.py |
| RepairStatusReason | 6 | Stable event names (not `reason=`) | reason_codes.py |

## EventLedger Trust Lifecycle (ADR-109 Phase 2)

Log signals for EventLedger authority boundary transitions.

### EVENT_LEDGER_TRUST_REVOKED
Emitted when health mode degrades and ledger trust is revoked.
```
EVENT_LEDGER_TRUST_REVOKED reason=health_mode=DEGRADED_WS
EVENT_LEDGER_TRUST_REVOKED reason=health_mode=STALE_TRUTH
```
After this signal, all ledger-preferred order-visibility paths fall back to snapshot.

### EVENT_LEDGER_TRUST_RESTORED
Emitted when trust is restored after recovery (HEALTHY + converged comparison).
```
EVENT_LEDGER_TRUST_RESTORED
```

### EVENT_LEDGER_HYDRATED
Emitted when new orders are hydrated from snapshot into the ledger.
```
EVENT_LEDGER_HYDRATED orders=10 snapshot_ts=1774835756 bootstrapped=True trusted=True
```

### EVENT_LEDGER_SHADOW_DIVERGENCE
Emitted when ledger and snapshot disagree on open-order state.
```
EVENT_LEDGER_SHADOW_DIVERGENCE divergences=2 ledger_orders=8 snapshot_orders=10 trusted=False
```

### EVENT_FIRST_FILL_APPLIED
Emitted when WS user-data path processes a fill event-first (before snapshot).
```
EVENT_FIRST_FILL_APPLIED cid=g_g_BTCUSDT_e5_123_0 symbol=BTCUSDT source=user_data actions=7 trusted=True
```

### EVENT_LEDGER_ORDER_RECOVERED
Emitted when a stale ledger-open order is closed by snapshot-backed reconciliation
(missed WS terminal event). Requires two consecutive snapshot absences.
```
EVENT_LEDGER_ORDER_RECOVERED cid=g_g_PIPPINUSDT_e0_123_0 symbol=PIPPINUSDT reason=snapshot_absence_consecutive
```

### EVENT_LEDGER_RECONCILED
Emitted after snapshot reconciliation closes stale orders and re-compares.
```
EVENT_LEDGER_RECONCILED orders=5 converged=True
```

### Operator interpretation
| Signal | Meaning | Action |
|--------|---------|--------|
| `TRUST_REVOKED` | Ledger no longer authoritative | Monitor for health recovery |
| `TRUST_RESTORED` | Ledger re-enabled after recovery | Normal operation resumed |
| `SHADOW_DIVERGENCE` with `trusted=False` | Ledger and snapshot disagree, ledger not trusted | Expected during/after degraded mode |
| `SHADOW_DIVERGENCE` with `trusted=True` | Should not happen (divergence revokes trust) | Investigate immediately |
| `ORDER_RECOVERED` | Stale order closed by snapshot absence | Normal recovery; WS terminal event was missed |
| `RECONCILED converged=True` | Recovery healed all divergences | Trust can now reconverge |
| `RECONCILED converged=False` | Recovery closed some but not all | Remaining divergences need more cycles or investigation |

## Timestamp Offset Recovery (ADR-118)

Log signals for server-time offset refresh (clock drift recovery).

### BINANCE_TIME_OFFSET_REFRESHED
Emitted when offset is successfully refreshed from `/fapi/v1/time`.
```
BINANCE_TIME_OFFSET_REFRESHED old_ms=-300 new_ms=-1800
```

### BINANCE_TIME_OFFSET_REFRESH_FAILED
Emitted when offset refresh fails (network error, timeout). Fail-open: existing offset kept.
```
BINANCE_TIME_OFFSET_REFRESH_FAILED keeping=-300
```

### Operator interpretation
| Signal | Meaning | Action |
|--------|---------|--------|
| `OFFSET_REFRESHED` with large delta | WSL2/system clock drifted significantly | Normal recovery, no action needed |
| `OFFSET_REFRESH_FAILED` | Cannot reach Binance time endpoint | Check network; existing offset still used |
| Repeated `-1021` after refresh | Drift too fast for periodic correction | Investigate system clock source |

## Trading Loop Cleanup Signals (ADR-121)

### TRADING_PLANNED_CLEANUP_STARTED / COMPLETED
Normal duration-reached exit. Cleanup runs as planned.
```
TRADING_PLANNED_CLEANUP_STARTED stop_reason=duration_reached
TRADING_PLANNED_CLEANUP_COMPLETED failures=0 stop_reason=duration_reached
```

### TRADING_ABORT_CLEANUP_STARTED / COMPLETED
Fatal abort (WS reconnect exhaustion, crash, etc.) after live trading started.
```
TRADING_ABORT_CLEANUP_STARTED stop_reason=stream_ended
TRADING_ABORT_CLEANUP_COMPLETED failures=0 stop_reason=stream_ended
```

### Operator interpretation
| Signal | Meaning | Action |
|--------|---------|--------|
| `PLANNED_CLEANUP_COMPLETED failures=0` | Normal clean exit | No action |
| `ABORT_CLEANUP_COMPLETED failures=0` | Fatal exit, but cleanup succeeded | Investigate root cause (WS, network) |
| `ABORT_CLEANUP_COMPLETED failures>0` | Fatal exit, cleanup partially failed | Check exchange state manually |
| `Cleanup-on-exit skipped: loop_never_started` | Loop died before trading | No exchange state to clean |
