"""Tests for shadow tuning startup helper (PR-B3a, ADR-125).

Covers:
- Every input symbol produces exactly one outcome
- NO_GO is logged but non-fatal
- Empty symbols list produces no output
- Missing price produces PRICE_UNAVAILABLE
- Missing constraints produces a visible NO_GO
- No runtime mutation (pure shadow)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from grinder.execution.engine import SymbolConstraints
from grinder.tuning.shadow import run_tuning_shadow
from grinder.tuning.solver import NoGoReason, TuningSolverConfig, TuningStatus

BTC_CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    tick_size=Decimal("0.10"),
    min_notional=Decimal("5"),
)

ETH_CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    tick_size=Decimal("0.01"),
    min_notional=Decimal("5"),
)

CHEAP_CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("1"),
    min_qty=Decimal("1"),
    tick_size=Decimal("0.0001"),
    min_notional=Decimal("5"),
)

DEFAULT_CONFIG = TuningSolverConfig(
    max_position_usd=Decimal("10000"),
    max_inventory_levels=5,
)


class TestThreeSymbolsThreeOutcomes:
    """Every input symbol produces exactly one outcome."""

    def test_three_symbols_three_results(self) -> None:
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        constraints = {
            "BTCUSDT": BTC_CONSTRAINTS,
            "ETHUSDT": ETH_CONSTRAINTS,
            "SOLUSDT": SymbolConstraints(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                tick_size=Decimal("0.001"),
                min_notional=Decimal("5"),
            ),
        }
        prices = {
            "BTCUSDT": Decimal("80000"),
            "ETHUSDT": Decimal("3000"),
            "SOLUSDT": Decimal("150"),
        }

        results = run_tuning_shadow(symbols, constraints, prices, DEFAULT_CONFIG)

        assert len(results) == 3
        assert results[0].symbol == "BTCUSDT"
        assert results[1].symbol == "ETHUSDT"
        assert results[2].symbol == "SOLUSDT"

    def test_ordering_preserved(self) -> None:
        symbols = ["ETHUSDT", "BTCUSDT"]
        constraints = {"BTCUSDT": BTC_CONSTRAINTS, "ETHUSDT": ETH_CONSTRAINTS}
        prices = {"BTCUSDT": Decimal("80000"), "ETHUSDT": Decimal("3000")}

        results = run_tuning_shadow(symbols, constraints, prices, DEFAULT_CONFIG)

        assert [r.symbol for r in results] == ["ETHUSDT", "BTCUSDT"]


class TestNoGoNonFatal:
    """NO_GO is logged but does not crash or block."""

    def test_no_go_logged_but_non_fatal(self) -> None:
        # BTCUSDT: order=0.001, worst = 0.001*80000*5 = 400 (TUNED at cap=10000)
        # NOPRICUSDT: price missing → PRICE_UNAVAILABLE (NO_GO)
        symbols = ["BTCUSDT", "NOPRICUSDT"]
        constraints = {
            "BTCUSDT": BTC_CONSTRAINTS,
            "NOPRICUSDT": BTC_CONSTRAINTS,
        }
        prices = {"BTCUSDT": Decimal("80000")}  # NOPRICUSDT has no price

        results = run_tuning_shadow(symbols, constraints, prices, DEFAULT_CONFIG)

        assert len(results) == 2
        btc = results[0]
        nopric = results[1]
        assert btc.status == TuningStatus.TUNED
        assert nopric.status == TuningStatus.NO_GO
        assert nopric.reason == NoGoReason.PRICE_UNAVAILABLE


class TestEmptySymbols:
    """Empty symbols list produces no output, no crash."""

    def test_empty_list(self) -> None:
        results = run_tuning_shadow([], {}, {}, DEFAULT_CONFIG)
        assert results == []


class TestPriceUnavailable:
    """Missing price for a symbol produces PRICE_UNAVAILABLE."""

    def test_missing_price_produces_no_go(self) -> None:
        symbols = ["BTCUSDT"]
        constraints = {"BTCUSDT": BTC_CONSTRAINTS}
        prices: dict[str, Decimal] = {}  # no price for BTCUSDT

        results = run_tuning_shadow(symbols, constraints, prices, DEFAULT_CONFIG)

        assert len(results) == 1
        assert results[0].status == TuningStatus.NO_GO
        assert results[0].reason == NoGoReason.PRICE_UNAVAILABLE

    def test_partial_prices(self) -> None:
        """One symbol has price, other doesn't."""
        symbols = ["BTCUSDT", "ETHUSDT"]
        constraints = {"BTCUSDT": BTC_CONSTRAINTS, "ETHUSDT": ETH_CONSTRAINTS}
        prices = {"BTCUSDT": Decimal("80000")}  # ETHUSDT missing

        results = run_tuning_shadow(symbols, constraints, prices, DEFAULT_CONFIG)

        assert len(results) == 2
        assert results[0].status == TuningStatus.TUNED
        assert results[1].status == TuningStatus.NO_GO
        assert results[1].reason == NoGoReason.PRICE_UNAVAILABLE


