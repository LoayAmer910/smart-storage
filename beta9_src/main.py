import os
import shutil
import sqlite3
import threading
import time
import zipfile

import archive_manager
import archive_manager_smart
import database_manager
import smart_compression
import storage_config

# Cloud reporting lives entirely behind this try/except - StorageArch must
# start and run normally even if local_cloud can't be imported at all (a
# missing optional dependency, a packaging issue, anything). This is the
# ONLY place in the codebase that imports local_cloud; archive_manager.py,
# archive_manager_smart.py, smart_compression.py, and database_manager.py
# never do (plan: "Do not put networking inside" those modules - they don't
# even know this package exists).
try:
    from local_cloud import telemetry_service as _telemetry_service
    from local_cloud import remote_config_service as _remote_config_service
    from local_cloud import installation_service as _installation_service
    from local_cloud import update_service as _update_service
    from local_cloud import background_sender as _background_sender_module
    from local_cloud import schemas as _cloud_schemas
    from local_cloud import retention as _cloud_retention
    _CLOUD_AVAILABLE = True
except Exception:  # noqa: BLE001 - Cloud must never prevent StorageArch from starting
    _CLOUD_AVAILABLE = False

_cloud_sender_instance = None


def _cloud_sender():
    """Lazily creates and starts the single BackgroundSender for this
    process. Returns None if Cloud isn't available at all - every caller
    already treats a None sender as "don't wake anything", so this never
    needs a separate availability check at each call site."""
    global _cloud_sender_instance
    if not _CLOUD_AVAILABLE:
        return None
    if _cloud_sender_instance is None:
        try:
            _cloud_sender_instance = _background_sender_module.BackgroundSender(_installation_service.get_credentials)
            _cloud_sender_instance.start()
        except Exception:  # noqa: BLE001
            _cloud_sender_instance = None
    return _cloud_sender_instance


