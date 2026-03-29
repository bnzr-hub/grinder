# GRINDER - Risk Specification

> Risk management, limits, and emergency procedures

---

## 10.1 Risk Hierarchy

```
┌─────────────────────────────────────────────────────────────────┐
│                      RISK MANAGEMENT                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Level 1: PRE-TRADE                                             │
│  ├── Position limit check                                       │
│  ├── Order size limit check                                     │
│  ├── Concentration limit check                                  │
│  └── Margin requirement check                                   │
│                                                                  │
│  Level 2: REAL-TIME                                             │
│  ├── Inventory monitoring                                       │
│  ├── P&L monitoring                                             │
│  ├── Drawdown monitoring                                        │
│  └── Exposure monitoring (beta-adjusted)                        │
│                                                                  │
│  Level 3: CIRCUIT BREAKERS                                      │
│  ├── Session drawdown limit                                     │
│  ├── Daily drawdown limit                                       │
│  ├── Consecutive loss limit                                     │
│  └── Error rate limit                                           │
│                                                                  │
│  Level 4: EMERGENCY                                             │
│  ├── Kill switch (manual)                                       │
│  ├── Auto-deleveraging                                          │
│  └── Exchange circuit breaker response                          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 10.2 Risk Limits

```python
@dataclass
class RiskLimits:
    """Configurable risk limits."""

    # Inventory limits
    inv_max_symbol_usd: Decimal = Decimal("5000")
    inv_max_total_usd: Decimal = Decimal("20000")
    inv_max_symbol_pct: float = 0.25  # Max % in one symbol

    # Drawdown limits
    dd_max_session_usd: Decimal = Decimal("500")
    dd_max_session_pct: float = 0.05
    dd_max_daily_usd: Decimal = Decimal("1000")
    dd_max_daily_pct: float = 0.10

    # Position limits
    pos_max_leverage: float = 3.0
    pos_max_concentration: float = 0.4
    pos_beta_adjusted_cap_usd: Decimal = Decimal("10000")

    # Loss limits
    loss_max_per_rt_usd: Decimal = Decimal("50")
    loss_max_consecutive: int = 5
    loss_cooldown_s: int = 300

    # Order limits
    order_max_size_usd: Decimal = Decimal("1000")
    order_max_per_minute: int = 60
    order_max_pending: int = 50
```

### Limit Tables

#### Inventory Limits

| Limit | Default | Description |
|-------|---------|-------------|
| `INV_MAX_SYMBOL_USD` | $5,000 | Max notional per symbol |
| `INV_MAX_TOTAL_USD` | $20,000 | Max total notional |
| `INV_MAX_SYMBOL_PCT` | 25% | Max % of total in one symbol |

#### Drawdown Limits

| Limit | Default | Description |
|-------|---------|-------------|
| `DD_MAX_SESSION_PCT` | 5% | Max session drawdown |
| `DD_MAX_DAILY_PCT` | 10% | Max daily drawdown |
| `DD_MAX_SESSION_USD` | $500 | Max session drawdown (USD) |
| `DD_MAX_DAILY_USD` | $1,000 | Max daily drawdown (USD) |

#### Order Limits

| Limit | Default | Description |
|-------|---------|-------------|
| `ORDER_MAX_SIZE_USD` | $1,000 | Max single order size |
| `ORDER_MAX_PER_MINUTE` | 60 | Rate limit |
| `ORDER_MAX_PENDING` | 50 | Max concurrent orders |

---

## 10.3 Pre-Trade Risk Checks

```python
class PreTradeRiskChecker:
    """Pre-trade risk validation."""

    def __init__(self, limits: RiskLimits, state: RiskState):
        self.limits = limits
        self.state = state

    def check_order(self, order: OrderRequest) -> RiskCheckResult:
        """Check if order passes all pre-trade checks."""
        checks = [
            self._check_order_size(order),
            self._check_position_limit(order),
            self._check_concentration(order),
            self._check_margin(order),
            self._check_rate_limit(order),
        ]

        failed = [c for c in checks if not c.passed]

        if failed:
            return RiskCheckResult(
                passed=False,
                reason=failed[0].reason,
                details=failed
            )

        return RiskCheckResult(passed=True, reason="OK")

    def _check_order_size(self, order: OrderRequest) -> CheckResult:
        """Check order size limit."""
        notional = order.price * order.quantity

        if notional > self.limits.order_max_size_usd:
            return CheckResult(
                passed=False,
                reason=f"ORDER_SIZE_EXCEEDS_LIMIT: {notional} > {self.limits.order_max_size_usd}"
            )
        return CheckResult(passed=True, reason="OK")

    def _check_position_limit(self, order: OrderRequest) -> CheckResult:
        """Check position limit."""
        current_pos = self.state.get_position(order.symbol)
        notional_after = self._calc_notional_after(current_pos, order)

        if notional_after > self.limits.inv_max_symbol_usd:
            return CheckResult(
                passed=False,
                reason=f"POSITION_LIMIT: {notional_after} > {self.limits.inv_max_symbol_usd}"
            )
        return CheckResult(passed=True, reason="OK")

    def _check_concentration(self, order: OrderRequest) -> CheckResult:
        """Check concentration limit."""
        total_notional = self.state.total_notional()
        symbol_notional = self.state.get_position(order.symbol).notional

        if total_notional > 0:
            concentration = float(symbol_notional / total_notional)
            if concentration > self.limits.inv_max_symbol_pct:
                return CheckResult(
                    passed=False,
                    reason=f"CONCENTRATION: {concentration:.1%} > {self.limits.inv_max_symbol_pct:.1%}"
                )
        return CheckResult(passed=True, reason="OK")
```

---

## 10.4 Real-Time Risk Monitoring

```python
class RiskMonitor:
    """Real-time risk monitoring."""

    def __init__(self, limits: RiskLimits):
        self.limits = limits
        self.session_high_watermark: Decimal = Decimal(0)
        self.daily_high_watermark: Decimal = Decimal(0)
        self.consecutive_losses: int = 0

    def update(self, state: RiskState) -> list[RiskAlert]:
        """Update risk state and return any alerts."""
        alerts = []

        # Update watermarks
        current_equity = state.equity
        if current_equity > self.session_high_watermark:
            self.session_high_watermark = current_equity
        if current_equity > self.daily_high_watermark:
            self.daily_high_watermark = current_equity

        # Calculate drawdowns
        session_dd = (self.session_high_watermark - current_equity) / self.session_high_watermark
        daily_dd = (self.daily_high_watermark - current_equity) / self.daily_high_watermark

        # Check limits
        if session_dd >= self.limits.dd_max_session_pct:
            alerts.append(RiskAlert(
                level="CRITICAL",
                type="DD_SESSION_BREACH",
                message=f"Session DD {session_dd:.2%} >= {self.limits.dd_max_session_pct:.2%}",
                action=RiskAction.EMERGENCY_EXIT
            ))

        if daily_dd >= self.limits.dd_max_daily_pct:
            alerts.append(RiskAlert(
                level="CRITICAL",
                type="DD_DAILY_BREACH",
                message=f"Daily DD {daily_dd:.2%} >= {self.limits.dd_max_daily_pct:.2%}",
                action=RiskAction.EMERGENCY_EXIT
            ))

        # Check inventory
        for symbol, pos in state.positions.items():
            if abs(pos.notional) > self.limits.inv_max_symbol_usd:
                alerts.append(RiskAlert(
                    level="WARNING",
                    type="INVENTORY_LIMIT",
                    message=f"{symbol} notional {pos.notional} > limit",
                    action=RiskAction.REDUCE_POSITION
                ))

        return alerts

    def record_round_trip(self, rt: RoundTrip) -> list[RiskAlert]:
        """Record round-trip and check loss limits."""
        alerts = []

        if rt.net_pnl < 0:
            self.consecutive_losses += 1

            # Check per-RT loss
            if abs(rt.net_pnl) > self.limits.loss_max_per_rt_usd:
                alerts.append(RiskAlert(
                    level="WARNING",
                    type="RT_LOSS_LARGE",
                    message=f"RT loss {rt.net_pnl} > limit",
                    action=RiskAction.PAUSE_SYMBOL
                ))

            # Check consecutive losses
            if self.consecutive_losses >= self.limits.loss_max_consecutive:
                alerts.append(RiskAlert(
                    level="WARNING",
                    type="CONSECUTIVE_LOSSES",
                    message=f"{self.consecutive_losses} consecutive losses",
                    action=RiskAction.PAUSE_TRADING
                ))
        else:
            self.consecutive_losses = 0

        return alerts
