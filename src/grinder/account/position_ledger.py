"""PositionLedger from ACCOUNT_UPDATE events (Phase 3).

Tracks position state from user-data WebSocket events. Compared against
AccountSnapshot positions on each sync for divergence visibility.

Phase 3 PR-1: shadow only.
Phase 3 PR-2: trusted read model with snapshot fallback.

Keyed by (symbol, position_side) to support both one-way and hedge modes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.account.contracts import AccountSnapshot
    from grinder.execution.futures_events import FuturesPositionEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LedgerPosition:
    """Position state from events."""

    symbol: str
    position_side: str  # BOTH, LONG, SHORT
    position_amt: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    last_event_ts: int


class PositionDivergenceKind(Enum):
    POSITION_MISSING_IN_LEDGER = "POSITION_MISSING_IN_LEDGER"
    POSITION_MISSING_IN_SNAPSHOT = "POSITION_MISSING_IN_SNAPSHOT"
    POSITION_AMT_MISMATCH = "POSITION_AMT_MISMATCH"


@dataclass(frozen=True)
class PositionDivergence:
    """Single divergence between ledger and snapshot."""

    kind: PositionDivergenceKind
    symbol: str
    position_side: str
    detail: str = ""


@dataclass(frozen=True)
class PositionComparisonResult:
    """Result of comparing PositionLedger against AccountSnapshot."""

    divergences: tuple[PositionDivergence, ...]
    ledger_count: int
    snapshot_count: int

    @property
    def is_converged(self) -> bool:
        return len(self.divergences) == 0


class PositionLedger:
    """Position read model from ACCOUNT_UPDATE events.

    Trust state machine mirrors EventLedger (ADR-109 Phase 2):
    - bootstrapped: at least one non-zero position event received
    - last_convergence_ok: last comparison with snapshot converged
    - trust_revoked: explicitly revoked on divergence or degraded mode

    is_trusted = bootstrapped AND last_convergence_ok AND NOT trust_revoked
    """

    def __init__(self) -> None:
        self._positions: dict[tuple[str, str], LedgerPosition] = {}
        self._stale_event_count = 0
        # Trust state
        self._bootstrapped = False
        self._last_convergence_ok = False
        self._trust_revoked = False

    @property
    def is_trusted(self) -> bool:
        """True when ledger can be used as position read model."""
        return self._bootstrapped and self._last_convergence_ok and not self._trust_revoked

    @property
    def trust_revoked(self) -> bool:
        return self._trust_revoked

    def apply_position_event(self, event: FuturesPositionEvent) -> None:
        """Apply a position event. Stale events (older ts) are suppressed."""
        key = (event.symbol, event.position_side)
        existing = self._positions.get(key)
        if existing is not None and event.ts <= existing.last_event_ts:
            self._stale_event_count += 1
            logger.info(
                "POSITION_LEDGER_EVENT_DROPPED_STALE symbol=%s side=%s "
                "incoming_amt=%s incoming_ts=%d prev_amt=%s prev_ts=%d "
                "reason=stale_event_guard source=user_data_position",
                event.symbol,
                event.position_side,
                event.position_amt,
                event.ts,
                existing.position_amt,
                existing.last_event_ts,
            )
            return
        prev_amt = existing.position_amt if existing is not None else None
        prev_ts = existing.last_event_ts if existing is not None else None
        self._positions[key] = LedgerPosition(
            symbol=event.symbol,
            position_side=event.position_side,
            position_amt=event.position_amt,
            entry_price=event.entry_price,
            unrealized_pnl=event.unrealized_pnl,
            last_event_ts=event.ts,
        )
        logger.info(
            "POSITION_LEDGER_EVENT_APPLIED symbol=%s side=%s "
            "incoming_amt=%s incoming_ts=%d prev_amt=%s prev_ts=%s "
            "new_amt=%s new_ts=%d source=user_data_position",
            event.symbol,
            event.position_side,
            event.position_amt,
            event.ts,
            prev_amt,
            prev_ts,
            event.position_amt,
            event.ts,
        )
        # Bootstrap on first non-zero position event
        if event.position_amt != 0 and not self._bootstrapped:
            self._bootstrapped = True

    def record_comparison_result(self, converged: bool) -> None:
        """Update trust state based on comparison result.

        Called by engine after compare_with_snapshot() on each sync.
        Convergence restores trust; divergence revokes it immediately.
        """
        self._last_convergence_ok = converged
        if converged:
            if self._trust_revoked and self._bootstrapped:
                self._trust_revoked = False
                logger.info("POSITION_LEDGER_TRUST_RESTORED")
        else:
            self.revoke_trust("divergence")

    def revoke_trust(self, reason: str) -> None:
        """Revoke trust. Idempotent — logs only on first revoke."""
        if not self._trust_revoked:
            self._trust_revoked = True
            logger.info("POSITION_LEDGER_TRUST_REVOKED reason=%s", reason)

    def get_signed_qty(self, symbol: str, position_side: str = "BOTH") -> Decimal:
        """Get position amount from ledger. Returns 0 if missing."""
        lp = self._positions.get((symbol, position_side))
        if lp is None:
            return Decimal("0")
        return lp.position_amt

    def hydrate_from_snapshot(self, snapshot: AccountSnapshot) -> int:
        """Bootstrap/refresh ledger from snapshot positions.

        Populates the ledger with non-zero positions from the snapshot
        that are not already present. Idempotent — existing entries with
        a newer event_ts are not overwritten.

        Sets _bootstrapped = True once at least one non-zero position is
        present (from hydration or from WS events).

        Called on every sync cycle (mirrors EventLedger pattern).
        Returns the number of positions hydrated.
        """
        hydrated = 0
        for pos in snapshot.positions:
            qty = pos.signed_qty if pos.signed_qty is not None else pos.qty
            if qty == 0:
                continue
            key = (pos.symbol, pos.side)
            existing = self._positions.get(key)
            # Don't overwrite if ledger already has same or newer event.
            # Use pos.ts (per-position fetch time), not snapshot.ts which is
            # max(positions_ts, orders_ts) and can falsely suppress WS events.
            if existing is not None and existing.last_event_ts >= pos.ts:
                continue
            self._positions[key] = LedgerPosition(
                symbol=pos.symbol,
                position_side=pos.side,
                position_amt=qty,
                entry_price=pos.entry_price,
                unrealized_pnl=pos.unrealized_pnl,
                last_event_ts=pos.ts,
            )
            logger.info(
                "POSITION_LEDGER_HYDRATE_APPLIED symbol=%s side=%s "
                "amt=%s ts=%d source=snapshot_hydration",
                pos.symbol,
                pos.side,
                qty,
                pos.ts,
            )
            hydrated += 1
        if hydrated > 0 and not self._bootstrapped:
            self._bootstrapped = True
        return hydrated

    def compare_with_snapshot(self, snapshot: AccountSnapshot) -> PositionComparisonResult:
        """Compare ledger positions against snapshot positions.

        Only compares position_amt (the most critical field for correctness).
        """
        divergences: list[PositionDivergence] = []

        # Build snapshot position map: (symbol, side) → signed_qty
        snap_positions: dict[tuple[str, str], Decimal] = {}
        for pos in snapshot.positions:
            qty = pos.signed_qty if pos.signed_qty is not None else pos.qty
            if qty != 0:
                snap_positions[(pos.symbol, pos.side)] = qty

        # Check ledger positions against snapshot
        for key, lp in sorted(self._positions.items()):
            if lp.position_amt == 0:
                continue  # flat in ledger — skip
            snap_qty = snap_positions.pop(key, None)
            if snap_qty is None:
                divergences.append(
                    PositionDivergence(
                        kind=PositionDivergenceKind.POSITION_MISSING_IN_SNAPSHOT,
                        symbol=lp.symbol,
                        position_side=lp.position_side,
                        detail=f"ledger_amt={lp.position_amt}",
                    )
                )
            elif lp.position_amt != snap_qty:
                divergences.append(
                    PositionDivergence(
                        kind=PositionDivergenceKind.POSITION_AMT_MISMATCH,
                        symbol=lp.symbol,
                        position_side=lp.position_side,
                        detail=f"ledger={lp.position_amt} snapshot={snap_qty}",
                    )
                )

        # Remaining snapshot positions not in ledger
        for (sym, side), qty in sorted(snap_positions.items()):
            divergences.append(
                PositionDivergence(
                    kind=PositionDivergenceKind.POSITION_MISSING_IN_LEDGER,
                    symbol=sym,
                    position_side=side,
                    detail=f"snapshot_amt={qty}",
                )
            )

        non_flat_ledger = sum(1 for lp in self._positions.values() if lp.position_amt != 0)
        non_flat_snap = sum(
            1
            for p in snapshot.positions
            if (p.signed_qty if p.signed_qty is not None else p.qty) != 0
        )

        return PositionComparisonResult(
            divergences=tuple(divergences),
            ledger_count=non_flat_ledger,
            snapshot_count=non_flat_snap,
        )

    def positions(self) -> dict[tuple[str, str], LedgerPosition]:
        """Return current position state (read-only copy)."""
        return dict(self._positions)

    def reset(self) -> None:
        """Clear all state including trust."""
        self._positions.clear()
        self._stale_event_count = 0
        self._bootstrapped = False
        self._last_convergence_ok = False
        self._trust_revoked = False
