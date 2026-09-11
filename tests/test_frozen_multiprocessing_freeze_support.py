"""Regression test for the real Beta EXE tiny-file archive delay (5.5-10
minutes for a 20-30 KB file, both eventually succeeding via baseline
fallback).

Root cause: app.py (the actual PyInstaller entry point - see
build_storagearch_windows.ps1's `... app.py`) never called
`multiprocessing.freeze_support()`. PyInstaller's own
pyi_rth_multiprocessing.py runtime hook only *patches*
`multiprocessing.freeze_support` to intercept a spawned child's command
line and dispatch straight into the pickled worker function - it never
calls that patched function itself. Without an explicit call at the very
top of `if __name__ == "__main__":`, a frozen onefile EXE re-launched as a
smart_compression.py `_run_candidate`/`_restore_worker` spawn-context child
process re-runs the ENTIRE entry point from scratch instead of running the
worker - which itself spawns another child the same way, recursively,
while the real parent's `result_queue.get(timeout=...)` in
smart_compression._run_candidate just times out waiting for a result that
never arrives (confirmed directly: a minimal onefile repro without the
call spawned dozens of recursive full-relaunch processes within 30
wall-clock seconds and never returned a result; the identical repro WITH
the call returns in ~0.25s - see the session record for the measured
before/after).

This module can't reproduce the frozen-onefile recursion itself (that
needs an actual PyInstaller build, not a pytest process), so it covers the
two things that ARE testable here: (1) a structural check that the fix
itself doesn't regress, and (2) a real end-to-end performance budget for
the archive path this bug affected, so any future change that reintroduces
an unbounded per-candidate cost for tiny files gets caught.
"""
import ast
import os
import time

import main


def test_app_entry_point_calls_freeze_support_first():
    app_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
    tree = ast.parse(open(app_py, encoding="utf-8").read(), filename=app_py)

    main_guard = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        ):
            main_guard = node
            break
    assert main_guard is not None, "app.py must have an `if __name__ == \"__main__\":` guard"
    assert main_guard.body, "the __main__ guard body must not be empty"

    first_stmt = main_guard.body[0]
    assert isinstance(first_stmt, ast.Expr) and isinstance(first_stmt.value, ast.Call), (
        "the first statement under `if __name__ == \"__main__\":` in app.py must be a call to "
        "multiprocessing.freeze_support() - required for smart_compression.py's spawn-context "
        "subprocesses to work correctly (not recursively relaunch the whole frozen app) once "
        "packaged by PyInstaller. See this file's module docstring."
    )
    call = first_stmt.value
    call_name = ast.unparse(call.func)
    assert call_name == "multiprocessing.freeze_support", (
        f"expected the first __main__ statement to be multiprocessing.freeze_support(), got: {call_name}()"
    )


def test_tiny_baseline_file_archives_in_seconds(sandbox, make_file):
    # A generic small file (well above policy.tiny_file_bytes=4096, matching
    # the real 20.9KB/26.7KB Beta reports) with content that won't pass any
    # real Smart Compression candidate's benefit bar, so this exercises the
    # exact "tries candidates, falls back to baseline" path the real bug
    # was in - without needing Cloud/telemetry set up at all.
    main.initialize()
    main.create_category("Cat")
    path = make_file("notes.txt", ("hello world, plain text content - " * 30) + "x" * 21000)
    assert os.path.getsize(path) >= 20000

    start = time.perf_counter()
    result = main.archive_file_result(path, "Cat")
    elapsed = time.perf_counter() - start

    assert result["success"] is True
    assert elapsed < 10.0, (
        f"archiving a ~20KB file took {elapsed:.1f}s - the real Beta EXE regression this test "
        "guards against took 5.5-10 MINUTES for files this size"
    )
