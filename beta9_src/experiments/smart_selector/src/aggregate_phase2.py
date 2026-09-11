"""Aggregates phase2_results.json into per-type stats, the special
cost/benefit report, CSV, and the final markdown reports."""

import csv
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(__file__)
_PHASE2_ROOT = os.path.dirname(_HERE)
_RND_ROOT = os.path.dirname(os.path.dirname(_PHASE2_ROOT))
_RESULTS_DIR = os.path.join(_PHASE2_ROOT, "results")

sys.path.insert(0, _HERE)
import policy  # noqa: E402


def load():
    with open(os.path.join(_RESULTS_DIR, "phase2_results.json"), encoding="utf-8") as f:
        return json.load(f)


def per_type_stats(files):
    by_type = defaultdict(list)
    for r in files:
        if r.get("skipped"):
            continue
        by_type[r["detected_type"]].append(r)

    stats = {}
    for t, recs in by_type.items():
        total_original = sum(r["original_bytes"] for r in recs)
        total_current = sum(r["current_storagearch_bytes"] for r in recs if r["current_storagearch_bytes"])
        total_smart = sum(r["selected_bytes"] for r in recs if r["selected_bytes"])

        attempts = sum(len(r["candidates"]) for r in recs)
        passes = sum(1 for r in recs for c in r["candidates"] if c["sha256_pass"])

        win_counts = Counter(r["selected_strategy"] for r in recs)
        fallback_count = sum(1 for r in recs if r["used_fallback"])

        proc_times = []
        for r in recs:
            for c in r["candidates"]:
                total_t = (c["compression_time_s"] or 0) + (c["restore_time_s"] or 0)
                proc_times.append(total_t)

        stats[t] = {
            "files_tested": len(recs),
            "exact_passes": passes,
            "exact_attempts": attempts,
            "total_original_bytes": total_original,
            "total_current_storagearch_bytes": total_current,
            "total_smart_bytes": total_smart,
            "weighted_improvement_pct": round((total_current - total_smart) / total_current * 100, 3) if total_current else None,
            "actual_bytes_saved": (total_current - total_smart) if total_current else 0,
            "strategy_win_distribution": dict(win_counts),
            "fallback_count": fallback_count,
            "avg_processing_cost_s": round(sum(proc_times) / len(proc_times), 4) if proc_times else None,
            "median_processing_cost_s": round(statistics.median(proc_times), 4) if proc_times else None,
        }
    return stats


def special_cost_benefit_report(files):
    small_pct_meaningful_bytes = []
    rejected_for_cost = []
    intentional_baseline = []
    large_file_benefited = []

    for r in files:
        if r.get("skipped"):
            continue
        if r["used_fallback"]:
            intentional_baseline.append(r["relpath"])
        if r["original_bytes"] > 1_000_000 and r["selected_saved_percent"] and 0 < r["selected_saved_percent"] < 3.0 and r["selected_saved_bytes"] > 100_000:
            small_pct_meaningful_bytes.append({
                "relpath": r["relpath"], "original_bytes": r["original_bytes"],
                "saved_bytes": r["selected_saved_bytes"], "saved_percent": r["selected_saved_percent"],
            })
        if r["original_bytes"] > 1_000_000 and r["selected_saved_percent"] and r["selected_saved_percent"] >= 10.0:
            large_file_benefited.append({
                "relpath": r["relpath"], "original_bytes": r["original_bytes"],
                "saved_bytes": r["selected_saved_bytes"], "saved_percent": r["selected_saved_percent"],
            })
        for c in r["candidates"]:
            if c["rejection_reason"] == "COST_NOT_JUSTIFIED_BY_BENEFIT":
                rejected_for_cost.append({
                    "relpath": r["relpath"], "strategy": c["strategy"],
                    "stored_bytes": c["stored_bytes"], "saved_percent": c["saved_percent"],
                    "time_s": (c["compression_time_s"] or 0) + (c["restore_time_s"] or 0),
                })

    # Simulated large-PNG scenario (corpus has no real multi-GB PNG) -
    # proves the policy handles it correctly using policy.evaluate() directly,
    # not fabricated benchmark data.
    two_gb = 2 * 1024 * 1024 * 1024
    sim_candidate_size = two_gb - 20 * 1024 * 1024
    sim_decision = policy.evaluate(
        original_size=two_gb, baseline_size=two_gb, candidate_size=sim_candidate_size,
        compression_time_s=5.0, restore_time_s=2.5, sha256_match=True,
    )

    return {
        "small_percent_but_meaningful_bytes": small_pct_meaningful_bytes[:15],
        "rejected_for_cost_not_justified": rejected_for_cost[:15],
        "intentional_baseline_count": len(intentional_baseline),
        "large_files_that_benefited": large_file_benefited[:15],
        "simulated_large_png_scenario": {
            "description": "2 GiB PNG, 1% saving (~20 MiB absolute), zstd-class throughput (~7.7s total) - "
                            "no real file this large exists in the corpus, so tested directly via policy.evaluate() "
                            "with realistic simulated timing, not fabricated benchmark output.",
            "original_bytes": two_gb,
            "candidate_bytes": sim_candidate_size,
            "saved_bytes": two_gb - sim_candidate_size,
            "saved_percent": round((two_gb - sim_candidate_size) / two_gb * 100, 3),
            "decision": sim_decision.reason,
            "accepted": sim_decision.accepted,
        },
    }


