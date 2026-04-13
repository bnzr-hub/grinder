"""Tests for _submit_to_exchange / _apply_submit_outcome split."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.engine import LiveActionStatus, SubmitOutcome
from grinder.risk.drawdown_guard_v1 import OrderIntent as RiskIntent


def _make_engine() -> tuple[LiveEngineV0, Any]:
    """Return (engine, mock_port) for testing."""
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
    return LiveEngineV0(paper, port, config), port


def _place(cid: str = "g-test") -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="DRIFTUSDT",
        side=OrderSide.BUY,
        price=Decimal("0.0413"),
        quantity=Decimal("124"),
        client_order_id=cid,
        reason="grid_v2_PLACE_ENTRY",
    )


class TestSubmitToExchange:
    """_submit_to_exchange is pure — no engine state mutations."""

    def test_success_returns_outcome(self) -> None:
        engine, _port = _make_engine()
        _port.place_order.return_value = "order-123"
        outcome = engine._submit_to_exchange(_place(), 1000)
        assert outcome.success is True
        assert outcome.order_id == "order-123"
        assert outcome.attempts == 1

    def test_non_retryable_error(self) -> None:
        engine, _port = _make_engine()
        err = ConnectorNonRetryableError("Binance error -2027: position limit")
        err.exchange_code = -2027
        err.pre_send = False
        _port.place_order.side_effect = err
        outcome = engine._submit_to_exchange(_place(), 1000)
        assert outcome.success is False
        assert outcome.exchange_code == -2027
        assert outcome.error is not None

    def test_transient_error_retries(self) -> None:
        engine, _port = _make_engine()
        _port.place_order.side_effect = [
            ConnectorTransientError("timeout"),
            "order-456",
        ]
        outcome = engine._submit_to_exchange(_place(), 1000)
        assert outcome.success is True
        assert outcome.attempts == 2

    def test_all_retries_exhausted(self) -> None:
        engine, _port = _make_engine()
        _port.place_order.side_effect = ConnectorTransientError("timeout")
        outcome = engine._submit_to_exchange(_place(), 1000)
        assert outcome.success is False
        assert outcome.retries_exhausted is True
        assert outcome.attempts == 3  # default max_attempts

    def test_no_engine_state_mutation(self) -> None:
        """Submit must not touch pending maps, counters, or health state."""
        engine, _port = _make_engine()
        _port.place_order.return_value = "order-123"

        # Snapshot mutable state before
        recent_before = len(engine._recent_places)
        budget_before = engine._order_budget_exhausted

        engine._submit_to_exchange(_place(), 1000)

        # State must be unchanged after submit
        assert len(engine._recent_places) == recent_before
        assert engine._order_budget_exhausted == budget_before


class TestApplySubmitOutcome:
    """_apply_submit_outcome applies state mutations serially."""

    def test_success_tracks_recent_place(self) -> None:
        engine, _port = _make_engine()
        action = _place("g-s0")
        outcome = SubmitOutcome(order_id="order-1", success=True, attempts=1)

        before = len(engine._recent_places)
        engine._apply_submit_outcome(action, outcome, RiskIntent.INCREASE_RISK)
        assert len(engine._recent_places) == before + 1

    def test_success_returns_executed(self) -> None:
        engine, _port = _make_engine()
        action = _place("g-s0")
        outcome = SubmitOutcome(order_id="order-1", success=True, attempts=1)
        result = engine._apply_submit_outcome(action, outcome, RiskIntent.INCREASE_RISK)
        assert result.status == LiveActionStatus.EXECUTED

    def test_failure_returns_failed(self) -> None:
        engine, _port = _make_engine()
        action = _place("g-s0")
        outcome = SubmitOutcome(error="Binance error -2027", attempts=1)
        result = engine._apply_submit_outcome(action, outcome, RiskIntent.INCREASE_RISK)
        assert result.status == LiveActionStatus.FAILED

    def test_budget_latch_on_order_limit(self) -> None:
        engine, _port = _make_engine()
        action = _place("g-s0")
        outcome = SubmitOutcome(error="Order count limit reached", attempts=1)
        assert engine._order_budget_exhausted is False
        engine._apply_submit_outcome(action, outcome, RiskIntent.INCREASE_RISK)
        assert engine._order_budget_exhausted is True

    def test_retries_exhausted_returns_failed(self) -> None:
        engine, _port = _make_engine()
        action = _place("g-s0")
        outcome = SubmitOutcome(error="timeout", attempts=3, retries_exhausted=True)
        result = engine._apply_submit_outcome(action, outcome, RiskIntent.INCREASE_RISK)
        assert result.status == LiveActionStatus.FAILED

    def test_circuit_open_returns_failed_pre_send(self) -> None:
        engine, _port = _make_engine()
        action = _place("g-s0")
        outcome = SubmitOutcome(error="circuit open", attempts=1, circuit_open=True)
        result = engine._apply_submit_outcome(action, outcome, RiskIntent.INCREASE_RISK)
        assert result.status == LiveActionStatus.FAILED
        assert result.pre_send is True


class TestExecuteActionWrapper:
    """_execute_action now delegates to submit + apply."""

    def test_noop_skipped(self) -> None:
        engine, _port = _make_engine()
        action = ExecutionAction(action_type=ActionType.NOOP, symbol="DRIFTUSDT")
        result = engine._execute_action(action, 1000, RiskIntent.REDUCE_RISK)
        assert result.status == LiveActionStatus.SKIPPED

    def test_place_through_split(self) -> None:
        engine, _port = _make_engine()
        _port.place_order.return_value = "order-789"
        action = _place("g-split")
        result = engine._execute_action(action, 1000, RiskIntent.INCREASE_RISK)
        assert result.status == LiveActionStatus.EXECUTED
        assert result.order_id == "order-789"

    def test_min_notional_blocked(self) -> None:
        engine, _port = _make_engine()
        engine._min_notional_cache["DRIFTUSDT"] = Decimal("100")
        action = _place("g-blocked")  # 0.0413 * 124 = 5.12 < 100
        result = engine._execute_action(action, 1000, RiskIntent.INCREASE_RISK)
        assert result.status == LiveActionStatus.BLOCKED


class TestOrderSubmitFailedLog:
    """ORDER_SUBMIT_FAILED structured log event on non-retryable errors."""

    def test_non_retryable_logs_all_fields(self, caplog: Any) -> None:
        """Non-retryable error must emit ORDER_SUBMIT_FAILED with symbol,
        side, type, cid, exchange_code, reduce_only, price, qty, error."""
        engine, _port = _make_engine()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="ARIAUSDT",
            side=OrderSide.SELL,
            price=Decimal("0.7287"),
            quantity=Decimal("10"),
            client_order_id="g_g_ARIAUSDT_e27_1775956613_0",
            reduce_only=False,
        )
        outcome = SubmitOutcome(
            error="Binance error -4164: Order's notional must be no smaller than 5.0",
            exchange_code=-4164,
            attempts=1,
        )
        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine._apply_submit_outcome(action, outcome, RiskIntent.INCREASE_RISK)

        order_fail_logs = [r for r in caplog.records if "ORDER_SUBMIT_FAILED" in r.message]
        assert len(order_fail_logs) == 1, (
            f"Expected 1 ORDER_SUBMIT_FAILED log, got {len(order_fail_logs)}"
        )
        msg = order_fail_logs[0].message
        assert "symbol=ARIAUSDT" in msg
        assert "side=SELL" in msg
        assert "exchange_code=-4164" in msg
        assert "price=0.7287" in msg
        assert "qty=10" in msg
        assert "cid=g_g_ARIAUSDT_e27_1775956613_0" in msg
        assert "reduce_only=False" in msg
        assert "-4164" in msg

    def test_reduce_only_2022_logs_before_repair(self, caplog: Any) -> None:
        """For -2022 reduce-only errors, ORDER_SUBMIT_FAILED must fire
        BEFORE the REDUCE_ONLY_REPAIR_TRIGGERED handler."""
        engine, _port = _make_engine()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="ARIAUSDT",
            side=OrderSide.BUY,
            price=Decimal("0.7237"),
            quantity=Decimal("10"),
            client_order_id="g_g_ARIAUSDT_x9_1775953943_0",
            reduce_only=True,
        )
        outcome = SubmitOutcome(
            error="Binance error -2022: ReduceOnly Order is rejected.",
            exchange_code=-2022,
            attempts=1,
        )
        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine._apply_submit_outcome(action, outcome, RiskIntent.REDUCE_RISK)

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        submit_idx = next(
            (i for i, r in enumerate(warnings) if "ORDER_SUBMIT_FAILED" in r.message), None
        )
        repair_idx = next(
            (i for i, r in enumerate(warnings) if "REDUCE_ONLY_REPAIR_TRIGGERED" in r.message),
            None,
        )
        assert submit_idx is not None, "ORDER_SUBMIT_FAILED not emitted"
        assert repair_idx is not None, "REDUCE_ONLY_REPAIR_TRIGGERED not emitted"
        assert submit_idx < repair_idx, (
            "ORDER_SUBMIT_FAILED must fire before REDUCE_ONLY_REPAIR_TRIGGERED"
        )

    def test_no_log_on_success(self, caplog: Any) -> None:
        """Successful submit must NOT emit ORDER_SUBMIT_FAILED."""
        engine, _port = _make_engine()
        action = _place("g-ok")
        outcome = SubmitOutcome(order_id="order-1", success=True, attempts=1)
        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine._apply_submit_outcome(action, outcome, RiskIntent.INCREASE_RISK)
        assert not any("ORDER_SUBMIT_FAILED" in r.message for r in caplog.records)
