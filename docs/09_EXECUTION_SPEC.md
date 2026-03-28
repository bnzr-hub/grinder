# GRINDER - Execution Specification

> Order management, fill tracking, and smart routing

> **Status:** PARTIAL (core engine done, smart routing planned)
>
> **Reality (implemented now):**
> - `ExecutionEngine` (`src/grinder/execution/engine.py`, 670 lines) — GridPlan → order actions
> - `ExchangePort` protocol + `BinancePort` / `BinanceFuturesPort` (live order placement)
> - `IdempotentPort` — idempotent order wrapper (420 lines)
> - `ConstraintProvider` — symbol-specific qty rounding, step_size/min_qty (M7-05, ADR-059)
> - `FuturesEvents` — exchange event processing (388 lines)
> - Paper execution engine for testing
> - Order reconciliation (PLACE, CANCEL actions)
> - Prometheus metrics (`grinder_execution_*`)
>
> **Not implemented yet:**
> - `SmartOrderRouter` — amend vs cancel-replace decision logic
> - Batch operations (`_batch_cancel`, `_batch_place`)
> - `estimate_fill_probability()` with volatility/OFI adjustment
> - Fill-probability-based level filtering
> - `FillTracker` / `RoundTrip` as standalone modules
> - `PositionSyncer` — periodic exchange reconciliation
> - `LatencyMonitor` — order latency tracking (p50/p95/p99)
> - `OrderRetryPolicy` — retry with exponential backoff
>
> **Tracking:** Core execution works end-to-end. Advanced routing and tracking are post-launch.
> This spec describes target state beyond current implementation.

---

## 9.1 Order Types

| Type | Use Case | Implementation |
|------|----------|----------------|
| Maker Limit | Default grid orders | `POST_ONLY` flag |
| Taker Limit | Emergency exit | `IOC` with limit price |
| TWAP | Large position reduction | Split over time |
| Iceberg | Hide large orders | Show partial size |

---

## 9.2 Order Lifecycle

```
┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐
│ PENDING │───►│  OPEN   │───►│ FILLED  │───►│COMPLETED│
└────┬────┘    └────┬────┘    └─────────┘    └─────────┘
     │              │
     │              │ Cancel/Expire
     │              ▼
     │         ┌─────────┐
     └────────►│CANCELLED│
               └─────────┘
```

### Order States

```python
class OrderState(Enum):
    PENDING = "PENDING"      # Created, not yet sent
    OPEN = "OPEN"            # Sent and acknowledged
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"        # Fully filled
    CANCELLED = "CANCELLED"  # Cancelled
    REJECTED = "REJECTED"    # Rejected by exchange
    EXPIRED = "EXPIRED"      # TTL expired
```

---

## 9.3 Smart Order Router

```python
class SmartOrderRouter:
    """
    Intelligent order routing with latency optimization.
    """

    async def place_grid_orders(self, plan: GridPlan,
                                 current_orders: list[Order]) -> list[OrderResult]:
        """
        Efficiently update grid orders.

        Strategies:
        1. Amend existing orders if price change is small
        2. Cancel-replace if price change is large
        3. Batch operations for efficiency
        """

        # Calculate required changes
        to_cancel = []
        to_place = []
        to_amend = []

        for level in plan.get_levels():
            existing = self._find_matching_order(current_orders, level)

            if existing is None:
                to_place.append(level)
            elif abs(existing.price - level.price) > AMEND_THRESHOLD_BPS:
                to_cancel.append(existing)
                to_place.append(level)
            elif existing.price != level.price:
                to_amend.append((existing, level))

        # Execute in optimal order
        results = []

        # 1. Cancel orders that are too far
        if to_cancel:
            results.extend(await self._batch_cancel(to_cancel))

        # 2. Amend orders that are close
        if to_amend:
            results.extend(await self._batch_amend(to_amend))

        # 3. Place new orders
        if to_place:
            results.extend(await self._batch_place(to_place))

        return results

    async def _batch_cancel(self, orders: list[Order]) -> list[OrderResult]:
        """Cancel multiple orders efficiently."""
        # Use batch cancel endpoint if available
        if len(orders) > 5 and self.connector.supports_batch_cancel:
            return await self.connector.batch_cancel([o.order_id for o in orders])

        # Otherwise parallel individual cancels
        tasks = [self.connector.cancel_order(o.order_id) for o in orders]
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def _batch_place(self, levels: list[GridLevel]) -> list[OrderResult]:
        """Place multiple orders efficiently."""
        # Convert to order requests
        requests = [self._level_to_order(level) for level in levels]

        # Use batch place if available
        if len(requests) > 3 and self.connector.supports_batch_place:
            return await self.connector.batch_place(requests)

        # Otherwise parallel individual places
        tasks = [self.connector.place_order(req) for req in requests]
        return await asyncio.gather(*tasks, return_exceptions=True)
```

