"""Phase 3 integration adapter: bridges the tested Phase 2 Smart Selector
engine (experiments/smart_selector/) into the production archive/checkout/
checkin flow.

Reuses the exact tested components - file_analysis, strategy_registry,
policy, and the strategy instances (including the .ipch classification fix
and the size-adaptive zstd fix) - rather than reimplementing any selection
logic. This module only adds the two things Phase 2's benchmark harness
didn't need: returning the actual stored bytes (not just their size), and
a restore path that hands back original bytes for the caller to write out.

Every accepted decision has already been through a full compress -> restore
-> SHA-256 verify cycle before being returned - archive_manager_smart.py
never has to (and must never) trust a candidate without that having
happened first.
"""

import hashlib
import multiprocessing
import os
import queue as queue_module
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SMART_SELECTOR_SRC = os.path.join(_HERE, "experiments", "smart_selector", "src")
_IMAGES_SRC = os.path.join(_HERE, "experiments", "images", "src")
_VIDEO_SRC = os.path.join(_HERE, "experiments", "video", "src")
_TEXT_FILTERS_SRC = os.path.join(_HERE, "experiments", "text_filters", "src")
for _p in (_SMART_SELECTOR_SRC, _IMAGES_SRC, _VIDEO_SRC, _TEXT_FILTERS_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import file_analysis  # noqa: E402
import policy  # noqa: E402
import strategy_registry  # noqa: E402
import resource_scheduler  # noqa: E402 - Beta 9 Priority #3, see its own module docstring
from selector import _STRATEGY_INSTANCES  # noqa: E402 - the exact tested strategy set/config
from strategies.baseline_zip import BaselineZipStrategy  # noqa: E402

# Safety valve for resource_scheduler admission: if real system RAM stays
# pinned above the high watermark by something OUTSIDE this program for
# this long, stop waiting and dispatch anyway rather than risk a new class
# of hang/timeout regression - RAM protection here is best-effort, it must
# never become a liveness risk the way the old fixed-pool model never was.
_ADMISSION_MAX_WAIT_S = 60.0
_ADMISSION_POLL_INTERVAL_S = 0.1

# CPU-bound-work gate for decide_and_prepare's baseline-zip-compress + full-
# file-SHA256-hash steps. Both run on the CALLING decision-dispatch thread,
# BEFORE that file's candidates ever touch the 8-slot SmartWorkerPool - so
# they are NOT bounded by pool.size the way real candidate compression is.
# archive_manager_smart.py's decide_thread_count (min(64, max(pool.size*4,
# 16)) = 32 on an 8-slot pool) was sized to fix a DIFFERENT problem (HEAVY
# candidates holding a session hostage for the whole RAM-admission wait,
# starving other files' ready work - see _run_candidates_pool_aware's
# docstring) and was validated against that starvation symptom specifically.
# It has a real side effect this gate targets: up to 32 threads can now run
# baseline-zip-compress+hash - genuine CPU-bound work, not idle waiting -
# fully concurrently, on top of the 8 real worker PROCESSES already doing
# real candidate compression. On a 16-logical-core dev machine that is up
# to 40 concurrent CPU-bound things for 16 cores, and was measured (real
# full 4063-file/10.4GB run, 2026-08-20T17:26:12) causing severe CPU
# starvation of individual candidates: 21 files that used to accept
# generic_zstd fell back to no compression at all (candidate timeout under
# contention) and all 3 bmp_jxl_lossless candidates for the large BMPs
# likewise timed out and fell back to generic_zstd - both a slower pipeline
# (+75.349s vs the prior good baseline) and a larger archive. Bounding
# concurrent baseline+hash work to roughly the real worker-process count
# keeps total CPU-bound concurrency near the actual core budget without
# touching decide_thread_count itself (still 32 - files still queue up
# "ready" to admit/dispatch exactly as the starvation fix intended) or the
# admission-wait/session-decoupling architecture at all.
_CPU_BOUND_PREP_GATE = threading.Semaphore(max(1, min(multiprocessing.cpu_count(), 8)))

# Restore-timeout hardening. Root-caused with a real reproduction, not
# assumed: restoring a real 147MB BMP via bmp_jxl_lossless (cjxl/djxl
# subprocess decode) took only 7.7-7.8s in isolation, but 30.48-30.70s -
# JUST over the previous flat 30s default - when run through
# checkout_folder_smart's real concurrent restore pool (up to
# SmartWorkerPool.size, i.e. up to 8, simultaneous restores competing for
# the same CPU cores). generic_zstd/zlib/brotli/lzma/preflate restores are
# in-process and were never observed anywhere near this - the 30s default
# stays completely unchanged for them and for jpeg_xl_lossless_jpeg/
# bmp_jxl_lossless on small/medium files. Only the two strategies that
# shell out to a slow external decoder, and only once original file size
# makes that decode genuinely large, get a longer, size-scaled budget.
_SLOW_DECODE_RESTORE_STRATEGIES = frozenset({"jpeg_xl_lossless_jpeg", "bmp_jxl_lossless"})
_RESTORE_TIMEOUT_FLOOR_S = 30.0
# Calibrated from the measured 147MB/~30.7s-under-concurrent-load data
# point with a real safety margin (not just barely over): scaled so a
# 147MB file lands at 90s - the SAME absolute ceiling this codebase
# already trusts elsewhere for this exact strategy family on the compress
# side (policy.absolute_timeout_seconds), not a new/arbitrary number.
_RESTORE_TIMEOUT_MB_AT_90S = 147_000_054 / (1024 * 1024)
_RESTORE_TIMEOUT_SLOPE_S_PER_MB = 90.0 / _RESTORE_TIMEOUT_MB_AT_90S


def restore_timeout_for(strategy_name, original_size_bytes):
    """Per-strategy, size-aware restore timeout - see the module-level
    comment above for the real measurement this is calibrated from. Any
    strategy not in _SLOW_DECODE_RESTORE_STRATEGIES (or a size we don't
    know) keeps the exact previous flat 30s default - completely
    unaffected by this change."""
    if strategy_name not in _SLOW_DECODE_RESTORE_STRATEGIES or not original_size_bytes:
        return _RESTORE_TIMEOUT_FLOOR_S
    size_mb = original_size_bytes / (1024 * 1024)
    return max(_RESTORE_TIMEOUT_FLOOR_S, size_mb * _RESTORE_TIMEOUT_SLOPE_S_PER_MB)

# Beta 9 diagnostic-only profiling collector (see beta9_research/scratch/
# profile_pipeline.py). Zero-cost when PROFILE_ENABLED is False (the
# default) - a single bool check per candidate, no lock/allocation. Records
# (strategy_name, file_size, admission_wait_s, candidate_wall_s) so a real
# benchmark run can show exactly where time goes without guessing.
PROFILE_ENABLED = False
_profile_lock = threading.Lock()
_profile_records = []


def profile_reset():
    with _profile_lock:
        _profile_records.clear()


def profile_dump():
    with _profile_lock:
        return list(_profile_records)


def _profile_record(strategy_name, file_size, admission_wait_s, candidate_wall_s):
    with _profile_lock:
        _profile_records.append((strategy_name, file_size, admission_wait_s, candidate_wall_s))

_MP_CONTEXT = multiprocessing.get_context("spawn")
_BASELINE = BaselineZipStrategy()

BASELINE_STRATEGY_NAME = "baseline_storagearch_zip"


class SmartRestoreError(Exception):
    """Raised when a smart-stored representation cannot be restored to the
    exact original bytes. Callers must treat this as a hard integrity
    error, never silently return a different file."""


@dataclass
class SmartDecision:
    strategy_name: str
    stored_bytes: bytes
    stored_size: int
    original_size: int
    original_sha256: str
    stored_sha256: str
    compress_time_s: float
    restore_time_s: float


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _compress_restore_verify(strategy, file_path, work_dir, original_hash):
    """The actual candidate work (compress -> restore -> SHA-256 verify),
    factored out of _storage_worker so _persistent_storage_worker (see
    _run_candidates) can run the same logic for many candidates inside one
    long-lived process instead of duplicating it.

    `original_hash` is computed ONCE per file by the caller (decide_and_
    prepare) and passed in here rather than recomputed - this used to call
    _sha256_file(file_path) itself on every single candidate, meaning a
    file routed to N candidates (JPEGs get 5, PNG/BMP/TIFF/GIF >=32KB get
    4 - see strategy_registry.py) paid N FULL extra reads of the same
    unchanged original file just to hash it identically N times, on top of
    the read each candidate's own compress() call already needs. Confirmed
    as a real, significant cost on a real Beta test: for image-heavy
    folders (JPEG/PNG dominate multi-candidate routing) this multiplies
    disk I/O for that portion of the data several-fold, which matters most
    exactly when disk (not CPU) is the bottleneck - consistent with a real
    ~10GB folder Archive staying near 0% CPU while still not finishing.
    """
    try:
        t0 = time.perf_counter()
        compress_result = strategy.compress(file_path, work_dir)
        compress_time = time.perf_counter() - t0

        with open(compress_result.output_path, "rb") as f:
            stored_bytes = f.read()
        stored_hash = _sha256_bytes(stored_bytes)

        t0 = time.perf_counter()
        restore_result = strategy.restore(compress_result.output_path, work_dir, os.path.basename(file_path))
        restore_time = time.perf_counter() - t0

        restored_hash = _sha256_file(restore_result.output_path)
        exact_match = restored_hash == original_hash

        return {
            "status": "PASS" if exact_match else "FAIL_HASH_MISMATCH",
            "stored_bytes": stored_bytes if exact_match else None,
            "stored_size": len(stored_bytes),
            "original_sha256": original_hash,
            "stored_sha256": stored_hash,
            "compress_time_s": compress_time,
            "restore_time_s": restore_time,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "ERROR", "notes": f"{type(exc).__name__}: {exc}"}


def _storage_worker(strategy, file_path, work_dir, result_queue, original_hash):
    """Runs in a spawned child process (real-kill timeout support - see
    _run_candidate). Unlike the Phase 2 benchmark harness, this keeps the
    compressed bytes (needed to actually store them), not just their size.
    """
    try:
        result_queue.put(_compress_restore_verify(strategy, file_path, work_dir, original_hash))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _restore_and_verify(strategy, stored_bytes, work_dir, original_filename, expected_original_sha256):
    """Restore-only counterpart to _compress_restore_verify - decodes an
    already-chosen stored representation back to original bytes and checks
    it against the hash recorded when it was accepted, without redoing any
    compression. Factored out so both the one-shot _restore_worker (single-
    file checkout_item_smart) and the persistent worker's "restore" task
    (checkout_folder_smart, via SmartWorkerSession.run_restore) share the
    exact same logic instead of two independent implementations that could
    drift apart on what "restore" actually verifies."""
    try:
        stored_path = os.path.join(work_dir, "stored" + getattr(strategy, "extension", ".bin"))
        with open(stored_path, "wb") as f:
            f.write(stored_bytes)
        restore_result = strategy.restore(stored_path, work_dir, original_filename)
        with open(restore_result.output_path, "rb") as rf:
            restored_bytes = rf.read()
        restored_hash = _sha256_bytes(restored_bytes)
        if restored_hash != expected_original_sha256:
            return {"status": "FAIL_HASH_MISMATCH", "notes": "restored bytes did not match expected original SHA-256"}
        return {"status": "PASS", "data": restored_bytes}
    except Exception as exc:  # noqa: BLE001
        return {"status": "ERROR", "notes": f"{type(exc).__name__}: {exc}"}


def _persistent_storage_worker(task_queue, result_queue):
    """Same per-candidate work as _storage_worker, but loops handling many
    tasks in a single spawned process instead of exiting after one - see
    _run_candidates' docstring for why that matters on a frozen PyInstaller
    onefile build. Reads tagged tuples from task_queue until it reads the
    `None` sentinel, which ends the loop and lets the process exit.

    Two task shapes, distinguished by a leading tag so one persistent
    worker (and one SmartWorkerSession) can serve both archive-side
    candidate evaluation AND checkout-side restore, instead of restore
    needing its own separate spawn-per-call path (see SmartWorkerSession.
    run_restore and checkout_folder_smart, which used to spawn one fresh
    process per smart-stored folder member with no pooling at all - the
    confirmed cause of the real post-commit "Archiving..." hang on a large
    folder with many smart-stored members):
      ("compress", strategy, file_path, work_dir, original_hash)
      ("restore", strategy, stored_bytes, work_dir, original_filename, expected_original_sha256)
    """
    while True:
        task = task_queue.get()
        if task is None:
            return
        tag = task[0]
        if tag == "compress":
            _tag, strategy, file_path, work_dir, original_hash = task
            try:
                result_queue.put(_compress_restore_verify(strategy, file_path, work_dir, original_hash))
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
        else:
            _tag, strategy, stored_bytes, work_dir, original_filename, expected_original_sha256 = task
            try:
                result_queue.put(_restore_and_verify(strategy, stored_bytes, work_dir, original_filename, expected_original_sha256))
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)


