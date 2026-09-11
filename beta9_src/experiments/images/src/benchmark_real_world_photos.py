"""One-off benchmark runner for the user-supplied 'photos' folder.

Unlike benchmark.py (which walks test_data/<corpus>/<category>/ subfolders),
this targets an arbitrary flat/recursive folder of real JPEGs living outside
test_data/, without moving, renaming, or otherwise touching the originals.

Only runs the two strategies relevant to the JPEG XL feasibility question:
baseline_zip (current StorageArch method) and jpeg_xl_lossless (candidate).
Also computes the "Smart Selector" result: per file, whichever of the two
passing representations is smaller - this is what a real Smart Compression
Engine would actually choose, never JPEG XL unconditionally.
"""

import csv
import json
import os
import statistics
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

import harness
from strategies.baseline_zip import BaselineZipStrategy
from strategies.jpeg_xl_lossless import JpegXlLosslessStrategy

_HERE = os.path.dirname(__file__)
_IMAGES_ROOT = os.path.dirname(_HERE)
_OUTPUT_ROOT = os.path.join(_IMAGES_ROOT, "output")
_BENCHMARKS_ROOT = os.path.join(_IMAGES_ROOT, "benchmarks")

_JPEG_EXTENSIONS = {"jpg", "jpeg"}

_BASELINE = BaselineZipStrategy()
_JPEG_XL = JpegXlLosslessStrategy()


def _discover_jpegs(root):
    files = []
    for current_path, _dir_names, file_names in os.walk(root):
        for name in sorted(file_names):
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            if ext in _JPEG_EXTENSIONS:
                files.append(os.path.join(current_path, name))
    return sorted(files)


def _pct(reference, value):
    if reference is None or value is None or reference == 0:
        return None
    return round((reference - value) / reference * 100, 2)


def run(photos_root):
    run_tag = f"realworld_photos_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    available, reason = _JPEG_XL.is_available()
    if not available:
        raise RuntimeError(f"jpeg_xl_lossless_jpeg unavailable: {reason}")

    files = _discover_jpegs(photos_root)
    rows = []

    for file_path in files:
        filename = os.path.basename(file_path)

        baseline_record = harness.run_strategy_on_file(_BASELINE, file_path, _OUTPUT_ROOT, run_tag)
        jxl_record = harness.run_strategy_on_file(_JPEG_XL, file_path, _OUTPUT_ROOT, run_tag)

        current_storagearch_size = (
            baseline_record["smart_size"] if baseline_record["status"] == "PASS" else None
        )
        jxl_size = jxl_record["smart_size"] if jxl_record["status"] == "PASS" else None

        row = {
            "filename": filename,
            "original_size": baseline_record["original_size"],
            "original_sha256": baseline_record["original_sha256"],
            "current_storagearch_size": current_storagearch_size,
            "jxl_size": jxl_size,
            "jxl_status": jxl_record["status"],
            "jxl_restored_sha256": jxl_record["restored_sha256"],
            "jxl_exact_match": jxl_record["exact_match"],
            "jxl_compression_time_s": jxl_record["compression_time_s"],
            "jxl_restore_time_s": jxl_record["restore_time_s"],
            "jxl_notes": jxl_record["notes"],
            "saving_vs_original_pct": _pct(baseline_record["original_size"], jxl_size),
            "improvement_vs_current_pct": _pct(current_storagearch_size, jxl_size),
        }

        if jxl_size is not None and current_storagearch_size is not None:
            row["smart_selector_size"] = min(jxl_size, current_storagearch_size)
            row["smart_selector_choice"] = "jpeg_xl" if jxl_size < current_storagearch_size else "current_storagearch"
        else:
            row["smart_selector_size"] = current_storagearch_size
            row["smart_selector_choice"] = "current_storagearch (jxl unavailable/failed)"

        rows.append(row)

    return run_tag, rows


def write_reports(run_tag, rows):
    os.makedirs(_BENCHMARKS_ROOT, exist_ok=True)
    csv_path = os.path.join(_BENCHMARKS_ROOT, f"results_{run_tag}.csv")
    json_path = os.path.join(_BENCHMARKS_ROOT, f"results_{run_tag}.json")

    fields = list(rows[0].keys()) if rows else []
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    return csv_path, json_path


def summarize(rows):
    total = len(rows)
    passes = sum(1 for r in rows if r["jxl_exact_match"])
    total_original = sum(r["original_size"] for r in rows)
    total_current = sum(r["current_storagearch_size"] or 0 for r in rows)
    total_jxl = sum(r["jxl_size"] or 0 for r in rows if r["jxl_size"] is not None)
    total_smart = sum(r["smart_selector_size"] or 0 for r in rows)

    improvements = [r["improvement_vs_current_pct"] for r in rows if r["improvement_vs_current_pct"] is not None]
    savings = [r["saving_vs_original_pct"] for r in rows if r["saving_vs_original_pct"] is not None]

    jxl_smaller = sum(
        1 for r in rows if r["jxl_size"] is not None and r["current_storagearch_size"] is not None
        and r["jxl_size"] < r["current_storagearch_size"]
    )
    current_smaller = sum(
        1 for r in rows if r["jxl_size"] is not None and r["current_storagearch_size"] is not None
        and r["current_storagearch_size"] < r["jxl_size"]
    )

    best = max(rows, key=lambda r: r["improvement_vs_current_pct"] if r["improvement_vs_current_pct"] is not None else -1e9)
    worst = min(rows, key=lambda r: r["improvement_vs_current_pct"] if r["improvement_vs_current_pct"] is not None else 1e9)

    return {
        "total_files": total,
        "sha256_passes": passes,
        "total_original_size": total_original,
        "total_current_storagearch_size": total_current,
        "total_jxl_size": total_jxl,
        "total_smart_selector_size": total_smart,
        "avg_saving_vs_original_pct": round(sum(savings) / len(savings), 2) if savings else None,
        "avg_improvement_vs_current_pct": round(sum(improvements) / len(improvements), 2) if improvements else None,
        "median_improvement_vs_current_pct": round(statistics.median(improvements), 2) if improvements else None,
        "best_file": best["filename"],
        "best_improvement_pct": best["improvement_vs_current_pct"],
        "worst_file": worst["filename"],
        "worst_improvement_pct": worst["improvement_vs_current_pct"],
        "jxl_smaller_count": jxl_smaller,
        "current_smaller_count": current_smaller,
        "smart_selector_saving_vs_original_pct": _pct(total_original, total_smart),
        "smart_selector_improvement_vs_current_pct": _pct(total_current, total_smart),
    }


if __name__ == "__main__":
    photos_root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(_IMAGES_ROOT), "..", "photos")
    run_tag, rows = run(photos_root)
    csv_path, json_path = write_reports(run_tag, rows)
    summary = summarize(rows)

    print("CSV:", csv_path)
    print("JSON:", json_path)
    print(json.dumps(summary, indent=2))
