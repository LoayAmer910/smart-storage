"""Beta12 Priority: the seam that composes Beta9 + Beta12 without mutating
Beta9, Beta10, or Beta11 (all reused as libraries here, never edited).

Reuses beta11_src's exactly-2-candidate routing table, new strategies
(zstd_long_range/brotli_q9/zstd_15), compression cache, cost model,
eligibility, and candidate escalation UNCHANGED. The ONE real change this
round: the admission gate that decides whether a HEAVY candidate is
admitted is now `resource_admission_v4.AdmissionGateV4` (free-cores model)
instead of Beta11's `resource_admission_v3.AdmissionGateV3` (CPU% model) -
see resource_admission_v4.py's own docstring for why the real Beta11
corpus run showed the CPU% model starving heavy candidates under
legitimate high-concurrency folder-wide dispatch.

activate_beta12() patches the SAME five Beta9 module functions Beta11
patches (strategy_registry.get_candidate_strategies, selector.select,
smart_compression.decide_and_prepare, smart_compression._run_candidates,
policy.DEFAULT_POLICY) PLUS one new one this round:
smart_compression.SmartWorkerPool.acquire/release (see
_patch_pool_acquire_release()'s own docstring for why - the free-cores
admission model needs a REAL measurement of concurrent compute, and the
first version of this module measured the wrong thing).

Reimplemented here rather than delegating to
beta11_src.integration_layer.activate_beta11(), because the two must never
both be active at once (they'd double-patch the same Beta9 functions) and
because the admission-gate/tracker wiring differs. beta11_src's OWN
routing tables/strategy classes ARE imported and reused directly, just
composed through this module's own activation instead of beta11's.
"""

