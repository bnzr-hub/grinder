"""Grid_v2 geometry helpers for price alignment validation.

A level is geometrically valid iff:
    abs(actual_price - expected_price) <= epsilon_ticks * tick_size

Two main functions:
- is_price_aligned: check single price pair
- match_entries_with_tolerance: fuzzy-match actual orders to expected keys
"""

from __future__ import annotations

from decimal import Decimal


def is_price_aligned(
    expected: Decimal,
    actual: Decimal,
    tick_size: Decimal,
    epsilon_ticks: int = 1,
) -> bool:
    """Check if actual price is within epsilon ticks of expected."""
    if tick_size <= 0:
        return True
    tolerance = tick_size * epsilon_ticks
    return abs(actual - expected) <= tolerance


def match_entries_with_tolerance(
    expected_keys: set[tuple[str, Decimal]],
    actual_entries: dict[tuple[str, Decimal], str],
    tick_size: Decimal,
    epsilon_ticks: int = 1,
) -> tuple[
    set[tuple[str, Decimal]],  # matched (actual keys that are close to expected)
    set[tuple[str, Decimal]],  # truly_missing (no actual order near expected)
    set[tuple[str, Decimal]],  # truly_extra (actual orders far from any expected)
    list[tuple[str, Decimal, Decimal, str]],  # geometry_mismatches: (side, expected, actual, cid)
]:
    """Match actual exchange orders to expected keys with tick tolerance.

    Returns:
        - matched: expected keys that have a corresponding actual within tolerance
        - truly_missing: expected keys with no actual order nearby
        - truly_extra: actual keys that don't match any expected key
        - geometry_mismatches: orders within tolerance but not exact match
          (side, expected_price, actual_price, order_id)
    """
    tolerance = tick_size * epsilon_ticks if tick_size > 0 else Decimal(0)

    actual_used: set[tuple[str, Decimal]] = set()
    matched: set[tuple[str, Decimal]] = set()
    geometry_mismatches: list[tuple[str, Decimal, Decimal, str]] = []

    for exp_side, exp_price in sorted(expected_keys):
        # Exact match first
        exact_key = (exp_side, exp_price)
        if exact_key in actual_entries and exact_key not in actual_used:
            matched.add(exact_key)
            actual_used.add(exact_key)
            continue

        # Fuzzy match: find actual order within tolerance on same side
        best_key: tuple[str, Decimal] | None = None
        best_dist = tolerance + Decimal(1)  # worse than tolerance
        for act_key, _cid in actual_entries.items():
            act_side, act_price = act_key
            if act_side != exp_side or act_key in actual_used:
                continue
            dist = abs(act_price - exp_price)
            if dist <= tolerance and dist < best_dist:
                best_dist = dist
                best_key = act_key

        if best_key is not None:
            # Within epsilon = fully matched, no repair needed.
            # The order is "close enough" — silent match.
            matched.add((exp_side, exp_price))
            actual_used.add(best_key)

    truly_missing = expected_keys - matched
    truly_extra = set(actual_entries.keys()) - actual_used

    # Geometry mismatches: actual orders on same side as a missing expected key,
    # outside epsilon tolerance. These need cancel+replace correction.
    # We pair each truly_extra with the nearest truly_missing on the same side.
    # Paired entries are removed from truly_missing/truly_extra (they become
    # geometry corrections, not structural add/remove).
    geo_paired_missing: set[tuple[str, Decimal]] = set()
    geo_paired_extra: set[tuple[str, Decimal]] = set()
    for extra_key in sorted(truly_extra):
        extra_side, extra_price = extra_key
        best_miss: tuple[str, Decimal] | None = None
        best_dist = Decimal("Infinity")
        for miss_key in truly_missing - geo_paired_missing:
            miss_side, miss_price = miss_key
            if miss_side != extra_side:
                continue
            dist = abs(extra_price - miss_price)
            if dist < best_dist:
                best_dist = dist
                best_miss = miss_key
        if best_miss is not None:
            _miss_side, miss_price = best_miss
            cid = actual_entries[extra_key]
            geometry_mismatches.append((extra_side, miss_price, extra_price, cid))
            geo_paired_missing.add(best_miss)
            geo_paired_extra.add(extra_key)

    # Remove paired keys from structural sets — they are geometry corrections
    truly_missing -= geo_paired_missing
    truly_extra -= geo_paired_extra

    return matched, truly_missing, truly_extra, geometry_mismatches