def write_csv(files, path):
    rows = []
    for r in files:
        if r.get("skipped"):
            rows.append({
                "relpath": r["relpath"], "extension": r.get("extension"), "detected_type": r.get("detected_type"),
                "original_bytes": r.get("original_bytes"), "current_storagearch_bytes": None,
                "strategy": None, "stored_bytes": None, "saved_bytes": None, "saved_percent": None,
                "compression_time_s": None, "restore_time_s": None, "sha256_pass": None,
                "accepted": None, "rejection_reason": r.get("skip_reason"), "selected": "",
            })
            continue
        for c in r["candidates"]:
            rows.append({
                "relpath": r["relpath"], "extension": r["extension"], "detected_type": r["detected_type"],
                "original_bytes": r["original_bytes"], "current_storagearch_bytes": r["current_storagearch_bytes"],
                "strategy": c["strategy"], "stored_bytes": c["stored_bytes"], "saved_bytes": c["saved_bytes"],
                "saved_percent": c["saved_percent"], "compression_time_s": c["compression_time_s"],
                "restore_time_s": c["restore_time_s"], "sha256_pass": c["sha256_pass"],
                "accepted": c["accepted"], "rejection_reason": c["rejection_reason"],
                "selected": "YES" if c["strategy"] == r["selected_strategy"] else "",
            })
        if not r["candidates"]:
            rows.append({
                "relpath": r["relpath"], "extension": r["extension"], "detected_type": r["detected_type"],
                "original_bytes": r["original_bytes"], "current_storagearch_bytes": r["current_storagearch_bytes"],
                "strategy": "baseline_storagearch_zip", "stored_bytes": r["selected_bytes"], "saved_bytes": 0,
                "saved_percent": 0, "compression_time_s": None, "restore_time_s": None, "sha256_pass": True,
                "accepted": True, "rejection_reason": None, "selected": "YES",
            })

    fields = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    data = load()
    files = data["files"]
    skipped = [r for r in files if r.get("skipped")]
    tested = [r for r in files if not r.get("skipped")]

    stats = per_type_stats(files)
    special = special_cost_benefit_report(files)

    total_original = sum(r["original_bytes"] for r in tested)
    total_current = sum(r["current_storagearch_bytes"] for r in tested if r["current_storagearch_bytes"])
    total_smart = sum(r["selected_bytes"] for r in tested if r["selected_bytes"])

    hash_mismatches = [
        (r["relpath"], c["strategy"]) for r in tested for c in r["candidates"]
        if c["sha256_pass"] is False and c["rejection_reason"] == "SHA256_MISMATCH"
    ]
    any_selected_exceeds_baseline = [
        r["relpath"] for r in tested
        if r["selected_bytes"] is not None and r["current_storagearch_bytes"] is not None
        and r["selected_bytes"] > r["current_storagearch_bytes"]
    ]

    overview = {
        "run_elapsed_s": data["elapsed_s"],
        "total_files_scanned": len(files) + len(data.get("duplicates_skipped", [])),
        "files_tested": len(tested),
        "files_skipped": len(skipped),
        "skip_reasons": dict(Counter(r["skip_reason"] for r in skipped)),
        "total_original_bytes": total_original,
        "total_current_storagearch_bytes": total_current,
        "total_smart_bytes": total_smart,
        "total_bytes_saved_by_smart": total_current - total_smart,
        "overall_improvement_pct": round((total_current - total_smart) / total_current * 100, 3) if total_current else None,
        "hash_mismatches_anywhere": len(hash_mismatches),
        "any_selected_result_exceeds_baseline": len(any_selected_exceeds_baseline),
        "fallback_total_count": sum(1 for r in tested if r["used_fallback"]),
        "smart_selection_count": sum(1 for r in tested if not r["used_fallback"]),
    }

    with open(os.path.join(_RESULTS_DIR, "phase2_overview.json"), "w", encoding="utf-8") as f:
        json.dump({"overview": overview, "per_type": stats, "special": special}, f, indent=2, default=str)

    write_csv(files, os.path.join(_RESULTS_DIR, "phase2_per_file.csv"))

    print(json.dumps(overview, indent=2))
