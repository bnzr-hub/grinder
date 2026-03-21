"""Tests for futures user-data stream event types."""

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from grinder.core import OrderSide, OrderState
from grinder.execution.futures_events import (
    BINANCE_STATUS_MAP,
    FuturesOrderEvent,
    FuturesPositionEvent,
    UserDataEvent,
    UserDataEventType,
)


class TestBinanceStatusMapping:
    """Tests for Binance status -> OrderState mapping."""

    def test_new_maps_to_open(self) -> None:
        assert BINANCE_STATUS_MAP["NEW"] == OrderState.OPEN

    def test_partially_filled_maps_correctly(self) -> None:
        assert BINANCE_STATUS_MAP["PARTIALLY_FILLED"] == OrderState.PARTIALLY_FILLED

    def test_filled_maps_correctly(self) -> None:
        assert BINANCE_STATUS_MAP["FILLED"] == OrderState.FILLED

    def test_canceled_maps_to_cancelled(self) -> None:
        assert BINANCE_STATUS_MAP["CANCELED"] == OrderState.CANCELLED

    def test_rejected_maps_correctly(self) -> None:
        assert BINANCE_STATUS_MAP["REJECTED"] == OrderState.REJECTED

    def test_expired_maps_correctly(self) -> None:
        assert BINANCE_STATUS_MAP["EXPIRED"] == OrderState.EXPIRED

    def test_expired_in_match_maps_to_expired(self) -> None:
        assert BINANCE_STATUS_MAP["EXPIRED_IN_MATCH"] == OrderState.EXPIRED


