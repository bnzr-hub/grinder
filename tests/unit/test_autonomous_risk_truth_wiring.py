"""Tests for autonomous risk-truth wiring in bootstrap/refresher/selector."""

from __future__ import annotations

from argparse import Namespace
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from scripts import run_autonomous as run_autonomous_mod

from grinder.execution.engine import SymbolConstraints
from grinder.execution_plane.registry import EngineRegistry
from grinder.runtime.account_truth import fetch_futures_risk_base
from grinder.runtime.live_engine_bridge import LiveEngineBridge
from grinder.tuning.autonomous_state import AutonomousTuningState
from grinder.tuning.refresher import TuningRefresher
from grinder.tuning.solver import TuningResult, TuningStatus


def _args(**overrides: object) -> Namespace:
    defaults = {
        "symbols": "",
        "blacklist": "",
        "cycle_interval_s": 1.0,
        "top_k": 1,
        "max_changes_per_cycle": 1,
        "execution_enabled": False,
        "execution_ack": False,
        "max_cycles": None,
        "exchange_port": "noop",
        "mainnet": False,
        "armed": False,
        "max_notional_per_order": "100",
        "max_orders_per_run": 500,
        "_ws_transport": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


class TestBootstrapRiskTruth:
    def test_derive_bootstrap_symbol_risk_budget_uses_real_equity_and_gross(self) -> None:
        with (
            patch(
                "grinder.runtime.account_truth.fetch_futures_risk_base",
                return_value=Decimal("1000"),
            ),
            patch(
                "grinder.runtime.account_truth.fetch_futures_gross_exposure",
                return_value=Decimal("200"),
            ),
        ):
            budget = run_autonomous_mod._derive_bootstrap_symbol_risk_budget(testnet=True)

        # PortfolioBudgetAllocator default NEUTRAL risk_pct = 2%
        assert budget == Decimal("20")

    def test_bootstrap_passes_real_risk_budget_to_solver(self) -> None:
        fake_result = TuningResult(
            symbol="BTCUSDT",
            status=TuningStatus.TUNED,
            order_size=Decimal("0.01"),
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            max_inventory_levels=15,
            max_position_notional_usd=Decimal("95.23"),
            actual_order_notional_usd=Decimal("5.77"),
        )
        captured: dict[str, Decimal] = {}

        def _fake_solve(
            symbol: str, sc: SymbolConstraints, price: Decimal, config: Any
        ) -> TuningResult:
            captured["max_position_usd"] = config.max_position_usd
            return fake_result

        with (
            patch.object(
                run_autonomous_mod,
                "_derive_bootstrap_symbol_risk_budget",
                return_value=Decimal("20"),
            ),
            patch.object(run_autonomous_mod, "_fetch_price_rest", return_value=Decimal("100")),
            patch("scripts.http_measured_client.RequestsHttpClient", return_value=MagicMock()),
            patch("grinder.execution.constraint_provider.ConstraintProvider") as provider_cls,
            patch("grinder.tuning.solver.solve", side_effect=_fake_solve),
        ):
            provider = MagicMock()
            provider.get_constraints.return_value = {
                "BTCUSDT": SymbolConstraints(
                    step_size=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    tick_size=Decimal("0.10"),
                    min_notional=Decimal("5"),
                )
            }
            provider_cls.return_value = provider
            sizes, results = run_autonomous_mod._bootstrap_tuning_cache(
                ["BTCUSDT"], MagicMock(), _args(symbols="BTCUSDT")
            )

        assert captured["max_position_usd"] == Decimal("20")
        assert sizes["BTCUSDT"] == "0.01"
        assert results["BTCUSDT"].max_position_notional_usd == Decimal("95.23")

    def test_fetch_futures_risk_base_honors_wallet_balance_mode(self) -> None:
        payload = {
            "totalMarginBalance": "1000",
            "totalWalletBalance": "900",
            "availableBalance": "800",
        }
        with (
            patch.dict("os.environ", {"GRINDER_RISK_BASE_MODE": "wallet_balance"}, clear=False),
            patch("grinder.runtime.account_truth._signed_get", return_value=payload),
        ):
            risk_base = fetch_futures_risk_base(testnet=True)

        assert risk_base == Decimal("900")


class TestSelectorManualCapRemoval:
    def test_initial_v2_features_do_not_use_manual_max_notional(self) -> None:
        tuned = {
            "BTCUSDT": TuningResult(
                symbol="BTCUSDT",
                status=TuningStatus.TUNED,
                order_size=Decimal("0.1"),
            )
        }
        with (
            patch(
                "grinder.selector.feature_provider.fetch_selection_features",
                return_value={},
            ),
            patch(
                "grinder.selector.feature_provider.fetch_selection_features_v2",
                return_value={},
            ) as fetch_v2,
        ):
            run_autonomous_mod._fetch_initial_selector_features(tuned, mainnet=True)

        assert fetch_v2.call_args.kwargs["max_notional_per_order"] is None

    def test_v2_selector_prefilter_uses_no_manual_cap(self) -> None:
        state = AutonomousTuningState(v1_features={}, v2_features={})
        cache = MagicMock()

        with patch(
            "grinder.selector.prefilter.prefilter_v1", return_value=([], {})
        ) as prefilter_v1:
            prefilter, _ = run_autonomous_mod._build_v2_selector(state, cache, frozenset())
            prefilter(["BTCUSDT"])

        assert prefilter_v1.call_args.kwargs["max_notional_per_order"] is None


class TestRefresherRiskTruth:
    def test_refresher_uses_bridge_truth_for_solver_budget(self) -> None:
        bridge = LiveEngineBridge()
        bridge.update_equity(Decimal("1000"))
        bridge.update_risk_base(Decimal("20"))
        bridge.update_gross_exposure(Decimal("200"))
        state = AutonomousTuningState(candidates=["BTCUSDT"])
        registry = EngineRegistry()
        refresher = TuningRefresher(
            state=state,
            cache=MagicMock(),
            bridge=bridge,
            registry=registry,
            args=_args(),
        )
        captured: dict[str, Decimal] = {}

        def _fake_solve(
            symbol: str, sc: SymbolConstraints, price: Decimal, config: Any
        ) -> TuningResult:
            captured["max_position_usd"] = config.max_position_usd
            return TuningResult(
                symbol=symbol,
                status=TuningStatus.TUNED,
                order_size=Decimal("0.01"),
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.001"),
                max_inventory_levels=15,
                max_position_notional_usd=Decimal("95.23"),
                actual_order_notional_usd=Decimal("5.77"),
            )

        with (
            patch.object(refresher, "_fetch_price", return_value=Decimal("100")),
            patch("scripts.http_measured_client.RequestsHttpClient", return_value=MagicMock()),
            patch("grinder.execution.constraint_provider.ConstraintProvider") as provider_cls,
            patch("grinder.tuning.solver.solve", side_effect=_fake_solve),
        ):
            provider = MagicMock()
            provider.get_constraints.return_value = {
                "BTCUSDT": SymbolConstraints(
                    step_size=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    tick_size=Decimal("0.10"),
                    min_notional=Decimal("5"),
                )
            }
            provider_cls.return_value = provider
            refresher._retune_symbols(["BTCUSDT"], {"BTCUSDT": Decimal("2.0")}, Decimal("20"))

        assert captured["max_position_usd"] == Decimal("20")

    def test_refresher_keeps_equity_and_risk_base_separate(self) -> None:
        bridge = LiveEngineBridge()
        refresher = TuningRefresher(
            state=AutonomousTuningState(candidates=["BTCUSDT"]),
            cache=MagicMock(),
            bridge=bridge,
            registry=EngineRegistry(),
            args=_args(),
        )

        with (
            patch(
                "grinder.runtime.account_truth.fetch_futures_equity", return_value=Decimal("1000")
            ),
            patch(
                "grinder.runtime.account_truth.fetch_futures_risk_base",
                return_value=Decimal("900"),
            ),
            patch(
                "grinder.runtime.account_truth.fetch_futures_gross_exposure",
                return_value=Decimal("200"),
            ),
        ):
            refresher._update_equity()

        assert bridge.last_known_equity == Decimal("1000")
        assert bridge.last_known_risk_base == Decimal("900")
