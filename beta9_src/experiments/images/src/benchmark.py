"""Orchestrates the image R&D benchmark: walks a corpus, runs every
applicable strategy against every file, and writes CSV/JSON/Markdown
reports into benchmarks/.

The current-StorageArch ZIP/DEFLATE baseline is always run first for each
file, because every other strategy's "saving_vs_current_method" is
measured against it, not against the original file size alone.
"""

import csv
import json
import os
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
from strategies.jpeg_xl_lossless import JpegXlLosslessStrategy

_HERE = os.path.dirname(__file__)
_IMAGES_ROOT = os.path.dirname(_HERE)
_OUTPUT_ROOT = os.path.join(_IMAGES_ROOT, "output")
_BENCHMARKS_ROOT = os.path.join(_IMAGES_ROOT, "benchmarks")

_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png"}

_BASELINE = BaselineZipStrategy()
_CANDIDATE_STRATEGIES = [
    GenericLzmaStrategy(),
    GenericZlibStrategy(),
    GenericZstdStrategy(),
    GenericBrotliStrategy(),
    JpegXlLosslessStrategy(),
]


def _discover_files(corpus_root):
    files = []
    if not os.path.isdir(corpus_root):
        return files
    for category in sorted(os.listdir(corpus_root)):
        category_path = os.path.join(corpus_root, category)
        if not os.path.isdir(category_path):
            continue
        for filename in sorted(os.listdir(category_path)):
            extension = os.path.splitext(filename)[1].lstrip(".").lower()
            if extension in _IMAGE_EXTENSIONS:
                files.append((category, os.path.join(category_path, filename)))
    return files


def _pct_saving(reference_size, smart_size):
    if reference_size is None or smart_size is None or reference_size == 0:
        return None
    return round((reference_size - smart_size) / reference_size * 100, 2)


def run_benchmark(corpus_name, corpus_root):
    run_tag = f"{corpus_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    files = _discover_files(corpus_root)
    rows = []

    for category, file_path in files:
        baseline_record = harness.run_strategy_on_file(_BASELINE, file_path, _OUTPUT_ROOT, run_tag)
        current_storagearch_size = (
            baseline_record["smart_size"] if baseline_record["status"] == "PASS" else None
        )

        def finish(record):
            record["category"] = category
            record["current_storagearch_size"] = current_storagearch_size
            record["saving_vs_original_pct"] = _pct_saving(record["original_size"], record["smart_size"])
            record["saving_vs_current_method_pct"] = _pct_saving(
                current_storagearch_size, record["smart_size"]
            )
            return record

        rows.append(finish(baseline_record))

        extension = os.path.splitext(file_path)[1].lstrip(".").lower()
        for strategy in _CANDIDATE_STRATEGIES:
            if not strategy.applies_to(extension):
                continue
            record = harness.run_strategy_on_file(strategy, file_path, _OUTPUT_ROOT, run_tag)
            rows.append(finish(record))

    return run_tag, rows


_CSV_FIELDS = [
    "filename", "category", "format", "strategy_used", "status", "exact_match",
    "original_size", "current_storagearch_size", "smart_size",
    "saving_vs_original_pct", "saving_vs_current_method_pct",
    "compression_time_s", "restore_time_s",
    "original_sha256", "restored_sha256", "notes",
]


def write_reports(run_tag, rows):
    os.makedirs(_BENCHMARKS_ROOT, exist_ok=True)
    csv_path = os.path.join(_BENCHMARKS_ROOT, f"results_{run_tag}.csv")
    json_path = os.path.join(_BENCHMARKS_ROOT, f"results_{run_tag}.json")
    md_path = os.path.join(_BENCHMARKS_ROOT, f"summary_{run_tag}.md")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in _CSV_FIELDS})

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    _write_markdown_summary(md_path, run_tag, rows)
    return csv_path, json_path, md_path


def _write_markdown_summary(md_path, run_tag, rows):
    lines = [f"# Image R&D Benchmark — {run_tag}", ""]

    total = len(rows)
    passed = sum(1 for r in rows if r["status"] == "PASS")
    failed = sum(1 for r in rows if r["status"] == "FAIL_HASH_MISMATCH")
    errored = sum(1 for r in rows if r["status"] == "ERROR")
    skipped = sum(1 for r in rows if r["status"] == "SKIPPED_UNAVAILABLE")
    lines += [
        f"- Total runs: {total}",
        f"- PASS (exact SHA-256 match): {passed}",
        f"- FAIL (hash mismatch): {failed}",
        f"- ERROR: {errored}",
        f"- SKIPPED (strategy unavailable): {skipped}",
        "",
        "## Per-strategy aggregate (PASS rows only)",
        "",
        "| strategy | files passed | avg saving vs original | avg saving vs current StorageArch |",
        "|---|---|---|---|",
    ]

    by_strategy = {}
    for row in rows:
        if row["status"] != "PASS":
            continue
        by_strategy.setdefault(row["strategy_used"], []).append(row)

    for strategy_name, strategy_rows in sorted(by_strategy.items()):
        orig_savings = [r["saving_vs_original_pct"] for r in strategy_rows if r["saving_vs_original_pct"] is not None]
        cur_savings = [r["saving_vs_current_method_pct"] for r in strategy_rows if r["saving_vs_current_method_pct"] is not None]
        avg_orig = round(sum(orig_savings) / len(orig_savings), 2) if orig_savings else "n/a"
        avg_cur = round(sum(cur_savings) / len(cur_savings), 2) if cur_savings else "n/a"
        lines.append(f"| {strategy_name} | {len(strategy_rows)} | {avg_orig}% | {avg_cur}% |")

    lines += ["", "## Full results", "", "| filename | strategy | status | original | current_storagearch | smart | vs_original | vs_current | exact_match |", "|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['filename']} | {row['strategy_used']} | {row['status']} | "
            f"{row['original_size']} | {row.get('current_storagearch_size')} | {row.get('smart_size')} | "
            f"{row.get('saving_vs_original_pct')} | {row.get('saving_vs_current_method_pct')} | {row['exact_match']} |"
        )

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _prune_empty_dirs(root):
    for current_path, dir_names, _file_names in os.walk(root, topdown=False):
        for dir_name in dir_names:
            candidate = os.path.join(current_path, dir_name)
            if not os.listdir(candidate):
                os.rmdir(candidate)


def main(corpus_name="synthetic"):
    corpus_root = os.path.join(_IMAGES_ROOT, "test_data", corpus_name)
    run_tag, rows = run_benchmark(corpus_name, corpus_root)
    csv_path, json_path, md_path = write_reports(run_tag, rows)
    _prune_empty_dirs(_OUTPUT_ROOT)
    print(f"Benchmark complete: {len(rows)} strategy runs across corpus '{corpus_name}'")
    print("CSV:", csv_path)
    print("JSON:", json_path)
    print("Markdown:", md_path)


if __name__ == "__main__":
    corpus_arg = sys.argv[1] if len(sys.argv) > 1 else "synthetic"
    main(corpus_arg)
