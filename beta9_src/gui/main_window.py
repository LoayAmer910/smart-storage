from PySide6.QtWidgets import QMainWindow, QMessageBox, QTabWidget

import main
from gui import settings as settings_module
from gui.categories_tab import CategoriesTab
from gui.dashboard_tab import DashboardTab
from gui.dialogs import PendingCheckoutsDialog
from gui.items_tab import ItemsTab
from gui.settings_tab import SettingsTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("StorageArch")
        self.resize(1000, 640)

        self.settings = settings_module.load_settings()
        self._busy = False
        self._closing_in_progress = False

        self.dashboard_tab = DashboardTab(self)
        self.categories_tab = CategoriesTab(self)
        self.items_tab = ItemsTab(self)
        self.settings_tab = SettingsTab(self)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.dashboard_tab, "Dashboard")
        self.tabs.addTab(self.categories_tab, "Categories")
        self.tabs.addTab(self.items_tab, "Items")
        self.tabs.addTab(self.settings_tab, "Settings")
        self.setCentralWidget(self.tabs)

        self.statusBar().showMessage("Ready")

        self.refresh_all()

    # ------------------------------------------------------------- helpers

    def save_settings(self):
        settings_module.save_settings(self.settings)

    def refresh_all(self):
        self.dashboard_tab.refresh()
        self.categories_tab.refresh()
        self.items_tab.refresh()

    def open_items_for_category(self, category_name):
        self.tabs.setCurrentWidget(self.items_tab)
        self.items_tab.filter_by_category(category_name)

    def is_busy(self):
        return self._busy

    def set_busy(self, busy, message=None):
        self._busy = busy
        self.tabs.setEnabled(not busy)
        self.statusBar().showMessage(message if busy else "Ready")

    # --------------------------------------------------------------- close

    def closeEvent(self, event):
        if self._closing_in_progress:
            event.accept()
            return

        if self.is_busy():
            QMessageBox.information(
                self, "StorageArch", "An operation is still running. Please wait for it to finish before closing."
            )
            event.ignore()
            return

        try:
            pending = main.items_pending_return()
        except Exception:  # noqa: BLE001 - never block shutdown on a read failure
            pending = []

        if not pending:
            event.accept()
            return

        dialog = PendingCheckoutsDialog(self, pending_items=pending)
        if dialog.exec() != PendingCheckoutsDialog.Accepted:
            event.ignore()
            return

        action = dialog.result_action()
        if action == PendingCheckoutsDialog.LEAVE_CHECKED_OUT:
            event.accept()
            return

        if action == PendingCheckoutsDialog.RETURN_SELECTED:
            failures = []
            for item_id in dialog.selected_item_ids():
                try:
                    if not main.checkin(item_id):
                        failures.append(item_id)
                except Exception:  # noqa: BLE001
                    failures.append(item_id)
            if failures:
                QMessageBox.warning(
                    self,
                    "StorageArch",
                    f"{len(failures)} item(s) could not be returned and remain checked out.",
                )
            self._closing_in_progress = True
            event.accept()
            return

        event.ignore()