class TestFuturesOrderEvent:
    """Tests for FuturesOrderEvent dataclass."""

    @pytest.fixture
    def sample_order_event(self) -> FuturesOrderEvent:
        return FuturesOrderEvent(
            ts=1000000,
            symbol="BTCUSDT",
            order_id=123456,
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            side=OrderSide.BUY,
            status=OrderState.OPEN,
            price=Decimal("50000.00"),
            qty=Decimal("0.001"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
        )

    def test_to_dict_serializes_correctly(self, sample_order_event: FuturesOrderEvent) -> None:
        d = sample_order_event.to_dict()
        assert d["ts"] == 1000000
        assert d["symbol"] == "BTCUSDT"
        assert d["order_id"] == 123456
        assert d["client_order_id"] == "grinder_BTCUSDT_1_1000000_1"
        assert d["side"] == "BUY"
        assert d["status"] == "OPEN"
        assert d["price"] == "50000.00"
        assert d["qty"] == "0.001"
        assert d["executed_qty"] == "0"
        assert d["avg_price"] == "0"

    def test_from_dict_deserializes_correctly(self, sample_order_event: FuturesOrderEvent) -> None:
        d = sample_order_event.to_dict()
        restored = FuturesOrderEvent.from_dict(d)
        assert restored == sample_order_event

    def test_roundtrip_preserves_data(self, sample_order_event: FuturesOrderEvent) -> None:
        """to_dict -> from_dict should preserve all fields."""
        d = sample_order_event.to_dict()
        restored = FuturesOrderEvent.from_dict(d)
        assert restored.ts == sample_order_event.ts
        assert restored.symbol == sample_order_event.symbol
        assert restored.order_id == sample_order_event.order_id
        assert restored.client_order_id == sample_order_event.client_order_id
        assert restored.side == sample_order_event.side
        assert restored.status == sample_order_event.status
        assert restored.price == sample_order_event.price
        assert restored.qty == sample_order_event.qty
        assert restored.executed_qty == sample_order_event.executed_qty
        assert restored.avg_price == sample_order_event.avg_price

    def test_to_json_is_deterministic(self, sample_order_event: FuturesOrderEvent) -> None:
        json1 = sample_order_event.to_json()
        json2 = sample_order_event.to_json()
        assert json1 == json2

    def test_from_binance_parses_new_order(self) -> None:
        binance_msg = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1568879465651,
            "T": 1568879465650,
            "o": {
                "s": "BTCUSDT",
                "c": "grinder_BTCUSDT_1_1000000_1",
                "S": "BUY",
                "o": "LIMIT",
                "f": "GTC",
                "q": "0.001",
                "p": "50000",
                "ap": "0",
                "X": "NEW",
                "i": 8886774,
                "z": "0",
            },
        }
        event = FuturesOrderEvent.from_binance(binance_msg)

        assert event.ts == 1568879465651
        assert event.symbol == "BTCUSDT"
        assert event.order_id == 8886774
        assert event.client_order_id == "grinder_BTCUSDT_1_1000000_1"
        assert event.side == OrderSide.BUY
        assert event.status == OrderState.OPEN
        assert event.price == Decimal("50000")
        assert event.qty == Decimal("0.001")
        assert event.executed_qty == Decimal("0")
        assert event.avg_price == Decimal("0")

    def test_from_binance_parses_partially_filled(self) -> None:
        binance_msg = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1568879466000,
            "o": {
                "s": "BTCUSDT",
                "c": "grinder_BTCUSDT_1_1000000_1",
                "S": "BUY",
                "X": "PARTIALLY_FILLED",
                "i": 8886774,
                "p": "50000",
                "q": "0.001",
                "z": "0.0005",
                "ap": "50000",
            },
        }
        event = FuturesOrderEvent.from_binance(binance_msg)

        assert event.status == OrderState.PARTIALLY_FILLED
        assert event.executed_qty == Decimal("0.0005")
        assert event.avg_price == Decimal("50000")

    def test_from_binance_parses_filled(self) -> None:
        binance_msg = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1568879467000,
            "o": {
                "s": "BTCUSDT",
                "c": "grinder_BTCUSDT_1_1000000_1",
                "S": "BUY",
                "X": "FILLED",
                "i": 8886774,
                "p": "50000",
                "q": "0.001",
                "z": "0.001",
                "ap": "50000.5",
            },
        }
        event = FuturesOrderEvent.from_binance(binance_msg)

        assert event.status == OrderState.FILLED
        assert event.executed_qty == Decimal("0.001")
        assert event.avg_price == Decimal("50000.5")

    def test_from_binance_parses_canceled(self) -> None:
        binance_msg = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1568879468000,
            "o": {
                "s": "BTCUSDT",
                "c": "grinder_BTCUSDT_1_1000000_1",
                "S": "SELL",
                "X": "CANCELED",
                "i": 8886775,
                "p": "51000",
                "q": "0.002",
                "z": "0",
                "ap": "0",
            },
        }
        event = FuturesOrderEvent.from_binance(binance_msg)

        assert event.side == OrderSide.SELL
        assert event.status == OrderState.CANCELLED
        assert event.executed_qty == Decimal("0")

    def test_from_binance_handles_missing_fields(self) -> None:
        """Should not crash on minimal/incomplete messages."""
        minimal_msg = {
            "e": "ORDER_TRADE_UPDATE",
            "o": {},
        }
        event = FuturesOrderEvent.from_binance(minimal_msg)

        assert event.ts == 0
        assert event.symbol == ""
        assert event.order_id == 0
        assert event.status == OrderState.OPEN  # Default for unknown status


class TestFuturesPositionEvent:
    """Tests for FuturesPositionEvent dataclass."""

    @pytest.fixture
    def sample_position_event(self) -> FuturesPositionEvent:
        return FuturesPositionEvent(
            ts=1000000,
            symbol="BTCUSDT",
            position_amt=Decimal("0.001"),
            entry_price=Decimal("50000.00"),
            unrealized_pnl=Decimal("5.00"),
        )

    def test_to_dict_serializes_correctly(
        self, sample_position_event: FuturesPositionEvent
    ) -> None:
        d = sample_position_event.to_dict()
        assert d["ts"] == 1000000
        assert d["symbol"] == "BTCUSDT"
        assert d["position_amt"] == "0.001"
        assert d["entry_price"] == "50000.00"
        assert d["unrealized_pnl"] == "5.00"

    def test_from_dict_deserializes_correctly(
        self, sample_position_event: FuturesPositionEvent
    ) -> None:
        d = sample_position_event.to_dict()
        restored = FuturesPositionEvent.from_dict(d)
        assert restored == sample_position_event

    def test_roundtrip_preserves_data(self, sample_position_event: FuturesPositionEvent) -> None:
        d = sample_position_event.to_dict()
        restored = FuturesPositionEvent.from_dict(d)
        assert restored.ts == sample_position_event.ts
        assert restored.symbol == sample_position_event.symbol
        assert restored.position_amt == sample_position_event.position_amt
        assert restored.entry_price == sample_position_event.entry_price
        assert restored.unrealized_pnl == sample_position_event.unrealized_pnl

    def test_to_json_is_deterministic(self, sample_position_event: FuturesPositionEvent) -> None:
        json1 = sample_position_event.to_json()
        json2 = sample_position_event.to_json()
        assert json1 == json2

    def test_from_binance_parses_long_position(self) -> None:
        binance_msg = {
            "e": "ACCOUNT_UPDATE",
            "E": 1564745798939,
            "T": 1564745798938,
            "a": {
                "m": "ORDER",
                "B": [],
                "P": [
                    {
                        "s": "BTCUSDT",
                        "pa": "0.001",
                        "ep": "50000",
                        "up": "5.5",
                        "mt": "cross",
                        "ps": "BOTH",
                    }
                ],
            },
        }
        event = FuturesPositionEvent.from_binance(binance_msg, "BTCUSDT")

        assert event.ts == 1564745798939
        assert event.symbol == "BTCUSDT"
        assert event.position_amt == Decimal("0.001")
        assert event.entry_price == Decimal("50000")
        assert event.unrealized_pnl == Decimal("5.5")

    def test_from_binance_parses_short_position(self) -> None:
        binance_msg = {
            "e": "ACCOUNT_UPDATE",
            "E": 1564745798939,
            "a": {
                "P": [
                    {
                        "s": "BTCUSDT",
                        "pa": "-0.002",
                        "ep": "51000",
                        "up": "-10.0",
                    }
                ],
            },
        }
        event = FuturesPositionEvent.from_binance(binance_msg, "BTCUSDT")

        assert event.position_amt == Decimal("-0.002")
        assert event.unrealized_pnl == Decimal("-10.0")

    def test_from_binance_returns_zero_for_missing_symbol(self) -> None:
        binance_msg = {
            "e": "ACCOUNT_UPDATE",
            "E": 1564745798939,
            "a": {
                "P": [
                    {
                        "s": "ETHUSDT",
                        "pa": "1.0",
                        "ep": "3000",
                        "up": "50.0",
                    }
                ],
            },
        }
        # Request BTCUSDT but only ETHUSDT in message
        event = FuturesPositionEvent.from_binance(binance_msg, "BTCUSDT")

        assert event.symbol == "BTCUSDT"
        assert event.position_amt == Decimal("0")
        assert event.entry_price == Decimal("0")
        assert event.unrealized_pnl == Decimal("0")

    def test_all_from_binance_extracts_all_positions(self) -> None:
        binance_msg = {
            "e": "ACCOUNT_UPDATE",
            "E": 1564745798939,
            "a": {
                "P": [
                    {"s": "BTCUSDT", "pa": "0.001", "ep": "50000", "up": "5.0"},
                    {"s": "ETHUSDT", "pa": "-2.0", "ep": "3000", "up": "-100.0"},
                ],
            },
        }
        events = FuturesPositionEvent.all_from_binance(binance_msg)

        assert len(events) == 2
        assert events[0].symbol == "BTCUSDT"
        assert events[0].position_amt == Decimal("0.001")
        assert events[1].symbol == "ETHUSDT"
        assert events[1].position_amt == Decimal("-2.0")

    def test_from_binance_handles_empty_positions(self) -> None:
        binance_msg = {
            "e": "ACCOUNT_UPDATE",
            "E": 1564745798939,
            "a": {"P": []},
        }
        event = FuturesPositionEvent.from_binance(binance_msg, "BTCUSDT")

        assert event.position_amt == Decimal("0")


