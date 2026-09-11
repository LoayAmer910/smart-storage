import os
import sqlite3
from pathlib import Path

import storage_config

ITEM_STATUSES = {
    "ARCHIVED",
    "CHECKED_OUT",
    "ARCHIVING",
    "CHECKING_IN",
    "ERROR",
    "MISSING",
}

ARCHIVE_STATUSES = {"VERIFIED", "CHANGED", "CORRUPTED", "MISSING"}

_INVALID_FOLDER_CHARS = '<>:"/\\|?*'


def _sanitize_folder_name(name):
    # מראה מקום קטן שחייב להישאר זהה ל-_sanitize_name ב-archive_manager.py -
    # הפונקציה הזו רק גוזרת folder_name קבוע בזמן יצירת קטגוריה/הגירת נתונים
    # ישנים, ואף פעם לא רצה שוב אחרי שינוי שם לוגי (ראו update_category).
    if not isinstance(name, str):
        name = str(name)
    sanitized = "".join(ch if ch not in _INVALID_FOLDER_CHARS else "_" for ch in name).strip()
    return sanitized or "category"


def _normalize_path(path):
    if path is None:
        return None
    return str(Path(path)).replace("\\", "/")


def _is_non_empty_string(value):
    return isinstance(value, str) and value.strip() != ""


def _is_non_negative_integer(value):
    try:
        return int(value) >= 0
    except (TypeError, ValueError):
        return False


def _get_default_db_path(database_path=None):
    if database_path is None:
        return Path(storage_config.db_path())
    return Path(database_path)


