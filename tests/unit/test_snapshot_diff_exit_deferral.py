"""Tests for -2022 latch firing in the correct exception handler.

Root cause of the rapid-fill -2022 race: the latch code was in the
ConnectorTransientError handler, but -2022 is a ConnectorNonRetryableError.
The latch never fired, so all exits in a batch hit exchange.

After fix: latch fires on first -2022 (non-retryable), blocking remaining
exits in the same batch via Gate 0.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from grinder.connectors.errors import ConnectorNonRetryableError
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


def _sell_exit(cid: str = "g-x0") -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        price=Decimal("50100"),
        quantity=Decimal("100"),
        client_order_id=cid,
        reduce_only=True,
        reason="grid_v2_PLACE_EXIT",
    )


class TestLatchFiresOnNonRetryable2022:
    """The -2022 latch must fire in the NonRetryable handler."""

    def test_2022_sets_latch(self) -> None:
        """First -2022 from exchange sets the pending_repair latch."""
        engine = _make_engine()
        engine._last_account_snapshot = None  # fail-open: no snapshot

        # Mock port to raise -2022 NonRetryableError
        engine._exchange_port = MagicMock()
        engine._exchange_port.place_order.side_effect = ConnectorNonRetryableError(
            "Binance error -2022: ReduceOnly Order is rejected.",
            exchange_code=-2022,
        )

        result = engine._process_action(_sell_exit("g-x0"), ts=1000)

        assert result.status == LiveActionStatus.FAILED
        assert ("BTCUSDT", "SELL") in engine._reduce_only_pending_repair

    def test_second_exit_blocked_after_latch(self) -> None:
        """After first -2022 sets latch, second exit is blocked by Gate 0."""
        engine = _make_engine()
        engine._last_account_snapshot = None

        engine._exchange_port = MagicMock()
        engine._exchange_port.place_order.side_effect = ConnectorNonRetryableError(
            "Binance error -2022: ReduceOnly Order is rejected.",
            exchange_code=-2022,
        )

        # First exit: hits exchange, gets -2022, sets latch
        r1 = engine._process_action(_sell_exit("g-x0"), ts=1000)
        assert r1.status == LiveActionStatus.FAILED

        # Second exit: Gate 0 blocks it (latch is set)
        r2 = engine._process_action(_sell_exit("g-x1"), ts=1000)
        assert r2.status == LiveActionStatus.BLOCKED
        assert r2.block_reason == BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED

        # Port should only be called once (first exit only)
        assert engine._exchange_port.place_order.call_count == 1

    def test_rapid_batch_only_one_hits_exchange(self) -> None:
        """In a batch of 4 exits, only the first reaches exchange. Rest are latched."""
        engine = _make_engine()
        engine._last_account_snapshot = None

        engine._exchange_port = MagicMock()
        engine._exchange_port.place_order.side_effect = ConnectorNonRetryableError(
            "Binance error -2022: ReduceOnly Order is rejected.",
            exchange_code=-2022,
        )

        results = []
        for i in range(4):
            r = engine._process_action(_sell_exit(f"g-x{i}"), ts=1000)
            results.append(r)

        assert results[0].status == LiveActionStatus.FAILED
        for r in results[1:]:
            assert r.status == LiveActionStatus.BLOCKED
        # Exchange only called once
        assert engine._exchange_port.place_order.call_count == 1

    def test_opposite_direction_not_latched(self) -> None:
        """SELL latch does not block BUY exits."""
        engine = _make_engine()
        engine._last_account_snapshot = None

        # Directly set the latch for SELL direction (simulating first -2022)
        engine._reduce_only_pending_repair.add(("BTCUSDT", "SELL"))

        # BUY exit: should NOT be blocked by SELL latch
        buy_exit = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49900"),
            quantity=Decimal("100"),
            client_order_id="g-x1",
            reduce_only=True,
            reason="grid_v2_PLACE_EXIT",
        )
        r = engine._process_action(buy_exit, ts=1000)
        assert r.block_reason != BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED
