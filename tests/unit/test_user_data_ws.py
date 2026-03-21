"""Tests for FuturesUserDataWsConnector."""

import json

import pytest

from grinder.connectors.binance_user_data_ws import (
    FakeListenKeyManager,
    FuturesUserDataWsConnector,
    UserDataWsConfig,
)
from grinder.connectors.binance_ws import FakeWsTransport
from grinder.connectors.data_connector import ConnectorState
from grinder.connectors.errors import ConnectorClosedError, ConnectorTransientError
from grinder.core import OrderState
from grinder.execution.futures_events import UserDataEventType


# Sample messages for testing
def make_order_update_msg(
    symbol: str = "BTCUSDT",
    client_order_id: str = "grinder_1",
    side: str = "BUY",
    status: str = "NEW",
    order_id: int = 123,
    price: str = "50000",
    qty: str = "0.001",
    executed_qty: str = "0",
    avg_price: str = "0",
    ts: int = 1000000,
) -> str:
    """Create ORDER_TRADE_UPDATE message."""
    return json.dumps(
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": ts,
            "T": ts,
            "o": {
                "s": symbol,
                "c": client_order_id,
                "S": side,
                "o": "LIMIT",
                "X": status,
                "i": order_id,
                "p": price,
                "q": qty,
                "z": executed_qty,
                "ap": avg_price,
            },
        }
    )


def make_account_update_msg(
    symbol: str = "BTCUSDT",
    position_amt: str = "0.001",
    entry_price: str = "50000",
    unrealized_pnl: str = "5",
    ts: int = 1000000,
) -> str:
    """Create ACCOUNT_UPDATE message."""
    return json.dumps(
        {
            "e": "ACCOUNT_UPDATE",
            "E": ts,
            "T": ts,
            "a": {
                "m": "ORDER",
                "B": [],
                "P": [
                    {
                        "s": symbol,
                        "pa": position_amt,
                        "ep": entry_price,
                        "up": unrealized_pnl,
                        "mt": "cross",
                        "ps": "BOTH",
                    }
                ],
            },
        }
    )


class TestFuturesUserDataWsConnector:
    """Tests for FuturesUserDataWsConnector."""

    @pytest.fixture
    def config(self) -> UserDataWsConfig:
        """Create test config."""
        return UserDataWsConfig(
            base_url="https://testnet.binancefuture.com",
            api_key="test_api_key",
            use_testnet=True,
            keepalive_interval_sec=30,
        )

    @pytest.fixture
    def fake_manager(self) -> FakeListenKeyManager:
        """Create fake listen key manager."""
        return FakeListenKeyManager(listen_key="test_listen_key_abc123")

    @pytest.fixture
    def order_messages(self) -> list[str]:
        """Sample order update messages."""
        return [
            make_order_update_msg(status="NEW", ts=1000),
            make_order_update_msg(status="PARTIALLY_FILLED", executed_qty="0.0005", ts=2000),
            make_order_update_msg(
                status="FILLED", executed_qty="0.001", avg_price="50000", ts=3000
            ),
        ]

    @pytest.fixture
    def position_messages(self) -> list[str]:
        """Sample position update messages."""
        return [
            make_account_update_msg(position_amt="0", ts=1000),
            make_account_update_msg(position_amt="0.001", entry_price="50000", ts=2000),
            make_account_update_msg(position_amt="0", ts=3000),
        ]


