"""Shadow tuning: startup-time visibility into symbol tunability (PR-B3a, ADR-125).

Runs the deterministic tuning solver for each symbol on startup and logs
the outcome. Pure shadow — no dispatch mutation, no selector change, no
runtime behavior change.

Every input symbol produces exactly one outcome log line:
- ``SYMBOL_TUNED symbol=X order_size=N tick_size=T step_size=S min_notional=M price=P``
- ``SYMBOL_NO_GO symbol=X reason=R price=P min_notional=M``
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from grinder.tuning.solver import TuningStatus, solve

if TYPE_CHECKING:
    from grinder.execution.engine import SymbolConstraints
    from grinder.tuning.solver import TuningResult, TuningSolverConfig

logger = logging.getLogger(__name__)

# Sentinel for missing constraints — forces solver to emit a clear NO_GO
_ZERO_CONSTRAINTS_SENTINEL: SymbolConstraints | None = None


def _get_zero_constraints() -> SymbolConstraints:
    """Lazy-import to avoid circular dependency at module level."""
    global _ZERO_CONSTRAINTS_SENTINEL  # noqa: PLW0603
    if _ZERO_CONSTRAINTS_SENTINEL is None:
        from grinder.execution.engine import SymbolConstraints as SC  # noqa: PLC0415

        _ZERO_CONSTRAINTS_SENTINEL = SC(
            step_size=Decimal("0"),
            min_qty=Decimal("0"),
            tick_size=Decimal("0"),
            min_notional=Decimal("0"),
        )
    return _ZERO_CONSTRAINTS_SENTINEL


def run_tuning_shadow(
    symbols: list[str],
    constraints: dict[str, SymbolConstraints] | None,
    prices: dict[str, Decimal],
    config: TuningSolverConfig,
) -> list[TuningResult]:
    """Run tuning solver for each symbol and log outcomes.

    Every symbol produces exactly one outcome — either TUNED or NO_GO.
    Missing constraints or prices are handled gracefully by constructing
    inputs that cause the solver to emit the appropriate NO_GO reason.

    This is shadow-only: the returned results are informational.
    Nothing is mutated in the runtime.

    Args:
        symbols: List of trading pairs to evaluate.
        constraints: Symbol constraints from ConstraintProvider (may be None).
        prices: Map of symbol -> current price (may be empty/partial).
        config: Tuning solver configuration (risk caps, grid knobs, blacklist).

    Returns:
        List of TuningResult, one per input symbol, in input order.
    """
    if not symbols:
        return []

    effective_constraints = constraints or {}
    results: list[TuningResult] = []

    for symbol in symbols:
        sc = effective_constraints.get(symbol, _get_zero_constraints())
        price = prices.get(symbol, Decimal("0"))

        result = solve(symbol, sc, price, config)
        results.append(result)

        if result.status == TuningStatus.TUNED:
            logger.info(
                "SYMBOL_TUNED symbol=%s order_size=%s tick_size=%s step_size=%s"
                " min_notional=%s price=%s",
                symbol,
                result.order_size,
                result.tick_size,
                result.step_size,
                sc.min_notional,
                price,
            )
        else:
            logger.info(
                "SYMBOL_NO_GO symbol=%s reason=%s price=%s min_notional=%s",
                symbol,
                result.reason.value if result.reason else "UNKNOWN",
                price,
                sc.min_notional,
            )

    tuned = sum(1 for r in results if r.status == TuningStatus.TUNED)
    no_go = len(results) - tuned
    logger.info(
        "TUNING_SHADOW_COMPLETE symbols=%d tuned=%d no_go=%d",
        len(results),
        tuned,
        no_go,
    )

    return results
