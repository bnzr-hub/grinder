"""Tests for constraint provider (M7-06, ADR-060; M7-08, ADR-063).

Tests verify:
- Parsing LOT_SIZE filters from exchangeInfo
- Decimal parsing determinism
- Cache loading/saving
- TTL/refresh behavior (M7-08)
- Fallback chain: cache → API → stale → empty
- Integration with ExecutionEngine constraints
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

from grinder.core import GridMode
from grinder.execution import ExecutionEngine, ExecutionState, NoOpExchangePort
from grinder.execution.binance_port import HttpResponse
from grinder.execution.constraint_provider import (
    ConstraintParseError,
    ConstraintProvider,
    ConstraintProviderConfig,
    load_constraints_from_file,
    parse_exchange_info,
    parse_lot_size_filter,
    parse_min_notional_filter,
    parse_price_filter,
)
from grinder.execution.engine import ExecutionEngineConfig, SymbolConstraints
from grinder.policies.base import GridPlan

# --- Fixtures ---

FIXTURE_PATH = (
    Path(__file__).parent.parent / "fixtures" / "exchange_info" / "binance_futures_usdt.json"
)


@pytest.fixture
def exchange_info_data() -> dict[str, Any]:
    """Load exchange info fixture."""
    with FIXTURE_PATH.open() as f:
        data: dict[str, Any] = json.load(f)
        return data


@pytest.fixture
def temp_cache_dir() -> Generator[Path, None, None]:
    """Create temporary directory for cache tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# --- Tests: parse_lot_size_filter ---


class TestParseLotSizeFilter:
    """Tests for LOT_SIZE filter parsing."""

    def test_parse_lot_size_btc(self) -> None:
        """Test parsing LOT_SIZE filter for BTC (step=0.001, min=0.001)."""
        filters = [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
        ]

        result = parse_lot_size_filter(filters)

        assert result is not None
        step_size, min_qty = result
        assert step_size == Decimal("0.001")
        assert min_qty == Decimal("0.001")

    def test_parse_lot_size_sol(self) -> None:
        """Test parsing LOT_SIZE filter for SOL (step=1, min=1)."""
        filters = [
            {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "1000000", "stepSize": "1"},
        ]

        result = parse_lot_size_filter(filters)

        assert result is not None
        step_size, min_qty = result
        assert step_size == Decimal("1")
        assert min_qty == Decimal("1")

    def test_parse_lot_size_missing(self) -> None:
        """Test returns None when no LOT_SIZE filter present."""
        filters = [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ]

        result = parse_lot_size_filter(filters)

        assert result is None

    def test_parse_lot_size_invalid(self) -> None:
        """Test returns None when LOT_SIZE filter has invalid data."""
        filters = [
            {"filterType": "LOT_SIZE", "minQty": "invalid", "stepSize": "0.001"},
        ]

        result = parse_lot_size_filter(filters)

        assert result is None


# --- Tests: parse_price_filter ---


