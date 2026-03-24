"""Tests for grid_v2 geometry price alignment helpers."""

from __future__ import annotations

from decimal import Decimal

from grinder.grid_v2.geometry import is_price_aligned, match_entries_with_tolerance


class TestIsPriceAligned:
    """Test 1-2: mismatch detect + tolerance."""

    def test_exact_match(self) -> None:
        assert is_price_aligned(Decimal("100.50"), Decimal("100.50"), Decimal("0.10"))

    def test_within_epsilon(self) -> None:
        # 1 tick tolerance, diff = 0.10 = exactly 1 tick
        assert is_price_aligned(
            Decimal("100.50"), Decimal("100.60"), Decimal("0.10"), epsilon_ticks=1
        )

    def test_exceeds_epsilon(self) -> None:
        # diff = 0.20 > 1 tick
        assert not is_price_aligned(
            Decimal("100.50"), Decimal("100.70"), Decimal("0.10"), epsilon_ticks=1
        )

    def test_zero_tick_always_aligned(self) -> None:
        assert is_price_aligned(Decimal("100"), Decimal("200"), Decimal("0"))

    def test_epsilon_2_ticks(self) -> None:
        # diff = 0.20, 2 ticks of 0.10
        assert is_price_aligned(
            Decimal("100.50"), Decimal("100.70"), Decimal("0.10"), epsilon_ticks=2
        )


class TestMatchEntriesWithTolerance:
    """Tests 1-4: matching, tolerance, repair action generation, structural."""

    def test_exact_match_no_mismatches(self) -> None:
        expected = {("BUY", Decimal("100.00")), ("SELL", Decimal("101.00"))}
        actual = {
            ("BUY", Decimal("100.00")): "cid_b",
            ("SELL", Decimal("101.00")): "cid_s",
        }
        matched, missing, extra, geo = match_entries_with_tolerance(
            expected, actual, Decimal("0.10")
        )
        assert len(matched) == 2
        assert not missing
        assert not extra
        assert not geo

    def test_within_epsilon_silently_matched(self) -> None:
        """Order within epsilon = silently matched, no geometry mismatch, no repair."""
        expected = {("BUY", Decimal("100.00"))}
        actual = {("BUY", Decimal("100.10")): "cid_b"}  # 1 tick off, within epsilon=1
        matched, missing, extra, geo = match_entries_with_tolerance(
            expected, actual, Decimal("0.10"), epsilon_ticks=1
        )
        assert len(matched) == 1
        assert not missing
        assert not extra
        assert not geo  # within epsilon = no mismatch, no repair

    def test_beyond_epsilon_is_structural(self) -> None:
        """Order too far from expected -> structural missing + extra."""
        expected = {("BUY", Decimal("100.00"))}
        actual = {("BUY", Decimal("100.30")): "cid_b"}  # 3 ticks off, epsilon=1
        matched, missing, extra, geo = match_entries_with_tolerance(
            expected, actual, Decimal("0.10"), epsilon_ticks=1
        )
        assert not matched
        assert missing == {("BUY", Decimal("100.00"))}
        assert extra == {("BUY", Decimal("100.30"))}
        assert not geo

    def test_mixed_exact_and_geometry(self) -> None:
        expected = {
            ("BUY", Decimal("100.00")),
            ("SELL", Decimal("101.00")),
        }
        actual = {
            ("BUY", Decimal("100.00")): "cid_exact",  # exact
            ("SELL", Decimal("101.10")): "cid_drift",  # 1 tick off
        }
        matched, missing, extra, geo = match_entries_with_tolerance(
            expected, actual, Decimal("0.10"), epsilon_ticks=1
        )
        assert len(matched) == 2
        assert not missing
        assert not extra
        assert not geo  # within epsilon = silently matched

    def test_truly_missing_entry(self) -> None:
        expected = {("BUY", Decimal("100.00")), ("SELL", Decimal("101.00"))}
        actual = {("BUY", Decimal("100.00")): "cid_b"}
        matched, missing, extra, geo = match_entries_with_tolerance(
            expected, actual, Decimal("0.10")
        )
        assert len(matched) == 1
        assert missing == {("SELL", Decimal("101.00"))}
        assert not extra
        assert not geo

    def test_truly_extra_entry(self) -> None:
        expected = {("BUY", Decimal("100.00"))}
        actual = {
            ("BUY", Decimal("100.00")): "cid_b",
            ("SELL", Decimal("105.00")): "cid_extra",
        }
        matched, missing, extra, geo = match_entries_with_tolerance(
            expected, actual, Decimal("0.10")
        )
        assert len(matched) == 1
        assert not missing
        assert extra == {("SELL", Decimal("105.00"))}
        assert not geo

    def test_deterministic_ordering(self) -> None:
        """Same inputs -> same outputs on repeated calls."""
        expected = {("BUY", Decimal("100.00")), ("SELL", Decimal("101.00"))}
        actual = {
            ("BUY", Decimal("100.30")): "cid_b",  # outside epsilon=1
            ("SELL", Decimal("101.30")): "cid_s",  # outside epsilon=1
        }
        results = []
        for _ in range(3):
            _matched, missing, extra, _geo = match_entries_with_tolerance(
                expected, actual, Decimal("0.10"), epsilon_ticks=1
            )
            results.append(sorted((str(m), str(e)) for m in missing for e in extra))
        assert results[0] == results[1] == results[2]
