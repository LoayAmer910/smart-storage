"""Video Optimizer - lossless-only video container optimization for the
Smart Compression pipeline (Phase 1: metadata stripping + faststart remux +
container repack via ffmpeg stream copy, never re-encoding).

Every transformation this module performs is a pure container-level
operation (`-c copy`): the actual video/audio stream bytes are copied
through unchanged, only the container's metadata and atom layout change.
This keeps VIDEO_OPTIMIZER's output invertible enough to satisfy the same
byte-exact restore-and-verify gate every other Smart Compression strategy
must pass (see VideoOptimizerStrategy in
experiments/images/src/strategies/video_optimizer_strategy.py, and
smart_compression.py's module docstring on why that gate exists) - it does
NOT do lossy re-encoding (H.265/AV1 transcode, bitrate normalization,
duplicate-frame removal), which would produce a file that is not
byte-identical to the original and therefore cannot pass that gate as
currently built. That is a deliberate scope decision for this phase, not
an oversight - see the project's own docs on the trade-off.

is_available() only ever checks the local filesystem for ffmpeg.exe/
ffprobe.exe - never a network fetch, mirroring every other bundled-tool
strategy (JXL, preflate) in this codebase.
"""

import ctypes
import os
import subprocess
import sys

_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Path resolution mirrors experiments/images/src/strategies/jpeg_xl_lossless.py's
# _TOOLS_DIR: a frozen PyInstaller build extracts bundled data to sys._MEIPASS,
# __file__-relative resolution only works for the dev-tree layout.
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _TOOLS_DIR = os.path.join(sys._MEIPASS, "video", "tools")
    _COMMON_TOOLS_DIR = os.path.join(sys._MEIPASS, "common", "tools")
else:
    _RND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    _COMMON_TOOLS_DIR = os.path.join(_RND_ROOT, "common", "tools")

FFMPEG_PATH = os.path.join(_TOOLS_DIR, "ffmpeg.exe")
FFPROBE_PATH = os.path.join(_TOOLS_DIR, "ffprobe.exe")
# xdelta3.exe lives in the shared experiments/common/tools/ location (not
# video/tools/) since it's used by every reversible-patch strategy, not
# just video (see experiments/text_filters/src/text_prefilter.py).
XDELTA3_PATH = os.path.join(_COMMON_TOOLS_DIR, "xdelta3.exe")

VIDEO_EXTENSIONS = frozenset({"mp4", "mkv", "mov", "avi", "webm", "m4v", "mpg", "mpeg"})

# ffmpeg's +faststart (moov-atom reposition) is a real MP4/MOV "box"-structure
# operation. Matroska/EBML (mkv/webm) and RIFF (avi) have no equivalent
# concept, so +faststart is only ever passed for these extensions.
_FASTSTART_EXTENSIONS = frozenset({"mp4", "mov", "m4v"})

# Above this size a stream-copy remux is still cheap I/O-wise, but temp-file
# disk headroom and single-candidate wall-clock time both grow with it -
# conservative ceiling for Phase 1 pending real measurement on very large
# files. Files above this are cleanly reported as "cannot process" (see
# is_video_too_large) rather than risking a long-running single candidate.
MAX_VIDEO_BYTES_DEFAULT = 8 * 1024 * 1024 * 1024  # 8 GiB

_CLOUD_PLACEHOLDER_MASK = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def is_available():
    """Return (available: bool, reason: str) - filesystem check only, same
    pattern as every other bundled-tool strategy in this codebase (never a
    network fetch)."""
    if not (os.path.isfile(FFMPEG_PATH) and os.path.isfile(FFPROBE_PATH) and os.path.isfile(XDELTA3_PATH)):
        return False, f"ffmpeg.exe/ffprobe.exe/xdelta3.exe not present in {_TOOLS_DIR}"
    return True, ""