```

---

## 10.5 Portfolio Risk (Beta-Adjusted)

```python
class PortfolioRiskManager:
    """Portfolio-level risk management."""

    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def calculate_beta_adjusted_exposure(self,
                                         positions: dict[str, Position],
                                         betas: dict[str, float]) -> Decimal:
        """
        Calculate beta-adjusted exposure.

        High-beta positions contribute more to portfolio risk.
        """
        total = Decimal(0)

        for symbol, pos in positions.items():
            beta = betas.get(symbol, 1.0)
            beta_adjusted = pos.notional * Decimal(str(abs(beta)))
            total += beta_adjusted

        return total

    def check_portfolio_limits(self,
                               positions: dict[str, Position],
                               betas: dict[str, float]) -> list[RiskAlert]:
        """Check portfolio-level limits."""
        alerts = []

        # Beta-adjusted exposure
        beta_exposure = self.calculate_beta_adjusted_exposure(positions, betas)
        if beta_exposure > self.limits.pos_beta_adjusted_cap_usd:
            alerts.append(RiskAlert(
                level="WARNING",
                type="BETA_EXPOSURE_HIGH",
                message=f"Beta-adjusted exposure {beta_exposure} > limit",
                action=RiskAction.REDUCE_HIGH_BETA
            ))

        # Gross notional
        gross = sum(abs(p.notional) for p in positions.values())
        if gross > self.limits.inv_max_total_usd:
            alerts.append(RiskAlert(
                level="WARNING",
                type="GROSS_NOTIONAL_HIGH",
                message=f"Gross notional {gross} > limit",
                action=RiskAction.REDUCE_POSITIONS
            ))

        # Net exposure (directional)
        net = sum(p.notional for p in positions.values())
        if abs(net) > self.limits.inv_max_total_usd * Decimal("0.5"):
            alerts.append(RiskAlert(
                level="INFO",
                type="NET_EXPOSURE_HIGH",
                message=f"Net exposure {net} may indicate directional bias",
                action=RiskAction.NONE
            ))

        return alerts
```

---

## 10.6 Emergency Procedures

### Emergency Exit Sequence

```python
async def emergency_exit(reason: str) -> None:
    """Execute emergency exit procedure."""

    logger.critical(f"EMERGENCY EXIT: {reason}")

    # 1. Stop all new orders immediately
    await policy_engine.stop()

    # 2. Cancel all pending orders
    logger.info("Cancelling all pending orders...")
    cancel_results = await execution_engine.cancel_all_orders()
    logger.info(f"Cancelled {len(cancel_results)} orders")

    # 3. Reduce positions with market orders
    logger.info("Reducing positions...")
    for symbol, pos in positions.items():
        if pos.quantity == 0:
            continue

        # Use IOC market order
        side = "SELL" if pos.quantity > 0 else "BUY"
        await execution_engine.place_order(OrderRequest(
            symbol=symbol,
            side=side,
            quantity=abs(pos.quantity),
            type="MARKET",
            time_in_force="IOC",
            reduce_only=True,
        ))

    # 4. Wait for fills
    await asyncio.sleep(5)

    # 5. Verify positions closed
    remaining = await execution_engine.get_positions()
    if any(p.quantity != 0 for p in remaining.values()):
        logger.error("Some positions not closed!")
        await alert_manager.send_alert(
            level="CRITICAL",
            message="Emergency exit incomplete - manual intervention required",
            context={"remaining": remaining}
        )

    # 6. Enter PAUSED state
    await state_machine.transition("POSITION_REDUCED")

    logger.info("Emergency exit complete")
```

### Kill Switch

```python
class KillSwitch:
    """Manual kill switch for emergency stop."""

    def __init__(self):
        self.armed = False
        self.triggered = False
        self.trigger_ts: int | None = None

    def arm(self) -> None:
        """Arm the kill switch."""
        self.armed = True
        logger.warning("Kill switch ARMED")

    def trigger(self, reason: str) -> None:
        """Trigger the kill switch."""
        if not self.armed:
            logger.warning("Kill switch triggered but not armed - ignoring")
            return

        self.triggered = True
        self.trigger_ts = int(time.time() * 1000)
        logger.critical(f"KILL SWITCH TRIGGERED: {reason}")

        # Execute emergency exit
        asyncio.create_task(emergency_exit(f"KILL_SWITCH: {reason}"))

    def reset(self, operator: str) -> None:
        """Reset kill switch (requires manual action)."""
        if not self.triggered:
            return

        logger.warning(f"Kill switch reset by {operator}")
        self.triggered = False
        self.trigger_ts = None
        self.armed = False
