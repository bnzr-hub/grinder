"""SharedRegimeRegistry — thread-safe per-symbol regime state for autonomous runtime.

Engine threads publish their authoritative RegimeDecision here after each
classify_regime() call. The autonomous runtime reads the registry to derive
portfolio-level regime for PortfolioBudgetAllocator.

Lifecycle:
- Engine startup: no regime yet (absent from registry)
- First plan() with regime classification: publish snapshot
- Regime change: update snapshot (deduped — unchanged regime not re-logged)
- Engine shutdown: remove symbol from registry

Thread safety: all mutations and reads are under a single lock.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.controller.regime import Regime, RegimeReason

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegimeSnapshot:
    """Published per-symbol regime snapshot."""

    symbol: str
    regime: Regime
    reason: RegimeReason
    confidence: int
    ts_mono: float  # monotonic timestamp of publication


class SharedRegimeRegistry:
    """Thread-safe registry of per-symbol regime decisions.

    Engine threads call publish() after classify_regime().
    Runtime reads via snapshot() or get().
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, RegimeSnapshot] = {}

    def publish(
        self,
        symbol: str,
        regime: Regime,
        reason: RegimeReason,
        confidence: int,
    ) -> bool:
        """Publish or update regime for a symbol. Returns True if regime changed.

        Deduplicates: unchanged regime is stored but not logged.
        """
        with self._lock:
            prev = self._entries.get(symbol)
            snap = RegimeSnapshot(
                symbol=symbol,
                regime=regime,
                reason=reason,
                confidence=confidence,
                ts_mono=time.monotonic(),
            )
            self._entries[symbol] = snap

            changed = prev is None or prev.regime != regime
            if changed:
                if prev is None:
                    logger.info(
                        "REGIME_PUBLISHED symbol=%s regime=%s reason=%s confidence=%d",
                        symbol,
                        regime.value,
                        reason.value,
                        confidence,
                    )
                else:
                    logger.info(
                        "REGIME_UPDATED symbol=%s old=%s new=%s reason=%s confidence=%d",
                        symbol,
                        prev.regime.value,
                        regime.value,
                        reason.value,
                        confidence,
                    )
            return changed

    def remove(self, symbol: str) -> bool:
        """Remove symbol from registry (engine shutdown). Returns True if was present."""
        with self._lock:
            prev = self._entries.pop(symbol, None)
            if prev is not None:
                logger.info(
                    "REGIME_REMOVED symbol=%s last_regime=%s",
                    symbol,
                    prev.regime.value,
                )
                return True
            return False

    def get(self, symbol: str) -> RegimeSnapshot | None:
        """Get current regime for one symbol."""
        with self._lock:
            return self._entries.get(symbol)

    def snapshot(self) -> dict[str, RegimeSnapshot]:
        """Get copy of all current regime entries."""
        with self._lock:
            return dict(self._entries)

    def symbols(self) -> frozenset[str]:
        """Get set of symbols with published regimes."""
        with self._lock:
            return frozenset(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
