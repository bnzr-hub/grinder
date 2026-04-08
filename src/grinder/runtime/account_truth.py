"""Exchange-account truth helpers for autonomous runtime.

Shared by bootstrap tuning and tuning refresher so autonomous sizing can
derive from the same canonical exchange/account facts instead of legacy
defaults or ad-hoc balance semantics.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.request
from decimal import Decimal
from typing import Any

from grinder.risk.risk_base import (
    BalanceData,
    RiskBaseConfig,
    RiskBaseMode,
    build_risk_base_snapshot,
)


def compute_gross_exposure_from_positions(positions: list[dict[str, Any]]) -> Decimal:
    """Compute gross exposure from positionRisk payload.

    Returns sum(abs(notional)) across all positions. Malformed rows are skipped.
    """
    gross = Decimal("0")
    for pos in positions:
        notional = pos.get("notional", "0")
        try:
            gross += abs(Decimal(str(notional)))
        except Exception:
            continue
    return gross


def _signed_get(path: str, *, testnet: bool) -> Any:
    api_key = os.environ.get("BINANCE_API_KEY", "").strip()
    api_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        return None

    base = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
    ts = int(time.time() * 1000)
    query = f"timestamp={ts}&recvWindow=10000"
    sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{base}{path}?{query}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _risk_base_config_from_env() -> RiskBaseConfig:
    """Read canonical risk-base mode from env with runtime-compatible defaults."""
    mode_raw = os.environ.get("GRINDER_RISK_BASE_MODE", "total_margin_balance").strip().lower()
    mode = RiskBaseMode(mode_raw) if mode_raw else RiskBaseMode.TOTAL_MARGIN_BALANCE
    min_usd = float(os.environ.get("GRINDER_RISK_BASE_MIN_USD", "50"))
    stale_ttl_s = int(os.environ.get("GRINDER_RISK_BASE_STALE_TTL_S", "30"))
    max_age_hard_s = int(os.environ.get("GRINDER_RISK_BASE_MAX_AGE_HARD_S", "60"))
    return RiskBaseConfig(
        mode=mode,
        min_usd=min_usd,
        stale_ttl_s=stale_ttl_s,
        max_age_hard_s=max_age_hard_s,
    )


def fetch_futures_risk_base(*, testnet: bool) -> Decimal | None:
    """Fetch canonical futures risk base from Binance REST.

    Uses the same balance fields and env-driven mode contract as runtime
    risk-base plumbing, so bootstrap/refresher sizing stays aligned with
    live risk semantics.
    """
    try:
        data = _signed_get("/fapi/v2/account", testnet=testnet)
        if not isinstance(data, dict):
            return None
        balance = BalanceData(
            total_margin_balance=Decimal(str(data.get("totalMarginBalance", "0"))),
            wallet_balance=Decimal(str(data.get("totalWalletBalance", "0"))),
            available_balance=Decimal(str(data.get("availableBalance", "0"))),
            ts_ms=int(time.time() * 1000),
        )
        snapshot = build_risk_base_snapshot(
            balance=balance,
            config=_risk_base_config_from_env(),
            now_ms=int(time.time() * 1000),
        )
        if snapshot is None:
            return None
        return snapshot.value_usd
    except Exception:
        return None
    return None


def fetch_futures_equity(*, testnet: bool) -> Decimal | None:
    """Backward-compatible alias for canonical autonomous risk base fetch."""
    return fetch_futures_risk_base(testnet=testnet)


def fetch_futures_gross_exposure(*, testnet: bool) -> Decimal | None:
    """Fetch gross futures exposure from Binance REST."""
    try:
        data = _signed_get("/fapi/v2/positionRisk", testnet=testnet)
        if not isinstance(data, list):
            return None
        return compute_gross_exposure_from_positions(data)
    except Exception:
        return None
    return None
