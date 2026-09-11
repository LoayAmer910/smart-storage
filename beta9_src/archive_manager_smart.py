"""Phase 3: Smart-aware archive/checkin/checkout/manifest functions.

Deliberately additive - archive_manager.py is never modified, so the
existing legacy path is byte-for-byte the same code it always was. These
functions call into archive_manager's proven atomic temp-zip/commit
primitives (_build_temp_zip, _commit_temp_zip, _discard_temp_zip,
build_archive_member_name, calculate_zip_file_hash) rather than
reimplementing that safety mechanism.

Storage representation: a smart-stored member's bytes ARE the already-
compressed representation (e.g. .jxl bytes, zstd-compressed bytes), written
with ZIP_STORED (no redundant re-compression). Its metadata travels with
it atomically via the ZipInfo.comment field - "smart:<strategy>:<original_
sha256>:<original_size>" - so it can never desync from the bytes it
describes (no separate side-file to go stale). create_archive_manifest_
smart reads that comment when rebuilding the manifest, same as the legacy
function reads zip contents directly.

Manifest field semantics are kept IDENTICAL to the legacy manifest for
both archive types: files[].sha256 always means "hash of the raw stored
zip member bytes" (== original file hash for legacy, == stored-
representation hash for smart). This is why archive_manager.verify_archive
needed zero changes to correctly validate both kinds of archives - it was
already only ever checking "does the zip still match what the manifest
recorded", which is format-agnostic by construction.
"""

import concurrent.futures
import hashlib
import json
import os
import queue
import shutil
import threading
import time
import zipfile
from datetime import datetime

import archive_manager
import smart_compression
import storage_config

_SMART_COMMENT_PREFIX = b"smart:"


def _smart_comment(strategy_name, original_sha256, original_size):
    return f"smart:{strategy_name}:{original_sha256}:{original_size}".encode("utf-8")


def _parse_smart_comment(comment):
    """Returns (strategy_name, original_sha256, original_size) or None."""
    if not comment or not comment.startswith(_SMART_COMMENT_PREFIX):
        return None
    try:
        _, strategy_name, original_sha256, original_size_str = comment.decode("utf-8").split(":", 3)
        return strategy_name, original_sha256, int(original_size_str)
    except (ValueError, UnicodeDecodeError):
        return None  # malformed - treat as legacy rather than fail


def add_file_to_archive_smart(file_path, category_name, zip_path=None, disabled_strategies=None):
    """Same contract as archive_manager.add_file_to_archive, plus a 4th
    return value: the strategy name used ("baseline_storagearch_zip" or
    None both mean legacy storage; any other string means smart storage).
    On ANY failure to prepare a smart candidate, falls through to the
    unmodified legacy function - Smart Compression can never be the
    reason an archive operation fails.

    `disabled_strategies` (optional): passed straight through to
    smart_compression.decide_and_prepare - see that function's own doc
    comment. No networking here either; the caller (main.py) resolves this
    set before calling in.
    """
    if not os.path.isfile(file_path):
        return None, False, None, None

    try:
        decision = smart_compression.decide_and_prepare(file_path, disabled_strategies=disabled_strategies)
    except Exception:  # noqa: BLE001 - Smart Compression must never break archiving
        decision = None

    if decision is None:
        zip_path, file_added, member_name = archive_manager.add_file_to_archive(file_path, category_name, zip_path)
        return zip_path, file_added, member_name, None

    if zip_path is None:
        category_path = archive_manager.create_category(category_name)
        zip_path = archive_manager.get_next_archive_path(category_name)
        os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    else:
        category_dir = os.path.dirname(zip_path)
        if category_dir:
            os.makedirs(category_dir, exist_ok=True)

    file_name = os.path.basename(file_path)
    member_name = archive_manager.build_archive_member_name(file_name, decision.original_sha256)

    archive_exists = os.path.isfile(zip_path)
    if archive_exists:
        with zipfile.ZipFile(zip_path, "r") as existing_zip:
            if member_name in existing_zip.namelist():
                return zip_path, False, member_name, None

    def write_new_member(new_zip):
        info = zipfile.ZipInfo(member_name)
        info.comment = _smart_comment(decision.strategy_name, decision.original_sha256, decision.original_size)
        new_zip.writestr(info, decision.stored_bytes, compress_type=zipfile.ZIP_STORED)

    if archive_exists:
        # נתיב מהיר: append בלבד - ראו archive_manager._append_new_members.
        # זה מה שהופך הוספת פריט קטן לקטגוריה גדולה-קיימת למיידית, במקום
        # לדחוס-מחדש את כל הארכיון (כולל כל שאר הפריטים ה"חכמים") בכל הוספה.
        guard = archive_manager._append_new_members(zip_path, write_new_member)
        if guard is None:
            return None, False, None, None
        guard_path, original_size = guard

        if archive_manager.calculate_zip_file_hash(zip_path, member_name) != decision.stored_sha256:
            archive_manager._rollback_append(zip_path, guard_path, original_size)
            return None, False, None, None

        archive_manager._confirm_append(guard_path)
        return zip_path, True, member_name, decision.strategy_name

    temp_path = archive_manager._build_temp_zip(zip_path, lambda old_zip, new_zip: write_new_member(new_zip))
    if temp_path is None:
        return None, False, None, None

    if archive_manager.calculate_zip_file_hash(temp_path, member_name) != decision.stored_sha256:
        archive_manager._discard_temp_zip(temp_path)
        return None, False, None, None

    archive_manager._commit_temp_zip(temp_path, zip_path)
    return zip_path, True, member_name, decision.strategy_name