class TestConstraintsUnavailable:
    """Missing constraints produce a visible NO_GO, not a silent skip."""

    def test_constraints_none(self) -> None:
        """Entire constraints dict is None."""
        symbols = ["BTCUSDT"]
        prices = {"BTCUSDT": Decimal("80000")}

        results = run_tuning_shadow(symbols, None, prices, DEFAULT_CONFIG)

        assert len(results) == 1
        assert results[0].status == TuningStatus.NO_GO
        # Zero constraints → tick_size=0 checked first → TICK_SIZE_UNAVAILABLE
        assert results[0].reason == NoGoReason.TICK_SIZE_UNAVAILABLE

    def test_symbol_missing_from_constraints(self) -> None:
        """Symbol not in constraints dict."""
        symbols = ["BTCUSDT", "UNKNOWNUSDT"]
        constraints = {"BTCUSDT": BTC_CONSTRAINTS}
        prices = {"BTCUSDT": Decimal("80000"), "UNKNOWNUSDT": Decimal("10")}

        results = run_tuning_shadow(symbols, constraints, prices, DEFAULT_CONFIG)

        assert len(results) == 2
        assert results[0].status == TuningStatus.TUNED
        assert results[1].status == TuningStatus.NO_GO
        assert results[1].symbol == "UNKNOWNUSDT"


class TestNoRuntimeMutation:
    """Shadow helper is pure — no side effects beyond logging."""

    def test_returns_results_without_mutation(self) -> None:
        """Results are returned, inputs unchanged."""
        symbols = ["BTCUSDT"]
        constraints = {"BTCUSDT": BTC_CONSTRAINTS}
        prices = {"BTCUSDT": Decimal("80000")}

        results = run_tuning_shadow(symbols, constraints, prices, DEFAULT_CONFIG)

        # Returned results are informational only
        assert len(results) == 1
        assert results[0].status == TuningStatus.TUNED
        # Input collections unchanged
        assert len(constraints) == 1
        assert len(prices) == 1


class TestLogFormat:
    """Log lines match doc 37 signal format."""

    def test_tuned_log_format(self) -> None:
        """SYMBOL_TUNED log contains required fields."""
        with LogCapture("grinder.tuning.shadow") as logs:
            run_tuning_shadow(
                ["BTCUSDT"],
                {"BTCUSDT": BTC_CONSTRAINTS},
                {"BTCUSDT": Decimal("80000")},
                DEFAULT_CONFIG,
            )

        tuned_lines = [m for m in logs.messages if "SYMBOL_TUNED" in m]
        assert len(tuned_lines) == 1
        line = tuned_lines[0]
        assert "symbol=BTCUSDT" in line
        assert "order_size=" in line
        assert "tick_size=" in line
        assert "step_size=" in line
        assert "price=" in line

    def test_no_go_log_format(self) -> None:
        """SYMBOL_NO_GO log contains required fields."""
        with LogCapture("grinder.tuning.shadow") as logs:
            run_tuning_shadow(
                ["BTCUSDT"],
                {"BTCUSDT": BTC_CONSTRAINTS},
                {},  # no price
                DEFAULT_CONFIG,
            )

        no_go_lines = [m for m in logs.messages if "SYMBOL_NO_GO" in m]
        assert len(no_go_lines) == 1
        line = no_go_lines[0]
        assert "symbol=BTCUSDT" in line
        assert "reason=PRICE_UNAVAILABLE" in line
        assert "price=" in line

    def test_summary_log(self) -> None:
        """TUNING_SHADOW_COMPLETE summary emitted."""
        with LogCapture("grinder.tuning.shadow") as logs:
            run_tuning_shadow(
                ["BTCUSDT", "ETHUSDT"],
                {"BTCUSDT": BTC_CONSTRAINTS, "ETHUSDT": ETH_CONSTRAINTS},
                {"BTCUSDT": Decimal("80000")},  # ETHUSDT missing
                DEFAULT_CONFIG,
            )

        summary = [m for m in logs.messages if "TUNING_SHADOW_COMPLETE" in m]
        assert len(summary) == 1
        assert "symbols=2" in summary[0]
        assert "tuned=1" in summary[0]
        assert "no_go=1" in summary[0]


class TestDeterminism:
    """Same inputs produce identical results."""

    def test_repeated_calls_identical(self) -> None:
        symbols = ["BTCUSDT"]
        constraints = {"BTCUSDT": BTC_CONSTRAINTS}
        prices = {"BTCUSDT": Decimal("80000")}

        r1 = run_tuning_shadow(symbols, constraints, prices, DEFAULT_CONFIG)
        r2 = run_tuning_shadow(symbols, constraints, prices, DEFAULT_CONFIG)

        assert r1 == r2