def _run_ffprobe(args, file_path, timeout=30):
    try:
        result = subprocess.run(
            [FFPROBE_PATH, "-v", "error", *args, file_path],
            check=False, capture_output=True, creationflags=_NO_WINDOW_FLAGS, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip()


def is_video_extension(file_path):
    extension = os.path.splitext(file_path)[1].lstrip(".").lower()
    return extension in VIDEO_EXTENSIONS


def is_video_too_large(file_path, max_bytes=MAX_VIDEO_BYTES_DEFAULT):
    try:
        return os.path.getsize(file_path) > max_bytes
    except OSError:
        return True  # can't even stat it - treat as unsafe to process


def _is_cloud_placeholder(file_path):
    # Same detection as archive_manager.CloudPlaceholderError, re-implemented
    # locally (not imported) so this module stays import-independent of the
    # rest of the app - it must be safely importable standalone by the
    # strategy harness/benchmarks, same as every other strategies/* module.
    if os.name != "nt":
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(file_path)
    except OSError:
        return False
    if attrs == _INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & _CLOUD_PLACEHOLDER_MASK)


def safe_video_read(file_path):
    """Pre-flight check that a video file can actually be read and
    processed before spending any ffmpeg time on it. Returns
    (ok: bool, reason: str) - covers cloud-only OneDrive placeholders,
    missing/empty/oversized files, and corrupted or unsupported-codec
    files (via a real ffprobe parse, not just a byte-readability check).
    """
    if not os.path.isfile(file_path):
        return False, "File does not exist"

    try:
        size = os.path.getsize(file_path)
    except OSError as exc:
        return False, f"Cannot stat file: {exc}"
    if size == 0:
        return False, "File is empty"
    if is_video_too_large(file_path):
        return False, f"File exceeds the {MAX_VIDEO_BYTES_DEFAULT} byte processing limit"

    if _is_cloud_placeholder(file_path):
        return False, "File is a OneDrive/cloud placeholder not downloaded to this device"

    try:
        with open(file_path, "rb") as f:
            f.read(4096)
    except OSError as exc:
        return False, f"Cannot read file: {exc}"

    available, reason = is_available()
    if not available:
        return False, reason

    # A real, fast probe (not a full decode) that ffmpeg's demuxer can parse
    # the container and find a video stream - catches corrupted files and
    # genuinely unsupported/unknown codecs before any real work is attempted.
    if detect_video_codec(file_path) is None:
        return False, "Not a readable/supported video file (corrupted or unsupported codec)"

    return True, ""


def detect_video_codec(file_path):
    """Returns the first video stream's codec name (e.g. "h264", "hevc",
    "vp9"), or None if the file has no readable video stream (corrupted
    file, unsupported/unknown codec, or not actually a video)."""
    output = _run_ffprobe(
        ["-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0"],
        file_path,
    )
    if not output:
        return None
    return output.splitlines()[0].strip() or None


def _mp4_faststart_already_applied(file_path):
    # Cheap, ffmpeg-free structural check: MP4/MOV "faststart" means the
    # moov atom (metadata/index) appears before the mdat atom (sample data)
    # - scans only top-level box headers, never reads sample data.
    try:
        with open(file_path, "rb") as f:
            file_size = os.fstat(f.fileno()).st_size
            offset = 0
            while offset < file_size:
                f.seek(offset)
                header = f.read(8)
                if len(header) < 8:
                    break
                box_size = int.from_bytes(header[0:4], "big")
                box_type = header[4:8]
                header_len = 8
                if box_size == 1:  # 64-bit extended size follows the header
                    extended = f.read(8)
                    if len(extended) < 8:
                        break
                    box_size = int.from_bytes(extended, "big")
                    header_len = 16
                if box_type == b"moov":
                    return True
                if box_type == b"mdat":
                    return False
                if box_size < header_len:
                    break  # malformed box - bail out, not confirmed optimal
                offset += box_size
    except OSError:
        return False
    return False  # neither atom found in a scannable prefix - not confirmed optimal


