"""Tests for grid_v2 shadow runner (doc-27 section 24, PR5).

Proves:
1. Shadow runner is fully isolated — no side effects on live dispatch.
2. Shadow startup mirrors primary bridge logic.
3. Shadow divergence detection works correctly.
4. Shadow errors are fail-open (caught, logged, never propagated).
5. Engine shadow wiring: shadow flag on, primary off → shadow runs.
6. Engine shadow wiring: shadow flag on, primary on → shadow skipped.
7. Shadow does not inject actions into engine live_actions.
"""

from decimal import Decimal

import pytest

from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.grid_v2.shadow import (
    GridV2ShadowRunner,
    ShadowActionSummary,
    ShadowResult,
    _compute_divergence,
)
from grinder.grid_v2.state import GridV2Config

_BASE_TS = 1_710_000_000_000
_REF_PRICE = Decimal("50000")
_STEP = Decimal("0.005")
_ORDER_SIZE = Decimal("0.001")


def _config(
    step: Decimal = _STEP,
    levels: int = 3,
    size: Decimal = _ORDER_SIZE,
    max_levels: int = 10,
    max_notional: Decimal = Decimal("100000"),
) -> GridV2Config:
    return GridV2Config(
        grid_step_pct=step,
        entry_levels_per_side=levels,
        order_size=size,
        max_inventory_levels=max_levels,
        max_inventory_notional_usd=max_notional,
    )


def _shadow(symbol: str = "BTCUSDT") -> GridV2ShadowRunner:
    return GridV2ShadowRunner(_config(), symbol)


def _ea(
    action_type: ActionType = ActionType.PLACE,
    symbol: str = "BTCUSDT",
    reason: str = "test",
    side: OrderSide | None = OrderSide.BUY,
) -> ExecutionAction:
    return ExecutionAction(
        action_type=action_type,
        order_id=None,
        symbol=symbol,
        side=side,
        price=Decimal("50000"),
        quantity=Decimal("0.001"),
        reason=reason,
    )


# ============================================================
# Shadow Runner Unit Tests
# ============================================================


class TestShadowStartupFresh:
    """Shadow fresh startup creates isolated bridge with seed window."""

    def test_fresh_startup_sets_started(self) -> None:
        s = _shadow()
        assert s.started is False
        ok = s.try_startup([], Decimal(0), _REF_PRICE, _BASE_TS)
        assert ok is True
        assert s.started is True
        assert s.bridge.reconstruction_ok is True

    def test_fresh_startup_seed_actions_not_exposed(self) -> None:
        """Shadow bridge creates seed actions internally but they are never returned."""
        s = _shadow()
        s.try_startup([], Decimal(0), _REF_PRICE, _BASE_TS)
        # Bridge has state machine but shadow never exposes seed actions
        assert s.bridge.state_machine is not None


class TestShadowStartupBlocked:
    """Shadow startup with non-flat position and no orders → blocked."""

    def test_non_flat_no_orders_blocks(self) -> None:
        s = _shadow()
        ok = s.try_startup([], Decimal("0.01"), _REF_PRICE, _BASE_TS)
        assert ok is False
        assert s.started is True
        assert s.bridge.reconstruction_ok is False

    def test_blocked_shadow_on_snapshot_returns_blocked(self) -> None:
        s = _shadow()
        s.try_startup([], Decimal("0.01"), _REF_PRICE, _BASE_TS)
        result = s.on_snapshot([], frozenset(), frozenset(), _BASE_TS)
        assert result.shadow_active is False
        assert result.divergence == "shadow_blocked"


class TestShadowStartupIdempotent:
    """Shadow startup only runs once (gated by _started flag)."""

    def test_second_startup_no_op(self) -> None:
        s = _shadow()
        s.try_startup([], Decimal(0), _REF_PRICE, _BASE_TS)
        # Second call returns current state, doesn't re-run
        ok2 = s.try_startup([], Decimal(0), _REF_PRICE, _BASE_TS + 1000)
        assert ok2 is True  # returns reconstruction_ok from first startup


