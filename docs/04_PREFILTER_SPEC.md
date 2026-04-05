# GRINDER - Prefilter Specification

> Top-K symbol selection for L2 data and grid trading

---

## 4.1 Prefilter Pipeline

```
Universe (300+ perps)
        │
        ▼
┌───────────────────┐
│   Hard Filters    │  ← SPREAD_MAX, VOL_MIN, etc.
│   (~100 pass)     │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│   Score & Rank    │  ← S_activity + S_vol + S_cost + S_idio
│                   │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│   Stability       │  ← T_ENTER, T_HOLD, diversity
│   Controls        │
└─────────┬─────────┘
          │
          ▼
    Top-K (10-20)
        │
        ▼
   Enable L2 + Grid
```

---

## 4.2 Goal

Select **Top-K in-play symbols** where grid has positive expectancy:
- Enough movement for fills
- Enough liquidity to keep costs low
- Manageable toxicity

---

## 4.3 Universe

**Base list**: Binance Futures USDT-margined perps (~200+ symbols)

**Exclusions**:
- Illiquid (< VOL_MIN_24H)
- Extreme spread (> SPREAD_MAX)
- Under maintenance
- Delisting announced
- Broken feeds

---

## 4.4 Hard Filters

```python
def hard_filter(symbol: str, features: dict) -> tuple[bool, str]:
    """Return (pass, reason) for hard filter."""

    if features["spread_bps"] > SPREAD_MAX:
        return False, "SPREAD_TOO_HIGH"

    if features["vol_24h_usd"] < VOL_MIN_24H:
        return False, "VOL_24H_TOO_LOW"

    if features["vol_last_12x5m_usd"] < VOL_MIN_LAST_12X5M:
        return False, "VOL_LAST_12X5M_TOO_LOW"

    if features["trade_count_1m"] < TRADE_COUNT_MIN_1M:
        return False, "ACTIVITY_TOO_LOW"

    if symbol in BLACKLIST:
        return False, "BLACKLISTED"

    if features.get("is_delisting", False):
        return False, "DELISTING"

    return True, "PASS"
```

### Hard Gate Thresholds

| Gate | Default | Description |
|------|---------|-------------|
| `SPREAD_MAX` | 15 bps | Max bid-ask spread |
| `VOL_MIN_24H` | $10M | Min 24h volume |
| `VOL_MIN_LAST_12X5M` | $2M | Min rolling 12×5m quote volume |
| `TRADE_COUNT_MIN_1M` | 100 | Min trades per minute |
| `OI_MIN` | $5M | Min open interest |

---

## 4.5 Scoring Function

### Features Used (Cheap, No L2)

- **Activity**: `trade_intensity`, `volume_ratio_1m`, `volume_z_1m`
- **Volatility**: `natr_14_5m`, `range_bps_1m`
- **Cost proxies**: `spread_bps_L1`, `microprice_dev_bps`
- **Flow**: `cvd_change_1m/5m`
- **BTC dependency**: `beta_to_btc`, `residual_return_5m`
- **Optional**: `premiumIndex` (cheap REST)

### In-Play Score Computation

```python
def compute_inplay_score(features: dict, weights: dict) -> float:
    """Compute in-play score for symbol selection."""

    # Activity component (we want active symbols)
    s_activity = (
        zscore(features["trade_intensity"]) +
        zscore(features["volume_ratio_1m"])
    ) / 2

    # Volatility component (we want movement for fills)
    s_volatility = (
        zscore(features["natr_14_5m"]) +
        zscore(features["range_bps_1m"])
    ) / 2

    # Cost component (negative: we want low costs)
    s_cost = -(
        zscore(features["spread_bps"]) +
        zscore(features["microprice_dev_bps"])
    ) / 2

    # Idiosyncratic component (we want own movement, not just BTC)
    s_idio = zscore(abs(features["residual_return_5m"]))

    # Weighted sum
    score = (
        weights["W_ACTIVITY"] * s_activity +
        weights["W_VOLATILITY"] * s_volatility +
        weights["W_COST"] * s_cost +
        weights["W_IDIOSYNCRATIC"] * s_idio
    )

    return score
```

### Score Formula

```
S = W_A × S_activity + W_V × S_volatility + W_C × S_cost + W_I × S_idio

Where:
  S_activity = (z(trade_intensity) + z(volume_ratio)) / 2
  S_volatility = (z(natr_14_5m) + z(range_bps_1m)) / 2
  S_cost = -(z(spread_bps) + z(microprice_dev_bps)) / 2
  S_idio = z(|residual_return_5m|)
```

### Default Weights

