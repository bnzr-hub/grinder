#!/usr/bin/env python3
"""Place a single test order for Stage D testing.

This script places one limit order far from market to test reconcile cancel.
The order prefers grinder_d_... clientOrderId format and falls back to g_d_...
if the symbol would exceed Binance's 36-char clientOrderId limit.

Usage:
    source .env.stage_d
    ALLOW_MAINNET_TRADE=1 PYTHONPATH=src python3 -m scripts.place_test_order

Safety:
    - Far from market price (won't fill)
    - Minimum notional (~$110)
    - Single order only
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

try:
    import requests
except ImportError:
    print("ERROR: requests library required")
    sys.exit(1)

from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.binance_futures_port import (
    BINANCE_FUTURES_MAINNET_URL,
    BinanceFuturesPort,
    BinanceFuturesPortConfig,
)
from grinder.execution.binance_port import HttpResponse
from grinder.reconcile.identity import OrderIdentityConfig, generate_client_order_id


@dataclass
class RequestsHttpClient:
    """HTTP client using requests library."""

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 5000,
        op: str = "",  # noqa: ARG002
    ) -> HttpResponse:
        timeout_s = timeout_ms / 1000.0
        try:
            if method == "GET":
                resp = requests.get(url, params=params, headers=headers, timeout=timeout_s)
            elif method == "POST":
                resp = requests.post(url, params=params, headers=headers, timeout=timeout_s)
            elif method == "DELETE":
                resp = requests.delete(url, params=params, headers=headers, timeout=timeout_s)
            else:
                raise ConnectorNonRetryableError(f"Unsupported method: {method}")
            return HttpResponse(
                status_code=resp.status_code,
                json_data=resp.json() if resp.content else {},
            )
        except requests.exceptions.Timeout as e:
            raise ConnectorTransientError(f"Request timeout: {e}") from e
        except requests.exceptions.ConnectionError as e:
            raise ConnectorTransientError(f"Connection error: {e}") from e
        except requests.exceptions.RequestException as e:
            raise ConnectorNonRetryableError(f"Request error: {e}") from e


def _select_identity_config(
    symbol: str, strategy_id: str, level_id: int
) -> tuple[OrderIdentityConfig, str]:
    """Pick CID prefix that fits Binance 36-char clientOrderId limit.

    Prefers legacy-compatible "grinder_" prefix for existing Stage D flows.
    Falls back to short "g_" prefix when symbol length would overflow.
    """
    ts_ms = int(time.time() * 1000)
    for prefix in ("grinder_", "g_"):
        config = OrderIdentityConfig(prefix=prefix, strategy_id=strategy_id)
        try:
            sample_cid = generate_client_order_id(config, symbol, level_id, ts_ms, 1)
            return config, sample_cid
        except ValueError:
            continue
    raise ValueError(
        f"Cannot build valid clientOrderId for symbol={symbol}, strategy_id={strategy_id}, level_id={level_id}"
    )


def main() -> int:
    # Check env
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    allow_mainnet = os.environ.get("ALLOW_MAINNET_TRADE", "")

    if not api_key or not api_secret:
        print("ERROR: BINANCE_API_KEY and BINANCE_API_SECRET required")
        return 1
    if allow_mainnet != "1":
        print("ERROR: ALLOW_MAINNET_TRADE=1 required")
        return 1

    # Config
    symbol = "BTCUSDT"
    # Far from market - BUY at $40,000 when BTC is ~$100k
    price = Decimal("40000.00")
    # Minimum quantity to meet $100 notional requirement
    quantity = Decimal("0.003")  # $120 notional at $40k

    print("=" * 60)
    print("PLACING TEST ORDER FOR STAGE D")
    print("=" * 60)
    print(f"  Symbol:   {symbol}")
    print("  Side:     BUY")
    print(f"  Price:    ${price}")
    print(f"  Quantity: {quantity}")
    print(f"  Notional: ${price * quantity}")
    print()
    # Create port with identity config.
    # Keep legacy "grinder_" when it fits; fallback to short "g_" for long symbols.
    identity_config, sample_cid = _select_identity_config(symbol, strategy_id="d", level_id=0)
    print(f"  ClientOrderId example: {sample_cid}")
    if identity_config.prefix == "g_":
        print("  ClientOrderId prefix fallback: g_ (grinder_ would exceed 36-char Binance limit)")
    print("=" * 60)

    http_client = RequestsHttpClient()
    config = BinanceFuturesPortConfig(
        mode=SafeMode.LIVE_TRADE,
        base_url=BINANCE_FUTURES_MAINNET_URL,
        api_key=api_key,
        api_secret=api_secret,
        symbol_whitelist=[symbol],
        dry_run=False,
        allow_mainnet=True,
        max_notional_per_order=Decimal("200"),
        max_orders_per_run=1,
        max_open_orders=1,
        target_leverage=1,
        identity_config=identity_config,
    )

    port = BinanceFuturesPort(http_client=http_client, config=config)

    # Place order with short clientOrderId (Binance limit: 36 chars max)
    # Uses seconds timestamp (not ms) to keep ID short
    ts = int(time.time())
    try:
        order_id = port.place_order(
            symbol=symbol,
            side=OrderSide.BUY,
            price=price,
            quantity=quantity,
            level_id=0,  # Short level ID
            ts=ts,  # Seconds instead of ms
            reduce_only=False,
        )
        print("\nORDER PLACED:")
        print(f"  clientOrderId: {order_id}")
        print()
        print("Now run Stage D to cancel this order.")
        return 0

    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        print(f"\nERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
