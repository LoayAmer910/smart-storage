"""Beta12 Priority: free-cores-based heavy admission - REPLACES Beta11's
CPU% threshold, per this round's explicit root-cause fix.

Why: Beta11's real 13.46GB/4319-file corpus run showed the CPU%>70
threshold denying jpeg_xl_lossless_jpeg for 67 of 70 real JPEGs, not
because the machine lacked capacity, but because dispatching many files
concurrently through a ThreadPoolExecutor legitimately pushes AGGREGATE
CPU% high even when individual cores sit idle waiting on I/O/IPC. CPU% is
a poor proxy for "is there room to run one more HEAVY candidate" under
real folder-wide concurrent dispatch - the number that actually answers
that question is how many CPU cores are not currently doing real work.

heavy_allowed = free_cores >= min_free_cores (default 2)
free_cores = total_cores - active_worker_threads (floored at 0)

active_worker_threads is a REAL, live count from ActiveWorkerTracker (a
context manager a caller wraps around each unit of real dispatch work -
see integration_layer.py) - never estimated, never a static number.

INCIDENT (this round, found via a real run on the real corpus): the first
version of this gate replaced CPU% with free-cores but ALSO silently
dropped Beta9's own real system-RAM awareness in the process (it checked
only this PROCESS's own memory, never system-wide RAM). Free-cores is
almost always satisfied on a machine where SmartWorkerPool's session
count (hard-capped at 8) is well under the core count, so heavy dispatch
became essentially unrestricted - including for the real 300-400MB images
in the test corpus. Beta9's own resource_scheduler.classify() already
returns a real, per-file-size RAM estimate for HEAVY strategies (e.g.
jpeg_xl_lossless_jpeg's cost scales ~29 bytes of RAM per input byte - a
300MB image projects to several GB for that ONE candidate). Several such
images admitted concurrently (which free-cores alone would allow) can
genuinely exhaust real system RAM and collapse into OS-level paging -
confirmed as the real cause of a run that took ~6 hours instead of the
expected ~4 minutes before being killed.

FIX: this gate now ALSO checks real available system RAM against the
SAME per-file size-aware estimate resource_scheduler.classify() already
computes - reusing Beta9's own real ctypes-based
total_ram_bytes()/available_ram_bytes() measurements, not a new
mechanism. This is in ADDITION to free-cores, not a step back toward
CPU%; the two together are genuinely orthogonal, real signals (cores
available to run more work AND enough RAM for the specific candidate
about to be dispatched), matching what Beta9's own
resource_scheduler.ResourceAwareScheduler already does for RAM alone.
Process-memory (this process's own working set) and disk-pressure remain
as additional, absolute safety nets.
"""

import os
import threading
import time

from beta10_src import resource_admission_v2  # reused for the AdmissionDecision shape only; importing it also
# bootstraps beta9_src onto sys.path (see beta10_src/resource_admission_v2.py's own bootstrap), so the plain
# `import resource_scheduler` below resolves to the real Beta9 module without this file repeating that setup.
from beta11_src.resource_admission_v3 import current_process_rss_mb, disk_usage_percent

import resource_scheduler  # noqa: E402 - real Beta9 module; total_ram_bytes/available_ram_bytes called unmodified

AdmissionDecision = resource_admission_v2.AdmissionDecision

# RAISED from Beta9's own 0.80 default (this round, explicit owner
# approval) - real, repeated observation this round: on the owner's actual
# machine, ordinary day-to-day RAM usage (browser, other apps) regularly
# sits in the high-70s to low-80s%, which meant jpeg_xl_lossless_jpeg (and
# other HEAVY candidates) were being denied even for tiny files with a
# real, substantial RAM margin still available in absolute terms (this
# machine: 16.9GB total, so even at 85% used there is still ~2.5GB free -
# comfortably more than JXL's ~150MB floor estimate, or generic_lzma/
# lzma_x86's few-hundred-MB working set). 0.80 was Beta9's own original
# choice for its SEPARATE per-candidate dispatch-time scheduler
# (resource_scheduler.ResourceAwareScheduler, unmodified, still 0.80) -
# this pre-routing gate is the one this project's own admission decisions
# actually go through when Beta12 is active (confirmed directly: this is
# the exact check that denied JXL - see PROJECTED_RAM_OVER_WATERMARK), so
# raising ONLY this one is deliberate and scoped, not a blanket safety
# rollback. Still a real ceiling, not disabled - a genuinely RAM-starved
# system (85%+) is still protected.
DEFAULT_RAM_HIGH_WATERMARK = 0.85


class ActiveWorkerTracker:
    """A real, thread-safe counter of concurrent units of dispatch work in
    flight right now. Use as a context manager around each real dispatch
    call: `with tracker: decide_and_prepare(...)`. `.current` is the live
    count at any instant - never an estimate."""

    def __init__(self):
        self._lock = threading.Lock()
        self._count = 0

    def __enter__(self):
        with self._lock:
            self._count += 1
        return self

    def __exit__(self, *exc):
        with self._lock:
            self._count -= 1
        return False

    @property
    def current(self):
        with self._lock:
            return self._count