class TestParsePriceFilter:
    """Tests for PRICE_FILTER parsing."""

    def test_parse_tick_size_btc(self) -> None:
        """BTCUSDT futures tickSize=0.10."""
        filters = [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "556.80",
                "maxPrice": "4529764",
                "tickSize": "0.10",
            },
        ]
        result = parse_price_filter(filters)
        assert result == Decimal("0.10")

    def test_parse_tick_size_missing(self) -> None:
        """No PRICE_FILTER → None."""
        filters = [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"}]
        result = parse_price_filter(filters)
        assert result is None

    def test_parse_tick_size_invalid(self) -> None:
        """Invalid tickSize → None."""
        filters = [{"filterType": "PRICE_FILTER", "tickSize": "invalid"}]
        result = parse_price_filter(filters)
        assert result is None


# --- Tests: parse_min_notional_filter ---


class TestParseMinNotionalFilter:
    """Tests for MIN_NOTIONAL / NOTIONAL filter parsing."""

    def test_parse_min_notional(self) -> None:
        """MIN_NOTIONAL with notional=5."""
        filters = [
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ]
        result = parse_min_notional_filter(filters)
        assert result == Decimal("5")

    def test_parse_notional_filter(self) -> None:
        """NOTIONAL filter (alternate name) with notional=10."""
        filters = [
            {"filterType": "NOTIONAL", "notional": "10"},
        ]
        result = parse_min_notional_filter(filters)
        assert result == Decimal("10")

    def test_min_notional_preferred_over_notional(self) -> None:
        """When both present, MIN_NOTIONAL takes priority."""
        filters = [
            {"filterType": "NOTIONAL", "notional": "10"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ]
        result = parse_min_notional_filter(filters)
        assert result == Decimal("5")

    def test_parse_min_notional_missing(self) -> None:
        """No notional filter → None."""
        filters = [
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
        ]
        result = parse_min_notional_filter(filters)
        assert result is None

    def test_parse_min_notional_invalid(self) -> None:
        """Invalid notional value → None."""
        filters = [
            {"filterType": "MIN_NOTIONAL", "notional": "invalid"},
        ]
        result = parse_min_notional_filter(filters)
        assert result is None

    def test_parse_min_notional_missing_key(self) -> None:
        """MIN_NOTIONAL filter without notional key → None."""
        filters = [
            {"filterType": "MIN_NOTIONAL"},
        ]
        result = parse_min_notional_filter(filters)
        assert result is None

    def test_malformed_min_notional_falls_back_to_notional(self) -> None:
        """Malformed MIN_NOTIONAL falls back to valid NOTIONAL."""
        filters = [
            {"filterType": "MIN_NOTIONAL", "notional": "invalid"},
            {"filterType": "NOTIONAL", "notional": "10"},
        ]
        result = parse_min_notional_filter(filters)
        assert result == Decimal("10")


# --- Tests: parse_exchange_info ---


class TestParseExchangeInfo:
    """Tests for full exchangeInfo parsing."""

    def test_parse_all_symbols(self, exchange_info_data: dict[str, Any]) -> None:
        """Test parsing all symbols from fixture."""
        constraints = parse_exchange_info(exchange_info_data)

        assert len(constraints) == 3
        assert "BTCUSDT" in constraints
        assert "ETHUSDT" in constraints
        assert "SOLUSDT" in constraints

    def test_parse_btc_constraints(self, exchange_info_data: dict[str, Any]) -> None:
        """Test BTCUSDT constraints parsed correctly (incl. tick_size, min_notional)."""
        constraints = parse_exchange_info(exchange_info_data)

        btc = constraints["BTCUSDT"]
        assert btc.step_size == Decimal("0.001")
        assert btc.min_qty == Decimal("0.001")
        assert btc.tick_size == Decimal("0.10")
        assert btc.min_notional == Decimal("5")

    def test_parse_sol_constraints(self, exchange_info_data: dict[str, Any]) -> None:
        """Test SOLUSDT constraints parsed correctly (integer qty)."""
        constraints = parse_exchange_info(exchange_info_data)

        sol = constraints["SOLUSDT"]
        assert sol.step_size == Decimal("1")
        assert sol.min_qty == Decimal("1")

    def test_parse_deterministic(self, exchange_info_data: dict[str, Any]) -> None:
        """Test parsing is deterministic (same input = same output)."""
        constraints1 = parse_exchange_info(exchange_info_data)
        constraints2 = parse_exchange_info(exchange_info_data)

        for symbol in constraints1:
            assert constraints1[symbol] == constraints2[symbol]

    def test_parse_missing_symbols_key(self) -> None:
        """Test raises error when symbols key is missing."""
        with pytest.raises(ConstraintParseError, match="missing 'symbols'"):
            parse_exchange_info({"timezone": "UTC"})


# --- Tests: load_constraints_from_file ---


class TestLoadConstraintsFromFile:
    """Tests for loading constraints from JSON file."""

    def test_load_from_fixture(self) -> None:
        """Test loading from fixture file."""
        constraints = load_constraints_from_file(FIXTURE_PATH)

        assert len(constraints) == 3
        assert constraints["BTCUSDT"].step_size == Decimal("0.001")

    def test_load_file_not_found(self) -> None:
        """Test raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_constraints_from_file(Path("/nonexistent/file.json"))


# --- Tests: ConstraintProvider ---


class TestConstraintProvider:
    """Tests for ConstraintProvider class."""

    def test_from_cache_file(self) -> None:
        """Test loading from existing cache file."""
        provider = ConstraintProvider.from_cache(FIXTURE_PATH)
        constraints = provider.get_constraints()

        assert len(constraints) == 3
        assert "BTCUSDT" in constraints

    def test_get_constraint_single(self) -> None:
        """Test getting constraint for single symbol."""
        provider = ConstraintProvider.from_cache(FIXTURE_PATH)

        btc = provider.get_constraint("BTCUSDT")
        assert btc is not None
        assert btc.step_size == Decimal("0.001")

        missing = provider.get_constraint("DOESNOTEXIST")
        assert missing is None

    def test_cache_writes_and_reads(self, temp_cache_dir: Path) -> None:
        """Test caching: save then load from file."""
        # Load original data
        with FIXTURE_PATH.open() as f:
            data = json.load(f)

        # Create provider with custom cache dir
        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="test_cache.json",
            allow_fetch=False,
        )
        provider = ConstraintProvider(config=config)

        # Manually save to cache (simulating API fetch)
        provider._save_to_cache(data)

        # Now load should work
        constraints = provider._load_from_cache()
        assert constraints is not None
        assert len(constraints) == 3

    def test_empty_constraints_when_no_source(self, temp_cache_dir: Path) -> None:
        """Test returns empty dict when no cache and no API available."""
        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="nonexistent.json",
            allow_fetch=False,
        )
        provider = ConstraintProvider(config=config)

        constraints = provider.get_constraints()
        assert constraints == {}


# --- Tests: min_notional backward compatibility and round-trip ---


class TestMinNotionalBackwardCompat:
    """Tests for min_notional backward compatibility and cache round-trip."""

    def test_old_cache_without_min_notional(self, temp_cache_dir: Path) -> None:
        """Cache file without min_notional loads successfully with default 0."""
        old_cache = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    ],
                }
            ]
        }
        cache_file = temp_cache_dir / "old_cache.json"
        with cache_file.open("w") as f:
            json.dump(old_cache, f)

        provider = ConstraintProvider.from_cache(cache_file)
        constraints = provider.get_constraints()

        assert len(constraints) == 1
        btc = constraints["BTCUSDT"]
        assert btc.min_notional == Decimal("0")
        assert btc.step_size == Decimal("0.001")

    def test_cache_round_trip_preserves_min_notional(self, temp_cache_dir: Path) -> None:
        """Parse -> cache -> reload preserves min_notional."""
        # Load original fixture (has MIN_NOTIONAL=5)
        with FIXTURE_PATH.open() as f:
            data = json.load(f)

        # Save to cache
        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="round_trip.json",
            allow_fetch=False,
        )
        provider = ConstraintProvider(config=config)
        provider._save_to_cache(data)

        # Reload from cache
        reloaded = provider._load_from_cache()
        assert reloaded is not None
        assert reloaded["BTCUSDT"].min_notional == Decimal("5")
        assert reloaded["ETHUSDT"].min_notional == Decimal("5")
        assert reloaded["SOLUSDT"].min_notional == Decimal("5")

    def test_symbol_constraints_default_min_notional(self) -> None:
        """SymbolConstraints without min_notional defaults to 0."""
        sc = SymbolConstraints(step_size=Decimal("0.001"), min_qty=Decimal("0.001"))
        assert sc.min_notional == Decimal("0")

    def test_parse_exchange_info_no_notional_filter(self) -> None:
        """Symbol without any notional filter gets min_notional=0."""
        data = {
            "symbols": [
                {
                    "symbol": "TESTUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    ],
                }
            ]
        }
        constraints = parse_exchange_info(data)
        assert constraints["TESTUSDT"].min_notional == Decimal("0")


# --- Tests: Integration with ExecutionEngine ---


class TestConstraintProviderIntegration:
    """Integration tests: ConstraintProvider + ExecutionEngine."""

    def test_provider_constraints_applied_in_engine(self) -> None:
        """Test constraints from provider are applied by engine."""
        # Load constraints from fixture
        constraints = load_constraints_from_file(FIXTURE_PATH)

        # Create engine with constraints enabled (M7-07)
        port = NoOpExchangePort()
        config = ExecutionEngineConfig(constraints_enabled=True)
        engine = ExecutionEngine(port=port, symbol_constraints=constraints, config=config)

        # Plan with small qty that will be floored
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.0015")],  # Will floor to 0.001
            reason_codes=["TEST"],
        )

        empty_state = ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)
        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        # Qty should be floored to 0.001 (step_size)
        # Since 0.001 >= min_qty (0.001), orders should be placed
        placed_orders = list(result.state.open_orders.values())
        assert len(placed_orders) == 2
        for order in placed_orders:
            assert order.quantity == Decimal("0.001")

    def test_provider_constraints_skip_below_min(self) -> None:
        """Test orders below min_qty are skipped."""
        # Create custom constraints with high min_qty
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.01"),  # Higher than our test qty
            )
        }

        port = NoOpExchangePort()
        config = ExecutionEngineConfig(constraints_enabled=True)
        engine = ExecutionEngine(port=port, symbol_constraints=constraints, config=config)

        # Plan with qty that will be below min after rounding
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.005")],  # Below min_qty of 0.01
            reason_codes=["TEST"],
        )

        empty_state = ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)
        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        # Orders should be skipped
        assert len(result.state.open_orders) == 0
        skipped_events = [e for e in result.events if e.event_type == "ORDER_SKIPPED"]
        assert len(skipped_events) == 2

    def test_sol_integer_qty_constraints(self) -> None:
        """Test SOL constraints with integer qty (step=1, min=1)."""
        constraints = load_constraints_from_file(FIXTURE_PATH)

        port = NoOpExchangePort()
        config = ExecutionEngineConfig(constraints_enabled=True)
        engine = ExecutionEngine(port=port, symbol_constraints=constraints, config=config)

        # Plan with fractional qty that will floor to 0
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("100"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.5")],  # Floors to 0, below min=1
            reason_codes=["TEST"],
        )

        empty_state = ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)
        result = engine.evaluate(plan, "SOLUSDT", empty_state, ts=1000)

        # Orders should be skipped (qty floors to 0, below min=1)
        assert len(result.state.open_orders) == 0
        skipped_events = [e for e in result.events if e.event_type == "ORDER_SKIPPED"]
        assert len(skipped_events) == 2


# --- Mock HTTP Client for TTL tests ---


class MockHttpClient:
    """Mock HTTP client that tracks calls and returns configured responses."""

    def __init__(
        self,
        response: HttpResponse | None = None,
        should_fail: bool = False,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = response
        self.should_fail = should_fail

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,  # noqa: ARG002
        timeout_ms: int = 5000,  # noqa: ARG002
        op: str = "",  # noqa: ARG002
    ) -> HttpResponse:
        self.calls.append({"method": method, "url": url, "params": params})
        if self.should_fail:
            raise ConnectionError("Mock connection failure")
        if self.response is None:
            raise ValueError("No mock response configured")
        return self.response


# --- Tests: TTL/Refresh behavior (M7-08, ADR-063) ---


class TestConstraintProviderTTL:
    """Tests for TTL and refresh behavior (M7-08, ADR-063)."""

    @pytest.fixture
    def mock_exchange_info(self) -> dict[str, Any]:
        """Minimal valid exchangeInfo for API mock."""
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"}],
                }
            ]
        }

    def test_ttl_fresh_cache_uses_cache_no_fetch(
        self, temp_cache_dir: Path, mock_exchange_info: dict[str, Any]
    ) -> None:
        """Fresh cache within TTL should be used without API fetch."""
        # Create fresh cache file
        cache_file = temp_cache_dir / "exchange_info.json"
        with cache_file.open("w") as f:
            json.dump(mock_exchange_info, f)

        # Create mock client (should NOT be called)
        mock_client = MockHttpClient(
            response=HttpResponse(status_code=200, json_data=mock_exchange_info)
        )

        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="exchange_info.json",
            cache_ttl_seconds=3600,  # 1 hour TTL
            allow_fetch=True,
        )
        provider = ConstraintProvider(http_client=mock_client, config=config)

        constraints = provider.get_constraints()

        assert len(constraints) == 1
        assert "BTCUSDT" in constraints
        assert len(mock_client.calls) == 0  # No API call made

    def test_ttl_stale_cache_fetches_when_allow_fetch_true(
        self, temp_cache_dir: Path, mock_exchange_info: dict[str, Any]
    ) -> None:
        """Stale cache should trigger API fetch when allow_fetch=True."""
        # Create cache file
        cache_file = temp_cache_dir / "exchange_info.json"
        with cache_file.open("w") as f:
            json.dump(mock_exchange_info, f)

        # Make cache stale by setting mtime in the past
        old_mtime = time.time() - 7200  # 2 hours ago
        os.utime(cache_file, (old_mtime, old_mtime))

        # Create mock client that will be called
        mock_client = MockHttpClient(
            response=HttpResponse(status_code=200, json_data=mock_exchange_info)
        )

        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="exchange_info.json",
            cache_ttl_seconds=3600,  # 1 hour TTL (cache is 2h old = stale)
            allow_fetch=True,
        )
        provider = ConstraintProvider(http_client=mock_client, config=config)

        constraints = provider.get_constraints()

        assert len(constraints) == 1
        assert len(mock_client.calls) == 1  # API was called

    def test_ttl_stale_cache_no_fetch_uses_stale(
        self, temp_cache_dir: Path, mock_exchange_info: dict[str, Any]
    ) -> None:
        """Stale cache with allow_fetch=False should use stale cache."""
        # Create cache file
        cache_file = temp_cache_dir / "exchange_info.json"
        with cache_file.open("w") as f:
            json.dump(mock_exchange_info, f)

        # Make cache stale
        old_mtime = time.time() - 7200
        os.utime(cache_file, (old_mtime, old_mtime))

        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="exchange_info.json",
            cache_ttl_seconds=3600,
            allow_fetch=False,  # No fetch allowed
        )
        provider = ConstraintProvider(http_client=None, config=config)

        constraints = provider.get_constraints()

        # Should still return constraints from stale cache
        assert len(constraints) == 1
        assert "BTCUSDT" in constraints

    def test_cache_miss_fetches_when_allow_fetch_true(
        self, temp_cache_dir: Path, mock_exchange_info: dict[str, Any]
    ) -> None:
        """Missing cache should trigger API fetch when allow_fetch=True."""
        mock_client = MockHttpClient(
            response=HttpResponse(status_code=200, json_data=mock_exchange_info)
        )

        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="nonexistent.json",
            allow_fetch=True,
        )
        provider = ConstraintProvider(http_client=mock_client, config=config)

        constraints = provider.get_constraints()

        assert len(constraints) == 1
        assert len(mock_client.calls) == 1

    def test_cache_miss_no_fetch_returns_empty(self, temp_cache_dir: Path) -> None:
        """Missing cache with allow_fetch=False should return empty dict."""
        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="nonexistent.json",
            allow_fetch=False,
        )
        provider = ConstraintProvider(http_client=None, config=config)

        constraints = provider.get_constraints()

        assert constraints == {}

    def test_api_failure_with_stale_cache_falls_back(
        self, temp_cache_dir: Path, mock_exchange_info: dict[str, Any]
    ) -> None:
        """API failure should fall back to stale cache."""
        # Create stale cache
        cache_file = temp_cache_dir / "exchange_info.json"
        with cache_file.open("w") as f:
            json.dump(mock_exchange_info, f)
        old_mtime = time.time() - 7200
        os.utime(cache_file, (old_mtime, old_mtime))

        # Mock client that fails
        mock_client = MockHttpClient(should_fail=True)

        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="exchange_info.json",
            cache_ttl_seconds=3600,
            allow_fetch=True,
        )
        provider = ConstraintProvider(http_client=mock_client, config=config)

        constraints = provider.get_constraints()

        # Should fall back to stale cache
        assert len(constraints) == 1
        assert "BTCUSDT" in constraints
        assert len(mock_client.calls) == 1  # API was attempted

    def test_api_failure_without_cache_returns_empty(self, temp_cache_dir: Path) -> None:
        """API failure with no cache should return empty dict."""
        mock_client = MockHttpClient(should_fail=True)

        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="nonexistent.json",
            allow_fetch=True,
        )
        provider = ConstraintProvider(http_client=mock_client, config=config)

        constraints = provider.get_constraints()

        assert constraints == {}
        assert len(mock_client.calls) == 1
