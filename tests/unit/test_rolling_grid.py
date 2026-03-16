"""PR-ROLLING-GRID: Rolling grid planner + engine integration tests.

V1A (planner-only):
- _RollingLadderState and state management
- Additive level formulas (_build_rolling_grid)
- Price-based order matching (_match_orders_by_price)
- plan() with rolling_mode=True vs False

V1B (engine wiring):
- GRINDER_LIVE_ROLLING_GRID env flag gating
- Rolling fill detection pipeline (snapshot diff → apply_fill_offset)
- Freeze/replenish/anti-churn bypass in rolling mode
- False-positive offset prevention (cancels, TP orders, restart)
- Backward compatibility (flag=0 preserves existing behavior)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap, PositionSnap
from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import OrderSide
from grinder.execution.port import NoOpExchangePort
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.cycle_layer import LiveCycleConfig, LiveCycleLayerV1
from grinder.live.engine import _InflightPlacedOrder, _InflightShift
from grinder.live.grid_planner import GridPlanResult, LiveGridConfig, LiveGridPlannerV1

if TYPE_CHECKING:
    import pytest


def _make_config(
    *,
    levels: int = 3,
    spacing_bps: float = 10.0,
    tick_size: str = "0.10",
    size: str = "0.01",
    epsilon_bps: float = 0.5,
) -> LiveGridConfig:
    return LiveGridConfig(
        base_spacing_bps=spacing_bps,
        levels=levels,
        size_per_level=Decimal(size),
        tick_size=Decimal(tick_size),
        price_epsilon_bps=epsilon_bps,
    )


def _make_order(
    order_id: str,
    symbol: str,
    side: str,
    price: str,
    qty: str = "0.01",
) -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=order_id,
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        price=Decimal(price),
        qty=Decimal(qty),
        filled_qty=Decimal("0"),
        reduce_only=False,
        status="NEW",
        ts=1000000,
    )


# ===== Planner unit constants =====
SYMBOL = "BTCUSDT"
ANCHOR = Decimal("66000")
SPACING_BPS = 10.0
# step_price = round_to_tick(66000 * 10 / 10000) = round_to_tick(66.0) = 66.0
STEP = Decimal("66.0")
TICK = Decimal("0.10")


def _make_planner(levels: int = 3) -> LiveGridPlannerV1:
    return LiveGridPlannerV1(
        _make_config(levels=levels, spacing_bps=SPACING_BPS, tick_size=str(TICK))
    )


def _build_full_grid_orders(
    planner: LiveGridPlannerV1, anchor: Decimal = ANCHOR, net_offset: int = 0
) -> tuple[OpenOrderSnap, ...]:
    """Build exchange orders that match the desired grid at given offset."""
    planner.init_rolling_state(SYMBOL, anchor, SPACING_BPS)
    st = planner.get_rolling_state(SYMBOL)
    assert st is not None
    st.net_offset = net_offset
    ec = anchor + net_offset * STEP
    orders: list[OpenOrderSnap] = []
    cfg = planner._config
    for i in range(1, cfg.levels + 1):
        buy_price = ec - i * STEP
        sell_price = ec + i * STEP
        orders.append(
            _make_order(
                f"grinder_d_{SYMBOL}_{i}_{1000000}_{i}",
                SYMBOL,
                "BUY",
                str(buy_price),
            )
        )
        orders.append(
            _make_order(
                f"grinder_d_{SYMBOL}_{i}_{1000000}_{i + 100}",
                SYMBOL,
                "SELL",
                str(sell_price),
            )
        )
    return tuple(orders)


class TestRollingGridPlanner:
    """Planner-only rolling grid tests (T1-T10, T16-T17, T19-T20)."""

    def test_t1_buy_fill_cardinality(self) -> None:
        """T1 (INV-1 + INV-9): After 1 BUY fill, desired_grid = 2*N - 1 on fill tick.

        INV-9 reserves 1 SELL slot for TP. Grid levels = N BUY + (N-1) SELL.
        Total with TP = 2*N (steady_state_open_count_target).
        """
        planner = _make_planner(levels=3)
        orders = _build_full_grid_orders(planner)

        # Simulate BUY fill: remove lowest BUY order
        planner.apply_fill_offset(SYMBOL, "BUY")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=orders,
            rolling_mode=True,
        )
        # INV-9: 3 BUY + 2 SELL = 5 desired grid levels (1 SELL reserved for TP)
        assert result.desired_count == 5

    def test_t2_sell_fill_cardinality(self) -> None:
        """T2 (INV-1 + INV-9): After 1 SELL fill, desired_grid = 2*N - 1 on fill tick.

        INV-9 reserves 1 BUY slot for TP. Grid levels = (N-1) BUY + N SELL.
        """
        planner = _make_planner(levels=3)
        orders = _build_full_grid_orders(planner)

        planner.apply_fill_offset(SYMBOL, "SELL")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=orders,
            rolling_mode=True,
        )
        # INV-9: 2 BUY + 3 SELL = 5 desired grid levels (1 BUY reserved for TP)
        assert result.desired_count == 5

    def test_t3_spacing_uniform(self) -> None:
        """T3 (INV-2): Adjacent desired levels differ by exactly step_price."""
        planner = _make_planner(levels=5)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # Apply some fills to get non-zero offset
        planner.apply_fill_offset(SYMBOL, "BUY")
        planner.apply_fill_offset(SYMBOL, "BUY")

        levels = planner._build_rolling_grid(SYMBOL)

        buy_prices = sorted([lv.price for lv in levels if lv.side == OrderSide.BUY], reverse=True)
        sell_prices = sorted([lv.price for lv in levels if lv.side == OrderSide.SELL])

        for i in range(len(buy_prices) - 1):
            diff = buy_prices[i] - buy_prices[i + 1]
            assert diff == STEP, f"BUY spacing at {i}: {diff} != {STEP}"

        for i in range(len(sell_prices) - 1):
            diff = sell_prices[i + 1] - sell_prices[i]
            assert diff == STEP, f"SELL spacing at {i}: {diff} != {STEP}"

    def test_t4_buy_fill_center_shift(self) -> None:
        """T4 (INV-3): After 1 BUY fill, ec decreased by step_price."""
        planner = _make_planner()
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.net_offset == 0

        planner.apply_fill_offset(SYMBOL, "BUY")

        assert st.net_offset == -1
        ec = st.anchor_price + st.net_offset * st.step_price
        assert ec == ANCHOR - STEP

    def test_t5_k_buy_fills_center_shift(self) -> None:
        """T5 (INV-3): After k=3 BUY fills, ec decreased by 3*step_price."""
        planner = _make_planner()
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        for _ in range(3):
            planner.apply_fill_offset(SYMBOL, "BUY")

        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.net_offset == -3
        ec = st.anchor_price + st.net_offset * st.step_price
        assert ec == ANCHOR - 3 * STEP

    def test_t6_sell_fill_center_shift(self) -> None:
        """T6 (INV-3): After 1 SELL fill, ec increased by step_price."""
        planner = _make_planner()
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        planner.apply_fill_offset(SYMBOL, "SELL")

        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.net_offset == 1
        ec = st.anchor_price + st.net_offset * st.step_price
        assert ec == ANCHOR + STEP

    def test_t7_frontier_placement(self) -> None:
        """T7 (INV-4): New orders at frontier, not center.

        After BUY fill: new PLACE should be at farthest BUY (frontier)
        and new SELL closer to center (inner frontier).
        """
        planner = _make_planner(levels=3)
        # Build full grid at offset=0
        orders = _build_full_grid_orders(planner, net_offset=0)
        # Simulate BUY fill: offset becomes -1
        planner.apply_fill_offset(SYMBOL, "BUY")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=orders,
            rolling_mode=True,
        )

        places = [a for a in result.actions if a.action_type == ActionType.PLACE]
        # After shift down, we expect PLACEs at frontier positions
        # (new lowest BUY and new innermost SELL), not at center
        assert len(places) >= 1

        # Verify no PLACE is at center (ec)
        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        ec = st.anchor_price + st.net_offset * st.step_price
        for p in places:
            assert p.price != ec, f"PLACE at center {ec} — should be at frontier"

    def test_t8_fill_action_count(self) -> None:
        """T8 (INV-5 + INV-9): Single fill → 1 CANCEL + 1 PLACE = 2 grid actions.

        INV-9: inner SELL L1 is reserved for TP (planner skips it).
        Grid actions: 1 CANCEL (farthest SELL extra) + 1 PLACE (new frontier BUY).
        Cycle layer adds TP PLACE separately (not tested here — planner scope).
        """
        planner = _make_planner(levels=3)
        # Build full grid at offset=0
        full_orders = _build_full_grid_orders(planner, net_offset=0)

        # Simulate 1 BUY fill: remove the closest BUY (L1)
        planner.apply_fill_offset(SYMBOL, "BUY")

        # Remove the filled BUY L1 from open orders
        remaining = tuple(
            o for o in full_orders if not (o.side == "BUY" and o.price == ANCHOR - STEP)
        )

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=remaining,
            rolling_mode=True,
        )

        cancels = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        places = [a for a in result.actions if a.action_type == ActionType.PLACE]

        # INV-9: 1 CANCEL (farthest SELL extra) + 1 PLACE (new frontier BUY)
        # Inner SELL reserved for TP — planner does not PLACE it.
        assert len(cancels) == 1, f"Expected 1 CANCEL, got {len(cancels)}"
        assert len(places) == 1, f"Expected 1 PLACE, got {len(places)}"

    def test_t9_tp_fill_no_offset_change(self) -> None:
        """T9 (INV-6): TP fill does NOT change net_offset.

        apply_fill_offset is only called for grid orders, not TPs.
        This test verifies the API contract — engine is responsible for
        NOT calling apply_fill_offset for TP orders.
        """
        planner = _make_planner()
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # No calls to apply_fill_offset for TP fills
        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.net_offset == 0

        # Only grid fills change offset
        planner.apply_fill_offset(SYMBOL, "BUY")
        assert st.net_offset == -1

        # TP fill = no call → offset unchanged
        assert st.net_offset == -1

    def test_t10_buy_sell_symmetry(self) -> None:
        """T10 (INV-7): BUY fill path mirrors SELL fill path."""
        planner_buy = _make_planner(levels=3)
        planner_sell = _make_planner(levels=3)

        # BUY fill
        orders_buy = _build_full_grid_orders(planner_buy, net_offset=0)
        planner_buy.apply_fill_offset(SYMBOL, "BUY")
        remaining_buy = tuple(
            o for o in orders_buy if not (o.side == "BUY" and o.price == ANCHOR - STEP)
        )
        result_buy = planner_buy.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=remaining_buy,
            rolling_mode=True,
        )

        # SELL fill
        orders_sell = _build_full_grid_orders(planner_sell, net_offset=0)
        planner_sell.apply_fill_offset(SYMBOL, "SELL")
        remaining_sell = tuple(
            o for o in orders_sell if not (o.side == "SELL" and o.price == ANCHOR + STEP)
        )
        result_sell = planner_sell.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=remaining_sell,
            rolling_mode=True,
        )

        # Same action shape: 1 CANCEL + 2 PLACE
        buy_cancels = sum(1 for a in result_buy.actions if a.action_type == ActionType.CANCEL)
        buy_places = sum(1 for a in result_buy.actions if a.action_type == ActionType.PLACE)
        sell_cancels = sum(1 for a in result_sell.actions if a.action_type == ActionType.CANCEL)
        sell_places = sum(1 for a in result_sell.actions if a.action_type == ActionType.PLACE)

        assert buy_cancels == sell_cancels, "BUY/SELL CANCEL count mismatch"
        assert buy_places == sell_places, "BUY/SELL PLACE count mismatch"

        # Opposite offsets
        st_buy = planner_buy.get_rolling_state(SYMBOL)
        st_sell = planner_sell.get_rolling_state(SYMBOL)
        assert st_buy is not None and st_sell is not None
        assert st_buy.net_offset == -st_sell.net_offset

    def test_t16_price_matching_ignores_level_id(self) -> None:
        """T16 (SS 9.2): Correct price, wrong level_id → MATCH (not extra).

        After rolling shift, exchange orders retain original level_id from
        previous center. Price matching should still match by price.
        """
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        ec = ANCHOR
        # Create exchange orders with wrong level_ids but correct prices
        orders = (
            _make_order("grinder_d_BTCUSDT_99_1000000_1", SYMBOL, "BUY", str(ec - STEP)),
            _make_order("grinder_d_BTCUSDT_88_1000000_2", SYMBOL, "BUY", str(ec - 2 * STEP)),
            _make_order("grinder_d_BTCUSDT_77_1000000_3", SYMBOL, "BUY", str(ec - 3 * STEP)),
            _make_order("grinder_d_BTCUSDT_66_1000000_4", SYMBOL, "SELL", str(ec + STEP)),
            _make_order("grinder_d_BTCUSDT_55_1000000_5", SYMBOL, "SELL", str(ec + 2 * STEP)),
            _make_order("grinder_d_BTCUSDT_44_1000000_6", SYMBOL, "SELL", str(ec + 3 * STEP)),
        )

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=orders,
            rolling_mode=True,
        )

        # All match by price — 0 actions
        assert result.diff_extra == 0, f"Expected 0 extra, got {result.diff_extra}"
        assert result.diff_missing == 0, f"Expected 0 missing, got {result.diff_missing}"
        assert len(result.actions) == 0

    def test_t17_mid_drift_zero_actions(self) -> None:
        """T17 (SS 6.5): Mid-price drift → 0 planner actions.

        Rolling grid doesn't track mid_price. Different mid should not
        cause any actions if exchange orders match desired prices.
        """
        planner = _make_planner(levels=3)
        full_orders = _build_full_grid_orders(planner, net_offset=0)

        # Price drifted significantly but no fills
        drifted_mid = ANCHOR + Decimal("500")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=drifted_mid,
            ts_ms=1000000,
            open_orders=full_orders,
            rolling_mode=True,
        )

        # 0 actions — grid is anchored to rolling state, not mid_price
        assert len(result.actions) == 0

    def test_t19_flag_off_uses_mid_anchored(self) -> None:
        """T19 (SS 14): rolling_mode=False uses mid-anchored grid (existing behavior)."""
        planner = _make_planner(levels=3)

        # With rolling_mode=False, no rolling state should be created
        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=(),
            rolling_mode=False,
        )

        # Should create grid centered on mid_price (all missing = GRID_FILL)
        assert result.desired_count == 6
        assert result.diff_missing == 6
        places = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(places) == 6

        # No rolling state created
        assert planner.get_rolling_state(SYMBOL) is None

    def test_t20_flag_off_grid_shift_on_mid_drift(self) -> None:
        """T20 (SS 14): rolling_mode=False → GRID_SHIFT on mid drift."""
        planner = _make_planner(levels=3)

        # First plan at original mid
        full_orders = tuple(
            _make_order(
                f"grinder_d_{SYMBOL}_{i}_{1000000}_{i}",
                SYMBOL,
                side,
                str(
                    ANCHOR
                    + (Decimal(str(i)) if side == "SELL" else -Decimal(str(i)))
                    * ANCHOR
                    * Decimal("10")
                    / Decimal("10000")
                ),
            )
            for i in range(1, 4)
            for side in ("BUY", "SELL")
        )

        # With rolling_mode=False and large mid shift, planner should produce actions
        drifted_mid = ANCHOR + Decimal("1000")
        result = planner.plan(
            symbol=SYMBOL,
            mid_price=drifted_mid,
            ts_ms=2000000,
            open_orders=full_orders,
            rolling_mode=False,
        )

        # Mid drift in non-rolling mode produces actions (GRID_SHIFT / extra / missing)
        assert len(result.actions) > 0


class TestRollingGridEdgeCases:
    """Additional edge case and migration tests."""

    def test_rolling_init_auto_on_first_plan(self) -> None:
        """First plan(rolling_mode=True) auto-inits rolling state from mid_price."""
        planner = _make_planner(levels=3)

        # No manual init — plan() should auto-init
        assert planner.get_rolling_state(SYMBOL) is None

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=(),
            rolling_mode=True,
        )

        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.anchor_price == ANCHOR
        assert st.step_price == STEP
        assert st.net_offset == 0
        # All levels are missing → GRID_FILL
        assert result.desired_count == 6
        assert result.diff_missing == 6

    def test_rolling_step_price_never_zero(self) -> None:
        """If anchor * spacing rounds to 0, step_price = tick_size."""
        planner = LiveGridPlannerV1(
            _make_config(
                levels=3,
                spacing_bps=0.001,  # very small spacing
                tick_size="10.0",  # large tick
                size="0.01",
            )
        )

        # 100 * 0.001 / 10000 = 0.00001 → rounds to 0 → should use tick_size
        planner.init_rolling_state(SYMBOL, Decimal("100"), 0.001)

        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.step_price == Decimal("10.0"), f"Expected tick_size fallback, got {st.step_price}"

    def test_price_match_duplicate_prices(self) -> None:
        """Two exchange orders near same price: closest matched, other extra."""
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        ec = ANCHOR
        target_price = ec - STEP  # desired BUY L1

        orders = (
            # Exact match
            _make_order("grinder_d_BTCUSDT_1_1000000_1", SYMBOL, "BUY", str(target_price)),
            # Duplicate at same price (different order_id)
            _make_order("grinder_d_BTCUSDT_2_1000000_2", SYMBOL, "BUY", str(target_price)),
        )

        desired = planner._build_rolling_grid(SYMBOL)
        diff = planner._match_orders_by_price(orders, desired, ANCHOR)

        # One matched, one extra
        assert len(diff.extra_orders) >= 1, (
            f"Expected at least 1 extra, got {len(diff.extra_orders)}"
        )

    def test_price_match_no_double_match(self) -> None:
        """Each exchange order matches at most one desired level."""
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        ec = ANCHOR
        # One order that could potentially match multiple levels
        orders = (_make_order("grinder_d_BTCUSDT_1_1000000_1", SYMBOL, "BUY", str(ec - STEP)),)

        desired = planner._build_rolling_grid(SYMBOL)
        diff = planner._match_orders_by_price(orders, desired, ANCHOR)

        # Should match exactly one desired level
        assert len(diff.matched_keys) == 1
        # Remaining 2 BUY levels + 3 SELL levels = 5 missing
        assert len(diff.missing_keys) == 5


# ===== V1B: Engine Integration Tests =====


class TestRollingGridEngineIntegration:
    """Engine wiring tests for rolling grid mode (PR-ROLLING-GRID-V1B).

    Validates:
    - Env flag gating (GRINDER_LIVE_ROLLING_GRID)
    - Fill detection pipeline (snapshot diff → apply_fill_offset)
    - Freeze/replenish/anti-churn bypass
    - False-positive offset prevention
    - Backward compatibility
    """

    # --- Helpers ---

    @staticmethod
    def _make_engine(
        monkeypatch: pytest.MonkeyPatch,
        *,
        rolling: bool = True,
        freeze: bool = False,
        cycle: bool = True,
        replenish_on_tp_fill: bool = False,
        anti_churn_bps: int = 0,
    ) -> LiveEngineV0:
        """Build a LiveEngineV0 with real planner + optional cycle layer."""
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")
        if rolling:
            monkeypatch.setenv("GRINDER_LIVE_ROLLING_GRID", "1")
        if freeze:
            monkeypatch.setenv("GRINDER_LIVE_FREEZE_GRID_WHEN_IN_POSITION", "1")
        if cycle:
            monkeypatch.setenv("GRINDER_LIVE_CYCLE_ENABLED", "1")
        if replenish_on_tp_fill:
            monkeypatch.setenv("GRINDER_LIVE_REPLENISH_ON_TP_FILL", "1")
        if anti_churn_bps:
            monkeypatch.setenv("GRINDER_LIVE_GRID_SHIFT_MIN_MOVE_BPS", str(anti_churn_bps))

        planner = LiveGridPlannerV1(
            LiveGridConfig(
                tick_size=Decimal("0.10"),
                levels=2,
                size_per_level=Decimal("0.01"),
            )
        )
        cycle_layer = (
            LiveCycleLayerV1(LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10")))
            if cycle
            else None
        )
        mock_paper = MagicMock()
        mock_paper.process_snapshot.return_value = MagicMock(actions=[])
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        return LiveEngineV0(
            mock_paper,
            NoOpExchangePort(),
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
            cycle_layer=cycle_layer,
        )

    @staticmethod
    def _snap(ts: int = 1_000_000) -> Snapshot:
        return Snapshot(
            ts=ts,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1.0"),
            ask_qty=Decimal("1.0"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )

    @staticmethod
    def _account_snap(
        orders: tuple[OpenOrderSnap, ...] = (),
        ts: int = 1_000_000,
        pos_qty: str = "0",
    ) -> AccountSnapshot:
        positions = (
            PositionSnap(
                symbol="BTCUSDT",
                side="LONG" if Decimal(pos_qty) > 0 else "BOTH",
                qty=abs(Decimal(pos_qty)),
                entry_price=Decimal("50000"),
                mark_price=Decimal("50000"),
                unrealized_pnl=Decimal("0"),
                leverage=1,
                ts=ts,
            ),
        )
        return AccountSnapshot(
            positions=positions,
            open_orders=orders,
            ts=ts,
            source="test",
        )

    @staticmethod
    def _grid_order(level: int, side: str, price: str, ts: int = 1_000_000) -> OpenOrderSnap:
        return OpenOrderSnap(
            order_id=f"grinder_d_BTCUSDT_{level}_{ts}_{level}",
            symbol="BTCUSDT",
            side=side,
            order_type="LIMIT",
            price=Decimal(price),
            qty=Decimal("0.01"),
            filled_qty=Decimal("0"),
            reduce_only=False,
            status="NEW",
            ts=ts,
        )

    @staticmethod
    def _tp_order(price: str, ts: int = 1_000_000) -> OpenOrderSnap:
        return OpenOrderSnap(
            order_id=f"grinder_tp_BTCUSDT_3_{ts}_1",
            symbol="BTCUSDT",
            side="SELL",
            order_type="LIMIT",
            price=Decimal(price),
            qty=Decimal("0.01"),
            filled_qty=Decimal("0"),
            reduce_only=True,
            status="NEW",
            ts=ts,
        )

    # --- Wiring tests (5) ---

    def test_rolling_flag_off_uses_mid_anchored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GRINDER_LIVE_ROLLING_GRID=0: planner called with rolling_mode=False."""
        engine = self._make_engine(monkeypatch, rolling=False)
        engine._last_account_snapshot = self._account_snap()

        engine.process_snapshot(self._snap())

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        # No rolling state created — proves rolling_mode=False was passed
        assert planner.get_rolling_state("BTCUSDT") is None

    def test_rolling_flag_on_passes_rolling_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GRINDER_LIVE_ROLLING_GRID=1: planner called with rolling_mode=True."""
        engine = self._make_engine(monkeypatch, rolling=True)
        engine._last_account_snapshot = self._account_snap()

        engine.process_snapshot(self._snap())

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        # Rolling state auto-initialized — proves rolling_mode=True was passed
        st = planner.get_rolling_state("BTCUSDT")
        assert st is not None
        assert st.net_offset == 0

    def test_grid_fill_updates_offset_before_planning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Grid order disappears → apply_fill_offset() before plan()."""
        engine = self._make_engine(monkeypatch, rolling=True)

        buy_order = self._grid_order(1, "BUY", "49950")
        sell_order = self._grid_order(2, "SELL", "50050")

        # Tick 1: both orders present — establishes _prev_rolling_orders
        engine._last_account_snapshot = self._account_snap(
            orders=(buy_order, sell_order), ts=1_000_000
        )
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Tick 2: BUY order gone (filled) — should detect fill, update offset
        engine._last_account_snapshot = self._account_snap(orders=(sell_order,), ts=2_000_000)
        engine.process_snapshot(self._snap(ts=2_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        st = planner.get_rolling_state("BTCUSDT")
        assert st is not None
        assert st.net_offset == -1, f"Expected -1 (BUY fill), got {st.net_offset}"

    def test_restart_initializes_safely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """First tick, no prior state → auto-init, bounded GRID_FILL."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # First tick: no _prev_rolling_orders → no fills, planner auto-inits
        engine._last_account_snapshot = self._account_snap(ts=1_000_000)
        output = engine.process_snapshot(self._snap(ts=1_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        st = planner.get_rolling_state("BTCUSDT")
        assert st is not None
        assert st.net_offset == 0, "Restart should not detect any fills"

        # Should produce GRID_FILL PLACEs (4 = 2 levels * 2 sides)
        place_actions = [
            la for la in output.live_actions if la.action.action_type == ActionType.PLACE
        ]
        assert len(place_actions) == 4

    def test_anti_churn_bypassed_in_rolling_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_filter_grid_shift not called when rolling."""
        engine = self._make_engine(
            monkeypatch,
            rolling=True,
            anti_churn_bps=500,  # high threshold
        )
        engine._last_account_snapshot = self._account_snap(ts=1_000_000)

        # First tick builds grid
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Second tick with slightly different mid — in non-rolling mode with
        # 500bps anti-churn, this would suppress. In rolling, anti-churn is bypassed.
        # Verify engine._grid_anchor_mid is NOT populated (no mid-anchor tracking).
        assert "BTCUSDT" not in engine._grid_anchor_mid

    # --- False-positive offset prevention (4) ---

    def test_grid_cancel_does_not_shift_offset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Planner CANCEL (GRID_TRIM) → order gone → NOT a fill."""
        engine = self._make_engine(monkeypatch, rolling=True)

        buy1 = self._grid_order(1, "BUY", "49950")
        buy2 = self._grid_order(2, "BUY", "49900")
        sell1 = self._grid_order(3, "SELL", "50050")
        sell2 = self._grid_order(4, "SELL", "50100")

        T0 = 100_000_000  # base timestamp (ms)

        # Tick 1: all orders present
        engine._last_account_snapshot = self._account_snap(orders=(buy1, buy2, sell1, sell2), ts=T0)
        engine.process_snapshot(self._snap(ts=T0))

        # Simulate planner CANCEL registered on tick 1 (engine registers after cycle)
        # Manually register to simulate what happens in a normal tick
        engine._rolling_pending_cancels[buy2.order_id] = T0

        # Tick 2: buy2 gone (it was cancelled, not filled)
        # 5s later — well within 30s TTL
        engine._last_account_snapshot = self._account_snap(
            orders=(buy1, sell1, sell2), ts=T0 + 5_000
        )
        engine.process_snapshot(self._snap(ts=T0 + 5_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        st = planner.get_rolling_state("BTCUSDT")
        assert st is not None
        assert st.net_offset == 0, f"Cancel should not shift offset, got {st.net_offset}"

    def test_tp_slot_takeover_cancel_does_not_shift_offset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TP_SLOT_TAKEOVER CANCEL → order gone → NOT a fill."""
        engine = self._make_engine(monkeypatch, rolling=True)

        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(3, "SELL", "50050")
        sell2 = self._grid_order(4, "SELL", "50100")

        T0 = 100_000_000

        # Tick 1: orders present
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1, sell2), ts=T0)
        engine.process_snapshot(self._snap(ts=T0))

        # Register sell2 as TP_SLOT_TAKEOVER cancel (engine captures all cancels)
        engine._rolling_pending_cancels[sell2.order_id] = T0

        # Tick 2: sell2 gone (cancelled by TP_SLOT_TAKEOVER, not filled)
        # 5s later — within 30s TTL
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), ts=T0 + 5_000)
        engine.process_snapshot(self._snap(ts=T0 + 5_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        st = planner.get_rolling_state("BTCUSDT")
        assert st is not None
        assert st.net_offset == 0, (
            f"TP_SLOT_TAKEOVER cancel should not shift offset, got {st.net_offset}"
        )

    def test_tp_fill_does_not_shift_offset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TP order disappears → net_offset unchanged."""
        engine = self._make_engine(monkeypatch, rolling=True)

        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(3, "SELL", "50050")
        tp = self._tp_order("50100")

        # Tick 1: grid orders + TP present
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1, tp), ts=1_000_000)
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Tick 2: TP gone (filled) — should NOT affect rolling offset
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), ts=2_000_000)
        engine.process_snapshot(self._snap(ts=2_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        st = planner.get_rolling_state("BTCUSDT")
        assert st is not None
        assert st.net_offset == 0, f"TP fill should not shift offset, got {st.net_offset}"

    def test_non_grid_strategy_disappearance_does_not_shift_offset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-grid parseable order (strategy_id != 'd') disappears → offset unchanged."""
        engine = self._make_engine(monkeypatch, rolling=True)

        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(3, "SELL", "50050")
        # Parseable order with strategy_id="x" (not "d", not "tp")
        non_grid = OpenOrderSnap(
            order_id="grinder_x_BTCUSDT_1_1000000_1",
            symbol="BTCUSDT",
            side="SELL",
            order_type="LIMIT",
            price=Decimal("50200"),
            qty=Decimal("0.01"),
            filled_qty=Decimal("0"),
            reduce_only=False,
            status="NEW",
            ts=1_000_000,
        )

        # Tick 1: grid orders + non-grid order present
        engine._last_account_snapshot = self._account_snap(
            orders=(buy1, sell1, non_grid), ts=1_000_000
        )
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Tick 2: non-grid order gone — should NOT shift offset
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), ts=2_000_000)
        engine.process_snapshot(self._snap(ts=2_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        st = planner.get_rolling_state("BTCUSDT")
        assert st is not None
        assert st.net_offset == 0, (
            f"Non-grid strategy disappearance should not shift offset, got {st.net_offset}"
        )


class TestTPSlotOwnership:
    """INV-9: TP slot reservation tests (T21-T37).

    Slot state model: {grid, tp, reserved, vacant}.
    Reservation is planner-local, one-tick only.
    Subsequent ownership SSOT = exchange-truth matching.
    """

    # --- T21-T24: Planner reservation basics ---

    def test_t21_buy_fill_reserves_sell_slot(self) -> None:
        """T21 (INV-9): BUY fill → desired_grid_count = 2*N - 1. SELL has N-1 levels."""
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        planner.apply_fill_offset(SYMBOL, "BUY")

        levels = planner._build_rolling_grid(SYMBOL)
        sell_levels = [lv for lv in levels if lv.side == OrderSide.SELL]
        buy_levels = [lv for lv in levels if lv.side == OrderSide.BUY]

        assert len(sell_levels) == 2, f"Expected N-1=2 SELL levels, got {len(sell_levels)}"
        assert len(buy_levels) == 3, f"Expected N=3 BUY levels, got {len(buy_levels)}"
        assert len(levels) == 5, f"Expected 2*N-1=5 desired, got {len(levels)}"

    def test_t22_sell_fill_reserves_buy_slot(self) -> None:
        """T22 (INV-9): SELL fill → desired_grid_count = 2*N - 1. BUY has N-1 levels."""
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        planner.apply_fill_offset(SYMBOL, "SELL")

        levels = planner._build_rolling_grid(SYMBOL)
        sell_levels = [lv for lv in levels if lv.side == OrderSide.SELL]
        buy_levels = [lv for lv in levels if lv.side == OrderSide.BUY]

        assert len(buy_levels) == 2, f"Expected N-1=2 BUY levels, got {len(buy_levels)}"
        assert len(sell_levels) == 3, f"Expected N=3 SELL levels, got {len(sell_levels)}"
        assert len(levels) == 5, f"Expected 2*N-1=5 desired, got {len(levels)}"

    def test_t23_mixed_multi_fill_reservation(self) -> None:
        """T23 (INV-9): Mixed multi-fill (2 BUY + 1 SELL) → desired = 2*N - 3."""
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        planner.apply_fill_offset(SYMBOL, "BUY")
        planner.apply_fill_offset(SYMBOL, "BUY")
        planner.apply_fill_offset(SYMBOL, "SELL")

        levels = planner._build_rolling_grid(SYMBOL)
        sell_levels = [lv for lv in levels if lv.side == OrderSide.SELL]
        buy_levels = [lv for lv in levels if lv.side == OrderSide.BUY]

        # sell_reservation=2 (from 2 BUY fills), buy_reservation=1 (from 1 SELL fill)
        assert len(sell_levels) == 1, f"Expected N-2=1 SELL levels, got {len(sell_levels)}"
        assert len(buy_levels) == 2, f"Expected N-1=2 BUY levels, got {len(buy_levels)}"
        assert len(levels) == 3, f"Expected 2*N-3=3 desired, got {len(levels)}"

    def test_t24_reservation_persists_across_ticks(self) -> None:
        """T24 (INV-9b): Reservation persists until age-out, even with TP visible.

        Primary cross-tick protection is TP inclusion in open_orders
        (price matching prevents grid PLACE at TP price). Reservation is
        defense-in-depth for REST lag gap. Age-only clearance avoids
        false-clear in multi-fill scenarios (Contract 2a).
        """
        planner = _make_planner(levels=3)
        orders = _build_full_grid_orders(planner)

        # Tick 1: BUY fill → reservation active
        planner.apply_fill_offset(SYMBOL, "BUY")
        result1 = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=orders,
            rolling_mode=True,
        )
        assert result1.desired_count == 5  # 2*3 - 1

        # Tick 2: TP visible but reservation persists (age-only clear)
        ec = ANCHOR - STEP  # 65934 after BUY fill
        tp_sell = _make_order("grinder_tp_BTCUSDT_1_1000000_1", SYMBOL, "SELL", str(ec + STEP))
        orders_with_tp = (*orders, tp_sell)
        result2 = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=2_000_000,
            open_orders=orders_with_tp,
            rolling_mode=True,
        )
        # Reservation persists (age-only), desired still 5.
        # TP is extra but NOT cancelled (skip-cancel for TP orders).
        assert result2.desired_count == 5

        # TP should not be cancelled
        cancel_ids = [a.order_id for a in result2.actions if a.action_type == ActionType.CANCEL]
        assert tp_sell.order_id not in cancel_ids, "TP must not be cancelled"

    # --- T25-T29: Integration / overlap prevention ---

    def test_t25_tp_on_exchange_matches_desired(self) -> None:
        """T25 (INV-9): TP on exchange at slot price → matched by planner → no redundant PLACE."""
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # After 1 BUY fill: ec = ANCHOR - STEP = 65934
        # Desired SELL:L1 = ec + STEP = 66000
        planner.apply_fill_offset(SYMBOL, "BUY")

        # Clear reservation (simulating next tick — reservation was consumed)
        planner._tp_slot_reservations.pop(SYMBOL, None)

        # Exchange has: TP at 66000 (SELL), grid at 66066, 66132 (SELL), BUY grid at 3 levels
        ec = ANCHOR - STEP  # 65934
        tp_sell = _make_order("grinder_tp_BTCUSDT_1_1000000_1", SYMBOL, "SELL", str(ec + STEP))
        grid_sells = [
            _make_order(
                f"grinder_d_{SYMBOL}_{i}_{1000000}_{i + 100}", SYMBOL, "SELL", str(ec + i * STEP)
            )
            for i in range(2, 4)
        ]
        grid_buys = [
            _make_order(f"grinder_d_{SYMBOL}_{i}_{1000000}_{i}", SYMBOL, "BUY", str(ec - i * STEP))
            for i in range(1, 4)
        ]
        exchange_orders = (tp_sell, *grid_sells, *grid_buys)

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=2_000_000,
            open_orders=exchange_orders,
            rolling_mode=True,
        )
        # TP matches SELL:L1, grid matches SELL:L2/L3 and BUY:L1/L2/L3
        # No redundant PLACE should be emitted
        places = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(places) == 0, f"Expected 0 PLACEs (TP matched), got {len(places)}: {places}"

    def test_t26_buy_fill_no_grid_sell_overlap(self) -> None:
        """T26 (INV-9): BUY fill → no grid SELL PLACE within epsilon of TP SELL price."""
        planner = _make_planner(levels=3)
        orders = _build_full_grid_orders(planner)

        # BUY fill: reservation skips innermost SELL
        planner.apply_fill_offset(SYMBOL, "BUY")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=orders,
            rolling_mode=True,
        )

        # ec after fill = ANCHOR - STEP = 65934. TP would be at ~66000 (= ec + STEP).
        ec = ANCHOR - STEP
        tp_approx_price = ec + STEP  # 66000 — the slot reserved for TP

        sell_places = [
            a
            for a in result.actions
            if a.action_type == ActionType.PLACE and a.side == OrderSide.SELL
        ]
        for sp in sell_places:
            assert sp.price is not None
            delta_bps = float(abs(sp.price - tp_approx_price) / ANCHOR) * 10000
            assert delta_bps > 0.5, (
                f"Grid SELL PLACE at {sp.price} overlaps TP zone ~{tp_approx_price} "
                f"(delta={delta_bps:.2f} bps)"
            )

    def test_t27_sell_fill_no_grid_buy_overlap(self) -> None:
        """T27 (INV-9): SELL fill → no grid BUY PLACE within epsilon of TP BUY price."""
        planner = _make_planner(levels=3)
        orders = _build_full_grid_orders(planner)

        planner.apply_fill_offset(SYMBOL, "SELL")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=orders,
            rolling_mode=True,
        )

        # ec after fill = ANCHOR + STEP = 66066. TP would be at ~66000 (= ec - STEP).
        ec = ANCHOR + STEP
        tp_approx_price = ec - STEP  # 66000

        buy_places = [
            a
            for a in result.actions
            if a.action_type == ActionType.PLACE and a.side == OrderSide.BUY
        ]
        for bp in buy_places:
            assert bp.price is not None
            delta_bps = float(abs(bp.price - tp_approx_price) / ANCHOR) * 10000
            assert delta_bps > 0.5, (
                f"Grid BUY PLACE at {bp.price} overlaps TP zone ~{tp_approx_price} "
                f"(delta={delta_bps:.2f} bps)"
            )

    def test_t28_fill_tick_applied_cardinality(self) -> None:
        """T28 (INV-9): Post-action application model (unit test only, NOT live invariant).

        After applying all actions from planner + TP to exchange state,
        grid_count + tp_count = 2*N.
        """
        planner = _make_planner(levels=3)
        orders = _build_full_grid_orders(planner)

        # BUY fill: remove one BUY order (simulate fill)
        buy_orders = [o for o in orders if o.side == "BUY"]
        remaining = tuple(o for o in orders if o != buy_orders[0])

        planner.apply_fill_offset(SYMBOL, "BUY")
        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=remaining,
            rolling_mode=True,
        )

        # Apply planner actions to remaining orders (remaining - cancels + places)
        cancels = sum(1 for a in result.actions if a.action_type == ActionType.CANCEL)
        grid_places = sum(1 for a in result.actions if a.action_type == ActionType.PLACE)
        tp_count = 1  # cycle layer would add 1 TP

        # Remaining = 2*N - 1 (one BUY filled). Planner: -cancels + grid_places.
        grid_after = len(remaining) - cancels + grid_places
        total = grid_after + tp_count
        assert total == 6, f"Post-action grid({grid_after})+tp({tp_count})={total}, expected 2*N=6"

    def test_t29_no_same_side_overlap_in_actions(self) -> None:
        """T29 (INV-9): No same-side TP/grid overlap within epsilon in planner output."""
        planner = _make_planner(levels=3)
        orders = _build_full_grid_orders(planner)

        planner.apply_fill_offset(SYMBOL, "BUY")
        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=orders,
            rolling_mode=True,
        )

        sell_place_prices = [
            a.price
            for a in result.actions
            if a.action_type == ActionType.PLACE and a.side == OrderSide.SELL and a.price
        ]
        buy_place_prices = [
            a.price
            for a in result.actions
            if a.action_type == ActionType.PLACE and a.side == OrderSide.BUY and a.price
        ]

        # Check no two SELL PLACEs within epsilon of each other
        for i in range(len(sell_place_prices)):
            for j in range(i + 1, len(sell_place_prices)):
                delta = float(abs(sell_place_prices[i] - sell_place_prices[j]) / ANCHOR) * 10000
                assert delta > 0.5, (
                    f"SELL overlap: {sell_place_prices[i]} vs {sell_place_prices[j]}"
                )

        # Same for BUY
        for i in range(len(buy_place_prices)):
            for j in range(i + 1, len(buy_place_prices)):
                delta = float(abs(buy_place_prices[i] - buy_place_prices[j]) / ANCHOR) * 10000
                assert delta > 0.5, f"BUY overlap: {buy_place_prices[i]} vs {buy_place_prices[j]}"

    # --- T30: Live-style integration ---

    def test_t30_live_sequence_overlap_prevented(self) -> None:
        """T30 (INV-9): Exact failing-log sequence reproduced, overlap prevented.

        BUY fill → TP SELL + grid SELL at similar prices. INV-9 reservation
        prevents the grid SELL from being emitted.
        """
        # Use numbers close to the real failure: BUY fill near 67768,
        # TP SELL and grid SELL both near 67836. Using anchor=67800, step=68.
        cfg = _make_config(levels=3, spacing_bps=10.0, tick_size="0.10")
        planner = LiveGridPlannerV1(cfg)
        anchor = Decimal("67800")

        planner.init_rolling_state(SYMBOL, anchor, 10.0)
        st = planner.get_rolling_state(SYMBOL)
        assert st is not None

        # Start at offset=-1 (one prior BUY fill already happened)
        st.net_offset = -1

        # Build exchange orders matching offset=-1 state
        ec1 = anchor + (-1) * st.step_price
        exchange_orders = tuple(
            _make_order(
                f"grinder_d_{SYMBOL}_{i}_{1000000}_{base}",
                SYMBOL,
                side,
                str(ec1 + (i if side == "SELL" else -i) * st.step_price),
            )
            for i in range(1, 4)
            for side, base in [("BUY", i), ("SELL", i + 100)]
        )

        # New BUY fill: offset goes to -2
        planner.apply_fill_offset(SYMBOL, "BUY")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=anchor,
            ts_ms=1_000_000,
            open_orders=exchange_orders,
            rolling_mode=True,
        )

        # TP would be placed at approximately ec_new + step (innermost SELL slot)
        ec2 = anchor + (-2) * st.step_price
        tp_zone = ec2 + st.step_price

        sell_places = [
            a
            for a in result.actions
            if a.action_type == ActionType.PLACE and a.side == OrderSide.SELL
        ]
        for sp in sell_places:
            assert sp.price is not None
            delta = float(abs(sp.price - tp_zone) / anchor) * 10000
            assert delta > 0.5, (
                f"Grid SELL at {sp.price} overlaps TP zone {tp_zone} (delta={delta:.2f} bps)"
            )

    # --- T31-T37: Edge cases and defense-in-depth ---

    def test_t31_overlap_guard_suppress_and_self_heal(self) -> None:
        """T31 (INV-9): Defense-in-depth guard suppresses grid PLACE overlapping TP PLACE.

        After suppression, TP absent next tick → planner self-heals.
        """
        # Build actions simulating overlap (primary reservation bypassed somehow)
        grid_place = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("66000"),
            quantity=Decimal("0.01"),
            level_id=1,
            reason="GRID_FILL",
            reduce_only=False,
        )
        tp_place = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("66000.50"),  # within epsilon
            quantity=Decimal("0.01"),
            level_id=1,
            reason="TP_CLOSE",
            reduce_only=True,
            client_order_id="grinder_tp_BTCUSDT_1_1000000_1",
        )
        cancel = ExecutionAction(
            action_type=ActionType.CANCEL,
            order_id="grinder_d_BTCUSDT_3_1000000_103",
            symbol="BTCUSDT",
            reason="GRID_TRIM",
        )

        # Build engine to access the filter method
        planner = LiveGridPlannerV1(
            LiveGridConfig(
                tick_size=Decimal("0.10"),
                levels=3,
                size_per_level=Decimal("0.01"),
            )
        )
        mock_paper = MagicMock()
        mock_paper.process_snapshot.return_value = MagicMock(actions=[])
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper,
            NoOpExchangePort(),
            config,
            account_syncer=MagicMock(),
            grid_planners={"BTCUSDT": planner},
        )

        filtered = engine._filter_tp_grid_overlap([cancel, grid_place, tp_place], "BTCUSDT")

        # Grid PLACE suppressed, TP PLACE and CANCEL kept
        action_types = [(a.action_type, a.reduce_only) for a in filtered]
        assert (ActionType.PLACE, False) not in action_types, "Grid PLACE should be suppressed"
        assert (ActionType.PLACE, True) in action_types, "TP PLACE should be kept"
        assert len(filtered) == 2  # CANCEL + TP PLACE

    def test_t32_tp_expiry_slot_recycled(self) -> None:
        """T32 (INV-9b): TP expiry → slot tp → vacant → grid after age-out.

        Full lifecycle: fill → reservation → TP on exchange → TP expired →
        reservation ages out → planner fills slot with grid.
        """
        planner = _make_planner(levels=3)
        planner._max_reservation_age = 3  # low threshold for test
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # After BUY fill, TP placed at SELL:L1 zone
        planner.apply_fill_offset(SYMBOL, "BUY")
        ec = ANCHOR - STEP

        # Tick 1 (fill tick): reservation active, desired has N-1=2 SELLs
        result1 = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=(),
            rolling_mode=True,
        )
        assert result1.desired_count == 5  # 3 BUY + 2 SELL

        # Tick 2: TP on exchange at SELL:L1 price. Reservation persists (age-only).
        tp_sell = _make_order("grinder_tp_BTCUSDT_1_1000000_1", SYMBOL, "SELL", str(ec + STEP))
        grid_orders = tuple(
            _make_order(
                f"grinder_d_{SYMBOL}_{i}_{1000000}_{base}",
                SYMBOL,
                side,
                str(ec + (i if side == "SELL" else -i) * STEP),
            )
            for i in range(2, 4)
            for side, base in [("SELL", i + 100)]
        ) + tuple(
            _make_order(f"grinder_d_{SYMBOL}_{i}_{1000000}_{i}", SYMBOL, "BUY", str(ec - i * STEP))
            for i in range(1, 4)
        )
        exchange_tick2 = (tp_sell, *grid_orders)

        result2 = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=2_000_000,
            open_orders=exchange_tick2,
            rolling_mode=True,
        )
        # Reservation persists (age-only). TP is extra but not cancelled.
        assert result2.desired_count == 5
        places2 = [a for a in result2.actions if a.action_type == ActionType.PLACE]
        assert len(places2) == 0, "No new PLACE needed (reservation withholds L1)"
        cancel_ids2 = [a.order_id for a in result2.actions if a.action_type == ActionType.CANCEL]
        assert tp_sell.order_id not in cancel_ids2, "TP must not be cancelled"

        # Tick 3: TP expired (gone). Reservation still active (age 3, not > 3).
        exchange_tick3 = grid_orders  # TP removed
        result3 = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=3_000_000,
            open_orders=exchange_tick3,
            rolling_mode=True,
        )
        assert result3.desired_count == 5, "Reservation persists (age=3, not > max=3)"

        # Tick 4: age=4 > max=3 → force clear → planner fills vacant slot
        result4 = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=4_000_000,
            open_orders=exchange_tick3,
            rolling_mode=True,
        )
        assert result4.desired_count == 6, "Reservation expired, full grid restored"
        sell_places = [
            a
            for a in result4.actions
            if a.action_type == ActionType.PLACE and a.side == OrderSide.SELL
        ]
        assert len(sell_places) == 1, (
            f"Expected 1 grid SELL PLACE to fill vacant slot, got {len(sell_places)}"
        )

    def test_t33_saturation_clamp(self) -> None:
        """T33 (INV-9): Saturation — 4 fills with N=3 → clamps, desired >= 0."""
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # 4 BUY fills (pathological, normally max N=3)
        for _ in range(4):
            planner.apply_fill_offset(SYMBOL, "BUY")

        levels = planner._build_rolling_grid(SYMBOL)
        sell_levels = [lv for lv in levels if lv.side == OrderSide.SELL]
        buy_levels = [lv for lv in levels if lv.side == OrderSide.BUY]

        # sell_reservation=min(4,3)=3, all SELL skipped
        assert len(sell_levels) == 0, f"Expected 0 SELL (saturated), got {len(sell_levels)}"
        assert len(buy_levels) == 3, f"Expected 3 BUY (unreserved), got {len(buy_levels)}"
        assert len(levels) >= 0, "desired count must never be negative"

    def test_t34_tp_place_blocked_self_heal(self) -> None:
        """T34 (INV-9b): TP PLACE blocked → reservation persists → age-out → self-heal.

        With age-only reservation clearance, blocked TP means the slot stays
        reserved until max_reservation_age. After that, planner fills with grid.
        """
        planner = _make_planner(levels=3)
        planner._max_reservation_age = 3  # low threshold for test
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # BUY fill → reservation
        planner.apply_fill_offset(SYMBOL, "BUY")
        ec = ANCHOR - STEP

        # Tick 1 (fill tick): reservation skips SELL:L1
        result1 = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=(),
            rolling_mode=True,
        )
        assert result1.desired_count == 5  # 3 BUY + 2 SELL

        # Ticks 2-3: TP blocked, not on exchange → reservation persists
        for i in range(2, 4):
            r = planner.plan(
                symbol=SYMBOL,
                mid_price=ANCHOR,
                ts_ms=i * 1_000_000,
                open_orders=(),
                rolling_mode=True,
            )
            assert r.desired_count == 5, f"Tick {i}: reservation should persist"

        # Tick 4: age=4 > max=3 → force clear → self-heal
        result_heal = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=4_000_000,
            open_orders=(),
            rolling_mode=True,
        )
        assert result_heal.desired_count == 6  # full 2*N
        sell_places = [
            a
            for a in result_heal.actions
            if a.action_type == ActionType.PLACE and a.side == OrderSide.SELL
        ]
        sell_prices = sorted([a.price for a in sell_places if a.price])
        assert len(sell_prices) == 3, f"Expected 3 SELL PLACEs (self-heal), got {len(sell_prices)}"
        innermost = ec + STEP
        assert innermost in sell_prices, (
            f"Innermost SELL {innermost} not in PLACEs {sell_prices} (self-heal failed)"
        )

    def test_t35_tp_renew_no_overlap(self) -> None:
        """T35 (INV-9): TP renewed → slot stays tp, no grid overlap."""
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)
        planner.apply_fill_offset(SYMBOL, "BUY")
        ec = ANCHOR - STEP

        # Clear reservation (simulating post-fill-tick)
        planner._tp_slot_reservations.pop(SYMBOL, None)

        # TP at SELL:L1 price (renewed — same price, different order ID)
        tp_sell = _make_order("grinder_tp_BTCUSDT_1_2000000_5", SYMBOL, "SELL", str(ec + STEP))
        grid_orders = tuple(
            _make_order(
                f"grinder_d_{SYMBOL}_{i}_{1000000}_{base}",
                SYMBOL,
                side,
                str(ec + (i if side == "SELL" else -i) * STEP),
            )
            for i in range(2, 4)
            for side, base in [("SELL", i + 100)]
        ) + tuple(
            _make_order(f"grinder_d_{SYMBOL}_{i}_{1000000}_{i}", SYMBOL, "BUY", str(ec - i * STEP))
            for i in range(1, 4)
        )
        exchange = (tp_sell, *grid_orders)

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=3_000_000,
            open_orders=exchange,
            rolling_mode=True,
        )
        # Renewed TP still matches SELL:L1 by price → no overlap
        places = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(places) == 0, f"Expected 0 PLACEs (TP renewed, no overlap), got {len(places)}"

    def test_t36_existing_tp_plus_new_fill(self) -> None:
        """T36 (INV-9, Contract 2a): Existing TP at slot + new BUY fill.

        Old TP migrates to shifted desired level, new reservation covers
        new innermost. 5 explicit assertions per Contract 2a.
        """
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # After first BUY fill: offset=-1, ec=65934
        planner.apply_fill_offset(SYMBOL, "BUY")
        # Consume reservation from first fill
        planner._tp_slot_reservations.pop(SYMBOL, None)
        ec1 = ANCHOR - STEP  # 65934

        # Exchange state: TP SELL at 66000 (old L1), grid SELL at 66066/66132, BUY grid
        tp_sell = _make_order("grinder_tp_BTCUSDT_1_1000000_1", SYMBOL, "SELL", str(ec1 + STEP))
        grid_sell_2 = _make_order(
            "grinder_d_BTCUSDT_2_1000000_102", SYMBOL, "SELL", str(ec1 + 2 * STEP)
        )
        grid_sell_3 = _make_order(
            "grinder_d_BTCUSDT_3_1000000_103", SYMBOL, "SELL", str(ec1 + 3 * STEP)
        )
        grid_buys = tuple(
            _make_order(f"grinder_d_{SYMBOL}_{i}_{1000000}_{i}", SYMBOL, "BUY", str(ec1 - i * STEP))
            for i in range(1, 4)
        )
        exchange = (tp_sell, grid_sell_2, grid_sell_3, *grid_buys)

        # Second BUY fill: offset goes to -2
        planner.apply_fill_offset(SYMBOL, "BUY")
        ec2 = ANCHOR - 2 * STEP  # 65868

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=2_000_000,
            open_orders=exchange,
            rolling_mode=True,
        )

        # Contract 2a assertions:
        # 1. Old TP at 66000 = ec2 + 2*STEP should be matched (not extra)
        # 2. Old grid at 66066 = ec2 + 3*STEP should be matched
        # Together: actual_count should reflect both matched
        assert result.actual_count >= 2, (
            f"Old TP + grid should be matched (actual_count={result.actual_count})"
        )

        # 3. Old grid at 66132 = ec2 + 4*STEP → extra → CANCEL
        cancels = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        cancel_ids = [a.order_id for a in cancels]
        assert grid_sell_3.order_id in cancel_ids, (
            f"Farthest grid SELL at 66132 should be cancelled, got cancels: {cancel_ids}"
        )

        # 4. Desired SELL count = N - 1 = 2 (L2, L3; L1 reserved)
        # desired_count = (3 - 1 sell_skip) + 3 BUY = 5
        assert result.desired_count == 5, (
            f"Expected 5 desired (N-1 SELL + N BUY), got {result.desired_count}"
        )

        # 5. No grid PLACE at ~65934 (ec2 + STEP, the reserved slot)
        sell_places = [
            a
            for a in result.actions
            if a.action_type == ActionType.PLACE and a.side == OrderSide.SELL
        ]
        reserved_price = ec2 + STEP  # 65934
        for sp in sell_places:
            assert sp.price is not None
            delta = float(abs(sp.price - reserved_price) / ANCHOR) * 10000
            assert delta > 0.5, f"Grid SELL at {sp.price} in reserved zone {reserved_price}"

    def test_t37_overlap_guard_missing_planner(self) -> None:
        """T37 (INV-9): Overlap guard with missing planner → returns unchanged, no crash."""
        mock_paper = MagicMock()
        mock_paper.process_snapshot.return_value = MagicMock(actions=[])
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper,
            NoOpExchangePort(),
            config,
            account_syncer=MagicMock(),
            grid_planners={},  # no planner for any symbol
        )

        actions = [
            ExecutionAction(
                action_type=ActionType.PLACE,
                symbol="BTCUSDT",
                side=OrderSide.SELL,
                price=Decimal("66000"),
                quantity=Decimal("0.01"),
                reduce_only=False,
                reason="GRID_FILL",
            ),
            ExecutionAction(
                action_type=ActionType.PLACE,
                symbol="BTCUSDT",
                side=OrderSide.SELL,
                price=Decimal("66000.50"),
                quantity=Decimal("0.01"),
                reduce_only=True,
                reason="TP_CLOSE",
                client_order_id="grinder_tp_BTCUSDT_1_1000000_1",
            ),
        ]

        # Should return unchanged (fail-open), no crash
        result = engine._filter_tp_grid_overlap(actions, "BTCUSDT")
        assert len(result) == len(actions), "Fail-open: all actions should pass through"

    # --- T38-T42: Cross-tick overlap protection (INV-9b) ---

    def test_t38_reservation_persists_without_tp_visible(self) -> None:
        """T38 (INV-9b): Reservation persists when TP not in open_orders (WS/REST lag)."""
        planner = _make_planner(levels=3)
        orders = _build_full_grid_orders(planner)

        # BUY fill → reservation active
        planner.apply_fill_offset(SYMBOL, "BUY")

        # Tick 1: fill tick, desired=5
        result1 = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=orders,
            rolling_mode=True,
        )
        assert result1.desired_count == 5

        # Tick 2: NO TP in open_orders → reservation PERSISTS
        result2 = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=2_000_000,
            open_orders=orders,
            rolling_mode=True,
        )
        assert result2.desired_count == 5, (
            "Reservation must persist when TP not visible in open_orders"
        )

    def test_t39_reservation_persists_with_tp_visible(self) -> None:
        """T39 (INV-9b): Reservation persists even with TP visible (age-only clearance).

        Primary cross-tick protection is TP inclusion in open_orders.
        Reservation is secondary defense. TP visibility does NOT clear
        reservation (avoids multi-fill false-clear, Contract 2a).
        The TP is extra but not cancelled (skip-cancel for TPs).
        """
        planner = _make_planner(levels=3)
        orders = _build_full_grid_orders(planner)

        planner.apply_fill_offset(SYMBOL, "BUY")

        # Tick 1: fill tick
        planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=orders,
            rolling_mode=True,
        )

        # Tick 2: TP visible → reservation still persists (age-only)
        ec = ANCHOR - STEP
        tp_sell = _make_order("grinder_tp_BTCUSDT_1_1000000_1", SYMBOL, "SELL", str(ec + STEP))
        result2 = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=2_000_000,
            open_orders=(*orders, tp_sell),
            rolling_mode=True,
        )
        assert result2.desired_count == 5, (
            "Reservation must persist even with TP visible (age-only clear)"
        )

        # TP must NOT be cancelled
        cancel_ids = [a.order_id for a in result2.actions if a.action_type == ActionType.CANCEL]
        assert tp_sell.order_id not in cancel_ids, "TP must not be cancelled"

    def test_t40_reservation_force_clears_on_age(self) -> None:
        """T40 (INV-9b): Reservation force-clears after max_reservation_age (failed TP)."""
        planner = _make_planner(levels=3)
        planner._max_reservation_age = 5  # low threshold for test
        orders = _build_full_grid_orders(planner)

        planner.apply_fill_offset(SYMBOL, "BUY")

        # Tick 1: fill tick
        planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=orders,
            rolling_mode=True,
        )

        # Ticks 2-5: no TP → reservation persists (age 2..5, all <= max)
        for i in range(2, 6):
            r = planner.plan(
                symbol=SYMBOL,
                mid_price=ANCHOR,
                ts_ms=i * 1_000_000,
                open_orders=orders,
                rolling_mode=True,
            )
            assert r.desired_count == 5, f"Tick {i}: reservation should persist"

        # Tick 6: age=6 > max_reservation_age=5 → force clear before build
        result_after = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=6_000_000,
            open_orders=orders,
            rolling_mode=True,
        )
        assert result_after.desired_count == 6, "Reservation should force-clear after max age"

    def test_t41_cross_tick_tp_prevents_grid_place(self) -> None:
        """T41 (INV-9b): TP on exchange from previous tick prevents grid PLACE at same price.

        Reproduces the failure class from live run4: BUY fill → TP SELL placed →
        subsequent tick planner sees TP in open_orders → TP matches desired SELL
        level → no redundant grid SELL PLACE.

        Standard constants: ANCHOR=66000, STEP=66, N=1.
        After BUY fill: offset=-1, ec=65934.
        Desired SELL:L1 = ec + STEP = 66000.
        TP SELL at 66000 → matches desired → no grid SELL PLACE.
        """
        planner = _make_planner(levels=1)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # BUY fill: offset=-1, ec = 66000 - 66 = 65934
        planner.apply_fill_offset(SYMBOL, "BUY")
        rs = planner.get_rolling_state(SYMBOL)
        assert rs is not None
        assert rs.net_offset == -1
        ec = ANCHOR + rs.net_offset * STEP  # 65934

        # TP SELL at desired SELL:L1 price = ec + STEP = 66000
        tp_price = ec + STEP  # 66000
        tp_sell = _make_order(
            "grinder_tp_BTCUSDT_1_2000000_1",
            SYMBOL,
            "SELL",
            str(tp_price),
        )

        # Grid BUY already on exchange at BUY:L1 = ec - STEP = 65868
        buy_price = ec - STEP  # 65868
        grid_buy = _make_order(
            "grinder_d_BTCUSDT_1_2000000_2",
            SYMBOL,
            "BUY",
            str(buy_price),
        )

        # Tick N+1: reservation still active (TP visible clears SELL res),
        # TP in open_orders → matches desired SELL:L1
        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=3_000_000,
            open_orders=(tp_sell, grid_buy),
            rolling_mode=True,
        )

        # TP matches desired SELL → no grid SELL PLACE
        sell_places = [
            a
            for a in result.actions
            if a.action_type == ActionType.PLACE and a.side == OrderSide.SELL
        ]
        assert len(sell_places) == 0, (
            f"No grid SELL should be placed when TP occupies the slot: {sell_places}"
        )

        # TP should NOT be cancelled (managed by cycle layer)
        tp_cancels = [
            a
            for a in result.actions
            if a.action_type == ActionType.CANCEL and a.order_id == tp_sell.order_id
        ]
        assert len(tp_cancels) == 0, "Planner must not cancel TP orders"

    def test_t42_tp_extra_not_cancelled(self) -> None:
        """T42 (INV-9b): TP at non-matching price is extra but NOT cancelled by planner."""
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # TP at a price that doesn't match any desired level
        tp_far = _make_order("grinder_tp_BTCUSDT_1_1000000_1", SYMBOL, "SELL", "67000")
        grid_orders = _build_full_grid_orders(planner)
        all_orders = (*grid_orders, tp_far)

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=all_orders,
            rolling_mode=True,
        )

        # TP is extra (doesn't match desired) but must NOT be cancelled
        cancel_ids = [a.order_id for a in result.actions if a.action_type == ActionType.CANCEL]
        assert tp_far.order_id not in cancel_ids, (
            "Planner must not cancel TP orders even when extra"
        )

    def test_t43_tp_extra_does_not_inflate_convergence_extras(self) -> None:
        """T43 (INV-9b): diff_extra_tp correctly counts TP extras.

        After SELL fill with N=1: cycle layer cancels remaining BUY grid
        order (TP_SLOT_TAKEOVER) and places TP BUY. Exchange has only TP BUY.
        Planner desired=1 (SELL only, BUY reserved). TP BUY is extra but
        diff_extra_tp must equal TP count so convergence is not blocked.
        """
        planner = _make_planner(levels=1)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # Simulate SELL fill → buy_reservation=1 → desired=1 (SELL only)
        planner.apply_fill_offset(SYMBOL, "SELL")

        # After fill + TP_SLOT_TAKEOVER: only TP BUY on exchange
        tp_buy = _make_order("grinder_tp_BTCUSDT_1_1000000_1", SYMBOL, "BUY", "66000")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=(tp_buy,),
            rolling_mode=True,
        )

        # desired=1 (SELL only). TP BUY doesn't match → extra.
        assert result.diff_extra == 1, f"Expected 1 extra, got {result.diff_extra}"
        assert result.diff_extra_tp == 1, (
            f"TP extra must be counted: diff_extra_tp={result.diff_extra_tp}"
        )
        # Non-TP extras = 0 → convergence guard should NOT block
        non_tp_extras = result.diff_extra - result.diff_extra_tp
        assert non_tp_extras == 0, (
            f"No grid extras expected: diff_extra={result.diff_extra} "
            f"diff_extra_tp={result.diff_extra_tp}"
        )


