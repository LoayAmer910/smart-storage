"""Beta 9 Priority #3: resource-token admission scheduler.

Replaces nothing about SmartWorkerPool's fixed session count or dispatch
model - it gates WHEN a HEAVY/MEDIUM candidate's compress task is handed
to its (already-running) worker, based on real system RAM, so concurrent
JXL-class jobs across DIFFERENT files/sessions can't oversubscribe RAM the
way the fixed, resource-blind dispatch did in Beta 8. LIGHT candidates
(baseline, small-file zstd, zlib) are never gated - they are exactly what
should keep filling spare CPU while HEAVY jobs are RAM-bound, matching the
measured Beta 8 problem (RAM ~82%, CPU ~22%).

Cost table calibrated from REAL measurements on this dataset (see
beta9_research/logs/STAGE_GATE_RESULTS.md section C):
  - cjxl.exe on a 40.99MB real JPEG: peak working set 1341MB, 3.98s
  - cjxl.exe on a 1.72MB real JPEG:  peak working set  204MB, 0.67s
Both jpeg_xl_lossless_jpeg and bmp_jxl_lossless shell out to the same
cjxl.exe, so they share this cost model.

Real head-to-head evidence (same section): naive fixed-pool dispatch on a
mixed heavy+light workload hit 99.94% system RAM and collapsed into OS
paging (603.9s); this scheduler kept RAM at 77% and finished in 5.44s.
"""
import ctypes
import threading


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _memory_status():
    stat = _MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    return stat


def available_ram_bytes():
    return _memory_status().ullAvailPhys


def total_ram_bytes():
    return _memory_status().ullTotalPhys


# strategy_name -> (weight_class, ram_estimator(file_size_bytes) -> bytes)
_JXL_SLOPE = (1341_000_000 - 204_000_000) / (40_989_766 - 1_715_764)  # ~29 bytes RAM per byte input
_JXL_FLOOR = 150_000_000


def _jxl_ram(size):
    return max(_JXL_FLOOR, int(_JXL_SLOPE * size) + 60_000_000)


# CALIBRATION FIX (post-Beta-9-v1): bmp_jxl_lossless was sharing _jxl_ram
# with jpeg_xl_lossless_jpeg, but they invoke cjxl in DIFFERENT encoding
# modes with different memory profiles - jpeg_xl_lossless_jpeg uses
# --lossless_jpeg=1 (JPEG-domain transcoding, calibrated from real 41MB/
# 1.7MB JPEG measurements), bmp_jxl_lossless uses --distance 0 Modular
# mode on a PNG-converted raster (real measured peak: 598.6MB for a real
# 147MB BMP - 7.2x LOWER than _jxl_ram(147MB) would have estimated).
# Applying the JPEG-calibrated slope to BMP inputs was a real bug: it
# reserved far more "committed RAM" than BMP encoding actually needs,
# causing unwarranted admission backpressure against OTHER concurrent
# HEAVY candidates (JPEG's own JXL/brotli/lzma work) with no real RAM
# pressure behind it. Single real data point so far (147MB -> 598.6MB,
# ~4.07MB/MB) - floor and multiplier both include real headroom above
# that one measurement pending more calibration points.
_BMP_JXL_RATIO = 598_600_000 / 147_000_054  # ~4.07 bytes RAM per byte input


def _bmp_jxl_ram(size):
    return max(150_000_000, int(_BMP_JXL_RATIO * size * 1.3))  # +30% headroom over the single measured point


def _zstd_ram(size):
    # single-threaded zstd needs roughly window-size + a small multiple of
    # the block being processed, not the whole file - conservative flat
    # estimate well above what was observed (level<=19, no --long)
    return min(size, 32_000_000) + 20_000_000


def _brotli_lzma_ram(size):
    # not measured directly this session - conservative HEAVY placeholder,
    # deliberately pessimistic until real measurement replaces it
    return max(100_000_000, int(size * 3))


