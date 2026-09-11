"""Size-adaptive zstd strategy - the Phase 2 fix for the .ipch timeout issue.

Phase 1's generic_zstd (in experiments/images/src/strategies/generic_byte.py)
hardcodes zstd level 19 for every file. That's fine up to a few MB, but
Phase 2's mixed-corpus run showed it repeatedly hitting the 30s real-kill
cap on ~40MB Visual Studio .ipch precompiled-header files - level 19's
expensive match-finding does not scale to that size on this content.

This does NOT modify the Phase 1 strategy (other formats/phases keep using
it unchanged) - it's a separate class, swapped in only for Phase 2's
"generic_zstd" registry slot, that picks a level from the input file's
actual size before compressing. Small files keep the same level 19 that
already worked well; large files drop to a much faster level so they
complete quickly and let the existing benefit/cost policy evaluate them
properly instead of being killed every time.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "images", "src"))

import zstandard  # noqa: E402
from strategies.generic_byte import _GenericByteStrategy  # noqa: E402

# Thresholds and levels are a small, practical calibration measured
# directly against real 40-45MB .ipch files from this corpus (the actual
# files that were timing out), not a blind guess or a search:
#   level 19 on a 41.7MB .ipch: >10s, sometimes >30s (the original problem)
#   level 3  on the same file:  fast, but compresses far worse than
#                                baseline_zip - policy correctly rejects it,
#                                so files that USED to get real savings now
#                                get none (a regression caught by re-testing
#                                the affected subset)
#   level 9  on the same file:  0.99s,  6,726,085 bytes
#   level 12 on the same file:  1.97s,  6,675,506 bytes  <- chosen
#   level 15 on the same file:  6.93s,  6,617,611 bytes
# Level 12 keeps compression close to level 19's quality while finishing in
# ~2s even on the largest files seen (45MB) - comfortably inside the 30s
# cap with wide margin, so there is no need for a third, more aggressive
# tier for anything in this corpus's actual size range.
_LEVEL_TIERS = [
    (4 * 1024 * 1024, 19),
    (float("inf"), 12),
]


def select_level(size_bytes):
    for threshold, level in _LEVEL_TIERS:
        if size_bytes < threshold:
            return level
    return _LEVEL_TIERS[-1][1]


class AdaptiveZstdStrategy(_GenericByteStrategy):
    name = "generic_zstd"  # same name as Phase 1's - this is a drop-in replacement, not a new candidate
    extension = ".zst"

    def compress(self, input_path, work_dir):
        self._current_level = select_level(os.path.getsize(input_path))
        return super().compress(input_path, work_dir)

    def _encode(self, data):
        level = getattr(self, "_current_level", 19)
        # threads=2 ONLY above 20MB - deliberately higher than the 4MB
        # level tier above. Real measurement (2026-08-26): gating threads
        # at the SAME 4MB tier as level (tried twice) was a net loss on a
        # folder dominated by several-MB files (JSON ~7.9MB etc.) - thread
        # setup/sync overhead cost more than it saved for files that size.
        # A real 3.2GB corpus mixing ~40MB Visual Studio .ipch/browse-db
        # files (which this run showed taking 58-72s each under real
        # 8-worker-pool contention, ~30x the isolated single-file
        # calibration in this module's own docstring) with the same
        # several-MB "other" files lets 20MB separate the two cleanly:
        # files big enough to actually have data worth splitting across
        # threads get threads=2, the several-MB files that already
        # regressed at a lower threshold are left untouched.
        threads = 2 if len(data) >= (20 * 1024 * 1024) else 0
        return zstandard.ZstdCompressor(level=level, threads=threads).compress(data)

    def _decode(self, data):
        return zstandard.ZstdDecompressor().decompress(data)
