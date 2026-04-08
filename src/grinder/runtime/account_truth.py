"""Exchange-account truth helpers for autonomous runtime.

Shared by bootstrap tuning and tuning refresher so autonomous sizing can
derive from the same real exchange/account facts instead of legacy defaults.
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


def fetch_futures_equity(*, testnet: bool) -> Decimal | None:
    """Fetch USDT futures equity from Binance REST.

    Returns wallet balance + unrealized PnL for USDT, matching total margin
    balance semantics used by the autonomous risk loop.
    """
    try:
        data = _signed_get("/fapi/v2/balance", testnet=testnet)
        if not isinstance(data, list):
            return None
        for asset in data:
            if asset.get("asset") == "USDT":
                wallet = Decimal(str(asset.get("crossWalletBalance", "0")))
                upnl = Decimal(str(asset.get("crossUnPnl", "0")))
                return wallet + upnl
    except Exception:
        return None
    return None


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
