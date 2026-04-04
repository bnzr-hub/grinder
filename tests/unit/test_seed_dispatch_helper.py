"""Tests for _dispatch_grid_v2_seed_batch helper."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.live.engine import LiveActionStatus, SeedDispatchResult


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
    engine = LiveEngineV0(paper, port, config)
    return engine


def _seed_place(cid: str, price: str = "0.0413") -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="DRIFTUSDT",
        side=OrderSide.BUY,
        price=Decimal(price),
        quantity=Decimal("124"),
        client_order_id=cid,
        reason="grid_v2_PLACE_ENTRY",
    )


class TestSeedDispatchResult:
    def test_result_fields(self) -> None:
        r = SeedDispatchResult(live_actions=[], executed_count=3, failed_count=1)
        assert r.executed_count == 3
        assert r.failed_count == 1
        assert r.blocked_count == 0


class TestSeedDispatchHelper:
    """_dispatch_grid_v2_seed_batch preserves all bookkeeping."""

    def test_preserves_action_count(self) -> None:
        engine = _make_engine()
        engine._grid_v2_symbol = "DRIFTUSDT"
        seeds = [_seed_place(f"g-s{i}") for i in range(5)]

        with patch.object(engine, "_process_action") as mock_pa:
            mock_pa.return_value = MagicMock(
                status=LiveActionStatus.EXECUTED,
                pre_send=False,
                exchange_code=None,
                block_reason=None,
            )
            result = engine._dispatch_grid_v2_seed_batch(seeds, 1000)

        assert len(result.live_actions) == 5
        assert mock_pa.call_count == 5

    def test_preserves_cids(self) -> None:
        engine = _make_engine()
        engine._grid_v2_symbol = "DRIFTUSDT"
        seeds = [_seed_place("g-s0"), _seed_place("g-s1")]

        with patch.object(engine, "_process_action") as mock_pa:
            mock_pa.return_value = MagicMock(
                status=LiveActionStatus.EXECUTED,
                pre_send=False,
                exchange_code=None,
                block_reason=None,
            )
            engine._dispatch_grid_v2_seed_batch(seeds, 1000)

        # Each seed was dispatched with its own CID
        cids = [c[0][0].client_order_id for c in mock_pa.call_args_list]
        assert cids == ["g-s0", "g-s1"]

    def test_registers_pending_on_success(self) -> None:
        engine = _make_engine()
        engine._grid_v2_symbol = "DRIFTUSDT"
        seeds = [_seed_place("g-s0")]

        with patch.object(engine, "_process_action") as mock_pa:
            mock_pa.return_value = MagicMock(
                status=LiveActionStatus.EXECUTED,
                pre_send=False,
                exchange_code=None,
                block_reason=None,
            )
            with patch.object(engine, "_grid_v2_register_pending_place") as mock_reg:
                result = engine._dispatch_grid_v2_seed_batch(seeds, 1000)

        mock_reg.assert_called_once_with("g-s0")
        assert result.executed_count == 1

    def test_cleans_failed_on_block(self) -> None:
        engine = _make_engine()
        engine._grid_v2_symbol = "DRIFTUSDT"
        seeds = [_seed_place("g-s0")]

        with patch.object(engine, "_process_action") as mock_pa:
            mock_pa.return_value = MagicMock(
                status=LiveActionStatus.BLOCKED,
                pre_send=True,
                exchange_code=None,
                block_reason=MagicMock(value="NOTIONAL_TOO_LOW"),
            )
            with patch.object(engine, "_grid_v2_clean_failed_place") as mock_clean:
                result = engine._dispatch_grid_v2_seed_batch(seeds, 1000)

        mock_clean.assert_called_once_with("g-s0")
        assert result.blocked_count == 1

    def test_clears_seed_cid_on_definitive_failure(self) -> None:
        engine = _make_engine()
        engine._grid_v2_symbol = "DRIFTUSDT"
        engine._grid_v2_pending_seed_cids = frozenset({"g-s0"})
        engine._grid_v2_awaiting_sync = True
        seeds = [_seed_place("g-s0")]

        with patch.object(engine, "_process_action") as mock_pa:
            mock_pa.return_value = MagicMock(
                status=LiveActionStatus.BLOCKED,
                pre_send=True,
                exchange_code=None,
                block_reason=MagicMock(value="NOTIONAL_TOO_LOW"),
            )
            with patch.object(engine, "_grid_v2_clean_failed_place"):
                engine._dispatch_grid_v2_seed_batch(seeds, 1000)

        assert "g-s0" not in engine._grid_v2_pending_seed_cids
        assert engine._grid_v2_awaiting_sync is False

    def test_deterministic_result_order(self) -> None:
        engine = _make_engine()
        engine._grid_v2_symbol = "DRIFTUSDT"
        seeds = [_seed_place(f"g-s{i}") for i in range(3)]

        call_order: list[str] = []

        def track_action(action: ExecutionAction, ts: int) -> MagicMock:
            call_order.append(action.client_order_id or "?")
            return MagicMock(
                status=LiveActionStatus.EXECUTED,
                pre_send=False,
                exchange_code=None,
                block_reason=None,
            )

        with (
            patch.object(engine, "_process_action", side_effect=track_action),
            patch.object(engine, "_grid_v2_register_pending_place"),
        ):
            result = engine._dispatch_grid_v2_seed_batch(seeds, 1000)

        assert call_order == ["g-s0", "g-s1", "g-s2"]
        assert len(result.live_actions) == 3

    def test_empty_batch(self) -> None:
        engine = _make_engine()
        engine._grid_v2_symbol = "DRIFTUSDT"
        result = engine._dispatch_grid_v2_seed_batch([], 1000)
        assert result.live_actions == []
        assert result.executed_count == 0