def get_connection(database_path=None):
    db_path = _get_default_db_path(database_path)
    os.makedirs(db_path.parent, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=5)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_database(database_path=None):
    db_path = _get_default_db_path(database_path)
    os.makedirs(db_path.parent, exist_ok=True)
    connection = get_connection(db_path)

    connection.execute("PRAGMA foreign_keys = ON")

    connection.execute("""
        CREATE TABLE IF NOT EXISTS categories
        (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            color TEXT,
            max_archive_size INTEGER NOT NULL DEFAULT 2147483648,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            folder_name TEXT
        )
    """)

    # הגירה עבור מסדי נתונים שנוצרו לפני שנוסף folder_name (שם התיקייה הפיזית,
    # קבוע מרגע היצירה, בלתי תלוי בשינוי שם לוגי מאוחר יותר - ראו update_category).
    # לקטגוריות ישנות התיקייה הפיזית כבר קיימת בדיסק כ-_sanitize_folder_name(name),
    # כי כך archive_manager.create_category פעל מאז ומעולם - הגיבוי חייב לשחזר בדיוק את זה.
    existing_columns = [row[1] for row in connection.execute("PRAGMA table_info(categories)").fetchall()]
    if "folder_name" not in existing_columns:
        connection.execute("ALTER TABLE categories ADD COLUMN folder_name TEXT")

    rows_needing_backfill = connection.execute(
        "SELECT id, name FROM categories WHERE folder_name IS NULL OR folder_name = ''"
    ).fetchall()
    for category_id, category_name in rows_needing_backfill:
        connection.execute(
            "UPDATE categories SET folder_name = ? WHERE id = ?",
            (_sanitize_folder_name(category_name), category_id)
        )

    connection.execute("""
        CREATE TABLE IF NOT EXISTS archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL UNIQUE,
            current_size INTEGER NOT NULL DEFAULT 0,
            item_count INTEGER NOT NULL DEFAULT 0,
            zip_hash TEXT,
            manifest_path TEXT,
            integrity_status TEXT NOT NULL DEFAULT 'MISSING',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            last_verified_at TEXT,
            FOREIGN KEY (category_id) REFERENCES categories(id)
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS items
        (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archive_id INTEGER NOT NULL,
            file_type TEXT NOT NULL,
            file_name TEXT NOT NULL,
            original_path TEXT NOT NULL,
            path_in_archive TEXT NOT NULL,
            original_size INTEGER NOT NULL,
            compressed_size INTEGER NOT NULL,
            original_hash TEXT NOT NULL,
            archive_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ARCHIVED',
            workspace_path TEXT,
            archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            checked_out_at TEXT,
            checked_in_at TEXT,
            FOREIGN KEY (archive_id) REFERENCES archives(id)
        )
    """)

    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_items_original_hash
        ON items (original_hash)
    """)

    # Phase 3 migration: Smart Compression strategy per item. Appended at
    # the end of the table (not inserted mid-schema), so every existing
    # index-based column access (item[8], item[10], ...) stays valid.
    # NULL means legacy/baseline storage - existing rows need no backfill,
    # NULL is exactly the correct value for archives created before this.
    existing_item_columns = [row[1] for row in connection.execute("PRAGMA table_info(items)").fetchall()]
    if "strategy" not in existing_item_columns:
        connection.execute("ALTER TABLE items ADD COLUMN strategy TEXT")

    connection.commit()
    connection.close()

    return str(db_path)


_CATEGORY_COLUMNS = "id, name, description, color, max_archive_size, created_at, folder_name"


def save_category(
        category_name,
        description=None,
        color=None,
        max_archive_size=2147483648,
        database_path=None
):
    if not _is_non_empty_string(category_name):
        return False
    if not _is_non_negative_integer(max_archive_size):
        return False

    # folder_name נקבע פעם אחת, בזמן היצירה בלבד, ולעולם לא משתנה שוב -
    # כך ששינוי שם לוגי מאוחר יותר (update_category) לא נוגע בנתיב הפיזי הקיים.
    folder_name = _sanitize_folder_name(category_name)

    connection = get_connection(database_path)
    cursor = connection.execute(
        "INSERT OR IGNORE INTO categories (name, description, color, max_archive_size, folder_name) VALUES (?, ?, ?, ?, ?)",
        (category_name, description, color, max_archive_size, folder_name)
    )
    connection.commit()
    category_saved = cursor.rowcount == 1
    connection.close()
    return category_saved


def get_category_by_name(category_name, database_path=None):
    connection = get_connection(database_path)
    cursor = connection.execute(
        f"SELECT {_CATEGORY_COLUMNS} FROM categories WHERE name = ?",
        (category_name,)
    )
    category = cursor.fetchone()
    connection.close()
    return category


def get_category_by_id(category_id, database_path=None):
    connection = get_connection(database_path)
    cursor = connection.execute(
        f"SELECT {_CATEGORY_COLUMNS} FROM categories WHERE id = ?",
        (category_id,)
    )
    category = cursor.fetchone()
    connection.close()
    return category


def get_category_id(category_name, database_path=None):
    category = get_category_by_name(category_name, database_path)
    return category[0] if category else None


def list_categories(database_path=None):
    connection = get_connection(database_path)
    cursor = connection.execute(
        f"SELECT {_CATEGORY_COLUMNS} FROM categories ORDER BY name"
    )
    categories = cursor.fetchall()
    connection.close()
    return categories


def update_category(
        category_id,
        name,
        description=None,
        color=None,
        max_archive_size=2147483648,
        database_path=None
):
    # מעדכנת רק את השדות הלוגיים (8.2); folder_name הפיזי לעולם לא נוגע כאן -
    # לכן שינוי שם לא יכול לשבור נתיבי ארכיון/Manifest קיימים.
    if not _is_non_empty_string(name):
        return False
    if not _is_non_negative_integer(max_archive_size):
        return False

    connection = get_connection(database_path)
    existing = connection.execute("SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone()
    if existing is None:
        connection.close()
        return False

    colliding = connection.execute(
        "SELECT id FROM categories WHERE name = ? AND id != ?", (name, category_id)
    ).fetchone()
    if colliding is not None:
        connection.close()
        return False

    try:
        connection.execute(
            """
            UPDATE categories
            SET name = ?, description = ?, color = ?, max_archive_size = ?
            WHERE id = ?
            """,
            (name, description, color, max_archive_size, category_id)
        )
        connection.commit()
    except sqlite3.IntegrityError:
        connection.close()
        return False

    connection.close()
    return True


def delete_category(category_id, database_path=None):
    # מוחקת קטגוריה רק אם היא ריקה לחלוטין (0 ארכיונים) - לעולם לא מוחקת
    # נתונים מאורכבים באופן שקט, בהתאם לכלל הבטיחות 11.1/11.9.
    connection = get_connection(database_path)
    category = connection.execute("SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone()
    if category is None:
        connection.close()
        return "NOT_FOUND"

    archive_count = connection.execute(
        "SELECT COUNT(*) FROM archives WHERE category_id = ?", (category_id,)
    ).fetchone()[0]
    if archive_count > 0:
        connection.close()
        return "NOT_EMPTY"

    connection.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    connection.commit()
    connection.close()
    return "DELETED"


def get_dashboard_summary(database_path=None):
    connection = get_connection(database_path)
    category_count = connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    item_count = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    total_original_size, total_compressed_size = connection.execute(
        "SELECT COALESCE(SUM(original_size), 0), COALESCE(SUM(compressed_size), 0) FROM items"
    ).fetchone()
    checked_out_count = connection.execute(
        "SELECT COUNT(*) FROM items WHERE status = 'CHECKED_OUT'"
    ).fetchone()[0]
    integrity_rows = connection.execute(
        "SELECT integrity_status, COUNT(*) FROM archives GROUP BY integrity_status"
    ).fetchall()
    connection.close()

    integrity_counts = {status: 0 for status in ARCHIVE_STATUSES}
    for status, count in integrity_rows:
        if status in integrity_counts:
            integrity_counts[status] = count

    return {
        "category_count": category_count,
        "item_count": item_count,
        "total_original_size": total_original_size,
        "total_compressed_size": total_compressed_size,
        "total_saved": total_original_size - total_compressed_size,
        "checked_out_count": checked_out_count,
        "integrity_counts": integrity_counts,
    }


def list_all_archives(database_path=None):
    connection = get_connection(database_path)
    cursor = connection.execute(
        """
        SELECT
            archives.id,
            categories.name,
            archives.file_name,
            archives.file_path,
            archives.current_size,
            archives.item_count,
            archives.integrity_status,
            archives.last_verified_at
        FROM archives
        JOIN categories ON archives.category_id = categories.id
        ORDER BY categories.name, archives.file_name
        """
    )
    archives = cursor.fetchall()
    connection.close()
    return archives


def save_archive(
        category_id,
        zip_path,
        current_size=0,
        item_count=0,
        zip_hash=None,
        manifest_path=None,
        integrity_status="MISSING",
        database_path=None
):
    if not isinstance(category_id, int) or category_id <= 0:
        return False
    if not _is_non_empty_string(zip_path):
        return False

    file_name = os.path.basename(zip_path)
    stored_zip_path = _normalize_path(zip_path)
    stored_manifest_path = _normalize_path(manifest_path) if manifest_path else None

    connection = get_connection(database_path)
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO archives
            (category_id, file_name, file_path, current_size, item_count, zip_hash, manifest_path, integrity_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            category_id,
            file_name,
            stored_zip_path,
            current_size,
            item_count,
            zip_hash,
            stored_manifest_path,
            integrity_status
        )
    )
    connection.commit()
    archive_saved = cursor.rowcount == 1
    connection.close()
    return archive_saved


def get_archive_by_id(archive_id, database_path=None):
    connection = get_connection(database_path)
    cursor = connection.execute(
        """
        SELECT id, category_id, file_name, file_path, current_size, item_count, zip_hash, manifest_path, integrity_status, created_at, updated_at, last_verified_at
        FROM archives
        WHERE id = ?
        """,
        (archive_id,)
    )
    archive = cursor.fetchone()
    connection.close()
    return archive


def get_archives_by_category_id(category_id, database_path=None):
    connection = get_connection(database_path)
    cursor = connection.execute(
        """
        SELECT id, category_id, file_name, file_path, current_size, item_count, zip_hash, manifest_path, integrity_status
        FROM archives
        WHERE category_id = ?
        ORDER BY file_name
        """,
        (category_id,)
    )
    archives = cursor.fetchall()
    connection.close()
    return archives


def get_archive_by_path(zip_path, database_path=None):
    stored_zip_path = _normalize_path(zip_path)
    connection = get_connection(database_path)
    cursor = connection.execute(
        """
        SELECT id, category_id, file_name, file_path, current_size, item_count, zip_hash, manifest_path, integrity_status
        FROM archives
        WHERE file_path = ?
        """,
        (stored_zip_path,)
    )
    archive = cursor.fetchone()
    connection.close()
    return archive


def get_archive_id(category_id, zip_path, database_path=None):
    file_name = os.path.basename(zip_path)
    connection = get_connection(database_path)
    cursor = connection.execute(
        "SELECT id FROM archives WHERE category_id = ? AND file_name = ?",
        (category_id, file_name)
    )
    archive = cursor.fetchone()
    connection.close()
    return archive[0] if archive else None


def update_archive_details(
        archive_id,
        current_size,
        item_count,
        zip_hash,
        manifest_path,
        integrity_status,
        database_path=None
):
    connection = get_connection(database_path)
    stored_manifest_path = _normalize_path(manifest_path) if manifest_path else None
    connection.execute(
        """
        UPDATE archives
        SET current_size = ?,
            item_count = ?,
            zip_hash = ?,
            manifest_path = ?,
            integrity_status = ?,
            updated_at = CURRENT_TIMESTAMP,
            last_verified_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            current_size,
            item_count,
            zip_hash,
            stored_manifest_path,
            integrity_status,
            archive_id
        )
    )
    connection.commit()
    connection.close()


