# GRINDER - Constants & Default Parameters

> All tunable parameters with default values and safe ranges

---

## 1. Prefilter Constants

### 1.1 Hard Gates

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `SPREAD_MAX_BPS` | 15.0 | 5-50 | Max spread to consider symbol |
| `VOL_MIN_24H_USD` | 10,000,000 | 1M-100M | Min 24h volume |
| `VOL_MIN_LAST_12X5M_USD` | 2,000,000 | 500K-20M | Min rolling 12×5m quote volume |
| `TRADE_COUNT_MIN_1M` | 100 | 10-1000 | Min trades per minute |
| `OI_MIN_USD` | 5,000,000 | 1M-50M | Min open interest |

```python
# prefilter/constants.py

SPREAD_MAX_BPS = 15.0
VOL_MIN_24H_USD = 10_000_000
VOL_MIN_LAST_12X5M_USD = 2_000_000
TRADE_COUNT_MIN_1M = 100
OI_MIN_USD = 5_000_000
```

### 1.2 Scoring Weights

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `W_ACTIVITY` | 0.30 | 0.1-0.5 | Weight for activity score |
| `W_VOLATILITY` | 0.25 | 0.1-0.4 | Weight for volatility score |
| `W_COST` | 0.25 | 0.1-0.4 | Weight for cost score |
| `W_IDIOSYNCRATIC` | 0.20 | 0.05-0.3 | Weight for idiosyncratic score |

```python
# prefilter/constants.py

W_ACTIVITY = 0.30
W_VOLATILITY = 0.25
W_COST = 0.25
W_IDIOSYNCRATIC = 0.20
```

### 1.3 Top-K Selection

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `K_SYMBOLS` | 10 | 5-50 | Number of symbols to select |
| `T_ENTER_S` | 30 | 10-120 | Seconds to stay in top-K before enabling L2 |
| `T_HOLD_S` | 300 | 60-600 | Min hold time before dropping from top-K |
| `T_RERANK_S` | 60 | 30-300 | Re-ranking interval |

```python
# prefilter/constants.py

K_SYMBOLS = 10
T_ENTER_S = 30
T_HOLD_S = 300
T_RERANK_S = 60
```

---

## 2. Toxicity Constants

### 2.1 Thresholds

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `TOX_LOW` | 30.0 | 10-40 | Toxicity below this = LOW regime |
| `TOX_MID` | 60.0 | 40-70 | Toxicity below this = MID regime |
| `TOX_HIGH` | 60.0 | 50-80 | Toxicity above this = HIGH regime |

```python
# toxicity/constants.py

TOX_LOW = 30.0
TOX_MID = 60.0
TOX_HIGH = 60.0  # Same as MID threshold for binary decision
```

### 2.2 Component Weights

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `TOX_W_VPIN` | 0.25 | 0.1-0.4 | VPIN weight in composite |
| `TOX_W_KYLE` | 0.20 | 0.1-0.3 | Kyle proxy weight |
| `TOX_W_AMIHUD` | 0.15 | 0.05-0.25 | Amihud weight |
| `TOX_W_SPREAD` | 0.20 | 0.1-0.3 | Spread z-score weight |
| `TOX_W_OFI` | 0.20 | 0.1-0.3 | OFI z-score weight |

```python
# toxicity/constants.py

TOX_W_VPIN = 0.25
TOX_W_KYLE = 0.20
TOX_W_AMIHUD = 0.15
TOX_W_SPREAD = 0.20
TOX_W_OFI = 0.20
```

### 2.3 Feature Parameters

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `VPIN_BUCKETS` | 50 | 20-100 | Number of volume buckets for VPIN |
| `VPIN_BUCKET_SIZE_USD` | 10000 | 1K-100K | USD volume per bucket |
| `OFI_ZSCORE_CLIP` | 5.0 | 3.0-10.0 | Max absolute z-score |

