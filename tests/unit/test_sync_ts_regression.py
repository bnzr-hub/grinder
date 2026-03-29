"""Tests for bounded ts_regression tolerance (ADR-108).

Adversarial tests for:
1. Bounded regression tolerated after streak threshold
2. Repeated cached regression does not starve reconciler
3. Large regression still blocks
4. Streak resets on normal snapshot
5. Observability: tolerated vs blocked
6. Live-style regression case (reproduced incident)
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap, PositionSnap
from grinder.account.syncer import AccountSyncer


def _snap(ts: int, positions: int = 1, orders: int = 1) -> AccountSnapshot:
    """Create a snapshot with given ts and non-empty content."""
    pos = tuple(
        PositionSnap(
            symbol="BTCUSDT",
            side="LONG",
            qty=Decimal("1"),
            entry_price=Decimal("50000"),
            mark_price=Decimal("50000"),
            unrealized_pnl=Decimal("0"),
            leverage=1,
            ts=ts,
        )
        for _ in range(positions)
    )
    ords = tuple(
        OpenOrderSnap(
            order_id=f"o-{i}",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            price=Decimal("49000"),
            qty=Decimal("1"),
            filled_qty=Decimal("0"),
            reduce_only=False,
            status="NEW",
            ts=ts,
        )
        for i in range(orders)
    )
    return AccountSnapshot(positions=pos, open_orders=ords, ts=ts, source="test")


def _make_syncer(snapshots: list[AccountSnapshot]) -> AccountSyncer:
    """Create syncer with a port that returns snapshots in sequence."""
    port = MagicMock()
    port.fetch_account_snapshot = MagicMock(side_effect=snapshots)
    return AccountSyncer(port)


class TestBoundedRegressionTolerated:
    """Small regression accepted after streak threshold."""

    def test_streak_below_threshold_blocks(self) -> None:
        """First N-1 small regressions are still flagged as mismatches."""
        syncer = _make_syncer(
            [
                _snap(1000),  # initial
                _snap(999),  # regression 1
                _snap(999),  # regression 2
            ]
        )
        r1 = syncer.sync()
        assert r1.ok  # initial accepted
        assert syncer.last_ts == 1000

        r2 = syncer.sync()
        assert len(r2.mismatches) == 1  # streak 1/3
        assert r2.mismatches[0].rule == "ts_regression"
        assert syncer.last_ts == 1000  # frozen

        r3 = syncer.sync()
        assert len(r3.mismatches) == 1  # streak 2/3
        assert syncer.last_ts == 1000  # still frozen

    def test_streak_at_threshold_tolerates(self) -> None:
        """After ACCEPT_AFTER regressions, next one is tolerated."""
        syncer = _make_syncer(
            [
                _snap(1000),
                _snap(999),  # streak 1
                _snap(999),  # streak 2
                _snap(999),  # streak 3 (reaches threshold)
                _snap(999),  # streak >= threshold → tolerate
            ]
        )
        syncer.sync()  # initial
        syncer.sync()  # streak 1
        syncer.sync()  # streak 2
        syncer.sync()  # streak 3

        r5 = syncer.sync()  # streak >= threshold → tolerate
        assert r5.ok  # no mismatches
        assert syncer.last_ts == 999  # advanced to snapshot ts


class TestRepeatedCachedRegression:
    """Repeated cached snapshot does not starve forever."""

    def test_reconciler_unblocked_after_tolerance(self) -> None:
        """After tolerance kicks in, subsequent snapshots are accepted."""
        syncer = _make_syncer(
            [
                _snap(1000),
                _snap(998),  # streak 1
                _snap(998),  # streak 2
                _snap(998),  # streak 3 (reaches threshold)
                _snap(998),  # streak >= threshold → tolerate
                _snap(998),  # now last_ts=998, no regression
            ]
        )
        syncer.sync()
        syncer.sync()
        syncer.sync()
        syncer.sync()
        syncer.sync()  # tolerate
        assert syncer.last_ts == 998

        r5 = syncer.sync()  # 998 == last_ts → no regression
        assert r5.ok
        assert syncer.last_ts == 998


class TestLargeRegressionBlocks:
    """Regression beyond tolerance threshold always blocks."""

    def test_large_regression_always_mismatch(self) -> None:
        """Regression > TS_REGRESSION_TOLERANCE_MS → always blocked."""
        syncer = _make_syncer(
            [
                _snap(100_000),
                _snap(80_000),  # 20s regression → exceeds 10s tolerance
            ]
        )
        syncer.sync()

        r2 = syncer.sync()
        assert len(r2.mismatches) == 1
        assert "BLOCKED" in r2.mismatches[0].detail
        assert syncer.last_ts == 100_000  # frozen

    def test_large_regression_never_tolerated(self) -> None:
        """Large regression is not tolerated even after many streaks."""
        snaps = [_snap(100_000)]
        for _ in range(10):
            snaps.append(_snap(80_000))
        syncer = _make_syncer(snaps)
        syncer.sync()

        for _ in range(10):
            r = syncer.sync()
            assert len(r.mismatches) == 1
            assert "BLOCKED" in r.mismatches[0].detail
        assert syncer.last_ts == 100_000


class TestStreakResets:
    """Streak resets on normal snapshot."""

    def test_normal_snapshot_resets_streak(self) -> None:
        """Good snapshot after regression resets streak counter."""
        syncer = _make_syncer(
            [
                _snap(1000),
                _snap(999),  # streak 1
                _snap(1001),  # normal → reset
                _snap(1000),  # new regression, streak starts from 0
                _snap(1000),  # streak 1 again (not 3)
            ]
        )
        syncer.sync()
        syncer.sync()  # streak 1
        syncer.sync()  # normal → reset
        assert syncer.last_ts == 1001

        r4 = syncer.sync()  # new regression streak 1
        assert len(r4.mismatches) == 1
        assert syncer.last_ts == 1001  # frozen again

        r5 = syncer.sync()  # streak 2 (not tolerated yet)
        assert len(r5.mismatches) == 1


class TestLiveStyleRegression:
    """Reproduce the exact incident from the 3600s run."""

    def test_cached_snapshot_2s_regression_unblocks(self) -> None:
        """Exit fill advances last_ts, then Binance returns cached snapshot
        ~2s older. After 3 consecutive regressions, tolerance kicks in and
        reconciler can proceed.
        """
        syncer = _make_syncer(
            [
                _snap(1774784613626),  # accepted (from fill cycle)
                _snap(1774784611697),  # cached: 1.9s regression, streak 1
                _snap(1774784611697),  # cached: streak 2
                _snap(1774784611697),  # cached: streak 3 (reaches threshold)
                _snap(1774784611697),  # cached: streak >= threshold → TOLERATE
                _snap(1774784611697),  # now last_ts=..697, no regression
            ]
        )
        r1 = syncer.sync()
        assert r1.ok
        assert syncer.last_ts == 1774784613626

        # First 3 regressions: flagged (streak building)
        r2 = syncer.sync()
        assert not r2.ok
        r3 = syncer.sync()
        assert not r3.ok
        r4 = syncer.sync()
        assert not r4.ok
        assert syncer.last_ts == 1774784613626  # still frozen

        # Fourth regression: tolerated (streak >= threshold)
        r5 = syncer.sync()
        assert r5.ok
        assert syncer.last_ts == 1774784611697  # advanced!

        # Fifth: no regression anymore
        r6 = syncer.sync()
        assert r6.ok
