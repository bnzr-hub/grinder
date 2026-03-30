"""Tests for ADR-109 Phase 2 PR-3: degraded-mode recovery boundary.

Covers:
1. EventLedger revoke_trust / restore_trust_after_recovery
2. Engine health mode transition revokes ledger trust
3. Recovery: trust restored only after HEALTHY + converged comparison
4. No silent re-enable without explicit recovery
5. Real _tick_account_sync path with degraded/recovery cycle
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap
from grinder.account.event_ledger import EventLedger
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide, OrderState
from grinder.execution.futures_events import FuturesOrderEvent
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.health_gate import LiveHealthMode


def _snap(
    orders: list[OpenOrderSnap] | None = None,
    ts: int = 1000,
) -> AccountSnapshot:
    return AccountSnapshot(
        positions=(),
        open_orders=tuple(orders or []),
        ts=ts,
        source="test",
    )


def _order(cid: str, symbol: str = "BTCUSDT") -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=cid,
        symbol=symbol,
        side="BUY",
        order_type="LIMIT",
        price=Decimal("50000"),
        qty=Decimal("0.001"),
        filled_qty=Decimal("0"),
        reduce_only=False,
        status="NEW",
        ts=1000,
    )


def _ws_event(
    cid: str,
    status: OrderState = OrderState.OPEN,
    ts: int = 1500,
) -> FuturesOrderEvent:
    return FuturesOrderEvent(
        ts=ts,
        symbol="BTCUSDT",
        order_id=12345,
        client_order_id=cid,
        side=OrderSide.BUY,
        status=status,
        price=Decimal("50000"),
        qty=Decimal("0.001"),
        executed_qty=Decimal("0"),
        avg_price=Decimal("50000"),
    )


def _make_trusted_ledger(cids: list[str]) -> EventLedger:
    ledger = EventLedger()
    orders = [_order(cid) for cid in cids]
    snap = _snap(orders=orders, ts=1000)
    ledger.hydrate_from_snapshot(snap)
    ledger.compare_with_snapshot(snap)
    assert ledger.is_trusted
    return ledger


# ---------------------------------------------------------------------------
# Ledger-unit tests for revoke/restore
# ---------------------------------------------------------------------------


class TestRevokeTrust:
    def test_revoke_disables_trusted(self) -> None:
        ledger = _make_trusted_ledger(["g-e0"])
        assert ledger.is_trusted

        ledger.revoke_trust("test_reason")
        assert not ledger.is_trusted
        assert ledger.trust_revoked

    def test_revoke_idempotent(self) -> None:
        ledger = _make_trusted_ledger(["g-e0"])
        ledger.revoke_trust("first")
        ledger.revoke_trust("second")  # no error
        assert ledger.trust_revoked

    def test_convergence_alone_does_not_restore(self) -> None:
        """After revocation, a converged comparison does NOT restore trust."""
        ledger = _make_trusted_ledger(["g-e0"])
        ledger.revoke_trust("test")

        snap = _snap(orders=[_order("g-e0")], ts=2000)
        ledger.compare_with_snapshot(snap)
        # Comparison converged, but trust_revoked flag still set
        assert not ledger.is_trusted


class TestRestoreTrust:
    def test_restore_succeeds_when_converged(self) -> None:
        ledger = _make_trusted_ledger(["g-e0"])
        ledger.revoke_trust("test")
        assert not ledger.is_trusted

        # Re-compare to set convergence
        snap = _snap(orders=[_order("g-e0")], ts=2000)
        ledger.compare_with_snapshot(snap)

        restored = ledger.restore_trust_after_recovery()
        assert restored
        assert ledger.is_trusted
        assert not ledger.trust_revoked

    def test_restore_fails_when_diverged(self) -> None:
        ledger = _make_trusted_ledger(["g-e0"])
        ledger.revoke_trust("test")

        # Compare with different orders → divergence
        snap = _snap(orders=[_order("g-e99")], ts=2000)
        ledger.compare_with_snapshot(snap)

        restored = ledger.restore_trust_after_recovery()
        assert not restored
        assert not ledger.is_trusted

    def test_restore_fails_when_not_bootstrapped(self) -> None:
        ledger = EventLedger()
        ledger.revoke_trust("test")

        restored = ledger.restore_trust_after_recovery()
        assert not restored

    def test_reset_clears_revocation(self) -> None:
        ledger = _make_trusted_ledger(["g-e0"])
        ledger.revoke_trust("test")
        assert ledger.trust_revoked

        ledger.reset()
        assert not ledger.trust_revoked
        assert not ledger.is_trusted  # also not trusted (not bootstrapped)


# ---------------------------------------------------------------------------
# Engine integration: health mode → ledger trust
# ---------------------------------------------------------------------------


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    return LiveEngineV0(paper, port, LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE))


def _setup_engine_for_sync(engine: LiveEngineV0) -> MagicMock:
    engine._grid_v2_enabled = True
    engine._grid_v2_symbol = "BTCUSDT"
    engine._grid_v2_started = True
    syncer = MagicMock()
    engine._account_syncer = syncer
    engine._risk_base_enabled = False
    engine._sync_reconciler_enabled = False
    engine._symbol_risk_manager = MagicMock()
    engine._symbol_risk_manager.config.enabled = False
    engine._grid_v2_sync_reconstruct_on_position_drift = MagicMock()  # type: ignore[method-assign]
    return syncer


def _sync_result(
    orders: list[OpenOrderSnap] | None = None,
    ts: int = 2000,
) -> MagicMock:
    snap = _snap(orders=orders or [], ts=ts)
    result = MagicMock()
    result.snapshot = snap
    result.mismatches = []
    result.error = None
    return result


class TestEngineHealthDegradedRevokesRealPath:
    """Health mode transition via real _evaluate_and_update_health_mode revokes trust."""

    def test_degraded_ws_revokes_trust(self) -> None:
        from unittest.mock import patch as _patch  # noqa: PLC0415

        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.health_gate import LiveHealthResult  # noqa: PLC0415

        engine = _make_engine()
        engine._event_ledger = _make_trusted_ledger(["g-e0"])
        assert engine._event_ledger.is_trusted

        # Engine starts HEALTHY
        engine._health_mode_prev = LiveHealthMode.HEALTHY

        # Mock evaluate_health to return DEGRADED_WS
        degraded_result = LiveHealthResult(
            mode=LiveHealthMode.DEGRADED_WS,
            reason="ws_disconnected",
            write_allowed=True,
            cancel_allowed=True,
            reduce_only_allowed=True,
        )
        dummy_snap = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )

        with _patch("grinder.live.health_gate.evaluate_health", return_value=degraded_result):
            engine._evaluate_and_update_health_mode(dummy_snap)

        assert engine._health_mode == LiveHealthMode.DEGRADED_WS
        assert not engine._event_ledger.is_trusted
        assert engine._event_ledger.trust_revoked

    def test_stale_truth_revokes_trust(self) -> None:
        from unittest.mock import patch as _patch  # noqa: PLC0415

        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.health_gate import LiveHealthResult  # noqa: PLC0415

        engine = _make_engine()
        engine._event_ledger = _make_trusted_ledger(["g-e0"])
        engine._health_mode_prev = LiveHealthMode.HEALTHY

        stale_result = LiveHealthResult(
            mode=LiveHealthMode.STALE_TRUTH,
            reason="sync_hard_stale",
            write_allowed=False,
            cancel_allowed=True,
            reduce_only_allowed=True,
        )
        dummy_snap = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )

        with _patch("grinder.live.health_gate.evaluate_health", return_value=stale_result):
            engine._evaluate_and_update_health_mode(dummy_snap)

        assert not engine._event_ledger.is_trusted
        assert engine._event_ledger.trust_revoked

    def test_healthy_mode_does_not_revoke(self) -> None:
        from unittest.mock import patch as _patch  # noqa: PLC0415

        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.health_gate import LiveHealthResult  # noqa: PLC0415

        engine = _make_engine()
        engine._event_ledger = _make_trusted_ledger(["g-e0"])
        engine._health_mode_prev = LiveHealthMode.HEALTHY

        healthy_result = LiveHealthResult(
            mode=LiveHealthMode.HEALTHY,
            reason="all_fresh",
            write_allowed=True,
            cancel_allowed=True,
            reduce_only_allowed=True,
        )
        dummy_snap = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )

        with _patch("grinder.live.health_gate.evaluate_health", return_value=healthy_result):
            engine._evaluate_and_update_health_mode(dummy_snap)

        assert engine._event_ledger.is_trusted
        assert not engine._event_ledger.trust_revoked


class TestEngineRecoveryViaSync:
    """Recovery: trust restored after HEALTHY + converged sync."""

    def test_full_degraded_recovery_cycle(self) -> None:
        """Healthy → degraded (revoke) → healthy + converged sync (restore)."""
        engine = _make_engine()
        syncer = _setup_engine_for_sync(engine)

        # Step 1: Build trusted ledger via WS + sync
        engine._event_ledger.apply_order_event(_ws_event("g-e0", ts=1000))
        syncer.sync.return_value = _sync_result(orders=[_order("g-e0")], ts=2000)
        engine._tick_account_sync()
        assert engine._event_ledger.is_trusted

        # Step 2: Health degrades → trust revoked
        engine._health_mode = LiveHealthMode.DEGRADED_WS
        engine._event_ledger.revoke_trust("health_mode=DEGRADED_WS")
        assert not engine._event_ledger.is_trusted

        # Step 3: Health returns to HEALTHY
        engine._health_mode = LiveHealthMode.HEALTHY

        # Step 4: Next sync with convergence → trust restored
        syncer.sync.return_value = _sync_result(orders=[_order("g-e0")], ts=3000)
        engine._tick_account_sync()
        assert engine._event_ledger.is_trusted

    def test_no_silent_reenable_without_healthy_mode(self) -> None:
        """Trust not restored if health is still degraded even with convergence."""
        engine = _make_engine()
        syncer = _setup_engine_for_sync(engine)

        engine._event_ledger.apply_order_event(_ws_event("g-e0", ts=1000))
        syncer.sync.return_value = _sync_result(orders=[_order("g-e0")], ts=2000)
        engine._tick_account_sync()
        assert engine._event_ledger.is_trusted

        # Degrade and revoke
        engine._health_mode = LiveHealthMode.STALE_TRUTH
        engine._event_ledger.revoke_trust("health_mode=STALE_TRUTH")

        # Sync succeeds with convergence, but health is NOT HEALTHY
        syncer.sync.return_value = _sync_result(orders=[_order("g-e0")], ts=3000)
        engine._tick_account_sync()

        # Trust should NOT be restored — health is still STALE_TRUTH
        assert not engine._event_ledger.is_trusted

    def test_no_silent_reenable_without_convergence(self) -> None:
        """Trust not restored if health is HEALTHY but comparison diverges."""
        engine = _make_engine()
        syncer = _setup_engine_for_sync(engine)

        engine._event_ledger.apply_order_event(_ws_event("g-e0", ts=1000))
        syncer.sync.return_value = _sync_result(orders=[_order("g-e0")], ts=2000)
        engine._tick_account_sync()

        # Degrade and revoke
        engine._health_mode = LiveHealthMode.DEGRADED_WS
        engine._event_ledger.revoke_trust("health_mode=DEGRADED_WS")

        # Health recovers
        engine._health_mode = LiveHealthMode.HEALTHY

        # Sync with DIFFERENT orders → divergence
        syncer.sync.return_value = _sync_result(orders=[_order("g-e99")], ts=3000)
        engine._tick_account_sync()

        # Trust NOT restored because divergence
        assert not engine._event_ledger.is_trusted