```python
# toxicity/constants.py

VPIN_BUCKETS = 50
VPIN_BUCKET_SIZE_USD = 10000
OFI_ZSCORE_CLIP = 5.0
```

---

## 3. Grid Policy Constants

### 3.1 Range Grid (Bilateral)

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `RANGE_SPACING_BPS` | 10.0 | 5-30 | Base spacing between levels |
| `RANGE_LEVELS` | 5 | 3-10 | Levels per side |
| `RANGE_SIZE_USD` | 100 | 50-1000 | USD per level |
| `RANGE_MAX_SKEW_BPS` | 20.0 | 5-50 | Max inventory skew adjustment |
| `RANGE_SPREAD_MAX_BPS` | 8.0 | 3-15 | Max spread for range trading |

```python
# policies/constants.py

RANGE_SPACING_BPS = 10.0
RANGE_LEVELS = 5
RANGE_SIZE_USD = 100
RANGE_MAX_SKEW_BPS = 20.0
RANGE_SPREAD_MAX_BPS = 8.0
```

### 3.2 Trend Follower (Unidirectional)

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `TREND_SPACING_BPS` | 15.0 | 10-40 | Spacing for trend grid |
| `TREND_LEVELS` | 4 | 2-8 | Levels in trend direction |
| `TREND_SIZE_USD` | 150 | 100-500 | USD per level |
| `MOMENTUM_TREND_THRESHOLD` | 2.0 | 1.0-3.0 | Momentum z-score for trend |
| `MOMENTUM_EXHAUSTION` | 4.0 | 3.0-5.0 | Momentum z-score for exhaustion |

```python
# policies/constants.py

TREND_SPACING_BPS = 15.0
TREND_LEVELS = 4
TREND_SIZE_USD = 150
MOMENTUM_TREND_THRESHOLD = 2.0
MOMENTUM_EXHAUSTION = 4.0
```

### 3.3 Funding Harvester

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `FUNDING_THRESHOLD` | 0.0005 | 0.0002-0.001 | Min funding rate to harvest |
| `FUNDING_EXTREME` | 0.001 | 0.0005-0.005 | Extreme funding threshold |
| `FUNDING_SPACING_BPS` | 12.0 | 8-20 | Spacing for funding grid |
| `FUNDING_LEVELS` | 4 | 3-6 | Levels per side |

```python
# policies/constants.py

FUNDING_THRESHOLD = 0.0005  # 0.05% per 8h
FUNDING_EXTREME = 0.001     # 0.1% per 8h
FUNDING_SPACING_BPS = 12.0
FUNDING_LEVELS = 4
```

### 3.4 Liquidation Catcher

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `LIQ_SURGE_MULT` | 5.0 | 3.0-10.0 | Multiplier vs mean for surge |
| `LIQ_COOLDOWN_TICKS` | 30 | 10-60 | Ticks to wait after surge |
| `LIQ_RECOVERY_SPACING_BPS` | 20.0 | 15-40 | Wider spacing for volatility |
| `LIQ_RECOVERY_SIZE_USD` | 80 | 50-200 | Smaller size due to risk |

```python
# policies/constants.py

LIQ_SURGE_MULT = 5.0
LIQ_COOLDOWN_TICKS = 30
LIQ_RECOVERY_SPACING_BPS = 20.0
LIQ_RECOVERY_SIZE_USD = 80
```

### 3.5 Mean Reversion Sniper

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `MR_EXTREME_THRESHOLD` | 3.0 | 2.0-4.0 | Momentum z-score for extreme |
| `MR_SPACING_BPS` | 8.0 | 5-15 | Tight spacing for precision |
| `MR_LEVELS` | 3 | 2-5 | Few levels, small risk |
| `MR_SIZE_USD` | 60 | 30-150 | Small size per level |

```python
# policies/constants.py

MR_EXTREME_THRESHOLD = 3.0
MR_SPACING_BPS = 8.0
MR_LEVELS = 3
MR_SIZE_USD = 60
```

