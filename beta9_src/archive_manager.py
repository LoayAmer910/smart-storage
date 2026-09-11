import concurrent.futures
import ctypes
import os
import shutil
import time
import zipfile
import hashlib
from datetime import datetime
import json

import storage_config

# Small bounded retry for filesystem operations that can transiently fail on
# Windows with an access-denied/sharing-violation OSError when another
# process - most commonly antivirus real-time scanning - holds a brief lock
# on a file this process just finished writing (or is about to remove or
# rename). A real ~10.36GB/~2,900-file Beta QA run hit exactly this: the
# post-write verify/commit phase of add_folder_to_archive_smart failed with
# a bare OSError and no prior handling anywhere in that phase even
# attempted a retry, immediately after a sustained high-disk-activity write
# burst - the exact window where a large just-written file is most likely
# to still be a live AV scan target. This is NOT a substitute for real
# error handling: it only retries plain OSError/PermissionError, a small
# bounded number of times with a short backoff, and re-raises the LAST
# exception unchanged once attempts are exhausted - a permanent failure
# (disk full, a genuinely missing/corrupted path, a real permissions
# problem) still surfaces exactly as before, just without being masked by
# a transient AV-timing race on a large file.
_TRANSIENT_RETRY_ATTEMPTS = 4
_TRANSIENT_RETRY_BASE_DELAY_S = 0.25  # doubles each subsequent attempt


def _retry_transient_os_error(func, *args, **kwargs):
    delay = _TRANSIENT_RETRY_BASE_DELAY_S
    for attempt in range(_TRANSIENT_RETRY_ATTEMPTS):
        try:
            return func(*args, **kwargs)
        except OSError:
            if attempt == _TRANSIENT_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay *= 2

INVALID_PATH_CHARS = '<>:"/\\|?*'

# Characters Windows never allows inside a single path component (unlike
# INVALID_PATH_CHARS above, this intentionally excludes "/" and "\\" - those
# are legitimate separators when validating a full path rather than one
# sanitized name).
_INVALID_COMPONENT_CHARS = set('<>:"|?*') | {chr(c) for c in range(32)}
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_WINDOWS_PATH_LENGTH = 260


class InvalidPathError(OSError):
    """A path could not be read because of its name or length, not a transient I/O failure.

    Subclasses OSError so existing `except OSError` handlers (e.g. in
    main.py's _archive_*_impl) keep working unchanged; callers that want to
    show a more specific message can catch this first.
    """


class CloudPlaceholderError(InvalidPathError):
    """A file could not be read because it is a OneDrive/cloud "Files On-Demand"
    placeholder that has not actually been downloaded to this device yet -
    Windows raises a bare OSError [Errno 22] Invalid argument for these,
    identical in shape to a genuinely invalid path, so this needs its own
    detection to tell the user the real, actionable cause."""


# OneDrive (and similar cloud-sync clients) mark a not-yet-downloaded file
# with one of these attributes instead of transparently downloading it on
# open - reading such a file can raise a bare OSError [Errno 22] Invalid
# argument that looks identical to a genuinely invalid path.
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_PLACEHOLDER_MASK = (
    _FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def _is_cloud_placeholder(path):
    if os.name != "nt":
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
    except OSError:
        return False
    if attrs == _INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & _CLOUD_PLACEHOLDER_MASK)


def _invalid_component_reason(component):
    # A single path component (no separators) is invalid on Windows if it:
    # ends with a trailing space/dot, uses a reserved device name (with or
    # without an extension), or contains a character the filesystem rejects.
    if not component:
        return None
    stem = component.rsplit(".", 1)[0] if "." in component else component
    if stem.upper() in _RESERVED_WINDOWS_NAMES:
        return f"'{component}' is a reserved system name"
    if component != component.rstrip(" ."):
        return f"'{component}' ends with a trailing space or dot"
    bad_chars = sorted(set(component) & _INVALID_COMPONENT_CHARS)
    if bad_chars:
        return f"'{component}' contains invalid character(s): {' '.join(bad_chars)}"
    return None


def validate_source_path(path):
    """Checks a source file/folder path for problems that would make Windows
    refuse to open it (raising OSError [Errno 22] deep inside hashing/zipping),
    before any backend work starts.

    Returns (True, None) if the path looks safe to use, or (False, reason)
    with a short English reason describing the first problem found.
    """
    if not isinstance(path, str) or not path.strip():
        return False, "Path is empty"
    if not os.path.isabs(path):
        return False, "Path must be absolute"
    if len(path) > _MAX_WINDOWS_PATH_LENGTH:
        return False, f"Path is too long ({len(path)} > {_MAX_WINDOWS_PATH_LENGTH} characters)"
    if not os.path.exists(path):
        return False, "Path does not exist"

    drive, tail = os.path.splitdrive(path)
    for component in tail.replace("/", "\\").split("\\"):
        reason = _invalid_component_reason(component)
        if reason:
            return False, reason

    return True, None


PROTECTED_ROOTS = (
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
)


def _sanitize_name(name):
    if not isinstance(name, str):
        name = str(name)
    sanitized = "".join(
        ch if ch not in INVALID_PATH_CHARS else "_"
        for ch in name
    ).strip()
    sanitized = sanitized or "category"
    return sanitized


def create_storage_folders():
    """
    building folders for SmartArchiveData
    os.makedirs: allows to make changes in os
    """

    os.makedirs(storage_config.archives_dir(), exist_ok=True)
    os.makedirs(storage_config.workspace_dir(), exist_ok=True)
    os.makedirs(storage_config.temp_dir(), exist_ok=True)
    os.makedirs(storage_config.log_dir(), exist_ok=True)


def get_category_path(category_name):
    sanitized_name = _sanitize_name(category_name)
    category_path = os.path.join(
        storage_config.archives_dir(),
        sanitized_name
    )
    return category_path


