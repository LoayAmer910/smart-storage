"""PNG-aware exact-reconstruction candidate - Phase 2 research.

Idea: PNG's IDAT is raw filtered scanline bytes compressed once with zlib.
Recompressing the ALREADY-zlib-compressed IDAT bytes with a stronger generic
codec (what Phase 1's generic_* strategies do) wastes effort, because
compressed data is high-entropy and barely compresses further. This
strategy instead decompresses IDAT back to the raw filtered scanlines
first, and recompresses THAT with a stronger codec (brotli) - the
pre-compression data still has exploitable structure.

The catch: to reconstruct the exact original FILE, we need the exact
original IDAT bytes back, not just equivalent pixels. Two paths:

  EXACT_REDEFLATE: try re-deflating the recovered raw scanlines with a
  small grid of standard zlib parameters. If any exactly reproduces the
  original IDAT bytes, no extra data is needed - this is a real win.

  DIFF_PATCH: if no standard re-deflate matches (expected for PNGs made
  by encoders with custom filter/deflate heuristics - libpng, browsers,
  OS screenshot tools, etc.), fall back to a literal-embedding byte patch
  (stdlib difflib) from a level-6 baseline candidate to the true IDAT
  bytes, compressed. Only worthwhile if the patch stays small - reported
  honestly either way, never assumed.

Every byte outside IDAT (headers, palette, ancillary chunks, IEND) is
carried verbatim - only the IDAT run is ever transformed.
"""

import difflib
import os
import pickle
import struct
import zlib

import brotli

import png_container
from strategies.base import CompressionStrategy, CompressResult, RestoreResult

_MAGIC = b"PNGSMART1"
_MODE_EXACT_REDEFLATE = 0
_MODE_DIFF_PATCH = 1

# Small, fast grid of standard zlib parameters to try before giving up on
# an exact match. level=6 is zlib's own default and PNG's most common
# real-world setting; level=9 is "maximum compression"; Z_FILTERED is the
# strategy libpng recommends specifically for filtered image data.
_REDEFLATE_GRID = [
    (level, strategy)
    for level in (6, 9, 1, 4)
    for strategy in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED)
]

# Above this COMPRESSED buffer size (the size of the two byte streams
# difflib.SequenceMatcher actually compares), computing a patch is not
# attempted. Empirically, ~5KB streams take ~0.1s; SequenceMatcher's cost
# on binary data grows much faster than linearly, so this stays
# conservative. The exact-redeflate attempt is still always tried first,
# since it is cheap (a handful of zlib.compress calls) regardless of size.
_DIFF_PATCH_SIZE_LIMIT = 15_000


def _try_exact_redeflate(idat_raw, true_idat_concat):
    for level, strategy in _REDEFLATE_GRID:
        compressor = zlib.compressobj(level, zlib.DEFLATED, 15, 8, strategy)
        candidate = compressor.compress(idat_raw) + compressor.flush()
        if candidate == true_idat_concat:
            return level, strategy
    return None


def _build_patch(baseline, target):
    """Literal-embedding patch: transform `baseline` into `target`.

    Built from difflib opcodes, but 'equal' ops only ever reference offsets
    into `baseline` - all other bytes are embedded literally, so applying
    the patch never depends on anything but `baseline` itself.
    """
    matcher = difflib.SequenceMatcher(None, baseline, target, autojunk=False)
    ops = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            ops.append(("=", i1, i2))
        else:
            ops.append(("+", target[j1:j2]))
    return ops


def _apply_patch(baseline, ops):
    out = bytearray()
    for op in ops:
        if op[0] == "=":
            out += baseline[op[1]:op[2]]
        else:
            out += op[1]
    return bytes(out)


