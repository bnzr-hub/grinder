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
        max_inventory_levels: Maximum accumulated lots before same-side
            replenishment is suppressed. Rolling pauses at this level.
        force_reduce_trigger_level: Lot count that triggers FORCE_REDUCE.
            Adverse geometry beyond max_inventory indicates risk escalation.
        forced_flat_trigger_level: Lot count that triggers FORCED_FLAT.
            Hard emergency boundary — symbol must be fully flattened.
        adverse_depth_levels: Worst-case depth used for risk sizing.
            Order size is computed to survive this many fills without
            exceeding the symbol risk budget. Must equal forced_flat_trigger_level.
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
