"""Bounded, streaming frame-residual experiment - v1-B only.

Same algorithm as before (mod-256 wraparound residual per frame, streamed
into a compressor, decoded-frame exact-reconstruction check). Only the
performance layer changed: lower zstd level, and concurrent reader threads
per ffmpeg pipe (bounded queues) instead of strict alternating blocking
reads, so neither decoder stalls waiting on the other.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np
import zstandard

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "images", "src"))

_HERE = os.path.dirname(__file__)
_VIDEO_ROOT = os.path.dirname(_HERE)
_FFMPEG = os.path.join(_VIDEO_ROOT, "tools", "ffmpeg.exe")
_ORIGINALS_DIR = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\video"

CAND = {
    "name": "v1-B",
    "original": os.path.join(_ORIGINALS_DIR, "WhatsApp Video 2026-08-13 at 11.49.40.mp4"),
    "base": os.path.join(_VIDEO_ROOT, "output", "v1_candB_openh264_25pct.mp4"),
    "width": 464,
    "height": 832,
    "expected_frames": 512,
    "current_storagearch_size": 1796861,
}

_QUEUE_MAXSIZE = 8  # bounds memory to a small constant number of frames in flight


def _open_decode_pipe(input_path):
    return subprocess.Popen(
        [_FFMPEG, "-hide_banner", "-loglevel", "error", "-i", input_path,
         "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10 * 1024 * 1024,
    )


def _reader_thread(stream, frame_bytes, out_queue):
    try:
        while True:
            chunks = []
            remaining = frame_bytes
            while remaining > 0:
                chunk = stream.read(remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) < frame_bytes:
                out_queue.put(None)  # EOF sentinel
                return
            out_queue.put(data)
    except Exception as exc:  # noqa: BLE001
        out_queue.put(exc)


def main():
    cand = CAND
    width, height = cand["width"], cand["height"]
    frame_bytes = width * height * 3 // 2
    expected_frames = cand["expected_frames"]

    t_start = time.perf_counter()

    orig_proc = _open_decode_pipe(cand["original"])
    base_proc = _open_decode_pipe(cand["base"])

    orig_q = queue.Queue(maxsize=_QUEUE_MAXSIZE)
    base_q = queue.Queue(maxsize=_QUEUE_MAXSIZE)

    orig_thread = threading.Thread(target=_reader_thread, args=(orig_proc.stdout, frame_bytes, orig_q), daemon=True)
    base_thread = threading.Thread(target=_reader_thread, args=(base_proc.stdout, frame_bytes, base_q), daemon=True)
    orig_thread.start()
    base_thread.start()

    compress_obj = zstandard.ZstdCompressor(level=6).compressobj()
    compressed_chunks = []

    n_frames = 0
    raw_residual_size = 0
    all_frames_exact = True

    while True:
        orig_frame = orig_q.get()
        base_frame = base_q.get()

        if orig_frame is None or base_frame is None:
            break
        if isinstance(orig_frame, Exception):
            raise orig_frame
        if isinstance(base_frame, Exception):
            raise base_frame

        orig_arr = np.frombuffer(orig_frame, dtype=np.uint8)
        base_arr = np.frombuffer(base_frame, dtype=np.uint8)

        residual = orig_arr - base_arr  # vectorized, uint8 wraparound, exactly invertible
        reconstructed = base_arr + residual
        if not np.array_equal(reconstructed, orig_arr):
            all_frames_exact = False

        residual_bytes = residual.tobytes()
        raw_residual_size += len(residual_bytes)
        piece = compress_obj.compress(residual_bytes)
        if piece:
            compressed_chunks.append(piece)

        n_frames += 1

    orig_proc.stdout.close()
    base_proc.stdout.close()
    orig_proc.wait(timeout=10)
    base_proc.wait(timeout=10)

    compressed_chunks.append(compress_obj.flush())
    compressed_residual_size = sum(len(c) for c in compressed_chunks)

    elapsed_total = time.perf_counter() - t_start
    base_size = os.path.getsize(cand["base"])
    total_smart_size = base_size + compressed_residual_size
    current = cand["current_storagearch_size"]

    result = {
        "name": cand["name"],
        "n_frames": n_frames,
        "expected_frames": expected_frames,
        "raw_residual_size": raw_residual_size,
        "compressed_residual_size": compressed_residual_size,
        "compression_method": "zstd level 6 (streaming)",
        "base_size": base_size,
        "base_plus_residual_total": total_smart_size,
        "current_storagearch_size": current,
        "improvement_vs_current_pct": round((current - total_smart_size) / current * 100, 2),
        "decoded_frame_reconstruction_pass": all_frames_exact,
        "elapsed_total_s": round(elapsed_total, 2),
    }

    out_path = os.path.join(_VIDEO_ROOT, "benchmarks", "frame_residual_v1B_streaming.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
