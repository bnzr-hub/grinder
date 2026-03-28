"""Live Acceptance Matrix: cross-feature integration tests (PR-8, ADR-107).

Proves the integrated live system behaves coherently when multiple
previously-fixed subsystems interact under adversarial conditions.

Scenarios:
1. Health gate + preflight interaction
2. Risk saturation + effective desired projection (no churn)
3. Reduce-only budget + exit topology repair
4. -2022 reject → repair latch → topology repair convergence
5. Partial fill → topology recompute → budget legality
6. Actual matches effective but not theoretical → zero churn
7. Symbol isolation under mixed degraded modes
8. Observability contract under integrated failure
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from collections.abc import Sequence

from unittest.mock import patch

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap, PositionSnap
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.grid_v2.sync_reconciler import ProjectionMode, reconcile_grid_state
from grinder.live import BlockReason, LiveEngineConfig, LiveEngineV0
from grinder.live.health_gate import LiveHealthMode
from grinder.live.preflight import (
    CheckStatus,
    check_config_consistency,
    run_preflight,
)
from grinder.live.reason_codes import (
    NoActionReason,
    classify_no_action_reason,
)
from grinder.live.reduce_only_budget import (
    compute_budget_snapshot,
    detect_surplus_exits,
)

# --- Helpers ---


def _mock_bridge(
    *,
    buy_prices: list[Decimal] | None = None,
    sell_prices: list[Decimal] | None = None,
    ref: Decimal = Decimal("50000"),
    max_inv: int = 10,
    order_size: Decimal = Decimal("0.001"),
) -> MagicMock:
    bridge = MagicMock()
    sm = MagicMock()
    sm.mode.value = "FLAT"
    sm.snapshot.entry_window.buy_entry_prices = buy_prices or []
    sm.snapshot.entry_window.sell_entry_prices = sell_prices or []
    sm.snapshot.entry_window.reference_price = ref
    sm.snapshot.open_lots = []
    sm.snapshot.exit_orders = []
    bridge.state_machine = sm
    bridge._config.max_inventory_levels = max_inv
    bridge._config.grid_step_pct = Decimal("0.005")
    bridge._config.order_size = order_size
    bridge._config.price_tick_size = Decimal("0.10")
    bridge._quantize_price = lambda p, _s: p
    bridge.adapter.registry.cid_for_entry.return_value = None
    bridge.adapter.registry.cid_for_exit.return_value = None
    bridge.adapter.registry.all_entry_cids = set()
    bridge.adapter.parse_cid.return_value = None
    return bridge


def _snap(open_orders: Sequence[object] | None = None) -> MagicMock:
    s = MagicMock()
    s.open_orders = open_orders or []
    return s


# --- Integration Tests ---


class TestHealthGatePlusPreflightInteraction:
    """Preflight blocks unsafe startup; health gate blocks degraded writes."""

    def test_preflight_blocks_unsafe_config(self) -> None:
        """armed+mainnet with wrong mode → preflight FAIL."""
        result = check_config_consistency(armed=True, mainnet=True, mode_value="read_only")
        assert result.status == CheckStatus.FAIL

    def test_armed_preflight_passes_then_health_degrades(self) -> None:
        """Armed preflight passes (mocked probes), then health gate blocks writes."""
        # Armed preflight with mocked passing checks
        with (
            patch("grinder.live.preflight.check_dns_resolution") as dns,
            patch("grinder.live.preflight.check_exchange_time_sync") as ts,
            patch("grinder.live.preflight.check_ws_bootstrap") as ws,
            patch("grinder.live.preflight.check_account_sync_read") as sync,
            patch("grinder.live.preflight.check_symbol_metadata") as sym,
        ):
            from grinder.live.preflight import PreflightCheck  # noqa: PLC0415

            for m in (dns, ts, ws, sync, sym):
                m.return_value = PreflightCheck(name="mock", status=CheckStatus.PASS)
            pf = run_preflight(
                armed=True,
                mainnet=True,
                mode_value="live_trade",
                env_acks={"ALLOW_MAINNET_TRADE": True, "GRINDER_REAL_PORT_ACK": True},
            )
        assert pf.passed

        # Runtime: engine health degrades → blocks writes
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        engine = LiveEngineV0(
            paper, MagicMock(), LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        )
        engine._health_mode = LiveHealthMode.STALE_TRUTH
        with patch.object(engine, "_is_write_allowed_by_health", return_value=False):
            action = ExecutionAction(
                action_type=ActionType.PLACE,
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("49000"),
                quantity=Decimal("1"),
                reason="test",
            )
            result = engine._process_action(action, 1000)
        assert result.block_reason == BlockReason.HEALTH_GATE_UNSAFE


class TestRiskSaturationPlusProjection:
    """Risk saturation + effective desired projection → no churn."""

    def test_saturated_projection_zero_no_churn(self) -> None:
        """Risk-saturated: theoretical=4, effective=0, actual=0 → zero actions."""
        bridge = _mock_bridge(
            buy_prices=[Decimal("49750"), Decimal("49500")],
            sell_prices=[Decimal("50250"), Decimal("50500")],
        )
        result = reconcile_grid_state(
            snapshot=_snap(),
            symbol="BTCUSDT",
            bridge=bridge,
            risk_entry_capacity=0,
        )
        assert result.theoretical_desired_entry_count == 4
        assert result.desired_entry_count == 0
        assert result.projection_mode == ProjectionMode.RISK_CONSTRAINED_ZERO
        assert len(result.actions) == 0

        # Observability: reason code classifies correctly
        reason = classify_no_action_reason(
            recon_has_diff=False,
            theoretical_entries=4,
            effective_entries=0,
            actual_entries=0,
            is_risk_saturated=True,
            is_awaiting_sync=False,
            is_started=True,
            reconstruction_ok=True,
        )
        assert reason == NoActionReason.RISK_SATURATED_TARGET_ZERO


class TestReduceOnlyBudgetPlusExitRepair:
    """Budget guard + topology repair compose correctly."""

    def test_over_budget_detected_and_repair_planned(self) -> None:
        """Position=5, exits=8 → surplus detected, repair plans cancels."""
        snap = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("5"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(
                OpenOrderSnap(
                    order_id="o-1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50250"),
                    qty=Decimal("3"),
                    filled_qty=Decimal("0"),
                    reduce_only=True,
                    status="NEW",
                    ts=1000,
                ),
                OpenOrderSnap(
                    order_id="o-2",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50500"),
                    qty=Decimal("3"),
                    filled_qty=Decimal("0"),
                    reduce_only=True,
                    status="NEW",
                    ts=1000,
                ),
                OpenOrderSnap(
                    order_id="o-3",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50750"),
                    qty=Decimal("2"),
                    filled_qty=Decimal("0"),
                    reduce_only=True,
                    status="NEW",
                    ts=1000,
                ),
            ),
            ts=1000,
            source="test",
        )
        # Budget check: over budget
        budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        assert budget.is_over_budget
        assert budget.over_budget_qty == Decimal("3")

        # Surplus detection
        surplus = detect_surplus_exits(snap, "BTCUSDT", OrderSide.SELL)
        assert len(surplus) >= 1

        # After cancelling surplus, remaining should be legal
        cancelled_ids = {r.order_id for r in surplus}
        remaining = sum(
            o.qty - o.filled_qty for o in snap.open_orders if o.order_id not in cancelled_ids
        )
        assert remaining <= Decimal("5")


class TestRejectRepairLatchDirectionIsolation:
    """-2022 reject → repair latch → direction-scoped blocking.

    Latch clear is simulated (sync repair path not driven here).
    """

    def test_reject_latch_blocks_and_isolates_direction(self) -> None:
        """Real engine: -2022 reject → SELL blocked → BUY allowed → manual clear."""
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        engine = LiveEngineV0(
            paper,
            MagicMock(),
            LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Step 1: -2022 reject on SELL side
        engine._on_reduce_only_reject("BTCUSDT", "SELL", -2022)
        assert ("BTCUSDT", "SELL") in engine._reduce_only_pending_repair

        # Step 2: SELL reduce-only exits blocked
        sell_action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("50250"),
            quantity=Decimal("1"),
            reduce_only=True,
            reason="test",
        )
        result = engine._process_action(sell_action, 1000)
        assert result.block_reason == BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED

        # Step 3: BUY reduce-only exits NOT blocked (direction isolation)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("10"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )
        buy_action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49750"),
            quantity=Decimal("1"),
            reduce_only=True,
            reason="test",
        )
        buy_result = engine._process_action(buy_action, 1000)
        # BUY not blocked by pending repair (only SELL is latched)
        assert buy_result.block_reason != BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED or (
            ("BTCUSDT", "BUY") in engine._reduce_only_pending_repair
        )

        # Step 4: Manual latch clear (simulating sync repair)
        engine._reduce_only_pending_repair.discard(("BTCUSDT", "SELL"))
        assert ("BTCUSDT", "SELL") not in engine._reduce_only_pending_repair


class TestPartialFillTopologyRecompute:
    """Partial fill changes budget → topology recompute stays legal."""

    def test_partial_fill_shrinks_budget_consumer(self) -> None:
        snap = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("5"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(
                OpenOrderSnap(
                    order_id="o-1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50250"),
                    qty=Decimal("3"),
                    filled_qty=Decimal("2"),  # partial fill
                    reduce_only=True,
                    status="PARTIALLY_FILLED",
                    ts=1000,
                ),
                OpenOrderSnap(
                    order_id="o-2",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50500"),
                    qty=Decimal("3"),
                    filled_qty=Decimal("0"),
                    reduce_only=True,
                    status="NEW",
                    ts=1000,
                ),
            ),
            ts=1000,
            source="test",
        )
        budget = compute_budget_snapshot(snap, "BTCUSDT", OrderSide.SELL)
        # remaining: o-1=1, o-2=3, total=4 <= position=5
        assert budget.open_reduce_only_remaining_qty == Decimal("4")
        assert not budget.is_over_budget


class TestActualMatchesEffectiveNotTheoretical:
    """actual == effective != theoretical → zero churn + correct reason."""

    def test_no_churn_partial_projection(self) -> None:
        bridge = _mock_bridge(
            buy_prices=[Decimal("49750"), Decimal("49500")],
            sell_prices=[Decimal("50250"), Decimal("50500")],
        )
        # Exchange has exactly the 2 effective entries
        parsed = MagicMock()
        parsed.kind.value = "ENTRY"
        bridge.adapter.parse_cid.return_value = parsed
        orders = []
        for cid, side, price in [
            ("g-E-1", "BUY", Decimal("49750")),
            ("g-E-2", "SELL", Decimal("50250")),
        ]:
            o = MagicMock()
            o.symbol = "BTCUSDT"
            o.order_id = cid
            o.side = side
            o.price = price
            orders.append(o)

        result = reconcile_grid_state(
            snapshot=_snap(orders),
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

        reason = classify_no_action_reason(
            recon_has_diff=False,
            theoretical_entries=4,
            effective_entries=2,
            actual_entries=2,
            is_risk_saturated=False,
            is_awaiting_sync=False,
            is_started=True,
            reconstruction_ok=True,
        )
        assert reason == NoActionReason.EFFECTIVE_TARGET_PARTIAL_MATCHED


class TestSymbolIsolationMixedModes:
    """One symbol under repair, another clean — real engine state."""

    def test_real_engine_symbol_isolation(self) -> None:
        """BTCUSDT SELL latched, ETHUSDT SELL not blocked."""
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        engine = LiveEngineV0(
            paper,
            MagicMock(),
            LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Latch BTCUSDT SELL
        engine._on_reduce_only_reject("BTCUSDT", "SELL", -2022)
        assert ("BTCUSDT", "SELL") in engine._reduce_only_pending_repair

        # BTCUSDT SELL → blocked
        btc_sell = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("50250"),
            quantity=Decimal("1"),
            reduce_only=True,
            reason="test",
        )
        r1 = engine._process_action(btc_sell, 1000)
        assert r1.block_reason == BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED

        # ETHUSDT SELL → NOT blocked (different symbol)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="ETHUSDT",
                    side="LONG",
                    qty=Decimal("10"),
                    entry_price=Decimal("3000"),
                    mark_price=Decimal("3000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )
        eth_sell = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            price=Decimal("3050"),
            quantity=Decimal("1"),
            reduce_only=True,
            reason="test",
        )
        r2 = engine._process_action(eth_sell, 1000)
        assert r2.block_reason != BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED

        # BTCUSDT BUY → NOT blocked (different side)
        assert ("BTCUSDT", "BUY") not in engine._reduce_only_pending_repair


class TestObservabilityUnderIntegratedFailure:
    """Observability contract holds when multiple subsystems interact."""

    def test_reason_codes_distinguish_all_no_op_types(self) -> None:
        """Each no-op scenario has a unique reason code."""
        reasons = set()

        # Healthy
        reasons.add(
            classify_no_action_reason(
                recon_has_diff=False,
                theoretical_entries=4,
                effective_entries=4,
                actual_entries=4,
                is_risk_saturated=False,
                is_awaiting_sync=False,
                is_started=True,
                reconstruction_ok=True,
            )
        )
        # Saturated
        reasons.add(
            classify_no_action_reason(
                recon_has_diff=False,
                theoretical_entries=4,
                effective_entries=0,
                actual_entries=0,
                is_risk_saturated=True,
                is_awaiting_sync=False,
                is_started=True,
                reconstruction_ok=True,
            )
        )
        # Partial matched
        reasons.add(
            classify_no_action_reason(
                recon_has_diff=False,
                theoretical_entries=4,
                effective_entries=2,
                actual_entries=2,
                is_risk_saturated=False,
                is_awaiting_sync=False,
                is_started=True,
                reconstruction_ok=True,
            )
        )
        # Awaiting sync
        reasons.add(
            classify_no_action_reason(
                recon_has_diff=False,
                theoretical_entries=0,
                effective_entries=0,
                actual_entries=0,
                is_risk_saturated=False,
                is_awaiting_sync=True,
                is_started=True,
                reconstruction_ok=True,
            )
        )

        # All four are distinct
        assert len(reasons) == 4

    def test_projection_mode_and_entry_suppression_consistent(self) -> None:
        """Projection mode and entry suppression reason are consistent."""
        bridge = _mock_bridge(
            buy_prices=[Decimal("49750")],
            sell_prices=[Decimal("50250")],
        )

        # Zero capacity → RISK_CONSTRAINED_ZERO
        r0 = reconcile_grid_state(
            snapshot=_snap(),
            symbol="X",
            bridge=bridge,
            risk_entry_capacity=0,
        )
        assert r0.projection_mode == ProjectionMode.RISK_CONSTRAINED_ZERO
        assert r0.theoretical_desired_entry_count == 2
        assert r0.desired_entry_count == 0

        # Partial capacity → RISK_CONSTRAINED_PARTIAL
        r1 = reconcile_grid_state(
            snapshot=_snap(),
            symbol="X",
            bridge=bridge,
            risk_entry_capacity=1,
        )
        assert r1.projection_mode == ProjectionMode.RISK_CONSTRAINED_PARTIAL
        assert r1.desired_entry_count == 1

        # Unconstrained → UNCONSTRAINED
        r2 = reconcile_grid_state(
            snapshot=_snap(),
            symbol="X",
            bridge=bridge,
            risk_entry_capacity=None,
        )
        assert r2.projection_mode == ProjectionMode.UNCONSTRAINED
        assert r2.desired_entry_count == 2


class TestLaunchReadinessCommand:
    """Integration tests for scripts.launch_readiness GO/NO-GO contract.

    Uses fake syncer/port objects passed through run_readiness_check →
    run_preflight, exercising the real wiring contract.
    """

    _VALID_ENV: ClassVar[dict[str, str]] = {
        "GRINDER_GRID_V2_ENABLED": "1",
        "GRINDER_GRID_V2_SYMBOL": "BTCUSDT",
        "GRINDER_GRID_V2_TICK_SIZE": "0.10",
        "GRINDER_GRID_V2_SYNC_RECONCILER_ENABLED": "1",
        "GRINDER_GRID_V2_SYNC_RECONCILER_PRIMARY": "1",
        "GRINDER_GRID_V2_NETOFF_ENABLED": "0",
        "GRINDER_MAX_POSITION_USD": "5000",
        "GRINDER_SYMBOL_RISK_MAX_NOTIONAL_PCT": "0.80",
        "GRINDER_GRID_V2_RESEED_ON_FLAT": "1",
        "GRINDER_GRID_V2_RESEED_ON_FLAT_ONLY_ON_SKEW": "0",
        "GRINDER_ARMED": "1",
        "GRINDER_MAINNET": "1",
        "GRINDER_MODE": "live_trade",
        "ALLOW_MAINNET_TRADE": "1",
        "GRINDER_REAL_PORT_ACK": "1",
    }

    def test_missing_config_returns_no_go(self) -> None:
        """Missing required env vars → NO-GO."""
        from scripts.launch_readiness import run_readiness_check  # noqa: PLC0415

        with patch.dict(os.environ, {}, clear=True):
            result = run_readiness_check("BTCUSDT")
        assert result is False

    def test_full_preflight_with_fake_syncer_port_go(self) -> None:
        """Full preflight with fake syncer + fake port → GO.

        Uses real run_preflight() wiring through run_readiness_check.
        Fake syncer returns success, fake port has symbol constraints.
        Network probes patched (no real DNS/WS in CI).
        """
        from scripts.launch_readiness import run_readiness_check  # noqa: PLC0415

        # Fake syncer: .sync() returns result with error=None
        fake_syncer = MagicMock()
        fake_syncer.sync.return_value = MagicMock(error=None)

        # Fake port: has get_symbol_constraints returning BTCUSDT
        fake_port = MagicMock()
        fake_port.get_symbol_constraints.return_value = {"BTCUSDT": {}}

        with (
            patch.dict(os.environ, self._VALID_ENV, clear=True),
            patch("grinder.live.preflight.check_dns_resolution") as dns,
            patch("grinder.live.preflight.check_exchange_time_sync") as ts,
            patch("grinder.live.preflight.check_ws_bootstrap") as ws,
        ):
            from grinder.live.preflight import CheckStatus, PreflightCheck  # noqa: PLC0415

            ok = PreflightCheck(name="mock", status=CheckStatus.PASS, detail="ok")
            dns.return_value = ok
            ts.return_value = ok
            ws.return_value = ok
            result = run_readiness_check("BTCUSDT", syncer=fake_syncer, port=fake_port)
        assert result is True

    def test_syncer_error_returns_no_go(self) -> None:
        """Syncer returns error → account_sync_read FAIL → NO-GO."""
        from scripts.launch_readiness import run_readiness_check  # noqa: PLC0415

        fake_syncer = MagicMock()
        fake_syncer.sync.return_value = MagicMock(error="connection refused")
        fake_port = MagicMock()
        fake_port.get_symbol_constraints.return_value = {"BTCUSDT": {}}

        with (
            patch.dict(os.environ, self._VALID_ENV, clear=True),
            patch("grinder.live.preflight.check_dns_resolution") as dns,
            patch("grinder.live.preflight.check_exchange_time_sync") as ts,
            patch("grinder.live.preflight.check_ws_bootstrap") as ws,
        ):
            from grinder.live.preflight import CheckStatus, PreflightCheck  # noqa: PLC0415

            ok = PreflightCheck(name="mock", status=CheckStatus.PASS, detail="ok")
            dns.return_value = ok
            ts.return_value = ok
            ws.return_value = ok
            result = run_readiness_check("BTCUSDT", syncer=fake_syncer, port=fake_port)
        assert result is False

    def test_missing_symbol_metadata_returns_no_go(self) -> None:
        """Port missing symbol constraints → symbol_metadata FAIL → NO-GO."""
        from scripts.launch_readiness import run_readiness_check  # noqa: PLC0415

        fake_syncer = MagicMock()
        fake_syncer.sync.return_value = MagicMock(error=None)
        fake_port = MagicMock(spec=[])  # no get_symbol_constraints

        with (
            patch.dict(os.environ, self._VALID_ENV, clear=True),
            patch("grinder.live.preflight.check_dns_resolution") as dns,
            patch("grinder.live.preflight.check_exchange_time_sync") as ts,
            patch("grinder.live.preflight.check_ws_bootstrap") as ws,
        ):
            from grinder.live.preflight import CheckStatus, PreflightCheck  # noqa: PLC0415

            ok = PreflightCheck(name="mock", status=CheckStatus.PASS, detail="ok")
            dns.return_value = ok
            ts.return_value = ok
            ws.return_value = ok
            result = run_readiness_check("BTCUSDT", syncer=fake_syncer, port=fake_port)
        assert result is False

    def test_non_armed_skips_preflight(self) -> None:
        """Non-armed mode → preflight skipped → config checks drive verdict."""
        from scripts.launch_readiness import run_readiness_check  # noqa: PLC0415

        env = dict(self._VALID_ENV)
        env["GRINDER_ARMED"] = "0"
        with patch.dict(os.environ, env, clear=True):
            result = run_readiness_check("BTCUSDT")
        # Preflight skipped (PASS), config checks pass → GO
        assert result is True