class TestConnection(TestFuturesUserDataWsConnector):
    """Tests for connection lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_creates_listen_key(
        self, config: UserDataWsConfig, fake_manager: FakeListenKeyManager
    ) -> None:
        """Should call listenKeyManager.create on connect."""
        transport = FakeWsTransport(messages=[])
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        await connector.connect()

        assert fake_manager.create_count == 1
        assert connector.listen_key == "test_listen_key_abc123"

        await connector.close()

    @pytest.mark.asyncio
    async def test_connect_sets_state_connected(
        self, config: UserDataWsConfig, fake_manager: FakeListenKeyManager
    ) -> None:
        """Should set state to CONNECTED after successful connect."""
        transport = FakeWsTransport(messages=[])
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        await connector.connect()

        assert connector.state == ConnectorState.CONNECTED

        await connector.close()

    @pytest.mark.asyncio
    async def test_close_closes_listen_key(
        self, config: UserDataWsConfig, fake_manager: FakeListenKeyManager
    ) -> None:
        """Should call listenKeyManager.close on close."""
        transport = FakeWsTransport(messages=[])
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        await connector.connect()
        await connector.close()

        assert fake_manager.close_count == 1

    @pytest.mark.asyncio
    async def test_close_sets_state_closed(
        self, config: UserDataWsConfig, fake_manager: FakeListenKeyManager
    ) -> None:
        """Should set state to CLOSED after close."""
        transport = FakeWsTransport(messages=[])
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        await connector.connect()
        await connector.close()

        assert connector.state == ConnectorState.CLOSED

    @pytest.mark.asyncio
    async def test_context_manager(
        self, config: UserDataWsConfig, fake_manager: FakeListenKeyManager
    ) -> None:
        """Should work as async context manager."""
        transport = FakeWsTransport(messages=[make_order_update_msg()])
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        async with connector:
            assert connector.state == ConnectorState.CONNECTED

        assert connector.state == ConnectorState.CLOSED  # type: ignore[comparison-overlap]


class TestIterEvents(TestFuturesUserDataWsConnector):
    """Tests for iter_events."""

    @pytest.mark.asyncio
    async def test_yields_order_events(
        self,
        config: UserDataWsConfig,
        fake_manager: FakeListenKeyManager,
        order_messages: list[str],
    ) -> None:
        """Should yield FuturesOrderEvent for ORDER_TRADE_UPDATE messages."""
        transport = FakeWsTransport(messages=order_messages)
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        events = []
        async with connector:
            async for event in connector.iter_events():
                events.append(event)
                if len(events) >= 3:
                    break

        assert len(events) == 3
        assert all(e.event_type == UserDataEventType.ORDER_TRADE_UPDATE for e in events)
        assert events[0].order_event is not None
        assert events[0].order_event.status == OrderState.OPEN
        assert events[1].order_event is not None
        assert events[1].order_event.status == OrderState.PARTIALLY_FILLED
        assert events[2].order_event is not None
        assert events[2].order_event.status == OrderState.FILLED

    @pytest.mark.asyncio
    async def test_yields_position_events(
        self,
        config: UserDataWsConfig,
        fake_manager: FakeListenKeyManager,
        position_messages: list[str],
    ) -> None:
        """Should yield FuturesPositionEvent for ACCOUNT_UPDATE messages."""
        transport = FakeWsTransport(messages=position_messages)
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        events = []
        async with connector:
            async for event in connector.iter_events():
                events.append(event)
                if len(events) >= 3:
                    break

        assert len(events) == 3
        assert all(e.event_type == UserDataEventType.ACCOUNT_UPDATE for e in events)
        assert events[0].position_event is not None
        assert events[0].position_event.position_amt.is_zero()
        assert events[1].position_event is not None
        assert not events[1].position_event.position_amt.is_zero()

    @pytest.mark.asyncio
    async def test_handles_mixed_events(
        self, config: UserDataWsConfig, fake_manager: FakeListenKeyManager
    ) -> None:
        """Should handle mix of order and position events."""
        messages = [
            make_order_update_msg(status="NEW", ts=1000),
            make_account_update_msg(position_amt="0.001", ts=2000),
            make_order_update_msg(status="FILLED", ts=3000),
        ]
        transport = FakeWsTransport(messages=messages)
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        events = []
        async with connector:
            async for event in connector.iter_events():
                events.append(event)
                if len(events) >= 3:
                    break

        assert len(events) == 3
        assert events[0].event_type == UserDataEventType.ORDER_TRADE_UPDATE
        assert events[1].event_type == UserDataEventType.ACCOUNT_UPDATE
        assert events[2].event_type == UserDataEventType.ORDER_TRADE_UPDATE

    @pytest.mark.asyncio
    async def test_handles_unknown_events(
        self, config: UserDataWsConfig, fake_manager: FakeListenKeyManager
    ) -> None:
        """Should yield UNKNOWN for unrecognized event types."""
        messages = [
            json.dumps({"e": "MARGIN_CALL", "E": 1000, "cw": "100.0"}),
            make_order_update_msg(status="NEW", ts=2000),
        ]
        transport = FakeWsTransport(messages=messages)
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        events = []
        async with connector:
            async for event in connector.iter_events():
                events.append(event)
                if len(events) >= 2:
                    break

        assert len(events) == 2
        assert events[0].event_type == UserDataEventType.UNKNOWN
        assert events[0].raw_data is not None
        assert events[1].event_type == UserDataEventType.ORDER_TRADE_UPDATE

    def test_listen_key_expired_triggers_transient_reconnect_signal(
        self, config: UserDataWsConfig, fake_manager: FakeListenKeyManager
    ) -> None:
        """listenKeyExpired should force reconnect path via ConnectorTransientError."""
        transport = FakeWsTransport(messages=[])
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        with pytest.raises(ConnectorTransientError, match="listenKeyExpired"):
            connector._parse_event(json.dumps({"e": "listenKeyExpired", "E": 1234}))

    @pytest.mark.asyncio
    async def test_skips_subscription_responses(
        self, config: UserDataWsConfig, fake_manager: FakeListenKeyManager
    ) -> None:
        """Should skip subscription confirmation messages."""
        messages = [
            json.dumps({"result": None, "id": 1}),
            make_order_update_msg(status="NEW", ts=1000),
        ]
        transport = FakeWsTransport(messages=messages)
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        events = []
        async with connector:
            async for event in connector.iter_events():
                events.append(event)
                if len(events) >= 1:
                    break

        # Only the order update, not the subscription response
        assert len(events) == 1
        assert events[0].event_type == UserDataEventType.ORDER_TRADE_UPDATE

    @pytest.mark.asyncio
    async def test_raises_if_not_connected(
        self, config: UserDataWsConfig, fake_manager: FakeListenKeyManager
    ) -> None:
        """Should raise ConnectorClosedError if not connected."""
        transport = FakeWsTransport(messages=[])
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        with pytest.raises(ConnectorClosedError):
            async for _ in connector.iter_events():
                pass


class TestStats(TestFuturesUserDataWsConnector):
    """Tests for statistics tracking."""

    @pytest.mark.asyncio
    async def test_tracks_message_count(
        self,
        config: UserDataWsConfig,
        fake_manager: FakeListenKeyManager,
        order_messages: list[str],
    ) -> None:
        """Should track messages received."""
        transport = FakeWsTransport(messages=order_messages)
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        async with connector:
            async for _ in connector.iter_events():
                if connector.stats.messages_received >= 3:
                    break

        assert connector.stats.messages_received == 3

    @pytest.mark.asyncio
    async def test_tracks_order_events(
        self,
        config: UserDataWsConfig,
        fake_manager: FakeListenKeyManager,
        order_messages: list[str],
    ) -> None:
        """Should track order events count."""
        transport = FakeWsTransport(messages=order_messages)
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        async with connector:
            async for _ in connector.iter_events():
                if connector.stats.order_events >= 3:
                    break

        assert connector.stats.order_events == 3

    @pytest.mark.asyncio
    async def test_tracks_position_events(
        self,
        config: UserDataWsConfig,
        fake_manager: FakeListenKeyManager,
        position_messages: list[str],
    ) -> None:
        """Should track position events count."""
        transport = FakeWsTransport(messages=position_messages)
        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=fake_manager,
            transport=transport,
        )

        async with connector:
            async for _ in connector.iter_events():
                if connector.stats.position_events >= 3:
                    break

        assert connector.stats.position_events == 3


class TestFakeListenKeyManager:
    """Tests for FakeListenKeyManager."""

    def test_create_returns_configured_key(self) -> None:
        manager = FakeListenKeyManager(listen_key="custom_key_123")
        assert manager.create() == "custom_key_123"

    def test_create_increments_count(self) -> None:
        manager = FakeListenKeyManager()
        manager.create()
        manager.create()
        assert manager.create_count == 2

    def test_create_raises_when_configured(self) -> None:
        manager = FakeListenKeyManager(create_fails=True)
        with pytest.raises(ConnectorTransientError):
            manager.create()

    def test_keepalive_returns_true_by_default(self) -> None:
        manager = FakeListenKeyManager()
        assert manager.keepalive("test_key") is True

    def test_keepalive_returns_false_when_configured(self) -> None:
        manager = FakeListenKeyManager(keepalive_fails=True)
        assert manager.keepalive("test_key") is False

    def test_close_returns_true(self) -> None:
        manager = FakeListenKeyManager()
        assert manager.close("test_key") is True

    def test_close_increments_count(self) -> None:
        manager = FakeListenKeyManager()
        manager.close("test_key")
        manager.close("test_key")
        assert manager.close_count == 2
