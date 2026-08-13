"""Qt GUI for Matrix Deploy.

Keeps UI concerns only; all deployment/download logic lives in the worker
threads and the Qt-free service modules.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import webbrowser
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
    QSplitter,
    QStyle,
    QTabBar,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .artifactory import ArtifactoryCredentials
from .config import AppConfig, Room
from .deployer import DeploymentCredentials
from .env_settings import load_env_secrets, load_env_settings
from .faq_content import FAQ_SECTIONS
from .jenkins import JenkinsCredentials
from .workers import (
    BuildTriggerWorker,
    DeploymentWorker,
    DownloadWorker,
    LogTailWorker,
    OpenRoomGuiWorker,
    SystemActionWorker,
)

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

# (label, max rooms in flight at once; None = all selected rooms at once)
CONCURRENCY_OPTIONS: List[tuple] = [
    ("Sequential (1 at a time)", 1),
    ("2 at a time", 2),
    ("3 at a time", 3),
    ("4 at a time", 4),
    ("6 at a time", 6),
    ("All in parallel", None),
]
# 3-at-a-time balances throughput against router/uplink bandwidth and
# per-room SSH/SCP overhead better than "all rooms at once" for large fleets.
DEFAULT_CONCURRENCY_INDEX = 2

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


# Supersample factors we pre-render every hand-drawn icon at. Qt's QSS/pixmap
# loader auto-picks the matching ``file@2x.png`` / ``file@3x.png`` variant for
# the target device-pixel-ratio, so icons stay crisp at 100%/150%/200% and
# when the window is dragged between monitors of different DPI.
_HIDPI_SCALES = (1, 2, 3)


def _atnx_path(base: Path, scale: int) -> Path:
    """Return the Qt high-DPI ``@Nx`` sibling of ``base`` (``base`` itself for 1x)."""
    if scale == 1:
        return base
    return base.with_name(f"{base.stem}@{scale}x{base.suffix}")


def _make_chevron_icon(color: str, width: int = 12, height: int = 8) -> str:
    """Render a down-chevron and return its base (1x) file path.

    QSS's CSS-border triangle trick doesn't render reliably under the Fusion
    style on all Qt builds, so we draw a real icon (per color/size) and
    reference it via ``image: url(...)`` instead. ``width``/``height`` also
    drive the QSS ``::indicator``/``::down-arrow`` box size, which is what
    Qt uses for hit-testing - i.e. making these bigger also enlarges the
    clickable area, not just the drawn icon.

    The icon is rendered at each ``_HIDPI_SCALES`` factor into ``@Nx`` files so
    it stays sharp on High-DPI / mixed-DPI displays instead of being bitmap
    stretched.
    """
    safe_name = f"{color.lstrip('#')}_{width}x{height}"
    base = Path(tempfile.gettempdir()) / f"matrix_deploy_chevron_{safe_name}.png"
    for scale in _HIDPI_SCALES:
        target = _atnx_path(base, scale)
        if target.exists():
            continue
        w, h = width * scale, height * scale
        margin_x = max(1, round(w / 12))
        margin_y = max(1, round(h / 8))
        pixmap = QPixmap(w, h)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawPolygon(
            QPolygon(
                [
                    QPoint(margin_x, margin_y),
                    QPoint(w - margin_x, margin_y),
                    QPoint(w // 2, h - margin_y),
                ]
            )
        )
        painter.end()
        pixmap.save(str(target), "PNG")
    return str(base).replace("\\", "/")


def _make_grip_icon(
    color: str,
    orientation: str = "horizontal",
    lines: int = 3,
    thickness: int = 2,
    length: int = 18,
    gap: int = 2,
) -> str:
    """Render a set of parallel grip ridges to a cached PNG and return its path.

    Used on ``QSplitter`` handles so the drag affordance reads clearly (like a
    classic drag grabber) instead of Fusion's near-invisible default.
    ``orientation='horizontal'`` stacks horizontal ridges (an ``\u2261`` look,
    for a vertical splitter's horizontal handle you drag up/down);
    ``'vertical'`` places vertical ridges side by side (a ``\u2016`` look, for a
    horizontal splitter's handle you drag left/right).

    Rendered at each ``_HIDPI_SCALES`` factor into ``@Nx`` files so the ridges
    stay sharp on High-DPI / mixed-DPI displays.
    """
    safe = f"{color.lstrip('#')}_{orientation}_{lines}x{thickness}x{length}x{gap}"
    base = Path(tempfile.gettempdir()) / f"matrix_deploy_grip_{safe}.png"
    for scale in _HIDPI_SCALES:
        target = _atnx_path(base, scale)
        if target.exists():
            continue
        t, g, ln = thickness * scale, gap * scale, length * scale
        span = lines * t + (lines - 1) * g
        width, height = (ln, span) if orientation == "horizontal" else (span, ln)
        pixmap = QPixmap(width, height)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        radius = t / 2
        for i in range(lines):
            offset = i * (t + g)
            if orientation == "horizontal":
                painter.drawRoundedRect(0, offset, ln, t, radius, radius)
            else:
                painter.drawRoundedRect(offset, 0, t, ln, radius, radius)
        painter.end()
        pixmap.save(str(target), "PNG")
    return str(base).replace("\\", "/")


def build_app_stylesheet() -> str:
    """Application-wide look: neutral light surface, cohesive inputs/combos/
    frames that match the polished buttons. Built lazily (after QApplication
    exists) so the combo-box chevron icon can be rendered with QPainter."""
    chevron = _make_chevron_icon("#455A64")
    chevron_open = _make_chevron_icon("#1976D2")
    # Bigger than the combo-box chevron on purpose: this is the collapse/
    # expand indicator on checkable QGroupBox titles, and its QSS box size
    # doubles as the clickable hit area, which was previously too small
    # to reliably click.
    group_chevron = _make_chevron_icon("#37474F", width=22, height=16)
    # Grip dots for the resize handles: a horizontal row for the vertical
    # splitter (drag up/down) and a vertical column for the horizontal one
    # (drag left/right).
    grip_h = _make_grip_icon("#546E7A", "horizontal")
    grip_v = _make_grip_icon("#546E7A", "vertical")
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
QGroupBox::indicator {
    width: 22px;
    height: 16px;
    image: url(__GROUP_CHEVRON__);
}
QGroupBox::indicator:unchecked {
    image: none;
    border-left: 9px solid #37474F;
    border-top: 7px solid transparent;
    border-bottom: 7px solid transparent;
    width: 0; height: 0;
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
QSplitter::handle {
    background: #CFD8DC;
    border: 1px solid #B0BEC5;
    border-radius: 4px;
    margin: 2px;
}
QSplitter::handle:horizontal { width: 11px; image: url(__GRIP_V__); }
QSplitter::handle:vertical { height: 11px; image: url(__GRIP_H__); }
QSplitter::handle:hover { background: #BBDEFB; border-color: #64B5F6; }
QSplitter::handle:pressed { background: #90CAF9; border-color: #1976D2; }
QStatusBar { background: #CFD8DC; color: #37474F; }
QToolTip {
    background: #37474F; color: white; border: none; padding: 4px 6px;
}
"""
    return (
        css.replace("__CHEVRON__", chevron)
        .replace("__CHEVRON_OPEN__", chevron_open)
        .replace("__GROUP_CHEVRON__", group_chevron)
        .replace("__GRIP_H__", grip_h)
        .replace("__GRIP_V__", grip_v)
    )


SECTION_LABEL_STYLE = (
    "font-size:15px; font-weight:700; color:#1565C0;"
    "border-bottom:2px solid #90CAF9; padding-bottom:3px;"
)

_CONSOLE_STYLE = (
    "QTextEdit { background-color:#1e1e1e; color:#d4d4d4;"
    "border:1px solid #37474F; border-radius:6px; padding:6px;"
    "font-family:'Consolas','Courier New',monospace; }"
    "QScrollBar:vertical { background:#1e1e1e; width:12px; margin:0; }"
    "QScrollBar::handle:vertical { background:#555b62; border-radius:6px; min-height:24px; }"
    "QScrollBar::handle:vertical:hover { background:#6b7280; }"
    "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
)


def _make_console() -> QTextEdit:
    console = QTextEdit()
    console.setReadOnly(True)
    console.setMinimumHeight(200)
    console.setLineWrapMode(QTextEdit.WidgetWidth)
    console.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    console.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    console.setStyleSheet(_CONSOLE_STYLE)
    # (stamp, message) pairs kept in parallel with the rich-text view, so
    # Copy can rebuild a clean "[HH:MM:SS] message" line (our own tag, no
    # journalctl noise) without re-scraping HTML.
    console._raw_lines = []
    return console


def _append_console(console: QTextEdit, message: str, level: str) -> None:
    color = LEVEL_COLORS.get(level, "#d4d4d4")
    stamp = time.strftime("%H:%M:%S")
    console.append(
        f'<span style="color:#5c6773;">[{stamp}]</span> '
        f'<span style="color:{color};">{message}</span>'
    )
    console.moveCursor(QTextCursor.End)
    console._raw_lines.append((stamp, message))


_JOURNAL_PREFIX_RE = re.compile(
    r"^[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+[^\s\[:]+(?:\[\d+\])?:\s*"
)


def _strip_journal_prefix(line: str) -> str:
    """Strip journalctl's own 'MMM DD HH:MM:SS host process[pid]:' prefix,
    if present, leaving just the message. Lines that don't match (banners,
    our own info/success messages, etc.) are returned unchanged."""
    return _JOURNAL_PREFIX_RE.sub("", line, count=1)


def _copy_raw(console: QTextEdit) -> None:
    """Copy the console's content as '[HH:MM:SS] message', stripping
    journalctl's own per-line timestamp/host/process prefix (which is
    redundant noise) but keeping our own shorthand time tag."""
    lines = [
        f"[{stamp}] {_strip_journal_prefix(message)}"
        for stamp, message in console._raw_lines
    ]
    QApplication.clipboard().setText("\n".join(lines))


class _JobPanel(QWidget):
    """One tab: header (status + copy/cancel), progress bar, and a console.

    Represents a single launched job (a deployment, a system action, or a
    download) and owns the widgets that render that job's output/progress.
    """

    def __init__(self, title: str, on_cancel) -> None:
        super().__init__()
        self.title = title
        self.worker = None
        self.rooms: List[Room] = []
        self.finished = False
        self.total_rooms = 0
        self._completed = 0
        self.had_failure = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.status_label = QLabel("Running\u2026")
        self.status_label.setStyleSheet("font-weight:700; color:#ffd43b;")
        header.addWidget(self.status_label)
        header.addStretch()

        copy_btn = QPushButton("Copy")
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.setStyleSheet(_button_style(BTN_COLORS["utility"]))
        copy_btn.clicked.connect(self._copy)
        header.addWidget(copy_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.setStyleSheet(_button_style(BTN_COLORS["danger"]))
        self.cancel_btn.clicked.connect(on_cancel)
        header.addWidget(self.cancel_btn)
        layout.addLayout(header)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.console = _make_console()
        layout.addWidget(self.console, stretch=1)

    def _copy(self) -> None:
        _copy_raw(self.console)

    def append(self, message: str, level: str = "detail") -> None:
        _append_console(self.console, message, level)

    def set_progress(self, sent: int, total: int) -> None:
        if total > 0:
            self.progress.setValue(int(sent / total * 100))

    def room_completed(self, ok: bool) -> None:
        self._completed += 1
        if not ok:
            self.had_failure = True
        if self.total_rooms:
            self.progress.setValue(int(self._completed / self.total_rooms * 100))

    def mark_finished(self, ok: bool) -> None:
        self.finished = True
        self.cancel_btn.setEnabled(False)
        self.progress.setRange(0, 100)  # exit indeterminate mode, if it was set
        if ok:
            self.status_label.setText("Completed")
            self.status_label.setStyleSheet("font-weight:700; color:#51cf66;")
            self.progress.setValue(100)
        else:
            self.status_label.setText("Finished with errors")
            self.status_label.setStyleSheet("font-weight:700; color:#ff6b6b;")


class MatrixDeployWindow(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        # Multiple jobs can run at once; each owns a tab (_JobPanel).
        self._jobs: List[_JobPanel] = []
        # Room numbers with an in-flight job, to prevent double-booking a room.
        self._busy_rooms: set = set()
        self.room_checkboxes: Dict[int, QCheckBox] = {}
        self.room_status_labels: Dict[int, QLabel] = {}
        self.room_progress_bars: Dict[int, QProgressBar] = {}
        self.room_open_gui_buttons: Dict[int, QPushButton] = {}
        # Keep references to in-flight OpenRoomGuiWorker threads so they are
        # not garbage-collected mid-run.
        self._open_gui_workers: List[OpenRoomGuiWorker] = []

        self._last_action = "none"

        self.setWindowTitle("Matrix Deploy")
        self.setGeometry(80, 40, 1400, 980)
        self.setMinimumSize(1160, 760)
        self.setStyleSheet(build_app_stylesheet())
        self._build_ui()
        self._load_settings()
        self._refresh_config_status()
        self._update_status_bar()
        # Nudge first-time users to Settings if the essentials are missing.
        self._warn_if_unconfigured()

    # -- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(0)

        # Window-level tabs keep each workflow uncluttered: the everyday
        # Deploy flow, one-time Settings/credentials, and a searchable FAQ.
        self.top_tabs = QTabWidget()
        self.top_tabs.addTab(self._build_deploy_tab(), "Deploy")
        self.top_tabs.addTab(self._build_settings_tab(), "Settings")
        self.top_tabs.addTab(self._build_faq_tab(), "FAQ")
        root.addWidget(self.top_tabs)

    def _build_deploy_tab(self) -> QWidget:
        """The main single-page workflow: files, options, actions, the
        categorized operation buttons, and the rooms/output split."""
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(6, 8, 6, 6)
        outer.setSpacing(12)

        # Files/options/action controls stacked in one widget so they can be
        # collapsed via the splitter below to make room for the console.
        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(12)
        controls_layout.addWidget(self._build_files_group())
        controls_layout.addLayout(self._build_options_row())
        controls_layout.addLayout(self._build_action_row())
        controls_layout.addLayout(self._build_controls_row())

        # Operating rooms and output sit side by side, user-resizable.
        bottom_split = QSplitter(Qt.Horizontal)
        bottom_split.addWidget(self._build_rooms_group())
        bottom_split.addWidget(self._build_terminal_group())
        bottom_split.setStretchFactor(0, 2)
        bottom_split.setStretchFactor(1, 3)
        bottom_split.setSizes([420, 700])
        bottom_split.setHandleWidth(11)
        bottom_split.setChildrenCollapsible(True)
        self.bottom_split = bottom_split

        # Controls stack on top; rooms/output take the rest by default but
        # this bar can be dragged up to reclaim vertical space for a taller
        # console - the actual complaint being addressed here.
        main_split = QSplitter(Qt.Vertical)
        main_split.addWidget(controls)
        main_split.addWidget(bottom_split)
        main_split.setStretchFactor(0, 0)
        main_split.setStretchFactor(1, 1)
        main_split.setCollapsible(0, True)
        main_split.setCollapsible(1, False)
        main_split.setHandleWidth(11)
        # Give controls just what it needs up front; drag the handle up to
        # reclaim more of that space for the console below.
        main_split.setSizes([controls.sizeHint().height(), 10_000])
        self.main_split = main_split

        # Persistent hints on the drag handles (handle 0 is a hidden no-op;
        # the real draggable handle is at index 1).
        main_split.handle(1).setToolTip(
            "Drag to resize \u2022 collapse the controls to enlarge the output below"
        )
        bottom_split.handle(1).setToolTip(
            "Drag to resize \u2022 give more width to the output or the rooms list"
        )

        outer.addWidget(main_split)
        return tab

    def _build_settings_tab(self) -> QWidget:
        """Credentials/connection config lives here, off the main flow, with a
        live status banner and an explicit Save."""
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(6, 8, 6, 6)
        outer.setSpacing(12)

        # Live configuration status: red until the critical fields are set.
        self.config_status_label = QLabel()
        self.config_status_label.setWordWrap(True)
        self.config_status_label.setStyleSheet(
            "padding:10px 12px; border-radius:6px; font-weight:600;"
        )
        outer.addWidget(self.config_status_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(12)
        inner_layout.addWidget(self._build_connection_group())

        note = QLabel(
            "Secrets (passwords/tokens) are never saved to disk. Non-secret "
            "fields are saved to ~/.matrix_deploy_settings.json. You can also "
            "prefill fields from a gitignored .env file - see the FAQ."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#607D8B; font-size:12px;")
        inner_layout.addWidget(note)
        inner_layout.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll, stretch=1)

        save_row = QHBoxLayout()
        save_row.addStretch()
        save_btn = self._make_button(
            "Save Settings", "primary", icon=QStyle.SP_DialogSaveButton
        )
        save_btn.clicked.connect(self._save_settings_clicked)
        save_row.addWidget(save_btn)
        outer.addLayout(save_row)
        return tab

    def _build_faq_tab(self) -> QWidget:
        """Searchable reference page rendered from ``faq_content.FAQ_SECTIONS``."""
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(6, 8, 6, 6)
        outer.setSpacing(8)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self.faq_search_input = QLineEdit()
        self.faq_search_input.setPlaceholderText(
            "Find a command or topic, then press Enter to jump to the next match"
        )
        self.faq_search_input.returnPressed.connect(self._faq_search_next)
        self.faq_search_input.textChanged.connect(self._faq_search_reset)
        search_row.addWidget(self.faq_search_input, stretch=1)
        find_btn = self._make_button("Find Next", "utility")
        find_btn.clicked.connect(self._faq_search_next)
        search_row.addWidget(find_btn)
        outer.addLayout(search_row)

        self.faq_browser = QTextBrowser()
        self.faq_browser.setOpenExternalLinks(True)
        self.faq_browser.setStyleSheet(
            "QTextBrowser { background:#FFFFFF; border:1px solid #CFD8DC;"
            "border-radius:6px; padding:10px; }"
        )
        self.faq_browser.setHtml(self._build_faq_html())
        outer.addWidget(self.faq_browser, stretch=1)
        return tab

    @staticmethod
    def _build_faq_html() -> str:
        parts = [
            "<style>"
            "h2 { color:#1565C0; border-bottom:2px solid #90CAF9; padding-bottom:4px;"
            " margin-top:18px; }"
            "h3 { color:#37474F; margin-bottom:2px; }"
            "code { background:#ECEFF1; color:#C62828; padding:1px 4px;"
            " border-radius:3px; font-family:Consolas,monospace; }"
            "p, li { color:#263238; line-height:1.5; }"
            "</style>"
            "<h1>Matrix Deploy - Reference</h1>"
        ]
        for title, items in FAQ_SECTIONS:
            parts.append(f"<h2>{title}</h2>")
            for question, answer in items:
                parts.append(f"<h3>{question}</h3>")
                parts.append(f"<p>{answer}</p>")
        return "".join(parts)

    def _faq_search_reset(self) -> None:
        # Move the cursor to the top so the next search starts from the
        # beginning whenever the query changes.
        self.faq_browser.moveCursor(QTextCursor.Start)

    def _faq_search_next(self) -> None:
        term = self.faq_search_input.text().strip()
        if not term:
            return
        if not self.faq_browser.find(term):
            # Wrap around to the top and try once more.
            self.faq_browser.moveCursor(QTextCursor.Start)
            if not self.faq_browser.find(term):
                self.statusBar().showMessage(f"No match for '{term}'", 2000)

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

        layout.addWidget(QLabel("Jenkins Username:"), 6, 0)
        self.jenkins_username_input = QLineEdit()
        self.jenkins_username_input.setPlaceholderText(
            "Jenkins login ID, e.g. 'Daniel Rodriguez' (not your email)"
        )
        layout.addWidget(self.jenkins_username_input, 6, 1)

        layout.addWidget(QLabel("Jenkins Token:"), 7, 0)
        self.jenkins_token_input = QLineEdit()
        self.jenkins_token_input.setEchoMode(QLineEdit.Password)
        self.jenkins_token_input.setPlaceholderText("API token (not saved to disk)")
        layout.addWidget(self.jenkins_token_input, 7, 1)
        layout.addWidget(self._password_toggle(self.jenkins_token_input), 7, 2)

        # Live-refresh the configuration status banner as the critical fields
        # are edited.
        self.router_ip_input.textChanged.connect(self._refresh_config_status)
        self.username_input.textChanged.connect(self._refresh_config_status)

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
        self.build_btn = self._make_button(
            "Build New",
            "service",
            tooltip="Trigger a new 'Embedded Builder' Jenkins build (matrix / wrynose).",
            icon=QStyle.SP_BrowserReload,
        )
        self.build_btn.clicked.connect(self._trigger_build)
        layout.addWidget(self.build_btn, 0, 4)

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

        open_gui_btn = QPushButton("Open GUI")
        open_gui_btn.setToolTip(
            "Open this room's NMS demonstrator GUI in your browser and fetch "
            "its admin password"
        )
        open_gui_btn.setMinimumWidth(80)
        open_gui_btn.clicked.connect(lambda _c=False, r=room: self._open_room_gui(r))
        self.room_open_gui_buttons[room.number] = open_gui_btn

        row = QHBoxLayout()
        row.setContentsMargins(8, 4, 8, 4)
        row.addWidget(cb)
        row.addWidget(badge)
        row.addWidget(bar, stretch=1)
        row.addWidget(open_gui_btn)

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

        row.addWidget(QLabel("Concurrency:"))
        self.concurrency_combo = QComboBox()
        self.concurrency_combo.addItems([label for label, _ in CONCURRENCY_OPTIONS])
        self.concurrency_combo.setCurrentIndex(DEFAULT_CONCURRENCY_INDEX)
        self.concurrency_combo.setMinimumWidth(170)
        self.concurrency_combo.setToolTip(
            "How many selected rooms run at once for deployments/system actions. "
            "Rooms beyond the limit queue and start as earlier ones finish. "
            "Ignored (forced to 1) if 'same_physical_host' is set in the config, "
            "or for actions that touch local shared state (e.g. Remove Fingerprint)."
        )
        row.addWidget(self.concurrency_combo)
        row.addStretch()
        return row

    def _concurrency_settings(self) -> tuple:
        """Returns (sequential, max_concurrency) for the current combo selection."""
        _, max_concurrency = CONCURRENCY_OPTIONS[self.concurrency_combo.currentIndex()]
        return max_concurrency == 1, max_concurrency

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
        self.cancel_btn.setToolTip("Cancel all currently running jobs")
        self.cancel_btn.clicked.connect(self._cancel_deployment)
        row.addWidget(self.cancel_btn, stretch=1)
        return row

    @staticmethod
    def _button_group(
        title: str, buttons: List[QPushButton], columns: int = 2
    ) -> QGroupBox:
        """Wrap a set of related buttons in a titled, collapsible frame
        (inherits global QGroupBox styling). Buttons are laid out in a grid
        that wraps every ``columns`` items, so groups with many actions (e.g.
        Services) breathe instead of being crammed into one row. Click the
        checkbox in the title to collapse/expand the group."""
        box = QGroupBox(title)
        box.setCheckable(True)
        box.setChecked(True)
        box.setToolTip("Click to collapse/expand this group")
        grid = QGridLayout()
        grid.setContentsMargins(8, 6, 8, 8)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        for i, btn in enumerate(buttons):
            grid.addWidget(btn, i // columns, i % columns)
        for col in range(columns):
            grid.setColumnStretch(col, 1)
        box.setLayout(grid)

        def _toggle(checked: bool, _buttons=buttons) -> None:
            for b in _buttons:
                b.setVisible(checked)

        box.toggled.connect(_toggle)
        return box

    def _build_controls_row(self) -> QHBoxLayout:
        # --- Services (restarts / reboot) --------------------------------
        self.restart_service_btn = self._make_button(
            "Restart matrix-api", "service", "Restart the matrix-api service on selected rooms",
            icon=QStyle.SP_BrowserReload,
        )
        self.restart_service_btn.clicked.connect(self._restart_service)

        self.restart_nms_btn = self._make_button(
            "Restart NMS", "service", "Restart the barco-nms service on selected rooms",
            icon=QStyle.SP_BrowserReload,
        )
        self.restart_nms_btn.clicked.connect(self._restart_nms_service)

        self.status_service_btn = self._make_button(
            "Status: matrix-api", "utility", "Show systemctl status for matrix-api on selected rooms",
            icon=QStyle.SP_FileDialogDetailedView,
        )
        self.status_service_btn.clicked.connect(self._matrix_api_status)

        self.status_nms_btn = self._make_button(
            "Status: NMS", "utility", "Show systemctl status for barco-nms on selected rooms",
            icon=QStyle.SP_FileDialogDetailedView,
        )
        self.status_nms_btn.clicked.connect(self._nms_status)

        self.stop_service_btn = self._make_button(
            "Stop matrix-api", "danger", "Stop the matrix-api service on selected rooms",
            icon=QStyle.SP_MediaStop,
        )
        self.stop_service_btn.clicked.connect(self._stop_service)

        self.stop_nms_btn = self._make_button(
            "Stop NMS", "danger", "Stop the barco-nms service on selected rooms",
            icon=QStyle.SP_MediaStop,
        )
        self.stop_nms_btn.clicked.connect(self._stop_nms_service)

        self.reboot_btn = self._make_button(
            "Reboot", "danger", "Reboot selected rooms", icon=QStyle.SP_ComputerIcon
        )
        self.reboot_btn.clicked.connect(self._reboot)

        self.shutdown_btn = self._make_button(
            "Shutdown", "danger", "Shut down (power off) selected rooms", icon=QStyle.SP_ComputerIcon
        )
        self.shutdown_btn.clicked.connect(self._shutdown)

        self.matrix_api_certs_btn = self._make_button(
            "matrix-api-certs",
            "danger",
            "Disable cert-init/unseal units, regenerate the self-signed "
            "matrix-api server cert/key, fix ownership/permissions, and "
            "restart matrix-api on selected rooms",
            icon=QStyle.SP_FileDialogDetailedView,
        )
        self.matrix_api_certs_btn.clicked.connect(self._matrix_api_certs)

        self.fix_room_config_race_btn = self._make_button(
            "Fix IP Race Condition",
            "service",
            "Patch matrix-room-config-generator.service so it waits on "
            "barco-nms-network-init.service before starting, fixing a race "
            "where it could grab the wrong/stale IP address. Takes effect on "
            "next reboot; safe to re-run.",
            icon=QStyle.SP_BrowserReload,
        )
        self.fix_room_config_race_btn.clicked.connect(self._fix_room_config_race)

        self.log_debug_btn = self._make_button(
            "Log Level: Debug",
            "service",
            "Set logConfig.streams[].level to 'debug' in matrix.api.config.json "
            "and restart matrix-api on selected rooms",
            icon=QStyle.SP_FileDialogDetailedView,
        )
        self.log_debug_btn.clicked.connect(self._set_log_debug)

        services_box = self._button_group(
            "Services",
            [
                self.restart_service_btn,
                self.restart_nms_btn,
                self.status_service_btn,
                self.status_nms_btn,
                self.stop_service_btn,
                self.stop_nms_btn,
                self.reboot_btn,
                self.shutdown_btn,
                self.matrix_api_certs_btn,
                self.fix_room_config_race_btn,
                self.log_debug_btn,
            ],
            columns=2,
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

        self.remove_overlay_btn = self._make_button(
            "Remove Overlay",
            "utility",
            "Push application-user.yml with nexxis.overlay.noVideoOverlayId = matrixEmptyOverlay "
            "and restart barco-nms",
        )
        self.remove_overlay_btn.clicked.connect(self._remove_overlay)

        bandwidth_box = self._button_group(
            "Bandwidth",
            [
                self.bandwidth_max_btn,
                self.link_bw_high_btn,
                self.bandwidth_limited_btn,
                self.link_bw_low_btn,
                self.remove_overlay_btn,
            ],
            columns=2,
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

        self.remove_fingerprint_btn = self._make_button(
            "Remove Fingerprint",
            "utility",
            "Remove the cached SSH host key for selected rooms from your local "
            "known_hosts file (fixes 'REMOTE HOST IDENTIFICATION HAS CHANGED'). "
            "Local-only; does not connect to the room.",
            icon=QStyle.SP_TrashIcon,
        )
        self.remove_fingerprint_btn.clicked.connect(self._remove_fingerprint)

        diagnostics_box = self._button_group(
            "Diagnostics",
            [
                self.get_logs_btn,
                self.view_config_btn,
                self.get_nms_password_btn,
                self.remove_fingerprint_btn,
            ],
            columns=2,
        )

        # --- Live Logs (streaming, read-only) -----------------------------
        self.watch_matrix_api_btn = self._make_button(
            "Watch matrix-api Live",
            "neutral",
            "Stream matrix-api's journal live for selected rooms (one tab per "
            "room). Read-only - safe to run alongside other jobs on the same "
            "room. Click Stop on the tab to end.",
            icon=QStyle.SP_MediaPlay,
        )
        self.watch_matrix_api_btn.clicked.connect(self._watch_matrix_api_live)

        self.watch_nms_btn = self._make_button(
            "Watch NMS Live",
            "neutral",
            "Stream barco-nms's journal live for selected rooms (one tab per "
            "room). Read-only - safe to run alongside other jobs on the same "
            "room. Click Stop on the tab to end.",
            icon=QStyle.SP_MediaPlay,
        )
        self.watch_nms_btn.clicked.connect(self._watch_nms_live)

        live_logs_box = self._button_group(
            "Live Logs",
            [self.watch_matrix_api_btn, self.watch_nms_btn],
            columns=1,
        )

        # Each group now wraps its buttons into a 2-col grid, so stretch by
        # column count keeps the grids roughly aligned across the row and the
        # groups top-aligned.
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(services_box, stretch=2)
        row.addWidget(bandwidth_box, stretch=2)
        row.addWidget(diagnostics_box, stretch=2)
        row.addWidget(live_logs_box, stretch=1)
        row.setAlignment(Qt.AlignTop)
        return row

    def _build_terminal_group(self) -> QGroupBox:
        group = QGroupBox("Output")
        layout = QVBoxLayout()

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._on_tab_close_requested)

        # Pinned "Combined" tab: merged, job-prefixed stream of every job.
        combined = QWidget()
        cv = QVBoxLayout(combined)
        cv.setContentsMargins(6, 6, 6, 6)
        cv.setSpacing(6)
        c_toolbar = QHBoxLayout()
        self.maximize_output_btn = self._make_button(
            "Maximize",
            "utility",
            tooltip="Collapse the controls and rooms list to enlarge this output. "
            "Click again to restore.",
            icon=QStyle.SP_TitleBarMaxButton,
        )
        self.maximize_output_btn.setCheckable(True)
        self.maximize_output_btn.toggled.connect(self._toggle_maximize_output)
        c_toolbar.addWidget(self.maximize_output_btn)
        c_toolbar.addStretch()
        copy_btn = self._make_button("Copy", "utility", icon=QStyle.SP_FileDialogDetailedView)
        copy_btn.clicked.connect(self._copy_terminal)
        clear = self._make_button("Clear", "utility", icon=QStyle.SP_DialogResetButton)
        clear.clicked.connect(self.terminal_clear)
        c_toolbar.addWidget(copy_btn)
        c_toolbar.addWidget(clear)
        cv.addLayout(c_toolbar)
        self.combined_console = _make_console()
        cv.addWidget(self.combined_console, stretch=1)
        self.tabs.addTab(combined, "Combined")
        # The Combined tab is pinned; remove its close button.
        self.tabs.tabBar().setTabButton(0, QTabBar.RightSide, None)

        layout.addWidget(self.tabs)
        group.setLayout(layout)
        return group

    def terminal_clear(self) -> None:
        self.combined_console.clear()
        self.combined_console._raw_lines = []

    def _copy_terminal(self) -> None:
        _copy_raw(self.combined_console)
        self.statusBar().showMessage("Output copied to clipboard", 2000)

    def _toggle_maximize_output(self, maximized: bool) -> None:
        """One-click expand: collapse the controls stack and the rooms list so
        the Output console takes the whole tab, and restore the prior sizes on
        toggle-off."""
        if maximized:
            self._saved_main_sizes = self.main_split.sizes()
            self._saved_bottom_sizes = self.bottom_split.sizes()
            self.main_split.setSizes([0, 10_000])
            self.bottom_split.setSizes([0, 10_000])
            self.maximize_output_btn.setText("Restore")
        else:
            if getattr(self, "_saved_main_sizes", None):
                self.main_split.setSizes(self._saved_main_sizes)
            if getattr(self, "_saved_bottom_sizes", None):
                self.bottom_split.setSizes(self._saved_bottom_sizes)
            self.maximize_output_btn.setText("Maximize")

    def _on_tab_close_requested(self, index: int) -> None:
        if index == 0:
            return  # Combined tab is pinned.
        panel = self.tabs.widget(index)
        if isinstance(panel, _JobPanel) and not panel.finished:
            QMessageBox.information(
                self,
                "Job Running",
                "This job is still running. Cancel it before closing the tab.",
            )
            return
        self.tabs.removeTab(index)
        if panel in self._jobs:
            self._jobs.remove(panel)
        panel.deleteLater()

    def _update_status_bar(self) -> None:
        selected = sum(1 for cb in self.room_checkboxes.values() if cb.isChecked())
        total = len(self.room_checkboxes)
        self.statusBar().showMessage(
            f"{selected}/{total} rooms selected  \u2022  last action: {self._last_action}"
        )

    # -- terminal / status helpers ---------------------------------------

    def append_log(self, message: str, level: str = "detail") -> None:
        """Append to the pinned Combined console (the firehose view)."""
        _append_console(self.combined_console, message, level)

    # -- job management ---------------------------------------------------

    @staticmethod
    def _rooms_label(rooms: List[Room]) -> str:
        if not rooms:
            return ""
        nums = [r.number for r in rooms]
        if len(nums) <= 5:
            return "OR " + ",".join(str(n) for n in nums)
        return f"{len(nums)} ORs"

    def _launch_job(
        self, title: str, worker, rooms: List[Room], lock_rooms: bool = True
    ) -> _JobPanel:
        """Create a tab for the job, wire its signals, and register its busy
        rooms. Does NOT start the worker: the caller must connect the finish
        signal (all_done / finished_ok / stopped) first and then call
        ``worker.start()``, to avoid missing a fast-finishing job's completion
        signal. Pass ``lock_rooms=False`` for read-only jobs (e.g. live log
        tailing) that should be allowed to run alongside other jobs on the
        same rooms."""
        panel = _JobPanel(title, on_cancel=worker.cancel)
        panel.worker = worker
        panel.rooms = rooms
        panel.total_rooms = len(rooms)
        self._jobs.append(panel)
        if lock_rooms:
            self._busy_rooms |= {r.number for r in rooms}

        def _log(message: str, level: str, _panel=panel, _title=title) -> None:
            _panel.append(message, level)
            _append_console(self.combined_console, f"[{_title}] {message}", level)

        worker.log.connect(_log)

        # Shared room grid updates (safe: a room belongs to one active job).
        if hasattr(worker, "room_status"):
            worker.room_status.connect(self._set_room_status)
        if hasattr(worker, "room_progress"):
            worker.room_progress.connect(self._set_room_progress)
        # Per-job progress: room-count for per-room workers, else byte progress.
        if hasattr(worker, "room_done"):
            worker.room_done.connect(
                lambda _num, ok, _panel=panel: _panel.room_completed(ok)
            )
        elif hasattr(worker, "progress"):
            worker.progress.connect(panel.set_progress)

        label = self._rooms_label(rooms)
        tab_title = f"{title} \u00b7 {label}" if label else title
        idx = self.tabs.addTab(panel, tab_title)
        self.tabs.setCurrentIndex(idx)
        return panel

    def _finish_job(self, panel: _JobPanel, label: str, ok: Optional[bool] = None) -> None:
        """Called when a job's worker signals completion: free its rooms,
        mark the tab, and refresh the status bar."""
        resolved_ok = (not panel.had_failure) if ok is None else ok
        panel.mark_finished(resolved_ok)
        self._busy_rooms -= {r.number for r in panel.rooms}
        mark = "\u2713" if resolved_ok else "\u2717"
        idx = self.tabs.indexOf(panel)
        if idx != -1:
            self.tabs.setTabText(idx, f"{self.tabs.tabText(idx)} {mark}")
        self._last_action = f"{label} {mark}"
        self.append_log(f"=== {label} Complete ===", "info")
        self._update_status_bar()

    def _cancel_deployment(self) -> None:
        cancelled = False
        for panel in self._jobs:
            if not panel.finished and panel.worker is not None:
                panel.worker.cancel()
                cancelled = True
        if not cancelled:
            self.statusBar().showMessage("No running jobs to cancel", 2000)

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

        worker = DownloadWorker(
            self.config, ArtifactoryCredentials(email, token), CACHE_DIR
        )
        panel = self._launch_job("Download SWU", worker, rooms=[])
        worker.finished_ok.connect(
            lambda ok, path, _p=panel: self._download_finished(ok, path, _p)
        )
        worker.start()

    def _download_finished(self, success: bool, file_path: str, panel: _JobPanel) -> None:
        if success:
            self.swu_file_input.setText(file_path)
            panel.append("Download successful - SWU path updated.", "success")
        else:
            panel.append("Download failed. See messages above.", "error")
        self._finish_job(panel, "Download SWU", ok=success)

    # -- build --------------------------------------------------------

    def _trigger_build(self) -> None:
        username = self.jenkins_username_input.text().strip()
        token = self.jenkins_token_input.text().strip()
        if not username or not token:
            QMessageBox.warning(
                self, "Missing Credentials",
                "Enter your Jenkins username and token in Connection Settings.\n\n"
                "Note: this is your Jenkins login ID (e.g. 'Daniel Rodriguez'), "
                "not your email - check https://jenkins-embedded.dev.actsw.net "
                "under your account/profile page if unsure.",
            )
            return

        reply = QMessageBox.question(
            self,
            "Build New",
            "Trigger a new 'Embedded Builder' Jenkins build (matrix / wrynose)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        worker = BuildTriggerWorker(JenkinsCredentials(username, token))
        panel = self._launch_job("Build New", worker, rooms=[])
        worker.finished_ok.connect(
            lambda ok, msg, url, _p=panel: self._trigger_build_finished(ok, msg, url, _p)
        )
        worker.start()

    def _trigger_build_finished(
        self, success: bool, message: str, build_url: str, panel: _JobPanel
    ) -> None:
        if success:
            panel.append(message, "success")
            if build_url:
                panel.append(f"View build: {build_url}", "info")
                idx = self.tabs.indexOf(panel)
                match = re.search(r"#(\d+)", message)
                if idx != -1 and match:
                    self.tabs.setTabText(idx, f"Build New #{match.group(1)}")
        else:
            panel.append("Build trigger failed. See messages above.", "error")
        self._finish_job(panel, "Build New", ok=success)

    # -- deployment -------------------------------------------------------

    def _selected_rooms(self) -> List[Room]:
        return [
            self.config.room(num)
            for num, cb in self.room_checkboxes.items()
            if cb.isChecked() and self.config.room(num) is not None
        ]

    def _open_room_gui(self, room: Room) -> None:
        """Open a single room's NMS demonstrator GUI in the browser and
        fetch/display its admin password using the existing NMS password
        (act-mfg-eeprom) logic."""
        if not self._require_connection():
            return
        if room.number in self._busy_rooms:
            QMessageBox.warning(
                self,
                "Room Busy",
                f"OR {room.number} ({room.name}) already has a running job.",
            )
            return

        if not self.sudo_password_input.text():
            QMessageBox.warning(
                self,
                "Missing Sudo Password",
                "Sudo password is required to fetch the NMS admin password.",
            )
            return

        creds = DeploymentCredentials(
            ssh_password=self.password_input.text() or None,
            sudo_password=self.sudo_password_input.text() or None,
        )

        btn = self.room_open_gui_buttons.get(room.number)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText("Connecting\u2026")

        worker = OpenRoomGuiWorker(self.config, creds, room)
        self._open_gui_workers.append(worker)
        worker.log.connect(self.append_log)
        worker.finished_ok.connect(
            lambda num, pwd, w=worker: self._room_gui_ready(num, pwd, w)
        )
        worker.start()

    def _room_gui_ready(
        self, room_number: int, password: Optional[str], worker: OpenRoomGuiWorker
    ) -> None:
        if worker in self._open_gui_workers:
            self._open_gui_workers.remove(worker)

        btn = self.room_open_gui_buttons.get(room_number)
        if btn is not None:
            btn.setEnabled(True)
            btn.setText("Open GUI")

        room = self.config.room(room_number)
        if room is None:
            return

        url = room.demonstrator_gui_url(self.config.connection.router_ip)
        webbrowser.open(url)

        if password:
            QApplication.clipboard().setText(password)
            self.append_log(
                f"OR {room.number} ({room.name}): opened {url} "
                f"- admin password copied to clipboard.",
                "success",
            )
            QMessageBox.information(
                self,
                "NMS Admin Password",
                f"OR {room.number} ({room.name})\n\n"
                f"URL: {url}\n\n"
                f"Admin password (copied to clipboard):\n{password}",
            )
        else:
            self.append_log(
                f"OR {room.number} ({room.name}): opened {url} "
                f"- could not retrieve admin password.",
                "warning",
            )
            QMessageBox.warning(
                self,
                "Password Not Found",
                f"Opened {url} but could not retrieve the admin password.\n"
                "Check the sudo password and connectivity to the room.",
            )

    def _start_deployment(self) -> None:
        if not self._require_connection():
            return
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

        busy = [r for r in rooms if r.number in self._busy_rooms]
        if busy:
            QMessageBox.warning(
                self,
                "Rooms Busy",
                "These rooms already have a running job:\n"
                + ", ".join(r.name for r in busy),
            )
            return

        # Final confirmation - deployments are disruptive and easy to fire on
        # the wrong rooms/operation by accident.
        reply = QMessageBox.question(
            self,
            "Confirm Deployment",
            f"Start '{op}' on {len(rooms)} room(s)?\n\n"
            + ", ".join(r.name for r in rooms),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # Reset badges and progress bars for selected rooms.
        for room in rooms:
            self._set_room_status(room.number, "pending")

        creds = DeploymentCredentials(
            ssh_password=self.password_input.text() or None,
            sudo_password=self.sudo_password_input.text() or None,
        )
        sequential, max_concurrency = self._concurrency_settings()

        worker = DeploymentWorker(
            config=self.config,
            creds=creds,
            rooms=rooms,
            do_swu=do_swu,
            do_config=do_config,
            swu_file=swu_file,
            template_path=template,
            output_dir=Path.home() / "Downloads",
            sequential=sequential,
            max_concurrency=max_concurrency,
        )
        panel = self._launch_job("Deploy", worker, rooms)
        panel.append(f"Operation: {op}", "info")
        panel.append(f"Rooms: {', '.join(r.name for r in rooms)}", "info")
        worker.all_done.connect(lambda _p=panel: self._finish_job(_p, "Deployment"))
        worker.start()

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
        if not self._require_connection():
            return
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

        busy = [r for r in rooms if r.number in self._busy_rooms]
        if busy:
            QMessageBox.warning(
                self,
                "Rooms Busy",
                "These rooms already have a running job:\n"
                + ", ".join(r.name for r in busy),
            )
            return

        for room in rooms:
            self._set_room_status(room.number, "pending")

        creds = DeploymentCredentials(
            ssh_password=self.password_input.text() or None,
            sudo_password=self.sudo_password_input.text() or None,
        )
        sequential, max_concurrency = self._concurrency_settings()

        worker = SystemActionWorker(
            config=self.config,
            creds=creds,
            rooms=rooms,
            action=action,
            bandwidth=bandwidth,
            link_bandwidth_kbps=link_bandwidth_kbps,
            logs_dir=logs_dir,
            sequential=sequential,
            max_concurrency=max_concurrency,
        )
        panel = self._launch_job(title, worker, rooms)
        panel.append(f"Rooms: {', '.join(r.name for r in rooms)}", "info")
        worker.all_done.connect(lambda _p=panel, t=title: self._finish_job(_p, t))
        worker.start()

    def _restart_service(self) -> None:
        self._run_system_action("restart_service", "Restart matrix-api")

    def _restart_nms_service(self) -> None:
        self._run_system_action("restart_nms_service", "Restart NMS")

    def _matrix_api_status(self) -> None:
        self._run_system_action(
            "matrix_api_status", "Status: matrix-api", require_sudo=False, confirm=False
        )

    def _nms_status(self) -> None:
        self._run_system_action(
            "nms_status", "Status: NMS", require_sudo=False, confirm=False
        )

    def _stop_service(self) -> None:
        self._run_system_action("stop_service", "Stop matrix-api")

    def _stop_nms_service(self) -> None:
        self._run_system_action("stop_nms_service", "Stop NMS")

    def _reboot(self) -> None:
        self._run_system_action("reboot", "Reboot")

    def _shutdown(self) -> None:
        self._run_system_action("shutdown", "Shutdown")

    def _matrix_api_certs(self) -> None:
        self._run_system_action("matrix_api_certs", "matrix-api-certs")

    def _fix_room_config_race(self) -> None:
        self._run_system_action("fix_room_config_race", "Fix IP Race Condition")

    def _set_log_debug(self) -> None:
        self._run_system_action("set_log_debug", "Log Level: Debug")

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

    def _remove_overlay(self) -> None:
        self._run_system_action("remove_overlay", "Remove Overlay")

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

    def _remove_fingerprint(self) -> None:
        self._run_system_action(
            "remove_fingerprint",
            "Remove Fingerprint",
            require_sudo=False,
            confirm=False,
        )

    # -- live log tailing ---------------------------------------------------

    def _watch_service_live(self, service: str, title: str) -> None:
        """Launch one live ``journalctl -f`` tail tab per selected room.

        Read-only, so it deliberately does NOT lock the room (lock_rooms=False):
        watching logs while a deploy/system action runs on the same room is a
        normal, safe use case.
        """
        if not self._require_connection():
            return
        rooms = self._selected_rooms()
        if not rooms:
            QMessageBox.warning(self, "No Rooms", "Select at least one operating room.")
            return

        creds = DeploymentCredentials(
            ssh_password=self.password_input.text() or None,
            sudo_password=self.sudo_password_input.text() or None,
        )

        for room in rooms:
            worker = LogTailWorker(self.config, creds, room, service)
            panel = self._launch_job(title, worker, [room], lock_rooms=False)
            panel.status_label.setText("Watching\u2026")
            panel.status_label.setStyleSheet("font-weight:700; color:#22b8cf;")
            panel.progress.setRange(0, 0)  # indeterminate "live" indicator
            panel.cancel_btn.setText("Stop")
            worker.stopped.connect(
                lambda _p=panel, t=title: self._finish_job(_p, t, ok=True)
            )
            worker.start()

    def _watch_matrix_api_live(self) -> None:
        self._watch_service_live(self.config.connection.service_name, "Watch matrix-api")

    def _watch_nms_live(self) -> None:
        self._watch_service_live(self.config.connection.nms_service_name, "Watch NMS")

    # -- configuration validation ----------------------------------------

    def _missing_critical(self) -> List[str]:
        """Labels of critical connection fields that are still blank. These
        are required for any room operation."""
        missing = []
        if not self.router_ip_input.text().strip():
            missing.append("Router IP")
        if not self.username_input.text().strip():
            missing.append("SSH Username")
        return missing

    def _refresh_config_status(self) -> None:
        """Repaint the Settings-tab status banner from current field state."""
        label = getattr(self, "config_status_label", None)
        if label is None:
            return
        missing = self._missing_critical()
        if missing:
            label.setText(
                "\u26a0  Not ready: fill in " + " and ".join(missing)
                + " before running any room operation."
            )
            label.setStyleSheet(
                "padding:10px 12px; border-radius:6px; font-weight:600;"
                "background:#FDECEA; color:#C62828; border:1px solid #F5C6CB;"
            )
        else:
            label.setText("\u2713  Ready: required connection settings are set.")
            label.setStyleSheet(
                "padding:10px 12px; border-radius:6px; font-weight:600;"
                "background:#E8F5E9; color:#2E7D32; border:1px solid #A5D6A7;"
            )

    def _warn_if_unconfigured(self) -> None:
        missing = self._missing_critical()
        if not missing:
            return
        self.top_tabs.setCurrentIndex(1)  # Settings tab
        QMessageBox.information(
            self,
            "Configuration Needed",
            "Before using Matrix Deploy, set the following in the Settings "
            "tab:\n\n\u2022 " + "\n\u2022 ".join(missing)
            + "\n\nMost operations also need the Sudo Password.",
        )

    def _require_connection(self) -> bool:
        """Gate for room operations. Returns True if the critical connection
        fields are set; otherwise warns, jumps to Settings, and returns False."""
        missing = self._missing_critical()
        if not missing:
            return True
        self.top_tabs.setCurrentIndex(1)  # Settings tab
        QMessageBox.warning(
            self,
            "Configuration Needed",
            "Set the following in the Settings tab first:\n\n\u2022 "
            + "\n\u2022 ".join(missing),
        )
        return False

    def _save_settings_clicked(self) -> None:
        self._save_settings()
        self._refresh_config_status()
        self.statusBar().showMessage("Settings saved", 2000)

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
        self.jenkins_username_input.setText(data.get("jenkins_username", ""))

        # Secrets are prefilled from .env only (never from the settings file)
        # and are still never persisted back to disk by this app.
        secrets = load_env_secrets()
        if "ssh_password" in secrets:
            self.password_input.setText(secrets["ssh_password"])
        if "sudo_password" in secrets:
            self.sudo_password_input.setText(secrets["sudo_password"])
        if "artifactory_token" in secrets:
            self.artifactory_token_input.setText(secrets["artifactory_token"])
        if "jenkins_token" in secrets:
            self.jenkins_token_input.setText(secrets["jenkins_token"])

    def _save_settings(self) -> None:
        data = {
            "router_ip": self.router_ip_input.text(),
            "username": self.username_input.text(),
            "swu_file": self.swu_file_input.text(),
            "config_file": self.config_file_input.text(),
            "artifactory_email": self.artifactory_email_input.text(),
            "jenkins_username": self.jenkins_username_input.text(),
            # Passwords and tokens are intentionally NOT persisted.
        }
        try:
            SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        running = [p for p in self._jobs if not p.finished]
        if running:
            reply = QMessageBox.question(
                self,
                "Jobs Running",
                f"{len(running)} job(s) are still running. Quit anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            for panel in running:
                if panel.worker is not None:
                    panel.worker.cancel()
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
