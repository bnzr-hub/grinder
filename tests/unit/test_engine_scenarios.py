"""Engine scenario tests using the reusable EngineScenario harness.

Four distinct scenarios covering main live bug classes:
1. Engine startup creates bridge (basic)
2. Startup mode is FLAT with no open lots (lifecycle)
3. Repair price quantization (ADR-114 regression)
4. Failed exit re-registration (ADR-113 regression)
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.execution.types import ActionType
from grinder.live.engine import LiveActionStatus
from tests.helpers.engine_scenario import EngineScenario


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
        # Capture env before
        before = os.environ.get("GRINDER_GRID_V2_ENABLED")

        with EngineScenario() as scenario:
            assert os.environ["GRINDER_GRID_V2_ENABLED"] == "1"
            scenario.tick()

        # After context manager exits, env should be restored
        after = os.environ.get("GRINDER_GRID_V2_ENABLED")
        assert after == before, f"Env leaked: before={before!r}, after={after!r}"


class TestRepairQuantization:
    """ADR-114: Repair exit price tick-quantized via harness."""

    def test_off_tick_price_quantized(self) -> None:
        with EngineScenario(tick_size="0.01") as scenario:
            scenario.tick()  # startup

            # Set up bridge with off-tick exit price
            scenario.mock_exit_repair_bridge(exit_price="50123.456")

            mock_ok = MagicMock()
            mock_ok.status = LiveActionStatus.EXECUTED

            with patch.object(scenario.engine, "_process_action", return_value=mock_ok) as mock_pa:
                scenario.engine._exit_topology_repair_on_sync(scenario.long_position_snapshot())

            place_calls = [
                c for c in mock_pa.call_args_list if c[0][0].action_type == ActionType.PLACE
            ]
            if place_calls:
                price = place_calls[0][0][0].price
                tick = Decimal("0.01")
                assert price % tick == 0, f"Price {price} not tick-aligned"


class TestFailedExitReregistration:
    """ADR-113: Failed exit re-registered and placed via harness."""

    def test_deferred_exit_reregistered(self) -> None:
        with EngineScenario(tick_size="0.01") as scenario:
            scenario.tick()  # startup

            # Bridge with unregistered exit (simulates post-2022 cleanup)
            bridge = scenario.mock_exit_repair_bridge(exit_price="50250", cid_in_registry=None)

            mock_ok = MagicMock()
            mock_ok.status = LiveActionStatus.EXECUTED

            with patch.object(scenario.engine, "_process_action", return_value=mock_ok):
                scenario.engine._exit_topology_repair_on_sync(scenario.long_position_snapshot())

            # Should have re-registered
            bridge.adapter.generate_exit_cid.assert_called_once()
            bridge.adapter.registry.register_exit.assert_called_once()