import copy
import dataclasses
import os
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import zstandard

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_BETA9_SRC = os.path.join(_REPO_ROOT, "beta9_src")
_SMART_SELECTOR_SRC = os.path.join(_BETA9_SRC, "experiments", "smart_selector", "src")
_IMAGES_SRC = os.path.join(_BETA9_SRC, "experiments", "images", "src")
for _p in (_IMAGES_SRC, _SMART_SELECTOR_SRC, _BETA9_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import file_analysis  # noqa: E402 - real Beta9 module, read-only use
import policy  # noqa: E402
import resource_scheduler  # noqa: E402 - real Beta9 module; classify(...) is called unmodified
import selector  # noqa: E402 - real Beta9 module; selector.select(...) is called unmodified
import smart_compression  # noqa: E402 - real Beta9 module; decide_and_prepare/_run_candidates called unmodified
import strategy_registry  # noqa: E402 - real Beta9 module; get_candidate_strategies(...) is called unmodified

from beta10_src.integration_layer import _apply_conservative_overlay  # noqa: E402 - reused, not reimplemented
from beta11_src.binary_strategies_v1 import Zstd15Strategy  # noqa: E402
from beta11_src.compression_cache_v1 import MISS, NO_CANDIDATE, CompressionCache  # noqa: E402
from beta11_src.integration_layer import _EXACT_CANDIDATE_OVERRIDES, _BROTLI_Q9_MAX_BYTES  # noqa: E402
from beta11_src.text_strategies_v1 import BrotliQ9Strategy, ZstdLongRangeStrategy  # noqa: E402

from . import resource_admission_v4  # noqa: E402
from . import strategy_log_v2  # noqa: E402
from . import text_strategies_v2  # noqa: E402
from . import binary_strategies_v2  # noqa: E402

BETA12_MODULES = {
    "resource_admission_v4": resource_admission_v4,
    "strategy_log_v2": strategy_log_v2,
}

# HOTFIX: per-candidate absolute timeout stays relaxed at 120s (carried
# from Beta11) - not part of this round's scope, unchanged.
_RELAXED_ABSOLUTE_TIMEOUT_S = 120.0

# How long a HEAVY/MEDIUM candidate may wait for real free-core capacity
# before being dropped - see _filter_ineligible_heavy_candidates for the
# real incident this exists to fix. Bounded on purpose: this can delay a
# candidate, never block one forever.
_ADMISSION_WAIT_MAX_S = 3.0
_ADMISSION_WAIT_POLL_S = 0.25

# HOTFIX (this round, found via three real full-corpus runs): PNG's second
# candidate was specified as generic_brotli (Beta9's real quality-11
# brotli) - the same strategy this whole project has repeatedly measured
# as catastrophically slow at volume (it's WHY brotli_q9 was invented for
# text in the first place). This test corpus has 1,845 PNG files; at
# ~2-4s each for quality-11 brotli, that's ~1,800-2,200s of PNG-only work
# ACROSS the whole run, which alone blew well past the <=8-minute target
# in three separate, non-hanging, fully-progressing real runs (confirmed
# via per-file heartbeat logging - not a deadlock, just real compute
# volume). This is a local override on TOP of beta11_src's own real
# _EXACT_CANDIDATE_OVERRIDES table (which is left completely unedited) -
# only the PNG entry for THIS layer's own routing is replaced, swapping
# generic_brotli for brotli_q9 (same real, already-tested Beta11 strategy
# already used for text) to keep a real second candidate while actually
# meeting the runtime target.
_CANDIDATE_OVERRIDES_V4 = dict(_EXACT_CANDIDATE_OVERRIDES)
_CANDIDATE_OVERRIDES_V4["png"] = ("generic_zstd", BrotliQ9Strategy.name)

# HOTFIX #2 (same round): after fixing PNG, csv/log files became the new
# dominant real-time cost - beta11_src's ZstdLongRangeStrategy (level 19)
# is real but expensive at volume on many real CSV/log files. Swapped for
# text_strategies_v2.ZstdLongRangeFastStrategy (level 12, same long-range
# window) - see that module's own docstring for the real evidence (Beta9's
# own adaptive_zstd.py already proved this exact level-19-vs-12 tradeoff).
for _ext in ("txt", "log", "csv", "md", "html"):
    _CANDIDATE_OVERRIDES_V4[_ext] = tuple(
        text_strategies_v2.ZstdLongRangeFastStrategy.name if n == "zstd_long_range" else n
        for n in _CANDIDATE_OVERRIDES_V4[_ext]
    )

# REGRESSION FIX (this round): HOTFIX #2 above was calibrated against
# real-VOLUME runtime cost (many large CSV/log files in one folder
# Archive), never re-measured on small individual files - confirmed as a
# real regression on a single 137KB CSV: ZstdLongRangeFastStrategy (level
# 12) produced 49,121B, WORSE than plain generic_zstd's 45,674B (Beta9's
# own AdaptiveZstdStrategy, which self-selects level 19 under 4MB, level
# 12 at/above it - see adaptive_zstd.py's own _LEVEL_TIERS). Level 12's
# speed tradeoff only pays for itself on files large enough that level 19
# would actually be slow - HOTFIX #2 applied it unconditionally to every
# file regardless of size, losing real compression ratio on the (likely
# common) small-file case for no runtime benefit at all.
#
# Fix: ADD generic_zstd as a third candidate rather than replacing
# anything - it is LIGHT-classified (never gated by resource_admission_v4,
# unlike jpeg_xl_lossless_jpeg's HEAVY classification) and already
# self-tunes its own level by size via adaptive_zstd.py, so it costs
# real-volume runs nothing beyond one more LIGHT/fast probe (never
# reintroducing the actual root cause of the original incident - that was
# brotli quality-11 / lzma preset-9's catastrophic slowness at volume,
# never zstd at any level) while guaranteeing every file gets at least
# whatever plain generic_zstd would have achieved. The existing benefit/
# cost policy still picks whichever of the three actually wins per file.
for _ext in ("txt", "log", "csv", "md", "html"):
    if "generic_zstd" not in _CANDIDATE_OVERRIDES_V4[_ext]:
        _CANDIDATE_OVERRIDES_V4[_ext] = _CANDIDATE_OVERRIDES_V4[_ext] + ("generic_zstd",)

# COMPETITIVE-RATIO IMPROVEMENT (this round): real head-to-head measurement
# against 7-Zip's native .7z (LZMA2, -mx9) showed it beating every
# StorageArch candidate above on non-JPEG text/code files by a real, small
# margin (e.g. real 137KB CSV: 7-Zip .7z 42,260B vs StorageArch's best of
# 45,674B). generic_lzma (Beta9's own real LZMA preset-9 strategy) is the
# closest match to what 7-Zip's .7z format actually uses - added here as
# an ADDITIONAL candidate (never replacing anything already tried, so this
# can only ever match or beat what was already achievable, never regress
# it) for the same text/code extensions plus json/py/js, which had NO
# override at all before this (silently stuck on Beta9's zstd-only
# _CHEAP_ONLY fallback).
#
# Why this does NOT reintroduce the "catastrophically slow at volume"
# incident that motivated HOTFIX #2/#3 above (that incident was real,
# measured, and specific to generic_brotli quality-11 / generic_lzma
# preset-9 on LARGE files, 15MB-147MB+ in that investigation) - THREE
# independent, already-existing safety nets apply to every generic_lzma
# candidate added here, none of them new:
#   1. resource_scheduler.STRATEGY_CLASS classifies it HEAVY (Beta9's own
#      classification, unchanged) - resource_admission_v4's RAM-watermark
#      gate can and will deny it under real memory pressure, exactly like
#      it already does for jpeg_xl_lossless_jpeg.
#   2. policy.py's own slow_probe_strategy_names already includes
#      "generic_lzma" - a real-kill 30s dispatch timeout (not this
#      round's relaxed 120s default), unchanged, pre-existing Beta9 policy.
#   3. A size cap ADDED here (_GENERIC_LZMA_MAX_BYTES) drops it from the
#      candidate list entirely above that size, before any of the above
#      even runs. Set to 12MB (raised from an earlier 5MB - see below) based
#      on real direct measurement of lzma.compress(preset=9) - a real
#      2.58MB XLSX took 978ms (2,518,036B, competitive with 7-Zip's
#      2,518,120B), a real 4.15MB EXE took 1,706ms (1,320,288B), a real
#      9.37MB SQL backup took 9.31s (1,548,904B - beats generic_zstd's
#      1,686,340B by 8.9%, motivating the "sql" extension addition below) -
#      all comfortably under the 30s slow-probe timeout even with real
#      dispatch/IPC overhead added, and consistent with the documented
#      incident's own numbers scaling roughly linearly with size (41MB took
#      ~55-69s at this same preset - a 12MB file is ~3.4x smaller, so
#      ~3.4x faster by the same scaling, landing at ~16-20s worst case,
#      still real margin under 30s, not the original incident's regime).
_GENERIC_LZMA_MAX_BYTES = 12_000_000

# TRUSTED-LIBRARY FAST PATH (explicit owner approval after real
# measurement - see _run_fast_path_candidate's own docstring for the full
# safety analysis). zstd_15 added (round 2, same explicit approval): it is
# literally the same zstandard library as generic_zstd, just a different
# compression level - no new trust category. brotli_q9 added (round 2,
# same approval): Google's brotli, a mature, widely-deployed format (HTTP
# Content-Encoding since ~2015, used by every major browser) - not
# DEFLATE/LZMA-tier decades of history, but a real, battle-tested library
# nonetheless, and this is scoped to Beta12's OWN quality-9 wrapper of it.
# Real measured contribution to a 4.1MB EXE's total time: zstd_15 ~470ms,
# brotli_q9 ~580ms compress-only (both previously paid ~1.5-2x that via
# the full verify cycle). Every OTHER strategy (jpeg_xl_lossless_jpeg,
# video_optimizer, text prefilters, zstd_long_range_v2, ...) keeps the
# full compress -> restore -> SHA-256 verify cycle unchanged - those wrap
# custom/novel bundling logic (this project's own code) or strategies not
# reviewed for this fast path.
# generic_zlib/generic_brotli added (this round, same pre-existing approval
# category as zstd_15/brotli_q9 above, not a new trust boundary): both are
# GenericZstdStrategy's OWN sibling classes in generic_byte.py (identical
# _GenericByteStrategy base, same "no image-specific transform - exact
# reversibility holds by construction" guarantee documented at that
# module's own top). generic_zlib wraps Python's stdlib zlib (the exact
# DEFLATE codec ZIP itself uses - the single most mature/proven codec of
# any strategy in this pipeline). generic_brotli is LITERALLY the same
# brotli library as the already-approved brotli_q9, just quality=11
# instead of 9 - same "same library, different level, no new trust
# category" reasoning already used for zstd_15 above, just applied to the
# other lever (quality) instead of level. Real measured motivation: found
# via BMP profiling - BMP has no beta12 override (falls through to Beta9's
# own default 5-candidate _BMP_FULL list), so generic_brotli/generic_zlib
# were paying full multiprocessing-worker dispatch + restore + verify
# overhead (~600ms real, measured via cProfile - WaitForMultipleObjects
# alone) for candidates that between them never actually won on the real
# test BMP (generic_zstd's 68B beat generic_brotli's 74B and
# generic_zlib's 90B on the real indexed_8color_64_c0.bmp), while their
# own compress() calls take single-digit-to-tens of ms in isolation -
# nearly all of that time was pure IPC/dispatch overhead, not real
# compute, the exact same waste category the fast path already exists to
# eliminate for the other five.
# jpeg_xl_lossless_jpeg added (this round, EXPLICIT owner approval for a
# NEW trust category - unlike every strategy above, cjxl.exe/djxl.exe are
# external subprocess binaries doing a less-common lossless-JPEG-
# recompression feature, not a mature widely-used Python-bound library.
# Approved anyway after presenting the real trade-off: real profiling (see
# this module's own benchmark notes) showed JPEG paying ~100-900ms
# per file, dominated by (a) a full multiprocessing-worker dispatch round
# trip (_run_normal_candidate_fast_admit's WaitForMultipleObjects wait,
# ~160-600ms real measured) plus (b) djxl's own restore+verify subprocess
# spawn (~44-144ms real measured via direct compress()/restore() split
# timing) - both eliminated by fast-pathing, same mechanism as the other
# six. Safety trade-off is identical in KIND to the rest of this set
# (original_sha256/stored_sha256 still computed and stored exactly as
# before; checkout_item_smart/restore_original_bytes still verify every
# restore and still raise SmartRestoreError if djxl's own compress/restore
# pairing were ever wrong) but DIFFERENT in degree - a real bug in a less-
# common lossless-JPEG codepath of an external tool is more plausible than
# one in decades-proven zstd/lzma/brotli/zlib, so this was NOT bundled into
# the original blanket approval above; it required its own explicit
# conversation, which happened.
_FAST_PATH_STRATEGY_NAMES = frozenset({
    "generic_zstd", "generic_lzma", "lzma_x86", "zstd_15", "brotli_q9",
    "generic_zlib", "generic_brotli",
})

# Bound for fast-path candidate compression - see _run_fast_path_candidate's
# own comment for the real incident this fixes.
#
# Its OWN semaphore rather than reusing smart_compression._CPU_BOUND_PREP_GATE:
# sharing that 8-slot gate was measured (2026-08-27, real other_only folder,
# 3 paired rounds) to over-constrain, since baseline-zip/SHA-256 prep and
# candidate compression then compete for the same 8 slots.
#
# Size 8 is not a guess - it is the measured optimum. Direct isolated sweep
# on this exact workload (the 40 real ~7.9MB JSON files that triggered the
# incident, generic_lzma preset 9, wall time for all 40):
#     concurrency  2 -> 200.7s      concurrency 12 ->  85.4s
#     concurrency  4 -> 118.9s      concurrency 16 ->  77.6s
#     concurrency  6 ->  84.2s      concurrency 32 ->  77.8s
#     concurrency  8 ->  62.3s  <-- best, ~22% better than 16/32
# Throughput peaks sharply at 8 and DEGRADES above it: LZMA preset 9 uses a
# 64MB dictionary per concurrent call, so past ~8 the working set stops
# fitting cache/memory bandwidth on this 10-physical-core machine and extra
# threads only add thrash. Below 8 the cores are simply underused.
#
# 8 also matches the bound this codebase already independently settled on
# for both SmartWorkerPool and _CPU_BOUND_PREP_GATE, so total CPU-bound
# concurrency stays in the same regime those were validated against. This
# is a strict REDUCTION from the previous unbounded 32-decide-thread case -
# the opposite direction from the 2026-08-20 regression, which came from
# RAISING concurrency.
_FAST_PATH_CPU_GATE = threading.Semaphore(max(1, min(os.cpu_count() or 4, 8)))
# jpeg_xl_lossless_jpeg REMOVED from the fast path (2026-08-26, real
# incident): a real file (seattle_engineer_photo.jpg, from a user's
# photo_file13 folder) was accepted by this fast path - meaning its own
# compress->restore->SHA-256 verify cycle was skipped at write time - and
# later failed a REAL restore with SmartRestoreError: "restored bytes did
# not match expected original SHA-256". Root-caused directly: running
# djxl.exe by hand on the exact compressed bytes reproduces it 100% of the
# time, with djxl printing its own warning before falling back to a lossy
# pixel re-encode: "Warning: could not decode losslessly to JPEG. Retrying
# with --pixels_to_jpeg... Decoded to pixels." - exit code 0, no error, so
# nothing at write time could have caught this except the very restore-
# verify step this fast path was skipping. This is exactly the risk this
# fast path's own approval comment (above) already named as "more
# plausible" for this specific strategy - it has now materialized on real
# user data. Removing it restores the full verify cycle for
# jpeg_xl_lossless_jpeg only (every other fast-pathed strategy is
# unaffected) - a bad candidate is now rejected BEFORE being written,
# never getting the chance to fail later at actual restore/checkout time.
# Measured cost: repeated real-folder A/B testing (photo_file13,
# realistic_5gb mixed, other_only-no-JPEGs-control) showed no reliable
# time regression - varied within normal run-to-run noise, same order as
# WITH the fast path - and no compression change (deterministic, as
# expected: this only affects the accept-time safety check, never which
# candidate wins).

# HOTFIX #3 (same round): binary's own literal spec candidate
# (generic_brotli, quality 11) hit the exact same real-volume slowness on
# real .bin payloads - swapped for brotli_q9 for consistency with the PNG
# fix above (same underlying cause, same fix).
# zstd_15/brotli_q9 REMOVED here (this round - was ("zstd_15",
# BrotliQ9Strategy.name) before lzma_x86 existed as a HOTFIX-era safety
# net). Real measurement, every exe/dll benchmark this whole thread of
# work has run: lzma_x86 has selected 100% of the time for exe/dll, by a
# wide, non-marginal margin (a real 4.1MB EXE: lzma_x86 1,277,758B vs
# brotli_q9 1,553,561B vs zstd_15 1,605,250B - lzma_x86 wins by 275KB+
# every time, not a close call). Keeping them as "just in case" candidates
# was pure wasted CPU contention during EXE/DLL's real bottleneck: a
# controlled A/B (same file, same process, back-to-back, 4 rounds)
# measured lzma_x86 running ALONE at 2328ms average vs 2558ms average with
# zstd_15/brotli_q9 competing for cores concurrently - about 9% slower for
# zero size/selection benefit, confirmed consistent across all 4 rounds.
_CANDIDATE_OVERRIDES_V4["exe"] = ()
_CANDIDATE_OVERRIDES_V4["dll"] = ()
_CANDIDATE_OVERRIDES_V4["bin"] = ()

# HOTFIX #4 (same round): GIF and TIFF were never in the original 7-type
# override table (the spec only named text/office/jpeg/bmp/png/binary), so
# they silently fell through to Beta9's OWN real default routing for
# "generic candidate" image types - strategy_registry.py's _FULL_GENERIC,
# FOUR candidates including generic_lzma (preset 9) AND generic_brotli
# (quality 11), the exact two strategies this entire round's investigation
# found to be the real volume bottleneck. Given exactly-2-candidates was
# this round's whole design goal, GIF/TIFF get the same safe 2-candidate
# treatment as PNG instead of silently inheriting Beta9's slower 4-candidate
# default.
_CANDIDATE_OVERRIDES_V4["gif"] = ("generic_zstd", BrotliQ9Strategy.name)
_CANDIDATE_OVERRIDES_V4["tiff"] = ("generic_zstd", BrotliQ9Strategy.name)
_CANDIDATE_OVERRIDES_V4["tif"] = ("generic_zstd", BrotliQ9Strategy.name)

# COMPETITIVE-RATIO IMPROVEMENT, part 2 (same round): the same real
# head-to-head measurement against 7-Zip .7z that motivated adding
# generic_lzma to text/code/json above also showed PNG/GIF/TIFF/DOCX/
# XLSX/EXE/DLL all losing to 7-Zip's LZMA2 by real, measured margins (a
# real 4.1MB EXE: 7-Zip .7z 1,253,716B vs StorageArch's best candidate at
# the time, 1,605,250B). Every extension already has at least one
# candidate tried (never replaced here) - generic_lzma is added
# ADDITIONALLY, after every extension-specific override above has already
# been established, subject to the exact same three pre-existing safety
# nets documented above (HEAVY/RAM-admission gating, 30s slow-probe
# timeout, and the same _GENERIC_LZMA_MAX_BYTES 2MB cap) - so large files
# (GIF/TIFF here are often multi-MB) correctly skip it above the cap,
# exactly like brotli_q9 already does, never reintroducing the real
# large-file volume incident this whole override table exists to avoid.
# exe/dll/bin deliberately EXCLUDED here (handled by lzma_x86 alone below)
# - RUNTIME FIX (this round): real measurement showed lzma_x86 (LZMA + x86
# BCJ filter) strictly beats plain generic_lzma on every exe/dll/bin file
# tested (4.1MB EXE: 1,277,754B vs 1,320,288B; 726KB DLL: 266,156B vs
# 272,428B) - trying both back-to-back on the SAME file only paid real
# extra wall-clock time (a real 4.1MB EXE went from ~1s to 7.8s once both
# ran) for zero chance of generic_lzma ever actually winning. Since
# generic_lzma can never be the answer here, it is not even offered as a
# candidate - this is pure Policy Tuning (skip a strategy with no chance
# of winning), not a size regression: lzma_x86 alone already captures
# everything generic_lzma could have contributed and more.
for _ext in ("txt", "log", "csv", "md", "html", "json", "py", "js", "png", "gif", "tiff", "tif", "docx", "xlsx"):
    _CANDIDATE_OVERRIDES_V4.setdefault(_ext, ("generic_zstd",))
    if "generic_lzma" not in _CANDIDATE_OVERRIDES_V4[_ext]:
        _CANDIDATE_OVERRIDES_V4[_ext] = _CANDIDATE_OVERRIDES_V4[_ext] + ("generic_lzma",)

# SQL - EXCLUSIVE generic_lzma, not additive (this round, real evidence on
# 4 real 9.37MB SQL backup files, all consistent): generic_lzma beat
# generic_zstd on EVERY file (~8.9% smaller every time, no exceptions) AND
# was consistently FASTER too (11.2-11.4s vs zstd's 13.1-13.2s) - the exact
# "one strategy strictly beats the other, trying both only wastes time"
# situation already established for exe/dll/bin (lzma_x86 alone, no
# zstd_15/brotli_q9). Adding generic_lzma ADDITIVELY (the pattern used for
# txt/csv/json/etc above) would have cost real time running both
# candidates sequentially for zero chance of zstd ever winning - confirmed
# this exact regression happened first (58.7-65.2s real folder time, up
# from 30.6-46.4s) before being fixed to exclusive-lzma here. Real result
# after this fix: SQL files get BOTH the smaller size AND less total
# compute than before (one strategy, the faster+smaller one, instead of
# two). Requires the size cap raised to 12MB above (real SQL backups here
# are 9.37MB, over the old 5MB cap).
_CANDIDATE_OVERRIDES_V4["sql"] = ("generic_lzma",)

# COMPETITIVE-RATIO IMPROVEMENT, part 3 (Phase 3): lzma_x86 (see
# binary_strategies_v2.py) - LZMA with the x86 BCJ filter, exactly what
# 7-Zip applies automatically for executable content. Real measurement:
# a 4.1MB EXE improved from generic_lzma's 1,320,288B to 1,277,754B (a
# real 42,534B win, closing most - not all - of the remaining gap to
# 7-Zip's 1,253,716B); a 726KB DLL improved from 272,428B to 266,156B.
# Byte-exact round trip confirmed both times. Scoped ONLY to exe/dll/bin -
# the x86 BCJ filter is specific to x86/x64 machine code, meaningless (at
# best a no-op, at worst counter-productive) on text/image/office content,
# so it is not added anywhere else. Subject to the exact same safety nets
# as generic_lzma above (HEAVY/RAM-admission gating, 30s slow-probe
# timeout - both apply per-strategy-name, "lzma_x86" inherits generic
# HEAVY classification via resource_scheduler.classify's unknown-strategy
# default) plus the same _GENERIC_LZMA_MAX_BYTES 5MB cap, reused directly
# rather than duplicated.
for _ext in ("exe", "dll", "bin"):
    if "lzma_x86" not in _CANDIDATE_OVERRIDES_V4[_ext]:
        _CANDIDATE_OVERRIDES_V4[_ext] = _CANDIDATE_OVERRIDES_V4[_ext] + ("lzma_x86",)

# SKIP-KNOWN-LOSERS RUNTIME FIX (this round, explicit owner approval): real
# measurement on real files of these exact 6 extensions showed NO candidate
# - not the ones already here, not generic_lzma, not lzma_x86, not a
# PNG-via-JXL re-encode, not ZIP-surgical thumbnail removal for docx/xlsx -
# ever winning against baseline_storagearch_zip's own benefit/cost gate
# (content is already near-entropy-optimal for these specific files: e.g.
# a real PNG's IDAT chunk alone was 99.5% of the file). Every real
# candidate attempt for these extensions was therefore 100% wasted
# real time for a result that was ALWAYS going to be baseline anyway -
# confirmed as real, measured, wasted wall-clock: a real 2.58MB XLSX went
# from ~200ms (baseline alone) to 6-7 SECONDS once candidates were tried.
# Routing these to an EMPTY candidate list makes decide_and_prepare return
# None immediately after computing baseline (which it always computes
# regardless, as the fallback), matching plain ZIP's own speed almost
# exactly - real measurement: PNG 12ms->5ms, GIF 269ms->146ms, TIFF
# 530ms->408ms (all three flip to beating both ZIP and 7-Zip on time).
#
# This is a real, explicit trade-off, not a free lunch: it permanently
# gives up on ever finding a win for THESE extensions, even for some
# future file that might genuinely have real embeddable metadata/
# thumbnails to strip (the specific files measured here mostly didn't).
# Revisit if real-world evidence on a broader/different corpus shows
# otherwise for a specific extension.
for _ext in ("png", "gif", "tiff", "tif", "xlsx", "docx"):
    _CANDIDATE_OVERRIDES_V4[_ext] = ()

# ALREADY-MAXIMALLY-COMPRESSED CONTAINER FORMATS (this round, real profiling
# on a real 1.24GB/225-file mixed folder - see HANDOFF_PROMPT.md's fourth
# session). gz/xz/bz2/zip are, by construction, the OUTPUT of a strong
# general-purpose compressor - unlike png/gif/tiff/xlsx/docx above (whose
# "no candidate wins" finding was empirical and corpus-specific, flagged as
# revisit-if-a-different-corpus-shows-otherwise), this one holds by
# compression theory regardless of what corpus produced the file: you
# cannot meaningfully re-compress the output of LZMA/DEFLATE/BZip2/DEFLATE
# with another general-purpose compressor. Confirmed directly on this
# session's real files anyway rather than assuming: baseline DEFLATE and
# zstd level 19 both landed within -0.0% to +0.1% of original size on real
# .gz/.xz/.bz2/.zip files (technically often slightly LARGER, standard for
# recompressing incompressible data due to container overhead) while
# COSTING real time - a real 8.4MB file: baseline DEFLATE ~245-257ms, plus
# generic_zstd's own attempt ~2000-2200ms (zstd level 19 is not cheap) -
# 100% wasted for a result that will never be selected.
#
# Deliberately did NOT add tar/flac/webm/mp3/wav/mkv/mp4/rtf/pdf/odt/pptx/
# epub here despite similar "already compressed" intuitions - real
# measurement on the same corpus showed several of these compress
# substantially (mp3 75%, wav 99.8%, pdf 82.9%, rtf 100%, pptx 36.2%, odt
# 14.3%, ogg 49.6%, mkv 6.3%, mp4 3.3% - likely synthetic/padded test
# content in this specific corpus, not necessarily representative of real-
# world files of these types) - adding them would risk a real savings
# regression the owner explicitly said not to accept. gz/xz/bz2/zip are the
# only ones in this round's investigation with a compression-theoretic (not
# just corpus-specific) guarantee of no benefit.
for _ext in ("gz", "xz", "bz2", "zip"):
    _CANDIDATE_OVERRIDES_V4[_ext] = ()


def _relaxed_policy():
    return dataclasses.replace(policy.DEFAULT_POLICY, absolute_timeout_seconds=_RELAXED_ABSOLUTE_TIMEOUT_S)


def _apply_exact_candidate_overrides(extension, size_bytes, candidate_names):
    override = _CANDIDATE_OVERRIDES_V4.get((extension or "").lower())
    if override is None:
        return candidate_names
    names = list(override)
    if BrotliQ9Strategy.name in names and size_bytes >= _BROTLI_Q9_MAX_BYTES:
        names = [n for n in names if n != BrotliQ9Strategy.name]
    if "generic_lzma" in names and size_bytes >= _GENERIC_LZMA_MAX_BYTES:
        names = [n for n in names if n != "generic_lzma"]
    if "lzma_x86" in names and size_bytes >= _GENERIC_LZMA_MAX_BYTES:
        names = [n for n in names if n != "lzma_x86"]
    return names


def _filter_ineligible_heavy_candidates(size_bytes, candidate_names, admission_gate, log_ref):
    """Same shape as Beta11's filter, but records (heavy_allowed, free_cores)
    for the log at the exact moment the decision was made, and never
    checks CPU% - only resource_admission_v4's free-cores model."""
    kept = []
    any_heavy_checked = False
    heavy_allowed = None
    free_cores = None
    for name in candidate_names:
        try:
            weight_class, _ram_estimate = resource_scheduler.classify(name, size_bytes)
        except Exception:  # noqa: BLE001
            weight_class = "LIGHT"
        if weight_class in ("HEAVY", "MEDIUM"):
            any_heavy_checked = True
            # NON-DETERMINISM / COMPRESSION-LOSS FIX (2026-08-27, real
            # measured incident). This used to call evaluate() exactly ONCE
            # and, on a "no free cores right now" answer, drop the candidate
            # PERMANENTLY for this file - so whether a file got its best
            # strategy or fell all the way back to baseline depended purely
            # on how busy the machine happened to be in the microsecond its
            # decide-thread reached this line.
            #
            # That made the SAME code produce different archives run to run.
            # Measured directly on one real 3.5GB mixed folder, 4 identical
            # runs of identical code: jpeg_xl_lossless_jpeg was accepted for
            # 16, 15, 14 and 12 files respectively, and total compression
            # tracked it in lockstep - 21.716%, 21.471%, 21.221%, 20.731%.
            # A full 1.0 percentage point of real compression was being won
            # or lost by timing luck alone, and it also made every A/B
            # measurement on this project noisy enough to be misleading.
            #
            # Fix: a bounded WAIT instead of an instant drop. Capacity that
            # is merely busy right now frees up within seconds as other
            # files finish, so waiting costs queueing time, not extra work -
            # whereas dropping costs real compression permanently. The cap
            # keeps the original guarantee that this can never block
            # forever: once it expires the candidate is dropped exactly as
            # before, so the worst case is unchanged and only the common
            # case improves.
            deadline = time.monotonic() + _ADMISSION_WAIT_MAX_S
            while True:
                decision = admission_gate.evaluate(name, size_bytes)
                free_cores = admission_gate.free_cores()
                heavy_allowed = decision.admitted
                if decision.admitted or time.monotonic() >= deadline:
                    break
                time.sleep(_ADMISSION_WAIT_POLL_S)
            if not decision.admitted:
                continue
        kept.append(name)
    if log_ref is not None and any_heavy_checked:
        log_ref["last_heavy_allowed"] = heavy_allowed
        log_ref["last_free_cores"] = free_cores
    return kept


def _register_new_strategies():
    added = []
    for strategy in (
        ZstdLongRangeStrategy(), BrotliQ9Strategy(), Zstd15Strategy(),
        text_strategies_v2.ZstdLongRangeFastStrategy(), binary_strategies_v2.LzmaX86Strategy(),
    ):
        if strategy.name not in selector._STRATEGY_INSTANCES:
            selector._STRATEGY_INSTANCES[strategy.name] = strategy
            added.append(strategy.name)
    return tuple(added)


def _unregister_new_strategies(names):
    for name in names:
        selector._STRATEGY_INSTANCES.pop(name, None)


_ORIGINAL_SELECT = None
_ORIGINAL_GET_CANDIDATE_STRATEGIES = None
_ORIGINAL_DECIDE_AND_PREPARE = None
_ORIGINAL_RUN_CANDIDATES = None
_ORIGINAL_POOL_ACQUIRE = None
_ORIGINAL_POOL_RELEASE = None
_ORIGINAL_DEFAULT_POLICY = None
_ROUTING_ADMISSION_GATE = None
_ACTIVE_WORKER_TRACKER = None
_COMPRESSION_CACHE = None
_STRATEGY_LOG = None
_REGISTERED_STRATEGY_NAMES = ()
_ACTIVATED = False
_MISSING = object()

# Thread-local, NOT a shared dict: get_candidate_strategies() and
# _run_candidates()/decide_and_prepare() for the SAME file always run on
# the SAME calling thread (decide_and_prepare calls them synchronously in
# sequence), but under real concurrent folder-wide dispatch (many files
# via a ThreadPoolExecutor) DIFFERENT files run on DIFFERENT threads
# simultaneously - a single shared dict here would let one file's
# admission state clobber another's mid-flight. threading.local() gives
# each calling thread its own isolated view with no locking needed.
_admission_state = threading.local()


def _get_admission_state():
    if not hasattr(_admission_state, "heavy_allowed"):
        _admission_state.heavy_allowed = None
        _admission_state.free_cores = None
    return _admission_state


def _default_admission_gate(tracker):
    return resource_admission_v4.AdmissionGateV4(
        active_worker_threads_fn=lambda: tracker.current,
        weight_classifier=resource_scheduler.classify,
    )


def _patch_pool_acquire_release(tracker):
    """Wraps smart_compression.SmartWorkerPool.acquire/release (CLASS
    methods) so ActiveWorkerTracker counts REAL compute concurrency - a
    session held between acquire() and release() maps to one of Beta9's
    own real OS worker PROCESSES actually dispatching a candidate (hard-
    capped at 8 by SmartWorkerPool.__init__: `min(size or cpu_count, 8)`).

    BUGFIX (this round, found via the real corpus run): the first version
    of this module bracketed the tracker around the ENTIRE
    decide_and_prepare/select call instead - which counts DECIDE threads
    (Beta9's own executor runs up to min(64, max(pool.size*4, 16)) = 32 of
    those on an 8-session pool), not real compute. Most of those 32
    threads are idle-waiting on IPC/admission at any instant (that
    oversubscription is deliberate - see archive_manager_smart.py's own
    comment: "Waiting for RAM headroom only needs a thread, not a worker
    process"), so counting them as "active workers" against a real core
    count (e.g. 16) reported free_cores<=0 almost always, denying HEAVY
    candidates even more aggressively than Beta11's CPU% model did (a
    real measured regression: 1/2229 heavy admissions instead of Beta11's
    already-too-low 3/70). Tracking pool.acquire()/release() instead
    measures the thing that actually correlates with real CPU cores in
    use - a session is only held while its worker process is genuinely
    dispatching/awaiting ONE candidate's real compute, not while merely
    queued in the decide-thread pool.
    """
    original_acquire = smart_compression.SmartWorkerPool.acquire
    original_release = smart_compression.SmartWorkerPool.release

    def acquire(self):
        session = original_acquire(self)
        tracker.__enter__()
        return session

    def release(self, session):
        tracker.__exit__(None, None, None)
        return original_release(self, session)

    smart_compression.SmartWorkerPool.acquire = acquire
    smart_compression.SmartWorkerPool.release = release
    return original_acquire, original_release


def activate_beta12(admission_gate=None, compression_cache=None, strategy_log=None, active_worker_tracker=None):
    """Idempotently patches selector.select, strategy_registry.get_candidate_strategies,
    smart_compression.decide_and_prepare, smart_compression._run_candidates,
    and policy.DEFAULT_POLICY. See the module docstring for the one real
    change vs Beta11 (free-cores admission instead of CPU%).

    Does NOT write to any file under beta9_src/, beta10_src/, or
    beta11_src/. Safe to call more than once - only the first call
    actually patches. Call deactivate_beta12() to restore everything.
    """
    global _ORIGINAL_SELECT, _ORIGINAL_GET_CANDIDATE_STRATEGIES, _ORIGINAL_DECIDE_AND_PREPARE
    global _ORIGINAL_RUN_CANDIDATES, _ORIGINAL_POOL_ACQUIRE, _ORIGINAL_POOL_RELEASE, _ORIGINAL_DEFAULT_POLICY
    global _ROUTING_ADMISSION_GATE, _ACTIVE_WORKER_TRACKER, _COMPRESSION_CACHE, _STRATEGY_LOG
    global _REGISTERED_STRATEGY_NAMES, _ACTIVATED
    if _ACTIVATED:
        return

    _ORIGINAL_SELECT = selector.select
    _ORIGINAL_GET_CANDIDATE_STRATEGIES = strategy_registry.get_candidate_strategies
    _ORIGINAL_DECIDE_AND_PREPARE = smart_compression.decide_and_prepare
    _ORIGINAL_RUN_CANDIDATES = smart_compression._run_candidates
    _ORIGINAL_DEFAULT_POLICY = policy.DEFAULT_POLICY

    _ACTIVE_WORKER_TRACKER = active_worker_tracker or resource_admission_v4.ActiveWorkerTracker()
    _ROUTING_ADMISSION_GATE = admission_gate or _default_admission_gate(_ACTIVE_WORKER_TRACKER)
    _COMPRESSION_CACHE = compression_cache if compression_cache is not None else CompressionCache()
    _STRATEGY_LOG = strategy_log if strategy_log is not None else strategy_log_v2.StrategyLogV2()

    _REGISTERED_STRATEGY_NAMES = _register_new_strategies()
    policy.DEFAULT_POLICY = _relaxed_policy()
    _ORIGINAL_POOL_ACQUIRE, _ORIGINAL_POOL_RELEASE = _patch_pool_acquire_release(_ACTIVE_WORKER_TRACKER)

    def _select_with_beta12_overlay(input_path, photos_root, output_root, run_tag, config=None):
        used_config = config if config is not None else policy.DEFAULT_POLICY
        beta9_result = _ORIGINAL_SELECT(input_path, photos_root, output_root, run_tag, config=used_config)
        return _apply_conservative_overlay(beta9_result, admission_gate=_ROUTING_ADMISSION_GATE)

    def _get_candidate_strategies_with_beta12(detected_type, extension, size_bytes):
        names = _ORIGINAL_GET_CANDIDATE_STRATEGIES(detected_type, extension, size_bytes)
        try:
            names = _apply_exact_candidate_overrides(extension, size_bytes, names)
            log_ref = {}
            names = _filter_ineligible_heavy_candidates(size_bytes, names, _ROUTING_ADMISSION_GATE, log_ref)
            state = _get_admission_state()
            state.heavy_allowed = log_ref.get("last_heavy_allowed")
            state.free_cores = log_ref.get("last_free_cores")
        except Exception:  # noqa: BLE001 - a Beta12 filtering bug must never break Beta9's routing
            return names
        return names

    def _run_fast_path_candidate(strategy, file_path, original_hash):
        """TRUSTED-LIBRARY FAST PATH (this round, explicitly approved after
        real measurement - see the module-level docstring note below).

        Skips the restore+verify half of _compress_restore_verify for
        generic_zstd/generic_lzma/lzma_x86 ONLY - these wrap mature,
        spec-compliant, decades-proven libraries (Python's own zstandard/
        lzma modules) exactly the way ZIP/7-Zip trust DEFLATE/LZMA without
        re-verifying every file. Real measured speedup: JSON 2377ms ->
        511ms (4.6x), CSV 505ms -> 136ms (3.7x), EXE 4737ms -> 2573ms
        (with lzma_x86's own runtime fix), DLL 1047ms -> 426ms.

        IMPORTANT - what safety is and isn't preserved: this does NOT
        remove the pipeline's hard-error guarantee, only WHEN it can fire.
        original_sha256/stored_sha256 are still computed and stored
        exactly as before (both computable directly, without a restore
        step) - checkout_item_smart/restore_original_bytes still verify
        every restore against original_sha256 on every future checkout and
        still raise SmartRestoreError (never silently return wrong bytes)
        if there ever were a real bug in one of these strategies' own
        compress()/restore() pairing. What changes is WHEN a bug would
        surface: previously at archive time (this candidate would simply
        never be accepted); now potentially later, at first checkout. This
        is the real, explicit trade-off the project owner approved after
        seeing the measured numbers above - not an accidental gap.
        """
        work_dir = tempfile.mkdtemp(prefix="fastpath_")
        try:
            # CPU-OVERSUBSCRIPTION FIX (2026-08-27, real measured incident on
            # a real 2.43GB text/document folder). Unlike _run_normal_candidate_
            # fast_admit above - which dispatches into SmartWorkerPool's bounded
            # 8 worker PROCESSES - this fast path calls strategy.compress()
            # DIRECTLY on the calling decide-thread. archive_manager_smart's
            # decide_thread_count is min(64, max(pool.size*4, 16)) = 32, and
            # zstd/lzma/brotli compress() are C extensions that RELEASE THE GIL,
            # so those 32 threads genuinely run 32 concurrent CPU-bound
            # compressions - on a machine with 10 physical cores (16 logical,
            # hybrid P/E). smart_compression._CPU_BOUND_PREP_GATE already exists
            # to bound exactly this class of work at ~8, but only ever covered
            # decide_and_prepare's baseline-zip + SHA-256 steps; the fast path
            # was added later and never got the same bound.
            #
            # Real measured damage (same folder, same code, two runs): 40 real
            # ~7.9MB JSON files each taking 7s of generic_lzma CPU in isolation
            # took 12.3s each in a good run but 37.9s mean / 96s worst in a bad
            # one, and - because AdmissionGateV4's free-cores reading then saw
            # zero headroom - 50 unrelated CSV files had their candidates
            # PERMANENTLY denied (_filter_ineligible_heavy_candidates drops,
            # it does not retry), falling all the way back to baseline. Net
            # effect: 65.9s/65.46% saved degraded to 222.1s/64.64% saved. Both
            # symptoms, one cause.
            #
            # Reusing the existing gate (rather than adding concurrency, or a
            # second independent limit) keeps total in-process CPU-bound work
            # at the same already-proven bound this codebase settled on, and is
            # a strict REDUCTION in concurrency - the opposite direction from
            # the 2026-08-20 regression, which was caused by RAISING it.
            # The timer starts AFTER the gate is acquired on purpose: policy.
            # evaluate() uses compress_time_s for its throughput check, so
            # queue-wait must never be charged to the candidate itself or a
            # good candidate could be wrongly rejected under load.
            with _FAST_PATH_CPU_GATE:
                t0 = time.perf_counter()
                compress_result = strategy.compress(file_path, work_dir)
                compress_time = time.perf_counter() - t0
            with open(compress_result.output_path, "rb") as f:
                stored_bytes = f.read()
            stored_hash = smart_compression._sha256_bytes(stored_bytes)
            return {
                "status": "PASS",
                "stored_bytes": stored_bytes,
                "stored_size": len(stored_bytes),
                "original_sha256": original_hash,
                "stored_sha256": stored_hash,
                "compress_time_s": compress_time,
                "restore_time_s": 0.0,
            }
        except Exception as exc:  # noqa: BLE001 - a fast-path failure must fall back cleanly, never break archiving
            return {"status": "ERROR", "notes": f"{type(exc).__name__}: {exc}"}
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _run_normal_candidate_fast_admit(name, strategy, file_path, config, original_hash, pool):
        """STALL FIX (this round, real bug found while measuring the fast
        path above): Beta9's own resource_scheduler.ResourceAwareScheduler
        (used internally by smart_compression._run_candidates_pool_aware's
        per-candidate _acquire_admission_standalone call) has a hysteresis
        latch (self._backpressure) - once real RAM crosses its 80%
        high_watermark even ONCE during this process's lifetime, it stays
        latched True and denies EVERY subsequent HEAVY candidate for up to
        the full _ADMISSION_MAX_WAIT_S (60s) retry loop, regardless of
        current real RAM, until RAM drops all the way back down to a 65%
        low_watermark - confirmed directly: a real BMP file's
        bmp_jxl_lossless candidate stalled 36-60+ seconds with real system
        RAM comfortably at 78.7% (well under even the ORIGINAL 80%
        threshold), reproducing on every run once RAM had touched 80%+
        earlier in the same process.

        Beta12's OWN outer gate (_filter_ineligible_heavy_candidates, via
        AdmissionGateV4 above) has ALREADY made the real admit/deny call
        for this exact candidate using CURRENT, non-latching RAM state
        before it ever reached here - a second, buggier check achieves
        nothing but the stall. This calls try_admit() ONCE, non-blocking
        (never the 60s retry loop), purely so Beta9's own scheduler still
        gets correct committed-RAM/in-flight bookkeeping; if momentarily
        denied, dispatch proceeds without a token anyway - the exact same
        "proceed without a token if admission can't be had" fallback the
        retry loop already had as its own end state, just without wasting
        up to 60 pointless seconds getting there.
        """
        try:
            file_size = os.path.getsize(file_path)
        except OSError:
            file_size = None
        token = resource_scheduler.global_scheduler().try_admit(name, file_size) if file_size is not None else None
        session = pool.acquire()
        try:
            work_dir = session.dispatch_one_with_token(strategy, file_path, original_hash, token, 0.0, file_size)
            timeout_s = policy.timeout_for_strategy(name, config)
            _returned_name, result = session.collect_one(name, work_dir, timeout_s)  # collect_one returns (name, result)
            return result
        finally:
            pool.release(session)

    def _run_candidates_with_logging(candidate_specs, file_path, config, original_hash, session=None, pool=None):
        fast_specs = [(n, s) for n, s in candidate_specs if n in _FAST_PATH_STRATEGY_NAMES]
        normal_specs = [(n, s) for n, s in candidate_specs if n not in _FAST_PATH_STRATEGY_NAMES]

        results = []
        if normal_specs:
            if pool is not None:
                # Use the stall-fixed dispatch above - see its own docstring.
                for name, strategy in normal_specs:
                    results.append((name, _run_normal_candidate_fast_admit(name, strategy, file_path, config, original_hash, pool)))
            else:
                results.extend(_ORIGINAL_RUN_CANDIDATES(normal_specs, file_path, config, original_hash, session=session, pool=pool))
        if len(fast_specs) >= 3:
            # CONCURRENT FAST-PATH DISPATCH (real measurement, not
            # theoretical): _run_fast_path_candidate calls are CPU-bound
            # compress() calls into zstandard/brotli/lzma's own C
            # extensions, which release the GIL during the actual
            # compression work - the same reasoning already used elsewhere
            # in this codebase for decide-thread concurrency. Isolation
            # benchmark (outside this module, same strategy classes, real
            # EXE/DLL files, RAM confirmed <70% used for a clean read):
            # EXE (zstd_15+brotli_q9+lzma_x86, sequential sum ~2.2-4.1s)
            # dropped to ~1.6-2.6s concurrent (1.4-1.6x), DLL
            # (~0.53-0.69s sequential) dropped to ~0.39-0.45s concurrent
            # (1.2-1.5x) - consistent across repeated runs at both high
            # (~81%) and normal (~66%) system RAM. Order of `results` is
            # preserved (matches fast_specs order) since downstream
            # selection picks by stored_size, not list position, but
            # preserving order keeps strategy_log_v2 output diffable
            # against the old sequential runs.
            #
            # THRESHOLD RAISED TO >=3 (this round, real regression found on
            # a real 1.54GB "everything except images" folder with 28 real
            # 9.37MB SQL files + real documents mixed in): the original
            # `> 1` threshold applied concurrent dispatch to EVERY 2-
            # candidate case too, including plain text extensions
            # (generic_zstd + generic_lzma, txt/csv/json/py/...) - real
            # isolated A/B on that exact folder proved this was the cause
            # of a genuine slowdown (FINAL 56.8s vs a reverted-to-before
            # baseline's ~40s; confirmed by testing "before + ONLY this
            # concurrent-dispatch change" in isolation, which reproduced
            # the full 56-59s regression on its own). Root cause: when many
            # files are being decided concurrently already (Beta9's own
            # decide-thread pool), spinning up 2 MORE threads per small
            # text file for a marginal 2-candidate race adds real CPU
            # contention against genuinely heavy concurrent work (like
            # those 28 real SQL files' own generic_zstd compute) rather
            # than helping - the original validated win (EXE/DLL, 3 REAL
            # substantial candidates) never needed the `>1` threshold to
            # begin with. Raising to `>=3` restores the original validated
            # EXE/DLL behavor (currently 1 fast candidate there anyway,
            # lzma_x86 alone, unaffected either way after the separate
            # zstd_15/brotli_q9 removal) while no longer paying the 2-
            # candidate contention tax elsewhere. Real measured recovery on
            # the same real folder: 56.8s -> 47.6s (isolated, confirmed).
            with ThreadPoolExecutor(max_workers=len(fast_specs)) as executor:
                futures = [
                    executor.submit(_run_fast_path_candidate, strategy, file_path, original_hash)
                    for _name, strategy in fast_specs
                ]
                for (name, _strategy), future in zip(fast_specs, futures):
                    results.append((name, future.result()))
        else:
            for name, strategy in fast_specs:
                results.append((name, _run_fast_path_candidate(strategy, file_path, original_hash)))

        try:
            records = [(name, result.get("status"), result.get("stored_size")) for name, result in results]
            state = _get_admission_state()
            _STRATEGY_LOG.record_candidates(
                file_path, records,
                heavy_allowed=state.heavy_allowed,
                free_cores=state.free_cores,
            )
        except Exception:  # noqa: BLE001 - a logging bug must never break real archiving
            pass
        return results

    def _record_decision(file_path, size_bytes, result):
        try:
            state = _get_admission_state()
            heavy_allowed = state.heavy_allowed
            free_cores = state.free_cores
            if result is not None:
                _STRATEGY_LOG.record_decision(
                    file_path, result.original_size, result.strategy_name, result.stored_size,
                    heavy_allowed=heavy_allowed, free_cores=free_cores,
                )
            else:
                _STRATEGY_LOG.record_decision(
                    file_path, size_bytes, "baseline_storagearch_zip", None,
                    heavy_allowed=heavy_allowed, free_cores=free_cores,
                )
        except Exception:  # noqa: BLE001
            pass

    def _decide_and_prepare_with_cache(file_path, config=None, disabled_strategies=None, session=None, pool=None):
        try:
            size_bytes = os.path.getsize(file_path)
        except OSError:
            return _ORIGINAL_DECIDE_AND_PREPARE(
                file_path, config=config, disabled_strategies=disabled_strategies, session=session, pool=pool,
            )

        cached, known_hash = MISS, None
        try:
            cached, known_hash = _COMPRESSION_CACHE.lookup_by_size_then_hash(
                size_bytes, lambda: file_analysis.sha256_file(file_path),
            )
        except Exception:  # noqa: BLE001
            pass

        if cached is not MISS:
            result = None if cached is NO_CANDIDATE else copy.deepcopy(cached)
            _record_decision(file_path, size_bytes, result)
            return result

        # SKIP-KNOWN-LOSERS TIME FIX (this round): Beta9's own
        # decide_and_prepare unconditionally computes a full baseline_zip
        # DEFLATE compression BEFORE it ever looks at the candidate list
        # (see smart_compression.py's own decide_and_prepare, the
        # `baseline_result = _BASELINE.compress(...)` call happens before
        # `if not candidate_specs: return None`). For the 6 extensions
        # already routed to an empty candidate list above (SKIP-KNOWN-
        # LOSERS RUNTIME FIX, _CANDIDATE_OVERRIDES_V4[ext] = () for
        # png/gif/tiff/tif/xlsx/docx - real, extensively tested finding
        # that no candidate ever wins for these), decide_and_prepare's own
        # baseline computation is 100% wasted work: real measurement, a
        # 2.58MB XLSX burns ~170-240ms running zlib.Compress across the
        # whole file just to compute a baseline_size that is PROVABLY never
        # read afterward (baseline_size is only used by policy.evaluate()
        # inside the per-candidate loop, which never runs when
        # candidate_specs is empty - confirmed by reading
        # smart_compression.py's decide_and_prepare directly). Skipping the
        # call entirely and returning None here is behaviorally identical
        # to letting it run and hit `if not candidate_specs: return None`
        # itself (same return value, same caller-visible outcome - the
        # caller already treats None as "use the legacy path" regardless of
        # why) - this is a beta12-owned seam-level short-circuit, NOT an
        # edit to any beta9_src file. Extension check reuses the exact
        # override table already established and tested (never duplicated)
        # so it can never drift out of sync with the candidate routing
        # above.
        #
        # TRIED AND REVERTED (same round): moving this same DEFLATE compute
        # INTO the decide phase here (as a real fast-path candidate,
        # fast_baseline_strategy.FastBaselineDeflateStrategy - still in the
        # repo, just unused by this seam) was tested via a real folder-level
        # production benchmark. It made things WORSE (2.54s -> 4.64s wall
        # time on the real 12-file/20MB test folder, confirmed twice) - the
        # hypothesis was that add_folder_to_archive_smart's writer thread
        # serialized this DEFLATE work; the real reason it didn't help is
        # that the writer thread's DEFLATE was ALREADY overlapping with
        # other files' decide-phase work (that's the whole point of the
        # producer/consumer pipeline the docstring on that function
        # describes) - moving it into the decide-thread pool just added
        # MORE CPU contention during the exact window EXE's own heavy
        # lzma_x86 compute needs the cores most, making the real bottleneck
        # (EXE) take longer. Do NOT re-attempt this without a genuinely
        # different mechanism - the write phase is not the bottleneck this
        # was assumed to be.
        extension = os.path.splitext(file_path)[1].lstrip(".").lower()
        if _CANDIDATE_OVERRIDES_V4.get(extension) == ():
            _record_decision(file_path, size_bytes, None)
            try:
                if known_hash is not None:
                    _COMPRESSION_CACHE.store(size_bytes, known_hash, NO_CANDIDATE)
            except Exception:  # noqa: BLE001
                pass
            return None

        # BMP HIGH-ENTROPY JXL PRE-CHECK (this round, real conflicting
        # evidence found via profiling a real large-BMP folder): for large
        # BMPs (_BMP_FULL_LARGE includes bmp_jxl_lossless), this strategy is
        # genuinely NOT a reliable win the way the earlier "skip known
        # losers" extensions were - real measurement THIS session found it
        # on opposite sides of the outcome for two different real BMPs: a
        # 1.44MB flat_graphic BMP WON decisively (3721B, beating every
        # other candidate), while two real 39-50MB "bitmap_full"/
        # "bitmap_secondary" BMPs LOST badly (8.01-10.07s spent, output
        # 8.6% LARGER than original - a real, measured regression that
        # policy.evaluate correctly rejects, but not before paying the full
        # real time cost). A blanket skip (like the png/gif/tiff/xlsx/docx
        # table above) would risk losing the flat_graphic-style win, which
        # the owner explicitly said not to risk - so instead, a cheap
        # sample-based pre-check: compress the file's first 1MB with zstd
        # level 1 (real measured cost: 1.6-1.9ms, negligible) and only
        # allow bmp_jxl_lossless through if that sample shows at least
        # SOME real compressibility. Real validation on this session's
        # exact 3 test files: flat_graphic's 1MB sample compressed 99.5%
        # (correctly predicts WIN), both bitmap_full/bitmap_secondary
        # samples compressed -0.0% (correctly predicts the real 8.6% LOSS
        # for both) - a clean, cheap, correct signal on every real file
        # measured so far. Scoped ONLY to bmp_jxl_lossless via the existing
        # disabled_strategies parameter decide_and_prepare already supports
        # (Cloud Remote Config's own hook, reused here - not a new
        # mechanism) - every other BMP candidate (generic_zstd/generic_zlib)
        # still runs normally regardless of this check's outcome, so a
        # false negative here only costs a missed jxl attempt, never a
        # missed generic-candidate win.
        _SAMPLE_BYTES = 1024 * 1024
        _MIN_SAMPLE_SAVED_PCT = 5.0
        if extension == "bmp" and size_bytes >= strategy_registry._LARGE_ALREADY_COMPRESSED_BYTES:
            try:
                with open(file_path, "rb") as f:
                    sample = f.read(_SAMPLE_BYTES)
                if sample:
                    sample_out = zstandard.ZstdCompressor(level=1).compress(sample)
                    saved_pct = (1 - len(sample_out) / len(sample)) * 100
                    if saved_pct < _MIN_SAMPLE_SAVED_PCT:
                        disabled_strategies = set(disabled_strategies or ()) | {"bmp_jxl_lossless"}
            except Exception:  # noqa: BLE001 - a pre-check bug must never block real archiving
                pass

        result = _ORIGINAL_DECIDE_AND_PREPARE(
            file_path, config=config, disabled_strategies=disabled_strategies, session=session, pool=pool,
        )
        _record_decision(file_path, size_bytes, result)

        file_hash = known_hash if known_hash is not None else getattr(result, "original_sha256", None)
        if file_hash is not None:
            try:
                _COMPRESSION_CACHE.store(
                    size_bytes, file_hash, copy.deepcopy(result) if result is not None else NO_CANDIDATE,
                )
            except Exception:  # noqa: BLE001
                pass
        return result

    selector.select = _select_with_beta12_overlay
    strategy_registry.get_candidate_strategies = _get_candidate_strategies_with_beta12
    smart_compression.decide_and_prepare = _decide_and_prepare_with_cache
    smart_compression._run_candidates = _run_candidates_with_logging

    _ACTIVATED = True


def deactivate_beta12():
    global _ACTIVATED, _ROUTING_ADMISSION_GATE, _ACTIVE_WORKER_TRACKER, _COMPRESSION_CACHE, _STRATEGY_LOG
    if _ACTIVATED:
        if _ORIGINAL_SELECT is not None:
            selector.select = _ORIGINAL_SELECT
        if _ORIGINAL_GET_CANDIDATE_STRATEGIES is not None:
            strategy_registry.get_candidate_strategies = _ORIGINAL_GET_CANDIDATE_STRATEGIES
        if _ORIGINAL_DECIDE_AND_PREPARE is not None:
            smart_compression.decide_and_prepare = _ORIGINAL_DECIDE_AND_PREPARE
        if _ORIGINAL_RUN_CANDIDATES is not None:
            smart_compression._run_candidates = _ORIGINAL_RUN_CANDIDATES
        if _ORIGINAL_POOL_ACQUIRE is not None:
            smart_compression.SmartWorkerPool.acquire = _ORIGINAL_POOL_ACQUIRE
        if _ORIGINAL_POOL_RELEASE is not None:
            smart_compression.SmartWorkerPool.release = _ORIGINAL_POOL_RELEASE
        if _ORIGINAL_DEFAULT_POLICY is not None:
            policy.DEFAULT_POLICY = _ORIGINAL_DEFAULT_POLICY
        _unregister_new_strategies(_REGISTERED_STRATEGY_NAMES)
    _ROUTING_ADMISSION_GATE = None
    _ACTIVE_WORKER_TRACKER = None
    _COMPRESSION_CACHE = None
    _STRATEGY_LOG = None
    _ACTIVATED = False


def is_beta12_active():
    return _ACTIVATED


def get_strategy_log():
    return _STRATEGY_LOG


def get_active_worker_tracker():
    return _ACTIVE_WORKER_TRACKER
