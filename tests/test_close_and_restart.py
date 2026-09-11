import os

from PySide6.QtCore import QEvent

import main
from gui.dialogs import PendingCheckoutsDialog
from tests.conftest import wait_idle


class FakePendingDialogLeave:
    Accepted = 1
    LEAVE_CHECKED_OUT = PendingCheckoutsDialog.LEAVE_CHECKED_OUT
    RETURN_SELECTED = PendingCheckoutsDialog.RETURN_SELECTED
    CANCEL = PendingCheckoutsDialog.CANCEL

    def __init__(self, *a, **k):
        pass

    def exec(self):
        return self.Accepted

    def result_action(self):
        return PendingCheckoutsDialog.LEAVE_CHECKED_OUT

    def selected_item_ids(self):
        return []


class FakePendingDialogReturnAll:
    Accepted = 1
    LEAVE_CHECKED_OUT = PendingCheckoutsDialog.LEAVE_CHECKED_OUT
    RETURN_SELECTED = PendingCheckoutsDialog.RETURN_SELECTED
    CANCEL = PendingCheckoutsDialog.CANCEL

    def __init__(self, *a, pending_items=(), **k):
        self._ids = [item[0] for item in pending_items]

    def exec(self):
        return self.Accepted

    def result_action(self):
        return PendingCheckoutsDialog.RETURN_SELECTED

    def selected_item_ids(self):
        return self._ids


class FakePendingDialogCancel:
    Rejected = 0
    Accepted = 1
    LEAVE_CHECKED_OUT = PendingCheckoutsDialog.LEAVE_CHECKED_OUT
    RETURN_SELECTED = PendingCheckoutsDialog.RETURN_SELECTED
    CANCEL = PendingCheckoutsDialog.CANCEL

    def __init__(self, *a, **k):
        pass

    def exec(self):
        return self.Rejected

    def result_action(self):
        return PendingCheckoutsDialog.CANCEL

    def selected_item_ids(self):
        return []


def _close_event():
    return QEvent(QEvent.Close)


def test_close_with_no_checked_out_items_closes_immediately(gui_window):
    gui_window._closing_in_progress = False
    event = _close_event()
    gui_window.closeEvent(event)
    assert event.isAccepted()


def test_close_with_checked_out_items_offers_leave_checked_out(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("pending.txt")
    main.archive_file(path, "Docs")
    item_id = main.search("pending.txt")[0][0]
    main.checkout(item_id)

    gui_window._closing_in_progress = False
    monkeypatch.setattr("gui.main_window.PendingCheckoutsDialog", FakePendingDialogLeave)
    event = _close_event()
    gui_window.closeEvent(event)

    assert event.isAccepted()
    items = main.search("pending.txt")
    assert items[0][7] == "CHECKED_OUT"  # left exactly as the user chose, not auto-returned


def test_close_with_checked_out_items_can_return_selected(gui_window, qtbot, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("returnme.txt")
    main.archive_file(path, "Docs")
    item_id = main.search("returnme.txt")[0][0]
    main.checkout(item_id)

    gui_window._closing_in_progress = False
    monkeypatch.setattr("gui.main_window.PendingCheckoutsDialog", FakePendingDialogReturnAll)
    event = _close_event()
    gui_window.closeEvent(event)

    assert event.isAccepted()
    items = main.search("returnme.txt")
    assert items[0][7] == "ARCHIVED"


def test_close_can_be_cancelled_by_user(gui_window, monkeypatch, make_file):
    main.create_category("Docs")
    path = make_file("cancelclose.txt")
    main.archive_file(path, "Docs")
    item_id = main.search("cancelclose.txt")[0][0]
    main.checkout(item_id)

    gui_window._closing_in_progress = False
    monkeypatch.setattr("gui.main_window.PendingCheckoutsDialog", FakePendingDialogCancel)
    event = _close_event()
    gui_window.closeEvent(event)

    assert not event.isAccepted()
    items = main.search("cancelclose.txt")
    assert items[0][7] == "CHECKED_OUT"  # nothing changed


def test_close_while_busy_is_refused(gui_window, message_boxes):
    gui_window._closing_in_progress = False
    gui_window.set_busy(True, "working...")
    event = _close_event()
    gui_window.closeEvent(event)
    assert not event.isAccepted()
    gui_window.set_busy(False)


def test_restart_reloads_persistent_state_from_disk(sandbox, qtbot):
    from gui.main_window import MainWindow

    main.initialize()
    main.create_category("Docs")
    path = os.path.join(str(sandbox), "persisted.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("persisted content")
    assert main.archive_file(path, "Docs") is True

    window1 = MainWindow()
    qtbot.addWidget(window1)
    window1.refresh_all()
    assert window1.dashboard_tab.items_label.text() == "1"
    window1.close()

    # simulate a brand new process: fresh MainWindow against the same on-disk DB
    main.initialize()
    window2 = MainWindow()
    qtbot.addWidget(window2)
    window2.refresh_all()
    assert window2.dashboard_tab.items_label.text() == "1"
    assert window2.dashboard_tab.categories_label.text() == "1"

    window2.items_tab.search_edit.setText("persisted")
    window2.items_tab.refresh()
    assert window2.items_tab.table.rowCount() == 1
    window2.close()