class AdmissionGateV4:
    def __init__(
        self,
        min_free_cores=2,
        total_cores=None,
        max_process_memory_mb=3000.0,
        disk_usage_threshold_percent=90.0,
        disk_pause_ms=100,
        ram_high_watermark=DEFAULT_RAM_HIGH_WATERMARK,
        active_worker_threads_fn=None,
        process_memory_mb_fn=None,
        disk_usage_percent_fn=None,
        ram_available_bytes_fn=None,
        ram_total_bytes_fn=None,
        disk_path=None,
        weight_classifier=None,
    ):
        self.min_free_cores = min_free_cores
        self.total_cores = total_cores or (os.cpu_count() or 4)
        self.max_process_memory_mb = max_process_memory_mb
        self.disk_usage_threshold_percent = disk_usage_threshold_percent
        self.disk_pause_ms = disk_pause_ms
        self.ram_high_watermark = ram_high_watermark

        self._active_worker_threads_fn = active_worker_threads_fn or (lambda: 0)
        self._process_memory_mb_fn = process_memory_mb_fn or current_process_rss_mb
        disk_path = disk_path or (os.path.splitdrive(os.getcwd())[0] + "\\")
        self._disk_usage_percent_fn = disk_usage_percent_fn or (lambda: disk_usage_percent(disk_path))
        # Beta9's own real, ctypes-based measurements - reused directly,
        # never recomputed a second way (see resource_scheduler.py).
        self._ram_available_bytes_fn = ram_available_bytes_fn or resource_scheduler.available_ram_bytes
        self._ram_total_bytes_fn = ram_total_bytes_fn or resource_scheduler.total_ram_bytes
        # Injectable so tests/callers can supply Beta9's own real
        # resource_scheduler.classify without this module having to import
        # it directly (keeps this module free of the beta9_src sys.path
        # bootstrap - the caller, which already has it, injects it).
        self._weight_classifier = weight_classifier

    def free_cores(self, active_worker_threads=None):
        active = active_worker_threads if active_worker_threads is not None else self._safe_call(self._active_worker_threads_fn, cast=int)
        return max(0, self.total_cores - active)

    def evaluate(self, strategy_name, file_size, active_worker_threads=None,
                 process_memory_mb=None, disk_usage_percent_value=None,
                 ram_available_bytes=None, ram_total_bytes=None):
        weight_class = "MEDIUM"
        ram_estimate_bytes = 0
        if self._weight_classifier is not None:
            try:
                weight_class, ram_estimate_bytes = self._weight_classifier(strategy_name, file_size)
            except Exception:  # noqa: BLE001 - never let classification failure break admission
                weight_class = "MEDIUM"
                ram_estimate_bytes = 0

        if weight_class == "LIGHT":
            return AdmissionDecision(True, "LIGHT_ALWAYS_ADMITTED", weight_class, None, None, None, None)

        free = self.free_cores(active_worker_threads)
        if free < self.min_free_cores:
            return AdmissionDecision(False, "INSUFFICIENT_FREE_CORES", weight_class, None, None, None, None)

        # Real, size-aware RAM check - see the module docstring's INCIDENT
        # note for why this exists: free cores alone does not protect
        # against a single large HEAVY candidate (e.g. JXL on a real
        # 300-400MB image, whose RAM cost scales ~29x input size per
        # resource_scheduler.py's own calibration) exhausting real system
        # RAM even when CPU cores are idle.
        available = ram_available_bytes if ram_available_bytes is not None else self._safe_call(self._ram_available_bytes_fn, cast=int)
        total = ram_total_bytes if ram_total_bytes is not None else self._safe_call(self._ram_total_bytes_fn, cast=int)
        if total > 0:
            projected_used_fraction = (total - available + ram_estimate_bytes) / total
            if projected_used_fraction >= self.ram_high_watermark:
                return AdmissionDecision(False, "PROJECTED_RAM_OVER_WATERMARK", weight_class, None, None, None, None)

        mem_mb = process_memory_mb if process_memory_mb is not None else self._safe_call(self._process_memory_mb_fn)
        if mem_mb > self.max_process_memory_mb:
            return AdmissionDecision(False, "PROCESS_MEMORY_OVER_THRESHOLD", weight_class, None, None, None, None)

        disk_pct = disk_usage_percent_value if disk_usage_percent_value is not None else self._safe_call(self._disk_usage_percent_fn)
        if disk_pct > self.disk_usage_threshold_percent:
            time.sleep(self.disk_pause_ms / 1000.0)
            return AdmissionDecision(False, "DISK_PRESSURE_OVER_THRESHOLD", weight_class, None, None, None, None)

        return AdmissionDecision(True, "ADMITTED_FREE_CORES_AVAILABLE", weight_class, None, None, None, None)

    @staticmethod
    def _safe_call(fn, cast=float):
        try:
            value = fn()
        except Exception:  # noqa: BLE001 - a sampler failure must never crash admission
            return 0
        if value is None:
            return 0
        try:
            return cast(value)
        except (TypeError, ValueError):
            return 0