### 3.6 Throttle Parameters

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `THROTTLE_SPACING_MULT` | 1.5 | 1.2-2.0 | Spacing multiplier when throttled |
| `THROTTLE_SIZE_MULT` | 0.5 | 0.3-0.7 | Size multiplier when throttled |
| `THROTTLE_LEVELS_DIV` | 2 | 2-4 | Level divisor when throttled |

```python
# policies/constants.py

THROTTLE_SPACING_MULT = 1.5
THROTTLE_SIZE_MULT = 0.5
THROTTLE_LEVELS_DIV = 2
```


### 3.7 Adaptive Grid Controller (planned)

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `STEP_BASE_BPS` | 8.0 | 4-20 | Base grid step floor |
| `STEP_MIN_BPS` | 4.0 | 2-10 | Hard min step |
| `STEP_MAX_BPS` | 40.0 | 20-100 | Hard max step |
| `STEP_VOL_MULT` | 100.0 | 50-200 | Convert NATR units to bps |
| `STEP_SPREAD_MULT` | 1.0 | 0.5-2.0 | Spread contribution multiplier |
| `STEP_EMA_ALPHA` | 0.2 | 0.05-0.5 | Smoothing factor for step |
| `CENTER_DRIFT_BPS` | 15.0 | 5-50 | Center drift threshold |
| `STEP_RESET_DELTA_BPS` | 6.0 | 2-20 | Step delta threshold for SOFT reset |
| `REGIME_MIN_HOLD_TICKS` | 10 | 5-50 | Min ticks to hold regime |
| `REGIME_SWITCH_COOLDOWN_TICKS` | 5 | 3-30 | Cooldown ticks after switch |
| `TOX_PAUSE` | 60.0 | 50-80 | Tox score that forces pause/toxic regime |
| `SPREAD_PAUSE_BPS` | 12.0 | 8-25 | Spread that forces thin book / pause |
| `DEPTH_MIN_USD` | 250000 | 50k-1M | Top-5 depth min USD for normal ops |
| `SHOCK_BPS` | 80.0 | 30-200 | 1m jump bps that triggers shock regime |
| `TREND_SLOPE_TH` | 0.0005 | 0.0002-0.002 | Trend slope threshold |

```python
# controller/constants.py  (planned)

STEP_BASE_BPS = 8.0
STEP_MIN_BPS = 4.0
STEP_MAX_BPS = 40.0
STEP_VOL_MULT = 100.0
STEP_SPREAD_MULT = 1.0
STEP_EMA_ALPHA = 0.2

CENTER_DRIFT_BPS = 15.0
STEP_RESET_DELTA_BPS = 6.0

REGIME_MIN_HOLD_TICKS = 10
REGIME_SWITCH_COOLDOWN_TICKS = 5

TOX_PAUSE = 60.0
SPREAD_PAUSE_BPS = 12.0
DEPTH_MIN_USD = 250_000

SHOCK_BPS = 80.0
TREND_SLOPE_TH = 0.0005
```

---

## 4. Risk Management Constants

### 4.1 Inventory Limits

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `INV_MAX_PER_SYMBOL_USD` | 5000 | 1K-50K | Max notional per symbol |
| `INV_MAX_TOTAL_USD` | 50000 | 10K-500K | Max total notional |
| `INV_MAX_PCT` | 1.0 | 0.5-5.0 | Max inventory as % of capital |
| `INV_WARN_PCT` | 0.7 | 0.5-0.9 | Warning threshold |

```python
# risk/constants.py

INV_MAX_PER_SYMBOL_USD = 5000
INV_MAX_TOTAL_USD = 50000
INV_MAX_PCT = 1.0
INV_WARN_PCT = 0.7
```

