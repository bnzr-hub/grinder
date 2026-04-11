"""Shared autonomous tuning/selector state (ADR-162).

Thread-safe container for the latest tuning results and selector features.
Selector closures read from this at invocation time (not construction time).
TuningRefresher atomically replaces the state on each successful refresh.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from decimal import Decimal


class AutonomousTuningState:
    """Thread-safe shared state for autonomous tuning and selector features.

    All read accessors return the current snapshot references.
    replace() swaps all references atomically under a lock.
    """

    def __init__(
        self,
        *,
        candidates: list[str] | None = None,
        tuned_results: dict[str, Any] | None = None,
        tuned_sizes: dict[str, str] | None = None,
        natr_map: dict[str, Decimal] | None = None,
        v1_features: dict[str, Any] | None = None,
        v2_features: dict[str, Any] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._candidates = candidates or []
        self._tuned_results = tuned_results or {}
        self._tuned_sizes = tuned_sizes or {}
        self._natr_map = natr_map or {}
        self._v1_features = v1_features or {}
        self._v2_features = v2_features or {}
        self._version = 0

    @property
    def candidates(self) -> list[str]:
        return self._candidates

    @property
    def tuned_results(self) -> dict[str, Any]:
        return self._tuned_results

    @property
    def tuned_sizes(self) -> dict[str, str]:
        return self._tuned_sizes

    @property
    def natr_map(self) -> dict[str, Decimal]:
        return self._natr_map

    @property
    def v1_features(self) -> dict[str, Any]:
        return self._v1_features

    @property
    def v2_features(self) -> dict[str, Any]:
        return self._v2_features

    @property
    def version(self) -> int:
        return self._version

    def replace(
        self,
        *,
        tuned_results: dict[str, Any],
        tuned_sizes: dict[str, str],
        natr_map: dict[str, Decimal],
        v1_features: dict[str, Any],
        v2_features: dict[str, Any],
        candidates: list[str] | None = None,
    ) -> None:
        """Atomically replace all state references.

        ``candidates`` is optional for backwards compatibility with the
        cold bootstrap path and existing refresh cycles that only refresh
        the known tuned set. When supplied (ADR-183 PR-2 dynamic admission),
        the candidates list is replaced under the same lock as the other
        maps so downstream readers can never observe a torn snapshot where
        a symbol is in ``candidates`` but missing from one of the map
        fields, or vice versa.
        """
        with self._lock:
            if candidates is not None:
                self._candidates = candidates
            self._tuned_results = tuned_results
            self._tuned_sizes = tuned_sizes
            self._natr_map = natr_map
            self._v1_features = v1_features
            self._v2_features = v2_features
            self._version += 1
