"""Tests for deactivation ceremony helpers (PR-C2b, ADR-130).

Covers pure helper logic only — live ceremony is proven via bounded run.
"""

from __future__ import annotations

from unittest.mock import patch

from scripts.deactivation_ceremony import (
    _verify_clean,
    run_preflight,
)


class TestVerifyClean:
    """_verify_clean delegates to exchange_state verify."""

    def test_clean_returns_true(self) -> None:
        with patch("scripts.deactivation_ceremony.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "status=CLEAN"
            mock_run.return_value.stderr = ""

            ok, output = _verify_clean("BTCUSDT")

            assert ok is True
            assert "CLEAN" in output

    def test_dirty_returns_false(self) -> None:
        with patch("scripts.deactivation_ceremony.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = "status=DIRTY"
            mock_run.return_value.stderr = ""

            ok, _output = _verify_clean("BTCUSDT")

            assert ok is False


class TestRunPreflight:
    """run_preflight calls verify and returns boolean."""

    def test_clean_preflight(self) -> None:
        with patch("scripts.deactivation_ceremony._verify_clean", return_value=(True, "CLEAN")):
            assert run_preflight("BTCUSDT") is True

    def test_dirty_preflight(self) -> None:
        with patch("scripts.deactivation_ceremony._verify_clean", return_value=(False, "DIRTY")):
            assert run_preflight("BTCUSDT") is False
