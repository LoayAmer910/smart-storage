from beta12_src.strategy_log_v2 import StrategyLogV2


def test_record_candidates_includes_heavy_allowed_and_free_cores():
    log = StrategyLogV2()
    log.record_candidates("/a.jpg", [("jpeg_xl_lossless_jpeg", "PASS", 500)], heavy_allowed=True, free_cores=6)
    records = log.get_records()
    assert records[0]["heavy_allowed"] is True
    assert records[0]["free_cores"] == 6


def test_record_decision_includes_heavy_allowed_and_free_cores():
    log = StrategyLogV2()
    log.record_decision("/a.jpg", 1000, "jpeg_xl_lossless_jpeg", 500, heavy_allowed=True, free_cores=6)
    records = log.get_records()
    assert records[0]["heavy_allowed"] is True
    assert records[0]["free_cores"] == 6
    assert records[0]["selected_strategy"] == "jpeg_xl_lossless_jpeg"


def test_candidates_then_decision_merge_and_v1_aggregation_still_works():
    log = StrategyLogV2()
    log.record_candidates("/a.jpg", [("jpeg_xl_lossless_jpeg", "PASS", 500), ("generic_zstd", "PASS", 700)],
                           heavy_allowed=True, free_cores=6)
    log.record_decision("/a.jpg", 1000, "jpeg_xl_lossless_jpeg", 500, heavy_allowed=True, free_cores=6)
    records = log.get_records()
    assert len(records) == 1
    assert records[0]["candidates"] == [("jpeg_xl_lossless_jpeg", "PASS", 500), ("generic_zstd", "PASS", 700)]
    # v1's own aggregation methods still work unchanged
    assert log.total_saved_bytes() == 500
    assert log.baseline_selected_count() == 0


def test_heavy_disallowed_case_recorded_correctly():
    log = StrategyLogV2()
    log.record_decision("/b.jpg", 1000, "generic_zstd", 800, heavy_allowed=False, free_cores=1)
    records = log.get_records()
    assert records[0]["heavy_allowed"] is False
    assert records[0]["free_cores"] == 1
