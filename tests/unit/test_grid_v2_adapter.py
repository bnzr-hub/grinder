"""Tests for grid_v2 reconciliation adapter (doc-27 section 22)."""

from decimal import Decimal

import pytest

from grinder.core import OrderSide
from grinder.grid_v2.adapter import (
    GridV2Adapter,
    GridV2MismatchKind,
    GridV2OrderKind,
    GridV2OrderRegistry,
    MismatchSeverity,
    _compute_max_seq,
)
from grinder.grid_v2.state import (
    ActionIntent,
    ActionIntentKind,
    BranchMode,
    EntryFilled,
    ExitFilled,
    GridV2Config,
    GridV2StateMachine,
    LotSide,
    RecenterRequested,
)
from grinder.reconcile.identity import (
    BINANCE_MAX_CLIENT_ORDER_ID_LEN,
    OrderIdentityConfig,
    is_ours,
)

_BASE_TS = 1_710_000_000_000  # milliseconds
_REF_PRICE = Decimal("50000")
_STEP = Decimal("0.005")
_ORDER_SIZE = Decimal("0.001")


def _config(
    step: Decimal = _STEP,
    levels: int = 3,
    size: Decimal = _ORDER_SIZE,
    max_levels: int = 10,
    max_notional: Decimal = Decimal("100000"),
) -> GridV2Config:
    return GridV2Config(
        grid_step_pct=step,
        entry_levels_per_side=levels,
        order_size=size,
        max_inventory_levels=max_levels,
        max_inventory_notional_usd=max_notional,
    )


def _adapter(
    symbol: str = "BTCUSDT",
    config: GridV2Config | None = None,
) -> GridV2Adapter:
    return GridV2Adapter(config or _config(), symbol)


def _sm(
    config: GridV2Config | None = None,
    ref: Decimal = _REF_PRICE,
    ts: int = _BASE_TS,
) -> GridV2StateMachine:
    return GridV2StateMachine.create_initial(config or _config(), ref, ts)


class TestCidScheme:
    def test_generate_entry_cid_format(self) -> None:
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        assert cid.startswith("g_g_BTCUSDT_e0_")

    def test_generate_exit_cid_format(self) -> None:
        a = _adapter()
        cid = a.generate_exit_cid(_BASE_TS)
        assert cid.startswith("g_g_BTCUSDT_x0_")

    def test_parse_entry_roundtrip(self) -> None:
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        parsed = a.parse_cid(cid)
        assert parsed is not None
        assert parsed.kind == GridV2OrderKind.ENTRY
        assert parsed.symbol == "BTCUSDT"
        assert parsed.index == 0
        assert parsed.raw_cid == cid

    def test_parse_exit_roundtrip(self) -> None:
        a = _adapter()
        cid = a.generate_exit_cid(_BASE_TS)
        parsed = a.parse_cid(cid)
        assert parsed is not None
        assert parsed.kind == GridV2OrderKind.EXIT
        assert parsed.index == 0

    def test_max_length_7char_symbol(self) -> None:
        a = _adapter(symbol="BTCUSDT")  # 7 chars
        cid = a.generate_entry_cid(_BASE_TS)
        assert len(cid) <= BINANCE_MAX_CLIENT_ORDER_ID_LEN

    def test_max_length_8char_symbol(self) -> None:
        a = _adapter(symbol="SHIBUSDT")  # 8 chars
        # Generate up to seq 999 (max for 8-char)
        for _ in range(999):
            a.generate_entry_cid(_BASE_TS)
        cid = a.generate_entry_cid(_BASE_TS)  # seq=999
        assert len(cid) <= BINANCE_MAX_CLIENT_ORDER_ID_LEN

    def test_is_ours_accepts_g(self) -> None:
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        assert a.is_ours(cid) is True
        config = OrderIdentityConfig(strategy_id="g", prefix="g_")
        assert is_ours(cid, config) is True

    def test_is_ours_rejects_foreign(self) -> None:
        a = _adapter()
        assert a.is_ours("grinder_d_BTCUSDT_1_1710000_0") is False
        assert a.is_ours("some_random_string") is False
        assert a.is_ours("") is False

    def test_parse_foreign_returns_none(self) -> None:
        a = _adapter()
        assert a.parse_cid("grinder_d_BTCUSDT_1_1710000_0") is None

    def test_parse_garbage_returns_none(self) -> None:
        a = _adapter()
        assert a.parse_cid("not_a_cid") is None
        assert a.parse_cid("") is None

    def test_overflow_raises(self) -> None:
        a = _adapter(symbol="A" * 16)  # max_seq = 9
        for _ in range(10):
            a.generate_entry_cid(_BASE_TS)
        with pytest.raises(ValueError, match="overflow"):
            a.generate_entry_cid(_BASE_TS)

    def test_entry_seq_increments(self) -> None:
        a = _adapter()
        cid1 = a.generate_entry_cid(_BASE_TS)
        cid2 = a.generate_entry_cid(_BASE_TS)
        assert cid1 != cid2
        p1 = a.parse_cid(cid1)
        p2 = a.parse_cid(cid2)
        assert p1 is not None and p2 is not None
        assert p1.index == 0
        assert p2.index == 1


