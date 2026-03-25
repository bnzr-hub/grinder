"""Tests for exchange-balance risk base contract (PR-1).

Covers:
1. Config validation (defaults, bounds, invariants).
2. RiskBaseSnapshot builder (mode selection, staleness, missing fields).
3. Metrics singleton (record/reset).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.risk.risk_base import (
    BalanceData,
    RiskBaseConfig,
    RiskBaseMode,
    RiskBaseSnapshot,
    build_risk_base_snapshot,
)
from grinder.risk.risk_base_metrics import (
    STATUS_FRESH,
    STATUS_HARD_STALE,
    STATUS_SOFT_STALE,
    STATUS_UNAVAILABLE,
    RiskBaseMetrics,
    get_risk_base_metrics,
    reset_risk_base_metrics,
)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------
class TestRiskBaseConfig:
    def test_defaults(self) -> None:
        cfg = RiskBaseConfig()
        assert cfg.mode == RiskBaseMode.TOTAL_MARGIN_BALANCE
        assert cfg.min_usd == 50.0
        assert cfg.stale_ttl_s == 30
        assert cfg.max_age_hard_s == 60

    def test_negative_min_usd_raises(self) -> None:
        with pytest.raises(ValueError, match="min_usd"):
            RiskBaseConfig(min_usd=-1)

    def test_negative_stale_ttl_raises(self) -> None:
        with pytest.raises(ValueError, match="stale_ttl_s"):
            RiskBaseConfig(stale_ttl_s=-1)

    def test_negative_max_age_hard_raises(self) -> None:
        with pytest.raises(ValueError, match="max_age_hard_s"):
            RiskBaseConfig(max_age_hard_s=-1)

    def test_hard_less_than_soft_raises(self) -> None:
        with pytest.raises(ValueError, match="max_age_hard_s"):
            RiskBaseConfig(stale_ttl_s=60, max_age_hard_s=30)

    def test_equal_ttl_and_hard_ok(self) -> None:
        cfg = RiskBaseConfig(stale_ttl_s=30, max_age_hard_s=30)
        assert cfg.stale_ttl_s == 30
        assert cfg.max_age_hard_s == 30

    def test_frozen(self) -> None:
        cfg = RiskBaseConfig()
        with pytest.raises(AttributeError):
            cfg.min_usd = 100.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Builder: mode selection
# ---------------------------------------------------------------------------
class TestBuildRiskBaseSnapshotModes:
    def _cfg(self, mode: RiskBaseMode = RiskBaseMode.TOTAL_MARGIN_BALANCE) -> RiskBaseConfig:
        return RiskBaseConfig(mode=mode, min_usd=50.0, stale_ttl_s=30, max_age_hard_s=60)

    def _balance(
        self,
        tmb: str = "1000",
        wb: str = "900",
        ab: str = "800",
        ts_ms: int = 100_000,
    ) -> BalanceData:
        return BalanceData(
            total_margin_balance=Decimal(tmb),
            wallet_balance=Decimal(wb),
            available_balance=Decimal(ab),
            ts_ms=ts_ms,
        )

    def test_total_margin_balance_selected(self) -> None:
        snap = build_risk_base_snapshot(
            self._balance(), self._cfg(RiskBaseMode.TOTAL_MARGIN_BALANCE), now_ms=100_000
        )
        assert snap is not None
        assert snap.value_usd == Decimal("1000")
        assert snap.mode == "total_margin_balance"

    def test_wallet_balance_selected(self) -> None:
        snap = build_risk_base_snapshot(
            self._balance(), self._cfg(RiskBaseMode.WALLET_BALANCE), now_ms=100_000
        )
        assert snap is not None
        assert snap.value_usd == Decimal("900")
        assert snap.mode == "wallet_balance"

    def test_available_balance_selected(self) -> None:
        snap = build_risk_base_snapshot(
            self._balance(), self._cfg(RiskBaseMode.AVAILABLE_BALANCE), now_ms=100_000
        )
        assert snap is not None
        assert snap.value_usd == Decimal("800")
        assert snap.mode == "available_balance"


# ---------------------------------------------------------------------------
# Builder: missing / invalid fields
# ---------------------------------------------------------------------------
class TestBuildRiskBaseSnapshotMissing:
    def _cfg(self) -> RiskBaseConfig:
        return RiskBaseConfig(mode=RiskBaseMode.TOTAL_MARGIN_BALANCE)

    def test_missing_selected_field_returns_none(self) -> None:
        balance = BalanceData(
            total_margin_balance=None,
            wallet_balance=Decimal("1000"),
            available_balance=Decimal("800"),
            ts_ms=100_000,
        )
        snap = build_risk_base_snapshot(balance, self._cfg(), now_ms=100_000)
        assert snap is None

    def test_zero_value_returns_none(self) -> None:
        balance = BalanceData(
            total_margin_balance=Decimal("0"),
            ts_ms=100_000,
        )
        snap = build_risk_base_snapshot(balance, self._cfg(), now_ms=100_000)
        assert snap is None

    def test_negative_value_returns_none(self) -> None:
        balance = BalanceData(
            total_margin_balance=Decimal("-100"),
            ts_ms=100_000,
        )
        snap = build_risk_base_snapshot(balance, self._cfg(), now_ms=100_000)
        assert snap is None

    def test_all_fields_missing_returns_none(self) -> None:
        balance = BalanceData(ts_ms=100_000)
        snap = build_risk_base_snapshot(balance, self._cfg(), now_ms=100_000)
        assert snap is None


# --- Builder staleness ---
class TestBuildRiskBaseSnapshotStaleness:
    def _cfg(self) -> RiskBaseConfig:
        return RiskBaseConfig(stale_ttl_s=30, max_age_hard_s=60)

    def _balance(self, ts_ms: int = 100_000) -> BalanceData:
        return BalanceData(total_margin_balance=Decimal("1000"), ts_ms=ts_ms)

    def test_fresh_snapshot(self) -> None:
        """age <= TTL -> fresh (not stale)."""
        snap = build_risk_base_snapshot(self._balance(ts_ms=100_000), self._cfg(), now_ms=110_000)
        assert snap is not None
        assert snap.age_s == 10
        assert snap.is_stale_soft is False
        assert snap.is_stale_hard is False

    def test_soft_stale(self) -> None:
        """TTL < age < HARD -> soft stale."""
        snap = build_risk_base_snapshot(self._balance(ts_ms=100_000), self._cfg(), now_ms=145_000)
        assert snap is not None
        assert snap.age_s == 45
        assert snap.is_stale_soft is True
        assert snap.is_stale_hard is False

    def test_hard_stale(self) -> None:
        """age >= HARD -> hard stale."""
        snap = build_risk_base_snapshot(self._balance(ts_ms=100_000), self._cfg(), now_ms=170_000)
        assert snap is not None
        assert snap.age_s == 70
        assert snap.is_stale_soft is True
        assert snap.is_stale_hard is True

    def test_exact_soft_boundary(self) -> None:
        """age == TTL is NOT stale (stale is strictly >)."""
        snap = build_risk_base_snapshot(self._balance(ts_ms=100_000), self._cfg(), now_ms=130_000)
        assert snap is not None
        assert snap.age_s == 30
        assert snap.is_stale_soft is False

    def test_exact_hard_boundary(self) -> None:
        """age == HARD is NOT hard-stale (stale is strictly >)."""
        snap = build_risk_base_snapshot(self._balance(ts_ms=100_000), self._cfg(), now_ms=160_000)
        assert snap is not None
        assert snap.age_s == 60
        assert snap.is_stale_hard is False
        assert snap.is_stale_soft is True  # 60 > 30


# ---------------------------------------------------------------------------
# Builder: min_usd marker
# ---------------------------------------------------------------------------
class TestBuildRiskBaseSnapshotMinUsd:
    def test_below_min_marker_set(self) -> None:
        cfg = RiskBaseConfig(min_usd=100.0)
        balance = BalanceData(total_margin_balance=Decimal("50"), ts_ms=100_000)
        snap = build_risk_base_snapshot(balance, cfg, now_ms=100_000)
        assert snap is not None
        assert snap.is_below_min is True
        # Snapshot is still produced (PR-1: informational only, no blocking)
        assert snap.value_usd == Decimal("50")

    def test_above_min_marker_clear(self) -> None:
        cfg = RiskBaseConfig(min_usd=50.0)
        balance = BalanceData(total_margin_balance=Decimal("100"), ts_ms=100_000)
        snap = build_risk_base_snapshot(balance, cfg, now_ms=100_000)
        assert snap is not None
        assert snap.is_below_min is False


# ---------------------------------------------------------------------------
# Builder: source fields present
# ---------------------------------------------------------------------------
class TestBuildRiskBaseSnapshotFieldPresence:
    def test_all_fields_present(self) -> None:
        balance = BalanceData(
            total_margin_balance=Decimal("1000"),
            wallet_balance=Decimal("900"),
            available_balance=Decimal("800"),
            ts_ms=100_000,
        )
        snap = build_risk_base_snapshot(balance, RiskBaseConfig(), now_ms=100_000)
        assert snap is not None
        assert snap.source_fields_present == (
            "total_margin_balance",
            "wallet_balance",
            "available_balance",
        )

    def test_partial_fields_present(self) -> None:
        balance = BalanceData(
            total_margin_balance=Decimal("1000"),
            wallet_balance=None,
            available_balance=Decimal("800"),
            ts_ms=100_000,
        )
        snap = build_risk_base_snapshot(balance, RiskBaseConfig(), now_ms=100_000)
        assert snap is not None
        assert snap.source_fields_present == ("total_margin_balance", "available_balance")


# ---------------------------------------------------------------------------
# Builder: determinism (fixed input -> fixed output)
# ---------------------------------------------------------------------------
class TestBuildRiskBaseSnapshotDeterminism:
    def test_same_inputs_same_output(self) -> None:
        cfg = RiskBaseConfig()
        balance = BalanceData(
            total_margin_balance=Decimal("1234.56"),
            wallet_balance=Decimal("1200"),
            available_balance=Decimal("1100"),
            ts_ms=100_000,
        )
        snap1 = build_risk_base_snapshot(balance, cfg, now_ms=120_000)
        snap2 = build_risk_base_snapshot(balance, cfg, now_ms=120_000)
        assert snap1 == snap2


# ---------------------------------------------------------------------------
# Metrics singleton
# ---------------------------------------------------------------------------
class TestRiskBaseMetrics:
    def setup_method(self) -> None:
        reset_risk_base_metrics()

    def teardown_method(self) -> None:
        reset_risk_base_metrics()

    def test_singleton_created(self) -> None:
        m = get_risk_base_metrics()
        assert isinstance(m, RiskBaseMetrics)
        assert m is get_risk_base_metrics()

    def test_default_state(self) -> None:
        m = get_risk_base_metrics()
        assert m.value_usd == 0.0
        assert m.age_s == 0
        assert m.status == STATUS_UNAVAILABLE

    def test_record_fresh(self) -> None:
        m = get_risk_base_metrics()
        m.record_snapshot(value_usd=1000.0, age_s=5, is_stale_soft=False, is_stale_hard=False)
        assert m.status == STATUS_FRESH
        assert m.value_usd == 1000.0

    def test_record_soft_stale(self) -> None:
        m = get_risk_base_metrics()
        m.record_snapshot(value_usd=1000.0, age_s=35, is_stale_soft=True, is_stale_hard=False)
        assert m.status == STATUS_SOFT_STALE

    def test_record_hard_stale(self) -> None:
        m = get_risk_base_metrics()
        m.record_snapshot(value_usd=1000.0, age_s=70, is_stale_soft=True, is_stale_hard=True)
        assert m.status == STATUS_HARD_STALE

    def test_record_unavailable(self) -> None:
        m = get_risk_base_metrics()
        m.record_snapshot(value_usd=500.0, age_s=0, is_stale_soft=False, is_stale_hard=False)
        assert m.status == STATUS_FRESH
        m.record_unavailable()
        assert m.status == STATUS_UNAVAILABLE
        assert m.value_usd == 0.0
        assert m.age_s == 0  # P2 fix: age_s reset on unavailable

    def test_record_unavailable_resets_age_from_stale(self) -> None:
        """P2 fix: after stale snapshot, unavailable must reset age_s to 0."""
        m = get_risk_base_metrics()
        m.record_snapshot(value_usd=1000.0, age_s=45, is_stale_soft=True, is_stale_hard=False)
        assert m.age_s == 45
        m.record_unavailable()
        assert m.age_s == 0
        assert m.status == STATUS_UNAVAILABLE

    def test_prometheus_lines(self) -> None:
        m = get_risk_base_metrics()
        m.record_snapshot(value_usd=1234.56, age_s=10, is_stale_soft=False, is_stale_hard=False)
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_risk_base_usd 1234.56" in text
        assert "grinder_risk_base_stale_seconds 10" in text
        assert "grinder_risk_base_status 1" in text

    def test_reset(self) -> None:
        m1 = get_risk_base_metrics()
        m1.record_snapshot(value_usd=500.0, age_s=0, is_stale_soft=False, is_stale_hard=False)
        reset_risk_base_metrics()
        m2 = get_risk_base_metrics()
        assert m2 is not m1
        assert m2.status == STATUS_UNAVAILABLE


# ---------------------------------------------------------------------------
# RiskBaseSnapshot is frozen
# ---------------------------------------------------------------------------
class TestRiskBaseSnapshotFrozen:
    def test_frozen(self) -> None:
        snap = RiskBaseSnapshot(
            value_usd=Decimal("100"),
            mode="total_margin_balance",
            asof_ts_ms=100_000,
            age_s=0,
            is_stale_soft=False,
            is_stale_hard=False,
            source_fields_present=("total_margin_balance",),
        )
        with pytest.raises(AttributeError):
            snap.value_usd = Decimal("200")  # type: ignore[misc]