def update_archive_status(archive_id, integrity_status, database_path=None):
    if integrity_status not in ARCHIVE_STATUSES:
        return False

    connection = get_connection(database_path)
    connection.execute(
        """
        UPDATE archives
        SET integrity_status = ?,
            last_verified_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (integrity_status, archive_id)
    )
    connection.commit()
    connection.close()
    return True


def save_item(
        archive_id,
        file_type,
        file_name,
        original_path,
        path_in_archive,
        original_size,
        compressed_size,
        original_hash,
        archive_hash,
        status="ARCHIVED",
        workspace_path=None,
        database_path=None,
        strategy=None
):
    if not isinstance(archive_id, int) or archive_id <= 0:
        return False
    if not _is_non_empty_string(file_type):
        return False
    if not _is_non_empty_string(file_name):
        return False
    if not _is_non_empty_string(original_path):
        return False
    if not _is_non_empty_string(path_in_archive):
        return False
    if not isinstance(original_size, int) or original_size < 0:
        return False
    if not isinstance(compressed_size, int) or compressed_size < 0:
        return False
    if not _is_non_empty_string(original_hash):
        return False
    if not _is_non_empty_string(archive_hash):
        return False
    if status not in ITEM_STATUSES:
        return False

    connection = get_connection(database_path)
    cursor = connection.execute(
        "SELECT id FROM items WHERE archive_id = ? AND path_in_archive = ?",
        (archive_id, path_in_archive)
    )
    existing_item = cursor.fetchone()

    if existing_item is not None:
        connection.close()
        return False

    stored_workspace_path = _normalize_path(workspace_path) if workspace_path else None
    connection.execute(
        """
        INSERT INTO items (
            archive_id,
            file_type,
            file_name,
            original_path,
            path_in_archive,
            original_size,
            compressed_size,
            original_hash,
            archive_hash,
            status,
            workspace_path,
            strategy
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            archive_id,
            file_type,
            file_name,
            _normalize_path(original_path),
            path_in_archive,
            original_size,
            compressed_size,
            original_hash,
            archive_hash,
            status,
            stored_workspace_path,
            strategy
        )
    )
    connection.commit()
    connection.close()
    return True


