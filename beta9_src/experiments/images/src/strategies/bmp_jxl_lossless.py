"""Beta 9 candidate: BMP-aware lossless recompression.

Scoped exactly to the variant validated with real evidence in
beta9_research/logs/STAGE_GATE_RESULTS.md section D: uncompressed 24bpp
BITMAPINFOHEADER (40-byte info header), no palette, no per-row padding.
Any other BMP (paletted, BI_BITFIELDS/RLE compression, BITMAPV4/V5HEADER,
padded rows) raises - the harness treats that as this candidate failing
for that file (see smart_compression._compress_restore_verify's except
clause) and falls through to generic_zstd/baseline exactly as in Beta 8.
Never guess at an unverified layout.

Keeps the 14+40=54 byte file+info header verbatim, recompresses the pixel
payload losslessly through cjxl (--distance 0), and on restore reverses
the exact same transform before concatenating back with the verbatim
header - reconstruction is checked against the full original file bytes,
not just decoded pixels (see the real per-file results in the Stage Gate
doc: 3/3 real files byte-exact, +9.92% net vs the current generic_zstd
path on this corpus).
"""

import os
import struct
import subprocess
import sys

import numpy as np
from PIL import Image

from strategies.base import CompressionStrategy, CompressResult, RestoreResult

_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _TOOLS_DIR = os.path.join(sys._MEIPASS, "images", "tools")
else:
    _TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tools")
_CJXL_PATH = os.path.join(_TOOLS_DIR, "cjxl.exe")
_DJXL_PATH = os.path.join(_TOOLS_DIR, "djxl.exe")


def _parse_supported_header(data):
    if len(data) < 54 or data[:2] != b"BM":
        raise ValueError("not a BMP")
    bfOffBits = struct.unpack("<I", data[10:14])[0]
    biSize = struct.unpack("<I", data[14:18])[0]
    width, height = struct.unpack("<ii", data[18:26])
    bitcount = struct.unpack("<H", data[28:30])[0]
    compression = struct.unpack("<I", data[30:34])[0]
    if not (biSize == 40 and bitcount == 24 and compression == 0 and bfOffBits == 54):
        raise ValueError("unsupported BMP variant - not the validated 24bpp/no-palette/no-padding case")
    row_stride = ((width * 24 + 31) // 32) * 4
    if row_stride != width * 3:
        raise ValueError("row padding present - not implemented")
    expected_pixel_bytes = row_stride * abs(height)
    if len(data) - bfOffBits != expected_pixel_bytes:
        raise ValueError("pixel data size mismatch")
    return bfOffBits, width, height


class BmpJxlLosslessStrategy(CompressionStrategy):
    name = "bmp_jxl_lossless"
    applicable_formats = frozenset({"bmp"})

    def is_available(self):
        if not (os.path.isfile(_CJXL_PATH) and os.path.isfile(_DJXL_PATH)):
            return False, "cjxl.exe/djxl.exe not present"
        return True, ""

    def compress(self, input_path, work_dir):
        with open(input_path, "rb") as f:
            orig = f.read()
        bfOffBits, width, height = _parse_supported_header(orig)  # raises for unsupported variants

        header = orig[:bfOffBits]
        pixel_bytes = orig[bfOffBits:]
        arr = np.frombuffer(pixel_bytes, dtype=np.uint8).reshape(abs(height), width, 3)  # bottom-up, BGR
        arr_rgb_topdown = arr[::-1, :, ::-1]

        png_path = os.path.join(work_dir, os.path.basename(input_path) + ".tmp.png")
        Image.fromarray(arr_rgb_topdown, mode="RGB").save(png_path, compress_level=1)

        jxl_path = os.path.join(work_dir, os.path.basename(input_path) + ".jxl")
        subprocess.run(
            [_CJXL_PATH, png_path, jxl_path, "--distance", "0", "--effort", "7"],
            check=True, capture_output=True, creationflags=_NO_WINDOW_FLAGS,
        )
        os.remove(png_path)

        out_path = os.path.join(work_dir, os.path.basename(input_path) + ".bmpjxl")
        with open(out_path, "wb") as f:
            f.write(struct.pack("<I", len(header)))
            f.write(header)
            with open(jxl_path, "rb") as jf:
                f.write(jf.read())

        return CompressResult(output_path=out_path, stored_size=os.path.getsize(out_path))

    def restore(self, compressed_path, work_dir, original_filename):
        with open(compressed_path, "rb") as f:
            data = f.read()
        (header_len,) = struct.unpack("<I", data[:4])
        header = data[4:4 + header_len]
        jxl_bytes = data[4 + header_len:]

        jxl_path = os.path.join(work_dir, "_bmp_restore.jxl")
        png_path = os.path.join(work_dir, "_bmp_restore.png")
        with open(jxl_path, "wb") as f:
            f.write(jxl_bytes)
        subprocess.run([_DJXL_PATH, jxl_path, png_path], check=True,
                        capture_output=True, creationflags=_NO_WINDOW_FLAGS)

        decoded = np.array(Image.open(png_path).convert("RGB"))
        restored_bgr_bottomup = decoded[::-1, :, ::-1]
        pixel_bytes = restored_bgr_bottomup.tobytes()

        out_path = os.path.join(work_dir, "restored_" + original_filename)
        with open(out_path, "wb") as f:
            f.write(header)
            f.write(pixel_bytes)
        return RestoreResult(output_path=out_path)