def create_category(category_name):
    category_path = get_category_path(category_name)
    os.makedirs(category_path, exist_ok=True)
    return category_path


def _get_archive_filename(category_name, sequence_number):
    sanitized = _sanitize_name(category_name)
    return f"{sanitized}_{sequence_number:03d}.zip"


def list_category_archives(category_name):
    category_path = get_category_path(category_name)
    if not os.path.isdir(category_path):
        return []

    zip_files = [
        os.path.join(category_path, entry)
        for entry in os.listdir(category_path)
        if entry.lower().endswith(".zip")
    ]
    zip_files.sort()
    return zip_files


def get_archive_sequence_number(zip_path):
    base_name = os.path.splitext(os.path.basename(zip_path))[0]
    parts = base_name.rsplit("_", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def get_next_archive_path(category_name):
    category_path = create_category(category_name)
    archives = list_category_archives(category_name)
    if not archives:
        return os.path.join(category_path, _get_archive_filename(category_name, 1))

    latest = archives[-1]
    last_seq = get_archive_sequence_number(latest)
    next_seq = 1 if last_seq is None else last_seq + 1
    return os.path.join(category_path, _get_archive_filename(category_name, next_seq))


def get_archive_current_size(zip_path):
    try:
        return os.path.getsize(zip_path)
    except OSError:
        return 0


def get_selected_archive(category_name, max_archive_size=None, estimated_new_size=0):
    archives = list_category_archives(category_name)
    if not archives:
        return get_next_archive_path(category_name)

    if max_archive_size is None:
        return get_next_archive_path(category_name)

    for zip_path in archives:
        current_size = get_archive_current_size(zip_path)
        if current_size + estimated_new_size <= max_archive_size:
            return zip_path

    return get_next_archive_path(category_name)


def has_enough_free_space(path, needed_bytes):
    # בודקת שיש מספיק מקום פנוי בכונן שעליו נמצא path, לפני דחיסה/שליפה/בנייה מחדש (כלל 11.2).
    check_path = os.path.abspath(path)
    while not os.path.exists(check_path):
        parent = os.path.dirname(check_path)
        if not parent or parent == check_path:
            check_path = os.path.abspath(".")
            break
        check_path = parent

    try:
        usage = shutil.disk_usage(check_path)
    except OSError:
        return False

    return usage.free >= needed_bytes


def _is_protected_path(path):
    # מגנה על תיקיות המערכת של Windows - אין לגעת בהן בשום פעולה.
    try:
        resolved = os.path.abspath(path)
    except (OSError, ValueError):
        return True

    normalized = resolved.rstrip("\\/").lower()
    for protected in PROTECTED_ROOTS:
        protected_normalized = protected.rstrip("\\/").lower()
        if normalized == protected_normalized or normalized.startswith(protected_normalized + os.sep):
            return True
    return False


def is_protected_path(path):
    return _is_protected_path(path)


def remove_source(source_path, mode):
    # מטפלת במקור אחרי ארכוב מוצלח, לפי בחירת המשתמש (7.3.11-12).
    # mode: "keep" (השארה), "recycle" (סל מיחזור), "delete" (מחיקה לצמיתות)
    if mode == "keep":
        return True
    if mode not in ("recycle", "delete"):
        return False
    if not os.path.exists(source_path):
        return False
    if _is_protected_path(source_path):
        return False

    try:
        if mode == "recycle":
            from send2trash import send2trash
            send2trash(source_path)
        else:
            if os.path.isdir(source_path):
                shutil.rmtree(source_path)
            else:
                os.remove(source_path)
    except OSError:
        return False
    return True


def recover_interrupted_operations(data_root=None):
    # מסירה קובצי .temp יתומים שנשארו מפעולה שנקטעה (כלל 11.4).
    # קובץ .temp מוחלף בארכיון האמיתי רק אחרי אימות מוצלח (os.replace),
    # כך שקובץ .temp יתום פירושו תמיד שהארכיון התקין הקודם עדיין שלם.
    if data_root is None:
        archives_root = storage_config.archives_dir()
    else:
        archives_root = os.path.join(data_root, "archives")
    removed = []
    if not os.path.isdir(archives_root):
        return removed

    for current_path, _dir_names, file_names in os.walk(archives_root):
        for file_name in file_names:
            if file_name.endswith(".temp"):
                temp_path = os.path.join(current_path, file_name)
                try:
                    os.remove(temp_path)
                    removed.append(temp_path)
                except OSError:
                    pass
            elif file_name.endswith(".sizeguard"):
                # קובץ .sizeguard יתום פירושו שפעולת append נקטעה (קריסה/
                # הפסקת חשמל) לפני שהושלמה. append כותב החל ממיקום
                # start_dir (תחילת ה-Central Directory הישן), ולכן חיתוך
                # (truncate) לאורך המקורי בלבד אינו מספיק - הבתים בין
                # start_dir לאורך המקורי כבר נדרסו. במקום זאת, משחזרים את
                # הגיבוי (.appendbackup, ראו _record_append_guard) חזרה
                # למקום המדויק ורק אז מקצצים - שחזור זהה-בתים לארכיון
                # המקורי, בדיוק כמו rollback רגיל בתוך אותו ריצה (11.3/11.4).
                guard_path = os.path.join(current_path, file_name)
                zip_path = guard_path[: -len(".sizeguard")]
                backup_path = zip_path + ".appendbackup"
                try:
                    with open(guard_path, "r", encoding="utf-8") as guard_file:
                        start_dir = int(guard_file.read().strip())
                    if os.path.isfile(zip_path) and os.path.isfile(backup_path):
                        with open(backup_path, "rb") as backup_file:
                            tail_backup = backup_file.read()
                        with open(zip_path, "r+b") as zip_file:
                            zip_file.seek(start_dir)
                            zip_file.write(tail_backup)
                            zip_file.truncate(start_dir + len(tail_backup))
                        removed.append(zip_path)
                except (OSError, ValueError):
                    pass
                finally:
                    for stray_path in (guard_path, backup_path):
                        try:
                            os.remove(stray_path)
                        except OSError:
                            pass
            elif file_name.endswith(".appendbackup"):
                # גיבוי יתום בלי guard תואם: הקריסה קרתה בין כתיבת הגיבוי
                # לכתיבת ה-guard עצמו (ראו _record_append_guard) - ה-append
                # על הארכיון עוד לא התחיל כלל, אין מה לשחזר, רק לנקות.
                candidate_guard = os.path.join(current_path, file_name[: -len(".appendbackup")] + ".sizeguard")
                if not os.path.isfile(candidate_guard):
                    stray_backup = os.path.join(current_path, file_name)
                    try:
                        os.remove(stray_backup)
                        removed.append(stray_backup)
                    except OSError:
                        pass
    return removed


# מחשבת טביעת אצבע(האש) מסוג SHA-256 עבור קובץ.
#
# מקבלת:
# file_path - הנתיב של הקובץ
#
# מחזירה:
# את טביעת האצבע של הקובץ כמחרוזת
# אם הקובץ לא קיים, מחזירה None
def calculate_file_hash(file_path):
    if not os.path.isfile(file_path):
        return None

    hash_value = hashlib.sha256()

    try:
        with open(file_path, "rb") as file:
            while True:
                file_part = file.read(4096)

                if not file_part:
                    break

                hash_value.update(file_part)
    except OSError as exc:
        # Windows raises a bare OSError [Errno 22] Invalid argument both for
        # names it can't open (trailing space/dot, reserved device name,
        # path too long, ...) and for OneDrive/cloud "Files On-Demand"
        # placeholders that were never actually downloaded to this device -
        # wrap it here, at the one place that actually calls open() on the
        # raw path, so every caller sees a specific, actionable reason
        # instead of an unexplained errno.
        if _is_cloud_placeholder(file_path):
            raise CloudPlaceholderError(
                f"'{file_path}' is a OneDrive/cloud file that has not been downloaded to this device"
            ) from exc
        is_valid, reason = validate_source_path(file_path)
        if not is_valid:
            raise InvalidPathError(f"Cannot read '{file_path}': {reason}") from exc
        raise InvalidPathError(f"Cannot read '{file_path}': {exc.strerror or exc}") from exc

    return hash_value.hexdigest()


def calculate_zip_file_hash(zip_path, file_name):
    # מחשבת טביעת SHA-256 של קובץ שנמצא בתוך ZIP.
    # מקבלת:
    # zip_path - הנתיב של קובץ ה-ZIP
    # file_name - שם הקובץ שנמצא בתוכו
    # מחזירה:
    # את טביעת האצבע של הקובץ
    # אם הקובץ לא נמצא ב-ZIP, מחזירה None
    if not os.path.isfile(zip_path):
        return None

    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            if file_name not in zip_file.namelist():
                return None

            hash_value = hashlib.sha256()

            # קוראת את הקובץ מתוך ה-ZIP בחלקים קטנים.
            with zip_file.open(file_name, "r") as archived_file:
                while True:
                    file_part = archived_file.read(4096)

                    if not file_part:
                        break

                    hash_value.update(file_part)

        return hash_value.hexdigest()
    except (zipfile.BadZipFile, OSError):
        return None


def calculate_archive_hash(zip_path):
    # מחשבת Hash לכל קובץ ה-ZIP כיחידה אחת.
    return calculate_file_hash(zip_path)


def calculate_folder_hash(folder_path):
    # מחשבת את ה-Hash של כל קבצי התיקייה (לזיהוי תוכן ולמניעת כפילויות) -
    # ראו התיעוד למטה על מקבילות. הפלט זהה-בית לגרסה הישנה, הרצה-ברצף.
    if not os.path.isdir(folder_path):
        return None

    entries = []  # [(relative_path, full_path), ...] - same deterministic sorted-walk order as before
    for root, dirs, files in os.walk(folder_path):
        dirs.sort()
        files.sort()
        for file_name in files:
            full_path = os.path.join(root, file_name)
            if _is_cloud_placeholder(full_path):
                # A OneDrive/cloud file that hasn't been downloaded to this
                # device yet can't be read at all (see CloudPlaceholderError)
                # - excluded from the folder's content hash the same way a
                # single such file is refused outright by calculate_file_hash,
                # rather than failing the whole folder over one un-downloaded
                # member. add_folder_to_archive_smart's own folder walk
                # applies the identical skip so the archived content matches
                # what was actually hashed here.
                continue
            relative_path = os.path.relpath(full_path, folder_path).replace(os.sep, "/")
            entries.append((relative_path, full_path))

    if not entries:
        return hashlib.sha256().hexdigest()

    # Hashing every file's content used to run one file at a time in this
    # single thread, entirely BEFORE the (already-parallel) Smart decision
    # phase even starts - a real, measured, fully sequential O(total folder
    # bytes) pass with no overlap with anything else in the operation.
    # Each file's hash is independent I/O-bound work, so it's safe to
    # compute them concurrently (calculate_file_hash releases the GIL
    # during its actual file reads) - only the final combining step (a
    # cheap, tiny per-entry hash_value.update() of a path string + a
    # 64-char hex digest) still runs sequentially, in the SAME sorted-walk
    # order as before, so the resulting folder hash is byte-identical to
    # what the old single-threaded version would have produced for the
    # same folder content.
    file_hashes = [None] * len(entries)

    def _hash_one(index, full_path):
        file_hashes[index] = calculate_file_hash(full_path)

    max_workers = min(8, os.cpu_count() or 4, len(entries))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_hash_one, index, full_path) for index, (_relative_path, full_path) in enumerate(entries)]
        for future in futures:
            future.result()

    hash_value = hashlib.sha256()
    for (relative_path, _full_path), file_hash in zip(entries, file_hashes):
        if file_hash is None:
            return None
        hash_value.update(relative_path.encode("utf-8"))
        hash_value.update(file_hash.encode("utf-8"))
    return hash_value.hexdigest()


