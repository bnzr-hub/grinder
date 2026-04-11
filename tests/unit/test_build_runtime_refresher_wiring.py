"""Runtime assembly tests for dynamic bootstrap wiring (ADR-183 PR-1 + PR-2).

Closes the residual testing gap flagged on PR #672 and PR #674 reviews:
``TuningRefresher`` has strong unit coverage, but the runtime-assembly
seam in ``scripts.run_autonomous.build_runtime`` was still under-tested.
A silent regression at the wiring layer — e.g. a refactor that stops
passing ``universe_provider`` or the bootstrap helper callables into
``TuningRefresher`` — would disable dynamic bootstrap without breaking
any existing unit test.

These tests patch ``TuningRefresher`` at construction time inside
``_build_tuning_state_and_selector`` and assert the exact kwargs that
``build_runtime`` passes through. The assertions are refactor-resistant:
any future change that removes or mislabels the dynamic bootstrap
plumbing will fail these tests immediately.

Invariants locked:
  - ``universe_provider`` is non-None in auto-discovery mode
  - ``universe_provider`` is non-None in symbols-override mode too
    (dynamic discovery should still run on overridden runs)
  - ``blacklist`` is propagated to the refresher
  - ``coarse_select_fn`` is the module-local ``_select_bootstrap_subset``
  - ``prefilter_fn`` is the module-local ``_apply_bootstrap_prefilter``
  - the legacy plumbing (``state``, ``cache``, ``bridge``, ``registry``,
    ``args``) is still passed exactly as before
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import scripts.run_autonomous as mod

from grinder.connectors.binance_ws import FakeWsTransport
from grinder.orchestration.universe_provider import UniverseProvider


def _default_args(**overrides: object) -> argparse.Namespace:
    """Mirrors test_bootstrap_auto_universe._default_args so both suites
    exercise `build_runtime` with the same arg shape."""
    defaults = {
        "symbols": "",  # empty = auto-discovery mode
        "blacklist": "",
        "cycle_interval_s": 1.0,
        "top_k": 3,
        "max_changes_per_cycle": 1,
        "execution_enabled": False,
        "execution_ack": False,
        "max_cycles": None,
        "exchange_port": "noop",
        "mainnet": False,
        "armed": False,
        "max_notional_per_order": "100",
        "max_orders_per_run": 500,
        "_ws_transport": FakeWsTransport(messages=[]),
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run_build_runtime_capturing_refresher(
    args: argparse.Namespace,
    *,
    discovered: list[str] | None = None,
) -> dict[str, object]:
    """Run ``build_runtime(args)`` with ``TuningRefresher`` patched to a
    MagicMock so we can capture the constructor kwargs.

    Deliberately does NOT patch ``_select_bootstrap_subset`` or
    ``_apply_bootstrap_prefilter`` at the module level, so the captured
    ``coarse_select_fn`` / ``prefilter_fn`` kwargs are identity-equal to
    the real module-local helpers (and tests can assert that identity).
    The network dependencies of those helpers are neutralized by patching
    ``fetch_selection_features`` and ``_fetch_quote_volume_24h_map``
    instead, which produce empty prefilter output without actual REST.

    Returns the captured ``kwargs`` dict from the (single) TuningRefresher
    construction. Raises if construction was called zero times or more
    than once.
    """
    # We patch TuningRefresher at its source module — `_build_tuning_state_and_selector`
    # imports it locally as `from grinder.tuning.refresher import TuningRefresher`,
    # which resolves via module-attribute lookup at call time.
    with (
        patch("grinder.tuning.refresher.TuningRefresher") as mock_refresher_cls,
        patch.object(mod, "_bootstrap_tuning_cache", return_value=({}, {})),
        patch.object(mod, "_fetch_quote_volume_24h_map", return_value={}),
        # Neutralize the REST call inside the real
        # `_apply_bootstrap_prefilter` so it returns an empty prefilter
        # result without hitting the network. This lets the helper
        # itself stay as the real module-local function so tests can
        # assert identity against `mod._apply_bootstrap_prefilter`.
        patch(
            "grinder.selector.feature_provider.fetch_selection_features",
            return_value={},
        ),
        patch(
            "grinder.orchestration.universe_provider.UniverseProvider.get_candidates",
            return_value=discovered or ["BTCUSDT", "ETHUSDT", "DRIFTUSDT"],
        ),
    ):
        mock_refresher_cls.return_value = MagicMock()
        mod.build_runtime(args)

    assert mock_refresher_cls.call_count == 1, (
        f"TuningRefresher must be constructed exactly once per build_runtime; "
        f"got {mock_refresher_cls.call_count} calls"
    )
    _, kwargs = mock_refresher_cls.call_args
    return dict(kwargs)


class TestDynamicBootstrapWiring:
    """build_runtime must pass dynamic bootstrap plumbing into TuningRefresher."""

    def test_universe_provider_is_wired_in_auto_discovery_mode(self) -> None:
        """Auto-discovery mode: TuningRefresher receives the real
        ``universe_provider`` instance (not None)."""
        kwargs = _run_build_runtime_capturing_refresher(_default_args())
        assert "universe_provider" in kwargs, (
            "TuningRefresher constructor missing universe_provider kwarg — "
            "dynamic bootstrap discovery would be silently disabled"
        )
        up = kwargs["universe_provider"]
        assert up is not None, (
            "universe_provider is None — dynamic bootstrap discovery "
            "early-returns and the feature is dead in production"
        )
        assert isinstance(up, UniverseProvider), (
            f"expected UniverseProvider instance, got {type(up).__name__}"
        )

    def test_universe_provider_is_wired_in_symbols_override_mode(self) -> None:
        """Symbols-override mode still wires the universe_provider so
        dynamic bootstrap discovery can run on override sessions too."""
        kwargs = _run_build_runtime_capturing_refresher(
            _default_args(symbols="BTCUSDT,ETHUSDT"),
        )
        up = kwargs.get("universe_provider")
        assert up is not None, (
            "symbols-override mode must still wire universe_provider — "
            "dynamic bootstrap discovery should be runtime-uniform"
        )
        assert isinstance(up, UniverseProvider)

    def test_coarse_select_fn_is_wired(self) -> None:
        """TuningRefresher receives the cold-bootstrap coarse selector
        so dynamic discovery uses the SAME filter semantics as cold
        bootstrap. This is the refactor-proof check against someone
        later removing the DI."""
        kwargs = _run_build_runtime_capturing_refresher(_default_args())
        assert "coarse_select_fn" in kwargs, (
            "TuningRefresher constructor missing coarse_select_fn — "
            "dynamic bootstrap discovery can't coarse-filter"
        )
        assert kwargs["coarse_select_fn"] is mod._select_bootstrap_subset, (
            f"coarse_select_fn is not the module-local helper: {kwargs['coarse_select_fn']!r}"
        )

    def test_prefilter_fn_is_wired(self) -> None:
        """TuningRefresher receives the cold-bootstrap prefilter helper."""
        kwargs = _run_build_runtime_capturing_refresher(_default_args())
        assert "prefilter_fn" in kwargs, (
            "TuningRefresher constructor missing prefilter_fn — "
            "dynamic bootstrap discovery can't prefilter"
        )
        assert kwargs["prefilter_fn"] is mod._apply_bootstrap_prefilter, (
            f"prefilter_fn is not the module-local helper: {kwargs['prefilter_fn']!r}"
        )

    def test_blacklist_is_wired(self) -> None:
        """TuningRefresher receives the same blacklist build_runtime
        computed from args. Dynamic bootstrap must honor blacklist.
        """
        kwargs = _run_build_runtime_capturing_refresher(
            _default_args(blacklist="BADCOIN,SCAMUSDT"),
        )
        assert "blacklist" in kwargs, (
            "TuningRefresher constructor missing blacklist — dynamic "
            "discovery would ignore blacklist"
        )
        bl = kwargs["blacklist"]
        assert isinstance(bl, frozenset), (
            f"blacklist must be frozenset for refresher use, got {type(bl).__name__}"
        )
        assert "BADCOIN" in bl
        assert "SCAMUSDT" in bl

    def test_empty_blacklist_still_wired_as_frozenset(self) -> None:
        """Empty blacklist produces an empty frozenset, not None."""
        kwargs = _run_build_runtime_capturing_refresher(_default_args())
        bl = kwargs.get("blacklist")
        assert bl is not None
        assert isinstance(bl, frozenset)
        assert bl == frozenset()

    def test_legacy_plumbing_still_passed(self) -> None:
        """Verify the PR-1/PR-2 wiring addition did not accidentally
        drop any of the pre-ADR-183 constructor args. The refresher
        still needs state, cache, bridge, registry, and args.
        """
        kwargs = _run_build_runtime_capturing_refresher(_default_args())
        # Legacy required kwargs
        for name in ("state", "cache", "bridge", "registry", "args"):
            assert name in kwargs, (
                f"TuningRefresher missing legacy kwarg {name!r} — "
                f"a refactor broke pre-ADR-183 wiring"
            )
            assert kwargs[name] is not None, (
                f"legacy kwarg {name} is None — runtime assembly is broken"
            )
        # args must be the argparse namespace (or at least have mainnet attr)
        assert hasattr(kwargs["args"], "mainnet"), (
            "args kwarg is not an argparse.Namespace-like object"
        )


class TestRefresherConstructorSnapshot:
    """A single snapshot test that locks the full set of kwargs
    ``build_runtime`` passes to TuningRefresher. Complements the
    per-kwarg tests above by making any accidental kwarg ADD/REMOVE
    immediately visible as a diff, not just a silent change."""

    _EXPECTED_KWARGS: frozenset[str] = frozenset(
        {
            "state",
            "cache",
            "bridge",
            "registry",
            "args",
            "universe_provider",
            "blacklist",
            "coarse_select_fn",
            "prefilter_fn",
        }
    )

    def test_refresher_kwarg_set_is_stable(self) -> None:
        """The exact set of kwargs passed to TuningRefresher from
        build_runtime is locked. If someone adds a new kwarg, they
        must update this test and explicitly acknowledge the runtime
        seam change. If someone removes one, the per-kwarg tests
        above will fail first."""
        kwargs = _run_build_runtime_capturing_refresher(_default_args())
        actual = frozenset(kwargs.keys())
        # Extra kwargs = someone silently widened the surface
        extra = actual - self._EXPECTED_KWARGS
        assert not extra, (
            f"TuningRefresher got unexpected kwargs from build_runtime: "
            f"{sorted(extra)}. Update _EXPECTED_KWARGS and document the "
            f"new constructor surface in ADR-183 / refresher docstring."
        )
        # Missing kwargs = someone silently narrowed the surface
        missing = self._EXPECTED_KWARGS - actual
        assert not missing, (
            f"TuningRefresher missing expected kwargs from build_runtime: "
            f"{sorted(missing)}. This would silently disable part of the "
            f"dynamic bootstrap pipeline."
        )


class TestRuntimeAssemblySmoke:
    """Normal runtime assembly still succeeds under both modes. These
    aren't directly about wiring correctness but catch the common
    regression where a wiring change accidentally breaks build_runtime
    entirely.
    """

    def test_auto_discovery_build_runtime_still_succeeds(self) -> None:
        with (
            patch.object(mod, "_bootstrap_tuning_cache", return_value=({}, {})),
            patch.object(
                mod,
                "_apply_bootstrap_prefilter",
                side_effect=lambda syms, **kw: syms[: kw.get("limit", 30)],
            ),
            patch(
                "grinder.orchestration.universe_provider.UniverseProvider.get_candidates",
                return_value=["BTCUSDT", "ETHUSDT"],
            ),
        ):
            runtime = mod.build_runtime(_default_args())
        assert "host" in runtime
        assert "universe_provider" in runtime

    def test_symbols_override_build_runtime_still_succeeds(self) -> None:
        with (
            patch.object(mod, "_bootstrap_tuning_cache", return_value=({}, {})),
        ):
            runtime = mod.build_runtime(_default_args(symbols="BTCUSDT"))
        assert "host" in runtime
        assert "universe_provider" in runtime