def _run_candidate(strategy, file_path, timeout_s):
    """Real-kill timeout wrapper, same mechanism as Phase 2's selector.py -
    a slow/hung candidate is genuinely terminated, never just abandoned.

    Reads result_queue BEFORE joining the process - not after. _storage_
    worker's result includes the full compressed payload, which for any
    non-trivial file exceeds a single pipe buffer (~128KB on Windows); the
    Queue's writer only truly exits once its background feeder thread has
    finished flushing every byte to the pipe. Joining first (i.e. waiting
    for the child to exit) while nothing is draining that pipe is a classic
    multiprocessing deadlock - the child blocks writing to a full pipe, the
    parent blocks in join() waiting for a child that can't proceed. Always
    drain the queue first, exactly as the multiprocessing docs prescribe."""
    work_dir = tempfile.mkdtemp(prefix="smartcompress_")
    result_queue = _MP_CONTEXT.Queue()
    original_hash = _sha256_file(file_path)
    process = _MP_CONTEXT.Process(target=_storage_worker, args=(strategy, file_path, work_dir, result_queue, original_hash))
    process.start()

    try:
        result = result_queue.get(timeout=timeout_s)
    except queue_module.Empty:
        if process.is_alive():
            process.terminate()
            process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
            result_queue.close()
            shutil.rmtree(work_dir, ignore_errors=True)
            return {"status": "TIMEOUT", "notes": f"exceeded {timeout_s}s - candidate terminated"}
        result_queue.close()
        return {"status": "ERROR", "notes": "candidate process exited without a result"}

    result_queue.close()
    process.join(2)
    if process.is_alive():
        process.terminate()
        process.join(2)
    return result


