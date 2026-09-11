"""Video Optimizer strategy adapter - wires video_optimizer.py's ffmpeg/
xdelta3 engine into the Smart Compression pipeline's CompressionStrategy
interface, following the exact same shape as jpeg_xl_lossless.py (a thin
subprocess-backed adapter, not a reimplementation of the engine itself).

Lossless-only (Phase 1): metadata stripping + faststart remux + container
repack via ffmpeg stream copy, bundled with an xdelta3 patch so restore()
reconstructs the EXACT original bytes (see video_optimizer.py's own
docstring on why the bundle+patch approach exists and what it costs/wins).
No lossy re-encoding (H.265/AV1 transcode) happens here.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_RND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_HERE))))
_VIDEO_SRC = os.path.join(_RND_ROOT, "experiments", "video", "src")
if _VIDEO_SRC not in sys.path:
    sys.path.insert(0, _VIDEO_SRC)

import video_optimizer  # noqa: E402
from strategies.base import CompressionStrategy, CompressResult, RestoreResult  # noqa: E402


class VideoOptimizerStrategy(CompressionStrategy):
    name = "video_optimizer"
    applicable_formats = frozenset(video_optimizer.VIDEO_EXTENSIONS)

    def is_available(self):
        return video_optimizer.is_available()

    def compress(self, input_path, work_dir):
        result = video_optimizer.make_reversible_bundle(input_path, work_dir)
        if not result["success"]:
            # Mirrors every other strategy's contract: a candidate that
            # cannot usefully run raises here, which the harness/worker
            # treats as a failed candidate (falls back to baseline), never
            # a crash that could abort the whole archive operation - see
            # archive_manager_smart.add_folder_to_archive_smart's
            # `except Exception: decision = None` around decide_and_prepare.
            raise RuntimeError(f"video_optimizer: {result['reason']}")
        bundle_path = result["bundle_path"]
        return CompressResult(output_path=bundle_path, stored_size=os.path.getsize(bundle_path))

    def restore(self, compressed_path, work_dir, original_filename):
        result = video_optimizer.restore_from_bundle(compressed_path, work_dir)
        if not result["success"]:
            raise RuntimeError(f"video_optimizer restore: {result['reason']}")
        # Renamed to the caller's expected "restored_<original_filename>"
        # naming convention (matches jpeg_xl_lossless.py's restore()).
        final_path = os.path.join(work_dir, "restored_" + original_filename)
        os.replace(result["output_path"], final_path)
        return RestoreResult(output_path=final_path)
