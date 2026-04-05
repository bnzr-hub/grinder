"""Shared user-data WebSocket runtime for event-first fill/cancel path.

Provides reusable helpers for both `run_trading.py` and `LiveEngineBridge`:
- `build_user_data_connector()` — construct a FuturesUserDataWsConnector
- `run_user_data_loop()` — retry loop that feeds events to engine

Phase 2 only (ORDER_TRADE_UPDATE). Phase 3 (ACCOUNT_UPDATE position authority)
is not implemented here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def build_user_data_connector(
    *,
    symbol: str | None = None,
    use_testnet: bool = True,
) -> Any | None:
    """Build optional user-data WS connector. Returns None if conditions not met.

    Conditions for activation:
    - GRINDER_USER_DATA_IMMEDIATE_ENABLED env not explicitly "false"
    - BINANCE_API_KEY present
    """
    from grinder.connectors.binance_user_data_ws import (  # noqa: PLC0415
        BINANCE_FUTURES_MAINNET_URL,
        BINANCE_FUTURES_TESTNET_URL,
        FuturesUserDataWsConnector,
        ListenKeyConfig,
        ListenKeyManager,
        UserDataWsConfig,
    )
    from grinder.connectors.data_connector import TimeoutConfig  # noqa: PLC0415
    from grinder.env_parse import parse_bool  # noqa: PLC0415

    enabled = parse_bool("GRINDER_USER_DATA_IMMEDIATE_ENABLED", default=True, strict=False)
    if not enabled:
        logger.info("USER_DATA_DISABLED reason=env_toggle")
        return None

    api_key = os.environ.get("BINANCE_API_KEY", "").strip()
    if not api_key:
        logger.info("USER_DATA_DISABLED reason=missing_api_key")
        return None

    base_url = BINANCE_FUTURES_TESTNET_URL if use_testnet else BINANCE_FUTURES_MAINNET_URL
    ws_cfg = UserDataWsConfig(
        base_url=base_url,
        api_key=api_key,
        use_testnet=use_testnet,
        symbol_filter=symbol,
        timeout=TimeoutConfig(connect_timeout_ms=10000),
    )
    lk_cfg = ListenKeyConfig(base_url=base_url, api_key=api_key)

    try:
        from scripts.http_measured_client import RequestsHttpClient  # noqa: PLC0415

        http_client = RequestsHttpClient(port_name="user_data")
    except ImportError:
        from grinder.connectors.binance_user_data_ws import (  # noqa: PLC0415
            RequestsHttpClient as FallbackClient,
        )

        http_client = FallbackClient()  # type: ignore[call-arg]

    lk_mgr = ListenKeyManager(http_client, lk_cfg)
    logger.info(
        "USER_DATA_CONNECTOR_BUILT symbol_filter=%s net=%s",
        symbol or "none",
        "testnet" if use_testnet else "mainnet",
    )
    return FuturesUserDataWsConnector(config=ws_cfg, listen_key_manager=lk_mgr)


async def run_user_data_loop(
    make_conn: Callable[[], Any],
    on_event: Callable[[Any], None],
    shutdown: asyncio.Event,
    *,
    max_retries: int = 5,
) -> None:
    """Retry loop for user-data WS: fresh connector per attempt, shutdown-aware.

    Args:
        make_conn: Factory that creates a fresh connector (with connect/iter_events/close).
        on_event: Callback for each received event.
        shutdown: Event to signal graceful stop. Wakes up backoff sleeps immediately.
        max_retries: Maximum outer restart attempts.
    """
    for attempt in range(max_retries):
        if shutdown.is_set():
            break
        conn = make_conn()
        try:
            await conn.connect()
            async for event in conn.iter_events():
                if shutdown.is_set():
                    return
                on_event(event)
        except Exception as e:
            if shutdown.is_set():
                break
            delay = min(2**attempt * 3, 30)
            logger.warning(
                "USER_DATA_LOOP_ATTEMPT attempt=%d/%d error=%s restart_in=%ds",
                attempt + 1,
                max_retries,
                e,
                delay,
            )
            if attempt < max_retries - 1:
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=delay)
                    return
                except TimeoutError:
                    pass
        else:
            break
        finally:
            with contextlib.suppress(Exception):
                await conn.close()


async def bridge_shutdown_from_threading_event(
    threading_event: Any,
    async_event: asyncio.Event,
    poll_interval: float = 0.5,
) -> None:
    """Bridge a threading.Event to an asyncio.Event by polling.

    Runs as a background task inside the engine's async loop.
    Sets async_event when threading_event.is_set() becomes True.
    """
    while not threading_event.is_set():
        await asyncio.sleep(poll_interval)
    async_event.set()