### 4.2 Drawdown Limits

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `DD_MAX_SESSION_PCT` | 3.0 | 1.0-5.0 | Max session drawdown |
| `DD_MAX_DAILY_PCT` | 5.0 | 2.0-10.0 | Max daily drawdown |
| `DD_MAX_WEEKLY_PCT` | 10.0 | 5.0-15.0 | Max weekly drawdown |
| `DD_WARN_MULT` | 0.7 | 0.5-0.8 | Warning at this fraction |

```python
# risk/constants.py

DD_MAX_SESSION_PCT = 3.0
DD_MAX_DAILY_PCT = 5.0
DD_MAX_WEEKLY_PCT = 10.0
DD_WARN_MULT = 0.7
```

### 4.3 Loss Limits

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `LOSS_STREAK_MAX` | 5 | 3-10 | Max consecutive losing RTs |
| `LOSS_RT_MAX_BPS` | -50 | -100 to -20 | Max loss per RT (emergency exit) |
| `LOSS_SESSION_MAX_USD` | 500 | 100-2000 | Max session loss |

```python
# risk/constants.py

LOSS_STREAK_MAX = 5
LOSS_RT_MAX_BPS = -50
LOSS_SESSION_MAX_USD = 500
```

### 4.4 Emergency Parameters

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `EMERGENCY_EXIT_SPACING_BPS` | 5.0 | 3-10 | Tight spacing for fast exit |
| `EMERGENCY_EXIT_LEVELS` | 5 | 3-10 | More levels for fast exit |
| `EMERGENCY_EXIT_SIZE_MULT` | 2.0 | 1.5-3.0 | Larger size for fast exit |

```python
# risk/constants.py

EMERGENCY_EXIT_SPACING_BPS = 5.0
EMERGENCY_EXIT_LEVELS = 5
EMERGENCY_EXIT_SIZE_MULT = 2.0
```

---

## 5. Execution Constants

### 5.1 Order Parameters

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `ORDER_MIN_SIZE_USD` | 10 | 5-50 | Min order size |
| `ORDER_MAX_SIZE_USD` | 10000 | 1K-100K | Max order size |
| `ORDER_TIMEOUT_MS` | 5000 | 1K-30K | Order placement timeout |
| `ORDER_RETRY_COUNT` | 3 | 1-5 | Max retries on failure |
| `ORDER_RETRY_DELAY_MS` | 100 | 50-500 | Delay between retries |

```python
# execution/constants.py

ORDER_MIN_SIZE_USD = 10
ORDER_MAX_SIZE_USD = 10000
ORDER_TIMEOUT_MS = 5000
ORDER_RETRY_COUNT = 3
ORDER_RETRY_DELAY_MS = 100
```

### 5.2 Grid Refresh

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `GRID_REFRESH_INTERVAL_MS` | 1000 | 500-5000 | Grid recalculation interval |
| `GRID_PRICE_TOLERANCE_BPS` | 2.0 | 0.5-5.0 | Don't refresh if price moved less |
| `GRID_MIN_LEVELS_ACTIVE` | 1 | 1-3 | Min levels to keep active |

```python
# execution/constants.py

GRID_REFRESH_INTERVAL_MS = 1000
GRID_PRICE_TOLERANCE_BPS = 2.0
GRID_MIN_LEVELS_ACTIVE = 1
```

### 5.3 Fill Probability

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `FILL_PROB_MIN` | 0.10 | 0.05-0.3 | Min fill probability to place order |
| `FILL_PROB_DECAY_RATE` | 0.15 | 0.05-0.3 | Exponential decay per level |

```python
# execution/constants.py

FILL_PROB_MIN = 0.10
FILL_PROB_DECAY_RATE = 0.15
```

---

## 6. Data Quality Constants

### 6.1 Staleness Thresholds

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `STALE_BOOK_TICKER_MS` | 2000 | 1K-5K | bookTicker stale threshold |
| `STALE_DEPTH_MS` | 3000 | 2K-10K | depth stale threshold |
| `STALE_AGG_TRADE_MS` | 5000 | 2K-15K | aggTrade stale threshold |
| `STALE_FUNDING_S` | 300 | 60-600 | Funding rate stale threshold |

