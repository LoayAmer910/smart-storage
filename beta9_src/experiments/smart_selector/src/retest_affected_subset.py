"""Re-tests ONLY the affected subset (all .ipch files + every file that
previously hit a real-kill TIMEOUT) through the fixed selector. Writes to
a separate results file - never touches phase2_results.json (the master
2562-file dataset stays untouched, as instructed).
"""

import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(__file__)
sys.path.insert(0, _HERE)
import selector  # noqa: E402

_RESULTS_DIR = os.path.join(os.path.dirname(_HERE), "results")
_SCRATCH = os.path.join(os.path.dirname(_HERE), "scratch")
PHOTOS_ROOT = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\photos"


def main():
    with open(os.path.join(_RESULTS_DIR, "affected_subset.json"), encoding="utf-8") as f:
        affected_relpaths = json.load(f)
    with open(os.path.join(_RESULTS_DIR, "phase2_inventory.json"), encoding="utf-8") as f:
        inventory = json.load(f)
    with open(os.path.join(_RESULTS_DIR, "phase2_results.json"), encoding="utf-8") as f:
        before_data = json.load(f)

    by_relpath = {r["relpath"]: r for r in inventory["unique"]}
    before_by_relpath = {r["relpath"]: r for r in before_data["files"]}

    results = []
    t_total0 = time.perf_counter()
    for i, relpath in enumerate(affected_relpaths):
        rec = by_relpath[relpath]
        t0 = time.perf_counter()
        result = selector.select(rec["abs_path"], PHOTOS_ROOT, _SCRATCH, "affected_retest")
        elapsed = time.perf_counter() - t0
        result["retest_elapsed_s"] = elapsed
        results.append(result)
        print(f"[{i+1}/{len(affected_relpaths)}] {relpath} ({rec['size_bytes']} bytes) "
              f"-> {result['selected_strategy']} in {elapsed:.2f}s", flush=True)

    total_elapsed = time.perf_counter() - t_total0

    out_path = os.path.join(_RESULTS_DIR, "phase2_affected_subset_retest.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"total_elapsed_s": total_elapsed, "files": results}, f, indent=2, ensure_ascii=False)

    # before/after comparison
    before_total_time = 0.0
    after_total_time = total_elapsed
    before_total_selected = 0
    after_total_selected = 0
    before_timeouts = 0
    after_timeouts = 0
    hash_mismatches_after = 0

    for r in results:
        relpath = r["relpath"]
        before = before_by_relpath.get(relpath)
        if before:
            before_total_time += sum((c["compression_time_s"] or 0) + (c["restore_time_s"] or 0) for c in before["candidates"])
            before_total_selected += before["selected_bytes"] or 0
            before_timeouts += sum(1 for c in before["candidates"] if c["rejection_reason"] == "TIMEOUT")
        after_total_selected += r["selected_bytes"] or 0
        after_timeouts += sum(1 for c in r["candidates"] if c["rejection_reason"] == "TIMEOUT")
        for c in r["candidates"]:
            if c["sha256_pass"] is False and c["rejection_reason"] == "SHA256_MISMATCH":
                hash_mismatches_after += 1

    summary = {
        "affected_file_count": len(affected_relpaths),
        "before_total_processing_time_s": round(before_total_time, 2),
        "after_total_processing_time_s": round(after_total_time, 2),
        "before_total_selected_bytes": before_total_selected,
        "after_total_selected_bytes": after_total_selected,
        "before_timeout_count": before_timeouts,
        "after_timeout_count": after_timeouts,
        "hash_mismatches_after": hash_mismatches_after,
    }
    with open(os.path.join(_RESULTS_DIR, "phase2_affected_subset_comparison.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\n" + json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
