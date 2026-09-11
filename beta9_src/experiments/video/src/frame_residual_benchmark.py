"""Structured/frame-residual R&D experiment.

Tests a different hypothesis than the xdelta binary-patch experiment:
instead of diffing compressed BYTES (Base file vs Original file), decode
both to raw pixels and diff decoded FRAMES. The question: is a pixel-domain
residual meaningfully more compressible than a byte-domain binary patch?

This experiment also explicitly tests the harder, separate question the
byte-domain experiment sidestepped: even if decoded frames reconstruct
exactly, can the ORIGINAL MP4 FILE be reconstructed byte-for-byte? Decoded
pixels do not uniquely determine an encoder's exact bitstream output (many
different H.264 bitstreams decode to identical pixels), so this is tested
directly rather than assumed.

Uses only already-available tools: ffmpeg/ffprobe (extracted earlier) and
numpy/zstd/brotli/lzma/zlib (already installed). No new dependencies.
"""

import json
import lzma
import os
import subprocess
import sys
import time
import zlib

import brotli
import numpy as np
import zstandard

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "images", "src"))
import hashing  # noqa: E402

_HERE = os.path.dirname(__file__)
_VIDEO_ROOT = os.path.dirname(_HERE)
_FFMPEG = os.path.join(_VIDEO_ROOT, "tools", "ffmpeg.exe")
_OUTPUT = os.path.join(_VIDEO_ROOT, "output")
_ORIGINALS_DIR = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\video"

CANDIDATES = [
    {
        "name": "v1-B",
        "original": os.path.join(_ORIGINALS_DIR, "WhatsApp Video 2026-08-13 at 11.49.40.mp4"),
        "base": os.path.join(_OUTPUT, "v1_candB_openh264_25pct.mp4"),
        "width": 464,
        "height": 832,
        "current_storagearch_size": 1796861,
    },
    {
        "name": "v2-B",
        "original": os.path.join(_ORIGINALS_DIR, "WhatsApp Video 2026-08-13 at 11.55.52.mp4"),
        "base": os.path.join(_OUTPUT, "v2_candB_openh264_25pct.mp4"),
        "width": 1024,
        "height": 576,
        "current_storagearch_size": 6841587,
    },
]


