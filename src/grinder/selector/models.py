"""Typed models for symbol selection (V1 and V2)."""

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
    GRID_SPACING_BELOW_MIN = "GRID_SPACING_BELOW_MIN"


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


@dataclass(frozen=True)
class SelectionFeaturesV2:
    """Market-data features for V2 selection scoring.

    Extends V1 features with range/trend/execution/toxicity inputs.
    All values computed from the same 5m klines already fetched for V1.
    """

    symbol: str
    quote_volume_last_12x5m: Decimal
    best_bid: Decimal
    best_ask: Decimal
    natr_14_5m: Decimal
    range_score: int  # sum_abs_returns / (net_return + 1); higher = choppier
    net_return_bps: int  # |net directional move| in bps; higher = more trending
    execution_fit_score: float  # [0,1]; 1.0 = comfortably under cap
    toxicity_penalty_raw: float  # [0,1]; 1.0 = worst (widest spread relative to cap)

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
class ScoredSymbolV2:
    """Symbol with its V2 selection score breakdown."""

    symbol: str
    score: float
    liquidity_score: float
    spread_score: float
    volatility_score: float
    range_score: float
    execution_fit_score: float
    trend_penalty: float
    toxicity_penalty: float
