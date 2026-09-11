"""Resumes the Phase 2 E2E run from the first unfinished file, using the
already-fixed real-kill timeout in selector.py. Does NOT rerun any of the
2400 files already present in phase2_results.json - loads them, skips
them, and only processes the remaining tail. Logs progress per file (not
just every 200) so the current position is always observable.
"""

import json
import os
import sys
import time

# The test_file/ corpus includes Hebrew-named folders (e.g. "תכנות").
# Windows' console defaults to the cp1252 codec, which cannot encode
# those characters - the previous run crashed mid-print on exactly this.
# UTF-8 with replacement keeps progress logging alive for any filename.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(__file__)
sys.path.insert(0, _HERE)
import selector  # noqa: E402

_RESULTS_DIR = os.path.join(os.path.dirname(_HERE), "results")
_SCRATCH = os.path.join(os.path.dirname(_HERE), "scratch")
PHOTOS_ROOT = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\photos"


def main():
    with open(os.path.join(_RESULTS_DIR, "phase2_inventory.json"), encoding="utf-8") as f:
        inventory = json.load(f)
    with open(os.path.join(_RESULTS_DIR, "phase2_results.json"), encoding="utf-8") as f:
        existing = json.load(f)

    done_relpaths = {r["relpath"] for r in existing["files"]}
    print(f"already completed: {len(done_relpaths)}", flush=True)

    files = sorted(inventory["unique"], key=lambda r: r["size_bytes"])
    remaining = [r for r in files if r["relpath"] not in done_relpaths]
    print(f"remaining: {len(remaining)} of {len(files)} total", flush=True)

    results = list(existing["files"])
    run_tag = "phase2_e2e_resumed"
    out_path = os.path.join(_RESULTS_DIR, "phase2_results.json")
    t_start = time.perf_counter()

    for i, rec in enumerate(remaining):
        t_file0 = time.perf_counter()
        result = selector.select(rec["abs_path"], PHOTOS_ROOT, _SCRATCH, run_tag)
        t_file = time.perf_counter() - t_file0
        results.append(result)

        print(
            f"[{len(done_relpaths) + i + 1}/{len(files)}] {rec['relpath']} "
            f"({rec['size_bytes']} bytes) -> {result.get('selected_strategy')} in {t_file:.2f}s",
            flush=True,
        )

        # checkpoint every file on this tail - it's the slow/large part, worth the small I/O cost
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"run_tag": run_tag, "elapsed_s": existing["elapsed_s"] + (time.perf_counter() - t_start), "files": results}, f)

    total_elapsed = existing["elapsed_s"] + (time.perf_counter() - t_start)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"run_tag": run_tag, "elapsed_s": total_elapsed, "files": results}, f)
    print(f"COMPLETE: {len(results)} files total, {total_elapsed:.1f}s cumulative")


if __name__ == "__main__":
    main()
