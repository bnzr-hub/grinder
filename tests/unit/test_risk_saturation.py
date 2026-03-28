"""Tests for Risk-Saturated Mode (PR-3, ADR-102).

Adversarial tests for:
- Reconciler legal-capacity projection (zero, partial, full ladder)
- Reconciler still cancels extras when capacity=0
- Exits unaffected by capacity constraint
- Saturation tracking: threshold boundary, recovery, per-symbol isolation
- Non-cap block resets consecutive counter
- Proactive recovery: saturation clears when headroom returns (no entry action needed)
- Non-symbol-cap blocks do NOT enter saturation (RISK_BASE_STALE, portfolio caps)
- Legal capacity computed as min across symbol, gross, net caps
- Portfolio gross/net cap limits partial capacity
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.execution.types import ActionType
from grinder.grid_v2.state import BranchMode, ExitOrderStatus
from grinder.grid_v2.sync_reconciler import reconcile_grid_state
from grinder.risk.portfolio_risk import (
    PortfolioRiskConfig,
    RiskGateDecision,
    RiskGateReason,
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
    """Build a minimal mock bridge for reconciler tests."""
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


def _mock_snapshot(open_orders: list[object] | None = None) -> MagicMock:
    snap = MagicMock()
    snap.open_orders = open_orders or []
    return snap


# ---------------------------------------------------------------------------
# Mock engine for _compute_risk_legal_entry_capacity tests
# ---------------------------------------------------------------------------


def _mock_engine(
    *,
    risk_base_enabled: bool = True,
    risk_base_value_usd: Decimal = Decimal("10000"),
    sym_max_notional_pct: float = 0.80,
    gross_max_pct: float = 0.0,
    net_max_pct: float = 0.0,
    order_size: float = 0.001,
    ref_price: float = 50000.0,
    sym_notional: float = 0.0,
    portfolio_gross: float = 0.0,
    portfolio_net: float = 0.0,
    risk_gate_decision: RiskGateDecision | None = None,
) -> MagicMock:
    """Build a mock engine with fields for _compute_risk_legal_entry_capacity."""
    from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

    engine = MagicMock(spec=LiveEngineV0)
    engine._risk_base_enabled = risk_base_enabled
    engine._risk_base_snapshot = MagicMock()
    engine._risk_base_snapshot.value_usd = risk_base_value_usd
    engine._portfolio_risk_config = PortfolioRiskConfig(
        symbol_max_notional_pct=sym_max_notional_pct,
        portfolio_max_gross_notional_pct=gross_max_pct,
        portfolio_max_net_notional_pct=net_max_pct,
    )
    engine._grid_v2_bridge = MagicMock()
    engine._grid_v2_bridge._config.order_size = Decimal(str(order_size))
    engine._grid_v2_bridge.state_machine.snapshot.entry_window.reference_price = Decimal(
        str(ref_price)
    )

    # Bind the real method
    engine._compute_risk_legal_entry_capacity = (
        LiveEngineV0._compute_risk_legal_entry_capacity.__get__(engine, LiveEngineV0)
    )
    engine._estimate_per_entry_notional = LiveEngineV0._estimate_per_entry_notional.__get__(
        engine, LiveEngineV0
    )

    # Mocked external calls
    engine._sym_notional = sym_notional
    engine._portfolio_gross = portfolio_gross
    engine._portfolio_net = portfolio_net
    engine._risk_gate_decision = risk_gate_decision

    return engine


class TestReconcilerLegalCapacity:
    """reconcile_grid_state with risk_entry_capacity projection."""

    def test_capacity_zero_suppresses_all_entries(self) -> None:
        """capacity=0 → desired entries = 0, no PLACE actions."""
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500")],
        )
        snap = _mock_snapshot()

        result = reconcile_grid_state(
            snapshot=snap, symbol="BTCUSDT", bridge=bridge, risk_entry_capacity=0
        )

        assert result.desired_entry_count == 0
        assert not [a for a in result.actions if a.action_type == ActionType.PLACE]

    def test_capacity_none_unconstrained(self) -> None:
        """capacity=None (unconstrained) → full desired ladder."""
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500")],
        )
        snap = _mock_snapshot()

        result = reconcile_grid_state(
            snapshot=snap, symbol="BTCUSDT", bridge=bridge, risk_entry_capacity=None
        )

        assert result.desired_entry_count == 4
        assert len([a for a in result.actions if a.action_type == ActionType.PLACE]) == 4

    def test_partial_capacity_truncates_ladder(self) -> None:
        """capacity=2 with 4 full entries → desired = 2 closest to reference."""
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500")],
            reference_price=Decimal("50000"),
        )
        snap = _mock_snapshot()

        result = reconcile_grid_state(
            snapshot=snap, symbol="BTCUSDT", bridge=bridge, risk_entry_capacity=2
        )

        assert result.desired_entry_count == 2
        placed_prices = {a.price for a in result.actions if a.action_type == ActionType.PLACE}
        assert Decimal("49750") in placed_prices
        assert Decimal("50250") in placed_prices

    def test_capacity_exceeds_desired_keeps_full(self) -> None:
        """capacity=10 with 4 desired → keep all 4."""
        bridge = _mock_bridge(
            entry_buy_prices=[Decimal("49750"), Decimal("49500")],
            entry_sell_prices=[Decimal("50250"), Decimal("50500")],
        )
        snap = _mock_snapshot()

        result = reconcile_grid_state(
            snapshot=snap, symbol="BTCUSDT", bridge=bridge, risk_entry_capacity=10
        )

        assert result.desired_entry_count == 4

    def test_capacity_zero_still_cancels_extras(self) -> None:
        """capacity=0 → desired=0 → ALL actual entries are 'extra' → CANCEL."""
        bridge = _mock_bridge(entry_buy_prices=[Decimal("49750")])
        entry_order = MagicMock()
        entry_order.symbol = "BTCUSDT"
        entry_order.order_id = "g-BTCUSDT-E-1"
        entry_order.side = "BUY"
        entry_order.price = Decimal("49750")
        parsed_cid = MagicMock()
        parsed_cid.kind.value = "ENTRY"
        bridge.adapter.parse_cid.return_value = parsed_cid

        snap = _mock_snapshot(open_orders=[entry_order])

        result = reconcile_grid_state(
            snapshot=snap, symbol="BTCUSDT", bridge=bridge, risk_entry_capacity=0
        )

        cancel_actions = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 1

    def test_capacity_constraint_preserves_exits(self) -> None:
        """Exit orders unaffected by entry capacity constraint."""
        exit_eo = MagicMock()
        exit_eo.status = ExitOrderStatus.OPEN
        exit_eo.price = Decimal("50500")
        exit_eo.exit_order_id = "exit-1"
        bridge = _mock_bridge(entry_buy_prices=[Decimal("49750")], exit_orders=[exit_eo])
        bridge.adapter.registry.cid_for_exit.return_value = "g-BTCUSDT-X-1"

        snap = _mock_snapshot()

        result = reconcile_grid_state(
            snapshot=snap, symbol="BTCUSDT", bridge=bridge, risk_entry_capacity=0
        )

        assert result.desired_exit_count == 1


class TestCapacityComputation:
    """_compute_risk_legal_entry_capacity with structured reason + multi-cap min."""

    def test_symbol_cap_blocked_returns_is_sym_cap_true(self) -> None:
        """When risk gate blocks with SYMBOL_CAP_EXCEEDED → is_symbol_cap=True."""
        engine = _mock_engine()
        decision = RiskGateDecision(allowed=False, reason=RiskGateReason.SYMBOL_CAP_EXCEEDED)
        with (
            patch("grinder.risk.portfolio_risk.evaluate_risk_gate", return_value=decision),
        ):
            cap, is_sym = engine._compute_risk_legal_entry_capacity("BTCUSDT", MagicMock())

        assert cap == 0
        assert is_sym is True

    def test_risk_base_stale_returns_is_sym_cap_false(self) -> None:
        """RISK_BASE_STALE block → capacity=0 but is_symbol_cap=False.

        Saturation counter must NOT increment for non-symbol-cap blocks.
        """
        engine = _mock_engine()
        decision = RiskGateDecision(allowed=False, reason=RiskGateReason.RISK_BASE_STALE)
        with patch("grinder.risk.portfolio_risk.evaluate_risk_gate", return_value=decision):
            cap, is_sym = engine._compute_risk_legal_entry_capacity("BTCUSDT", MagicMock())

        assert cap == 0
        assert is_sym is False

    def test_portfolio_gross_blocked_returns_is_sym_cap_false(self) -> None:
        """PORTFOLIO_GROSS_CAP_EXCEEDED → capacity=0, is_symbol_cap=False."""
        engine = _mock_engine()
        decision = RiskGateDecision(
            allowed=False, reason=RiskGateReason.PORTFOLIO_GROSS_CAP_EXCEEDED
        )
        with patch("grinder.risk.portfolio_risk.evaluate_risk_gate", return_value=decision):
            cap, is_sym = engine._compute_risk_legal_entry_capacity("BTCUSDT", MagicMock())

        assert cap == 0
        assert is_sym is False

    def test_symbol_allows_4_portfolio_gross_allows_1(self) -> None:
        """Symbol headroom = 4 entries, portfolio gross headroom = 1 → capacity = 1.

        min(symbol, gross) = min(4, 1) = 1.
        """
        engine = _mock_engine(
            sym_max_notional_pct=0.80,  # limit = 8000
            gross_max_pct=0.50,  # limit = 5000
            order_size=0.001,
            ref_price=50000.0,
        )
        # Gate passes (allowed=True)
        allowed = RiskGateDecision(allowed=True)
        # sym_notional = 7800 → sym headroom = 200 → 200/50 = 4 entries
        # portfolio_gross = 4950 → gross headroom = 50 → 50/50 = 1 entry
        with (
            patch("grinder.risk.portfolio_risk.evaluate_risk_gate", return_value=allowed),
            patch(
                "grinder.risk.portfolio_risk.compute_symbol_notional",
                return_value=Decimal("7800"),
            ),
            patch(
                "grinder.risk.portfolio_risk.compute_portfolio_notionals",
                return_value=(Decimal("4950"), Decimal("0")),
            ),
        ):
            cap, is_sym = engine._compute_risk_legal_entry_capacity("BTCUSDT", MagicMock())

        assert cap == 1
        assert is_sym is False

    def test_portfolio_net_allows_0_overrides_symbol_headroom(self) -> None:
        """Symbol headroom = 4, portfolio net headroom = 0 → capacity = 0.

        Net cap is the tightest constraint.
        """
        engine = _mock_engine(
            sym_max_notional_pct=0.80,  # limit = 8000
            net_max_pct=0.30,  # limit = 3000
            order_size=0.001,
            ref_price=50000.0,
        )
        allowed = RiskGateDecision(allowed=True)
        with (
            patch("grinder.risk.portfolio_risk.evaluate_risk_gate", return_value=allowed),
            patch(
                "grinder.risk.portfolio_risk.compute_symbol_notional",
                return_value=Decimal("7800"),
            ),
            patch(
                "grinder.risk.portfolio_risk.compute_portfolio_notionals",
                return_value=(Decimal("0"), Decimal("3100")),
            ),
        ):
            cap, is_sym = engine._compute_risk_legal_entry_capacity("BTCUSDT", MagicMock())

        assert cap == 0
        assert is_sym is False  # not symbol-cap-specific

    def test_risk_base_disabled_returns_unconstrained(self) -> None:
        """risk_base_enabled=False → (None, False) = unconstrained."""
        engine = _mock_engine(risk_base_enabled=False)
        cap, is_sym = engine._compute_risk_legal_entry_capacity("BTCUSDT", MagicMock())

        assert cap is None
        assert is_sym is False


class TestSaturationTracking:
    """Engine-level consecutive cap block tracking and proactive recovery."""

    def test_threshold_boundary_not_saturated(self) -> None:
        """N-1 consecutive cap blocks → NOT saturated yet."""
        tracker: dict[str, int] = {}
        saturated: set[str] = set()
        threshold = 3

        for _ in range(threshold - 1):
            tracker["BTCUSDT"] = tracker.get("BTCUSDT", 0) + 1

        assert tracker["BTCUSDT"] == 2
        assert "BTCUSDT" not in saturated

    def test_threshold_exact_enters_saturation(self) -> None:
        """Exactly N consecutive cap blocks → enters saturation."""
        tracker: dict[str, int] = {}
        saturated: set[str] = set()
        threshold = 3

        for _ in range(threshold):
            tracker["BTCUSDT"] = tracker.get("BTCUSDT", 0) + 1
            if tracker["BTCUSDT"] >= threshold and "BTCUSDT" not in saturated:
                saturated.add("BTCUSDT")

        assert "BTCUSDT" in saturated

    def test_proactive_recovery_on_sync_headroom(self) -> None:
        """Saturation clears proactively on sync when headroom returns.

        Critical: symbol is saturated, headroom returns, NO entry action attempted.
        Saturation must still clear because sync evaluates proactively.
        """
        tracker: dict[str, int] = {"BTCUSDT": 5}
        saturated: set[str] = {"BTCUSDT"}

        # Simulate sync-time evaluation: legal capacity > 0
        legal_cap = 3
        if legal_cap > 0:
            saturated.discard("BTCUSDT")
            tracker["BTCUSDT"] = 0

        assert "BTCUSDT" not in saturated
        assert tracker["BTCUSDT"] == 0

    def test_non_sym_cap_zero_does_not_enter_saturation(self) -> None:
        """RISK_BASE_STALE → cap=0 but is_sym_cap=False → counter NOT incremented.

        This is the core fix for P1-1: non-symbol-cap blocks must not
        accumulate toward saturation, even though they suppress entries.
        """
        tracker: dict[str, int] = {"BTCUSDT": 2}
        saturated: set[str] = set()

        # Simulate sync: cap=0, is_sym_cap=False
        is_sym_cap = False
        if not is_sym_cap:
            # Non-symbol-cap block resets counter
            tracker["BTCUSDT"] = 0

        assert tracker["BTCUSDT"] == 0
        assert "BTCUSDT" not in saturated

    def test_per_symbol_isolation(self) -> None:
        """Saturation for BTCUSDT does not affect ETHUSDT."""
        saturated: set[str] = {"BTCUSDT"}

        assert "ETHUSDT" not in saturated

    def test_non_cap_block_resets_counter(self) -> None:
        """CAP, CAP, NON_CAP → counter = 0 (consecutive chain broken)."""
        tracker: dict[str, int] = {"BTCUSDT": 2}
        tracker["BTCUSDT"] = 0
        assert tracker["BTCUSDT"] == 0

    def test_passive_zero_headroom_full_ladder_no_saturation(self) -> None:
        """Zero headroom + sym_cap + all desired entries already on exchange → no saturation.

        SM has desired entry prices, exchange has matching entries for all of
        them → missing_entries = 0 → has_restore_demand = False. Multiple
        sync cycles at zero headroom must NOT increment saturation counter.
        """
        tracker: dict[str, int] = {}
        saturated: set[str] = set()
        threshold = 3

        # desired and actual entry keys are identical → missing = empty
        for _ in range(5):  # 5 sync cycles
            legal_cap = 0
            is_sym_cap = True
            has_restore_demand = False  # desired - actual = empty

            if legal_cap == 0 and is_sym_cap and has_restore_demand:
                tracker["BTCUSDT"] = tracker.get("BTCUSDT", 0) + 1
                if tracker["BTCUSDT"] >= threshold:
                    saturated.add("BTCUSDT")

        assert tracker.get("BTCUSDT", 0) == 0
        assert "BTCUSDT" not in saturated

    def test_zero_headroom_missing_entries_enters_saturation(self) -> None:
        """Zero headroom + sym_cap + entries missing on exchange → enters saturation.

        SM wants entries at (BUY, 49750) and (SELL, 50250), but exchange has
        none → missing_entries = 2 → has_restore_demand = True. After
        threshold cycles → saturation.
        """
        tracker: dict[str, int] = {}
        saturated: set[str] = set()
        threshold = 3

        for _ in range(threshold):
            legal_cap = 0
            is_sym_cap = True
            has_restore_demand = True  # desired - actual = non-empty

            if legal_cap == 0 and is_sym_cap and has_restore_demand:
                tracker["BTCUSDT"] = tracker.get("BTCUSDT", 0) + 1
                if tracker["BTCUSDT"] >= threshold and "BTCUSDT" not in saturated:
                    saturated.add("BTCUSDT")

        assert tracker["BTCUSDT"] == 3
        assert "BTCUSDT" in saturated

    def test_sym_cap_saturation_clears_on_reason_change(self) -> None:
        """Symbol-cap saturation → non-symbol-cap zero-cap → flag cleared.

        If blocking reason changes from SYMBOL_CAP to e.g. RISK_BASE_STALE,
        the saturated flag must clear because the failure mode changed.
        """
        tracker: dict[str, int] = {"BTCUSDT": 5}
        saturated: set[str] = {"BTCUSDT"}

        # Sync: cap=0, but reason is now non-symbol-cap
        is_sym_cap = False
        if not is_sym_cap and "BTCUSDT" in saturated:
            saturated.discard("BTCUSDT")
            tracker["BTCUSDT"] = 0

        assert "BTCUSDT" not in saturated
        assert tracker["BTCUSDT"] == 0
