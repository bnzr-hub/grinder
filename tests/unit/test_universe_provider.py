"""Tests for UniverseProvider (PR-D1, ADR-131).

Covers:
- filter_candidates: TRADING-only, USDT-perpetual-only, blacklist,
  deterministic order, empty, malformed, dedup, combined
- UniverseProvider: refresh interval, fetch failure retention,
  fetcher injection, clock injection
"""

from __future__ import annotations

from grinder.orchestration.universe_provider import (
    UniverseProvider,
    UniverseProviderConfig,
    filter_candidates,
)


def _make_symbol(
    symbol: str,
    quote: str = "USDT",
    contract: str = "PERPETUAL",
    status: str = "TRADING",
) -> dict[str, str]:
    return {
        "symbol": symbol,
        "quoteAsset": quote,
        "contractType": contract,
        "status": status,
        "baseAsset": symbol.replace(quote, ""),
    }


FIXTURE = {
    "symbols": [
        _make_symbol("BTCUSDT"),
        _make_symbol("ETHUSDT"),
        _make_symbol("SOLUSDT"),
        _make_symbol("BTCBUSD", quote="BUSD"),
        _make_symbol("BTCUSDT_230630", contract="CURRENT_QUARTER"),
        _make_symbol("XRPUSDT", status="SETTLING"),
        _make_symbol("SCAMUSDT"),
    ]
}


# --- Tests: filter_candidates (pure function) ---


class TestKeepsTradingOnly:
    def test_settling_excluded(self) -> None:
        result = filter_candidates(FIXTURE)
        assert "XRPUSDT" not in result

    def test_trading_kept(self) -> None:
        result = filter_candidates(FIXTURE)
        assert "BTCUSDT" in result
        assert "ETHUSDT" in result


class TestKeepsUsdtPerpetualOnly:
    def test_busd_excluded(self) -> None:
        result = filter_candidates(FIXTURE)
        assert "BTCBUSD" not in result

    def test_quarterly_excluded(self) -> None:
        result = filter_candidates(FIXTURE)
        assert "BTCUSDT_230630" not in result

    def test_perpetual_usdt_kept(self) -> None:
        result = filter_candidates(FIXTURE)
        assert "SOLUSDT" in result


class TestBlacklistExcluded:
    def test_blacklisted_symbol(self) -> None:
        config = UniverseProviderConfig(blacklist=frozenset({"SCAMUSDT"}))
        result = filter_candidates(FIXTURE, config)
        assert "SCAMUSDT" not in result
        assert "BTCUSDT" in result

    def test_no_blacklist(self) -> None:
        result = filter_candidates(FIXTURE)
        assert "SCAMUSDT" in result


class TestDeterministicOrder:
    def test_alphabetical(self) -> None:
        result = filter_candidates(FIXTURE)
        assert result == sorted(result)

    def test_repeated_calls_same(self) -> None:
        r1 = filter_candidates(FIXTURE)
        r2 = filter_candidates(FIXTURE)
        assert r1 == r2


class TestEmptyResponse:
    def test_empty_symbols(self) -> None:
        assert filter_candidates({"symbols": []}) == []

    def test_missing_symbols_key(self) -> None:
        assert filter_candidates({}) == []


class TestMalformedEntries:
    def test_non_dict_entry(self) -> None:
        data = {"symbols": ["not_a_dict", _make_symbol("BTCUSDT")]}
        result = filter_candidates(data)
        assert result == ["BTCUSDT"]

    def test_missing_symbol_field(self) -> None:
        data = {"symbols": [{"quoteAsset": "USDT"}, _make_symbol("ETHUSDT")]}
        result = filter_candidates(data)
        assert result == ["ETHUSDT"]

    def test_empty_symbol_string(self) -> None:
        data = {"symbols": [{"symbol": "", "quoteAsset": "USDT"}, _make_symbol("BTCUSDT")]}
        result = filter_candidates(data)
        assert result == ["BTCUSDT"]


class TestDuplicateSymbols:
    def test_deduplication(self) -> None:
        data = {"symbols": [_make_symbol("BTCUSDT"), _make_symbol("BTCUSDT")]}
        result = filter_candidates(data)
        assert result == ["BTCUSDT"]


