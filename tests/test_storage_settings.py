import os

import main
import storage_config
from tests.conftest import wait_idle


def test_fresh_configured_data_root_is_used(gui_window, sandbox):
    new_root = os.path.join(str(sandbox), "custom_data")
    assert main.set_storage_location(data_root=new_root) is True

    locations = main.get_storage_locations()
    assert locations["data_root"] == new_root
    assert os.path.isdir(os.path.join(new_root, "archives"))
    assert os.path.isfile(os.path.join(new_root, "smartarchive.db"))


def test_fresh_configured_workspace_root_is_used(gui_window, sandbox, make_file):
    main.create_category("Docs")
    new_workspace = os.path.join(str(sandbox), "custom_workspace")
    assert main.set_storage_location(workspace_root=new_workspace) is True

    path = make_file("doc.txt", "content")
    assert main.archive_file(path, "Docs") is True
    item_id = main.search("doc.txt")[0][0]
    workspace_path = main.checkout(item_id)
    assert workspace_path is not None
    assert os.path.dirname(workspace_path) == new_workspace
    assert os.path.isfile(workspace_path)


def test_storage_settings_persist_and_survive_restart(gui_window, sandbox):
    from gui.main_window import MainWindow

    new_root = os.path.join(str(sandbox), "persisted_data")
    new_workspace = os.path.join(str(sandbox), "persisted_workspace")
    assert main.set_storage_location(data_root=new_root, workspace_root=new_workspace) is True

    main.create_category("Persisted")

    gui_window.close()
    # brand-new process-equivalent reload: storage_config.load() re-reads from disk
    storage_config.load()
    main.initialize()
    window2 = MainWindow()

    locations = main.get_storage_locations()
    assert locations["data_root"] == new_root
    assert locations["workspace_root"] == new_workspace
    assert any(c[1] == "Persisted" for c in main.list_categories())

    window2._closing_in_progress = True
    window2.close()


def test_archive_checkout_checkin_after_switching_locations(gui_window, sandbox):
    new_root = os.path.join(str(sandbox), "switched_data")
    new_workspace = os.path.join(str(sandbox), "switched_workspace")
    assert main.set_storage_location(data_root=new_root, workspace_root=new_workspace) is True

    main.create_category("Switched")
    file_path = os.path.join(str(sandbox), "switched_file.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("switched content")
    assert main.archive_file(file_path, "Switched") is True

    item_id = main.search("switched_file.txt")[0][0]
    workspace_path = main.checkout(item_id)
    assert os.path.dirname(workspace_path) == new_workspace

    with open(workspace_path, "a", encoding="utf-8") as f:
        f.write(" - edited")
    assert main.checkin(item_id) is True

    status = main.verify_archive_by_id(main.get_item(item_id)[1])
    assert status == "VERIFIED"


def test_protected_path_is_rejected_for_data_root(gui_window):
    result = main.set_storage_location(data_root=r"C:\Windows\System32")
    assert result is False
    # nothing changed - still pointing at the original sandboxed default
    locations = main.get_storage_locations()
    assert "Windows" not in locations["data_root"]


def test_protected_path_is_rejected_for_workspace_root(gui_window):
    result = main.set_storage_location(workspace_root=r"C:\Program Files\SomeApp")
    assert result is False


def test_workspace_change_refused_while_items_checked_out(gui_window, sandbox, make_file):
    main.create_category("Docs")
    path = make_file("checked_out.txt")
    main.archive_file(path, "Docs")
    item_id = main.search("checked_out.txt")[0][0]
    main.checkout(item_id)

    new_workspace = os.path.join(str(sandbox), "should_not_apply")
    result = main.set_storage_location(workspace_root=new_workspace)
    assert result is False
    assert main.get_storage_locations()["workspace_root"] != new_workspace


def test_switching_data_location_does_not_delete_or_move_existing_data(gui_window, sandbox, make_file):
    old_root = main.get_storage_locations()["data_root"]

    main.create_category("Original")
    path = make_file("keep.txt", "must survive")
    main.archive_file(path, "Original")
    items = main.search("keep.txt")
    original_zip_path = items[0][10]
    assert os.path.isfile(original_zip_path)

    new_root = os.path.join(str(sandbox), "new_empty_location")
    assert main.set_storage_location(data_root=new_root) is True

    # the OLD location's files are untouched on disk even though the app now points elsewhere
    assert os.path.isfile(original_zip_path)
    # the app now sees an empty (new) location - no silent merge/loss, just a different view
    assert main.dashboard_summary()["category_count"] == 0

    # switching back restores full access to the original data
    assert main.set_storage_location(data_root=old_root) is True
    assert any(c[1] == "Original" for c in main.list_categories())
    assert os.path.isfile(original_zip_path)


def test_settings_tab_change_data_location_through_gui(gui_window, monkeypatch, sandbox, message_boxes):
    new_root = os.path.join(str(sandbox), "gui_chosen_data")
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: new_root))

    gui_window.settings_tab._change_data_root()

    assert main.get_storage_locations()["data_root"] == new_root
    assert os.path.isdir(os.path.join(new_root, "archives"))


def test_settings_tab_change_workspace_refused_with_pending_checkout(gui_window, monkeypatch, sandbox, make_file, message_boxes):
    main.create_category("Docs")
    path = make_file("held.txt")
    main.archive_file(path, "Docs")
    item_id = main.search("held.txt")[0][0]
    main.checkout(item_id)

    new_workspace = os.path.join(str(sandbox), "gui_chosen_workspace")
    monkeypatch.setattr("gui.settings_tab.QFileDialog.getExistingDirectory", staticmethod(lambda *a, **k: new_workspace))

    gui_window.settings_tab._change_workspace_root()

    assert main.get_storage_locations()["workspace_root"] != new_workspace
    assert any(call[0] == "warning" for call in message_boxes.calls)
