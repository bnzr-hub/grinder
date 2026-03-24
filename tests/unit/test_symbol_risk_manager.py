"""Tests for per-symbol risk manager v1."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from grinder.live.engine import LiveEngineV0
from grinder.risk.symbol_risk_manager import (
    SymbolRiskConfig,
    SymbolRiskManager,
    SymbolRiskSnapshot,
    SymbolRiskState,
)


class TestSymbolRiskConfig:
    def test_default_config_disabled(self) -> None:
        cfg = SymbolRiskConfig()
        assert cfg.enabled is False
        assert cfg.max_notional_usd == 0.0
        assert cfg.max_position_qty == 0.0
        assert cfg.max_consecutive_losses == 0

    def test_negative_cooldown_raises(self) -> None:
        with pytest.raises(ValueError, match="cooldown_s"):
            SymbolRiskConfig(cooldown_s=-1)

    def test_negative_ttl_raises(self) -> None:
        with pytest.raises(ValueError, match="exit_only_ttl_s"):
            SymbolRiskConfig(exit_only_ttl_s=-1)

    def test_negative_losses_raises(self) -> None:
        with pytest.raises(ValueError, match="max_consecutive_losses"):
            SymbolRiskConfig(max_consecutive_losses=-1)


class TestSymbolRiskManagerDisabled:
    def test_disabled_always_normal(self) -> None:
        mgr = SymbolRiskManager(SymbolRiskConfig(enabled=False))
        snap = SymbolRiskSnapshot(position_notional_usd=999999, position_qty=999)
        decision = mgr.evaluate("BTCUSDT", snap)
        assert decision.state == SymbolRiskState.NORMAL
        assert mgr.get_state("BTCUSDT") == SymbolRiskState.NORMAL


class TestSymbolRiskManagerNormal:
    def _cfg(self, **kw: object) -> SymbolRiskConfig:
        defaults: dict[str, object] = {
            "enabled": True,
            "max_notional_usd": 10000.0,
            "max_position_qty": 1.0,
            "max_consecutive_losses": 5,
            "cooldown_s": 10.0,
            "exit_only_ttl_s": 30.0,
        }
        defaults.update(kw)
        return SymbolRiskConfig(**defaults)  # type: ignore[arg-type]

    def test_normal_no_escalation(self) -> None:
        mgr = SymbolRiskManager(self._cfg())
        snap = SymbolRiskSnapshot(
            position_notional_usd=5000, position_qty=0.5, consecutive_losses=2
        )
        d = mgr.evaluate("SYM", snap, now=0.0)
        assert d.state == SymbolRiskState.NORMAL
        assert d.escalation_reason == ""

    def test_escalate_by_notional(self) -> None:
        mgr = SymbolRiskManager(self._cfg())
        snap = SymbolRiskSnapshot(
            position_notional_usd=15000, position_qty=0.5, consecutive_losses=0
        )
        d = mgr.evaluate("SYM", snap, now=0.0)
        assert d.state == SymbolRiskState.CAPPED
        assert d.escalation_reason == "NOTIONAL_EXCEEDED"

    def test_escalate_by_qty(self) -> None:
        mgr = SymbolRiskManager(self._cfg())
        snap = SymbolRiskSnapshot(
            position_notional_usd=5000, position_qty=1.5, consecutive_losses=0
        )
        d = mgr.evaluate("SYM", snap, now=0.0)
        assert d.state == SymbolRiskState.CAPPED
        assert d.escalation_reason == "QTY_EXCEEDED"

    def test_escalate_by_consecutive_losses(self) -> None:
        mgr = SymbolRiskManager(self._cfg())
        snap = SymbolRiskSnapshot(
            position_notional_usd=5000, position_qty=0.5, consecutive_losses=5
        )
        d = mgr.evaluate("SYM", snap, now=0.0)
        # Consecutive losses go straight to EXIT_ONLY
        assert d.state == SymbolRiskState.EXIT_ONLY
        assert d.escalation_reason == "CONSECUTIVE_LOSSES"

    def test_capped_blocks_escalation_then_exit_only(self) -> None:
        mgr = SymbolRiskManager(self._cfg())
        # First: trigger CAPPED by notional
        snap_high = SymbolRiskSnapshot(
            position_notional_usd=15000, position_qty=0.5, consecutive_losses=0
        )
        d = mgr.evaluate("SYM", snap_high, now=0.0)
        assert d.state == SymbolRiskState.CAPPED

        # Still over threshold on next tick → escalate to EXIT_ONLY
        d2 = mgr.evaluate("SYM", snap_high, now=1.0)
        assert d2.state == SymbolRiskState.EXIT_ONLY
        assert d2.escalation_reason == "NOTIONAL_EXCEEDED"

    def test_exit_only_stays_until_ttl(self) -> None:
        mgr = SymbolRiskManager(self._cfg(exit_only_ttl_s=30.0, cooldown_s=5.0))
        # Escalate to EXIT_ONLY via consecutive losses (bypasses CAPPED)
        snap_high = SymbolRiskSnapshot(
            position_notional_usd=5000, position_qty=0.5, consecutive_losses=5
        )
        mgr.evaluate("SYM", snap_high, now=0.0)
        assert mgr.get_state("SYM") == SymbolRiskState.EXIT_ONLY

        # Conditions safe, but TTL not expired
        snap_safe = SymbolRiskSnapshot(
            position_notional_usd=3000, position_qty=0.3, consecutive_losses=0
        )
        d = mgr.evaluate("SYM", snap_safe, now=10.0)
        assert d.state == SymbolRiskState.EXIT_ONLY  # TTL not passed

        # After TTL + cooldown
        d2 = mgr.evaluate("SYM", snap_safe, now=35.0)
        assert d2.state == SymbolRiskState.CAPPED  # Step down one level
        assert d2.deescalation_reason == "CONDITIONS_SAFE"

    def test_cooldown_prevents_immediate_deescalation(self) -> None:
        mgr = SymbolRiskManager(self._cfg(cooldown_s=60.0))
        # Escalate to CAPPED
        snap_high = SymbolRiskSnapshot(
            position_notional_usd=15000, position_qty=0.5, consecutive_losses=0
        )
        mgr.evaluate("SYM", snap_high, now=0.0)
        assert mgr.get_state("SYM") == SymbolRiskState.CAPPED

        # Conditions safe but cooldown not expired
        snap_safe = SymbolRiskSnapshot(
            position_notional_usd=3000, position_qty=0.3, consecutive_losses=0
        )
        d = mgr.evaluate("SYM", snap_safe, now=30.0)
        assert d.state == SymbolRiskState.CAPPED  # cooldown active

        # After cooldown
        d2 = mgr.evaluate("SYM", snap_safe, now=65.0)
        assert d2.state == SymbolRiskState.NORMAL
        assert d2.deescalation_reason == "CONDITIONS_SAFE"

    def test_deterministic_on_repeated_snapshots(self) -> None:
        mgr = SymbolRiskManager(self._cfg())
        snap = SymbolRiskSnapshot(
            position_notional_usd=15000, position_qty=0.5, consecutive_losses=0
        )
        # Run same snapshot multiple times
        results = [mgr.evaluate("SYM", snap, now=float(i)) for i in range(5)]
        # First escalates to CAPPED, second to EXIT_ONLY, rest stay EXIT_ONLY
        states = [r.state for r in results]
        assert states[0] == SymbolRiskState.CAPPED
        assert states[1] == SymbolRiskState.EXIT_ONLY
        assert all(s == SymbolRiskState.EXIT_ONLY for s in states[2:])

    def test_disabled_triggers_no_effect(self) -> None:
        mgr = SymbolRiskManager(self._cfg(max_notional_usd=0, max_position_qty=0))
        snap = SymbolRiskSnapshot(
            position_notional_usd=999999, position_qty=999, consecutive_losses=0
        )
        d = mgr.evaluate("SYM", snap, now=0.0)
        assert d.state == SymbolRiskState.NORMAL

    def test_multi_symbol_independent(self) -> None:
        mgr = SymbolRiskManager(self._cfg())
        snap_high = SymbolRiskSnapshot(
            position_notional_usd=15000, position_qty=0.5, consecutive_losses=0
        )
        snap_safe = SymbolRiskSnapshot(
            position_notional_usd=3000, position_qty=0.3, consecutive_losses=0
        )
        mgr.evaluate("SYM_A", snap_high, now=0.0)
        mgr.evaluate("SYM_B", snap_safe, now=0.0)
        assert mgr.get_state("SYM_A") == SymbolRiskState.CAPPED
        assert mgr.get_state("SYM_B") == SymbolRiskState.NORMAL


class TestConsecutiveLossTracking:
    def test_loss_increments(self) -> None:
        engine = MagicMock(spec=LiveEngineV0)
        engine._symbol_consecutive_losses = {}
        LiveEngineV0._record_grid_v2_lot_closed(
            engine, "SYM", Decimal("100"), Decimal("99"), "LONG"
        )
        assert engine._symbol_consecutive_losses["SYM"] == 1
        LiveEngineV0._record_grid_v2_lot_closed(
            engine, "SYM", Decimal("100"), Decimal("98"), "LONG"
        )
        assert engine._symbol_consecutive_losses["SYM"] == 2

    def test_win_resets(self) -> None:
        engine = MagicMock(spec=LiveEngineV0)
        engine._symbol_consecutive_losses = {"SYM": 3}
        LiveEngineV0._record_grid_v2_lot_closed(
            engine, "SYM", Decimal("100"), Decimal("101"), "LONG"
        )
        assert engine._symbol_consecutive_losses["SYM"] == 0

    def test_short_loss(self) -> None:
        engine = MagicMock(spec=LiveEngineV0)
        engine._symbol_consecutive_losses = {}
        # SHORT loss: exit > entry
        LiveEngineV0._record_grid_v2_lot_closed(
            engine, "SYM", Decimal("100"), Decimal("101"), "SHORT"
        )
        assert engine._symbol_consecutive_losses["SYM"] == 1

    def test_short_win(self) -> None:
        engine = MagicMock(spec=LiveEngineV0)
        engine._symbol_consecutive_losses = {"SYM": 2}
        # SHORT win: exit < entry
        LiveEngineV0._record_grid_v2_lot_closed(
            engine, "SYM", Decimal("100"), Decimal("99"), "SHORT"
        )
        assert engine._symbol_consecutive_losses["SYM"] == 0
