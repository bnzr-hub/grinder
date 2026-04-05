"""Tests for Phase 3 shadow PositionLedger."""

from __future__ import annotations

from decimal import Decimal

from grinder.account.contracts import AccountSnapshot, PositionSnap
from grinder.account.position_ledger import (
    PositionDivergenceKind,
    PositionLedger,
)
from grinder.execution.futures_events import FuturesPositionEvent


def _pos_event(
    symbol: str = "BTCUSDT",
    position_side: str = "BOTH",
    position_amt: str = "0.001",
    entry_price: str = "50000",
    unrealized_pnl: str = "0.5",
    ts: int = 1000,
) -> FuturesPositionEvent:
    return FuturesPositionEvent(
        ts=ts,
        symbol=symbol,
        position_amt=Decimal(position_amt),
        entry_price=Decimal(entry_price),
        unrealized_pnl=Decimal(unrealized_pnl),
        position_side=position_side,
    )


def _snap(
    positions: list[PositionSnap] | None = None,
    ts: int = 1000,
) -> AccountSnapshot:
    return AccountSnapshot(
        positions=tuple(positions or []),
        open_orders=(),
        ts=ts,
        source="test",
    )


def _pos_snap(
    symbol: str = "BTCUSDT",
    side: str = "BOTH",
    signed_qty: str = "0.001",
) -> PositionSnap:
    return PositionSnap(
        symbol=symbol,
        side=side,
        qty=abs(Decimal(signed_qty)),
        entry_price=Decimal("50000"),
        mark_price=Decimal("50100"),
        unrealized_pnl=Decimal("0.5"),
        leverage=20,
        ts=1000,
        signed_qty=Decimal(signed_qty),
    )


class TestPositionLedgerApply:
    def test_apply_creates_entry(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event())
        positions = ledger.positions()
        assert ("BTCUSDT", "BOTH") in positions
        assert positions[("BTCUSDT", "BOTH")].position_amt == Decimal("0.001")

    def test_apply_stale_event_suppressed(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(ts=2000, position_amt="0.002"))
        ledger.apply_position_event(_pos_event(ts=1000, position_amt="0.001"))
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0.002")

    def test_apply_newer_replaces(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(ts=1000, position_amt="0.001"))
        ledger.apply_position_event(_pos_event(ts=2000, position_amt="0.003"))
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0.003")

    def test_flat_position_stored(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0"))
        assert ("BTCUSDT", "BOTH") in ledger.positions()
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0")

    def test_reset_clears_all(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event())
        ledger.reset()
        assert ledger.positions() == {}


class TestPositionLedgerCompare:
    def test_converged_no_divergence(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0.001"))
        snap = _snap(positions=[_pos_snap(signed_qty="0.001")])
        result = ledger.compare_with_snapshot(snap)
        assert result.is_converged

    def test_amt_mismatch(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0.001"))
        snap = _snap(positions=[_pos_snap(signed_qty="0.002")])
        result = ledger.compare_with_snapshot(snap)
        assert not result.is_converged
        assert result.divergences[0].kind == PositionDivergenceKind.POSITION_AMT_MISMATCH

    def test_missing_in_snapshot(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0.001"))
        snap = _snap(positions=[])
        result = ledger.compare_with_snapshot(snap)
        assert not result.is_converged
        assert result.divergences[0].kind == PositionDivergenceKind.POSITION_MISSING_IN_SNAPSHOT

    def test_missing_in_ledger(self) -> None:
        ledger = PositionLedger()
        snap = _snap(positions=[_pos_snap(signed_qty="0.001")])
        result = ledger.compare_with_snapshot(snap)
        assert not result.is_converged
        assert result.divergences[0].kind == PositionDivergenceKind.POSITION_MISSING_IN_LEDGER

    def test_flat_ledger_flat_snapshot_converged(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0"))
        snap = _snap(positions=[])
        result = ledger.compare_with_snapshot(snap)
        assert result.is_converged


class TestFuturesPositionEventPositionSide:
    def test_position_side_parsed_from_binance(self) -> None:
        data = {
            "e": "ACCOUNT_UPDATE",
            "E": 1000,
            "a": {
                "P": [{"s": "BTCUSDT", "pa": "0.001", "ep": "50000", "up": "0.5", "ps": "LONG"}]
            },
        }
        event = FuturesPositionEvent.from_binance(data, "BTCUSDT")
        assert event.position_side == "LONG"

    def test_position_side_default_when_absent(self) -> None:
        data = {
            "e": "ACCOUNT_UPDATE",
            "E": 1000,
            "a": {"P": [{"s": "BTCUSDT", "pa": "0.001", "ep": "50000", "up": "0.5"}]},
        }
        event = FuturesPositionEvent.from_binance(data, "BTCUSDT")
        assert event.position_side == "BOTH"

    def test_to_dict_includes_position_side(self) -> None:
        event = _pos_event(position_side="SHORT")
        d = event.to_dict()
        assert d["position_side"] == "SHORT"

    def test_from_dict_backward_compat(self) -> None:
        d = {
            "ts": 1000,
            "symbol": "BTCUSDT",
            "position_amt": "0.001",
            "entry_price": "50000",
            "unrealized_pnl": "0.5",
        }
        event = FuturesPositionEvent.from_dict(d)
        assert event.position_side == "BOTH"

    def test_round_trip(self) -> None:
        original = _pos_event(position_side="LONG")
        restored = FuturesPositionEvent.from_dict(original.to_dict())
        assert restored.position_side == "LONG"
        assert restored.position_amt == original.position_amt
