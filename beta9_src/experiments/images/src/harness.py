"""Runs one strategy against one file: compress -> restore -> verify -> cleanup.

The mandatory hard gate lives here, not in any individual strategy:
SHA256(original) == SHA256(restored). No strategy can mark itself
successful; only this harness decides exact_match, and it decides it by
re-hashing bytes on disk, never by trusting a strategy's own return value.

The source file is only ever opened for reading. All strategy output goes
into a scratch subdirectory under output/, which is removed after a
successful run. On failure, the scratch directory is kept so the bad
artifact can be inspected, and the run is clearly marked as failed.
"""

import os
import shutil
import time
import traceback

import hashing


def run_strategy_on_file(strategy, input_path, output_root, run_tag):
    """Returns a result dict; never raises - all failures are captured in the dict."""
    filename = os.path.basename(input_path)
    extension = os.path.splitext(filename)[1].lstrip(".").lower()
    original_size = os.path.getsize(input_path)
    original_sha256 = hashing.sha256_file(input_path)

    base_record = {
        "filename": filename,
        "format": extension,
        "strategy_used": strategy.name,
        "original_size": original_size,
        "original_sha256": original_sha256,
        "smart_size": None,
        "compression_time_s": None,
        "restore_time_s": None,
        "restored_sha256": None,
        "exact_match": False,
        "status": None,
        "notes": "",
    }

    available, reason = strategy.is_available()
    if not available:
        base_record["status"] = "SKIPPED_UNAVAILABLE"
        base_record["notes"] = reason
        return base_record

    work_dir = os.path.join(output_root, run_tag, strategy.name, filename)
    os.makedirs(work_dir, exist_ok=True)

    try:
        t0 = time.perf_counter()
        compress_result = strategy.compress(input_path, work_dir)
        compress_time = time.perf_counter() - t0

        t1 = time.perf_counter()
        restore_result = strategy.restore(compress_result.output_path, work_dir, filename)
        restore_time = time.perf_counter() - t1

        restored_sha256 = hashing.sha256_file(restore_result.output_path)
        exact_match = restored_sha256 == original_sha256

        base_record.update({
            "smart_size": compress_result.stored_size,
            "compression_time_s": round(compress_time, 4),
            "restore_time_s": round(restore_time, 4),
            "restored_sha256": restored_sha256,
            "exact_match": exact_match,
            "status": "PASS" if exact_match else "FAIL_HASH_MISMATCH",
        })

        if exact_match:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            base_record["notes"] = f"restored file kept for inspection at {work_dir}"

    except Exception as exc:  # noqa: BLE001 - a strategy failing must never crash the benchmark
        base_record["status"] = "ERROR"
        base_record["notes"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"

    return base_record
