"""StorageArch GUI FINAL - PyInstaller entry point, isolated from the
project root on purpose (same fix as entrypoint/final_entry.py, applied to
the GUI app instead of the CLI engine driver).

The project root has its OWN copies of app.py, main.py, archive_manager.py,
archive_manager_smart.py, smart_compression.py, storage_config.py, and
database_manager.py - separate, different files from beta9_src's
self-contained copies of the same names (beta9_src is a full, independent
snapshot of the whole app, including its own gui/ and local_cloud/). A
frozen build whose entry script lives in the project root implicitly
searches that directory first when resolving `import app` (and everything
`import app` transitively pulls in) - the GUI would still open (root's own
app.py/main.py are complete, working code), but every real compression
candidate call from root's smart_compression.py doesn't accept beta12_src's
`pool=` kwarg, so every file would silently fall back to plain Deflate
with no visible error - exactly the packaging defect confirmed and fixed
for the CLI engine exe (see entrypoint/final_entry.py's docstring for the
full repro).

This file lives in its own directory with zero colliding filenames, so
PyInstaller's implicit script-directory search contributes nothing.
`pathex` in StorageArch_GUI_Final.spec points only at beta9_src for every
real import - uncontested, root never referenced.
"""
import multiprocessing
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_BETA9_SRC = os.path.join(_REPO_ROOT, "beta9_src")
_SMART_SELECTOR_SRC = os.path.join(_BETA9_SRC, "experiments", "smart_selector", "src")
_IMAGES_SRC = os.path.join(_BETA9_SRC, "experiments", "images", "src")
for _p in (_IMAGES_SRC, _SMART_SELECTOR_SRC, _BETA9_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# Deliberately NOT adding _REPO_ROOT to sys.path - beta9_src is fully
# self-contained (its own app/main/gui/local_cloud/archive_manager*/
# smart_compression/storage_config/database_manager), so nothing here
# needs the project root, and adding it would reintroduce the exact
# collision this file exists to avoid.

import app  # noqa: E402 - real Beta9 GUI entry module (beta9_src/app.py), never edited
from beta12_src.integration_layer import activate_beta12  # noqa: E402

_DIAG_LOG = os.path.join(_REPO_ROOT, "dist", "final_run", "gui_final_module_resolution.log")


def _log_resolved_modules():
    # One-shot, non-fatal diagnostic proving the collision fix took effect
    # in THIS build - written once at startup so it can be checked after a
    # real launch without needing a console window (this GUI is windowed).
    try:
        import archive_manager_smart
        import smart_compression
        os.makedirs(os.path.dirname(_DIAG_LOG), exist_ok=True)
        with open(_DIAG_LOG, "w", encoding="utf-8") as f:
            f.write(f"archive_manager_smart resolved from: {archive_manager_smart.__file__}\n")
            f.write(f"smart_compression resolved from: {smart_compression.__file__}\n")
            import inspect
            sig = inspect.signature(smart_compression.decide_and_prepare)
            f.write(f"decide_and_prepare signature: {sig}\n")
            f.write(f"has pool param (proves beta9_src copy, not project-root copy): {'pool' in sig.parameters}\n")
    except Exception as exc:  # noqa: BLE001 - diagnostics must never block real startup
        try:
            os.makedirs(os.path.dirname(_DIAG_LOG), exist_ok=True)
            with open(_DIAG_LOG, "w", encoding="utf-8") as f:
                f.write(f"diagnostic failed: {exc!r}\n")
        except Exception:  # noqa: BLE001
            pass


def main():
    activate_beta12()
    _log_resolved_modules()
    app.main_entry()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
