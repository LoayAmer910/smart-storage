import os

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import main
from gui.dialogs import BetaCodeDialog
from gui.format_utils import ARCHIVE_SIZE_PRESETS


class SettingsTab(QWidget):
    """
    8.5 - הגדרות: מיקום אחסון/עבודה ניתן לשינוי מה-GUI (נשמר ב-storagearch_config.json
    ונטען מחדש בכל initialize), וגודל ZIP מרבי כברירת מחדל לקטגוריות חדשות.

    שינוי מיקום לעולם לא מעביר/מוחק קבצים קיימים: אם למיקום הנוכחי כבר יש נתונים,
    המשתמש מקבל אזהרה מפורשת לפני שהוא ממשיך (7.5/11.1 - אין הרס שקט של מידע).
    """

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window

        self.data_root_label = QLabel()
        self.workspace_root_label = QLabel()
        self.archives_path_label = QLabel()
        self.temp_path_label = QLabel()
        for label in (self.data_root_label, self.workspace_root_label, self.archives_path_label, self.temp_path_label):
            label.setWordWrap(True)

        change_data_button = QPushButton("Change data location...")
        change_data_button.clicked.connect(self._change_data_root)
        change_workspace_button = QPushButton("Change workspace location...")
        change_workspace_button.clicked.connect(self._change_workspace_root)
        reset_workspace_button = QPushButton("Reset workspace to default (inside data location)")
        reset_workspace_button.clicked.connect(self._reset_workspace_root)

        locations_box = QGroupBox("Storage locations")
        locations_form = QFormLayout()
        locations_form.addRow("Data/archives location:", self.data_root_label)
        locations_form.addRow("Archives folder:", self.archives_path_label)
        locations_form.addRow("Temp folder:", self.temp_path_label)
        locations_form.addRow("Workspace location:", self.workspace_root_label)
        locations_buttons = QHBoxLayout()
        locations_buttons.addWidget(change_data_button)
        locations_buttons.addWidget(change_workspace_button)
        locations_buttons.addWidget(reset_workspace_button)
        locations_layout = QVBoxLayout()
        locations_layout.addLayout(locations_form)
        locations_layout.addLayout(locations_buttons)
        locations_box.setLayout(locations_layout)

        self.default_size_combo = QComboBox()
        for label, value in ARCHIVE_SIZE_PRESETS:
            self.default_size_combo.addItem(label, value)
        save_size_button = QPushButton("Save default size")
        save_size_button.clicked.connect(self._save_default_size)

        size_box = QGroupBox("New category defaults")
        size_form = QFormLayout()
        size_form.addRow("Default max archive size:", self.default_size_combo)
        size_layout = QVBoxLayout()
        size_layout.addLayout(size_form)
        size_layout.addWidget(save_size_button)
        size_box.setLayout(size_layout)

        # C5-C7: Cloud (Development Beta) - entirely optional, OFF by
        # default, never required for normal local use. See
        # local_cloud/ and CLOUD_OPERATOR_GUIDE.md.
        self.cloud_status_label = QLabel()
        self.cloud_status_label.setWordWrap(True)

        self.telemetry_checkbox = QCheckBox("Send anonymous diagnostics to help improve StorageArch (Telemetry)")
        self.telemetry_checkbox.toggled.connect(self._on_telemetry_toggled)

        self.cloud_connect_button = QPushButton("Connect to Beta...")
        self.cloud_connect_button.clicked.connect(self._connect_cloud)
        self.cloud_disconnect_button = QPushButton("Disconnect")
        self.cloud_disconnect_button.clicked.connect(self._disconnect_cloud)
        feedback_button = QPushButton("Send Feedback...")
        feedback_button.clicked.connect(self._send_feedback)
        check_updates_button = QPushButton("Check for Updates")
        check_updates_button.clicked.connect(self._check_for_updates)

        cloud_buttons = QHBoxLayout()
        cloud_buttons.addWidget(self.cloud_connect_button)
        cloud_buttons.addWidget(self.cloud_disconnect_button)
        cloud_buttons.addWidget(feedback_button)
        cloud_buttons.addWidget(check_updates_button)

        cloud_box = QGroupBox("Cloud (Development Beta) - optional")
        cloud_layout = QVBoxLayout()
        cloud_layout.addWidget(self.cloud_status_label)
        cloud_layout.addWidget(self.telemetry_checkbox)
        cloud_layout.addLayout(cloud_buttons)
        cloud_box.setLayout(cloud_layout)

        layout = QVBoxLayout(self)
        layout.addWidget(locations_box)
        layout.addWidget(size_box)
        layout.addWidget(cloud_box)
        layout.addStretch(1)

        self.refresh()

    def refresh(self):
        locations = main.get_storage_locations()
        data_root = locations["data_root"]
        workspace_root = locations["workspace_root"] or os.path.join(data_root, "workspace")

        self.data_root_label.setText(os.path.abspath(data_root))
        self.archives_path_label.setText(os.path.abspath(os.path.join(data_root, "archives")))
        self.temp_path_label.setText(os.path.abspath(os.path.join(data_root, "temp")))
        self.workspace_root_label.setText(os.path.abspath(workspace_root))

        current_default = self.main_window.settings.get("default_max_archive_size")
        index = self.default_size_combo.findData(current_default)
        self.default_size_combo.setCurrentIndex(index if index >= 0 else 1)

        self._refresh_cloud_status()

    def _refresh_cloud_status(self):
        if not main.cloud_available():
            self.cloud_status_label.setText("Cloud is not available in this build.")
            for widget in (self.telemetry_checkbox, self.cloud_connect_button, self.cloud_disconnect_button):
                widget.setEnabled(False)
            return

        registered = main.cloud_is_registered()
        self.cloud_connect_button.setEnabled(not registered)
        self.cloud_disconnect_button.setEnabled(registered)
        self.telemetry_checkbox.setEnabled(registered)
        self.cloud_status_label.setText(
            "Connected to StorageArch Cloud (Development)." if registered
            else "Not connected. StorageArch works fully offline without connecting."
        )

        self.telemetry_checkbox.blockSignals(True)
        self.telemetry_checkbox.setChecked(main.cloud_telemetry_enabled())
        self.telemetry_checkbox.blockSignals(False)

    def _on_telemetry_toggled(self, checked):
        main.cloud_set_telemetry_enabled(checked)

    def _connect_cloud(self):
        dialog = BetaCodeDialog(self)
        if dialog.exec() != BetaCodeDialog.Accepted:
            return
        code = dialog.beta_code()
        if not code:
            return
        success, error = main.cloud_register(code)
        if not success:
            QMessageBox.warning(self, "Connect to Beta", f"Could not connect: {error or 'unknown error'}")
            return
        QMessageBox.information(self, "Connect to Beta", "Connected. Telemetry remains OFF until you enable it below.")
        self._refresh_cloud_status()

    def _disconnect_cloud(self):
        confirmation = QMessageBox.warning(
            self,
            "Disconnect",
            "This clears the local Cloud connection and turns Telemetry off. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmation != QMessageBox.Yes:
            return
        main.cloud_set_telemetry_enabled(False)
        main.cloud_disconnect()
        self._refresh_cloud_status()

    def _send_feedback(self):
        from gui.dialogs import FeedbackDialog

        registered = main.cloud_available() and main.cloud_is_registered()
        beta_code = None
        if not registered:
            if not main.cloud_available():
                QMessageBox.information(self, "Send Feedback", "Cloud is not available in this build.")
                return
            code_dialog = BetaCodeDialog(self)
            code_dialog.setWindowTitle("Beta Code required to send Feedback")
            if code_dialog.exec() != BetaCodeDialog.Accepted:
                return
            beta_code = code_dialog.beta_code()
            if not beta_code:
                return

        dialog = FeedbackDialog(self)
        if dialog.exec() != FeedbackDialog.Accepted:
            return
        values = dialog.values()
        success, error = main.cloud_submit_feedback(
            values["category"], rating=values["rating"], feedback_text=values["feedback_text"],
            include_diagnostics=values["include_diagnostics"], beta_code=beta_code,
        )
        if success:
            QMessageBox.information(self, "Send Feedback", "Thank you - your feedback has been queued to send.")
        else:
            QMessageBox.warning(self, "Send Feedback", f"Could not queue feedback: {error or 'unknown error'}")

    def _check_for_updates(self):
        if not main.cloud_available():
            QMessageBox.information(self, "Check for Updates", "Cloud is not available in this build.")
            return
        release = main.cloud_check_for_updates()
        if release is None:
            QMessageBox.information(self, "Check for Updates", "No release information is available right now (offline, or nothing published yet).")
            return
        severity_note = " (CRITICAL)" if release.get("severity") == "critical" else ""
        QMessageBox.information(
            self,
            "Check for Updates",
            f"Latest Development release: {release.get('app_version')} (build {release.get('build_version')}){severity_note}\n\n"
            "This is a notification only - StorageArch never downloads or installs updates automatically.",
        )

    def _save_default_size(self):
        self.main_window.settings["default_max_archive_size"] = self.default_size_combo.currentData()
        self.main_window.save_settings()
        QMessageBox.information(self, "Settings", "Default archive size saved.")

    def _current_location_has_data(self):
        summary = main.dashboard_summary()
        return summary["category_count"] > 0 or summary["item_count"] > 0

    def _change_data_root(self):
        if self.main_window.is_busy():
            return
        chosen = QFileDialog.getExistingDirectory(self, "Choose data/archives location")
        if not chosen:
            return

        if main.is_protected_path(chosen):
            QMessageBox.warning(self, "Change data location", "This location is protected and cannot be used.")
            return

        if self._current_location_has_data():
            confirmation = QMessageBox.warning(
                self,
                "Change data location",
                "The current location already has categories/items in it.\n\n"
                "Switching will NOT move, copy, or delete anything - your existing data "
                "stays exactly where it is on disk. StorageArch will simply start operating "
                "against the new location, which may start empty. You can switch back at any "
                "time to regain access to the current data.\n\n"
                "Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirmation != QMessageBox.Yes:
                return

        if not main.set_storage_location(data_root=chosen):
            QMessageBox.warning(self, "Change data location", "Could not switch to the selected location.")
            return

        QMessageBox.information(self, "Change data location", "Data location updated.")
        self.refresh()
        self.main_window.refresh_all()

    def _change_workspace_root(self):
        if self.main_window.is_busy():
            return

        pending = main.items_pending_return()
        if pending:
            QMessageBox.warning(
                self,
                "Change workspace location",
                f"{len(pending)} item(s) are still checked out to the current workspace.\n"
                "Check them in first before changing the workspace location, so their "
                "checked-out copies are not left behind in an unreachable folder.",
            )
            return

        chosen = QFileDialog.getExistingDirectory(self, "Choose workspace location")
        if not chosen:
            return

        if main.is_protected_path(chosen):
            QMessageBox.warning(self, "Change workspace location", "This location is protected and cannot be used.")
            return

        if not main.set_storage_location(workspace_root=chosen):
            QMessageBox.warning(self, "Change workspace location", "Could not switch to the selected location.")
            return

        QMessageBox.information(self, "Change workspace location", "Workspace location updated.")
        self.refresh()
        self.main_window.refresh_all()

    def _reset_workspace_root(self):
        if self.main_window.is_busy():
            return
        pending = main.items_pending_return()
        if pending:
            QMessageBox.warning(
                self,
                "Reset workspace location",
                f"{len(pending)} item(s) are still checked out to the current workspace.\n"
                "Check them in first before resetting the workspace location.",
            )
            return

        if not main.reset_workspace_location():
            QMessageBox.warning(self, "Reset workspace location", "Could not reset the workspace location.")
            return

        QMessageBox.information(self, "Reset workspace location", "Workspace location reset to the default.")
        self.refresh()
        self.main_window.refresh_all()
