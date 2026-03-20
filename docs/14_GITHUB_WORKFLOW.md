# GRINDER - GitHub Workflow

> CI/CD, branching strategy, and development workflows
> Adapted from cryptoscreener pipeline

---

## 14.1 Pipeline Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    CI/CD PIPELINE                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  PR Created                                                      │
│      │                                                           │
│      ▼                                                           │
│  ┌─────────┐  ┌─────────────┐  ┌──────────────┐                │
│  │   CI    │  │  Acceptance │  │ Proof Guard  │                │
│  │ (lint,  │  │   Packet    │  │ (validates   │                │
│  │  test)  │  │ (generates) │  │  PR body)    │                │
│  └────┬────┘  └──────┬──────┘  └──────┬───────┘                │
│       │              │                │                         │
│       └──────────────┴────────────────┘                         │
│                      │                                          │
│                      ▼                                          │
│            ┌─────────────────┐                                  │
│            │  All Checks     │                                  │
│            │    Passed?      │                                  │
│            └────────┬────────┘                                  │
│                     │                                           │
│         ┌──────────┴──────────┐                                 │
│         ▼                     ▼                                 │
│    ┌─────────┐          ┌─────────┐                            │
│    │ APPROVE │          │  FAIL   │                            │
│    │ & MERGE │          │ (fix)   │                            │
│    └─────────┘          └─────────┘                            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 14.2 Workflow Files

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `ci.yml` | push, PR | Lint, type check, test, replay |
| `acceptance_packet.yml` | PR | Generate proof bundle |
| `proof_guard.yml` | PR | Validate PR has proof |
| `replay_determinism.yml` | PR (paths) | Verify backtest determinism |
| `docker_smoke.yml` | PR (paths) | Build & test container |
| `nightly_soak.yml` | schedule, manual | Load testing |
| `secret_guard.yml` | push, PR | Scan for secrets |
| `promtool.yml` | PR (paths) | Validate Prometheus rules |

---

## 14.3 CI Workflow

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [ main ]
  pull_request:

jobs:
  checks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install
        run: pip install -e ".[dev]"

      - name: Ruff
        run: ruff check .

      - name: Ruff format
        run: ruff format --check .

      - name: Mypy
        run: mypy .

      - name: Pytest
        run: pytest -q

      - name: Backtest replay
        run: python -m scripts.run_replay --fixture tests/fixtures/sample_day/ -v
```

### Quality Gates

| Gate | Tool | Threshold |
|------|------|-----------|
| Linting | ruff | 0 errors |
| Formatting | ruff format | 0 diffs |
| Type check | mypy | 0 errors |
| Tests | pytest | 100% pass |
| Coverage | pytest-cov | ≥80% |

---

## 14.4 Acceptance Packet

The acceptance packet is auto-generated proof bundle that validates:

1. **Identity** - PR metadata (URL, state, base/head refs)
2. **Merge Safety** - Stacked PR detection, PR chain resolution
3. **CI Status** - All checks passed
4. **Quality Gates** - ruff, mypy, pytest locally
5. **PR Diff** - Full diff for review
6. **Replay Determinism** - If touching backtest code

### PR Body Format

```markdown
## Summary
...

<!-- ACCEPTANCE_PACKET_START -->
== ACCEPTANCE PACKET: IDENTITY ==
{PR metadata JSON}

== ACCEPTANCE PACKET: MERGE SAFETY ==
merge_type: DIRECT | STACKED
base_branch: main
ready_for_final_merge: true

== ACCEPTANCE PACKET: CI ==
{CI check statuses}

== ACCEPTANCE PACKET: GATES ==
--- ruff check . ---
... PASS

--- mypy . ---
... PASS

--- pytest -q ---
... PASS

== ACCEPTANCE PACKET: PR DIFF ==
diff --git a/... b/...
...