def _preflate_ram(size):
    # preflate_tool.exe holds the plaintext + token-predictor state, a
    # modest multiple of the raw deflate member size - much lighter than
    # JXL, and low-volume in this corpus (xlsx only)
    return max(40_000_000, int(size * 4))


def _text_prefilter_ram(size):
    # Whole file + canonical form + zstd level-19 window all held in memory
    # at once (see text_prefilter.py - no streaming yet) - a real, size-
    # scaled estimate, roughly 3x the file size for the three in-memory
    # copies (original bytes, canonical bytes, compressed bytes) plus a
    # flat zstd window floor.
    return max(20_000_000, int(size * 3))


def _video_optimizer_ram(size):
    # ffmpeg -c copy is a container-level stream copy, not a real codec
    # operation - no frame decode/encode buffers, just I/O + a bounded
    # internal packet queue, genuinely LIGHT regardless of file size
    # (unlike a real transcode, which this strategy deliberately never
    # does - see video_optimizer.py). xdelta3's own working set scales
    # with a bounded window, not the whole file. Flat conservative
    # estimate, not size-scaled, since actual usage stays flat.
    return 80_000_000


# REGRESSION FIX (post-Beta-9-v1): generic_zstd was classified MEDIUM,
# putting it behind the SAME backpressure latch as HEAVY JXL work. Since
# most real files in this corpus are >4MB (the size-adaptive LIGHT
# downgrade only applied under 4MB), the vast majority of ordinary zstd
# dispatches - previously fast and unthrottled in Beta 8 - got caught by
# the latch the instant concurrent JXL work pushed RAM past the 80%
# watermark, then serialized one file at a time through a 60s retry/
# backoff loop until the latch cleared (RAM back under 65%) or the safety
# valve expired. Confirmed against a real aborted 10.36GB run: CPU usage
# collapsed from 77% to 23% over ~25 minutes while RAM stayed pinned at
# 72-83% - the RAM never dropped BELOW the low watermark long enough for
# the latch to clear, so ordinary zstd work stayed throttled continuously
# instead of just briefly. The single-file RAM measurements that justified
# the original MEDIUM tier (see _zstd_ram) were never wrong about zstd's
# actual footprint - the bug was gating on that footprint via the SAME
# shared latch as JXL, when zstd was never the resource JXL's admission
# control was built to protect against in the first place. Only the two
# real cjxl.exe-backed strategies keep meaningful gating - that's the
# actual measured, validated win (see the module docstring's real
# head-to-head numbers) and is untouched by this fix.
STRATEGY_CLASS = {
    "jpeg_xl_lossless_jpeg": ("HEAVY", _jxl_ram),
    "bmp_jxl_lossless": ("HEAVY", _bmp_jxl_ram),  # different cjxl encoding mode than JPEG - see _bmp_jxl_ram docstring
    "generic_brotli": ("HEAVY", _brotli_lzma_ram),
    "generic_lzma": ("HEAVY", _brotli_lzma_ram),
    "generic_zstd": ("LIGHT", _zstd_ram),
    "generic_zstd_light": ("LIGHT", _zstd_ram),  # small-file (<4MB) fast path - see adaptive_zstd.py's own size tiers
    "generic_zlib": ("LIGHT", _zstd_ram),
    "preflate_zstd_ooxml": ("LIGHT", _preflate_ram),
    "video_optimizer": ("LIGHT", _video_optimizer_ram),
    "json_prefilter": ("LIGHT", _text_prefilter_ram),
    "csv_prefilter": ("LIGHT", _text_prefilter_ram),
    "xml_prefilter": ("LIGHT", _text_prefilter_ram),
    "baseline_storagearch_zip": ("LIGHT", lambda size: 20_000_000),
}