class TestOrderRegistry:
    def test_register_and_lookup_entry(self) -> None:
        r = GridV2OrderRegistry()
        r.register_entry("cid1", OrderSide.BUY, Decimal("100"))
        reg = r.lookup_entry("cid1")
        assert reg is not None
        assert reg.side == OrderSide.BUY
        assert reg.price == Decimal("100")

    def test_register_and_lookup_exit(self) -> None:
        r = GridV2OrderRegistry()
        r.register_exit("cid1", "exit-x", "lot-x")
        reg = r.lookup_exit("cid1")
        assert reg is not None
        assert reg.exit_order_id == "exit-x"
        assert reg.lot_id == "lot-x"

    def test_reverse_lookup_entry(self) -> None:
        r = GridV2OrderRegistry()
        r.register_entry("cid1", OrderSide.BUY, Decimal("100"))
        assert r.cid_for_entry(OrderSide.BUY, Decimal("100")) == "cid1"

    def test_reverse_lookup_exit(self) -> None:
        r = GridV2OrderRegistry()
        r.register_exit("cid1", "exit-x", "lot-x")
        assert r.cid_for_exit("exit-x") == "cid1"

    def test_remove_entry_cleans_both(self) -> None:
        r = GridV2OrderRegistry()
        r.register_entry("cid1", OrderSide.BUY, Decimal("100"))
        removed = r.remove_entry("cid1")
        assert removed is not None
        assert r.lookup_entry("cid1") is None
        assert r.cid_for_entry(OrderSide.BUY, Decimal("100")) is None

    def test_remove_exit_cleans_both(self) -> None:
        r = GridV2OrderRegistry()
        r.register_exit("cid1", "exit-x", "lot-x")
        removed = r.remove_exit("cid1")
        assert removed is not None
        assert r.lookup_exit("cid1") is None
        assert r.cid_for_exit("exit-x") is None

    def test_duplicate_entry_raises(self) -> None:
        r = GridV2OrderRegistry()
        r.register_entry("cid1", OrderSide.BUY, Decimal("100"))
        with pytest.raises(ValueError, match="Duplicate"):
            r.register_entry("cid1", OrderSide.SELL, Decimal("200"))

    def test_duplicate_exit_raises(self) -> None:
        r = GridV2OrderRegistry()
        r.register_exit("cid1", "exit-x", "lot-x")
        with pytest.raises(ValueError, match="Duplicate"):
            r.register_exit("cid1", "exit-y", "lot-y")

    def test_empty_lookups_none(self) -> None:
        r = GridV2OrderRegistry()
        assert r.lookup_entry("nonexistent") is None
        assert r.lookup_exit("nonexistent") is None
        assert r.cid_for_entry(OrderSide.BUY, Decimal("100")) is None
        assert r.cid_for_exit("exit-x") is None

    def test_count_properties(self) -> None:
        r = GridV2OrderRegistry()
        assert r.entry_count == 0
        assert r.exit_count == 0
        r.register_entry("e1", OrderSide.BUY, Decimal("100"))
        r.register_exit("x1", "exit-x", "lot-x")
        assert r.entry_count == 1
        assert r.exit_count == 1
        assert "e1" in r.all_entry_cids
        assert "x1" in r.all_exit_cids

    def test_clear_resets_all(self) -> None:
        r = GridV2OrderRegistry()
        r.register_entry("e1", OrderSide.BUY, Decimal("100"))
        r.register_exit("x1", "exit-x", "lot-x")
        r.clear()
        assert r.entry_count == 0
        assert r.exit_count == 0
        assert r.lookup_entry("e1") is None
        assert r.lookup_exit("x1") is None


class TestFillTranslation:
    def test_entry_fill_to_entry_filled(self) -> None:
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        a.registry.register_entry(cid, OrderSide.BUY, Decimal("49750"))
        result = a.translate_fill(cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE, _BASE_TS)
        assert result is not None
        assert isinstance(result.event, EntryFilled)
        assert result.event.order_id == cid
        assert result.event.side == OrderSide.BUY
        assert result.event.price == Decimal("49750")
        assert result.event.qty == _ORDER_SIZE
        assert result.source_cid == cid

    def test_exit_fill_to_exit_filled(self) -> None:
        a = _adapter()
        cid = a.generate_exit_cid(_BASE_TS)
        a.registry.register_exit(cid, "exit-E1", "lot-E1")
        result = a.translate_fill(cid, OrderSide.SELL, Decimal("50250"), _ORDER_SIZE, _BASE_TS)
        assert result is not None
        assert isinstance(result.event, ExitFilled)
        assert result.event.exit_order_id == "exit-E1"
        assert result.event.lot_id == "lot-E1"
        assert result.event.price == Decimal("50250")

    def test_foreign_cid_returns_none(self) -> None:
        a = _adapter()
        result = a.translate_fill(
            "grinder_d_BTCUSDT_1_1710000_0",
            OrderSide.BUY,
            Decimal("100"),
            _ORDER_SIZE,
            _BASE_TS,
        )
        assert result is None

    def test_unregistered_entry_raises(self) -> None:
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        # NOT registered
        with pytest.raises(ValueError, match="not in registry"):
            a.translate_fill(cid, OrderSide.BUY, Decimal("100"), _ORDER_SIZE, _BASE_TS)

    def test_unregistered_exit_raises(self) -> None:
        a = _adapter()
        cid = a.generate_exit_cid(_BASE_TS)
        # NOT registered
        with pytest.raises(ValueError, match="not in registry"):
            a.translate_fill(cid, OrderSide.SELL, Decimal("100"), _ORDER_SIZE, _BASE_TS)

    def test_preserves_price_qty_ts(self) -> None:
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        a.registry.register_entry(cid, OrderSide.SELL, Decimal("50250"))
        result = a.translate_fill(
            cid,
            OrderSide.SELL,
            Decimal("50250.50"),
            Decimal("0.002"),
            _BASE_TS + 1000,
        )
        assert result is not None
        assert result.event.price == Decimal("50250.50")
        assert result.event.qty == Decimal("0.002")
        assert result.event.ts == _BASE_TS + 1000

    def test_duplicate_entry_fill_same_event(self) -> None:
        """Adapter is stateless: same CID -> same event (22.4)."""
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        a.registry.register_entry(cid, OrderSide.BUY, Decimal("49750"))
        r1 = a.translate_fill(cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE, _BASE_TS)
        r2 = a.translate_fill(cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE, _BASE_TS)
        assert r1 is not None and r2 is not None
        assert r1.event == r2.event

    def test_no_registry_mutation_after_translate(self) -> None:
        """Translation never mutates registry (22.3)."""
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        a.registry.register_entry(cid, OrderSide.BUY, Decimal("49750"))
        before = a.registry.entry_count
        a.translate_fill(cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE, _BASE_TS)
        assert a.registry.entry_count == before
        assert a.registry.lookup_entry(cid) is not None


