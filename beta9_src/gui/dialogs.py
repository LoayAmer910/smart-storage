from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)
from PySide6.QtCore import Qt

from gui.format_utils import ARCHIVE_SIZE_PRESETS


class CreateCategoryDialog(QDialog):
    """
    7.2 - יצירת קטגוריה: שם, תיאור, צבע, גודל מרבי לארכיון.
    8.2 - אותו דיאלוג משמש גם לעריכת קטגוריה קיימת (initial_values), כדי לא
    לשכפל טופס זהה; folder_name הפיזי אינו חלק מהטופס בכלל ולעולם לא נערך.
    """

    def __init__(self, parent=None, default_max_size=2147483648, title="New Category", initial_values=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._selected_color = (initial_values or {}).get("color")

        self.name_edit = QLineEdit((initial_values or {}).get("name", ""))
        self.description_edit = QLineEdit((initial_values or {}).get("description") or "")

        self.color_button = QPushButton("Choose color...")
        self.color_button.clicked.connect(self._pick_color)
        self.color_label = QLabel(self._selected_color or "(none)")
        if self._selected_color:
            self.color_label.setStyleSheet(f"background-color: {self._selected_color};")

        self.size_combo = QComboBox()
        for label, value in ARCHIVE_SIZE_PRESETS:
            self.size_combo.addItem(label, value)
        initial_size = (initial_values or {}).get("max_archive_size", default_max_size)
        default_index = self.size_combo.findData(initial_size)
        self.size_combo.setCurrentIndex(default_index if default_index >= 0 else 1)

        color_row = QHBoxLayout()
        color_row.addWidget(self.color_button)
        color_row.addWidget(self.color_label)

        form = QFormLayout()
        form.addRow("Name:", self.name_edit)
        form.addRow("Description:", self.description_edit)
        form.addRow("Color:", color_row)
        form.addRow("Max archive size:", self.size_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _pick_color(self):
        color = QColorDialog.getColor(parent=self)
        if color.isValid():
            self._selected_color = color.name()
            self.color_label.setText(self._selected_color)
            self.color_label.setStyleSheet(f"background-color: {self._selected_color};")

    def values(self):
        return {
            "name": self.name_edit.text().strip(),
            "description": self.description_edit.text().strip() or None,
            "color": self._selected_color,
            "max_archive_size": self.size_combo.currentData(),
        }


class SourceActionDialog(QDialog):
    """7.3.11-12 - מה לעשות עם המקור אחרי ארכוב מוצלח. מחיקה לצמיתות דורשת אישור נוסף."""

    KEEP, RECYCLE, DELETE = "keep", "recycle", "delete"

    def __init__(self, parent=None, source_label=""):
        super().__init__(parent)
        self.setWindowTitle("Original file handling")
        self._chosen_action = self.KEEP

        info = QLabel(f"The item was archived and verified successfully.\nWhat should happen to the original?\n{source_label}")
        info.setWordWrap(True)

        self.keep_radio = QRadioButton("Keep the original where it is")
        self.recycle_radio = QRadioButton("Move the original to the Recycle Bin")
        self.delete_radio = QRadioButton("Delete the original permanently")
        self.keep_radio.setChecked(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.keep_radio)
        layout.addWidget(self.recycle_radio)
        layout.addWidget(self.delete_radio)
        layout.addWidget(buttons)

    def _on_accept(self):
        if self.delete_radio.isChecked():
            confirmation = QMessageBox.warning(
                self,
                "Confirm permanent deletion",
                "This will permanently delete the original file/folder.\n"
                "This action cannot be undone. Are you sure?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirmation != QMessageBox.Yes:
                return
            self._chosen_action = self.DELETE
        elif self.recycle_radio.isChecked():
            self._chosen_action = self.RECYCLE
        else:
            self._chosen_action = self.KEEP
        self.accept()

    def chosen_action(self):
        return self._chosen_action


class PendingCheckoutsDialog(QDialog):
    """
    בזמן סגירת התוכנה, אם יש פריטים CHECKED_OUT: להציג אותם ולתת בחירה,
    בלי להחזיר או למחוק אוטומטית (11.10).
    """

    RETURN_SELECTED, LEAVE_CHECKED_OUT, CANCEL = "return_selected", "leave", "cancel"

    def __init__(self, parent=None, pending_items=()):
        super().__init__(parent)
        self.setWindowTitle("Items still checked out")
        self._result_action = self.CANCEL

        info = QLabel(
            "The following items are still checked out to the workspace.\n"
            "They will NOT be returned or deleted automatically."
        )
        info.setWordWrap(True)

        self.list_widget = QListWidget()
        for item in pending_items:
            list_item = QListWidgetItem(f"{item[1]}  ({item[4]})")
            list_item.setData(Qt.UserRole, item[0])
            list_item.setCheckState(Qt.Checked)
            self.list_widget.addItem(list_item)

        return_button = QPushButton("Return selected items and exit")
        return_button.clicked.connect(self._return_selected)
        leave_button = QPushButton("Leave checked out and exit")
        leave_button.clicked.connect(self._leave_checked_out)
        cancel_button = QPushButton("Cancel (back to StorageArch)")
        cancel_button.clicked.connect(self._cancel)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.list_widget)
        layout.addWidget(return_button)
        layout.addWidget(leave_button)
        layout.addWidget(cancel_button)

    def _return_selected(self):
        self._result_action = self.RETURN_SELECTED
        self.accept()

    def _leave_checked_out(self):
        self._result_action = self.LEAVE_CHECKED_OUT
        self.accept()

    def _cancel(self):
        self._result_action = self.CANCEL
        self.reject()

    def result_action(self):
        return self._result_action

    def selected_item_ids(self):
        selected = []
        for row in range(self.list_widget.count()):
            list_item = self.list_widget.item(row)
            if list_item.checkState() == Qt.Checked:
                selected.append(list_item.data(Qt.UserRole))
        return selected


class FeedbackDialog(QDialog):
    """C7 - Cloud Feedback (plan §13). Category required; rating/text
    optional; diagnostics checkbox OFF by default and unchecked here means
    nothing extra is ever attached - this dialog never knows or cares
    whether Telemetry is on (plan §13.3: Feedback works either way)."""

    CATEGORIES = [
        ("Bug", "bug"),
        ("Performance", "performance"),
        ("Compression", "compression"),
        ("UI / UX", "ui_ux"),
        ("Other", "other"),
    ]
    MAX_TEXT_LENGTH = 2000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Send Feedback")
        self.resize(420, 320)

        self.category_combo = QComboBox()
        for label, value in self.CATEGORIES:
            self.category_combo.addItem(label, value)

        self.rating_spin = QSpinBox()
        self.rating_spin.setRange(0, 5)
        self.rating_spin.setSpecialValueText("(no rating)")

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("Optional - up to 2000 characters. Do not include filenames, paths, passwords, or personal information.")

        self.diagnostics_checkbox = QCheckBox("Include diagnostic information (Windows version, architecture)")
        self.diagnostics_checkbox.setChecked(False)

        form = QFormLayout()
        form.addRow("Category:", self.category_combo)
        form.addRow("Rating (optional):", self.rating_spin)
        form.addRow("Details (optional):", self.text_edit)
        form.addRow(self.diagnostics_checkbox)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_accept(self):
        if len(self.text_edit.toPlainText()) > self.MAX_TEXT_LENGTH:
            QMessageBox.warning(self, "Send Feedback", f"Details text is too long (max {self.MAX_TEXT_LENGTH} characters).")
            return
        self.accept()

    def values(self):
        return {
            "category": self.category_combo.currentData(),
            "rating": self.rating_spin.value() or None,
            "feedback_text": self.text_edit.toPlainText().strip() or None,
            "include_diagnostics": self.diagnostics_checkbox.isChecked(),
        }


class BetaCodeDialog(QDialog):
    """C5 - collects the shared Development Beta Code for registration
    (plan §15.3). The code itself is never stored in this repository or
    embedded in the app - the user types it in here each time it's needed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to StorageArch Cloud (Development Beta)")

        info = QLabel(
            "Enter the Development Beta Code to connect this installation to the "
            "StorageArch Cloud (Development). This is optional - StorageArch works "
            "fully offline without it."
        )
        info.setWordWrap(True)

        self.code_edit = QLineEdit()
        self.code_edit.setEchoMode(QLineEdit.Password)

        form = QFormLayout()
        form.addRow("Beta Code:", self.code_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def beta_code(self):
        return self.code_edit.text().strip()


def show_duplicate_info(parent, duplicate):
    QMessageBox.information(
        parent,
        "Duplicate content found",
        "This content already exists in storage - it was not saved again.\n\n"
        f"Existing item: {duplicate['item_name']}\n"
        f"Category: {duplicate['category']}\n"
        f"Archive: {duplicate['archive']}\n"
        f"Path in archive: {duplicate['path_in_archive']}\n"
        f"Archive status: {duplicate['status']}",
    )


def show_untrusted_duplicate_warning(parent, duplicate):
    QMessageBox.warning(
        parent,
        "Duplicate found but not trustworthy",
        "A database record with the same content already exists, but its archive\n"
        f"could not be verified (status: {duplicate['status']}).\n"
        "Archiving was stopped for safety instead of writing another copy.",
    )