class PngIdatRecompressStrategy(CompressionStrategy):
    name = "png_idat_recompress"
    applicable_formats = frozenset({"png"})

    def compress(self, input_path, work_dir):
        prefix_chunks, idat_lengths, idat_concat, suffix_chunks, idat_raw = png_container.decompose(input_path)

        recompressed_scanlines = brotli.compress(idat_raw, quality=11)

        match = _try_exact_redeflate(idat_raw, idat_concat)
        if match is not None:
            level, strategy = match
            payload = {
                "mode": _MODE_EXACT_REDEFLATE,
                "level": level,
                "strategy": strategy,
                "recompressed_scanlines": recompressed_scanlines,
            }
        else:
            # Compute the level-6 baseline first (cheap: one zlib.compress
            # call regardless of size) so the size gate below checks the
            # actual buffers difflib would compare, not the much larger
            # decompressed scanline stream.
            baseline_compressor = zlib.compressobj(6, zlib.DEFLATED, 15, 8, zlib.Z_DEFAULT_STRATEGY)
            baseline = baseline_compressor.compress(idat_raw) + baseline_compressor.flush()
            diff_input_size = max(len(baseline), len(idat_concat))

            if diff_input_size > _DIFF_PATCH_SIZE_LIMIT:
                payload = {
                    "mode": None,  # signals "no valid reconstruction found" to compress() caller
                    "reason": (
                        f"compressed IDAT size {diff_input_size} exceeds diff-patch limit "
                        f"{_DIFF_PATCH_SIZE_LIMIT} (difflib does not scale to this size)"
                    ),
                }
            else:
                patch_ops = _build_patch(baseline, idat_concat)
                compressed_patch = brotli.compress(pickle.dumps(patch_ops, protocol=4), quality=11)
                payload = {
                    "mode": _MODE_DIFF_PATCH,
                    "recompressed_scanlines": recompressed_scanlines,
                    "compressed_patch": compressed_patch,
                }

        if payload.get("mode") is None:
            raise RuntimeError(
                f"png_idat_recompress: no valid exact-reconstruction candidate ({payload.get('reason')})"
            )

        container = {
            "prefix_chunks": prefix_chunks,
            "idat_lengths": idat_lengths,
            "suffix_chunks": suffix_chunks,
            "payload": payload,
        }
        serialized = pickle.dumps(container, protocol=4)

        arcname = os.path.basename(input_path)
        out_path = os.path.join(work_dir, arcname + ".pngsmart")
        with open(out_path, "wb") as f:
            f.write(_MAGIC)
            f.write(struct.pack("<I", len(serialized)))
            f.write(serialized)

        return CompressResult(output_path=out_path, stored_size=os.path.getsize(out_path))

    def restore(self, compressed_path, work_dir, original_filename):
        with open(compressed_path, "rb") as f:
            magic = f.read(len(_MAGIC))
            if magic != _MAGIC:
                raise ValueError("not a png_idat_recompress container")
            (length,) = struct.unpack("<I", f.read(4))
            container = pickle.loads(f.read(length))

        payload = container["payload"]
        idat_raw = brotli.decompress(payload["recompressed_scanlines"])

        if payload["mode"] == _MODE_EXACT_REDEFLATE:
            compressor = zlib.compressobj(payload["level"], zlib.DEFLATED, 15, 8, payload["strategy"])
            idat_concat = compressor.compress(idat_raw) + compressor.flush()
        else:
            baseline_compressor = zlib.compressobj(6, zlib.DEFLATED, 15, 8, zlib.Z_DEFAULT_STRATEGY)
            baseline = baseline_compressor.compress(idat_raw) + baseline_compressor.flush()
            patch_ops = pickle.loads(brotli.decompress(payload["compressed_patch"]))
            idat_concat = _apply_patch(baseline, patch_ops)

        restored_bytes = png_container.rebuild(
            container["prefix_chunks"], container["idat_lengths"], idat_concat, container["suffix_chunks"]
        )

        out_path = os.path.join(work_dir, "restored_" + original_filename)
        with open(out_path, "wb") as f:
            f.write(restored_bytes)

        return RestoreResult(output_path=out_path)
