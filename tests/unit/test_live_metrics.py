from __future__ import annotations

from grinder.live.live_metrics import (
    METRIC_GRID_V2_INTEGRITY_MISMATCH_PENDING,
    METRIC_GRID_V2_REJECTED_FILL_CLEANED,
    get_live_engine_metrics,
    reset_live_engine_metrics,
)


class TestLiveGridV2Metrics:
    def setup_method(self) -> None:
        reset_live_engine_metrics()

    def teardown_method(self) -> None:
        reset_live_engine_metrics()

    def test_records_integrity_mismatch_pending_counter(self) -> None:
        metrics = get_live_engine_metrics()
        metrics.record_grid_v2_integrity_mismatch_pending("PIPPINUSDT")
        metrics.record_grid_v2_integrity_mismatch_pending("PIPPINUSDT")

        lines = metrics.format_metrics()
        assert f'{METRIC_GRID_V2_INTEGRITY_MISMATCH_PENDING}{{sym="PIPPINUSDT"}} 2' in lines

    def test_records_rejected_fill_cleaned_by_source_and_reason(self) -> None:
        metrics = get_live_engine_metrics()
        metrics.record_grid_v2_rejected_fill_cleaned(
            "PIPPINUSDT",
            "user_data",
            "EXIT_LOT_NOT_FOUND",
        )

        lines = metrics.format_metrics()
        assert (
            f"{METRIC_GRID_V2_REJECTED_FILL_CLEANED}"
            '{sym="PIPPINUSDT",source="user_data",reason="EXIT_LOT_NOT_FOUND"} 1' in lines
        )
