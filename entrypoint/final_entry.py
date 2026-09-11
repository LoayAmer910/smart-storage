"""StorageArch FINAL - PyInstaller entry point, isolated from the project
root on purpose.

The project root has its OWN copies of storage_config.py, archive_manager.py,
archive_manager_smart.py, smart_compression.py, database_manager.py -
separate, different files from beta9_src's self-contained copies of the
same names (beta9_src is a full, independent snapshot of the whole app -
see beta9_src/main.py's own docstring history). A plain `python
storagearch_final.py` run from the project root resolves `import
smart_compression` correctly via sys.path order (beta9_src inserted ahead
of root), but PyInstaller's Analysis implicitly searches the ENTRY
SCRIPT's own directory first/foremost when resolving bare imports -
if the entry script lives in the project root (next to the root's OWN
smart_compression.py etc.), the frozen build silently bundles the WRONG
(root) copies instead of beta9_src's, with no error - every file then
silently falls back to baseline storage (every real strategy call raises
inside a broad `except Exception` in archive_manager_smart.py's
_decide_and_enqueue and is swallowed). Confirmed via a real frozen-exe
repro during this round's packaging work: the root smart_compression.py's
decide_and_prepare() doesn't even accept beta9_src's `pool=` parameter,
so every real candidate call threw TypeError once the wrong module won.

Fix: this entry script lives in its own directory with zero colliding
filenames, so PyInstaller's implicit script-directory search contributes
nothing. `pathex` in StorageArch_Final.spec still points at beta9_src for
every real import - now uncontested.
"""
import json
import multiprocessing
import os
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:  # noqa: BLE001
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_BETA9_SRC = os.path.join(_REPO_ROOT, "beta9_src")
_SMART_SELECTOR_SRC = os.path.join(_BETA9_SRC, "experiments", "smart_selector", "src")
_IMAGES_SRC = os.path.join(_BETA9_SRC, "experiments", "images", "src")
for _p in (_IMAGES_SRC, _SMART_SELECTOR_SRC, _BETA9_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# Deliberately NOT adding _REPO_ROOT to sys.path - beta9_src is fully
# self-contained (its own storage_config/archive_manager/archive_manager_smart/
# smart_compression/database_manager), so nothing here needs the project
# root, and adding it would reintroduce the exact collision this file exists
# to avoid.

import storage_config  # noqa: E402
import archive_manager_smart  # noqa: E402
from beta12_src.integration_layer import activate_beta12, deactivate_beta12  # noqa: E402


def _iter_files(root):
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            yield os.path.join(dirpath, name)


def run(source_folder, archive_name, output_dir, report_json_path):
    os.makedirs(output_dir, exist_ok=True)
    data_root = os.path.abspath(os.path.join(output_dir, "EngineData"))
    storage_config.save(data_root=data_root)
    os.makedirs(storage_config.log_dir(), exist_ok=True)

    zip_path = os.path.abspath(os.path.join(output_dir, f"{archive_name}.szf"))

    file_count_on_disk = sum(1 for _ in _iter_files(source_folder))
    print(f"Discovered {file_count_on_disk} files under {source_folder}", flush=True)
    print(f"total_cores (os.cpu_count()): {os.cpu_count()}", flush=True)
    print(f"engine module resolved from: {archive_manager_smart.__file__}", flush=True)

    activate_beta12()
    t0 = time.perf_counter()
    try:
        result_zip_path, member_root, file_results = archive_manager_smart.add_folder_to_archive_smart(
            source_folder, archive_name, zip_path=zip_path,
        )
    finally:
        wall_seconds = time.perf_counter() - t0
        deactivate_beta12()

    if result_zip_path is None:
        report = {
            "source_folder": source_folder,
            "status": "FAILED",
            "total_seconds": round(wall_seconds, 3),
        }
        with open(report_json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(json.dumps(report, indent=2), flush=True)
        return 1

    log_path = os.path.join(storage_config.log_dir(), "smart_folder_performance.log")
    last_entry = {}
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last_entry = json.loads(line)
    except OSError:
        pass

    total_input_bytes = sum(r["original_size"] for r in file_results)
    output_archive_bytes = os.path.getsize(result_zip_path)
    total_bytes_saved = total_input_bytes - output_archive_bytes

    strategy_counts = {}
    for r in file_results:
        key = r["strategy"] or "baseline_storagearch_zip"
        strategy_counts[key] = strategy_counts.get(key, 0) + 1

    report = {
        "source_folder": source_folder,
        "file_count": len(file_results),
        "total_input_bytes": total_input_bytes,
        "output_archive_bytes": output_archive_bytes,
        "total_bytes_saved": total_bytes_saved,
        "total_bytes_saved_pct": round(total_bytes_saved / total_input_bytes * 100, 3) if total_input_bytes else 0.0,
        "total_seconds": round(wall_seconds, 3),
        "verify_seconds": last_entry.get("verify_seconds"),
        "worst_strategy": last_entry.get("worst_strategy"),
        "worst_file_type": last_entry.get("worst_file_type"),
        "worst_processing_seconds": last_entry.get("worst_processing_seconds"),
        "strategy_counts": strategy_counts,
        "archive_path": result_zip_path,
        "engine": "StorageArch FINAL (Beta9 verified-candidate core + free-cores admission overlay)",
    }
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump({"report": report, "file_results": file_results}, f, indent=2, ensure_ascii=False, default=str)
    print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)
    print(f"\nFull report written to: {report_json_path}", flush=True)
    return 0


if __name__ == "__main__":
    import argparse

    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser()
    parser.add_argument("source_folder")
    parser.add_argument("archive_name")
    parser.add_argument("output_dir")
    parser.add_argument("report_json_path")
    args = parser.parse_args()
    sys.exit(run(args.source_folder, args.archive_name, args.output_dir, args.report_json_path))
