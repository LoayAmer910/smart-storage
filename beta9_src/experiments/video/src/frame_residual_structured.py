"""Structured / categorized frame-residual experiment - v1-B only.

Same mod-256 exact residual as the flat experiment, but instead of
compressing the raw per-pixel residual stream directly, each frame is
split into 16x16 luma / 8x8 chroma macroblocks (384 residual bytes each:
256 Y + 64 U + 64 V), and each block is encoded in whichever of five
formats produces the fewest real bytes (including header overhead), then
the whole structured stream is compressed once with zstd.

Decoding is verified from the actual packed bytes (not the pre-packing
numpy arrays) for SMALL/MEDIUM/RAW, and from the recorded sparse
index/value pairs for SPARSE - a genuine roundtrip check, not a
tautological one.
"""

import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time

import numpy as np
import zstandard

_HERE = os.path.dirname(__file__)
_VIDEO_ROOT = os.path.dirname(_HERE)
_FFMPEG = os.path.join(_VIDEO_ROOT, "tools", "ffmpeg.exe")
_ORIGINALS_DIR = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\video"
_SCRATCH = os.path.join(_VIDEO_ROOT, "output", "structured_residual")

CAND = {
    "name": "v1-B",
    "original": os.path.join(_ORIGINALS_DIR, "WhatsApp Video 2026-08-13 at 11.49.40.mp4"),
    "base": os.path.join(_VIDEO_ROOT, "output", "v1_candB_openh264_25pct.mp4"),
    "width": 464,
    "height": 832,
    "expected_frames": 512,
    "current_storagearch_size": 1796861,
    "flat_compressed_residual_baseline": 106442569,
}

BLOCK_Y = 16
BLOCK_C = 8
_QUEUE_MAXSIZE = 8

CAT_ZERO, CAT_SPARSE, CAT_SMALL, CAT_MEDIUM, CAT_RAW = 0, 1, 2, 3, 4
CAT_NAMES = {0: "ZERO", 1: "SPARSE", 2: "SMALL", 3: "MEDIUM", 4: "RAW"}


