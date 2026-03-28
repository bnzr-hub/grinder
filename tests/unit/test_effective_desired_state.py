"""Tests for Effective Desired State Projection (PR-4, ADR-103).

Adversarial tests for the three-layer model:
  1. Theoretical desired state (SM wants)
  2. Effective desired state (legal target after risk projection)
  3. Actual exchange state

Tests cover:
  - Unconstrained projection (theoretical == effective)
  - Partial projection (theoretical > effective, subset kept)
  - Fully constrained projection (effective = 0)
  - Actual matches effective but not theoretical → no churn
  - Actual below effective → only missing effective entries staged
  - Actual above effective → extras cancelled
  - Observability contract: projection mode and counts in result
  - Projection reason stability (enum values)
  - _project_desired_entries deterministic ranking
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from collections.abc import Sequence

from grinder.core import OrderSide
from grinder.execution.types import ActionType
from grinder.grid_v2.state import BranchMode
from grinder.grid_v2.sync_reconciler import (
    ProjectionMode,
    _project_desired_entries,
    reconcile_grid_state,
)


def _mock_bridge(
    *,
    entry_buy_prices: list[Decimal] | None = None,
    entry_sell_prices: list[Decimal] | None = None,
    open_lots: int = 0,
    max_inventory: int = 10,
    mode: BranchMode = BranchMode.FLAT,
    order_size: Decimal = Decimal("0.001"),
    exit_orders: list[object] | None = None,
    reference_price: Decimal = Decimal("50000"),
) -> MagicMock:
    bridge = MagicMock()
    sm = MagicMock()
    sm.mode = mode
    sm.snapshot.entry_window.buy_entry_prices = entry_buy_prices or []
    sm.snapshot.entry_window.sell_entry_prices = entry_sell_prices or []
    sm.snapshot.entry_window.reference_price = reference_price
    sm.snapshot.open_lots = [MagicMock() for _ in range(open_lots)]
    sm.snapshot.exit_orders = exit_orders or []
    bridge.state_machine = sm
    bridge._config.max_inventory_levels = max_inventory
    bridge._config.grid_step_pct = Decimal("0.005")
    bridge._config.order_size = order_size
    bridge._config.price_tick_size = Decimal("0.10")
    bridge._quantize_price = lambda p, _side: p
    bridge.adapter.registry.cid_for_entry.return_value = None
    bridge.adapter.registry.cid_for_exit.return_value = None
    bridge.adapter.registry.all_entry_cids = set()
    bridge.adapter.parse_cid.return_value = None
    return bridge


def _mock_snapshot(open_orders: Sequence[object] | None = None) -> MagicMock:
    snap = MagicMock()
    snap.open_orders = open_orders or []
    return snap


def _entry_order(symbol: str, cid: str, side: str, price: Decimal) -> MagicMock:
    o = MagicMock()
    o.symbol = symbol
    o.order_id = cid
    o.side = side
    o.price = price
    return o


class TestProjectionModeEnum:
    """ProjectionMode enum values are stable and documented."""

    def test_enum_values(self) -> None:
        assert ProjectionMode.UNCONSTRAINED.value == "UNCONSTRAINED"
        assert ProjectionMode.RISK_CONSTRAINED_PARTIAL.value == "RISK_CONSTRAINED_PARTIAL"
        assert ProjectionMode.RISK_CONSTRAINED_ZERO.value == "RISK_CONSTRAINED_ZERO"

    def test_all_modes_exist(self) -> None:
        assert len(ProjectionMode) == 3


class TestProjectDesiredEntries:
    """_project_desired_entries deterministic ranking tests."""

    def test_unconstrained_returns_full_set(self) -> None:
        keys = {
            (OrderSide.BUY, Decimal("49750")),
            (OrderSide.SELL, Decimal("50250")),
        }
        result, mode = _project_desired_entries(keys, None, Decimal("50000"))
        assert result == keys
        assert mode == ProjectionMode.UNCONSTRAINED

    def test_capacity_exceeds_returns_unconstrained(self) -> None:
        keys = {(OrderSide.BUY, Decimal("49750"))}
        result, mode = _project_desired_entries(keys, 10, Decimal("50000"))
        assert result == keys
        assert mode == ProjectionMode.UNCONSTRAINED

    def test_zero_capacity_returns_empty(self) -> None:
        keys = {
            (OrderSide.BUY, Decimal("49750")),
            (OrderSide.SELL, Decimal("50250")),
        }
        result, mode = _project_desired_entries(keys, 0, Decimal("50000"))
        assert result == set()
        assert mode == ProjectionMode.RISK_CONSTRAINED_ZERO

    def test_partial_keeps_closest_to_reference(self) -> None:
        keys = {
            (OrderSide.BUY, Decimal("49750")),  # 250 from ref
            (OrderSide.BUY, Decimal("49500")),  # 500 from ref
            (OrderSide.SELL, Decimal("50250")),  # 250 from ref
            (OrderSide.SELL, Decimal("50500")),  # 500 from ref
        }
        result, mode = _project_desired_entries(keys, 2, Decimal("50000"))
        assert mode == ProjectionMode.RISK_CONSTRAINED_PARTIAL
        assert len(result) == 2
        assert (OrderSide.BUY, Decimal("49750")) in result
        assert (OrderSide.SELL, Decimal("50250")) in result

    def test_empty_theoretical_returns_unconstrained(self) -> None:
        result, mode = _project_desired_entries(set(), 0, Decimal("50000"))
        assert result == set()
        assert mode == ProjectionMode.UNCONSTRAINED


class TestUnconstrainedProjection:
    """Theoretical == effective when unconstrained."""

    def test_theoretical_equals_effective(self) -> None:
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500")],
        )
        result = reconcile_grid_state(
            snapshot=_mock_snapshot(),
            symbol="BTCUSDT",
            bridge=bridge,
            risk_entry_capacity=None,
        )
        assert result.theoretical_desired_entry_count == 4
        assert result.desired_entry_count == 4
        assert result.projection_mode == ProjectionMode.UNCONSTRAINED
        assert result.legal_entry_capacity is None


class TestPartialProjection:
    """Theoretical > effective, subset kept."""

    def test_partial_projection_fields(self) -> None:
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500")],
        )
        result = reconcile_grid_state(
            snapshot=_mock_snapshot(),
            symbol="BTCUSDT",
            bridge=bridge,
            risk_entry_capacity=2,
        )
        assert result.theoretical_desired_entry_count == 4
        assert result.desired_entry_count == 2
        assert result.projection_mode == ProjectionMode.RISK_CONSTRAINED_PARTIAL
        assert result.legal_entry_capacity == 2

    def test_only_effective_entries_staged(self) -> None:
        """Only missing effective entries generate PLACE actions."""
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500")],
        )
        result = reconcile_grid_state(
            snapshot=_mock_snapshot(),
            symbol="BTCUSDT",
            bridge=bridge,
            risk_entry_capacity=2,
        )
        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 2
        placed_prices = {a.price for a in place_actions}
        assert Decimal("49750") in placed_prices
        assert Decimal("50250") in placed_prices
        assert Decimal("49500") not in placed_prices
        assert Decimal("50500") not in placed_prices


class TestFullyConstrainedProjection:
    """Effective = 0 while theoretical > 0."""

    def test_zero_effective_fields(self) -> None:
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500")],
        )
        result = reconcile_grid_state(
            snapshot=_mock_snapshot(),
            symbol="BTCUSDT",
            bridge=bridge,
            risk_entry_capacity=0,
        )
        assert result.theoretical_desired_entry_count == 4
        assert result.desired_entry_count == 0
        assert result.projection_mode == ProjectionMode.RISK_CONSTRAINED_ZERO
        assert result.legal_entry_capacity == 0
        assert not [a for a in result.actions if a.action_type == ActionType.PLACE]


class TestActualMatchesEffectiveNotTheoretical:
    """Key adversarial case: actual matches effective but not theoretical → no churn."""

    def test_no_churn_when_actual_matches_effective(self) -> None:
        """Exchange has 2 entries matching the effective (partial) target.
        Theoretical is 4. No missing, no extra → zero actions.
        """
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500")],
            reference_price=Decimal("50000"),
        )
        # Exchange has exactly the 2 effective entries (closest to ref)
        parsed_entry = MagicMock()
        parsed_entry.kind.value = "ENTRY"
        bridge.adapter.parse_cid.return_value = parsed_entry

        orders = [
            _entry_order("BTCUSDT", "g-E-1", "BUY", Decimal("49750")),
            _entry_order("BTCUSDT", "g-E-2", "SELL", Decimal("50250")),
        ]
        snap = _mock_snapshot(open_orders=orders)

        result = reconcile_grid_state(
            snapshot=snap,
            symbol="BTCUSDT",
            bridge=bridge,
            risk_entry_capacity=2,
        )

        assert result.theoretical_desired_entry_count == 4
        assert result.desired_entry_count == 2
        assert result.actual_entry_count == 2
        assert result.missing_entries == 0
        assert result.extra_entries == 0
        assert len(result.actions) == 0


class TestActualBelowEffective:
    """Actual < effective → only missing effective entries are staged."""

    def test_stages_only_missing_effective(self) -> None:
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500")],
            reference_price=Decimal("50000"),
        )
        # Exchange has 1 of 2 effective entries
        parsed_entry = MagicMock()
        parsed_entry.kind.value = "ENTRY"
        bridge.adapter.parse_cid.return_value = parsed_entry

        orders = [_entry_order("BTCUSDT", "g-E-1", "BUY", Decimal("49750"))]
        snap = _mock_snapshot(open_orders=orders)

        result = reconcile_grid_state(
            snapshot=snap,
            symbol="BTCUSDT",
            bridge=bridge,
            risk_entry_capacity=2,
        )

        assert result.missing_entries == 1
        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 1
        assert place_actions[0].price == Decimal("50250")


class TestActualAboveEffective:
    """Actual > effective → extras relative to effective are cancelled."""

    def test_cancels_extras_relative_to_effective(self) -> None:
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500")],
            reference_price=Decimal("50000"),
        )
        # Exchange has all 4 theoretical entries, but effective is 0
        parsed_entry = MagicMock()
        parsed_entry.kind.value = "ENTRY"
        bridge.adapter.parse_cid.return_value = parsed_entry

        orders = [
            _entry_order("BTCUSDT", "g-E-1", "BUY", Decimal("49750")),
            _entry_order("BTCUSDT", "g-E-2", "BUY", Decimal("49500")),
            _entry_order("BTCUSDT", "g-E-3", "SELL", Decimal("50250")),
            _entry_order("BTCUSDT", "g-E-4", "SELL", Decimal("50500")),
        ]
        snap = _mock_snapshot(open_orders=orders)

        result = reconcile_grid_state(
            snapshot=snap,
            symbol="BTCUSDT",
            bridge=bridge,
            risk_entry_capacity=0,
        )

        assert result.theoretical_desired_entry_count == 4
        assert result.desired_entry_count == 0
        assert result.extra_entries == 4
        cancel_actions = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 4


class TestObservabilityContract:
    """ReconcileResult exposes projection metadata."""

    def test_result_has_all_projection_fields(self) -> None:
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750")],
            entry_sell_prices=[Decimal("50250")],
        )
        result = reconcile_grid_state(
            snapshot=_mock_snapshot(),
            symbol="BTCUSDT",
            bridge=bridge,
            risk_entry_capacity=1,
        )
        assert hasattr(result, "theoretical_desired_entry_count")
        assert hasattr(result, "desired_entry_count")
        assert hasattr(result, "projection_mode")
        assert hasattr(result, "legal_entry_capacity")
        assert result.theoretical_desired_entry_count == 2
        assert result.desired_entry_count == 1
        assert result.projection_mode == ProjectionMode.RISK_CONSTRAINED_PARTIAL
        assert result.legal_entry_capacity == 1

    def test_empty_result_has_projection_fields(self) -> None:
        bridge = MagicMock()
        bridge.state_machine = None
        result = reconcile_grid_state(
            snapshot=_mock_snapshot(),
            symbol="BTCUSDT",
            bridge=bridge,
        )
        assert result.theoretical_desired_entry_count == 0
        assert result.desired_entry_count == 0
        assert result.projection_mode == ProjectionMode.UNCONSTRAINED


class TestGapDetectionProjectionInteraction:
    """Gap detection must not bypass legal projection.

    Pipeline: theoretical → gap supplement → projection → effective.
    Gap detection adds to theoretical before projection, so effective
    never exceeds legal capacity.
    """

    def test_zero_cap_with_exchange_gap_no_entry_added(self) -> None:
        """risk_entry_capacity=0 + gap in actual entries → no effective entries.

        Gap detection may add to theoretical, but projection zeroes everything.
        """
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
        )
        # Actual: two BUY entries with a gap between them
        parsed_entry = MagicMock()
        parsed_entry.kind.value = "ENTRY"
        bridge.adapter.parse_cid.return_value = parsed_entry

        orders = [
            _entry_order("BTCUSDT", "g-E-1", "BUY", Decimal("49750")),
            # Gap: 49500 missing, next entry at 49250 → gap > 1.5 * step
            _entry_order("BTCUSDT", "g-E-2", "BUY", Decimal("49000")),
        ]
        snap = _mock_snapshot(open_orders=orders)

        result = reconcile_grid_state(
            snapshot=snap,
            symbol="BTCUSDT",
            bridge=bridge,
            risk_entry_capacity=0,
        )

        assert result.desired_entry_count == 0
        assert result.projection_mode == ProjectionMode.RISK_CONSTRAINED_ZERO
        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 0

    def test_partial_cap_with_gap_effective_capped(self) -> None:
        """risk_entry_capacity=1 + gap detection adds extra level →
        effective count stays <= 1.
        """
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            reference_price=Decimal("50000"),
        )
        parsed_entry = MagicMock()
        parsed_entry.kind.value = "ENTRY"
        bridge.adapter.parse_cid.return_value = parsed_entry

        orders = [
            _entry_order("BTCUSDT", "g-E-1", "BUY", Decimal("49750")),
            _entry_order("BTCUSDT", "g-E-2", "BUY", Decimal("49000")),
        ]
        snap = _mock_snapshot(open_orders=orders)

        result = reconcile_grid_state(
            snapshot=snap,
            symbol="BTCUSDT",
            bridge=bridge,
            risk_entry_capacity=1,
        )

        # Theoretical may be > 1 (SM + gap), but effective must be <= 1
        assert result.desired_entry_count <= 1
        assert result.legal_entry_capacity == 1

    def test_effective_never_exceeds_capacity(self) -> None:
        """General invariant: desired_entry_count <= legal_entry_capacity
        when capacity is not None.
        """
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500"), Decimal("49250")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500"), Decimal("50750")],
        )
        for cap in [0, 1, 2, 3]:
            result = reconcile_grid_state(
                snapshot=_mock_snapshot(),
                symbol="BTCUSDT",
                bridge=bridge,
                risk_entry_capacity=cap,
            )
            assert result.desired_entry_count <= cap, (
                f"effective {result.desired_entry_count} > capacity {cap}"
            )
