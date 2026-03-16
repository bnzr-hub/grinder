"""Tests for scripts/exchange_state.py — canonical operator CLI.

ADR-089: exchange_state.py is the canonical pre-flight/cleanup/verify tool
referenced by docs/runbooks/34_ROLLING_LIVE_VERIFICATION.md.

All tests mock _build_port so no real exchange connection is needed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from scripts.exchange_state import _build_port, cmd_check, cmd_cleanup, cmd_verify, main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_port(
    *,
    orders: list[dict[str, Any]] | None = None,
    positions: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Build a MagicMock that behaves like BinanceFuturesPort."""
    port = MagicMock()
    port.fetch_open_orders_raw.return_value = orders or []
    port.fetch_positions_raw.return_value = positions or [{"positionAmt": "0"}]
    port.cancel_all_orders.return_value = None
    port.close_position.return_value = "mock_order_123"
    return port


FLAT_POS = [{"positionAmt": "0"}]
LONG_POS = [
    {
        "positionAmt": "0.005",
        "entryPrice": "50000",
        "markPrice": "51000",
        "unRealizedProfit": "5.0",
    }
]
TWO_ORDERS = [
    {"orderId": "111", "side": "BUY", "price": "49000", "origQty": "0.01", "status": "NEW"},
    {"orderId": "222", "side": "SELL", "price": "51000", "origQty": "0.01", "status": "NEW"},
]


# ---------------------------------------------------------------------------
# CLI dispatch / usage tests
# ---------------------------------------------------------------------------