def get_folder_total_size(folder_path):
    total_size = 0
    for root, dirs, files in os.walk(folder_path):
        for file_name in files:
            file_path = os.path.join(root, file_name)
            try:
                total_size += os.path.getsize(file_path)
            except OSError:
                return None
    return total_size


def _build_temp_zip(zip_path, mutate):
    # בונה ZIP חדש בקובץ זמני (zip_path + ".temp") בלי לגעת בארכיון הקיים.
    # mutate(old_zip_or_None, new_zip) ממלאת את הארכיון החדש.
    # מחזירה את נתיב הקובץ הזמני בהצלחה, או None אם נכשל (והקובץ הזמני מוסר).
    temp_path = zip_path + ".temp"
    category_dir = os.path.dirname(zip_path)
    if category_dir:
        os.makedirs(category_dir, exist_ok=True)

    try:
        if os.path.isfile(zip_path):
            with zipfile.ZipFile(zip_path, "r") as old_zip:
                with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as new_zip:
                    mutate(old_zip, new_zip)
        else:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as new_zip:
                mutate(None, new_zip)
    except (zipfile.BadZipFile, OSError):
        if os.path.isfile(temp_path):
            os.remove(temp_path)
        return None

    return temp_path


def _commit_temp_zip(temp_path, zip_path):
    # מחליפה את הארכיון הישן בחדש רק אחרי שהחדש אומת - שינוי אטומי (כלל 11.3).
    # Retried (see _retry_transient_os_error): os.replace is still atomic on
    # every attempt - a mid-replace failure never leaves a half-written
    # zip_path, it just means the rename hasn't happened yet - but on
    # Windows the rename itself can transiently fail with a sharing
    # violation if something else (most commonly AV) has zip_path or
    # temp_path briefly open, which a short bounded retry resolves without
    # weakening the atomicity guarantee at all: it's the exact same
    # os.replace call, just possibly attempted more than once.
    _retry_transient_os_error(os.replace, temp_path, zip_path)