class TestActionResolution:
    def test_place_entry_generates_cid_and_registers(self) -> None:
        a = _adapter()
        actions = (
            ActionIntent(
                kind=ActionIntentKind.PLACE_ENTRY,
                side=OrderSide.BUY,
                price=Decimal("49750"),
                qty=_ORDER_SIZE,
                reason="FILL_REPLACEMENT",
            ),
        )
        resolved = a.resolve_actions(actions, _BASE_TS)
        assert len(resolved) == 1
        assert resolved[0].kind == ActionIntentKind.PLACE_ENTRY
        assert resolved[0].side == OrderSide.BUY
        assert resolved[0].price == Decimal("49750")
        assert a.registry.lookup_entry(resolved[0].cid) is not None

    def test_place_exit_generates_cid_and_registers(self) -> None:
        a = _adapter()
        actions = (
            ActionIntent(
                kind=ActionIntentKind.PLACE_EXIT,
                side=OrderSide.SELL,
                price=Decimal("50250"),
                qty=_ORDER_SIZE,
                lot_id="lot-entry1",
                reason="PAIRED_EXIT_FOR_LOT",
            ),
        )
        resolved = a.resolve_actions(actions, _BASE_TS)
        assert len(resolved) == 1
        reg = a.registry.lookup_exit(resolved[0].cid)
        assert reg is not None
        assert reg.exit_order_id == "exit-entry1"
        assert reg.lot_id == "lot-entry1"

    def test_cancel_entry_returns_cid_no_removal(self) -> None:
        """CANCEL_ENTRY looks up CID but does NOT remove (22.3)."""
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        a.registry.register_entry(cid, OrderSide.BUY, Decimal("49750"))
        actions = (
            ActionIntent(
                kind=ActionIntentKind.CANCEL_ENTRY,
                side=OrderSide.BUY,
                price=Decimal("49750"),
                reason="BRANCH_SUPPRESS",
            ),
        )
        resolved = a.resolve_actions(actions, _BASE_TS)
        assert len(resolved) == 1
        assert resolved[0].cid == cid
        # Registry NOT mutated
        assert a.registry.lookup_entry(cid) is not None

    def test_cancel_exit_returns_cid_no_removal(self) -> None:
        a = _adapter()
        cid = a.generate_exit_cid(_BASE_TS)
        a.registry.register_exit(cid, "exit-E1", "lot-E1")
        actions = (
            ActionIntent(
                kind=ActionIntentKind.CANCEL_EXIT,
                order_id="exit-E1",
                reason="OPERATOR_CLEANUP",
            ),
        )
        resolved = a.resolve_actions(actions, _BASE_TS)
        assert len(resolved) == 1
        assert resolved[0].cid == cid
        assert a.registry.lookup_exit(cid) is not None

    def test_cancel_unregistered_entry_raises(self) -> None:
        a = _adapter()
        actions = (
            ActionIntent(
                kind=ActionIntentKind.CANCEL_ENTRY,
                side=OrderSide.BUY,
                price=Decimal("99999"),
                reason="BRANCH_SUPPRESS",
            ),
        )
        with pytest.raises(ValueError, match="No registered entry CID"):
            a.resolve_actions(actions, _BASE_TS)

    def test_cancel_unregistered_exit_raises(self) -> None:
        a = _adapter()
        actions = (
            ActionIntent(
                kind=ActionIntentKind.CANCEL_EXIT,
                order_id="exit-nonexistent",
                reason="OPERATOR_CLEANUP",
            ),
        )
        with pytest.raises(ValueError, match="No registered exit CID"):
            a.resolve_actions(actions, _BASE_TS)

    def test_multiple_actions_in_order(self) -> None:
        a = _adapter()
        actions = (
            ActionIntent(
                kind=ActionIntentKind.PLACE_ENTRY,
                side=OrderSide.BUY,
                price=Decimal("49000"),
                qty=_ORDER_SIZE,
                reason="FILL_REPLACEMENT",
            ),
            ActionIntent(
                kind=ActionIntentKind.PLACE_ENTRY,
                side=OrderSide.SELL,
                price=Decimal("51000"),
                qty=_ORDER_SIZE,
                reason="FILL_REPLACEMENT",
            ),
        )
        resolved = a.resolve_actions(actions, _BASE_TS)
        assert len(resolved) == 2
        assert resolved[0].side == OrderSide.BUY
        assert resolved[1].side == OrderSide.SELL

    def test_seq_increments_for_multiple_places(self) -> None:
        a = _adapter()
        actions = (
            ActionIntent(
                kind=ActionIntentKind.PLACE_ENTRY,
                side=OrderSide.BUY,
                price=Decimal("49000"),
                qty=_ORDER_SIZE,
                reason="FILL_REPLACEMENT",
            ),
            ActionIntent(
                kind=ActionIntentKind.PLACE_ENTRY,
                side=OrderSide.SELL,
                price=Decimal("51000"),
                qty=_ORDER_SIZE,
                reason="FILL_REPLACEMENT",
            ),
        )
        resolved = a.resolve_actions(actions, _BASE_TS)
        assert resolved[0].cid != resolved[1].cid


