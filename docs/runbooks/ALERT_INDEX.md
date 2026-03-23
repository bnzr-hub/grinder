# Alert Routing Index

Operator quick-reference: alert fires → where to look → what to do first.

> **If `GrinderTargetDown` is firing, stop. Fix scraping first — all other alerts are blind.**

**SSOT references (do not duplicate here):**
- Alert rules source: [`monitoring/alert_rules.yml`](../../monitoring/alert_rules.yml)
- SLO definitions & PromQL: [`docs/OBSERVABILITY_SLOS.md`](../OBSERVABILITY_SLOS.md)
- Dashboard UID mapping: [`monitoring/grafana/dashboards/README.md`](../../monitoring/grafana/dashboards/README.md)
- Detailed triage procedures: [`docs/runbooks/06_ALERT_RESPONSE.md`](06_ALERT_RESPONSE.md)
- Alert contract (OBS-3/OBS-4): validated by `scripts/verify_alert_rules.py`

---

## How to open a dashboard by `dashboard_uid`

```bash
# See monitoring/grafana/dashboards/README.md for full instructions.
# Quick: Grafana > Dashboards > Search (or press /) > search by title from table below.
```

## How to find an alert rule

```bash
rg -n 'alert: <NAME>' monitoring/alert_rules.yml
```

---

## Alert routing table

56 alerts across 13 groups. Sorted by severity (critical → page → warning → ticket → info).

### Critical (9 alerts)