class TestAllFiltersCombined:
    def test_full_fixture(self) -> None:
        config = UniverseProviderConfig(blacklist=frozenset({"SCAMUSDT"}))
        result = filter_candidates(FIXTURE, config)
        assert result == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


# --- Tests: UniverseProvider (stateful provider with fetch/refresh/fail-safe) ---


def _fake_fetcher(data: dict) -> object:  # type: ignore[type-arg]
    """Create a fetcher that returns fixed data."""

    def fetch(url: str, timeout: int) -> dict:  # type: ignore[type-arg]
        return data

    return fetch


def _failing_fetcher() -> object:
    def fetch(url: str, timeout: int) -> dict:  # type: ignore[type-arg]
        raise ConnectionError("network down")

    return fetch


class TestProviderFetch:
    """Provider fetches and returns candidates."""

    def test_initial_fetch(self) -> None:
        t = [0.0]
        provider = UniverseProvider(
            config=UniverseProviderConfig(refresh_s=60.0),
            clock=lambda: t[0],
            fetcher=_fake_fetcher(FIXTURE),
        )
        result = provider.get_candidates()
        assert "BTCUSDT" in result
        assert "ETHUSDT" in result
        assert provider.fetch_count == 1

    def test_fetch_produces_sorted_list(self) -> None:
        provider = UniverseProvider(
            fetcher=_fake_fetcher(FIXTURE),
        )
        result = provider.get_candidates()
        assert result == sorted(result)


class TestProviderRefreshInterval:
    """Provider skips fetch when cache is fresh."""

    def test_skips_fetch_within_interval(self) -> None:
        t = [0.0]
        call_count = [0]

        def counting_fetcher(url: str, timeout: int) -> dict:  # type: ignore[type-arg]
            call_count[0] += 1
            return FIXTURE

        provider = UniverseProvider(
            config=UniverseProviderConfig(refresh_s=60.0),
            clock=lambda: t[0],
            fetcher=counting_fetcher,
        )

        provider.get_candidates()
        assert call_count[0] == 1

        t[0] = 30.0  # within refresh interval
        provider.get_candidates()
        assert call_count[0] == 1  # no second fetch

    def test_refetches_after_interval(self) -> None:
        t = [0.0]
        call_count = [0]

        def counting_fetcher(url: str, timeout: int) -> dict:  # type: ignore[type-arg]
            call_count[0] += 1
            return FIXTURE

        provider = UniverseProvider(
            config=UniverseProviderConfig(refresh_s=60.0),
            clock=lambda: t[0],
            fetcher=counting_fetcher,
        )

        provider.get_candidates()
        assert call_count[0] == 1

        t[0] = 61.0  # past refresh interval
        provider.get_candidates()
        assert call_count[0] == 2


class TestProviderFetchFailureRetention:
    """Provider retains previous universe on fetch failure."""

    def test_retains_on_failure(self) -> None:
        t = [0.0]
        call_seq = [0]

        def flaky_fetcher(url: str, timeout: int) -> dict:  # type: ignore[type-arg]
            call_seq[0] += 1
            if call_seq[0] == 1:
                return FIXTURE
            raise ConnectionError("network down")

        provider = UniverseProvider(
            config=UniverseProviderConfig(refresh_s=10.0),
            clock=lambda: t[0],
            fetcher=flaky_fetcher,
        )

        r1 = provider.get_candidates()
        assert len(r1) > 0
        assert provider.fetch_failures == 0

        t[0] = 20.0  # stale, will try fetch → fail
        r2 = provider.get_candidates()
        assert r2 == r1  # retained previous
        assert provider.fetch_failures == 1

    def test_empty_on_first_failure(self) -> None:
        provider = UniverseProvider(
            fetcher=_failing_fetcher(),
        )
        result = provider.get_candidates()
        assert result == []
        assert provider.fetch_failures == 1


class TestProviderBlacklist:
    """Provider applies blacklist from config."""

    def test_blacklist_applied(self) -> None:
        provider = UniverseProvider(
            config=UniverseProviderConfig(blacklist=frozenset({"SCAMUSDT"})),
            fetcher=_fake_fetcher(FIXTURE),
        )
        result = provider.get_candidates()
        assert "SCAMUSDT" not in result
        assert "BTCUSDT" in result