def _acquire_admission_standalone(strategy_name, file_path):
    """Module-level twin of SmartWorkerSession._acquire_admission, usable
    WITHOUT holding any SmartWorkerSession/pool slot while it blocks - see
    _run_candidates_pool_aware, which calls this BEFORE acquiring a pool
    session specifically so that a candidate waiting up to
    _ADMISSION_MAX_WAIT_S for real RAM headroom does NOT tie up one of the
    pool's small, fixed number of worker-process slots for the whole wait -
    other files' ready candidates can still be dispatched on the other
    sessions the entire time this one is waiting. Same safety contract as
    the method version: never raises, same real-RAM admission check, same
    _ADMISSION_MAX_WAIT_S ceiling, same "proceed without a token if the
    wait expires" fallback (identical to Beta 8's unconditional dispatch) -
    only WHERE the wait happens changes, never whether/how long it waits or
    what it protects against. Returns (token_or_None, wait_seconds,
    file_size_or_None)."""
    t0 = time.monotonic()
    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        return None, time.monotonic() - t0, None
    scheduler = resource_scheduler.global_scheduler()
    deadline = t0 + _ADMISSION_MAX_WAIT_S
    while True:
        token = scheduler.try_admit(strategy_name, file_size)
        if token is not None:
            return token, time.monotonic() - t0, file_size
        if time.monotonic() >= deadline:
            return None, time.monotonic() - t0, file_size
        time.sleep(_ADMISSION_POLL_INTERVAL_S)


