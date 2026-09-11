import main
from tests.conftest import wait_idle
from tests.test_items_workflow import _select_row_by_name, _set_fake_source_action


def test_rapid_add_file_clicks_do_not_start_two_archive_operations(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("rapidfile.txt", "content")

    _set_fake_source_action(monkeypatch, "keep")
    gui_window.items_tab.refresh_categories()
    gui_window.items_tab.destination_category.setCurrentIndex(gui_window.items_tab.destination_category.findText("Docs"))
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))

    gui_window.items_tab._add_file()
    assert gui_window.is_busy()
    gui_window.items_tab._add_file()  # second click while the first is mid-flight must be a no-op
    gui_window.items_tab._add_file()  # third for good measure
    wait_idle(qtbot, gui_window)

    items = main.search("rapidfile.txt")
    assert len(items) == 1  # only one archive write happened, not three


def test_rapid_verify_clicks_do_not_corrupt_state(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("verifyrapid.txt", "content")
    _set_fake_source_action(monkeypatch, "keep")
    gui_window.items_tab.refresh_categories()
    gui_window.items_tab.destination_category.setCurrentIndex(gui_window.items_tab.destination_category.findText("Docs"))
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    gui_window.items_tab._add_file()
    wait_idle(qtbot, gui_window)

    gui_window.items_tab.refresh()
    _select_row_by_name(gui_window.items_tab.table, "verifyrapid.txt")

    for _ in range(5):
        gui_window.items_tab._verify_selected()
    wait_idle(qtbot, gui_window)

    status = main.verify_archive_by_id(main.get_item(main.search("verifyrapid.txt")[0][0])[1])
    assert status == "VERIFIED"


def test_rapid_category_creation_clicks_do_not_create_duplicates(gui_window, monkeypatch):
    class FakeDialog:
        Accepted = 1

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return self.Accepted

        def values(self):
            return {"name": "RapidCat", "description": None, "color": None, "max_archive_size": 2 * 1024 ** 3}

    monkeypatch.setattr("gui.categories_tab.CreateCategoryDialog", FakeDialog)

    for _ in range(5):
        gui_window.categories_tab._create_category()

    matches = [c for c in main.list_categories() if c[1] == "RapidCat"]
    assert len(matches) == 1


def test_repeated_refresh_calls_are_harmless(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("refreshme.txt", "content")
    _set_fake_source_action(monkeypatch, "keep")
    gui_window.items_tab.refresh_categories()
    gui_window.items_tab.destination_category.setCurrentIndex(gui_window.items_tab.destination_category.findText("Docs"))
    monkeypatch.setattr("gui.items_tab.QFileDialog.getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    gui_window.items_tab._add_file()
    wait_idle(qtbot, gui_window)

    for _ in range(10):
        gui_window.refresh_all()

    assert len(main.search("refreshme.txt")) == 1
