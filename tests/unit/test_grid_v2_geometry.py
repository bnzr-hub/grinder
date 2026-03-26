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

    def test_beyond_epsilon_same_side_is_geometry_mismatch(self) -> None:
        """Order outside epsilon on same side -> geometry mismatch (cancel+replace)."""
        expected = {("BUY", Decimal("100.00"))}
        actual = {("BUY", Decimal("100.30")): "cid_b"}  # 3 ticks off, epsilon=1
        matched, missing, extra, geo = match_entries_with_tolerance(
            expected, actual, Decimal("0.10"), epsilon_ticks=1
        )
        assert not matched
        # Paired into geometry mismatch, removed from structural sets
        assert not missing
        assert not extra
        assert len(geo) == 1
        side, expected_price, actual_price, cid = geo[0]
        assert side == "BUY"
        assert expected_price == Decimal("100.00")
        assert actual_price == Decimal("100.30")
        assert cid == "cid_b"

    def test_beyond_epsilon_different_side_is_structural(self) -> None:
        """Order outside epsilon on different side -> structural (not geometry)."""
        expected = {("BUY", Decimal("100.00"))}
        actual = {("SELL", Decimal("100.30")): "cid_s"}  # different side
        matched, missing, extra, geo = match_entries_with_tolerance(
            expected, actual, Decimal("0.10"), epsilon_ticks=1
        )
        assert not matched
        assert missing == {("BUY", Decimal("100.00"))}
        assert extra == {("SELL", Decimal("100.30"))}
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

    def test_geometry_mismatch_multiple_same_side(self) -> None:
        """Multiple same-side extras pair with nearest missing."""
        expected = {("BUY", Decimal("100.00")), ("BUY", Decimal("99.50"))}
        actual = {
            ("BUY", Decimal("100.20")): "cid_a",  # 2 ticks from 100.00
            ("BUY", Decimal("99.70")): "cid_b",  # 2 ticks from 99.50
        }
        matched, missing, extra, geo = match_entries_with_tolerance(
            expected, actual, Decimal("0.10"), epsilon_ticks=1
        )
        assert not matched
        assert not missing  # all paired into geo
        assert not extra
        assert len(geo) == 2
        # Each extra paired with nearest missing on same side
        geo_pairs = {(s, ep, ap) for s, ep, ap, _c in geo}
        assert ("BUY", Decimal("100.00"), Decimal("100.20")) in geo_pairs
        assert ("BUY", Decimal("99.50"), Decimal("99.70")) in geo_pairs

    def test_geometry_mismatch_mixed_with_exact(self) -> None:
        """One exact match + one geometry mismatch in same call."""
        expected = {("BUY", Decimal("100.00")), ("BUY", Decimal("99.50"))}
        actual = {
            ("BUY", Decimal("100.00")): "cid_exact",  # exact
            ("BUY", Decimal("99.30")): "cid_drift",  # 2 ticks from 99.50
        }
        matched, missing, extra, geo = match_entries_with_tolerance(
            expected, actual, Decimal("0.10"), epsilon_ticks=1
        )
        assert len(matched) == 1  # exact only
        assert not missing  # 99.50 paired into geo
        assert not extra  # 99.30 paired into geo
        assert len(geo) == 1
        assert geo[0] == ("BUY", Decimal("99.50"), Decimal("99.30"), "cid_drift")

    def test_no_geometry_mismatch_when_all_exact(self) -> None:
        """All exact -> zero geometry mismatches."""
        expected = {("BUY", Decimal("100.00")), ("SELL", Decimal("101.00"))}
        actual = {
            ("BUY", Decimal("100.00")): "cid_b",
            ("SELL", Decimal("101.00")): "cid_s",
        }
        _, _, _, geo = match_entries_with_tolerance(
            expected, actual, Decimal("0.10"), epsilon_ticks=1
        )
        assert not geo

    def test_deterministic_ordering(self) -> None:
        """Same inputs -> same outputs on repeated calls."""
        expected = {("BUY", Decimal("100.00")), ("SELL", Decimal("101.00"))}
        actual = {
            ("BUY", Decimal("100.30")): "cid_b",  # outside epsilon=1, same-side
            ("SELL", Decimal("101.30")): "cid_s",  # outside epsilon=1, same-side
        }
        results = []
        for _ in range(3):
            _matched, _missing, _extra, geo = match_entries_with_tolerance(
                expected, actual, Decimal("0.10"), epsilon_ticks=1
            )
            # Same-side pairs become geometry mismatches
            results.append(sorted((s, str(ep), str(ap)) for s, ep, ap, _c in geo))
        assert results[0] == results[1] == results[2]
        assert len(results[0]) == 2  # both are geometry mismatches