def get_item_id(archive_id, path_in_archive, database_path=None):
    connection = get_connection(database_path)
    cursor = connection.execute(
        "SELECT id FROM items WHERE archive_id = ? AND path_in_archive = ?",
        (archive_id, path_in_archive)
    )
    item = cursor.fetchone()
    connection.close()
    return item[0] if item else None


def get_item_by_id(item_id, database_path=None):
    connection = get_connection(database_path)
    cursor = connection.execute(
        "SELECT * FROM items WHERE id = ?",
        (item_id,)
    )
    item = cursor.fetchone()
    connection.close()
    return item


def find_item_by_hash(original_hash, database_path=None):
    connection = get_connection(database_path)
    cursor = connection.execute(
        """
        SELECT
            items.id,
            items.file_name,
            items.file_type,
            items.path_in_archive,
            items.original_path,
            archives.id,
            archives.file_name,
            archives.file_path,
            categories.name,
            items.status
        FROM items
        JOIN archives ON items.archive_id = archives.id
        JOIN categories ON archives.category_id = categories.id
        WHERE items.original_hash = ?
        LIMIT 1
        """,
        (original_hash,)
    )
    item = cursor.fetchone()
    connection.close()
    return item


def get_archive_path_by_item_id(item_id, database_path=None):
    connection = get_connection(database_path)
    cursor = connection.execute(
        """
        SELECT archives.file_path
        FROM items
        JOIN archives ON items.archive_id = archives.id
        WHERE items.id = ?
        """,
        (item_id,)
    )
    archive = cursor.fetchone()
    connection.close()
    return archive[0] if archive else None


