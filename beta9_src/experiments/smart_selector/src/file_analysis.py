"""Generalized real-file-type detection for the Smart Selector.

Extends the Phase 1 image-signature sniffer to the full mixed corpus:
images, ZIP-family containers (including OOXML Office docs, which are
ZIP internally), PDF, SQLite, gzip, and a best-effort text/data-format
guess (JSON/XML/CSV/SQL/source code) for formats with no reliable magic
bytes. Detection never trusts the extension alone - extension is only
used as a tie-breaker for text-based formats and to flag mismatches.
"""

import hashlib
import os

_BINARY_SIGNATURES = [
    ("JPEG", lambda b: b[:3] == b"\xff\xd8\xff"),
    ("PNG", lambda b: b[:8] == b"\x89PNG\r\n\x1a\n"),
    ("GIF", lambda b: b[:6] in (b"GIF87a", b"GIF89a")),
    ("BMP", lambda b: b[:2] == b"BM"),
    ("TIFF", lambda b: b[:4] in (b"II*\x00", b"MM\x00*")),
    ("WEBP", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
    ("AVIF", lambda b: b[4:8] == b"ftyp" and b[8:12] in (b"avif", b"avis")),
    ("HEIC", lambda b: b[4:8] == b"ftyp" and b[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"hevm", b"hevs", b"mif1")),
    ("PDF", lambda b: b[:5] == b"%PDF-"),
    ("SQLITE", lambda b: b[:16] == b"SQLite format 3\x00"),
    ("GZIP", lambda b: b[:2] == b"\x1f\x8b"),
    ("ZIP", lambda b: b[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")),
    ("ELF_OR_PE", lambda b: b[:2] == b"MZ" or b[:4] == b"\x7fELF"),
]

_OOXML_EXTENSIONS = {"docx", "xlsx", "pptx"}

_TEXT_EXTENSION_HINTS = {
    "json": "JSON", "xml": "XML", "csv": "CSV", "sql": "SQL", "py": "PYTHON",
    "txt": "TEXT", "md": "MARKDOWN", "log": "LOG", "cpp": "CPP", "c": "C",
    "h": "C_HEADER", "sln": "SLN", "vcxproj": "VCXPROJ", "filters": "FILTERS",
    "user": "USER_CONFIG", "recipe": "TEXT",
}

# Compiler/IDE build artifacts whose headers can pass the printable-byte
# text-sniff heuristic (observed: .ipch precompiled headers were being
# misclassified as TEXT_UNKNOWN from their first 64 bytes). These are
# always genuinely binary regardless of what the byte sample looks like,
# so the extension is checked before any content sniffing runs.
_BUILD_ARTIFACT_EXTENSIONS = {
    "ipch", "pch", "obj", "pdb", "idb", "ilk", "sdf", "suo", "opendb",
    "tlog", "lastbuildstate", "res", "exp", "pgc", "pgd", "iobj", "ipdb",
}


def sha256_file(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _looks_like_text(head, sample_size=512):
    if not head:
        return False
    sample = head[:sample_size]
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for b in sample if 9 <= b <= 13 or 32 <= b <= 126)
    return printable / max(1, len(sample)) > 0.85


def detect_format(path, extension):
    """Returns (detected_type, is_binary, extension_mismatch)."""
    if extension in _BUILD_ARTIFACT_EXTENSIONS:
        return "BUILD_ARTIFACT", True, False

    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return "UNREADABLE", True, False

    for name, check in _BINARY_SIGNATURES:
        try:
            if check(head):
                if name == "ZIP" and extension in _OOXML_EXTENSIONS:
                    return "OOXML_ZIP", True, False
                mismatch = (
                    name.lower() not in (extension, {"jpg": "jpeg", "tif": "tiff"}.get(extension, extension))
                    and name not in ("ZIP", "ELF_OR_PE")
                )
                return name, True, mismatch
        except Exception:  # noqa: BLE001
            continue

    if extension in _TEXT_EXTENSION_HINTS and _looks_like_text(head):
        return _TEXT_EXTENSION_HINTS[extension], False, False

    if _looks_like_text(head):
        return "TEXT_UNKNOWN", False, False

    return "BINARY_UNKNOWN", True, False


def analyze_file(path, photos_root):
    extension = os.path.splitext(path)[1].lower().lstrip(".")
    size = os.path.getsize(path)
    relpath = os.path.relpath(path, photos_root)

    if size == 0:
        return {
            "relpath": relpath, "extension": extension, "size_bytes": 0,
            "detected_type": "EMPTY", "is_binary": False, "extension_mismatch": False,
            "sha256": None, "valid": False, "invalid_reason": "zero-byte file",
        }

    detected_type, is_binary, mismatch = detect_format(path, extension)
    try:
        digest = sha256_file(path)
    except OSError as exc:
        return {
            "relpath": relpath, "extension": extension, "size_bytes": size,
            "detected_type": detected_type, "is_binary": is_binary, "extension_mismatch": mismatch,
            "sha256": None, "valid": False, "invalid_reason": f"unreadable: {exc}",
        }

    return {
        "relpath": relpath, "extension": extension, "size_bytes": size,
        "detected_type": detected_type, "is_binary": is_binary, "extension_mismatch": mismatch,
        "sha256": digest, "valid": True, "invalid_reason": None,
    }