```python
# data/constants.py

STALE_BOOK_TICKER_MS = 2000
STALE_DEPTH_MS = 3000
STALE_AGG_TRADE_MS = 5000
STALE_FUNDING_S = 300
```

### 6.2 Outlier Detection

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `PRICE_JUMP_MAX_PCT` | 5.0 | 1.0-10.0 | Max single-tick price jump |
| `VOLUME_SPIKE_MULT` | 100.0 | 20-500 | Volume spike multiplier |
| `SPREAD_ANOMALY_MULT` | 10.0 | 5-50 | Spread anomaly multiplier |

```python
# data/constants.py

PRICE_JUMP_MAX_PCT = 5.0
VOLUME_SPIKE_MULT = 100.0
SPREAD_ANOMALY_MULT = 10.0
```

### 6.3 Reconnection

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `WS_RECONNECT_DELAY_MS` | 1000 | 500-5K | Initial reconnect delay |
| `WS_RECONNECT_MAX_DELAY_MS` | 30000 | 10K-60K | Max reconnect delay |
| `WS_RECONNECT_MULTIPLIER` | 2.0 | 1.5-3.0 | Exponential backoff multiplier |
| `WS_PING_INTERVAL_MS` | 30000 | 10K-60K | Ping interval |
| `WS_PONG_TIMEOUT_MS` | 10000 | 5K-30K | Pong timeout |

```python
# data/constants.py

WS_RECONNECT_DELAY_MS = 1000
WS_RECONNECT_MAX_DELAY_MS = 30000
WS_RECONNECT_MULTIPLIER = 2.0
WS_PING_INTERVAL_MS = 30000
WS_PONG_TIMEOUT_MS = 10000
```

---

## 7. Feature Calculation Constants

### 7.1 Rolling Windows

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `WINDOW_1S_TICKS` | 10 | 5-20 | Ticks per 1s window (at 100ms) |
| `WINDOW_10S_TICKS` | 100 | 50-200 | Ticks per 10s window |
| `WINDOW_1M_TICKS` | 600 | 300-1200 | Ticks per 1m window |
| `WINDOW_5M_TICKS` | 3000 | 1500-6000 | Ticks per 5m window |

```python
# features/constants.py

WINDOW_1S_TICKS = 10
WINDOW_10S_TICKS = 100
WINDOW_1M_TICKS = 600
WINDOW_5M_TICKS = 3000
```

### 7.2 Normalization

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `ZSCORE_WINDOW` | 300 | 100-1000 | Rolling z-score window |
| `ZSCORE_CLIP` | 5.0 | 3.0-10.0 | Z-score clipping value |
| `ZSCORE_MIN_SAMPLES` | 30 | 10-100 | Min samples before normalizing |
| `ZSCORE_EPSILON` | 1e-8 | 1e-10 to 1e-6 | Division epsilon |

```python
# features/constants.py

ZSCORE_WINDOW = 300
ZSCORE_CLIP = 5.0
ZSCORE_MIN_SAMPLES = 30
ZSCORE_EPSILON = 1e-8
```

### 7.3 Beta Calculation

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `BETA_WINDOW` | 60 | 30-120 | Rolling regression window |
| `BETA_MIN_SAMPLES` | 30 | 10-60 | Min samples for regression |
| `BETA_DEFAULT` | 1.0 | 0.5-2.0 | Default beta when insufficient data |

```python
# features/constants.py

BETA_WINDOW = 60
BETA_MIN_SAMPLES = 30
BETA_DEFAULT = 1.0
```

---

## 8. L2 Feature Constants

### 8.1 Depth Subscription

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `DEPTH_LEVELS` | 5 | 5-20 | Number of depth levels |
| `DEPTH_UPDATE_MS` | 250 | 100-1000 | Depth update interval |
| `DEPTH_IMBALANCE_LEVELS` | 5 | 3-10 | Levels for imbalance calc |

