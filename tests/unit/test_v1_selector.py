"""Tests for V1 symbol selection: prefilter + ranker."""

from __future__ import annotations

from decimal import Decimal

from grinder.selector.models import SelectionFeatures, SkipReason
from grinder.selector.prefilter import prefilter_v1
from grinder.selector.ranker import rank_v1
from grinder.tuning.solver import TuningResult, TuningStatus


def _feat(
    symbol: str,
    volume: str = "3000000",
    bid: str = "1.0",
    ask: str = "1.001",
    natr: str = "2.0",
) -> SelectionFeatures:
    return SelectionFeatures(
        symbol=symbol,
        quote_volume_last_12x5m=Decimal(volume),
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        natr_14_5m=Decimal(natr),
    )


def _tuned(symbol: str) -> TuningResult:
    return TuningResult(symbol=symbol, status=TuningStatus.TUNED, order_size=Decimal("100"))


def _not_tuned(symbol: str) -> TuningResult:
    return TuningResult(symbol=symbol, status=TuningStatus.NO_GO)


# --- Prefilter tests ---


class TestPrefilterV1:
    def test_eligible_symbol_passes(self) -> None:
        eligible, _skipped = prefilter_v1(
            ["BTCUSDT"],
            {"BTCUSDT": _tuned("BTCUSDT")},
            {"BTCUSDT": _feat("BTCUSDT")},
        )
        assert eligible == ["BTCUSDT"]
        assert _skipped == {}

    def test_low_volume_excluded(self) -> None:
        eligible, _skipped = prefilter_v1(
            ["LOWVOL"],
            {"LOWVOL": _tuned("LOWVOL")},
            {"LOWVOL": _feat("LOWVOL", volume="1999999")},  # below $2M
        )
        assert eligible == []
        assert _skipped["LOWVOL"] == SkipReason.LOW_VOLUME_LAST_12X5M

    def test_volume_at_floor_passes(self) -> None:
        """Symbol with exactly $2M rolling volume passes prefilter."""
        eligible, _skipped = prefilter_v1(
            ["EXACT"],
            {"EXACT": _tuned("EXACT")},
            {"EXACT": _feat("EXACT", volume="2000000")},
        )
        assert eligible == ["EXACT"]
        assert _skipped == {}

    def test_low_natr_excluded(self) -> None:
        eligible, _skipped = prefilter_v1(
            ["FLAT"],
            {"FLAT": _tuned("FLAT")},
            {"FLAT": _feat("FLAT", natr="0.5")},  # below 1.0
        )
        assert eligible == []
        assert _skipped["FLAT"] == SkipReason.LOW_NATR_5M

    def test_not_tuned_excluded(self) -> None:
        eligible, _skipped = prefilter_v1(
            ["NOGO"],
            {"NOGO": _not_tuned("NOGO")},
            {"NOGO": _feat("NOGO")},
        )
        assert eligible == []
        assert _skipped["NOGO"] == SkipReason.NOT_TUNED

    def test_missing_features_excluded(self) -> None:
        eligible, _skipped = prefilter_v1(
            ["NOFEAT"],
            {"NOFEAT": _tuned("NOFEAT")},
            {},  # no features
        )
        assert eligible == []
        assert _skipped["NOFEAT"] == SkipReason.FEATURES_UNAVAILABLE

    def test_blacklisted_excluded(self) -> None:
        eligible, _skipped = prefilter_v1(
            ["BANNED"],
            {"BANNED": _tuned("BANNED")},
            {"BANNED": _feat("BANNED")},
            blacklist=frozenset({"BANNED"}),
        )
        assert eligible == []
        assert _skipped["BANNED"] == SkipReason.BLACKLISTED

    def test_mixed_pass_and_skip(self) -> None:
        eligible, _skipped = prefilter_v1(
            ["GOOD", "LOW", "FLAT"],
            {
                "GOOD": _tuned("GOOD"),
                "LOW": _tuned("LOW"),
                "FLAT": _tuned("FLAT"),
            },
            {
                "GOOD": _feat("GOOD", volume="5000000", natr="3.0"),
                "LOW": _feat("LOW", volume="100"),
                "FLAT": _feat("FLAT", natr="0.1"),
            },
        )
        assert eligible == ["GOOD"]
        assert len(_skipped) == 2

    def test_exceeds_run_cap_excluded(self) -> None:
        """Symbol whose entry notional exceeds max_notional_per_order is excluded."""
        # order_size=100, mid_price=1.0 → notional=100 > cap=10
        eligible, _skipped = prefilter_v1(
            ["BIGORDER"],
            {"BIGORDER": _tuned("BIGORDER")},
            {"BIGORDER": _feat("BIGORDER", bid="1.0", ask="1.0")},
            max_notional_per_order=Decimal("10"),
        )
        assert eligible == []
        assert _skipped["BIGORDER"] == SkipReason.EXCEEDS_RUN_CAP

    def test_within_run_cap_passes(self) -> None:
        """Symbol whose entry notional fits max_notional_per_order passes."""
        # order_size=100, mid_price=0.01 → notional=1 < cap=10
        eligible, _skipped = prefilter_v1(
            ["SMALL"],
            {"SMALL": _tuned("SMALL")},
            {"SMALL": _feat("SMALL", bid="0.01", ask="0.01")},
            max_notional_per_order=Decimal("10"),
        )
        assert eligible == ["SMALL"]

    def test_no_cap_skips_check(self) -> None:
        """When max_notional_per_order is None, cap check is skipped."""
        eligible, _skipped = prefilter_v1(
            ["ANY"],
            {"ANY": _tuned("ANY")},
            {"ANY": _feat("ANY", bid="1000.0", ask="1000.0")},
            max_notional_per_order=None,
        )
        assert eligible == ["ANY"]


