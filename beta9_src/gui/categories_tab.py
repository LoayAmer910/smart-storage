from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

import main
from gui.dialogs import CreateCategoryDialog
from gui.format_utils import format_bytes

COLUMNS = ("Name", "Description", "Color", "Max archive size", "Items", "Created")
CATEGORY_ID_ROLE = Qt.UserRole


class CategoriesTab(QWidget):
    """8.2 - מסך קטגוריות: צפייה, יצירה, עריכה, מחיקה, פתיחת רשימת הפריטים."""

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

        self.new_category_button = QPushButton("New category...")
        self.new_category_button.clicked.connect(self._create_category)
        self.edit_category_button = QPushButton("Edit category...")
        self.edit_category_button.clicked.connect(self._edit_category)
        self.delete_category_button = QPushButton("Delete category")
        self.delete_category_button.clicked.connect(self._delete_category)
        self.open_items_button = QPushButton("Open items")
        self.open_items_button.clicked.connect(self._open_items)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)

        button_row = QHBoxLayout()
        button_row.addWidget(self.new_category_button)
        button_row.addWidget(self.edit_category_button)
        button_row.addWidget(self.delete_category_button)
        button_row.addWidget(self.open_items_button)
        button_row.addWidget(self.refresh_button)
        button_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(button_row)
        layout.addWidget(self.table)

    def refresh(self):
        try:
            categories = main.list_categories()
            archives = main.list_archives()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Categories", f"Could not load categories:\n{exc}")
            return

        item_counts = {}
        for archive in archives:
            category_name = archive[1]
            item_counts[category_name] = item_counts.get(category_name, 0) + archive[5]

        self.table.setRowCount(len(categories))
        for row_index, category in enumerate(categories):
            category_id, name, description, color, max_size, created_at, _folder_name = category
            values = [
                name,
                description or "",
                color or "",
                format_bytes(max_size),
                str(item_counts.get(name, 0)),
                created_at or "",
            ]
            for col_index, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(CATEGORY_ID_ROLE, category_id)
                self.table.setItem(row_index, col_index, cell)

    def _selected_category_name(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).text()

    def _selected_category_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).data(CATEGORY_ID_ROLE)

    def _create_category(self):
        if self.main_window.is_busy():
            return
        existing_names = {self.table.item(row, 0).text() for row in range(self.table.rowCount())}
        default_size = self.main_window.settings.get("default_max_archive_size")
        dialog = CreateCategoryDialog(self, default_max_size=default_size, title="New Category")
        if dialog.exec() != CreateCategoryDialog.Accepted:
            return

        values = dialog.values()
        if not values["name"]:
            QMessageBox.warning(self, "New category", "Category name cannot be empty.")
            return
        if values["name"] in existing_names:
            QMessageBox.warning(self, "New category", f"A category named '{values['name']}' already exists.")
            return

        try:
            created = main.create_category(
                values["name"], values["description"], values["color"], values["max_archive_size"]
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "New category", f"Could not create category:\n{exc}")
            return

        if not created:
            QMessageBox.warning(self, "New category", "Category could not be created.")
            return

        QMessageBox.information(self, "New category", f"Category '{values['name']}' created.")
        self.main_window.refresh_all()

    def _edit_category(self):
        if self.main_window.is_busy():
            return
        category_id = self._selected_category_id()
        if category_id is None:
            QMessageBox.information(self, "Edit category", "Select a category first.")
            return

        category = main.get_category(category_id)
        if category is None:
            QMessageBox.warning(self, "Edit category", "Category no longer exists.")
            self.main_window.refresh_all()
            return

        current_values = {
            "name": category[1],
            "description": category[2],
            "color": category[3],
            "max_archive_size": category[4],
        }
        existing_names = {
            self.table.item(row, 0).text()
            for row in range(self.table.rowCount())
            if self.table.item(row, 0).data(CATEGORY_ID_ROLE) != category_id
        }

        dialog = CreateCategoryDialog(self, title="Edit Category", initial_values=current_values)
        if dialog.exec() != CreateCategoryDialog.Accepted:
            return

        values = dialog.values()
        if not values["name"]:
            QMessageBox.warning(self, "Edit category", "Category name cannot be empty.")
            return
        if values["name"] in existing_names:
            QMessageBox.warning(self, "Edit category", f"A category named '{values['name']}' already exists.")
            return

        try:
            updated = main.update_category(
                category_id, values["name"], values["description"], values["color"], values["max_archive_size"]
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Edit category", f"Could not update category:\n{exc}")
            return

        if not updated:
            QMessageBox.warning(self, "Edit category", "Category could not be updated.")
            return

        QMessageBox.information(self, "Edit category", f"Category updated to '{values['name']}'.")
        self.main_window.refresh_all()

    def _delete_category(self):
        if self.main_window.is_busy():
            return
        category_id = self._selected_category_id()
        category_name = self._selected_category_name()
        if category_id is None:
            QMessageBox.information(self, "Delete category", "Select a category first.")
            return

        confirmation = QMessageBox.warning(
            self,
            "Delete category",
            f"Delete category '{category_name}'? This only succeeds if the category has no archived items.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmation != QMessageBox.Yes:
            return

        try:
            result = main.delete_category(category_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Delete category", f"Could not delete category:\n{exc}")
            return

        if result == "DELETED":
            QMessageBox.information(self, "Delete category", f"Category '{category_name}' deleted.")
        elif result == "NOT_EMPTY":
            QMessageBox.warning(
                self,
                "Delete category",
                f"Category '{category_name}' contains archived items and cannot be deleted.\n"
                "Deleting archived data is not supported - this action was refused to protect your data.",
            )
        else:
            QMessageBox.warning(self, "Delete category", "Category no longer exists.")

        self.main_window.refresh_all()

    def _open_items(self):
        category_name = self._selected_category_name()
        if not category_name:
            QMessageBox.information(self, "Open items", "Select a category first.")
            return
        self.main_window.open_items_for_category(category_name)
