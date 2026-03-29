"""Tests for Observability + Reason Codes (PR-7, ADR-106).

Pins the reason code taxonomy to prevent drift.
Tests classify_no_action_reason for all major paths.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from grinder.account.contracts import AccountSnapshot, PositionSnap
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import BlockReason, LiveEngineConfig, LiveEngineV0
from grinder.live.health_gate import LiveHealthMode
from grinder.live.reason_codes import (
    EntrySuppressionReason,
    ExitSuppressionReason,
    HealthBlockReason,
    NoActionReason,
    RepairStatusReason,
    classify_no_action_reason,
)


class TestNoActionReasonEnum:
    """NoActionReason enum values are stable."""

    def test_all_values(self) -> None:
        assert (
            NoActionReason.ACTUAL_MATCHES_EFFECTIVE_TARGET.value
            == "ACTUAL_MATCHES_EFFECTIVE_TARGET"
        )
        assert NoActionReason.RISK_SATURATED_TARGET_ZERO.value == "RISK_SATURATED_TARGET_ZERO"
        assert (
            NoActionReason.EFFECTIVE_TARGET_PARTIAL_MATCHED.value
            == "EFFECTIVE_TARGET_PARTIAL_MATCHED"
        )
        assert NoActionReason.AWAITING_SYNC.value == "AWAITING_SYNC"
        assert NoActionReason.NOT_STARTED.value == "NOT_STARTED"
        assert NoActionReason.RECONSTRUCTION_PENDING.value == "RECONSTRUCTION_PENDING"

    def test_count(self) -> None:
        assert len(NoActionReason) == 6


class TestEntrySuppressionReasonEnum:
    """EntrySuppressionReason enum values are stable."""

    def test_all_values(self) -> None:
        assert EntrySuppressionReason.RISK_SATURATED.value == "RISK_SATURATED"
        assert EntrySuppressionReason.EFFECTIVE_TARGET_ZERO.value == "EFFECTIVE_TARGET_ZERO"
        assert EntrySuppressionReason.EFFECTIVE_TARGET_PARTIAL.value == "EFFECTIVE_TARGET_PARTIAL"
        assert EntrySuppressionReason.NO_RESTORE_DEMAND.value == "NO_RESTORE_DEMAND"
        assert EntrySuppressionReason.INVENTORY_FULL.value == "INVENTORY_FULL"


class TestExitSuppressionReasonEnum:
    """ExitSuppressionReason enum values are stable."""

    def test_all_values(self) -> None:
        assert (
            ExitSuppressionReason.REDUCE_ONLY_BUDGET_EXCEEDED.value == "REDUCE_ONLY_BUDGET_EXCEEDED"
        )
        assert (
            ExitSuppressionReason.EXIT_NOT_REGISTERED_DEFERRED.value
            == "EXIT_NOT_REGISTERED_DEFERRED"
        )
        assert (
            ExitSuppressionReason.PENDING_REPAIR_AFTER_REJECT.value == "PENDING_REPAIR_AFTER_REJECT"
        )
        assert (
            ExitSuppressionReason.TOPOLOGY_REPAIR_INCOMPLETE.value == "TOPOLOGY_REPAIR_INCOMPLETE"
        )


class TestHealthBlockReasonEnum:
    """HealthBlockReason enum values are stable."""

    def test_all_values(self) -> None:
        assert HealthBlockReason.STALE_TRUTH.value == "STALE_TRUTH"
        assert HealthBlockReason.PAUSED_UNSAFE.value == "PAUSED_UNSAFE"
        assert HealthBlockReason.DEGRADED_WS.value == "DEGRADED_WS"
        assert HealthBlockReason.DEGRADED_SYNC.value == "DEGRADED_SYNC"


class TestRepairStatusReasonEnum:
    """RepairStatusReason enum values are stable."""

    def test_all_values(self) -> None:
        assert RepairStatusReason.START.value == "START"
        assert RepairStatusReason.CANCEL.value == "CANCEL"
        assert RepairStatusReason.PLACE.value == "PLACE"
        assert RepairStatusReason.DEFERRED.value == "DEFERRED"
        assert RepairStatusReason.CONVERGED.value == "CONVERGED"
        assert RepairStatusReason.INCOMPLETE.value == "INCOMPLETE"


class TestClassifyNoActionReason:
    """classify_no_action_reason for all major paths."""

    def test_healthy_steady_state(self) -> None:
        """Actual matches effective target → healthy no-op."""
        reason = classify_no_action_reason(
            recon_has_diff=False,
            theoretical_entries=4,
            effective_entries=4,
            actual_entries=4,
            is_risk_saturated=False,
            is_awaiting_sync=False,
            is_started=True,
            reconstruction_ok=True,
        )
        assert reason == NoActionReason.ACTUAL_MATCHES_EFFECTIVE_TARGET

    def test_risk_saturated_target_zero(self) -> None:
        """Risk-saturated, effective=0, theoretical>0 → distinct from healthy."""
        reason = classify_no_action_reason(
            recon_has_diff=False,
            theoretical_entries=4,
            effective_entries=0,
            actual_entries=0,
            is_risk_saturated=True,
            is_awaiting_sync=False,
            is_started=True,
            reconstruction_ok=True,
        )
        assert reason == NoActionReason.RISK_SATURATED_TARGET_ZERO

    def test_partial_projection_matched(self) -> None:
        """Effective < theoretical but actual == effective → partial matched."""
        reason = classify_no_action_reason(
            recon_has_diff=False,
            theoretical_entries=4,
            effective_entries=2,
            actual_entries=2,
            is_risk_saturated=False,
            is_awaiting_sync=False,
            is_started=True,
            reconstruction_ok=True,
        )
        assert reason == NoActionReason.EFFECTIVE_TARGET_PARTIAL_MATCHED

    def test_not_started(self) -> None:
        reason = classify_no_action_reason(
            recon_has_diff=False,
            theoretical_entries=0,
            effective_entries=0,
            actual_entries=0,
            is_risk_saturated=False,
            is_awaiting_sync=False,
            is_started=False,
            reconstruction_ok=False,
        )
        assert reason == NoActionReason.NOT_STARTED

    def test_awaiting_sync(self) -> None:
        reason = classify_no_action_reason(
            recon_has_diff=False,
            theoretical_entries=0,
            effective_entries=0,
            actual_entries=0,
            is_risk_saturated=False,
            is_awaiting_sync=True,
            is_started=True,
            reconstruction_ok=True,
        )
        assert reason == NoActionReason.AWAITING_SYNC

    def test_reconstruction_pending(self) -> None:
        reason = classify_no_action_reason(
            recon_has_diff=False,
            theoretical_entries=0,
            effective_entries=0,
            actual_entries=0,
            is_risk_saturated=False,
            is_awaiting_sync=False,
            is_started=True,
            reconstruction_ok=False,
        )
        assert reason == NoActionReason.RECONSTRUCTION_PENDING

    def test_has_diff_returns_none(self) -> None:
        """When there ARE actions, no reason needed."""
        reason = classify_no_action_reason(
            recon_has_diff=True,
            theoretical_entries=4,
            effective_entries=4,
            actual_entries=2,
            is_risk_saturated=False,
            is_awaiting_sync=False,
            is_started=True,
            reconstruction_ok=True,
        )
        assert reason is None

    def test_healthy_distinguishable_from_saturated(self) -> None:
        """Key invariant: healthy no-op and saturated no-op have different reasons."""
        healthy = classify_no_action_reason(
            recon_has_diff=False,
            theoretical_entries=4,
            effective_entries=4,
            actual_entries=4,
            is_risk_saturated=False,
            is_awaiting_sync=False,
            is_started=True,
            reconstruction_ok=True,
        )
        saturated = classify_no_action_reason(
            recon_has_diff=False,
            theoretical_entries=4,
            effective_entries=0,
            actual_entries=0,
            is_risk_saturated=True,
            is_awaiting_sync=False,
            is_started=True,
            reconstruction_ok=True,
        )
        assert healthy != saturated
        assert healthy == NoActionReason.ACTUAL_MATCHES_EFFECTIVE_TARGET
        assert saturated == NoActionReason.RISK_SATURATED_TARGET_ZERO


class TestEnginePathExitSuppression:
    """Engine-path tests: exit suppression emits stable reason codes."""

    def test_budget_exceeded_emits_exit_suppressed(self) -> None:
        """Budget-exceeded block at Gate 0 uses ExitSuppressionReason."""
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        port = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(paper, port, config)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="SHORT",
                    qty=Decimal("1"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )
        # Try to exit more than position
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("999"),
            reduce_only=True,
            reason="test",
        )
        result = engine._process_action(action, 1000)
        assert result.block_reason == BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED

    def test_pending_repair_emits_exit_suppressed(self) -> None:
        """Pending repair block at Gate 0 uses ExitSuppressionReason."""
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        port = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(paper, port, config)
        engine._reduce_only_pending_repair.add(("BTCUSDT", "BUY"))

        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("1"),
            reduce_only=True,
            reason="test",
        )
        result = engine._process_action(action, 1000)
        assert result.block_reason == BlockReason.REDUCE_ONLY_BUDGET_EXCEEDED


class TestEnginePathHealthBlock:
    """Engine-path test: health gate block emits stable reason."""

    def test_health_gate_block_uses_reason(self) -> None:
        """STALE_TRUTH health mode blocks writes with HEALTH_GATE_UNSAFE."""
        from unittest.mock import patch  # noqa: PLC0415

        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        port = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(paper, port, config)
        engine._health_mode = LiveHealthMode.STALE_TRUTH

        # Patch _is_write_allowed_by_health to return False (simulating STALE_TRUTH)
        with patch.object(engine, "_is_write_allowed_by_health", return_value=False):
            action = ExecutionAction(
                action_type=ActionType.PLACE,
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("49000"),
                quantity=Decimal("1"),
                reason="test_entry",
            )
            result = engine._process_action(action, 1000)
        assert result.block_reason == BlockReason.HEALTH_GATE_UNSAFE
