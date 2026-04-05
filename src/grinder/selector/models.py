"""Typed models for V1 symbol selection."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class SkipReason(Enum):
    """Why a symbol was excluded by the prefilter."""

    NOT_TUNED = "NOT_TUNED"
    NOT_TRADING = "NOT_TRADING"
    LOW_VOLUME_LAST_12X5M = "LOW_VOLUME_LAST_12X5M"
    LOW_NATR_5M = "LOW_NATR_5M"
    FEATURES_UNAVAILABLE = "FEATURES_UNAVAILABLE"
    BLACKLISTED = "BLACKLISTED"
    EXCEEDS_RUN_CAP = "EXCEEDS_RUN_CAP"


@dataclass(frozen=True)
class SelectionFeatures:
    """Market-data features for one symbol used in selection.

    All values are from the most recent available snapshot.
    """

    symbol: str
    quote_volume_last_12x5m: Decimal  # sum of last 12 closed 5m kline quote volumes
    best_bid: Decimal
    best_ask: Decimal
    natr_14_5m: Decimal  # NATR(14) on 5m candles, as percentage (e.g. 1.5 = 1.5%)

    @property
    def mid_price(self) -> Decimal:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        mid = self.mid_price
        if mid <= 0:
            return Decimal("9999")
        return (self.best_ask - self.best_bid) / mid * 10000


@dataclass(frozen=True)
class ScoredSymbol:
    """Symbol with its V1 selection score breakdown."""

    symbol: str
    score: float
    volume_score: float
    spread_score: float
    volatility_score: float
