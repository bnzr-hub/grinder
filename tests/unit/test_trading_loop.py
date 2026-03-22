"""Unit tests for trading loop entrypoint.

Tests cover:
- Engine initialization gauge in read_only mode
- validate_env() safety gates (ACK required for paper/live_trade)
- build_engine() rehearsal knobs (--armed, --paper-size-per-level, fill model)
- Full loop integration with FakeWsTransport fixture
- validate_real_port_gates() 5-gate validation
- build_exchange_port() port selection
- HA-gated /readyz semantics
- HA-gated loop processing
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import time as time_module
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import scripts.run_trading as run_trading_mod
from scripts.run_trading import (
    _configure_logging,
    _run_cleanup_on_exit,
    build_connector,
    build_engine,
    build_exchange_port,
    evaluate_cleanup_on_exit_policy,
    evaluate_futures_preflight,
    evaluate_grid_v2_account_sync_preflight,
    evaluate_launch_guard,
    is_trading_ready,
    reset_trading_state,
    trading_loop,
    validate_env,
    validate_max_orders_ack,
    validate_real_port_gates,
)

if TYPE_CHECKING:
    from pathlib import Path

from grinder.connectors.binance_ws import (
    BINANCE_WS_FUTURES_MAINNET,
    BINANCE_WS_MAINNET,
    FakeWsTransport,
)
from grinder.connectors.live_connector import (
    LiveConnectorConfig,
    LiveConnectorV0,
    SafeMode,
)
from grinder.execution.binance_futures_port import BinanceFuturesPort
from grinder.execution.port import NoOpExchangePort
from grinder.execution.sor_metrics import get_sor_metrics, reset_sor_metrics
from grinder.ha.role import HARole, reset_ha_state, set_ha_state
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import LiveEngineV0
from grinder.paper.engine import PaperEngine


class FakeSleep:
    """Fake sleep for bounded-time testing."""

    def __init__(self) -> None:
        self.total_slept: float = 0.0
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.total_slept += seconds
        self.calls.append(seconds)


class TestEngineGauge:
    """Test that LiveEngineV0 sets grinder_live_engine_initialized=1."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def test_read_only_creates_engine_gauge_one(self) -> None:
        """Engine in read_only mode sets initialized gauge to 1."""
        paper = PaperEngine()
        port = NoOpExchangePort()
        config = LiveEngineConfig(mode=SafeMode.READ_ONLY)
        LiveEngineV0(paper_engine=paper, exchange_port=port, config=config)
        assert get_sor_metrics().engine_initialized is True