def _terminate_worker(process):
    if process.is_alive():
        process.terminate()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join(2)


class SmartWorkerSession:
    """Reuses ONE persistent Smart Compression worker process across MANY
    decide_and_prepare() calls - e.g. every file in a folder Archive -
    instead of _run_candidates spawning and tearing down its own worker
    process for each call (the default when no session is passed in).

    Why this matters, measured directly on the real Beta build: a frozen
    PyInstaller --onefile EXE re-extracts its entire packed archive on
    EVERY spawned child process, before any Python code runs (the
    bootloader's native self-extraction happens first). _run_candidates
    already fixed this within a single file (one worker reused across that
    file's candidates, see its own docstring) - but add_folder_to_archive_
    smart still called decide_and_prepare once per file with no session,
    so a folder still spawned (and paid the onefile self-extraction cost
    of) one fresh worker PER FILE. A real Beta test archiving a ~10.36GB/
    ~2,900-file folder confirmed this directly: ~25+ minutes with ~0% CPU
    and ~0 disk activity - consistent with thousands of sequential process
    spawns each mostly idle waiting on OS-level extraction/antivirus
    scanning of the newly-extracted onefile copy, not real compression
    work. Passing one SmartWorkerSession through decide_and_prepare for
    every file in the folder pays that spawn cost ONCE for the whole
    folder Archive operation instead of once per file.

    Safe to share across files: each run_candidates() call is still a
    plain synchronous request/response over the session's own queues -
    this only avoids re-spawning the OS process itself, not any of
    _run_candidates' per-candidate timeout/replacement safety (a hung/dead
    worker is still terminated and replaced with a fresh one, exactly as
    before - the session's process reference is simply updated in place so
    later files in the same folder keep using the session transparently).
    """

    def __init__(self):
        self._task_queue = _MP_CONTEXT.Queue()
        self._result_queue = _MP_CONTEXT.Queue()
        self._process = self._spawn()

    def _spawn(self):
        process = _MP_CONTEXT.Process(target=_persistent_storage_worker, args=(self._task_queue, self._result_queue))
        process.start()
        return process

    def run_candidates(self, candidate_specs, file_path, config, original_hash):
        """`config` (a policy.PolicyConfig) instead of one flat timeout - each
        candidate is bounded by policy.timeout_for_strategy(name, config),
        not the same ceiling for every strategy regardless of how
        differently they actually perform under real contention (see that
        function's own docstring)."""
        results = []
        for name, strategy in candidate_specs:
            work_dir = self.dispatch_one(strategy, file_path, original_hash)
            timeout_s = policy.timeout_for_strategy(name, config)
            results.append(self.collect_one(name, work_dir, timeout_s))
        return results

    def dispatch_one(self, strategy, file_path, original_hash):
        """Enqueues a single candidate task WITHOUT waiting for the result -
        pairs with collect_one() so the calling thread can do other real
        work (see decide_and_prepare's baseline-overlap path, used when a
        file has exactly one candidate - the common case for the types
        that dominated real Smart Pipeline wall-clock time, CSV/.ipch/
        generic_zstd) while the worker computes this candidate concurrently
        in its own process, instead of the calling thread sitting idle
        blocked on result_queue.get() the whole time. Returns the task's
        work_dir (needed by collect_one for cleanup on a timeout).
        """
        work_dir = tempfile.mkdtemp(prefix="smartcompress_")
        self._pending_strategy_name = strategy.name
        self._pending_file_size = None
        self._pending_dispatch_t0 = time.perf_counter()
        self._pending_token, self._pending_admission_wait = self._acquire_admission(strategy.name, file_path)
        self._task_queue.put(("compress", strategy, file_path, work_dir, original_hash))
        return work_dir

    def dispatch_one_with_token(self, strategy, file_path, original_hash, token, admission_wait, file_size):
        """Same as dispatch_one, except the admission token was already
        acquired by the CALLER before this session was even taken from the
        pool (see _run_candidates_pool_aware and _acquire_admission_
        standalone) - skips the internal wait entirely since it already
        happened, session-free. Everything downstream (task enqueue,
        profiling bookkeeping, collect_one's token release) is identical to
        dispatch_one - this only changes WHEN/WHERE the admission wait
        itself happened, never what it checked or how long it could wait.
        """
        work_dir = tempfile.mkdtemp(prefix="smartcompress_")
        self._pending_strategy_name = strategy.name
        self._pending_file_size = file_size
        self._pending_dispatch_t0 = time.perf_counter()
        self._pending_token = token
        self._pending_admission_wait = admission_wait
        self._task_queue.put(("compress", strategy, file_path, work_dir, original_hash))
        return work_dir

    def _acquire_admission(self, strategy_name, file_path):
        """Blocks (LIGHT candidates never wait - see ResourceAwareScheduler)
        until real system RAM has headroom for this candidate, or until
        _ADMISSION_MAX_WAIT_S elapses, whichever comes first. Never raises -
        a failure to admit within the safety window just proceeds without a
        token, exactly like Beta 8's unconditional dispatch. Returns
        (token_or_None, wait_seconds) - the wait time is always measured
        (cheap: two time.monotonic() calls), independent of PROFILE_ENABLED,
        so profiling can be toggled after the fact from recorded runs."""
        t0 = time.monotonic()
        try:
            file_size = os.path.getsize(file_path)
            self._pending_file_size = file_size
        except OSError:
            return None, time.monotonic() - t0
        scheduler = resource_scheduler.global_scheduler()
        deadline = t0 + _ADMISSION_MAX_WAIT_S
        while True:
            token = scheduler.try_admit(strategy_name, file_size)
            if token is not None:
                return token, time.monotonic() - t0
            if time.monotonic() >= deadline:
                return None, time.monotonic() - t0
            time.sleep(_ADMISSION_POLL_INTERVAL_S)

    def collect_one(self, name, work_dir, timeout_s):
        """Blocks for up to `timeout_s` collecting the result of a task
        already enqueued via dispatch_one (or, from run_candidates, one
        dispatched immediately before this call) - same per-candidate
        timeout/dead-worker-replacement safety run_candidates always had,
        just factored out so dispatch and collection can be separated by
        other work in between.
        """
        try:
            try:
                result = self._result_queue.get(timeout=timeout_s)
            except queue_module.Empty:
                self._respawn_after_timeout(work_dir)
                return name, {"status": "TIMEOUT", "notes": f"exceeded {timeout_s}s - candidate terminated"}
            return name, result
        finally:
            token = getattr(self, "_pending_token", None)
            if token is not None:
                token.release()
                self._pending_token = None
            if PROFILE_ENABLED:
                wall_s = time.perf_counter() - getattr(self, "_pending_dispatch_t0", time.perf_counter())
                _profile_record(
                    getattr(self, "_pending_strategy_name", name),
                    getattr(self, "_pending_file_size", None),
                    getattr(self, "_pending_admission_wait", 0.0),
                    wall_s,
                )

    def _respawn_after_timeout(self, work_dir):
        """Kills the timed-out worker and replaces BOTH it and its queues
        with fresh ones, not just the process. `process.terminate()`/
        `kill()` is an abrupt kill - if it lands while the worker is mid-
        write to `self._result_queue` (pickling/flushing its result to the
        pipe, which a background feeder thread can still be doing even
        after the compress/restore work itself finished), reusing that same
        Queue object for the NEXT candidate risked a late, straggling
        result from the just-killed worker being delivered to
        collect_one()'s NEXT call instead of Empty/timeout - silently
        mislabeling one candidate's real result as a completely different
        candidate's (e.g. a real generic_zstd result attributed to
        generic_brotli), since nothing in the result payload itself records
        which candidate produced it. A fresh Queue pair guarantees the next
        candidate's collect_one() can only ever see that candidate's own
        result, never a leftover from the one just killed - session-wide
        candidate/strategy selection is completely unaffected either way,
        this only hardens correctness of very rare timeout/respawn edges.
        """
        _terminate_worker(self._process)
        shutil.rmtree(work_dir, ignore_errors=True)
        try:
            self._task_queue.close()
            self._result_queue.close()
        except Exception:  # noqa: BLE001 - best-effort, a fresh pair is created regardless
            pass
        self._task_queue = _MP_CONTEXT.Queue()
        self._result_queue = _MP_CONTEXT.Queue()
        self._process = self._spawn()  # fresh worker - session stays usable for the rest of the folder

    def run_restore(self, strategy_name, stored_bytes, original_filename, expected_original_sha256, timeout_s):
        """Restore counterpart to run_candidates - decodes one smart-stored
        member back to its original bytes using THIS session's already-
        running worker process instead of spawning a new one (see
        restore_original_bytes' `session` parameter and checkout_folder_
        smart, which dispatches every folder member's restore through a
        SmartWorkerPool exactly like add_folder_to_archive_smart already
        does for compression). Returns the restored bytes on success;
        raises SmartRestoreError on any failure, same contract as the
        session-less restore_original_bytes path.
        """
        if strategy_name not in _STRATEGY_INSTANCES:
            raise SmartRestoreError(f"unknown strategy '{strategy_name}' - cannot restore")
        strategy = _STRATEGY_INSTANCES[strategy_name]
        work_dir = tempfile.mkdtemp(prefix="smartrestore_")
        self._task_queue.put(("restore", strategy, stored_bytes, work_dir, original_filename, expected_original_sha256))
        try:
            result = self._result_queue.get(timeout=timeout_s)
        except queue_module.Empty:
            self._respawn_after_timeout(work_dir)
            raise SmartRestoreError(f"restore of '{original_filename}' via {strategy_name} timed out after {timeout_s}s")

        if result["status"] != "PASS":
            raise SmartRestoreError(f"restore of '{original_filename}' via {strategy_name} failed: {result.get('notes')}")
        return result["data"]

    def close(self):
        try:
            self._task_queue.put(None)  # graceful stop, mirrors _run_candidates' own shutdown
        except Exception:  # noqa: BLE001 - close() must never raise, callers rely on it in `finally`
            pass
        if self._process is not None:
            self._process.join(2)
            _terminate_worker(self._process)
        try:
            self._task_queue.close()
            self._result_queue.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class SmartWorkerPool:
    """A small pool of N persistent SmartWorkerSession workers, so MANY
    files' decide_and_prepare() calls can run CONCURRENTLY (one per
    session) instead of serially through a single shared session.

    A single SmartWorkerSession (folder-wide, see its own docstring) fixed
    the per-file process-spawn cost, but still processes files one at a
    time: baseline compression runs synchronously in the calling thread,
    then that same thread blocks waiting on the ONE worker process for
    every candidate. A real Beta test on a ~10.36GB/~2,900-file folder
    measured ~5.7-6.2% CPU utilization throughout, on a 16-logical-core
    machine - almost exactly 1/16 (6.25%), confirming all that work was
    serialized onto a single core the whole time, not actually
    parallelized, while 20+ minutes passed for 2,900 independent files.

    Files are fully independent units of work - dispatching them across a
    pool of workers via a thread pool (see add_folder_to_archive_smart)
    lets that independence translate into real multi-core throughput:
    each thread's blocking IPC wait releases the GIL, baseline
    compression's zlib C call releases the GIL, and each candidate's real
    compression work already happens in its own separate OS process
    either way - none of that requires true multi-threading in the
    calling process, only enough concurrent dispatch to keep multiple
    worker processes busy at once instead of one at a time.
    """

    def __init__(self, size=None):
        cpu_count = os.cpu_count() or 4
        self.size = max(1, min(size or cpu_count, 8))
        self._sessions = [SmartWorkerSession() for _ in range(self.size)]
        self._available = queue_module.Queue()
        for session in self._sessions:
            self._available.put(session)

    def acquire(self):
        return self._available.get()

    def release(self, session):
        self._available.put(session)

    def close(self):
        for session in self._sessions:
            session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _run_candidates_pool_aware(candidate_specs, file_path, config, original_hash, pool):
    """Pool-slot-starvation fix: identical outcome/safety to session.
    run_candidates, but a candidate that must wait for RAM admission does
    NOT hold one of the pool's small, fixed number of worker-process slots
    while it waits. Root cause this fixes (proven via a real 354-file/
    6.3GB idle-machine reproduction): the old order acquired a pool
    session FIRST, then waited for admission (up to _ADMISSION_MAX_WAIT_S)
    WHILE HOLDING that session - on a real 16GB-RAM machine, HEAVY
    candidates routinely waited up to the full ceiling, each one tying up
    one of only 8 shared session slots the whole time even though the
    worker process behind it was doing nothing but waiting - starving
    every OTHER file's ready (often fast, LIGHT-only) work of a slot to
    run on. Here, admission is acquired BEFORE the session, so waiting
    candidates block only their own calling thread, never a session other
    files need. Nothing about WHETHER a candidate waits, HOW LONG it may
    wait, or what real-RAM check gates it changes at all - only that the
    wait no longer holds a session hostage."""
    results = []
    for name, strategy in candidate_specs:
        token, admission_wait, file_size = _acquire_admission_standalone(name, file_path)
        session = pool.acquire()
        try:
            work_dir = session.dispatch_one_with_token(strategy, file_path, original_hash, token, admission_wait, file_size)
            timeout_s = policy.timeout_for_strategy(name, config)
            results.append(session.collect_one(name, work_dir, timeout_s))
        finally:
            pool.release(session)
    return results