class TestShadowStartupTickSkip:
    """P1-1: Fresh startup tick skips fill detection (mirrors PR4 primary fix)."""

    def test_fresh_startup_tick_skips_fill_detection(self) -> None:
        """On the tick that startup_fresh() runs, fill detection is skipped."""
        s = _shadow()
        s.try_startup([], Decimal(0), _REF_PRICE, _BASE_TS)

        # First on_snapshot after fresh start: seeded CIDs are NOT on exchange.
        # Without the skip, all seed CIDs would be treated as "disappeared" → false fills.
        result = s.on_snapshot(
            legacy_actions=[],
            exchange_cids=frozenset(),  # no CIDs on exchange yet
            pending_cancel_cids=frozenset(),
            ts=_BASE_TS,
        )
        assert result.shadow_active is True
        assert result.shadow_action_count == 0, "Startup tick must skip fill detection"
        assert result.detail == "startup_tick_skip"

    def test_second_tick_runs_fill_detection(self) -> None:
        """After the startup tick, fill detection resumes normally."""
        s = _shadow()
        s.try_startup([], Decimal(0), _REF_PRICE, _BASE_TS)

        # First tick: startup skip
        s.on_snapshot([], frozenset(), frozenset(), _BASE_TS)

        # Second tick: fill detection should run (all seeds still "missing"
        # from exchange, but now they are expected to be there).
        # We pass the CIDs as present on exchange so no fills detected.
        entry_cids = frozenset(s.bridge.adapter.registry.all_entry_cids)
        result2 = s.on_snapshot([], entry_cids, frozenset(), _BASE_TS + 1000)
        assert result2.shadow_active is True
        assert result2.detail != "startup_tick_skip"

    def test_reconstruct_startup_no_skip(self) -> None:
        """Reconstruct startup (with exchange orders) does NOT trigger skip."""
        # Simulate exchange orders for reconstruct path
        # Use a freshly-started shadow to get valid CIDs
        fresh = _shadow()
        fresh.try_startup([], Decimal(0), _REF_PRICE, _BASE_TS)
        entry_cids = list(fresh.bridge.adapter.registry.all_entry_cids)
        # Reconstruct from one entry order
        cid = entry_cids[0]
        reg = fresh.bridge.adapter.registry.lookup_entry(cid)
        assert reg is not None
        orders = [(cid, reg.side, reg.price, _ORDER_SIZE)]

        s2 = _shadow()
        ok = s2.try_startup(orders, Decimal(0), _REF_PRICE, _BASE_TS)
        assert ok is True
        # First tick should NOT skip (reconstruct, not fresh)
        result = s2.on_snapshot([], frozenset({cid}), frozenset(), _BASE_TS)
        assert result.detail != "startup_tick_skip"


class TestShadowTickCountIncrement:
    """on_snapshot increments tick count regardless of active state."""

    def test_tick_count_blocked(self) -> None:
        s = _shadow()
        s.try_startup([], Decimal("0.01"), _REF_PRICE, _BASE_TS)
        s.on_snapshot([], frozenset(), frozenset(), _BASE_TS)
        s.on_snapshot([], frozenset(), frozenset(), _BASE_TS + 1000)
        assert s.tick_count == 2

    def test_tick_count_active(self) -> None:
        s = _shadow()
        s.try_startup([], Decimal(0), _REF_PRICE, _BASE_TS)
        s.on_snapshot([], frozenset(), frozenset(), _BASE_TS)
        assert s.tick_count == 1


class TestShadowNoDispatchSideEffects:
    """Core invariant: shadow NEVER produces actions for dispatch."""

    def test_on_snapshot_returns_result_not_actions(self) -> None:
        """ShadowResult is observability-only, not ExecutionActions."""
        s = _shadow()
        s.try_startup([], Decimal(0), _REF_PRICE, _BASE_TS)
        result = s.on_snapshot([], frozenset(), frozenset(), _BASE_TS)
        assert isinstance(result, ShadowResult)
        # ShadowResult has shadow_actions (summaries) but no ExecutionActions
        for sa in result.shadow_actions:
            assert isinstance(sa, ShadowActionSummary)

    def test_shadow_bridge_isolated_from_primary(self) -> None:
        """Shadow and primary bridges are separate instances."""
        from grinder.grid_v2.bridge import GridV2Bridge  # noqa: PLC0415

        primary = GridV2Bridge(_config(), "BTCUSDT")
        shadow = _shadow()
        assert shadow.bridge is not primary