| Alert | component | category | `dashboard_uid` | Runbook | First look |
|-------|-----------|----------|-----------------|---------|------------|
| `GrinderTargetDown` | scrape | availability | `prometheus-targets` | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md) | `curl localhost:9090/metrics` — if empty, scrape is down |
| `GrinderDown` | process | availability | `prometheus-targets` | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md) | `pgrep -af grinder` — is process alive? |
| `EngineInitDown` | engine | availability | `grinder-trading-loop` | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md#engine-initialization-failures) | Check logs for init errors, fill model load |
| `KillSwitchTripped` | risk | safety | `grinder-overview` | [04_KILL_SWITCH](04_KILL_SWITCH.md) | Check trip reason labels, drawdown % |
| `ConsecutiveLossTrip` | risk | safety | `grinder-overview` | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md#consecutive-loss-guard-pr-c3b) | Check `GRINDER_OPERATOR_OVERRIDE` |
| `FuturesHttpRequestsDetected` | exchange | safety | `grinder-trading-loop` | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md#unexpected-futures-http) | Any `/fapi/` route = real Binance call |
| `ReconcileRemediationExecuted` | reconcile | safety | `grinder-reconcile` | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcileremediationexecuted) | Verify action was intended |
| `MlInferenceLatencyCritical` | ml | latency | `grinder-ml-overview` | [18_ML_INFERENCE_SLOS](18_ML_INFERENCE_SLOS.md#mlinferencelatencycritical) | Consider model rollback or kill-switch |
| `FillProbBlocksHigh` | sor | safety | `grinder-trading-loop` | [31_FILL_PROB_ROLLOUT](31_FILL_PROB_ROLLOUT.md#circuit-breaker-tuning) | Check threshold, model calibration |

### Page (6 alerts)

| Alert | component | category | `dashboard_uid` | Runbook | First look |
|-------|-----------|----------|-----------------|---------|------------|
| `GrinderDataStaleBurst` | dq | correctness | `grinder-overview` | [23_DATA_QUALITY_TRIAGE](23_DATA_QUALITY_TRIAGE.md#grinderdatastaleburst) | Check exchange feed, WebSocket health |
| `GrinderDQBlockingActive` | dq | safety | `grinder-overview` | [23_DATA_QUALITY_TRIAGE](23_DATA_QUALITY_TRIAGE.md#grinderdqblockingactive) | Investigate feed quality before action |
| `GrinderHttpWriteDeadlineMissBurst` | exchange | latency | `grinder-trading-loop` | [24_LATENCY_RETRY_TRIAGE](24_LATENCY_RETRY_TRIAGE.md#grinderhttpwritedeadlinemissburst) | Check network latency, exchange status |
| `GrinderHttpCancelLatencyP99High` | exchange | latency | `grinder-trading-loop` | [24_LATENCY_RETRY_TRIAGE](24_LATENCY_RETRY_TRIAGE.md#grinderhttpcancellatencyp99high) | Check exchange API latency |
| `FillIngestNoPolls` | fills | availability | `grinder-overview` | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillingestnopolls) | Reconcile loop may be stuck |
| `FillCursorSaveErrors` | fills | integrity | `grinder-overview` | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillcursorsaveerrors) | Check disk permissions, path |

### Warning (30 alerts)

| Alert | component | category | `dashboard_uid` | Runbook | First look |
|-------|-----------|----------|-----------------|---------|------------|
| `ReadyzNotReady` | readyz | availability | — | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md#readyz-not-ready) | Check HA role (standby is normal) |
| `GridV2IntegrityMismatchPendingBurst` | engine | integrity | — | [06_ALERT_RESPONSE](06_ALERT_RESPONSE.md#warning-alerts) | Frequent pre-repair mismatch streaks in grid_v2 watchdog |
| `GridV2RejectedFillCleanedBurst` | engine | integrity | — | [06_ALERT_RESPONSE](06_ALERT_RESPONSE.md#warning-alerts) | Rejected-fill auto-clean events are spiking (race/orphan/duplicate pressure) |
| `HighGatingBlocks` | gating | safety | — | [06_ALERT_RESPONSE](06_ALERT_RESPONSE.md#warning-alerts) | Check block reasons |
| `ToxicityTriggers` | gating | safety | — | [06_ALERT_RESPONSE](06_ALERT_RESPONSE.md#warning-alerts) | Spread spike or price impact |
| `HighDrawdown` | risk | safety | — | [04_KILL_SWITCH](04_KILL_SWITCH.md) | Monitor; kill-switch at 5% |
| `ConsecutiveLossesHigh` | risk | safety | — | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md#consecutive-loss-guard-pr-c3b) | Early warning; guard trips at threshold |
| `ReconcileLoopDown` | reconcile | availability | — | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcileloopdown) | Loop stopped while process alive |
| `ReconcileSnapshotStale` | reconcile | correctness | — | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcilesnapshotstale) | Snapshot age >120s |
| `ReconcileMismatchSpike` | reconcile | correctness | — | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcilemismatchspike) | Investigate expected vs observed |
| `ReconcileMismatchNoBlocks` | reconcile | correctness | — | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcilemismatchnoblocks) | Gates may be bypassed |
| `ReconcileBudgetCallsExhausted` | reconcile | capacity | — | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcilebudgetcallsexhausted) | No remediation calls left today |
| `ReconcileBudgetNotionalLow` | reconcile | capacity | — | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcilebudgetnotionallow) | Notional <10 USDT remaining |
| `MlInferenceLatencyHigh` | ml | latency | — | [18_ML_INFERENCE_SLOS](18_ML_INFERENCE_SLOS.md#mlinferencelatencyhigh) | Check ONNX model performance |
| `MlInferenceErrorRateHigh` | ml | correctness | — | [18_ML_INFERENCE_SLOS](18_ML_INFERENCE_SLOS.md#mlinferenceerrorratehigh) | Check model and input data |
| `MlInferenceStalled` | ml | availability | — | [18_ML_INFERENCE_SLOS](18_ML_INFERENCE_SLOS.md#mlinferencestalled) | No inferences for 10m; check config |
| `FsmBadStateTooLong` | fsm | availability | — | [27_FSM_OPERATOR_OVERRIDE](27_FSM_OPERATOR_OVERRIDE.md#fsmbadstatetoolong) | Check operator override |
| `FsmActionBlockedSpike` | fsm | safety | — | [27_FSM_OPERATOR_OVERRIDE](27_FSM_OPERATOR_OVERRIDE.md#fsmactionblockedspike) | Intents rejected by permission matrix |
| `SorBlockedSpike` | sor | safety | — | [28_SOR_FIRE_DRILL](28_SOR_FIRE_DRILL.md#sorblockedspike) | Orders rejected by SOR |
| `FillProbBlocksSpike` | sor | safety | — | [31_FILL_PROB_ROLLOUT](31_FILL_PROB_ROLLOUT.md#circuit-breaker-tuning) | Early warning before FillProbBlocksHigh |
| `FillProbCircuitBreakerTripped` | sor | safety | — | [31_FILL_PROB_ROLLOUT](31_FILL_PROB_ROLLOUT.md#circuit-breaker-tuning) | Review model calibration, threshold |
| `AccountSyncStale` | account | correctness | — | [29_ACCOUNT_SYNC](29_ACCOUNT_SYNC.md#accountsyncstale) | Sync process not completing (liveness) |
| `AccountSyncDataStale` | account | correctness | — | [29_ACCOUNT_SYNC](29_ACCOUNT_SYNC.md#accountsyncdatastale) | Exchange data stale (non-empty accounts only) |
| `AccountSyncErrors` | account | availability | — | [29_ACCOUNT_SYNC](29_ACCOUNT_SYNC.md#accountsyncerrors) | Check API connectivity, credentials |
| `AccountSyncMismatchSpike` | account | correctness | — | [30_ACCOUNT_SYNC_FIRE_DRILL](30_ACCOUNT_SYNC_FIRE_DRILL.md#accountsyncmismatchspike) | Expected vs observed diverged |
| `FillIngestDisabled` | fills | availability | — | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillingestdisabled) | Set `FILL_INGEST_ENABLED=1` |
| `FillParseErrors` | fills | correctness | — | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillparseerrors) | Check for API schema changes |
| `FillIngestHttpErrors` | fills | availability | — | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillingesthttperrors) | Check Binance connectivity, API key |
| `FillCursorStuck` | fills | correctness | — | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillcursorstuck) | Cursor not saved for 30m+ |
| `FillCursorNonMonotonicRejected` | fills | integrity | — | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillcursornonmonotonicrejected) | Possible data corruption |

### Ticket (4 alerts)

| Alert | component | category | `dashboard_uid` | Runbook | First look |
|-------|-----------|----------|-----------------|---------|------------|
| `GrinderDataGapSpike` | dq | correctness | — | [23_DATA_QUALITY_TRIAGE](23_DATA_QUALITY_TRIAGE.md#grinderdatagapspike) | Check exchange connectivity, WebSocket |
| `GrinderDataOutlierSpike` | dq | correctness | — | [23_DATA_QUALITY_TRIAGE](23_DATA_QUALITY_TRIAGE.md#grinderdataoutlierspike) | Flash crash, exchange glitch, or thresholds |
| `GrinderHttpReadRetriesSpike` | exchange | latency | — | [24_LATENCY_RETRY_TRIAGE](24_LATENCY_RETRY_TRIAGE.md#grinderhttpreadretriesspike) | Sustained retries waste latency budget |
| `GrinderHttp429RateLimitSpike` | exchange | capacity | — | [24_LATENCY_RETRY_TRIAGE](24_LATENCY_RETRY_TRIAGE.md#grinderhttp429ratelimitspike) | Reduce reconcile interval or request freq |

### Info (7 alerts)

| Alert | component | category | `dashboard_uid` | Runbook | First look |
|-------|-----------|----------|-----------------|---------|------------|
| `GrinderRecentRestart` | process | availability | — | [06_ALERT_RESPONSE](06_ALERT_RESPONSE.md#ticket--info-alerts) | Verify intentional; check logs if not |
| `AllGatingBlocked` | gating | safety | — | [06_ALERT_RESPONSE](06_ALERT_RESPONSE.md#ticket--info-alerts) | All decisions blocked for 5m |
| `KillSwitchTripIncreased` | risk | safety | — | [04_KILL_SWITCH](04_KILL_SWITCH.md) | Kill-switch trip count increased |
| `ReconcileRemediationPlanned` | reconcile | integrity | — | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcileremediationplanned) | Dry-run mode — expected behavior |
| `ReconcileRemediationBlocked` | reconcile | integrity | — | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcileremediationblocked) | Safety gates active — expected in rollout |
| `MlActiveModePersistentlyBlocked` | ml | availability | — | [18_ML_INFERENCE_SLOS](18_ML_INFERENCE_SLOS.md#mlactivemodepersistentlyblocked) | Check block reasons in metrics |
| `SorNoopSpike` | sor | safety | — | [28_SOR_FIRE_DRILL](28_SOR_FIRE_DRILL.md#sornoopspike) | Orders skipped (already in desired state) |

---

## Coverage summary

| Severity | Count | With `dashboard_uid` |
|----------|-------|---------------------|
| critical | 9 | 9 (100%) |
| page | 6 | 6 (100%) |
| warning | 30 | 0 |
| ticket | 4 | 0 |
| info | 7 | 0 |

| **Total** | **56** | **15** |

> `dashboard_uid` is required for `critical` and `page` alerts (OBS-3/OBS-4 contract).
> Enforced by `scripts/verify_alert_rules.py`.
>
> This index is validated against `monitoring/alert_rules.yml` by `scripts/verify_alert_index.py` (OBS-7).
