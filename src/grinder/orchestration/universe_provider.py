"""Universe provider: autonomous symbol discovery from exchangeInfo (PR-D1, ADR-131).

SSOT: docs/37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md Section 4.1.
      docs/38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md Phase D1.

Venue-level discovery only:
- Fetches Binance USDT-M futures exchangeInfo
- Keeps USDT perpetual contracts in TRADING status
- Applies operator blacklist
- Returns deterministic sorted candidate list

Does NOT apply: liquidity prefilter, scoring, tuning, ranking.
Those belong to downstream layers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UniverseProviderConfig:
    """Configuration for universe provider.

    Attributes:
        blacklist: Symbols to exclude unconditionally.
        quote_asset: Required quote asset (default: USDT).
        contract_type: Required contract type (default: PERPETUAL).
        required_status: Required trading status (default: TRADING).
    """

    blacklist: frozenset[str] = frozenset()
    quote_asset: str = "USDT"
    contract_type: str = "PERPETUAL"
    required_status: str = "TRADING"


def filter_candidates(
    exchange_info: dict[str, Any],
    config: UniverseProviderConfig | None = None,
) -> list[str]:
    """Extract candidate symbols from exchangeInfo response.

    Pure function. Deterministic: same input -> same sorted output.

    Venue-level filtering only:
    - quoteAsset matches config.quote_asset
    - contractType matches config.contract_type
    - status matches config.required_status
    - symbol not in blacklist

    Args:
        exchange_info: Raw Binance exchangeInfo JSON response.
        config: Provider configuration. Defaults to standard USDT perpetual filter.

    Returns:
        Alphabetically sorted list of candidate symbols.
    """
    cfg = config or UniverseProviderConfig()
    symbols_data = exchange_info.get("symbols", [])

    candidates: list[str] = []
    seen: set[str] = set()

    for entry in symbols_data:
        if not isinstance(entry, dict):
            continue

        symbol = entry.get("symbol")
        if not symbol or not isinstance(symbol, str):
            continue

        if symbol in seen:
            continue
        seen.add(symbol)

        if entry.get("quoteAsset") != cfg.quote_asset:
            continue

        if entry.get("contractType") != cfg.contract_type:
            continue

        if entry.get("status") != cfg.required_status:
            continue

        if symbol in cfg.blacklist:
            continue

        candidates.append(symbol)

    candidates.sort()

    logger.info(
        "UNIVERSE_DISCOVERED count=%d blacklist_size=%d",
        len(candidates),
        len(cfg.blacklist),
    )

    return candidates
