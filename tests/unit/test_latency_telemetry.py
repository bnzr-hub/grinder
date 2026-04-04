"""Tests for latency telemetry phase-summary logs."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from grinder.observability.latency_telemetry import (
    PhaseTimer,
    log_account_sync,
    log_bootstrap,
    log_branch_convergence,
    log_engine_startup,
    log_fill_cancel_wave,
    log_fill_exit,
    log_fill_reaction,
    log_grid_v2_startup,
    log_reconcile,
    log_seed_dispatch,
    log_shutdown,
)


class TestPhaseTimer:
    def test_elapsed_non_negative(self) -> None:
        t = PhaseTimer()
        assert t.elapsed_ms() >= 0

    def test_elapsed_increases(self) -> None:
        t = PhaseTimer()
        time.sleep(0.01)
        assert t.elapsed_ms() >= 5


class TestLogFunctions:
    """Each log function emits structured LATENCY_ log without error."""

    def test_log_bootstrap(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_bootstrap(42, 3, 2)
        assert "LATENCY_BOOTSTRAP" in caplog.text
        assert "total_ms=42" in caplog.text

    def test_log_engine_startup(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_engine_startup("DRIFTUSDT", 100, 200, 300)
        assert "LATENCY_ENGINE_STARTUP" in caplog.text
        assert "symbol=DRIFTUSDT" in caplog.text

    def test_log_grid_v2_startup(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_grid_v2_startup("DRIFTUSDT", 1500, 10)
        assert "LATENCY_GRID_V2_STARTUP" in caplog.text
        assert "startup_ms=1500" in caplog.text
        assert "seed_count=10" in caplog.text

    def test_log_grid_v2_startup_nonzero(self, caplog: pytest.LogCaptureFixture) -> None:
        """Startup duration must be nonzero when a real delay occurred."""
        with caplog.at_level(logging.INFO):
            log_grid_v2_startup("DRIFTUSDT", 3200, 10)
        assert "startup_ms=3200" in caplog.text

    def test_log_seed_dispatch(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_seed_dispatch("DRIFTUSDT", 3000, 10)
        assert "LATENCY_SEED" in caplog.text
        assert "total_ms=3000" in caplog.text

    def test_log_fill_reaction(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_fill_reaction("DRIFTUSDT", 50, 7)
        assert "LATENCY_FILL_REACTION" in caplog.text
        assert "fill_to_actions_ms=50" in caplog.text

    def test_log_account_sync(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_account_sync("DRIFTUSDT", 630)
        assert "LATENCY_ACCOUNT_SYNC" in caplog.text
        assert "sync_ms=630" in caplog.text

    def test_log_reconcile(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_reconcile("DRIFTUSDT", 15, 3)
        assert "LATENCY_RECONCILE" in caplog.text

    def test_log_fill_exit(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_fill_exit("AIAUSDT", 281)
        assert "LATENCY_FILL_EXIT" in caplog.text
        assert "action_ms=281" in caplog.text

    def test_log_fill_cancel_wave(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_fill_cancel_wave("AIAUSDT", 18, 744, 4)
        assert "LATENCY_FILL_CANCEL_WAVE" in caplog.text
        assert "count=4" in caplog.text

    def test_log_branch_convergence(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_branch_convergence("AIAUSDT", 1280, 7)
        assert "LATENCY_BRANCH_CONVERGENCE" in caplog.text
        assert "total_ms=1280" in caplog.text

    def test_log_shutdown(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_shutdown("DRIFTUSDT", 100, 200, 300)
        assert "LATENCY_SHUTDOWN" in caplog.text
        assert "cancel_ms=100" in caplog.text
