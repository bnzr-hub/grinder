"""V1 hard prefilter for symbol selection.

Excludes symbols that don't meet minimum requirements for grid trading.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.selector.models import SelectionFeatures, SkipReason
    from grinder.tuning.solver import TuningResult

logger = logging.getLogger(__name__)

# V1 defaults
DEFAULT_VOLUME_LAST_12X5M_MIN = Decimal("2000000")  # $2M rolling 12x5m quote volume
DEFAULT_NATR_5M_MIN = Decimal("1.0")  # 1% NATR minimum


def prefilter_v1(
    symbols: list[str],
    tuning_results: dict[str, TuningResult],
    features: dict[str, SelectionFeatures],
    *,
    volume_last_12x5m_min: Decimal = DEFAULT_VOLUME_LAST_12X5M_MIN,
    natr_5m_min: Decimal = DEFAULT_NATR_5M_MIN,
    min_spacing_bps: Decimal | None = None,
    blacklist: frozenset[str] = frozenset(),
    max_notional_per_order: Decimal | None = None,
) -> tuple[list[str], dict[str, SkipReason]]:
    """Apply V1 hard prefilter to candidate symbols.

    Returns (eligible, skipped) where skipped maps symbol → reason.
    """
    from grinder.selector.models import SkipReason  # noqa: PLC0415
    from grinder.selector.spacing import (  # noqa: PLC0415
        DEFAULT_MIN_SPACING_BPS,
        compute_adaptive_spacing_bps,
    )
    from grinder.tuning.solver import TuningStatus  # noqa: PLC0415

    _min_spacing = min_spacing_bps if min_spacing_bps is not None else DEFAULT_MIN_SPACING_BPS

    eligible: list[str] = []
    skipped: dict[str, SkipReason] = {}

    for symbol in symbols:
        # Blacklist
        if symbol in blacklist:
            skipped[symbol] = SkipReason.BLACKLISTED
            continue

        # Tuning status
        result = tuning_results.get(symbol)
        if result is None or result.status != TuningStatus.TUNED:
            skipped[symbol] = SkipReason.NOT_TUNED
            continue

        # Features available
        feat = features.get(symbol)
        if feat is None:
            skipped[symbol] = SkipReason.FEATURES_UNAVAILABLE
            continue

        # Rolling 12x5m volume floor
        if feat.quote_volume_last_12x5m < volume_last_12x5m_min:
            skipped[symbol] = SkipReason.LOW_VOLUME_LAST_12X5M
            logger.debug(
                "SELECTOR_PREFILTER_SKIP symbol=%s reason=LOW_VOLUME_LAST_12X5M volume=%s min=%s",
                symbol,
                feat.quote_volume_last_12x5m,
                volume_last_12x5m_min,
            )
            continue

        # NATR floor
        if feat.natr_14_5m < natr_5m_min:
            skipped[symbol] = SkipReason.LOW_NATR_5M
            logger.debug(
                "SELECTOR_PREFILTER_SKIP symbol=%s reason=LOW_NATR_5M natr=%s min=%s",
                symbol,
                feat.natr_14_5m,
                natr_5m_min,
            )
            continue

        # Adaptive grid spacing tradability
        spacing_bps = compute_adaptive_spacing_bps(feat.natr_14_5m)
        if spacing_bps < _min_spacing:
            skipped[symbol] = SkipReason.GRID_SPACING_BELOW_MIN
            logger.debug(
                "SELECTOR_PREFILTER_SKIP symbol=%s reason=GRID_SPACING_BELOW_MIN"
                " spacing_bps=%s min=%s",
                symbol,
                spacing_bps,
                _min_spacing,
            )
            continue

        # Runtime-cap legality: first entry notional must fit max_notional_per_order
        if max_notional_per_order is not None and result.order_size is not None:
            entry_notional = result.order_size * feat.mid_price
            if entry_notional > max_notional_per_order:
                skipped[symbol] = SkipReason.EXCEEDS_RUN_CAP
                logger.debug(
                    "SELECTOR_PREFILTER_SKIP symbol=%s reason=EXCEEDS_RUN_CAP notional=%s cap=%s",
                    symbol,
                    entry_notional,
                    max_notional_per_order,
                )
                continue

        eligible.append(symbol)

    logger.info(
        "SELECTOR_PREFILTER eligible=%d skipped=%d total=%d",
        len(eligible),
        len(skipped),
        len(symbols),
    )
    return eligible, skipped
