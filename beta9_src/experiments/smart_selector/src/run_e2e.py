"""Phase 2 mixed-corpus E2E runner. Runs the Smart Selector against every
unique valid file already discovered by inventory_mixed.py, in size order
(smallest first) so partial results are meaningful if interrupted, and
writes incremental progress so a crash doesn't lose completed work.
"""

import json
import os
import sys
import time

_HERE = os.path.dirname(__file__)
sys.path.insert(0, _HERE)
import selector  # noqa: E402

_RESULTS_DIR = os.path.join(os.path.dirname(_HERE), "results")
_SCRATCH = os.path.join(os.path.dirname(_HERE), "scratch")
PHOTOS_ROOT = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\photos"


def main():
    with open(os.path.join(_RESULTS_DIR, "phase2_inventory.json"), encoding="utf-8") as f:
        inventory = json.load(f)

    files = sorted(inventory["unique"], key=lambda r: r["size_bytes"])
    run_tag = "phase2_e2e"

    out_path = os.path.join(_RESULTS_DIR, "phase2_results.json")
    results = []
    t_start = time.perf_counter()

    for i, rec in enumerate(files):
        result = selector.select(rec["abs_path"], PHOTOS_ROOT, _SCRATCH, run_tag)
        results.append(result)
        if (i + 1) % 200 == 0 or (i + 1) == len(files):
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({"run_tag": run_tag, "elapsed_s": time.perf_counter() - t_start, "files": results}, f)
            print(f"{i+1}/{len(files)} done, elapsed={time.perf_counter()-t_start:.1f}s", flush=True)

    elapsed = time.perf_counter() - t_start
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"run_tag": run_tag, "elapsed_s": elapsed, "files": results}, f)
    print(f"COMPLETE: {len(results)} files in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
