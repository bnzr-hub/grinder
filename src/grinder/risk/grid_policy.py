"""Shared grid risk policy — SSOT for grid geometry and risk sizing.

All grid-related constants and depth assumptions live here. Used by:
- tuning solver (sizing at bootstrap)
- tuning refresher (sizing at refresh)
- GridRiskSizer (admission)
- bridge/runtime (engine construction)

Per spec docs/41_AUTONOMOUS_RISK_MANAGER_V1_SPEC.md §12/§20.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GridPolicy:
    """Canonical grid geometry and risk policy.

    Attributes:
        live_entry_levels_per_side: Active entry orders per side of the grid.
            The visible ladder depth at any point in time.
        max_inventory_levels: Maximum accumulated lots (inventory cap).
            When reached, same-side replenishment / rolling is suppressed.
            This is a lot-count cap applied by the grid_v2 state machine.
        force_reduce_trigger_level: Adverse price level that triggers
            FORCE_REDUCE. In live runtime, this is computed as a price
            threshold (reference ± step_price * level) via adverse_trigger.py,
            NOT as a raw lot count. The level number defines how many grid
            steps away from reference price the threshold sits.
        forced_flat_trigger_level: Adverse price level that triggers
            FORCED_FLAT (emergency symbol flatten). Same price-based
            semantics as force_reduce_trigger_level — NOT a lot count.
        adverse_depth_levels: Worst-case depth used for risk sizing.
            Order size is computed to survive this many adverse price
            levels without exceeding the symbol risk budget.
            Must equal forced_flat_trigger_level.
    """

    live_entry_levels_per_side: int = 5
    max_inventory_levels: int = 15
    force_reduce_trigger_level: int = 16
    forced_flat_trigger_level: int = 20
    adverse_depth_levels: int = 20  # == forced_flat_trigger_level

    def __post_init__(self) -> None:
        if self.live_entry_levels_per_side < 1:
            raise ValueError(
                f"live_entry_levels_per_side must be >= 1, got {self.live_entry_levels_per_side}"
            )
        if self.max_inventory_levels < self.live_entry_levels_per_side:
            raise ValueError(
                f"max_inventory_levels ({self.max_inventory_levels}) must be >= "
                f"live_entry_levels_per_side ({self.live_entry_levels_per_side})"
            )
        if self.force_reduce_trigger_level <= self.max_inventory_levels:
            raise ValueError(
                f"force_reduce_trigger_level ({self.force_reduce_trigger_level}) must be > "
                f"max_inventory_levels ({self.max_inventory_levels})"
            )
        if self.forced_flat_trigger_level <= self.force_reduce_trigger_level:
            raise ValueError(
                f"forced_flat_trigger_level ({self.forced_flat_trigger_level}) must be > "
                f"force_reduce_trigger_level ({self.force_reduce_trigger_level})"
            )


# Canonical default policy instance
DEFAULT_GRID_POLICY = GridPolicy()