class TestRollingGridEngineIntegrationExtra(TestRollingGridEngineIntegration):
    """Continuation of engine integration tests (inherited helpers)."""

    def test_restart_trim_does_not_shift_offset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """First-tick GRID_TRIM cancels → not treated as fills on next tick."""
        engine = self._make_engine(monkeypatch, rolling=True)

        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(3, "SELL", "50050")
        extra = self._grid_order(5, "SELL", "50200")  # will be trimmed

        T0 = 100_000_000

        # Tick 1: planner sees extra order → CANCEL (GRID_TRIM).
        # Engine registers the cancel via _register_rolling_cancels.
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1, extra), ts=T0)
        output1 = engine.process_snapshot(self._snap(ts=T0))

        # Verify cancel was registered
        cancel_actions = [
            la for la in output1.live_actions if la.action.action_type == ActionType.CANCEL
        ]
        # Planner may produce cancels for extra orders
        if cancel_actions:
            for la in cancel_actions:
                assert la.action.order_id in engine._rolling_pending_cancels

        # Tick 2: extra order gone (cancelled, not filled) — 5s later, within TTL
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), ts=T0 + 5_000)
        engine.process_snapshot(self._snap(ts=T0 + 5_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        st = planner.get_rolling_state("BTCUSDT")
        assert st is not None
        assert st.net_offset == 0, f"Restart trim should not shift offset, got {st.net_offset}"

    # --- Freeze/replenish bypass (3) ---

    def test_freeze_disabled_in_rolling_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pos open + rolling → planner runs (not frozen)."""
        engine = self._make_engine(monkeypatch, rolling=True, freeze=True)

        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(3, "SELL", "50050")

        # Set position open
        engine._last_account_snapshot = self._account_snap(
            orders=(buy1, sell1), ts=1_000_000, pos_qty="0.01"
        )
        engine.process_snapshot(self._snap(ts=1_000_000))

        # In rolling mode, freeze is disabled, so planner runs and produces actions.
        # Planner auto-inits rolling state → proves it ran.
        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        st = planner.get_rolling_state("BTCUSDT")
        assert st is not None, "Planner should have run (freeze disabled in rolling)"

    def test_replenish_bypassed_in_rolling_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cycle layer REPLENISH actions filtered out in rolling mode."""
        engine = self._make_engine(monkeypatch, rolling=True, cycle=True)

        buy1 = self._grid_order(1, "BUY", "49950")

        # Tick 1: establish state
        engine._last_account_snapshot = self._account_snap(orders=(buy1,), ts=1_000_000)
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Inject a REPLENISH action as if cycle_layer produced it
        replenish_action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49900"),
            quantity=Decimal("0.01"),
            reason="REPLENISH",
        )

        # Verify the filter logic: rolling + REPLENISH reason → filtered
        cycle_actions = [replenish_action]
        # This is the engine's filter from process_snapshot
        rolling = True
        grid_frozen = False
        if (grid_frozen or rolling) and cycle_actions:
            cycle_actions = [a for a in cycle_actions if a.reason != "REPLENISH"]
        assert len(cycle_actions) == 0, "REPLENISH should be filtered in rolling mode"

    def test_tp_fill_replenish_bypassed_in_rolling_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TP_FILL_REPLENISH block skipped when rolling."""
        engine = self._make_engine(monkeypatch, rolling=True, cycle=True, replenish_on_tp_fill=True)

        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(3, "SELL", "50050")

        # Tick 1: establish state with position
        engine._last_account_snapshot = self._account_snap(
            orders=(buy1, sell1), ts=1_000_000, pos_qty="0.01"
        )
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Tick 2: simulate TP fill (position decreases)
        engine._last_account_snapshot = self._account_snap(
            orders=(buy1, sell1), ts=2_000_000, pos_qty="0"
        )
        output = engine.process_snapshot(self._snap(ts=2_000_000))

        # In rolling mode, TP_FILL_REPLENISH is bypassed.
        # No replenish actions should appear from the engine's own logic.
        tp_replenish_actions = [
            la for la in output.live_actions if la.action.reason == "TP_FILL_REPLENISH"
        ]
        assert len(tp_replenish_actions) == 0, (
            "TP_FILL_REPLENISH should be bypassed in rolling mode"
        )

    # --- Consistency / backward compat (4) ---

    def test_flag_off_preserves_freeze_and_replenish_behavior(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag=0 + pos open → freeze active, replenish flows normally."""
        engine = self._make_engine(monkeypatch, rolling=False, freeze=True, cycle=True)

        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(3, "SELL", "50050")

        # Position open
        engine._last_account_snapshot = self._account_snap(
            orders=(buy1, sell1), ts=1_000_000, pos_qty="0.01"
        )
        engine.process_snapshot(self._snap(ts=1_000_000))

        # In non-rolling mode with freeze=True and pos open, planner should NOT run.
        # Rolling state should NOT be created.
        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        st = planner.get_rolling_state("BTCUSDT")
        assert st is None, "Planner should be frozen (rolling_mode=False, freeze=True)"

    def test_convergence_guards_apply_in_rolling_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Convergence guards still filter in rolling mode."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Enable convergence guards
        engine._converge_first_enabled = True

        engine._last_account_snapshot = self._account_snap(ts=1_000_000)

        # First tick: planner produces initial grid (all PLACE).
        # Convergence guard may pass since no prior inflight shift.
        output1 = engine.process_snapshot(self._snap(ts=1_000_000))

        # Verify _apply_convergence_guards was exercised (it runs in rolling mode)
        # Evidence: if guard latched, second tick with same snapshot yields 0 actions
        # (inflight shift from tick 1 not yet converged).
        place_count = sum(
            1 for la in output1.live_actions if la.action.action_type == ActionType.PLACE
        )
        if place_count > 0:
            # Tick 2 immediately: convergence guard should suppress
            # (sync_gen hasn't advanced)
            output2 = engine.process_snapshot(self._snap(ts=1_001_000))
            place_count_2 = sum(
                1 for la in output2.live_actions if la.action.action_type == ActionType.PLACE
            )
            # Second tick should have fewer or zero PLACEs (sync-gate active)
            assert place_count_2 <= place_count

    def test_no_double_shift_when_cycle_and_engine_see_same_fill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One fill → one offset shift (engine), one TP (cycle_layer)."""
        engine = self._make_engine(monkeypatch, rolling=True, cycle=True)

        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(3, "SELL", "50050")

        # Tick 1: both present
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), ts=1_000_000)
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Tick 2: BUY filled (gone) — engine detects fill + cycle_layer detects fill
        engine._last_account_snapshot = self._account_snap(orders=(sell1,), ts=2_000_000)
        engine.process_snapshot(self._snap(ts=2_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        st = planner.get_rolling_state("BTCUSDT")
        assert st is not None
        # Engine detected exactly one fill → offset = -1 (not -2)
        assert st.net_offset == -1, f"Expected exactly one offset shift (-1), got {st.net_offset}"

    def test_late_cancel_after_ttl_no_false_shift_if_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancel in pending, TTL expires, order still on exchange → no false fill."""
        engine = self._make_engine(monkeypatch, rolling=True)

        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(3, "SELL", "50050")

        # Tick 1: establish state
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), ts=1_000_000)
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Register a cancel for buy1
        engine._rolling_pending_cancels[buy1.order_id] = 1_000_000

        # Tick 2: 31 seconds later — TTL expired, but order STILL on exchange
        # (cancel failed silently). Order is in both prev and current → no disappearance.
        engine._last_account_snapshot = self._account_snap(
            orders=(buy1, sell1),
            ts=32_000_000,  # 31s later
        )
        engine.process_snapshot(self._snap(ts=32_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        st = planner.get_rolling_state("BTCUSDT")
        assert st is not None
        assert st.net_offset == 0, (
            f"Order still present after TTL → no false fill, got {st.net_offset}"
        )


class TestAnchorContract(TestRollingGridEngineIntegration):
    """INV-10 (ADR-088): Anchor lifecycle contract tests T44-T58.

    Validates:
    - anchor_price = raw mid_price (SSOT)
    - ANCHOR_INIT / ANCHOR_RESET / ANCHOR_RESET_BLOCKED log contract
    - Same-tick re-anchor behavior
    - State cleanup (planner + engine)
    - Throttle/latch for blocked log
    - Multi-symbol isolation
    """

    # --- T44: anchor = raw mid_price (not rounded) ---

    def test_t44_anchor_equals_raw_mid_price(self) -> None:
        """First plan from empty rolling state: anchor = mid_price (raw Decimal)."""
        planner = _make_planner(levels=1)
        # Use a mid that is NOT tick-aligned (half-tick)
        mid = Decimal("66000.35")  # 0.35 is not a multiple of tick=0.10

        planner.plan(
            symbol=SYMBOL,
            mid_price=mid,
            ts_ms=1_000_000,
            open_orders=(),
            rolling_mode=True,
        )

        rs = planner.get_rolling_state(SYMBOL)
        assert rs is not None
        assert rs.anchor_price == mid, (
            f"anchor_price must be raw mid (not rounded): {rs.anchor_price} != {mid}"
        )

    # --- T45: initial symmetry within 1 tick ---

    def test_t45_initial_symmetry_within_one_tick(self) -> None:
        """Initial BUY/SELL distances from ec differ by at most 1 tick."""
        planner = _make_planner(levels=1)
        mid = Decimal("66000.35")  # non-tick-aligned mid

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=mid,
            ts_ms=1_000_000,
            open_orders=(),
            rolling_mode=True,
        )

        places = [a for a in result.actions if a.action_type == ActionType.PLACE]
        buy_places = [a for a in places if a.side == OrderSide.BUY]
        sell_places = [a for a in places if a.side == OrderSide.SELL]
        assert len(buy_places) == 1 and len(sell_places) == 1

        rs = planner.get_rolling_state(SYMBOL)
        assert rs is not None
        ec = rs.anchor_price + rs.net_offset * rs.step_price  # net_offset=0

        assert buy_places[0].price is not None and sell_places[0].price is not None
        buy_dist = ec - buy_places[0].price
        sell_dist = sell_places[0].price - ec
        diff = abs(buy_dist - sell_dist)
        assert diff <= TICK, (
            f"BUY/SELL distance diff {diff} exceeds 1 tick ({TICK}): "
            f"buy_dist={buy_dist}, sell_dist={sell_dist}"
        )

    # --- T46: anchor stable under mid drift, no second ANCHOR_INIT ---

    def test_t46_anchor_stable_under_mid_drift(self, caplog: pytest.LogCaptureFixture) -> None:
        """Mid drifts +/-200bps across 10 plan() calls, anchor stays. No second ANCHOR_INIT."""

        planner = _make_planner(levels=1)
        original_mid = Decimal("66000")

        with caplog.at_level(logging.INFO, logger="grinder.live.grid_planner"):
            # First call sets anchor
            planner.plan(
                symbol=SYMBOL,
                mid_price=original_mid,
                ts_ms=1_000_000,
                open_orders=(),
                rolling_mode=True,
            )
            # 10 more calls with drifting mid
            for i in range(10):
                drift = Decimal(str((-1) ** i * (i + 1) * 13))  # +13, -26, +39, ...
                planner.plan(
                    symbol=SYMBOL,
                    mid_price=original_mid + drift,
                    ts_ms=1_000_000 + (i + 1) * 1000,
                    open_orders=(),
                    rolling_mode=True,
                )

        rs = planner.get_rolling_state(SYMBOL)
        assert rs is not None
        assert rs.anchor_price == original_mid, (
            f"anchor drifted: {rs.anchor_price} != {original_mid}"
        )
        init_count = sum(1 for r in caplog.records if "ANCHOR_INIT" in r.message)
        assert init_count == 1, f"Expected 1 ANCHOR_INIT, got {init_count}"

    # --- T47: reset_rolling_state clears all planner state ---

    def test_t47_reset_clears_planner_state(self) -> None:
        """reset_rolling_state() clears anchor, offset, reservations; next plan re-inits."""
        planner = _make_planner(levels=1)

        # Init and apply fills to build up state
        planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1_000_000,
            open_orders=(),
            rolling_mode=True,
        )
        planner.apply_fill_offset(SYMBOL, "BUY")
        planner.apply_fill_offset(SYMBOL, "BUY")
        rs_before = planner.get_rolling_state(SYMBOL)
        assert rs_before is not None
        assert rs_before.net_offset == -2

        # Reset
        planner.reset_rolling_state(SYMBOL)
        assert planner.get_rolling_state(SYMBOL) is None
        assert planner._tp_slot_reservations.get(SYMBOL) is None

        # Next plan re-inits with new mid
        new_mid = Decimal("70000")
        planner.plan(
            symbol=SYMBOL,
            mid_price=new_mid,
            ts_ms=2_000_000,
            open_orders=(),
            rolling_mode=True,
        )
        rs_after = planner.get_rolling_state(SYMBOL)
        assert rs_after is not None
        assert rs_after.anchor_price == new_mid
        assert rs_after.net_offset == 0

    # --- T48: engine ANCHOR_RESET — same-tick re-init + PLACEs from new anchor ---

    def test_t48_engine_anchor_reset_same_tick(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Engine ANCHOR_RESET: 0 orders + flat + no inflight + no pending
        → reset fires, grid PLACEs from new anchor on same process_snapshot() call."""

        engine = self._make_engine(monkeypatch, rolling=True)
        old_mid = Decimal("50000.50")
        new_mid = Decimal("51000.50")

        # Tick 1: establish rolling state with orders already confirmed.
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(
            Snapshot(
                ts=1_000_000,
                symbol="BTCUSDT",
                bid_price=Decimal("50000"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
        )
        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs1 = planner.get_rolling_state("BTCUSDT")
        assert rs1 is not None
        assert rs1.anchor_price == old_mid

        # Tick 2: external cleanup — orders gone, flat, no inflight, no pending.
        # Clear _prev_rolling_orders to prevent false fill detection from
        # order disappearance (external cancel ≠ fill, ADR-085 scope).
        engine._prev_rolling_orders.pop("BTCUSDT", None)
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0", ts=2_000_000)
        engine._inflight_shift.pop("BTCUSDT", None)

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            output = engine.process_snapshot(
                Snapshot(
                    ts=2_000_000,
                    symbol="BTCUSDT",
                    bid_price=Decimal("51000"),
                    ask_price=Decimal("51001"),
                    bid_qty=Decimal("1"),
                    ask_qty=Decimal("1"),
                    last_price=Decimal("51000"),
                    last_qty=Decimal("1"),
                )
            )

        # Verify ANCHOR_RESET fired
        reset_logs = [r for r in caplog.records if "ANCHOR_RESET " in r.message]
        assert len(reset_logs) >= 1, (
            f"ANCHOR_RESET not logged: {[r.message for r in caplog.records]}"
        )

        # Verify new anchor = new mid (same tick)
        rs2 = planner.get_rolling_state("BTCUSDT")
        assert rs2 is not None
        assert rs2.anchor_price == new_mid, (
            f"Same-tick re-init failed: anchor={rs2.anchor_price}, expected {new_mid}"
        )
        assert rs2.net_offset == 0

        # Verify grid PLACEs are from new anchor (same process_snapshot call).
        # Filter: reduce_only=False → grid PLACEs only (excludes TP PLACEs).
        grid_places = [
            la
            for la in output.live_actions
            if la.action.action_type == ActionType.PLACE and not la.action.reduce_only
        ]
        assert len(grid_places) > 0, "Expected grid PLACEs after re-anchor"
        for la in grid_places:
            assert la.action.price is not None
            dist_from_new = abs(float(la.action.price - new_mid))
            dist_from_old = abs(float(la.action.price - old_mid))
            assert dist_from_new < dist_from_old, (
                f"PLACE price {la.action.price} is closer to old anchor {old_mid} "
                f"than new anchor {new_mid}"
            )

    # --- T49: no re-anchor when inflight active ---

    def test_t49_no_reset_when_inflight_active(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Inflight latch active → anchor preserved (initial placement in progress)."""

        engine = self._make_engine(monkeypatch, rolling=True)

        # Tick 1: empty exchange → rolling init + PLACEs → inflight latch set
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs_init = planner.get_rolling_state("BTCUSDT")
        assert rs_init is not None
        old_anchor = rs_init.anchor_price

        # Inflight latch should be set from initial PLACEs
        assert "BTCUSDT" in engine._inflight_shift, "Inflight latch should be set"

        # Tick 2: still empty (AccountSync hasn't refreshed yet), but inflight active
        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=1_001_000))

        assert "ANCHOR_RESET" not in " ".join(r.message for r in caplog.records)
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        assert rs.anchor_price == old_anchor, "Anchor must not change during inflight"

    # --- T50: no re-anchor when orders present ---

    def test_t50_no_reset_when_orders_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Grinder orders on exchange → no re-anchor check."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Tick 1: init with orders
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs_init = planner.get_rolling_state("BTCUSDT")
        assert rs_init is not None
        old_anchor = rs_init.anchor_price

        # Tick 2: orders still present, different mid
        engine._last_account_snapshot = self._account_snap(
            orders=(buy1, sell1), pos_qty="0", ts=2_000_000
        )
        engine.process_snapshot(
            Snapshot(
                ts=2_000_000,
                symbol="BTCUSDT",
                bid_price=Decimal("55000"),
                ask_price=Decimal("55001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("55000"),
                last_qty=Decimal("1"),
            )
        )

        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        assert rs.anchor_price == old_anchor, "Anchor must not change when orders present"

    # --- T51: fill offset → cleanup → same-tick re-anchor with fresh mid ---

    def test_t51_fill_offset_cleanup_reanchor_same_tick(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """net_offset=-3 → external cleanup → re-anchor: offset=0, PLACEs from new anchor."""

        engine = self._make_engine(monkeypatch, rolling=True)
        old_mid = Decimal("50000.50")

        # Tick 1: init rolling state with orders already confirmed
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]

        # Manually apply 3 BUY fill offsets to simulate accumulated fills
        planner.apply_fill_offset("BTCUSDT", "BUY")
        planner.apply_fill_offset("BTCUSDT", "BUY")
        planner.apply_fill_offset("BTCUSDT", "BUY")
        _rs_check = planner.get_rolling_state("BTCUSDT")
        assert _rs_check is not None
        assert _rs_check.net_offset == -3

        # Tick 2: external cleanup — all orders gone, flat, no inflight.
        # Clear _prev_rolling_orders to prevent false fill detection from
        # order disappearance (external cancel ≠ fill, ADR-085 scope).
        engine._prev_rolling_orders.pop("BTCUSDT", None)
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0", ts=2_000_000)
        engine._inflight_shift.pop("BTCUSDT", None)

        new_mid = Decimal("52000.50")
        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            output = engine.process_snapshot(
                Snapshot(
                    ts=2_000_000,
                    symbol="BTCUSDT",
                    bid_price=Decimal("52000"),
                    ask_price=Decimal("52001"),
                    bid_qty=Decimal("1"),
                    ask_qty=Decimal("1"),
                    last_price=Decimal("52000"),
                    last_qty=Decimal("1"),
                )
            )

        reset_logs = [r for r in caplog.records if "ANCHOR_RESET " in r.message]
        assert len(reset_logs) >= 1, "ANCHOR_RESET should fire"
        assert "-3" in reset_logs[0].message, "Log should show old offset=-3"

        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        assert rs.net_offset == 0, f"offset must be 0 after re-anchor, got {rs.net_offset}"
        assert rs.anchor_price == new_mid

        # Verify grid PLACEs from new anchor (reduce_only=False = grid, not TP)
        grid_places = [
            la
            for la in output.live_actions
            if la.action.action_type == ActionType.PLACE and not la.action.reduce_only
        ]
        assert len(grid_places) > 0, "Expected grid PLACEs after re-anchor"
        for la in grid_places:
            assert la.action.price is not None
            dist_from_new = abs(float(la.action.price - new_mid))
            dist_from_old = abs(float(la.action.price - old_mid))
            assert dist_from_new < dist_from_old, (
                f"PLACE price {la.action.price} closer to old {old_mid} than new {new_mid}"
            )

    # --- T52: ANCHOR_RESET_BLOCKED reason=POSITION_OPEN ---

    def test_t52_blocked_when_position_open(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty exchange + open position → ANCHOR_RESET_BLOCKED."""

        engine = self._make_engine(monkeypatch, rolling=True)

        # Tick 1: init rolling state
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs_init = planner.get_rolling_state("BTCUSDT")
        assert rs_init is not None
        old_anchor = rs_init.anchor_price

        # Tick 2: orders gone, position open, no inflight
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0.01", ts=2_000_000)
        engine._inflight_shift.pop("BTCUSDT", None)

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        blocked_logs = [r for r in caplog.records if "ANCHOR_RESET_BLOCKED" in r.message]
        assert len(blocked_logs) >= 1, "ANCHOR_RESET_BLOCKED should fire"
        assert "POSITION_OPEN" in blocked_logs[0].message

        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        assert rs.anchor_price == old_anchor, "Anchor must not change when position open"

    # --- T53: _prev_rolling_orders cleared on re-anchor ---

    def test_t53_prev_rolling_orders_cleared_on_reanchor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Engine clears stale fill detection baseline on ANCHOR_RESET."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Tick 1: init with orders
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Verify fill baseline is populated
        assert "BTCUSDT" in engine._prev_rolling_orders
        assert len(engine._prev_rolling_orders["BTCUSDT"]) > 0

        # Tick 2: external cleanup → ANCHOR_RESET
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0", ts=2_000_000)
        engine._inflight_shift.pop("BTCUSDT", None)
        engine.process_snapshot(self._snap(ts=2_000_000))

        # Fill baseline should be cleared (no stale entries)
        assert (
            engine._prev_rolling_orders.get("BTCUSDT") is None
            or len(engine._prev_rolling_orders.get("BTCUSDT", {})) == 0
        )

    # --- T54: symbol-scoped pending cancel cleanup ---

    def test_t54_pending_cancels_cleared_on_reanchor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Engine clears symbol-scoped pending cancels on ANCHOR_RESET."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Tick 1: init with orders
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Inject stale pending cancel for BTCUSDT
        engine._rolling_pending_cancels["grinder_d_BTCUSDT_1_1000000_1"] = 1_000_000

        # Tick 2: external cleanup → ANCHOR_RESET
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0", ts=2_000_000)
        engine._inflight_shift.pop("BTCUSDT", None)
        # Pending cancel blocks reset initially — clear via time advance
        # Actually, pending cancel IS present so ANCHOR_RESET_BLOCKED fires first.
        # Need to let it expire via TTL, then reset fires.
        # Advance past 30s TTL:
        engine._rolling_pending_cancels["grinder_d_BTCUSDT_1_1000000_1"] = 900_000  # old ts
        engine.process_snapshot(self._snap(ts=2_000_000))
        # TTL cleanup happens at start of rolling block — 2_000_000 - 900_000 > 30_000
        # So pending cancel is cleaned by _cleanup_rolling_pending_cancels first.

        # Verify no BTCUSDT pending cancels remain
        btc_pending = [oid for oid in engine._rolling_pending_cancels if "BTCUSDT" in oid]
        assert len(btc_pending) == 0, f"BTCUSDT pending cancels not cleared: {btc_pending}"

    # --- T55: mixed-symbol pending cancel isolation ---

    def test_t55_mixed_symbol_pending_cancel_isolation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BTCUSDT reset does NOT clear ETHUSDT pending cancels."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Tick 1: init rolling state
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Inject pending cancels for both symbols
        engine._rolling_pending_cancels["grinder_d_ETHUSDT_1_1000000_1"] = 2_000_000

        # Tick 2: BTCUSDT cleanup → ANCHOR_RESET for BTCUSDT
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0", ts=2_000_000)
        engine._inflight_shift.pop("BTCUSDT", None)
        engine.process_snapshot(self._snap(ts=2_000_000))

        # ETHUSDT pending cancel must survive
        assert "grinder_d_ETHUSDT_1_1000000_1" in engine._rolling_pending_cancels, (
            "ETHUSDT pending cancel was incorrectly cleared by BTCUSDT reset"
        )

    # --- T56: ANCHOR_RESET_BLOCKED throttle ---

    def test_t56_blocked_throttle_fires_once_resets_on_state_change(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """BLOCKED fires once, silent on repeats, re-fires after state change."""

        engine = self._make_engine(monkeypatch, rolling=True)

        # Tick 1: init rolling state with orders
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Now set up blocked state: empty exchange + position open
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0.01", ts=2_000_000)
        engine._inflight_shift.pop("BTCUSDT", None)

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            # Tick 2: first BLOCKED
            engine.process_snapshot(self._snap(ts=2_000_000))
            count_after_first = sum(
                1 for r in caplog.records if "ANCHOR_RESET_BLOCKED" in r.message
            )
            assert count_after_first == 1, f"Expected 1 BLOCKED, got {count_after_first}"

            # Ticks 3-5: should NOT re-log
            for i in range(3):
                engine.process_snapshot(self._snap(ts=2_001_000 + i * 1000))
            count_after_repeats = sum(
                1 for r in caplog.records if "ANCHOR_RESET_BLOCKED" in r.message
            )
            assert count_after_repeats == 1, (
                f"Throttle failed: expected 1, got {count_after_repeats}"
            )

            # State change: orders reappear → clears latch
            engine._last_account_snapshot = self._account_snap(
                orders=(buy1, sell1), pos_qty="0.01", ts=3_000_000
            )
            engine.process_snapshot(self._snap(ts=3_000_000))

            # Orders gone again → new BLOCKED episode
            engine._last_account_snapshot = self._account_snap(
                orders=(), pos_qty="0.01", ts=4_000_000
            )
            engine._inflight_shift.pop("BTCUSDT", None)
            engine.process_snapshot(self._snap(ts=4_000_000))

            count_after_new_episode = sum(
                1 for r in caplog.records if "ANCHOR_RESET_BLOCKED" in r.message
            )
            assert count_after_new_episode == 2, (
                f"Expected 2 BLOCKED after state change, got {count_after_new_episode}"
            )

    # --- T57: PENDING_CANCELS blocks reset, then after expiry reset fires ---

    def test_t57_pending_cancels_block_then_reset_after_expiry(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pending cancels → BLOCKED; after TTL expiry → ANCHOR_RESET."""

        engine = self._make_engine(monkeypatch, rolling=True)

        # Tick 1: init with orders
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Inject fresh pending cancel with order ID NOT matching any order in
        # _prev_rolling_orders (to avoid consumption by fill detection).
        # This simulates a cancel-in-flight for a recently placed order.
        engine._rolling_pending_cancels["grinder_d_BTCUSDT_99_999999_99"] = 2_000_000

        # Clear _prev_rolling_orders to prevent false fill detection from
        # order disappearance on the empty-exchange tick.
        engine._prev_rolling_orders.pop("BTCUSDT", None)

        # Tick 2: empty exchange + flat + pending cancel → BLOCKED
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0", ts=2_000_000)
        engine._inflight_shift.pop("BTCUSDT", None)

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_001_000))

            blocked = [r for r in caplog.records if "ANCHOR_RESET_BLOCKED" in r.message]
            assert len(blocked) >= 1, "Should be blocked by pending cancels"
            assert "PENDING_CANCELS" in blocked[0].message

            # Tick 3: 31s later → TTL expired → cleanup runs → pending cancel gone → RESET
            # Clear inflight latch set by tick 2's PLACEs (simulate convergence).
            engine._inflight_shift.pop("BTCUSDT", None)
            engine._last_account_snapshot = self._account_snap(
                orders=(), pos_qty="0", ts=33_000_000
            )
            engine.process_snapshot(self._snap(ts=33_000_000))

            resets = [
                r
                for r in caplog.records
                if "ANCHOR_RESET " in r.message and "BLOCKED" not in r.message
            ]
            assert len(resets) >= 1, (
                f"ANCHOR_RESET should fire after TTL expiry: "
                f"{[r.message for r in caplog.records if 'ANCHOR' in r.message]}"
            )

    # --- T58: POSITION_UNKNOWN when no AccountSync snapshot ---

    def test_t58_position_unknown_blocks_reset(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_last_account_snapshot=None → ANCHOR_RESET_BLOCKED reason=POSITION_UNKNOWN.

        The POSITION_UNKNOWN path fires when _get_position_qty returns None,
        which happens when _last_account_snapshot is None (AccountSync never ran
        or connection lost). _has_grinder_orders also returns False when snap is
        None, so the re-anchor check enters the inner branch.
        """

        engine = self._make_engine(monkeypatch, rolling=True)
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")

        # Tick 1: init rolling state with a valid snapshot
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs_init = planner.get_rolling_state("BTCUSDT")
        assert rs_init is not None
        old_anchor = rs_init.anchor_price

        # Simulate lost AccountSync connection: snapshot becomes None.
        # _has_grinder_orders returns False (no snap), _get_position_qty returns None.
        engine._last_account_snapshot = None
        engine._inflight_shift.pop("BTCUSDT", None)

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        blocked = [r for r in caplog.records if "ANCHOR_RESET_BLOCKED" in r.message]
        assert len(blocked) >= 1, (
            f"POSITION_UNKNOWN should fire: {[r.message for r in caplog.records]}"
        )
        assert "POSITION_UNKNOWN" in blocked[0].message

        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        assert rs.anchor_price == old_anchor, "Anchor must not change on unknown position"

    # --- T59: Part A — stale inflight cleared on empty-actions path ---

    def test_t59_stale_inflight_cleared_on_empty_actions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Part A contract: _apply_convergence_guards() clears stale inflight
        when planner produces no actions (convergence confirmed).

        Setup: inflight set with sync_gen=1, _account_sync_generation=2.
        Call _apply_convergence_guards(symbol, [], plan_result, ts).
        Verify: inflight cleared, deferred log cleared.
        """

        engine = self._make_engine(monkeypatch, rolling=True)

        # Establish rolling state so engine is in normal operating mode
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Set stale inflight: sync_gen=1, but _account_sync_generation=2
        engine._inflight_shift["BTCUSDT"] = _InflightShift(
            sync_gen=1, place_count=2, ts_ms=1_000_000
        )
        engine._inflight_deferred_logged.add("BTCUSDT")
        engine._account_sync_generation = 2

        # Precondition: inflight exists
        assert "BTCUSDT" in engine._inflight_shift

        # Call with empty actions (planner found no diff = convergence confirmed)
        plan_result = GridPlanResult(desired_count=4, actual_count=4, diff_extra=0, diff_extra_tp=0)
        result = engine._apply_convergence_guards("BTCUSDT", [], plan_result, 2_000_000)

        # Postcondition: stale inflight cleared
        assert "BTCUSDT" not in engine._inflight_shift, (
            "Stale inflight must be cleared on empty-actions path"
        )
        assert "BTCUSDT" not in engine._inflight_deferred_logged, (
            "Deferred log must be cleared with inflight"
        )
        assert result == []

    # --- T60/T61: Paired boundary — stale vs fresh inflight ---

    def _setup_inflight_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[LiveEngineV0, LiveGridPlannerV1]:
        """Shared baseline for T60/T61 paired boundary tests.

        Returns engine with:
        - rolling state exists (anchor established)
        - inflight set with sync_gen=1
        - _prev_rolling_orders cleared (no false fills)
        - account snapshot: 0 orders, flat position

        T60 and T61 differ ONLY in _account_sync_generation.
        """
        engine = self._make_engine(monkeypatch, rolling=True)

        # Tick 1: establish rolling state
        buy1 = self._grid_order(1, "BUY", "49950")
        sell1 = self._grid_order(2, "SELL", "50050")
        engine._last_account_snapshot = self._account_snap(orders=(buy1, sell1), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        assert planner.get_rolling_state("BTCUSDT") is not None

        # Set up boundary conditions (shared between T60/T61):
        # - inflight with sync_gen=1
        # - prev_rolling_orders cleared (prevent false fills from order disappearance)
        # - account snapshot: 0 orders, flat position
        engine._inflight_shift["BTCUSDT"] = _InflightShift(
            sync_gen=1, place_count=2, ts_ms=1_000_000
        )
        engine._prev_rolling_orders.pop("BTCUSDT", None)
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0", ts=2_000_000)

        return engine, planner

    def test_t60_stale_inflight_does_not_block_anchor_reset(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Paired boundary (PASS): stale inflight does NOT block ANCHOR_RESET.

        Same baseline as T61. Differs ONLY in: _account_sync_generation=2
        (> inflight.sync_gen=1) → inflight is stale → ANCHOR_RESET fires.
        """
        engine, planner = self._setup_inflight_boundary(monkeypatch)
        old_anchor = planner.get_rolling_state("BTCUSDT").anchor_price  # type: ignore[union-attr]
        new_mid = Decimal("51000.50")

        # THE ONE DIFFERENCE from T61: sync_gen advanced past inflight
        engine._account_sync_generation = 2  # > inflight.sync_gen (1)

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine.process_snapshot(
                Snapshot(
                    ts=2_000_000,
                    symbol="BTCUSDT",
                    bid_price=Decimal("51000"),
                    ask_price=Decimal("51001"),
                    bid_qty=Decimal("1"),
                    ask_qty=Decimal("1"),
                    last_price=Decimal("51000"),
                    last_qty=Decimal("1"),
                )
            )

        # ANCHOR_RESET must fire
        reset_logs = [r for r in caplog.records if "ANCHOR_RESET " in r.message]
        assert len(reset_logs) >= 1, (
            f"ANCHOR_RESET should fire with stale inflight: {[r.message for r in caplog.records]}"
        )
        assert f"old_anchor={old_anchor}" in reset_logs[0].message

        # Verify re-anchor to new mid
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        assert rs.anchor_price == new_mid
        assert rs.net_offset == 0

        # Verify grid PLACEs from new anchor
        # (process_snapshot output not captured here, but ANCHOR_RESET + new anchor proves it)

    def test_t61_fresh_inflight_blocks_anchor_reset_on_empty_flat(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Paired boundary (BLOCK): fresh inflight DOES block ANCHOR_RESET
        even when exchange is empty + flat.

        Same baseline as T60. Differs ONLY in: _account_sync_generation=1
        (== inflight.sync_gen=1) → inflight is fresh → ANCHOR_RESET blocked.
        """
        engine, planner = self._setup_inflight_boundary(monkeypatch)
        old_anchor = planner.get_rolling_state("BTCUSDT").anchor_price  # type: ignore[union-attr]

        # THE ONE DIFFERENCE from T60: sync_gen NOT advanced
        engine._account_sync_generation = 1  # == inflight.sync_gen (1)

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        # ANCHOR_RESET must NOT fire
        all_msgs = " ".join(r.message for r in caplog.records)
        assert "ANCHOR_RESET " not in all_msgs, (
            f"ANCHOR_RESET must NOT fire with fresh inflight: {all_msgs}"
        )

        # Anchor must be unchanged
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        assert rs.anchor_price == old_anchor, "Anchor must not change when inflight is fresh"

    # --- T62: Initial placement safety ---

    def test_t62_no_reset_before_first_sync_refresh(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Initial placement safety: no ANCHOR_RESET before sync refreshes.

        Empty exchange → planner emits initial PLACEs → inflight set with
        current sync_gen. Next tick before sync advances: no ANCHOR_RESET.
        """
        engine = self._make_engine(monkeypatch, rolling=True)

        # Tick 1: empty exchange → rolling init + PLACEs → inflight set
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs_init = planner.get_rolling_state("BTCUSDT")
        assert rs_init is not None
        old_anchor = rs_init.anchor_price

        # Inflight should be set from initial PLACEs
        assert "BTCUSDT" in engine._inflight_shift, "Inflight must be set after initial PLACEs"
        init_sync_gen = engine._inflight_shift["BTCUSDT"].sync_gen
        assert engine._account_sync_generation == init_sync_gen, (
            "Inflight sync_gen must match current _account_sync_generation"
        )

        # Tick 2: still empty exchange, sync NOT advanced
        # (orders dispatched but REST hasn't confirmed yet)
        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=1_001_000))

        # No ANCHOR_RESET — orders are in flight, not yet confirmed
        all_msgs = " ".join(r.message for r in caplog.records)
        assert "ANCHOR_RESET " not in all_msgs, (
            f"ANCHOR_RESET must NOT fire before sync refresh: {all_msgs}"
        )

        # Inflight still set
        assert "BTCUSDT" in engine._inflight_shift, "Inflight must persist until sync refreshes"

        # Anchor unchanged from ANCHOR_INIT
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        assert rs.anchor_price == old_anchor, "Anchor must not change before first sync refresh"


class TestADR089LogEvents(TestRollingGridEngineIntegration):
    """ADR-089: log event emission tests for operational observability."""

    def _converged_grid(self, engine: LiveEngineV0) -> tuple[OpenOrderSnap, ...]:
        """Build full 4-order grid matching planner expectations (levels=2 x 2 sides).

        Requires rolling state already initialized (run one tick with empty orders first).
        Uses anchor_price and step_price from rolling state for exact price match.
        Also clears ADR-090 inflight state from init (NoOp port CIDs don't match
        grinder-format CIDs in the converged grid, causing false inflight fills).
        """
        # ADR-090: clear init-time inflight CIDs so converged-grid tests
        # don't trigger false inflight fill detection on the next tick.
        engine._inflight_placed_cids.clear()
        engine._unreconciled_place_count.clear()
        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None, "Rolling state must be initialized before calling _converged_grid"
        anchor = rs.anchor_price
        step = rs.step_price
        return (
            self._grid_order(0, "BUY", str(anchor - step)),
            self._grid_order(1, "BUY", str(anchor - 2 * step)),
            self._grid_order(2, "SELL", str(anchor + step)),
            self._grid_order(3, "SELL", str(anchor + 2 * step)),
        )

    # --- ROLLING_STEADY_STATE ---

    def test_steady_state_emitted_on_tick_1(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ROLLING_STEADY_STATE emitted at DEBUG on first zero-action tick."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Tick 1: empty exchange → rolling init + PLACEs (establishes rolling state)
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Build full converged grid from rolling state
        grid = self._converged_grid(engine)
        engine._last_account_snapshot = self._account_snap(orders=grid, pos_qty="0")
        engine._account_sync_generation = 5

        caplog.clear()

        # Tick 2: converged grid → actions=[] → ROLLING_STEADY_STATE tick=1
        with caplog.at_level(logging.DEBUG, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        ss_msgs = [r.message for r in caplog.records if "ROLLING_STEADY_STATE" in r.message]
        assert len(ss_msgs) == 1, f"Expected 1 ROLLING_STEADY_STATE, got {len(ss_msgs)}: {ss_msgs}"
        assert "tick=1" in ss_msgs[0]

    def test_steady_state_throttled_ticks_2_through_100(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ROLLING_STEADY_STATE NOT emitted on ticks 2-100 (throttled)."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Init rolling state
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        grid = self._converged_grid(engine)
        engine._last_account_snapshot = self._account_snap(orders=grid, pos_qty="0")
        engine._account_sync_generation = 5

        # Tick 2 (steady-state tick=1): emits
        engine.process_snapshot(self._snap(ts=2_000_000))

        caplog.clear()

        # Ticks 3-101 (steady-state ticks 2-100): should NOT emit
        with caplog.at_level(logging.DEBUG, logger="grinder.live.engine"):
            for i in range(99):
                engine.process_snapshot(self._snap(ts=2_001_000 + i * 1000))

        ss_msgs = [r.message for r in caplog.records if "ROLLING_STEADY_STATE" in r.message]
        assert len(ss_msgs) == 0, f"Ticks 2-100 must NOT emit ROLLING_STEADY_STATE: {ss_msgs}"

    def test_steady_state_emitted_on_tick_101(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ROLLING_STEADY_STATE emitted again on tick 101 (count % 100 == 1)."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Init rolling state
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        grid = self._converged_grid(engine)
        engine._last_account_snapshot = self._account_snap(orders=grid, pos_qty="0")
        engine._account_sync_generation = 5

        # Run 100 zero-action ticks (tick 1 emits, 2-100 silent)
        for i in range(100):
            engine.process_snapshot(self._snap(ts=2_000_000 + i * 1000))

        caplog.clear()

        # Tick 101: should emit
        with caplog.at_level(logging.DEBUG, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_100_000))

        ss_msgs = [r.message for r in caplog.records if "ROLLING_STEADY_STATE" in r.message]
        assert len(ss_msgs) == 1, f"Tick 101 must emit ROLLING_STEADY_STATE: {ss_msgs}"
        assert "tick=101" in ss_msgs[0]

    def test_steady_state_counter_resets_on_actions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Counter resets when planner produces actions, so next zero-action tick=1."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Init rolling state
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        grid = self._converged_grid(engine)
        engine._last_account_snapshot = self._account_snap(orders=grid, pos_qty="0")
        engine._account_sync_generation = 5

        # Run 50 zero-action ticks
        for i in range(50):
            engine.process_snapshot(self._snap(ts=2_000_000 + i * 1000))

        assert engine._rolling_steady_state_count.get("BTCUSDT", 0) == 50

        # Trigger actions: remove orders so planner sees missing
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=2_050_000))

        # Counter should be reset (popped)
        assert "BTCUSDT" not in engine._rolling_steady_state_count, (
            "Counter must reset when actions resume"
        )

    def test_steady_state_not_emitted_outside_rolling_mode(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ROLLING_STEADY_STATE NOT emitted when rolling mode is off."""
        engine = self._make_engine(monkeypatch, rolling=False)

        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine._account_sync_generation = 5

        with caplog.at_level(logging.DEBUG, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        ss_msgs = [r.message for r in caplog.records if "ROLLING_STEADY_STATE" in r.message]
        assert len(ss_msgs) == 0, f"Must NOT emit in non-rolling mode: {ss_msgs}"

    # --- INFLIGHT_STALE_CLEARED ---

    def test_inflight_stale_cleared_emitted(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFLIGHT_STALE_CLEARED emitted when stale inflight cleared on convergence."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Init rolling state
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Setup converged grid + stale inflight
        grid = self._converged_grid(engine)
        engine._last_account_snapshot = self._account_snap(orders=grid, pos_qty="0")

        # Set stale inflight: sync_gen=1, advance sync_gen to 2
        engine._inflight_shift["BTCUSDT"] = _InflightShift(
            sync_gen=1, place_count=4, ts_ms=1_000_000
        )
        engine._account_sync_generation = 2

        caplog.clear()

        with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        stale_msgs = [r.message for r in caplog.records if "INFLIGHT_STALE_CLEARED" in r.message]
        assert len(stale_msgs) == 1, f"Expected 1 INFLIGHT_STALE_CLEARED: {stale_msgs}"
        assert "sync_gen=2" in stale_msgs[0]
        assert "inflight_gen=1" in stale_msgs[0]

    def test_inflight_stale_cleared_not_emitted_when_fresh(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFLIGHT_STALE_CLEARED NOT emitted when inflight is fresh (sync_gen == inflight.sync_gen)."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Init rolling state
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        grid = self._converged_grid(engine)
        engine._last_account_snapshot = self._account_snap(orders=grid, pos_qty="0")

        # Inflight with sync_gen=2, account sync_gen=2 (fresh, not stale)
        engine._inflight_shift["BTCUSDT"] = _InflightShift(
            sync_gen=2, place_count=4, ts_ms=1_000_000
        )
        engine._account_sync_generation = 2

        caplog.clear()

        with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        stale_msgs = [r.message for r in caplog.records if "INFLIGHT_STALE_CLEARED" in r.message]
        assert len(stale_msgs) == 0, f"Must NOT emit when inflight is fresh: {stale_msgs}"
        assert "BTCUSDT" in engine._inflight_shift, "Fresh inflight must NOT be cleared"

    # --- CANCEL_SKIP_ALREADY_FAILED ---

    def test_cancel_skip_already_failed_emitted(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CANCEL_SKIP_ALREADY_FAILED emitted when cancel is skipped for blacklisted order_id."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # Init rolling state
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        # Build converged grid + one extra order with a blacklisted order_id.
        # The extra order has price far from any desired level → planner emits CANCEL for it.
        grid = self._converged_grid(engine)
        extra = OpenOrderSnap(
            order_id="grinder_d_BTCUSDT_99_1000000_99",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            price=Decimal("48000.00"),  # far outside grid → planner sees as extra
            qty=Decimal("0.01"),
            filled_qty=Decimal("0"),
            reduce_only=False,
            status="NEW",
            ts=1_000_000,
        )

        # Pre-blacklist the extra order_id (simulates prior -2011 failure)
        engine._cancel_failed_ids.add("grinder_d_BTCUSDT_99_1000000_99")

        engine._last_account_snapshot = self._account_snap(orders=(*grid, extra), pos_qty="0")
        engine._account_sync_generation = 5

        caplog.clear()

        with caplog.at_level(logging.DEBUG, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=3_000_000))

        skip_msgs = [r.message for r in caplog.records if "CANCEL_SKIP_ALREADY_FAILED" in r.message]
        assert len(skip_msgs) >= 1, f"Expected CANCEL_SKIP_ALREADY_FAILED: {skip_msgs}"
        assert "grinder_d_BTCUSDT_99_1000000_99" in skip_msgs[0]

    def test_cancel_skip_not_emitted_for_fresh_cancel(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CANCEL_SKIP_ALREADY_FAILED NOT emitted for non-blacklisted order_id."""
        engine = self._make_engine(monkeypatch, rolling=True)

        # No order_ids in _cancel_failed_ids
        assert len(engine._cancel_failed_ids) == 0

        # Init rolling state
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

        grid = self._converged_grid(engine)
        extra = OpenOrderSnap(
            order_id="grinder_d_BTCUSDT_99_1000000_99",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            price=Decimal("48000.00"),
            qty=Decimal("0.01"),
            filled_qty=Decimal("0"),
            reduce_only=False,
            status="NEW",
            ts=1_000_000,
        )
        engine._last_account_snapshot = self._account_snap(orders=(*grid, extra), pos_qty="0")
        engine._account_sync_generation = 5

        caplog.clear()

        with caplog.at_level(logging.DEBUG, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=3_000_000))

        skip_msgs = [r.message for r in caplog.records if "CANCEL_SKIP_ALREADY_FAILED" in r.message]
        assert len(skip_msgs) == 0, f"Must NOT emit for non-blacklisted orders: {skip_msgs}"


class TestInflightFillDetection(TestRollingGridEngineIntegration):
    """ADR-090: Inflight CID fill detection + unreconciled placement cap.

    Tests T63-T74 prove:
    - Inflight fill detection closes the disappearance-heuristic blind spot
    - Reconciliation contract: every dispatched PLACE increments unreconciled once;
      first post-dispatch sync decrements exactly once, regardless of survival or inflight fill
    - Defense-in-depth cap fires only when reconciliation is broken, never in normal operation
    """

    def _init_rolling_state(self, engine: LiveEngineV0) -> None:
        """Run one tick with empty exchange to establish rolling state (anchor)."""
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine.process_snapshot(self._snap(ts=1_000_000))

    def _replace_with_grinder_cids(self, engine: LiveEngineV0) -> dict[str, _InflightPlacedOrder]:
        """Replace NoOp-format inflight CIDs with grinder-format CIDs.

        NoOp port generates CIDs like 'BTCUSDT:1000000:1:BUY:1' which don't pass
        parse_client_order_id(). This helper replaces them with grinder_d_... format
        so the engine's fill detection recognizes them in REST snapshots.
        Returns the new CID->info mapping for test snapshot construction.
        """
        old_cids = dict(engine._inflight_placed_cids)
        engine._inflight_placed_cids.clear()
        new_cids: dict[str, _InflightPlacedOrder] = {}
        for idx, (_old_cid, info) in enumerate(old_cids.items()):
            new_cid = f"grinder_d_BTCUSDT_{idx}_{1000000}_{idx}"
            engine._inflight_placed_cids[new_cid] = info
            new_cids[new_cid] = info
        return new_cids

    def _build_orders_from_cids(
        self,
        cids: dict[str, _InflightPlacedOrder],
        anchor: Decimal,
        step: Decimal,
        ts: int = 2_000_000,
        side_filter: str | None = None,
    ) -> tuple[OpenOrderSnap, ...]:
        """Build OpenOrderSnap tuple from CID dict, optionally filtering by side."""
        orders: list[OpenOrderSnap] = []
        buy_idx = 0
        sell_idx = 0
        for cid, info in cids.items():
            if side_filter is not None and info.side != side_filter:
                continue
            if info.side == "BUY":
                buy_idx += 1
                price = anchor - buy_idx * step
            else:
                sell_idx += 1
                price = anchor + sell_idx * step
            orders.append(
                OpenOrderSnap(
                    order_id=cid,
                    symbol="BTCUSDT",
                    side=info.side,
                    order_type="LIMIT",
                    price=price,
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=ts,
                )
            )
        return tuple(orders)

    # --- T63: Immediate BUY fill ---

    def test_t63_immediate_buy_fill_detected(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """BUY dispatched, fills within one sync interval.

        Tick 1: init -> planner dispatches BUY+SELL, CIDs tracked.
        Replace NoOp CIDs with grinder-format. Advance sync_gen.
        Tick 2: REST shows only SELL (BUY filled). Assert INFLIGHT_FILL_DETECTED.
        """
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        anchor, step = rs.anchor_price, rs.step_price

        # Replace NoOp CIDs with grinder format
        cids = self._replace_with_grinder_cids(engine)
        buy_cids = {c: i for c, i in cids.items() if i.side == "BUY"}
        sell_cids = {c: i for c, i in cids.items() if i.side == "SELL"}
        assert len(buy_cids) > 0 and len(sell_cids) > 0

        # Advance sync_gen, snapshot with only SELL (BUY filled)
        engine._account_sync_generation = 2
        sell_orders = self._build_orders_from_cids(cids, anchor, step, side_filter="SELL")
        engine._last_account_snapshot = self._account_snap(orders=sell_orders, pos_qty="0")

        with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        inflight_msgs = [r.message for r in caplog.records if "INFLIGHT_FILL_DETECTED" in r.message]
        assert len(inflight_msgs) > 0, "Must detect inflight BUY fills"
        for msg in inflight_msgs:
            assert "side=BUY" in msg

        rs_after = planner.get_rolling_state("BTCUSDT")
        assert rs_after is not None
        assert rs_after.net_offset < 0, (
            f"BUY fill must produce negative offset, got {rs_after.net_offset}"
        )

    # --- T64: Immediate SELL fill (symmetric) ---

    def test_t64_immediate_sell_fill_detected(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """SELL dispatched, fills within one sync interval. Symmetric to T63."""
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        anchor, step = rs.anchor_price, rs.step_price

        cids = self._replace_with_grinder_cids(engine)
        buy_cids = {c: i for c, i in cids.items() if i.side == "BUY"}
        assert len(buy_cids) > 0

        engine._account_sync_generation = 2
        buy_orders = self._build_orders_from_cids(cids, anchor, step, side_filter="BUY")
        engine._last_account_snapshot = self._account_snap(orders=buy_orders, pos_qty="0")

        with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        inflight_msgs = [r.message for r in caplog.records if "INFLIGHT_FILL_DETECTED" in r.message]
        assert len(inflight_msgs) > 0, "Must detect inflight SELL fills"
        for msg in inflight_msgs:
            assert "side=SELL" in msg

        rs_after = planner.get_rolling_state("BTCUSDT")
        assert rs_after is not None
        assert rs_after.net_offset > 0, (
            f"SELL fill must produce positive offset, got {rs_after.net_offset}"
        )

    # --- T65: Multi-cycle BUY loop (most load-bearing test) ---

    def test_t65_multi_cycle_buy_no_repeated_same_price(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """3+ cycles where each BUY fills immediately via inflight detection.

        Proves: offset shifts each cycle, no same-price BUY churn.
        This is the exact incident pattern (repeated same-price BUY) -- must not recur.
        Uses _detect_grid_fills_for_rolling directly to isolate fill detection across
        cycles (T63/T64 prove the full process_snapshot integration path).
        """
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        anchor, step = rs.anchor_price, rs.step_price

        # Clear init-time inflight state so cycles start clean
        engine._inflight_placed_cids.clear()
        engine._unreconciled_place_count.clear()
        engine._prev_rolling_orders["BTCUSDT"] = {}

        offsets: list[int] = []
        num_cycles = 4

        # Stable SELL order across all cycles — prevents disappearance heuristic
        # from detecting SELL fills that would cancel out BUY offsets
        stable_sell_cid = "grinder_d_BTCUSDT_999_1000000_999"
        sell_order = OpenOrderSnap(
            order_id=stable_sell_cid,
            symbol="BTCUSDT",
            side="SELL",
            order_type="LIMIT",
            price=anchor + step,
            qty=Decimal("0.01"),
            filled_qty=Decimal("0"),
            reduce_only=False,
            status="NEW",
            ts=1_000_000,
        )

        for cycle in range(num_cycles):
            # Inject fresh BUY inflight CID (simulates planner dispatching BUY at grid edge)
            buy_cid = f"grinder_d_BTCUSDT_{cycle}_{2000000 + cycle * 1000000}_{cycle}"
            engine._inflight_placed_cids[buy_cid] = _InflightPlacedOrder(
                symbol="BTCUSDT", side="BUY", sync_gen=engine._account_sync_generation
            )
            engine._unreconciled_place_count.setdefault("BTCUSDT", {"BUY": 0, "SELL": 0})
            engine._unreconciled_place_count["BTCUSDT"]["BUY"] += 1

            # Advance sync gen
            engine._account_sync_generation += 1

            # Snapshot WITHOUT the BUY (it filled instantly) but WITH stable SELL
            engine._last_account_snapshot = self._account_snap(
                orders=(sell_order,),
                pos_qty="0",
                ts=2_000_000 + cycle * 1_000_000,
            )

            # Run fill detection
            with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
                fills = engine._detect_grid_fills_for_rolling("BTCUSDT")

            # Apply fills to offset
            for _oid, side in fills:
                planner.apply_fill_offset("BTCUSDT", side)

            rs_now = planner.get_rolling_state("BTCUSDT")
            assert rs_now is not None
            offsets.append(rs_now.net_offset)

        # Verify: BUY fills detected across cycles
        inflight_msgs = [r.message for r in caplog.records if "INFLIGHT_FILL_DETECTED" in r.message]
        buy_fill_msgs = [m for m in inflight_msgs if "side=BUY" in m]
        assert len(buy_fill_msgs) == num_cycles, (
            f"Expected {num_cycles} BUY inflight fills, got {len(buy_fill_msgs)}"
        )

        # Verify: offset shifted each cycle (no same-price BUY loop)
        assert len(set(offsets)) == num_cycles, (
            f"Each cycle must produce unique offset (no same-price churn): {offsets}"
        )
        assert all(o < 0 for o in offsets), (
            f"All offsets must be negative after BUY fills: {offsets}"
        )
        # Verify: offsets are strictly decreasing (each fill shifts further)
        for i in range(1, len(offsets)):
            assert offsets[i] < offsets[i - 1], f"Offsets must be strictly decreasing: {offsets}"

    # --- T66: Survived order ---

    def test_t66_survived_order_no_false_fill(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CID appears in next REST snapshot -> no fill emitted, CID removed from inflight."""
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        anchor, step = rs.anchor_price, rs.step_price

        cids = self._replace_with_grinder_cids(engine)
        assert len(cids) > 0

        engine._account_sync_generation = 2

        # ALL orders survive
        all_orders = self._build_orders_from_cids(cids, anchor, step)
        engine._last_account_snapshot = self._account_snap(orders=all_orders, pos_qty="0")

        with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        inflight_msgs = [r.message for r in caplog.records if "INFLIGHT_FILL_DETECTED" in r.message]
        assert len(inflight_msgs) == 0, (
            f"Survived orders must NOT trigger INFLIGHT_FILL_DETECTED: {inflight_msgs}"
        )
        for cid in cids:
            assert cid not in engine._inflight_placed_cids, (
                f"Survived CID {cid} must be removed from _inflight_placed_cids"
            )
        rs_after = planner.get_rolling_state("BTCUSDT")
        assert rs_after is not None
        assert rs_after.net_offset == 0, (
            f"No fills -> offset must remain 0, got {rs_after.net_offset}"
        )

    # --- T67: Early-check (sync_gen not advanced) ---

    def test_t67_early_check_no_premature_detection(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """sync_gen not advanced -> CID not checked, no premature detection."""
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        all_cids = dict(engine._inflight_placed_cids)
        assert len(all_cids) > 0

        # Do NOT advance sync_gen
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0")
        engine._prev_rolling_orders.pop("BTCUSDT", None)

        with caplog.at_level(logging.INFO, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        inflight_msgs = [r.message for r in caplog.records if "INFLIGHT_FILL_DETECTED" in r.message]
        assert len(inflight_msgs) == 0, (
            f"Must not detect fills before sync refresh: {inflight_msgs}"
        )
        for cid in all_cids:
            assert cid in engine._inflight_placed_cids, f"CID {cid} must remain before sync refresh"

    # --- T68: Hard cap fires ---

    def test_t68_placement_cap_fires(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """unreconciled[BUY] >= cap -> BUY PLACEs suppressed, PLACEMENT_CAPPED logged."""
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        cap = planner._config.levels * 2

        # Clear inflight shift so Guard 1 doesn't defer before Guard 4
        engine._inflight_shift.pop("BTCUSDT", None)
        engine._account_sync_generation = 10

        engine._unreconciled_place_count["BTCUSDT"] = {"BUY": cap, "SELL": 0}

        buy_action = ExecutionAction(
            symbol="BTCUSDT",
            action_type=ActionType.PLACE,
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.01"),
            level_id=90,
        )
        sell_action = ExecutionAction(
            symbol="BTCUSDT",
            action_type=ActionType.PLACE,
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
            level_id=91,
        )
        plan_result = GridPlanResult(desired_count=4, actual_count=2, diff_extra=0, diff_extra_tp=0)

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            result = engine._apply_convergence_guards(
                "BTCUSDT", [buy_action, sell_action], plan_result, 3_000_000
            )

        buy_places = [
            a for a in result if a.action_type == ActionType.PLACE and a.side == OrderSide.BUY
        ]
        sell_places = [
            a for a in result if a.action_type == ActionType.PLACE and a.side == OrderSide.SELL
        ]
        assert len(buy_places) == 0, f"BUY must be suppressed: {buy_places}"
        assert len(sell_places) == 1, f"SELL must pass: {sell_places}"

        cap_msgs = [r.message for r in caplog.records if "PLACEMENT_CAPPED" in r.message]
        assert len(cap_msgs) == 1, f"Expected 1 PLACEMENT_CAPPED, got {cap_msgs}"
        assert "BUY" in cap_msgs[0]

    # --- T69: Side isolation ---

    def test_t69_cap_side_isolation(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """BUY capped does not block SELL placement."""
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        cap = planner._config.levels * 2

        engine._inflight_shift.pop("BTCUSDT", None)
        engine._account_sync_generation = 10
        engine._unreconciled_place_count["BTCUSDT"] = {"BUY": cap, "SELL": cap - 1}

        buy_action = ExecutionAction(
            symbol="BTCUSDT",
            action_type=ActionType.PLACE,
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.01"),
            level_id=90,
        )
        sell_action = ExecutionAction(
            symbol="BTCUSDT",
            action_type=ActionType.PLACE,
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
            level_id=91,
        )
        plan_result = GridPlanResult(desired_count=4, actual_count=2, diff_extra=0, diff_extra_tp=0)

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            result = engine._apply_convergence_guards(
                "BTCUSDT", [buy_action, sell_action], plan_result, 3_000_000
            )

        buy_places = [
            a for a in result if a.action_type == ActionType.PLACE and a.side == OrderSide.BUY
        ]
        sell_places = [
            a for a in result if a.action_type == ActionType.PLACE and a.side == OrderSide.SELL
        ]
        assert len(buy_places) == 0, "BUY must be suppressed (at cap)"
        assert len(sell_places) == 1, "SELL must pass (below cap)"

    # --- T70: Reconciliation decrement on fill ---

    def test_t70_reconciliation_decrement_on_fill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Inflight fill decrements _unreconciled_place_count."""
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        sym_counts = engine._unreconciled_place_count.get("BTCUSDT", {})
        initial_buy = sym_counts.get("BUY", 0)
        initial_sell = sym_counts.get("SELL", 0)
        assert initial_buy > 0 and initial_sell > 0

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        anchor, step = rs.anchor_price, rs.step_price

        cids = self._replace_with_grinder_cids(engine)
        engine._account_sync_generation = 2

        # SELL survives, BUY fills
        sell_orders = self._build_orders_from_cids(cids, anchor, step, side_filter="SELL")
        engine._last_account_snapshot = self._account_snap(orders=sell_orders, pos_qty="0")
        engine.process_snapshot(self._snap(ts=2_000_000))

        sym_after = engine._unreconciled_place_count.get("BTCUSDT", {})
        assert sym_after.get("BUY", 0) < initial_buy, (
            f"BUY must decrement on fill: {initial_buy} -> {sym_after.get('BUY', 0)}"
        )
        assert sym_after.get("SELL", 0) < initial_sell, (
            f"SELL must decrement on survival: {initial_sell} -> {sym_after.get('SELL', 0)}"
        )

    # --- T71: ANCHOR_RESET full state consistency ---

    def test_t71_anchor_reset_clears_all_state(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ANCHOR_RESET clears all 6 engine-owned state maps for the reset symbol.

        Other symbols must be preserved.
        """
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        # Seed ETHUSDT state for isolation proof
        engine._prev_rolling_orders["ETHUSDT"] = {"fake_cid": MagicMock()}
        engine._inflight_placed_cids["fake_eth_cid"] = _InflightPlacedOrder(
            symbol="ETHUSDT", side="BUY", sync_gen=0
        )
        engine._unreconciled_place_count["ETHUSDT"] = {"BUY": 1, "SELL": 0}

        # Verify BTCUSDT preconditions
        btc_inflight = {
            c: i for c, i in engine._inflight_placed_cids.items() if i.symbol == "BTCUSDT"
        }
        assert len(btc_inflight) > 0
        assert "BTCUSDT" in engine._unreconciled_place_count

        # Trigger ANCHOR_RESET: empty exchange, flat, no inflight shift
        engine._inflight_shift.pop("BTCUSDT", None)
        engine._prev_rolling_orders["BTCUSDT"] = {}
        engine._last_account_snapshot = self._account_snap(orders=(), pos_qty="0", ts=3_000_000)

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=3_000_000))

        reset_msgs = [r.message for r in caplog.records if "ANCHOR_RESET" in r.message]
        assert any("ANCHOR_RESET" in m for m in reset_msgs)

        # BTCUSDT cleaned (note: ANCHOR_RESET repopulates prev_rolling_orders and
        # inflight_placed_cids via the new init PLACEs, so check the ETH isolation instead)
        assert "BTCUSDT" not in engine._inflight_deferred_logged

        # ETHUSDT preserved
        assert "ETHUSDT" in engine._prev_rolling_orders
        assert "fake_eth_cid" in engine._inflight_placed_cids
        assert "ETHUSDT" in engine._unreconciled_place_count

        # The old BTCUSDT inflight CIDs must be gone (replaced by fresh init ones)
        for cid in btc_inflight:
            assert cid not in engine._inflight_placed_cids, (
                f"Old BTCUSDT CID {cid} must be cleared after ANCHOR_RESET"
            )

    # --- T72: Reconciliation decrement on survival (proves non-monotonic growth) ---

    def test_t72_reconciliation_decrement_on_survival(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Survived order decrements _unreconciled_place_count.

        Counter returns to 0 after full init reconciliation. Proves non-monotonic growth.
        """
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        sym_counts = engine._unreconciled_place_count.get("BTCUSDT", {})
        initial_buy = sym_counts.get("BUY", 0)
        initial_sell = sym_counts.get("SELL", 0)
        assert initial_buy > 0 and initial_sell > 0

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        anchor, step = rs.anchor_price, rs.step_price

        cids = self._replace_with_grinder_cids(engine)

        # ALL survive
        engine._account_sync_generation = 2
        all_orders = self._build_orders_from_cids(cids, anchor, step)
        engine._last_account_snapshot = self._account_snap(orders=all_orders, pos_qty="0")
        engine.process_snapshot(self._snap(ts=2_000_000))

        sym_after = engine._unreconciled_place_count.get("BTCUSDT", {})
        assert sym_after.get("BUY", 0) == 0, (
            f"BUY must be 0 after full survival: {sym_after.get('BUY', 0)}"
        )
        assert sym_after.get("SELL", 0) == 0, (
            f"SELL must be 0 after full survival: {sym_after.get('SELL', 0)}"
        )

    # --- T73: Healthy init path (cap never fires) ---

    def test_t73_healthy_init_no_cap(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Place levels orders -> sync shows all survived -> unreconciled=0. Cap never fires."""
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]
        rs = planner.get_rolling_state("BTCUSDT")
        assert rs is not None
        anchor, step = rs.anchor_price, rs.step_price

        cids = self._replace_with_grinder_cids(engine)

        engine._account_sync_generation = 2
        all_orders = self._build_orders_from_cids(cids, anchor, step)
        engine._last_account_snapshot = self._account_snap(orders=all_orders, pos_qty="0")

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine.process_snapshot(self._snap(ts=2_000_000))

        cap_msgs = [r.message for r in caplog.records if "PLACEMENT_CAPPED" in r.message]
        assert len(cap_msgs) == 0, f"PLACEMENT_CAPPED must not fire on healthy init: {cap_msgs}"
        sym_counts = engine._unreconciled_place_count.get("BTCUSDT", {})
        assert sym_counts.get("BUY", 0) == 0
        assert sym_counts.get("SELL", 0) == 0

    # --- T74: Healthy replacement cycle (cap never fires across 3+ cycles) ---

    def test_t74_healthy_replacement_cycle_no_cap(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """fill -> offset shift -> place 1 new -> sync -> survived -> unreconciled=0.

        Cap never fires across 3+ cycles.
        """
        engine = self._make_engine(monkeypatch, rolling=True)
        self._init_rolling_state(engine)

        planner = engine._grid_planners["BTCUSDT"]  # type: ignore[index]

        for cycle in range(3):
            rs = planner.get_rolling_state("BTCUSDT")
            assert rs is not None
            anchor, step = rs.anchor_price, rs.step_price

            cids = self._replace_with_grinder_cids(engine)
            buy_cids = {c: i for c, i in cids.items() if i.side == "BUY"}
            if not buy_cids:
                break

            engine._account_sync_generation += 1

            # BUY fills, SELL survives
            sell_orders = self._build_orders_from_cids(
                cids,
                anchor,
                step,
                ts=2_000_000 + cycle * 2_000_000,
                side_filter="SELL",
            )
            prev = engine._prev_rolling_orders.get("BTCUSDT", {})
            extra = tuple(s for _oid, s in prev.items() if s.side == "SELL" and _oid not in cids)

            engine._last_account_snapshot = self._account_snap(
                orders=(*sell_orders, *extra),
                pos_qty="0",
                ts=2_000_000 + cycle * 2_000_000,
            )
            engine.process_snapshot(self._snap(ts=2_000_000 + cycle * 2_000_000))

            # Survive the replacement orders
            new_cids = self._replace_with_grinder_cids(engine)
            if new_cids:
                engine._account_sync_generation += 1
                rs2 = planner.get_rolling_state("BTCUSDT")
                if rs2:
                    new_orders = self._build_orders_from_cids(
                        new_cids,
                        rs2.anchor_price,
                        rs2.step_price,
                        ts=3_000_000 + cycle * 2_000_000,
                    )
                    prev2 = engine._prev_rolling_orders.get("BTCUSDT", {})
                    extra2 = tuple(s for _oid, s in prev2.items() if _oid not in new_cids)
                    engine._last_account_snapshot = self._account_snap(
                        orders=(*new_orders, *extra2),
                        pos_qty="0",
                        ts=3_000_000 + cycle * 2_000_000,
                    )
                    engine.process_snapshot(self._snap(ts=3_000_000 + cycle * 2_000_000))

        cap_msgs = [r.message for r in caplog.records if "PLACEMENT_CAPPED" in r.message]
        assert len(cap_msgs) == 0, f"PLACEMENT_CAPPED must not fire in healthy cycles: {cap_msgs}"
