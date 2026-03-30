"""Engine scenario tests using the reusable EngineScenario harness.

Scenarios apply reusable invariant checks from tests.helpers.invariants.

Scenarios:
1. Engine startup creates bridge (basic)
2. Env isolation (harness cleanup)
3. Repair price quantization (ADR-114) + tick invariant
4. Failed exit re-registration (ADR-113) + lot/exit invariant
5. Startup seed actions legality (tick + no-duplicate invariants)
6. Cleanup convergence after mock unwind
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.execution.types import ActionType
from grinder.grid_v2.state import InventoryLot, LotSide, LotStatus
from grinder.live.engine import LiveActionStatus
from tests.helpers.engine_scenario import EngineScenario
from tests.helpers.invariants import (
    assert_actions_step_aligned,
    assert_actions_tick_aligned,
    assert_cleanup_converged,
    assert_lot_exit_consistency,
    assert_no_duplicate_slots,
    assert_tick_aligned,
)


class TestStartup:
    """Engine startup creates bridge correctly via harness."""

    def test_engine_starts_grid_v2(self) -> None:
        with EngineScenario() as scenario:
            scenario.tick()
            scenario.assert_bridge_exists()
            scenario.assert_grid_enabled()


class TestEnvIsolation:
    """Harness env cleanup: vars must not leak between scenarios."""

    def test_env_restored_after_scenario(self) -> None:
        before = os.environ.get("GRINDER_GRID_V2_ENABLED")

        with EngineScenario() as scenario:
            assert os.environ["GRINDER_GRID_V2_ENABLED"] == "1"
            scenario.tick()

        after = os.environ.get("GRINDER_GRID_V2_ENABLED")
        assert after == before, f"Env leaked: before={before!r}, after={after!r}"


class TestRepairQuantization:
    """ADR-114: Repair exit price tick-quantized + INV-1 tick invariant."""

    def test_off_tick_price_quantized(self) -> None:
        with EngineScenario(tick_size="0.01") as scenario:
            scenario.tick()

            scenario.mock_exit_repair_bridge(exit_price="50123.456")

            mock_ok = MagicMock()
            mock_ok.status = LiveActionStatus.EXECUTED

            with patch.object(scenario.engine, "_process_action", return_value=mock_ok) as mock_pa:
                scenario.engine._exit_topology_repair_on_sync(scenario.long_position_snapshot())

            place_calls = [
                c for c in mock_pa.call_args_list if c[0][0].action_type == ActionType.PLACE
            ]
            assert len(place_calls) >= 1, "Expected at least one PLACE action"
            price = place_calls[0][0][0].price
            # INV-1: dispatched price must be tick-aligned
            assert_tick_aligned(price, Decimal("0.01"), label="repair-exit")

    def test_repair_actions_all_tick_aligned(self) -> None:
        """All repair PLACE actions pass INV-1 collectively."""
        with EngineScenario(tick_size="0.0001") as scenario:
            scenario.tick()

            scenario.mock_exit_repair_bridge(exit_price="0.05313250")

            mock_ok = MagicMock()
            mock_ok.status = LiveActionStatus.EXECUTED

            with patch.object(scenario.engine, "_process_action", return_value=mock_ok) as mock_pa:
                scenario.engine._exit_topology_repair_on_sync(scenario.long_position_snapshot())

            place_actions = [
                c[0][0] for c in mock_pa.call_args_list if c[0][0].action_type == ActionType.PLACE
            ]
            assert_actions_tick_aligned(place_actions, Decimal("0.0001"))


class TestFailedExitReregistration:
    """ADR-113: Failed exit re-registered + INV-4 lot/exit invariant."""

    def test_deferred_exit_reregistered(self) -> None:
        with EngineScenario(tick_size="0.01") as scenario:
            scenario.tick()

            bridge = scenario.mock_exit_repair_bridge(exit_price="50250", cid_in_registry=None)

            mock_ok = MagicMock()
            mock_ok.status = LiveActionStatus.EXECUTED

            with patch.object(scenario.engine, "_process_action", return_value=mock_ok):
                scenario.engine._exit_topology_repair_on_sync(scenario.long_position_snapshot())

            bridge.adapter.generate_exit_cid.assert_called_once()
            bridge.adapter.registry.register_exit.assert_called_once()

    def test_lot_has_exit_intent_after_repair(self) -> None:
        """INV-4: After repair, the lot must have a matching OPEN exit."""
        with EngineScenario(tick_size="0.01") as scenario:
            scenario.tick()

            bridge = scenario.mock_exit_repair_bridge(exit_price="50250", cid_in_registry=None)

            # Add a real open lot matching the exit's lot_id
            open_lot = InventoryLot(
                lot_id="lot-1",
                side=LotSide.LONG,
                entry_price=Decimal("50000"),
                qty=Decimal("0.001"),
                opened_at_ts=1000,
                source_entry_order_id="entry-1",
                exit_price=Decimal("50250"),
                exit_order_id="exit-1",
                status=LotStatus.OPEN,
            )
            bridge.state_machine.snapshot.open_lots = (open_lot,)

            mock_ok = MagicMock()
            mock_ok.status = LiveActionStatus.EXECUTED

            with patch.object(scenario.engine, "_process_action", return_value=mock_ok):
                scenario.engine._exit_topology_repair_on_sync(scenario.long_position_snapshot())

            # INV-4: open lot must have a matching OPEN exit
            exits = bridge.state_machine.snapshot.exit_orders
            assert_lot_exit_consistency(
                open_lots=(open_lot,),
                exit_orders=tuple(exits),
            )


class TestSeedActionsLegality:
    """Seed actions from startup must satisfy INV-1, INV-2, INV-3."""

    def test_seed_no_duplicate_slots(self) -> None:
        """INV-3: Seed PLACE actions must not have duplicate (side, price) slots."""
        with EngineScenario() as scenario:
            scenario.tick()
            seeds = list(scenario.engine._grid_v2_seed_actions)
            assert_no_duplicate_slots(seeds)

    def test_seed_tick_aligned(self) -> None:
        """INV-1: All seed prices must be tick-aligned."""
        with EngineScenario(tick_size="0.10") as scenario:
            scenario.tick()
            seeds = list(scenario.engine._grid_v2_seed_actions)
            assert_actions_tick_aligned(seeds, Decimal("0.10"))

    def test_seed_step_aligned(self) -> None:
        """INV-2: All seed quantities must be step-aligned."""
        with EngineScenario(order_size="0.001") as scenario:
            scenario.tick()
            seeds = list(scenario.engine._grid_v2_seed_actions)
            assert_actions_step_aligned(seeds, Decimal("0.001"))


class TestCleanupConvergence:
    """INV-7: After full unwind, state must converge to FLAT/0/0."""

    def test_fresh_engine_no_pending_places(self) -> None:
        """Fresh engine has no pending place CIDs (pre-convergence sanity)."""
        with EngineScenario() as scenario:
            scenario.tick()
            entry_count = len(scenario.engine._grid_v2_pending_place_cids)
            assert entry_count == 0, f"Pending place CIDs: {entry_count}"

    def test_mock_cleanup_converges(self) -> None:
        """After mocking a bridge to FLAT with no exits, INV-7 passes."""
        with EngineScenario() as scenario:
            scenario.tick()

            # Mock a bridge in FLAT state with no orders
            bridge = MagicMock()
            bridge.state_machine = MagicMock()
            bridge.state_machine.mode.value = "FLAT"
            bridge.state_machine.snapshot.open_lots = ()
            bridge.state_machine.snapshot.exit_orders = ()
            bridge.adapter.registry.entry_count = 0
            bridge.adapter.registry.exit_count = 0
            scenario.engine._grid_v2_bridge = bridge

            assert_cleanup_converged(
                mode="FLAT",
                open_lot_count=0,
                active_order_count=bridge.adapter.registry.entry_count
                + bridge.adapter.registry.exit_count,
            )

    def test_non_flat_bridge_fails_convergence(self) -> None:
        """INV-7 negative: LONG_BRANCH with lots should fail."""
        with EngineScenario() as scenario:
            scenario.tick()

            bridge = MagicMock()
            bridge.state_machine = MagicMock()
            bridge.state_machine.mode.value = "LONG_BRANCH"
            bridge.state_machine.snapshot.open_lots = (MagicMock(),)
            bridge.adapter.registry.entry_count = 1
            bridge.adapter.registry.exit_count = 1
            scenario.engine._grid_v2_bridge = bridge

            import pytest  # noqa: PLC0415

            with pytest.raises(AssertionError, match="INV-7"):
                assert_cleanup_converged(
                    mode="LONG_BRANCH",
                    open_lot_count=1,
                    active_order_count=2,
                )
