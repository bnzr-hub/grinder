"""Tests for SharedRegimeRegistry — thread-safe per-symbol regime state."""

from __future__ import annotations

from grinder.controller.regime import Regime, RegimeReason
from grinder.risk.regime_registry import SharedRegimeRegistry


class TestPublish:
    def test_first_publish(self) -> None:
        """First publish for a symbol stores snapshot."""
        reg = SharedRegimeRegistry()
        changed = reg.publish("BTCUSDT", Regime.RANGE, RegimeReason.DEFAULT, 80)
        assert changed is True
        snap = reg.get("BTCUSDT")
        assert snap is not None
        assert snap.regime == Regime.RANGE
        assert snap.reason == RegimeReason.DEFAULT
        assert snap.confidence == 80

    def test_same_regime_not_changed(self) -> None:
        """Publishing same regime returns changed=False."""
        reg = SharedRegimeRegistry()
        reg.publish("BTCUSDT", Regime.RANGE, RegimeReason.DEFAULT, 80)
        changed = reg.publish("BTCUSDT", Regime.RANGE, RegimeReason.DEFAULT, 80)
        assert changed is False

    def test_different_regime_changed(self) -> None:
        """Publishing different regime returns changed=True."""
        reg = SharedRegimeRegistry()
        reg.publish("BTCUSDT", Regime.RANGE, RegimeReason.DEFAULT, 80)
        changed = reg.publish("BTCUSDT", Regime.VOL_SHOCK, RegimeReason.HIGH_VOLATILITY, 85)
        assert changed is True
        snap = reg.get("BTCUSDT")
        assert snap is not None
        assert snap.regime == Regime.VOL_SHOCK

    def test_multiple_symbols(self) -> None:
        """Multiple symbols tracked independently."""
        reg = SharedRegimeRegistry()
        reg.publish("BTCUSDT", Regime.RANGE, RegimeReason.DEFAULT, 80)
        reg.publish("ETHUSDT", Regime.TOXIC, RegimeReason.SPREAD_SPIKE, 100)
        assert len(reg) == 2
        assert reg.get("BTCUSDT").regime == Regime.RANGE
        assert reg.get("ETHUSDT").regime == Regime.TOXIC


class TestRemove:
    def test_remove_existing(self) -> None:
        """Remove existing symbol returns True."""
        reg = SharedRegimeRegistry()
        reg.publish("BTCUSDT", Regime.RANGE, RegimeReason.DEFAULT, 80)
        removed = reg.remove("BTCUSDT")
        assert removed is True
        assert reg.get("BTCUSDT") is None
        assert len(reg) == 0

    def test_remove_absent(self) -> None:
        """Remove absent symbol returns False."""
        reg = SharedRegimeRegistry()
        removed = reg.remove("BTCUSDT")
        assert removed is False


class TestSnapshot:
    def test_snapshot_copy(self) -> None:
        """Snapshot returns a copy, not the internal dict."""
        reg = SharedRegimeRegistry()
        reg.publish("BTCUSDT", Regime.RANGE, RegimeReason.DEFAULT, 80)
        snap = reg.snapshot()
        assert "BTCUSDT" in snap
        # Mutating copy does not affect registry
        snap.pop("BTCUSDT")
        assert reg.get("BTCUSDT") is not None

    def test_symbols(self) -> None:
        reg = SharedRegimeRegistry()
        reg.publish("BTCUSDT", Regime.RANGE, RegimeReason.DEFAULT, 80)
        reg.publish("ETHUSDT", Regime.TOXIC, RegimeReason.SPREAD_SPIKE, 100)
        assert reg.symbols() == frozenset({"BTCUSDT", "ETHUSDT"})


class TestGetAbsent:
    def test_absent_returns_none(self) -> None:
        reg = SharedRegimeRegistry()
        assert reg.get("NOPE") is None


class TestLifecycle:
    def test_publish_remove_republish(self) -> None:
        """Full lifecycle: publish → remove → republish."""
        reg = SharedRegimeRegistry()
        reg.publish("BTCUSDT", Regime.RANGE, RegimeReason.DEFAULT, 80)
        reg.remove("BTCUSDT")
        assert reg.get("BTCUSDT") is None
        changed = reg.publish("BTCUSDT", Regime.TOXIC, RegimeReason.SPREAD_SPIKE, 100)
        assert changed is True
        assert reg.get("BTCUSDT").regime == Regime.TOXIC


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        reg = SharedRegimeRegistry()
        reg.publish("A", Regime.RANGE, RegimeReason.DEFAULT, 80)
        reg.publish("B", Regime.TOXIC, RegimeReason.SPREAD_SPIKE, 100)
        s1 = reg.snapshot()
        s2 = reg.snapshot()
        assert s1 == s2