# --- Tests: _run_startup_tuning_shadow wrapper (call-site integration) ---


class TestRunStartupTuningShadowWrapper:
    """Tests for the production call-site wrapper in run_trading.py."""

    def test_invokes_shadow_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrapper calls run_tuning_shadow and returns normally."""
        import scripts.run_trading as rt  # noqa: PLC0415

        called_with: list[tuple[list[str], object]] = []

        def fake_shadow(
            symbols: list[str],
            constraints: object,
            prices: object,
            config: object,
        ) -> list[object]:
            called_with.append((symbols, constraints))
            return []

        monkeypatch.setattr("grinder.tuning.shadow.run_tuning_shadow", fake_shadow)

        rt._run_startup_tuning_shadow(["BTCUSDT"], {"BTCUSDT": BTC_CONSTRAINTS})

        assert len(called_with) == 1
        assert called_with[0][0] == ["BTCUSDT"]

    def test_fail_open_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exception inside shadow path is swallowed — startup continues."""
        import scripts.run_trading as rt  # noqa: PLC0415

        def exploding_shadow(*args: object, **kwargs: object) -> list[object]:
            raise RuntimeError("solver exploded")

        monkeypatch.setattr("grinder.tuning.shadow.run_tuning_shadow", exploding_shadow)

        # Must not raise
        rt._run_startup_tuning_shadow(["BTCUSDT"], {"BTCUSDT": BTC_CONSTRAINTS})

    def test_passes_empty_prices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrapper passes empty prices dict (no new network I/O in B3a)."""
        import scripts.run_trading as rt  # noqa: PLC0415

        received_prices: list[dict[str, Decimal]] = []

        def capture_shadow(
            symbols: list[str],
            constraints: object,
            prices: dict[str, Decimal],
            config: object,
        ) -> list[object]:
            received_prices.append(prices)
            return []

        monkeypatch.setattr("grinder.tuning.shadow.run_tuning_shadow", capture_shadow)

        rt._run_startup_tuning_shadow(["BTCUSDT"], {"BTCUSDT": BTC_CONSTRAINTS})

        assert len(received_prices) == 1
        assert received_prices[0] == {}

    def test_populates_cache(self) -> None:
        """Wrapper records results into TuningCache."""
        import scripts.run_trading as rt  # noqa: PLC0415

        from grinder.tuning.cache import get_tuning_cache, reset_tuning_cache  # noqa: PLC0415

        reset_tuning_cache()
        rt._run_startup_tuning_shadow(["BTCUSDT"], {"BTCUSDT": BTC_CONSTRAINTS})

        cache = get_tuning_cache()
        result = cache.get("BTCUSDT")
        assert result is not None
        assert result.symbol == "BTCUSDT"
        reset_tuning_cache()

    def test_records_metrics(self) -> None:
        """Wrapper records results into TuningMetrics."""
        import scripts.run_trading as rt  # noqa: PLC0415

        from grinder.tuning.metrics import get_tuning_metrics, reset_tuning_metrics  # noqa: PLC0415

        reset_tuning_metrics()
        rt._run_startup_tuning_shadow(["BTCUSDT", "ETHUSDT"], {"BTCUSDT": BTC_CONSTRAINTS})

        m = get_tuning_metrics()
        total = sum(m.result_total.values())
        assert total == 2  # both symbols get a result
        reset_tuning_metrics()

    def test_fail_open_with_cache_metrics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cache/metrics errors don't block startup."""
        import scripts.run_trading as rt  # noqa: PLC0415

        from grinder.tuning.metrics import reset_tuning_metrics  # noqa: PLC0415

        def exploding_cache_put(*args: object, **kwargs: object) -> None:
            raise RuntimeError("cache broken")

        monkeypatch.setattr("grinder.tuning.cache.TuningCache.put", exploding_cache_put)
        reset_tuning_metrics()

        # Must not raise
        rt._run_startup_tuning_shadow(["BTCUSDT"], {"BTCUSDT": BTC_CONSTRAINTS})
        reset_tuning_metrics()


# --- Helper for capturing log output ---


class LogCapture:
    """Context manager that captures log messages from a named logger."""

    def __init__(self, logger_name: str) -> None:
        self.logger_name = logger_name
        self.messages: list[str] = []
        self._handler: logging.Handler | None = None

    def __enter__(self) -> LogCapture:
        _logger = logging.getLogger(self.logger_name)
        self._handler = _CaptureHandler(self.messages)
        self._handler.setLevel(logging.DEBUG)
        _logger.addHandler(self._handler)
        _logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *args: object) -> None:
        if self._handler:
            logging.getLogger(self.logger_name).removeHandler(self._handler)


class _CaptureHandler(logging.Handler):
    def __init__(self, messages: list[str]) -> None:
        super().__init__()
        self._messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        self._messages.append(self.format(record))
