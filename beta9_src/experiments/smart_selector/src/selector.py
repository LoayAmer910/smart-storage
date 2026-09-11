"""The Smart Selector orchestrator.

FILE -> detect type -> route candidates -> run baseline (always) -> run
routed candidates (each under a real wall-clock timeout) -> verify exact
restoration -> apply benefit/cost policy -> pick the smallest ACCEPTED
candidate, or fall back to baseline_storagearch_zip.

No manual strategy choice ever happens - this module is the whole
decision path. baseline_storagearch_zip is never skipped: it is both the
comparison reference and the guaranteed-safe fallback.
"""

import multiprocessing
import os
import queue as queue_module
import sys
import time

_HERE = os.path.dirname(__file__)
_SMART_SELECTOR_ROOT = os.path.dirname(_HERE)
_RND_ROOT = os.path.dirname(os.path.dirname(_SMART_SELECTOR_ROOT))
_IMAGES_SRC = os.path.join(_RND_ROOT, "experiments", "images", "src")
sys.path.insert(0, _IMAGES_SRC)
sys.path.insert(0, _HERE)

import harness  # noqa: E402
from strategies.baseline_zip import BaselineZipStrategy  # noqa: E402
from strategies.generic_byte import (  # noqa: E402
    GenericBrotliStrategy, GenericLzmaStrategy, GenericZlibStrategy,
)
from strategies.jpeg_xl_lossless import JpegXlLosslessStrategy  # noqa: E402
from strategies.preflate_ooxml import PreflateZstdOoxmlStrategy  # noqa: E402
from strategies.bmp_jxl_lossless import BmpJxlLosslessStrategy  # noqa: E402
from strategies.video_optimizer_strategy import VideoOptimizerStrategy  # noqa: E402
from strategies.text_prefilter_strategy import (  # noqa: E402
    JsonPrefilterStrategy, CsvPrefilterStrategy, XmlPrefilterStrategy,
)

import file_analysis  # noqa: E402
import policy  # noqa: E402
import strategy_registry  # noqa: E402
from adaptive_zstd import AdaptiveZstdStrategy  # noqa: E402

_BASELINE = BaselineZipStrategy()
_STRATEGY_INSTANCES = {
    "baseline_storagearch_zip": _BASELINE,
    "generic_zstd": AdaptiveZstdStrategy(),  # size-adaptive level - see adaptive_zstd.py
    "generic_brotli": GenericBrotliStrategy(),
    "generic_lzma": GenericLzmaStrategy(),
    "generic_zlib": GenericZlibStrategy(),
    "jpeg_xl_lossless_jpeg": JpegXlLosslessStrategy(),
    # Beta 9 candidates - see beta9_research/logs/STAGE_GATE_RESULTS.md for
    # the real-dataset evidence behind each one (sections A and D)
    "preflate_zstd_ooxml": PreflateZstdOoxmlStrategy(),
    "bmp_jxl_lossless": BmpJxlLosslessStrategy(),
    # Beta 13 addition - lossless-only (metadata strip + faststart remux +
    # xdelta3-reversible bundle, no re-encoding). See video_optimizer.py
    # and video_optimizer_strategy.py for the full design/trade-off notes.
    "video_optimizer": VideoOptimizerStrategy(),
    # Reversible text pre-filters (canonicalize + zstd + xdelta3 patch) -
    # see text_prefilter.py and text_prefilter_strategy.py.
    "json_prefilter": JsonPrefilterStrategy(),
    "csv_prefilter": CsvPrefilterStrategy(),
    "xml_prefilter": XmlPrefilterStrategy(),
}

_MP_CONTEXT = multiprocessing.get_context("spawn")


def _subprocess_worker_entry(strategy, input_path, output_root, run_tag, result_queue):
    """Runs in a separate OS process (spawned fresh) so it can be genuinely
    killed on timeout - a ThreadPoolExecutor cannot do this, since Python
    has no API to force-terminate a thread; a real OS process can be."""
    try:
        result = harness.run_strategy_on_file(strategy, input_path, output_root, run_tag)
        result_queue.put(result)
    except Exception as exc:  # noqa: BLE001
        result_queue.put({
            "filename": os.path.basename(input_path), "strategy_used": strategy.name,
            "status": "ERROR", "smart_size": None, "compression_time_s": None,
            "restore_time_s": None, "exact_match": False, "notes": f"subprocess exception: {exc}",
        })


