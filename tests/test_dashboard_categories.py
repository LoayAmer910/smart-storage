import main
from gui.dialogs import CreateCategoryDialog


def test_dashboard_shows_real_zeros_on_fresh_db(gui_window):
    gui_window.dashboard_tab.refresh()
    assert gui_window.dashboard_tab.categories_label.text() == "0"
    assert gui_window.dashboard_tab.items_label.text() == "0"
    assert gui_window.dashboard_tab.checked_out_label.text() == "0"
    assert gui_window.dashboard_tab.warning_label.text() == ""


def test_dashboard_reflects_real_backend_state(gui_window, make_file):
    main.create_category("Docs", max_archive_size=2 * 1024 ** 3)
    file_path = make_file("sample.txt", "hello world")
    assert main.archive_file(file_path, "Docs") is True

    gui_window.dashboard_tab.refresh()
    assert gui_window.dashboard_tab.categories_label.text() == "1"
    assert gui_window.dashboard_tab.items_label.text() == "1"
    # values must come from the real DB, not be hardcoded
    summary = main.dashboard_summary()
    assert gui_window.dashboard_tab.original_size_label.text() != "-"
    assert summary["total_original_size"] == len("hello world")


def _create_category_through_gui(window, monkeypatch, name, existing_dialog_values=None):
    values = existing_dialog_values or {"name": name, "description": None, "color": None, "max_archive_size": 2 * 1024 ** 3}

    class FakeDialog:
        Accepted = CreateCategoryDialog.Accepted

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return CreateCategoryDialog.Accepted

        def values(self):
            return values

    monkeypatch.setattr("gui.categories_tab.CreateCategoryDialog", FakeDialog)
    window.categories_tab._create_category()


def test_create_category_through_gui_persists_to_backend(gui_window, monkeypatch):
    gui_window.categories_tab.refresh()
    _create_category_through_gui(gui_window, monkeypatch, "Projects")

    categories = main.list_categories()
    assert len(categories) == 1
    assert categories[0][1] == "Projects"

    # no empty ZIP should be created just from creating the category
    import os
    archives_dir = os.path.join("SmartArchiveData", "archives", "Projects")
    assert not os.path.isdir(archives_dir) or not any(
        name.endswith(".zip") for name in os.listdir(archives_dir)
    )


def test_create_category_duplicate_name_is_rejected_by_gui(gui_window, monkeypatch, message_boxes):
    main.create_category("Projects")
    gui_window.categories_tab.refresh()

    _create_category_through_gui(gui_window, monkeypatch, "Projects")

    categories = main.list_categories()
    assert len(categories) == 1  # duplicate was not inserted again
    assert any(call[0] == "warning" for call in message_boxes.calls)


def test_create_category_empty_name_is_rejected(gui_window, monkeypatch):
    _create_category_through_gui(
        gui_window, monkeypatch, "", existing_dialog_values={"name": "", "description": None, "color": None, "max_archive_size": 2 * 1024 ** 3}
    )
    assert main.list_categories() == []


def test_open_items_for_category_switches_tab_and_filters(gui_window, make_file):
    main.create_category("Docs")
    main.create_category("Code")
    main.archive_file(make_file("a.txt"), "Docs")
    main.archive_file(make_file("b.txt", "different content"), "Code")

    gui_window.categories_tab.refresh()
    gui_window.categories_tab.table.selectRow(
        [gui_window.categories_tab.table.item(r, 0).text() for r in range(gui_window.categories_tab.table.rowCount())].index("Docs")
    )
    gui_window.categories_tab._open_items()

    assert gui_window.tabs.currentWidget() is gui_window.items_tab
    assert gui_window.items_tab.table.rowCount() == 1
    assert gui_window.items_tab.table.item(0, 0).text() == "a.txt"