== ACCEPTANCE PACKET: END ==
RESULT: PASS
<!-- ACCEPTANCE_PACKET_END -->
```

### Modes

| Mode | When | Content |
|------|------|---------|
| FULL VERBATIM | Body < 50KB | Full proof embedded |
| CI ARTIFACT | Body ≥ 50KB | Reference to artifact |

---

## 14.5 Proof Guard

Validates PR body has required proof markers before merge:

```yaml
# .github/workflows/proof_guard.yml
- Waits for acceptance-packet workflow
- Fetches PR body from API (not stale payload)
- Detects mode: FULL VERBATIM or CI ARTIFACT
- For FULL VERBATIM: checks all markers present, diff --git exists
- For CI ARTIFACT: validates via GitHub Check-Runs API
- Enforces replay determinism if touching fixture/replay files
```

**Operational rule:** when refreshing proof/acceptance checks for a PR, run the
workflow against the PR ref/head SHA, not against `main`. A successful
`workflow_dispatch` on `main` does not replace stale PR-head check runs and will
leave `gh pr checks` and merge protection blocked.

### Failure Modes

| Error | Cause | Fix |
|-------|-------|-----|
| `PENDING marker` | CI hasn't updated body | Wait for acceptance-packet |
| `Missing markers` | Incomplete proof | Re-run acceptance_packet.sh |
| `No diff --git` | Empty diff | Ensure changes are committed |
| `Check-run failed` | CI ARTIFACT validation | Fix CI errors |

---

## 14.6 Branching Strategy

```
main ─────●─────●─────●─────●─────●───► (production)
          │           ↑           ↑
          │           │           │
develop ──●───●───●───●───●───●───●───► (integration)
              │       ↑       │
              │       │       │
