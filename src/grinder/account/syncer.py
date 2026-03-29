"""AccountSyncer: read-only sync + mismatch detection (Launch-15 PR2).

Fetches AccountSnapshot from ExchangePort, validates invariants,
detects mismatches, records metrics, and optionally emits evidence.

SSOT: docs/15_ACCOUNT_SYNC_SPEC.md (Sec 15.1-15.7)

Design:
- Read-only: never writes to the exchange (I4).
- Safe-by-default: disabled unless account_sync_enabled flag is set.
- Monotonic ts guard (I5): rejects snapshots older than last accepted.
- No duplicate keys (I6): flags duplicate position keys or order IDs.

Mismatch rules:
- duplicate_key: two positions with same (symbol, side) or two orders with same order_id
- ts_regression: snapshot.ts < last accepted snapshot.ts
- negative_qty: position qty < 0 or order qty < 0
- orphan_order: order on exchange not tracked by ExecutionEngine
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from grinder.account.metrics import get_account_sync_metrics

if TYPE_CHECKING:
    from grinder.account.contracts import AccountSnapshot
    from grinder.execution.port import ExchangePort

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Mismatch:
    """A detected mismatch between exchange and internal state.

    Attributes:
        rule: Mismatch rule name (duplicate_key, ts_regression, negative_qty, orphan_order).
        detail: Human-readable description.
    """

    rule: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        """Serialize to JSON-compatible dict."""
        return {"rule": self.rule, "detail": self.detail}


@dataclass
class SyncResult:
    """Result of a single sync cycle.

    Attributes:
        snapshot: The fetched AccountSnapshot (None if fetch failed).
        mismatches: Detected mismatches (empty = clean sync).
        error: Error message if fetch failed.
    """

    snapshot: AccountSnapshot | None = None
    mismatches: list[Mismatch] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True if sync succeeded with no mismatches."""
        return self.snapshot is not None and not self.mismatches and self.error is None


