"""The 'current StorageArch method' comparator.

Mirrors archive_manager.add_file_to_archive's actual ZIP settings exactly
(zipfile.ZIP_DEFLATED, allowZip64=True, default compresslevel) so that
smart_size vs current_storagearch_size comparisons are apples-to-apples.

Deliberately does NOT use StorageArch's content-hash-prefixed member
naming (build_archive_member_name) - that's a multi-file dedup detail
irrelevant to a single-file compression-ratio comparison, and skipping it
keeps this module import-free from archive_manager.py.

This is always run as the reference baseline, not just another candidate.
"""

import os
import zipfile

from strategies.base import CompressionStrategy, CompressResult, RestoreResult


class BaselineZipStrategy(CompressionStrategy):
    name = "baseline_storagearch_zip"
    applicable_formats = frozenset({"*"})

    def compress(self, input_path, work_dir):
        arcname = os.path.basename(input_path)
        out_zip = os.path.join(work_dir, arcname + ".zip")
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.write(input_path, arcname=arcname)
        return CompressResult(output_path=out_zip, stored_size=os.path.getsize(out_zip))

    def restore(self, compressed_path, work_dir, original_filename):
        out_path = os.path.join(work_dir, "restored_" + original_filename)
        with zipfile.ZipFile(compressed_path, "r") as zf:
            arcname = zf.namelist()[0]
            with zf.open(arcname, "r") as member, open(out_path, "wb") as out_file:
                while True:
                    chunk = member.read(1024 * 1024)
                    if not chunk:
                        break
                    out_file.write(chunk)
        return RestoreResult(output_path=out_path)
