"""Reversible text pre-filters for the Smart Compression pipeline.

Two DIFFERENT reversibility mechanisms live here, chosen per-format based
on real, measured results (not assumption) - see each function group's own
docstring for the numbers:

- JSON/XML: canonicalize (whitespace strip + JSON key sort / XML C14N) +
  zstd-compress + xdelta3 binary patch back to the original. Implemented
  (canonicalize_json/canonicalize_xml, make_reversible_bundle/
  restore_from_bundle) but NOT routed by strategy_registry.py - measured
  directly against realistic sample files and consistently LOST to plain
  generic_zstd, because the whitespace these formats strip is exactly the
  redundancy zstd already compresses for free; stripping it first only
  adds a patch cost for zero net gain. Kept registered (harmless, never
  selected against a losing benefit/cost check anyway) rather than
  deleted, in case a future format/dataset actually benefits.

- CSV: row-sorting for better compression locality (measured: ~6% smaller
  before patch cost), but a full row reorder breaks generic binary
  diffing's locality assumption (nearly every row's neighbors changed),
  making an xdelta3 patch cost about as much as the gain. csv_prefilter
  therefore uses its OWN bundle format (make_csv_reversible_bundle/
  restore_csv_reversible_bundle) storing an explicit integer permutation
  array instead of a binary diff - the sort is undone by direct index
  lookup, not by diffing, so it stays cheap regardless of how much row
  order changed.

Canonicalization/sorting only needs to be SOME deterministic transform
that tends to produce a smaller/more-compressible intermediate form - it
does not need to be semantically faithful on its own, because the patch
mechanism (not the transform logic) is what guarantees exact byte-for-byte
restoration of the original file.
"""

import csv
import io
import json
import os
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

import zstandard

_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Path resolution mirrors video_optimizer.py's _COMMON_TOOLS_DIR handling -
# a frozen PyInstaller build extracts bundled data to sys._MEIPASS.
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _COMMON_TOOLS_DIR = os.path.join(sys._MEIPASS, "common", "tools")
else:
    _RND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _COMMON_TOOLS_DIR = os.path.join(_RND_ROOT, "common", "tools")

XDELTA3_PATH = os.path.join(_COMMON_TOOLS_DIR, "xdelta3.exe")

# Canonical text form is typically well under a few MB even for large real
# JSON/CSV/XML files - level 19 matches adaptive_zstd.py's own small-file
# tier (see that module's _LEVEL_TIERS calibration notes). A dedicated
# size-adaptive tier can be added later if real large-file timing data
# shows level 19 is too slow for this format's typical size range.
_ZSTD_LEVEL = 19

_BUNDLE_COMPRESSED_ENTRY = "canonical.zst"
_BUNDLE_PATCH_ENTRY = "patch.vcdiff"

# Conservative ceiling for Phase 1, pending real large-file measurement -
# mirrors video_optimizer.MAX_VIDEO_BYTES_DEFAULT's reasoning.
MAX_TEXT_BYTES_DEFAULT = 512 * 1024 * 1024  # 512 MiB

_CLOUD_PLACEHOLDER_MASK = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def is_available():
    """Filesystem check only, never a network fetch - same pattern as
    every other bundled-tool strategy in this codebase."""
    if not os.path.isfile(XDELTA3_PATH):
        return False, f"xdelta3.exe not present in {_COMMON_TOOLS_DIR}"
    return True, ""


def canonicalize_json(original_bytes):
    obj = json.loads(original_bytes.decode("utf-8"))
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonicalize_csv(original_bytes):
    text = original_bytes.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return original_bytes
    # Header row (first row) kept in place; only data rows are sorted -
    # a stable, deterministic full-row lexicographic sort, since a generic
    # pre-filter has no domain knowledge of which column is a real "key".
    header, data_rows = rows[0], rows[1:]
    data_rows.sort()
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(data_rows)
    return output.getvalue().encode("utf-8")


def canonicalize_xml(original_bytes):
    text = original_bytes.decode("utf-8")
    # xml.etree.ElementTree.canonicalize() implements W3C C14N - a
    # well-defined, well-tested canonical form (sorted attributes,
    # normalized whitespace/namespaces) rather than a hand-rolled
    # attribute-sort that would need its own correctness proof.
    return ET.canonicalize(xml_data=text).encode("utf-8")


_CANONICALIZERS = {
    "json": canonicalize_json,
    "csv": canonicalize_csv,
    "xml": canonicalize_xml,
}

ALL_TEXT_EXTENSIONS = frozenset(_CANONICALIZERS)


def is_text_too_large(file_path, max_bytes=MAX_TEXT_BYTES_DEFAULT):
    try:
        return os.path.getsize(file_path) > max_bytes
    except OSError:
        return True