class TestUserDataEvent:
    """Tests for UserDataEvent wrapper."""

    @pytest.fixture
    def order_event(self) -> FuturesOrderEvent:
        return FuturesOrderEvent(
            ts=1000000,
            symbol="BTCUSDT",
            order_id=123,
            client_order_id="grinder_1",
            side=OrderSide.BUY,
            status=OrderState.OPEN,
            price=Decimal("50000"),
            qty=Decimal("0.001"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
        )

    @pytest.fixture
    def position_event(self) -> FuturesPositionEvent:
        return FuturesPositionEvent(
            ts=1000000,
            symbol="BTCUSDT",
            position_amt=Decimal("0.001"),
            entry_price=Decimal("50000"),
            unrealized_pnl=Decimal("5"),
        )

    def test_order_event_to_dict(self, order_event: FuturesOrderEvent) -> None:
        wrapper = UserDataEvent(
            event_type=UserDataEventType.ORDER_TRADE_UPDATE,
            order_event=order_event,
        )
        d = wrapper.to_dict()

        assert d["event_type"] == "ORDER_TRADE_UPDATE"
        assert "order_event" in d
        assert d["order_event"]["symbol"] == "BTCUSDT"
        assert "position_event" not in d
        assert "raw_data" not in d

    def test_position_event_to_dict(self, position_event: FuturesPositionEvent) -> None:
        wrapper = UserDataEvent(
            event_type=UserDataEventType.ACCOUNT_UPDATE,
            position_event=position_event,
        )
        d = wrapper.to_dict()

        assert d["event_type"] == "ACCOUNT_UPDATE"
        assert "position_event" in d
        assert d["position_event"]["symbol"] == "BTCUSDT"
        assert "order_event" not in d

    def test_unknown_event_to_dict(self) -> None:
        wrapper = UserDataEvent(
            event_type=UserDataEventType.UNKNOWN,
            raw_data={"e": "MARGIN_CALL", "foo": "bar"},
        )
        d = wrapper.to_dict()

        assert d["event_type"] == "UNKNOWN"
        assert d["raw_data"]["e"] == "MARGIN_CALL"

    def test_from_dict_order_event(self, order_event: FuturesOrderEvent) -> None:
        wrapper = UserDataEvent(
            event_type=UserDataEventType.ORDER_TRADE_UPDATE,
            order_event=order_event,
        )
        d = wrapper.to_dict()
        restored = UserDataEvent.from_dict(d)

        assert restored.event_type == UserDataEventType.ORDER_TRADE_UPDATE
        assert restored.order_event is not None
        assert restored.order_event.symbol == "BTCUSDT"

    def test_from_dict_position_event(self, position_event: FuturesPositionEvent) -> None:
        wrapper = UserDataEvent(
            event_type=UserDataEventType.ACCOUNT_UPDATE,
            position_event=position_event,
        )
        d = wrapper.to_dict()
        restored = UserDataEvent.from_dict(d)

        assert restored.event_type == UserDataEventType.ACCOUNT_UPDATE
        assert restored.position_event is not None
        assert restored.position_event.position_amt == Decimal("0.001")

    def test_from_binance_order_trade_update(self) -> None:
        binance_msg = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1000000,
            "o": {
                "s": "BTCUSDT",
                "c": "grinder_1",
                "S": "BUY",
                "X": "NEW",
                "i": 123,
                "p": "50000",
                "q": "0.001",
                "z": "0",
                "ap": "0",
            },
        }
        event = UserDataEvent.from_binance(binance_msg)

        assert event.event_type == UserDataEventType.ORDER_TRADE_UPDATE
        assert event.order_event is not None
        assert event.order_event.symbol == "BTCUSDT"
        assert event.position_event is None

    def test_from_binance_account_update(self) -> None:
        binance_msg = {
            "e": "ACCOUNT_UPDATE",
            "E": 1000000,
            "a": {"P": [{"s": "BTCUSDT", "pa": "0.001", "ep": "50000", "up": "5"}]},
        }
        event = UserDataEvent.from_binance(binance_msg)

        assert event.event_type == UserDataEventType.ACCOUNT_UPDATE
        assert event.position_event is not None
        assert event.position_event.symbol == "BTCUSDT"
        assert event.order_event is None

    def test_from_binance_account_update_with_filter(self) -> None:
        binance_msg = {
            "e": "ACCOUNT_UPDATE",
            "E": 1000000,
            "a": {
                "P": [
                    {"s": "BTCUSDT", "pa": "0.001", "ep": "50000", "up": "5"},
                    {"s": "ETHUSDT", "pa": "1.0", "ep": "3000", "up": "100"},
                ]
            },
        }
        event = UserDataEvent.from_binance(binance_msg, symbol_filter="ETHUSDT")

        assert event.position_event is not None
        assert event.position_event.symbol == "ETHUSDT"
        assert event.position_event.position_amt == Decimal("1.0")

    def test_from_binance_unknown_event(self) -> None:
        binance_msg = {
            "e": "MARGIN_CALL",
            "E": 1000000,
            "cw": "100.0",
        }
        event = UserDataEvent.from_binance(binance_msg)

        assert event.event_type == UserDataEventType.UNKNOWN
        assert event.raw_data is not None
        assert event.raw_data["e"] == "MARGIN_CALL"
        assert event.order_event is None
        assert event.position_event is None

    def test_from_binance_non_actionable_unknown_logs_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        binance_msg = {"e": "ACCOUNT_CONFIG_UPDATE", "E": 1000000}

        event = UserDataEvent.from_binance(binance_msg)

        assert event.event_type == UserDataEventType.UNKNOWN
        warnings = [
            r
            for r in caplog.records
            if r.message == "unknown_event_type" and r.levelno >= logging.WARNING
        ]
        assert warnings == []

    def test_from_binance_risky_unknown_keeps_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        binance_msg = {"e": "MARGIN_CALL", "E": 1000000}

        _ = UserDataEvent.from_binance(binance_msg)

        warnings = [
            r
            for r in caplog.records
            if r.message == "unknown_event_type" and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1

    def test_to_json_is_deterministic(self, order_event: FuturesOrderEvent) -> None:
        wrapper = UserDataEvent(
            event_type=UserDataEventType.ORDER_TRADE_UPDATE,
            order_event=order_event,
        )
        json1 = wrapper.to_json()
        json2 = wrapper.to_json()
        assert json1 == json2


class TestOrderLifecycle:
    """Integration tests for order lifecycle parsing."""

    def test_full_order_lifecycle(self) -> None:
        """Test parsing NEW -> PARTIALLY_FILLED -> FILLED sequence."""
        messages = [
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": 1000,
                "o": {
                    "s": "BTCUSDT",
                    "c": "grinder_1",
                    "S": "BUY",
                    "X": "NEW",
                    "i": 123,
                    "p": "50000",
                    "q": "0.002",
                    "z": "0",
                    "ap": "0",
                },
            },
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": 2000,
                "o": {
                    "s": "BTCUSDT",
                    "c": "grinder_1",
                    "S": "BUY",
                    "X": "PARTIALLY_FILLED",
                    "i": 123,
                    "p": "50000",
                    "q": "0.002",
                    "z": "0.001",
                    "ap": "50000",
                },
            },
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": 3000,
                "o": {
                    "s": "BTCUSDT",
                    "c": "grinder_1",
                    "S": "BUY",
                    "X": "FILLED",
                    "i": 123,
                    "p": "50000",
                    "q": "0.002",
                    "z": "0.002",
                    "ap": "50000.5",
                },
            },
        ]

        events = [UserDataEvent.from_binance(msg) for msg in messages]

        assert len(events) == 3
        assert all(e.event_type == UserDataEventType.ORDER_TRADE_UPDATE for e in events)

        # Check status progression
        assert events[0].order_event is not None
        assert events[0].order_event.status == OrderState.OPEN
        assert events[0].order_event.executed_qty == Decimal("0")

        assert events[1].order_event is not None
        assert events[1].order_event.status == OrderState.PARTIALLY_FILLED
        assert events[1].order_event.executed_qty == Decimal("0.001")

        assert events[2].order_event is not None
        assert events[2].order_event.status == OrderState.FILLED
        assert events[2].order_event.executed_qty == Decimal("0.002")
        assert events[2].order_event.avg_price == Decimal("50000.5")