---

## 9.4 Fill Probability Model

```python
def estimate_fill_probability(order_price: Decimal,
                               side: str,
                               features: dict,
                               horizon_s: int = 60) -> float:
    """
    Estimate probability of limit order filling within horizon.

    Factors:
    - Distance from mid
    - Recent volatility
    - Order flow direction
    - Queue position (approximated)
    """

    mid = features["mid"]

    # Distance in bps
    if side == "BUY":
        distance_bps = (mid - float(order_price)) / mid * 10000
    else:
        distance_bps = (float(order_price) - mid) / mid * 10000

    # Expected move from volatility
    expected_move_bps = features["natr_14_5m"] * 10000 * np.sqrt(horizon_s / 300)

    # Base probability from distance vs expected move
    if distance_bps <= 0:
        base_prob = 0.9  # Already through mid
    elif distance_bps < expected_move_bps * 0.5:
        base_prob = 0.7
    elif distance_bps < expected_move_bps:
        base_prob = 0.5
    elif distance_bps < expected_move_bps * 1.5:
        base_prob = 0.3
    else:
        base_prob = 0.1

    # Adjust for order flow
    ofi = features.get("ofi_zscore", 0)
    if side == "BUY" and ofi < -1:
        base_prob *= 1.3  # Selling pressure helps buys
    elif side == "SELL" and ofi > 1:
        base_prob *= 1.3
    elif side == "BUY" and ofi > 1:
        base_prob *= 0.7  # Buying pressure hurts buys (competition)
    elif side == "SELL" and ofi < -1:
        base_prob *= 0.7

    return np.clip(base_prob, 0.05, 0.95)
```

---

## 9.5 Grid Level Management

```python
@dataclass
class GridLevel:
    """Single grid level."""
    level_id: str
    side: str  # "BUY" or "SELL"
    price: Decimal
    size: Decimal
    fill_prob: float
    expected_profit_bps: float
    reason_codes: list[str]

class GridManager:
    """Manage active grid levels."""

    def __init__(self, config: GridConfig):
        self.config = config
        self.levels: dict[str, GridLevel] = {}
        self.orders: dict[str, Order] = {}  # level_id -> Order

    def compute_levels(self, plan: GridPlan, features: dict) -> list[GridLevel]:
        """Compute grid levels from plan."""
        levels = []
        center = plan.center_price

        # Apply skew
        skewed_center = center * (1 + plan.skew_bps / 10000)

        # Generate levels
        for i in range(1, plan.levels_up + 1):
            price = skewed_center * (1 + i * plan.spacing_bps / 10000)
            size = plan.size_schedule[min(i-1, len(plan.size_schedule)-1)]

            fill_prob = estimate_fill_probability(price, "SELL", features)

            # Skip if fill probability too low
            if fill_prob < self.config.min_fill_prob:
                continue

            levels.append(GridLevel(
                level_id=f"SELL_{i}",
                side="SELL",
                price=Decimal(str(round(price, 8))),
                size=size,
                fill_prob=fill_prob,
                expected_profit_bps=self._calc_expected_profit(plan, i),
                reason_codes=[f"LEVEL_{i}"]
            ))

        for i in range(1, plan.levels_down + 1):
            price = skewed_center * (1 - i * plan.spacing_bps / 10000)
            size = plan.size_schedule[min(i-1, len(plan.size_schedule)-1)]

            fill_prob = estimate_fill_probability(price, "BUY", features)

            if fill_prob < self.config.min_fill_prob:
                continue

            levels.append(GridLevel(
                level_id=f"BUY_{i}",
                side="BUY",
                price=Decimal(str(round(price, 8))),
                size=size,
                fill_prob=fill_prob,
                expected_profit_bps=self._calc_expected_profit(plan, i),
                reason_codes=[f"LEVEL_{i}"]
            ))

        return levels
```

