import os

import archive_manager
import main
from gui.workers import run_in_background
from tests.conftest import wait_idle


def test_archive_file_result_invalid_path():
    result = main.archive_file_result("", "Docs")
    assert result["success"] is False
    assert result["reason"] == "INVALID_PATH"
    assert result["message"]


def test_archive_file_result_source_missing(gui_window):
    main.create_category("Docs")
    result = main.archive_file_result("does_not_exist.txt", "Docs")
    assert result["success"] is False
    assert result["reason"] == "SOURCE_MISSING"


def test_archive_file_result_protected_path(gui_window):
    # a fabricated, non-existent path under a protected root - the guard triggers on the
    # path string alone, before any filesystem access, so nothing real is ever touched.
    fake_protected_path = r"C:\Windows\System32\storagearch_test_fixture_does_not_exist.txt"
    result = main.archive_file_result(fake_protected_path, "Docs")
    assert result["success"] is False
    assert result["reason"] == "PROTECTED_PATH"


def test_archive_file_result_insufficient_space(gui_window, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("big.txt", "content")
    monkeypatch.setattr(archive_manager, "has_enough_free_space", lambda *a, **k: False)
    result = main.archive_file_result(path, "Docs")
    assert result["success"] is False
    assert result["reason"] == "INSUFFICIENT_SPACE"


def test_archive_file_result_duplicate(gui_window, make_file):
    main.create_category("Docs")
    path1 = make_file("first.txt", "same content")
    path2 = make_file("second.txt", "same content")
    assert main.archive_file(path1, "Docs") is True

    result = main.archive_file_result(path2, "Docs")
    assert result["success"] is True
    assert result["reason"] == "DUPLICATE"
    assert result["duplicate"]["item_name"] == "first.txt"


def test_archive_file_result_unexpected_error_is_caught_not_raised(gui_window, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("boom.txt", "content")

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(archive_manager, "calculate_file_hash", _raise)
    result = main.archive_file_result(path, "Docs")  # must not raise
    assert result["success"] is False
    assert result["reason"] == "UNEXPECTED_ERROR"
    assert "simulated unexpected failure" not in result["message"]  # internal detail not leaked verbatim to the user
    assert result["message"]


def test_archive_folder_result_source_missing(gui_window):
    main.create_category("Docs")
    result = main.archive_folder_result("no_such_folder", "Docs")
    assert result["success"] is False
    assert result["reason"] == "SOURCE_MISSING"


def test_items_tab_shows_specific_failure_message_not_generic_text(gui_window, message_boxes):
    gui_window.items_tab._pending_archive = ("some/path.txt", "Docs", False)
    gui_window.items_tab._on_archive_done(
        {"success": False, "reason": "INSUFFICIENT_SPACE", "message": "Not enough free space to archive file"}
    )
    assert any(
        call[0] == "warning" and call[2] == "Not enough free space to archive file"
        for call in message_boxes.calls
    )


def test_worker_exception_is_shown_as_dialog_not_raised_to_gui(gui_window, qtbot, message_boxes):
    def _boom():
        raise ValueError("worker exploded")

    captured = {}

    def on_success(_result):
        captured["success"] = True

    def on_error(message):
        captured["error"] = message

    thread, worker = run_in_background(gui_window, _boom, on_success, on_error)
    qtbot.waitUntil(lambda: "error" in captured, timeout=5000)
    assert "worker exploded" in captured["error"]
    assert "success" not in captured