class TestExchangeStateCLI:
    """CLI entry point and argument handling."""

    def test_usage_when_no_args(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No args -> usage text + exit 1."""
        with patch("scripts.exchange_state.sys") as mock_sys:
            mock_sys.argv = ["exchange_state"]
            mock_sys.exit = MagicMock(side_effect=SystemExit(1))
            with pytest.raises(SystemExit):
                main()
        out = capsys.readouterr().out
        assert "Usage:" in out
        assert "check" in out
        assert "cleanup" in out
        assert "verify" in out

    def test_usage_when_one_arg(self, capsys: pytest.CaptureFixture[str]) -> None:
        """One arg (command only, no symbol) -> usage text + exit 1."""
        with patch("scripts.exchange_state.sys") as mock_sys:
            mock_sys.argv = ["exchange_state", "check"]
            mock_sys.exit = MagicMock(side_effect=SystemExit(1))
            with pytest.raises(SystemExit):
                main()
        out = capsys.readouterr().out
        assert "Usage:" in out

    def test_unknown_command_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Unknown command -> error message + exit 1."""
        with patch("scripts.exchange_state.sys") as mock_sys:
            mock_sys.argv = ["exchange_state", "nope", "BTCUSDT"]
            mock_sys.exit = MagicMock(side_effect=SystemExit(1))
            with pytest.raises(SystemExit):
                main()
        out = capsys.readouterr().out
        assert "unknown command" in out.lower()

    def test_dispatch_check(self) -> None:
        """'check' dispatches to cmd_check."""
        port = _mock_port()
        with (
            patch("scripts.exchange_state._build_port", return_value=port),
            patch("scripts.exchange_state.sys") as mock_sys,
        ):
            mock_sys.argv = ["exchange_state", "check", "btcusdt"]
            mock_sys.exit = MagicMock()
            main()
        port.fetch_open_orders_raw.assert_called_once_with("BTCUSDT")

    def test_dispatch_verify(self) -> None:
        """'verify' dispatches to cmd_verify."""
        port = _mock_port()
        with (
            patch("scripts.exchange_state._build_port", return_value=port),
            patch("scripts.exchange_state.sys") as mock_sys,
        ):
            mock_sys.argv = ["exchange_state", "verify", "BTCUSDT"]
            mock_sys.exit = MagicMock()
            main()
        port.fetch_open_orders_raw.assert_called_once_with("BTCUSDT")
        port.fetch_positions_raw.assert_called_once_with("BTCUSDT")

    def test_symbol_uppercased(self) -> None:
        """Symbol argument is uppercased regardless of input."""
        port = _mock_port()
        with (
            patch("scripts.exchange_state._build_port", return_value=port),
            patch("scripts.exchange_state.sys") as mock_sys,
        ):
            mock_sys.argv = ["exchange_state", "check", "ethusdt"]
            mock_sys.exit = MagicMock()
            main()
        port.fetch_open_orders_raw.assert_called_once_with("ETHUSDT")


# ---------------------------------------------------------------------------
# cmd_verify tests
# ---------------------------------------------------------------------------


class TestCmdVerify:
    """Verify command: assert 0 orders + flat position."""

    def test_verify_clean_exits_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """0 orders + flat -> CLEAN, no sys.exit(1)."""
        port = _mock_port(orders=[], positions=FLAT_POS)
        with patch("scripts.exchange_state._build_port", return_value=port):
            cmd_verify("BTCUSDT")
        out = capsys.readouterr().out
        assert "status=CLEAN" in out
        assert "orders=0" in out
        assert "position=FLAT" in out

    def test_verify_dirty_orders_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Open orders -> DIRTY, sys.exit(1)."""
        port = _mock_port(orders=TWO_ORDERS, positions=FLAT_POS)
        with (
            patch("scripts.exchange_state._build_port", return_value=port),
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_verify("BTCUSDT")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "status=DIRTY" in out
        assert "orders=2" in out

    def test_verify_dirty_position_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Open position -> DIRTY, sys.exit(1)."""
        port = _mock_port(orders=[], positions=LONG_POS)
        with (
            patch("scripts.exchange_state._build_port", return_value=port),
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_verify("BTCUSDT")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "status=DIRTY" in out
        assert "0.005" in out

    def test_verify_dirty_both_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Open orders + position -> DIRTY, sys.exit(1)."""
        port = _mock_port(orders=TWO_ORDERS, positions=LONG_POS)
        with (
            patch("scripts.exchange_state._build_port", return_value=port),
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_verify("BTCUSDT")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "status=DIRTY" in out


# ---------------------------------------------------------------------------
# cmd_check tests
# ---------------------------------------------------------------------------


class TestCmdCheck:
    """Check command: read-only display of orders + position."""

    def test_check_clean_outputs_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Clean state -> EXCHANGE_STATE_CHECK with FLAT + 0 orders."""
        port = _mock_port(orders=[], positions=FLAT_POS)
        with patch("scripts.exchange_state._build_port", return_value=port):
            cmd_check("BTCUSDT")
        out = capsys.readouterr().out
        assert "EXCHANGE_STATE_CHECK symbol=BTCUSDT" in out
        assert "open_orders=0" in out
        assert "position: FLAT" in out
        assert "summary: orders=0 position=FLAT" in out

    def test_check_with_orders_shows_details(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Orders present -> each order listed with details."""
        port = _mock_port(orders=TWO_ORDERS, positions=FLAT_POS)
        with patch("scripts.exchange_state._build_port", return_value=port):
            cmd_check("BTCUSDT")
        out = capsys.readouterr().out
        assert "open_orders=2" in out
        assert "order_id=111" in out
        assert "order_id=222" in out
        assert "side=BUY" in out
        assert "side=SELL" in out

    def test_check_with_position_shows_details(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Position present -> position details + non-FLAT summary."""
        port = _mock_port(orders=[], positions=LONG_POS)
        with patch("scripts.exchange_state._build_port", return_value=port):
            cmd_check("BTCUSDT")
        out = capsys.readouterr().out
        assert "qty=0.005" in out
        assert "entry=50000" in out
        assert "summary: orders=0 position=0.005" in out

    def test_check_is_read_only(self) -> None:
        """Check must call _build_port with write=False."""
        port = _mock_port()
        with patch("scripts.exchange_state._build_port", return_value=port) as mock_build:
            cmd_check("BTCUSDT")
        mock_build.assert_called_once_with("BTCUSDT", write=False)


# ---------------------------------------------------------------------------
# cmd_cleanup tests
# ---------------------------------------------------------------------------


class TestCmdCleanup:
    """Cleanup command: cancel all orders + close position + verify."""

    def test_cleanup_requires_allow_mainnet_trade(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Missing ALLOW_MAINNET_TRADE -> error + exit 1."""
        monkeypatch.delenv("ALLOW_MAINNET_TRADE", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            cmd_cleanup("BTCUSDT")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "ALLOW_MAINNET_TRADE" in out

    def test_cleanup_calls_cancel_and_verify(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With orders present -> cancel_all_orders called, then verify."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        port = _mock_port(orders=TWO_ORDERS, positions=FLAT_POS)
        # After cancel, return empty orders
        port.fetch_open_orders_raw.side_effect = [TWO_ORDERS, [], []]
        port.fetch_positions_raw.side_effect = [FLAT_POS, FLAT_POS]
        with patch("scripts.exchange_state._build_port", return_value=port):
            cmd_cleanup("BTCUSDT")
        port.cancel_all_orders.assert_called_once_with("BTCUSDT")
        out = capsys.readouterr().out
        assert "EXCHANGE_CLEANUP symbol=BTCUSDT" in out
        assert "orders_before=2" in out
        assert "cancel_all_orders: done" in out
        assert "EXCHANGE_STATE_VERIFY" in out
        assert "status=CLEAN" in out

    def test_cleanup_skips_cancel_when_no_orders(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """0 orders -> skip cancel, still verify."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        port = _mock_port(orders=[], positions=FLAT_POS)
        with patch("scripts.exchange_state._build_port", return_value=port):
            cmd_cleanup("BTCUSDT")
        port.cancel_all_orders.assert_not_called()
        out = capsys.readouterr().out
        assert "cancel_all_orders: skipped (0 orders)" in out

    def test_cleanup_closes_position(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Open position -> close_position called."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        port = _mock_port(orders=[], positions=LONG_POS)
        # After close, position is flat for verify
        port.fetch_positions_raw.side_effect = [LONG_POS, FLAT_POS]
        with patch("scripts.exchange_state._build_port", return_value=port):
            cmd_cleanup("BTCUSDT")
        port.close_position.assert_called_once_with("BTCUSDT")
        out = capsys.readouterr().out
        assert "position_before=0.005" in out
        assert "close_position: order_id=mock_order_123" in out

    def test_cleanup_skips_close_when_flat(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Flat position -> skip close."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        port = _mock_port(orders=[], positions=FLAT_POS)
        with patch("scripts.exchange_state._build_port", return_value=port):
            cmd_cleanup("BTCUSDT")
        port.close_position.assert_not_called()
        out = capsys.readouterr().out
        assert "close_position: skipped (FLAT)" in out

    def test_cleanup_writes_require_write_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cleanup must call _build_port with write=True."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        port = _mock_port()
        with patch("scripts.exchange_state._build_port", return_value=port) as mock_build:
            cmd_cleanup("BTCUSDT")
        # First call is cleanup (write=True), second is verify (write=False)
        calls = mock_build.call_args_list
        assert calls[0] == (("BTCUSDT",), {"write": True})
        assert calls[1] == (("BTCUSDT",), {"write": False})


# ---------------------------------------------------------------------------
# _build_port credential gate
# ---------------------------------------------------------------------------


class TestBuildPortCredentials:
    """_build_port requires API credentials."""

    def test_missing_api_key_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No BINANCE_API_KEY -> error + exit 1."""
        monkeypatch.delenv("BINANCE_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            _build_port("BTCUSDT")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "BINANCE_API_KEY" in out

    def test_empty_api_key_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty BINANCE_API_KEY -> error + exit 1."""
        monkeypatch.setenv("BINANCE_API_KEY", "  ")
        monkeypatch.setenv("BINANCE_API_SECRET", "secret")
        with pytest.raises(SystemExit) as exc_info:
            _build_port("BTCUSDT")
        assert exc_info.value.code == 1
