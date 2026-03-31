"""Symbol constraint provider for execution layer (M7-06, ADR-060).

Provides SymbolConstraints (step_size, min_qty, tick_size, min_notional) from Binance exchangeInfo.

Sources (in priority order):
1. Local JSON cache file (var/cache/exchange_info.json)
2. Binance Futures REST API (fapi.binance.com/fapi/v1/exchangeInfo)

Design decisions:
- All values parsed as strings -> Decimal (determinism)
- LOT_SIZE filter is the SSOT for stepSize/minQty
- MIN_NOTIONAL or NOTIONAL filter is the SSOT for minNotional
- Cache file has TTL but can be used indefinitely in offline mode
- Missing symbol = no constraints applied (pass-through, ADR-059)

See: ADR-060 for full contract
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from grinder.execution.engine import SymbolConstraints

if TYPE_CHECKING:
    from grinder.execution.binance_port import HttpClient

logger = logging.getLogger(__name__)

# Default cache location
DEFAULT_CACHE_DIR = Path("var/cache")
DEFAULT_CACHE_FILE = "exchange_info_futures.json"

# Binance Futures USDT-M exchangeInfo endpoint
BINANCE_FUTURES_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# Cache TTL in seconds (24 hours)
DEFAULT_CACHE_TTL_SECONDS = 86400


@dataclass(frozen=True)
class ConstraintProviderConfig:
    """Configuration for ConstraintProvider.

    Attributes:
        cache_dir: Directory for cache files (default: var/cache)
        cache_file: Cache filename (default: exchange_info_futures.json)
        cache_ttl_seconds: Cache TTL in seconds (default: 86400 = 24h)
        allow_fetch: Whether to allow fetching from API (default: True)
        exchange_info_url: URL for exchangeInfo endpoint
    """

    cache_dir: Path = DEFAULT_CACHE_DIR
    cache_file: str = DEFAULT_CACHE_FILE
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    allow_fetch: bool = True
    exchange_info_url: str = BINANCE_FUTURES_EXCHANGE_INFO_URL


class ConstraintProviderError(Exception):
    """Base error for constraint provider operations."""


class ConstraintParseError(ConstraintProviderError):
    """Error parsing exchangeInfo response."""


class ConstraintFetchError(ConstraintProviderError):
    """Error fetching exchangeInfo from API."""


def parse_lot_size_filter(filters: list[dict[str, Any]]) -> tuple[Decimal, Decimal] | None:
    """Parse LOT_SIZE filter from symbol filters.

    Args:
        filters: List of filter dicts from exchangeInfo symbol

    Returns:
        (step_size, min_qty) as Decimals, or None if no LOT_SIZE filter
    """
    for f in filters:
        if f.get("filterType") == "LOT_SIZE":
            try:
                step_size = Decimal(str(f["stepSize"]))
                min_qty = Decimal(str(f["minQty"]))
                return step_size, min_qty
            except (KeyError, ValueError, InvalidOperation) as e:
                logger.warning("Failed to parse LOT_SIZE filter: %s", e)
                return None
    return None


def parse_price_filter(filters: list[dict[str, Any]]) -> Decimal | None:
    """Parse PRICE_FILTER tick_size from symbol filters.

    Args:
        filters: List of filter dicts from exchangeInfo symbol

    Returns:
        tick_size as Decimal, or None if no PRICE_FILTER
    """
    for f in filters:
        if f.get("filterType") == "PRICE_FILTER":
            try:
                return Decimal(str(f["tickSize"]))
            except (KeyError, ValueError, InvalidOperation) as e:
                logger.warning("Failed to parse PRICE_FILTER: %s", e)
                return None
    return None


def parse_min_notional_filter(filters: list[dict[str, Any]]) -> Decimal | None:
    """Parse MIN_NOTIONAL or NOTIONAL filter from symbol filters.

    Binance uses two filter names for the same concept:
    - ``MIN_NOTIONAL`` with key ``notional``
    - ``NOTIONAL`` with key ``notional``
    MIN_NOTIONAL is checked first (more common on futures).

    Args:
        filters: List of filter dicts from exchangeInfo symbol

    Returns:
        min_notional as Decimal, or None if neither filter present
    """
    for filter_type in ("MIN_NOTIONAL", "NOTIONAL"):
        for f in filters:
            if f.get("filterType") == filter_type:
                try:
                    return Decimal(str(f["notional"]))
                except (KeyError, ValueError, InvalidOperation) as e:
                    logger.warning("Failed to parse %s filter: %s", filter_type, e)
                    break  # try next filter type
    return None


def parse_exchange_info(data: dict[str, Any]) -> dict[str, SymbolConstraints]:
    """Parse exchangeInfo response into SymbolConstraints dict.

    Args:
        data: Raw exchangeInfo JSON response

    Returns:
        Dict mapping symbol -> SymbolConstraints

    Raises:
        ConstraintParseError: If response structure is invalid
    """
    if "symbols" not in data:
        raise ConstraintParseError("exchangeInfo missing 'symbols' key")

    constraints: dict[str, SymbolConstraints] = {}

    for symbol_info in data["symbols"]:
        symbol = symbol_info.get("symbol")
        if not symbol:
            continue

        filters = symbol_info.get("filters", [])
        lot_size = parse_lot_size_filter(filters)
        tick_size = parse_price_filter(filters)
        min_notional = parse_min_notional_filter(filters)

        if lot_size is not None:
            step_size, min_qty = lot_size
            constraints[symbol] = SymbolConstraints(
                step_size=step_size,
                min_qty=min_qty,
                tick_size=tick_size or Decimal("0"),
                min_notional=min_notional or Decimal("0"),
            )
            logger.debug(
                "Parsed constraints for %s: step_size=%s, min_qty=%s, tick_size=%s, min_notional=%s",
                symbol,
                step_size,
                min_qty,
                tick_size,
                min_notional,
            )

    logger.info("Parsed constraints for %d symbols", len(constraints))
    return constraints


class ConstraintProvider:
    """Provider for symbol constraints from exchangeInfo.

    Usage:
        # From cache only (offline)
        provider = ConstraintProvider.from_cache(Path("var/cache/exchange_info.json"))
        constraints = provider.get_constraints()

        # From API with caching
        provider = ConstraintProvider(http_client=client, config=config)
        constraints = provider.get_constraints()  # Fetches if cache stale/missing
    """

    def __init__(
        self,
        http_client: HttpClient | None = None,
        config: ConstraintProviderConfig | None = None,
    ) -> None:
        """Initialize provider.

        Args:
            http_client: HTTP client for API requests (optional for cache-only mode)
            config: Provider configuration
        """
        self._http_client = http_client
        self._config = config or ConstraintProviderConfig()
        self._cached_constraints: dict[str, SymbolConstraints] | None = None

    @classmethod
    def from_cache(cls, cache_path: Path) -> ConstraintProvider:
        """Create provider that reads only from existing cache file.

        Args:
            cache_path: Path to cache JSON file

        Returns:
            ConstraintProvider configured for offline mode
        """
        config = ConstraintProviderConfig(
            cache_dir=cache_path.parent,
            cache_file=cache_path.name,
            allow_fetch=False,
        )
        return cls(http_client=None, config=config)

    @property
    def cache_path(self) -> Path:
        """Full path to cache file."""
        return self._config.cache_dir / self._config.cache_file

    def _is_cache_valid(self) -> bool:
        """Check if cache exists and is within TTL."""
        if not self.cache_path.exists():
            return False

        try:
            mtime = self.cache_path.stat().st_mtime
            age_seconds = time.time() - mtime
            return age_seconds < self._config.cache_ttl_seconds
        except OSError:
            return False

    def _load_from_cache(self) -> dict[str, SymbolConstraints] | None:
        """Load constraints from cache file.

        Returns:
            Constraints dict or None if cache missing/invalid
        """
        if not self.cache_path.exists():
            logger.debug("Cache file not found: %s", self.cache_path)
            return None

        try:
            with self.cache_path.open(encoding="utf-8") as f:
                data = json.load(f)
            return parse_exchange_info(data)
        except (OSError, json.JSONDecodeError, ConstraintParseError) as e:
            logger.warning("Failed to load cache: %s", e)
            return None

    def _fetch_from_api(self) -> dict[str, Any]:
        """Fetch exchangeInfo from Binance API.

        Returns:
            Raw exchangeInfo response dict

        Raises:
            ConstraintFetchError: If fetch fails
        """
        if self._http_client is None:
            raise ConstraintFetchError("No HTTP client configured for API fetch")

        if not self._config.allow_fetch:
            raise ConstraintFetchError("API fetch disabled by configuration")

        try:
            response = self._http_client.request(
                method="GET",
                url=self._config.exchange_info_url,
                timeout_ms=10000,
            )

            if response.status_code != 200:
                raise ConstraintFetchError(f"API returned status {response.status_code}")

            # HttpResponse.json_data is the parsed JSON
            if isinstance(response.json_data, dict):
                return response.json_data
            raise ConstraintFetchError("exchangeInfo response is not a dict")

        except Exception as e:
            if isinstance(e, ConstraintFetchError):
                raise
            raise ConstraintFetchError(f"Failed to fetch exchangeInfo: {e}") from e

    def _save_to_cache(self, data: dict[str, Any]) -> None:
        """Save exchangeInfo to cache file.

        Args:
            data: Raw exchangeInfo response to cache
        """
        try:
            self._config.cache_dir.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            logger.info("Saved exchangeInfo to cache: %s", self.cache_path)
        except OSError as e:
            logger.warning("Failed to save cache: %s", e)

    def get_constraints(
        self,
        *,
        force_refresh: bool = False,
    ) -> dict[str, SymbolConstraints]:
        """Get symbol constraints.

        Resolution order:
        1. If force_refresh=False and cache is valid, use cache
        2. If allow_fetch=True, fetch from API and update cache
        3. If fetch fails but stale cache exists, use stale cache
        4. Return empty dict if nothing available

        Args:
            force_refresh: Force API fetch even if cache is valid

        Returns:
            Dict mapping symbol -> SymbolConstraints
        """
        # Check in-memory cache
        if self._cached_constraints is not None and not force_refresh:
            return self._cached_constraints

        # Try file cache if not forcing refresh
        if not force_refresh and self._is_cache_valid():
            constraints = self._load_from_cache()
            if constraints is not None:
                self._cached_constraints = constraints
                logger.info("Loaded %d constraints from cache", len(constraints))
                return constraints

        # Try API fetch
        if self._config.allow_fetch and self._http_client is not None:
            try:
                data = self._fetch_from_api()
                constraints = parse_exchange_info(data)
                self._save_to_cache(data)
                self._cached_constraints = constraints
                logger.info("Fetched %d constraints from API", len(constraints))
                return constraints
            except ConstraintFetchError as e:
                logger.warning("API fetch failed: %s", e)

        # Fallback to stale cache
        constraints = self._load_from_cache()
        if constraints is not None:
            self._cached_constraints = constraints
            logger.info("Using stale cache with %d constraints", len(constraints))
            return constraints

        # No constraints available
        logger.warning("No constraints available (cache and API both failed)")
        return {}

    def get_constraint(self, symbol: str) -> SymbolConstraints | None:
        """Get constraints for a specific symbol.

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")

        Returns:
            SymbolConstraints or None if not found
        """
        constraints = self.get_constraints()
        return constraints.get(symbol)


def load_constraints_from_file(path: Path) -> dict[str, SymbolConstraints]:
    """Convenience function to load constraints from a JSON file.

    Args:
        path: Path to exchangeInfo JSON file

    Returns:
        Dict mapping symbol -> SymbolConstraints

    Raises:
        ConstraintParseError: If file is invalid
        FileNotFoundError: If file doesn't exist
    """
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return parse_exchange_info(data)
