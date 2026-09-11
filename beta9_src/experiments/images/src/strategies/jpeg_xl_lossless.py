"""JPEG XL lossless-JPEG-recompression strategy - PREPARED, NOT ACTIVE.

Per explicit project instruction: do NOT download cjxl.exe/djxl.exe yet.
This module only defines where the strategy WOULD look for those binaries
and how it would invoke them (cjxl --lossless_jpeg=1 / djxl), so the
harness and benchmark plumbing are ready the moment binaries are approved
and placed here.

is_available() only ever checks the local filesystem - it never attempts
a network fetch. Until the tools are present, this strategy always
reports itself unavailable and the harness records that as a clean
SKIPPED_UNAVAILABLE row, never a failure.

When ready to activate, this requires explicit user approval with:
  - exact source (official libjxl GitHub releases)
  - filename/version
  - expected download size
  - destination: experiments/images/tools/
"""

import os
import subprocess
import sys

from strategies.base import CompressionStrategy, CompressResult, RestoreResult

# cjxl.exe/djxl.exe are console-subsystem executables. Launched via
# subprocess.run() from a --windowed (GUI-subsystem, no console) parent -
# StorageArch.exe itself, or a spawned worker process that is also the same
# --windowed onefile app - Windows' CreateProcess default behavior is to
# allocate and briefly flash a brand-new console window for the child,
# since it has no existing console to inherit. That happens once per
# compress() and once per restore() call, i.e. up to twice per JPEG per
# worker - purely a visible-window side effect, not a functional one: it
# never affects the subprocess's actual execution, stdout/stderr capture,
# exit code, or output correctness. CREATE_NO_WINDOW tells CreateProcess to
# skip allocating that console entirely; everything else about the call
# (arguments, check=True, capture_output=True, the resulting files) is
# unchanged.
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Path resolution ONLY - no change to availability/selection/threshold logic
# below. A frozen PyInstaller onefile build extracts its bundled data files
# (see the build command's --add-binary entries for cjxl.exe/djxl.exe) to a
# temporary directory at runtime, exposed via sys._MEIPASS - __file__-
# relative resolution (the dev-tree layout, unchanged for the non-frozen
# case) does not point anywhere useful once this module is loaded from the
# frozen app's embedded PYZ instead of a real file on disk from
# experiments/images/src/strategies/. is_available() still just checks
# os.path.isfile on whatever this resolves to, exactly as before - the tool
# is either really there or it isn't, in both cases.
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _TOOLS_DIR = os.path.join(sys._MEIPASS, "images", "tools")
else:
    _TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tools")
_CJXL_PATH = os.path.join(_TOOLS_DIR, "cjxl.exe")
_DJXL_PATH = os.path.join(_TOOLS_DIR, "djxl.exe")


class JpegXlLosslessStrategy(CompressionStrategy):
    name = "jpeg_xl_lossless_jpeg"
    applicable_formats = frozenset({"jpg", "jpeg"})

    def is_available(self):
        if not (os.path.isfile(_CJXL_PATH) and os.path.isfile(_DJXL_PATH)):
            return False, (
                "cjxl.exe/djxl.exe not present in experiments/images/tools/ - "
                "not downloaded yet, awaiting explicit approval per project rules"
            )
        return True, ""

    def compress(self, input_path, work_dir):
        arcname = os.path.basename(input_path)
        out_path = os.path.join(work_dir, arcname + ".jxl")
        subprocess.run(
            [_CJXL_PATH, input_path, out_path, "--lossless_jpeg=1"],
            check=True, capture_output=True, creationflags=_NO_WINDOW_FLAGS,
        )
        return CompressResult(output_path=out_path, stored_size=os.path.getsize(out_path))

    def restore(self, compressed_path, work_dir, original_filename):
        out_path = os.path.join(work_dir, "restored_" + original_filename)
        subprocess.run(
            [_DJXL_PATH, compressed_path, out_path],
            check=True, capture_output=True, creationflags=_NO_WINDOW_FLAGS,
        )
        return RestoreResult(output_path=out_path)
