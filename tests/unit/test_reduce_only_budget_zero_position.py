"""Tests for reduce-only budget guard: batch exhaustion and -2022 latch.

Proves:
1. Multiple exits in same tick exhaust budget from batch additions
2. -2022 latch blocks further exits for the affected direction
3. Latch is direction-isolated (opposite side not blocked)
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from grinder.account.contracts import AccountSnapshot, PositionSnap
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.engine import BlockReason, LiveActionStatus


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    return LiveEngineV0(paper, port, LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE))


def _long_snapshot(symbol: str = "BTCUSDT", qty: str = "200") -> AccountSnapshot:
    return AccountSnapshot(
        positions=(
            PositionSnap(
                symbol=symbol,
                side="LONG",
                qty=Decimal(qty),
                entry_price=Decimal("50000"),
                mark_price=Decimal("50050"),
                unrealized_pnl=Decimal("0.1"),
                leverage=1,
                ts=1000,
            ),
        ),
        open_orders=(),
        ts=1000,
        source="test",
    )


def _sell_exit(cid: str = "g-x0", qty: str = "100") -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        price=Decimal("50100"),
        quantity=Decimal(qty),
        client_order_id=cid,
        reduce_only=True,
        reason="grid_v2_PLACE_EXIT",
    )


class TestBatchBudgetExhaustion:
    """Multiple exits in same tick exhaust budget from position closeable."""

    def test_two_exits_within_budget_pass(self) -> None:
        engine = _make_engine()
        engine._last_account_snapshot = _long_snapshot(qty="200")

        r1 = engine._process_action(_sell_exit("g-x0", "100"), ts=1000)
        r2 = engine._process_action(_sell_exit("g-x1", "100"), ts=1000)

        assert r1.status != LiveActionStatus.BLOCKED
        assert r2.status != LiveActionStatus.BLOCKED

    def test_third_exit_exceeds_budget_blocked(self) -> None:
        engine = _make_engine()
        engine._last_account_snapshot = _long_snapshot(qty="200")

        engine._process_action(_sell_exit("g-x0", "100"), ts=1000)
        engine._process_action(_sell_exit("g-x1", "100"), ts=1000)
        r3 = engine._process_action(_sell_exit("g-x2", "100"), ts=1000)

        assert r3.status == LiveActionStatus.BLOCKED
        assert r3.block_reason == BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED


class TestRejectLatchBlocking:
    """After -2022 reject, further exits for same direction are blocked."""

    def test_latch_blocks_same_direction(self) -> None:
        engine = _make_engine()
        engine._last_account_snapshot = _long_snapshot(qty="200")

        # Simulate -2022 latch for SELL direction
        engine._on_reduce_only_reject("BTCUSDT", "SELL", -2022)

        result = engine._process_action(_sell_exit("g-x0", "100"), ts=1000)
        assert result.status == LiveActionStatus.BLOCKED
        assert result.block_reason == BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED

    def test_latch_does_not_block_opposite_direction(self) -> None:
        engine = _make_engine()
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="SHORT",
                    qty=Decimal("200"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("49950"),
                    unrealized_pnl=Decimal("0.1"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )

        # Latch SELL direction
        engine._on_reduce_only_reject("BTCUSDT", "SELL", -2022)

        # BUY exit (close SHORT) should NOT be blocked by SELL latch
        buy_exit = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49900"),
            quantity=Decimal("100"),
            client_order_id="g-x0",
            reduce_only=True,
            reason="grid_v2_PLACE_EXIT",
        )
        result = engine._process_action(buy_exit, ts=1000)
        assert result.status != LiveActionStatus.BLOCKED

    def test_latch_cleared_after_repair(self) -> None:
        engine = _make_engine()
        engine._last_account_snapshot = _long_snapshot(qty="200")

        # Set and then clear latch
        engine._on_reduce_only_reject("BTCUSDT", "SELL", -2022)
        engine._reduce_only_pending_repair.discard(("BTCUSDT", "SELL"))

        result = engine._process_action(_sell_exit("g-x0", "100"), ts=1000)
        assert result.status != LiveActionStatus.BLOCKED
