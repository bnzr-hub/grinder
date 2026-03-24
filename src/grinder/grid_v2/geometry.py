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
            matched.add((exp_side, exp_price))
            actual_used.add(best_key)
            if best_key != exact_key:
                cid = actual_entries[best_key]
                geometry_mismatches.append((exp_side, exp_price, best_key[1], cid))

    truly_missing = expected_keys - matched
    truly_extra = set(actual_entries.keys()) - actual_used
    return matched, truly_missing, truly_extra, geometry_mismatches