def _discard_temp_zip(temp_path):
    # Best-effort cleanup of an already-abandoned temp file - the operation
    # that would have used it has already failed/been reported by the
    # caller either way, so a persistent failure to delete it (after
    # retrying transient AV-lock-style failures) must never itself raise
    # and mask that already-determined outcome; it just leaves one orphaned
    # `<zip>.temp` file behind, which is harmless and gets overwritten by
    # the next attempt at this same path.
    if not temp_path:
        return
    try:
        if os.path.isfile(temp_path):
            _retry_transient_os_error(os.remove, temp_path)
    except OSError:
        pass


# --- Append-mode fast path -------------------------------------------------
#
# _build_temp_zip's mutate() callbacks always copy EVERY existing member
# forward (decompress old bytes, recompress into the new zip) just to add
# one new member. For a multi-GB archive that means gigabytes of needless
# CPU+I/O for a tiny addition. Python's zipfile 'a' (append) mode is a
# strictly cheaper way to add (or replace) members: it only writes new
# local file records after the existing ones and rewrites the (small,
# metadata-only) central directory at the end of the file - it never reads
# or rewrites any EXISTING member's content bytes.
#
# This trades _build_temp_zip's "build a whole new file, then os.replace()"
# atomicity model (which works because the old file is never touched) for
# a different one: the existing file IS mutated in place. To keep the same
# "a failed/interrupted write can never corrupt the previously-committed
# archive" guarantee, a backup of the bytes append is about to overwrite is
# recorded before writing anything.
#
# IMPORTANT: append does not simply extend the file - it starts writing at
# start_dir (the byte offset of the OLD central directory), which is
# BEFORE the file's current end (the old central directory + End-Of-
# Central-Directory record occupy the tail after start_dir). So recording
# only the pre-append file LENGTH and later truncating back to it is NOT a
# valid rollback: the bytes between start_dir and that length get
# overwritten by the new local file header/data as soon as writing starts,
# and truncating to a length alone does not restore what used to be there
# - it leaves a mix of new/partial bytes and can turn the file into an
# unreadable/BadZipFile archive. The actual OLD tail bytes (start_dir to
# EOF - metadata only, proportional to member COUNT, not to archive
# content size, so cheap to copy even for a multi-GB archive) are backed
# up to a sibling `<zip>.appendbackup` file, with a `<zip>.sizeguard`
# marker recording start_dir, before any byte is touched. Any failure - an
# exception during the append, a failed post-write hash verification, or a
# crash/power-loss that leaves an orphaned guard+backup pair for
# recover_interrupted_operations() to find later - is handled by writing
# the backed-up tail back to start_dir and truncating there, which is
# byte-identical to the appended bytes never having been written at all.
# This mirrors the orphaned-.temp recovery _build_temp_zip already relies
# on (11.3/11.4).
def _record_append_guard(zip_path):
    with zipfile.ZipFile(zip_path, "r") as probe:
        start_dir = probe.start_dir

    with open(zip_path, "rb") as zip_file:
        zip_file.seek(start_dir)
        tail_backup = zip_file.read()

    backup_path = zip_path + ".appendbackup"
    temporary_backup_path = backup_path + ".tmp"
    with open(temporary_backup_path, "wb") as backup_file:
        backup_file.write(tail_backup)
    os.replace(temporary_backup_path, backup_path)

    guard_path = zip_path + ".sizeguard"
    temporary_guard_path = guard_path + ".tmp"
    with open(temporary_guard_path, "w", encoding="utf-8") as guard_file:
        guard_file.write(str(start_dir))
    os.replace(temporary_guard_path, guard_path)

    return guard_path, start_dir


