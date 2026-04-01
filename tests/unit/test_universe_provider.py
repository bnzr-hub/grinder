"""Tests for UniverseProvider (PR-D1, ADR-131).

Covers:
- Keeps only TRADING symbols
- Keeps only USDT perpetual
- Blacklist excluded
- Deterministic alphabetical order
- Empty response
- Malformed entries handled
- Duplicate symbols deduplicated
- All filters combined
"""

from __future__ import annotations

from grinder.orchestration.universe_provider import (
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

    def test_empty_dict(self) -> None:
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
