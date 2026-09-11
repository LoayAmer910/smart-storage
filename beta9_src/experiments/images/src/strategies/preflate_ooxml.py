"""Beta 9 candidate: Preflate-assisted recompression for OOXML/ZIP
containers (currently routed only for .xlsx, per the Stage Gate evidence
in beta9_research/logs/STAGE_GATE_RESULTS.md section A - DOCX/PPTX/ZIP/GZ
in the real test corpus are dominated by already-compressed embedded
media where this gives ~0% net benefit, so routing is scoped to xlsx
until broader evidence justifies more).

For each real DEFLATE member above a minimum size, this:
  1. locates that member's exact raw compressed byte range within the
     original file (no re-parsing/rewriting of the zip structure)
  2. runs preflate_tool.exe "analyze" on it - DEFLATE -> plaintext +
     corrections, self-verified inside the tool (see preflate_tool's own
     round-trip check) before it ever reports success
  3. recompresses the plaintext with the existing size-adaptive Zstd
     policy (same select_level as generic_zstd - no new compression
     policy invented here)
  4. stores everything needed to reconstruct the ORIGINAL FILE BYTES
     exactly: the untouched "skeleton" bytes around each replaced member,
     plus each member's (zstd-compressed plaintext, corrections)

Any member preflate_tool can't safely round-trip is left untouched in the
skeleton (Preservation Rule: never guess, always fall back). If nothing
in a file is replaceable, this strategy's output is ~the original size
plus a tiny header, so the existing benefit/cost policy naturally picks a
cheaper candidate instead - this strategy never needs its own internal
"is this worth it" logic beyond what the shared policy already does.
"""

import json
import os
import struct
import subprocess
import sys
import zipfile

from strategies.base import CompressionStrategy, CompressResult, RestoreResult

# adaptive_zstd.py lives in experiments/smart_selector/src, a sibling tree
# to this one (experiments/images/src/strategies) - selector.py puts both
# on sys.path already when running for real, but this module must also
# resolve standalone (e.g. under direct import in tests), so add it here too.
_RND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
_SMART_SELECTOR_SRC = os.path.join(_RND_ROOT, "smart_selector", "src")
if _SMART_SELECTOR_SRC not in sys.path:
    sys.path.insert(0, _SMART_SELECTOR_SRC)

import zstandard  # noqa: E402
from adaptive_zstd import select_level  # noqa: E402

_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MIN_MEMBER_SIZE = 64 * 1024  # below this, preflate's fixed per-member overhead isn't worth it

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _TOOLS_DIR = os.path.join(sys._MEIPASS, "preflate", "tools")
else:
    _TOOLS_DIR = os.path.join(_RND_ROOT, "preflate", "tools")
_TOOL_PATH = os.path.join(_TOOLS_DIR, "preflate_tool.exe")

_MAGIC = b"PFZO1\0"


def _local_header_data_offset(fileobj, header_offset):
    fileobj.seek(header_offset)
    header = fileobj.read(30)
    if header[:4] != b"PK\x03\x04":
        return None
    name_len, extra_len = struct.unpack("<HH", header[26:30])
    return header_offset + 30 + name_len + extra_len


