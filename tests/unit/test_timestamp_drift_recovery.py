"""Tests for Binance timestamp drift recovery.

Proves:
1. refresh_ts_offset updates the stored offset
2. Signed requests use the corrected timestamp
3. Engine refreshes offset on -1021
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    import pytest

from grinder.connectors.live_connector import SafeMode
from grinder.execution.binance_futures_port import BinanceFuturesPort, BinanceFuturesPortConfig
from grinder.live import LiveEngineConfig, LiveEngineV0


def _make_port(
    offset: int = 0, monkeypatch: pytest.MonkeyPatch | None = None
) -> BinanceFuturesPort:
    if monkeypatch is not None:
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
    http = MagicMock()
    cfg = BinanceFuturesPortConfig(
        api_key="test",
        api_secret="test",
        base_url="https://fapi.binance.com",
        mode=SafeMode.LIVE_TRADE,
        allow_mainnet=True,
        symbol_whitelist=["BTCUSDT"],
        max_notional_per_order=Decimal("100"),
    )
    return BinanceFuturesPort(http_client=http, config=cfg, _ts_offset_ms=offset)


class TestRefreshTsOffset:
    """refresh_ts_offset updates the stored offset from server time."""

    def test_offset_updated_from_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        port = _make_port(offset=0, monkeypatch=monkeypatch)
        assert port._ts_offset_ms == 0

        # Mock http_client to return server time
        port.http_client.request.return_value = MagicMock(  # type: ignore[attr-defined]
            json_data={"serverTime": 1000000}
        )

        with patch("grinder.execution.binance_futures_port.time.time", return_value=1002.0):
            new_offset = port.refresh_ts_offset()

        assert new_offset == 2000  # local 2000ms ahead
        assert port._ts_offset_ms == 2000

    def test_offset_updates_from_behind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        port = _make_port(offset=500, monkeypatch=monkeypatch)

        port.http_client.request.return_value = MagicMock(  # type: ignore[attr-defined]
            json_data={"serverTime": 1000000}
        )

        with patch("grinder.execution.binance_futures_port.time.time", return_value=998.5):
            port.refresh_ts_offset()

        assert port._ts_offset_ms == -1500  # local 1500ms behind

    def test_failed_refresh_keeps_existing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        port = _make_port(offset=1234, monkeypatch=monkeypatch)

        port.http_client.request.side_effect = TimeoutError("timeout")  # type: ignore[attr-defined]
        port.refresh_ts_offset()

        assert port._ts_offset_ms == 1234  # unchanged


class TestSignedTimestampUsesOffset:
    """Signed requests apply the offset to timestamp."""

    def test_positive_offset_subtracted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        port = _make_port(offset=2000, monkeypatch=monkeypatch)

        with patch("grinder.execution.binance_futures_port.time.time", return_value=1005.0):
            params = port._sign_request({})

        # timestamp = 1005000 - 2000 = 1003000
        assert params["timestamp"] == 1003000

    def test_negative_offset_adds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        port = _make_port(offset=-1500, monkeypatch=monkeypatch)

        with patch("grinder.execution.binance_futures_port.time.time", return_value=998.5):
            params = port._sign_request({})

        # timestamp = 998500 - (-1500) = 1000000
        assert params["timestamp"] == 1000000


class TestEngineRefreshesOnDriftError:
    """Engine calls refresh_ts_offset when -1021 detected."""

    def test_sync_1021_triggers_refresh(self) -> None:
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        port_mock = MagicMock()
        engine = LiveEngineV0(
            paper, port_mock, LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        )
        engine._exchange_port = MagicMock()
        engine._exchange_port.refresh_ts_offset = MagicMock(return_value=500)

        syncer = MagicMock()
        engine._account_syncer = syncer
        sync_result = MagicMock()
        sync_result.error = "ConnectorTransientError: Binance transient error -1021: Timestamp"
        sync_result.snapshot = None
        syncer.sync.return_value = sync_result

        engine._tick_account_sync()

        engine._exchange_port.refresh_ts_offset.assert_called_once()

    def test_sync_non_1021_does_not_refresh(self) -> None:
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        port_mock = MagicMock()
        engine = LiveEngineV0(
            paper, port_mock, LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        )
        engine._exchange_port = MagicMock()
        engine._exchange_port.refresh_ts_offset = MagicMock()

        syncer = MagicMock()
        engine._account_syncer = syncer
        sync_result = MagicMock()
        sync_result.error = "ConnectorTransientError: network timeout"
        sync_result.snapshot = None
        syncer.sync.return_value = sync_result

        engine._tick_account_sync()

        engine._exchange_port.refresh_ts_offset.assert_not_called()


class TestActionPath1021Refresh:
    """Engine action-dispatch path also refreshes offset on -1021."""

    def test_action_dispatch_1021_triggers_refresh(self) -> None:
        """_process_action exhaustion with -1021 calls refresh_ts_offset."""
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        port_mock = MagicMock()
        engine = LiveEngineV0(
            paper, port_mock, LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        )
        engine._exchange_port = MagicMock()
        engine._exchange_port.refresh_ts_offset = MagicMock(return_value=500)
        # Make place_order raise -1021 non-retryable error
        from grinder.connectors.errors import ConnectorTransientError  # noqa: PLC0415

        engine._exchange_port.place_order.side_effect = ConnectorTransientError(
            "Binance transient error -1021: Timestamp for this request was 1000ms ahead"
        )

        from grinder.core import OrderSide  # noqa: PLC0415
        from grinder.execution.types import ActionType, ExecutionAction  # noqa: PLC0415

        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            client_order_id="test-1",
            reason="test",
        )
        engine._process_action(action, ts=1000)

        engine._exchange_port.refresh_ts_offset.assert_called_once()

    def test_action_dispatch_non_1021_does_not_refresh(self) -> None:
        """Non-1021 transient error does not trigger refresh."""
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        port_mock = MagicMock()
        engine = LiveEngineV0(
            paper, port_mock, LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        )
        engine._exchange_port = MagicMock()
        engine._exchange_port.refresh_ts_offset = MagicMock()
        from grinder.connectors.errors import ConnectorTransientError  # noqa: PLC0415

        engine._exchange_port.place_order.side_effect = ConnectorTransientError(
            "Binance transient error -1000: some other error"
        )

        from grinder.core import OrderSide  # noqa: PLC0415
        from grinder.execution.types import ActionType, ExecutionAction  # noqa: PLC0415

        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            client_order_id="test-1",
            reason="test",
        )
        engine._process_action(action, ts=1000)

        engine._exchange_port.refresh_ts_offset.assert_not_called()