class TestPositionLifecycle:
    """Integration tests for position lifecycle parsing."""

    def test_position_open_and_close(self) -> None:
        """Test parsing position: 0 -> long -> 0."""
        messages = [
            {
                "e": "ACCOUNT_UPDATE",
                "E": 1000,
                "a": {"P": [{"s": "BTCUSDT", "pa": "0", "ep": "0", "up": "0"}]},
            },
            {
                "e": "ACCOUNT_UPDATE",
                "E": 2000,
                "a": {"P": [{"s": "BTCUSDT", "pa": "0.001", "ep": "50000", "up": "5"}]},
            },
            {
                "e": "ACCOUNT_UPDATE",
                "E": 3000,
                "a": {"P": [{"s": "BTCUSDT", "pa": "0", "ep": "0", "up": "0"}]},
            },
        ]

        events = [UserDataEvent.from_binance(msg) for msg in messages]

        assert len(events) == 3
        assert all(e.event_type == UserDataEventType.ACCOUNT_UPDATE for e in events)

        # Check position progression
        assert events[0].position_event is not None
        assert events[0].position_event.position_amt == Decimal("0")

        assert events[1].position_event is not None
        assert events[1].position_event.position_amt == Decimal("0.001")
        assert events[1].position_event.entry_price == Decimal("50000")

        assert events[2].position_event is not None
        assert events[2].position_event.position_amt == Decimal("0")