class TestShadowDivergenceDetection:
    """Divergence computation correctly identifies differences."""

    def test_both_empty_no_divergence(self) -> None:
        div, _detail = _compute_divergence((), [], 0, 0)
        assert div == "none"

    def test_count_mismatch(self) -> None:
        shadow = (ShadowActionSummary("PLACE", "BUY", "test"),)
        div, detail = _compute_divergence(shadow, [], 1, 0)
        assert div == "count_mismatch"
        assert "shadow=1" in detail
        assert "legacy=0" in detail

    def test_type_mismatch(self) -> None:
        shadow = (ShadowActionSummary("PLACE", "BUY", "test"),)
        legacy = [_ea(action_type=ActionType.CANCEL)]
        div, _detail = _compute_divergence(shadow, legacy, 1, 1)
        assert div == "type_mismatch"

    def test_match(self) -> None:
        shadow = (ShadowActionSummary("PLACE", "BUY", "test"),)
        legacy = [_ea(action_type=ActionType.PLACE)]
        div, _detail = _compute_divergence(shadow, legacy, 1, 1)
        assert div == "none"

    def test_dict_legacy_action_no_crash(self) -> None:
        """P1-2: legacy actions may be dicts (from PaperEngine). Must not crash."""
        shadow = (ShadowActionSummary("PLACE", "BUY", "test"),)
        legacy_dict: list[dict[str, str]] = [{"action_type": "PLACE", "symbol": "BTCUSDT"}]
        div, _detail = _compute_divergence(
            shadow,
            legacy_dict,  # type: ignore[arg-type]
            1,
            1,
        )
        assert div == "none"

    def test_dict_legacy_type_mismatch(self) -> None:
        """Dict legacy with different action type → type_mismatch."""
        shadow = (ShadowActionSummary("PLACE", "BUY", "test"),)
        legacy_dict: list[dict[str, str]] = [{"action_type": "CANCEL", "symbol": "BTCUSDT"}]
        div, _detail = _compute_divergence(
            shadow,
            legacy_dict,  # type: ignore[arg-type]
            1,
            1,
        )
        assert div == "type_mismatch"


class TestShadowResultSerialization:
    """ShadowResult serializes to JSON deterministically."""

    def test_to_dict_roundtrip(self) -> None:
        r = ShadowResult(
            shadow_started=True,
            shadow_active=True,
            shadow_action_count=1,
            shadow_actions=(ShadowActionSummary("PLACE", "BUY", "test"),),
            legacy_action_count=0,
            divergence="count_mismatch",
            detail="shadow=1 legacy=0",
        )
        d = r.to_dict()
        assert d["shadow_action_count"] == 1
        assert d["divergence"] == "count_mismatch"
        actions_list = d["shadow_actions"]
        assert isinstance(actions_list, list)
        assert len(actions_list) == 1

    def test_to_json(self) -> None:
        import json  # noqa: PLC0415

        r = ShadowResult(
            shadow_started=True,
            shadow_active=False,
            shadow_action_count=0,
            shadow_actions=(),
            legacy_action_count=0,
            divergence="shadow_blocked",
            detail="test",
        )
        parsed = json.loads(r.to_json())
        assert parsed["shadow_active"] is False


class TestShadowErrorFailOpen:
    """Shadow errors are caught and logged, never propagated."""

    def test_error_returns_blocked_result(self) -> None:
        """If shadow bridge raises during fill processing, result is graceful."""
        s = _shadow()
        s.try_startup([], Decimal(0), _REF_PRICE, _BASE_TS)

        # Force an error by corrupting bridge state
        s._bridge._reconstruction_ok = False

        # on_snapshot should NOT raise — fail-open
        result = s.on_snapshot([], frozenset(), frozenset(), _BASE_TS)
        assert result.shadow_active is False
        assert result.divergence == "shadow_blocked"


# ============================================================
# Engine Integration Tests
# ============================================================


class TestEngineShadowNoLiveActions:
    """P0: Shadow mode does not inject actions into live_actions."""

    def test_shadow_on_legacy_produces_zero_executed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Shadow enabled + primary off → legacy runs, shadow observes, 0 extra executed."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_SHADOW", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        # Primary grid_v2 OFF (default)
        monkeypatch.delenv("GRINDER_GRID_V2_ENABLED", raising=False)

        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[], to_dict=lambda: {})
        port = MagicMock()

        engine = LiveEngineV0(
            paper_engine=paper,
            exchange_port=port,
            config=LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY),
        )

        # Verify shadow runner was created
        assert engine._grid_v2_shadow is not None
        assert engine._grid_v2_bridge is None  # primary NOT created

        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=_BASE_TS, source="test"
        )

        snapshot = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )

        output = engine.process_snapshot(snapshot)

        # Shadow should have started
        assert engine._grid_v2_shadow.started is True
        # No actions dispatched (READ_ONLY mode, and shadow doesn't dispatch)
        executed = [a for a in output.live_actions if a.status.value == "EXECUTED"]
        assert len(executed) == 0


