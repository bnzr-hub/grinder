"""Tests for exchange capability layer."""

from __future__ import annotations

from decimal import Decimal

from grinder.execution.exchange_capabilities import (
    AccountCapabilities,
    ExchangeCapabilitiesProvider,
    SymbolRulebook,
    parse_leverage_brackets,
    parse_symbol_rulebook,
)

# --- Fixtures ---

DRIFT_SYMBOL_INFO = {
    "symbol": "DRIFTUSDT",
    "status": "TRADING",
    "pricePrecision": 4,
    "quantityPrecision": 0,
    "maxNumOrders": 200,
    "orderTypes": ["LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT_MARKET"],
    "timeInForce": ["GTC", "IOC", "FOK"],
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
        {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "100000"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
    ],
}

BTC_SYMBOL_INFO = {
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "pricePrecision": 2,
    "quantityPrecision": 3,
    "maxNumOrders": 200,
    "orderTypes": ["LIMIT", "MARKET"],
    "timeInForce": ["GTC", "IOC"],
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
        {"filterType": "NOTIONAL", "notional": "5"},
    ],
}

DRIFT_BRACKETS = [
    {
        "symbol": "DRIFTUSDT",
        "brackets": [
            {
                "bracket": 1,
                "initialLeverage": 20,
                "notionalCap": "5000",
                "notionalFloor": "0",
                "maintMarginRatio": "0.025",
            },
            {
                "bracket": 2,
                "initialLeverage": 10,
                "notionalCap": "15000",
                "notionalFloor": "5000",
                "maintMarginRatio": "0.05",
            },
            {
                "bracket": 3,
                "initialLeverage": 5,
                "notionalCap": "40000",
                "notionalFloor": "15000",
                "maintMarginRatio": "0.1",
            },
        ],
    }
]


# --- Parsing tests ---


class TestParseSymbolRulebook:
    def test_parses_drift(self) -> None:
        rb = parse_symbol_rulebook(DRIFT_SYMBOL_INFO)
        assert rb is not None
        assert rb.symbol == "DRIFTUSDT"
        assert rb.status == "TRADING"
        assert rb.tick_size == Decimal("0.0001")
        assert rb.step_size == Decimal("1")
        assert rb.min_qty == Decimal("1")
        assert rb.max_qty == Decimal("100000")
        assert rb.min_notional == Decimal("5")
        assert rb.price_precision == 4
        assert rb.quantity_precision == 0
        assert "LIMIT" in rb.order_types

    def test_parses_btc(self) -> None:
        rb = parse_symbol_rulebook(BTC_SYMBOL_INFO)
        assert rb is not None
        assert rb.tick_size == Decimal("0.10")
        assert rb.step_size == Decimal("0.001")
        assert rb.min_notional == Decimal("5")

    def test_missing_symbol_returns_none(self) -> None:
        assert parse_symbol_rulebook({}) is None

    def test_missing_filters_returns_zeros(self) -> None:
        rb = parse_symbol_rulebook({"symbol": "TESTUSDT", "status": "TRADING", "filters": []})
        assert rb is not None
        assert rb.tick_size == Decimal("0")
        assert rb.min_notional == Decimal("0")


class TestParseLeverageBrackets:
    def test_parses_drift_brackets(self) -> None:
        result = parse_leverage_brackets(DRIFT_BRACKETS)
        assert "DRIFTUSDT" in result
        brackets = result["DRIFTUSDT"]
        assert len(brackets) == 3
        assert brackets[0].initial_leverage == 20
        assert brackets[0].notional_cap == Decimal("5000")
        assert brackets[2].initial_leverage == 5

    def test_empty_response(self) -> None:
        assert parse_leverage_brackets([]) == {}


# --- SymbolRulebook legality tests ---