class PreflateZstdOoxmlStrategy(CompressionStrategy):
    name = "preflate_zstd_ooxml"
    applicable_formats = frozenset({"xlsx"})

    def is_available(self):
        if not os.path.isfile(_TOOL_PATH):
            return False, "preflate_tool.exe not present in experiments/preflate/tools/"
        return True, ""

    def compress(self, input_path, work_dir):
        with open(input_path, "rb") as f:
            original = f.read()

        try:
            zf = zipfile.ZipFile(input_path)
            candidates = [
                i for i in zf.infolist()
                if i.compress_type == zipfile.ZIP_DEFLATED and i.compress_size >= _MIN_MEMBER_SIZE
            ]
        except Exception:
            candidates = []

        members = []  # (orig_offset, orig_len, zstd_blob, corr_blob)
        with open(input_path, "rb") as fobj:
            for info in candidates:
                data_offset = _local_header_data_offset(fobj, info.header_offset)
                if data_offset is None:
                    continue
                raw = original[data_offset:data_offset + info.compress_size]
                if len(raw) != info.compress_size:
                    continue

                in_path = os.path.join(work_dir, f"_pf_in_{data_offset}.deflate")
                plain_path = os.path.join(work_dir, f"_pf_plain_{data_offset}.bin")
                corr_path = os.path.join(work_dir, f"_pf_corr_{data_offset}.bin")
                with open(in_path, "wb") as f:
                    f.write(raw)

                proc = subprocess.run(
                    [_TOOL_PATH, "analyze", in_path, plain_path, corr_path],
                    capture_output=True, creationflags=_NO_WINDOW_FLAGS,
                )
                if proc.returncode != 0:
                    continue  # not safely round-trippable - leave in skeleton untouched

                plaintext = open(plain_path, "rb").read()
                corrections = open(corr_path, "rb").read()
                level = select_level(len(plaintext))
                zstd_blob = zstandard.ZstdCompressor(level=level).compress(plaintext)

                # only keep the replacement if it's actually smaller
                if len(zstd_blob) + len(corrections) < info.compress_size:
                    members.append((data_offset, info.compress_size, zstd_blob, corrections))

        members.sort(key=lambda m: m[0])

        # build skeleton: original bytes with each replaced range removed,
        # preserving everything else (zip structure, other members) verbatim
        skeleton = bytearray()
        cursor = 0
        member_meta = []
        for offset, length, zstd_blob, corr_blob in members:
            skeleton += original[cursor:offset]
            member_meta.append(dict(orig_offset=offset, orig_len=length,
                                     zstd_len=len(zstd_blob), corr_len=len(corr_blob)))
            cursor = offset + length
        skeleton += original[cursor:]

        header = json.dumps(dict(skeleton_size=len(skeleton), members=member_meta)).encode("utf-8")

        out_path = os.path.join(work_dir, os.path.basename(input_path) + ".pfzo")
        with open(out_path, "wb") as f:
            f.write(_MAGIC)
            f.write(struct.pack("<I", len(header)))
            f.write(header)
            f.write(bytes(skeleton))
            for _, _, zstd_blob, corr_blob in members:
                f.write(zstd_blob)
                f.write(corr_blob)

        return CompressResult(output_path=out_path, stored_size=os.path.getsize(out_path))

    def restore(self, compressed_path, work_dir, original_filename):
        with open(compressed_path, "rb") as f:
            data = f.read()

        if data[:len(_MAGIC)] != _MAGIC:
            raise ValueError("bad preflate_zstd_ooxml container magic")
        pos = len(_MAGIC)
        (header_len,) = struct.unpack("<I", data[pos:pos + 4])
        pos += 4
        header = json.loads(data[pos:pos + header_len])
        pos += header_len

        skeleton_size = header["skeleton_size"]
        skeleton = data[pos:pos + skeleton_size]
        pos += skeleton_size

        recreated_members = []
        for m in header["members"]:
            zstd_blob = data[pos:pos + m["zstd_len"]]
            pos += m["zstd_len"]
            corr_blob = data[pos:pos + m["corr_len"]]
            pos += m["corr_len"]

            plaintext = zstandard.ZstdDecompressor().decompress(zstd_blob)
            plain_path = os.path.join(work_dir, f"_pf_r_plain_{m['orig_offset']}.bin")
            corr_path = os.path.join(work_dir, f"_pf_r_corr_{m['orig_offset']}.bin")
            deflate_path = os.path.join(work_dir, f"_pf_r_deflate_{m['orig_offset']}.bin")
            with open(plain_path, "wb") as f:
                f.write(plaintext)
            with open(corr_path, "wb") as f:
                f.write(corr_blob)

            proc = subprocess.run(
                [_TOOL_PATH, "recreate", plain_path, corr_path, deflate_path],
                capture_output=True, creationflags=_NO_WINDOW_FLAGS,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"preflate_tool recreate failed for member at {m['orig_offset']}: "
                                    f"{proc.stderr.decode(errors='replace')}")
            recreated = open(deflate_path, "rb").read()
            if len(recreated) != m["orig_len"]:
                raise RuntimeError("recreated member length mismatch")
            recreated_members.append((m["orig_offset"], recreated))

        # reassemble: walk skeleton segments interleaved with recreated members,
        # in original-file order
        out = bytearray()
        skel_cursor = 0
        prev_orig_end = 0
        for orig_offset, recreated in recreated_members:
            seg_len = orig_offset - prev_orig_end
            out += skeleton[skel_cursor:skel_cursor + seg_len]
            skel_cursor += seg_len
            out += recreated
            prev_orig_end = orig_offset + len(recreated)
        out += skeleton[skel_cursor:]

        out_path = os.path.join(work_dir, "restored_" + original_filename)
        with open(out_path, "wb") as f:
            f.write(out)
        return RestoreResult(output_path=out_path)
