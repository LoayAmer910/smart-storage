"""Benefit-vs-cost decision policy for the Smart Selector.

This is the one place that decides whether a byte-exact candidate is
worth using over the current StorageArch baseline. It is deliberately a
single configurable object, not scattered thresholds, so Phase 3 can tune
it without touching selection logic.

DEFAULTS AND WHY (calibrated from real Phase 1 numbers, not a search):

- min_meaningful_saved_bytes = 1 MiB
  A saving smaller than this is noise for storage-planning purposes on
  any file size we've actually seen. It exists specifically so a huge
  file with a small percentage (the user's "2GB PNG at 1%" example, which
  still saves ~20MB) can qualify on ABSOLUTE grounds even when it misses
  the percentage bar below.

- min_meaningful_saved_percent = 3.0%
  Phase 1's own per-format recommendations split almost exactly here:
  formats recommended "CURRENT STORAGEARCH FALLBACK" (PNG 1.01%, WebP
  0.13%, AVIF 0.18% byte-weighted) all sit under 3%; formats recommended
  as worth probing (BMP 17.33%, TIFF 11.03%, GIF 9.25%, JPEG 13.94%) sit
  far above it. 3% is the natural cut line already implied by that data.

- min_throughput_mb_per_s = 2.0
  Below the fastest observed strategy speeds by a wide margin (Phase 1
  medians were almost all sub-100ms even on multi-MB files), but still
  clearly excludes a genuinely slow candidate (call it >0.5s/MB) unless
  the win is large enough to justify it (see the override below).
  Throughput alone is misleading on small files, where fixed per-call
  overhead (e.g. cjxl.exe subprocess startup) dominates the MB/s math
  even though the real cost is trivial - see small_absolute_time_seconds.

- small_absolute_time_seconds = 1.0
  A second, independent escape hatch for the throughput floor: if total
  processing time is under one second, the candidate is cheap enough to
  accept regardless of what the MB/s rate computes to on a small file.
  Set well above every Phase 1 median (<0.1-0.5s) so it never masks a
  genuinely slow candidate on a file large enough for that to matter.

- large_benefit_bytes_override = 50 MiB / large_benefit_percent_override = 20%
  A candidate this good is allowed to ignore the throughput floor - Phase
  1's BMP median improvement (61.17%!) and best cases (up to 96.67%) show
  real candidates legitimately this good exist, and they should not be
  punished for being somewhat slower on larger inputs.

- absolute_timeout_seconds = 90.0
  Hard per-candidate ceiling regardless of benefit, for strategies NOT
  listed in slow_probe_strategy_names below (generic_zstd,
  jpeg_xl_lossless_jpeg, generic_zlib, and any future strategy). Phase 1's
  original 30.0s value was calibrated only against single-file,
  uncontended measurements (worst-case average ~2s) - never against the
  real 8-way SmartWorkerPool contention of an actual multi-thousand-file
  folder Archive. Confirmed directly as a real regression: enabling
  jpeg_xl_lossless_jpeg (a genuine, independently-verified improvement -
  see JpegXlLosslessStrategy) added real CPU-bound work across the same
  fixed 8-worker pool used by every OTHER file's candidates too - a real
  ~10.36GB/~2,915-file run showed worst_processing_seconds for `ipch`
  (a type that never touches JPEG/JXL at all) rising from 174.4s to
  219.6s between the pre- and post-JXL runs, direct proof of increased
  pool-wide contention, not something specific to JPEG. Large multi-
  candidate files (e.g. real 147MB BMPs, previously winning ~60% real
  savings via generic_zstd) were pushed entirely to baseline - not
  because their winning candidate got WORSE, but because the added
  contention was enough to make it miss the old 30s ceiling under real
  load, killing genuinely-still-in-progress-and-about-to-succeed work.
  Tripling the ceiling gives real WINNING candidates the same headroom a
  single-file measurement always had.

- slow_probe_timeout_seconds = 30.0 / slow_probe_strategy_names =
  {generic_brotli, generic_lzma}
  A SEPARATE, shorter ceiling for the two candidates directly measured
  (real content, real timing) to almost never win against generic_zstd
  regardless of file type - brotli(quality=11)/lzma(preset=9) are
  extremely slow on large inputs (measured: 55s on a real 41MB JPEG,
  ~197s extrapolated on a real 147MB BMP, both for single-digit-percent
  or worse improvement over generic_zstd, which finishes in a few
  seconds) yet were STILL tried for every JPEG/BMP/PNG/TIFF/GIF, each one
  now waiting up to the FULL absolute_timeout_seconds (90s) before being
  killed. Giving every candidate the 90s ceiling to fix the BMP
  regression above reintroduced a different real regression: real total
  Archive runtime on the same ~10.36GB/~2,915-file folder went from a
  ~16-minute baseline to ~25 minutes, because doomed-anyway probes that
  used to fail fast at 30s now failed slow at 90s, hundreds of times
  over. Keeping brotli/lzma at the original 30s (their own real
  timing shows they need far less than that to prove themselves not
  worth it) while only the strategies that actually WIN under real
  contention (generic_zstd, jpeg_xl_lossless_jpeg) get the full 90s
  restores real total runtime without giving up any of the BMP-regression
  fix's compression gains - neither strategy is removed, disabled, or
  skipped; both are still tried for every applicable file, exactly as
  before, just bounded by a ceiling that matches their own real-world
  behavior instead of one flat number for every strategy regardless of
  how differently they actually perform.

- tiny_file_bytes = 4096
  Below this, Phase 1 repeatedly showed container/format overhead making
  "compressed" output BIGGER than the original (icons, 278-byte PNGs).
  Not worth even attempting a non-baseline candidate.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyConfig:
    min_meaningful_saved_bytes: int = 1 * 1024 * 1024
    min_meaningful_saved_percent: float = 3.0
    min_throughput_mb_per_s: float = 2.0
    small_absolute_time_seconds: float = 1.0
    large_benefit_bytes_override: int = 50 * 1024 * 1024
    large_benefit_percent_override: float = 20.0
    absolute_timeout_seconds: float = 90.0
    slow_probe_timeout_seconds: float = 30.0
    slow_probe_strategy_names: frozenset = frozenset({"generic_brotli", "generic_lzma"})
    tiny_file_bytes: int = 4096


DEFAULT_POLICY = PolicyConfig()


def timeout_for_strategy(strategy_name, config=DEFAULT_POLICY):
    """Per-candidate dispatch/kill timeout - see slow_probe_timeout_seconds'
    own docstring above for why this isn't one flat number for every
    strategy. Used to bound how long a candidate's worker process is
    allowed to run before being terminated and treated as TIMEOUT; NOT
    used by evaluate()'s own EXCEEDS_ABSOLUTE_TIMEOUT check, which always
    compares against the strategy-agnostic absolute_timeout_seconds since
    by the time evaluate() runs, dispatch-level timeout enforcement has
    already guaranteed total_time can never exceed whichever ceiling this
    function returned for that candidate anyway.
    """
    if strategy_name in config.slow_probe_strategy_names:
        return config.slow_probe_timeout_seconds
    return config.absolute_timeout_seconds


@dataclass(frozen=True)
class PolicyDecision:
    accepted: bool
    reason: str
    saved_bytes: int
    saved_percent: float
    throughput_mb_per_s: float


def evaluate(
    original_size,
    baseline_size,
    candidate_size,
    compression_time_s,
    restore_time_s,
    sha256_match,
    config=DEFAULT_POLICY,
):
    """Decides whether a verified candidate should replace the baseline.

    Does NOT re-verify SHA-256 - the caller must only invoke this after
    exact-restore verification, and sha256_match is passed through purely
    as the final non-negotiable gate.
    """
    total_time = (compression_time_s or 0.0) + (restore_time_s or 0.0)
    saved_bytes = baseline_size - candidate_size
    saved_percent = (saved_bytes / baseline_size * 100) if baseline_size else 0.0
    size_mb = max(original_size, 1) / (1024 * 1024)
    throughput = size_mb / total_time if total_time > 0 else float("inf")

    if not sha256_match:
        return PolicyDecision(False, "SHA256_MISMATCH", saved_bytes, saved_percent, throughput)

    if candidate_size >= baseline_size:
        return PolicyDecision(False, "NOT_SMALLER_THAN_BASELINE", saved_bytes, saved_percent, throughput)

    if total_time > config.absolute_timeout_seconds:
        return PolicyDecision(False, "EXCEEDS_ABSOLUTE_TIMEOUT", saved_bytes, saved_percent, throughput)

    benefit_ok = (
        saved_bytes >= config.min_meaningful_saved_bytes
        or saved_percent >= config.min_meaningful_saved_percent
    )
    if not benefit_ok:
        return PolicyDecision(False, "BENEFIT_TOO_SMALL", saved_bytes, saved_percent, throughput)

    large_benefit = (
        saved_bytes >= config.large_benefit_bytes_override
        or saved_percent >= config.large_benefit_percent_override
    )
    cost_ok = (
        large_benefit
        or throughput >= config.min_throughput_mb_per_s
        or total_time <= config.small_absolute_time_seconds
    )
    if not cost_ok:
        return PolicyDecision(False, "COST_NOT_JUSTIFIED_BY_BENEFIT", saved_bytes, saved_percent, throughput)

    return PolicyDecision(True, "ACCEPTED", saved_bytes, saved_percent, throughput)


def should_attempt_candidates(original_size, config=DEFAULT_POLICY):
    """Cheap pre-filter: skip non-baseline candidates entirely for tiny files."""
    return original_size >= config.tiny_file_bytes
