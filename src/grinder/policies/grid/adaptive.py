"""Adaptive Grid Policy v1/v2 (L1 + optional L2 gating + DD ratio, deterministic).

Computes dynamic grid parameters from FeatureSnapshot:
- step_bps: from NATR + regime multipliers
- width_bps: from X_stress model (NATR * sqrt(n) * k_tail)
- levels: from width / step with clamps

Uses Regime classifier for behavior adjustment:
- RANGE: symmetric grid, base multipliers
- TREND_UP/DOWN: asymmetric (more levels against trend)
- VOL_SHOCK: wider step, narrower width
- THIN_BOOK: pause or minimal grid
- TOXIC/EMERGENCY: pause trading

v2 additions (M7-03, l2_gating_enabled=True):
- L2 insufficient depth gate: blocks entries when depth exhausted
- L2 impact threshold gate: pauses entries when VWAP slippage exceeds threshold

v2 additions (M7-04, dd_budget_ratio):
- DD budget ratio: scales entry sizing or blocks entries when ratio=0

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.8-17.10, ADR-022, ADR-057, ADR-058
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from grinder.controller.regime import Regime, RegimeConfig, classify_regime
from grinder.core import GridMode, MarketRegime, ResetAction
from grinder.policies.base import GridPlan, GridPolicy
from grinder.sizing import AutoSizer, AutoSizerConfig, GridShape

if TYPE_CHECKING:
    from grinder.features.l2_types import L2FeatureSnapshot
    from grinder.features.types import FeatureSnapshot
    from grinder.gating.types import GatingResult


@dataclass(frozen=True)
class AdaptiveGridConfig:
    """Configuration for AdaptiveGridPolicy.

    All thresholds in basis points (integer) for determinism.

    Attributes:
        # Step parameters (§17.9)
        step_min_bps: Floor for step to avoid micro-grid (default 5 bps)
        step_alpha: Volatility multiplier for step (default 0.3)

        # Width/X_stress parameters (§17.8)
        horizon_minutes: Stress horizon in minutes (default 60)
        bar_interval_minutes: Bar interval for NATR (default 1)
        k_tail: Tail multiplier for stress (default 2.0)
        x_min_bps: Minimum width in bps (default 20)
        x_cap_bps: Maximum width in bps (default 500)

        # Levels parameters (§17.10)
        levels_min: Minimum levels per side (default 2)
        levels_max: Maximum levels per side (default 20)

        # Regime multipliers
        vol_shock_step_mult: Step multiplier in VOL_SHOCK (default 1.5)
        thin_book_step_mult: Step multiplier in THIN_BOOK (default 2.0)
        trend_width_mult: Width multiplier on against-trend side (default 1.3)

        # Size schedule (legacy - use existing size_per_level)
        size_per_level: Order quantity per level (base asset)

        # Regime config for classifier
        regime_config: Configuration for regime classifier
    """

    # Step parameters
    step_min_bps: int = 5
    step_alpha: int = 30  # Stored as 30 = 0.30 (scaled by 100 for integer math)

    # Width/X_stress parameters
    horizon_minutes: int = 60
    bar_interval_minutes: int = 1
    k_tail: int = 200  # Stored as 200 = 2.00 (scaled by 100)
    x_min_bps: int = 20
    x_cap_bps: int = 500

    # Levels parameters
    levels_min: int = 2
    levels_max: int = 20

    # Regime multipliers (scaled by 100)
    vol_shock_step_mult: int = 150  # 1.50
    thin_book_step_mult: int = 200  # 2.00
    trend_width_mult: int = 130  # 1.30

    # Size schedule (legacy - used when auto_sizing_enabled=False)
    size_per_level: Decimal = field(default_factory=lambda: Decimal("0.01"))

    # Regime config
    regime_config: RegimeConfig = field(default_factory=RegimeConfig)

    # Auto-sizing (ASM-P2-01) - when enabled, computes size_schedule from risk budget
    auto_sizing_enabled: bool = False
    equity: Decimal | None = None  # Account equity (USD)
    dd_budget: Decimal | None = None  # Max drawdown as decimal (0.20 = 20%)
    adverse_move: Decimal | None = None  # Worst-case price move as decimal (0.25 = 25%)
    auto_sizer_config: AutoSizerConfig = field(default_factory=AutoSizerConfig)

    # L2 gating (M7-03, ADR-057) - when enabled, uses L2 features for entry gating
    # Default OFF to preserve v1 behavior
    l2_gating_enabled: bool = False
    l2_impact_threshold_bps: int = 200  # PAUSE entries when impact >= this threshold

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "step_min_bps": self.step_min_bps,
            "step_alpha": self.step_alpha,
            "horizon_minutes": self.horizon_minutes,
            "bar_interval_minutes": self.bar_interval_minutes,
            "k_tail": self.k_tail,
            "x_min_bps": self.x_min_bps,
            "x_cap_bps": self.x_cap_bps,
            "levels_min": self.levels_min,
            "levels_max": self.levels_max,
            "vol_shock_step_mult": self.vol_shock_step_mult,
            "thin_book_step_mult": self.thin_book_step_mult,
            "trend_width_mult": self.trend_width_mult,
            "size_per_level": str(self.size_per_level),
            "regime_config": self.regime_config.to_dict(),
            "auto_sizing_enabled": self.auto_sizing_enabled,
            "equity": str(self.equity) if self.equity else None,
            "dd_budget": str(self.dd_budget) if self.dd_budget else None,
            "adverse_move": str(self.adverse_move) if self.adverse_move else None,
            "l2_gating_enabled": self.l2_gating_enabled,
            "l2_impact_threshold_bps": self.l2_impact_threshold_bps,
        }


@dataclass(frozen=True)
class L2GateResult:
    """Result of L2 gating evaluation (M7-03, ADR-057).

    Determines whether to pause or block entries based on L2 features.

    Attributes:
        should_pause: True if both sides should be paused (full grid pause)
        block_buy_side: True if buy-side entries should be blocked
        block_sell_side: True if sell-side entries should be blocked
        reason_codes: List of reason codes explaining the gate decision
    """

    should_pause: bool = False
    block_buy_side: bool = False
    block_sell_side: bool = False
    reason_codes: list[str] = field(default_factory=list)

    @classmethod
    def no_gate(cls) -> L2GateResult:
        """Return a result that allows all entries (no gating)."""
        return cls()


def _regime_to_market_regime(regime: Regime) -> MarketRegime:
    """Convert controller Regime to core MarketRegime."""
    mapping = {
        Regime.RANGE: MarketRegime.RANGE,
        Regime.TREND_UP: MarketRegime.TREND_UP,
        Regime.TREND_DOWN: MarketRegime.TREND_DOWN,
        Regime.VOL_SHOCK: MarketRegime.VOL_SHOCK,
        Regime.THIN_BOOK: MarketRegime.THIN_BOOK,
        # TOXIC, PAUSED, EMERGENCY map to THIN_BOOK for grid purposes
        # (actual pause is handled by reset_action)
        Regime.TOXIC: MarketRegime.THIN_BOOK,
        Regime.PAUSED: MarketRegime.THIN_BOOK,
        Regime.EMERGENCY: MarketRegime.THIN_BOOK,
    }
    return mapping.get(regime, MarketRegime.RANGE)


def compute_step_bps(
    natr_bps: int,
    regime: Regime,
    config: AdaptiveGridConfig,
) -> int:
    """Compute grid step in basis points.

    Formula (§17.9):
        step_bps = max(step_min_bps, alpha * NATR * shock_multiplier(regime))

    All calculations use integer arithmetic for determinism.

    Args:
        natr_bps: Normalized ATR in basis points
        regime: Current market regime
        config: Adaptive grid configuration

    Returns:
        Step in basis points (integer)
    """
    # Get regime multiplier (scaled by 100)
    if regime == Regime.VOL_SHOCK:
        regime_mult = config.vol_shock_step_mult
    elif regime in (Regime.THIN_BOOK, Regime.TOXIC):
        regime_mult = config.thin_book_step_mult
    else:
        regime_mult = 100  # 1.0

    # Compute step: alpha * NATR * regime_mult
    # alpha is stored as 30 = 0.30, regime_mult as 150 = 1.50
    # So we need: (alpha/100) * natr_bps * (regime_mult/100)
    # = (alpha * natr_bps * regime_mult) / 10000
    step_raw = (config.step_alpha * natr_bps * regime_mult) // 10000

    # Apply floor
    return max(config.step_min_bps, step_raw)


def compute_width_bps(
    natr_bps: int,
    regime: Regime,
    config: AdaptiveGridConfig,
) -> tuple[int, int]:
    """Compute grid width (X_stress) in basis points.

    Formula (§17.8):
        sigma_H = NATR * sqrt(H / TF)
        X_base = k_tail * sigma_H
        X_stress = clamp(X_base, X_min, X_cap)

    Returns (width_up, width_down) for asymmetric grids.

    Args:
        natr_bps: Normalized ATR in basis points
        regime: Current market regime
        config: Adaptive grid configuration

    Returns:
        Tuple of (width_up_bps, width_down_bps) in integer bps
    """
    # Compute horizon volatility scaling: n = H / TF
    n = config.horizon_minutes // config.bar_interval_minutes
    if n <= 0:
        n = 1

    # sqrt(n) scaled by 100 for integer math
    sqrt_n_scaled = int(math.sqrt(n) * 100)

    # sigma_H = NATR * sqrt(n), scaled: sigma_H_bps = natr_bps * sqrt_n_scaled / 100
    sigma_h_bps = (natr_bps * sqrt_n_scaled) // 100

    # X_base = k_tail * sigma_H (k_tail stored as 200 = 2.00)
    x_base_bps = (config.k_tail * sigma_h_bps) // 100

    # Clamp to [X_min, X_cap]
    x_stress_bps = max(config.x_min_bps, min(config.x_cap_bps, x_base_bps))

    # Adjust for trend asymmetry
    if regime == Regime.TREND_UP:
        # Uptrend: more width on the short (up) side
        width_up = (x_stress_bps * config.trend_width_mult) // 100
        width_down = x_stress_bps
    elif regime == Regime.TREND_DOWN:
        # Downtrend: more width on the long (down) side
        width_up = x_stress_bps
        width_down = (x_stress_bps * config.trend_width_mult) // 100
    else:
        # Symmetric for RANGE, VOL_SHOCK, THIN_BOOK
        width_up = x_stress_bps
        width_down = x_stress_bps

    return (width_up, width_down)


def compute_levels(
    width_up_bps: int,
    width_down_bps: int,
    step_bps: int,
    config: AdaptiveGridConfig,
) -> tuple[int, int]:
    """Compute number of grid levels per side.

    Formula (§17.10):
        levels = ceil(width / step)
        clamped to [levels_min, levels_max]

    Args:
        width_up_bps: Width on the up (sell) side
        width_down_bps: Width on the down (buy) side
        step_bps: Grid step in basis points
        config: Adaptive grid configuration

    Returns:
        Tuple of (levels_up, levels_down)
    """
    if step_bps <= 0:
        step_bps = config.step_min_bps

    # Integer ceiling division: ceil(width / step) = (width + step - 1) // step
    levels_up_raw = (width_up_bps + step_bps - 1) // step_bps
    levels_down_raw = (width_down_bps + step_bps - 1) // step_bps

    # Clamp
    levels_up = max(config.levels_min, min(config.levels_max, levels_up_raw))
    levels_down = max(config.levels_min, min(config.levels_max, levels_down_raw))

    return (levels_up, levels_down)


class AdaptiveGridPolicy(GridPolicy):
    """Adaptive grid policy with dynamic step/width/levels.

    Computes grid parameters from market features:
    - step_bps: volatility-scaled with regime multipliers
    - width_bps: X_stress model with trend asymmetry
    - levels: derived from width/step with clamps

    Uses Regime classifier to determine market conditions
    and adjust grid behavior accordingly.

    Sizing is legacy (fixed size_per_level from config).
    Auto-sizing deferred to P1-05b.
    """

    name = "ADAPTIVE_GRID"

    def __init__(
        self,
        config: AdaptiveGridConfig | None = None,
    ) -> None:
        """Initialize adaptive grid policy.

        Args:
            config: Adaptive grid configuration (uses defaults if None)
        """
        self.config = config or AdaptiveGridConfig()

    def evaluate(
        self,
        features: dict[str, Any],
        kill_switch_active: bool = False,
        toxicity_result: GatingResult | None = None,
        l2_features: L2FeatureSnapshot | None = None,
        dd_budget_ratio: Decimal | None = None,
    ) -> GridPlan:
        """Evaluate features and return adaptive grid plan.

        Args:
            features: Dict containing at least:
                - mid_price: Current mid price (Decimal)
                - natr_bps: Normalized ATR in basis points
                - spread_bps: Bid-ask spread in bps
                - thin_l1: Min L1 depth
                - net_return_bps: Net return over horizon
                - range_score: Choppiness indicator
                - warmup_bars: Number of completed bars
            kill_switch_active: Whether kill-switch is triggered
            toxicity_result: Result from toxicity gate
            l2_features: Optional L2 feature snapshot for v2 gating (M7-03)
            dd_budget_ratio: Pre-computed DD budget ratio [0..1] from DdAllocator (M7-04).
                - None: no DD scaling (v1 behavior)
                - 1.0: no scaling
                - 0 < ratio < 1: scale size_schedule by ratio
                - 0.0: block all new entries

        Returns:
            GridPlan with adaptive configuration

        Raises:
            ValueError: If dd_budget_ratio is out of range [0..1]
        """
        # Validate dd_budget_ratio (M7-04, ADR-058)
        self._validate_dd_budget_ratio(dd_budget_ratio)
        mid_price = features.get("mid_price")
        if mid_price is None:
            raise KeyError("mid_price is required in features")

        if not isinstance(mid_price, Decimal):
            mid_price = Decimal(str(mid_price))

        # Get feature values with defaults for warmup
        natr_bps = features.get("natr_bps", 0)
        warmup_bars = features.get("warmup_bars", 0)

        # Classify regime
        feature_snapshot = self._build_feature_snapshot(features)
        regime_decision = classify_regime(
            features=feature_snapshot,
            kill_switch_active=kill_switch_active,
            toxicity_result=toxicity_result,
            config=self.config.regime_config,
        )

        # Handle pause conditions
        if regime_decision.regime in (Regime.EMERGENCY, Regime.TOXIC, Regime.PAUSED):
            return self._create_pause_plan(
                mid_price=mid_price,
                regime=regime_decision.regime,
                reason=f"REGIME_{regime_decision.regime.value}",
            )

        # L2 gating (M7-03, ADR-057)
        # When enabled, check L2 features for entry blocking conditions
        l2_gate_result = self._evaluate_l2_gate(l2_features)

        # Compute adaptive parameters
        step_bps = compute_step_bps(natr_bps, regime_decision.regime, self.config)
        width_up_bps, width_down_bps = compute_width_bps(
            natr_bps, regime_decision.regime, self.config
        )
        levels_up, levels_down = compute_levels(width_up_bps, width_down_bps, step_bps, self.config)

        # Compute average width for GridPlan
        avg_width_bps = (width_up_bps + width_down_bps) // 2

        # Build size schedule
        max_levels = max(levels_up, levels_down)
        size_schedule = self._compute_size_schedule(
            max_levels=max_levels,
            step_bps=step_bps,
            price=mid_price,
        )

        # Build reason codes
        reason_codes = [f"REGIME_{regime_decision.regime.value}"]
        if warmup_bars < 15:
            reason_codes.append("WARMUP_INCOMPLETE")
        if regime_decision.regime in (Regime.TREND_UP, Regime.TREND_DOWN):
            reason_codes.append("ASYMMETRIC_GRID")

        # Apply L2 gating (M7-03): pause or reduce levels based on L2 conditions
        final_levels_up = levels_up
        final_levels_down = levels_down
        reset_action = ResetAction.NONE

        if l2_gate_result.should_pause:
            # Full pause: zero levels on both sides
            final_levels_up = 0
            final_levels_down = 0
            reset_action = ResetAction.HARD
            reason_codes.extend(l2_gate_result.reason_codes)
        else:
            # Apply side-specific blocking (add detailed reason codes)
            if l2_gate_result.block_buy_side:
                final_levels_down = 0
                reason_codes.extend(l2_gate_result.reason_codes)
                reason_codes.append("L2_BLOCK_BUY")
            if l2_gate_result.block_sell_side:
                final_levels_up = 0
                # Only add reason codes if not already added (avoid duplicates)
                if not l2_gate_result.block_buy_side:
                    reason_codes.extend(l2_gate_result.reason_codes)
                reason_codes.append("L2_BLOCK_SELL")

        # Recompute size schedule if levels changed
        final_max_levels = max(final_levels_up, final_levels_down)
        if final_max_levels != max_levels:
            size_schedule = self._compute_size_schedule(
                max_levels=final_max_levels,
                step_bps=step_bps,
                price=mid_price,
            )

        # Apply DD budget ratio (M7-04, ADR-058)
        final_levels_up, final_levels_down, size_schedule, dd_reason_codes = (
            self._apply_dd_budget_ratio(
                levels_up=final_levels_up,
                levels_down=final_levels_down,
                size_schedule=size_schedule,
                dd_budget_ratio=dd_budget_ratio,
            )
        )
        reason_codes.extend(dd_reason_codes)
        if dd_budget_ratio == Decimal("0"):
            reset_action = ResetAction.HARD

        return GridPlan(
            mode=GridMode.BILATERAL,
            center_price=mid_price,
            spacing_bps=float(step_bps),
            levels_up=final_levels_up,
            levels_down=final_levels_down,
            size_schedule=size_schedule,
            skew_bps=0.0,
            regime=_regime_to_market_regime(regime_decision.regime),
            width_bps=float(avg_width_bps),
            reset_action=reset_action,
            reason_codes=reason_codes,
        )

    def _build_feature_snapshot(self, features: dict[str, Any]) -> FeatureSnapshot | None:
        """Build FeatureSnapshot from features dict if possible."""
        from grinder.features.types import FeatureSnapshot  # noqa: PLC0415

        # Check if we have enough data
        required = ["ts", "symbol", "mid_price", "spread_bps", "natr_bps"]
        if not all(k in features for k in required):
            return None

        mid_price = features["mid_price"]
        if not isinstance(mid_price, Decimal):
            mid_price = Decimal(str(mid_price))

        thin_l1 = features.get("thin_l1", Decimal("1.0"))
        if not isinstance(thin_l1, Decimal):
            thin_l1 = Decimal(str(thin_l1))

        atr = features.get("atr")
        if atr is not None and not isinstance(atr, Decimal):
            atr = Decimal(str(atr))

        return FeatureSnapshot(
            ts=features.get("ts", 0),
            symbol=features.get("symbol", "UNKNOWN"),
            mid_price=mid_price,
            spread_bps=features.get("spread_bps", 0),
            imbalance_l1_bps=features.get("imbalance_l1_bps", 0),
            thin_l1=thin_l1,
            natr_bps=features.get("natr_bps", 0),
            atr=atr,
            sum_abs_returns_bps=features.get("sum_abs_returns_bps", 0),
            net_return_bps=features.get("net_return_bps", 0),
            range_score=features.get("range_score", 10),
            warmup_bars=features.get("warmup_bars", 0),
        )

    def _evaluate_l2_gate(
        self,
        l2_features: L2FeatureSnapshot | None,
    ) -> L2GateResult:
        """Evaluate L2 features for entry gating (M7-03, ADR-057).

        When l2_gating_enabled=True, checks:
        1. Insufficient depth: blocks entries on the affected side
        2. Impact threshold: pauses entries when VWAP slippage >= threshold

        Args:
            l2_features: L2 feature snapshot, or None if not available

        Returns:
            L2GateResult with blocking decisions and reason codes

        Semantics:
        - If l2_gating_enabled=False: no gating (v1 behavior)
        - If l2_features is None: no gating (L2 data unavailable)
        - insufficient_depth=1: HARD block on that side (insufficient liquidity)
        - impact >= threshold: PAUSE entries (high slippage risk)
        """
        # v1 behavior: no L2 gating
        if not self.config.l2_gating_enabled:
            return L2GateResult.no_gate()

        # No L2 data available
        if l2_features is None:
            return L2GateResult.no_gate()

        reason_codes: list[str] = []
        block_buy = False
        block_sell = False
        should_pause = False

        # Check insufficient depth (hard block) - priority over impact threshold
        if l2_features.impact_buy_topN_insufficient_depth == 1:
            block_buy = True
            reason_codes.append("L2_INSUFFICIENT_DEPTH_BUY")

        if l2_features.impact_sell_topN_insufficient_depth == 1:
            block_sell = True
            reason_codes.append("L2_INSUFFICIENT_DEPTH_SELL")

        # Check impact threshold (soft gate - pause entries)
        threshold = self.config.l2_impact_threshold_bps

        # High buy impact - block buy side entries (if not already blocked)
        if l2_features.impact_buy_topN_bps >= threshold and not block_buy:
            block_buy = True
            reason_codes.append("L2_IMPACT_BUY_HIGH")

        # High sell impact - block sell side entries (if not already blocked)
        if l2_features.impact_sell_topN_bps >= threshold and not block_sell:
            block_sell = True
            reason_codes.append("L2_IMPACT_SELL_HIGH")

        # If both sides blocked, it's a full pause
        if block_buy and block_sell:
            should_pause = True

        return L2GateResult(
            should_pause=should_pause,
            block_buy_side=block_buy,
            block_sell_side=block_sell,
            reason_codes=reason_codes,
        )

    def _validate_dd_budget_ratio(self, dd_budget_ratio: Decimal | None) -> None:
        """Validate dd_budget_ratio is in [0..1] range (M7-04, ADR-058)."""
        if dd_budget_ratio is not None and (
            dd_budget_ratio < Decimal("0") or dd_budget_ratio > Decimal("1")
        ):
            raise ValueError(f"dd_budget_ratio must be in [0..1], got {dd_budget_ratio}")

    def _apply_dd_budget_ratio(
        self,
        levels_up: int,
        levels_down: int,
        size_schedule: list[Decimal],
        dd_budget_ratio: Decimal | None,
    ) -> tuple[int, int, list[Decimal], list[str]]:
        """Apply DD budget ratio to grid sizing (M7-04, ADR-058).

        Pure helper function for deterministic DD ratio application.

        Args:
            levels_up: Number of sell-side levels
            levels_down: Number of buy-side levels
            size_schedule: Per-level sizing quantities
            dd_budget_ratio: Pre-computed ratio from DdAllocator [0..1] or None

        Returns:
            Tuple of (levels_up, levels_down, size_schedule, reason_codes)

        Semantics:
        - None: no scaling (v1 behavior, no reason codes)
        - 1.0: no scaling (DD_RATIO_NONE not emitted for ratio=1)
        - 0 < ratio < 1: scale size_schedule quantities
        - 0.0: block all new entries (levels = 0)
        """
        reason_codes: list[str] = []

        # No DD ratio provided: v1 behavior
        if dd_budget_ratio is None:
            return levels_up, levels_down, size_schedule, reason_codes

        # Ratio is exactly 1: no scaling needed
        if dd_budget_ratio == Decimal("1"):
            return levels_up, levels_down, size_schedule, reason_codes

        # Ratio is 0: block all new entries
        if dd_budget_ratio == Decimal("0"):
            reason_codes.append("DD_BLOCK_ENTRIES")
            return 0, 0, [], reason_codes

        # 0 < ratio < 1: scale size_schedule
        # Note: No rounding here - policy outputs exact Decimal values.
        # Symbol-specific lot size rounding happens at execution layer.
        scaled_schedule = [qty * dd_budget_ratio for qty in size_schedule]

        reason_codes.append("DD_SCALE_APPLIED")
        return levels_up, levels_down, scaled_schedule, reason_codes

    def _create_pause_plan(
        self,
        mid_price: Decimal,
        regime: Regime,
        reason: str,
    ) -> GridPlan:
        """Create a minimal/paused grid plan."""
        return GridPlan(
            mode=GridMode.BILATERAL,
            center_price=mid_price,
            spacing_bps=float(self.config.step_min_bps),
            levels_up=0,
            levels_down=0,
            size_schedule=[],
            skew_bps=0.0,
            regime=_regime_to_market_regime(regime),
            width_bps=0.0,
            reset_action=ResetAction.HARD,
            reason_codes=[reason],
        )

    def _compute_size_schedule(
        self,
        max_levels: int,
        step_bps: int,
        price: Decimal,
    ) -> list[Decimal]:
        """Compute size schedule for grid levels.

        If auto_sizing_enabled, uses AutoSizer to compute risk-aware quantities.
        Otherwise, uses legacy uniform sizing from config.size_per_level.

        Args:
            max_levels: Maximum number of levels (for schedule length)
            step_bps: Grid step in basis points
            price: Current market price (for notional calculation)

        Returns:
            List of quantities per level (base asset units)
        """
        if not self.config.auto_sizing_enabled:
            # Legacy: uniform sizing
            return [self.config.size_per_level] * max_levels

        # Auto-sizing: compute from risk budget
        if (
            self.config.equity is None
            or self.config.dd_budget is None
            or self.config.adverse_move is None
        ):
            # Missing required params - fall back to legacy
            return [self.config.size_per_level] * max_levels

        sizer = AutoSizer(self.config.auto_sizer_config)
        grid_shape = GridShape(
            levels=max_levels,
            step_bps=float(step_bps),
            top_k=0,  # Use all levels
        )

        schedule = sizer.compute(
            equity=self.config.equity,
            dd_budget=self.config.dd_budget,
            adverse_move=self.config.adverse_move,
            grid_shape=grid_shape,
            price=price,
        )

        return schedule.qty_per_level

    def should_activate(self, features: dict[str, Any]) -> bool:
        """Check if this policy should be active.

        Adaptive policy activates when features are available.

        Args:
            features: Dict of computed features

        Returns:
            True if natr_bps is available (feature engine is running)
        """
        return "natr_bps" in features
