"""Full mixed-corpus inventory: recursively scans photos/ (images +
test_file/ + everything else), detects real type, SHA-256 dedupes.
Read-only over photos/ - never writes there.
"""

import json
import os
import sys

_HERE = os.path.dirname(__file__)
sys.path.insert(0, _HERE)
import file_analysis  # noqa: E402

PHOTOS_ROOT = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\photos"
_RESULTS_DIR = os.path.join(os.path.dirname(_HERE), "results")


def scan():
    all_paths = []
    for current, _dirs, names in os.walk(PHOTOS_ROOT):
        for name in names:
            all_paths.append(os.path.join(current, name))

    records = []
    seen_hashes = {}
    invalid = []

    for path in all_paths:
        rec = file_analysis.analyze_file(path, PHOTOS_ROOT)
        if not rec["valid"]:
            invalid.append(rec)
            continue
        rec["abs_path"] = path
        rec["is_duplicate_of"] = seen_hashes.get(rec["sha256"])
        if rec["is_duplicate_of"] is None:
            seen_hashes[rec["sha256"]] = rec["relpath"]
        records.append(rec)

    unique = [r for r in records if r["is_duplicate_of"] is None]
    duplicates = [r for r in records if r["is_duplicate_of"] is not None]

    summary = {
        "total_files_discovered": len(all_paths),
        "invalid_files": len(invalid),
        "valid_records": len(records),
        "unique_files": len(unique),
        "duplicate_files": len(duplicates),
        "unique_total_bytes": sum(r["size_bytes"] for r in unique),
        "duplicate_bytes_avoided": sum(r["size_bytes"] for r in duplicates),
    }
    return {"summary": summary, "unique": unique, "duplicates": duplicates, "invalid": invalid}


if __name__ == "__main__":
    result = scan()
    os.makedirs(_RESULTS_DIR, exist_ok=True)
    with open(os.path.join(_RESULTS_DIR, "phase2_inventory.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result["summary"], indent=2))