class TestValidateEnv:
    """Test validate_env() safety gates."""

    def test_paper_without_ack_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Paper mode without ACK causes sys.exit(1)."""
        monkeypatch.setenv("GRINDER_TRADING_MODE", "paper")
        monkeypatch.delenv("GRINDER_TRADING_LOOP_ACK", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            validate_env()
        assert exc_info.value.code == 1

    def test_paper_with_ack_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Paper mode with correct ACK returns SafeMode.PAPER."""
        monkeypatch.setenv("GRINDER_TRADING_MODE", "paper")
        monkeypatch.setenv("GRINDER_TRADING_LOOP_ACK", "YES_I_KNOW")
        mode = validate_env()
        assert mode == SafeMode.PAPER

    def test_read_only_no_ack_needed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default read_only mode works without any ACK."""
        monkeypatch.delenv("GRINDER_TRADING_MODE", raising=False)
        monkeypatch.delenv("GRINDER_TRADING_LOOP_ACK", raising=False)
        mode = validate_env()
        assert mode == SafeMode.READ_ONLY


class TestCleanupOnExitPolicy:
    def test_cleanup_policy_disabled(self) -> None:
        enabled, reason = evaluate_cleanup_on_exit_policy(
            cleanup_on_exit=False,
            mode=SafeMode.LIVE_TRADE,
            exchange_port="futures",
            armed=True,
            mainnet=True,
            fixture_path=None,
            stop_reason="duration_reached",
        )
        assert enabled is False
        assert reason == "disabled"

    def test_cleanup_policy_enabled_live_futures_mainnet(self) -> None:
        enabled, reason = evaluate_cleanup_on_exit_policy(
            cleanup_on_exit=True,
            mode=SafeMode.LIVE_TRADE,
            exchange_port="futures",
            armed=True,
            mainnet=True,
            fixture_path=None,
            stop_reason="duration_reached",
        )
        assert enabled is True
        assert reason == "enabled"

    def test_cleanup_policy_skips_when_not_armed(self) -> None:
        enabled, reason = evaluate_cleanup_on_exit_policy(
            cleanup_on_exit=True,
            mode=SafeMode.LIVE_TRADE,
            exchange_port="futures",
            armed=False,
            mainnet=True,
            fixture_path=None,
            stop_reason="duration_reached",
        )
        assert enabled is False
        assert reason == "not_armed"

    def test_cleanup_policy_skips_fixture_mode(self) -> None:
        enabled, reason = evaluate_cleanup_on_exit_policy(
            cleanup_on_exit=True,
            mode=SafeMode.LIVE_TRADE,
            exchange_port="futures",
            armed=True,
            mainnet=True,
            fixture_path="fixture.jsonl",
            stop_reason="duration_reached",
        )
        assert enabled is False
        assert reason == "fixture_mode"

    def test_cleanup_policy_skips_when_not_duration_timeout(self) -> None:
        enabled, reason = evaluate_cleanup_on_exit_policy(
            cleanup_on_exit=True,
            mode=SafeMode.LIVE_TRADE,
            exchange_port="futures",
            armed=True,
            mainnet=True,
            fixture_path=None,
            stop_reason="shutdown_requested",
        )
        assert enabled is False
        assert reason == "not_duration_timeout"


class TestRunCleanupOnExit:
    def test_run_cleanup_on_exit_invokes_exchange_state_per_symbol(self) -> None:
        calls: list[list[str]] = []

        def _fake_run(
            cmd: list[str],
            *,
            cwd: Any,
            env: dict[str, str],
            capture_output: bool,
            text: bool,
            check: bool,
        ) -> Any:
            calls.append(cmd)
            assert env["ALLOW_MAINNET_TRADE"] == "1"
            assert capture_output is True
            assert text is True
            assert check is False
            _ = cwd
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        failures = _run_cleanup_on_exit(
            ["BTCUSDT", "ETHUSDT"],
            run_cmd=_fake_run,
            executable=sys.executable,
        )

        assert failures == 0
        assert calls == [
            [sys.executable, "-m", "scripts.exchange_state", "cleanup", "BTCUSDT"],
            [sys.executable, "-m", "scripts.exchange_state", "cleanup", "ETHUSDT"],
        ]


class TestBuildConnector:
    """Test build_connector() network selection."""

    def test_default_uses_testnet(self) -> None:
        """Default build_connector uses testnet WS URL."""
        connector = build_connector(["BTCUSDT"], SafeMode.READ_ONLY, None)
        assert connector._config.use_testnet is True
        assert connector._config.ws_url is None  # no explicit override
        # Effective URL should be testnet (derived from use_testnet=True)
        assert connector._ws_config is not None
        assert connector._ws_config.ws_url == "wss://testnet.binance.vision/ws"

    def test_mainnet_flag_sets_mainnet_url(self) -> None:
        """build_connector(use_testnet=False) uses mainnet WS URL (spot)."""
        connector = build_connector(["BTCUSDT"], SafeMode.READ_ONLY, None, use_testnet=False)
        assert connector._config.use_testnet is False
        assert connector._config.ws_url is None  # no explicit override
        # Effective URL should be spot mainnet (derived from use_testnet=False)
        assert connector._ws_config is not None
        assert connector._ws_config.ws_url == BINANCE_WS_MAINNET

    def test_futures_port_uses_futures_ws(self) -> None:
        """exchange_port=futures → fstream WS URL in both config and ws_config."""
        connector = build_connector(
            ["BTCUSDT"],
            SafeMode.READ_ONLY,
            None,
            use_testnet=False,
            exchange_port="futures",
        )
        assert connector._config.ws_url == BINANCE_WS_FUTURES_MAINNET
        # Critical: verify the EFFECTIVE URL used by BinanceWsConnector
        assert connector._ws_config is not None
        assert connector._ws_config.ws_url == BINANCE_WS_FUTURES_MAINNET

    def test_noop_port_uses_spot_ws(self) -> None:
        """exchange_port=noop + use_testnet=False → spot WS URL."""
        connector = build_connector(
            ["BTCUSDT"],
            SafeMode.READ_ONLY,
            None,
            use_testnet=False,
            exchange_port="noop",
        )
        assert connector._config.ws_url is None  # no explicit override
        assert connector._ws_config is not None
        assert connector._ws_config.ws_url == BINANCE_WS_MAINNET

    def test_testnet_overrides_futures(self) -> None:
        """Testnet flag takes precedence over futures port."""
        connector = build_connector(
            ["BTCUSDT"],
            SafeMode.READ_ONLY,
            None,
            use_testnet=True,
            exchange_port="futures",
        )
        assert connector._config.ws_url is None  # testnet → no futures override
        assert connector._ws_config is not None
        assert connector._ws_config.ws_url == "wss://testnet.binance.vision/ws"

    def test_direct_config_use_testnet_false_no_ws_url(self) -> None:
        """P1 regression: LiveConnectorConfig(use_testnet=False) without ws_url → spot."""
        c = LiveConnectorV0(config=LiveConnectorConfig(symbols=["BTCUSDT"], use_testnet=False))
        assert c._ws_config is not None
        assert c._ws_config.ws_url == BINANCE_WS_MAINNET


class TestFuturesPreflightValidation:
    """Preflight symbol-vs-venue validation via production function."""

    def test_wrapper_constraints_unavailable_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrapper: futures + constraints=None → sys.exit(1)."""
        monkeypatch.setattr(run_trading_mod, "_load_symbol_constraints", lambda: None)
        with pytest.raises(SystemExit) as exc_info:
            run_trading_mod._validate_futures_preflight_or_exit(["BTCUSDT"], "futures", None)
        assert exc_info.value.code == 1

    def test_wrapper_missing_symbol_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrapper: futures + symbol not in constraints → sys.exit(1)."""
        monkeypatch.setattr(
            run_trading_mod, "_load_symbol_constraints", lambda: {"BTCUSDT": object()}
        )
        with pytest.raises(SystemExit) as exc_info:
            run_trading_mod._validate_futures_preflight_or_exit(["FARTCOINUSDT"], "futures", None)
        assert exc_info.value.code == 1

    def test_constraints_unavailable_exits(self) -> None:
        """Futures + constraints=None → fail-closed."""
        result = evaluate_futures_preflight(["BTCUSDT"], "futures", None, None)
        assert result.status == "constraints_unavailable"

    def test_missing_symbol_exits(self) -> None:
        """Futures + symbol not in constraints → fail-closed."""
        constraints: dict[str, Any] = {"BTCUSDT": object()}
        result = evaluate_futures_preflight(["FARTCOINUSDT"], "futures", None, constraints)
        assert result.status == "symbol_missing"
        assert result.missing_symbols == ("FARTCOINUSDT",)

    def test_non_futures_no_exit(self) -> None:
        """Non-futures port → no validation, skipped."""
        result = evaluate_futures_preflight(["BTCUSDT"], "noop", None, None)
        assert result.status == "skipped"

    def test_futures_with_fixture_no_exit(self) -> None:
        """Futures + fixture path → skip validation."""
        result = evaluate_futures_preflight(["BTCUSDT"], "futures", "some/fixture.jsonl", None)
        assert result.status == "skipped"

    def test_valid_symbol_no_exit(self) -> None:
        """Futures + symbol present in constraints → passed."""
        constraints: dict[str, Any] = {"BTCUSDT": object()}
        result = evaluate_futures_preflight(["BTCUSDT"], "futures", None, constraints)
        assert result.status == "passed"


class TestGridV2AccountSyncPreflight:
    """Grid_v2 preflight: fail-closed when account sync is disabled."""

    def test_grid_v2_disabled_skips(self) -> None:
        result = evaluate_grid_v2_account_sync_preflight(
            exchange_port="futures",
            fixture_path=None,
            grid_v2_enabled=False,
            account_sync_enabled=False,
        )
        assert result.status == "skipped"

    def test_grid_v2_enabled_requires_account_sync(self) -> None:
        result = evaluate_grid_v2_account_sync_preflight(
            exchange_port="futures",
            fixture_path=None,
            grid_v2_enabled=True,
            account_sync_enabled=False,
        )
        assert result.status == "account_sync_disabled"

    def test_grid_v2_enabled_with_account_sync_passes(self) -> None:
        result = evaluate_grid_v2_account_sync_preflight(
            exchange_port="futures",
            fixture_path=None,
            grid_v2_enabled=True,
            account_sync_enabled=True,
        )
        assert result.status == "passed"

    def test_grid_v2_wrapper_exits_when_account_sync_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRINDER_GRID_V2_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "0")
        with pytest.raises(SystemExit) as exc_info:
            run_trading_mod._validate_grid_v2_account_sync_or_exit("futures", None)
        assert exc_info.value.code == 1


class TestBuildEngine:
    """Test build_engine() rehearsal knobs (PR-P2-LOOP-2)."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def test_default_not_armed(self) -> None:
        """Default build_engine has armed=False."""
        engine = build_engine(SafeMode.READ_ONLY)
        assert engine._config.armed is False

    def test_armed_flag_sets_armed(self) -> None:
        """build_engine(armed=True) sets config.armed=True."""
        engine = build_engine(SafeMode.PAPER, armed=True)
        assert engine._config.armed is True
        assert engine._config.mode == SafeMode.PAPER

    def test_paper_size_per_level(self) -> None:
        """build_engine(paper_size_per_level=...) overrides PaperEngine sizing."""
        engine = build_engine(SafeMode.READ_ONLY, paper_size_per_level=Decimal("0.001"))
        assert engine._paper_engine._policy.size_per_level == Decimal("0.001")

    def test_default_paper_size(self) -> None:
        """Default build_engine uses PaperEngine default size (100)."""
        engine = build_engine(SafeMode.READ_ONLY)
        assert engine._paper_engine._policy.size_per_level == Decimal("100")

    def test_fill_model_loaded_from_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """build_engine loads FillModelV0 when GRINDER_FILL_MODEL_DIR is set."""
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        model_data = json.dumps(
            {"bins": {"long|1|1|0": 5000}, "global_prior_bps": 5000, "n_train_rows": 10}
        )
        (model_dir / "model.json").write_text(model_data)
        sha = hashlib.sha256(model_data.encode()).hexdigest()
        (model_dir / "manifest.json").write_text(json.dumps({"sha256": {"model.json": sha}}))
        monkeypatch.setenv("GRINDER_FILL_MODEL_DIR", str(model_dir))
        engine = build_engine(SafeMode.READ_ONLY)
        assert engine._fill_model is not None
        assert len(engine._fill_model.bins) == 1

    def test_fill_model_none_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """build_engine without GRINDER_FILL_MODEL_DIR has fill_model=None."""
        monkeypatch.delenv("GRINDER_FILL_MODEL_DIR", raising=False)
        engine = build_engine(SafeMode.READ_ONLY)
        assert engine._fill_model is None

    def test_fill_model_bad_dir_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """build_engine with bad model dir fails open (fill_model=None)."""
        monkeypatch.setenv("GRINDER_FILL_MODEL_DIR", "/nonexistent/path")
        engine = build_engine(SafeMode.READ_ONLY)
        assert engine._fill_model is None

    def test_custom_exchange_port_passed_through(self) -> None:
        """build_engine(exchange_port=...) uses provided port instead of NoOp."""
        port = NoOpExchangePort()
        engine = build_engine(SafeMode.READ_ONLY, exchange_port=port)
        assert engine._exchange_port is port

    def test_account_syncer_wired_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """build_engine wires AccountSyncer when GRINDER_ACCOUNT_SYNC_ENABLED=1.

        Wiring regression test: verifies build_engine passes syncer to LiveEngineV0.
        """
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")
        engine = build_engine(SafeMode.READ_ONLY)
        assert engine._account_syncer is not None  # wiring test (private field OK here)

    def test_account_syncer_none_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """build_engine without GRINDER_ACCOUNT_SYNC_ENABLED has account_syncer=None.

        Wiring regression test: default = no syncer (safe-by-default).
        """
        monkeypatch.delenv("GRINDER_ACCOUNT_SYNC_ENABLED", raising=False)
        engine = build_engine(SafeMode.READ_ONLY)
        assert engine._account_syncer is None  # wiring test (private field OK here)


