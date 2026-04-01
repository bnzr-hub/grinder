"""In-memory tuning result cache with TTL (PR-B3b, ADR-126).

Stores TuningResult per symbol with time-based expiration.
Thread-safe, injectable clock for deterministic testing.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from grinder.tuning.solver import TuningResult


@dataclass
class TuningCache:
    """In-memory cache of TuningResult per symbol.

    Attributes:
        ttl_s: Time-to-live in seconds for cached entries.
        expired_total: Count of expired entries encountered on get().
    """

    ttl_s: float = 300.0
    expired_total: int = 0

    _entries: dict[str, tuple[TuningResult, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _clock: Callable[[], float] = field(default=time.monotonic)

    def put(self, symbol: str, result: TuningResult) -> None:
        """Store a tuning result for a symbol.

        Overwrites any existing entry for the same symbol.
        """
        expires_at = self._clock() + self.ttl_s
        with self._lock:
            self._entries[symbol] = (result, expires_at)

    def get(self, symbol: str) -> TuningResult | None:
        """Retrieve a tuning result if present and not expired.

        Returns None if missing or expired. Increments expired_total
        on expiry.
        """
        with self._lock:
            entry = self._entries.get(symbol)
            if entry is None:
                return None
            result, expires_at = entry
            if self._clock() >= expires_at:
                del self._entries[symbol]
                self.expired_total += 1
                return None
            return result

    @property
    def size(self) -> int:
        """Count of non-expired entries."""
        now = self._clock()
        with self._lock:
            return sum(1 for _, expires_at in self._entries.values() if now < expires_at)

    def reset(self) -> None:
        """Clear all entries and counters."""
        with self._lock:
            self._entries.clear()
            self.expired_total = 0


# Module-level singleton (eager init — no race at creation time)
_cache = TuningCache()


def get_tuning_cache() -> TuningCache:
    """Get global tuning cache instance."""
    return _cache


def reset_tuning_cache() -> None:
    """Reset global tuning cache (for testing)."""
    _cache.reset()
