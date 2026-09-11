"""xdelta3-based Base+Residual benchmark for the 4 existing Base candidates.

Reuses the Base MP4s already produced by ffmpeg in experiments/video/output/.
Does not re-encode anything. For each candidate:

  patch = xdelta3(source=Base, target=Original)   # Base -> Original
  restored = xdelta3 -d (source=Base, patch)       # Base + patch -> reconstructed Original

Then verifies restored_size == original_size and SHA256(restored) == SHA256(original)
before ever counting the candidate as a pass. The raw patch is then compressed with
zstd/brotli/lzma/zlib and the smallest is reported as best_compressed_patch.
"""

import lzma
import os
import subprocess
import sys
import time
import zlib

import brotli
import zstandard

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "images", "src"))
import hashing  # noqa: E402

_HERE = os.path.dirname(__file__)
_VIDEO_ROOT = os.path.dirname(_HERE)
_XDELTA = os.path.join(_VIDEO_ROOT, "tools", "xdelta3.exe")
_OUTPUT = os.path.join(_VIDEO_ROOT, "output")
_ORIGINALS_DIR = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\video"

CANDIDATES = [
    {
        "name": "v1-A",
        "original": os.path.join(_ORIGINALS_DIR, "WhatsApp Video 2026-08-13 at 11.49.40.mp4"),
        "base": os.path.join(_OUTPUT, "v1_candA_openh264_50pct.mp4"),
        "current_storagearch_size": 1796861,
    },
    {
        "name": "v1-B",
        "original": os.path.join(_ORIGINALS_DIR, "WhatsApp Video 2026-08-13 at 11.49.40.mp4"),
        "base": os.path.join(_OUTPUT, "v1_candB_openh264_25pct.mp4"),
        "current_storagearch_size": 1796861,
    },
    {
        "name": "v2-A",
        "original": os.path.join(_ORIGINALS_DIR, "WhatsApp Video 2026-08-13 at 11.55.52.mp4"),
        "base": os.path.join(_OUTPUT, "v2_candA_openh264_50pct.mp4"),
        "current_storagearch_size": 6841587,
    },
    {
        "name": "v2-B",
        "original": os.path.join(_ORIGINALS_DIR, "WhatsApp Video 2026-08-13 at 11.55.52.mp4"),
        "base": os.path.join(_OUTPUT, "v2_candB_openh264_25pct.mp4"),
        "current_storagearch_size": 6841587,
    },
]


def _compress_best(data):
    results = {}
    results["zlib"] = zlib.compress(data, level=9)
    results["lzma"] = lzma.compress(data, preset=9)
    results["zstd"] = zstandard.ZstdCompressor(level=19).compress(data)
    results["brotli"] = brotli.compress(data, quality=11)
    best_name = min(results, key=lambda k: len(results[k]))
    return best_name, results[best_name]


def run_candidate(cand):
    name = cand["name"]
    base_path = cand["base"]
    original_path = cand["original"]
    base_size = os.path.getsize(base_path)

    patch_path = os.path.join(_OUTPUT, f"{name}_patch.xd3")
    restored_path = os.path.join(_OUTPUT, f"{name}_restored.mp4")

    # patch creation: Base -> Original
    t0 = time.perf_counter()
    proc = subprocess.run(
        [_XDELTA, "-e", "-f", "-s", base_path, original_path, patch_path],
        capture_output=True, timeout=300,
    )
    patch_time = time.perf_counter() - t0
    if proc.returncode != 0:
        return {**cand, "status": "ERROR", "notes": f"xdelta3 encode failed: {proc.stderr.decode(errors='replace')}"}

    raw_patch_size = os.path.getsize(patch_path)

    # apply patch: Base + patch -> restored
    t0 = time.perf_counter()
    proc = subprocess.run(
        [_XDELTA, "-d", "-f", "-s", base_path, patch_path, restored_path],
        capture_output=True, timeout=300,
    )
    restore_time = time.perf_counter() - t0
    if proc.returncode != 0:
        return {**cand, "status": "ERROR", "notes": f"xdelta3 decode failed: {proc.stderr.decode(errors='replace')}"}

    restored_size = os.path.getsize(restored_path)
    original_size = os.path.getsize(original_path)
    original_sha256 = hashing.sha256_file(original_path)
    restored_sha256 = hashing.sha256_file(restored_path)

    size_match = restored_size == original_size
    hash_match = restored_sha256 == original_sha256
    exact_pass = size_match and hash_match

    with open(patch_path, "rb") as f:
        raw_patch = f.read()
    best_method, best_compressed = _compress_best(raw_patch)
    best_compressed_size = len(best_compressed)

    total_smart_size = base_size + best_compressed_size
    current = cand["current_storagearch_size"]
    improvement_vs_current_pct = round((current - total_smart_size) / current * 100, 2)
    saving_vs_original_pct = round((original_size - total_smart_size) / original_size * 100, 2)

    # cleanup on pass; keep artifacts on failure for inspection
    if exact_pass:
        os.remove(patch_path)
        os.remove(restored_path)

    return {
        **cand,
        "status": "PASS" if exact_pass else "FAIL",
        "base_size": base_size,
        "raw_patch_size": raw_patch_size,
        "best_compressed_patch_method": best_method,
        "best_compressed_patch_size": best_compressed_size,
        "total_smart_size": total_smart_size,
        "improvement_vs_current_pct": improvement_vs_current_pct,
        "saving_vs_original_pct": saving_vs_original_pct,
        "patch_creation_time_s": round(patch_time, 3),
        "restore_time_s": round(restore_time, 3),
        "restored_size": restored_size,
        "original_size": original_size,
        "size_match": size_match,
        "hash_match": hash_match,
        "notes": "",
    }


if __name__ == "__main__":
    results = []
    for cand in CANDIDATES:
        print(f"Processing {cand['name']}...", flush=True)
        result = run_candidate(cand)
        results.append(result)
        print(f"  status={result['status']}", flush=True)

    import json
    out_path = os.path.join(_VIDEO_ROOT, "benchmarks", "xdelta_results.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("Results written to", out_path)