def _run_candidates(candidate_specs, file_path, config, original_hash, session=None, pool=None):
    """Runs every `(name, strategy)` candidate for one file, reusing a
    SINGLE spawned worker process across all of them - unlike calling
    _run_candidate once per candidate (the original approach), which spawns
    a fresh process every time.

    `original_hash`: the target file's SHA-256, computed ONCE by the
    caller (decide_and_prepare) - passed through to every candidate
    instead of each one recomputing it via its own full read of the file
    (see _compress_restore_verify's docstring for why that redundant
    per-candidate re-hash was a real, measured bottleneck).

    `session` (optional): an already-running SmartWorkerSession to reuse
    instead of spawning+tearing-down a worker just for this one call - see
    SmartWorkerSession's docstring. When omitted (the default, unchanged
    behavior for every existing single-file caller), this function spawns
    its own short-lived session exactly as before.

    `pool` (optional): a SmartWorkerPool to draw sessions from PER
    CANDIDATE rather than holding one session for the whole file - see
    _run_candidates_pool_aware. Takes priority over `session` if both are
    given (callers should only ever pass one). Use this instead of
    `session` when multiple files' candidates may be dispatched
    concurrently across a shared pool (see add_folder_to_archive_smart),
    so a candidate waiting on RAM admission never blocks a session another
    file's ready work needs.

    Why this matters, measured directly (not guessed): on a frozen
    PyInstaller --onefile build, EVERY spawned child process re-extracts
    the entire onefile archive natively (the compiled bootloader's own
    self-extraction step, which runs before any Python code - including
    multiprocessing.freeze_support()'s own child-dispatch logic - ever gets
    control). For the real ~264-280MB Beta EXE (built with `--collect-all
    PySide6`), that repeated extraction cost ~3+ seconds per spawn in this
    session's own measurements alone; a real end-user machine with a slower
    disk or active antivirus scanning of each newly-extracted copy can take
    far longer. A JPEG's 5 non-baseline candidates (strategy_registry's
    _JPEG_FULL) previously meant 5 full onefile re-extractions in sequence
    for ONE Archive click - confirmed directly to be the real bottleneck
    behind the multi-minute real-GUI Archive delay, not the ~0.75s an
    earlier single-candidate console-only probe (no PySide6 bundled, so a
    much smaller archive to re-extract) misleadingly suggested. Spawning
    one worker and feeding it every candidate in turn pays that extraction
    cost once per Archive operation instead of once per candidate.

    Preserves _run_candidate's own safety properties: a per-candidate
    timeout still applies (result_queue.get(timeout=timeout_s)), and a
    hung/dead worker is terminated and replaced with a fresh one before the
    next candidate - one bad candidate still can't take down the ones after
    it, it just costs an extra spawn only in that (uncommon) case rather
    than on every candidate. Same drain-before-join queue ordering as
    _run_candidate, for the same pipe-buffer-deadlock reason documented
    there.

    Returns a list of `(name, result)` pairs, same result shape
    _run_candidate returns, in candidate_specs order.
    """
    if not candidate_specs:
        return []

    if pool is not None:
        return _run_candidates_pool_aware(candidate_specs, file_path, config, original_hash, pool)

    if session is not None:
        return session.run_candidates(candidate_specs, file_path, config, original_hash)

    with SmartWorkerSession() as owned_session:
        return owned_session.run_candidates(candidate_specs, file_path, config, original_hash)