def _run_with_timeout(strategy, input_path, output_root, run_tag, timeout_s):
    """Runs harness.run_strategy_on_file in a child process under a real
    wall-clock timeout. On timeout, the child process is actually
    terminated (terminate(), then kill() as a fallback) - not just
    abandoned - so no orphaned work keeps consuming CPU in the background.
    """
    result_queue = _MP_CONTEXT.Queue()
    process = _MP_CONTEXT.Process(
        target=_subprocess_worker_entry,
        args=(strategy, input_path, output_root, run_tag, result_queue),
    )
    process.start()
    process.join(timeout_s)

    if process.is_alive():
        process.terminate()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join(2)
        result_queue.close()
        return {
            "filename": os.path.basename(input_path), "strategy_used": strategy.name,
            "status": "TIMEOUT", "smart_size": None, "compression_time_s": timeout_s,
            "restore_time_s": None, "exact_match": False,
            "notes": f"exceeded {timeout_s}s wall-clock timeout - child process terminated",
        }

    try:
        result = result_queue.get_nowait()
    except queue_module.Empty:
        result = {
            "filename": os.path.basename(input_path), "strategy_used": strategy.name,
            "status": "ERROR", "smart_size": None, "compression_time_s": None,
            "restore_time_s": None, "exact_match": False,
            "notes": "child process exited without producing a result",
        }
    result_queue.close()
    process.join(1)
    return result


def select(input_path, photos_root, output_root, run_tag, config=policy.DEFAULT_POLICY):
    """Runs the full Smart Selector pipeline for one file. Returns a result dict."""
    analysis = file_analysis.analyze_file(input_path, photos_root)
    if not analysis["valid"]:
        return {
            "relpath": analysis["relpath"], "extension": analysis["extension"],
            "detected_type": analysis["detected_type"], "original_bytes": analysis["size_bytes"],
            "skipped": True, "skip_reason": analysis["invalid_reason"],
            "current_storagearch_bytes": None, "candidates": [], "selected_strategy": None,
        }

    baseline_result = _run_with_timeout(_BASELINE, input_path, output_root, run_tag, config.absolute_timeout_seconds)
    baseline_size = baseline_result["smart_size"] if baseline_result["status"] == "PASS" else None

    candidate_names = []
    if baseline_size is not None and policy.should_attempt_candidates(analysis["size_bytes"], config):
        candidate_names = strategy_registry.get_candidate_strategies(
            analysis["detected_type"], analysis["extension"], analysis["size_bytes"]
        )

    candidate_reports = []
    accepted = []
    for name in candidate_names:
        strat = _STRATEGY_INSTANCES[name]
        available, unavailable_reason = strat.is_available()
        if not available:
            candidate_reports.append({
                "strategy": name, "stored_bytes": None, "saved_bytes": None, "saved_percent": None,
                "compression_time_s": None, "restore_time_s": None, "sha256_pass": False,
                "accepted": False, "rejection_reason": f"UNAVAILABLE: {unavailable_reason}",
            })
            continue

        result = _run_with_timeout(strat, input_path, output_root, run_tag, config.absolute_timeout_seconds)

        if result["status"] != "PASS" or baseline_size is None:
            reason = {
                "TIMEOUT": "TIMEOUT",
                "ERROR": "EXCEPTION",
                "FAIL_HASH_MISMATCH": "SHA256_MISMATCH",
                "SKIPPED_UNAVAILABLE": "UNAVAILABLE",
            }.get(result["status"], result["status"])
            candidate_reports.append({
                "strategy": name, "stored_bytes": result.get("smart_size"), "saved_bytes": None,
                "saved_percent": None, "compression_time_s": result.get("compression_time_s"),
                "restore_time_s": result.get("restore_time_s"), "sha256_pass": bool(result.get("exact_match")),
                "accepted": False, "rejection_reason": reason,
            })
            continue

        decision = policy.evaluate(
            original_size=analysis["size_bytes"], baseline_size=baseline_size,
            candidate_size=result["smart_size"], compression_time_s=result["compression_time_s"],
            restore_time_s=result["restore_time_s"], sha256_match=result["exact_match"], config=config,
        )
        candidate_reports.append({
            "strategy": name, "stored_bytes": result["smart_size"], "saved_bytes": decision.saved_bytes,
            "saved_percent": round(decision.saved_percent, 3), "compression_time_s": result["compression_time_s"],
            "restore_time_s": result["restore_time_s"], "sha256_pass": result["exact_match"],
            "accepted": decision.accepted, "rejection_reason": None if decision.accepted else decision.reason,
        })
        if decision.accepted:
            accepted.append((name, result["smart_size"]))

    if accepted:
        selected_name, selected_size = min(accepted, key=lambda t: t[1])
        used_fallback = False
    else:
        selected_name, selected_size = "baseline_storagearch_zip", baseline_size
        used_fallback = True

    return {
        "relpath": analysis["relpath"],
        "extension": analysis["extension"],
        "detected_type": analysis["detected_type"],
        "extension_mismatch": analysis["extension_mismatch"],
        "original_bytes": analysis["size_bytes"],
        "original_sha256": analysis["sha256"],
        "current_storagearch_bytes": baseline_size,
        "baseline_status": baseline_result["status"],
        "skipped": False,
        "candidates": candidate_reports,
        "selected_strategy": selected_name,
        "selected_bytes": selected_size,
        "selected_saved_bytes": (baseline_size - selected_size) if (baseline_size and selected_size) else 0,
        "selected_saved_percent": round((baseline_size - selected_size) / baseline_size * 100, 3) if baseline_size else 0.0,
        "used_fallback": used_fallback,
    }