def is_video_optimal(file_path):
    """True only when there is nothing left for optimize_video() to
    usefully do: for MP4/MOV, faststart is already applied AND no format-
    level metadata tags are present. Other containers (MKV/WEBM/AVI) have
    no faststart concept in ffmpeg, so they are never reported optimal here
    - optimize_video() is still cheap/safe to run on them (metadata
    stripping only); the Smart Compression benefit/cost policy gate is
    what actually decides whether the result is worth keeping, not this
    heuristic.
    """
    extension = os.path.splitext(file_path)[1].lstrip(".").lower()
    if extension not in _FASTSTART_EXTENSIONS:
        return False
    if not _mp4_faststart_already_applied(file_path):
        return False
    tags = _run_ffprobe(["-show_entries", "format_tags", "-of", "csv=p=0"], file_path)
    return not tags


def optimize_video(file_path, output_path=None, timeout=180):
    """Runs the lossless container-level optimization: metadata stripping
    + faststart remux (MP4/MOV only) + container repack, via ffmpeg stream
    copy (-c copy - the actual video/audio bytes are never re-encoded).

    output_path defaults to "<file_path>.optimized.<ext>" next to the
    source file when not given.

    Returns {"success": bool, "reason": str, "output_path": str|None} -
    never raises for an ordinary ffmpeg failure (corrupted file,
    unsupported codec, timeout); those are reported via "success": False
    so callers can treat this exactly like "candidate not worth it",
    never a crash that could abort the whole archive.
    """
    ok, reason = safe_video_read(file_path)
    if not ok:
        return {"success": False, "reason": reason, "output_path": None}

    extension = os.path.splitext(file_path)[1].lstrip(".").lower()
    if output_path is None:
        output_path = f"{file_path}.optimized.{extension or 'bin'}"

    args = [FFMPEG_PATH, "-y", "-i", file_path, "-c", "copy", "-map_metadata", "-1"]
    if extension in _FASTSTART_EXTENSIONS:
        args += ["-movflags", "+faststart"]
    args.append(output_path)

    try:
        result = subprocess.run(
            args, check=False, capture_output=True, creationflags=_NO_WINDOW_FLAGS, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "reason": f"ffmpeg timed out after {timeout}s", "output_path": None}
    except OSError as exc:
        return {"success": False, "reason": f"Failed to launch ffmpeg: {exc}", "output_path": None}

    if result.returncode != 0 or not os.path.isfile(output_path):
        stderr_tail = result.stderr.decode("utf-8", errors="replace")[-500:] if result.stderr else ""
        return {"success": False, "reason": f"ffmpeg exited {result.returncode}: {stderr_tail}", "output_path": None}

    return {"success": True, "reason": "", "output_path": output_path}


# --- Byte-exact reversible bundling ----------------------------------------
#
# optimize_video()'s output (metadata stripped, faststart remuxed) is NOT
# byte-identical to the original - the Smart Compression pipeline requires
# every accepted strategy's compress()/restore() cycle to reconstruct the
# EXACT original bytes (SHA-256 verified, see smart_compression.py's module
# docstring and policy.py's hard sha256_match gate). To satisfy that without
# lossy re-encoding, the optimized file is bundled together with a binary
# patch (via the already-bundled xdelta3.exe - VCDIFF format) that can turn
# the optimized bytes back into the exact original bytes. Since the
# optimized file is usually near-identical to the original (only metadata/
# atom-layout differs), that patch is typically tiny - real measurement on
# a 1.7MB sample with 100KB of injected metadata: optimized 1,731,168B +
# patch 380B = 1,731,548B vs original 1,831,192B (~5.4% saved), byte-exact
# restore confirmed. On an already-clean file (nothing to strip) the patch
# still costs a few hundred bytes with nothing to gain, so the bundle ends
# up slightly LARGER than the original - policy.py's NOT_SMALLER_THAN_
# BASELINE gate (comparing against baseline_storagearch_zip) rejects that
# case automatically, exactly as intended: this strategy only ever wins
# when there is real stripped metadata to justify the patch overhead.
_BUNDLE_OPTIMIZED_ENTRY = "optimized"
_BUNDLE_PATCH_ENTRY = "patch.vcdiff"