def _is_cloud_placeholder(file_path):
    # Same detection as archive_manager.CloudPlaceholderError /
    # video_optimizer._is_cloud_placeholder, re-implemented locally so this
    # module stays import-independent of the rest of the app.
    if os.name != "nt":
        return False
    import ctypes
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(file_path)
    except OSError:
        return False
    if attrs == _INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & _CLOUD_PLACEHOLDER_MASK)


def safe_text_read(file_path, extension):
    """Pre-flight check mirroring video_optimizer.safe_video_read: cloud
    placeholders, missing/empty/oversized files, and content that doesn't
    actually parse as the claimed format are all reported as
    (ok: bool, reason: str) rather than raised, so callers can decline
    cleanly instead of crashing.
    """
    if not os.path.isfile(file_path):
        return False, "File does not exist"
    try:
        size = os.path.getsize(file_path)
    except OSError as exc:
        return False, f"Cannot stat file: {exc}"
    if size == 0:
        return False, "File is empty"
    if is_text_too_large(file_path):
        return False, f"File exceeds the {MAX_TEXT_BYTES_DEFAULT} byte processing limit"
    if _is_cloud_placeholder(file_path):
        return False, "File is a OneDrive/cloud placeholder not downloaded to this device"

    canonicalizer = _CANONICALIZERS.get(extension)
    if canonicalizer is None:
        return False, f"No canonicalizer for extension '{extension}'"

    try:
        with open(file_path, "rb") as f:
            original_bytes = f.read()
    except OSError as exc:
        return False, f"Cannot read file: {exc}"

    try:
        canonicalizer(original_bytes)
    except Exception as exc:  # noqa: BLE001 - any parse failure means "not usable", never a crash
        return False, f"Not valid {extension.upper()} (or unsupported structure): {exc}"

    available, reason = is_available()
    if not available:
        return False, reason

    return True, ""


def make_reversible_bundle(file_path, work_dir, timeout=60):
    """Produces a single bundle file (a small zip containing the zstd-
    compressed canonical form + an xdelta3 patch back to the original)
    inside work_dir.

    Returns {"success": bool, "reason": str, "bundle_path": str|None}.
    """
    extension = os.path.splitext(file_path)[1].lstrip(".").lower()
    ok, reason = safe_text_read(file_path, extension)
    if not ok:
        return {"success": False, "reason": reason, "bundle_path": None}

    with open(file_path, "rb") as f:
        original_bytes = f.read()
    canonical_bytes = _CANONICALIZERS[extension](original_bytes)

    canonical_path = os.path.join(work_dir, "canonical.tmp")
    with open(canonical_path, "wb") as f:
        f.write(canonical_bytes)

    patch_path = os.path.join(work_dir, "patch.vcdiff")
    try:
        result = subprocess.run(
            [XDELTA3_PATH, "-f", "-e", "-s", canonical_path, file_path, patch_path],
            check=False, capture_output=True, creationflags=_NO_WINDOW_FLAGS, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "reason": f"xdelta3 encode timed out after {timeout}s", "bundle_path": None}
    except OSError as exc:
        return {"success": False, "reason": f"Failed to launch xdelta3: {exc}", "bundle_path": None}
    if result.returncode != 0 or not os.path.isfile(patch_path):
        stderr_tail = result.stderr.decode("utf-8", errors="replace")[-500:] if result.stderr else ""
        return {"success": False, "reason": f"xdelta3 encode exited {result.returncode}: {stderr_tail}", "bundle_path": None}

    compressed_canonical = zstandard.ZstdCompressor(level=_ZSTD_LEVEL).compress(canonical_bytes)

    bundle_path = os.path.join(work_dir, "bundle.zip")
    with zipfile.ZipFile(bundle_path, "w") as bundle:
        # canonical.zst is already zstd-compressed - store, don't re-deflate
        # (real CPU cost for near-zero gain, same reasoning as video's
        # already-compressed payload entry). The patch is deflated: it's
        # small and typically has real redundancy (repeated whitespace/
        # ordering tokens), cheap to deflate regardless of source file size.
        bundle.writestr(zipfile.ZipInfo(_BUNDLE_COMPRESSED_ENTRY), compressed_canonical, zipfile.ZIP_STORED)
        bundle.write(patch_path, _BUNDLE_PATCH_ENTRY, compress_type=zipfile.ZIP_DEFLATED)

    return {"success": True, "reason": "", "bundle_path": bundle_path}