class TestPrefilterAdaptiveSpacing:
    def test_below_min_spacing_excluded(self) -> None:
        """Symbol with NATR producing spacing < 50 bps is rejected."""
        # NATR=0.9% → spacing=45 bps < 50 → rejected
        # Must lower natr_5m_min so LOW_NATR_5M doesn't shadow the spacing gate
        eligible, _skipped = prefilter_v1(
            ["LOW"],
            {"LOW": _tuned("LOW")},
            {"LOW": _feat("LOW", natr="0.9")},
            natr_5m_min=Decimal("0.5"),  # lower NATR floor so spacing gate fires
        )
        assert eligible == []
        assert _skipped["LOW"] == SkipReason.GRID_SPACING_BELOW_MIN

    def test_at_boundary_natr_1pct_passes(self) -> None:
        """NATR=1.0% → spacing=50 bps → exactly at boundary → passes."""
        eligible, _skipped = prefilter_v1(
            ["BOUNDARY"],
            {"BOUNDARY": _tuned("BOUNDARY")},
            {"BOUNDARY": _feat("BOUNDARY", natr="1.0")},
        )
        assert eligible == ["BOUNDARY"]

    def test_gate_order_low_natr_wins(self) -> None:
        """Symbol with NATR=0.5% gets LOW_NATR_5M, not GRID_SPACING_BELOW_MIN."""
        eligible, _skipped = prefilter_v1(
            ["VERYLOW"],
            {"VERYLOW": _tuned("VERYLOW")},
            {"VERYLOW": _feat("VERYLOW", natr="0.5")},
        )
        assert eligible == []
        assert _skipped["VERYLOW"] == SkipReason.LOW_NATR_5M

    def test_custom_min_spacing_bps(self) -> None:
        """Caller can raise min_spacing_bps to reject symbols with moderate NATR."""
        # NATR=1.5% → spacing=75 bps. With min_spacing_bps=100, rejected.
        eligible, _skipped = prefilter_v1(
            ["MED"],
            {"MED": _tuned("MED")},
            {"MED": _feat("MED", natr="1.5")},
            min_spacing_bps=Decimal("100"),
        )
        assert eligible == []
        assert _skipped["MED"] == SkipReason.GRID_SPACING_BELOW_MIN


# --- Ranker tests ---


class TestRankerV1:
    def test_higher_volume_ranks_higher(self) -> None:
        features = {
            "HIGH": _feat("HIGH", volume="10000000"),
            "LOW": _feat("LOW", volume="2000000"),
        }
        scored = rank_v1(["HIGH", "LOW"], features)
        assert scored[0].symbol == "HIGH"

    def test_tighter_spread_ranks_higher(self) -> None:
        features = {
            "TIGHT": _feat("TIGHT", bid="1.0000", ask="1.0001"),  # 1 bps
            "WIDE": _feat("WIDE", bid="1.0000", ask="1.0100"),  # 100 bps
        }
        scored = rank_v1(["TIGHT", "WIDE"], features)
        assert scored[0].symbol == "TIGHT"

    def test_higher_natr_ranks_higher(self) -> None:
        features = {
            "VOL": _feat("VOL", natr="5.0"),
            "CALM": _feat("CALM", natr="1.0"),
        }
        scored = rank_v1(["VOL", "CALM"], features)
        assert scored[0].symbol == "VOL"

    def test_combined_score_deterministic(self) -> None:
        features = {
            "A": _feat("A", volume="3000000", natr="2.0", bid="1.0", ask="1.001"),
            "B": _feat("B", volume="5000000", natr="1.5", bid="1.0", ask="1.002"),
        }
        r1 = rank_v1(["A", "B"], features)
        r2 = rank_v1(["A", "B"], features)
        assert [s.symbol for s in r1] == [s.symbol for s in r2]

    def test_empty_list(self) -> None:
        assert rank_v1([], {}) == []

    def test_single_symbol(self) -> None:
        features = {"ONLY": _feat("ONLY")}
        scored = rank_v1(["ONLY"], features)
        assert len(scored) == 1
        assert scored[0].symbol == "ONLY"

    def test_missing_features_excluded(self) -> None:
        scored = rank_v1(["A", "B"], {"A": _feat("A")})
        assert len(scored) == 1
        assert scored[0].symbol == "A"

    def test_no_alphabetical_default_order(self) -> None:
        """Selection must not just be first alphabetically."""
        features = {
            "AAUSDT": _feat("AAUSDT", volume="2000000", natr="1.0"),
            "ZZUSDT": _feat("ZZUSDT", volume="10000000", natr="5.0"),
        }
        scored = rank_v1(["AAUSDT", "ZZUSDT"], features)
        assert scored[0].symbol == "ZZUSDT"  # higher on all metrics


# --- SelectionFeatures tests ---


class TestSelectionFeatures:
    def test_spread_bps(self) -> None:
        feat = _feat("X", bid="100.00", ask="100.10")
        # (100.10 - 100.00) / 100.05 * 10000 ≈ 9.99
        assert 9.0 < float(feat.spread_bps) < 11.0

    def test_mid_price(self) -> None:
        feat = _feat("X", bid="99", ask="101")
        assert feat.mid_price == Decimal("100")
