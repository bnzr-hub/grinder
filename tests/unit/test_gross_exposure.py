"""Tests for gross exposure computation and bridge cache path."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from grinder.runtime.live_engine_bridge import BridgeConfig, LiveEngineBridge
from grinder.tuning.refresher import compute_gross_exposure_from_positions


class TestComputeGrossExposure:
    def test_mixed_sign_positions(self) -> None:
        """Positive and negative notionals sum by absolute value."""
        positions = [
            {"symbol": "BTCUSDT", "notional": "250"},
            {"symbol": "ETHUSDT", "notional": "-800"},
        ]
        assert compute_gross_exposure_from_positions(positions) == Decimal("1050")

    def test_all_zero(self) -> None:
        """Zero-notional positions contribute 0."""
        positions = [
            {"symbol": "BTCUSDT", "notional": "0"},
            {"symbol": "ETHUSDT", "notional": "0"},
        ]
        assert compute_gross_exposure_from_positions(positions) == Decimal("0")

    def test_empty_list(self) -> None:
        """No positions → 0."""
        assert compute_gross_exposure_from_positions([]) == Decimal("0")

    def test_single_long(self) -> None:
        positions = [{"symbol": "BTCUSDT", "notional": "1500.50"}]
        assert compute_gross_exposure_from_positions(positions) == Decimal("1500.50")

    def test_single_short(self) -> None:
        positions = [{"symbol": "ETHUSDT", "notional": "-2000.75"}]
        assert compute_gross_exposure_from_positions(positions) == Decimal("2000.75")

    def test_malformed_notional_skipped(self) -> None:
        """Bad notional value skipped, valid ones still summed."""
        positions = [
            {"symbol": "BTCUSDT", "notional": "100"},
            {"symbol": "BADUSDT", "notional": "not_a_number"},
            {"symbol": "ETHUSDT", "notional": "200"},
        ]
        assert compute_gross_exposure_from_positions(positions) == Decimal("300")

    def test_missing_notional_uses_zero(self) -> None:
        """Missing notional key defaults to '0'."""
        positions = [
            {"symbol": "BTCUSDT"},
            {"symbol": "ETHUSDT", "notional": "500"},
        ]
        assert compute_gross_exposure_from_positions(positions) == Decimal("500")

    def test_none_notional_skipped(self) -> None:
        """None notional value skipped (fail-open)."""
        positions: list[dict[str, Any]] = [
            {"symbol": "BTCUSDT", "notional": None},
            {"symbol": "ETHUSDT", "notional": "300"},
        ]
        assert compute_gross_exposure_from_positions(positions) == Decimal("300")

    def test_many_positions(self) -> None:
        """Sum across many positions."""
        positions = [{"notional": str(i * 100)} for i in range(1, 6)]
        # 100 + 200 + 300 + 400 + 500 = 1500
        assert compute_gross_exposure_from_positions(positions) == Decimal("1500")

    def test_deterministic(self) -> None:
        positions = [
            {"notional": "250"},
            {"notional": "-800"},
        ]
        r1 = compute_gross_exposure_from_positions(positions)
        r2 = compute_gross_exposure_from_positions(positions)
        assert r1 == r2 == Decimal("1050")


class TestBridgeGrossExposureCache:
    def test_initial_none(self) -> None:
        """Bridge starts with no gross exposure."""
        bridge = LiveEngineBridge(config=BridgeConfig())
        assert bridge.last_known_gross_exposure is None

    def test_update_and_read(self) -> None:
        """Update stores value, property reads it back."""
        bridge = LiveEngineBridge(config=BridgeConfig())
        bridge.update_gross_exposure(Decimal("1050"))
        assert bridge.last_known_gross_exposure == Decimal("1050")

    def test_overwrite(self) -> None:
        """Second update overwrites first."""
        bridge = LiveEngineBridge(config=BridgeConfig())
        bridge.update_gross_exposure(Decimal("500"))
        bridge.update_gross_exposure(Decimal("1200"))
        assert bridge.last_known_gross_exposure == Decimal("1200")
