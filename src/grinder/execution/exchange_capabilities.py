"""Exchange capability layer for Binance Futures.

Provides two SSOTs:
- SymbolRulebook: static/semi-static symbol constraints from exchangeInfo
- AccountCapabilities: dynamic account-scoped capabilities (leverage, brackets)

Usage:
    provider = ExchangeCapabilitiesProvider(port)
    rulebook = provider.get_symbol_rulebook("DRIFTUSDT")
    account = provider.get_account_capabilities("DRIFTUSDT")

    if not rulebook.can_place(side, price, qty):
        ...  # reject before dispatch
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeverageBracket:
    """One tier of a symbol's leverage bracket schedule."""

    bracket: int
    initial_leverage: int
    notional_cap: Decimal
    notional_floor: Decimal
    maint_margin_ratio: Decimal


@dataclass(frozen=True)
class SymbolRulebook:
    """Static symbol constraints from exchangeInfo filters.

    Extends SymbolConstraints with additional fields needed for
    full legality checking beyond qty/price basics.
    """

    symbol: str
    status: str  # "TRADING", "BREAK", etc.
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    price_precision: int
    quantity_precision: int
    max_num_orders: int
    order_types: tuple[str, ...]
    time_in_force: tuple[str, ...]

    def is_trading(self) -> bool:
        return self.status == "TRADING"

    def is_price_legal(self, price: Decimal) -> bool:
        """Check if price is tick-aligned."""
        if self.tick_size <= 0:
            return True
        return price % self.tick_size == 0

    def is_qty_legal(self, qty: Decimal) -> bool:
        """Check if qty is step-aligned and within bounds."""
        if self.step_size <= 0:
            return True
        if qty < self.min_qty:
            return False
        if self.max_qty > 0 and qty > self.max_qty:
            return False
        return qty % self.step_size == 0

    def is_notional_legal(self, price: Decimal, qty: Decimal) -> bool:
        """Check if order notional meets minimum."""
        if self.min_notional <= 0:
            return True
        return price * qty >= self.min_notional

    def check_legality(self, price: Decimal, qty: Decimal) -> list[str]:
        """Return list of violation reasons (empty = legal)."""
        violations: list[str] = []
        if not self.is_trading():
            violations.append(f"SYMBOL_NOT_TRADING status={self.status}")
        if not self.is_price_legal(price):
            violations.append(f"PRICE_NOT_TICK_ALIGNED price={price} tick={self.tick_size}")
        if not self.is_qty_legal(qty):
            violations.append(f"QTY_ILLEGAL qty={qty} step={self.step_size} min={self.min_qty}")
        if not self.is_notional_legal(price, qty):
            violations.append(f"NOTIONAL_TOO_LOW notional={price * qty} min={self.min_notional}")
        return violations


@dataclass(frozen=True)
class AccountCapabilities:
    """Dynamic account-scoped capabilities for a symbol."""

    symbol: str
    leverage: int
    max_notional_value: Decimal
    margin_type: str  # "cross" or "isolated"
    brackets: tuple[LeverageBracket, ...]
    fetched_at: float = 0.0  # monotonic timestamp

    def max_leverage_for_notional(self, notional: Decimal) -> int:
        """Find max leverage allowed for a given notional exposure."""
        for b in self.brackets:
            if notional <= b.notional_cap:
                return b.initial_leverage
        # Beyond all brackets — return lowest
        return self.brackets[-1].initial_leverage if self.brackets else 1

    def max_notional_cap(self) -> Decimal:
        """Maximum notional allowed at current leverage bracket."""
        return self.max_notional_value

    def is_stale(self, max_age_s: float = 300.0) -> bool:
        """Check if capabilities are older than max_age_s."""
        if self.fetched_at <= 0:
            return True
        return (time.monotonic() - self.fetched_at) > max_age_s


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")