```python
# features/constants.py

DEPTH_LEVELS = 5
DEPTH_UPDATE_MS = 250
DEPTH_IMBALANCE_LEVELS = 5
```

### 8.2 Wall Detection

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `WALL_SIZE_MULT` | 3.0 | 2.0-5.0 | Min multiplier vs mean |
| `WALL_MAX_DISTANCE_BPS` | 50.0 | 20-100 | Max distance from mid |
| `WALL_MIN_LEVELS` | 3 | 2-5 | Min levels for detection |
| `WALL_PERSISTENCE_TICKS` | 10 | 5-30 | Min ticks wall must persist |

```python
# features/constants.py

WALL_SIZE_MULT = 3.0
WALL_MAX_DISTANCE_BPS = 50.0
WALL_MIN_LEVELS = 3
WALL_PERSISTENCE_TICKS = 10
```

### 8.3 Spoofing Detection

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `SPOOF_LARGE_ORDER_MULT` | 5.0 | 3.0-10.0 | Large order multiplier |
| `SPOOF_FLICKER_MS` | 500 | 200-2000 | Max lifetime for flicker |
| `SPOOF_CANCEL_RATE_WARN` | 0.8 | 0.5-0.95 | Cancel rate warning |
| `SPOOF_SCORE_THRESHOLD` | 50 | 30-80 | Score to flag as suspicious |

```python
# features/constants.py

SPOOF_LARGE_ORDER_MULT = 5.0
SPOOF_FLICKER_MS = 500
SPOOF_CANCEL_RATE_WARN = 0.8
SPOOF_SCORE_THRESHOLD = 50
```

---

## 9. Backtest Constants

### 9.1 Cost Model

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `FEE_MAKER_BPS` | 2.0 | 0-5 | Maker fee in bps |
| `FEE_TAKER_BPS` | 4.0 | 2-10 | Taker fee in bps |
| `SLIPPAGE_BASE_BPS` | 1.0 | 0.5-5 | Base slippage estimate |
| `SLIPPAGE_IMPACT_MULT` | 0.1 | 0.05-0.5 | Size impact multiplier |

```python
# backtest/constants.py

FEE_MAKER_BPS = 2.0
FEE_TAKER_BPS = 4.0
SLIPPAGE_BASE_BPS = 1.0
SLIPPAGE_IMPACT_MULT = 0.1
```

### 9.2 Simulation Parameters

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `SIM_INITIAL_CAPITAL_USD` | 10000 | 1K-1M | Starting capital |
| `SIM_FILL_LATENCY_MS` | 50 | 10-500 | Simulated fill latency |
| `SIM_PARTIAL_FILL_PROB` | 0.1 | 0-0.5 | Partial fill probability |

```python
# backtest/constants.py

SIM_INITIAL_CAPITAL_USD = 10000
SIM_FILL_LATENCY_MS = 50
SIM_PARTIAL_FILL_PROB = 0.1
```

---

## 10. Observability Constants

### 10.1 Metrics

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `METRICS_FLUSH_INTERVAL_S` | 10 | 5-60 | Metrics flush interval |
| `METRICS_RETENTION_DAYS` | 30 | 7-90 | Metrics retention |
| `HISTOGRAM_BUCKETS` | [1,5,10,25,50,100,250,500,1000] | - | Latency histogram buckets |

### 10.2 Logging

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `LOG_LEVEL` | "INFO" | DEBUG-ERROR | Default log level |
| `LOG_ROTATION_MB` | 100 | 10-1000 | Log file rotation size |
| `LOG_RETENTION_FILES` | 10 | 3-50 | Number of rotated files to keep |

### 10.3 Alerts

| Constant | Default | Range | Description |
|----------|---------|-------|-------------|
| `ALERT_COOLDOWN_S` | 60 | 30-300 | Min time between same alerts |
| `ALERT_DD_WARN_PCT` | 70 | 50-90 | DD warning threshold (% of max) |
| `ALERT_LATENCY_P99_MS` | 500 | 100-2000 | P99 latency alert threshold |

