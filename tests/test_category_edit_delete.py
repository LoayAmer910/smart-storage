import os
import zipfile

import main
from tests.conftest import wait_idle
from tests.test_items_workflow import _archive_file_through_gui, _select_row_by_name


def _category_id_by_name(name):
    for category in main.list_categories():
        if category[1] == name:
            return category[0]
    raise AssertionError(f"category '{name}' not found")


def test_edit_category_name_persists_and_refreshes_gui(gui_window):
    main.create_category("OldName", description="old desc", max_archive_size=2 * 1024 ** 3)
    category_id = _category_id_by_name("OldName")

    updated = main.update_category(category_id, "NewName", "new desc", "#00ff00", 5 * 1024 ** 3)
    assert updated is True

    category = main.get_category(category_id)
    assert category[1] == "NewName"
    assert category[2] == "new desc"
    assert category[3] == "#00ff00"
    assert category[4] == 5 * 1024 ** 3

    gui_window.categories_tab.refresh()
    names = [gui_window.categories_tab.table.item(row, 0).text() for row in range(gui_window.categories_tab.table.rowCount())]
    assert "NewName" in names
    assert "OldName" not in names


def test_edit_category_duplicate_name_is_rejected(gui_window):
    main.create_category("Alpha")
    main.create_category("Beta")
    alpha_id = _category_id_by_name("Alpha")

    updated = main.update_category(alpha_id, "Beta")
    assert updated is False
    assert main.get_category(alpha_id)[1] == "Alpha"  # unchanged


def test_edit_category_empty_name_is_rejected(gui_window):
    main.create_category("KeepMe")
    category_id = _category_id_by_name("KeepMe")
    assert main.update_category(category_id, "") is False
    assert main.get_category(category_id)[1] == "KeepMe"


def test_rename_category_does_not_break_existing_archive_paths(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("report.txt", "content")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "Docs")

    items_before = main.search("report.txt")
    zip_path_before = items_before[0][10]
    assert os.path.isfile(zip_path_before)

    category_id = _category_id_by_name("Docs")
    assert main.update_category(category_id, "Documents") is True

    # existing archive/manifest/db paths remain valid and readable after rename
    items_after = main.search("report.txt")
    assert len(items_after) == 1
    zip_path_after = items_after[0][10]
    assert zip_path_after == zip_path_before  # physical folder is stable, not renamed
    assert os.path.isfile(zip_path_after)
    with zipfile.ZipFile(zip_path_after) as zf:
        assert items_after[0][3] in zf.namelist()

    status = main.verify_archive_by_id(main.get_item(items_after[0][0])[1])
    assert status == "VERIFIED"

    # a NEW archive added after rename goes to the SAME physical folder as the old one
    path2 = os.path.join(os.path.dirname(path), "second.txt")
    with open(path2, "w", encoding="utf-8") as f:
        f.write("second content")
    assert main.archive_file(path2, "Documents") is True
    items2 = main.search("second.txt")
    assert os.path.dirname(items2[0][10]) == os.path.dirname(zip_path_after)


def test_rename_category_through_gui_dialog(gui_window, monkeypatch):
    main.create_category("Renamed1", max_archive_size=2 * 1024 ** 3)
    gui_window.categories_tab.refresh()
    row = None
    for r in range(gui_window.categories_tab.table.rowCount()):
        if gui_window.categories_tab.table.item(r, 0).text() == "Renamed1":
            row = r
    assert row is not None
    gui_window.categories_tab.table.selectRow(row)

    class FakeEditDialog:
        Accepted = 1

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return self.Accepted

        def values(self):
            return {"name": "Renamed2", "description": None, "color": None, "max_archive_size": 2 * 1024 ** 3}

    monkeypatch.setattr("gui.categories_tab.CreateCategoryDialog", FakeEditDialog)
    gui_window.categories_tab._edit_category()

    categories = main.list_categories()
    assert any(c[1] == "Renamed2" for c in categories)
    assert not any(c[1] == "Renamed1" for c in categories)


def test_delete_empty_category_succeeds(gui_window):
    main.create_category("EmptyCat")
    category_id = _category_id_by_name("EmptyCat")

    result = main.delete_category(category_id)
    assert result == "DELETED"
    assert main.get_category(category_id) is None
    assert not any(c[1] == "EmptyCat" for c in main.list_categories())


def test_delete_nonexistent_category_is_safe(gui_window):
    result = main.delete_category(999999)
    assert result == "NOT_FOUND"


def test_delete_nonempty_category_is_refused_and_data_survives(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("HasData")
    path = make_file("keepme.txt", "important content")
    _archive_file_through_gui(gui_window, qtbot, monkeypatch, path, "HasData")

    category_id = _category_id_by_name("HasData")
    items_before = main.search("keepme.txt")
    zip_path = items_before[0][10]

    result = main.delete_category(category_id)
    assert result == "NOT_EMPTY"

    # nothing was silently deleted
    assert main.get_category(category_id) is not None
    items_after = main.search("keepme.txt")
    assert len(items_after) == 1
    assert os.path.isfile(zip_path)
    status = main.verify_archive_by_id(main.get_item(items_after[0][0])[1])
    assert status == "VERIFIED"


def test_delete_category_through_gui_shows_correct_messages(gui_window, monkeypatch, message_boxes):
    main.create_category("GuiDeleteEmpty")
    gui_window.categories_tab.refresh()
    for r in range(gui_window.categories_tab.table.rowCount()):
        if gui_window.categories_tab.table.item(r, 0).text() == "GuiDeleteEmpty":
            gui_window.categories_tab.table.selectRow(r)

    message_boxes.warning_confirm = True  # user confirms the "delete category?" prompt
    gui_window.categories_tab._delete_category()
    assert not any(c[1] == "GuiDeleteEmpty" for c in main.list_categories())


def test_delete_category_through_gui_without_selection(gui_window, message_boxes):
    gui_window.categories_tab.table.clearSelection()
    gui_window.categories_tab.table.setCurrentCell(-1, -1)
    gui_window.categories_tab._delete_category()
    assert any(call[0] == "information" for call in message_boxes.calls)


def test_category_edit_delete_persist_after_restart(gui_window, sandbox):
    from gui.main_window import MainWindow

    main.create_category("PersistEdit", max_archive_size=2 * 1024 ** 3)
    category_id = _category_id_by_name("PersistEdit")
    main.update_category(category_id, "PersistEditRenamed", "desc", "#123456", 3 * 1024 ** 3)
    main.create_category("PersistDeleteMe")
    delete_id = _category_id_by_name("PersistDeleteMe")
    assert main.delete_category(delete_id) == "DELETED"

    gui_window.close()
    main.initialize()
    window2 = MainWindow()

    categories = main.list_categories()
    names = [c[1] for c in categories]
    assert "PersistEditRenamed" in names
    assert "PersistDeleteMe" not in names
    renamed = next(c for c in categories if c[1] == "PersistEditRenamed")
    assert renamed[2] == "desc"
    assert renamed[3] == "#123456"
    assert renamed[4] == 3 * 1024 ** 3

    window2._closing_in_progress = True
    window2.close()