class TestConfirmFill:
    def test_confirm_entry_fill_removes(self) -> None:
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        a.registry.register_entry(cid, OrderSide.BUY, Decimal("49750"))
        reg = a.confirm_entry_fill(cid)
        assert reg is not None
        assert reg.cid == cid
        assert a.registry.lookup_entry(cid) is None

    def test_confirm_exit_fill_removes(self) -> None:
        a = _adapter()
        cid = a.generate_exit_cid(_BASE_TS)
        a.registry.register_exit(cid, "exit-E1", "lot-E1")
        reg = a.confirm_exit_fill(cid)
        assert reg is not None
        assert a.registry.lookup_exit(cid) is None

    def test_confirm_unknown_returns_none(self) -> None:
        a = _adapter()
        assert a.confirm_entry_fill("nonexistent") is None
        assert a.confirm_exit_fill("nonexistent") is None

    def test_translate_confirm_retranslate_raises(self) -> None:
        """translate -> confirm -> re-translate same CID -> ValueError."""
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        a.registry.register_entry(cid, OrderSide.BUY, Decimal("49750"))
        # First translate OK
        r = a.translate_fill(cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE, _BASE_TS)
        assert r is not None
        # Confirm removes
        a.confirm_entry_fill(cid)
        # Re-translate raises
        with pytest.raises(ValueError, match="not in registry"):
            a.translate_fill(cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE, _BASE_TS)


class TestConfirmCancel:
    def test_confirm_cancel_entry_removes(self) -> None:
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        a.registry.register_entry(cid, OrderSide.BUY, Decimal("49750"))
        reg = a.confirm_cancel_entry(cid)
        assert reg is not None
        assert a.registry.lookup_entry(cid) is None

    def test_confirm_cancel_exit_removes(self) -> None:
        a = _adapter()
        cid = a.generate_exit_cid(_BASE_TS)
        a.registry.register_exit(cid, "exit-E1", "lot-E1")
        reg = a.confirm_cancel_exit(cid)
        assert reg is not None
        assert a.registry.lookup_exit(cid) is None

    def test_confirm_cancel_unknown_returns_none(self) -> None:
        a = _adapter()
        assert a.confirm_cancel_entry("nonexistent") is None
        assert a.confirm_cancel_exit("nonexistent") is None

    def test_translate_after_cancel_confirm_raises(self) -> None:
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        a.registry.register_entry(cid, OrderSide.BUY, Decimal("49750"))
        a.confirm_cancel_entry(cid)
        with pytest.raises(ValueError, match="not in registry"):
            a.translate_fill(cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE, _BASE_TS)


class TestSeedEntryWindow:
    def test_registers_all_entries(self) -> None:
        sm = _sm()
        a = _adapter()
        resolved = a.seed_entry_window(sm.snapshot.entry_window, _BASE_TS)
        window = sm.snapshot.entry_window
        expected_count = len(window.buy_entry_prices) + len(window.sell_entry_prices)
        assert len(resolved) == expected_count
        assert a.registry.entry_count == expected_count

    def test_returns_resolved_actions(self) -> None:
        sm = _sm()
        a = _adapter()
        resolved = a.seed_entry_window(sm.snapshot.entry_window, _BASE_TS)
        for r in resolved:
            assert r.kind == ActionIntentKind.PLACE_ENTRY
            assert r.qty == _ORDER_SIZE

    def test_enables_cancel_resolution(self) -> None:
        sm = _sm()
        a = _adapter()
        a.seed_entry_window(sm.snapshot.entry_window, _BASE_TS)
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        cancel = ActionIntent(
            kind=ActionIntentKind.CANCEL_ENTRY,
            side=OrderSide.BUY,
            price=buy_price,
            reason="BRANCH_SUPPRESS",
        )
        resolved = a.resolve_actions((cancel,), _BASE_TS)
        assert len(resolved) == 1

    def test_count_matches_window(self) -> None:
        cfg = _config(levels=5)
        sm = GridV2StateMachine.create_initial(cfg, _REF_PRICE, _BASE_TS)
        a = _adapter(config=cfg)
        a.seed_entry_window(sm.snapshot.entry_window, _BASE_TS)
        assert a.registry.entry_count == 10  # 5 buy + 5 sell


