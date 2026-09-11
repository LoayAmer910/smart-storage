"""Beta12 Priority: a faster long-distance-matching zstd variant for text,
added after a real full-corpus run showed beta11_src's ZstdLongRangeStrategy
(hardcoded zstd level 19 + LDM) was the SECOND real-volume bottleneck this
round (after the PNG/generic_brotli one - see integration_layer.py's own
HOTFIX comment for that one). Confirmed via per-file heartbeat logging
across three real runs: after fixing PNG, csv/log files (dataset_records_*,
application_trace_*) became the new dominant time cost, each candidate
taking real tens of seconds at level 19's expensive optimal-parse match
finding.

Beta9's OWN real adaptive_zstd.py already documents and validates the
exact same fix for this exact problem: level 19 on a real 41.7MB file took
>10s (sometimes >30s under contention) while level 12 took only 1.97s for
only a small compression-ratio cost (6.675MB vs slightly smaller at 19) -
see that module's own real measurements. This module applies the SAME
already-proven fix to the long-range strategy: level 12 instead of 19,
keeping long-distance matching (window_log=27) since THAT is what
actually helps repetitive text content, independent of the base level.

A NEW strategy name (zstd_long_range_v2), not a replacement of
beta11_src's own ZstdLongRangeStrategy (which is left completely
unedited, still registered and still real/tested at its own name) - only
Beta12's OWN routing table points to this faster variant instead.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_IMAGES_SRC = os.path.join(_REPO_ROOT, "beta9_src", "experiments", "images", "src")
if _IMAGES_SRC not in sys.path:
    sys.path.insert(0, _IMAGES_SRC)

from strategies.base import CompressionStrategy, CompressResult, RestoreResult  # noqa: E402 - real Beta9 interface, read-only use

try:
    import zstandard
except ImportError:  # pragma: no cover - already a real Beta9 runtime dep
    zstandard = None


class ZstdLongRangeFastStrategy(CompressionStrategy):
    name = "zstd_long_range_v2"
    applicable_formats = frozenset({"txt", "log", "csv", "md", "html"})

    LEVEL = 12  # was 19 in beta11_src's ZstdLongRangeStrategy - see module docstring
    WINDOW_LOG = 27  # unchanged - long-range matching itself is the real win for repetitive text

    def is_available(self):
        if zstandard is None:
            return False, "zstandard package not installed in this environment"
        return True, ""

    def compress(self, input_path, work_dir):
        arcname = os.path.basename(input_path)
        out_path = os.path.join(work_dir, arcname + ".zstdlrv2")
        with open(input_path, "rb") as f:
            data = f.read()
        params = zstandard.ZstdCompressionParameters.from_level(
            self.LEVEL, enable_ldm=True, window_log=self.WINDOW_LOG,
        )
        compressor = zstandard.ZstdCompressor(compression_params=params)
        encoded = compressor.compress(data)
        with open(out_path, "wb") as f:
            f.write(encoded)
        return CompressResult(output_path=out_path, stored_size=os.path.getsize(out_path))

    def restore(self, compressed_path, work_dir, original_filename):
        out_path = os.path.join(work_dir, "restored_" + original_filename)
        with open(compressed_path, "rb") as f:
            encoded = f.read()
        decoded = zstandard.ZstdDecompressor().decompress(encoded)
        with open(out_path, "wb") as f:
            f.write(decoded)
        return RestoreResult(output_path=out_path)
