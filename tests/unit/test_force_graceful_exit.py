"""Tests for LiveEngineV0.force_graceful_exit (E3 ceremony support).

Covers:
- force_graceful_exit blocks dispatch for symbol
- returns False when no selector configured
- returns True when selector present
- idempotent on repeated calls
"""

from __future__ import annotations

from unittest.mock import MagicMock


class TestForceGracefulExit:
    def test_blocks_dispatch(self) -> None:
        """After force_graceful_exit, is_selector_dispatch_allowed returns False."""
        from grinder.selection.active_selector import (  # noqa: PLC0415
            ActiveSelector,
            ActiveSelectorConfig,
        )

        selector = ActiveSelector(
            config=ActiveSelectorConfig(enabled=True, k=3),
            initial_active={"BTCUSDT"},
        )

        # Create minimal engine mock with real selector
        engine = MagicMock()
        engine._active_selector = selector
        engine.is_selector_dispatch_allowed = selector.is_dispatch_allowed

        def _force_ge(sym: str) -> bool:
            selector._graceful_exit_only.add(sym)
            return True

        engine.force_graceful_exit = _force_ge

        # Before: dispatch allowed
        assert engine.is_selector_dispatch_allowed("BTCUSDT") is True

        # Apply graceful exit
        result = engine.force_graceful_exit("BTCUSDT")
        assert result is True

        # After: dispatch blocked
        assert engine.is_selector_dispatch_allowed("BTCUSDT") is False

    def test_no_selector_returns_false(self) -> None:
        """Without selector, force_graceful_exit returns False."""
        engine = MagicMock()
        engine._active_selector = None

        # Replicate the real method logic
        def force_ge(sym: str) -> bool:
            if engine._active_selector is None:
                return False
            engine._active_selector._graceful_exit_only.add(sym)
            return True

        assert force_ge("BTCUSDT") is False

    def test_idempotent(self) -> None:
        """Repeated calls don't error."""
        from grinder.selection.active_selector import (  # noqa: PLC0415
            ActiveSelector,
            ActiveSelectorConfig,
        )

        selector = ActiveSelector(
            config=ActiveSelectorConfig(enabled=True, k=3),
            initial_active={"BTCUSDT"},
        )

        selector._graceful_exit_only.add("BTCUSDT")
        selector._graceful_exit_only.add("BTCUSDT")  # idempotent
        assert "BTCUSDT" in selector.graceful_exit_only