def restore_from_bundle(bundle_path, work_dir, timeout=60):
    """Reverses make_reversible_bundle(): unpacks the bundle, decompresses
    the canonical form, and applies the xdelta3 patch to reconstruct the
    exact original bytes.

    Returns {"success": bool, "reason": str, "output_path": str|None}.
    """
    try:
        with zipfile.ZipFile(bundle_path, "r") as bundle:
            compressed_canonical = bundle.read(_BUNDLE_COMPRESSED_ENTRY)
            patch_bytes = bundle.read(_BUNDLE_PATCH_ENTRY)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        return {"success": False, "reason": f"Cannot read bundle: {exc}", "output_path": None}

    try:
        canonical_bytes = zstandard.ZstdDecompressor().decompress(compressed_canonical)
    except zstandard.ZstdError as exc:
        return {"success": False, "reason": f"Cannot decompress canonical form: {exc}", "output_path": None}

    canonical_path = os.path.join(work_dir, "canonical_from_bundle.tmp")
    patch_path = os.path.join(work_dir, "patch_from_bundle.vcdiff")
    with open(canonical_path, "wb") as f:
        f.write(canonical_bytes)
    with open(patch_path, "wb") as f:
        f.write(patch_bytes)

    restored_path = os.path.join(work_dir, "restored")
    try:
        result = subprocess.run(
            [XDELTA3_PATH, "-f", "-d", "-s", canonical_path, patch_path, restored_path],
            check=False, capture_output=True, creationflags=_NO_WINDOW_FLAGS, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "reason": f"xdelta3 decode timed out after {timeout}s", "output_path": None}
    except OSError as exc:
        return {"success": False, "reason": f"Failed to launch xdelta3: {exc}", "output_path": None}
    if result.returncode != 0 or not os.path.isfile(restored_path):
        stderr_tail = result.stderr.decode("utf-8", errors="replace")[-500:] if result.stderr else ""
        return {"success": False, "reason": f"xdelta3 decode exited {result.returncode}: {stderr_tail}", "output_path": None}

    return {"success": True, "reason": "", "output_path": restored_path}


# --- CSV: permutation-based reversibility (no xdelta3) ---------------------
#
# Rows are treated as opaque line-delimited byte strings, never actually
# parsed as CSV fields (no dialect/quoting assumptions) - this makes the
# split/rejoin an EXACT inverse by construction for any input, regardless
# of CSV dialect or embedded delimiters, and makes the technique correct
# even on a "'.csv' file" that isn't really valid CSV: it degrades to
# "sort these lines, remember how to unsort them," which is always
# reversible. The header (first record) is never moved.
_CSV_BUNDLE_CANONICAL_ENTRY = "canonical.zst"
_CSV_BUNDLE_PERM_ENTRY = "perm.zst"
_CSV_BUNDLE_META_ENTRY = "meta.json"


def _detect_line_terminator(data):
    if b"\r\n" in data:
        return b"\r\n"
    if b"\r" in data and b"\n" not in data:
        return b"\r"
    return b"\n"


def _split_records(data, terminator):
    has_trailing = len(data) > 0 and data.endswith(terminator)
    body = data[: -len(terminator)] if has_trailing else data
    records = body.split(terminator) if body else []
    return records, has_trailing


def _encode_permutation(order):
    return struct.pack(f"<{len(order)}I", *order)


def _decode_permutation(data, count):
    return list(struct.unpack(f"<{count}I", data)) if count else []


def safe_csv_read(file_path):
    """CSV-specific pre-flight check: cloud placeholder, missing/empty/
    oversized file, valid UTF-8 text, and at least 2 lines (a header plus
    one row) to be worth sorting - anything else declines cleanly rather
    than raising. Does NOT require the content to be well-formed CSV (see
    module note above)."""
    if not os.path.isfile(file_path):
        return False, "File does not exist"
    try:
        size = os.path.getsize(file_path)
    except OSError as exc:
        return False, f"Cannot stat file: {exc}"
    if size == 0:
        return False, "File is empty"
    if is_text_too_large(file_path):
        return False, f"File exceeds the {MAX_TEXT_BYTES_DEFAULT} byte processing limit"
    if _is_cloud_placeholder(file_path):
        return False, "File is a OneDrive/cloud placeholder not downloaded to this device"

    try:
        with open(file_path, "rb") as f:
            original_bytes = f.read()
    except OSError as exc:
        return False, f"Cannot read file: {exc}"

    try:
        original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return False, f"Not valid UTF-8 text: {exc}"

    terminator = _detect_line_terminator(original_bytes)
    records, _has_trailing = _split_records(original_bytes, terminator)
    if len(records) < 2:
        return False, "Not enough rows to benefit from sorting"

    available, reason = _xdelta3_or_zstd_available()
    if not available:
        return False, reason

    return True, ""


