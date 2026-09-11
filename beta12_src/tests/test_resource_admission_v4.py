from beta12_src.resource_admission_v4 import ActiveWorkerTracker, AdmissionGateV4

_GB = 1024 ** 3


def _classifier(weight_class, ram_estimate_bytes=0):
    def _fn(strategy_name, file_size):
        return weight_class, ram_estimate_bytes
    return _fn


def _healthy_kwargs(**overrides):
    """Deterministic, non-flaky defaults for every real-world signal this
    gate checks - every test overrides only the ONE signal it's testing."""
    defaults = dict(
        process_memory_mb_fn=lambda: 100.0,
        disk_usage_percent_fn=lambda: 10.0,
        ram_available_bytes_fn=lambda: 12 * _GB,
        ram_total_bytes_fn=lambda: 16 * _GB,
    )
    defaults.update(overrides)
    return defaults


def test_light_always_admitted_regardless_of_free_cores():
    gate = AdmissionGateV4(total_cores=4, active_worker_threads_fn=lambda: 100, weight_classifier=_classifier("LIGHT"),
                            **_healthy_kwargs())
    decision = gate.evaluate("generic_zstd", 1000)
    assert decision.admitted is True
    assert decision.reason == "LIGHT_ALWAYS_ADMITTED"


def test_heavy_admitted_when_enough_free_cores_and_ram():
    gate = AdmissionGateV4(total_cores=8, min_free_cores=2, active_worker_threads_fn=lambda: 4,
                            weight_classifier=_classifier("HEAVY", ram_estimate_bytes=500 * 1024 * 1024),
                            **_healthy_kwargs())
    decision = gate.evaluate("jpeg_xl_lossless_jpeg", 5_000_000)
    assert decision.admitted is True
    assert decision.reason == "ADMITTED_FREE_CORES_AVAILABLE"


def test_heavy_denied_when_insufficient_free_cores():
    gate = AdmissionGateV4(total_cores=8, min_free_cores=2, active_worker_threads_fn=lambda: 7,
                            weight_classifier=_classifier("HEAVY"), **_healthy_kwargs())
    decision = gate.evaluate("jpeg_xl_lossless_jpeg", 5_000_000)
    assert decision.admitted is False
    assert decision.reason == "INSUFFICIENT_FREE_CORES"


def test_high_total_cpu_from_many_threads_does_not_block_heavy_if_cores_free():
    # The actual Beta11 regression this round fixes: many threads busy
    # (high aggregate CPU%) must NOT deny heavy admission as long as real
    # free cores AND real RAM remain - this gate has no CPU% signal at all.
    gate = AdmissionGateV4(total_cores=16, min_free_cores=2, active_worker_threads_fn=lambda: 10,
                            weight_classifier=_classifier("HEAVY"), **_healthy_kwargs())
    decision = gate.evaluate("jpeg_xl_lossless_jpeg", 5_000_000)
    assert decision.admitted is True  # 16 - 10 = 6 free cores >= 2


def test_real_system_ram_denied_despite_free_cores_incident_regression():
    # INCIDENT regression test: this is the exact real-world scenario that
    # caused a ~6-hour stall on the real corpus - plenty of free cores
    # (idle machine), but ONE candidate's real, size-aware RAM estimate
    # (a large JXL encode on a 300-400MB real image) would push projected
    # system RAM usage over the watermark. Must be denied even though
    # free-cores alone would admit it.
    large_jxl_ram_estimate = 9 * _GB  # e.g. a real ~300MB image via resource_scheduler's own ~29x slope
    gate = AdmissionGateV4(
        total_cores=16, min_free_cores=2, active_worker_threads_fn=lambda: 0,
        weight_classifier=_classifier("HEAVY", ram_estimate_bytes=large_jxl_ram_estimate),
        process_memory_mb_fn=lambda: 100.0, disk_usage_percent_fn=lambda: 10.0,
        ram_available_bytes_fn=lambda: 10 * _GB, ram_total_bytes_fn=lambda: 16 * _GB,
    )
    decision = gate.evaluate("jpeg_xl_lossless_jpeg", 300_000_000)
    assert decision.admitted is False
    assert decision.reason == "PROJECTED_RAM_OVER_WATERMARK"


