# GRINDER Runbooks

Operational runbooks for GRINDER trading system.

## Index

| Runbook | Description |
|---------|-------------|
| [00_OPS_QUICKSTART](00_OPS_QUICKSTART.md) | Operator quickstart: alert → script → evidence (Launch-07) |
| [00_EVIDENCE_INDEX](00_EVIDENCE_INDEX.md) | Evidence artifact index: scripts, paths, key files (Launch-07) |
| [01_STARTUP_SHUTDOWN](01_STARTUP_SHUTDOWN.md) | How to start and stop GRINDER |
| [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md) | Quick health checks and diagnostics |
| [03_METRICS_DASHBOARDS](03_METRICS_DASHBOARDS.md) | Prometheus metrics and Grafana dashboards |
| [04_KILL_SWITCH](04_KILL_SWITCH.md) | Kill-switch events and recovery |
| [05_SOAK_GATE](05_SOAK_GATE.md) | Running soak tests and interpreting results |
| [06_ALERT_RESPONSE](06_ALERT_RESPONSE.md) | Responding to Prometheus alerts |
| [07_HA_OPERATIONS](07_HA_OPERATIONS.md) | HA deployment, failover, and troubleshooting |
| [08_SMOKE_TEST_TESTNET](08_SMOKE_TEST_TESTNET.md) | Testnet smoke test procedure |
| [09_MAINNET_TRADE_SMOKE](09_MAINNET_TRADE_SMOKE.md) | Spot mainnet smoke test procedure (LC-08b) |
| [10_FUTURES_MAINNET_TRADE_SMOKE](10_FUTURES_MAINNET_TRADE_SMOKE.md) | Futures USDT-M mainnet smoke test (LC-08b-F) |
| [11_RECONCILIATION_TRIAGE](11_RECONCILIATION_TRIAGE.md) | Reconciliation mismatch triage (LC-09b) |
| [12_ACTIVE_REMEDIATION](12_ACTIVE_REMEDIATION.md) | Active remediation operations (LC-10) |
| [13_OPERATOR_CEREMONY](13_OPERATOR_CEREMONY.md) | Operator ceremony for safe enablement (LC-11) |
| [14_RECONCILE_E2E_SMOKE](14_RECONCILE_E2E_SMOKE.md) | E2E reconcile→remediate smoke test (LC-13) |
| [15_ENABLEMENT_CEREMONY](15_ENABLEMENT_CEREMONY.md) | Staged enablement ceremony for ReconcileLoop (LC-15a) |
| [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md) | Reconcile alerts, SLOs, and triage (LC-15b) |
| [17_LIVE_TRADE_CONNECTOR_ENABLEMENT](17_LIVE_TRADE_CONNECTOR_ENABLEMENT.md) | LIVE_TRADE connector enablement (LC-22) |
| [21_SINGLE_VENUE_LAUNCH](21_SINGLE_VENUE_LAUNCH.md) | Single-venue launch readiness (Launch-01) |
| [22_ACTIVE_ENABLEMENT_CEREMONY](22_ACTIVE_ENABLEMENT_CEREMONY.md) | ACTIVE enablement ceremony (Launch-02) |
| [23_DATA_QUALITY_TRIAGE](23_DATA_QUALITY_TRIAGE.md) | Data quality alerts and triage (Launch-04) |
| [24_LATENCY_RETRY_TRIAGE](24_LATENCY_RETRY_TRIAGE.md) | HTTP latency/retry alerts and triage (Launch-05) |
| [25_LATENCY_RETRY_ENABLEMENT_CEREMONY](25_LATENCY_RETRY_ENABLEMENT_CEREMONY.md) | Latency/retry enablement ceremony (Launch-05b) |
| [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md) | Fill tracker metrics triage (Launch-06) |
| [27_FSM_OPERATOR_OVERRIDE](27_FSM_OPERATOR_OVERRIDE.md) | FSM operator override: PAUSE/EMERGENCY via env var (Launch-13) |
| [28_SOR_FIRE_DRILL](28_SOR_FIRE_DRILL.md) | SOR fire drill (Launch-14) |
| [29_ACCOUNT_SYNC](29_ACCOUNT_SYNC.md) | Account sync operations (Launch-15) |
| [30_ACCOUNT_SYNC_FIRE_DRILL](30_ACCOUNT_SYNC_FIRE_DRILL.md) | Account sync fire drill (Launch-15) |
| [31_FILL_PROB_ROLLOUT](31_FILL_PROB_ROLLOUT.md) | Fill probability enforcement setup and config (Track C) |
| [32_MAINNET_ROLLOUT_FILL_PROB](32_MAINNET_ROLLOUT_FILL_PROB.md) | Mainnet rollout ceremony for fill-prob enforcement (Track C) |
| [35_GRID_V2_SHADOW_VERIFICATION](35_GRID_V2_SHADOW_VERIFICATION.md) | Grid V2 shadow verification procedure (doc-27 PR5) |

## Quick Reference

| Endpoint | URL | Purpose |
|----------|-----|---------|
| Health | `http://localhost:9090/healthz` | Liveness check |
| Ready | `http://localhost:9090/readyz` | Readiness check (HA-aware) |
| Metrics | `http://localhost:9090/metrics` | Prometheus scrape |
| Prometheus | `http://localhost:9091` | Metrics UI |
| Grafana | `http://localhost:3000` | Dashboards (admin/admin) |

## Prerequisites

- Docker with Compose plugin installed (`docker compose` command)
  - Note: Legacy `docker-compose` (standalone binary) also works but is deprecated
- Repository cloned with submodules
- `.env` file configured (copy from `.env.example`)
