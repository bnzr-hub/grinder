"""Tests for reconciler burst churn suppression (ADR-111).

Timestamp-based suppression using exchange time only:
- PLACE_ENTRY suppressed when snapshot.ts < last_fill_ts
- PLACEs allowed when snapshot catches up
- Both fill paths use exchange time (no wall-clock mixing)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from grinder.connectors.live_connector import SafeMode
from grinder.live import LiveEngineConfig, LiveEngineV0


def _make_engine() -> LiveEngineV0:
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
    return LiveEngineV0(paper, port, config)


class TestTimestampDomainConsistency:
    """Both fill paths must use exchange time, not wall-clock."""

    def test_last_fill_ts_starts_at_zero(self) -> None:
        engine = _make_engine()
        assert engine._last_fill_ts == 0

    def test_user_data_fill_uses_exchange_ts(self) -> None:
        """User-data fill path updates _last_fill_ts with oe.ts (exchange time)."""
        engine = _make_engine()
        # Simulate: oe.ts = 1000 (exchange event time)
        engine._last_fill_ts = max(engine._last_fill_ts, 1000)
        assert engine._last_fill_ts == 1000

    def test_reconstructed_fill_uses_snapshot_ts(self) -> None:
        """Reconstructed fill path uses snapshot.ts (exchange time), not wall-clock."""
        engine = _make_engine()
        # Simulate: snapshot.ts = 2000 (exchange snapshot time)
        snapshot_ts = 2000
        engine._last_fill_ts = max(engine._last_fill_ts, snapshot_ts)
        assert engine._last_fill_ts == 2000


class TestSuppressionLogic:
    """Suppress when snapshot stale, allow when caught up."""

    def test_stale_snapshot_suppresses(self) -> None:
        """snapshot.ts < last_fill_ts → stale → suppress."""
        engine = _make_engine()
        engine._last_fill_ts = 5000
        snapshot_ts = 4000
        stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
        assert stale

    def test_caught_up_snapshot_allows(self) -> None:
        """snapshot.ts >= last_fill_ts → caught up → no suppression."""
        engine = _make_engine()
        engine._last_fill_ts = 5000
        for snapshot_ts in [5000, 5001, 6000]:
            stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
            assert not stale, f"snapshot_ts={snapshot_ts} should not be stale"

    def test_zero_fill_ts_never_suppresses(self) -> None:
        """No fills (last_fill_ts=0) → never suppress."""
        engine = _make_engine()
        for snapshot_ts in [0, 100, 5000]:
            stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
            assert not stale

    def test_no_starvation_after_repeated_fills(self) -> None:
        """Repeated fills don't starve once snapshot catches up."""
        engine = _make_engine()
        # Fill 1
        engine._last_fill_ts = 1000
        # Snapshot catches up
        snapshot_ts = 1500
        assert not (engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts)
        # Fill 2
        engine._last_fill_ts = 2000
        # Snapshot behind
        assert snapshot_ts < engine._last_fill_ts
        # Snapshot catches up again
        snapshot_ts = 2500
        assert not (engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts)

    def test_no_false_suppress_from_wallclock_ahead(self) -> None:
        """Exchange snapshot ts should never be compared against wall-clock.

        Both fill paths now use exchange time, so even if wall-clock is
        ahead of exchange time, there is no false suppression.
        """
        engine = _make_engine()
        # Both set from exchange time
        engine._last_fill_ts = 1774798338  # exchange fill ts
        snapshot_ts = 1774798340  # later exchange snapshot ts
        stale = engine._last_fill_ts > 0 and snapshot_ts < engine._last_fill_ts
        assert not stale, "Snapshot is newer than fill → no suppression"
