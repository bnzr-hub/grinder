"""Tests for portfolio risk evaluation and engine enforcement (PR-2, ADR-092).

Mandatory test cases:
1. symbol cap blocks increase-risk place.
2. portfolio gross cap blocks increase-risk place.
3. portfolio net cap blocks increase-risk place.
4. reduce-only place allowed under all breaches.
5. cancel allowed under all breaches.
6. stale soft blocks increase-risk.
7. stale hard blocks increase-risk.
8. unavailable blocks increase-risk.
9. feature flags off => behavior unchanged (regression).
10. multi-symbol portfolio breach blocks all increase-risk.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grinder.account.contracts import AccountSnapshot, PositionSnap
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.port import NoOpExchangePort
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import (
    BlockReason,
    LiveActionStatus,
    LiveEngineConfig,
    LiveEngineV0,
)
from grinder.risk.portfolio_risk import (
    PortfolioRiskConfig,
    RiskGateReason,
    compute_portfolio_notionals,
    compute_symbol_notional,
    evaluate_risk_gate,
)
from grinder.risk.risk_base import RiskBaseSnapshot
from grinder.risk.risk_base_metrics import reset_risk_base_metrics

# --- Helpers ---


def _snap(
    value: str = "1000", stale_soft: bool = False, stale_hard: bool = False, below_min: bool = False
) -> RiskBaseSnapshot:
    return RiskBaseSnapshot(
        value_usd=Decimal(value),
        mode="total_margin_balance",
        asof_ts_ms=100_000,
        age_s=0,
        is_stale_soft=stale_soft,
        is_stale_hard=stale_hard,
        source_fields_present=("total_margin_balance",),
        is_below_min=below_min,
    )


def _account(
    positions: list[tuple[str, ...]] | None = None,
) -> AccountSnapshot:
    """Build AccountSnapshot from (symbol, side, qty, mark_price[, signed_qty]) tuples."""
    pos_list = []
    if positions:
        for entry in positions:
            sym, side, qty, mark = entry[0], entry[1], entry[2], entry[3]
            signed_qty = Decimal(entry[4]) if len(entry) > 4 else None
            pos_list.append(
                PositionSnap(
                    symbol=sym,
                    side=side,
                    qty=Decimal(qty),
                    entry_price=Decimal(mark),
                    mark_price=Decimal(mark),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                    signed_qty=signed_qty,
                )
            )
    return AccountSnapshot(
        positions=tuple(sorted(pos_list, key=lambda p: p.sort_key())),
        open_orders=(),
        ts=1000,
        source="test",
    )


def _cfg(sym_pct: float = 0.0, gross_pct: float = 0.0, net_pct: float = 0.0) -> PortfolioRiskConfig:
    return PortfolioRiskConfig(
        symbol_max_notional_pct=sym_pct,
        portfolio_max_gross_notional_pct=gross_pct,
        portfolio_max_net_notional_pct=net_pct,
    )


# --- Evaluator unit tests ---


class TestComputeSymbolNotional:
    def test_single_position(self) -> None:
        acct = _account([("BTCUSDT", "LONG", "0.1", "50000")])
        assert compute_symbol_notional(acct, "BTCUSDT") == Decimal("5000")

    def test_no_positions(self) -> None:
        acct = _account([])
        assert compute_symbol_notional(acct, "BTCUSDT") == Decimal("0")

    def test_other_symbol_excluded(self) -> None:
        acct = _account([("ETHUSDT", "LONG", "1", "3000")])
        assert compute_symbol_notional(acct, "BTCUSDT") == Decimal("0")


class TestComputePortfolioNotionals:
    def test_long_only(self) -> None:
        acct = _account([("BTCUSDT", "LONG", "0.1", "50000")])
        gross, net = compute_portfolio_notionals(acct)
        assert gross == Decimal("5000")
        assert net == Decimal("5000")

    def test_long_and_short(self) -> None:
        acct = _account(
            [
                ("BTCUSDT", "LONG", "0.1", "50000"),  # +5000
                ("ETHUSDT", "SHORT", "1", "3000"),  # -3000
            ]
        )
        gross, net = compute_portfolio_notionals(acct)
        assert gross == Decimal("8000")
        assert net == Decimal("2000")  # abs(5000 - 3000)

    def test_balanced_net_zero(self) -> None:
        acct = _account(
            [
                ("BTCUSDT", "LONG", "0.1", "50000"),  # +5000
                ("ETHUSDT", "SHORT", "5", "1000"),  # -5000
            ]
        )
        gross, net = compute_portfolio_notionals(acct)
        assert gross == Decimal("10000")
        assert net == Decimal("0")

    def test_both_short_via_signed_qty(self) -> None:
        """P0 fix: BOTH + signed_qty<0 → treated as short."""
        acct = _account([("BTCUSDT", "BOTH", "0.1", "50000", "-0.1")])
        gross, net = compute_portfolio_notionals(acct)
        assert gross == Decimal("5000")
        assert net == Decimal("5000")  # abs(-5000) = 5000 (short direction)

    def test_both_long_via_signed_qty(self) -> None:
        """P0 fix: BOTH + signed_qty>0 → treated as long."""
        acct = _account([("BTCUSDT", "BOTH", "0.1", "50000", "0.1")])
        gross, net = compute_portfolio_notionals(acct)
        assert gross == Decimal("5000")
        assert net == Decimal("5000")  # abs(+5000) = 5000 (long direction)

    def test_both_no_signed_qty_defaults_long(self) -> None:
        """P0 fix: BOTH + signed_qty=None → fail-closed as long."""
        acct = _account([("BTCUSDT", "BOTH", "0.1", "50000")])
        gross, net = compute_portfolio_notionals(acct)
        assert gross == Decimal("5000")
        assert net == Decimal("5000")

    def test_both_short_with_long_other_symbol(self) -> None:
        """P0 fix: mixed BOTH short + LONG → net cancels correctly."""
        acct = _account(
            [
                ("BTCUSDT", "BOTH", "0.1", "50000", "-0.1"),  # -5000
                ("ETHUSDT", "LONG", "1", "3000"),  # +3000
            ]
        )
        gross, net = compute_portfolio_notionals(acct)
        assert gross == Decimal("8000")
        assert net == Decimal("2000")  # abs(-5000 + 3000)


class TestEvaluateRiskGateSnapshotUnavailable:
    """P1 fix: snapshot=None + caps enabled → fail-closed."""

    def test_snapshot_none_with_caps_blocks(self) -> None:
        d = evaluate_risk_gate(_snap("1000"), None, _cfg(sym_pct=0.10), "BTCUSDT")
        assert not d.allowed
        assert d.reason == RiskGateReason.RISK_BASE_UNAVAILABLE

    def test_snapshot_none_no_caps_allows(self) -> None:
        d = evaluate_risk_gate(_snap("1000"), None, _cfg(), "BTCUSDT")
        assert d.allowed


class TestEvaluateRiskGateUnavailable:
    def test_unavailable_blocks(self) -> None:
        d = evaluate_risk_gate(None, None, _cfg(), "BTCUSDT")
        assert not d.allowed
        assert d.reason == RiskGateReason.RISK_BASE_UNAVAILABLE

    def test_stale_soft_blocks(self) -> None:
        d = evaluate_risk_gate(_snap(stale_soft=True), None, _cfg(), "BTCUSDT")
        assert not d.allowed
        assert d.reason == RiskGateReason.RISK_BASE_STALE

    def test_stale_hard_blocks(self) -> None:
        d = evaluate_risk_gate(_snap(stale_hard=True, stale_soft=True), None, _cfg(), "BTCUSDT")
        assert not d.allowed
        assert d.reason == RiskGateReason.RISK_BASE_STALE

    def test_below_min_blocks(self) -> None:
        d = evaluate_risk_gate(_snap(below_min=True), None, _cfg(), "BTCUSDT")
        assert not d.allowed
        assert d.reason == RiskGateReason.RISK_BASE_BELOW_MIN


class TestEvaluateRiskGateSymbolCap:
    def test_symbol_cap_blocks(self) -> None:
        # Base 1000, sym_pct 0.10 → limit 100. Position = 200 → blocked.
        acct = _account([("BTCUSDT", "LONG", "0.004", "50000")])  # 200
        d = evaluate_risk_gate(_snap("1000"), acct, _cfg(sym_pct=0.10), "BTCUSDT")
        assert not d.allowed
        assert d.reason == RiskGateReason.SYMBOL_CAP_EXCEEDED

    def test_symbol_cap_allows_under_limit(self) -> None:
        acct = _account([("BTCUSDT", "LONG", "0.001", "50000")])  # 50
        d = evaluate_risk_gate(_snap("1000"), acct, _cfg(sym_pct=0.10), "BTCUSDT")
        assert d.allowed


class TestEvaluateRiskGatePortfolioCaps:
    def test_gross_cap_blocks(self) -> None:
        # Base 1000, gross_pct 0.80 → limit 800. Positions gross = 900 → blocked.
        acct = _account(
            [
                ("BTCUSDT", "LONG", "0.01", "50000"),  # 500
                ("ETHUSDT", "SHORT", "0.2", "2000"),  # 400
            ]
        )
        d = evaluate_risk_gate(_snap("1000"), acct, _cfg(gross_pct=0.80), "BTCUSDT")
        assert not d.allowed
        assert d.reason == RiskGateReason.PORTFOLIO_GROSS_CAP_EXCEEDED

    def test_net_cap_blocks(self) -> None:
        # Base 1000, net_pct 0.40 → limit 400. LONG 500 only → net=500 → blocked.
        acct = _account([("BTCUSDT", "LONG", "0.01", "50000")])
        d = evaluate_risk_gate(_snap("1000"), acct, _cfg(net_pct=0.40), "BTCUSDT")
        assert not d.allowed
        assert d.reason == RiskGateReason.PORTFOLIO_NET_CAP_EXCEEDED

    def test_all_caps_disabled_allows(self) -> None:
        acct = _account([("BTCUSDT", "LONG", "1", "50000")])  # huge position
        d = evaluate_risk_gate(_snap("1000"), acct, _cfg(), "BTCUSDT")
        assert d.allowed


class TestEvaluateRiskGateDrawdown:
    def test_symbol_dd_freeze_blocks(self) -> None:
        acct = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("1"),
                    signed_qty=Decimal("1"),
                    entry_price=Decimal("100"),
                    mark_price=Decimal("95"),
                    unrealized_pnl=Decimal("-30"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )
        cfg = PortfolioRiskConfig(
            symbol_max_notional_pct=0.2,  # budget=200
            symbol_freeze_dd_pct=10.0,  # dd = 15%
        )
        d = evaluate_risk_gate(_snap("1000"), acct, cfg, "BTCUSDT")
        assert not d.allowed
        assert d.reason == RiskGateReason.SYMBOL_DD_FREEZE

    def test_portfolio_dd_force_reduce_blocks(self) -> None:
        acct = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("1"),
                    signed_qty=Decimal("1"),
                    entry_price=Decimal("100"),
                    mark_price=Decimal("95"),
                    unrealized_pnl=Decimal("-90"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )
        cfg = PortfolioRiskConfig(portfolio_dd_force_reduce_pct=8.0)
        d = evaluate_risk_gate(_snap("1000"), acct, cfg, "BTCUSDT")
        assert not d.allowed
        assert d.reason == RiskGateReason.PORTFOLIO_DD_FORCE_REDUCE


class TestEvaluateRiskGateMultiSymbol:
    def test_portfolio_breach_blocks_all_symbols(self) -> None:
        """Gross breach on portfolio blocks INCREASE_RISK for ANY symbol."""
        acct = _account(
            [
                ("BTCUSDT", "LONG", "0.01", "50000"),  # 500
                ("ETHUSDT", "LONG", "0.2", "2000"),  # 400
            ]
        )
        cfg = _cfg(gross_pct=0.80)  # limit 800, gross=900

        d_btc = evaluate_risk_gate(_snap("1000"), acct, cfg, "BTCUSDT")
        d_eth = evaluate_risk_gate(_snap("1000"), acct, cfg, "ETHUSDT")
        d_new = evaluate_risk_gate(_snap("1000"), acct, cfg, "SOLUSDT")

        assert not d_btc.allowed
        assert not d_eth.allowed
        assert not d_new.allowed  # even a new symbol blocked


# --- Engine-level enforcement tests ---


class TestRiskBaseEnforcementEngine:
    """Engine-level integration tests for risk base enforcement gate."""

    @staticmethod
    def _make_engine(
        port: Any = None,
        risk_base_enabled: bool = True,
        sym_pct: str = "0",
        gross_pct: str = "0",
        net_pct: str = "0",
    ) -> LiveEngineV0:
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        if port is None:
            port = NoOpExchangePort()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        env_vars = {
            "GRINDER_RISK_BASE_ENABLED": "1" if risk_base_enabled else "0",
            "GRINDER_RISK_BASE_MODE": "total_margin_balance",
            "GRINDER_RISK_BASE_MIN_USD": "50",
            "GRINDER_RISK_BASE_STALE_TTL_S": "30",
            "GRINDER_RISK_BASE_MAX_AGE_HARD_S": "60",
            "GRINDER_SYMBOL_RISK_MAX_NOTIONAL_PCT": sym_pct,
            "GRINDER_PORTFOLIO_RISK_MAX_GROSS_NOTIONAL_PCT": gross_pct,
            "GRINDER_PORTFOLIO_RISK_MAX_NET_NOTIONAL_PCT": net_pct,
        }
        with patch.dict("os.environ", env_vars):
            return LiveEngineV0(paper, port, config)

    @staticmethod
    def _place_action(symbol: str = "BTCUSDT", reduce_only: bool = False) -> ExecutionAction:
        return ExecutionAction(
            action_type=ActionType.PLACE,
            symbol=symbol,
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.01"),
            level_id=1,
            reason="GRID_ENTRY",
            reduce_only=reduce_only,
        )

    @staticmethod
    def _cancel_action(symbol: str = "BTCUSDT") -> ExecutionAction:
        return ExecutionAction(
            action_type=ActionType.CANCEL,
            order_id="ORDER_1",
            symbol=symbol,
            reason="GRID_EXIT",
        )

    def setup_method(self) -> None:
        reset_risk_base_metrics()

    def teardown_method(self) -> None:
        reset_risk_base_metrics()

    def test_unavailable_blocks_increase_risk(self) -> None:
        """Case 8: unavailable risk base blocks INCREASE_RISK."""
        engine = self._make_engine()
        engine._risk_base_snapshot = None  # unavailable

        result = engine._process_action(self._place_action(), ts=1000)
        assert result.status == LiveActionStatus.BLOCKED
        assert result.block_reason == BlockReason.RISK_BASE_UNAVAILABLE

    def test_stale_soft_blocks_increase_risk(self) -> None:
        """Case 6: soft stale blocks INCREASE_RISK."""
        engine = self._make_engine()
        engine._risk_base_snapshot = _snap(stale_soft=True)

        result = engine._process_action(self._place_action(), ts=1000)
        assert result.status == LiveActionStatus.BLOCKED
        assert result.block_reason == BlockReason.RISK_BASE_STALE

    def test_stale_hard_blocks_increase_risk(self) -> None:
        """Case 7: hard stale blocks INCREASE_RISK."""
        engine = self._make_engine()
        engine._risk_base_snapshot = _snap(stale_hard=True, stale_soft=True)

        result = engine._process_action(self._place_action(), ts=1000)
        assert result.status == LiveActionStatus.BLOCKED
        assert result.block_reason == BlockReason.RISK_BASE_STALE

    def test_symbol_cap_blocks_increase_risk(self) -> None:
        """Case 1: symbol cap blocks increase-risk place."""
        engine = self._make_engine(sym_pct="0.10")
        engine._risk_base_snapshot = _snap("1000")
        engine._last_account_snapshot = _account(
            [
                ("BTCUSDT", "LONG", "0.004", "50000"),  # 200 >= 100 limit
            ]
        )

        result = engine._process_action(self._place_action(), ts=1000)
        assert result.status == LiveActionStatus.BLOCKED
        assert result.block_reason == BlockReason.RISK_SYMBOL_CAP

    def test_portfolio_gross_cap_blocks(self) -> None:
        """Case 2: portfolio gross cap blocks increase-risk place."""
        engine = self._make_engine(gross_pct="0.50")
        engine._risk_base_snapshot = _snap("1000")
        engine._last_account_snapshot = _account(
            [
                ("BTCUSDT", "LONG", "0.01", "50000"),  # 500
                ("ETHUSDT", "SHORT", "0.1", "2000"),  # 200 → gross=700 >= 500
            ]
        )

        result = engine._process_action(self._place_action(), ts=1000)
        assert result.status == LiveActionStatus.BLOCKED
        assert result.block_reason == BlockReason.RISK_PORTFOLIO_GROSS_CAP

    def test_portfolio_net_cap_blocks(self) -> None:
        """Case 3: portfolio net cap blocks increase-risk place."""
        engine = self._make_engine(net_pct="0.40")
        engine._risk_base_snapshot = _snap("1000")
        engine._last_account_snapshot = _account(
            [
                ("BTCUSDT", "LONG", "0.01", "50000"),  # net=500 >= 400
            ]
        )

        result = engine._process_action(self._place_action(), ts=1000)
        assert result.status == LiveActionStatus.BLOCKED
        assert result.block_reason == BlockReason.RISK_PORTFOLIO_NET_CAP

    def test_reduce_only_allowed_under_all_breaches(self) -> None:
        """Case 4: reduce-only place always passes, even when capped."""
        engine = self._make_engine(sym_pct="0.10", gross_pct="0.50")
        engine._risk_base_snapshot = _snap("1000")
        engine._last_account_snapshot = _account(
            [
                ("BTCUSDT", "LONG", "0.01", "50000"),  # huge notional
            ]
        )

        result = engine._process_action(self._place_action(reduce_only=True), ts=1000)
        # reduce_only classifies as REDUCE_RISK → skips risk base gate
        assert result.status != LiveActionStatus.BLOCKED or result.block_reason not in (
            BlockReason.RISK_SYMBOL_CAP,
            BlockReason.RISK_PORTFOLIO_GROSS_CAP,
            BlockReason.RISK_BASE_UNAVAILABLE,
        )

    def test_cancel_allowed_under_all_breaches(self) -> None:
        """Case 5: cancel always passes, even when risk base unavailable."""
        engine = self._make_engine()
        engine._risk_base_snapshot = None  # unavailable

        result = engine._process_action(self._cancel_action(), ts=1000)
        # CANCEL intent → skips risk base gate entirely
        assert result.block_reason != BlockReason.RISK_BASE_UNAVAILABLE

    def test_feature_off_no_enforcement(self) -> None:
        """Case 9: risk_base_enabled=False → no enforcement."""
        engine = self._make_engine(risk_base_enabled=False, sym_pct="0.10")
        engine._risk_base_snapshot = None  # would block if enabled

        result = engine._process_action(self._place_action(), ts=1000)
        # Should NOT be blocked by risk base gate
        assert result.block_reason not in (
            BlockReason.RISK_BASE_UNAVAILABLE,
            BlockReason.RISK_BASE_STALE,
            BlockReason.RISK_SYMBOL_CAP,
        )

    def test_multi_symbol_portfolio_breach_blocks_all(self) -> None:
        """Case 10: portfolio breach blocks INCREASE_RISK for all symbols."""
        engine = self._make_engine(gross_pct="0.50")
        engine._risk_base_snapshot = _snap("1000")
        engine._last_account_snapshot = _account(
            [
                ("BTCUSDT", "LONG", "0.01", "50000"),  # 500
                ("ETHUSDT", "LONG", "0.1", "2000"),  # 200 → gross=700 >= 500
            ]
        )

        r_btc = engine._process_action(self._place_action("BTCUSDT"), ts=1000)
        r_eth = engine._process_action(self._place_action("ETHUSDT"), ts=1000)
        r_sol = engine._process_action(self._place_action("SOLUSDT"), ts=1000)

        assert r_btc.block_reason == BlockReason.RISK_PORTFOLIO_GROSS_CAP
        assert r_eth.block_reason == BlockReason.RISK_PORTFOLIO_GROSS_CAP
        assert r_sol.block_reason == BlockReason.RISK_PORTFOLIO_GROSS_CAP

    def test_snapshot_none_caps_enabled_blocks(self) -> None:
        """P1 fix: no account snapshot + caps enabled → fail-closed."""
        engine = self._make_engine(sym_pct="0.10")
        engine._risk_base_snapshot = _snap("1000")
        engine._last_account_snapshot = None  # no snapshot yet

        result = engine._process_action(self._place_action(), ts=1000)
        assert result.status == LiveActionStatus.BLOCKED
        assert result.block_reason == BlockReason.RISK_BASE_UNAVAILABLE


class TestPortfolioRiskConfigValidation:
    def test_negative_sym_pct_raises(self) -> None:
        with pytest.raises(ValueError, match="symbol_max_notional_pct"):
            PortfolioRiskConfig(symbol_max_notional_pct=-0.1)

    def test_negative_gross_pct_raises(self) -> None:
        with pytest.raises(ValueError, match="portfolio_max_gross_notional_pct"):
            PortfolioRiskConfig(portfolio_max_gross_notional_pct=-0.1)

    def test_negative_net_pct_raises(self) -> None:
        with pytest.raises(ValueError, match="portfolio_max_net_notional_pct"):
            PortfolioRiskConfig(portfolio_max_net_notional_pct=-0.1)

    def test_defaults_all_disabled(self) -> None:
        cfg = PortfolioRiskConfig()
        assert cfg.symbol_max_notional_pct == 0.0
        assert cfg.portfolio_max_gross_notional_pct == 0.0
        assert cfg.portfolio_max_net_notional_pct == 0.0

    def test_pct_exactly_1_allowed(self) -> None:
        cfg = PortfolioRiskConfig(symbol_max_notional_pct=1.0)
        assert cfg.symbol_max_notional_pct == 1.0

    def test_pct_above_1_allowed_for_leverage(self) -> None:
        cfg = PortfolioRiskConfig(portfolio_max_gross_notional_pct=3.0)
        assert cfg.portfolio_max_gross_notional_pct == 3.0

    def test_pct_over_10_raises(self) -> None:
        with pytest.raises(ValueError, match="fraction"):
            PortfolioRiskConfig(portfolio_max_net_notional_pct=10.1)

    def test_dd_threshold_order_validation(self) -> None:
        with pytest.raises(ValueError, match="thresholds invalid"):
            PortfolioRiskConfig(
                symbol_freeze_dd_pct=5.0,
                symbol_unload_dd_pct=2.5,
            )