def search_items(search_text, category_name=None, extension=None, database_path=None):
    connection = get_connection(database_path)
    search_value = f"%{search_text}%"
    query = """
        SELECT
            items.id,
            items.file_name,
            items.file_type,
            items.path_in_archive,
            items.original_path,
            items.original_size,
            items.compressed_size,
            items.status,
            categories.name,
            archives.file_name,
            archives.file_path
        FROM items
        JOIN archives ON items.archive_id = archives.id
        JOIN categories ON archives.category_id = categories.id
        WHERE items.file_name LIKE ?
    """
    params = [search_value]

    if category_name:
        query += " AND categories.name = ?"
        params.append(category_name)

    if extension:
        query += " AND items.file_name LIKE ?"
        params.append(f"%{extension}")

    cursor = connection.execute(query, tuple(params))
    items = cursor.fetchall()
    connection.close()
    return items


def update_item_after_edit(
        item_id,
        new_size,
        new_compressed_size,
        new_hash,
        archive_hash=None,
        status="ARCHIVED",
        workspace_path=None,
        database_path=None,
        strategy=None
):
    if status not in ITEM_STATUSES:
        return False

    connection = get_connection(database_path)
    stored_workspace_path = _normalize_path(workspace_path) if workspace_path else None
    archive_hash = new_hash if archive_hash is None else archive_hash
    cursor = connection.execute(
        """
        UPDATE items
        SET original_size = ?,
            compressed_size = ?,
            original_hash = ?,
            archive_hash = ?,
            status = ?,
            workspace_path = ?,
            strategy = ?,
            checked_in_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            new_size,
            new_compressed_size,
            new_hash,
            archive_hash,
            status,
            stored_workspace_path,
            strategy,
            item_id
        )
    )
    connection.commit()
    item_updated = cursor.rowcount == 1
    connection.close()
    return item_updated


def update_item_status(item_id, status, workspace_path=None, database_path=None):
    if status not in ITEM_STATUSES:
        return False

    connection = get_connection(database_path)
    stored_workspace_path = _normalize_path(workspace_path) if workspace_path else None
    cursor = connection.execute(
        """
        UPDATE items
        SET status = ?,
            workspace_path = ?,
            checked_out_at = CASE WHEN ? = 'CHECKED_OUT' THEN CURRENT_TIMESTAMP ELSE checked_out_at END,
            checked_in_at = CASE WHEN ? = 'ARCHIVED' THEN CURRENT_TIMESTAMP ELSE checked_in_at END
        WHERE id = ?
        """,
        (status, stored_workspace_path, status, status, item_id)
    )
    connection.commit()
    item_updated = cursor.rowcount == 1
    connection.close()
    return item_updated


def get_checked_out_items(database_path=None):
    connection = get_connection(database_path)
    cursor = connection.execute(
        """
        SELECT id, file_name, file_type, path_in_archive, workspace_path
        FROM items
        WHERE status = 'CHECKED_OUT'
        """
    )
    items = cursor.fetchall()
    connection.close()
    return items