---

## 9.6 Fill Tracking

```python
@dataclass
class Fill:
    """Recorded fill event."""
    fill_id: str
    order_id: str
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    commission: Decimal
    commission_asset: str
    ts: int
    is_maker: bool

@dataclass
class RoundTrip:
    """Complete round-trip (entry + exit)."""
    rt_id: str
    symbol: str
    entry_fill: Fill
    exit_fill: Fill
    gross_pnl: Decimal
    commission: Decimal
    net_pnl: Decimal
    hold_time_ms: int
    policy: str

class FillTracker:
    """Track fills and compute round-trips."""

    def __init__(self):
        self.fills: list[Fill] = []
        self.round_trips: list[RoundTrip] = []
        self.pending_entries: dict[str, list[Fill]] = defaultdict(list)

    def record_fill(self, fill: Fill) -> RoundTrip | None:
        """Record fill and check for round-trip completion."""
        self.fills.append(fill)

        # Check if this completes a round-trip
        pending = self.pending_entries[fill.symbol]

        if not pending:
            # This is an entry
            pending.append(fill)
            return None

        # Check for matching exit
        entry = self._find_matching_entry(fill, pending)
        if entry:
            pending.remove(entry)
            rt = self._create_round_trip(entry, fill)
            self.round_trips.append(rt)
            return rt

        # Not a match - add as new entry
        pending.append(fill)
        return None

    def _create_round_trip(self, entry: Fill, exit: Fill) -> RoundTrip:
        """Create round-trip from entry and exit fills."""
        # Calculate P&L
        if entry.side == "BUY":
            gross_pnl = (exit.price - entry.price) * entry.quantity
        else:
            gross_pnl = (entry.price - exit.price) * entry.quantity

        commission = entry.commission + exit.commission

        return RoundTrip(
            rt_id=f"RT_{entry.fill_id}_{exit.fill_id}",
            symbol=entry.symbol,
            entry_fill=entry,
            exit_fill=exit,
            gross_pnl=gross_pnl,
            commission=commission,
            net_pnl=gross_pnl - commission,
            hold_time_ms=exit.ts - entry.ts,
            policy=entry.policy if hasattr(entry, "policy") else "UNKNOWN"
        )
```

---

## 9.7 Position Synchronization

```python
class PositionSyncer:
    """Synchronize local position with exchange."""

    def __init__(self, connector: ExchangeConnector):
        self.connector = connector
        self.local_positions: dict[str, Position] = {}
        self.last_sync_ts: int = 0

    async def sync(self) -> dict[str, PositionDiff]:
        """Sync and return differences."""
        exchange_positions = await self.connector.get_positions()
        diffs = {}

        for symbol, exchange_pos in exchange_positions.items():
            local_pos = self.local_positions.get(symbol)

            if local_pos is None:
                # New position from exchange
                diffs[symbol] = PositionDiff(
                    symbol=symbol,
                    local_qty=Decimal(0),
                    exchange_qty=exchange_pos.quantity,
                    diff=exchange_pos.quantity,
                    action="RECONCILE"
                )
            elif local_pos.quantity != exchange_pos.quantity:
                # Mismatch
                diff = exchange_pos.quantity - local_pos.quantity
                diffs[symbol] = PositionDiff(
                    symbol=symbol,
                    local_qty=local_pos.quantity,
                    exchange_qty=exchange_pos.quantity,
                    diff=diff,
                    action="RECONCILE" if abs(diff) > 0.01 else "IGNORE"
                )

            # Update local
            self.local_positions[symbol] = exchange_pos

        self.last_sync_ts = int(time.time() * 1000)
        return diffs

    async def reconcile(self, diff: PositionDiff) -> None:
        """Reconcile position difference."""
        if diff.action == "IGNORE":
            return

        logger.warning(f"Position mismatch for {diff.symbol}: "
                      f"local={diff.local_qty}, exchange={diff.exchange_qty}")

        # Update local to match exchange
        self.local_positions[diff.symbol] = Position(
            symbol=diff.symbol,
            quantity=diff.exchange_qty,
            # ... other fields from exchange
        )

        # Alert if significant
        if abs(diff.diff) > 1.0:  # > 1 unit
            await self.alert_manager.send_alert(
                level="WARNING",
                message=f"Position reconciliation: {diff.symbol}",
                context={"diff": str(diff.diff)}
            )
```