def shutdown_cloud(timeout=1.0):
    """Bounded shutdown for the Cloud background sender (plan §18.3) -
    call this from the app's own close/shutdown path. Never blocks longer
    than `timeout` seconds regardless of network state."""
    if _cloud_sender_instance is not None:
        try:
            _cloud_sender_instance.shutdown(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass


def _start_cloud_subsystem():
    if not _CLOUD_AVAILABLE:
        return
    try:
        sender = _cloud_sender()
        _telemetry_service.record_app_started(sender=sender)
        # Remote Config fetch happens in the background (plan §20 step 7) -
        # never delays initialize()'s return, and a daemon thread can never
        # block process exit even if it's still mid-request.
        threading.Thread(target=_remote_config_service.fetch_and_verify, daemon=True).start()
        # Age-based queue retention (plan §17): unlike the Remote Config
        # fetch above, this is pure local file I/O (a handful of bounded
        # SQLite DELETEs against files already capped at 1-5MB) - no
        # network call, so it runs synchronously right here rather than on
        # its own thread. That's deliberate: a detached background thread
        # reads STORAGEARCH_CLOUD_HOME (via local_state) lazily whenever it
        # actually gets scheduled, which can race past a test's fixture
        # teardown and silently fall through to the real per-user
        # %LOCALAPPDATA%\StorageArch\ - exactly the isolation bug this
        # project already fixed once (see tests/conftest.py's cloud_home
        # docstring). Reading the env var synchronously, on the same call
        # stack as initialize(), avoids reintroducing that bug. Still
        # best-effort (retention.prune_expired_queues never raises) and
        # still fast enough not to be a real startup delay.
        _cloud_retention.prune_expired_queues()
    except Exception:  # noqa: BLE001
        pass


def _disabled_smart_strategies():
    if not _CLOUD_AVAILABLE:
        return None
    try:
        return _remote_config_service.disabled_strategies_set()
    except Exception:  # noqa: BLE001
        return None


def _report_operation_telemetry(**kwargs):
    if not _CLOUD_AVAILABLE:
        return
    try:
        kwargs.setdefault("sender", _cloud_sender())
        _telemetry_service.record_operation_event(**kwargs)
    except Exception:  # noqa: BLE001 - telemetry must never affect the caller's result
        pass


# --------------------------------------------------------------------------
# C7: Cloud facade for the GUI. Every gui/*.py module already only ever
# calls into main.py (never database_manager/archive_manager directly) -
# these functions keep that same rule for Cloud, so local_cloud is never
# imported anywhere except here. Every one of them is safe to call even
# when Cloud isn't available at all or is fully offline.
# --------------------------------------------------------------------------

def cloud_available():
    return _CLOUD_AVAILABLE


def cloud_is_registered():
    if not _CLOUD_AVAILABLE:
        return False
    try:
        return _installation_service.is_registered()
    except Exception:  # noqa: BLE001
        return False


def cloud_register(beta_code):
    if not _CLOUD_AVAILABLE:
        return False, "cloud_unavailable"
    try:
        return _installation_service.register(beta_code)
    except Exception:  # noqa: BLE001
        return False, "unexpected_error"


def cloud_disconnect():
    if not _CLOUD_AVAILABLE:
        return
    try:
        _installation_service.disconnect()
    except Exception:  # noqa: BLE001
        pass


def cloud_telemetry_enabled():
    if not _CLOUD_AVAILABLE:
        return False
    try:
        return _telemetry_service.is_consent_on()
    except Exception:  # noqa: BLE001
        return False


def cloud_set_telemetry_enabled(enabled):
    if not _CLOUD_AVAILABLE:
        return
    try:
        _telemetry_service.set_consent(enabled, sender=_cloud_sender())
    except Exception:  # noqa: BLE001
        pass


def _build_diagnostic_snapshot():
    try:
        from local_cloud import system_info
        return {"windows_version": system_info.windows_version(), "architecture": system_info.architecture()}
    except Exception:  # noqa: BLE001
        return None


def cloud_submit_feedback(category, rating=None, feedback_text=None, include_diagnostics=False, beta_code=None):
    if not _CLOUD_AVAILABLE:
        return False, "cloud_unavailable"
    try:
        from local_cloud import feedback_service
        diagnostic_snapshot = _build_diagnostic_snapshot() if include_diagnostics else None
        return feedback_service.submit_feedback(
            category,
            rating=rating,
            feedback_text=feedback_text,
            include_diagnostics=include_diagnostics,
            diagnostic_snapshot=diagnostic_snapshot,
            beta_code=beta_code,
            sender=_cloud_sender(),
        )
    except Exception:  # noqa: BLE001
        return False, "unexpected_error"


def cloud_check_for_updates():
    if not _CLOUD_AVAILABLE:
        return None
    try:
        return _update_service.check_latest_release()
    except Exception:  # noqa: BLE001
        return None


def cloud_report_crash(exception, component=None, function_name=None, safe_code_location=None):
    if not _CLOUD_AVAILABLE:
        return
    try:
        from local_cloud import crash_service
        crash_service.report_crash(exception, component=component, function_name=function_name, safe_code_location=safe_code_location, sender=_cloud_sender())
    except Exception:  # noqa: BLE001
        pass


def initialize():
    # 7.1 - הגדרה ראשונית: תיקיות, מסד נתונים, ושחזור אחרי הפעלה שנקטעה.
    # תמיד קוראת קודם את תצורת המיקומים העדכנית מתוך תיקיית העבודה הנוכחית,
    # כך שכל קריאה ל-initialize (כולל אחרי שינוי הגדרות אחסון) פועלת נכון.
    storage_config.load()
    archive_manager.create_storage_folders()
    db_path = database_manager.create_database()
    removed_temp_files = archive_manager.recover_interrupted_operations()
    if removed_temp_files:
        print("Recovered from interrupted operation(s), removed leftover temp files:", removed_temp_files)
    print("SmartArchive initialized. Database:", db_path)
    _start_cloud_subsystem()
    return db_path


def create_category(category_name, description=None, color=None, max_archive_size=2147483648):
    # 7.2 - יצירת קטגוריה. לא נוצר ZIP ריק עד לפריט הראשון.
    if not isinstance(category_name, str) or not category_name.strip():
        print("Invalid category name")
        return False

    archive_manager.create_category(category_name)
    saved = database_manager.save_category(category_name, description, color, max_archive_size)
    if saved:
        print("Category created and saved in database")
    else:
        print("Category already exists in database")
    return database_manager.get_category_by_name(category_name) is not None


def update_category(category_id, name, description=None, color=None, max_archive_size=2147483648):
    # 8.2 - עריכת קטגוריה: רק השדות הלוגיים (שם/תיאור/צבע/גודל מרבי).
    # folder_name הפיזי הקבוע אינו נוגע כאן בכלל - כך שעריכה לעולם לא שוברת
    # נתיבי ארכיון/Manifest קיימים, גם כששם הקטגוריה משתנה.
    updated = database_manager.update_category(category_id, name, description, color, max_archive_size)
    if updated:
        print("Category updated")
    else:
        print("Category could not be updated (not found or duplicate name)")
    return updated


def delete_category(category_id):
    # 8.2 - מחיקת קטגוריה. מתבצעת אך ורק אם היא ריקה לגמרי (0 ארכיונים);
    # לעולם לא מוחקת נתונים מאורכבים בשקט (11.1/11.9).
    category = database_manager.get_category_by_id(category_id)
    if category is None:
        print("Category not found")
        return "NOT_FOUND"

    folder_name = category[6]
    result = database_manager.delete_category(category_id)
    if result == "DELETED":
        archive_manager.remove_empty_category_folder(folder_name)
        print("Category deleted")
    elif result == "NOT_EMPTY":
        print("Category contains archived data and cannot be deleted")
    return result


def refresh_archive_details(archive_id, zip_path, category_name, deep=True):
    # יוצרת Manifest ומעדכנת את פרטי הארכיון במסד.
    # Phase 3: smart-aware manifest builder - adds optional strategy/
    # original_sha256 fields for Smart-stored members; legacy members get
    # byte-identical manifest entries to before (see archive_manager_smart
    # module docstring for the field-semantics design that keeps
    # archive_manager.verify_archive working unmodified for both kinds).
    #
    # deep=True (default, used by check_duplicate_item's on-demand verify):
    # unchanged full behavior - create_archive_manifest_smart computes a
    # true whole-archive SHA-256, reused (not recomputed) by verify_archive
    # (Cause B fix), which still independently re-reads the zip (testzip +
    # per-item hash, with CRC-32 caching for unchanged members) to catch
    # real corruption (e.g. disk bit-rot) that happened since the manifest
    # was last built.
    #
    # deep=False (used by the three post-write call sites - archive_file_
    # result, archive_folder_result, _checkin_impl - right after a normal
    # Archive/Checkin): skips BOTH remaining full-archive passes instead of
    # just avoiding the duplicate one. The whole-archive SHA-256 is a
    # genuinely unavoidable O(archive size) read if computed fresh, and
    # verify_archive's testzip()+per-item pass is a SEPARATE full
    # independent read-back - together they made every Archive/Checkin
    # click on a multi-GB category take minutes, dominated by re-reading
    # existing content the write itself just finished handling, not by the
    # new item. This mode skips computing archive_sha256 (manifest gets
    # None - "not yet independently re-verified", never treated as a
    # mismatch, see verify_archive's None-tolerant checks) and does not
    # call verify_archive at all, marking the status VERIFIED directly:
    # that is not a rubber stamp - the new/changed member was already
    # independently read back and hash-checked before commit (see
    # _verify_write_by_readback and add_folder_to_archive's own pre-commit
    # verify), and every pre-existing member's bytes were read (and CRC-
    # validated by zipfile itself) moments earlier in this same synchronous
    # call, while _build_temp_zip's mutate() copied them into the new file.
    # Full independent re-verification (testzip + fresh whole-archive hash)
    # remains available on demand via the explicit "Verify Archive" action
    # (verify_archive_by_id), which is untouched by this flag.
    manifest_path, zip_hash = archive_manager_smart.create_archive_manifest_smart(
        zip_path, category_name, compute_archive_hash=deep
    )

    if manifest_path is None:
        database_manager.update_archive_status(archive_id, "CORRUPTED")
        return None, "CORRUPTED"

    current_size = os.path.getsize(zip_path)

    with zipfile.ZipFile(zip_path, "r") as zip_file:
        item_count = len([
            file_info
            for file_info in zip_file.infolist()
            if not file_info.is_dir()
        ])

    if deep:
        # בודקת את ה-ZIP מול ה-Hash וה-Manifest החדשים. מעבירה את ה-Hash
        # שכבר חושב כרגע (precomputed_archive_hash) כדי ש-verify_archive לא
        # יחשב שוב מאפס את אותו Hash של הארכיון המלא - עדיין מבצעת קריאה
        # בלתי תלויה משלה (testzip + hash לכל פריט) לזיהוי corruption אמיתי.
        integrity_status = archive_manager.verify_archive(
            zip_path, zip_hash, manifest_path, precomputed_archive_hash=zip_hash
        )
    else:
        integrity_status = "VERIFIED"

    database_manager.update_archive_details(
        archive_id,
        current_size,
        item_count,
        zip_hash,
        manifest_path,
        integrity_status
    )

    return manifest_path, integrity_status


def check_duplicate_item(duplicate_item, original_hash):
    # בודקת שהעותק שכבר שמור באמת נמצא בארכיון תקין.
    archive_id = duplicate_item[5]
    zip_path = duplicate_item[7]
    category_name = duplicate_item[8]
    path_in_archive = duplicate_item[3]
    archive = database_manager.get_archive_by_id(archive_id)

    if archive is None:
        return "MISSING"

    expected_zip_hash = archive[6]
    manifest_path = archive[7]

    if expected_zip_hash is None or manifest_path is None:
        archived_file_hash = archive_manager.calculate_zip_file_hash(zip_path, path_in_archive)

        if archived_file_hash == original_hash:
            manifest_path, integrity_status = refresh_archive_details(archive_id, zip_path, category_name)
        else:
            integrity_status = archive_manager.verify_archive(zip_path, None, None)
            database_manager.update_archive_status(archive_id, integrity_status)
    else:
        integrity_status = archive_manager.verify_archive(zip_path, expected_zip_hash, manifest_path)
        database_manager.update_archive_status(archive_id, integrity_status)

    return integrity_status


def _verify_write_by_readback(zip_path, path_in_archive, expected_hash, is_folder):
    # 7.3.8 - בדיקה עצמאית שהפריט ניתן לקריאה מהארכיון וה-Hash תואם למקור.
    # מתבצעת בתיקיית temp פנימית, לא בתיקיית העבודה, כדי לא להתבלבל עם שליפה אמיתית.
    #
    # פריטים (is_folder=False) עדיין נשלפים דרך archive_manager.checkout_item
    # הגולמי בכוונה: expected_hash שמועבר עבורם הוא archived_hash - ה-Hash של
    # הבתים המאוחסנים בפועל (לפריט Smart, אלו בתי הייצוג הדחוס, לא הקובץ
    # המקורי) - "הצלחנו לקרוא בדיוק את מה שכתבנו" ולא "השחזור זהה למקור"
    # (זה כבר מאומת בתוך decide_and_prepare עצמו לפני שהמועמד אושר בכלל).
    #
    # תיקיות (is_folder=True) שונות: expected_hash שמועבר הוא folder_hash -
    # ה-Hash של תוכן התיקייה המקורי, לא של הבתים המאוחסנים. תיקייה יכולה
    # להכיל קבצים עם אסטרטגיות Smart שונות זו מזו (או ללא Smart בכלל) לכל
    # קובץ בנפרד - שליפה גולמית הייתה מחזירה את בתי הייצוג הדחוס במקום
    # התוכן המקורי לכל קובץ Smart, ולעולם לא הייתה תואמת ל-folder_hash.
    # checkout_folder_smart קורא את התג המוטבע בכל פריט ומשחזר אותו נכון.
    verify_dir = os.path.join(storage_config.temp_dir(), "verify")
    if os.path.isdir(verify_dir):
        shutil.rmtree(verify_dir, ignore_errors=True)

    if is_folder:
        try:
            extracted_path = archive_manager_smart.checkout_folder_smart(zip_path, path_in_archive, verify_dir, None)
        except smart_compression.SmartRestoreError:
            extracted_path = None
    else:
        extracted_path = archive_manager.checkout_item(zip_path, path_in_archive, destination=verify_dir)
    if extracted_path is None:
        shutil.rmtree(verify_dir, ignore_errors=True)
        return False

    if is_folder:
        actual_hash = archive_manager.calculate_folder_hash(extracted_path)
    else:
        actual_hash = archive_manager.calculate_file_hash(extracted_path)

    shutil.rmtree(verify_dir, ignore_errors=True)
    return actual_hash == expected_hash


def _archive_file_impl(file_path, category_name, source_action="keep"):
    # מחזירה תמיד dict מובנה {"success", "reason", "message", ...} - archive_file
    # למטה עוטפת ומחזירה רק את success לשמירת תאימות לאחור עם שלב 1+2.
    if not isinstance(file_path, str) or not file_path.strip():
        print("Invalid file path")
        return {"success": False, "reason": "INVALID_PATH", "message": "Invalid file path"}
    if not isinstance(category_name, str) or not category_name.strip():
        print("Invalid category name")
        return {"success": False, "reason": "INVALID_CATEGORY", "message": "Invalid category name"}
    if source_action not in ("keep", "recycle", "delete"):
        print("Invalid source action")
        return {"success": False, "reason": "INVALID_SOURCE_ACTION", "message": "Invalid source action"}
    if archive_manager.is_protected_path(file_path):
        print("Refusing to archive a protected system path")
        return {"success": False, "reason": "PROTECTED_PATH", "message": "This location is protected and cannot be archived"}
    if not os.path.isfile(file_path):
        print("File does not exist")
        return {"success": False, "reason": "SOURCE_MISSING", "message": "File does not exist"}

    try:
        original_hash = archive_manager.calculate_file_hash(file_path)
        if original_hash is None:
            return {"success": False, "reason": "HASH_FAILED", "message": "Unable to compute file hash"}

        duplicate_item = database_manager.find_item_by_hash(original_hash)
        if duplicate_item is not None:
            integrity_status = check_duplicate_item(duplicate_item, original_hash)
            if integrity_status == "VERIFIED":
                print("Duplicate content found")
                print("Existing item:", duplicate_item[1])
                print("Category:", duplicate_item[8])
                print("Archive:", duplicate_item[7])
                print("Path in archive:", duplicate_item[3])
                print("Archive status:", integrity_status)
                print("The file was not saved again")
                return {
                    "success": True,
                    "reason": "DUPLICATE",
                    "message": "Duplicate content already exists - not saved again",
                    "duplicate": {
                        "item_name": duplicate_item[1],
                        "category": duplicate_item[8],
                        "archive": duplicate_item[7],
                        "path_in_archive": duplicate_item[3],
                        "status": integrity_status,
                    },
                }
            print("A matching database record was found")
            print("Archive:", duplicate_item[7])
            print("Archive status:", integrity_status)
            print("Automatic archiving stopped for safety")
            return {
                "success": False,
                "reason": "DUPLICATE_UNTRUSTED",
                "message": f"A matching record exists but its archive could not be verified "
                           f"(status: {integrity_status}); archiving stopped for safety",
            }

        category = database_manager.get_category_by_name(category_name)
        if category is None:
            database_manager.save_category(category_name)
            category = database_manager.get_category_by_name(category_name)
            print("Category created and saved in database")
        category_id = category[0]
        max_archive_size = category[4]
        folder_name_key = category[6]  # שם התיקייה הפיזית הקבוע - לא משתנה עם עריכת שם הקטגוריה

        estimated_size = os.path.getsize(file_path)
        zip_path = archive_manager.get_selected_archive(folder_name_key, max_archive_size, estimated_size)

        if not archive_manager.has_enough_free_space(zip_path, estimated_size):
            print("Not enough free space to archive file")
            return {"success": False, "reason": "INSUFFICIENT_SPACE", "message": "Not enough free space to archive file"}

        # Phase 3: analyzes the file, runs the Smart Selector, and stores
        # whichever representation wins (smart candidate or legacy
        # DEFLATE) - falls back to the unmodified legacy path automatically
        # whenever Smart Compression fails, times out, or isn't worthwhile.
        zip_path, file_added, member_name, strategy_used = archive_manager_smart.add_file_to_archive_smart(
            file_path, folder_name_key, zip_path, disabled_strategies=_disabled_smart_strategies()
        )
        if zip_path is None:
            print("File could not be archived")
            return {"success": False, "reason": "WRITE_FAILED", "message": "File could not be archived"}

        file_name = os.path.basename(file_path)
        archived_hash = archive_manager.calculate_zip_file_hash(zip_path, member_name)
        if archived_hash is None:
            print("Unable to read file from ZIP after archiving")
            return {"success": False, "reason": "VERIFICATION_FAILED", "message": "Unable to read file from ZIP after archiving"}

        if not file_added:
            # member already existed (dedup by original-content-hash-derived
            # name). archived_hash is the STORED bytes hash, which only
            # equals original_hash for legacy entries - for a pre-existing
            # Smart entry it never will, so compare against whatever that
            # existing entry's true original hash actually is instead of
            # assuming it equals archived_hash.
            with zipfile.ZipFile(zip_path, "r") as existing_zip:
                existing_comment = existing_zip.getinfo(member_name).comment
            parsed_existing = archive_manager_smart._parse_smart_comment(existing_comment)
            existing_original_hash = parsed_existing[1] if parsed_existing else archived_hash
            if existing_original_hash != original_hash:
                print("A different file with the same name already exists in ZIP")
                return {"success": False, "reason": "NAME_COLLISION", "message": "A different file with the same name already exists in this archive"}

        archive_saved = database_manager.save_archive(category_id, zip_path)
        archive_id = database_manager.get_archive_id(category_id, zip_path)
        if archive_saved:
            print("Archive saved in database")
        else:
            print("Archive already exists in database")

        original_size = os.path.getsize(file_path)
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            compressed_size = zip_file.getinfo(member_name).compress_size

        # For legacy storage, stored bytes ARE the original bytes, so
        # archived_hash must equal original_hash - that invariant is
        # unchanged. For Smart storage, archived_hash is the STORED
        # REPRESENTATION's hash by design (e.g. the .jxl bytes), which
        # never equals original_hash - exact restoration to the original
        # was already verified inside decide_and_prepare before this
        # candidate was ever accepted, so this check only applies to the
        # legacy path.
        if strategy_used is None and original_hash != archived_hash:
            print("Verification failed")
            database_manager.update_archive_status(archive_id, "CHANGED")
            return {"success": False, "reason": "VERIFICATION_FAILED", "message": "Verification failed after writing to the archive"}

        item_saved = database_manager.save_item(
            archive_id,
            "file",
            file_name,
            file_path,
            member_name,
            original_size,
            compressed_size,
            original_hash,
            archived_hash,
            "ARCHIVED",
            strategy=strategy_used
        )

        if item_saved:
            print("Item saved in database")
        else:
            print("Item already exists in database")

        manifest_path, integrity_status = refresh_archive_details(archive_id, zip_path, category_name, deep=False)
        print("Manifest created:", manifest_path)
        print("Archive status:", integrity_status)

        if not _verify_write_by_readback(zip_path, member_name, archived_hash, is_folder=False):
            print("Post-write verification failed")
            return {"success": False, "reason": "VERIFICATION_FAILED", "message": "Post-write verification failed"}
        print("Restored file verified successfully")

        savings = original_size - compressed_size
        print(f"Original size: {original_size} bytes, compressed: {compressed_size} bytes, saved: {savings} bytes")
        if strategy_used:
            print("Smart Compression used:", strategy_used)

        if source_action != "keep":
            if archive_manager.remove_source(file_path, source_action):
                print("Source handled:", source_action)
            else:
                print("Warning: could not apply source action:", source_action)

        return {
            "success": True,
            "reason": "OK",
            "message": "Archived and verified successfully",
            "original_size": original_size,
            "compressed_size": compressed_size,
            "savings": savings,
            "smart_compression_used": strategy_used is not None,
            "strategy": strategy_used or "baseline_storagearch_zip",
        }
    except sqlite3.Error as exc:
        print("Database error during archiving:", exc)
        return {"success": False, "reason": "DATABASE_ERROR", "message": "A database error occurred while archiving"}
    except PermissionError as exc:
        print("Permission error during archiving:", exc)
        return {"success": False, "reason": "PERMISSION_ERROR", "message": "Permission denied while archiving"}
    except archive_manager.InvalidPathError as exc:
        print("Invalid path during archiving:", exc)
        return {"success": False, "reason": "INVALID_PATH", "message": str(exc)}
    except OSError as exc:
        print("OS error during archiving:", exc)
        return {"success": False, "reason": "OS_ERROR", "message": "A filesystem error occurred while archiving"}
    except Exception as exc:  # noqa: BLE001 - last-resort guard so callers never see a raw traceback
        print("Unexpected error during archiving:", exc)
        return {"success": False, "reason": "UNEXPECTED_ERROR", "message": "An unexpected error occurred while archiving"}


def archive_file(file_path, category_name, source_action="keep"):
    return archive_file_result(file_path, category_name, source_action)["success"]


def _report_archive_file_telemetry(file_path, result, duration_ms):
    success = bool(result.get("success"))
    original_size = result.get("original_size")
    compressed_size = result.get("compressed_size")
    savings = result.get("savings")
    smart_metrics = None
    if success and original_size is not None:
        strategy = result.get("strategy") or "baseline_storagearch_zip"
        file_type = (os.path.splitext(file_path)[1].lstrip(".").lower() or "unknown") if isinstance(file_path, str) else "unknown"
        smart_metrics = [_cloud_schemas.build_smart_aggregate_row(
            file_type=file_type,
            strategy=strategy,
            file_count=1,
            original_bytes=original_size,
            stored_bytes=compressed_size or 0,
            saved_bytes=savings or 0,
            avg_saving_percent=(savings / original_size * 100) if original_size else None,
            avg_processing_ms=duration_ms,
            min_processing_ms=duration_ms,
            max_processing_ms=duration_ms,
        )] if _CLOUD_AVAILABLE else None
    _report_operation_telemetry(
        event_type="archive_completed" if success else "archive_failed",
        operation_type="archive",
        result="success" if success else "failure",
        duration_ms=duration_ms,
        error_category=None if success else result.get("reason"),
        file_count=1,
        original_bytes_total=original_size,
        stored_bytes_total=compressed_size,
        saved_bytes_total=savings,
        saving_percent=(savings / original_size * 100) if success and original_size else None,
        smart_metrics=smart_metrics,
    )


def archive_file_result(file_path, category_name, source_action="keep"):
    # C6: local operation runs to its final result FIRST; telemetry is
    # constructed and queued only after that result is fully committed, and
    # never changes what's returned here (plan: "Telemetry must NEVER
    # change the local operation result").
    start = time.perf_counter()
    result = _archive_file_impl(file_path, category_name, source_action)
    duration_ms = int((time.perf_counter() - start) * 1000)
    _report_archive_file_telemetry(file_path, result, duration_ms)
    return result


def _archive_folder_impl(folder_path, category_name, source_action="keep"):
    if not isinstance(folder_path, str) or not folder_path.strip():
        print("Invalid folder path")
        return {"success": False, "reason": "INVALID_PATH", "message": "Invalid folder path"}
    if not isinstance(category_name, str) or not category_name.strip():
        print("Invalid category name")
        return {"success": False, "reason": "INVALID_CATEGORY", "message": "Invalid category name"}
    if source_action not in ("keep", "recycle", "delete"):
        print("Invalid source action")
        return {"success": False, "reason": "INVALID_SOURCE_ACTION", "message": "Invalid source action"}
    if archive_manager.is_protected_path(folder_path):
        print("Refusing to archive a protected system path")
        return {"success": False, "reason": "PROTECTED_PATH", "message": "This location is protected and cannot be archived"}
    if not os.path.isdir(folder_path):
        print("Folder does not exist")
        return {"success": False, "reason": "SOURCE_MISSING", "message": "Folder does not exist"}

    try:
        folder_hash = archive_manager.calculate_folder_hash(folder_path)
        if folder_hash is None:
            return {"success": False, "reason": "HASH_FAILED", "message": "Unable to compute folder hash"}

        duplicate_item = database_manager.find_item_by_hash(folder_hash)
        if duplicate_item is not None:
            integrity_status = check_duplicate_item(duplicate_item, folder_hash)
            if integrity_status == "VERIFIED":
                print("Duplicate folder content found")
                print("Existing item:", duplicate_item[1])
                print("Category:", duplicate_item[8])
                print("Archive:", duplicate_item[7])
                print("Path in archive:", duplicate_item[3])
                print("Archive status:", integrity_status)
                return {
                    "success": True,
                    "reason": "DUPLICATE",
                    "message": "Duplicate content already exists - not saved again",
                    "duplicate": {
                        "item_name": duplicate_item[1],
                        "category": duplicate_item[8],
                        "archive": duplicate_item[7],
                        "path_in_archive": duplicate_item[3],
                        "status": integrity_status,
                    },
                }
            print("A matching database record was found")
            print("Archive:", duplicate_item[7])
            print("Archive status:", integrity_status)
            print("Automatic archiving stopped for safety")
            return {
                "success": False,
                "reason": "DUPLICATE_UNTRUSTED",
                "message": f"A matching record exists but its archive could not be verified "
                           f"(status: {integrity_status}); archiving stopped for safety",
            }

        category = database_manager.get_category_by_name(category_name)
        if category is None:
            database_manager.save_category(category_name)
            category = database_manager.get_category_by_name(category_name)
            print("Category created and saved in database")
        category_id = category[0]
        max_archive_size = category[4]
        folder_name_key = category[6]

        original_size = archive_manager.get_folder_total_size(folder_path) or 0
        zip_path = archive_manager.get_selected_archive(folder_name_key, max_archive_size, original_size)

        if not archive_manager.has_enough_free_space(zip_path, original_size):
            print("Not enough free space to archive folder")
            return {"success": False, "reason": "INSUFFICIENT_SPACE", "message": "Not enough free space to archive folder"}

        skipped_cloud_files = []
        zip_path, path_in_archive, folder_file_results = archive_manager_smart.add_folder_to_archive_smart(
            folder_path, folder_name_key, zip_path, disabled_strategies=_disabled_smart_strategies(),
            skipped_cloud_files=skipped_cloud_files,
        )
        if zip_path is None:
            print("Folder could not be added to ZIP")
            return {"success": False, "reason": "WRITE_FAILED", "message": "Folder could not be archived"}
        if skipped_cloud_files:
            print(f"Skipped {len(skipped_cloud_files)} OneDrive/cloud file(s) not downloaded to this device:")
            for skipped_path in skipped_cloud_files:
                print(" -", skipped_path)

        archive_saved = database_manager.save_archive(category_id, zip_path)
        archive_id = database_manager.get_archive_id(category_id, zip_path)
        if archive_saved:
            print("Archive saved in database")
        else:
            print("Archive already exists in database")

        root_folder_name = os.path.basename(os.path.normpath(folder_path))

        compressed_size = 0
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            for file_info in zip_file.infolist():
                if file_info.filename.startswith(path_in_archive) and not file_info.is_dir():
                    compressed_size += file_info.compress_size

        item_saved = database_manager.save_item(
            archive_id,
            "folder",
            root_folder_name,
            folder_path,
            path_in_archive,
            original_size,
            compressed_size,
            folder_hash,
            folder_hash,
            "ARCHIVED"
        )

        if item_saved:
            print("Folder item saved in database")
        else:
            print("Folder item already exists in database")

        manifest_path, integrity_status = refresh_archive_details(archive_id, zip_path, category_name, deep=False)
        print("Manifest created:", manifest_path)
        print("Archive status:", integrity_status)

        if not _verify_write_by_readback(zip_path, path_in_archive, folder_hash, is_folder=True):
            print("Post-write verification failed")
            return {"success": False, "reason": "VERIFICATION_FAILED", "message": "Post-write verification failed"}
        print("Folder verified successfully")

        savings = original_size - compressed_size
        print(f"Original size: {original_size} bytes, compressed: {compressed_size} bytes, saved: {savings} bytes")

        if source_action != "keep":
            if archive_manager.remove_source(folder_path, source_action):
                print("Source handled:", source_action)
            else:
                print("Warning: could not apply source action:", source_action)

        message = "Archived and verified successfully"
        if skipped_cloud_files:
            message += (
                f"\n{len(skipped_cloud_files)} file(s) were skipped because they are OneDrive/cloud files "
                "not yet downloaded to this device."
            )
        return {
            "success": True,
            "reason": "OK",
            "message": message,
            "original_size": original_size,
            "compressed_size": compressed_size,
            "savings": savings,
            "file_results": folder_file_results,
            "skipped_cloud_files": skipped_cloud_files,
        }
    except sqlite3.Error as exc:
        print("Database error during archiving:", exc)
        return {"success": False, "reason": "DATABASE_ERROR", "message": "A database error occurred while archiving"}
    except PermissionError as exc:
        print("Permission error during archiving:", exc)
        return {"success": False, "reason": "PERMISSION_ERROR", "message": "Permission denied while archiving"}
    except archive_manager.InvalidPathError as exc:
        print("Invalid path during archiving:", exc)
        return {"success": False, "reason": "INVALID_PATH", "message": str(exc)}
    except OSError as exc:
        print("OS error during archiving:", exc)
        return {"success": False, "reason": "OS_ERROR", "message": "A filesystem error occurred while archiving"}
    except Exception as exc:  # noqa: BLE001
        print("Unexpected error during archiving:", exc)
        return {"success": False, "reason": "UNEXPECTED_ERROR", "message": "An unexpected error occurred while archiving"}


def archive_folder(folder_path, category_name, source_action="keep"):
    return archive_folder_result(folder_path, category_name, source_action)["success"]


def _report_archive_folder_telemetry(result, duration_ms):
    success = bool(result.get("success"))
    original_size = result.get("original_size")
    compressed_size = result.get("compressed_size")
    savings = result.get("savings")

    # Every file inside the folder now goes through per-file Smart
    # Compression (archive_manager_smart.add_folder_to_archive_smart) -
    # aggregate those individual results into one smart_metrics row per
    # (file_type, strategy) combination actually used, the same shape
    # _report_archive_file_telemetry builds for a standalone file, just
    # grouped across every file in the folder instead of assuming exactly
    # one. A file that fell back to baseline storage is reported under
    # strategy "baseline_storagearch_zip", same normalization the file/
    # checkin telemetry paths already use.
    smart_metrics = None
    if success and _CLOUD_AVAILABLE:
        file_results = result.get("file_results") or []
        groups = {}
        for entry in file_results:
            key = (entry["file_type"], entry.get("strategy") or "baseline_storagearch_zip")
            groups.setdefault(key, []).append(entry)
        if groups:
            smart_metrics = []
            for (file_type, strategy), entries in groups.items():
                group_original = sum(e["original_size"] for e in entries)
                group_stored = sum(e["stored_size"] for e in entries)
                group_saved = max(group_original - group_stored, 0)
                saving_percents = [
                    (e["original_size"] - e["stored_size"]) / e["original_size"] * 100
                    for e in entries if e["original_size"]
                ]
                processing_times = [e["processing_ms"] for e in entries]
                smart_metrics.append(_cloud_schemas.build_smart_aggregate_row(
                    file_type=file_type,
                    strategy=strategy,
                    file_count=len(entries),
                    original_bytes=group_original,
                    stored_bytes=group_stored,
                    saved_bytes=group_saved,
                    avg_saving_percent=(sum(saving_percents) / len(saving_percents)) if saving_percents else None,
                    min_saving_percent=min(saving_percents) if saving_percents else None,
                    max_saving_percent=max(saving_percents) if saving_percents else None,
                    avg_processing_ms=(sum(processing_times) / len(processing_times)) if processing_times else None,
                    min_processing_ms=min(processing_times) if processing_times else None,
                    max_processing_ms=max(processing_times) if processing_times else None,
                ))

    _report_operation_telemetry(
        event_type="archive_completed" if success else "archive_failed",
        operation_type="archive",
        result="success" if success else "failure",
        duration_ms=duration_ms,
        error_category=None if success else result.get("reason"),
        original_bytes_total=original_size,
        stored_bytes_total=compressed_size,
        saved_bytes_total=savings,
        saving_percent=(savings / original_size * 100) if success and original_size else None,
        smart_metrics=smart_metrics,
    )


def archive_folder_result(folder_path, category_name, source_action="keep"):
    start = time.perf_counter()
    result = _archive_folder_impl(folder_path, category_name, source_action)
    duration_ms = int((time.perf_counter() - start) * 1000)
    _report_archive_folder_telemetry(result, duration_ms)
    return result


def search(search_text, category_name=None, extension=None):
    # 7.4 - חיפוש דרך מסד הנתונים, בלי לפתוח ZIP-ים.
    return database_manager.search_items(search_text, category_name, extension)


def list_categories():
    # 8.2 - רשימת כל הקטגוריות למסך הקטגוריות/דשבורד.
    return database_manager.list_categories()


def list_archives():
    # רשימת כל הארכיונים (לכל הקטגוריות) עם סטטוס התקינות שלהם.
    return database_manager.list_all_archives()


def get_category(category_id):
    return database_manager.get_category_by_id(category_id)


def get_item(item_id):
    return database_manager.get_item_by_id(item_id)


def get_archive(archive_id):
    return database_manager.get_archive_by_id(archive_id)


def find_duplicate(content_hash):
    # בדיקת כפילות בלבד, ללא כתיבה - לשימוש ה-GUI לפני הצגת דיאלוג הארכוב.
    # משתמשת באותה לוגיקת אימות בדיוק כמו archive_file/archive_folder.
    duplicate_item = database_manager.find_item_by_hash(content_hash)
    if duplicate_item is None:
        return None

    integrity_status = check_duplicate_item(duplicate_item, content_hash)
    return {
        "item_name": duplicate_item[1],
        "category": duplicate_item[8],
        "archive": duplicate_item[7],
        "path_in_archive": duplicate_item[3],
        "status": integrity_status,
    }


def dashboard_summary():
    # 8.1 - נתוני הלוח הראשי, נגזרים ישירות ממסד הנתונים בזמן קריאה.
    return database_manager.get_dashboard_summary()


def get_storage_locations():
    # 8.5 - מיקומי אחסון/עבודה נוכחיים.
    return storage_config.load()


def is_protected_path(path):
    return archive_manager.is_protected_path(path)


def set_storage_location(data_root=None, workspace_root=None):
    # 8.5 - שינוי מיקום אחסון/עבודה. מסרבת לנתיבי Windows מוגנים, ומסרבת
    # לשנות את תיקיית העבודה כשיש פריטים שעדיין CHECKED_OUT (כדי לא לאבד
    # גישה לעותקים פתוחים). לעולם לא מעבירה קבצים קיימים אוטומטית - שינוי
    # מיקום לא מוחק ולא מזיז נתונים קיימים בשום מיקום.
    if workspace_root is not None:
        if archive_manager.is_protected_path(workspace_root):
            print("Refusing protected workspace path")
            return False
        if database_manager.get_checked_out_items():
            print("Cannot change workspace location while items are checked out")
            return False

    if data_root is not None and archive_manager.is_protected_path(data_root):
        print("Refusing protected data location")
        return False

    storage_config.save(data_root=data_root, workspace_root=workspace_root)
    initialize()
    return True


def reset_workspace_location():
    # 8.5 - איפוס תיקיית העבודה לברירת המחדל (בתוך מיקום הנתונים הנוכחי).
    if database_manager.get_checked_out_items():
        print("Cannot reset workspace location while items are checked out")
        return False
    storage_config.save(workspace_root="")
    initialize()
    return True


def _checkout_impl(item_id, destination=None):
    # 7.4 - שליפת פריט בודד לתיקיית העבודה.
    # Returns (extracted_path_or_None, failure_reason) - failure_reason is
    # None on any success (including the idempotent already-checked-out
    # case), a short safe code otherwise. Only main.py's own checkout()
    # wrapper (below) reads the second value, for telemetry only - it is
    # not part of this function's original public contract.
    item = database_manager.get_item_by_id(item_id)
    if item is None:
        print("Item not found")
        return None, "ITEM_NOT_FOUND"

    if item[10] == "CHECKED_OUT":
        print("Item already checked out at:", item[11])
        return item[11], None

    archive_path = database_manager.get_archive_path_by_item_id(item_id)
    if not archive_path:
        print("Archive could not be found")
        return None, "ARCHIVE_NOT_FOUND"

    dest = destination or storage_config.workspace_dir()
    if not archive_manager.has_enough_free_space(dest, item[6]):
        print("Not enough free space to checkout item")
        return None, "INSUFFICIENT_SPACE"

    # Phase 3: item[15] is the strategy recorded at archive/checkin time
    # (NULL/None for legacy items and pre-Phase-3 archives - falls through
    # to the exact original archive_manager.checkout_item call unchanged).
    # A folder item (item[2]) has no single strategy of its own - each file
    # inside it may carry a DIFFERENT strategy (or none), so item[15] is
    # never populated for folders. checkout_folder_smart reads every
    # member's own embedded tag instead of relying on one external value.
    strategy = item[15]
    try:
        if item[2] == "folder":
            extracted_path = archive_manager_smart.checkout_folder_smart(
                archive_path, item[5], dest, item[3]
            )
        else:
            extracted_path = archive_manager_smart.checkout_item_smart(
                archive_path, item[5], dest, item[3], strategy, item[8]
            )
    except smart_compression.SmartRestoreError as exc:
        print("Smart Compression restore integrity error:", exc)
        return None, "SMART_RESTORE_ERROR"
    if extracted_path is None:
        print("File could not be extracted")
        return None, "EXTRACTION_FAILED"

    database_manager.update_item_status(item_id, "CHECKED_OUT", workspace_path=extracted_path)
    print("Item checked out to:", extracted_path)
    return extracted_path, None


def checkout(item_id, destination=None):
    start = time.perf_counter()
    extracted_path, failure_reason = _checkout_impl(item_id, destination)
    duration_ms = int((time.perf_counter() - start) * 1000)
    success = extracted_path is not None
    _report_operation_telemetry(
        event_type="checkout_completed" if success else "checkout_failed",
        operation_type="checkout",
        result="success" if success else "failure",
        duration_ms=duration_ms,
        error_category=failure_reason,
        file_count=1,
    )
    return extracted_path


def _checkin_impl(item_id, remove_workspace_copy=True):
    # 7.5 - החזרת פריט, עם או בלי שינוי.
    # Returns (success_bool, failure_reason, telemetry_stats). telemetry_stats
    # is None or {"original_bytes": int, "stored_bytes": int, "strategy": str|None}
    # - only checkin()'s wrapper below reads this, for telemetry only.
    item = database_manager.get_item_by_id(item_id)
    if item is None:
        print("Item not found")
        return False, "ITEM_NOT_FOUND", None

    if item[10] != "CHECKED_OUT":
        print("Item is not checked out")
        return False, "NOT_CHECKED_OUT", None

    workspace_path = item[11]
    if not workspace_path or not os.path.exists(workspace_path):
        print("Checked out copy not found in workspace")
        return False, "WORKSPACE_COPY_MISSING", None

    file_type = item[2]
    if file_type == "folder":
        current_hash = archive_manager.calculate_folder_hash(workspace_path)
    else:
        current_hash = archive_manager.calculate_file_hash(workspace_path)

    if current_hash is None:
        print("Unable to compute hash for workspace copy")
        return False, "HASH_FAILED", None

    archive_path = database_manager.get_archive_path_by_item_id(item_id)
    if not archive_path:
        print("Archive could not be found")
        return False, "ARCHIVE_NOT_FOUND", None

    if current_hash == item[8]:
        if remove_workspace_copy and not archive_manager.remove_source(workspace_path, "delete"):
            print("Could not remove workspace copy")
            return False, "SOURCE_REMOVAL_FAILED", None
        database_manager.update_item_status(item_id, "ARCHIVED", workspace_path=None)
        print("Item unchanged - returned to archive without rebuild")
        return True, None, None

    if file_type == "folder":
        needed_bytes = archive_manager.get_folder_total_size(workspace_path) or 0
    else:
        needed_bytes = os.path.getsize(workspace_path)

    if not archive_manager.has_enough_free_space(archive_path, needed_bytes):
        print("Not enough free space to check in item")
        return False, "INSUFFICIENT_SPACE", None

    # Phase 3: the modified file is run through the Smart Selector again
    # from scratch - never assumes the previous strategy still applies,
    # since the best choice can change after the content changes. Folders
    # are unaffected (Smart Compression only targets single files) and use
    # the exact unmodified legacy checkin path internally.
    success, strategy_used = archive_manager_smart.checkin_item_smart(
        archive_path, item[5], workspace_path, disabled_strategies=_disabled_smart_strategies()
    )
    if not success:
        database_manager.update_item_status(item_id, "ERROR", workspace_path=workspace_path)
        print("Check-in failed")
        return False, "WRITE_FAILED", None

    path_in_archive = item[5].rstrip("/")
    if file_type == "folder":
        new_size = needed_bytes
        new_archive_hash = current_hash
        with zipfile.ZipFile(archive_path, "r") as zip_file:
            prefix = path_in_archive + "/"
            new_compressed_size = sum(
                file_info.compress_size
                for file_info in zip_file.infolist()
                if file_info.filename.startswith(prefix) and not file_info.is_dir()
            )
    else:
        new_size = os.path.getsize(workspace_path)
        new_archive_hash = archive_manager.calculate_zip_file_hash(archive_path, path_in_archive)
        with zipfile.ZipFile(archive_path, "r") as zip_file:
            new_compressed_size = zip_file.getinfo(path_in_archive).compress_size

    database_manager.update_item_after_edit(
        item_id,
        new_size,
        new_compressed_size,
        current_hash,
        archive_hash=new_archive_hash,
        status="ARCHIVED",
        workspace_path=None,
        strategy=strategy_used
    )
    if strategy_used:
        print("Smart Compression used on check-in:", strategy_used)

    archive_id = item[1]
    category_name = os.path.basename(os.path.dirname(archive_path))
    manifest_path, integrity_status = refresh_archive_details(archive_id, archive_path, category_name, deep=False)
    print("Manifest updated:", manifest_path)
    print("Archive status:", integrity_status)

    if remove_workspace_copy and not archive_manager.remove_source(workspace_path, "delete"):
        print("Could not remove workspace copy after check-in")

    print("Item checked in with changes")
    is_folder_item = file_type == "folder"
    telemetry_stats = {
        "original_bytes": new_size,
        "stored_bytes": new_compressed_size,
        "strategy": strategy_used if not is_folder_item else None,
        "is_folder": is_folder_item,
        # Real per-file extension for Smart Metrics' file_type+strategy
        # aggregation - distinct from the "file"/"folder" item-kind
        # `file_type` local variable above, which this deliberately
        # shadows only inside this dict, not the surrounding scope.
        "file_type": (os.path.splitext(workspace_path)[1].lstrip(".").lower() or "unknown") if not is_folder_item else None,
    }
    return True, None, telemetry_stats


def checkin(item_id, remove_workspace_copy=True):
    start = time.perf_counter()
    success, failure_reason, telemetry_stats = _checkin_impl(item_id, remove_workspace_copy)
    duration_ms = int((time.perf_counter() - start) * 1000)

    smart_metrics = None
    original_bytes = stored_bytes = None
    if telemetry_stats:
        original_bytes = telemetry_stats.get("original_bytes")
        stored_bytes = telemetry_stats.get("stored_bytes")
        # Matches archive_file_result's own normalization
        # (strategy_used or "baseline_storagearch_zip"): checkin_item_smart
        # returns None strategy for the ordinary/legacy-storage case, not
        # only when Smart Compression is unavailable - gating on a truthy
        # `strategy` here (the previous behavior) silently dropped
        # smart_metrics for every baseline checkin, which is most of them,
        # even though real original/stored/saved byte data was available.
        if _CLOUD_AVAILABLE and not telemetry_stats.get("is_folder", False) and original_bytes is not None:
            strategy = telemetry_stats.get("strategy") or "baseline_storagearch_zip"
            saved_bytes = max(original_bytes - (stored_bytes or 0), 0)
            smart_metrics = [_cloud_schemas.build_smart_aggregate_row(
                file_type=telemetry_stats.get("file_type") or "unknown",
                strategy=strategy,
                file_count=1,
                original_bytes=original_bytes,
                stored_bytes=stored_bytes or 0,
                saved_bytes=saved_bytes,
                avg_saving_percent=(saved_bytes / original_bytes * 100) if original_bytes else None,
                avg_processing_ms=duration_ms,
                min_processing_ms=duration_ms,
                max_processing_ms=duration_ms,
            )]

    _report_operation_telemetry(
        event_type="checkin_completed" if success else "checkin_failed",
        operation_type="checkin",
        result="success" if success else "failure",
        duration_ms=duration_ms,
        error_category=failure_reason,
        file_count=1,
        original_bytes_total=original_bytes,
        stored_bytes_total=stored_bytes,
        smart_metrics=smart_metrics,
    )
    return success


def items_pending_return():
    # 7.5 - פריטים שנשלפו וטרם הוחזרו.
    return database_manager.get_checked_out_items()


def handle_pending_checkouts_on_exit():
    # תחליף CLI להצעת החזרה בעת סגירת התוכנה (11.10: לא כופה, רק מציג ומציע).
    pending = items_pending_return()
    if pending:
        print("Items still checked out:")
        for item in pending:
            print(" -", item[1], "at", item[4])
        print("Call checkin(item_id) for the ones you are done editing before exiting.")
    return pending


def verify_archive_by_id(archive_id):
    # 7.6 - בדיקת תקינות יזומה לארכיון בודד.
    start = time.perf_counter()
    archive = database_manager.get_archive_by_id(archive_id)
    if archive is None:
        _report_operation_telemetry(
            event_type="verify_failed", operation_type="verify", result="failure",
            duration_ms=int((time.perf_counter() - start) * 1000), error_category="MISSING",
        )
        return "MISSING"

    zip_path = archive[3]
    zip_hash = archive[6]
    manifest_path = archive[7]
    status = archive_manager.verify_archive(zip_path, zip_hash, manifest_path)
    database_manager.update_archive_status(archive_id, status)
    duration_ms = int((time.perf_counter() - start) * 1000)
    success = status == "VERIFIED"
    _report_operation_telemetry(
        event_type="verify_completed" if success else "verify_failed",
        operation_type="verify",
        result="success" if success else "failure",
        duration_ms=duration_ms,
        error_category=None if success else status,
    )
    return status


def verify_category(category_name):
    # 7.6 - בדיקת תקינות לכל הארכיונים בקטגוריה.
    category = database_manager.get_category_by_name(category_name)
    if category is None:
        return {}

    archives = database_manager.get_archives_by_category_id(category[0])
    results = {}
    for archive in archives:
        results[archive[2]] = verify_archive_by_id(archive[0])
    return results


def verify_all():
    # 7.6 - בדיקת תקינות לכל הארכיונים בכל הקטגוריות.
    results = {}
    for category in database_manager.list_categories():
        results[category[1]] = verify_category(category[1])
    return results


def main():
    print("SmartArchive started")
    initialize()

    category_name = "Documents"
    create_category(category_name)

    archive_folder("test_folder", category_name)

    search_results = search("test_folder")
    if not search_results:
        print("No files found")
        return

    print("Search results:", search_results)
    item_id = search_results[0][0]

    workspace_path = checkout(item_id)
    if workspace_path is None:
        print("Checkout failed")
        return

    if os.path.isfile(workspace_path):
        target_file = workspace_path
    else:
        target_file = os.path.join(workspace_path, "notes.txt")

    if os.path.isfile(target_file):
        with open(target_file, "a", encoding="utf-8") as edited_file:
            edited_file.write("\nedited by SmartArchive demo run\n")

    checkin(item_id)

    handle_pending_checkouts_on_exit()


if __name__ == '__main__':
    main()