---

## 11. Configuration Loading

```python
# config/loader.py

from dataclasses import dataclass, field
from pathlib import Path
import yaml

@dataclass
class GrinderConfig:
    """Complete GRINDER configuration."""

    # Prefilter
    spread_max_bps: float = 15.0
    vol_min_24h_usd: float = 10_000_000
    k_symbols: int = 10

    # Toxicity
    tox_low: float = 30.0
    tox_mid: float = 60.0
    tox_high: float = 60.0

    # Grid
    range_spacing_bps: float = 10.0
    range_levels: int = 5
    range_size_usd: float = 100.0

    # Risk
    dd_max_session_pct: float = 3.0
    dd_max_daily_pct: float = 5.0
    inv_max_per_symbol_usd: float = 5000.0

    # Execution
    order_min_size_usd: float = 10.0
    grid_refresh_interval_ms: int = 1000

    # Data
    stale_book_ticker_ms: int = 2000
    ws_reconnect_delay_ms: int = 1000

    @classmethod
    def from_yaml(cls, path: Path) -> "GrinderConfig":
        """Load config from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    @classmethod
    def from_env(cls) -> "GrinderConfig":
        """Load config from environment variables."""
        import os

        config = cls()
        for field_name in config.__dataclass_fields__:
            env_name = f"GRINDER_{field_name.upper()}"
            if env_name in os.environ:
                field_type = config.__dataclass_fields__[field_name].type
                value = os.environ[env_name]
                if field_type == float:
                    setattr(config, field_name, float(value))
                elif field_type == int:
                    setattr(config, field_name, int(value))
                else:
                    setattr(config, field_name, value)
        return config
```

---

## 12. Environment-Specific Defaults

### 12.1 Development

```yaml
# config/dev.yaml
spread_max_bps: 20.0
vol_min_24h_usd: 1000000
k_symbols: 5
dd_max_session_pct: 5.0
dd_max_daily_pct: 10.0
inv_max_per_symbol_usd: 1000.0
range_size_usd: 50.0
```

### 12.2 Paper Trading

```yaml
# config/paper.yaml
spread_max_bps: 15.0
vol_min_24h_usd: 5000000
k_symbols: 10
dd_max_session_pct: 3.0
dd_max_daily_pct: 5.0
inv_max_per_symbol_usd: 5000.0
range_size_usd: 100.0
```

### 12.3 Production

```yaml
# config/prod.yaml
spread_max_bps: 10.0
vol_min_24h_usd: 10000000
k_symbols: 10
dd_max_session_pct: 2.0
dd_max_daily_pct: 3.0
inv_max_per_symbol_usd: 10000.0
range_size_usd: 200.0
```

---

## 13. Constant Validation

```python
# config/validation.py

def validate_config(config: GrinderConfig) -> list[str]:
    """Validate configuration, return list of errors."""
    errors = []

    # Prefilter
    if config.spread_max_bps <= 0:
        errors.append("spread_max_bps must be positive")
    if config.k_symbols < 1 or config.k_symbols > 100:
        errors.append("k_symbols must be between 1 and 100")

    # Toxicity
    if not (0 <= config.tox_low <= config.tox_mid <= 100):
        errors.append("tox thresholds must be 0 <= low <= mid <= 100")

    # Risk
    if config.dd_max_session_pct >= config.dd_max_daily_pct:
        errors.append("dd_max_session_pct should be less than dd_max_daily_pct")
    if config.inv_max_per_symbol_usd <= 0:
        errors.append("inv_max_per_symbol_usd must be positive")

    # Execution
    if config.order_min_size_usd <= 0:
        errors.append("order_min_size_usd must be positive")
    if config.grid_refresh_interval_ms < 100:
        errors.append("grid_refresh_interval_ms must be at least 100ms")

    return errors
```
