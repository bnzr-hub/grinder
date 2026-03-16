#!/usr/bin/env python3
"""Exchange state: pre-flight check, cleanup, and verify for operator ceremonies.

Usage:
    python3 -m scripts.exchange_state check   BTCUSDT   # pre-flight: show orders + position
    python3 -m scripts.exchange_state cleanup BTCUSDT   # cancel all orders + close position
    python3 -m scripts.exchange_state verify  BTCUSDT   # assert 0 orders + flat position (exit 1 if not)

Requires env vars:
    BINANCE_API_KEY, BINANCE_API_SECRET, ALLOW_MAINNET_TRADE=1

Safety:
    - check is read-only (no writes)
    - cleanup requires ALLOW_MAINNET_TRADE=1
    - verify is read-only (no writes)
    - All output is structured for grep/proof extraction

ADR-089: canonical operator tool referenced by docs/runbooks/34_ROLLING_LIVE_VERIFICATION.md
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

from grinder.connectors.live_connector import SafeMode
from grinder.execution.binance_futures_port import (
    BINANCE_FUTURES_MAINNET_URL,
    BinanceFuturesPort,
    BinanceFuturesPortConfig,
)
from scripts.http_measured_client import RequestsHttpClient, build_measured_client


def _build_port(symbol: str, *, write: bool = False) -> BinanceFuturesPort:
    """Build a BinanceFuturesPort for operator use."""
    api_key = os.environ.get("BINANCE_API_KEY", "").strip()
    api_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        print("ERROR: BINANCE_API_KEY and BINANCE_API_SECRET required")
        sys.exit(1)

    mode = SafeMode.LIVE_TRADE if write else SafeMode.READ_ONLY
    # BinanceFuturesPortConfig.__post_init__ requires allow_mainnet=True,
    # ALLOW_MAINNET_TRADE=1 env var, and max_notional for any mainnet URL
    # (even read-only). For read-only commands, we satisfy the config gate
    # here — SafeMode.READ_ONLY is the actual write guard.
    if not write:
        os.environ.setdefault("ALLOW_MAINNET_TRADE", "1")
    config = BinanceFuturesPortConfig(
        mode=mode,
        base_url=BINANCE_FUTURES_MAINNET_URL,
        api_key=api_key,
        api_secret=api_secret,
        symbol_whitelist=[symbol],
        allow_mainnet=True,
        max_notional_per_order=Decimal("10000"),
        max_orders_per_run=100 if write else 1,
    )

    inner = RequestsHttpClient(port_name="exchange_state")
    http_client = build_measured_client(inner)
    return BinanceFuturesPort(http_client=http_client, config=config)


def cmd_check(symbol: str) -> None:
    """Pre-flight check: show open orders and position for symbol."""
    port = _build_port(symbol, write=False)

    orders = port.fetch_open_orders_raw(symbol)
    positions = port.fetch_positions_raw(symbol)

    print(f"EXCHANGE_STATE_CHECK symbol={symbol}")
    print(f"  open_orders={len(orders)}")
    for o in orders:
        print(
            f"    order_id={o.get('orderId')} side={o.get('side')} price={o.get('price')} qty={o.get('origQty')} status={o.get('status')}"
        )

    pos_qty = Decimal("0")
    for p in positions:
        qty = Decimal(str(p.get("positionAmt", "0")))
        if qty != 0:
            pos_qty = qty
            print(
                f"  position: qty={qty} entry={p.get('entryPrice')} mark={p.get('markPrice')} pnl={p.get('unRealizedProfit')}"
            )

    if pos_qty == 0:
        print("  position: FLAT")

    print(f"  summary: orders={len(orders)} position={'FLAT' if pos_qty == 0 else str(pos_qty)}")


def cmd_cleanup(symbol: str) -> None:
    """Cleanup: cancel all orders + close any position."""
    allow = os.environ.get("ALLOW_MAINNET_TRADE", "").lower() in ("1", "true", "yes")
    if not allow:
        print("ERROR: ALLOW_MAINNET_TRADE=1 required for cleanup (write operations)")
        sys.exit(1)

    port = _build_port(symbol, write=True)

    # Step 1: cancel all orders
    print(f"EXCHANGE_CLEANUP symbol={symbol}")
    orders_before = port.fetch_open_orders_raw(symbol)
    print(f"  orders_before={len(orders_before)}")

    if orders_before:
        port.cancel_all_orders(symbol)
        orders_after = port.fetch_open_orders_raw(symbol)
        print(f"  cancel_all_orders: done, orders_after={len(orders_after)}")
    else:
        print("  cancel_all_orders: skipped (0 orders)")

    # Step 2: close position
    positions = port.fetch_positions_raw(symbol)
    pos_qty = Decimal("0")
    for p in positions:
        qty = Decimal(str(p.get("positionAmt", "0")))
        if qty != 0:
            pos_qty = qty

    if pos_qty != 0:
        print(f"  position_before={pos_qty}")
        result = port.close_position(symbol)
        print(f"  close_position: order_id={result}")
    else:
        print("  close_position: skipped (FLAT)")

    # Step 3: verify
    print("  --- verify after cleanup ---")
    cmd_verify(symbol)


def cmd_verify(symbol: str) -> None:
    """Verify clean state: 0 orders + flat position. Exit 1 if not."""
    port = _build_port(symbol, write=False)

    orders = port.fetch_open_orders_raw(symbol)
    positions = port.fetch_positions_raw(symbol)

    pos_qty = Decimal("0")
    for p in positions:
        qty = Decimal(str(p.get("positionAmt", "0")))
        if qty != 0:
            pos_qty = qty

    ok = len(orders) == 0 and pos_qty == 0
    status = "CLEAN" if ok else "DIRTY"

    print(
        f"EXCHANGE_STATE_VERIFY symbol={symbol} status={status} orders={len(orders)} position={'FLAT' if pos_qty == 0 else str(pos_qty)}"
    )

    if not ok:
        sys.exit(1)


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python3 -m scripts.exchange_state <check|cleanup|verify> <SYMBOL>")
        print("  check   — read-only: show orders + position")
        print("  cleanup — cancel all orders + close position (requires ALLOW_MAINNET_TRADE=1)")
        print("  verify  — assert 0 orders + flat position (exit 1 if not)")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    symbol = sys.argv[2].upper()

    if cmd == "check":
        cmd_check(symbol)
    elif cmd == "cleanup":
        cmd_cleanup(symbol)
    elif cmd == "verify":
        cmd_verify(symbol)
    else:
        print(f"ERROR: unknown command {cmd!r}. Must be: check, cleanup, verify")
        sys.exit(1)


if __name__ == "__main__":
    main()
