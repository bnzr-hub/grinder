"""Tests for min-notional pre-send gate in LiveEngineV0."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.port import NoOpExchangePort
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import BlockReason, LiveActionStatus, LiveEngineV0


def _make_engine(min_notional_cache: dict[str, Decimal] | None = None) -> LiveEngineV0:
    """Build engine with injectable min_notional_cache."""
    mock_paper = MagicMock()
    mock_paper.process_snapshot.return_value = MagicMock(actions=[])
    config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
    engine = LiveEngineV0(
        mock_paper,
        NoOpExchangePort(),
        config,
    )
    if min_notional_cache:
        engine._min_notional_cache = min_notional_cache
    return engine


def _place_action(
    symbol: str = "DRIFTUSDT",
    price: str = "0.04",
    qty: str = "123",
) -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol=symbol,
        side=OrderSide.BUY,
        price=Decimal(price),
        quantity=Decimal(qty),
        reason="test",
    )


class TestMinNotionalGate:
    def test_blocks_sub_min_notional_order(self) -> None:
        """price * qty < min_notional → BLOCKED."""
        engine = _make_engine({"DRIFTUSDT": Decimal("5")})
        action = _place_action(price="0.04", qty="123")  # 4.92 < 5
        result = engine._execute_action(action, 1000, MagicMock())
        assert result.status == LiveActionStatus.BLOCKED
        assert result.block_reason == BlockReason.NOTIONAL_TOO_LOW

    def test_allows_legal_notional_order(self) -> None:
        """price * qty >= min_notional → not blocked by this gate."""
        engine = _make_engine({"DRIFTUSDT": Decimal("5")})
        action = _place_action(price="0.05", qty="123")  # 6.15 >= 5
        result = engine._execute_action(action, 1000, MagicMock())
        # Should proceed past min_notional gate (may still be EXECUTED via NoOp)
        assert result.status != LiveActionStatus.BLOCKED or (
            result.block_reason != BlockReason.NOTIONAL_TOO_LOW
        )

    def test_no_cache_entry_no_blocking(self) -> None:
        """Unknown symbol → no min_notional in cache → no blocking."""
        engine = _make_engine({})
        action = _place_action(symbol="UNKNOWNUSDT", price="0.01", qty="1")
        result = engine._execute_action(action, 1000, MagicMock())
        assert (
            result.block_reason != BlockReason.NOTIONAL_TOO_LOW
            if result.status == LiveActionStatus.BLOCKED
            else True
        )

    def test_cancel_not_affected(self) -> None:
        """CANCEL actions are never blocked by min_notional gate."""
        engine = _make_engine({"DRIFTUSDT": Decimal("5")})
        action = ExecutionAction(
            action_type=ActionType.CANCEL,
            symbol="DRIFTUSDT",
            order_id="order_1",
            reason="test",
        )
        result = engine._execute_action(action, 1000, MagicMock())
        assert (
            result.block_reason != BlockReason.NOTIONAL_TOO_LOW
            if hasattr(result, "block_reason") and result.block_reason
            else True
        )

    def test_reduce_only_exit_not_blocked(self) -> None:
        """reduce_only=True exits must NEVER be blocked by min_notional gate."""
        engine = _make_engine({"DRIFTUSDT": Decimal("5")})
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="DRIFTUSDT",
            side=OrderSide.SELL,
            price=Decimal("0.04"),
            quantity=Decimal("123"),  # 4.92 < 5 — would be blocked for entry
            reduce_only=True,
            reason="test_exit",
        )
        result = engine._execute_action(action, 1000, MagicMock())
        # Must NOT be blocked by NOTIONAL_TOO_LOW — exits stay viable
        assert (
            result.block_reason != BlockReason.NOTIONAL_TOO_LOW
            if result.status == LiveActionStatus.BLOCKED
            else True
        )

    def test_deterministic(self) -> None:
        engine = _make_engine({"DRIFTUSDT": Decimal("5")})
        action = _place_action(price="0.04", qty="123")
        r1 = engine._execute_action(action, 1000, MagicMock())
        r2 = engine._execute_action(action, 1000, MagicMock())
        assert r1.status == r2.status
        assert r1.block_reason == r2.block_reason