def add_folder_to_archive_smart(folder_path, category_name, zip_path=None, disabled_strategies=None, skipped_cloud_files=None):
    """Same contract as archive_manager.add_folder_to_archive, plus a 3rd
    return value: a list of per-file result dicts for Smart Metrics
    aggregation - one per newly-written file, each
    {"path_in_archive", "file_type", "strategy", "original_size",
    "stored_size", "processing_ms"} ("strategy" is None for a file that
    fell back to baseline storage).

    Every file inside the folder is independently run through Smart
    Compression's decide_and_prepare - the same per-file evaluation
    add_file_to_archive_smart already does for a standalone file, just
    applied to every member of a folder instead of assuming folders are
    out of scope for it. A single file's failure (decide_and_prepare
    returning None, e.g. an unsupported type or a strategy timeout) only
    falls that ONE file back to plain baseline storage - it never falls
    the whole folder back together, matching decide_and_prepare's own
    per-candidate safety contract.

    Uses the same append-mode fast path as the legacy folder-add function
    (archive_manager._append_new_members) when the target archive already
    exists, so this stays just as fast for "add a small new folder into an
    existing multi-GB category" as the non-smart path - Smart Compression
    only changes WHAT gets written per file, never re-introduces a full-
    archive rewrite.

    Every file's decide_and_prepare() call below is dispatched against a
    shared smart_compression.SmartWorkerPool (a handful of persistent
    worker processes, not one spawned per file - see SmartWorkerSession's
    docstring for why a single spawn-per-file was already fixed once), via
    a thread pool so independent files' work actually runs concurrently
    across CPU cores instead of one file at a time - see SmartWorkerPool's
    own docstring: a real Beta test on a ~10.36GB/~2,900-file folder still
    took 20+ minutes at ~6% CPU (≈1/16 cores) even after the single-
    session fix, because every file was still processed strictly in turn.

    Producer/consumer pipeline (not a batch-then-write design): a real
    ~10.36GB/~2,900-file Beta test STILL took 20-25+ minutes even with the
    worker pool and the single-open verification fix, with CPU/disk
    dropping to near-zero well AFTER the visibly multi-core Smart decision
    phase ended. Root cause: this function used to collect EVERY file's
    decision first (blocking on ALL ~2,900 decide_and_prepare() calls),
    and only THEN open the zip and write every member in one fully
    sequential pass - so the (I/O-bound, single-threaded) write phase for
    thousands of files never overlapped with the (CPU/IPC-bound, 8-way
    parallel) decision phase at all; the pool sat idle the whole time the
    single writer worked through the backlog. Now: decision work for every
    file is submitted to the pool up front, but the SAME thread that will
    write to the zip pulls each result off a small BOUNDED queue as soon
    as it's ready (`concurrent.futures` dispatches decisions in parallel;
    this thread's queue.get() blocks only until the NEXT one finishes) and
    writes it in immediately - so files already decided get written to
    disk while the pool keeps deciding the rest, instead of the two phases
    running one after the other. The queue's bound (a small multiple of
    the pool size) caps how many completed-but-not-yet-written decisions'
    compressed bytes can pile up in memory if writing ever lags behind
    deciding - if the queue fills, the pool naturally pauses handing out
    new results until the writer catches up, rather than buffering an
    unbounded amount of one folder's data at once.
    """
    if not os.path.isdir(folder_path):
        return None, None, None

    # Lightweight phase timing, printed at the end - cheap (a handful of
    # time.perf_counter() calls and one print), left in permanently rather
    # than removed after debugging, so the NEXT real large-folder Archive
    # (e.g. the real ~10.36GB/~2,900-file/~655-folder Beta QA case, not
    # reproducible in full locally) reports exactly where its wall-clock
    # time went instead of requiring another guess-and-instrument round.
    _phase_t0 = time.perf_counter()
    _phase_times = {}

    def _mark(phase_name):
        nonlocal _phase_t0
        now = time.perf_counter()
        _phase_times[phase_name] = now - _phase_t0
        _phase_t0 = now

    if zip_path is None:
        estimated_size = archive_manager.get_folder_total_size(folder_path) or 0
        zip_path = archive_manager.get_selected_archive(category_name, None, estimated_size)
    else:
        category_dir = os.path.dirname(zip_path)
        if category_dir:
            os.makedirs(category_dir, exist_ok=True)

    folder_name = os.path.basename(os.path.normpath(folder_path))
    folder_hash = archive_manager.calculate_folder_hash(folder_path)
    _mark("folder_hash")
    if folder_hash is None:
        return None, None, None
    member_root = archive_manager.build_archive_member_name(folder_name, folder_hash)

    archive_exists = os.path.isfile(zip_path)
    existing_paths = set()
    if archive_exists:
        with zipfile.ZipFile(zip_path, "r") as existing_zip:
            existing_paths = set(existing_zip.namelist())

    # Decide every new file's storage representation BEFORE opening the zip
    # for writing - decide_and_prepare can be slow (it runs real candidate
    # strategies), and keeping that work outside the write transaction
    # avoids holding the append guard/backup (see archive_manager.
    # _append_new_members) open any longer than the actual write needs.
    candidates = []  # (full_file_path, path_in_archive, file_type)
    folder_count = 0
    total_scanned_bytes = 0
    for current_path, _dir_names, file_names in os.walk(folder_path):
        folder_count += 1
        for file_name in file_names:
            full_file_path = os.path.join(current_path, file_name)
            if archive_manager._is_cloud_placeholder(full_file_path):
                # OneDrive/cloud "Files On-Demand" placeholder not actually
                # downloaded to this device - reading it would raise a bare
                # OSError [Errno 22] deep in the decide/write pipeline below
                # (see archive_manager.CloudPlaceholderError). Skipping it
                # here matches calculate_folder_hash's own skip above (the
                # folder_hash already excludes it), so the archived content
                # is exactly what was hashed - the rest of the folder still
                # archives normally, and the caller is told what was left out.
                if skipped_cloud_files is not None:
                    skipped_cloud_files.append(full_file_path)
                continue
            path_in_folder = os.path.relpath(full_file_path, folder_path)
            path_in_archive = f"{member_root}/{path_in_folder}".replace(os.sep, "/")
            try:
                total_scanned_bytes += os.path.getsize(full_file_path)
            except OSError:
                pass
            if path_in_archive in existing_paths:
                continue
            file_type = os.path.splitext(file_name)[1].lstrip(".").lower() or "unknown"
            candidates.append((full_file_path, path_in_archive, file_type))
    _mark("scan")

    results_queue = queue.Queue(maxsize=max(8, min(32, len(candidates) or 1)))

    def _decide_and_enqueue(full_file_path, path_in_archive, file_type, pool):
        # Passes `pool=` (not a pre-acquired `session=`) so a candidate
        # that must wait for RAM admission does not hold one of the
        # pool's small, fixed number of worker-process slots hostage for
        # the whole wait - see smart_compression._run_candidates_pool_
        # aware's docstring for the real starvation this fixes (proven on
        # a real 354-file/6.3GB corpus: HEAVY candidates routinely waited
        # up to the full 60s admission ceiling while holding a session,
        # starving every other file's ready - often fast, LIGHT-only -
        # work of a slot to run on).
        try:
            decision = smart_compression.decide_and_prepare(
                full_file_path, disabled_strategies=disabled_strategies, pool=pool
            )
        except Exception:  # noqa: BLE001 - Smart Compression must never break archiving
            decision = None
        # Always enqueue, even on an unexpected error above - the writer
        # below counts items received against len(candidates) and would
        # otherwise block forever waiting for a result that never arrives.
        results_queue.put((full_file_path, path_in_archive, decision, file_type))

    plan = []  # filled in by write_members as results stream in, in COMPLETION order (not scan order)

    def write_members(new_zip):
        for _ in range(len(candidates)):
            full_file_path, path_in_archive, decision, file_type = results_queue.get()
            plan.append((full_file_path, path_in_archive, decision, file_type))
            if decision is not None:
                info = zipfile.ZipInfo(path_in_archive)
                info.comment = _smart_comment(decision.strategy_name, decision.original_sha256, decision.original_size)
                new_zip.writestr(info, decision.stored_bytes, compress_type=zipfile.ZIP_STORED)
            else:
                new_zip.write(full_file_path, path_in_archive)

    # The pool/executor stay open for the ENTIRE write - write_members (run
    # synchronously inside _append_new_members/_build_temp_zip below) drains
    # results_queue as decisions complete, so deciding file N+1..N+8 and
    # writing file N happen concurrently instead of two back-to-back phases.
    guard = None
    temp_path = None
    write_error = None
    with smart_compression.SmartWorkerPool() as pool:
        # More decide-threads than real worker-process sessions (pool.size)
        # - the second half of the pool-slot-starvation fix (see
        # smart_compression._run_candidates_pool_aware's docstring for the
        # first half). Decoupling admission-wait from the SESSION alone
        # measured NO improvement, because this executor previously had
        # exactly pool.size threads too - a thread blocked in the up-to-
        # _ADMISSION_MAX_WAIT_S wait loop still occupied one of only 8
        # THREADS regardless of whether it also held a session, so nothing
        # was actually freed up for other files. Waiting for RAM headroom
        # only needs a thread, not a worker process/session - only the
        # actual dispatch+collect afterward needs one of pool.size real
        # sessions (still enforced by pool.acquire()/release() inside
        # _run_candidates_pool_aware, completely unchanged). More threads
        # here just means more files can be concurrently EITHER waiting on
        # admission OR ready to grab a session the moment one frees up,
        # instead of a handful of stuck HEAVY waits blocking everyone.
        decide_thread_count = min(64, max(pool.size * 4, 16))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=decide_thread_count)
        try:
            for full_file_path, path_in_archive, file_type in candidates:
                executor.submit(_decide_and_enqueue, full_file_path, path_in_archive, file_type, pool)

            try:
                if archive_exists:
                    guard = archive_manager._append_new_members(zip_path, write_members)
                else:
                    temp_path = archive_manager._build_temp_zip(zip_path, lambda old_zip, new_zip: write_members(new_zip))
            except Exception as exc:  # noqa: BLE001 - still need to drain/shutdown below before propagating
                write_error = exc
        finally:
            # write_members may have stopped early (an internal exception, a
            # failed guard/temp-zip build) without draining every result -
            # keep pulling (and discarding) whatever's left so no producer
            # thread stays blocked forever on a full bounded queue, which
            # would otherwise hang executor.shutdown(wait=True) below. A
            # short per-item timeout bounds the wait if a worker itself is
            # stuck/dead and will never produce its result.
            received = len(plan)
            while received < len(candidates):
                try:
                    results_queue.get(timeout=5)
                    received += 1
                except queue.Empty:
                    break
            executor.shutdown(wait=True)
    _mark("smart_decide_and_zip_write")  # pipelined - covers decision dispatch, IPC waits, and ZIP writing together

    if write_error is not None or (archive_exists and guard is None) or (not archive_exists and temp_path is None):
        _record_folder_archive_phase_timings(
            _phase_times, len(candidates), folder_count, total_scanned_bytes, folder_path, outcome="write_failed"
        )
        return None, None, None

    if archive_exists:
        guard_path, start_dir = guard
        check_zip_path = zip_path
    else:
        check_zip_path = temp_path

    # Single open handle for the WHOLE verification pass, instead of calling
    # archive_manager.calculate_zip_file_hash() per file - that helper reopens
    # the entire zip (re-locates the End-Of-Central-Directory and re-parses
    # the full central directory) on every call. For a large, already-grown
    # category archive, that made verification cost O(new file count) FULL
    # zip reopens - confirmed directly to dominate wall-clock time on a real
    # ~3.9GB archive (per-file reopen: ~36ms/file; single-open: ~1.6ms/file,
    # ~22x), and it exactly matches the "near-zero CPU, near-zero disk"
    # symptom a real ~10GB/~2,900-file folder Archive showed for extended
    # periods (each reopen is a small, low-CPU disk seek+read, not real
    # compute - and each one on a real machine is also a fresh target for
    # antivirus on-access scanning of the same large file, over and over).
    #
    # This whole verify+commit stretch used to have NO exception handling at
    # all - a real ~10.36GB/~2,900-file Beta QA run hit exactly that gap:
    # the operation failed with main.py's generic "A filesystem error
    # occurred while archiving" AND smart_folder_performance.log received no
    # new entry whatsoever, because whatever OSError fired here (reopening a
    # multi-GB just-written zip while it's still a fresh target for AV
    # on-access scanning is a real, previously-confirmed source of exactly
    # this kind of transient I/O failure - see the reopen-cost comment
    # above) propagated straight out of this function, past every existing
    # _record_folder_archive_phase_timings() call site, and was only ever
    # caught far upstream by main.py's blanket `except OSError` - by which
    # point there was no way left to tell WHERE in the operation it failed.
    # The two try/except blocks below close that gap without changing what
    # correctness guarantee each phase already provides:
    #   - a failure while re-reading/hashing (nothing durable confirmed yet)
    #     is treated exactly like a hash mismatch already was: roll back.
    #   - a failure in the commit call itself is handled per append-vs-
    #     temp-zip semantics (see the two branches below) - rolling back
    #     unconditionally here would be WRONG for the append case, since by
    #     that point the new members are already durably written and
    #     independently hash-verified; _confirm_append only deletes the
    #     now-unneeded guard/backup sidecar files, so a failure there must
    #     never undo already-good data.
    verify_ok = True
    file_results = []

    # Beta 9 perf fix: read-back verification of every newly-written member
    # used to run strictly sequentially, one file at a time, in a single
    # thread - the same shape of bottleneck already fixed once for folder-
    # hashing (see calculate_folder_hash's docstring) and for the decide/
    # write pipeline above, but it had crept back in here. On a real large
    # folder Archive this made "verify_seconds" balloon (measured: a real
    # ~11.2GB/4,060-file run went from Beta 8's 20.38s to 88.88s on the
    # exact same dataset) even though total bytes read back barely changed -
    # each member's open()+read()+sha256 is independent I/O-bound work that
    # was leaving every other CPU core and every other in-flight disk/AV
    # read idle while it ran. Fanned out across a small thread pool instead
    # (same max_workers shape as archive_manager.checkout_folder's existing
    # parallel checkout, see below) - each worker thread opens and reuses
    # its OWN zipfile.ZipFile handle (concurrent reads through a SINGLE
    # shared ZipFile/ZipExtFile across threads corrupt each other's file
    # position - see zipfile's own thread-safety caveat - so every thread
    # gets an independent read-only handle onto the same already-fully-
    # written, closed file, which is safe). Every item is still
    # independently opened, fully read, and SHA-256'd exactly as before -
    # nothing is skipped, cached across items, or approximated; this only
    # changes HOW MANY of those independent checks can be in flight at
    # once, never WHETHER each one happens or what it checks.
    verify_thread_local = threading.local()
    verify_handles = []
    verify_handles_lock = threading.Lock()

    def _verify_get_zip_handle():
        zip_handle = getattr(verify_thread_local, "zip_file", None)
        if zip_handle is None:
            # Retried (see archive_manager._retry_transient_os_error):
            # reopening the archive we JUST finished writing, immediately
            # after a sustained high-disk-activity write burst, is exactly
            # the window a transient Windows sharing violation (most
            # commonly antivirus real-time scanning still holding a brief
            # lock on the freshly-written file) is most likely to hit -
            # confirmed as the real cause of a genuine ~10.36GB/~2,900-file
            # Beta QA failure that produced no diagnostic at all before this
            # phase had any exception handling. Only the OPEN itself is
            # retried, not the per-member reads inside - a failure while
            # reading an already-successfully-opened member is left to the
            # existing (KeyError, BadZipFile, OSError) handling below, which
            # correctly treats it as a real verify failure rather than a
            # transient race to paper over.
            zip_handle = archive_manager._retry_transient_os_error(zipfile.ZipFile, check_zip_path, "r")
            verify_thread_local.zip_file = zip_handle
            with verify_handles_lock:
                verify_handles.append(zip_handle)
        return zip_handle

    def _verify_one(item):
        full_file_path, path_in_archive, decision, file_type = item
        expected_hash = decision.stored_sha256 if decision is not None else archive_manager.calculate_file_hash(full_file_path)
        zip_handle = _verify_get_zip_handle()
        try:
            hash_value = hashlib.sha256()
            with zip_handle.open(path_in_archive, "r") as archived_file:
                while True:
                    chunk = archived_file.read(65536)
                    if not chunk:
                        break
                    hash_value.update(chunk)
            actual_hash = hash_value.hexdigest()
        except (KeyError, zipfile.BadZipFile, OSError):
            # Mirrors calculate_zip_file_hash's own graceful None-on-
            # missing/unreadable-member behavior - never let a read
            # failure here escape as an uncaught exception, which would
            # skip the rollback below entirely instead of triggering it.
            actual_hash = None
        strategy_name = decision.strategy_name if decision is not None else None
        original_size = decision.original_size if decision is not None else os.path.getsize(full_file_path)
        if decision is not None:
            stored_size = decision.stored_size
        else:
            # decision is None -> this file went through the legacy
            # baseline path (archive_manager.add_file_to_archive), which
            # DOES apply real zipfile.ZIP_DEFLATED compression - it is NOT
            # stored uncompressed. Reporting stored_size = original_size
            # here (the old behavior) silently claimed 0% saved for every
            # such file regardless of what was actually written, which is
            # what the Smart Metrics dashboard was showing for every
            # baseline-fallback file type (confirmed via
            # SCRATCHPAD_metrics_diagnostic.py: real on-disk compress_size
            # showed ~98% saved on synthetic sub-4KB text files that all
            # reported 0%). The zip member we just finished reading back
            # above already carries its own real compress_size - use that
            # instead of guessing. Falls back to original_size only if the
            # member is somehow missing from the archive (matches the
            # actual_hash=None case above, which already fails this file's
            # verification separately).
            try:
                stored_size = zip_handle.getinfo(path_in_archive).compress_size
            except KeyError:
                stored_size = original_size
        processing_ms = (decision.compress_time_s * 1000) if decision is not None else 0.0
        return actual_hash == expected_hash, {
            "path_in_archive": path_in_archive,
            "file_type": file_type,
            "strategy": strategy_name,
            "original_size": original_size,
            "stored_size": stored_size,
            "processing_ms": processing_ms,
        }

    try:
        verify_worker_count = min(8, os.cpu_count() or 4, len(plan)) if plan else 1
        verify_executor = concurrent.futures.ThreadPoolExecutor(max_workers=verify_worker_count)
        try:
            for item_ok, item_result in verify_executor.map(_verify_one, plan):
                if not item_ok:
                    verify_ok = False
                    break
                file_results.append(item_result)
        finally:
            # Waits for every already-dispatched item to finish (including
            # ones still in flight after an early break above) before any
            # handle is closed - same "every submitted check actually runs
            # to completion" guarantee the old sequential loop gave for
            # free, just made explicit now that work can be in flight
            # concurrently.
            verify_executor.shutdown(wait=True)
            for zip_handle in verify_handles:
                try:
                    zip_handle.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup only, never masks the real verify outcome above
                    pass
    except Exception as exc:  # noqa: BLE001 - opening/reading the just-written zip for verification failed outright
        _mark("verify")
        if archive_exists:
            archive_manager._rollback_append(zip_path, guard_path, start_dir)
        else:
            archive_manager._discard_temp_zip(temp_path)
        _record_folder_archive_phase_timings(
            _phase_times, len(candidates), folder_count, total_scanned_bytes, folder_path,
            outcome=f"verify_error_rolled_back:{type(exc).__name__}",
        )
        return None, None, None

    _mark("verify")

    if not verify_ok:
        if archive_exists:
            archive_manager._rollback_append(zip_path, guard_path, start_dir)
        else:
            archive_manager._discard_temp_zip(temp_path)
        _record_folder_archive_phase_timings(
            _phase_times, len(candidates), folder_count, total_scanned_bytes, folder_path,
            outcome="verify_failed_rolled_back",
        )
        return None, None, None

    if archive_exists:
        try:
            archive_manager._confirm_append(guard_path)
        except Exception as exc:  # noqa: BLE001 - the append itself is already durable and verified above;
            # only guard/backup sidecar cleanup failed - never roll back good data because of that.
            _mark("commit")
            _record_folder_archive_phase_timings(
                _phase_times, len(candidates), folder_count, total_scanned_bytes, folder_path, file_results,
                outcome=f"committed_guard_cleanup_error:{type(exc).__name__}",
            )
            return zip_path, f"{member_root}/", file_results
    else:
        try:
            archive_manager._commit_temp_zip(temp_path, zip_path)
        except Exception as exc:  # noqa: BLE001 - the rename itself didn't happen; zip_path is untouched, so this
            # is a real failure, not a data-loss risk - discard the (already fully verified) temp and report it.
            _mark("commit")
            archive_manager._discard_temp_zip(temp_path)
            _record_folder_archive_phase_timings(
                _phase_times, len(candidates), folder_count, total_scanned_bytes, folder_path,
                outcome=f"commit_error:{type(exc).__name__}",
            )
            return None, None, None
    _mark("commit")

    _record_folder_archive_phase_timings(
        _phase_times, len(candidates), folder_count, total_scanned_bytes, folder_path, file_results
    )

    return zip_path, f"{member_root}/", file_results