```

---

## 10.7 Risk Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `risk_session_dd_pct` | Gauge | Current session drawdown |
| `risk_daily_dd_pct` | Gauge | Current daily drawdown |
| `risk_inventory_notional` | Gauge | Per-symbol notional |
| `risk_inventory_total` | Gauge | Total notional |
| `risk_beta_exposure` | Gauge | Beta-adjusted exposure |
| `risk_consecutive_losses` | Gauge | Consecutive losing trades |
| `risk_alerts_total` | Counter | Risk alerts by type |
| `risk_emergency_exits` | Counter | Emergency exits triggered |

---

## 10.8 Risk Reports

```python
@dataclass
class RiskReport:
    """Periodic risk report."""
    ts: int
    session_id: str

    # P&L
    session_pnl: Decimal
    daily_pnl: Decimal
    unrealized_pnl: Decimal

    # Drawdown
    session_dd_pct: float
    daily_dd_pct: float
    session_high_watermark: Decimal

    # Positions
    total_notional: Decimal
    net_notional: Decimal
    position_count: int
    largest_position: tuple[str, Decimal]

    # Risk metrics
    beta_adjusted_exposure: Decimal
    max_symbol_concentration: float
    consecutive_losses: int

    # Round-trips
    rt_count: int
    rt_win_rate: float
    rt_avg_pnl_bps: float

def generate_risk_report(state: RiskState) -> RiskReport:
    """Generate risk report from current state."""
    # ... implementation
```

---

## 10.9 Risk Configuration Validation

```python
def validate_risk_config(config: RiskLimits) -> list[str]:
    """Validate risk configuration."""
    errors = []

    # Drawdown limits should be reasonable
    if config.dd_max_session_pct >= config.dd_max_daily_pct:
        errors.append("Session DD limit should be less than daily DD limit")

    if config.dd_max_daily_pct > 0.20:
        errors.append("Daily DD limit > 20% is dangerous")

    # Inventory limits should be consistent
    if config.inv_max_symbol_usd > config.inv_max_total_usd:
        errors.append("Symbol limit cannot exceed total limit")

    # Order limits
    if config.order_max_size_usd > config.inv_max_symbol_usd:
        errors.append("Order size limit should be <= symbol position limit")

    return errors