def classify(strategy_name, file_size):
    """LIGHT strategies are never gated by backpressure regardless of size.
    Any strategy NOT in STRATEGY_CLASS (future/unknown) conservatively
    defaults to MEDIUM, downgraded to LIGHT under 4MB - the same line
    generic_zstd's own size-adaptive level tiers already draw (see
    adaptive_zstd.py's _LEVEL_TIERS), so a new strategy doesn't silently
    inherit unthrottled admission without at least one real measurement
    backing that."""
    weight_class, ram_fn = STRATEGY_CLASS.get(strategy_name, ("MEDIUM", _zstd_ram))
    if weight_class == "MEDIUM" and file_size < 4 * 1024 * 1024:
        weight_class = "LIGHT"
    return weight_class, ram_fn(file_size)


class ResourceAwareScheduler:
    """Token-bucket admission gated on real available RAM, with hysteresis
    to avoid flapping. HEAVY/MEDIUM jobs are throttled hardest; LIGHT jobs
    are always let through (bounded only by a small concurrency cap) so
    spare CPU keeps getting used while heavy jobs are RAM-bound.
    """

    def __init__(self, high_watermark=0.80, low_watermark=0.65,
                 max_light_concurrency=None, max_total_concurrency=16):
        self.high_watermark = high_watermark
        self.low_watermark = low_watermark
        self.max_light_concurrency = max_light_concurrency or max_total_concurrency
        self.max_total_concurrency = max_total_concurrency
        self._lock = threading.Lock()
        self._committed_ram = 0
        self._in_flight = 0
        self._in_flight_light = 0
        self._backpressure = False  # hysteresis latch

    def _current_used_fraction(self):
        total = total_ram_bytes()
        avail = available_ram_bytes()
        used = total - avail
        return (used + self._committed_ram) / total

    def try_admit(self, strategy_name, file_size):
        """Returns a context-manager token on success, or None if the
        caller should back off and retry shortly. Never blocks itself -
        callers own their own retry/backoff loop (see dispatch_one in
        smart_compression.py)."""
        weight_class, ram_estimate = classify(strategy_name, file_size)

        with self._lock:
            if self._in_flight >= self.max_total_concurrency:
                return None

            used_frac = self._current_used_fraction()
            if used_frac >= self.high_watermark:
                self._backpressure = True
            elif used_frac <= self.low_watermark:
                self._backpressure = False

            if weight_class == "LIGHT":
                if self._in_flight_light >= self.max_light_concurrency:
                    return None
                self._committed_ram += ram_estimate
                self._in_flight += 1
                self._in_flight_light += 1
                return _Token(self, ram_estimate, weight_class)

            projected = (total_ram_bytes() - available_ram_bytes() + self._committed_ram + ram_estimate) / total_ram_bytes()
            if self._backpressure or projected >= self.high_watermark:
                return None

            self._committed_ram += ram_estimate
            self._in_flight += 1
            return _Token(self, ram_estimate, weight_class)

    def _release(self, ram_estimate, weight_class):
        with self._lock:
            self._committed_ram -= ram_estimate
            self._in_flight -= 1
            if weight_class == "LIGHT":
                self._in_flight_light -= 1


class _Token:
    def __init__(self, scheduler, ram_estimate, weight_class):
        self._scheduler = scheduler
        self._ram_estimate = ram_estimate
        self._weight_class = weight_class
        self._released = False

    def release(self):
        if not self._released:
            self._released = True
            self._scheduler._release(self._ram_estimate, self._weight_class)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# Process-wide singleton - SmartWorkerPool's sessions run in the SAME
# parent process (each session is one child worker process, but dispatch
# always happens from parent-process threads), so one shared scheduler
# instance is what makes admission control actually cross-file/cross-
# session instead of being scoped per-session (which would defeat the
# purpose - the real RAM contention is BETWEEN concurrently-dispatching
# sessions, not within one).
_GLOBAL_SCHEDULER = ResourceAwareScheduler()


def global_scheduler():
    return _GLOBAL_SCHEDULER
