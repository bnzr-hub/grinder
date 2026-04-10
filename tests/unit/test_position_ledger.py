"""Tests for Phase 3 shadow PositionLedger."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

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
            "a": {"P": [{"s": "BTCUSDT", "pa": "0.001", "ep": "50000", "up": "0.5", "ps": "LONG"}]},
        }
        event = FuturesPositionEvent.from_binance(data, "BTCUSDT")
        assert event is not None
        assert event.position_side == "LONG"

    def test_position_side_default_when_absent(self) -> None:
        data = {
            "e": "ACCOUNT_UPDATE",
            "E": 1000,
            "a": {"P": [{"s": "BTCUSDT", "pa": "0.001", "ep": "50000", "up": "0.5"}]},
        }
        event = FuturesPositionEvent.from_binance(data, "BTCUSDT")
        assert event is not None
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


class TestPositionLedgerTrustState:
    def test_trust_false_before_bootstrap(self) -> None:
        ledger = PositionLedger()
        assert not ledger.is_trusted

    def test_trust_false_after_flat_event_only(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0"))
        assert not ledger.is_trusted

    def test_bootstrap_set_on_nonzero_event(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0.001"))
        assert ledger._bootstrapped

    def test_trust_false_until_convergence(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0.001"))
        assert not ledger.is_trusted  # bootstrapped but not converged

    def test_trust_true_after_convergence(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0.001"))
        ledger.record_comparison_result(converged=True)
        assert ledger.is_trusted

    def test_trust_revoked_on_divergence(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0.001"))
        ledger.record_comparison_result(converged=True)
        assert ledger.is_trusted
        ledger.record_comparison_result(converged=False)
        assert not ledger.is_trusted
        assert ledger.trust_revoked

    def test_trust_restored_after_convergence(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0.001"))
        ledger.record_comparison_result(converged=True)
        ledger.record_comparison_result(converged=False)
        assert ledger.trust_revoked
        ledger.record_comparison_result(converged=True)
        assert ledger.is_trusted
        assert not ledger.trust_revoked

    def test_reset_clears_trust(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0.001"))
        ledger.record_comparison_result(converged=True)
        assert ledger.is_trusted
        ledger.reset()
        assert not ledger.is_trusted
        assert not ledger._bootstrapped

    def test_revoke_idempotent(self) -> None:
        ledger = PositionLedger()
        ledger.revoke_trust("test1")
        ledger.revoke_trust("test2")  # should not re-log
        assert ledger.trust_revoked


class TestPositionLedgerHydration:
    """hydrate_from_snapshot bootstraps ledger from REST snapshot."""

    def test_hydrate_populates_empty_ledger(self) -> None:
        ledger = PositionLedger()
        snap = _snap(positions=[_pos_snap(signed_qty="0.001")], ts=2000)
        hydrated = ledger.hydrate_from_snapshot(snap)
        assert hydrated == 1
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0.001")
        assert ledger._bootstrapped

    def test_hydrate_skips_flat_positions(self) -> None:
        ledger = PositionLedger()
        snap = _snap(positions=[_pos_snap(signed_qty="0")], ts=2000)
        hydrated = ledger.hydrate_from_snapshot(snap)
        assert hydrated == 0
        assert not ledger._bootstrapped

    def test_hydrate_does_not_overwrite_newer_event(self) -> None:
        """WS event with newer ts takes precedence over older pos.ts."""
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(ts=3000, position_amt="0.005"))
        # pos.ts=1000 (from _pos_snap default), WS event ts=3000 is newer
        snap = _snap(positions=[_pos_snap(signed_qty="0.001")], ts=5000)
        hydrated = ledger.hydrate_from_snapshot(snap)
        assert hydrated == 0
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0.005")

    def test_hydrate_overwrites_older_event(self) -> None:
        """Position with newer pos.ts updates stale ledger entry."""
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(ts=500, position_amt="0.001"))
        # pos.ts=1000 > event.ts=500 → overwrite
        snap = _snap(positions=[_pos_snap(signed_qty="0.003")], ts=2000)
        hydrated = ledger.hydrate_from_snapshot(snap)
        assert hydrated == 1
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0.003")

    def test_hydrate_idempotent(self) -> None:
        """Repeated hydration with same snapshot does not double-count."""
        ledger = PositionLedger()
        snap = _snap(positions=[_pos_snap(signed_qty="0.001")], ts=2000)
        assert ledger.hydrate_from_snapshot(snap) == 1
        assert ledger.hydrate_from_snapshot(snap) == 0  # same pos.ts, no overwrite

    def test_hydrate_uses_pos_ts_not_snapshot_ts(self) -> None:
        """Freshness must use pos.ts, not snapshot.ts (which includes order ts).

        Scenario: pos.ts=1000, snapshot.ts=2000, WS event at ts=1500.
        The WS event is newer than the position snapshot and must not be suppressed.
        """
        ledger = PositionLedger()
        # Hydrate: pos.ts=1000, snapshot.ts=2000
        snap = _snap(positions=[_pos_snap(signed_qty="0.001")], ts=2000)
        ledger.hydrate_from_snapshot(snap)
        # Ledger entry should have last_event_ts=1000 (pos.ts), not 2000
        assert ledger.positions()[("BTCUSDT", "BOTH")].last_event_ts == 1000

        # WS event at ts=1500 is newer than pos.ts=1000 → must apply
        ledger.apply_position_event(_pos_event(ts=1500, position_amt="0.002"))
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0.002")

    def test_hydrate_then_compare_converges(self) -> None:
        """The core fix: hydrate from snapshot, then compare → converged."""
        ledger = PositionLedger()
        snap = _snap(positions=[_pos_snap(signed_qty="0.001")], ts=2000)
        ledger.hydrate_from_snapshot(snap)
        result = ledger.compare_with_snapshot(snap)
        assert result.is_converged

    def test_hydrate_prevents_persistent_missing_in_ledger(self) -> None:
        """Without hydration, empty ledger diverges forever. With it, first sync converges."""
        ledger = PositionLedger()
        snap = _snap(positions=[_pos_snap(signed_qty="0.001")], ts=2000)

        # Simulate the fixed sync path: hydrate then compare
        ledger.hydrate_from_snapshot(snap)
        cmp = ledger.compare_with_snapshot(snap)
        ledger.record_comparison_result(cmp.is_converged)

        assert cmp.is_converged
        assert ledger._bootstrapped
        assert ledger.is_trusted

    def test_hydrate_multi_symbol_no_filter(self) -> None:
        """Multiple positions hydrated correctly when no symbol_filter is passed.

        Backward compatibility: `symbol_filter=None` (default) preserves the
        original unfiltered behavior byte-for-byte.
        """
        ledger = PositionLedger()
        snap = _snap(
            positions=[
                _pos_snap(symbol="BTCUSDT", signed_qty="0.001"),
                _pos_snap(symbol="ETHUSDT", signed_qty="-0.5"),
            ],
            ts=2000,
        )
        hydrated = ledger.hydrate_from_snapshot(snap)
        assert hydrated == 2
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0.001")
        assert ledger.positions()[("ETHUSDT", "BOTH")].position_amt == Decimal("-0.5")


class TestPositionLedgerGetSignedQty:
    def test_returns_zero_when_missing(self) -> None:
        ledger = PositionLedger()
        assert ledger.get_signed_qty("UNKNOWN") == Decimal("0")

    def test_returns_ledger_amt(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="0.005"))
        assert ledger.get_signed_qty("BTCUSDT") == Decimal("0.005")

    def test_negative_short(self) -> None:
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(position_amt="-0.003"))
        assert ledger.get_signed_qty("BTCUSDT") == Decimal("-0.003")


class TestPositionLedgerLogs:
    """Issue #664: structured INFO log coverage for apply/drop/hydrate decisions.

    Observability-only. No behavior change. See docs/13_OBSERVABILITY.md.
    """

    _LEDGER_LOGGER = "grinder.account.position_ledger"
    _APPLIED = "POSITION_LEDGER_EVENT_APPLIED"
    _DROPPED = "POSITION_LEDGER_EVENT_DROPPED_STALE"
    _HYDRATE = "POSITION_LEDGER_HYDRATE_APPLIED"

    def test_applied_log_first_write(self, caplog: pytest.LogCaptureFixture) -> None:
        ledger = PositionLedger()
        with caplog.at_level(logging.INFO, logger=self._LEDGER_LOGGER):
            ledger.apply_position_event(_pos_event(ts=1000, position_amt="0.001"))

        applied = [r for r in caplog.records if self._APPLIED in r.getMessage()]
        dropped = [r for r in caplog.records if self._DROPPED in r.getMessage()]
        assert len(applied) == 1
        assert len(dropped) == 0
        msg = applied[0].getMessage()
        assert "symbol=BTCUSDT" in msg
        assert "side=BOTH" in msg
        assert "incoming_amt=0.001" in msg
        assert "incoming_ts=1000" in msg
        assert "prev_amt=None" in msg
        assert "prev_ts=None" in msg
        assert "new_amt=0.001" in msg
        assert "new_ts=1000" in msg
        assert "source=user_data_position" in msg

    def test_applied_log_overwrites_prev(self, caplog: pytest.LogCaptureFixture) -> None:
        ledger = PositionLedger()
        with caplog.at_level(logging.INFO, logger=self._LEDGER_LOGGER):
            ledger.apply_position_event(_pos_event(ts=1000, position_amt="0.001"))
            ledger.apply_position_event(_pos_event(ts=2000, position_amt="0.003"))

        applied = [r for r in caplog.records if self._APPLIED in r.getMessage()]
        dropped = [r for r in caplog.records if self._DROPPED in r.getMessage()]
        assert len(applied) == 2
        assert len(dropped) == 0
        second = applied[1].getMessage()
        assert "incoming_amt=0.003" in second
        assert "incoming_ts=2000" in second
        assert "prev_amt=0.001" in second
        assert "prev_ts=1000" in second
        assert "new_amt=0.003" in second
        assert "new_ts=2000" in second

    def test_dropped_stale_strict(self, caplog: pytest.LogCaptureFixture) -> None:
        ledger = PositionLedger()
        with caplog.at_level(logging.INFO, logger=self._LEDGER_LOGGER):
            ledger.apply_position_event(_pos_event(ts=2000, position_amt="0.002"))
            ledger.apply_position_event(_pos_event(ts=1000, position_amt="0.001"))

        applied = [r for r in caplog.records if self._APPLIED in r.getMessage()]
        dropped = [r for r in caplog.records if self._DROPPED in r.getMessage()]
        assert len(applied) == 1
        assert len(dropped) == 1
        msg = dropped[0].getMessage()
        assert "incoming_amt=0.001" in msg
        assert "incoming_ts=1000" in msg
        assert "prev_amt=0.002" in msg
        assert "prev_ts=2000" in msg
        assert "reason=stale_event_guard" in msg
        assert "source=user_data_position" in msg
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0.002")

    def test_dropped_same_ms(self, caplog: pytest.LogCaptureFixture) -> None:
        """Finding B diagnostic: same-ms event is rejected, log captures it explicitly."""
        ledger = PositionLedger()
        with caplog.at_level(logging.INFO, logger=self._LEDGER_LOGGER):
            ledger.apply_position_event(_pos_event(ts=1000, position_amt="0.002"))
            ledger.apply_position_event(_pos_event(ts=1000, position_amt="0.005"))

        applied = [r for r in caplog.records if self._APPLIED in r.getMessage()]
        dropped = [r for r in caplog.records if self._DROPPED in r.getMessage()]
        assert len(applied) == 1
        assert len(dropped) == 1
        msg = dropped[0].getMessage()
        assert "incoming_ts=1000" in msg
        assert "prev_ts=1000" in msg
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0.002")

    def test_hydrate_applied_log_write_only(self, caplog: pytest.LogCaptureFixture) -> None:
        """Hydrate emits log only on the write branch, not on skip."""
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(ts=500, position_amt="0.001"))
        # pos.ts=1000 > event.ts=500 → write branch
        snap = _snap(positions=[_pos_snap(signed_qty="0.003")], ts=2000)

        with caplog.at_level(logging.INFO, logger=self._LEDGER_LOGGER):
            ledger.hydrate_from_snapshot(snap)
            # Second call: ledger's last_event_ts == pos.ts == 1000 → skip branch
            ledger.hydrate_from_snapshot(snap)

        hydrate_logs = [r for r in caplog.records if self._HYDRATE in r.getMessage()]
        assert len(hydrate_logs) == 1
        msg = hydrate_logs[0].getMessage()
        assert "symbol=BTCUSDT" in msg
        assert "side=BOTH" in msg
        assert "amt=0.003" in msg
        assert "ts=1000" in msg
        assert "source=snapshot_hydration" in msg


class TestPositionLedgerSymbolFilter:
    """Issue #664: symbol-scoped hydrate/compare prevent cross-engine pollution.

    Each per-symbol engine has its own PositionLedger instance but the REST
    account snapshot is unfiltered and contains all symbols. Without filter,
    every engine hydrates every position, then stale cross-symbol copies
    generate false divergence logs when the real owner symbol transitions
    through FLAT. Fix: pass engine's own symbol as filter on both hydrate and
    compare so each engine-local ledger stays scoped to its owner symbol.
    """

    def test_hydrate_skips_foreign_symbols(self) -> None:
        """Multi-symbol snapshot + symbol_filter = only owner symbol written."""
        ledger = PositionLedger()
        snap = _snap(
            positions=[
                _pos_snap(symbol="BTCUSDT", signed_qty="0.001"),
                _pos_snap(symbol="ETHUSDT", signed_qty="-0.5"),
                _pos_snap(symbol="MAGMAUSDT", signed_qty="100"),
            ],
            ts=2000,
        )
        hydrated = ledger.hydrate_from_snapshot(snap, symbol_filter="BTCUSDT")
        assert hydrated == 1
        positions = ledger.positions()
        assert ("BTCUSDT", "BOTH") in positions
        assert ("ETHUSDT", "BOTH") not in positions
        assert ("MAGMAUSDT", "BOTH") not in positions

    def test_hydrate_owner_symbol_unaffected(self) -> None:
        """Owner symbol hydration behavior is identical to no-filter case."""
        ledger = PositionLedger()
        snap = _snap(
            positions=[
                _pos_snap(symbol="BTCUSDT", signed_qty="0.005"),
                _pos_snap(symbol="ETHUSDT", signed_qty="-1.0"),
            ],
            ts=2000,
        )
        hydrated = ledger.hydrate_from_snapshot(snap, symbol_filter="BTCUSDT")
        assert hydrated == 1
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0.005")
        assert ledger._bootstrapped

    def test_compare_no_false_divergence_foreign_symbols(self) -> None:
        """The canary reproduction scenario.

        ARIA-engine's ledger was hydrated filtered; subsequent compare against
        the same multi-symbol snapshot must NOT flag foreign symbols as
        POSITION_MISSING_IN_LEDGER and must NOT see any foreign stale state.
        """
        ledger = PositionLedger()
        snap = _snap(
            positions=[
                _pos_snap(symbol="BTCUSDT", signed_qty="0.001"),
                _pos_snap(symbol="ETHUSDT", signed_qty="-0.5"),
                _pos_snap(symbol="MAGMAUSDT", signed_qty="100"),
            ],
            ts=2000,
        )
        ledger.hydrate_from_snapshot(snap, symbol_filter="BTCUSDT")
        result = ledger.compare_with_snapshot(snap, symbol_filter="BTCUSDT")
        assert result.is_converged
        assert len(result.divergences) == 0
        assert result.ledger_count == 1
        assert result.snapshot_count == 1

    def test_compare_owner_symbol_amt_mismatch_still_detected(self) -> None:
        """Regression guard: filtered compare still catches real owner-side divergence."""
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(symbol="BTCUSDT", position_amt="0.005"))
        snap = _snap(
            positions=[
                _pos_snap(symbol="BTCUSDT", signed_qty="0.003"),
                _pos_snap(symbol="ETHUSDT", signed_qty="-1.0"),
            ],
            ts=2000,
        )
        result = ledger.compare_with_snapshot(snap, symbol_filter="BTCUSDT")
        assert not result.is_converged
        assert len(result.divergences) == 1
        assert result.divergences[0].symbol == "BTCUSDT"
        assert result.divergences[0].kind == PositionDivergenceKind.POSITION_AMT_MISMATCH

    def test_ws_apply_unaffected_by_hydrate_filter(self) -> None:
        """REQ-005: WS apply path still works after filtered hydration."""
        ledger = PositionLedger()
        ledger.apply_position_event(_pos_event(symbol="BTCUSDT", position_amt="0.003"))
        foreign_snap = _snap(
            positions=[
                _pos_snap(symbol="BTCUSDT", signed_qty="0.003"),
                _pos_snap(symbol="ETHUSDT", signed_qty="-0.5"),
            ],
            ts=2000,
        )
        ledger.hydrate_from_snapshot(foreign_snap, symbol_filter="BTCUSDT")
        assert ledger.get_signed_qty("BTCUSDT") == Decimal("0.003")
        assert ("ETHUSDT", "BOTH") not in ledger.positions()

    def test_hydrate_multi_symbol_with_filter(self) -> None:
        """Paired with test_hydrate_multi_symbol_no_filter: same snapshot,
        with filter only owner is written, proving backward compat boundary."""
        ledger = PositionLedger()
        snap = _snap(
            positions=[
                _pos_snap(symbol="BTCUSDT", signed_qty="0.001"),
                _pos_snap(symbol="ETHUSDT", signed_qty="-0.5"),
            ],
            ts=2000,
        )
        hydrated = ledger.hydrate_from_snapshot(snap, symbol_filter="BTCUSDT")
        assert hydrated == 1
        assert ledger.positions()[("BTCUSDT", "BOTH")].position_amt == Decimal("0.001")
        assert ("ETHUSDT", "BOTH") not in ledger.positions()
