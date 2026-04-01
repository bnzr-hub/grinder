"""Tests for TuningCache (PR-B3b, ADR-126).

Covers: put/get, TTL expiry, expired counter, overwrite, size, reset.
"""

from __future__ import annotations

from decimal import Decimal

from grinder.execution.engine import SymbolConstraints
from grinder.tuning.cache import TuningCache
from grinder.tuning.solver import TuningSolverConfig, TuningStatus, solve

BTC_CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    tick_size=Decimal("0.10"),
    min_notional=Decimal("5"),
)

DEFAULT_CONFIG = TuningSolverConfig(max_position_usd=Decimal("10000"), max_inventory_levels=5)


def _make_result(symbol: str = "BTCUSDT") -> solve.__class__:  # type: ignore[name-defined]
    """Helper: create a TUNED result for testing."""
    return solve(symbol, BTC_CONSTRAINTS, Decimal("80000"), DEFAULT_CONFIG)


class TestPutAndGet:
    def test_store_and_retrieve(self) -> None:
        cache = TuningCache(ttl_s=60.0)
        result = _make_result()
        cache.put("BTCUSDT", result)
        assert cache.get("BTCUSDT") == result

    def test_get_missing_symbol(self) -> None:
        cache = TuningCache(ttl_s=60.0)
        assert cache.get("NOPE") is None

    def test_put_overwrites(self) -> None:
        cache = TuningCache(ttl_s=60.0)
        r1 = _make_result("BTCUSDT")
        r2 = solve("BTCUSDT", BTC_CONSTRAINTS, Decimal("0"), DEFAULT_CONFIG)  # NO_GO
        cache.put("BTCUSDT", r1)
        cache.put("BTCUSDT", r2)
        retrieved = cache.get("BTCUSDT")
        assert retrieved == r2
        assert retrieved is not None
        assert retrieved.status == TuningStatus.NO_GO


class TestTTLExpiry:
    def test_expired_returns_none(self) -> None:
        t = [0.0]
        cache = TuningCache(ttl_s=10.0, _clock=lambda: t[0])
        cache.put("BTCUSDT", _make_result())

        t[0] = 10.0  # exactly at expiry
        assert cache.get("BTCUSDT") is None

    def test_not_expired_within_ttl(self) -> None:
        t = [0.0]
        cache = TuningCache(ttl_s=10.0, _clock=lambda: t[0])
        cache.put("BTCUSDT", _make_result())

        t[0] = 9.99
        assert cache.get("BTCUSDT") is not None

    def test_expired_counter_increments(self) -> None:
        t = [0.0]
        cache = TuningCache(ttl_s=5.0, _clock=lambda: t[0])
        cache.put("BTCUSDT", _make_result())
        cache.put("ETHUSDT", _make_result("ETHUSDT"))

        t[0] = 10.0
        cache.get("BTCUSDT")  # expired
        cache.get("ETHUSDT")  # expired
        assert cache.expired_total == 2


class TestSize:
    def test_size_reflects_valid_entries(self) -> None:
        t = [0.0]
        cache = TuningCache(ttl_s=10.0, _clock=lambda: t[0])
        cache.put("A", _make_result())
        cache.put("B", _make_result())
        assert cache.size == 2

        t[0] = 10.0  # both expired
        assert cache.size == 0

    def test_size_empty(self) -> None:
        cache = TuningCache(ttl_s=60.0)
        assert cache.size == 0


class TestReset:
    def test_reset_clears_all(self) -> None:
        cache = TuningCache(ttl_s=60.0)
        cache.put("BTCUSDT", _make_result())
        cache.expired_total = 5
        cache.reset()
        assert cache.get("BTCUSDT") is None
        assert cache.size == 0
        assert cache.expired_total == 0