def _run(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def _decode_to_raw_yuv(input_path, out_path):
    t0 = time.perf_counter()
    proc = _run([_FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", input_path, "-f", "rawvideo", "-pix_fmt", "yuv420p", out_path])
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed for {input_path}: {proc.stderr.decode(errors='replace')}")
    return elapsed


def _compress_best(data, label):
    results = {}
    timings = {}

    t0 = time.perf_counter()
    results["zlib"] = zlib.compress(data, level=6)
    timings["zlib"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    results["lzma"] = lzma.compress(data, preset=3)
    timings["lzma"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    results["zstd"] = zstandard.ZstdCompressor(level=19, threads=-1).compress(data)
    timings["zstd"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    results["brotli"] = brotli.compress(data, quality=6)
    timings["brotli"] = time.perf_counter() - t0

    for name in results:
        print(f"    {label} {name}: {len(results[name])} bytes in {timings[name]:.1f}s", flush=True)

    best_name = min(results, key=lambda k: len(results[k]))
    return best_name, results[best_name], timings


def _remux_roundtrip_test(original_path, work_dir, tag):
    """Demux original video+audio via stream copy, remux fresh - does this
    alone (with ZERO lossy re-encoding, full bitstream preserved) reproduce
    the exact original file bytes? Tests whether container muxing itself is
    the obstacle to file-exact reconstruction, independent of any residual
    quality."""
    remuxed_path = os.path.join(work_dir, f"{tag}_remux_test.mp4")
    proc = _run([_FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", original_path, "-c", "copy", remuxed_path])
    if proc.returncode != 0:
        return {"remux_ok": False, "reason": f"ffmpeg remux failed: {proc.stderr.decode(errors='replace')}"}

    original_sha = hashing.sha256_file(original_path)
    remuxed_sha = hashing.sha256_file(remuxed_path)
    original_size = os.path.getsize(original_path)
    remuxed_size = os.path.getsize(remuxed_path)
    match = original_sha == remuxed_sha

    result = {
        "remux_byte_exact": match,
        "original_size": original_size,
        "remuxed_size": remuxed_size,
        "original_sha256": original_sha,
        "remuxed_sha256": remuxed_sha,
    }
    os.remove(remuxed_path)
    return result


def run_candidate(cand):
    name = cand["name"]
    width, height = cand["width"], cand["height"]
    frame_bytes = width * height * 3 // 2  # yuv420p: Y + U/4 + V/4

    print(f"=== {name} ===", flush=True)

    orig_raw = os.path.join(_OUTPUT, f"{name}_original.yuv")
    base_raw = os.path.join(_OUTPUT, f"{name}_base.yuv")

    print("  decoding original to raw yuv420p...", flush=True)
    t_decode_orig = _decode_to_raw_yuv(cand["original"], orig_raw)
    print(f"    done in {t_decode_orig:.1f}s, {os.path.getsize(orig_raw)} bytes", flush=True)

    print("  decoding base to raw yuv420p...", flush=True)
    t_decode_base = _decode_to_raw_yuv(cand["base"], base_raw)
    print(f"    done in {t_decode_base:.1f}s, {os.path.getsize(base_raw)} bytes", flush=True)

    orig_size_bytes = os.path.getsize(orig_raw)
    base_size_bytes = os.path.getsize(base_raw)
    n_frames_orig = orig_size_bytes // frame_bytes
    n_frames_base = base_size_bytes // frame_bytes
    n_frames = min(n_frames_orig, n_frames_base)
    frame_count_mismatch = n_frames_orig != n_frames_base

    print(f"  frames: original={n_frames_orig} base={n_frames_base} using={n_frames}"
          f"{' (MISMATCH)' if frame_count_mismatch else ''}", flush=True)

    usable_bytes = n_frames * frame_bytes
    orig_arr = np.fromfile(orig_raw, dtype=np.uint8, count=usable_bytes)
    base_arr = np.fromfile(base_raw, dtype=np.uint8, count=usable_bytes)

    print("  computing residual (mod-256 wraparound, exactly invertible)...", flush=True)
    t0 = time.perf_counter()
    residual = (orig_arr - base_arr)  # numpy uint8 wraps automatically
    t_residual = time.perf_counter() - t0
    raw_residual_size = residual.nbytes

    print("  verifying decoded-frame reconstruction (Base + residual == Original)...", flush=True)
    reconstructed = (base_arr + residual)
    decoded_frame_pass = bool(np.array_equal(reconstructed, orig_arr))
    print(f"    decoded-frame exact match: {decoded_frame_pass}", flush=True)

    print("  compressing residual with zlib/lzma/zstd/brotli...", flush=True)
    residual_bytes = residual.tobytes()
    best_method, best_compressed, comp_timings = _compress_best(residual_bytes, "residual")
    best_compressed_size = len(best_compressed)

    os.remove(orig_raw)
    os.remove(base_raw)

    # --- file-exact reconstruction: can we get back the ORIGINAL MP4 bytes? ---
    print("  testing container remux round-trip (video+audio stream copy, zero re-encode)...", flush=True)
    remux_test = _remux_roundtrip_test(cand["original"], _OUTPUT, name)
    print(f"    remux byte-exact to true original: {remux_test.get('remux_byte_exact')}", flush=True)

    # Side-data required for exact file reconstruction: decoded pixels do not
    # determine the encoder's exact bitstream, so the only correct path is to
    # retain the original's actual encoded video elementary stream. Measure
    # its real size rather than assuming it equals the whole file.
    video_stream_path = os.path.join(_OUTPUT, f"{name}_original_video_stream.h264")
    proc = _run([_FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", cand["original"], "-an", "-c:v", "copy", "-f", "h264", video_stream_path])
    side_data_size = os.path.getsize(video_stream_path) if proc.returncode == 0 else None
    if os.path.exists(video_stream_path):
        os.remove(video_stream_path)

    base_size = os.path.getsize(cand["base"])
    total_smart_size = base_size + best_compressed_size + (side_data_size or 0)
    current = cand["current_storagearch_size"]
    improvement_vs_current_pct = round((current - total_smart_size) / current * 100, 2)

    # File-exact PASS requires: side-data available AND remux itself proven byte-exact
    # (if remux of the FULL untouched bitstream can't reproduce the original file,
    # no amount of side-data fixes that - it's a container-level limitation).
    file_exact_pass = bool(remux_test.get("remux_byte_exact")) and side_data_size is not None

    return {
        "name": name,
        "base_size": base_size,
        "raw_frame_data_size": usable_bytes,
        "raw_residual_size": raw_residual_size,
        "best_compressed_residual_method": best_method,
        "best_compressed_residual_size": best_compressed_size,
        "side_data_size": side_data_size,
        "total_smart_size": total_smart_size,
        "current_storagearch_size": current,
        "improvement_vs_current_pct": improvement_vs_current_pct,
        "decoded_frame_reconstruction_pass": decoded_frame_pass,
        "frame_count_mismatch": frame_count_mismatch,
        "n_frames_used": n_frames,
        "remux_test": remux_test,
        "file_exact_pass": file_exact_pass,
        "timing": {
            "decode_original_s": round(t_decode_orig, 2),
            "decode_base_s": round(t_decode_base, 2),
            "residual_compute_s": round(t_residual, 3),
            "compression_timings_s": {k: round(v, 2) for k, v in comp_timings.items()},
        },
    }


if __name__ == "__main__":
    results = []
    for cand in CANDIDATES:
        result = run_candidate(cand)
        results.append(result)

    out_path = os.path.join(_VIDEO_ROOT, "benchmarks", "frame_residual_results.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("\nResults written to", out_path)
