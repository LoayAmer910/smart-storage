from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog

from gui.dialogs import CreateCategoryDialog, PendingCheckoutsDialog, SourceActionDialog


def test_create_category_dialog_returns_values(qtbot):
    dialog = CreateCategoryDialog(default_max_size=2 * 1024 ** 3)
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("Documents")
    dialog.description_edit.setText("my docs")

    values = dialog.values()
    assert values["name"] == "Documents"
    assert values["description"] == "my docs"
    assert values["max_archive_size"] == 2 * 1024 ** 3


def test_create_category_dialog_cancel_does_not_crash(qtbot):
    dialog = CreateCategoryDialog()
    qtbot.addWidget(dialog)
    QTimer.singleShot(0, dialog.reject)
    result = dialog.exec()
    assert result == QDialog.Rejected


def test_source_action_dialog_default_is_keep(qtbot):
    dialog = SourceActionDialog(source_label="C:/some/file.txt")
    qtbot.addWidget(dialog)
    assert dialog.keep_radio.isChecked()
    dialog._on_accept()
    assert dialog.chosen_action() == SourceActionDialog.KEEP


def test_source_action_dialog_recycle_choice(qtbot):
    dialog = SourceActionDialog(source_label="C:/some/file.txt")
    qtbot.addWidget(dialog)
    dialog.recycle_radio.setChecked(True)
    dialog._on_accept()
    assert dialog.chosen_action() == SourceActionDialog.RECYCLE


def test_source_action_dialog_permanent_delete_requires_confirmation(qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    dialog = SourceActionDialog(source_label="C:/some/file.txt")
    qtbot.addWidget(dialog)
    dialog.delete_radio.setChecked(True)

    # user declines the second confirmation -> dialog must NOT accept / choose delete
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.No))
    accepted_signal_seen = []
    dialog.accepted.connect(lambda: accepted_signal_seen.append(True))
    dialog._on_accept()
    assert not accepted_signal_seen
    assert dialog.chosen_action() == SourceActionDialog.KEEP  # unchanged, nothing was confirmed

    # user confirms the second dialog -> now it proceeds with delete
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    dialog._on_accept()
    assert dialog.chosen_action() == SourceActionDialog.DELETE
    assert accepted_signal_seen


def test_pending_checkouts_dialog_lists_items_and_selection(qtbot):
    pending_items = [(1, "sample.txt", "file", "sample.txt", "workspace/sample.txt")]
    dialog = PendingCheckoutsDialog(pending_items=pending_items)
    qtbot.addWidget(dialog)
    assert dialog.list_widget.count() == 1
    assert dialog.selected_item_ids() == [1]

    dialog._leave_checked_out()
    assert dialog.result_action() == PendingCheckoutsDialog.LEAVE_CHECKED_OUT