def _append_backup_path(guard_path):
    return guard_path[: -len(".sizeguard")] + ".appendbackup" if guard_path else None


def _release_append_guard(guard_path):
    # Deletes the now-unneeded guard/backup sidecar files after a successful
    # (already independently verified) append, or after a completed
    # rollback. Retried per-file (see _retry_transient_os_error) for the
    # same transient-AV-lock reason as _discard_temp_zip - these are small
    # metadata files, but they were JUST written/read moments earlier in
    # the same operation, the exact window an on-access scanner is most
    # likely to still hold a brief lock on. A persistent failure to delete
    # one (real permissions problem, not just a transient lock) still
    # raises - callers (_confirm_append via add_folder_to_archive_smart's
    # commit-error handling, _rollback_append below) already treat that
    # as "cleanup couldn't finish, but the data itself is fine" rather than
    # forcing a spurious rollback of already-good content.
    if not guard_path:
        return
    for stray_path in (guard_path, _append_backup_path(guard_path)):
        if stray_path and os.path.isfile(stray_path):
            _retry_transient_os_error(os.remove, stray_path)


def _rollback_append(zip_path, guard_path, start_dir):
    # משחזרת את ה-ZIP בדיוק לבתים שהיו לפני ה-append (ראו התיעוד למעלה) -
    # כותבת את הגיבוי חזרה למיקום start_dir ומקצצת כל מה שנכתב אחריו.
    backup_path = _append_backup_path(guard_path)

    def _write_back_tail():
        with open(backup_path, "rb") as backup_file:
            tail_backup = backup_file.read()
        with open(zip_path, "r+b") as zip_file:
            zip_file.seek(start_dir)
            zip_file.write(tail_backup)
            zip_file.truncate(start_dir + len(tail_backup))

    try:
        if backup_path and os.path.isfile(backup_path):
            # Retried (see _retry_transient_os_error): this is the actual
            # content-restoring write, called immediately after this same
            # process just finished reading/hashing zip_path during verify -
            # exactly the window a transient AV lock is most likely to still
            # be held. Genuinely unrecoverable failures (disk error, real
            # permissions problem) still fall through to `except OSError:
            # pass` unchanged below - this only gives a brief sharing
            # violation a real chance to clear before giving up.
            _retry_transient_os_error(_write_back_tail)
    except OSError:
        pass
    _release_append_guard(guard_path)


def _append_new_members(zip_path, write_members, replaced_names=()):
    # write_members(new_zip) מקבלת ZipFile פתוח במצב append וכותבת פריטים
    # חדשים. replaced_names (אופציונלי, לשימוש ע"י checkin - החלפת פריט
    # קיים): שמות פריטים קיימים שיש להסיר מה-Central Directory החדש לפני
    # הכתיבה, כדי שלא יישארו שני רשומות לאותו נתיב (verify_archive מזהה
    # שמות כפולים כ-CHANGED). הבתים הישנים של אותם פריטים נשארים פיזית
    # בקובץ (מתים, לא מאוזכרים) במקום להיכתב-מחדש - זה המחיר (מקום דיסק
    # בלבד, לא תקינות) בתמורה לאי-נגיעה בשאר הארכיון. מחזירה
    # (guard_path, start_dir) בהצלחה, None אם נכשל (והקובץ שוחזר לבתים
    # המדויקים שהיו לפני הקריאה).
    guard_path, start_dir = _record_append_guard(zip_path)
    try:
        with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED, allowZip64=True) as new_zip:
            for name in replaced_names:
                old_info = new_zip.NameToInfo.pop(name, None)
                if old_info is not None:
                    try:
                        new_zip.filelist.remove(old_info)
                    except ValueError:
                        pass
            write_members(new_zip)
    except Exception:  # noqa: BLE001 - any failure here must roll back, never leave a half-written archive
        _rollback_append(zip_path, guard_path, start_dir)
        return None
    return guard_path, start_dir


def _confirm_append(guard_path):
    _release_append_guard(guard_path)


def checkout_item(zip_path, path_in_archive, destination=None, display_name=None):
    # שולפת פריט בודד (קובץ או תיקייה שלמה) מארכיון לתיקיית עבודה.
    #
    # path_in_archive הוא הנתיב הפנימי ב-ZIP (עשוי לכלול קידומת Hash למניעת
    # התנגשות שמות - ראו build_archive_member_name). display_name הוא השם
    # הנקי שהמשתמש מכיר (items.file_name); אם הוא שונה מהנתיב הפנימי, הפריט
    # שנשלף מוחזר לשם הנקי בתיקיית העבודה כדי שהמשתמש יראה את השם המוכר לו.
    if not os.path.isfile(zip_path):
        return None

    if destination is None:
        destination = storage_config.workspace_dir()
    os.makedirs(destination, exist_ok=True)

    normalized = path_in_archive.rstrip("/")

    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            members = [
                name for name in zip_file.namelist()
                if name == path_in_archive or name == normalized or name.startswith(normalized + "/")
            ]
            if not members:
                return None
            for member in members:
                zip_file.extract(member, destination)
    except (zipfile.BadZipFile, OSError):
        return None

    extracted_path = os.path.join(destination, normalized)

    if not display_name or display_name == normalized:
        return extracted_path

    final_path = os.path.join(destination, display_name)
    if os.path.exists(final_path):
        if os.path.isdir(final_path):
            shutil.rmtree(final_path)
        else:
            os.remove(final_path)
    os.replace(extracted_path, final_path)
    return final_path