def make_reversible_bundle(file_path, work_dir, timeout=180):
    """Produces a single bundle file (a small zip containing the optimized
    video + an xdelta3 patch back to the original) inside work_dir.

    Returns {"success": bool, "reason": str, "bundle_path": str|None}.
    """
    # ffmpeg infers the output container/muxer from the file extension, so
    # the temp output name must keep the original extension (a bare
    # extension-less name makes ffmpeg fail with "Unable to choose an
    # output format").
    extension = os.path.splitext(file_path)[1] or ".bin"
    optimize_result = optimize_video(
        file_path, output_path=os.path.join(work_dir, "optimized_tmp" + extension), timeout=timeout
    )
    if not optimize_result["success"]:
        return {"success": False, "reason": optimize_result["reason"], "bundle_path": None}

    optimized_path = optimize_result["output_path"]
    patch_path = os.path.join(work_dir, "patch.vcdiff")
    try:
        result = subprocess.run(
            [XDELTA3_PATH, "-f", "-e", "-s", optimized_path, file_path, patch_path],
            check=False, capture_output=True, creationflags=_NO_WINDOW_FLAGS, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "reason": f"xdelta3 encode timed out after {timeout}s", "bundle_path": None}
    except OSError as exc:
        return {"success": False, "reason": f"Failed to launch xdelta3: {exc}", "bundle_path": None}
    if result.returncode != 0 or not os.path.isfile(patch_path):
        stderr_tail = result.stderr.decode("utf-8", errors="replace")[-500:] if result.stderr else ""
        return {"success": False, "reason": f"xdelta3 encode exited {result.returncode}: {stderr_tail}", "bundle_path": None}

    bundle_path = os.path.join(work_dir, "bundle.zip")
    import zipfile
    with zipfile.ZipFile(bundle_path, "w") as bundle:
        # The optimized video entry is stored uncompressed: it's already-
        # compressed media (h264/vp9/etc.), so deflating it again wastes
        # real CPU/time (proportional to full video size) for near-zero
        # gain - baseline_storagearch_zip's own DEFLATE pass over the raw
        # original proves this empirically for any real file where it's
        # not worth it, since policy.evaluate() rejects this candidate
        # whenever baseline wins anyway (NOT_SMALLER_THAN_BASELINE).
        # The patch entry IS deflated: it represents whatever was
        # stripped/reordered (metadata, moov relocation) - genuinely
        # structured/text-like data in real files, and cheap to deflate
        # since patches are typically small regardless of video size.
        bundle.write(optimized_path, _BUNDLE_OPTIMIZED_ENTRY, compress_type=zipfile.ZIP_STORED)
        bundle.write(patch_path, _BUNDLE_PATCH_ENTRY, compress_type=zipfile.ZIP_DEFLATED)

    return {"success": True, "reason": "", "bundle_path": bundle_path}


def restore_from_bundle(bundle_path, work_dir, timeout=180):
    """Reverses make_reversible_bundle(): unpacks the bundle and applies
    the xdelta3 patch to reconstruct the exact original bytes.

    Returns {"success": bool, "reason": str, "output_path": str|None}.
    """
    import zipfile
    optimized_path = os.path.join(work_dir, "optimized_from_bundle")
    patch_path = os.path.join(work_dir, "patch_from_bundle.vcdiff")
    try:
        with zipfile.ZipFile(bundle_path, "r") as bundle:
            with bundle.open(_BUNDLE_OPTIMIZED_ENTRY) as src, open(optimized_path, "wb") as dst:
                dst.write(src.read())
            with bundle.open(_BUNDLE_PATCH_ENTRY) as src, open(patch_path, "wb") as dst:
                dst.write(src.read())
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        return {"success": False, "reason": f"Cannot read bundle: {exc}", "output_path": None}

    restored_path = os.path.join(work_dir, "restored")
    try:
        result = subprocess.run(
            [XDELTA3_PATH, "-f", "-d", "-s", optimized_path, patch_path, restored_path],
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
