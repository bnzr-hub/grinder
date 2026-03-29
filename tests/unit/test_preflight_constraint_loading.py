"""Regression test for preflight constraint-loading order.

Proves that check_symbol_metadata passes when constraints are loaded
before preflight, and fails when constraints are unavailable.
Matches the exact wiring in run_trading.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from grinder.live.preflight import CheckStatus, check_symbol_metadata


class TestPreflightConstraintLoadingOrder:
    """Launcher must load constraints before preflight metadata check."""

    def test_valid_symbol_passes_with_loaded_constraints(self) -> None:
        """Constraints loaded → check_symbol_metadata PASS for valid symbol.

        This is the exact pattern from run_trading.py: a holder object
        with get_symbol_constraints() returning loaded constraints.
        """

        class _Holder:
            def get_symbol_constraints(self) -> dict[str, object]:
                return {"PIPPINUSDT": {}, "BTCUSDT": {}, "ETHUSDT": {}}

        result = check_symbol_metadata(_Holder(), ["PIPPINUSDT"])
        assert result.status == CheckStatus.PASS
        assert "PIPPINUSDT" in result.detail

    def test_missing_symbol_fails_with_loaded_constraints(self) -> None:
        """Constraints loaded but symbol not in them → FAIL."""

        class _Holder:
            def get_symbol_constraints(self) -> dict[str, object]:
                return {"BTCUSDT": {}, "ETHUSDT": {}}

        result = check_symbol_metadata(_Holder(), ["NOSUCHUSDT"])
        assert result.status == CheckStatus.FAIL
        assert "NOSUCHUSDT" in result.detail

    def test_fails_when_constraints_unavailable(self) -> None:
        """No constraints loaded (port has no get_symbol_constraints) → FAIL.

        This is the pre-fix bug: preflight ran before constraints loaded,
        so port had no metadata, and valid symbols were rejected.
        """
        port_without_constraints = MagicMock(spec=[])  # no attributes
        result = check_symbol_metadata(port_without_constraints, ["PIPPINUSDT"])
        assert result.status == CheckStatus.FAIL
        assert "unavailable" in result.detail

    def test_fails_when_constraints_return_none(self) -> None:
        """get_symbol_constraints() returns None → FAIL."""

        class _Holder:
            def get_symbol_constraints(self) -> None:
                return None

        result = check_symbol_metadata(_Holder(), ["PIPPINUSDT"])
        assert result.status == CheckStatus.FAIL

    def test_fails_with_no_symbols(self) -> None:
        """Empty symbol list → FAIL (fail-closed)."""

        class _Holder:
            def get_symbol_constraints(self) -> dict[str, object]:
                return {"PIPPINUSDT": {}}

        result = check_symbol_metadata(_Holder(), [])
        assert result.status == CheckStatus.FAIL