def build_archive_member_name(basename, content_hash):
    # מבטיחה נתיב פנימי ייחודי וקבוע ב-ZIP גם כששני פריטים שונים חולקים שם
    # בסיס זהה (11.6): הקידומת נגזרת מ-SHA-256 של התוכן עצמו, ולכן דטרמיניסטית
    # - אותו תוכן תמיד מייצר אותה קידומת, בלי תלות בסדר הוספה או במצב הארכיון.
    prefix = (content_hash or "").strip()[:8] or "00000000"
    return f"{prefix}_{basename}"


def remove_empty_category_folder(folder_name):
    # מסירה תיקיית קטגוריה ריקה בלבד לאחר מחיקת קטגוריה מהמסד (8.2).
    # לעולם לא מוחקת רקורסיבית - אם יש בה קבצים, לא נוגעת בה.
    category_path = os.path.join(storage_config.archives_dir(), folder_name)
    if not os.path.isdir(category_path):
        return True
    try:
        os.rmdir(category_path)
    except OSError:
        return False
    return True


def add_file_to_archive(file_path, category_name, zip_path=None):
    if not os.path.isfile(file_path):
        return None, False, None

    if zip_path is None:
        category_path = create_category(category_name)
        zip_path = os.path.join(
            category_path,
            _get_archive_filename(category_name, 1)
        )
    else:
        category_path = os.path.dirname(zip_path)
        os.makedirs(category_path, exist_ok=True)

    file_name = os.path.basename(file_path)
    content_hash = calculate_file_hash(file_path)
    if content_hash is None:
        return None, False, None
    member_name = build_archive_member_name(file_name, content_hash)

    archive_exists = os.path.isfile(zip_path)
    if archive_exists:
        with zipfile.ZipFile(zip_path, "r") as existing_zip:
            if member_name in existing_zip.namelist():
                return zip_path, False, member_name

    if archive_exists:
        # נתיב מהיר: append בלבד - לא נוגע בבתים של אף פריט קיים בארכיון
        # (ראו תיעוד _append_new_members). זה מה שהופך הוספת פריט קטן
        # לארכיון גדול-קיים למהירה, במקום דחיסה-מחדש של כל הארכיון.
        guard = _append_new_members(zip_path, lambda new_zip: new_zip.write(file_path, member_name))
        if guard is None:
            return None, False, None
        guard_path, original_size = guard

        if calculate_zip_file_hash(zip_path, member_name) != content_hash:
            _rollback_append(zip_path, guard_path, original_size)
            return None, False, None

        _confirm_append(guard_path)
        return zip_path, True, member_name

    def mutate(old_zip, new_zip):
        new_zip.write(file_path, member_name)

    temp_path = _build_temp_zip(zip_path, mutate)
    if temp_path is None:
        return None, False, None

    if calculate_zip_file_hash(temp_path, member_name) != content_hash:
        _discard_temp_zip(temp_path)
        return None, False, None

    _commit_temp_zip(temp_path, zip_path)
    return zip_path, True, member_name


def add_folder_to_archive(folder_path, category_name, zip_path=None):
    if not os.path.isdir(folder_path):
        return None, None

    if zip_path is None:
        estimated_size = get_folder_total_size(folder_path) or 0
        zip_path = get_selected_archive(category_name, None, estimated_size)
    else:
        category_path = os.path.dirname(zip_path)
        os.makedirs(category_path, exist_ok=True)

    folder_name = os.path.basename(os.path.normpath(folder_path))
    folder_hash = calculate_folder_hash(folder_path)
    if folder_hash is None:
        return None, None
    member_root = build_archive_member_name(folder_name, folder_hash)

    archive_exists = os.path.isfile(zip_path)
    existing_paths = set()
    if archive_exists:
        with zipfile.ZipFile(zip_path, "r") as existing_zip:
            existing_paths = set(existing_zip.namelist())

    new_members = []
    for current_path, _dir_names, file_names in os.walk(folder_path):
        for file_name in file_names:
            full_file_path = os.path.join(current_path, file_name)
            path_in_folder = os.path.relpath(full_file_path, folder_path)
            path_in_archive = f"{member_root}/{path_in_folder}".replace(os.sep, "/")
            if path_in_archive not in existing_paths:
                new_members.append((full_file_path, path_in_archive))

    if archive_exists:
        # נתיב מהיר: append בלבד לפריטים החדשים של התיקייה - לא נוגע בבתים
        # של אף פריט קיים בארכיון, ולכן לא תלוי בגודל הארכיון הקיים.
        def write_members(new_zip):
            for full_file_path, path_in_archive in new_members:
                new_zip.write(full_file_path, path_in_archive)

        guard = _append_new_members(zip_path, write_members)
        if guard is None:
            return None, None
        guard_path, original_size = guard

        verify_ok = True
        for full_file_path, path_in_archive in new_members:
            if calculate_zip_file_hash(zip_path, path_in_archive) != calculate_file_hash(full_file_path):
                verify_ok = False
                break

        if not verify_ok:
            _rollback_append(zip_path, guard_path, original_size)
            return None, None

        _confirm_append(guard_path)
        return zip_path, f"{member_root}/"

    def mutate(old_zip, new_zip):
        for full_file_path, path_in_archive in new_members:
            new_zip.write(full_file_path, path_in_archive)

    temp_path = _build_temp_zip(zip_path, mutate)
    if temp_path is None:
        return None, None

    verify_ok = True
    for full_file_path, path_in_archive in new_members:
        if calculate_zip_file_hash(temp_path, path_in_archive) != calculate_file_hash(full_file_path):
            verify_ok = False
            break

    if not verify_ok:
        _discard_temp_zip(temp_path)
        return None, None

    _commit_temp_zip(temp_path, zip_path)
    return zip_path, f"{member_root}/"