def decide_and_prepare(file_path, config=None, disabled_strategies=None, session=None, pool=None):
    """Runs the full Smart Selector pipeline for one file about to be
    archived. Returns a SmartDecision if a candidate was accepted (already
    exact-restore-verified), or None if the caller should use the existing
    legacy StorageArch path. NEVER raises for ordinary "not worth it" /
    "tool missing" / "timeout" outcomes - those all just mean None.

    `disabled_strategies` (optional, default none disabled): an iterable of
    strategy names to exclude from consideration this call - the only hook
    Cloud Remote Config's `disabled_smart_strategies` (plan §28) has into
    this module, and deliberately just that: a plain, local, ADDITIVE
    filter on which already-installed strategies get tried. It can only
    ever narrow the candidate list, never widen it (a name Remote Config
    sends that isn't in strategy_registry simply matches nothing), and
    never touches exact-restore verification, SHA-256 checks, or fallback
    behavior below - every accepted candidate still goes through the exact
    same compress/restore/verify cycle either way. No networking, no Cloud
    import, anywhere in this module - the caller (main.py) resolves the
    disabled set before calling in.

    `session` (optional): a SmartWorkerSession to reuse across many calls
    to this function - see SmartWorkerSession's docstring. Omit for a
    single-file operation (unchanged default behavior - _run_candidates
    spawns and tears down its own short-lived worker for just that one
    file).

    `pool` (optional): a SmartWorkerPool to draw a session from PER
    CANDIDATE instead of holding one session for the file's whole
    candidate sequence - see _run_candidates_pool_aware's docstring for
    the real starvation bug this fixes. archive_manager_smart.
    add_folder_to_archive_smart passes this (not `session`) for folder
    Archive operations, where many files' candidates are dispatched
    concurrently across a shared pool.
    """
    config = config or policy.DEFAULT_POLICY
    disabled_strategies = frozenset(disabled_strategies or ())

    if not os.path.isfile(file_path):
        return None

    original_size = os.path.getsize(file_path)
    extension = os.path.splitext(file_path)[1].lstrip(".").lower()

    try:
        detected_type, _is_binary, _mismatch = file_analysis.detect_format(file_path, extension)
    except Exception:  # noqa: BLE001 - detection failure must never break archiving
        return None

    baseline_work = tempfile.mkdtemp(prefix="smartcompress_baseline_")
    try:
        with _CPU_BOUND_PREP_GATE:
            baseline_result = _BASELINE.compress(file_path, baseline_work)
        baseline_size = baseline_result.stored_size
    except Exception:  # noqa: BLE001
        return None
    finally:
        shutil.rmtree(baseline_work, ignore_errors=True)

    if not policy.should_attempt_candidates(original_size, config):
        return None

    candidate_names = strategy_registry.get_candidate_strategies(detected_type, extension, original_size)
    if disabled_strategies:
        candidate_names = [name for name in candidate_names if name not in disabled_strategies]

    candidate_specs = []
    for name in candidate_names:
        strategy = _STRATEGY_INSTANCES[name]
        available, _reason = strategy.is_available()
        if available:
            candidate_specs.append((name, strategy))

    if not candidate_specs:
        return None

    # Computed once here instead of once per candidate inside
    # _compress_restore_verify - see that function's docstring. Only done
    # once we know there's at least one real candidate to run, so a file
    # with zero available candidates never pays for this read at all.
    with _CPU_BOUND_PREP_GATE:
        original_hash = _sha256_file(file_path)

    accepted = []
    for name, result in _run_candidates(candidate_specs, file_path, config, original_hash, session=session, pool=pool):
        if result["status"] != "PASS":
            continue  # timeout / exception / hash mismatch -> just skip this candidate, never raise

        decision_check = policy.evaluate(
            original_size=original_size,
            baseline_size=baseline_size,
            candidate_size=result["stored_size"],
            compression_time_s=result["compress_time_s"],
            restore_time_s=result["restore_time_s"],
            sha256_match=True,
            config=config,
        )
        if decision_check.accepted:
            accepted.append((name, result))

    if not accepted:
        return None

    best_name, best_result = min(accepted, key=lambda t: t[1]["stored_size"])
    return SmartDecision(
        strategy_name=best_name,
        stored_bytes=best_result["stored_bytes"],
        stored_size=best_result["stored_size"],
        original_size=original_size,
        original_sha256=best_result["original_sha256"],
        stored_sha256=best_result["stored_sha256"],
        compress_time_s=best_result["compress_time_s"],
        restore_time_s=best_result["restore_time_s"],
    )