_FOLDER_PERF_LOG_FILENAME = "smart_folder_performance.log"


def _record_folder_archive_phase_timings(
    phase_times, file_count, folder_count, total_bytes, folder_path, file_results=None, outcome="committed"
):
    # Real per-operation phase timings for a folder Archive - see
    # add_folder_to_archive_smart's own note on why this stays in
    # permanently: the real ~10.36GB/~2,900-file/~655-folder Beta QA case
    # this whole investigation is about isn't reproducible in full locally,
    # so the next time it actually runs, this tells us exactly where its
    # wall-clock time went instead of requiring another guess.
    #
    # print() alone is not enough: the real Beta EXE is built --windowed
    # (see build_storagearch_windows.ps1 / the Beta build steps this
    # session ran) - a windowed PyInstaller app has NO console, so
    # anything written to stdout is simply discarded, never seen by
    # anyone. This writes the same aggregate data to a small persistent
    # local log file under storage_config.log_dir() instead (StorageArch's
    # normal local app-data area, next to archives/workspace/temp), opened
    # in append mode and explicitly flushed (+ fsync'd) immediately after
    # each write - so the entry is durably on disk even if the GUI hangs
    # or crashes moments later, not sitting in an OS write buffer that
    # never gets to disk.
    #
    # Privacy: only aggregate numbers and the folder's own basename go in
    # - no per-file names, no full source paths, no hashes, no archive
    # member names, nothing that could identify what was actually in the
    # folder beyond a bare directory name the user themselves chose.
    total = sum(phase_times.values())

    worst_file_type = None
    worst_strategy = None
    worst_processing_seconds = 0.0
    if file_results:
        by_type_strategy = {}
        for r in file_results:
            key = (r["file_type"], r["strategy"] or "baseline")
            by_type_strategy[key] = by_type_strategy.get(key, 0.0) + r["processing_ms"]
        (worst_file_type, worst_strategy), worst_processing_ms = max(
            by_type_strategy.items(), key=lambda kv: kv[1], default=((None, None), 0.0)
        )
        worst_processing_seconds = worst_processing_ms / 1000

    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "folder_name": os.path.basename(os.path.normpath(folder_path)),
        "total_bytes": total_bytes,
        "file_count": file_count,
        "folder_count": folder_count,
        "folder_hash_seconds": round(phase_times.get("folder_hash", 0.0), 3),
        "smart_pipeline_seconds": round(phase_times.get("smart_decide_and_zip_write", 0.0), 3),
        "verify_seconds": round(phase_times.get("verify", 0.0), 3),
        "manifest_commit_seconds": round(phase_times.get("commit", 0.0), 3),
        "total_seconds": round(total, 3),
        "worst_file_type": worst_file_type,
        "worst_strategy": worst_strategy,
        "worst_processing_seconds": round(worst_processing_seconds, 3),
        "outcome": outcome,
    }

    try:
        log_dir = storage_config.log_dir()
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, _FOLDER_PERF_LOG_FILENAME)
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            log_file.flush()
            os.fsync(log_file.fileno())
    except OSError:
        # Diagnostics must never break archiving - a failed log write is
        # silently skipped, same "never let this be the reason an Archive
        # operation fails" contract Smart Compression itself follows.
        pass