| Weight | Default | Range |
|--------|---------|-------|
| `W_ACTIVITY` | 0.30 | 0.1-0.5 |
| `W_VOLATILITY` | 0.25 | 0.1-0.4 |
| `W_COST` | 0.25 | 0.1-0.4 |
| `W_IDIOSYNCRATIC` | 0.20 | 0.05-0.3 |

---

## 4.6 Stability Controls

### Cooldown Timers

| Parameter | Default | Description |
|-----------|---------|-------------|
| `T_ENTER` | 30s | Time in top-K before enabling L2 |
| `T_HOLD` | 300s | Min hold time before dropping |
| `T_RERANK` | 60s | Re-ranking interval |

### Hysteresis Logic

```python
class TopKSelector:
    """Select Top-K with stability controls."""

    def __init__(self, k: int, t_enter_s: int, t_hold_s: int):
        self.k = k
        self.t_enter_s = t_enter_s
        self.t_hold_s = t_hold_s
        self.current_topk: set[str] = set()
        self.enter_times: dict[str, int] = {}  # When symbol entered candidate
        self.enable_times: dict[str, int] = {}  # When symbol was enabled

    def update(self, ranked: list[tuple[str, float]], ts: int) -> set[str]:
        """Update Top-K selection with stability."""

        candidates = set(symbol for symbol, _ in ranked[:self.k * 2])
        new_topk = set()

        # Keep existing if still in candidate range and not expired
        for symbol in self.current_topk:
            if symbol in candidates:
                # Check if hold time allows removal
                enabled_at = self.enable_times.get(symbol, 0)
                if ts - enabled_at < self.t_hold_s * 1000:
                    new_topk.add(symbol)
                    continue

            # Symbol fell out - remove if hold time passed
            enabled_at = self.enable_times.get(symbol, 0)
            if ts - enabled_at >= self.t_hold_s * 1000:
                # Can remove
                pass
            else:
                new_topk.add(symbol)

        # Add new symbols that pass enter cooldown
        for symbol, _ in ranked:
            if len(new_topk) >= self.k:
                break

            if symbol in new_topk:
                continue

            # Check enter cooldown
            if symbol not in self.enter_times:
                self.enter_times[symbol] = ts

            if ts - self.enter_times[symbol] >= self.t_enter_s * 1000:
                new_topk.add(symbol)
                self.enable_times[symbol] = ts

        # Update state
        self.current_topk = new_topk

        # Clean up old enter times
        for symbol in list(self.enter_times.keys()):
            if symbol not in candidates:
                del self.enter_times[symbol]

        return new_topk
```

---

## 4.7 Diversity Control (Anti-Correlation)

```python
def apply_diversity_filter(ranked: list[tuple[str, float]],
                           max_correlated: int = 3) -> list[str]:
    """Limit correlated symbols in Top-K."""

    selected = []
    sector_counts = defaultdict(int)

    for symbol, score in ranked:
        sector = get_sector(symbol)  # e.g., "meme", "defi", "layer1"

        # Skip if sector already saturated
        if sector_counts[sector] >= max_correlated:
            continue

        # Skip if too correlated with already selected
        for sel in selected:
            if correlation(symbol, sel) > 0.8:
                continue

        selected.append(symbol)
        sector_counts[sector] += 1

        if len(selected) >= K:
            break

    return selected
```

### Sector Classification

| Sector | Example Symbols |
|--------|-----------------|
| `major` | BTCUSDT, ETHUSDT |
| `layer1` | SOLUSDT, AVAXUSDT, ADAUSDT |
| `layer2` | ARBUSDT, OPUSDT, MATICUSDT |
| `defi` | UNIUSDT, AAVEUSDT, MKRUSDT |
| `meme` | DOGEUSDT, SHIBUSDT, PEPEUSDT |
| `gaming` | AXSUSDT, SANDUSDT, MANAUSDT |
| `ai` | FETUSDT, AGIXUSDT, OCEANUSDT |

---

## 4.8 Prefilter Output

```python
@dataclass
class PrefilterResult:
    """Output of prefilter cycle."""
    ts: int
    topk_symbols: list[str]
    scores: dict[str, float]
    reasons: dict[str, list[str]]  # Why each symbol included/excluded
    cycle_duration_ms: int

    # Diagnostics
    universe_size: int
    passed_hard_filter: int
    passed_soft_filter: int
```

---

## 4.9 Metrics

| Metric | Description |
|--------|-------------|
| `prefilter_cycle_duration_ms` | Time to run prefilter |
| `prefilter_universe_size` | Total symbols considered |
| `prefilter_passed_hard` | Symbols passing hard filters |
| `prefilter_topk_changes` | Symbols added/removed per cycle |
| `prefilter_score_distribution` | Histogram of scores |