---

## 9.8 Latency Monitoring

```python
class LatencyMonitor:
    """Monitor and track order latencies."""

    def __init__(self):
        self.samples: deque[float] = deque(maxlen=1000)
        self.order_send_times: dict[str, int] = {}

    def record_send(self, order_id: str, ts: int) -> None:
        """Record order send time."""
        self.order_send_times[order_id] = ts

    def record_ack(self, order_id: str, ts: int) -> None:
        """Record order acknowledgment."""
        if order_id not in self.order_send_times:
            return

        latency = ts - self.order_send_times[order_id]
        self.samples.append(latency)
        del self.order_send_times[order_id]

        # Alert on high latency
        if latency > 500:  # > 500ms
            logger.warning(f"High order latency: {latency}ms for {order_id}")

    def get_stats(self) -> dict:
        """Get latency statistics."""
        if not self.samples:
            return {}

        sorted_samples = sorted(self.samples)
        return {
            "min": sorted_samples[0],
            "max": sorted_samples[-1],
            "mean": sum(self.samples) / len(self.samples),
            "p50": sorted_samples[len(sorted_samples) // 2],
            "p95": sorted_samples[int(len(sorted_samples) * 0.95)],
            "p99": sorted_samples[int(len(sorted_samples) * 0.99)],
        }
```

---

## 9.9 Order Retry Logic

```python
class OrderRetryPolicy:
    """Retry policy for failed orders."""

    def __init__(self, config: RetryConfig):
        self.config = config

    async def execute_with_retry(self,
                                  operation: Callable,
                                  *args, **kwargs) -> OrderResult:
        """Execute order operation with retry."""
        last_error = None

        for attempt in range(self.config.max_retries):
            try:
                result = await asyncio.wait_for(
                    operation(*args, **kwargs),
                    timeout=self.config.timeout_s
                )
                return result

            except asyncio.TimeoutError:
                last_error = TimeoutError("Order timeout")
                logger.warning(f"Order timeout, attempt {attempt + 1}")

            except RateLimitError as e:
                # Back off on rate limit
                wait_time = e.retry_after or (2 ** attempt)
                logger.warning(f"Rate limited, waiting {wait_time}s")
                await asyncio.sleep(wait_time)
                last_error = e

            except OrderRejectedError as e:
                # Don't retry rejections
                logger.error(f"Order rejected: {e}")
                raise

            except Exception as e:
                last_error = e
                logger.error(f"Order error: {e}, attempt {attempt + 1}")

            # Exponential backoff
            await asyncio.sleep(self.config.base_delay_ms * (2 ** attempt) / 1000)

        raise OrderFailedError(f"Order failed after {self.config.max_retries} retries: {last_error}")
```

---

## 9.10 Execution Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `orders_placed_total` | Counter | Total orders placed |
| `orders_filled_total` | Counter | Total orders filled |
| `orders_cancelled_total` | Counter | Total orders cancelled |
| `orders_rejected_total` | Counter | Total orders rejected |
| `order_latency_ms` | Histogram | Order round-trip latency |
| `fill_rate` | Gauge | Fill rate (filled/placed) |
| `round_trips_total` | Counter | Total round-trips completed |
| `round_trip_pnl_bps` | Histogram | Round-trip P&L distribution |

## Reduce-Only Budget Guard v2 (ADR-104)

Gate 0 in `_process_action()` enforces reduce-only exit budget before dispatch:
- `BudgetSnapshot` computed from exchange truth + same-tick reservations.
- `check_budget()` returns ALLOWED/BLOCKED/POSITION_UNKNOWN.
- Batch accumulator tracks same-tick reservations, reset per tick.
- Sync-time `detect_surplus_exits()` repairs over-budget topology.
- See `docs/10_RISK_SPEC.md` for detailed accounting model.
