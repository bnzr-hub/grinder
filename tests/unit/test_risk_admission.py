"""Tests for RiskAdmissionGate — admission enforcement combining all risk planners."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from grinder.risk.day_risk_manager import DayRiskMode, DayRiskState
from grinder.risk.grid_risk_sizer import GridRiskSizer
from grinder.risk.portfolio_budget_allocator import (
    MarketRegime,
    PortfolioBudgetAllocator,
    PortfolioBudgetSnapshot,
)
from grinder.risk.risk_admission import (
    AdmissionBlockReason,
    RiskAdmissionGate,
)

_ZERO = Decimal("0")


def _day_state(
    mode: DayRiskMode = DayRiskMode.NORMAL,
    equity: str = "1000",
) -> DayRiskState:
    eq = Decimal(equity)
    return DayRiskState(
        mode=mode,
        equity_day_start=eq,
        equity_day_peak=eq,
        equity_current=eq,
        day_pnl_pct=_ZERO,
        day_peak_pnl_pct=_ZERO,
        profit_lock_floor_pct=_ZERO,
        session_key="2026-04-06",
    )


def _portfolio_snap(
    *,
    equity: str = "1000",
    new_entries_allowed: bool = True,
    risk_budget: str = "20",
    reasons: tuple[str, ...] = (),
) -> PortfolioBudgetSnapshot:
    eq = Decimal(equity)
    return PortfolioBudgetSnapshot(
        equity=eq,
        day_mode=DayRiskMode.NORMAL,
        market_regime=MarketRegime.NEUTRAL,
        base_risk_pct=Decimal("2.0"),
        effective_risk_pct=Decimal("2.0"),
        max_active_symbols=5,
        active_symbol_count=0,
        remaining_symbol_slots=5,
        gross_exposure_limit_usd=eq * Decimal("5"),
        gross_exposure_used_usd=_ZERO,
        gross_exposure_free_usd=eq * Decimal("5"),
        per_symbol_risk_budget_usd=Decimal(risk_budget),
        new_entries_allowed=new_entries_allowed,
        reasons=reasons,
    )


@dataclass(frozen=True)
class _FakeConstraint:
    """Minimal constraint object with exchange fields."""

    step_size: Decimal = Decimal("0.001")
    min_qty: Decimal = Decimal("0.001")
    min_notional: Decimal = Decimal("5")
    tick_size: Decimal = Decimal("0.10")


def _gate() -> RiskAdmissionGate:
    from grinder.risk.day_risk_manager import DayRiskManager  # noqa: PLC0415

    return RiskAdmissionGate(
        day_risk_manager=DayRiskManager(),
        portfolio_allocator=PortfolioBudgetAllocator(),
        grid_sizer=GridRiskSizer(),
    )


class TestEvaluateAdmitted:
    def test_happy_path(self) -> None:
        """Normal day + budget + grid OK → admitted."""
        gate = _gate()
        dec = gate.evaluate(
            "TESTUSDT",
            price=Decimal("100"),
            step_pct=Decimal("0.01"),
            entry_levels=5,
            exchange_min_qty=Decimal("0.001"),
            exchange_min_notional=Decimal("5"),
            qty_step=Decimal("0.001"),
            day_state=_day_state(),
            portfolio_snapshot=_portfolio_snap(),
        )
        assert dec.admitted
        assert dec.block_reason is None


class TestDayStopBlocks:
    def test_stop_for_day(self) -> None:
        """STOP_FOR_DAY → blocked with DAY_STOP reason."""
        gate = _gate()
        dec = gate.evaluate(
            "TESTUSDT",
            price=Decimal("100"),
            step_pct=Decimal("0.01"),
            entry_levels=5,
            exchange_min_qty=Decimal("0.001"),
            exchange_min_notional=Decimal("5"),
            qty_step=Decimal("0.001"),
            day_state=_day_state(mode=DayRiskMode.STOP_FOR_DAY),
            portfolio_snapshot=_portfolio_snap(),
        )
        assert not dec.admitted
        assert dec.block_reason == AdmissionBlockReason.DAY_STOP


class TestPortfolioBlocks:
    def test_no_entries_allowed(self) -> None:
        """Portfolio says no entries → blocked."""
        gate = _gate()
        dec = gate.evaluate(
            "TESTUSDT",
            price=Decimal("100"),
            step_pct=Decimal("0.01"),
            entry_levels=5,
            exchange_min_qty=Decimal("0.001"),
            exchange_min_notional=Decimal("5"),
            qty_step=Decimal("0.001"),
            day_state=_day_state(),
            portfolio_snapshot=_portfolio_snap(
                new_entries_allowed=False, reasons=("MAX_SYMBOLS_REACHED",)
            ),
        )
        assert not dec.admitted
        assert dec.block_reason == AdmissionBlockReason.PORTFOLIO_NO_ENTRIES

    def test_zero_risk_budget(self) -> None:
        """Zero risk budget → blocked."""
        gate = _gate()
        dec = gate.evaluate(
            "TESTUSDT",
            price=Decimal("100"),
            step_pct=Decimal("0.01"),
            entry_levels=5,
            exchange_min_qty=Decimal("0.001"),
            exchange_min_notional=Decimal("5"),
            qty_step=Decimal("0.001"),
            day_state=_day_state(),
            portfolio_snapshot=_portfolio_snap(risk_budget="0"),
        )
        assert not dec.admitted
        assert dec.block_reason == AdmissionBlockReason.PORTFOLIO_NO_ENTRIES


class TestGridNoGo:
    def test_below_min_qty(self) -> None:
        """Tiny budget → grid sizer says NO_GO."""
        gate = _gate()
        dec = gate.evaluate(
            "TESTUSDT",
            price=Decimal("100"),
            step_pct=Decimal("0.01"),
            entry_levels=5,
            exchange_min_qty=Decimal("10"),
            exchange_min_notional=Decimal("5"),
            qty_step=Decimal("0.001"),
            day_state=_day_state(),
            portfolio_snapshot=_portfolio_snap(risk_budget="1"),
        )
        assert not dec.admitted
        assert dec.block_reason == AdmissionBlockReason.GRID_NO_GO


class TestFilterCandidates:
    def test_filters_preserves_order(self) -> None:
        """Admitted symbols preserve ranking order."""
        gate = _gate()
        ranked = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        prices = {s: Decimal("100") for s in ranked}
        step_pcts = {s: Decimal("0.01") for s in ranked}
        constraints = {s: _FakeConstraint() for s in ranked}

        admitted, decisions = gate.filter_candidates(
            ranked,
            prices=prices,
            step_pcts=step_pcts,
            entry_levels=5,
            constraints=constraints,
            day_state=_day_state(),
            portfolio_snapshot=_portfolio_snap(),
        )
        assert admitted == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        assert all(d.admitted for d in decisions.values())

    def test_no_price_blocked(self) -> None:
        """Symbol without price → blocked with NO_PRICE."""
        gate = _gate()
        admitted, decisions = gate.filter_candidates(
            ["TESTUSDT"],
            prices={},
            step_pcts={"TESTUSDT": Decimal("0.01")},
            entry_levels=5,
            constraints={"TESTUSDT": _FakeConstraint()},
            day_state=_day_state(),
            portfolio_snapshot=_portfolio_snap(),
        )
        assert admitted == []
        assert decisions["TESTUSDT"].block_reason == AdmissionBlockReason.NO_PRICE

    def test_no_constraints_blocked(self) -> None:
        """Symbol without constraints → blocked with NO_CONSTRAINTS."""
        gate = _gate()
        admitted, decisions = gate.filter_candidates(
            ["TESTUSDT"],
            prices={"TESTUSDT": Decimal("100")},
            step_pcts={"TESTUSDT": Decimal("0.01")},
            entry_levels=5,
            constraints={},
            day_state=_day_state(),
            portfolio_snapshot=_portfolio_snap(),
        )
        assert admitted == []
        assert decisions["TESTUSDT"].block_reason == AdmissionBlockReason.NO_CONSTRAINTS

    def test_partial_filter(self) -> None:
        """One symbol blocked, others pass → filtered list preserves order."""
        gate = _gate()
        ranked = ["GOODUSDT", "BADUSDT", "OKUSDT"]
        prices = {s: Decimal("100") for s in ranked}
        step_pcts = {s: Decimal("0.01") for s in ranked}
        # BADUSDT has impossible exchange minimums
        constraints = {
            "GOODUSDT": _FakeConstraint(),
            "BADUSDT": _FakeConstraint(min_qty=Decimal("10000")),
            "OKUSDT": _FakeConstraint(),
        }

        admitted, decisions = gate.filter_candidates(
            ranked,
            prices=prices,
            step_pcts=step_pcts,
            entry_levels=5,
            constraints=constraints,
            day_state=_day_state(),
            portfolio_snapshot=_portfolio_snap(),
        )
        assert admitted == ["GOODUSDT", "OKUSDT"]
        assert decisions["BADUSDT"].block_reason == AdmissionBlockReason.GRID_NO_GO

    def test_day_stop_blocks_all(self) -> None:
        """STOP_FOR_DAY blocks all candidates."""
        gate = _gate()
        ranked = ["AAAUSDT", "BBBUSDT"]
        prices = {s: Decimal("100") for s in ranked}
        step_pcts = {s: Decimal("0.01") for s in ranked}
        constraints = {s: _FakeConstraint() for s in ranked}

        admitted, decisions = gate.filter_candidates(
            ranked,
            prices=prices,
            step_pcts=step_pcts,
            entry_levels=5,
            constraints=constraints,
            day_state=_day_state(mode=DayRiskMode.STOP_FOR_DAY),
            portfolio_snapshot=_portfolio_snap(),
        )
        assert admitted == []
        assert all(d.block_reason == AdmissionBlockReason.DAY_STOP for d in decisions.values())


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        gate = _gate()
        kwargs = {
            "prices": {"TESTUSDT": Decimal("100")},
            "step_pcts": {"TESTUSDT": Decimal("0.01")},
            "entry_levels": 5,
            "constraints": {"TESTUSDT": _FakeConstraint()},
            "day_state": _day_state(),
            "portfolio_snapshot": _portfolio_snap(),
        }
        a1, d1 = gate.filter_candidates(["TESTUSDT"], **kwargs)
        a2, d2 = gate.filter_candidates(["TESTUSDT"], **kwargs)
        assert a1 == a2
        assert d1 == d2
