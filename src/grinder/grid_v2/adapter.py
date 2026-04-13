"""Grid V2 reconciliation adapter (doc-27 section 22).

Pure mapping layer between exchange facts and grid_v2 domain events.
No I/O, no exchange calls, no remediation. Detection only.

Core invariant: translate_fill() and resolve_actions(CANCEL_*) never mutate
the registry. All removal is explicit via confirm_* or reconstruct_snapshot().
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from grinder.core import OrderSide

if TYPE_CHECKING:
    from collections.abc import Sequence
from grinder.grid_v2.state import (
    ActionIntent,
    ActionIntentKind,
    BranchMode,
    EntryFilled,
    EntryWindow,
    ExitFilled,
    ExitOrder,
    ExitOrderStatus,
    GridV2Config,
    GridV2Snapshot,
    GridV2StateMachine,
    InventoryLot,
    LotSide,
    LotStatus,
)
from grinder.reconcile.identity import (
    BINANCE_MAX_CLIENT_ORDER_ID_LEN,
    OrderIdentityConfig,
    generate_client_order_id,
    is_ours,
    normalize_symbol_for_cid,
    parse_client_order_id,
)

# Strategy and CID constants (section 22.1)

GRID_V2_STRATEGY_ID = "g"
_ENTRY_PREFIX = "e"
_EXIT_PREFIX = "x"

# Fixed CID overhead: g_ (2) + g (1) + 4 separators (4) + ts_sec (10) + seq (1)
_CID_FIXED_OVERHEAD = 18


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class GridV2OrderKind(Enum):
    """Whether a grid_v2 CID represents an entry or exit order."""

    ENTRY = "ENTRY"
    EXIT = "EXIT"


class GridV2MismatchKind(Enum):
    """Types of reconciliation mismatches (22.6)."""

    ENTRY_MISSING_ON_EXCHANGE = "ENTRY_MISSING_ON_EXCHANGE"
    EXIT_MISSING_ON_EXCHANGE = "EXIT_MISSING_ON_EXCHANGE"
    UNEXPECTED_ORDER = "UNEXPECTED_ORDER"


class MismatchSeverity(Enum):
    """Severity levels for mismatches."""

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"


# ---------------------------------------------------------------------------
# Data classes (all frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedGridV2Cid:
    """Parsed grid_v2 CID with entry/exit classification."""

    kind: GridV2OrderKind
    raw_cid: str
    symbol: str
    index: int
    ts: int
    seq: int


@dataclass(frozen=True)
class EntryRegistration:
    """Registry entry for an active entry order."""

    cid: str
    side: OrderSide
    price: Decimal


@dataclass(frozen=True)
class ExitRegistration:
    """Registry entry for an active exit order."""

    cid: str
    exit_order_id: str
    lot_id: str


@dataclass(frozen=True)
class GridV2Mismatch:
    """A detected reconciliation mismatch (22.6)."""

    kind: GridV2MismatchKind
    severity: MismatchSeverity
    detail: str
    cid: str | None = None
    lot_id: str | None = None


@dataclass(frozen=True)
class TranslatedFill:
    """Result of translating an exchange fill to a state machine event (22.4)."""

    event: EntryFilled | ExitFilled
    source_cid: str


@dataclass(frozen=True)
class ResolvedAction:
    """Result of resolving an ActionIntent to CID-bearing order parameters (22.5)."""

    cid: str
    kind: ActionIntentKind
    side: OrderSide
    price: Decimal
    qty: Decimal | None = None


@dataclass(frozen=True)
class PartialResolveResult:
    """Result of fault-tolerant action resolution."""

    actions: tuple[ResolvedAction, ...]
    skipped_count: int
    skip_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ReconciliationResult:
    """Result of comparing expected vs observed state (22.6)."""

    mismatches: tuple[GridV2Mismatch, ...]
    matched_entries: int
    matched_exits: int
    ts: int


# ---------------------------------------------------------------------------
# Order registry (22.2, 22.3)
# ---------------------------------------------------------------------------


class GridV2OrderRegistry:
    """Bidirectional CID <-> state machine entity mapping. No I/O."""

    def __init__(self) -> None:
        self._entries: dict[str, EntryRegistration] = {}
        self._exits: dict[str, ExitRegistration] = {}
        self._entry_by_side_price: dict[tuple[OrderSide, Decimal], str] = {}
        self._exit_by_exit_id: dict[str, str] = {}
        self._stale_entries: dict[str, EntryRegistration] = {}
        self._stale_exits: dict[str, ExitRegistration] = {}

    def register_entry(self, cid: str, side: OrderSide, price: Decimal) -> bool:
        """Register a new entry order. Returns True on success, False on skip.

        Duplicate CID: raises ValueError (contract violation).
        Duplicate (side, price): returns False (graceful skip for race safety).
        """
        if cid in self._entries:
            raise ValueError(f"Duplicate entry CID: {cid}")
        if (side, price) in self._entry_by_side_price:
            return False  # slot occupied by another CID — skip gracefully
        reg = EntryRegistration(cid=cid, side=side, price=price)
        self._entries[cid] = reg
        self._entry_by_side_price[(side, price)] = cid
        return True

    def register_exit(self, cid: str, exit_order_id: str, lot_id: str) -> None:
        """Register a new exit order.

        Raises ValueError on duplicate CID or duplicate exit_order_id.
        """
        if cid in self._exits:
            raise ValueError(f"Duplicate exit CID: {cid}")
        if exit_order_id in self._exit_by_exit_id:
            raise ValueError(
                f"Duplicate exit_order_id {exit_order_id!r}: "
                f"existing CID={self._exit_by_exit_id[exit_order_id]}"
            )
        reg = ExitRegistration(cid=cid, exit_order_id=exit_order_id, lot_id=lot_id)
        self._exits[cid] = reg
        self._exit_by_exit_id[exit_order_id] = cid

    def lookup_entry(self, cid: str) -> EntryRegistration | None:
        return self._entries.get(cid)

    def lookup_stale_entry(self, cid: str) -> EntryRegistration | None:
        """Lookup a recently-canceled entry CID for late fill translation."""
        return self._stale_entries.get(cid)

    def lookup_exit(self, cid: str) -> ExitRegistration | None:
        return self._exits.get(cid)

    def cid_for_entry(self, side: OrderSide, price: Decimal) -> str | None:
        """Reverse lookup: find CID for entry at (side, price)."""
        return self._entry_by_side_price.get((side, price))

    def cid_for_exit(self, exit_order_id: str) -> str | None:
        """Reverse lookup: find CID for exit by state machine exit_order_id."""
        return self._exit_by_exit_id.get(exit_order_id)

    def remove_entry(self, cid: str) -> EntryRegistration | None:
        """Remove entry registration. Returns removed reg or None."""
        reg = self._entries.pop(cid, None)
        if reg is not None:
            self._entry_by_side_price.pop((reg.side, reg.price), None)
            self._stale_entries.pop(cid, None)
            return reg
        return self._stale_entries.pop(cid, None)

    def mark_entry_cancel_pending(self, cid: str) -> EntryRegistration | None:
        """Move active entry to stale cache so late fills can still translate."""
        reg = self._entries.pop(cid, None)
        if reg is None:
            return None
        self._entry_by_side_price.pop((reg.side, reg.price), None)
        self._stale_entries[cid] = reg
        return reg

    def remove_exit(self, cid: str) -> ExitRegistration | None:
        """Remove exit registration. Returns removed reg or None."""
        reg = self._exits.pop(cid, None)
        if reg is not None:
            self._exit_by_exit_id.pop(reg.exit_order_id, None)
            self._stale_exits.pop(cid, None)
            return reg
        return self._stale_exits.pop(cid, None)

    def lookup_stale_exit(self, cid: str) -> ExitRegistration | None:
        """Lookup a recently-canceled exit CID for late fill translation."""
        return self._stale_exits.get(cid)

    def mark_exit_cancel_pending(self, cid: str) -> ExitRegistration | None:
        """Move active exit to stale cache so late fills can still translate."""
        reg = self._exits.pop(cid, None)
        if reg is None:
            return None
        self._exit_by_exit_id.pop(reg.exit_order_id, None)
        self._stale_exits[cid] = reg
        return reg

    def clear(self) -> None:
        """Full reset (used by reconstruct_snapshot)."""
        self._entries.clear()
        self._exits.clear()
        self._entry_by_side_price.clear()
        self._exit_by_exit_id.clear()
        self._stale_entries.clear()
        self._stale_exits.clear()

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def exit_count(self) -> int:
        return len(self._exits)

    @property
    def all_entry_cids(self) -> frozenset[str]:
        return frozenset(self._entries)

    @property
    def all_exit_cids(self) -> frozenset[str]:
        return frozenset(self._exits)


# Adapter and helpers (sections 22.1 through 22.11)


def _compute_max_seq(symbol: str) -> int:
    """Compute max sequence number for CID given symbol length.

    Budget: symbol + level_id must fit within 36 - fixed_overhead chars.
    level_id = prefix (1 char for e/x) + digits.
    """
    max_level_id_len = BINANCE_MAX_CLIENT_ORDER_ID_LEN - _CID_FIXED_OVERHEAD - len(symbol)
    if max_level_id_len < 2:
        raise ValueError(f"Symbol {symbol!r} (len={len(symbol)}) too long for grid_v2 CID")
    max_digits = max_level_id_len - 1  # subtract 1 for e/x prefix
    return int(10**max_digits - 1)


class GridV2Adapter:
    """Translates between exchange facts and grid_v2 domain events.

    Pure mapping layer. No I/O. Symbol-scoped (one adapter per symbol).

    Core invariant: translate_fill() and resolve_actions(CANCEL_*) never
    mutate the registry. All removal is explicit via confirm_* methods
    or reconstruct_snapshot().
    """

    def __init__(
        self,
        config: GridV2Config,
        symbol: str,
        strategy_id: str = GRID_V2_STRATEGY_ID,
    ) -> None:
        self._config = config
        self._symbol = symbol
        self._strategy_id = strategy_id
        # Short prefix "g" instead of "grinder_" to fit longer symbols
        # within Binance 36-char CID limit. Format: g_g_{symbol}_{level}_{ts}_{seq}
        self._identity_config = OrderIdentityConfig(strategy_id=strategy_id, prefix="g_")
        self._registry = GridV2OrderRegistry()
        self._entry_seq = 0
        self._exit_seq = 0
        self._max_seq = _compute_max_seq(symbol)
        self._f2_protective_recovery = False

    @property
    def registry(self) -> GridV2OrderRegistry:
        return self._registry

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def f2_protective_recovery(self) -> bool:
        """True if startup recovered from F2 (non-flat, no exits)."""
        return self._f2_protective_recovery

    # --- CID helpers (22.1) ---

    def generate_entry_cid(self, ts_ms: int) -> str:
        """Generate entry CID. Auto-increments _entry_seq."""
        if self._entry_seq > self._max_seq:
            raise ValueError(f"Entry seq overflow: {self._entry_seq} > {self._max_seq}")
        cid = generate_client_order_id(
            self._identity_config,
            self._symbol,
            f"{_ENTRY_PREFIX}{self._entry_seq}",
            ts_ms,
            0,
        )
        self._entry_seq += 1
        return cid

    def generate_exit_cid(self, ts_ms: int) -> str:
        """Generate exit CID. Auto-increments _exit_seq."""
        if self._exit_seq > self._max_seq:
            raise ValueError(f"Exit seq overflow: {self._exit_seq} > {self._max_seq}")
        cid = generate_client_order_id(
            self._identity_config,
            self._symbol,
            f"{_EXIT_PREFIX}{self._exit_seq}",
            ts_ms,
            0,
        )
        self._exit_seq += 1
        return cid

    def parse_cid(self, cid: str) -> ParsedGridV2Cid | None:  # noqa: PLR0911
        """Parse a CID into grid_v2 classification. None if not ours.

        Symbol-scoped: CIDs for other symbols are treated as foreign (returns None).
        """
        if not is_ours(cid, self._identity_config):
            return None
        parsed = parse_client_order_id(cid)
        if parsed is None:
            return None
        if not normalize_symbol_for_cid(self._symbol).startswith(parsed.symbol):
            return None
        lid = parsed.level_id
        if lid.startswith(_ENTRY_PREFIX):
            kind = GridV2OrderKind.ENTRY
            try:
                index = int(lid[len(_ENTRY_PREFIX) :])
            except ValueError:
                return None
        elif lid.startswith(_EXIT_PREFIX):
            kind = GridV2OrderKind.EXIT
            try:
                index = int(lid[len(_EXIT_PREFIX) :])
            except ValueError:
                return None
        else:
            return None
        return ParsedGridV2Cid(
            kind=kind,
            raw_cid=cid,
            symbol=parsed.symbol,
            index=index,
            ts=parsed.ts,
            seq=parsed.seq,
        )

    def is_ours(self, cid: str) -> bool:
        """Check if CID belongs to this adapter (strategy + symbol match)."""
        if not is_ours(cid, self._identity_config):
            return False
        parsed = parse_client_order_id(cid)
        return parsed is not None and normalize_symbol_for_cid(self._symbol).startswith(
            parsed.symbol
        )

    # --- Fill translation (22.4, read-only on registry) ---

    def translate_fill(
        self,
        client_order_id: str,
        side: OrderSide,
        price: Decimal,
        qty: Decimal,
        ts: int,
    ) -> TranslatedFill | None:
        """Translate an exchange fill into a state machine event.

        Returns None if CID is not ours (foreign order).
        Raises ValueError if CID is ours but not in registry (stale fill).
        Does NOT mutate the registry (22.3).
        """
        parsed = self.parse_cid(client_order_id)
        if parsed is None:
            return None

        if parsed.kind == GridV2OrderKind.ENTRY:
            entry_reg = self._registry.lookup_entry(client_order_id)
            if entry_reg is None:
                entry_reg = self._registry.lookup_stale_entry(client_order_id)
            if entry_reg is None:
                raise ValueError(f"Entry CID not in registry (stale?): {client_order_id}")
            event = EntryFilled(
                order_id=client_order_id,
                side=side,
                price=entry_reg.price,
                qty=qty,
                ts=ts,
            )
            return TranslatedFill(event=event, source_cid=client_order_id)

        # EXIT
        exit_reg = self._registry.lookup_exit(client_order_id)
        if exit_reg is None:
            exit_reg = self._registry.lookup_stale_exit(client_order_id)
        if exit_reg is None:
            raise ValueError(f"Exit CID not in registry (stale?): {client_order_id}")
        exit_event = ExitFilled(
            exit_order_id=exit_reg.exit_order_id,
            lot_id=exit_reg.lot_id,
            price=price,
            qty=qty,
            ts=ts,
        )
        return TranslatedFill(event=exit_event, source_cid=client_order_id)

    # --- Action resolution (22.5) ---

    def resolve_actions(
        self,
        actions: tuple[ActionIntent, ...],
        ts_ms: int,
    ) -> tuple[ResolvedAction, ...]:
        """Resolve ActionIntents to CID-bearing order requests.

        PLACE_ENTRY/PLACE_EXIT: generates new CID, registers in registry.
        CANCEL_ENTRY/CANCEL_EXIT: looks up CID (no removal per 22.3).
        CANCEL actions also release the registry slot immediately so a
        same-batch replacement PLACE_* can re-register at the same price.
        Raises ValueError for cancel of unregistered entry/exit.
        """
        resolved: list[ResolvedAction] = []

        for action in actions:
            if action.kind == ActionIntentKind.PLACE_ENTRY:
                if action.side is None or action.price is None or action.qty is None:
                    raise ValueError(f"PLACE_ENTRY missing fields: {action}")
                cid = self.generate_entry_cid(ts_ms)
                registered = self._registry.register_entry(cid, action.side, action.price)
                if registered:
                    resolved.append(
                        ResolvedAction(
                            cid=cid,
                            kind=action.kind,
                            side=action.side,
                            price=action.price,
                            qty=action.qty,
                        )
                    )

            elif action.kind == ActionIntentKind.PLACE_EXIT:
                if (
                    action.side is None
                    or action.price is None
                    or action.qty is None
                    or action.lot_id is None
                ):
                    raise ValueError(f"PLACE_EXIT missing fields: {action}")
                cid = self.generate_exit_cid(ts_ms)
                entry_order_id = action.lot_id.removeprefix("lot-")
                exit_order_id = f"exit-{entry_order_id}"
                self._registry.register_exit(cid, exit_order_id=exit_order_id, lot_id=action.lot_id)
                resolved.append(
                    ResolvedAction(
                        cid=cid,
                        kind=action.kind,
                        side=action.side,
                        price=action.price,
                        qty=action.qty,
                    )
                )

            elif action.kind == ActionIntentKind.CANCEL_ENTRY:
                if action.side is None or action.price is None:
                    raise ValueError(f"CANCEL_ENTRY missing fields: {action}")
                found_cid = self._registry.cid_for_entry(action.side, action.price)
                if found_cid is None:
                    raise ValueError(
                        f"No registered entry CID for CANCEL_ENTRY: "
                        f"side={action.side.value}, price={action.price}"
                    )
                self._registry.mark_entry_cancel_pending(found_cid)
                resolved.append(
                    ResolvedAction(
                        cid=found_cid,
                        kind=action.kind,
                        side=action.side,
                        price=action.price,
                    )
                )

            elif action.kind == ActionIntentKind.CANCEL_EXIT:
                if action.order_id is None:
                    raise ValueError(f"CANCEL_EXIT missing order_id: {action}")
                found_cid = self._registry.cid_for_exit(action.order_id)
                if found_cid is None:
                    raise ValueError(
                        f"No registered exit CID for CANCEL_EXIT: exit_order_id={action.order_id}"
                    )
                self._registry.mark_exit_cancel_pending(found_cid)
                resolved.append(
                    ResolvedAction(
                        cid=found_cid,
                        kind=action.kind,
                        side=action.side or OrderSide.BUY,
                        price=action.price or Decimal(0),
                    )
                )

        return tuple(resolved)

    def resolve_actions_partial(  # noqa: PLR0912
        self,
        actions: tuple[ActionIntent, ...],
        ts_ms: int,
    ) -> PartialResolveResult:
        """Resolve actions fault-tolerantly: skip unresolvable CANCELs.

        Like resolve_actions, but missing CANCEL CIDs are skipped instead
        of raising ValueError. PLACE actions are always resolved. This
        prevents phantom lots when a CANCEL fails but the paired PLACE
        should still go through.
        """
        resolved: list[ResolvedAction] = []
        skipped = 0
        skip_reasons: list[str] = []

        for action in actions:
            try:
                if action.kind == ActionIntentKind.PLACE_ENTRY:
                    if action.side is None or action.price is None or action.qty is None:
                        raise ValueError(f"PLACE_ENTRY missing fields: {action}")
                    cid = self.generate_entry_cid(ts_ms)
                    registered = self._registry.register_entry(cid, action.side, action.price)
                    if registered:
                        resolved.append(
                            ResolvedAction(
                                cid=cid,
                                kind=action.kind,
                                side=action.side,
                                price=action.price,
                                qty=action.qty,
                            )
                        )

                elif action.kind == ActionIntentKind.PLACE_EXIT:
                    if (
                        action.side is None
                        or action.price is None
                        or action.qty is None
                        or action.lot_id is None
                    ):
                        raise ValueError(f"PLACE_EXIT missing fields: {action}")
                    cid = self.generate_exit_cid(ts_ms)
                    entry_order_id = action.lot_id.removeprefix("lot-")
                    exit_order_id = f"exit-{entry_order_id}"
                    self._registry.register_exit(
                        cid, exit_order_id=exit_order_id, lot_id=action.lot_id
                    )
                    resolved.append(
                        ResolvedAction(
                            cid=cid,
                            kind=action.kind,
                            side=action.side,
                            price=action.price,
                            qty=action.qty,
                        )
                    )

                elif action.kind == ActionIntentKind.CANCEL_ENTRY:
                    if action.side is None or action.price is None:
                        raise ValueError(f"CANCEL_ENTRY missing fields: {action}")
                    found_cid = self._registry.cid_for_entry(action.side, action.price)
                    if found_cid is None:
                        skipped += 1
                        skip_reasons.append(
                            f"CANCEL_ENTRY side={action.side.value} price={action.price}: no CID"
                        )
                        continue
                    self._registry.mark_entry_cancel_pending(found_cid)
                    resolved.append(
                        ResolvedAction(
                            cid=found_cid,
                            kind=action.kind,
                            side=action.side,
                            price=action.price,
                        )
                    )

                elif action.kind == ActionIntentKind.CANCEL_EXIT:
                    if action.order_id is None:
                        raise ValueError(f"CANCEL_EXIT missing order_id: {action}")
                    found_cid = self._registry.cid_for_exit(action.order_id)
                    if found_cid is None:
                        skipped += 1
                        skip_reasons.append(f"CANCEL_EXIT exit_order_id={action.order_id}: no CID")
                        continue
                    self._registry.mark_exit_cancel_pending(found_cid)
                    resolved.append(
                        ResolvedAction(
                            cid=found_cid,
                            kind=action.kind,
                            side=action.side or OrderSide.BUY,
                            price=action.price or Decimal(0),
                        )
                    )

            except ValueError:
                raise  # re-raise non-CID errors (missing fields)

        return PartialResolveResult(
            actions=tuple(resolved),
            skipped_count=skipped,
            skip_reasons=tuple(skip_reasons),
        )

    # --- Explicit confirmation (22.3) ---

    def confirm_entry_fill(self, cid: str) -> EntryRegistration | None:
        """Remove entry after SM.apply(EntryFilled) succeeds."""
        return self._registry.remove_entry(cid)

    def confirm_exit_fill(self, cid: str) -> ExitRegistration | None:
        """Remove exit after SM.apply(ExitFilled) succeeds."""
        return self._registry.remove_exit(cid)

    def confirm_cancel_entry(self, cid: str) -> EntryRegistration | None:
        """Remove entry after exchange confirms cancel."""
        return self._registry.remove_entry(cid)

    def confirm_cancel_exit(self, cid: str) -> ExitRegistration | None:
        """Remove exit after exchange confirms cancel."""
        return self._registry.remove_exit(cid)

    # --- Initial window seeding (22.9) ---

    def seed_entry_window(self, window: EntryWindow, ts_ms: int) -> tuple[ResolvedAction, ...]:
        """Register CIDs for all prices in entry window. For initial placement."""
        resolved: list[ResolvedAction] = []
        for price in window.buy_entry_prices:
            cid = self.generate_entry_cid(ts_ms)
            self._registry.register_entry(cid, OrderSide.BUY, price)
            resolved.append(
                ResolvedAction(
                    cid=cid,
                    kind=ActionIntentKind.PLACE_ENTRY,
                    side=OrderSide.BUY,
                    price=price,
                    qty=self._config.order_size,
                )
            )
        for price in window.sell_entry_prices:
            cid = self.generate_entry_cid(ts_ms)
            self._registry.register_entry(cid, OrderSide.SELL, price)
            resolved.append(
                ResolvedAction(
                    cid=cid,
                    kind=ActionIntentKind.PLACE_ENTRY,
                    side=OrderSide.SELL,
                    price=price,
                    qty=self._config.order_size,
                )
            )
        return tuple(resolved)

    # --- Reconciliation (22.6) ---

    def reconcile(
        self,
        snapshot: GridV2Snapshot,  # noqa: ARG002 - reserved for future reconciliation logic
        exchange_open_cids: frozenset[str],
        ts: int,
    ) -> ReconciliationResult:
        """Compare state machine expected state against exchange reality."""
        mismatches: list[GridV2Mismatch] = []

        matched_entries = 0
        for cid in self._registry.all_entry_cids:
            if cid in exchange_open_cids:
                matched_entries += 1
            else:
                entry_reg = self._registry.lookup_entry(cid)
                if entry_reg is not None:
                    mismatches.append(
                        GridV2Mismatch(
                            kind=GridV2MismatchKind.ENTRY_MISSING_ON_EXCHANGE,
                            severity=MismatchSeverity.WARNING,
                            detail=(
                                f"Entry {entry_reg.side.value} @ {entry_reg.price} not on exchange"
                            ),
                            cid=cid,
                        )
                    )

        matched_exits = 0
        for cid in self._registry.all_exit_cids:
            if cid in exchange_open_cids:
                matched_exits += 1
            else:
                exit_reg = self._registry.lookup_exit(cid)
                if exit_reg is not None:
                    mismatches.append(
                        GridV2Mismatch(
                            kind=GridV2MismatchKind.EXIT_MISSING_ON_EXCHANGE,
                            severity=MismatchSeverity.CRITICAL,
                            detail=(
                                f"Exit for lot {exit_reg.lot_id} not on exchange (unprotected lot!)"
                            ),
                            cid=cid,
                            lot_id=exit_reg.lot_id,
                        )
                    )

        known_cids = self._registry.all_entry_cids | self._registry.all_exit_cids
        for cid in exchange_open_cids:
            if self.is_ours(cid) and cid not in known_cids:
                mismatches.append(
                    GridV2Mismatch(
                        kind=GridV2MismatchKind.UNEXPECTED_ORDER,
                        severity=MismatchSeverity.WARNING,
                        detail="Unexpected order on exchange",
                        cid=cid,
                    )
                )

        return ReconciliationResult(
            mismatches=tuple(mismatches),
            matched_entries=matched_entries,
            matched_exits=matched_exits,
            ts=ts,
        )

    # --- Snapshot reconstruction (22.7, 22.8, 22.10) ---

    def reconstruct_snapshot(  # noqa: PLR0912, PLR0915
        self,
        exchange_open_orders: Sequence[tuple[str, OrderSide, Decimal, Decimal]],
        position_qty: Decimal,
        reference_price: Decimal,
        ts: int,
    ) -> GridV2Snapshot:
        """Build GridV2Snapshot from exchange data for restart.

        Fail-closed: ValueError on any inconsistency (F1-F8).
        Resets registry, registers all discovered CIDs, recovers seq counters.
        Validates through GridV2StateMachine.__init__() (21.15).
        """
        entry_buys: list[tuple[Decimal, str]] = []  # (price, cid)
        entry_sells: list[tuple[Decimal, str]] = []
        inferred_lots: list[InventoryLot] = []
        inferred_exits: list[ExitOrder] = []
        exit_cids: list[str] = []
        seen_entry_indices: list[int] = []
        seen_exit_indices: list[int] = []
        seen_entry_side_prices: set[tuple[OrderSide, Decimal]] = set()
        classified_count = 0

        for cid, side, price, qty in exchange_open_orders:
            parsed = self.parse_cid(cid)
            if parsed is None:
                continue
            classified_count += 1

            if parsed.kind == GridV2OrderKind.ENTRY:
                key = (side, price)  # F6 check: duplicate (side, price)
                if key in seen_entry_side_prices:
                    raise ValueError(
                        f"Duplicate entry (side={side.value}, price={price}): "
                        f"registry one-to-one violation (F6)"
                    )
                seen_entry_side_prices.add(key)
                seen_entry_indices.append(parsed.index)

                if side == OrderSide.BUY:
                    entry_buys.append((price, cid))
                else:
                    entry_sells.append((price, cid))

            elif parsed.kind == GridV2OrderKind.EXIT:
                seen_exit_indices.append(parsed.index)

                lot_id = f"lot-{cid}"
                exit_order_id = f"exit-{cid}"

                if side == OrderSide.SELL:
                    lot_side = LotSide.LONG
                    entry_price = price / (Decimal(1) + self._config.grid_step_pct)
                else:
                    lot_side = LotSide.SHORT
                    entry_price = price / (Decimal(1) - self._config.grid_step_pct)

                lot = InventoryLot(
                    lot_id=lot_id,
                    side=lot_side,
                    entry_price=entry_price,
                    qty=qty,
                    opened_at_ts=ts,
                    source_entry_order_id=cid,
                    exit_price=price,
                    exit_order_id=exit_order_id,
                    status=LotStatus.OPEN,
                )
                exit_order = ExitOrder(
                    exit_order_id=exit_order_id,
                    lot_id=lot_id,
                    side=side,
                    price=price,
                    qty=qty,
                    status=ExitOrderStatus.OPEN,
                )
                inferred_lots.append(lot)
                inferred_exits.append(exit_order)
                exit_cids.append(cid)

        # F8: all CIDs unparseable (truly garbled, not just cross-symbol)
        if exchange_open_orders and classified_count == 0:
            any_parseable = any(
                parse_client_order_id(cid) is not None for cid, _, _, _ in exchange_open_orders
            )
            if not any_parseable:
                raise ValueError("All order CIDs failed to parse: unclassifiable orders (F8)")

        # F5: duplicate exits (same lot_id)
        lot_ids = [lot.lot_id for lot in inferred_lots]
        if len(lot_ids) != len(set(lot_ids)):
            raise ValueError("Duplicate exit orders for same inferred lot (F5)")

        # F7: entry count exceeds config
        buy_count = len(entry_buys)
        sell_count = len(entry_sells)
        if buy_count > self._config.entry_levels_per_side:
            raise ValueError(
                f"Buy entry count {buy_count} exceeds "
                f"entry_levels_per_side {self._config.entry_levels_per_side} (F7)"
            )
        if sell_count > self._config.entry_levels_per_side:
            raise ValueError(
                f"Sell entry count {sell_count} exceeds "
                f"entry_levels_per_side {self._config.entry_levels_per_side} (F7)"
            )

        # Position cross-validation (F2, F3, F4)
        has_lots = len(inferred_lots) > 0
        pos_flat = position_qty == 0

        if not pos_flat and not has_lots:
            # F2 recovery: synthesize protective lot + exit from position
            import logging  # noqa: PLC0415

            _f2_logger = logging.getLogger(__name__)
            _f2_logger.warning(
                "GRID_V2_F2_PROTECTIVE_RECOVERY symbol=grid_v2 position_qty=%s "
                "reference_price=%s — synthesizing protective exit",
                position_qty,
                reference_price,
            )
            if position_qty > 0:
                lot_side = LotSide.LONG
                exit_side = OrderSide.SELL
                exit_price = reference_price * (Decimal(1) + self._config.grid_step_pct)
            else:
                lot_side = LotSide.SHORT
                exit_side = OrderSide.BUY
                exit_price = reference_price * (Decimal(1) - self._config.grid_step_pct)

            f2_lot_id = "lot-f2-protective-0"
            f2_exit_order_id = "exit-f2-protective-0"
            f2_lot = InventoryLot(
                lot_id=f2_lot_id,
                side=lot_side,
                entry_price=reference_price,
                qty=abs(position_qty),
                opened_at_ts=ts,
                source_entry_order_id="f2-reconstructed",
                exit_price=exit_price,
                exit_order_id=f2_exit_order_id,
                status=LotStatus.OPEN,
            )
            f2_exit = ExitOrder(
                exit_order_id=f2_exit_order_id,
                lot_id=f2_lot_id,
                side=exit_side,
                price=exit_price,
                qty=abs(position_qty),
                status=ExitOrderStatus.OPEN,
            )
            inferred_lots.append(f2_lot)
            inferred_exits.append(f2_exit)
            self._f2_protective_recovery = True
        if pos_flat and has_lots:
            raise ValueError("Position flat but exit orders exist: inconsistent state (F3)")
        if has_lots:
            all_long = all(lot.side == LotSide.LONG for lot in inferred_lots)
            all_short = all(lot.side == LotSide.SHORT for lot in inferred_lots)

            # F1: mixed sides
            if not all_long and not all_short:
                raise ValueError("Mixed lot sides: I4 violation (F1)")

            # F4: position sign mismatch
            if all_long and position_qty < 0:
                raise ValueError("Position negative but lots are LONG: direction mismatch (F4)")
            if all_short and position_qty > 0:
                raise ValueError("Position positive but lots are SHORT: direction mismatch (F4)")

        # Determine mode
        if not inferred_lots:
            mode = BranchMode.FLAT
        elif all(lot.side == LotSide.LONG for lot in inferred_lots):
            mode = BranchMode.LONG_BRANCH
        else:
            mode = BranchMode.SHORT_BRANCH

        # Sort entries
        entry_buys.sort(key=lambda t: t[0], reverse=True)
        entry_sells.sort(key=lambda t: t[0])

        entry_window = EntryWindow(
            reference_price=reference_price,
            buy_entry_prices=tuple(p for p, _ in entry_buys),
            sell_entry_prices=tuple(p for p, _ in entry_sells),
            levels_per_side=self._config.entry_levels_per_side,
            step_pct=self._config.grid_step_pct,
        )

        snapshot = GridV2Snapshot(
            mode=mode,
            entry_window=entry_window,
            open_lots=tuple(inferred_lots),
            closed_lots=(),
            exit_orders=tuple(inferred_exits),
            emergency_stopped=False,
            last_recenter_ts=None,
        )

        # Validate through SM constructor (21.15)
        GridV2StateMachine(self._config, snapshot)

        # Reset registry and rebuild (22.3, 22.10)
        self._registry.clear()

        for price, cid in entry_buys:
            self._registry.register_entry(cid, OrderSide.BUY, price)
        for price, cid in entry_sells:
            self._registry.register_entry(cid, OrderSide.SELL, price)
        # Register only exits with exchange CIDs (F2 synthetic exits have no CID yet).
        for exit_cid, exit_order in zip(exit_cids, inferred_exits, strict=False):
            self._registry.register_exit(exit_cid, exit_order.exit_order_id, exit_order.lot_id)

        # Recover seq counters (22.10)
        self._entry_seq = (max(seen_entry_indices) + 1) if seen_entry_indices else 0
        self._exit_seq = (max(seen_exit_indices) + 1) if seen_exit_indices else 0

        if self._entry_seq > self._max_seq:
            raise ValueError(
                f"Entry seq overflow after reconstruction: {self._entry_seq} > {self._max_seq}"
            )
        if self._exit_seq > self._max_seq:
            raise ValueError(
                f"Exit seq overflow after reconstruction: {self._exit_seq} > {self._max_seq}"
            )

        return snapshot