def parse_symbol_rulebook(symbol_info: dict[str, Any]) -> SymbolRulebook | None:
    """Parse one symbol entry from exchangeInfo response."""
    symbol = symbol_info.get("symbol", "")
    if not symbol:
        return None

    filters = symbol_info.get("filters", [])

    tick_size = _ZERO
    step_size = _ZERO
    min_qty = _ZERO
    max_qty = _ZERO
    min_notional = _ZERO

    for f in filters:
        ft = f.get("filterType", "")
        if ft == "PRICE_FILTER":
            tick_size = Decimal(str(f.get("tickSize", "0")))
        elif ft == "LOT_SIZE":
            step_size = Decimal(str(f.get("stepSize", "0")))
            min_qty = Decimal(str(f.get("minQty", "0")))
            max_qty = Decimal(str(f.get("maxQty", "0")))
        elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
            raw = f.get("notional", f.get("minNotional", "0"))
            min_notional = Decimal(str(raw))

    return SymbolRulebook(
        symbol=symbol,
        status=symbol_info.get("status", "UNKNOWN"),
        tick_size=tick_size,
        step_size=step_size,
        min_qty=min_qty,
        max_qty=max_qty,
        min_notional=min_notional,
        price_precision=int(symbol_info.get("pricePrecision", 8)),
        quantity_precision=int(symbol_info.get("quantityPrecision", 8)),
        max_num_orders=int(symbol_info.get("maxNumOrders", 200)),
        order_types=tuple(symbol_info.get("orderTypes", [])),
        time_in_force=tuple(symbol_info.get("timeInForce", [])),
    )


def parse_leverage_brackets(
    bracket_response: list[dict[str, Any]],
) -> dict[str, tuple[LeverageBracket, ...]]:
    """Parse leverageBracket API response into symbol → brackets mapping."""
    result: dict[str, tuple[LeverageBracket, ...]] = {}
    for entry in bracket_response:
        symbol = entry.get("symbol", "")
        if not symbol:
            continue
        brackets: list[LeverageBracket] = []
        for b in entry.get("brackets", []):
            brackets.append(
                LeverageBracket(
                    bracket=int(b.get("bracket", 0)),
                    initial_leverage=int(b.get("initialLeverage", 1)),
                    notional_cap=Decimal(str(b.get("notionalCap", "0"))),
                    notional_floor=Decimal(str(b.get("notionalFloor", "0"))),
                    maint_margin_ratio=Decimal(str(b.get("maintMarginRatio", "0"))),
                )
            )
        result[symbol] = tuple(brackets)
    return result


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ExchangeCapabilitiesProvider:
    """Fetches and caches exchange capabilities from Binance Futures.

    Wraps:
    - exchangeInfo → SymbolRulebook (via ConstraintProvider cache)
    - leverageBracket → LeverageBracket tiers
    - positionRisk → current leverage/margin info
    """

    def __init__(self, port: Any = None) -> None:
        self._port = port
        self._rulebooks: dict[str, SymbolRulebook] = {}
        self._brackets: dict[str, tuple[LeverageBracket, ...]] = {}
        self._account_caps: dict[str, AccountCapabilities] = {}

    def load_from_exchange_info(self, exchange_info: dict[str, Any]) -> int:
        """Parse exchangeInfo response into symbol rulebooks.

        Returns number of symbols loaded.
        """
        symbols = exchange_info.get("symbols", [])
        count = 0
        for sym_info in symbols:
            rb = parse_symbol_rulebook(sym_info)
            if rb is not None:
                self._rulebooks[rb.symbol] = rb
                count += 1
        logger.info("EXCHANGE_CAPABILITIES_LOADED rulebooks=%d", count)
        return count

    def load_leverage_brackets(self, bracket_response: list[dict[str, Any]]) -> int:
        """Parse leverageBracket response. Returns symbols loaded."""
        parsed = parse_leverage_brackets(bracket_response)
        self._brackets.update(parsed)
        logger.info("EXCHANGE_LEVERAGE_BRACKETS_LOADED symbols=%d", len(parsed))
        return len(parsed)

    def load_account_capabilities(
        self, symbol: str, leverage: int, max_notional: Decimal, margin_type: str
    ) -> None:
        """Register account capabilities for a symbol."""
        brackets = self._brackets.get(symbol, ())
        self._account_caps[symbol] = AccountCapabilities(
            symbol=symbol,
            leverage=leverage,
            max_notional_value=max_notional,
            margin_type=margin_type,
            brackets=brackets,
            fetched_at=time.monotonic(),
        )

    def get_symbol_rulebook(self, symbol: str) -> SymbolRulebook | None:
        """Get symbol rulebook (static constraints)."""
        return self._rulebooks.get(symbol)

    def get_account_capabilities(self, symbol: str) -> AccountCapabilities | None:
        """Get account capabilities (dynamic, may be stale)."""
        return self._account_caps.get(symbol)

    @property
    def symbols(self) -> frozenset[str]:
        """All symbols with loaded rulebooks."""
        return frozenset(self._rulebooks)
