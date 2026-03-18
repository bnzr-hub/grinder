"""Connector exception hierarchy.

Provides structured exceptions for connector operations with clear
classification for retry/circuit-breaker logic in H2/H4.

Exception hierarchy:
- ConnectorError (base)
  - ConnectorTimeoutError (timeout on connect/read/write/close)
  - ConnectorClosedError (operation on closed connector)
  - ConnectorIOError (general I/O errors)
    - ConnectorTransientError (retryable: network, 5xx, 429)
    - ConnectorNonRetryableError (not retryable: 4xx, auth, validation)
  - IdempotencyConflictError (duplicate request while INFLIGHT, non-retryable)
  - CircuitOpenError (circuit breaker is OPEN, non-retryable fast-fail)
"""

from __future__ import annotations


class ConnectorError(Exception):
    """Base exception for all connector errors."""

    pass


class ConnectorTimeoutError(ConnectorError):
    """Timeout during connector operation.

    Attributes:
        op: Operation that timed out (connect, read, write, close)
        timeout_ms: Timeout value in milliseconds
    """

    def __init__(self, op: str, timeout_ms: int, message: str | None = None) -> None:
        self.op = op
        self.timeout_ms = timeout_ms
        msg = message or f"Timeout during {op} after {timeout_ms}ms"
        super().__init__(msg)


class ConnectorClosedError(ConnectorError):
    """Operation attempted on closed connector."""

    def __init__(self, op: str | None = None) -> None:
        self.op = op
        msg = f"Cannot {op}: connector is closed" if op else "Connector is closed"
        super().__init__(msg)


class ConnectorIOError(ConnectorError):
    """General I/O error during connector operation.

    Base class for transient and non-retryable errors.
    """

    pass


class ConnectorTransientError(ConnectorIOError):
    """Transient error that is safe to retry.

    Examples: network errors, 5xx responses, 429 rate limits.
    """

    pass


class ConnectorNonRetryableError(ConnectorIOError):
    """Error that should not be retried.

    Examples: 4xx responses, authentication failures, validation errors.

    Attributes:
        pre_send: True if error occurred before any HTTP request was sent.
            Local validation errors (symbol, notional, order count) set this
            to True. Exchange-returned errors (4xx from Binance) leave it False.
    """

    def __init__(self, *args: object, pre_send: bool = False) -> None:
        super().__init__(*args)
        self.pre_send = pre_send


class IdempotencyConflictError(ConnectorError):
    """Duplicate request detected while another is in-flight.

    This error is raised when:
    - A request with the same idempotency key is already being processed (INFLIGHT)
    - The caller should NOT retry with this key until the in-flight request completes

    This is a non-retryable error by design (fast-fail pattern in H3 v1).

    Attributes:
        key: The idempotency key that caused the conflict
        status: Current status of the conflicting entry (typically INFLIGHT)
    """

    def __init__(self, key: str, status: str = "INFLIGHT") -> None:
        self.key = key
        self.status = status
        super().__init__(f"Idempotency conflict: key '{key}' is {status}")


class CircuitOpenError(ConnectorError):
    """Circuit breaker is OPEN, operation rejected.

    This error is raised when:
    - The circuit breaker for an operation is in OPEN state
    - Or in HALF_OPEN state with probe limit reached

    This is a non-retryable error by design (fast-fail pattern in H4 v1).
    The caller should NOT retry until the circuit transitions to HALF_OPEN/CLOSED.

    Attributes:
        op_name: Operation that was rejected (place, cancel, replace, etc.)
        circuit_state: Current state of the circuit (OPEN or HALF_OPEN)
    """

    def __init__(self, op_name: str, circuit_state: str = "OPEN") -> None:
        self.op_name = op_name
        self.circuit_state = circuit_state
        super().__init__(f"Circuit breaker OPEN for '{op_name}' (state: {circuit_state})")