def checkin_item(zip_path, path_in_archive, source_path):
    # מחזירה פריט (קובץ או תיקייה) שנערך בתיקיית העבודה בחזרה לארכיון.
    #
    # נתיב append: מסירה את רשומות ה-Central Directory של הפריט הישן
    # (replaced_names) ומוסיפה את התוכן החדש - בלי לגעת בבתים של אף פריט
    # אחר בארכיון (ראו תיעוד _append_new_members). הבתים הישנים של הפריט
    # שהוחלף נשארים פיזית בקובץ (מתים, לא מאוזכרים ב-Central Directory
    # החדש) במקום להיכתב-מחדש - המחיר הוא גידול הדרגתי בגודל הקובץ על כל
    # checkin חוזר של אותו פריט, לא בתקינות: קריאה/אימות/verify_archive
    # תמיד רואים רק את ה-Central Directory הנוכחי, שמצביע רק על הבתים
    # החיים. זה בדיוק מה שהופך checkin של פריט קטן בארכיון גדול-קיים למהיר,
    # במקום דחיסה-מחדש של כל שאר הארכיון.
    if not os.path.isfile(zip_path):
        return False
    if not os.path.exists(source_path):
        return False

    normalized = path_in_archive.rstrip("/")
    is_folder = os.path.isdir(source_path)

    with zipfile.ZipFile(zip_path, "r") as existing_zip:
        existing_names = existing_zip.namelist()
    replaced_names = [
        name for name in existing_names
        if name == normalized or name.startswith(normalized + "/")
    ]
    if not replaced_names:
        return False

    if is_folder:
        new_members = []
        for current_path, _dir_names, file_names in os.walk(source_path):
            for file_name in file_names:
                full_path = os.path.join(current_path, file_name)
                relative_path = os.path.relpath(full_path, source_path).replace(os.sep, "/")
                new_members.append((full_path, f"{normalized}/{relative_path}"))

        def write_members(new_zip):
            for full_path, member_path in new_members:
                new_zip.write(full_path, member_path)
    else:
        def write_members(new_zip):
            new_zip.write(source_path, normalized)

    guard = _append_new_members(zip_path, write_members, replaced_names=replaced_names)
    if guard is None:
        return False
    guard_path, start_dir = guard

    if is_folder:
        verify_ok = True
        for full_path, member_path in new_members:
            if calculate_zip_file_hash(zip_path, member_path) != calculate_file_hash(full_path):
                verify_ok = False
                break
    else:
        verify_ok = calculate_zip_file_hash(zip_path, normalized) == calculate_file_hash(source_path)

    if not verify_ok:
        _rollback_append(zip_path, guard_path, start_dir)
        return False

    _confirm_append(guard_path)

    category_name = os.path.basename(os.path.dirname(zip_path))
    manifest_path = create_archive_manifest(zip_path, category_name, compute_archive_hash=False)
    return manifest_path is not None


def create_archive_manifest(zip_path, category_name, compute_archive_hash=True):
    # יוצרת תעודת זהות JSON לצד קובץ ה-ZIP.
    #
    # אותו דפוס caching כמו archive_manager_smart.create_archive_manifest_
    # smart (ראו התיעוד שם): פותחת את ה-ZIP פעם אחת בלבד (לא לכל פריט
    # בנפרד), ומדלגת על חישוב-Hash מחדש לפריט שה-CRC-32 שלו (חינם
    # מ-infolist()) זהה למה שהיה ב-Manifest הקודם - פריט חדש/שהשתנה עדיין
    # נחשב במלואו. compute_archive_hash=False (משמש ע"י checkin_item מיד
    # אחרי כתיבה ב-append, כשעדיין תיבוא קריאה ל-refresh_archive_details
    # שבונה Manifest טרי בכל מקרה) מדלגת גם על חישוב ה-SHA-256 המלא של כל
    # קובץ ה-ZIP - קריאה מלאה נוספת שאין בה צורך כשהמנפיסט הזה עומד להיבנות
    # שוב על ידי הקורא מיד לאחר מכן.
    if not os.path.isfile(zip_path):
        return None

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
                    file_hash = cached["sha256"]
                else:
                    hash_value = hashlib.sha256()
                    with zip_file.open(file_info.filename, "r") as archived_file:
                        while True:
                            chunk = archived_file.read(65536)
                            if not chunk:
                                break
                            hash_value.update(chunk)
                    file_hash = hash_value.hexdigest()

                files.append({
                    "path_in_archive": file_info.filename,
                    "original_size": file_info.file_size,
                    "compressed_size": file_info.compress_size,
                    "sha256": file_hash,
                    "_crc32": file_info.CRC,  # cache key only, see docstring - not part of the documented manifest schema
                })
    except (zipfile.BadZipFile, OSError):
        return None

    manifest_data = {
        "manifest_version": 1,
        "archive_name": os.path.basename(zip_path),
        "category": category_name,
        "created_at": created_at,
        "updated_at": current_time,
        "item_count": len(files),
        "archive_size": os.path.getsize(zip_path),
        "archive_sha256": calculate_archive_hash(zip_path) if compute_archive_hash else None,
        "files": files
    }

    temporary_path = manifest_path + ".tmp"

    # כותבת קודם קובץ זמני ורק אז מחליפה את ה-Manifest הישן.
    with open(temporary_path, "w", encoding="utf-8") as manifest_file:
        json.dump(manifest_data, manifest_file, ensure_ascii=False, indent=4)

    os.replace(temporary_path, manifest_path)

    return manifest_path