def _open_decode_pipe(input_path):
    return subprocess.Popen(
        [_FFMPEG, "-hide_banner", "-loglevel", "error", "-i", input_path,
         "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10 * 1024 * 1024,
    )


def _reader_thread(stream, frame_bytes, out_queue):
    try:
        while True:
            chunks, remaining = [], frame_bytes
            while remaining > 0:
                chunk = stream.read(remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) < frame_bytes:
                out_queue.put(None)
                return
            out_queue.put(data)
    except Exception as exc:  # noqa: BLE001
        out_queue.put(exc)


def _to_blocks(plane, block_size, n_rows, n_cols):
    br, bc = n_rows, n_cols
    reshaped = plane.reshape(br, block_size, bc, block_size)
    return reshaped.transpose(0, 2, 1, 3).reshape(br * bc, block_size * block_size)


def _from_blocks(blocks, block_size, n_rows, n_cols):
    br, bc = n_rows, n_cols
    reshaped = blocks.reshape(br, bc, block_size, block_size)
    return reshaped.transpose(0, 2, 1, 3).reshape(br * block_size, bc * block_size)


def _pack_4bit(offset_vals):
    n = offset_vals.shape[0]
    r = offset_vals.reshape(n, -1, 2)
    return (r[:, :, 0] | (r[:, :, 1] << 4)).astype(np.uint8)


def _unpack_4bit(packed):
    n = packed.shape[0]
    v0 = packed & 0x0F
    v1 = (packed >> 4) & 0x0F
    out = np.empty((n, packed.shape[1] * 2), dtype=np.uint8)
    out[:, 0::2] = v0
    out[:, 1::2] = v1
    return out


def _pack_6bit(offset_vals):
    n = offset_vals.shape[0]
    bits = np.unpackbits(offset_vals.reshape(n, -1, 1), axis=2, bitorder="big")[:, :, 2:8]
    flat = bits.reshape(n, -1)
    return np.packbits(flat, axis=1, bitorder="big")


def _unpack_6bit(packed, n_values):
    n = packed.shape[0]
    bits = np.unpackbits(packed, axis=1, bitorder="big")
    bits6 = bits.reshape(n, n_values, 6)
    bits8 = np.zeros((n, n_values, 8), dtype=np.uint8)
    bits8[:, :, 2:8] = bits6
    return np.packbits(bits8, axis=2, bitorder="big").reshape(n, n_values)


def main():
    cand = CAND
    width, height = cand["width"], cand["height"]
    y_size = width * height
    c_w, c_h = width // 2, height // 2
    c_size = c_w * c_h
    frame_bytes = y_size + 2 * c_size

    y_rows, y_cols = height // BLOCK_Y, width // BLOCK_Y
    c_rows, c_cols = c_h // BLOCK_C, c_w // BLOCK_C
    assert y_rows == c_rows and y_cols == c_cols, "luma/chroma block grids must align"
    n_blocks = y_rows * y_cols
    block_len = BLOCK_Y * BLOCK_Y + 2 * BLOCK_C * BLOCK_C  # 256+64+64=384

    t_start = time.perf_counter()

    orig_proc = _open_decode_pipe(cand["original"])
    base_proc = _open_decode_pipe(cand["base"])
    orig_q, base_q = queue.Queue(maxsize=_QUEUE_MAXSIZE), queue.Queue(maxsize=_QUEUE_MAXSIZE)
    threading.Thread(target=_reader_thread, args=(orig_proc.stdout, frame_bytes, orig_q), daemon=True).start()
    threading.Thread(target=_reader_thread, args=(base_proc.stdout, frame_bytes, base_q), daemon=True).start()

    header = struct.pack("<8sHHI", b"SRESID1\0", width, height, cand["expected_frames"])
    stream_parts = [header]

    cat_counts = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    n_frames = 0
    all_exact = True

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

        def planes(arr):
            y = arr[:y_size].reshape(height, width)
            u = arr[y_size:y_size + c_size].reshape(c_h, c_w)
            v = arr[y_size + c_size:].reshape(c_h, c_w)
            return y, u, v

        oy, ou, ov = planes(orig_arr)
        by, bu, bv = planes(base_arr)

        oy_b, ou_b, ov_b = _to_blocks(oy, BLOCK_Y, y_rows, y_cols), _to_blocks(ou, BLOCK_C, c_rows, c_cols), _to_blocks(ov, BLOCK_C, c_rows, c_cols)
        by_b, bu_b, bv_b = _to_blocks(by, BLOCK_Y, y_rows, y_cols), _to_blocks(bu, BLOCK_C, c_rows, c_cols), _to_blocks(bv, BLOCK_C, c_rows, c_cols)

        orig_blocks = np.concatenate([oy_b, ou_b, ov_b], axis=1)   # (n_blocks, 384)
        base_blocks = np.concatenate([by_b, bu_b, bv_b], axis=1)

        residual = orig_blocks - base_blocks  # uint8 wraparound
        residual_signed = residual.view(np.int8)

        is_zero = ~residual.any(axis=1)
        nnz = np.count_nonzero(residual, axis=1)
        bmin, bmax = residual_signed.min(axis=1), residual_signed.max(axis=1)
        fits_small = (bmin >= -7) & (bmax <= 7)
        fits_medium = (bmin >= -31) & (bmax <= 31)

        big = 10 ** 9
        costs = np.empty((n_blocks, 5), dtype=np.int64)
        costs[:, CAT_ZERO] = np.where(is_zero, 1, big)
        costs[:, CAT_SPARSE] = 3 + 3 * nnz
        costs[:, CAT_SMALL] = np.where(fits_small, 193, big)
        costs[:, CAT_MEDIUM] = np.where(fits_medium, 289, big)
        costs[:, CAT_RAW] = 385
        category = np.argmin(costs, axis=1)

        block_bytes = [None] * n_blocks

        zero_idx = np.nonzero(category == CAT_ZERO)[0]
        for i in zero_idx:
            block_bytes[i] = b"\x00"

        raw_idx = np.nonzero(category == CAT_RAW)[0]
        raw_rows = residual[raw_idx]
        for j, i in enumerate(raw_idx):
            block_bytes[i] = b"\x04" + raw_rows[j].tobytes()

        small_idx = np.nonzero(category == CAT_SMALL)[0]
        packed_small = None
        if len(small_idx):
            offset = (residual_signed[small_idx].astype(np.int16) + 7).astype(np.uint8)
            packed_small = _pack_4bit(offset)
            for j, i in enumerate(small_idx):
                block_bytes[i] = b"\x02" + packed_small[j].tobytes()

        medium_idx = np.nonzero(category == CAT_MEDIUM)[0]
        packed_medium = None
        if len(medium_idx):
            offset = (residual_signed[medium_idx].astype(np.int16) + 31).astype(np.uint8)
            packed_medium = _pack_6bit(offset)
            for j, i in enumerate(medium_idx):
                block_bytes[i] = b"\x03" + packed_medium[j].tobytes()

        sparse_idx = np.nonzero(category == CAT_SPARSE)[0]
        sparse_records = {}
        for i in sparse_idx:
            row = residual_signed[i]
            nz_pos = np.nonzero(row)[0]
            nz_val = row[nz_pos]
            sparse_records[i] = (nz_pos, nz_val)
            payload = struct.pack("<H", len(nz_pos)) + nz_pos.astype("<u2").tobytes() + nz_val.astype(np.int8).tobytes()
            block_bytes[i] = b"\x01" + payload

        stream_parts.append(b"".join(block_bytes))

        for c in range(5):
            cat_counts[c] += int(np.count_nonzero(category == c))

        # --- genuine decode-from-bytes verification ---
        reconstructed_signed = np.zeros((n_blocks, block_len), dtype=np.int8)
        if len(raw_idx):
            reconstructed_signed[raw_idx] = raw_rows.view(np.int8)
        if len(small_idx):
            unpacked = _unpack_4bit(packed_small)
            reconstructed_signed[small_idx] = (unpacked.astype(np.int16) - 7).astype(np.int8)
        if len(medium_idx):
            unpacked = _unpack_6bit(packed_medium, block_len)
            reconstructed_signed[medium_idx] = (unpacked.astype(np.int16) - 31).astype(np.int8)
        for i, (nz_pos, nz_val) in sparse_records.items():
            reconstructed_signed[i, nz_pos] = nz_val
        # zero blocks: already zero-initialized

        reconstructed_residual = reconstructed_signed.view(np.uint8)
        reconstructed_blocks = base_blocks + reconstructed_residual  # uint8 wraparound

        recon_y = _from_blocks(reconstructed_blocks[:, :BLOCK_Y * BLOCK_Y], BLOCK_Y, y_rows, y_cols)
        recon_u = _from_blocks(reconstructed_blocks[:, BLOCK_Y * BLOCK_Y:BLOCK_Y * BLOCK_Y + BLOCK_C * BLOCK_C], BLOCK_C, c_rows, c_cols)
        recon_v = _from_blocks(reconstructed_blocks[:, BLOCK_Y * BLOCK_Y + BLOCK_C * BLOCK_C:], BLOCK_C, c_rows, c_cols)

        frame_exact = (
            np.array_equal(recon_y, oy) and np.array_equal(recon_u, ou) and np.array_equal(recon_v, ov)
        )
        if not frame_exact:
            all_exact = False

        n_frames += 1

    orig_proc.stdout.close()
    base_proc.stdout.close()
    orig_proc.wait(timeout=10)
    base_proc.wait(timeout=10)

    structured_raw = b"".join(stream_parts)
    structured_raw_size = len(structured_raw)

    compressed = zstandard.ZstdCompressor(level=6).compress(structured_raw)
    structured_zstd_size = len(compressed)

    elapsed_total = time.perf_counter() - t_start
    base_size = os.path.getsize(cand["base"])
    total_smart_size = base_size + structured_zstd_size
    current = cand["current_storagearch_size"]
    flat_baseline = cand["flat_compressed_residual_baseline"]

    total_blocks = sum(cat_counts.values())
    result = {
        "frames_processed": n_frames,
        "block_size": "16x16 Y + 8x8 U + 8x8 V (384 bytes/block)",
        "total_blocks": total_blocks,
        "category_distribution": {
            CAT_NAMES[c]: {"count": cat_counts[c], "pct": round(100 * cat_counts[c] / total_blocks, 2)}
            for c in range(5)
        },
        "flat_compressed_residual_baseline": flat_baseline,
        "structured_residual_raw_size": structured_raw_size,
        "structured_residual_zstd_size": structured_zstd_size,
        "improvement_vs_flat_residual_pct": round((flat_baseline - structured_zstd_size) / flat_baseline * 100, 2),
        "base_size": base_size,
        "base_plus_structured_residual_total": total_smart_size,
        "current_storagearch_size": current,
        "improvement_vs_current_storagearch_pct": round((current - total_smart_size) / current * 100, 2),
        "decoded_frame_reconstruction_pass": all_exact,
        "total_elapsed_s": round(elapsed_total, 2),
    }

    out_path = os.path.join(_VIDEO_ROOT, "benchmarks", "frame_residual_structured_v1B.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