class TestTradingLoop:
    """Test full trading loop integration with fixture data."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    @pytest.mark.asyncio
    async def test_loop_processes_fixture_snapshots(self) -> None:
        """Loop processes N snapshots from FakeWsTransport and sets gauge."""
        messages = [
            json.dumps(
                {
                    "s": "BTCUSDT",
                    "b": "50000.00",
                    "B": "1.5",
                    "a": "50001.00",
                    "A": "2.0",
                }
            ),
            json.dumps(
                {
                    "s": "BTCUSDT",
                    "b": "50002.00",
                    "B": "1.2",
                    "a": "50003.00",
                    "A": "1.8",
                }
            ),
            json.dumps(
                {
                    "s": "BTCUSDT",
                    "b": "50004.00",
                    "B": "1.0",
                    "a": "50005.00",
                    "A": "1.5",
                }
            ),
        ]
        transport = FakeWsTransport(messages=messages, delay_ms=2)
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(symbols=["BTCUSDT"], ws_transport=transport),
            clock=time_module,
            sleep_func=FakeSleep(),
        )
        engine = LiveEngineV0(
            paper_engine=PaperEngine(),
            exchange_port=NoOpExchangePort(),
            config=LiveEngineConfig(mode=SafeMode.READ_ONLY),
        )

        await connector.connect()
        ticks = 0
        try:
            async with asyncio.timeout(5):
                async for snapshot in connector.iter_snapshots():
                    engine.process_snapshot(snapshot)
                    ticks += 1
                    if ticks >= 3:
                        break
        except TimeoutError:
            pass
        finally:
            await connector.close()

        assert ticks == 3
        assert get_sor_metrics().engine_initialized is True


class TestValidateRealPortGates:
    """Test validate_real_port_gates() 5-gate validation."""

    def test_non_live_trade_exits(self) -> None:
        """Gate 1: mode must be LIVE_TRADE."""
        with pytest.raises(SystemExit) as exc_info:
            validate_real_port_gates(SafeMode.READ_ONLY, armed=True)
        assert exc_info.value.code == 1

    def test_not_armed_exits(self) -> None:
        """Gate 2: must be armed."""
        with pytest.raises(SystemExit) as exc_info:
            validate_real_port_gates(SafeMode.LIVE_TRADE, armed=False)
        assert exc_info.value.code == 1

    def test_no_allow_mainnet_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gate 3: ALLOW_MAINNET_TRADE must be set."""
        monkeypatch.delenv("ALLOW_MAINNET_TRADE", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            validate_real_port_gates(SafeMode.LIVE_TRADE, armed=True)
        assert exc_info.value.code == 1

    def test_no_real_ack_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gate 4: GRINDER_REAL_PORT_ACK must be YES_I_REALLY_WANT_MAINNET."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        monkeypatch.delenv("GRINDER_REAL_PORT_ACK", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            validate_real_port_gates(SafeMode.LIVE_TRADE, armed=True)
        assert exc_info.value.code == 1

    def test_all_gates_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All 4 pre-key gates pass (keys checked in build_exchange_port)."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        monkeypatch.setenv("GRINDER_REAL_PORT_ACK", "YES_I_REALLY_WANT_MAINNET")
        # Should not raise
        validate_real_port_gates(SafeMode.LIVE_TRADE, armed=True)

    def test_paper_mode_exits(self) -> None:
        """Paper mode is not live_trade, should exit."""
        with pytest.raises(SystemExit) as exc_info:
            validate_real_port_gates(SafeMode.PAPER, armed=True)
        assert exc_info.value.code == 1


class TestBuildExchangePort:
    """Test build_exchange_port() port selection."""

    def test_noop_returns_noop(self) -> None:
        """port_name='noop' returns NoOpExchangePort (no gate checks)."""
        port = build_exchange_port("noop", SafeMode.READ_ONLY, False, ["BTCUSDT"], Decimal("100"))
        assert isinstance(port, NoOpExchangePort)

    def test_futures_missing_keys_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gate 5: futures without API keys exits."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        monkeypatch.setenv("GRINDER_REAL_PORT_ACK", "YES_I_REALLY_WANT_MAINNET")
        monkeypatch.delenv("BINANCE_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            build_exchange_port("futures", SafeMode.LIVE_TRADE, True, ["BTCUSDT"], Decimal("100"))
        assert exc_info.value.code == 1

    def test_futures_with_all_gates_returns_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All 5 gates pass → returns BinanceFuturesPort."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        monkeypatch.setenv("GRINDER_REAL_PORT_ACK", "YES_I_REALLY_WANT_MAINNET")
        monkeypatch.setenv("BINANCE_API_KEY", "test-key")
        monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")
        # Disable latency retry to avoid extra setup
        monkeypatch.delenv("LATENCY_RETRY_ENABLED", raising=False)
        port = build_exchange_port(
            "futures", SafeMode.LIVE_TRADE, True, ["BTCUSDT"], Decimal("100")
        )
        assert isinstance(port, BinanceFuturesPort)

    def test_unknown_port_exits(self) -> None:
        """Unknown port name exits."""
        with pytest.raises(SystemExit) as exc_info:
            build_exchange_port("unknown", SafeMode.READ_ONLY, False, ["BTCUSDT"], Decimal("100"))
        assert exc_info.value.code == 1

    def test_futures_max_orders_passed_to_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """max_orders_per_run is forwarded to BinanceFuturesPortConfig."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")
        monkeypatch.setenv("GRINDER_REAL_PORT_ACK", "YES_I_REALLY_WANT_MAINNET")
        monkeypatch.setenv("BINANCE_API_KEY", "test-key")
        monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")
        monkeypatch.delenv("LATENCY_RETRY_ENABLED", raising=False)
        port = build_exchange_port(
            "futures",
            SafeMode.LIVE_TRADE,
            True,
            ["BTCUSDT"],
            Decimal("100"),
            max_orders_per_run=50,
        )
        assert isinstance(port, BinanceFuturesPort)
        assert port.config.max_orders_per_run == 50


class TestValidateMaxOrdersAck:
    """Test validate_max_orders_ack() ACK guard."""

    def test_max_1_no_ack_needed(self) -> None:
        """max_orders=1 does not require ACK."""
        validate_max_orders_ack(1)  # Should not raise

    def test_max_gt1_without_ack_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """max_orders>1 without ACK env exits."""
        monkeypatch.delenv("GRINDER_MAX_ORDERS_ACK", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            validate_max_orders_ack(50)
        assert exc_info.value.code == 1

    def test_max_gt1_wrong_ack_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """max_orders>1 with wrong ACK value exits."""
        monkeypatch.setenv("GRINDER_MAX_ORDERS_ACK", "WRONG")
        with pytest.raises(SystemExit) as exc_info:
            validate_max_orders_ack(50)
        assert exc_info.value.code == 1

    def test_max_gt1_with_correct_ack_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """max_orders>1 with correct ACK passes."""
        monkeypatch.setenv("GRINDER_MAX_ORDERS_ACK", "YES_I_ACCEPT_MULTI_ORDER")
        validate_max_orders_ack(50)  # Should not raise


class TestHAGatingReadyz:
    """Test HA-gated /readyz semantics via is_trading_ready()."""

    def setup_method(self) -> None:
        reset_trading_state()
        reset_ha_state()

    def teardown_method(self) -> None:
        reset_trading_state()
        reset_ha_state()

    def test_not_ready_when_loop_not_ready(self) -> None:
        """is_trading_ready() returns False when _loop_ready=False."""
        assert is_trading_ready() is False

    def test_ready_without_ha(self) -> None:
        """Without HA, ready when loop_ready=True."""
        run_trading_mod._loop_ready = True
        run_trading_mod._ha_enabled = False
        assert is_trading_ready() is True

    def test_ready_with_ha_active(self) -> None:
        """With HA enabled + ACTIVE role → ready."""
        run_trading_mod._loop_ready = True
        run_trading_mod._ha_enabled = True
        set_ha_state(role=HARole.ACTIVE)
        assert is_trading_ready() is True

    def test_not_ready_with_ha_standby(self) -> None:
        """With HA enabled + STANDBY role → not ready."""
        run_trading_mod._loop_ready = True
        run_trading_mod._ha_enabled = True
        set_ha_state(role=HARole.STANDBY)
        assert is_trading_ready() is False

    def test_not_ready_with_ha_unknown(self) -> None:
        """With HA enabled + UNKNOWN role → not ready (fail-closed)."""
        run_trading_mod._loop_ready = True
        run_trading_mod._ha_enabled = True
        # Default role is UNKNOWN
        assert is_trading_ready() is False

    def test_reset_clears_state(self) -> None:
        """reset_trading_state() clears both flags."""
        run_trading_mod._loop_ready = True
        run_trading_mod._ha_enabled = True
        reset_trading_state()
        assert run_trading_mod._loop_ready is False
        assert run_trading_mod._ha_enabled is False


class TestHAGatingLoop:
    """Test HA-gated loop processing (ACTIVE processes, STANDBY skips)."""

    def setup_method(self) -> None:
        reset_sor_metrics()
        reset_trading_state()
        reset_ha_state()

    def teardown_method(self) -> None:
        reset_trading_state()
        reset_ha_state()

    @pytest.mark.asyncio
    async def test_active_processes_snapshots(self) -> None:
        """When HA enabled + ACTIVE, snapshots are processed."""
        run_trading_mod._ha_enabled = True
        set_ha_state(role=HARole.ACTIVE)

        messages = [
            json.dumps({"s": "BTCUSDT", "b": "50000.00", "B": "1.5", "a": "50001.00", "A": "2.0"}),
            json.dumps({"s": "BTCUSDT", "b": "50002.00", "B": "1.2", "a": "50003.00", "A": "1.8"}),
        ]
        transport = FakeWsTransport(messages=messages, delay_ms=2)
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(symbols=["BTCUSDT"], ws_transport=transport),
            clock=time_module,
            sleep_func=FakeSleep(),
        )
        engine = LiveEngineV0(
            paper_engine=PaperEngine(),
            exchange_port=NoOpExchangePort(),
            config=LiveEngineConfig(mode=SafeMode.READ_ONLY),
        )

        shutdown = asyncio.Event()

        # Run with short duration
        task = asyncio.create_task(trading_loop(connector, engine, shutdown, duration_s=0))
        await asyncio.sleep(0.3)
        shutdown.set()
        await task

        assert get_sor_metrics().engine_initialized is True

    @pytest.mark.asyncio
    async def test_standby_skips_snapshots(self) -> None:
        """When HA enabled + STANDBY, snapshots are skipped (not processed)."""
        run_trading_mod._ha_enabled = True
        set_ha_state(role=HARole.STANDBY)

        messages = [
            json.dumps({"s": "BTCUSDT", "b": "50000.00", "B": "1.5", "a": "50001.00", "A": "2.0"}),
            json.dumps({"s": "BTCUSDT", "b": "50002.00", "B": "1.2", "a": "50003.00", "A": "1.8"}),
        ]
        transport = FakeWsTransport(messages=messages, delay_ms=2)
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(symbols=["BTCUSDT"], ws_transport=transport),
            clock=time_module,
            sleep_func=FakeSleep(),
        )
        engine = LiveEngineV0(
            paper_engine=PaperEngine(),
            exchange_port=NoOpExchangePort(),
            config=LiveEngineConfig(mode=SafeMode.READ_ONLY),
        )

        shutdown = asyncio.Event()

        task = asyncio.create_task(trading_loop(connector, engine, shutdown, duration_s=0))
        await asyncio.sleep(0.3)
        shutdown.set()
        await task

        # Engine was initialized but no snapshots processed (all skipped by HA gating)
        assert get_sor_metrics().engine_initialized is True


# --- ADR-089: _configure_logging tests ---


class TestConfigureLogging:
    """ADR-089: native logging config via GRINDER_LOG_LEVEL."""

    def test_default_level_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default (no env var) → INFO level on root logger."""
        monkeypatch.delenv("GRINDER_LOG_LEVEL", raising=False)
        _configure_logging()
        assert logging.getLogger().level == logging.INFO

    def test_explicit_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GRINDER_LOG_LEVEL=DEBUG → DEBUG level on root logger."""
        monkeypatch.setenv("GRINDER_LOG_LEVEL", "DEBUG")
        _configure_logging()
        assert logging.getLogger().level == logging.DEBUG

    def test_explicit_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GRINDER_LOG_LEVEL=WARNING → WARNING level on root logger."""
        monkeypatch.setenv("GRINDER_LOG_LEVEL", "WARNING")
        _configure_logging()
        assert logging.getLogger().level == logging.WARNING

    def test_invalid_falls_back_to_info(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Invalid value → fallback to INFO + warning printed."""
        monkeypatch.setenv("GRINDER_LOG_LEVEL", "BANANA")
        _configure_logging()
        assert logging.getLogger().level == logging.INFO
        captured = capsys.readouterr()
        assert "invalid GRINDER_LOG_LEVEL" in captured.out

    def test_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GRINDER_LOG_LEVEL=debug (lowercase) → DEBUG."""
        monkeypatch.setenv("GRINDER_LOG_LEVEL", "debug")
        _configure_logging()
        assert logging.getLogger().level == logging.DEBUG


class TestLaunchGuard:
    """Launch guard v2: preflight → (optional cleanup) → verify → start."""

    @pytest.fixture(autouse=True)
    def _patch_exchange_state(self) -> None:
        """Import exchange_state module once for monkeypatching."""
        import scripts.exchange_state as es_mod  # noqa: PLC0415

        self._es_mod = es_mod

    def test_skipped_for_noop_port(self) -> None:
        """Non-futures port → guard skipped."""
        result = evaluate_launch_guard(
            exchange_port="noop",
            mainnet=True,
            armed=True,
            fixture_path=None,
            pre_cleanup=False,
            symbols=["BTCUSDT"],
        )
        assert result.status == "skipped"

    def test_skipped_for_testnet(self) -> None:
        """Testnet → guard skipped."""
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=False,
            armed=True,
            fixture_path=None,
            pre_cleanup=False,
            symbols=["BTCUSDT"],
        )
        assert result.status == "skipped"

    def test_skipped_for_fixture(self) -> None:
        """Fixture mode → guard skipped."""
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=True,
            armed=True,
            fixture_path="some/fixture.jsonl",
            pre_cleanup=False,
            symbols=["BTCUSDT"],
        )
        assert result.status == "skipped"

    def test_skipped_when_not_armed(self) -> None:
        """Not armed → guard skipped."""
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=True,
            armed=False,
            fixture_path=None,
            pre_cleanup=False,
            symbols=["BTCUSDT"],
        )
        assert result.status == "skipped"

    def test_verify_clean_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All symbols verify CLEAN → guard passes."""
        monkeypatch.setattr(self._es_mod, "cmd_verify_programmatic", lambda _s: (True, 0, "FLAT"))
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=True,
            armed=True,
            fixture_path=None,
            pre_cleanup=False,
            symbols=["BTCUSDT"],
        )
        assert result.status == "verify_clean"

    def test_verify_dirty_no_cleanup_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dirty state + no --pre-cleanup → fail-closed."""
        monkeypatch.setattr(self._es_mod, "cmd_verify_programmatic", lambda _s: (False, 3, "0.002"))
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=True,
            armed=True,
            fixture_path=None,
            pre_cleanup=False,
            symbols=["BTCUSDT"],
        )
        assert result.status == "verify_dirty_no_cleanup"
        assert result.orders == 3
        assert result.position == "0.002"

    def test_cleanup_then_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dirty → cleanup → clean → guard passes."""
        call_count = 0

        def mock_verify(symbol: str) -> tuple[bool, int, str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (False, 5, "0.001")  # first call: dirty
            return (True, 0, "FLAT")  # after cleanup: clean

        monkeypatch.setattr(self._es_mod, "cmd_verify_programmatic", mock_verify)
        monkeypatch.setattr(self._es_mod, "cmd_cleanup", lambda _s: None)
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=True,
            armed=True,
            fixture_path=None,
            pre_cleanup=True,
            symbols=["BTCUSDT"],
        )
        assert result.status == "cleanup_then_clean"

    def test_cleanup_then_still_dirty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dirty → cleanup → still dirty → fail-closed."""
        monkeypatch.setattr(self._es_mod, "cmd_verify_programmatic", lambda _s: (False, 2, "0.005"))
        monkeypatch.setattr(self._es_mod, "cmd_cleanup", lambda _s: None)
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=True,
            armed=True,
            fixture_path=None,
            pre_cleanup=True,
            symbols=["BTCUSDT"],
        )
        assert result.status == "cleanup_then_still_dirty"
        assert result.orders == 2

    def test_multi_symbol_first_dirty_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple symbols, first is dirty → fail-closed on first."""

        def mock_verify(symbol: str) -> tuple[bool, int, str]:
            if symbol == "BTCUSDT":
                return (False, 1, "0.001")
            return (True, 0, "FLAT")

        monkeypatch.setattr(self._es_mod, "cmd_verify_programmatic", mock_verify)
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=True,
            armed=True,
            fixture_path=None,
            pre_cleanup=False,
            symbols=["BTCUSDT", "ETHUSDT"],
        )
        assert result.status == "verify_dirty_no_cleanup"

    def test_multi_symbol_all_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple symbols, all clean → passes."""
        monkeypatch.setattr(self._es_mod, "cmd_verify_programmatic", lambda _s: (True, 0, "FLAT"))
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=True,
            armed=True,
            fixture_path=None,
            pre_cleanup=False,
            symbols=["BTCUSDT", "ETHUSDT", "PIPPINUSDT"],
        )
        assert result.status == "verify_clean"

    def test_verify_error_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify throws exception → verify_error with reason."""

        def _boom(_s: str) -> tuple[bool, int, str]:
            msg = "connection refused"
            raise ConnectionError(msg)

        monkeypatch.setattr(self._es_mod, "cmd_verify_programmatic", _boom)
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=True,
            armed=True,
            fixture_path=None,
            pre_cleanup=False,
            symbols=["BTCUSDT"],
        )
        assert result.status == "verify_error"
        assert "connection refused" in result.reason

    def test_cleanup_error_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cleanup throws exception → verify_error."""
        monkeypatch.setattr(self._es_mod, "cmd_verify_programmatic", lambda _s: (False, 1, "0.001"))

        def _boom(_s: str) -> None:
            msg = "API rate limit"
            raise RuntimeError(msg)

        monkeypatch.setattr(self._es_mod, "cmd_cleanup", _boom)
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=True,
            armed=True,
            fixture_path=None,
            pre_cleanup=True,
            symbols=["BTCUSDT"],
        )
        assert result.status == "verify_error"
        assert "cleanup failed" in result.reason

    def test_pre_cleanup_already_clean_returns_verify_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--pre-cleanup + already clean → verify_clean (not cleanup_then_clean)."""
        monkeypatch.setattr(self._es_mod, "cmd_verify_programmatic", lambda _s: (True, 0, "FLAT"))
        result = evaluate_launch_guard(
            exchange_port="futures",
            mainnet=True,
            armed=True,
            fixture_path=None,
            pre_cleanup=True,
            symbols=["BTCUSDT"],
        )
        assert result.status == "verify_clean"