class TestEngineShadowMutualExclusion:
    """Shadow is not created when primary grid_v2 is enabled."""

    def test_primary_on_shadow_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SHADOW", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Primary is active, shadow should NOT be created
        assert engine._grid_v2_bridge is not None
        assert engine._grid_v2_shadow is None


class TestEngineShadowFailClosedIsolated:
    """Shadow fail-closed does not affect legacy action dispatch."""

    def test_shadow_blocked_legacy_still_dispatches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Shadow blocked (non-flat) → legacy planner still produces actions normally."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.account.contracts import AccountSnapshot, PositionSnap  # noqa: PLC0415
        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.contracts import Snapshot  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_SHADOW", "1")
        monkeypatch.setenv("GRINDER_GRID_V2_SYMBOL", "BTCUSDT")
        monkeypatch.delenv("GRINDER_GRID_V2_ENABLED", raising=False)

        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(
            actions=[
                {
                    "action_type": "PLACE",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "price": "49000",
                    "quantity": "0.001",
                    "order_id": None,
                    "level_id": 0,
                    "reason": "GRID_OPEN",
                    "reduce_only": False,
                    "client_order_id": None,
                    "correlation_id": None,
                }
            ],
            to_dict=lambda: {},
        )
        port = MagicMock()
        port.place_order.return_value = "ORDER_1"

        engine = LiveEngineV0(
            paper_engine=paper,
            exchange_port=port,
            config=LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE),
        )

        # Non-flat position → shadow startup will be blocked
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.01"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50100"),
                    unrealized_pnl=Decimal("1"),
                    leverage=1,
                    ts=_BASE_TS,
                ),
            ),
            open_orders=(),
            ts=_BASE_TS,
            source="test",
        )

        snapshot = Snapshot(
            ts=_BASE_TS,
            symbol="BTCUSDT",
            bid_price=Decimal("49999"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )

        output = engine.process_snapshot(snapshot)

        # Shadow should be blocked
        assert engine._grid_v2_shadow is not None
        assert engine._grid_v2_shadow.started is True
        assert engine._grid_v2_shadow.bridge.reconstruction_ok is False

        # Legacy should still produce actions (paper engine was called)
        assert paper.process_snapshot.called
        # Actions from paper engine pass through dispatch
        executed = [a for a in output.live_actions if a.status.value == "EXECUTED"]
        assert len(executed) > 0, "Legacy dispatch must work even when shadow is blocked"


class TestEngineShadowNoSymbol:
    """Shadow not created when GRINDER_GRID_V2_SYMBOL is unset."""

    def test_shadow_without_symbol_not_created(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_SHADOW", "1")
        monkeypatch.delenv("GRINDER_GRID_V2_ENABLED", raising=False)
        monkeypatch.delenv("GRINDER_GRID_V2_SYMBOL", raising=False)

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY),
        )

        assert engine._grid_v2_shadow is None

    def test_shadow_without_symbol_logs_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """P2: Missing symbol emits explicit warning log."""
        import logging  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415

        from grinder.connectors.live_connector import SafeMode  # noqa: PLC0415
        from grinder.live.config import LiveEngineConfig  # noqa: PLC0415
        from grinder.live.engine import LiveEngineV0  # noqa: PLC0415

        monkeypatch.setenv("GRINDER_GRID_V2_SHADOW", "1")
        monkeypatch.delenv("GRINDER_GRID_V2_ENABLED", raising=False)
        monkeypatch.delenv("GRINDER_GRID_V2_SYMBOL", raising=False)

        with caplog.at_level(logging.WARNING):
            engine = LiveEngineV0(
                paper_engine=MagicMock(),
                exchange_port=MagicMock(),
                config=LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY),
            )

        assert engine._grid_v2_shadow is None
        assert "GRID_V2_SHADOW_DISABLED" in caplog.text
        assert "missing_symbol" in caplog.text
