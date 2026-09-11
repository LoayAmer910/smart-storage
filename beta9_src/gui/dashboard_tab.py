from PySide6.QtWidgets import QGridLayout, QGroupBox, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

import main
from gui.format_utils import format_bytes
from gui.workers import run_in_background


class DashboardTab(QWidget):
    """8.1 - לוח ראשי: נתוני שימוש אמיתיים מתוך מסד הנתונים, ללא ערכים קבועים מראש."""

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self._thread = None
        self._worker = None

        self.categories_label = QLabel("-")
        self.items_label = QLabel("-")
        self.original_size_label = QLabel("-")
        self.compressed_size_label = QLabel("-")
        self.saved_label = QLabel("-")
        self.checked_out_label = QLabel("-")
        self.verified_label = QLabel("-")
        self.changed_label = QLabel("-")
        self.corrupted_label = QLabel("-")
        self.missing_label = QLabel("-")
        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #b02a2a; font-weight: bold;")

        stats_box = QGroupBox("Storage overview")
        stats_grid = QGridLayout(stats_box)
        rows = [
            ("Categories", self.categories_label),
            ("Items", self.items_label),
            ("Original size", self.original_size_label),
            ("Compressed size", self.compressed_size_label),
            ("Space saved", self.saved_label),
            ("Checked out", self.checked_out_label),
        ]
        for row_index, (caption, value_label) in enumerate(rows):
            stats_grid.addWidget(QLabel(caption + ":"), row_index, 0)
            stats_grid.addWidget(value_label, row_index, 1)

        integrity_box = QGroupBox("Archive integrity overview")
        integrity_grid = QGridLayout(integrity_box)
        integrity_rows = [
            ("VERIFIED", self.verified_label),
            ("CHANGED", self.changed_label),
            ("CORRUPTED", self.corrupted_label),
            ("MISSING", self.missing_label),
        ]
        for row_index, (caption, value_label) in enumerate(integrity_rows):
            integrity_grid.addWidget(QLabel(caption + ":"), row_index, 0)
            integrity_grid.addWidget(value_label, row_index, 1)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.verify_all_button = QPushButton("Verify all archives")
        self.verify_all_button.clicked.connect(self._verify_all)

        layout = QVBoxLayout(self)
        layout.addWidget(stats_box)
        layout.addWidget(integrity_box)
        layout.addWidget(self.warning_label)
        layout.addWidget(self.refresh_button)
        layout.addWidget(self.verify_all_button)
        layout.addStretch(1)

    def refresh(self):
        try:
            summary = main.dashboard_summary()
        except Exception as exc:  # noqa: BLE001 - dashboard must never crash the GUI on read failure
            QMessageBox.warning(self, "Dashboard", f"Could not load dashboard data:\n{exc}")
            return

        self.categories_label.setText(str(summary["category_count"]))
        self.items_label.setText(str(summary["item_count"]))
        self.original_size_label.setText(format_bytes(summary["total_original_size"]))
        self.compressed_size_label.setText(format_bytes(summary["total_compressed_size"]))
        self.saved_label.setText(format_bytes(summary["total_saved"]))
        self.checked_out_label.setText(str(summary["checked_out_count"]))

        counts = summary["integrity_counts"]
        self.verified_label.setText(str(counts.get("VERIFIED", 0)))
        self.changed_label.setText(str(counts.get("CHANGED", 0)))
        self.corrupted_label.setText(str(counts.get("CORRUPTED", 0)))
        self.missing_label.setText(str(counts.get("MISSING", 0)))

        attention = counts.get("CHANGED", 0) + counts.get("CORRUPTED", 0) + counts.get("MISSING", 0)
        if attention > 0:
            self.warning_label.setText(f"{attention} archive(s) need attention - run Verify to review.")
        else:
            self.warning_label.setText("")

    def _verify_all(self):
        if self.main_window.is_busy():
            return
        self.main_window.set_busy(True, "Verifying all archives...")
        self._thread, self._worker = run_in_background(
            self, main.verify_all, self._on_verify_done, self._on_verify_failed
        )

    def _on_verify_done(self, _results):
        self.main_window.set_busy(False)
        self.refresh()
        self.main_window.refresh_all()

    def _on_verify_failed(self, message):
        self.main_window.set_busy(False)
        QMessageBox.critical(self, "Verify all archives", f"Verification failed:\n{message}")