def _restore_worker(strategy, stored_path, work_dir, original_filename, result_queue):
    """Module-level (not a closure) so multiprocessing spawn can pickle it."""
    try:
        restore_result = strategy.restore(stored_path, work_dir, original_filename)
        with open(restore_result.output_path, "rb") as rf:
            result_queue.put({"status": "PASS", "data": rf.read()})
    except Exception as exc:  # noqa: BLE001
        result_queue.put({"status": "ERROR", "notes": f"{type(exc).__name__}: {exc}"})


def restore_original_bytes(strategy_name, stored_bytes, original_filename, expected_original_sha256, timeout_s=30.0, session=None):
    """Reconstructs original bytes from a smart-stored representation.
    Raises SmartRestoreError on any failure or hash mismatch - callers
    must never fall back to returning stored_bytes as-is on error.

    `session` (optional): a SmartWorkerSession to reuse instead of
    spawning+tearing-down a worker process just for this one restore - see
    SmartWorkerSession.run_restore and checkout_folder_smart, which passes
    a pooled session in for every member of a folder checkout instead of
    each one paying its own process-spawn (and, on a frozen PyInstaller
    onefile build, onefile self-extraction) cost. Omit for a single-item
    restore (unchanged default behavior).
    """
    if session is not None:
        return session.run_restore(strategy_name, stored_bytes, original_filename, expected_original_sha256, timeout_s)

    if strategy_name not in _STRATEGY_INSTANCES:
        raise SmartRestoreError(f"unknown strategy '{strategy_name}' - cannot restore")

    strategy = _STRATEGY_INSTANCES[strategy_name]
    work_dir = tempfile.mkdtemp(prefix="smartrestore_")
    try:
        stored_path = os.path.join(work_dir, "stored" + getattr(strategy, "extension", ".bin"))
        with open(stored_path, "wb") as f:
            f.write(stored_bytes)

        result_queue = _MP_CONTEXT.Queue()
        process = _MP_CONTEXT.Process(
            target=_restore_worker, args=(strategy, stored_path, work_dir, original_filename, result_queue)
        )
        process.start()

        # Read before join - see the matching note on _run_candidate above.
        # _restore_worker's result carries the full restored file bytes,
        # which for any non-trivial file exceeds a pipe buffer; joining
        # first would deadlock exactly like the archive-side path did.
        try:
            result = result_queue.get(timeout=timeout_s)
        except queue_module.Empty:
            if process.is_alive():
                process.terminate()
                process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)
                raise SmartRestoreError(f"restore of '{original_filename}' via {strategy_name} timed out after {timeout_s}s")
            raise SmartRestoreError(f"restore of '{original_filename}' via {strategy_name} produced no result")

        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join(2)

        if result["status"] != "PASS":
            raise SmartRestoreError(f"restore of '{original_filename}' via {strategy_name} failed: {result.get('notes')}")

        restored_bytes = result["data"]
        restored_hash = _sha256_bytes(restored_bytes)
        if restored_hash != expected_original_sha256:
            raise SmartRestoreError(
                f"SHA256 mismatch restoring '{original_filename}' via {strategy_name}: "
                f"expected {expected_original_sha256}, got {restored_hash}"
            )
        return restored_bytes
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
