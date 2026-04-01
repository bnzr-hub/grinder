"""Universe provider: autonomous symbol discovery from exchangeInfo (PR-D1, ADR-131).

SSOT: docs/37_AUTONOMOUS_MULTI_SYMBOL_LIVE_ORCHESTRATION_SPEC.md Section 4.1.
      docs/38_AUTONOMOUS_MULTI_SYMBOL_IMPLEMENTATION_PLAN.md Phase D1.

Provider with fetch, refresh interval, and fail-safe retention:
- Fetches Binance USDT-M futures exchangeInfo
- Filters to USDT perpetual contracts in TRADING status, applies blacklist
- Respects configurable refresh interval (skips fetch if not stale)
- Retains previous universe on fetch failure (fail-safe)

Does NOT apply: liquidity prefilter, scoring, tuning, ranking.
Those belong to downstream layers.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Default Binance Futures exchangeInfo endpoint
DEFAULT_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
DEFAULT_REFRESH_S = 300.0
DEFAULT_FETCH_TIMEOUT_S = 10


@dataclass(frozen=True)
class UniverseProviderConfig:
    """Configuration for universe provider.

    Attributes:
        blacklist: Symbols to exclude unconditionally.
        quote_asset: Required quote asset (default: USDT).
        contract_type: Required contract type (default: PERPETUAL).
        required_status: Required trading status (default: TRADING).
        exchange_info_url: URL for exchangeInfo endpoint.
        refresh_s: Minimum seconds between fetches.
        fetch_timeout_s: HTTP fetch timeout in seconds.
    """

    blacklist: frozenset[str] = frozenset()
    quote_asset: str = "USDT"
    contract_type: str = "PERPETUAL"
    required_status: str = "TRADING"
    exchange_info_url: str = DEFAULT_EXCHANGE_INFO_URL
    refresh_s: float = DEFAULT_REFRESH_S
    fetch_timeout_s: int = DEFAULT_FETCH_TIMEOUT_S


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
    return candidates


class UniverseProvider:
    """Provider that fetches and caches the raw symbol universe.

    Respects refresh interval: skips fetch if previous result is still fresh.
    Retains previous universe on fetch failure (fail-safe).

    Args:
        config: Provider configuration.
        clock: Injectable clock for deterministic testing (default: time.monotonic).
        fetcher: Injectable fetch function for testing.
            Signature: (url: str, timeout: int) -> dict[str, Any].
            Default: stdlib urllib fetch.
    """

    def __init__(
        self,
        config: UniverseProviderConfig | None = None,
        clock: Any = None,
        fetcher: Any = None,
    ) -> None:
        import time  # noqa: PLC0415

        self._config = config or UniverseProviderConfig()
        self._clock = clock or time.monotonic
        self._fetcher = fetcher or self._default_fetch
        self._last_fetch_time: float = 0.0
        self._cached_candidates: list[str] = []
        self._fetch_count: int = 0
        self._fetch_failures: int = 0

    def get_candidates(self) -> list[str]:
        """Get current candidate universe.

        Fetches from API if stale (beyond refresh_s). On fetch failure,
        retains previous candidates (fail-safe). Returns empty list only
        if no successful fetch has ever occurred.

        Returns:
            Alphabetically sorted list of candidate symbols.
        """
        now = self._clock()
        elapsed = now - self._last_fetch_time

        if self._cached_candidates and elapsed < self._config.refresh_s:
            return self._cached_candidates

        try:
            data = self._fetcher(self._config.exchange_info_url, self._config.fetch_timeout_s)
            candidates = filter_candidates(data, self._config)
            self._cached_candidates = candidates
            self._last_fetch_time = now
            self._fetch_count += 1

            logger.info(
                "UNIVERSE_DISCOVERED count=%d fetch=%d",
                len(candidates),
                self._fetch_count,
            )
            return candidates

        except Exception as e:
            self._fetch_failures += 1
            logger.warning(
                "UNIVERSE_FETCH_FAILED error=%s failures=%d retaining=%d",
                e,
                self._fetch_failures,
                len(self._cached_candidates),
            )
            return self._cached_candidates

    @property
    def fetch_count(self) -> int:
        """Total successful fetches."""
        return self._fetch_count

    @property
    def fetch_failures(self) -> int:
        """Total fetch failures."""
        return self._fetch_failures

    @staticmethod
    def _default_fetch(url: str, timeout: int) -> dict[str, Any]:
        """Fetch exchangeInfo via stdlib urllib."""
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data: dict[str, Any] = json.loads(resp.read())
            return data