class TestSymbolRulebookLegality:
    def _drift_rb(self) -> SymbolRulebook:
        rb = parse_symbol_rulebook(DRIFT_SYMBOL_INFO)
        assert rb is not None
        return rb

    def test_legal_order(self) -> None:
        rb = self._drift_rb()
        assert rb.check_legality(Decimal("0.0413"), Decimal("124")) == []

    def test_price_not_tick_aligned(self) -> None:
        rb = self._drift_rb()
        violations = rb.check_legality(Decimal("0.04135"), Decimal("124"))
        assert any("PRICE_NOT_TICK_ALIGNED" in v for v in violations)

    def test_qty_below_min(self) -> None:
        rb = self._drift_rb()
        violations = rb.check_legality(Decimal("0.0413"), Decimal("0"))
        assert any("QTY_ILLEGAL" in v for v in violations)

    def test_notional_too_low(self) -> None:
        rb = self._drift_rb()
        violations = rb.check_legality(Decimal("0.0403"), Decimal("124"))
        assert any("NOTIONAL_TOO_LOW" in v for v in violations)

    def test_not_trading(self) -> None:
        rb = parse_symbol_rulebook({**DRIFT_SYMBOL_INFO, "status": "BREAK"})
        assert rb is not None
        violations = rb.check_legality(Decimal("0.0413"), Decimal("124"))
        assert any("SYMBOL_NOT_TRADING" in v for v in violations)

    def test_btc_legal(self) -> None:
        rb = parse_symbol_rulebook(BTC_SYMBOL_INFO)
        assert rb is not None
        assert rb.check_legality(Decimal("80000.0"), Decimal("0.001")) == []


# --- AccountCapabilities tests ---


class TestAccountCapabilities:
    def _drift_caps(self) -> AccountCapabilities:
        brackets = parse_leverage_brackets(DRIFT_BRACKETS)["DRIFTUSDT"]
        return AccountCapabilities(
            symbol="DRIFTUSDT",
            leverage=20,
            max_notional_value=Decimal("5000"),
            margin_type="cross",
            brackets=brackets,
            fetched_at=0.0,
        )

    def test_max_leverage_for_small_notional(self) -> None:
        caps = self._drift_caps()
        assert caps.max_leverage_for_notional(Decimal("1000")) == 20

    def test_max_leverage_for_large_notional(self) -> None:
        caps = self._drift_caps()
        assert caps.max_leverage_for_notional(Decimal("10000")) == 10

    def test_max_leverage_beyond_all_brackets(self) -> None:
        caps = self._drift_caps()
        assert caps.max_leverage_for_notional(Decimal("999999")) == 5

    def test_stale_when_fetched_at_zero(self) -> None:
        caps = self._drift_caps()
        assert caps.is_stale()

    def test_max_notional_cap(self) -> None:
        caps = self._drift_caps()
        assert caps.max_notional_cap() == Decimal("5000")


# --- Provider tests ---


class TestExchangeCapabilitiesProvider:
    def test_load_from_exchange_info(self) -> None:
        provider = ExchangeCapabilitiesProvider()
        count = provider.load_from_exchange_info({"symbols": [DRIFT_SYMBOL_INFO, BTC_SYMBOL_INFO]})
        assert count == 2
        assert "DRIFTUSDT" in provider.symbols
        assert "BTCUSDT" in provider.symbols

    def test_get_symbol_rulebook(self) -> None:
        provider = ExchangeCapabilitiesProvider()
        provider.load_from_exchange_info({"symbols": [DRIFT_SYMBOL_INFO]})
        rb = provider.get_symbol_rulebook("DRIFTUSDT")
        assert rb is not None
        assert rb.min_notional == Decimal("5")

    def test_get_missing_symbol_returns_none(self) -> None:
        provider = ExchangeCapabilitiesProvider()
        assert provider.get_symbol_rulebook("NOPE") is None

    def test_load_leverage_brackets(self) -> None:
        provider = ExchangeCapabilitiesProvider()
        count = provider.load_leverage_brackets(DRIFT_BRACKETS)
        assert count == 1

    def test_load_account_capabilities(self) -> None:
        provider = ExchangeCapabilitiesProvider()
        provider.load_leverage_brackets(DRIFT_BRACKETS)
        provider.load_account_capabilities("DRIFTUSDT", 20, Decimal("5000"), "cross")
        caps = provider.get_account_capabilities("DRIFTUSDT")
        assert caps is not None
        assert caps.leverage == 20
        assert caps.margin_type == "cross"
        assert len(caps.brackets) == 3

    def test_get_missing_account_returns_none(self) -> None:
        provider = ExchangeCapabilitiesProvider()
        assert provider.get_account_capabilities("NOPE") is None
