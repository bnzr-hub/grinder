"""Shadow PositionLedger from ACCOUNT_UPDATE events (Phase 3 PR-1).

Tracks position state from user-data WebSocket events. Shadow-only:
does not affect any trading decision. Compared against AccountSnapshot
positions on each sync for divergence visibility.

Keyed by (symbol, position_side) to support both one-way and hedge modes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

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
    """Shadow position read model from ACCOUNT_UPDATE events.

    Phase 3 PR-1: shadow/observability only. Does not affect any trading
    decision, risk gate, or position query. Compared against REST snapshot
    positions for divergence logging.
    """

    def __init__(self) -> None:
        self._positions: dict[tuple[str, str], LedgerPosition] = {}
        self._stale_event_count = 0

    def apply_position_event(self, event: FuturesPositionEvent) -> None:
        """Apply a position event. Stale events (older ts) are suppressed."""
        key = (event.symbol, event.position_side)
        existing = self._positions.get(key)
        if existing is not None and event.ts <= existing.last_event_ts:
            self._stale_event_count += 1
            return
        self._positions[key] = LedgerPosition(
            symbol=event.symbol,
            position_side=event.position_side,
            position_amt=event.position_amt,
            entry_price=event.entry_price,
            unrealized_pnl=event.unrealized_pnl,
            last_event_ts=event.ts,
        )

    def compare_with_snapshot(self, snapshot: AccountSnapshot) -> PositionComparisonResult:
        """Compare ledger positions against snapshot positions.

        Only compares position_amt (the most critical field for correctness).
        """
        divergences: list[PositionDivergence] = []

        # Build snapshot position map: (symbol, side) → signed_qty
        snap_positions: dict[tuple[str, str], Decimal] = {}
        for pos in snapshot.positions:
            if pos.signed_qty != 0:
                snap_positions[(pos.symbol, pos.side)] = pos.signed_qty

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
        non_flat_snap = sum(1 for p in snapshot.positions if p.signed_qty != 0)

        return PositionComparisonResult(
            divergences=tuple(divergences),
            ledger_count=non_flat_ledger,
            snapshot_count=non_flat_snap,
        )

    def positions(self) -> dict[tuple[str, str], LedgerPosition]:
        """Return current position state (read-only copy)."""
        return dict(self._positions)

    def reset(self) -> None:
        """Clear all state."""
        self._positions.clear()
        self._stale_event_count = 0
