"""Phase 1 + Phase 2 PNG benchmark on a user-supplied folder of real PNGs.

Phase 1: current StorageArch ZIP/DEFLATE baseline + the four generic
byte-exact strategies already validated on JPEGs (zlib/lzma/zstd/brotli).

Phase 2: the PNG-aware IDAT-recompression candidate (png_idat_recompress).

Phase 3: Smart Selector - per file, the smallest representation among every
strategy that actually passed the SHA256(original) == SHA256(restored)
hard gate. A strategy that fails or is skipped for a given file is simply
excluded from that file's selection, never counted as a "win".
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
from strategies.generic_byte import (
    GenericBrotliStrategy,
    GenericLzmaStrategy,
    GenericZlibStrategy,
    GenericZstdStrategy,
)
from strategies.png_idat_recompress import PngIdatRecompressStrategy

_HERE = os.path.dirname(__file__)
_IMAGES_ROOT = os.path.dirname(_HERE)
_OUTPUT_ROOT = os.path.join(_IMAGES_ROOT, "output")
_BENCHMARKS_ROOT = os.path.join(_IMAGES_ROOT, "benchmarks")

_BASELINE = BaselineZipStrategy()
_GENERIC_STRATEGIES = [
    GenericLzmaStrategy(),
    GenericZlibStrategy(),
    GenericZstdStrategy(),
    GenericBrotliStrategy(),
]
_PNG_AWARE = PngIdatRecompressStrategy()


def _discover_pngs(root):
    files = []
    for current_path, _dir_names, file_names in os.walk(root):
        for name in sorted(file_names):
            if os.path.splitext(name)[1].lower() == ".png":
                files.append(os.path.join(current_path, name))
    return sorted(files)


def _pct(reference, value):
    if reference is None or value is None or reference == 0:
        return None
    return round((reference - value) / reference * 100, 2)


def run(photos_root):
    run_tag = f"realworld_pngs_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    files = _discover_pngs(photos_root)
    rows = []

    for file_path in files:
        filename = os.path.basename(file_path)

        baseline_record = harness.run_strategy_on_file(_BASELINE, file_path, _OUTPUT_ROOT, run_tag)
        current_storagearch_size = (
            baseline_record["smart_size"] if baseline_record["status"] == "PASS" else None
        )

        generic_results = {}
        for strategy in _GENERIC_STRATEGIES:
            record = harness.run_strategy_on_file(strategy, file_path, _OUTPUT_ROOT, run_tag)
            generic_results[strategy.name] = record

        png_aware_record = harness.run_strategy_on_file(_PNG_AWARE, file_path, _OUTPUT_ROOT, run_tag)

        # candidates valid for Smart Selector: only PASS rows contribute a size
        candidates = {"baseline_storagearch_zip": current_storagearch_size}
        for name, record in generic_results.items():
            if record["status"] == "PASS":
                candidates[name] = record["smart_size"]
        png_aware_valid = png_aware_record["status"] == "PASS"
        if png_aware_valid:
            candidates["png_idat_recompress"] = png_aware_record["smart_size"]

        best_generic_name, best_generic_size = min(
            ((n, s) for n, s in candidates.items() if n != "png_idat_recompress" and n != "baseline_storagearch_zip"),
            key=lambda item: item[1],
            default=(None, None),
        )

        smart_name, smart_size = min(candidates.items(), key=lambda item: item[1])

        all_records = {"baseline_storagearch_zip": baseline_record, "png_idat_recompress": png_aware_record}
        all_records.update(generic_results)
        smart_record = all_records.get(smart_name)

        row = {
            "filename": filename,
            "original_size": baseline_record["original_size"],
            "current_storagearch_size": current_storagearch_size,
            "best_generic_method": best_generic_name,
            "best_generic_size": best_generic_size,
            "png_aware_status": png_aware_record["status"],
            "png_aware_size": png_aware_record["smart_size"] if png_aware_valid else None,
            "png_aware_notes": png_aware_record["notes"],
            "png_aware_compress_time_s": png_aware_record["compression_time_s"],
            "png_aware_restore_time_s": png_aware_record["restore_time_s"],
            "smart_selector_method": smart_name,
            "smart_selector_size": smart_size,
            "smart_selector_compress_time_s": smart_record["compression_time_s"] if smart_record else None,
            "smart_selector_restore_time_s": smart_record["restore_time_s"] if smart_record else None,
            "saving_vs_original_pct": _pct(baseline_record["original_size"], smart_size),
            "improvement_vs_current_pct": _pct(current_storagearch_size, smart_size),
            "any_hash_mismatch": any(
                r["status"] == "FAIL_HASH_MISMATCH"
                for r in [baseline_record, png_aware_record, *generic_results.values()]
            ),
        }
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


if __name__ == "__main__":
    photos_root = sys.argv[1]
    run_tag, rows = run(photos_root)
    csv_path, json_path = write_reports(run_tag, rows)
    print("CSV:", csv_path)
    print("JSON:", json_path)
    print(f"Processed {len(rows)} PNG files")
