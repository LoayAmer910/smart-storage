import multiprocessing
import os
import sys
import traceback

from PySide6.QtWidgets import QApplication, QMessageBox

import archive_manager
import main
from gui.main_window import MainWindow


def _install_crash_hook():
    # C7: a global unhandled-exception hook. Two jobs at once: (1) reports
    # a sanitized crash summary (only when Telemetry is on - crash_service
    # re-checks this itself, plan §12.1), and (2) prevents PySide6's
    # default behavior of hard-crashing the whole process on an uncaught
    # exception raised inside a Qt slot (installing any sys.excepthook
    # changes that to "print and continue" instead of aborting) - a
    # stability improvement that comes for free with this hook.
    default_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        try:
            frames = traceback.extract_tb(exc_tb)
            component = function_name = safe_code_location = None
            if frames:
                last_frame = frames[-1]
                module_file = os.path.basename(last_frame.filename) if last_frame.filename else None
                component = os.path.splitext(module_file)[0] if module_file else None
                function_name = last_frame.name
                safe_code_location = f"{module_file}:{last_frame.lineno}" if module_file else None
            main.cloud_report_crash(exc_value, component=component, function_name=function_name, safe_code_location=safe_code_location)
        except Exception:  # noqa: BLE001 - the crash hook must never itself crash
            pass
        default_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def start_application():
    # שחזור אחרי הפעלה שנקטעה + הכנת תיקיות/DB (7.1, 11.4), בלי להריץ את הדמו של main.py.
    _install_crash_hook()
    recovered = archive_manager.recover_interrupted_operations()
    main.initialize()

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()

    if recovered:
        QMessageBox.information(
            window,
            "StorageArch",
            f"Recovered from {len(recovered)} interrupted operation(s) from a previous session.\n"
            "The last verified archive for each was kept unchanged.",
        )

    window.show()
    return app, window


def main_entry():
    app, window = start_application()
    exit_code = app.exec()
    # C5 plan §18.3: bounded shutdown, never blocks app exit on the network.
    main.shutdown_cloud(timeout=1.0)
    sys.exit(exit_code)


if __name__ == "__main__":
    # Required first line for a frozen (PyInstaller) build that uses
    # multiprocessing's "spawn" start method (smart_compression.py's
    # per-candidate compress/restore subprocesses) - see PyInstaller's own
    # pyi_rth_multiprocessing.py runtime hook, which only patches
    # multiprocessing.freeze_support to actually intercept a spawned
    # child's command line and dispatch straight to it; it never calls that
    # patched function itself. Without this call here, a frozen onefile
    # StorageArch.exe re-launched as a multiprocessing child re-runs this
    # entire `if __name__ == "__main__":` block from scratch instead of
    # running the pickled worker function - which itself spawns another
    # child the same way, recursively, while the real parent's
    # `result_queue.get(timeout=...)` in smart_compression._run_candidate
    # just times out waiting for a result that never arrives. Confirmed
    # directly: a minimal onefile repro without this call spawned dozens of
    # recursive full-relaunch processes within 30s and never returned a
    # result; the identical repro WITH this call returns in ~0.25s.
    multiprocessing.freeze_support()
    main_entry()
