"""Tests for bootstrap tuning of auto-discovered universe."""

from __future__ import annotations

import argparse
from unittest.mock import patch

import scripts.run_autonomous as mod

from grinder.connectors.binance_ws import FakeWsTransport


def _default_args(**overrides: object) -> argparse.Namespace:
    defaults = {
        "symbols": "",  # empty = auto-discovery mode
        "blacklist": "",
        "cycle_interval_s": 1.0,
        "top_k": 3,
        "max_changes_per_cycle": 1,
        "execution_enabled": False,
        "execution_ack": False,
        "max_cycles": None,
        "exchange_port": "noop",
        "mainnet": False,
        "armed": False,
        "max_notional_per_order": "100",
        "max_orders_per_run": 500,
        "_ws_transport": FakeWsTransport(messages=[]),
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestAutoDiscoveryBootstrap:
    """Auto-discovered universe populates TuningCache at startup."""

    def test_auto_mode_calls_bootstrap_with_discovered_symbols(self) -> None:
        """When --symbols is empty, build_runtime bootstraps from discovery."""
        args = _default_args()

        with (
            patch.object(mod, "_bootstrap_tuning_cache", return_value=({}, {})) as mock_bootstrap,
            patch(
                "grinder.orchestration.universe_provider.UniverseProvider.get_candidates",
                return_value=["BTCUSDT", "ETHUSDT", "DRIFTUSDT"],
            ),
        ):
            mod.build_runtime(args)

        mock_bootstrap.assert_called_once()
        bootstrap_symbols = mock_bootstrap.call_args[0][0]
        assert "BTCUSDT" in bootstrap_symbols
        assert "ETHUSDT" in bootstrap_symbols
        assert "DRIFTUSDT" in bootstrap_symbols

    def test_override_symbols_still_use_old_path(self) -> None:
        """When --symbols is set, bootstrap uses override, not discovery."""
        args = _default_args(symbols="DRIFTUSDT")

        with patch.object(mod, "_bootstrap_tuning_cache", return_value=({}, {})) as mock_bootstrap:
            mod.build_runtime(args)

        mock_bootstrap.assert_called_once()
        bootstrap_symbols = mock_bootstrap.call_args[0][0]
        assert bootstrap_symbols == ["DRIFTUSDT"]

    def test_bootstrap_respects_universe_limit(self) -> None:
        """Auto-discovery bootstraps at most 30 symbols."""
        args = _default_args()
        many_symbols = [f"SYM{i}USDT" for i in range(100)]

        with (
            patch.object(mod, "_bootstrap_tuning_cache", return_value=({}, {})) as mock_bootstrap,
            patch(
                "grinder.orchestration.universe_provider.UniverseProvider.get_candidates",
                return_value=many_symbols,
            ),
        ):
            mod.build_runtime(args)

        bootstrap_symbols = mock_bootstrap.call_args[0][0]
        assert len(bootstrap_symbols) <= 30

    def test_discovery_failure_still_starts(self) -> None:
        """If discovery returns empty, bootstrap is skipped but loop starts."""
        args = _default_args()

        with (
            patch.object(mod, "_bootstrap_tuning_cache", return_value=({}, {})) as mock_bootstrap,
            patch(
                "grinder.orchestration.universe_provider.UniverseProvider.get_candidates",
                return_value=[],
            ),
        ):
            runtime = mod.build_runtime(args)

        mock_bootstrap.assert_not_called()
        assert "host" in runtime

    def test_discovery_exception_is_fail_open(self) -> None:
        """If get_candidates() raises, bootstrap is skipped but runtime starts."""
        args = _default_args()

        with (
            patch.object(mod, "_bootstrap_tuning_cache", return_value=({}, {})) as mock_bootstrap,
            patch(
                "grinder.orchestration.universe_provider.UniverseProvider.get_candidates",
                side_effect=RuntimeError("network error"),
            ),
        ):
            runtime = mod.build_runtime(args)

        mock_bootstrap.assert_not_called()
        assert "host" in runtime
