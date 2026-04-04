"""Tests for REST sync freshness policy (_get_effective_sync_interval)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from grinder.connectors.live_connector import SafeMode
from grinder.live import LiveEngineConfig, LiveEngineV0


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
    return LiveEngineV0(paper, port, config)


class TestSyncFreshnessPolicy:
    """_get_effective_sync_interval returns correct interval and reason."""

    def test_startup_uses_base_interval(self) -> None:
        engine = _make_engine()
        engine._grid_v2_started = False
        interval, reason = engine._get_effective_sync_interval()
        assert interval == 5000
        assert reason == "startup"

    def test_untrusted_ledger_uses_base_interval(self) -> None:
        engine = _make_engine()
        engine._grid_v2_started = True
        engine._event_ledger._bootstrapped = False  # not trusted
        interval, reason = engine._get_effective_sync_interval()
        assert interval == 5000
        assert reason == "ledger_not_trusted"

    def test_stale_user_data_uses_base_interval(self) -> None:
        engine = _make_engine()
        engine._grid_v2_started = True
        engine._event_ledger._bootstrapped = True
        engine._event_ledger._last_convergence_ok = True
        engine._event_ledger._trust_revoked = False
        engine._last_user_data_event_mono = 0.0  # never received
        interval, reason = engine._get_effective_sync_interval()
        assert interval == 5000
        assert reason == "user_data_stale"

    def test_fresh_trusted_uses_extended_interval(self) -> None:
        engine = _make_engine()
        engine._grid_v2_started = True
        engine._event_ledger._bootstrapped = True
        engine._event_ledger._last_convergence_ok = True
        engine._event_ledger._trust_revoked = False
        engine._last_user_data_event_mono = time.monotonic()  # just now
        interval, reason = engine._get_effective_sync_interval()
        assert interval == 15000
        assert reason == "trusted_fresh"

    def test_old_user_data_not_fresh(self) -> None:
        engine = _make_engine()
        engine._grid_v2_started = True
        engine._event_ledger._bootstrapped = True
        engine._event_ledger._last_convergence_ok = True
        engine._event_ledger._trust_revoked = False
        engine._last_user_data_event_mono = time.monotonic() - 10.0  # 10s ago
        interval, reason = engine._get_effective_sync_interval()
        assert interval == 5000
        assert reason == "user_data_stale"

    def test_trust_revoked_uses_base_interval(self) -> None:
        engine = _make_engine()
        engine._grid_v2_started = True
        engine._event_ledger._bootstrapped = True
        engine._event_ledger._last_convergence_ok = True
        engine._event_ledger._trust_revoked = True  # revoked
        engine._last_user_data_event_mono = time.monotonic()
        interval, reason = engine._get_effective_sync_interval()
        assert interval == 5000
        assert reason == "ledger_not_trusted"

    def test_awaiting_sync_forces_base(self) -> None:
        engine = _make_engine()
        engine._grid_v2_started = True
        engine._event_ledger._bootstrapped = True
        engine._event_ledger._last_convergence_ok = True
        engine._event_ledger._trust_revoked = False
        engine._last_user_data_event_mono = time.monotonic()
        engine._grid_v2_awaiting_sync = True  # pending convergence
        interval, reason = engine._get_effective_sync_interval()
        assert interval == 5000
        assert reason == "awaiting_sync"

    def test_pending_places_forces_base(self) -> None:
        engine = _make_engine()
        engine._grid_v2_started = True
        engine._event_ledger._bootstrapped = True
        engine._event_ledger._last_convergence_ok = True
        engine._event_ledger._trust_revoked = False
        engine._last_user_data_event_mono = time.monotonic()
        engine._grid_v2_pending_place_cids["g-test"] = 1
        interval, reason = engine._get_effective_sync_interval()
        assert interval == 5000
        assert reason == "pending_places"

    def test_pending_cancels_forces_base(self) -> None:
        engine = _make_engine()
        engine._grid_v2_started = True
        engine._event_ledger._bootstrapped = True
        engine._event_ledger._last_convergence_ok = True
        engine._event_ledger._trust_revoked = False
        engine._last_user_data_event_mono = time.monotonic()
        engine._grid_v2_pending_cancels["g-cancel"] = (1000, 1)
        interval, reason = engine._get_effective_sync_interval()
        assert interval == 5000
        assert reason == "pending_cancels"