```

## Risk-Saturated Mode (ADR-102)

When `RISK_SYMBOL_CAP` blocks entry placement repeatedly, the reconciler's projected target is constrained to legal capacity:

- **Trigger:** ≥ `GRINDER_RISK_SATURATION_THRESHOLD` (default 3) consecutive `RISK_SYMBOL_CAP` blocks per symbol WITH entries actually missing on exchange (desired entry keys from SM minus actual entry keys on exchange, before risk projection). Passive zero-headroom with a fully satisfied ladder does NOT count.
- **Legal capacity projection:** `risk_entry_capacity = min(sym_cap, gross_cap, net_cap)` where each = `floor(headroom / per_entry_notional)` for active caps.
  - `None` → unconstrained (risk base disabled, no caps enabled, or can't estimate).
  - `0` → fully blocked (no new entries allowed).
  - `N > 0` → partial ladder (N entries closest to reference price kept).
- **Structured reason:** `_compute_risk_legal_entry_capacity()` returns `(capacity, is_symbol_cap_blocked)`. Only `SYMBOL_CAP_EXCEEDED` with actual missing entries on exchange increments saturation counter. Other block reasons (stale, portfolio caps, DD) suppress entries but do NOT count toward saturation. If blocking reason changes from symbol-cap to another reason, saturation flag clears immediately.
- **Exits/cancels:** Unaffected. Only new entries are projected down.
- **Counter resets on:** allowed action at Gate 5.5, non-cap block reason (different failure mode breaks chain, also clears saturation flag), or sync-time headroom detection.
- **Proactive recovery:** Saturation is re-evaluated on every sync cycle. When headroom returns, saturation clears immediately — no new entry action required.
- **Logs:** `GRID_V2_RISK_SATURATED_ENTER` / `GRID_V2_RISK_SATURATED_EXIT`.
- **Per-symbol:** Saturation for BTCUSDT does not affect ETHUSDT.

## Effective Desired State Projection (ADR-103)

Three-layer model for reconciler target state under risk constraints:

1. **Theoretical desired state:** What SM/grid window wants absent hard constraints.
2. **Effective desired state:** Legal target after risk projection (`_project_desired_entries()`).
3. **Actual exchange state:** What exists on exchange.

The reconciler diffs **actual vs effective**, never actual vs theoretical.

- **ProjectionMode:** `UNCONSTRAINED` (theoretical == effective), `RISK_CONSTRAINED_PARTIAL` (N closest to reference kept), `RISK_CONSTRAINED_ZERO` (no entries allowed).
- **Partial projection policy:** Keep N entries closest to reference price. Deterministic ordering by `abs(price - reference)`.
- **ReconcileResult fields:**
  - `theoretical_desired_entry_count` — SM wants (before projection)
  - `desired_entry_count` — effective desired (after projection)
  - `projection_mode` — which ProjectionMode was applied
  - `legal_entry_capacity` — risk cap value passed to reconciler
- **Log schema:** `GRID_V2_SYNC_RECONCILER` now includes `theoretical_entries=N effective_entries=M projection=MODE capacity=C`.
- **Pipeline ordering:** theoretical construction → gap detection supplement → legal projection → diff against actual. Gap detection adds to theoretical BEFORE projection, so effective never exceeds legal capacity.
- **Key invariant:** When actual matches effective but not theoretical, reconciler produces zero actions (no churn). `desired_entry_count <= legal_entry_capacity` always holds when capacity is set.

## Reduce-Only Budget Guard v2 (ADR-104)

End-to-end reduce-only exit budget enforcement with reservation model and repair semantics.

### Core invariant
`open_reduce_only_remaining_qty + reserved_qty + new_qty <= position_closeable_qty`

### Budget accounting terms
- **position_closeable_qty:** Direction-aware closeable position. SELL exit → LONG qty only. BUY exit → SHORT qty only. Handles hedge-mode (LONG/SHORT) and one-way mode (BOTH with signed_qty). Includes provable current-tick lot additions.
- **open_reduce_only_remaining_qty:** `sum(qty - filled_qty)` for open reduce-only exits on exchange. Partial fills shrink this.
- **reserved_qty:** Same-tick batch accumulator. Resets each tick.

### Gate 0 v2
At dispatch time, each new reduce-only PLACE is checked via `check_budget()`:
- `ALLOWED`: total within position → dispatch proceeds, reservation incremented.
- `BLOCKED`: total exceeds position → action rejected with `REDUCE_ONLY_BUDGET_EXCEEDED`.
- `POSITION_UNKNOWN`: position qty ≤ 0 → fail-closed.

### Sync-time repair
On each account sync, `detect_surplus_exits()` checks if `open_remaining > position_closeable_qty`. If over-budget:
1. Identify surplus exits.
2. Sort by remaining qty ascending (cancel smallest first).
3. Cancel until `remaining_total <= position`.
4. Log: `GRID_V2_REDUCE_ONLY_REPAIR_START`, `GRID_V2_REDUCE_ONLY_REPAIR_CANCEL`, `GRID_V2_REDUCE_ONLY_REPAIR_CONVERGED`.

### -2022 reject repair path
On Binance `-2022 ReduceOnly Order is rejected`:
1. `(symbol, side)` flagged in `_reduce_only_pending_repair`. Opposite side unaffected.
2. Further reduce-only exits for that `(symbol, side)` blocked at Gate 0.
3. Next sync cycle runs `_reduce_only_repair_on_sync()` — detects and cancels surplus.
4. `CONVERGED` logged and flag cleared ONLY when all repair cancels succeed.
5. If any cancel fails: flag stays set (`DEFERRED`), retry on next sync.
6. If no surplus exists on sync: flag cleared (topology already legal).
No retry storm: blocked immediately after first reject.

### Isolation
Budget is symbol-scoped AND direction-scoped. BTCUSDT SELL budget is independent of ETHUSDT SELL and BTCUSDT BUY.

## Exit Topology Repair (ADR-105)

After budget enforcement (ADR-104), the exit topology repair subsystem converges the actual exchange exit set to the desired legal topology.

### Three-layer model
1. **Desired legal exits:** SM OPEN exit orders constrained by closeable position budget.
2. **Actual exchange exits:** Open reduce-only orders with EXIT CIDs.
3. **Repair diff:** extra (cancel), missing (place if registered), deferred (not yet registered).

### Deterministic ordering
- Cancels: sorted by CID ascending.
- Places: sorted by exit_order_id ascending.
- Deferred: logged explicitly, not silently dropped.

### Trigger reasons
Currently emitted:
- `SYNC_DRIFT`: normal sync cycle detects topology mismatch.
- `REJECT_RECOVERY`: post -2022 latch recovery.

Defined for future use (not currently emitted):
- `BUDGET_OVERRUN`: topology exceeds closeable budget.
- `PARTIAL_FILL_RECOMPUTE`: partial fills changed desired topology.

### Convergence
`is_converged = True` when extra=0, missing=0, deferred=0. Healthy topology produces zero repair actions.