class TestFixtureGoldenTests:
    """Golden tests using JSONL fixtures for deterministic verification."""

    FIXTURES_DIR = "tests/fixtures/user_data"

    def _load_fixture(self, filename: str) -> list[dict[str, Any]]:
        """Load JSONL fixture file."""
        fixture_path = Path(self.FIXTURES_DIR) / filename
        messages: list[dict[str, Any]] = []
        with fixture_path.open() as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if stripped:
                    messages.append(json.loads(stripped))
        return messages

    def test_order_lifecycle_fixture_produces_deterministic_output(self) -> None:
        """Same fixture input → same normalized output (determinism check)."""
        messages = self._load_fixture("order_lifecycle.jsonl")

        # First pass
        events1 = [UserDataEvent.from_binance(msg) for msg in messages]
        json1 = [e.to_json() for e in events1]

        # Second pass (same input)
        events2 = [UserDataEvent.from_binance(msg) for msg in messages]
        json2 = [e.to_json() for e in events2]

        # Verify determinism
        assert json1 == json2

    def test_position_lifecycle_fixture_produces_deterministic_output(self) -> None:
        """Same fixture input → same normalized output (determinism check)."""
        messages = self._load_fixture("position_lifecycle.jsonl")

        # First pass
        events1 = [UserDataEvent.from_binance(msg) for msg in messages]
        json1 = [e.to_json() for e in events1]

        # Second pass
        events2 = [UserDataEvent.from_binance(msg) for msg in messages]
        json2 = [e.to_json() for e in events2]

        assert json1 == json2

    def test_order_lifecycle_fixture_content(self) -> None:
        """Verify order_lifecycle.jsonl produces expected state transitions."""
        messages = self._load_fixture("order_lifecycle.jsonl")
        events = [UserDataEvent.from_binance(msg) for msg in messages]

        assert len(events) == 3
        assert all(e.event_type == UserDataEventType.ORDER_TRADE_UPDATE for e in events)

        # NEW
        assert events[0].order_event is not None
        assert events[0].order_event.status == OrderState.OPEN
        assert events[0].order_event.client_order_id == "grinder_order_001"
        assert events[0].order_event.symbol == "BTCUSDT"
        assert events[0].order_event.executed_qty == Decimal("0")

        # PARTIALLY_FILLED
        assert events[1].order_event is not None
        assert events[1].order_event.status == OrderState.PARTIALLY_FILLED
        assert events[1].order_event.executed_qty == Decimal("0.005")

        # FILLED
        assert events[2].order_event is not None
        assert events[2].order_event.status == OrderState.FILLED
        assert events[2].order_event.executed_qty == Decimal("0.010")
        assert events[2].order_event.avg_price == Decimal("42500.00")

    def test_position_lifecycle_fixture_content(self) -> None:
        """Verify position_lifecycle.jsonl produces expected state transitions."""
        messages = self._load_fixture("position_lifecycle.jsonl")
        events = [UserDataEvent.from_binance(msg) for msg in messages]

        assert len(events) == 4
        assert all(e.event_type == UserDataEventType.ACCOUNT_UPDATE for e in events)

        # Flat (no position)
        assert events[0].position_event is not None
        assert events[0].position_event.position_amt == Decimal("0")

        # Open long
        assert events[1].position_event is not None
        assert events[1].position_event.position_amt == Decimal("0.010")
        assert events[1].position_event.entry_price == Decimal("42500.00")

        # Unrealized PnL update
        assert events[2].position_event is not None
        assert events[2].position_event.unrealized_pnl == Decimal("25.50")

        # Closed (flat again)
        assert events[3].position_event is not None
        assert events[3].position_event.position_amt == Decimal("0")
