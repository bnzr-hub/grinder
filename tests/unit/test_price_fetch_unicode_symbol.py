"""Regression tests for non-ASCII symbol safety in REST price fetch paths.

The post-#671 verification canary on 2026-04-11 observed
``BOOTSTRAP_PRICE_FETCH_FAILED symbol=币安人生USDT error='ascii' codec can't
encode characters in position 33-36: ordinal not in range(128)``.

Root cause: ``scripts.run_autonomous._fetch_price_rest`` and
``grinder.tuning.refresher.TuningRefresher._fetch_price`` both interpolated
the symbol directly into ``f"{base}/fapi/v1/ticker/price?symbol={symbol}"``
and passed the resulting string to ``urllib.request.urlopen``. Inside the
stdlib HTTP client, ``putrequest`` calls ``_encode_request(request)`` which
runs ``request.encode('ascii')`` on the HTTP request line — any non-ASCII
character in the URL raises ``UnicodeEncodeError``, which the fail-open
wrapper catches, producing a misleading "fetch failed" warning.

Fix: percent-encode the symbol via ``urllib.parse.quote(symbol, safe="")``
before interpolation. The resulting URL is ASCII-clean regardless of
exchange-returned symbol payload, and ``urlopen`` can proceed. If the
symbol is genuinely unsupported, Binance returns an HTTP error which the
caller already handles via the existing fail-open try/except.

These tests lock the behavior at the URL-construction boundary without
hitting the network — both functions are patched to capture the URL that
``urlopen`` would receive.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import scripts.run_autonomous as run_autonomous_mod

from grinder.tuning.refresher import TuningRefresher


class TestCoolBootstrapPriceFetchUnicodeSymbol:
    """``scripts.run_autonomous._fetch_price_rest`` is the cold-bootstrap seam."""

    def _capture_urlopen_url(self, symbol: str, fake_response_body: bytes | None = None) -> str:
        """Run _fetch_price_rest and return the URL passed to urlopen."""
        captured: list[str] = []

        class _FakeResp:
            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *_a: object) -> None:
                pass

            def read(self) -> bytes:
                return fake_response_body or b'{"price": "1.00"}'

        def _fake_urlopen(url: str, timeout: int = 5) -> _FakeResp:
            captured.append(url)
            return _FakeResp()

        with patch("urllib.request.urlopen", _fake_urlopen):
            run_autonomous_mod._fetch_price_rest(symbol, testnet=False)

        assert len(captured) == 1, "urlopen called wrong number of times"
        return captured[0]

    def test_ascii_symbol_url_unchanged(self) -> None:
        url = self._capture_urlopen_url("BTCUSDT")
        assert url == "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT"

    def test_unicode_symbol_is_percent_encoded(self) -> None:
        """Non-ASCII symbol must be percent-encoded so urlopen can ASCII-encode
        the request line without raising UnicodeEncodeError."""
        url = self._capture_urlopen_url("币安人生USDT")
        # The resulting URL must be 100% ASCII
        url.encode("ascii")  # must not raise
        # Specifically, the Chinese chars must be percent-encoded
        assert "%E5%B8%81%E5%AE%89%E4%BA%BA%E7%94%9F" in url
        assert "USDT" in url  # trailing ASCII preserved
        assert "symbol=" in url  # query param preserved

    def test_special_chars_in_symbol_are_encoded(self) -> None:
        """The `safe=''` kwarg must percent-encode even URL-reserved chars
        so the symbol cannot escape the query parameter.
        """
        url = self._capture_urlopen_url("FOO&bar=baz")
        url.encode("ascii")
        # `&` and `=` would otherwise inject extra query params
        assert "FOO%26bar%3Dbaz" in url

    def test_testnet_base_url_preserved(self) -> None:
        captured: list[str] = []

        class _FakeResp:
            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *_a: object) -> None:
                pass

            def read(self) -> bytes:
                return b'{"price": "1.00"}'

        def _fake_urlopen(url: str, timeout: int = 5) -> _FakeResp:
            captured.append(url)
            return _FakeResp()

        with patch("urllib.request.urlopen", _fake_urlopen):
            run_autonomous_mod._fetch_price_rest("币安人生USDT", testnet=True)

        assert captured[0].startswith("https://testnet.binancefuture.com")
        captured[0].encode("ascii")  # must not raise

    def test_unicode_symbol_returns_price_on_success(self) -> None:
        """Successful fetch (mocked) with Unicode symbol returns parsed Decimal."""

        class _FakeResp:
            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *_a: object) -> None:
                pass

            def read(self) -> bytes:
                return b'{"price": "42.5"}'

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
            result = run_autonomous_mod._fetch_price_rest("币安人生USDT", testnet=False)
        assert result == Decimal("42.5")

    def test_urlopen_fail_still_returns_none(self) -> None:
        """Fail-open wrapper is preserved: network/HTTP failures return None."""

        def _fake_urlopen(_url: str, timeout: int = 5) -> None:
            raise RuntimeError("network down")

        with patch("urllib.request.urlopen", _fake_urlopen):
            result = run_autonomous_mod._fetch_price_rest("BTCUSDT", testnet=False)
        assert result is None


class TestRefresherFetchPriceUnicodeSymbol:
    """``TuningRefresher._fetch_price`` is the background-refresh seam.

    Mirrors the cold-bootstrap function but lives on the refresher so the
    existing daemon thread doesn't import from ``scripts``. Same percent-
    encoding invariant applies.
    """

    def _capture_urlopen_url(self, symbol: str) -> str:
        captured: list[str] = []

        class _FakeResp:
            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *_a: object) -> None:
                pass

            def read(self) -> bytes:
                return b'{"price": "1.00"}'

        def _fake_urlopen(url: str, timeout: int = 5) -> _FakeResp:
            captured.append(url)
            return _FakeResp()

        import urllib.request  # noqa: PLC0415

        with patch.object(urllib.request, "urlopen", _fake_urlopen):
            TuningRefresher._fetch_price(symbol, testnet=False)

        assert len(captured) == 1
        return captured[0]

    def test_ascii_symbol_url_unchanged(self) -> None:
        url = self._capture_urlopen_url("ETHUSDT")
        assert url == "https://fapi.binance.com/fapi/v1/ticker/price?symbol=ETHUSDT"

    def test_unicode_symbol_is_percent_encoded(self) -> None:
        url = self._capture_urlopen_url("币安人生USDT")
        url.encode("ascii")  # must not raise
        assert "%E5%B8%81%E5%AE%89%E4%BA%BA%E7%94%9F" in url
        assert "USDT" in url

    def test_fail_open_returns_none(self) -> None:
        import urllib.request  # noqa: PLC0415

        def _boom(_url: str, timeout: int = 5) -> None:
            raise RuntimeError("connection refused")

        with patch.object(urllib.request, "urlopen", _boom):
            result = TuningRefresher._fetch_price("BTCUSDT", testnet=False)
        assert result is None


class TestBootstrapTuningCacheHandlesUnicodeSymbol:
    """End-to-end regression: ``_bootstrap_tuning_cache`` must not crash when
    the input ``symbols`` list contains non-ASCII entries. The symbol should
    either get a normal price or be skipped cleanly — never propagate a
    UnicodeEncodeError up the stack.
    """

    def test_mixed_ascii_and_unicode_symbols_do_not_crash(self) -> None:
        """Bootstrap iterates BTCUSDT + 币安人生USDT and completes cleanly."""

        # Patch _fetch_price_rest to simulate: BTC returns price, Unicode
        # symbol returns None (as it would if Binance rejects or URL fetch
        # hits any error). The key invariant: no exception leaks up.
        def _fake_fetch(symbol: str, testnet: bool = False) -> Decimal | None:
            if symbol == "BTCUSDT":
                return Decimal("50000")
            return None  # simulate "unknown symbol on exchange"

        args = MagicMock()
        args.mainnet = False

        with (
            patch.object(run_autonomous_mod, "_fetch_price_rest", side_effect=_fake_fetch),
            patch.object(
                run_autonomous_mod,
                "_derive_bootstrap_symbol_risk_budget",
                return_value=Decimal("100"),
            ),
            patch(
                "grinder.execution.constraint_provider.ConstraintProvider.get_constraints",
                return_value={},
            ),
        ):
            # Pass mixed list — must NOT raise
            tuned_sizes, tuned_results = run_autonomous_mod._bootstrap_tuning_cache(
                ["BTCUSDT", "币安人生USDT"],
                cache=MagicMock(),
                args=args,
                natr_map={"BTCUSDT": Decimal("1.5"), "币安人生USDT": Decimal("1.5")},
            )

        # Bootstrap completed without exception — the Unicode symbol was
        # skipped cleanly via the fail-open paths. BTCUSDT may or may not
        # have tuned (depends on constraints) but the important thing is
        # the run completed.
        assert isinstance(tuned_sizes, dict)
        assert isinstance(tuned_results, dict)