def test_ram_watermark_exact_boundary():
    # total=16GB, watermark=0.80 -> projected must stay < 12.8GB used.
    # available=8GB (8GB used already) + estimate=4GB -> projected used
    # = 12GB / 16GB = 0.75 -> still admitted.
    gate = AdmissionGateV4(
        total_cores=16, active_worker_threads_fn=lambda: 0,
        weight_classifier=_classifier("HEAVY", ram_estimate_bytes=4 * _GB),
        process_memory_mb_fn=lambda: 100.0, disk_usage_percent_fn=lambda: 10.0,
        ram_available_bytes_fn=lambda: 8 * _GB, ram_total_bytes_fn=lambda: 16 * _GB,
    )
    decision = gate.evaluate("jpeg_xl_lossless_jpeg", 100_000_000)
    assert decision.admitted is True

    # Same available RAM, but a bigger estimate pushes projected usage to
    # 14GB/16GB = 0.875 >= 0.80 -> denied.
    gate2 = AdmissionGateV4(
        total_cores=16, active_worker_threads_fn=lambda: 0,
        weight_classifier=_classifier("HEAVY", ram_estimate_bytes=6 * _GB),
        process_memory_mb_fn=lambda: 100.0, disk_usage_percent_fn=lambda: 10.0,
        ram_available_bytes_fn=lambda: 8 * _GB, ram_total_bytes_fn=lambda: 16 * _GB,
    )
    decision2 = gate2.evaluate("jpeg_xl_lossless_jpeg", 300_000_000)
    assert decision2.admitted is False
    assert decision2.reason == "PROJECTED_RAM_OVER_WATERMARK"


def test_light_candidate_bypasses_ram_check_entirely():
    gate = AdmissionGateV4(
        total_cores=16, active_worker_threads_fn=lambda: 0,
        weight_classifier=_classifier("LIGHT", ram_estimate_bytes=999 * _GB),  # absurd - must not matter for LIGHT
        ram_available_bytes_fn=lambda: 1 * _GB, ram_total_bytes_fn=lambda: 16 * _GB,
    )
    decision = gate.evaluate("generic_zstd", 100_000_000)
    assert decision.admitted is True
    assert decision.reason == "LIGHT_ALWAYS_ADMITTED"


def test_process_memory_safety_net_still_applies():
    gate = AdmissionGateV4(total_cores=16, active_worker_threads_fn=lambda: 0,
                            weight_classifier=_classifier("HEAVY"),
                            **_healthy_kwargs(max_process_memory_mb=3000.0, process_memory_mb_fn=lambda: 4000.0))
    decision = gate.evaluate("jpeg_xl_lossless_jpeg", 5_000_000)
    assert decision.admitted is False
    assert decision.reason == "PROCESS_MEMORY_OVER_THRESHOLD"


def test_disk_pressure_safety_net_still_applies():
    gate = AdmissionGateV4(total_cores=16, active_worker_threads_fn=lambda: 0,
                            weight_classifier=_classifier("HEAVY"),
                            **_healthy_kwargs(disk_usage_threshold_percent=90.0, disk_usage_percent_fn=lambda: 95.0, disk_pause_ms=1))
    decision = gate.evaluate("jpeg_xl_lossless_jpeg", 5_000_000)
    assert decision.admitted is False
    assert decision.reason == "DISK_PRESSURE_OVER_THRESHOLD"


def test_free_cores_computation():
    gate = AdmissionGateV4(total_cores=8, active_worker_threads_fn=lambda: 3)
    assert gate.free_cores() == 5
    assert gate.free_cores(active_worker_threads=6) == 2


def test_free_cores_floors_at_zero():
    gate = AdmissionGateV4(total_cores=4, active_worker_threads_fn=lambda: 999)
    assert gate.free_cores() == 0


def test_active_worker_tracker_context_manager():
    tracker = ActiveWorkerTracker()
    assert tracker.current == 0
    with tracker:
        assert tracker.current == 1
        with tracker:
            assert tracker.current == 2
        assert tracker.current == 1
    assert tracker.current == 0


def test_active_worker_tracker_thread_safe_under_real_concurrency():
    import threading
    import time

    tracker = ActiveWorkerTracker()
    max_seen = {"value": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        with tracker:
            time.sleep(0.05)  # hold the slot long enough for all 8 threads to overlap
            with lock:
                max_seen["value"] = max(max_seen["value"], tracker.current)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_seen["value"] == 8
    assert tracker.current == 0
