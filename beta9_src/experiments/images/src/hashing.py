"""Standalone SHA-256 helpers for the image R&D prototype.

Deliberately not imported from the production archive_manager.py - this
module has no dependency on the StorageArch codebase at all, so the
prototype can never accidentally reach into production code.
"""

import hashlib

_CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming reads, bounded memory for large files


def sha256_file(file_path):
    """Stream a file through SHA-256 without loading it fully into memory."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def files_are_byte_identical(path_a, path_b):
    """The mandatory hard gate: SHA256(original) == SHA256(restored)."""
    return sha256_file(path_a) == sha256_file(path_b)
