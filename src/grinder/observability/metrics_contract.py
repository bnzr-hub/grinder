"""Metrics contract patterns (SSOT) - no external dependencies.

This module contains ONLY the contract patterns for /metrics validation.
It has NO imports from other grinder modules to avoid transitive dependencies
(e.g., redis via HA module).

Used by:
- live_contract.py (runtime contract validation)
- smoke_metrics_contract.py (CI smoke test)
"""

from __future__ import annotations

# Required metric patterns for contract validation
REQUIRED_HEALTHZ_KEYS = ["status", "uptime_s"]
REQUIRED_READYZ_KEYS = ["ready", "role"]

REQUIRED_METRICS_PATTERNS = [
    # System metrics
    "# HELP grinder_up",
    "# TYPE grinder_up",
    "grinder_up 1",
    "# HELP grinder_uptime_seconds",
    "# TYPE grinder_uptime_seconds",
    "grinder_uptime_seconds",
    # Readyz gauges (PR-ALERTS-0)
    "# HELP grinder_readyz_callback_registered",
    "# TYPE grinder_readyz_callback_registered",
    "grinder_readyz_callback_registered",
    "# HELP grinder_readyz_ready",
    "# TYPE grinder_readyz_ready",
    "grinder_readyz_ready",
    # Gating metrics
    "# HELP grinder_gating_allowed_total",
    "# TYPE grinder_gating_allowed_total",
    "# HELP grinder_gating_blocked_total",
    "# TYPE grinder_gating_blocked_total",
    # Risk metrics
    "# HELP grinder_kill_switch_triggered",
    "# TYPE grinder_kill_switch_triggered",
    "grinder_kill_switch_triggered",
    "# HELP grinder_kill_switch_trips_total",
    "# TYPE grinder_kill_switch_trips_total",
    "# HELP grinder_drawdown_pct",
    "# TYPE grinder_drawdown_pct",
    "grinder_drawdown_pct",
    "# HELP grinder_high_water_mark",
    "# TYPE grinder_high_water_mark",
    "grinder_high_water_mark",
    # PR-C3b: Consecutive loss guard
    "# HELP grinder_risk_consecutive_losses",
    "# TYPE grinder_risk_consecutive_losses",
    "grinder_risk_consecutive_losses",  # max across all symbol guards (PR-C3c)
    "# HELP grinder_risk_consecutive_loss_trips_total",
    "# TYPE grinder_risk_consecutive_loss_trips_total",
    "grinder_risk_consecutive_loss_trips_total",  # sum across all symbol guards (PR-C3c)
    # HA metrics
    "# HELP grinder_ha_role",
    "# TYPE grinder_ha_role",
    "grinder_ha_role",
    # LC-20: HA leader metric for remediation gating
    "# HELP grinder_ha_is_leader",
    "# TYPE grinder_ha_is_leader",
    "grinder_ha_is_leader",
    # Connector metrics (H2/H3/H4)
    "# HELP grinder_connector_retries_total",
    "# TYPE grinder_connector_retries_total",
    "# HELP grinder_idempotency_hits_total",
    "# TYPE grinder_idempotency_hits_total",
    "# HELP grinder_idempotency_conflicts_total",
    "# TYPE grinder_idempotency_conflicts_total",
    "# HELP grinder_idempotency_misses_total",
    "# TYPE grinder_idempotency_misses_total",
    "# HELP grinder_circuit_state",
    "# TYPE grinder_circuit_state",
    "# HELP grinder_circuit_rejected_total",
    "# TYPE grinder_circuit_rejected_total",
    "# HELP grinder_circuit_trips_total",
    "# TYPE grinder_circuit_trips_total",
    # Series-level patterns (H5-02): validate metrics emit series with correct label schema
    # These patterns verify the metric is emitted as a series (not just HELP/TYPE)
    # and uses the expected label keys (op, reason, state). Values are NOT checked.
    "grinder_connector_retries_total{op=",
    "grinder_idempotency_hits_total{op=",
    "grinder_idempotency_conflicts_total{op=",
    "grinder_idempotency_misses_total{op=",
    "grinder_circuit_state{op=",
    "grinder_circuit_rejected_total{op=",
    "grinder_circuit_trips_total{op=",
    # Reconcile metrics (LC-09b/LC-10/LC-11/LC-15b)
    "# HELP grinder_reconcile_mismatch_total",
    "# TYPE grinder_reconcile_mismatch_total",
    "# HELP grinder_reconcile_last_snapshot_age_seconds",
    "# TYPE grinder_reconcile_last_snapshot_age_seconds",
    "grinder_reconcile_last_snapshot_age_seconds",
    "# HELP grinder_reconcile_last_snapshot_ts_ms",
    "# TYPE grinder_reconcile_last_snapshot_ts_ms",
    "grinder_reconcile_last_snapshot_ts_ms",
    "# HELP grinder_reconcile_runs_total",
    "# TYPE grinder_reconcile_runs_total",
    "grinder_reconcile_runs_total",
    "# HELP grinder_reconcile_action_planned_total",
    "# TYPE grinder_reconcile_action_planned_total",
    "# HELP grinder_reconcile_action_executed_total",
    "# TYPE grinder_reconcile_action_executed_total",
    "# HELP grinder_reconcile_action_blocked_total",
    "# TYPE grinder_reconcile_action_blocked_total",
    "# HELP grinder_reconcile_runs_with_mismatch_total",
    "# TYPE grinder_reconcile_runs_with_mismatch_total",
    "grinder_reconcile_runs_with_mismatch_total",
    "# HELP grinder_reconcile_runs_with_remediation_total",
    "# TYPE grinder_reconcile_runs_with_remediation_total",
    "# HELP grinder_reconcile_last_remediation_ts_ms",
    "# TYPE grinder_reconcile_last_remediation_ts_ms",
    "grinder_reconcile_last_remediation_ts_ms",
    # Reconcile series-level patterns (type/action labels)
    "grinder_reconcile_mismatch_total{type=",
    "grinder_reconcile_action_planned_total{action=",
    "grinder_reconcile_action_executed_total{action=",
    "grinder_reconcile_runs_with_remediation_total{action=",
    # LC-18: Budget metrics
    "# HELP grinder_reconcile_budget_calls_used_day",
    "# TYPE grinder_reconcile_budget_calls_used_day",
    "grinder_reconcile_budget_calls_used_day",
    "# HELP grinder_reconcile_budget_notional_used_day",
    "# TYPE grinder_reconcile_budget_notional_used_day",
    "grinder_reconcile_budget_notional_used_day",
    "# HELP grinder_reconcile_budget_calls_remaining_day",
    "# TYPE grinder_reconcile_budget_calls_remaining_day",
    "grinder_reconcile_budget_calls_remaining_day",
    "# HELP grinder_reconcile_budget_notional_remaining_day",
    "# TYPE grinder_reconcile_budget_notional_remaining_day",
    "grinder_reconcile_budget_notional_remaining_day",
    "# HELP grinder_reconcile_budget_configured",
    "# TYPE grinder_reconcile_budget_configured",
    "grinder_reconcile_budget_configured",
    # Launch-03: Data quality metrics
    "# HELP grinder_data_stale_total",
    "# TYPE grinder_data_stale_total",
    "grinder_data_stale_total{stream=",
    "# HELP grinder_data_gap_total",
    "# TYPE grinder_data_gap_total",
    "grinder_data_gap_total{stream=",
    "# HELP grinder_data_outlier_total",
    "# TYPE grinder_data_outlier_total",
    "grinder_data_outlier_total{stream=",
    # Launch-05: HTTP latency/retry metrics
    "# HELP grinder_http_requests_total",
    "# TYPE grinder_http_requests_total",
    "grinder_http_requests_total{op=",
    "# HELP grinder_http_retries_total",
    "# TYPE grinder_http_retries_total",
    "grinder_http_retries_total{op=",
    "# HELP grinder_http_fail_total",
    "# TYPE grinder_http_fail_total",
    "grinder_http_fail_total{op=",
    "# HELP grinder_http_latency_ms",
    "# TYPE grinder_http_latency_ms",
    # Launch-06: Fill tracking metrics
    "# HELP grinder_fills_total",
    "# TYPE grinder_fills_total",
    "grinder_fills_total{source=",
    "# HELP grinder_fill_notional_total",
    "# TYPE grinder_fill_notional_total",
    "grinder_fill_notional_total{source=",
    "# HELP grinder_fill_fees_total",
    "# TYPE grinder_fill_fees_total",
    "grinder_fill_fees_total{source=",
    # Launch-06 PR3: Fill health metrics
    "# HELP grinder_fill_ingest_polls_total",
    "# TYPE grinder_fill_ingest_polls_total",
    "grinder_fill_ingest_polls_total{source=",
    "# HELP grinder_fill_ingest_enabled",
    "# TYPE grinder_fill_ingest_enabled",
    "grinder_fill_ingest_enabled{source=",
    "# HELP grinder_fill_ingest_errors_total",
    "# TYPE grinder_fill_ingest_errors_total",
    "grinder_fill_ingest_errors_total{source=",
    "# HELP grinder_fill_cursor_load_total",
    "# TYPE grinder_fill_cursor_load_total",
    "grinder_fill_cursor_load_total{source=",
    "# HELP grinder_fill_cursor_save_total",
    "# TYPE grinder_fill_cursor_save_total",
    "grinder_fill_cursor_save_total{source=",
    # Launch-06 PR6: Cursor stuck detection
    "# HELP grinder_fill_cursor_last_save_ts",
    "# TYPE grinder_fill_cursor_last_save_ts",
    "grinder_fill_cursor_last_save_ts{source=",
    "# HELP grinder_fill_cursor_age_seconds",
    "# TYPE grinder_fill_cursor_age_seconds",
    "grinder_fill_cursor_age_seconds{source=",
    # Launch-13: FSM metrics
    "# HELP grinder_fsm_current_state",
    "# TYPE grinder_fsm_current_state",
    "grinder_fsm_current_state{state=",
    "# HELP grinder_fsm_state_duration_seconds",
    "# TYPE grinder_fsm_state_duration_seconds",
    "grinder_fsm_state_duration_seconds",
    "# HELP grinder_fsm_transitions_total",
    "# TYPE grinder_fsm_transitions_total",
    "# HELP grinder_fsm_action_blocked_total",
    "# TYPE grinder_fsm_action_blocked_total",
    # Launch-14: SOR metrics
    "# HELP grinder_router_decision_total",
    "# TYPE grinder_router_decision_total",
    "grinder_router_decision_total{decision=",
    "# HELP grinder_router_amend_savings_total",
    "# TYPE grinder_router_amend_savings_total",
    "grinder_router_amend_savings_total",
    # Launch-15: Account sync metrics
    "# HELP grinder_account_sync_last_ts",
    "# TYPE grinder_account_sync_last_ts",
    "grinder_account_sync_last_ts",
    "# HELP grinder_account_sync_last_wall_ts",
    "# TYPE grinder_account_sync_last_wall_ts",
    "grinder_account_sync_last_wall_ts",
    "# HELP grinder_account_sync_age_seconds",
    "# TYPE grinder_account_sync_age_seconds",
    "grinder_account_sync_age_seconds",
    "# HELP grinder_account_sync_data_age_seconds",
    "# TYPE grinder_account_sync_data_age_seconds",
    "grinder_account_sync_data_age_seconds",
    "# HELP grinder_account_sync_errors_total",
    "# TYPE grinder_account_sync_errors_total",
    "grinder_account_sync_errors_total{reason=",
    "# HELP grinder_account_sync_mismatches_total",
    "# TYPE grinder_account_sync_mismatches_total",
    "grinder_account_sync_mismatches_total{rule=",
    "# HELP grinder_account_sync_positions_count",
    "# TYPE grinder_account_sync_positions_count",
    "grinder_account_sync_positions_count",
    "# HELP grinder_account_sync_open_orders_count",
    "# TYPE grinder_account_sync_open_orders_count",
    "grinder_account_sync_open_orders_count",
    "# HELP grinder_account_sync_pending_notional",
    "# TYPE grinder_account_sync_pending_notional",
    "grinder_account_sync_pending_notional",
    # PR-C4a: Fill model shadow metrics (always emitted, default 0)
    "# HELP grinder_ml_fill_prob_bps_last",
    "# TYPE grinder_ml_fill_prob_bps_last",
    "grinder_ml_fill_prob_bps_last",
    "# HELP grinder_ml_fill_model_loaded",
    "# TYPE grinder_ml_fill_model_loaded",
    "grinder_ml_fill_model_loaded",
    # PR-C5: Fill probability gate metrics (always emitted, default 0)
    "# HELP grinder_router_fill_prob_blocks_total",
    "# TYPE grinder_router_fill_prob_blocks_total",
    "grinder_router_fill_prob_blocks_total",
    "# HELP grinder_router_fill_prob_enforce_enabled",
    "# TYPE grinder_router_fill_prob_enforce_enabled",
    "grinder_router_fill_prob_enforce_enabled",
    # PR-C8: Fill probability circuit breaker trips
    "# HELP grinder_router_fill_prob_cb_trips_total",
    "# TYPE grinder_router_fill_prob_cb_trips_total",
    "grinder_router_fill_prob_cb_trips_total",
    # PR-C9: Auto-threshold from eval report (gauge, 0 = disabled/failed)
    "# HELP grinder_router_fill_prob_auto_threshold_bps",
    "# TYPE grinder_router_fill_prob_auto_threshold_bps",
    "grinder_router_fill_prob_auto_threshold_bps",
    # PR-C2: Symbol allowlist for fill-prob enforcement (gauge, 0 = all symbols)
    "# HELP grinder_router_fill_prob_enforce_allowlist_enabled",
    "# TYPE grinder_router_fill_prob_enforce_allowlist_enabled",
    "grinder_router_fill_prob_enforce_allowlist_enabled",
    # PR-C4: Engine initialized gauge (0 = LiveEngineV0 not running, 1 = initialized)
    "# HELP grinder_live_engine_initialized",
    "# TYPE grinder_live_engine_initialized",
    "grinder_live_engine_initialized",
    # PR-FUT-1: Port order attempt metrics
    "# HELP grinder_port_order_attempts_total",
    "# TYPE grinder_port_order_attempts_total",
    "grinder_port_order_attempts_total{port=",
    # PR-FUT-2: Port HTTP request metrics
    "# HELP grinder_port_http_requests_total",
    "# TYPE grinder_port_http_requests_total",
    "grinder_port_http_requests_total{port=",
    # RISK-EE-1: Emergency exit enabled gauge (always emitted)
    "# HELP grinder_emergency_exit_enabled",
    "# TYPE grinder_emergency_exit_enabled",
    "grinder_emergency_exit_enabled",
    # P0-2: Cancel unknown (-2011) counter
    "# HELP grinder_port_cancel_unknown_total",
    "# TYPE grinder_port_cancel_unknown_total",
    "grinder_port_cancel_unknown_total{port=",
    # P0-2d: Cancel OK counter
    "# HELP grinder_port_cancel_ok_total",
    "# TYPE grinder_port_cancel_ok_total",
    "grinder_port_cancel_ok_total{port=",
    # P0-2b: Order lookup counter
    "# HELP grinder_port_order_lookup_total",
    "# TYPE grinder_port_order_lookup_total",
    "grinder_port_order_lookup_total{port=",
    # PR-1 (ADR-092): Risk base metrics
    "# HELP grinder_risk_base_usd",
    "# TYPE grinder_risk_base_usd",
    "grinder_risk_base_usd",
    "# HELP grinder_risk_base_stale_seconds",
    "# TYPE grinder_risk_base_stale_seconds",
    "grinder_risk_base_stale_seconds",
    "# HELP grinder_risk_base_status",
    "# TYPE grinder_risk_base_status",
    "grinder_risk_base_status",
    # PR-2 (ADR-092): Risk gate block counter
    "# HELP grinder_risk_gate_blocks_total",
    "# TYPE grinder_risk_gate_blocks_total",
    "grinder_risk_gate_blocks_total{reason=",
    # PR-B3b (ADR-126): Tuning metrics
    "# HELP grinder_tuning_result_total",
    "# TYPE grinder_tuning_result_total",
    "grinder_tuning_result_total{status=",
    "# HELP grinder_tuning_no_go_total",
    "# TYPE grinder_tuning_no_go_total",
    "# HELP grinder_tuning_cache_size",
    "# TYPE grinder_tuning_cache_size",
    "grinder_tuning_cache_size",
    "# HELP grinder_tuning_cache_expired_total",
    "# TYPE grinder_tuning_cache_expired_total",
    "grinder_tuning_cache_expired_total",
]

# PR6: Concrete series patterns requiring fill ingest to be running.
# Separated from REQUIRED_METRICS_PATTERNS because system-level tests
# validate against fresh/default state (no ingest running). Tested in
# test_fill_health_metrics.py::TestFillHealthMetricsContract.
FILL_INGEST_SERIES_PATTERNS = [
    'grinder_fill_cursor_last_save_ts{source="reconcile"}',
    'grinder_fill_cursor_age_seconds{source="reconcile"}',
]

# Forbidden high-cardinality labels (H5-02 contract tightening)
# These labels MUST NOT appear in /metrics output to prevent cardinality explosion.
# See ADR-028 for design decisions.
FORBIDDEN_METRIC_LABELS = [
    "symbol=",
    "order_id=",
    "key=",
    "client_id=",
    "idempotency_key=",
]