class TestReconciliation:
    def _setup_adapter_with_entries(self) -> tuple[GridV2Adapter, list[str]]:
        a = _adapter()
        sm = _sm()
        resolved = a.seed_entry_window(sm.snapshot.entry_window, _BASE_TS)
        cids = [r.cid for r in resolved]
        return a, cids

    def test_all_matched_no_mismatches(self) -> None:
        a, cids = self._setup_adapter_with_entries()
        sm = _sm()
        result = a.reconcile(sm.snapshot, frozenset(cids), _BASE_TS)
        assert len(result.mismatches) == 0
        assert result.matched_entries == len(cids)

    def test_missing_entry(self) -> None:
        a, cids = self._setup_adapter_with_entries()
        sm = _sm()
        # Remove one CID from exchange
        exchange_cids = frozenset(cids[1:])
        result = a.reconcile(sm.snapshot, exchange_cids, _BASE_TS)
        missing = [
            m for m in result.mismatches if m.kind == GridV2MismatchKind.ENTRY_MISSING_ON_EXCHANGE
        ]
        assert len(missing) == 1
        assert missing[0].severity == MismatchSeverity.WARNING

    def test_missing_exit_critical(self) -> None:
        a = _adapter()
        exit_cid = a.generate_exit_cid(_BASE_TS)
        a.registry.register_exit(exit_cid, "exit-E1", "lot-E1")
        sm = _sm()
        result = a.reconcile(sm.snapshot, frozenset(), _BASE_TS)
        exits_missing = [
            m for m in result.mismatches if m.kind == GridV2MismatchKind.EXIT_MISSING_ON_EXCHANGE
        ]
        assert len(exits_missing) == 1
        assert exits_missing[0].severity == MismatchSeverity.CRITICAL
        assert exits_missing[0].lot_id == "lot-E1"

    def test_unexpected_order(self) -> None:
        a = _adapter()
        sm = _sm()
        # A g CID on exchange but not in registry
        foreign_cid = "g_g_BTCUSDT_e999_1710000_0"
        result = a.reconcile(sm.snapshot, frozenset({foreign_cid}), _BASE_TS)
        unexpected = [m for m in result.mismatches if m.kind == GridV2MismatchKind.UNEXPECTED_ORDER]
        assert len(unexpected) == 1

    def test_foreign_cid_ignored(self) -> None:
        a = _adapter()
        sm = _sm()
        result = a.reconcile(sm.snapshot, frozenset({"grinder_d_BTCUSDT_1_1710000_0"}), _BASE_TS)
        assert len(result.mismatches) == 0

    def test_empty_exchange_all_missing(self) -> None:
        a, cids = self._setup_adapter_with_entries()
        sm = _sm()
        result = a.reconcile(sm.snapshot, frozenset(), _BASE_TS)
        assert result.matched_entries == 0
        assert len(result.mismatches) == len(cids)

    def test_counts_correct(self) -> None:
        a, cids = self._setup_adapter_with_entries()
        sm = _sm()
        half = frozenset(cids[: len(cids) // 2])
        result = a.reconcile(sm.snapshot, half, _BASE_TS)
        assert result.matched_entries == len(half)


class TestSnapshotReconstruction:
    def test_flat_no_orders(self) -> None:
        a = _adapter()
        snap = a.reconstruct_snapshot([], Decimal(0), _REF_PRICE, _BASE_TS)
        assert snap.mode == BranchMode.FLAT
        assert len(snap.open_lots) == 0
        assert len(snap.exit_orders) == 0

    def test_long_branch_from_sell_exits(self) -> None:
        a = _adapter()
        exit_cid = a.generate_exit_cid(_BASE_TS)
        exit_price = _REF_PRICE * (Decimal(1) + _STEP)
        orders = [(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE)]
        snap = a.reconstruct_snapshot(orders, _ORDER_SIZE, _REF_PRICE, _BASE_TS)
        assert snap.mode == BranchMode.LONG_BRANCH
        assert len(snap.open_lots) == 1
        assert snap.open_lots[0].side == LotSide.LONG

    def test_short_branch_from_buy_exits(self) -> None:
        a = _adapter()
        exit_cid = a.generate_exit_cid(_BASE_TS)
        exit_price = _REF_PRICE * (Decimal(1) - _STEP)
        orders = [(exit_cid, OrderSide.BUY, exit_price, _ORDER_SIZE)]
        snap = a.reconstruct_snapshot(orders, -_ORDER_SIZE, _REF_PRICE, _BASE_TS)
        assert snap.mode == BranchMode.SHORT_BRANCH
        assert snap.open_lots[0].side == LotSide.SHORT

    def test_entry_window_from_entries(self) -> None:
        a = _adapter()
        buy_cid = a.generate_entry_cid(_BASE_TS)
        sell_cid = a.generate_entry_cid(_BASE_TS)
        orders = [
            (buy_cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE),
            (sell_cid, OrderSide.SELL, Decimal("50250"), _ORDER_SIZE),
        ]
        snap = a.reconstruct_snapshot(orders, Decimal(0), _REF_PRICE, _BASE_TS)
        assert snap.entry_window.buy_entry_prices == (Decimal("49750"),)
        assert snap.entry_window.sell_entry_prices == (Decimal("50250"),)

    def test_buy_prices_sorted_descending(self) -> None:
        a = _adapter()
        cid1 = a.generate_entry_cid(_BASE_TS)
        cid2 = a.generate_entry_cid(_BASE_TS)
        orders = [
            (cid1, OrderSide.BUY, Decimal("49500"), _ORDER_SIZE),
            (cid2, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE),
        ]
        snap = a.reconstruct_snapshot(orders, Decimal(0), _REF_PRICE, _BASE_TS)
        assert snap.entry_window.buy_entry_prices == (Decimal("49750"), Decimal("49500"))

    def test_sell_prices_sorted_ascending(self) -> None:
        a = _adapter()
        cid1 = a.generate_entry_cid(_BASE_TS)
        cid2 = a.generate_entry_cid(_BASE_TS)
        orders = [
            (cid1, OrderSide.SELL, Decimal("50500"), _ORDER_SIZE),
            (cid2, OrderSide.SELL, Decimal("50250"), _ORDER_SIZE),
        ]
        snap = a.reconstruct_snapshot(orders, Decimal(0), _REF_PRICE, _BASE_TS)
        assert snap.entry_window.sell_entry_prices == (Decimal("50250"), Decimal("50500"))

    def test_validates_through_constructor(self) -> None:
        """Reconstructed snapshot passes GridV2StateMachine.__init__() (21.15)."""
        a = _adapter()
        exit_cid = a.generate_exit_cid(_BASE_TS)
        exit_price = _REF_PRICE * (Decimal(1) + _STEP)
        orders = [(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE)]
        snap = a.reconstruct_snapshot(orders, _ORDER_SIZE, _REF_PRICE, _BASE_TS)
        # Should not raise
        GridV2StateMachine(_config(), snap)

    def test_registry_rebuilt_and_seq_recovered(self) -> None:
        a = _adapter()
        # First add some junk to registry
        a.registry.register_entry("old", OrderSide.BUY, Decimal("1"))
        # Reconstruct with one exit order
        exit_cid = a.generate_exit_cid(_BASE_TS)
        exit_price = _REF_PRICE * (Decimal(1) + _STEP)
        orders = [(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE)]
        a.reconstruct_snapshot(orders, _ORDER_SIZE, _REF_PRICE, _BASE_TS)
        # Old entry gone
        assert a.registry.lookup_entry("old") is None
        # New exit registered
        assert a.registry.lookup_exit(exit_cid) is not None


class TestReconstructionFailClosed:
    def test_f1_mixed_sides(self) -> None:
        a = _adapter()
        cid1 = a.generate_exit_cid(_BASE_TS)
        cid2 = a.generate_exit_cid(_BASE_TS)
        orders = [
            (cid1, OrderSide.SELL, Decimal("50250"), _ORDER_SIZE),  # LONG lot
            (cid2, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE),  # SHORT lot
        ]
        with pytest.raises(ValueError, match="F1"):
            a.reconstruct_snapshot(orders, _ORDER_SIZE, _REF_PRICE, _BASE_TS)

    def test_f2_nonfat_pos_no_exits(self) -> None:
        a = _adapter()
        entry_cid = a.generate_entry_cid(_BASE_TS)
        orders = [(entry_cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE)]
        with pytest.raises(ValueError, match="F2"):
            a.reconstruct_snapshot(orders, _ORDER_SIZE, _REF_PRICE, _BASE_TS)

    def test_f3_flat_pos_exits_exist(self) -> None:
        a = _adapter()
        exit_cid = a.generate_exit_cid(_BASE_TS)
        orders = [(exit_cid, OrderSide.SELL, Decimal("50250"), _ORDER_SIZE)]
        with pytest.raises(ValueError, match="F3"):
            a.reconstruct_snapshot(orders, Decimal(0), _REF_PRICE, _BASE_TS)

    def test_f4_pos_sign_mismatch(self) -> None:
        a = _adapter()
        exit_cid = a.generate_exit_cid(_BASE_TS)
        # SELL exit → LONG lot, but position is negative
        orders = [(exit_cid, OrderSide.SELL, Decimal("50250"), _ORDER_SIZE)]
        with pytest.raises(ValueError, match="F4"):
            a.reconstruct_snapshot(orders, -_ORDER_SIZE, _REF_PRICE, _BASE_TS)

    def test_f6_duplicate_entry_side_price(self) -> None:
        a = _adapter()
        cid1 = a.generate_entry_cid(_BASE_TS)
        cid2 = a.generate_entry_cid(_BASE_TS)
        orders = [
            (cid1, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE),
            (cid2, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE),
        ]
        with pytest.raises(ValueError, match="F6"):
            a.reconstruct_snapshot(orders, Decimal(0), _REF_PRICE, _BASE_TS)

    def test_f7_entries_exceed_levels(self) -> None:
        cfg = _config(levels=1)  # only 1 level per side
        a = GridV2Adapter(cfg, "BTCUSDT")
        cid1 = a.generate_entry_cid(_BASE_TS)
        cid2 = a.generate_entry_cid(_BASE_TS)
        orders = [
            (cid1, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE),
            (cid2, OrderSide.BUY, Decimal("49500"), _ORDER_SIZE),
        ]
        with pytest.raises(ValueError, match="F7"):
            a.reconstruct_snapshot(orders, Decimal(0), _REF_PRICE, _BASE_TS)

    def test_f8_all_unparseable(self) -> None:
        a = _adapter()
        orders = [
            ("totally_random_cid", OrderSide.BUY, Decimal("49750"), _ORDER_SIZE),
        ]
        with pytest.raises(ValueError, match="F8"):
            a.reconstruct_snapshot(orders, Decimal(0), _REF_PRICE, _BASE_TS)


class TestSeqRecovery:
    def test_seq_advances_to_max_plus_one(self) -> None:
        a = _adapter()
        # Generate entries up to seq 5
        for _ in range(6):
            a.generate_entry_cid(_BASE_TS)
        a.generate_entry_cid(_BASE_TS)  # seq=6
        # Create a new adapter and reconstruct with that CID
        a2 = _adapter()
        entry_cid = "g_g_BTCUSDT_e5_1710000_0"
        orders = [(entry_cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE)]
        a2.reconstruct_snapshot(orders, Decimal(0), _REF_PRICE, _BASE_TS)
        # Next entry CID should have seq >= 6
        new_cid = a2.generate_entry_cid(_BASE_TS)
        parsed = a2.parse_cid(new_cid)
        assert parsed is not None
        assert parsed.index >= 6

    def test_no_collision_after_reconstruct(self) -> None:
        a = _adapter()
        exit_cid = a.generate_exit_cid(_BASE_TS)
        exit_price = _REF_PRICE * (Decimal(1) + _STEP)
        orders = [(exit_cid, OrderSide.SELL, exit_price, _ORDER_SIZE)]
        a.reconstruct_snapshot(orders, _ORDER_SIZE, _REF_PRICE, _BASE_TS)
        # New exit CID should not collide
        new_cid = a.generate_exit_cid(_BASE_TS)
        assert new_cid != exit_cid


class TestReplayScenarios:
    def test_full_lifecycle(self) -> None:
        """create -> seed -> fill -> translate -> sm.apply -> confirm -> resolve."""
        cfg = _config()
        sm = _sm(config=cfg)
        a = _adapter(config=cfg)

        # 1. Seed initial window
        seed_resolved = a.seed_entry_window(sm.snapshot.entry_window, _BASE_TS)
        assert len(seed_resolved) == 6  # 3 buy + 3 sell

        # 2. Simulate a BUY fill at first buy price
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        buy_cid = a.registry.cid_for_entry(OrderSide.BUY, buy_price)
        assert buy_cid is not None

        # 3. Translate fill
        fill = a.translate_fill(buy_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + 1000)
        assert fill is not None
        assert isinstance(fill.event, EntryFilled)

        # 4. Apply to state machine
        result = sm.apply(fill.event)
        assert not result.rejected
        assert sm.mode == BranchMode.LONG_BRANCH

        # 5. Confirm entry fill
        a.confirm_entry_fill(buy_cid)
        assert a.registry.lookup_entry(buy_cid) is None

        # 6. Resolve actions from transition
        resolved = a.resolve_actions(result.actions, _BASE_TS + 1000)
        assert len(resolved) > 0

        # Check we got a PLACE_EXIT
        place_exits = [r for r in resolved if r.kind == ActionIntentKind.PLACE_EXIT]
        assert len(place_exits) == 1

    def test_multi_lot_long(self) -> None:
        cfg = _config()
        sm = _sm(config=cfg)
        a = _adapter(config=cfg)
        a.seed_entry_window(sm.snapshot.entry_window, _BASE_TS)

        # Fill two consecutive BUY entries
        for i, buy_price in enumerate(sm.snapshot.entry_window.buy_entry_prices[:2]):
            cid = a.registry.cid_for_entry(OrderSide.BUY, buy_price)
            assert cid is not None
            fill = a.translate_fill(cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS + i * 1000)
            assert fill is not None
            result = sm.apply(fill.event)
            assert not result.rejected
            a.confirm_entry_fill(cid)
            a.resolve_actions(result.actions, _BASE_TS + i * 1000)

        assert sm.mode == BranchMode.LONG_BRANCH
        assert len(sm.snapshot.open_lots) == 2

    def test_unwind_to_flat(self) -> None:
        cfg = _config()
        sm = _sm(config=cfg)
        a = _adapter(config=cfg)
        a.seed_entry_window(sm.snapshot.entry_window, _BASE_TS)

        # Fill one BUY entry
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        entry_cid = a.registry.cid_for_entry(OrderSide.BUY, buy_price)
        assert entry_cid is not None
        fill = a.translate_fill(entry_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS)
        assert fill is not None
        entry_result = sm.apply(fill.event)
        assert not entry_result.rejected
        a.confirm_entry_fill(entry_cid)
        resolved = a.resolve_actions(entry_result.actions, _BASE_TS)

        # Now simulate exit fill
        exit_resolved = [r for r in resolved if r.kind == ActionIntentKind.PLACE_EXIT]
        assert len(exit_resolved) == 1
        exit_cid = exit_resolved[0].cid

        exit_order = sm.snapshot.exit_orders[0]
        exit_fill = a.translate_fill(
            exit_cid, OrderSide.SELL, exit_order.price, _ORDER_SIZE, _BASE_TS + 1000
        )
        assert exit_fill is not None
        assert isinstance(exit_fill.event, ExitFilled)
        exit_result = sm.apply(exit_fill.event)
        assert not exit_result.rejected
        a.confirm_exit_fill(exit_cid)

        assert sm.mode == BranchMode.FLAT
        assert len(sm.snapshot.open_lots) == 0

    def test_recenter_after_unwind(self) -> None:
        cfg = _config()
        sm = _sm(config=cfg)
        a = _adapter(config=cfg)
        a.seed_entry_window(sm.snapshot.entry_window, _BASE_TS)

        # Fill + exit
        buy_price = sm.snapshot.entry_window.buy_entry_prices[0]
        entry_cid = a.registry.cid_for_entry(OrderSide.BUY, buy_price)
        assert entry_cid is not None
        fill = a.translate_fill(entry_cid, OrderSide.BUY, buy_price, _ORDER_SIZE, _BASE_TS)
        assert fill is not None
        entry_result = sm.apply(fill.event)
        a.confirm_entry_fill(entry_cid)
        resolved = a.resolve_actions(entry_result.actions, _BASE_TS)
        exit_cid = next(r for r in resolved if r.kind == ActionIntentKind.PLACE_EXIT).cid

        exit_order = sm.snapshot.exit_orders[0]
        exit_fill = a.translate_fill(
            exit_cid, OrderSide.SELL, exit_order.price, _ORDER_SIZE, _BASE_TS + 1000
        )
        assert exit_fill is not None
        sm.apply(exit_fill.event)
        a.confirm_exit_fill(exit_cid)
        assert sm.mode == BranchMode.FLAT

        # Recenter
        new_ref = Decimal("51000")
        recenter_result = sm.apply(
            RecenterRequested(new_reference_price=new_ref, ts=_BASE_TS + 2000)
        )
        assert not recenter_result.rejected

        # Seed new window
        # First confirm cancel of old entries (they were in window before fill)
        for cid in list(a.registry.all_entry_cids):
            a.confirm_cancel_entry(cid)
        new_seed = a.seed_entry_window(sm.snapshot.entry_window, _BASE_TS + 2000)
        assert len(new_seed) == 6


class TestIdempotency:
    def test_duplicate_entry_fill_same_event(self) -> None:
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        a.registry.register_entry(cid, OrderSide.BUY, Decimal("49750"))
        r1 = a.translate_fill(cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE, _BASE_TS)
        r2 = a.translate_fill(cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE, _BASE_TS)
        assert r1 is not None and r2 is not None
        assert r1.event == r2.event

    def test_duplicate_registration_rejected(self) -> None:
        r = GridV2OrderRegistry()
        r.register_entry("cid1", OrderSide.BUY, Decimal("100"))
        with pytest.raises(ValueError, match="Duplicate"):
            r.register_entry("cid1", OrderSide.BUY, Decimal("100"))


class TestEdgeCases:
    def test_zero_qty_passes_through(self) -> None:
        """Adapter passes through; SM rejects INVALID_QUANTITY."""
        a = _adapter()
        cid = a.generate_entry_cid(_BASE_TS)
        a.registry.register_entry(cid, OrderSide.BUY, Decimal("49750"))
        fill = a.translate_fill(cid, OrderSide.BUY, Decimal("49750"), Decimal(0), _BASE_TS)
        assert fill is not None
        assert fill.event.qty == Decimal(0)

    def test_empty_exchange_reconcile(self) -> None:
        a = _adapter()
        sm = _sm()
        result = a.reconcile(sm.snapshot, frozenset(), _BASE_TS)
        assert result.matched_entries == 0
        assert result.matched_exits == 0
        assert len(result.mismatches) == 0

    def test_exit_seq_monotonic(self) -> None:
        a = _adapter()
        cids = [a.generate_exit_cid(_BASE_TS) for _ in range(5)]
        assert len(set(cids)) == 5

    def test_no_orders_flat_valid(self) -> None:
        a = _adapter()
        snap = a.reconstruct_snapshot([], Decimal(0), _REF_PRICE, _BASE_TS)
        assert snap.mode == BranchMode.FLAT
        GridV2StateMachine(_config(), snap)

    def test_compute_max_seq_7char(self) -> None:
        # budget: 36 - 18 - 7 = 11 char level_id, 10 digits
        assert _compute_max_seq("BTCUSDT") == 9999999999

    def test_compute_max_seq_8char(self) -> None:
        # budget: 36 - 18 - 8 = 10 char level_id, 9 digits
        assert _compute_max_seq("SHIBUSDT") == 999999999

    def test_compute_max_seq_12char(self) -> None:
        # budget: 36 - 18 - 12 = 6 char level_id, 5 digits
        assert _compute_max_seq("FARTCOINUSDT") == 99999


class TestCrossSymbolIsolation:
    """P1: Adapter is symbol-scoped. CIDs for other symbols are foreign."""

    def test_parse_cid_wrong_symbol_returns_none(self) -> None:
        """ETHUSDT adapter + BTCUSDT CID -> parse_cid() is None."""
        btc_adapter = _adapter(symbol="BTCUSDT")
        btc_cid = btc_adapter.generate_entry_cid(_BASE_TS)
        eth_adapter = _adapter(symbol="ETHUSDT")
        assert eth_adapter.parse_cid(btc_cid) is None

    def test_is_ours_wrong_symbol_returns_false(self) -> None:
        btc_adapter = _adapter(symbol="BTCUSDT")
        btc_cid = btc_adapter.generate_entry_cid(_BASE_TS)
        eth_adapter = _adapter(symbol="ETHUSDT")
        assert eth_adapter.is_ours(btc_cid) is False

    def test_translate_fill_wrong_symbol_returns_none(self) -> None:
        """Cross-symbol CID → None (foreign), not ValueError (stale)."""
        btc_adapter = _adapter(symbol="BTCUSDT")
        btc_cid = btc_adapter.generate_entry_cid(_BASE_TS)
        # Register in BTC adapter so it's a valid CID on exchange
        btc_adapter.registry.register_entry(btc_cid, OrderSide.BUY, Decimal("49750"))

        eth_adapter = _adapter(symbol="ETHUSDT")
        result = eth_adapter.translate_fill(
            btc_cid,
            OrderSide.BUY,
            Decimal("49750"),
            _ORDER_SIZE,
            _BASE_TS,
        )
        assert result is None

    def test_reconcile_ignores_wrong_symbol_g_cids(self) -> None:
        """Cross-symbol g-CIDs on exchange are not flagged as UNEXPECTED."""
        btc_adapter = _adapter(symbol="BTCUSDT")
        btc_cid = btc_adapter.generate_entry_cid(_BASE_TS)

        eth_adapter = _adapter(symbol="ETHUSDT")
        sm = _sm()
        result = eth_adapter.reconcile(sm.snapshot, frozenset({btc_cid}), _BASE_TS)
        unexpected = [m for m in result.mismatches if m.kind == GridV2MismatchKind.UNEXPECTED_ORDER]
        assert len(unexpected) == 0

    def test_reconstruct_ignores_wrong_symbol_g_cids(self) -> None:
        """Cross-symbol g-CIDs are skipped during reconstruction."""
        btc_adapter = _adapter(symbol="BTCUSDT")
        btc_entry_cid = btc_adapter.generate_entry_cid(_BASE_TS)

        eth_adapter = _adapter(symbol="ETHUSDT")
        # BTC CID should be ignored; reconstruction sees 0 classified → flat
        snap = eth_adapter.reconstruct_snapshot(
            [(btc_entry_cid, OrderSide.BUY, Decimal("49750"), _ORDER_SIZE)],
            Decimal(0),
            _REF_PRICE,
            _BASE_TS,
        )
        assert snap.mode == BranchMode.FLAT
        assert eth_adapter.registry.entry_count == 0

    def test_same_symbol_still_works(self) -> None:
        """Sanity: same-symbol CID is still recognized."""
        a = _adapter(symbol="BTCUSDT")
        cid = a.generate_entry_cid(_BASE_TS)
        assert a.is_ours(cid) is True
        assert a.parse_cid(cid) is not None


class TestReverseKeyCollision:
    """P2: Duplicate reverse keys must be rejected (fail-closed)."""

    def test_duplicate_side_price_entry_raises(self) -> None:
        """Two different CIDs at same (side, price) → ValueError."""
        r = GridV2OrderRegistry()
        r.register_entry("cid1", OrderSide.BUY, Decimal("49750"))
        with pytest.raises(ValueError, match="Duplicate entry"):
            r.register_entry("cid2", OrderSide.BUY, Decimal("49750"))

    def test_duplicate_exit_order_id_raises(self) -> None:
        """Two different CIDs with same exit_order_id → ValueError."""
        r = GridV2OrderRegistry()
        r.register_exit("cid1", "exit-E1", "lot-E1")
        with pytest.raises(ValueError, match="Duplicate exit_order_id"):
            r.register_exit("cid2", "exit-E1", "lot-E2")

    def test_same_side_different_price_ok(self) -> None:
        """Same side, different prices → no collision."""
        r = GridV2OrderRegistry()
        r.register_entry("cid1", OrderSide.BUY, Decimal("49750"))
        r.register_entry("cid2", OrderSide.BUY, Decimal("49500"))
        assert r.entry_count == 2

    def test_same_price_different_side_ok(self) -> None:
        """Same price, different sides → no collision."""
        r = GridV2OrderRegistry()
        r.register_entry("cid1", OrderSide.BUY, Decimal("50000"))
        r.register_entry("cid2", OrderSide.SELL, Decimal("50000"))
        assert r.entry_count == 2

    def test_remove_then_reregister_ok(self) -> None:
        """After remove, same reverse key can be reused."""
        r = GridV2OrderRegistry()
        r.register_entry("cid1", OrderSide.BUY, Decimal("49750"))
        r.remove_entry("cid1")
        r.register_entry("cid2", OrderSide.BUY, Decimal("49750"))
        assert r.cid_for_entry(OrderSide.BUY, Decimal("49750")) == "cid2"

    def test_exit_remove_then_reregister_ok(self) -> None:
        """After exit remove, same exit_order_id can be reused."""
        r = GridV2OrderRegistry()
        r.register_exit("cid1", "exit-E1", "lot-E1")
        r.remove_exit("cid1")
        r.register_exit("cid2", "exit-E1", "lot-E2")
        assert r.cid_for_exit("exit-E1") == "cid2"