def _xdelta3_or_zstd_available():
    # csv_prefilter doesn't use xdelta3 at all (see module docstring) -
    # its only real dependency is the zstandard package, which is a hard
    # top-level import above (if it were missing, this module would have
    # already failed to import, so reaching here means it's present).
    return True, ""


def make_csv_reversible_bundle(file_path, work_dir):
    """Sorts CSV data rows (header excluded) for better compression
    locality, storing the sorted+compressed rows alongside a compressed
    integer permutation array (not a binary diff) so the original row
    order can be reconstructed by direct index lookup.

    Returns {"success": bool, "reason": str, "bundle_path": str|None}.
    """
    ok, reason = safe_csv_read(file_path)
    if not ok:
        return {"success": False, "reason": reason, "bundle_path": None}

    with open(file_path, "rb") as f:
        original_bytes = f.read()

    terminator = _detect_line_terminator(original_bytes)
    records, has_trailing = _split_records(original_bytes, terminator)
    header, data_records = records[0], records[1:]
    count = len(data_records)

    # order[j] = original index of the record now at sorted position j
    # (a stable sort - equal rows keep their original relative order,
    # which matters for real CSVs with many repeated/low-cardinality
    # values: it keeps the permutation itself more locally structured,
    # not just the sorted rows).
    order = sorted(range(count), key=lambda i: data_records[i])
    sorted_records = [data_records[i] for i in order]

    canonical_body = terminator.join([header] + sorted_records)
    if has_trailing:
        canonical_body += terminator

    compressed_canonical = zstandard.ZstdCompressor(level=_ZSTD_LEVEL).compress(canonical_body)
    compressed_perm = zstandard.ZstdCompressor(level=_ZSTD_LEVEL).compress(_encode_permutation(order))

    meta = json.dumps({
        "terminator": terminator.decode("latin-1"),
        "trailing": has_trailing,
        "row_count": count,
    }).encode("utf-8")

    bundle_path = os.path.join(work_dir, "csv_bundle.zip")
    with zipfile.ZipFile(bundle_path, "w") as bundle:
        # Both zst-compressed payloads are stored (not re-deflated) - the
        # meta record is tiny and deflated for negligible cost/benefit.
        bundle.writestr(zipfile.ZipInfo(_CSV_BUNDLE_CANONICAL_ENTRY), compressed_canonical, zipfile.ZIP_STORED)
        bundle.writestr(zipfile.ZipInfo(_CSV_BUNDLE_PERM_ENTRY), compressed_perm, zipfile.ZIP_STORED)
        bundle.writestr(_CSV_BUNDLE_META_ENTRY, meta, zipfile.ZIP_DEFLATED)

    return {"success": True, "reason": "", "bundle_path": bundle_path}


def restore_csv_reversible_bundle(bundle_path, work_dir):
    """Reverses make_csv_reversible_bundle(): decompresses the sorted rows
    and permutation array, then scatters each row back to its original
    index - exact by construction, no diffing involved.

    Returns {"success": bool, "reason": str, "output_path": str|None}.
    """
    try:
        with zipfile.ZipFile(bundle_path, "r") as bundle:
            compressed_canonical = bundle.read(_CSV_BUNDLE_CANONICAL_ENTRY)
            compressed_perm = bundle.read(_CSV_BUNDLE_PERM_ENTRY)
            meta = json.loads(bundle.read(_CSV_BUNDLE_META_ENTRY).decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, OSError, json.JSONDecodeError) as exc:
        return {"success": False, "reason": f"Cannot read bundle: {exc}", "output_path": None}

    terminator = meta["terminator"].encode("latin-1")
    has_trailing = meta["trailing"]
    count = meta["row_count"]

    try:
        canonical_body = zstandard.ZstdDecompressor().decompress(compressed_canonical)
        perm_bytes = zstandard.ZstdDecompressor().decompress(compressed_perm)
    except zstandard.ZstdError as exc:
        return {"success": False, "reason": f"Cannot decompress bundle payload: {exc}", "output_path": None}
    order = _decode_permutation(perm_bytes, count)

    body = canonical_body[: -len(terminator)] if has_trailing else canonical_body
    records = body.split(terminator)
    header, sorted_records = records[0], records[1:]

    original_data_records = [None] * count
    for sorted_pos, original_index in enumerate(order):
        original_data_records[original_index] = sorted_records[sorted_pos]

    restored_body = terminator.join([header] + original_data_records)
    if has_trailing:
        restored_body += terminator

    restored_path = os.path.join(work_dir, "restored")
    with open(restored_path, "wb") as f:
        f.write(restored_body)

    return {"success": True, "reason": "", "output_path": restored_path}
