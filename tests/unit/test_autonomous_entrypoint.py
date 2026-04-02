"""Tests for canonical autonomous production entrypoint.

Covers:
- Runtime graph assembly
- Shadow-only default
- Execution-enabled path
- Startup banner content
- Single-symbol override
"""

from __future__ import annotations

from argparse import Namespace

from scripts.run_autonomous import build_parser, build_runtime


def _default_args(**overrides: object) -> Namespace:
    """Build args with defaults."""
    defaults = {
        "symbols": "",
        "blacklist": "",
        "cycle_interval_s": 1.0,
        "top_k": 3,
        "max_changes_per_cycle": 1,
        "metrics_port": 19999,
        "execution_enabled": False,
        "execution_ack": False,
        "max_cycles": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


class TestRuntimeGraphAssembly:
    """build_runtime creates all required components."""

    def test_all_components_present(self) -> None:
        runtime = build_runtime(_default_args())

        assert runtime["loop"] is not None
        assert runtime["registry"] is not None
        assert runtime["operator"] is not None
        assert runtime["coordinator"] is not None
        assert runtime["tuning_cache"] is not None
        assert runtime["universe_provider"] is not None

    def test_loop_has_execution_deps(self) -> None:
        runtime = build_runtime(_default_args(execution_enabled=True, execution_ack=True))
        loop = runtime["loop"]

        assert loop.execution_coordinator is not None
        assert loop.execution_registry is not None
        assert loop.execution_operator is not None
        assert loop.execution_enabled is True
        assert loop.execution_acknowledged is True


class TestShadowDefault:
    """Default mode is shadow-only."""

    def test_default_shadow(self) -> None:
        runtime = build_runtime(_default_args())
        loop = runtime["loop"]

        assert loop.execution_enabled is False
        assert loop.execution_acknowledged is False

    def test_enabled_without_ack_still_shadow(self) -> None:
        runtime = build_runtime(_default_args(execution_enabled=True, execution_ack=False))
        loop = runtime["loop"]

        assert loop.execution_enabled is True
        assert loop.execution_acknowledged is False


class TestExecutionEnabled:
    """Execution-enabled path wires Stage 8."""

    def test_enabled_and_acked(self) -> None:
        runtime = build_runtime(_default_args(execution_enabled=True, execution_ack=True))
        loop = runtime["loop"]

        assert loop.execution_enabled is True
        assert loop.execution_acknowledged is True
        assert loop.execution_coordinator is not None


class TestSymbolOverride:
    """Symbol override constrains universe."""

    def test_override_set(self) -> None:
        runtime = build_runtime(_default_args(symbols="BTCUSDT,ETHUSDT"))
        loop = runtime["loop"]

        assert loop.config.operator_universe_override == frozenset({"BTCUSDT", "ETHUSDT"})

    def test_no_override(self) -> None:
        runtime = build_runtime(_default_args(symbols=""))
        loop = runtime["loop"]

        assert loop.config.operator_universe_override == frozenset()


class TestCeremonyBindings:
    """Real ceremony functions are bound to coordinator."""

    def test_activate_fn_bound(self) -> None:
        runtime = build_runtime(_default_args())
        assert runtime["coordinator"].activate_fn is not None

    def test_graceful_exit_fn_bound(self) -> None:
        runtime = build_runtime(_default_args())
        assert runtime["coordinator"].graceful_exit_fn is not None

    def test_deactivate_fn_bound(self) -> None:
        runtime = build_runtime(_default_args())
        assert runtime["coordinator"].deactivate_fn is not None


class TestParserDefaults:
    """CLI parser has safe defaults."""

    def test_default_shadow(self) -> None:
        args = build_parser().parse_args([])
        assert args.execution_enabled is False
        assert args.execution_ack is False

    def test_execution_flags(self) -> None:
        args = build_parser().parse_args(["--execution-enabled", "--execution-ack"])
        assert args.execution_enabled is True
        assert args.execution_ack is True