class AccountSyncer:
    """Read-only account syncer with mismatch detection.

    Fetches AccountSnapshot from ExchangePort, validates invariants,
    detects mismatches, and records metrics.

    Thread safety: NOT thread-safe. Use one instance per sync loop.

    Args:
        port: ExchangePort to fetch snapshots from.
    """

    # ADR-108: Bounded ts_regression tolerance.
    # Binance can return cached snapshots with slightly older timestamps.
    # Small regressions are tolerated after repeated occurrences to prevent
    # indefinitely starving the reconciler.
    TS_REGRESSION_TOLERANCE_MS: int = 10_000  # max tolerable regression delta
    TS_REGRESSION_ACCEPT_AFTER: int = 3  # accept after N consecutive regressions

    def __init__(self, port: ExchangePort) -> None:
        """Initialize AccountSyncer.

        Args:
            port: ExchangePort for fetching account state.
        """
        self._port = port
        self._last_ts: int = 0
        self._ts_regression_streak: int = 0  # consecutive ts_regression count

    @property
    def last_ts(self) -> int:
        """Timestamp of last accepted snapshot (0 = never synced)."""
        return self._last_ts

    def sync(self, known_order_ids: frozenset[str] | None = None) -> SyncResult:
        """Perform one sync cycle: fetch, validate, detect mismatches.

        Args:
            known_order_ids: Order IDs tracked by ExecutionEngine.
                If provided, orders on exchange not in this set are flagged
                as orphan_order mismatches. If None, orphan check is skipped.

        Returns:
            SyncResult with snapshot and any detected mismatches.
        """
        metrics = get_account_sync_metrics()

        # Step 1: Fetch snapshot
        try:
            snapshot = self._port.fetch_account_snapshot()
        except Exception as exc:
            reason = type(exc).__name__
            logger.warning("Account sync fetch failed: %s: %s", reason, exc)
            metrics.record_error(reason)
            return SyncResult(error=f"{reason}: {exc}")

        # Step 2: Validate + detect mismatches
        mismatches = self._detect_mismatches(snapshot, known_order_ids)

        # Step 3: Record metrics
        for m in mismatches:
            metrics.record_mismatch(m.rule)

        pending_notional = self._compute_pending_notional(snapshot)
        metrics.record_sync(
            ts=snapshot.ts,
            positions=len(snapshot.positions),
            open_orders=len(snapshot.open_orders),
            pending_notional=pending_notional,
        )

        # Step 4: Update last_ts (only if no ts_regression)
        if not any(m.rule == "ts_regression" for m in mismatches):
            self._last_ts = snapshot.ts

        return SyncResult(snapshot=snapshot, mismatches=mismatches)

    def _detect_mismatches(  # noqa: PLR0912
        self,
        snapshot: AccountSnapshot,
        known_order_ids: frozenset[str] | None,
    ) -> list[Mismatch]:
        """Run all mismatch rules against snapshot.

        Rules:
        - duplicate_key: position (symbol, side) or order_id uniqueness
        - ts_regression: snapshot.ts < last accepted ts (non-empty snapshots only;
          empty snapshots with ts=0 are a normal transition, not a regression)
        - negative_qty: position/order qty < 0
        - orphan_order: exchange order not in known_order_ids
        """
        mismatches: list[Mismatch] = []

        # Check ts_regression -- invariant I5 + ADR-108 bounded tolerance.
        # Empty snapshots (ts=0) are exempt (normal flat transition).
        # Small regressions from Binance cached snapshots are tolerated after
        # TS_REGRESSION_ACCEPT_AFTER consecutive occurrences to prevent
        # indefinitely starving downstream consumers (reconciler).
        is_empty = len(snapshot.positions) == 0 and len(snapshot.open_orders) == 0
        if self._last_ts > 0 and snapshot.ts < self._last_ts and not is_empty:
            delta_ms = self._last_ts - snapshot.ts
            if delta_ms > self.TS_REGRESSION_TOLERANCE_MS:
                # Large regression: always hard-block
                mismatches.append(
                    Mismatch(
                        rule="ts_regression",
                        detail=f"snapshot.ts={snapshot.ts} < last_ts={self._last_ts} "
                        f"delta_ms={delta_ms} BLOCKED (exceeds tolerance)",
                    )
                )
                self._ts_regression_streak += 1
            elif self._ts_regression_streak < self.TS_REGRESSION_ACCEPT_AFTER:
                # Small regression, not yet at accept threshold: warn + count
                mismatches.append(
                    Mismatch(
                        rule="ts_regression",
                        detail=f"snapshot.ts={snapshot.ts} < last_ts={self._last_ts} "
                        f"delta_ms={delta_ms} streak={self._ts_regression_streak + 1}"
                        f"/{self.TS_REGRESSION_ACCEPT_AFTER}",
                    )
                )
                self._ts_regression_streak += 1
            else:
                # Small regression, streak >= threshold: tolerate + advance last_ts.
                # This breaks the freeze loop from Binance cached snapshots.
                logger.warning(
                    "ACCOUNT_SYNC_TS_REGRESSION_TOLERATED "
                    "snapshot_ts=%d last_ts=%d delta_ms=%d streak=%d "
                    "action=accepting_to_unblock_reconciler",
                    snapshot.ts,
                    self._last_ts,
                    delta_ms,
                    self._ts_regression_streak,
                )
                # Do NOT append mismatch — accept the snapshot.
                # last_ts will be advanced in step 4 below.
                self._ts_regression_streak = 0  # reset streak
        else:
            # No regression or empty snapshot: reset streak
            self._ts_regression_streak = 0

        # Rule: duplicate_key for positions (I6)
        pos_keys: set[tuple[str, str]] = set()
        for p in snapshot.positions:
            key = (p.symbol, p.side)
            if key in pos_keys:
                mismatches.append(
                    Mismatch(
                        rule="duplicate_key",
                        detail=f"duplicate position key: {key}",
                    )
                )
            pos_keys.add(key)

        # Rule: duplicate_key for orders (I6)
        order_ids: set[str] = set()
        for o in snapshot.open_orders:
            if o.order_id in order_ids:
                mismatches.append(
                    Mismatch(
                        rule="duplicate_key",
                        detail=f"duplicate order_id: {o.order_id}",
                    )
                )
            order_ids.add(o.order_id)

        # Check negative_qty
        for p in snapshot.positions:
            if p.qty < 0:
                mismatches.append(
                    Mismatch(
                        rule="negative_qty",
                        detail=f"position {p.symbol}/{p.side} qty={p.qty}",
                    )
                )
        for o in snapshot.open_orders:
            if o.qty < 0:
                mismatches.append(
                    Mismatch(
                        rule="negative_qty",
                        detail=f"order {o.order_id} qty={o.qty}",
                    )
                )

        # Rule: orphan_order (if known_order_ids provided)
        if known_order_ids is not None:
            for o in snapshot.open_orders:
                if o.order_id not in known_order_ids:
                    mismatches.append(
                        Mismatch(
                            rule="orphan_order",
                            detail=f"order {o.order_id} not tracked by ExecutionEngine",
                        )
                    )

        return mismatches

    @staticmethod
    def _compute_pending_notional(snapshot: AccountSnapshot) -> float:
        """Compute total notional value of open orders (price * remaining_qty)."""
        total = Decimal(0)
        for o in snapshot.open_orders:
            remaining = o.qty - o.filled_qty
            if remaining > 0:
                total += o.price * remaining
        return float(total)

    @staticmethod
    def compute_position_notional(snapshot: AccountSnapshot) -> float:
        """Total position notional: sum(|qty| * mark_price) in USDT.

        Defensive abs() even though PositionSnap.qty contract says >= 0.
        Returns 0.0 for empty positions tuple.
        """
        total = Decimal(0)
        for p in snapshot.positions:
            total += abs(p.qty) * p.mark_price
        return float(total)

    def reset(self) -> None:
        """Reset syncer state (for testing)."""
        self._last_ts = 0
