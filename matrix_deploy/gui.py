"""Qt GUI for Matrix Deploy.

Keeps UI concerns only; all deployment/download logic lives in the worker
threads and the Qt-free service modules.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, QPoint
from PyQt5.QtGui import QColor, QPainter, QPixmap, QPolygon, QTextCursor
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .artifactory import ArtifactoryCredentials
from .config import AppConfig, Room
from .deployer import DeploymentCredentials
from .env_settings import load_env_secrets, load_env_settings
from .workers import DeploymentWorker, DownloadWorker, SystemActionWorker

SETTINGS_FILE = Path.home() / ".matrix_deploy_settings.json"
CACHE_DIR = Path.home() / "Desktop" / "latest-matrix-wrynose"
DOWNLOADS_DIR = Path.home() / "Downloads"

LEVEL_COLORS = {
    "error": "#ff6b6b",
    "success": "#51cf66",
    "warning": "#ffd43b",
    "info": "#22b8cf",
    "detail": "#868e96",
}

STATUS_BADGES = {
    "pending": ("PENDING", "#868e96"),
    "running": ("RUNNING", "#ffd43b"),
    "success": ("SUCCESS", "#51cf66"),
    "failed": ("FAILED", "#ff6b6b"),
    "cancelled": ("CANCELLED", "#868e96"),
}

# Semantic button palette, grouped by utility:
#   primary  -> run/deploy (go)         danger   -> destructive (cancel/reboot)
#   service  -> service restarts        bandwidth-> config push (blue family)
#   neutral  -> read-only diagnostics
BTN_COLORS = {
    "primary": "#2E7D32",
    "danger": "#C62828",
    "service": "#EF6C00",
    "bandwidth_strong": "#1565C0",
    "bandwidth_soft": "#5C6BC0",
    "neutral": "#455A64",
    "info": "#1976D2",
    "utility": "#607D8B",
}


def _shade(hex_color: str, factor: float) -> str:
    """Lighten (factor>1) or darken (factor<1) a #RRGGBB color."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    r, g, b = (max(0, min(255, int(c * factor))) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _button_style(bg: str) -> str:
    """Polished button with subtle top-to-bottom gradient, thin border and soft
    rounded corners. Gives gentle depth (glossy top highlight, slight press-in)
    without the harsh Win95 bevel."""
    top = _shade(bg, 1.18)
    bottom = _shade(bg, 0.9)
    edge = _shade(bg, 0.68)
    return (
        "QPushButton {"
        f"color:white; font-size:13px; font-weight:600; padding:7px 14px; min-height:20px;"
        f"border:1px solid {edge}; border-radius:5px;"
        f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {top},stop:1 {bottom});"
        "}"
        "QPushButton:hover {"
        f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {_shade(bg, 1.28)},stop:1 {bg});"
        "}"
        "QPushButton:pressed {"
        f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {bottom},stop:1 {_shade(bg, 0.8)});"
        f"padding-top:8px; padding-bottom:6px;"
        "}"
        "QPushButton:disabled {"
        "color:#ECEFF1; border:1px solid #90A4AE;"
        "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #B8C2C8,stop:1 #9EAAB0);"
        "}"
    )


def _make_chevron_icon(color: str) -> str:
    """Render a small down-chevron to a cached PNG and return its file path.

    QSS's CSS-border triangle trick doesn't render reliably under the Fusion
    style on all Qt builds, so we draw a real icon once (per color) and
    reference it via ``image: url(...)`` instead.
    """
    safe_name = color.lstrip("#")
    path = Path(tempfile.gettempdir()) / f"matrix_deploy_chevron_{safe_name}.png"
    if not path.exists():
        pixmap = QPixmap(12, 8)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawPolygon(QPolygon([QPoint(1, 1), QPoint(11, 1), QPoint(6, 7)]))
        painter.end()
        pixmap.save(str(path), "PNG")
    return str(path).replace("\\", "/")