def _restore_smart_jobs_concurrently(jobs):
    """Restores every (target_path, original_filename, strategy_name,
    original_sha256, stored_bytes, original_size) job in `jobs` using a
    bounded pool of persistent worker processes dispatched across threads -
    see checkout_folder_smart's own docstring for why this replaced a plain
    sequential loop over smart_compression.restore_original_bytes() (one
    fresh spawned process per member, with nothing else running while it
    happened). Propagates the first smart_compression.SmartRestoreError
    encountered (future.result() re-raises it on the calling thread) after
    still waiting for every other in-flight restore to finish, so no
    worker thread or process is ever abandoned mid-restore.

    `original_size` feeds smart_compression.restore_timeout_for(strategy_
    name, original_size) - real, reproduced evidence (see restore_timeout_
    for's own module-level comment in smart_compression.py) showed the
    previous flat 30s default is too tight for jpeg_xl_lossless_jpeg/
    bmp_jxl_lossless on large files once THIS SAME concurrent pool is
    running many restores at once - every other strategy, and these two on
    small/medium files, keep the exact unchanged 30s timeout.
    """
    with smart_compression.SmartWorkerPool() as pool:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=pool.size)

        def _restore_one(target_path, original_filename, strategy_name, original_sha256, stored_bytes, original_size):
            session = pool.acquire()
            try:
                timeout_s = smart_compression.restore_timeout_for(strategy_name, original_size)
                restored_bytes = smart_compression.restore_original_bytes(
                    strategy_name, stored_bytes, original_filename, original_sha256, timeout_s=timeout_s, session=session
                )
            finally:
                pool.release(session)
            with open(target_path, "wb") as dst:
                dst.write(restored_bytes)

        try:
            futures = [executor.submit(_restore_one, *job) for job in jobs]
            for future in futures:
                future.result()
        finally:
            executor.shutdown(wait=True)


