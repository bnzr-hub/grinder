"""Tests for run_user_data_loop retry lifecycle.

Exercises the REAL production helper from scripts/run_trading.py.
Proves:
1. Connect timeout → retry succeeds on second attempt (fresh connector)
2. Full lifecycle failure → outer restart with fresh connector
3. Shutdown aborts retries immediately (including during backoff)
4. Failed connect cleans up (fresh connector per attempt, no leaked state)
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import pytest
from scripts.run_trading import run_user_data_loop


class FakeConnector:
    """Fake connector with controllable connect/iter_events behavior."""

    def __init__(self, *, connect_fail: bool = False, events: list[object] | None = None) -> None:
        self._connect_fail = connect_fail
        self._events = events or []
        self.closed = False
        self.connected = False

    async def connect(self) -> None:
        if self._connect_fail:
            raise TimeoutError("WebSocket connection timeout")
        self.connected = True

    async def close(self) -> None:
        self.closed = True
        self.connected = False

    async def iter_events(self) -> AsyncIterator[object]:
        for event in self._events:
            yield event


class TestRealRetryLoop:
    """Tests exercise the actual run_user_data_loop from run_trading.py."""

    @pytest.mark.asyncio
    async def test_first_fails_second_succeeds(self) -> None:
        """Factory creates fresh connector; first times out, second connects."""
        call_count = 0
        connectors: list[FakeConnector] = []

        def factory() -> FakeConnector:
            nonlocal call_count
            call_count += 1
            c = FakeConnector(connect_fail=(call_count == 1), events=[])
            connectors.append(c)
            return c

        shutdown = asyncio.Event()
        on_event = MagicMock()

        await run_user_data_loop(factory, on_event, shutdown, max_retries=5)

        assert call_count == 2
        assert connectors[0].closed  # first cleaned up
        assert connectors[1].closed  # second also closed after clean exit

    @pytest.mark.asyncio
    async def test_each_attempt_gets_fresh_connector(self) -> None:
        """Factory called once per outer attempt; no connector reuse."""
        call_count = 0
        connectors: list[FakeConnector] = []

        def factory() -> FakeConnector:
            nonlocal call_count
            call_count += 1
            c = FakeConnector(connect_fail=(call_count <= 2), events=[])
            connectors.append(c)
            return c

        shutdown = asyncio.Event()
        await run_user_data_loop(factory, MagicMock(), shutdown, max_retries=5)

        assert call_count == 3
        assert len({id(c) for c in connectors}) == 3  # all distinct
        assert all(c.closed for c in connectors)

    @pytest.mark.asyncio
    async def test_shutdown_aborts_during_backoff(self) -> None:
        """Shutdown during backoff sleep exits promptly, not after full delay."""
        connectors: list[FakeConnector] = []

        def factory() -> FakeConnector:
            c = FakeConnector(connect_fail=True)
            connectors.append(c)
            return c

        shutdown = asyncio.Event()

        async def _set_shutdown_soon() -> None:
            await asyncio.sleep(0.05)  # 50ms — well before 3s backoff
            shutdown.set()

        t0 = time.monotonic()
        # Run both concurrently: loop + shutdown trigger
        await asyncio.gather(
            run_user_data_loop(factory, MagicMock(), shutdown, max_retries=5),
            _set_shutdown_soon(),
        )
        elapsed = time.monotonic() - t0

        # Should exit in <1s, not 3s+ (the first backoff delay)
        assert elapsed < 1.0, f"Took {elapsed:.1f}s — shutdown not prompt"
        assert len(connectors) >= 1
        assert all(c.closed for c in connectors)

    @pytest.mark.asyncio
    async def test_all_failures_clean_up(self) -> None:
        """All failed connectors are closed (no leaked state)."""
        connectors: list[FakeConnector] = []

        def factory() -> FakeConnector:
            c = FakeConnector(connect_fail=True)
            connectors.append(c)
            return c

        shutdown = asyncio.Event()
        await run_user_data_loop(factory, MagicMock(), shutdown, max_retries=3)

        assert len(connectors) == 3
        assert all(c.closed for c in connectors)

    @pytest.mark.asyncio
    async def test_events_delivered_to_callback(self) -> None:
        """Events from iter_events are passed to on_event callback."""
        sentinel = object()

        def factory() -> FakeConnector:
            return FakeConnector(events=[sentinel])

        shutdown = asyncio.Event()
        on_event = MagicMock()

        await run_user_data_loop(factory, on_event, shutdown, max_retries=1)

        on_event.assert_called_once_with(sentinel)

    @pytest.mark.asyncio
    async def test_shutdown_before_first_attempt(self) -> None:
        """If shutdown is already set, no connector is created."""
        call_count = 0

        def factory() -> FakeConnector:
            nonlocal call_count
            call_count += 1
            return FakeConnector()

        shutdown = asyncio.Event()
        shutdown.set()

        await run_user_data_loop(factory, MagicMock(), shutdown, max_retries=5)

        assert call_count == 0
