"""Regression test for a Phase 4 QA finding: smart_compression._run_candidate
and restore_original_bytes used to call process.join(timeout) BEFORE
draining the result multiprocessing.Queue. _storage_worker/_restore_worker
put the full compressed/restored file bytes through that queue - once a
payload exceeds a single pipe buffer (~128KB on Windows), the writer blocks
on a full pipe while nobody is reading (the parent was stuck in join()
instead), a classic multiprocessing Queue+join() ordering deadlock.

Small fixtures never exceeded the pipe buffer and never triggered it, which
is exactly why the existing Phase 3 suite (using a 26KB JPEG fixture) kept
passing despite the bug being present - any file whose smart-compressed or
restored representation exceeds ~128KB would hang for the full timeout on
archive, or raise a false SmartRestoreError on checkout. This test uses
payloads verified to land well above that threshold.
"""

import os
import time

import smart_compression


def test_run_candidate_does_not_deadlock_on_large_payload(tmp_path):
    # near-incompressible so the zstd OUTPUT itself stays well above the
    # ~128KB pipe-buffer threshold that triggers the deadlock
    src = tmp_path / "large_incompressible.bin"
    src.write_bytes(os.urandom(2 * 1024 * 1024))  # 2 MiB

    strategy = smart_compression._STRATEGY_INSTANCES["generic_zstd"]
    t0 = time.perf_counter()
    result = smart_compression._run_candidate(strategy, str(src), timeout_s=15.0)
    elapsed = time.perf_counter() - t0

    assert result["status"] == "PASS", result
    assert result["stored_size"] > 131072, "fixture must exceed the pipe-buffer threshold to be a meaningful test"
    assert elapsed < 10.0, f"took {elapsed:.1f}s - looks like the join()-before-drain deadlock regressed"


def test_restore_original_bytes_does_not_deadlock_on_large_payload(tmp_path):
    src = tmp_path / "large_incompressible2.bin"
    data = os.urandom(2 * 1024 * 1024)
    src.write_bytes(data)

    strategy_name = "generic_zstd"
    strategy = smart_compression._STRATEGY_INSTANCES[strategy_name]
    candidate = smart_compression._run_candidate(strategy, str(src), timeout_s=15.0)
    assert candidate["status"] == "PASS", candidate
    assert candidate["stored_size"] > 131072

    t0 = time.perf_counter()
    restored = smart_compression.restore_original_bytes(
        strategy_name, candidate["stored_bytes"], "large_incompressible2.bin",
        expected_original_sha256=candidate["original_sha256"], timeout_s=15.0,
    )
    elapsed = time.perf_counter() - t0

    assert restored == data
    assert elapsed < 10.0, f"took {elapsed:.1f}s - looks like the join()-before-drain deadlock regressed"