feature/* ────●───●───┘       │
                              │
fix/* ────────────────────────●
```

### Branch Types

| Branch | Base | Merge To | Purpose |
|--------|------|----------|---------|
| `main` | - | - | Production |
| `develop` | main | main | Integration |
| `feature/*` | develop | develop | New features |
| `fix/*` | develop | develop | Bug fixes |
| `hotfix/*` | main | main + develop | Urgent fixes |

### Stacked PRs

When PR base is not main:
1. Acceptance packet detects stacked PR
2. Resolves full PR chain (prerequisite PRs)
3. Reports merge order
4. `--require-main-base` flag for final merge validation

---

## 14.7 Commit Convention

### Format

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Types

| Type | Description |
|------|-------------|
| `feat` | New feature |
| `fix` | Bug fix |
| `refactor` | Code refactoring |
| `perf` | Performance improvement |
| `test` | Adding/updating tests |
| `docs` | Documentation only |
| `chore` | Build, CI, tooling |

### Scopes

| Scope | Area |
|-------|------|
| `data` | Data pipeline |
| `prefilter` | Top-K selection |
| `policy` | Grid policies |
| `execution` | Order execution |
| `risk` | Risk management |
| `ml` | ML models |
| `backtest` | Backtesting |

---

## 14.8 Docker Workflow

### Build & Smoke Test

```yaml
# .github/workflows/docker_smoke.yml
- name: Build Docker image
  run: docker build -t grinder:test .

- name: Start container
  run: |
    docker run -d --name grinder-smoke -p 9090:9090 \
      grinder:test --symbols BTCUSDT,ETHUSDT --duration-s 30

- name: Check endpoints
  run: |
    curl -f http://localhost:9090/healthz
    curl -f http://localhost:9090/readyz
    curl http://localhost:9090/metrics | grep grinder_pnl_total_usd
```

### Required Metrics

| Metric | Purpose |
|--------|---------|
| `grinder_pnl_total_usd` | Total P&L |
| `grinder_round_trips_total` | Trade count |
| `grinder_order_latency_ms` | Execution speed |
| `grinder_drawdown_pct` | Risk monitoring |
| `grinder_toxicity_score` | Market quality |
| `grinder_state` | System state |

---

## 14.9 Soak Testing

### Nightly Soak

```yaml
# Runs daily at 03:00 UTC
schedule:
  - cron: '0 3 * * *'

# Baseline test: 5 symbols, 5 minutes
python -m scripts.run_soak \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT \
  --duration-s 300 \
  --cadence-ms 1000 \
  --mode baseline

# Overload test: 20 symbols, 3 minutes, slow consumer
python -m scripts.run_soak \
  --symbols <20 symbols> \
  --duration-s 180 \
  --cadence-ms 500 \
  --slow-consumer-lag-ms 200 \
  --mode overload
```

### Thresholds

```yaml
# monitoring/soak_thresholds.yml
baseline:
  decision_latency_p99_ms: 50
  order_latency_p99_ms: 200
  event_queue_depth_max: 100
  errors_total: 0

overload:
  decision_latency_p99_ms: 100
  order_latency_p99_ms: 500
  event_queue_depth_max: 500
  events_dropped: 100
```

---

## 14.10 Replay Determinism

Triggered when PR touches:
- `tests/fixtures/**`
- `tests/backtest/**`
- `scripts/run_replay.py`
- `src/grinder/policies/**`
- `src/grinder/execution/**`
- `src/grinder/risk/**`
- `src/grinder/backtest/**`

### Verification Process

```python
# scripts/verify_replay_determinism.py

# Run replay twice
digest1 = run_replay(fixture_dir, run_id=1)
digest2 = run_replay(fixture_dir, run_id=2)

# Compare digests
if digest1 == digest2:
    print("DETERMINISM CHECK PASSED")
else:
    sys.exit(1)
```

---

## 14.11 Automation Scripts

| Script | Purpose |
|--------|---------|
| `acceptance_packet.sh` | Generate proof bundle |
| `verify_replay_determinism.py` | Double-run replay check |
| `secret_guard.py` | Scan for credentials |
| `check_soak_thresholds.py` | Validate soak results |

### Usage

```bash
# Generate acceptance packet for PR #123
./scripts/acceptance_packet.sh 123

# With main base requirement
./scripts/acceptance_packet.sh --require-main-base 123

# Verify replay determinism
python -m scripts.verify_replay_determinism

# Scan for secrets
python -m scripts.secret_guard --verbose
```

---

## 14.12 Pre-commit Hooks

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.1.6
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.7.1
    hooks:
      - id: mypy

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.5.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: detect-private-key
```

### Setup

```bash
pip install pre-commit
pre-commit install
```

---

## 14.13 Secrets Management

### Required Secrets

| Secret | Scope | Description |
|--------|-------|-------------|
| `BINANCE_API_KEY` | Production | Exchange API key |
| `BINANCE_API_SECRET` | Production | Exchange API secret |
| `SLACK_WEBHOOK_URL` | CI/Prod | Notifications |
| `PAGERDUTY_API_KEY` | Production | Alerting |

### Local Development

```bash
# Copy example and fill in values
cp .env.example .env

# NEVER commit .env!
```

---

## 14.14 Monitoring Integration

### Alert Rules

```yaml
# monitoring/alert_rules.yml
groups:
  - name: grinder_critical
    rules:
      - alert: GrinderEmergencyExit
        expr: grinder_state == 6
        labels:
          severity: critical

      - alert: GrinderHighDrawdown
        expr: grinder_drawdown_pct{period="daily"} > 0.08
        labels:
          severity: critical
```

---

## 14.15 Project Structure

```
grinder/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml
│   │   ├── acceptance_packet.yml
│   │   ├── proof_guard.yml
│   │   ├── replay_determinism.yml
│   │   ├── docker_smoke.yml
│   │   ├── nightly_soak.yml
│   │   ├── secret_guard.yml
│   │   └── promtool.yml
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.md
│   │   └── feature_request.md
│   ├── pull_request_template.md
│   └── CODEOWNERS
├── docs/
│   └── *.md
├── src/grinder/
├── tests/
├── scripts/
│   ├── acceptance_packet.sh
│   ├── verify_replay_determinism.py
│   ├── secret_guard.py
│   └── check_soak_thresholds.py
├── monitoring/
│   ├── prometheus.yml
│   ├── alert_rules.yml
│   ├── soak_thresholds.yml
│   └── grafana/
├── .pre-commit-config.yaml
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

---

## 14.16 CODEOWNERS

```
# Default owners
* @trading-team

# Risk management - requires additional review
/src/grinder/risk/ @risk-team @trading-team

# ML models
/src/grinder/ml/ @ml-team @trading-team

# Infrastructure
/.github/ @devops-team
/monitoring/ @devops-team @trading-team
```