def verify_archive(zip_path, expected_archive_hash, manifest_path, precomputed_archive_hash=None):
    # בודקת את מצב הארכיון ומחזירה אחד מארבעת הסטטוסים.
    #
    # שומרת על ערבות ה-"קריאה חוזרת בלתי תלויה" של testzip()/חישוב ה-Hash
    # (זיהוי corruption אמיתי כמו bit-rot בדיסק) - אך מבטלת חישובים כפולים:
    # פותחת את ה-ZIP פעם אחת בלבד לצורך testzip()+infolist(), ומקבלת אופציונלית
    # Hash של הארכיון שכבר חושב באותה נשימה (למשל ע"י create_archive_manifest_smart)
    # במקום לחשב אותו מחדש - חיסכון של קריאת-תוכן מלאה שלמה לארכיון.
    # לכל פריט, אם ה-CRC-32 שלו (שכבר נקרא בחינם מ-infolist(), בלי תוכן) זהה
    # ל-CRC-32 שנשמר ב-Manifest יחד עם ה-sha256 שלו (_crc32, אותו דפוס caching
    # הבטוח שכבר קיים ב-create_archive_manifest_smart), ה-sha256 השמור עדיין
    # תקף ולא נדרש לקרוא מחדש את תוכן הפריט. פריט חדש/שהשתנה (CRC שונה, או
    # בלי רשומת cache) עדיין נקרא ונחשב במלואו - זה לעולם לא מחליש זיהוי
    # corruption עבור פריטים שבאמת השתנו.
    if not os.path.isfile(zip_path):
        return "MISSING"

    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            damaged_file = zip_file.testzip()

            if damaged_file is not None:
                return "CORRUPTED"

            infolist = zip_file.infolist()
    except (zipfile.BadZipFile, OSError):
        return "CORRUPTED"

    if precomputed_archive_hash is not None:
        current_archive_hash = precomputed_archive_hash
    else:
        current_archive_hash = calculate_archive_hash(zip_path)

    # שינוי ב-Hash של ה-ZIP אומר שהארכיון השתנה. expected_archive_hash=None
    # אינו "שינוי" - הוא אומר שעדיין לא נרשם Baseline (למשל מיד אחרי כתיבה
    # מהירה שדילגה על חישוב ה-Hash המלא, ראו create_archive_manifest_smart
    # עם compute_archive_hash=False) - במקרה כזה ממשיכים לבדיקה המלאה
    # שכבר בוצעה למעלה (testzip) ולבדיקת כל פריט למטה, במקום להכריז CHANGED
    # ללא שום עדות אמיתית לשינוי.
    if expected_archive_hash is not None and expected_archive_hash != current_archive_hash:
        return "CHANGED"

    if not manifest_path or not os.path.isfile(manifest_path):
        return "CHANGED"

    try:
        with open(manifest_path, "r", encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
    except (json.JSONDecodeError, OSError):
        return "CHANGED"

    manifest_archive_sha256 = manifest.get("archive_sha256")
    if manifest_archive_sha256 is not None and manifest_archive_sha256 != current_archive_hash:
        return "CHANGED"

    # אם אין עדיין Baseline אמיתי של Hash (מיד אחרי כתיבה מהירה), עדיין יש
    # להשוות לפחות את גודל הקובץ שנרשם ב-Manifest מול הגודל בפועל: testzip()
    # למעלה בודק תקינות מבנית ו-CRC-32 לכל פריט קיים, אבל בתים שנוספו אחרי
    # ה-EOCD (סוף המבנה הפנימי של ה-ZIP) הם "תוספת" חוקית לפי מבנה ה-ZIP -
    # לא פוגעים באף פריט קיים ולכן לא מתגלים לא ע"י testzip() ולא ע"י בדיקת
    # ה-CRC-32 לכל פריט למטה. השוואת גודל היא בדיקה זולה (stat בלבד, בלי
    # קריאת תוכן) שתופסת בדיוק את המקרה הזה כשאין עדיין Hash מלא להשוואה.
    if manifest_archive_sha256 is None:
        manifest_archive_size = manifest.get("archive_size")
        if manifest_archive_size is not None:
            try:
                current_archive_size = os.path.getsize(zip_path)
            except OSError:
                return "CORRUPTED"
            if manifest_archive_size != current_archive_size:
                return "CHANGED"

    manifest_files = manifest.get("files", [])

    zip_file_names = [
        file_info.filename
        for file_info in infolist
        if not file_info.is_dir()
    ]
    crc_by_name = {
        file_info.filename: file_info.CRC
        for file_info in infolist
        if not file_info.is_dir()
    }

    manifest_file_names = [
        file_data.get("path_in_archive")
        for file_data in manifest_files
    ]

    # אותו נתיב פנימי לא אמור להופיע יותר מפעם אחת.
    if len(zip_file_names) != len(set(zip_file_names)):
        return "CHANGED"

    if zip_file_names != manifest_file_names:
        return "CHANGED"

    # משווה כל קובץ לערך שנשמר עבורו ב-Manifest - פותחת את ה-ZIP פעם אחת
    # בלבד לכל הלולאה (במקום לפתוח/לסרוק מחדש את ה-central directory לכל
    # פריט), ומדלגת על קריאת-תוכן חוזרת לפריטים שה-CRC-32 שלהם לא השתנה.
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            for file_data in manifest_files:
                path_in_archive = file_data.get("path_in_archive")
                expected_file_hash = file_data.get("sha256")
                cached_crc = file_data.get("_crc32")

                if (
                    cached_crc is not None
                    and expected_file_hash
                    and crc_by_name.get(path_in_archive) == cached_crc
                ):
                    current_file_hash = expected_file_hash
                else:
                    hash_value = hashlib.sha256()
                    with zip_file.open(path_in_archive, "r") as archived_file:
                        while True:
                            chunk = archived_file.read(65536)
                            if not chunk:
                                break
                            hash_value.update(chunk)
                    current_file_hash = hash_value.hexdigest()

                if current_file_hash != expected_file_hash:
                    return "CHANGED"
    except (zipfile.BadZipFile, KeyError, OSError):
        return "CORRUPTED"

    return "VERIFIED"
