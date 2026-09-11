import os
import zipfile

import main
from tests.conftest import wait_idle


class FakeSourceActionDialog:
    Accepted = 1
    Rejected = 0
    _next_action = "keep"

    def __init__(self, *args, **kwargs):
        pass

    def exec(self):
        return self.Accepted

    def chosen_action(self):
        return FakeSourceActionDialog._next_action


def _set_fake_source_action(monkeypatch, action):
    FakeSourceActionDialog._next_action = action
    monkeypatch.setattr("gui.items_tab.SourceActionDialog", FakeSourceActionDialog)


def _select_destination_category(window, category_name):
    window.items_tab.refresh_categories()
    index = window.items_tab.destination_category.findText(category_name)
    window.items_tab.destination_category.setCurrentIndex(index)


def _archive_file_through_gui(window, qtbot, monkeypatch, path, category, action="keep"):
    _set_fake_source_action(monkeypatch, action)
    _select_destination_category(window, category)
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    window.items_tab._add_file()
    wait_idle(qtbot, window)


def _archive_folder_through_gui(window, qtbot, monkeypatch, path, category, action="keep"):
    _set_fake_source_action(monkeypatch, action)
    _select_destination_category(window, category)
    monkeypatch.setattr("gui.items_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: path))
    window.items_tab._add_folder()
    wait_idle(qtbot, window)


def _select_row_by_name(table, name):
    for row in range(table.rowCount()):
        if table.item(row, 0).text() == name:
            table.selectRow(row)
            return row
    raise AssertionError(f"row '{name}' not found")


def _select_row_by_name_and_original_path(table, name, original_path_suffix):
    # for two items sharing the same filename, disambiguate via the
    # "Original location" column (index 7) - see spec 11.6 same-filename fix.
    for row in range(table.rowCount()):
        if table.item(row, 0).text() == name and table.item(row, 7).text().replace("\\", "/").endswith(
            original_path_suffix.replace("\\", "/")
        ):
            table.selectRow(row)
            return row
    raise AssertionError(f"row '{name}' with original path ending '{original_path_suffix}' not found")


# --------------------------------------------------------------- archiving


def test_archive_file_through_gui_creates_real_zip_manifest_and_db_row(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("report.txt", "quarterly report content")

    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")

    items = main.search("report.txt")
    assert len(items) == 1
    zip_path = items[0][10]
    assert os.path.isfile(zip_path)
    manifest_path = os.path.splitext(zip_path)[0] + ".manifest.json"
    assert os.path.isfile(manifest_path)
    # path_in_archive is now a collision-safe SHA-256-prefixed member name, not the
    # plain basename (spec 11.6 fix) - look up the real internal path via the DB.
    with zipfile.ZipFile(zip_path) as zf:
        assert items[0][3] in zf.namelist()

    gui_window.items_tab.refresh()
    row = _select_row_by_name(gui_window.items_tab.table, "report.txt")
    assert gui_window.items_tab.table.item(row, 6).text() == "ARCHIVED"


def test_archive_folder_through_gui_preserves_nested_structure(gui_window, qtbot, monkeypatch, make_folder):
    main.create_category("Code")
    folder = make_folder("project", {"root.txt": "root", "sub/inner.txt": "inner"})

    _archive_folder_through_gui(gui_window, qtbot, monkeypatch, folder, "Code")

    items = main.search("project")
    assert len(items) == 1
    assert items[0][2] == "folder"
    zip_path = items[0][10]
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert any(name.endswith("root.txt") for name in names)
        assert any(name.endswith("sub/inner.txt") for name in names)


def test_archive_without_selecting_category_is_rejected(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    path = make_file("orphan.txt")
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    gui_window.items_tab.destination_category.clear()
    gui_window.items_tab._add_file()
    assert main.search("orphan.txt") == []
    assert any(call[0] == "warning" for call in message_boxes.calls)


def test_cancel_file_dialog_does_nothing(gui_window, qtbot, monkeypatch):
    main.create_category("Docs")
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    gui_window.items_tab._add_file()  # must not raise, must not archive anything
    assert main.search("") == []


def test_duplicate_content_is_detected_and_not_stored_twice(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    path1 = make_file("first.txt", "identical content")
    path2 = make_file("second.txt", "identical content")

    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path1, "Docs")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path2, "Docs")

    items = main.search("")
    assert len(items) == 1  # second file was never written as a new item
    assert any(call[0] == "information" and "Duplicate" in call[1] for call in message_boxes.calls)


def test_source_removed_only_after_successful_archive(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("todelete.txt", "gone soon")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs", action="delete")
    assert not os.path.exists(path)
    items = main.search("todelete.txt")
    assert len(items) == 1


# ----------------------------------------------------------------- checkout


def test_checkout_extracts_item_and_updates_status(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("checkout_me.txt", "content")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")

    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "checkout_me.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    items = main.search("checkout_me.txt")
    assert items[0][7] == "CHECKED_OUT"
    workspace_path = os.path.join("SmartArchiveData", "workspace", "checkout_me.txt")
    assert os.path.isfile(workspace_path)

    gui_window.items_tab.refresh()
    row = _select_row_by_name(gui_window.items_tab.table, "checkout_me.txt")
    assert gui_window.items_tab.table.item(row, 6).text() == "CHECKED_OUT"


def test_checkout_already_checked_out_item_is_handled_gracefully(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("twice.txt", "content")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "twice.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "twice.txt")
    gui_window.items_tab._checkout()  # button would be disabled in the real UI; guard must still hold
    assert not gui_window.is_busy()
    assert any(call[0] == "information" for call in message_boxes.calls)


def test_checkout_without_selection_shows_message_and_does_not_crash(gui_window, message_boxes):
    gui_window.items_tab.table.clearSelection()
    gui_window.items_tab.table.setCurrentCell(-1, -1)
    gui_window.items_tab._checkout()
    assert not gui_window.is_busy()
    assert any(call[0] == "information" for call in message_boxes.calls)


# ------------------------------------------------------------------ checkin


def test_checkin_unchanged_item_returns_to_archived(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("unchanged.txt", "same content")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "unchanged.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "unchanged.txt")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)

    items = main.search("unchanged.txt")
    assert items[0][7] == "ARCHIVED"


def test_checkin_modified_file_updates_zip_manifest_and_hash(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("editme.txt", "before edit")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "editme.txt")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    workspace_path = os.path.join("SmartArchiveData", "workspace", "editme.txt")
    with open(workspace_path, "a", encoding="utf-8") as f:
        f.write(" - after edit")

    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "editme.txt")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)

    items = main.search("editme.txt")
    assert items[0][7] == "ARCHIVED"
    zip_path = items[0][10]
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read(items[0][3]).decode("utf-8") == "before edit - after edit"

    status = main.verify_archive_by_id(main.get_item(items[0][0])[1])
    assert status == "VERIFIED"


def test_checkin_modified_folder_preserves_unrelated_members(gui_window, qtbot, monkeypatch, make_folder):
    main.create_category("Code")
    folder = make_folder("proj", {"a.txt": "a-content", "b.txt": "b-content"})
    _archive_folder_through_gui(gui_window, qtbot, monkeypatch, folder, "Code")

    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "proj")
    gui_window.items_tab._checkout()
    wait_idle(qtbot, gui_window)

    workspace_folder = os.path.join("SmartArchiveData", "workspace", "proj")
    with open(os.path.join(workspace_folder, "a.txt"), "a", encoding="utf-8") as f:
        f.write("-edited")

    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "proj")
    gui_window.items_tab._checkin()
    wait_idle(qtbot, gui_window)

    items = main.search("proj")
    zip_path = items[0][10]
    # path_in_archive for the folder root is now SHA-256-prefixed (e.g. "abcd1234_proj/");
    # nested members keep their relative path under that same prefixed root.
    folder_root = items[0][3]
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read(f"{folder_root}a.txt").decode("utf-8") == "a-content-edited"
        assert zf.read(f"{folder_root}b.txt").decode("utf-8") == "b-content"  # unrelated member untouched


def test_checkin_without_being_checked_out_is_rejected(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("notcheckedout.txt")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "notcheckedout.txt")
    gui_window.items_tab._checkin()  # button is disabled in the real UI; guard must still hold
    assert not gui_window.is_busy()
    assert any(call[0] == "information" for call in message_boxes.calls)


# ------------------------------------------------------------------- verify


def test_verify_reports_verified_status(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("verifyme.txt")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "verifyme.txt")
    gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)

    status_calls = [call for call in message_boxes.calls if "Archive integrity status" in call[2]]
    assert status_calls
    assert "VERIFIED" in status_calls[-1][2]


def test_verify_detects_corrupted_archive(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("corruptme.txt")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")

    items = main.search("corruptme.txt")
    zip_path = items[0][10]
    with open(zip_path, "ab") as f:
        f.write(b"garbage-bytes-to-corrupt-the-zip")

    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "corruptme.txt")
    gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)

    status_calls = [call for call in message_boxes.calls if "Archive integrity status" in call[2]]
    assert status_calls
    assert "CORRUPTED" in status_calls[-1][2] or "CHANGED" in status_calls[-1][2]


def test_verify_detects_missing_archive(gui_window, qtbot, monkeypatch, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("missingme.txt")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")

    items = main.search("missingme.txt")
    zip_path = items[0][10]
    os.remove(zip_path)

    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "missingme.txt")
    gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)

    status_calls = [call for call in message_boxes.calls if "Archive integrity status" in call[2]]
    assert status_calls
    assert "MISSING" in status_calls[-1][2]


def test_verify_without_selection_shows_message(gui_window, message_boxes):
    gui_window.items_tab.table.clearSelection()
    gui_window.items_tab.table.setCurrentCell(-1, -1)
    gui_window.items_tab._verify_selected()
    assert not gui_window.is_busy()
    assert any(call[0] == "information" for call in message_boxes.calls)


# ------------------------------------------------------------ robustness


def test_rapid_double_click_does_not_start_two_operations(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("doubleclick.txt")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")
    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "doubleclick.txt")

    gui_window.items_tab._checkout()
    assert gui_window.is_busy()
    gui_window.items_tab._checkout()  # second immediate call must be a no-op while busy
    wait_idle(qtbot, gui_window)

    items = main.search("doubleclick.txt")
    assert items[0][7] == "CHECKED_OUT"