def checkout_folder_smart(zip_path, path_in_archive, destination, display_name):
    """Folder analogue of checkout_item_smart. A folder's members may each
    carry a DIFFERENT Smart Compression strategy (or none) - unlike a
    single file there is no one top-level strategy_name to pass in, so
    each member's own embedded ZipInfo.comment tag (written by
    add_folder_to_archive_smart) is read directly to decide how to restore
    it, exactly the same self-describing tag create_archive_manifest_smart
    already reads when rebuilding the manifest. Any member with no smart
    tag (legacy/baseline storage) is extracted byte-for-byte, matching
    archive_manager.checkout_item's behavior for that member exactly.
    Raises smart_compression.SmartRestoreError on any restore failure -
    callers must treat that as a hard integrity error, same contract as
    checkout_item_smart.

    Smart-stored members are restored CONCURRENTLY through a
    smart_compression.SmartWorkerPool, dispatched via a thread pool exactly
    like add_folder_to_archive_smart dispatches decide_and_prepare - not
    one restore_original_bytes() call after another, each spawning (and,
    on a frozen PyInstaller onefile build, paying the onefile self-
    extraction cost of) its own fresh worker process. This function is the
    real workload behind main.py's post-commit _verify_write_by_readback
    call for a just-archived folder: a real ~10.36GB/~2,900-file Beta test
    confirmed the ORIGINAL sequential/unpooled version of this loop as the
    exact cause of the GUI staying on "Archiving..." for a long time AFTER
    the archive was already committed and visible (in the database, and
    from a second StorageArch window) - CPU near 0% and low disk activity
    the whole time, consistent with the calling thread mostly idle waiting
    on one process spawn at a time rather than doing real restore work.
    Legacy/baseline members are still extracted directly on the calling
    thread (zipfile.extract's own C-level DEFLATE decompression is already
    fast and requires no subprocess).
    """
    if not os.path.isfile(zip_path):
        return None

    if destination is None:
        import storage_config
        destination = storage_config.workspace_dir()
    os.makedirs(destination, exist_ok=True)

    normalized = path_in_archive.rstrip("/")

    with zipfile.ZipFile(zip_path, "r") as zip_file:
        members = [
            info for info in zip_file.infolist()
            if info.filename == normalized or info.filename.startswith(normalized + "/")
        ]
        if not members:
            return None

        extract_root = os.path.join(destination, normalized)

        smart_jobs = []  # (target_path, strategy_name, original_sha256, stored_bytes, original_size)
        for info in members:
            if info.is_dir():
                continue
            target_path = os.path.join(destination, *info.filename.split("/"))
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            parsed = _parse_smart_comment(info.comment)
            if parsed is None:
                with zip_file.open(info.filename, "r") as src, open(target_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            else:
                strategy_name, original_sha256, original_size = parsed
                smart_jobs.append((target_path, os.path.basename(info.filename), strategy_name, original_sha256, zip_file.read(info.filename), original_size))

    if smart_jobs:
        _restore_smart_jobs_concurrently(smart_jobs)

    if not display_name or display_name == normalized:
        return extract_root

    final_path = os.path.join(destination, display_name)
    if os.path.exists(final_path):
        if os.path.isdir(final_path):
            shutil.rmtree(final_path)
        else:
            os.remove(final_path)
    os.replace(extract_root, final_path)
    return final_path


def checkin_item_smart(zip_path, path_in_archive, source_path, disabled_strategies=None):
    """Same contract as archive_manager.checkin_item, plus a 2nd return
    value: the strategy name used this time (may differ from what the
    item was previously stored with - the file is re-evaluated from
    scratch, never assumed to keep its old strategy). Folders always use
    the unmodified legacy path - Smart Compression only targets single
    files, matching Phase 1/2 scope.

    `disabled_strategies` (optional): see add_file_to_archive_smart's doc
    comment - same pass-through, same source of truth.
    """
    if not os.path.isfile(zip_path) or not os.path.exists(source_path):
        return False, None

    if os.path.isdir(source_path):
        return archive_manager.checkin_item(zip_path, path_in_archive, source_path), None

    normalized = path_in_archive.rstrip("/")

    with zipfile.ZipFile(zip_path, "r") as existing_zip:
        existing_names = existing_zip.namelist()
    if normalized not in existing_names:
        return False, None

    try:
        decision = smart_compression.decide_and_prepare(source_path, disabled_strategies=disabled_strategies)
    except Exception:  # noqa: BLE001
        decision = None

    def write_members(new_zip):
        if decision is not None:
            info = zipfile.ZipInfo(normalized)
            info.comment = _smart_comment(decision.strategy_name, decision.original_sha256, decision.original_size)
            new_zip.writestr(info, decision.stored_bytes, compress_type=zipfile.ZIP_STORED)
        else:
            new_zip.write(source_path, normalized)

    # נתיב append: מסירה רק את רשומת ה-Central Directory של הפריט הישן
    # (replaced_names) וכותבת את התוכן החדש תחת אותו שם - בלי לגעת בבתים
    # של אף פריט אחר בארכיון (ראו archive_manager._append_new_members).
    guard = archive_manager._append_new_members(zip_path, write_members, replaced_names=[normalized])
    if guard is None:
        return False, None
    guard_path, start_dir = guard

    if decision is not None:
        verify_ok = archive_manager.calculate_zip_file_hash(zip_path, normalized) == decision.stored_sha256
    else:
        verify_ok = archive_manager.calculate_zip_file_hash(zip_path, normalized) == archive_manager.calculate_file_hash(source_path)

    if not verify_ok:
        archive_manager._rollback_append(zip_path, guard_path, start_dir)
        return False, None

    archive_manager._confirm_append(guard_path)

    category_name = os.path.basename(os.path.dirname(zip_path))
    manifest_path, _archive_sha256 = create_archive_manifest_smart(zip_path, category_name, compute_archive_hash=False)
    strategy_name = decision.strategy_name if decision is not None else None
    return manifest_path is not None, strategy_name


def checkout_item_smart(zip_path, path_in_archive, destination, display_name, strategy_name, original_hash):
    """Same contract as archive_manager.checkout_item for the legacy case
    (strategy_name is None/baseline). For smart-stored items, extracts the
    stored representation and runs the matching strategy's restore path.
    Raises smart_compression.SmartRestoreError on any failure - callers
    must treat that as a hard integrity error, never fall back to
    returning the (wrong) stored bytes as if they were the original file.
    """
    if strategy_name is None or strategy_name == smart_compression.BASELINE_STRATEGY_NAME:
        return archive_manager.checkout_item(zip_path, path_in_archive, destination, display_name)

    if not os.path.isfile(zip_path):
        return None

    if destination is None:
        import storage_config
        destination = storage_config.workspace_dir()
    os.makedirs(destination, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        stored_bytes = zf.read(path_in_archive)

    final_name = display_name or os.path.basename(path_in_archive)
    restored_bytes = smart_compression.restore_original_bytes(strategy_name, stored_bytes, final_name, original_hash)

    final_path = os.path.join(destination, final_name)
    with open(final_path, "wb") as f:
        f.write(restored_bytes)
    return final_path


def create_archive_manifest_smart(zip_path, category_name, compute_archive_hash=True):
    """Same contract/output shape as archive_manager.create_archive_manifest,
    with two additional OPTIONAL per-file keys populated only for smart-
    stored members: "strategy" and "original_sha256". Legacy members are
    written with exactly the same keys as before - old manifest readers
    (and archive_manager.verify_archive, unmodified) see no difference.

    Runs after EVERY archive/checkin operation on this category (see
    refresh_archive_details in main.py), so its own cost adds directly to
    every single Archive click's wall-clock time. The naive version of this
    function (re-open the whole zip via archive_manager.calculate_zip_file_
    hash, which itself does its own zipfile.ZipFile(...).namelist() scan,
    once per member) made ONE manifest refresh cost O(total members in the
    category so far) - so a category that has accumulated many items over a
    real Beta testing session made every SUBSEQUENT archive operation
    progressively slower, confirmed directly: archiving the Nth item into a
    growing category measurably slowed from ~0.03s to ~0.2s+ by N=60 in this
    session's own measurement, entirely independent of the new item's own
    size. Two fixes, both safe/behavior-preserving:
      1. Hash each member using the SAME already-open `zip_file` handle
         instead of re-opening (and re-scanning the whole central
         directory of) the zip file once per member.
      2. Skip re-hashing a member's content entirely when it's provably
         unchanged since the last manifest: zip members are only ever
         copied byte-for-byte or replaced wholesale (never mutated in
         place - see _build_temp_zip's mutate callbacks), so an unchanged
         CRC-32 (already known for free from infolist(), no bytes read)
         for the same path_in_archive means the previously-recorded
         sha256 is still correct. A brand-new or genuinely-changed member
         (different CRC-32, or no prior record) still gets fully
         re-hashed exactly as before - this only skips work that's
         provably redundant, never trades away correctness.

    Returns (manifest_path, archive_sha256) - archive_sha256 is the SAME
    full-archive hash this function already computes to write into the
    manifest (archive_manager.calculate_archive_hash(zip_path)). Returning
    it lets callers (main.py's refresh_archive_details) reuse that value
    instead of immediately recomputing it via a second full read of the
    zip file - see Cause B in the perf investigation this function's
    docstring already documents. On failure, returns (None, None).

    `compute_archive_hash` (default True): a true whole-archive SHA-256
    requires reading every byte of the final zip file once - for a
    multi-GB category that single unavoidable read is still ~30s, and
    happening synchronously on every single Archive/Checkin click (even
    to add one tiny item) is the remaining root cause after Cause B's
    redundant-recompute fix. Callers on the hot "just wrote this file"
    path (main.py's refresh_archive_details in its fast/non-deep mode)
    pass False to skip it entirely: archive_sha256 is written as None
    (meaning "not yet independently re-verified since this write", not
    "corrupted" or "changed") and this function returns (manifest_path,
    None). Per-member entries (path/size/sha256/_crc32/strategy) are
    ALWAYS computed in full regardless of this flag - only the one
    whole-file hash is skippable, and archive_manager.verify_archive
    already treats a None archive_sha256 as "no baseline recorded yet"
    rather than as a mismatch, so this never produces a false CHANGED/
    CORRUPTED reading. The explicit "Verify Archive" action (main.py's
    verify_archive_by_id) is unaffected - it calls archive_manager.
    verify_archive directly and always performs the true deep check,
    computing a fresh whole-archive hash itself.
    """
    if not os.path.isfile(zip_path):
        return None, None

    manifest_path = os.path.splitext(zip_path)[0] + ".manifest.json"
    current_time = datetime.now().isoformat(timespec="seconds")
    created_at = current_time

    old_entries_by_path = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as manifest_file:
                old_manifest = json.load(manifest_file)
                created_at = old_manifest.get("created_at", current_time)
                for old_entry in old_manifest.get("files", []):
                    path = old_entry.get("path_in_archive")
                    if path:
                        old_entries_by_path[path] = old_entry
        except (json.JSONDecodeError, OSError):
            created_at = current_time

    files = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            for file_info in zip_file.infolist():
                if file_info.is_dir():
                    continue

                cached = old_entries_by_path.get(file_info.filename)
                if (
                    cached is not None
                    and cached.get("sha256")
                    and cached.get("_crc32") == file_info.CRC
                ):
                    stored_hash = cached["sha256"]
                else:
                    hash_value = hashlib.sha256()
                    with zip_file.open(file_info.filename, "r") as archived_file:
                        while True:
                            chunk = archived_file.read(65536)
                            if not chunk:
                                break
                            hash_value.update(chunk)
                    stored_hash = hash_value.hexdigest()

                entry = {
                    "path_in_archive": file_info.filename,
                    "original_size": file_info.file_size,
                    "compressed_size": file_info.compress_size,
                    "sha256": stored_hash,
                    "_crc32": file_info.CRC,  # cache key only, see docstring - not part of the documented manifest schema
                }

                parsed = _parse_smart_comment(file_info.comment)
                if parsed is not None:
                    strategy_name, original_sha256, true_original_size = parsed
                    entry["strategy"] = strategy_name
                    entry["original_sha256"] = original_sha256
                    entry["original_size"] = true_original_size  # override: zip's own size field is the STORED size for smart entries

                files.append(entry)
    except (zipfile.BadZipFile, OSError):
        return None, None

    archive_sha256 = archive_manager.calculate_archive_hash(zip_path) if compute_archive_hash else None

    manifest_data = {
        "manifest_version": 2,
        "archive_name": os.path.basename(zip_path),
        "category": category_name,
        "created_at": created_at,
        "updated_at": current_time,
        "item_count": len(files),
        "archive_size": os.path.getsize(zip_path),
        "archive_sha256": archive_sha256,
        "files": files,
    }

    temporary_path = manifest_path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as manifest_file:
        json.dump(manifest_data, manifest_file, ensure_ascii=False, indent=4)
    os.replace(temporary_path, manifest_path)

    return manifest_path, archive_sha256