def build_app_stylesheet() -> str:
    """Application-wide look: neutral light surface, cohesive inputs/combos/
    frames that match the polished buttons. Built lazily (after QApplication
    exists) so the combo-box chevron icon can be rendered with QPainter."""
    chevron = _make_chevron_icon("#455A64")
    chevron_open = _make_chevron_icon("#1976D2")
    css = """
QWidget {
    background-color: #ECEFF1;
    color: #263238;
    font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
    font-size: 13px;
}
QLineEdit {
    background: #FFFFFF;
    border: 1px solid #B0BEC5;
    border-radius: 5px;
    padding: 6px 8px;
    selection-background-color: #90CAF9;
}
QLineEdit:focus { border: 1px solid #1976D2; }
QLineEdit:disabled { background: #ECEFF1; color: #90A4AE; }
QComboBox {
    background: #FFFFFF;
    border: 1px solid #B0BEC5;
    border-radius: 5px;
    padding: 5px 8px;
    padding-right: 30px;
    min-height: 20px;
}
QComboBox:focus { border: 1px solid #1976D2; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 26px;
    border-left: 1px solid #B0BEC5;
    border-top-right-radius: 5px;
    border-bottom-right-radius: 5px;
    background: #E3E7EA;
}
QComboBox::drop-down:hover { background: #CFD8DC; }
QComboBox::down-arrow {
    image: url(__CHEVRON__);
    width: 12px;
    height: 8px;
}
QComboBox::down-arrow:on { image: url(__CHEVRON_OPEN__); }
QComboBox QAbstractItemView {
    background: #FFFFFF;
    border: 1px solid #B0BEC5;
    border-radius: 5px;
    padding: 2px;
    selection-background-color: #1976D2;
    selection-color: white;
    outline: none;
}
QGroupBox {
    background: #F5F7F8;
    border: 1px solid #CFD8DC;
    border-radius: 8px;
    margin-top: 14px;
    padding: 10px 10px 10px 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: #37474F;
}
QCheckBox { spacing: 6px; }
QScrollArea { border: 1px solid #CFD8DC; border-radius: 6px; background: #FFFFFF; }
QProgressBar {
    border: 1px solid #B0BEC5;
    border-radius: 4px;
    background: #FFFFFF;
    text-align: center;
    height: 14px;
}
QProgressBar::chunk { background-color: #1976D2; border-radius: 3px; }
QStatusBar { background: #CFD8DC; color: #37474F; }
QToolTip {
    background: #37474F; color: white; border: none; padding: 4px 6px;
}
"""
    return css.replace("__CHEVRON__", chevron).replace("__CHEVRON_OPEN__", chevron_open)


SECTION_LABEL_STYLE = (
    "font-size:15px; font-weight:700; color:#1565C0;"
    "border-bottom:2px solid #90CAF9; padding-bottom:3px;"
)


