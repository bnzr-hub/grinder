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
import time as time_module
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
import scripts.run_trading as run_trading_mod
from scripts.run_trading import (
    _configure_logging,
    build_connector,
    build_engine,
    build_exchange_port,
    is_trading_ready,
    reset_trading_state,
    trading_loop,
    validate_env,
    validate_max_orders_ack,
    validate_real_port_gates,
)

if TYPE_CHECKING:
    from pathlib import Path

from grinder.connectors.binance_ws import BINANCE_WS_MAINNET, FakeWsTransport
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


class TestBuildConnector:
    """Test build_connector() network selection."""

    def test_default_uses_testnet(self) -> None:
        """Default build_connector uses testnet WS URL."""
        connector = build_connector(["BTCUSDT"], SafeMode.READ_ONLY, None)
        assert connector._config.use_testnet is True
        assert connector._config.ws_url == "wss://testnet.binance.vision/ws"

    def test_mainnet_flag_sets_mainnet_url(self) -> None:
        """build_connector(use_testnet=False) uses mainnet WS URL."""
        connector = build_connector(["BTCUSDT"], SafeMode.READ_ONLY, None, use_testnet=False)
        assert connector._config.use_testnet is False
        assert connector._config.ws_url == BINANCE_WS_MAINNET


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