class MatrixDeployWindow(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.deploy_worker: Optional[DeploymentWorker] = None
        self.download_worker: Optional[DownloadWorker] = None
        self.system_action_worker: Optional[SystemActionWorker] = None
        self.room_checkboxes: Dict[int, QCheckBox] = {}
        self.room_status_labels: Dict[int, QLabel] = {}
        self.room_progress_bars: Dict[int, QProgressBar] = {}

        self._last_action = "none"

        self.setWindowTitle("Matrix Deploy")
        self.setGeometry(80, 60, 1320, 860)
        self.setMinimumSize(1120, 680)
        self.setStyleSheet(build_app_stylesheet())
        self._build_ui()
        self._load_settings()
        self._update_status_bar()

    # -- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 10)
        root.setSpacing(12)

        root.addWidget(self._build_connection_group())
        root.addWidget(self._build_files_group())
        root.addLayout(self._build_options_row())
        root.addLayout(self._build_action_row())
        root.addLayout(self._build_controls_row())

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        # Operating rooms and terminal sit side by side.
        split = QHBoxLayout()
        split.setSpacing(12)
        split.addWidget(self._build_rooms_group(), stretch=2)
        split.addWidget(self._build_terminal_group(), stretch=3)
        root.addLayout(split, stretch=5)

    @staticmethod
    def _password_toggle(line_edit: QLineEdit) -> QToolButton:
        """A small show/hide toggle that flips a password field's echo mode."""
        btn = QToolButton()
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setText("Show")
        btn.setToolTip("Show / hide")
        btn.setFixedWidth(52)
        btn.setStyleSheet(
            "QToolButton { background:#CFD8DC; color:#37474F; border:1px solid #B0BEC5;"
            "border-radius:5px; padding:5px 4px; font-size:11px; font-weight:600; }"
            "QToolButton:hover { background:#B0BEC5; }"
            "QToolButton:checked { background:#1976D2; color:white; border-color:#1565C0; }"
        )

        def _toggle(checked: bool) -> None:
            line_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
            btn.setText("Hide" if checked else "Show")

        btn.toggled.connect(_toggle)
        return btn

    def _build_connection_group(self) -> QGroupBox:
        group = QGroupBox("Connection Settings")
        layout = QGridLayout()
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)
        layout.setColumnStretch(1, 1)
        conn = self.config.connection

        layout.addWidget(QLabel("Router IP:"), 0, 0)
        self.router_ip_input = QLineEdit(conn.router_ip)
        layout.addWidget(self.router_ip_input, 0, 1)

        layout.addWidget(QLabel("SSH Username:"), 1, 0)
        self.username_input = QLineEdit(conn.ssh_username)
        layout.addWidget(self.username_input, 1, 1)

        layout.addWidget(QLabel("SSH Password:"), 2, 0)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setPlaceholderText("Leave empty if using SSH keys")
        layout.addWidget(self.password_input, 2, 1)
        layout.addWidget(self._password_toggle(self.password_input), 2, 2)

        layout.addWidget(QLabel("Sudo Password:"), 3, 0)
        self.sudo_password_input = QLineEdit()
        self.sudo_password_input.setEchoMode(QLineEdit.Password)
        self.sudo_password_input.setPlaceholderText("Required for config deployment")
        layout.addWidget(self.sudo_password_input, 3, 1)
        layout.addWidget(self._password_toggle(self.sudo_password_input), 3, 2)

        layout.addWidget(QLabel("Artifactory Email:"), 4, 0)
        self.artifactory_email_input = QLineEdit()
        self.artifactory_email_input.setPlaceholderText("your.email@arthrex.com")
        layout.addWidget(self.artifactory_email_input, 4, 1)

        layout.addWidget(QLabel("Artifactory Token:"), 5, 0)
        self.artifactory_token_input = QLineEdit()
        self.artifactory_token_input.setEchoMode(QLineEdit.Password)
        self.artifactory_token_input.setPlaceholderText("API token (not saved to disk)")
        layout.addWidget(self.artifactory_token_input, 5, 1)
        layout.addWidget(self._password_toggle(self.artifactory_token_input), 5, 2)

        group.setLayout(layout)
        return group

    def _build_files_group(self) -> QGroupBox:
        group = QGroupBox("Files")
        layout = QGridLayout()
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)
        layout.setColumnStretch(1, 1)

        layout.addWidget(QLabel("SWU File:"), 0, 0)
        self.swu_file_input = QLineEdit()
        self.swu_file_input.setPlaceholderText("Select or download an SWU file...")
        layout.addWidget(self.swu_file_input, 0, 1)
        swu_browse = self._make_button("Browse...", "utility", icon=QStyle.SP_DirOpenIcon)
        swu_browse.clicked.connect(self._browse_swu)
        layout.addWidget(swu_browse, 0, 2)
        self.download_btn = self._make_button(
            "Download Latest", "info", icon=QStyle.SP_ArrowDown
        )
        self.download_btn.clicked.connect(self._download_latest)
        layout.addWidget(self.download_btn, 0, 3)

        layout.addWidget(QLabel("Config Template:"), 1, 0)
        self.config_file_input = QLineEdit()
        self.config_file_input.setPlaceholderText("Select base config JSON template...")
        layout.addWidget(self.config_file_input, 1, 1)
        cfg_browse = self._make_button("Browse...", "utility", icon=QStyle.SP_DirOpenIcon)
        cfg_browse.clicked.connect(self._browse_config)
        layout.addWidget(cfg_browse, 1, 2)

        group.setLayout(layout)
        return group

    def _build_rooms_group(self) -> QGroupBox:
        group = QGroupBox("Operating Rooms")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(240)
        inner = QWidget()
        rows = QVBoxLayout(inner)
        rows.setSpacing(4)

        for index, room in enumerate(self.config.rooms):
            rows.addWidget(self._build_room_row(room, index))

        rows.addStretch()
        scroll.setWidget(inner)

        select_all = self._make_button(
            "Select All", "utility", icon=QStyle.SP_DialogApplyButton
        )
        select_all.clicked.connect(lambda: self._set_all_rooms(True))
        clear_all = self._make_button(
            "Clear All", "utility", icon=QStyle.SP_DialogResetButton
        )
        clear_all.clicked.connect(lambda: self._set_all_rooms(False))

        btn_row = QHBoxLayout()
        btn_row.addWidget(select_all)
        btn_row.addWidget(clear_all)
        btn_row.addStretch()

        outer = QVBoxLayout()
        outer.addWidget(scroll)
        outer.addLayout(btn_row)
        group.setLayout(outer)
        return group

    def _build_room_row(self, room: Room, index: int = 0) -> QWidget:
        """A single row: checkbox + status badge + per-room progress bar."""
        cb = QCheckBox(room.name)
        cb.setMinimumWidth(70)
        cb.stateChanged.connect(lambda _s: self._update_status_bar())
        self.room_checkboxes[room.number] = cb

        badge = QLabel("")
        badge.setAlignment(Qt.AlignCenter)
        badge.setMinimumWidth(85)
        badge.setStyleSheet("background:transparent;")
        self.room_status_labels[room.number] = badge

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(14)
        self.room_progress_bars[room.number] = bar

        row = QHBoxLayout()
        row.setContentsMargins(8, 4, 8, 4)
        row.addWidget(cb)
        row.addWidget(badge)
        row.addWidget(bar, stretch=1)

        wrapper = QWidget()
        wrapper.setLayout(row)
        # Zebra striping for quick scanning across 12 rooms.
        stripe = "#FFFFFF" if index % 2 == 0 else "#F0F3F5"
        wrapper.setStyleSheet(
            f"background:{stripe}; border-bottom:1px solid #ECEFF1;"
        )
        return wrapper

    def _build_options_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Operation:"))
        self.operation_combo = QComboBox()
        self.operation_combo.addItems(
            ["SWU Update Only", "Config Update Only", "Both (SWU + Config)"]
        )
        self.operation_combo.setCurrentIndex(2)
        self.operation_combo.setMinimumWidth(190)
        row.addWidget(self.operation_combo)

        self.sequential_checkbox = QCheckBox("Deploy sequentially (recommended)")
        self.sequential_checkbox.setChecked(True)
        self.sequential_checkbox.setToolTip(
            "All rooms share one physical host; sequential avoids /tmp and reboot contention."
        )
        row.addWidget(self.sequential_checkbox)
        row.addStretch()
        return row

    def _make_button(
        self, text: str, kind: str, tooltip: str = "", icon=None
    ) -> QPushButton:
        """Create a consistently styled, color-coded button. ``kind`` is a key
        in ``BTN_COLORS`` grouping the button by its utility. ``icon`` is an
        optional ``QStyle.StandardPixmap`` shown before the label."""
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(_button_style(BTN_COLORS[kind]))
        if icon is not None:
            btn.setIcon(self.style().standardIcon(icon))
        if tooltip:
            btn.setToolTip(tooltip)
        return btn

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.deploy_btn = self._make_button(
            "Start Deployment", "primary", icon=QStyle.SP_MediaPlay
        )
        self.deploy_btn.setStyleSheet(
            _button_style(BTN_COLORS["primary"]) + "QPushButton { font-size:14px; padding:10px; }"
        )
        self.deploy_btn.clicked.connect(self._start_deployment)
        row.addWidget(self.deploy_btn, stretch=3)

        self.cancel_btn = self._make_button(
            "Cancel", "danger", icon=QStyle.SP_DialogCancelButton
        )
        self.cancel_btn.setStyleSheet(
            _button_style(BTN_COLORS["danger"]) + "QPushButton { font-size:14px; padding:10px; }"
        )
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_deployment)
        row.addWidget(self.cancel_btn, stretch=1)
        return row

    @staticmethod
    def _button_group(title: str, buttons: List[QPushButton]) -> QGroupBox:
        """Wrap a set of related buttons in a titled frame (inherits global
        QGroupBox styling). Buttons stretch equally to fill the frame."""
        box = QGroupBox(title)
        inner = QHBoxLayout()
        inner.setContentsMargins(8, 6, 8, 8)
        inner.setSpacing(6)
        for btn in buttons:
            inner.addWidget(btn, stretch=1)
        box.setLayout(inner)
        return box

    def _build_controls_row(self) -> QHBoxLayout:
        # --- Services (restarts / reboot) --------------------------------
        self.restart_service_btn = self._make_button(
            "Restart Service", "service", "Restart the matrix-api service on selected rooms",
            icon=QStyle.SP_BrowserReload,
        )
        self.restart_service_btn.clicked.connect(self._restart_service)

        self.restart_nms_btn = self._make_button(
            "Restart NMS", "service", "Restart the barco-nms service on selected rooms",
            icon=QStyle.SP_BrowserReload,
        )
        self.restart_nms_btn.clicked.connect(self._restart_nms_service)

        self.reboot_btn = self._make_button(
            "Reboot", "danger", "Reboot selected rooms", icon=QStyle.SP_ComputerIcon
        )
        self.reboot_btn.clicked.connect(self._reboot)

        services_box = self._button_group(
            "Services", [self.restart_service_btn, self.restart_nms_btn, self.reboot_btn]
        )

        # --- Bandwidth (config pushes) -----------------------------------
        self.bandwidth_max_btn = self._make_button(
            "Bandwidth: MAX",
            "bandwidth_strong",
            "Push the golden nms-config.json with videoSourceSharing.bandwidth = MAX",
        )
        self.bandwidth_max_btn.clicked.connect(lambda: self._set_nms_bandwidth("MAX"))

        self.bandwidth_limited_btn = self._make_button(
            "Bandwidth: LIMITED",
            "bandwidth_soft",
            "Push the golden nms-config.json with videoSourceSharing.bandwidth = LIMITED",
        )
        self.bandwidth_limited_btn.clicked.connect(lambda: self._set_nms_bandwidth("LIMITED"))

        self.link_bw_low_btn = self._make_button(
            "Interop BW: 50000",
            "bandwidth_soft",
            "Push application-user.yml with interor.bandwidth upload/download = 50000 and restart barco-nms",
        )
        self.link_bw_low_btn.clicked.connect(lambda: self._set_nms_link_bandwidth(50000))

        self.link_bw_high_btn = self._make_button(
            "Interop BW: 500000",
            "bandwidth_strong",
            "Push application-user.yml with interor.bandwidth upload/download = 500000 and restart barco-nms",
        )
        self.link_bw_high_btn.clicked.connect(lambda: self._set_nms_link_bandwidth(500000))

        bandwidth_box = self._button_group(
            "Bandwidth",
            [
                self.bandwidth_max_btn,
                self.link_bw_high_btn,
                self.bandwidth_limited_btn,
                self.link_bw_low_btn,
            ],
        )

        # --- Diagnostics (read-only) -------------------------------------
        self.get_logs_btn = self._make_button(
            "Get Logs",
            "neutral",
            "Download matrix-api and barco-nms logs for selected rooms to your Downloads folder",
            icon=QStyle.SP_DialogSaveButton,
        )
        self.get_logs_btn.clicked.connect(self._get_logs)

        self.view_config_btn = self._make_button(
            "View matrix.api.config",
            "neutral",
            "Print matrix.api.config.json for selected rooms in the output terminal "
            "and save a raw copy of each to your Downloads folder",
            icon=QStyle.SP_FileDialogContentsView,
        )
        self.view_config_btn.clicked.connect(self._view_config)

        self.get_nms_password_btn = self._make_button(
            "Get NMS Password",
            "neutral",
            "Run 'sudo act-mfg-eeprom display' and show the default NMS password",
            icon=QStyle.SP_MessageBoxInformation,
        )
        self.get_nms_password_btn.clicked.connect(self._get_nms_password)

        diagnostics_box = self._button_group(
            "Diagnostics",
            [self.get_logs_btn, self.view_config_btn, self.get_nms_password_btn],
        )

        # Proportional stretch (by button count) keeps every button ~equal width.
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(services_box, stretch=3)
        row.addWidget(bandwidth_box, stretch=4)
        row.addWidget(diagnostics_box, stretch=3)
        return row

    def _build_terminal_group(self) -> QGroupBox:
        group = QGroupBox("Output Terminal")
        layout = QVBoxLayout()

        # Toolbar sits above the console so it never overlaps the output.
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        copy_btn = self._make_button("Copy", "utility", icon=QStyle.SP_FileDialogDetailedView)
        copy_btn.clicked.connect(self._copy_terminal)
        clear = self._make_button("Clear Output", "utility", icon=QStyle.SP_DialogResetButton)
        clear.clicked.connect(self.terminal_clear)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(clear)
        layout.addLayout(btn_row)

        self.terminal = QTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.setMinimumHeight(240)
        self.terminal.setLineWrapMode(QTextEdit.WidgetWidth)
        self.terminal.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.terminal.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.terminal.setStyleSheet(
            "QTextEdit { background-color:#1e1e1e; color:#d4d4d4;"
            "border:1px solid #37474F; border-radius:6px; padding:6px;"
            "font-family:'Consolas','Courier New',monospace; }"
            "QScrollBar:vertical { background:#1e1e1e; width:12px; margin:0; }"
            "QScrollBar::handle:vertical { background:#555b62; border-radius:6px; min-height:24px; }"
            "QScrollBar::handle:vertical:hover { background:#6b7280; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )
        layout.addWidget(self.terminal, stretch=1)

        group.setLayout(layout)
        return group

    def terminal_clear(self) -> None:
        self.terminal.clear()

    def _copy_terminal(self) -> None:
        QApplication.clipboard().setText(self.terminal.toPlainText())
        self.statusBar().showMessage("Output copied to clipboard", 2000)

    def _update_status_bar(self) -> None:
        selected = sum(1 for cb in self.room_checkboxes.values() if cb.isChecked())
        total = len(self.room_checkboxes)
        self.statusBar().showMessage(
            f"{selected}/{total} rooms selected  \u2022  last action: {self._last_action}"
        )

    # -- terminal / status helpers ---------------------------------------

    def append_log(self, message: str, level: str = "detail") -> None:
        color = LEVEL_COLORS.get(level, "#d4d4d4")
        stamp = time.strftime("%H:%M:%S")
        self.terminal.append(
            f'<span style="color:#5c6773;">[{stamp}]</span> '
            f'<span style="color:{color};">{message}</span>'
        )
        self.terminal.moveCursor(QTextCursor.End)

    def _set_room_status(self, room_number: int, status: str) -> None:
        text, color = STATUS_BADGES.get(status, ("", "#868e96"))
        label = self.room_status_labels.get(room_number)
        if label is not None:
            label.setText(text)
            label.setStyleSheet(f"color:{color}; font-weight:bold;")

        bar = self.room_progress_bars.get(room_number)
        if bar is None:
            return
        if status == "running":
            # Determinate; milestones fill it as each phase completes.
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setStyleSheet("")
        elif status == "success":
            bar.setRange(0, 100)
            bar.setValue(100)
            bar.setStyleSheet("QProgressBar::chunk { background-color: #51cf66; }")
        elif status in ("failed", "cancelled"):
            bar.setRange(0, 100)
            color = "#ff6b6b" if status == "failed" else "#868e96"
            bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}")
        else:  # pending / reset
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setStyleSheet("")

    def _set_room_progress(self, room_number: int, sent: int, total: int) -> None:
        bar = self.room_progress_bars.get(room_number)
        if bar is None or total <= 0:
            return
        # Switch from indeterminate to determinate during uploads.
        if bar.maximum() == 0:
            bar.setRange(0, 100)
        bar.setValue(int(sent / total * 100))

    # -- file browsing ----------------------------------------------------

    def _browse_swu(self) -> None:
        start = str(CACHE_DIR if CACHE_DIR.exists() else Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Select SWU File", start, "SWU Files (*.swu)")
        if path:
            self.swu_file_input.setText(path)

    def _browse_config(self) -> None:
        start = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Config Template", start, "JSON Files (*.json)"
        )
        if path:
            self.config_file_input.setText(path)

    def _set_all_rooms(self, checked: bool) -> None:
        for cb in self.room_checkboxes.values():
            cb.setChecked(checked)

    # -- download ---------------------------------------------------------

    def _download_latest(self) -> None:
        email = self.artifactory_email_input.text().strip()
        token = self.artifactory_token_input.text().strip()
        if not email or not token:
            QMessageBox.warning(
                self, "Missing Credentials",
                "Enter your Artifactory email and token in Connection Settings.",
            )
            return

        self.append_log("=== Downloading Latest SWU ===", "info")

        self.download_worker = DownloadWorker(
            self.config, ArtifactoryCredentials(email, token), CACHE_DIR
        )
        self.download_worker.log.connect(self.append_log)
        self.download_worker.progress.connect(self._update_progress)
        self.download_worker.finished_ok.connect(self._download_finished)
        # Set busy AFTER the worker exists so Cancel is correctly enabled.
        self._set_busy(True)
        self.download_worker.start()

    def _download_finished(self, success: bool, file_path: str) -> None:
        self._set_busy(False)
        if success:
            self.swu_file_input.setText(file_path)
            self.append_log("Download successful - SWU path updated.", "success")
            self._last_action = "Download Latest \u2713"
        else:
            self.append_log("Download failed. See messages above.", "error")
            self._last_action = "Download Latest \u2717"
        self._update_status_bar()

    # -- deployment -------------------------------------------------------

    def _selected_rooms(self) -> List[Room]:
        return [
            self.config.room(num)
            for num, cb in self.room_checkboxes.items()
            if cb.isChecked() and self.config.room(num) is not None
        ]

    def _start_deployment(self) -> None:
        rooms = self._selected_rooms()
        if not rooms:
            QMessageBox.warning(self, "No Rooms", "Select at least one operating room.")
            return

        op = self.operation_combo.currentText()
        do_swu = "SWU" in op
        do_config = "Config" in op

        swu_file = Path(self.swu_file_input.text()) if self.swu_file_input.text() else None
        template = Path(self.config_file_input.text()) if self.config_file_input.text() else None

        if do_swu and (not swu_file or not swu_file.exists()):
            QMessageBox.warning(self, "Missing SWU", "Select a valid SWU file.")
            return
        if do_config:
            if not template or not template.exists():
                QMessageBox.warning(self, "Missing Template", "Select a valid config template.")
                return
            if not self.sudo_password_input.text():
                QMessageBox.warning(
                    self,
                    "Missing Sudo Password",
                    "Sudo password is required to run act-mfg-eeprom display on the room.",
                )
                return

        # Reset badges and progress bars for selected rooms.
        for room in rooms:
            self._set_room_status(room.number, "pending")

        self.terminal.clear()
        self.append_log("=== Matrix Deployment Started ===", "info")
        self.append_log(f"Rooms: {', '.join(r.name for r in rooms)}", "info")
        self.append_log(f"Operation: {op}", "info")
        self._set_busy(True, deploying=True)

        creds = DeploymentCredentials(
            ssh_password=self.password_input.text() or None,
            sudo_password=self.sudo_password_input.text() or None,
        )

        self.deploy_worker = DeploymentWorker(
            config=self.config,
            creds=creds,
            rooms=rooms,
            do_swu=do_swu,
            do_config=do_config,
            swu_file=swu_file,
            template_path=template,
            output_dir=Path.home() / "Downloads",
        )
        self.deploy_worker.log.connect(self.append_log)
        self.deploy_worker.progress.connect(self._update_progress)
        self.deploy_worker.room_progress.connect(self._set_room_progress)
        self.deploy_worker.room_status.connect(self._set_room_status)
        self.deploy_worker.all_done.connect(self._deployment_finished)
        self.deploy_worker.start()

    def _cancel_deployment(self) -> None:
        if self.deploy_worker is not None:
            self.deploy_worker.cancel()
        if self.download_worker is not None:
            self.download_worker.cancel()
        if self.system_action_worker is not None:
            self.system_action_worker.cancel()

    def _deployment_finished(self) -> None:
        self.append_log("=== Deployment Complete ===", "info")
        self._last_action = "Deployment \u2713"
        self._set_busy(False)
        self._update_status_bar()

    def _system_action_finished(self) -> None:
        self.append_log("=== System Action Complete ===", "info")
        self._set_busy(False)
        self._update_status_bar()

    def _run_system_action(
        self,
        action: str,
        title: str,
        bandwidth: Optional[str] = None,
        link_bandwidth_kbps: Optional[int] = None,
        logs_dir: Optional[Path] = None,
        require_sudo: bool = True,
        confirm: bool = True,
    ) -> None:
        rooms = self._selected_rooms()
        if not rooms:
            QMessageBox.warning(self, "No Rooms", "Select at least one operating room.")
            return

        if require_sudo and not self.sudo_password_input.text():
            QMessageBox.warning(
                self,
                "Missing Sudo Password",
                "Sudo password is required for system actions.",
            )
            return

        if confirm:
            reply = QMessageBox.question(
                self,
                f"Confirm {title}",
                f"{title} for {', '.join(r.name for r in rooms)}?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        for room in rooms:
            self._set_room_status(room.number, "pending")

        self._last_action = title
        self.terminal.clear()
        self.append_log(f"=== {title} Started ===", "info")
        self.append_log(f"Rooms: {', '.join(r.name for r in rooms)}", "info")
        self._set_busy(True, deploying=True)

        creds = DeploymentCredentials(
            ssh_password=self.password_input.text() or None,
            sudo_password=self.sudo_password_input.text() or None,
        )

        self.system_action_worker = SystemActionWorker(
            config=self.config,
            creds=creds,
            rooms=rooms,
            action=action,
            bandwidth=bandwidth,
            link_bandwidth_kbps=link_bandwidth_kbps,
            logs_dir=logs_dir,
        )
        self.system_action_worker.log.connect(self.append_log)
        self.system_action_worker.room_status.connect(self._set_room_status)
        self.system_action_worker.all_done.connect(self._system_action_finished)
        self.system_action_worker.start()

    def _restart_service(self) -> None:
        self._run_system_action("restart_service", "Restart Service")

    def _restart_nms_service(self) -> None:
        self._run_system_action("restart_nms_service", "Restart NMS")

    def _reboot(self) -> None:
        self._run_system_action("reboot", "Reboot")

    def _set_nms_bandwidth(self, bandwidth: str) -> None:
        self._run_system_action(
            "nms_bandwidth", f"Set Bandwidth: {bandwidth}", bandwidth=bandwidth
        )

    def _set_nms_link_bandwidth(self, kbps: int) -> None:
        self._run_system_action(
            "nms_link_bandwidth",
            f"Set NMS Interop Bandwidth: {kbps}",
            link_bandwidth_kbps=kbps,
        )

    def _get_logs(self) -> None:
        self._run_system_action(
            "get_logs",
            "Get Logs",
            logs_dir=DOWNLOADS_DIR,
            require_sudo=False,
            confirm=False,
        )

    def _get_nms_password(self) -> None:
        self._run_system_action(
            "get_nms_password",
            "Get NMS Password",
            require_sudo=True,
            confirm=False,
        )

    def _view_config(self) -> None:
        self._run_system_action(
            "view_config",
            "View matrix.api.config",
            logs_dir=DOWNLOADS_DIR,
            require_sudo=False,
            confirm=False,
        )

    # -- shared state -----------------------------------------------------

    def _update_progress(self, current: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setValue(int(current / total * 100))

    def _set_busy(self, busy: bool, deploying: bool = False) -> None:
        self.progress_bar.setVisible(busy)
        if busy:
            self.progress_bar.setValue(0)
        self.deploy_btn.setEnabled(not busy)
        self.download_btn.setEnabled(not busy)
        self.restart_service_btn.setEnabled(not busy)
        self.restart_nms_btn.setEnabled(not busy)
        self.reboot_btn.setEnabled(not busy)
        self.bandwidth_max_btn.setEnabled(not busy)
        self.bandwidth_limited_btn.setEnabled(not busy)
        self.link_bw_low_btn.setEnabled(not busy)
        self.link_bw_high_btn.setEnabled(not busy)
        self.get_logs_btn.setEnabled(not busy)
        self.view_config_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(
            busy
            and (
                deploying
                or self.download_worker is not None
                or self.system_action_worker is not None
            )
        )

    # -- settings persistence --------------------------------------------

    def _load_settings(self) -> None:
        data: Dict[str, str] = {}
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}

        # .env (non-secret values only) takes precedence over the saved
        # settings file and config defaults. Secrets are never loaded here.
        data.update(load_env_settings())

        self.router_ip_input.setText(data.get("router_ip", self.config.connection.router_ip))
        self.username_input.setText(data.get("username", self.config.connection.ssh_username))
        self.swu_file_input.setText(data.get("swu_file", ""))
        self.config_file_input.setText(data.get("config_file", ""))
        self.artifactory_email_input.setText(data.get("artifactory_email", ""))

        # Secrets are prefilled from .env only (never from the settings file)
        # and are still never persisted back to disk by this app.
        secrets = load_env_secrets()
        if "ssh_password" in secrets:
            self.password_input.setText(secrets["ssh_password"])
        if "sudo_password" in secrets:
            self.sudo_password_input.setText(secrets["sudo_password"])
        if "artifactory_token" in secrets:
            self.artifactory_token_input.setText(secrets["artifactory_token"])

    def _save_settings(self) -> None:
        data = {
            "router_ip": self.router_ip_input.text(),
            "username": self.username_input.text(),
            "swu_file": self.swu_file_input.text(),
            "config_file": self.config_file_input.text(),
            "artifactory_email": self.artifactory_email_input.text(),
            # Passwords and tokens are intentionally NOT persisted.
        }
        try:
            SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._save_settings()
        event.accept()


def run() -> None:
    import sys

    # High-DPI: render consistently when dragged between monitors that use
    # different scale factors (e.g. 100% vs 150%). Must be set before the
    # QApplication is constructed.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    try:
        config = AppConfig.load()
    except (FileNotFoundError, KeyError, ValueError) as exc:
        QMessageBox.critical(None, "Config Error", f"Failed to load configuration:\n{exc}")
        sys.exit(1)
    window = MatrixDeployWindow(config)
    window.show()
    sys.exit(app.exec_())
