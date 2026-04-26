from __future__ import annotations

import ctypes
import html
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from taskbot.config import (
    discover_config_path,
    editable_config_path,
    ensure_runtime_directories,
    load_config,
    save_config_overrides,
)
from taskbot.store import (
    StoredTask,
    create_board,
    create_task,
    delete_board,
    delete_task,
    edit_task,
    ensure_task_store,
    load_store_snapshot,
    phase_labels,
    move_task_to_board,
    rename_board,
    store_path,
    update_task_phase,
)
from taskbot.terminal_stream import terminal_log_path


PHASE_TITLES = {
    "backlog": "Backlog",
    "planning": "Planning",
    "ready": "Ready",
    "in_progress": "In Progress",
    "needs_testing": "Needs Testing",
    "blocked": "Blocked",
    "completed": "Completed",
}
APPROVAL_POLICIES = ["never", "on-request", "on-failure", "untrusted"]
SANDBOX_MODES = ["workspace-write", "read-only", "danger-full-access"]
VERIFICATION_MODES = {
    "manual": "Manual testing only",
    "commands": "Run configured commands",
}
MODEL_CHOICES = [
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3",
    "gpt-5.3-mini",
    "gpt-5.2",
    "gpt-5.2-mini",
]
REASONING_EFFORT_CHOICES = [
    "low",
    "medium",
    "high",
    "xhigh",
]
REASONING_EFFORT_INHERIT_LABEL = "Inherit current Codex behavior"

RUNNER_CONTROL_TOOLTIPS = {
    "plan_once": "Run the planner for the next runnable task once, then stop.",
    "run_once": (
        "Run one full task pass for the next runnable task, including implementation "
        "and verification, then stop."
    ),
    "start_loop": "Keep running full task passes until you press Stop.",
    "stop": "Request the active runner to stop after the current phase finishes.",
}

ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
ANSI_FG_COLORS = {
    30: "#5a6470",
    31: "#d05c50",
    32: "#76c27a",
    33: "#d7b45b",
    34: "#6ca7ff",
    35: "#c787e8",
    36: "#64c8d8",
    37: "#dde6ef",
    90: "#7e8a96",
    91: "#f07a6c",
    92: "#8fda92",
    93: "#e8ca78",
    94: "#8ab8ff",
    95: "#d49bf2",
    96: "#7cdce8",
    97: "#f3f7fb",
}


def _macos_window_server_available() -> bool:
    if sys.platform != "darwin":
        return True

    try:
        application_services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        core_graphics = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
    except OSError:
        return True

    session_copy = application_services.CGSessionCopyCurrentDictionary
    session_copy.restype = ctypes.c_void_p
    main_display = core_graphics.CGMainDisplayID
    main_display.restype = ctypes.c_uint32
    return bool(session_copy()) and int(main_display()) != 0


def _macos_command_line_tools_python() -> bool:
    if sys.platform != "darwin":
        return False

    marker = "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework"
    for candidate in (sys.executable, sys.base_prefix):
        try:
            resolved = str(Path(candidate).resolve())
        except OSError:
            resolved = str(candidate)
        if resolved.startswith(marker):
            return True
    return False


def _ui_launch_preflight_error() -> Optional[str]:
    allow_headless = os.getenv("TASKBOT_UI_ALLOW_HEADLESS", "").strip().lower()
    if allow_headless in {"1", "true", "yes"}:
        return None

    if sys.platform == "darwin" and not _macos_window_server_available():
        return (
            "No active macOS desktop session was detected. Qt crashes during startup "
            "when it cannot reach WindowServer, so the Taskbot UI cannot launch from "
            "this shell. Start it from a logged-in desktop session, or for headless "
            "automation set TASKBOT_UI_ALLOW_HEADLESS=1 and QT_QPA_PLATFORM=offscreen."
        )

    return None


def _ansi_state_to_css(state: Dict[str, Any]) -> str:
    styles: List[str] = []
    if state.get("fg"):
        styles.append("color:{0}".format(state["fg"]))
    if state.get("bold"):
        styles.append("font-weight:700")
    if state.get("dim"):
        styles.append("opacity:0.72")
    return "; ".join(styles)


def _ansi_text_to_html(text: str, font_family: str) -> str:
    state: Dict[str, Any] = {"fg": None, "bold": False, "dim": False}
    parts: List[str] = []
    position = 0

    def append_segment(segment: str) -> None:
        if not segment:
            return
        escaped = html.escape(segment)
        css = _ansi_state_to_css(state)
        if css:
            parts.append('<span style="{0}">{1}</span>'.format(css, escaped))
        else:
            parts.append(escaped)

    for match in ANSI_SGR_RE.finditer(text):
        append_segment(text[position:match.start()])
        raw_codes = match.group(1)
        codes = [0] if raw_codes == "" else [int(code or "0") for code in raw_codes.split(";")]
        for code in codes:
            if code == 0:
                state = {"fg": None, "bold": False, "dim": False}
            elif code == 1:
                state["bold"] = True
            elif code == 2:
                state["dim"] = True
            elif code == 22:
                state["bold"] = False
                state["dim"] = False
            elif code in ANSI_FG_COLORS:
                state["fg"] = ANSI_FG_COLORS[code]
            elif code == 39:
                state["fg"] = None
        position = match.end()

    append_segment(text[position:])
    return (
        '<html><body style="margin:0; background:#14191e;">'
        '<pre style="margin:0; white-space:pre; '
        'font-family:\'{0}\'; font-size:10pt; color:#d7e0ea;">{1}</pre>'
        "</body></html>"
    ).format(html.escape(font_family, quote=True), "".join(parts))


def _path_signature(path: Path) -> tuple[bool, int, int]:
    try:
        stat_result = path.stat()
    except OSError:
        return (False, 0, 0)
    return (True, stat_result.st_mtime_ns, stat_result.st_size)


def _terminal_text_should_refresh(
    last_terminal_text: Optional[str],
    current_terminal_text: str,
) -> bool:
    return last_terminal_text is None or current_terminal_text != last_terminal_text


def _boards_from_store_snapshot(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    boards: List[Dict[str, Any]] = []
    for payload in store.get("boards", []):
        if not isinstance(payload, dict):
            continue
        boards.append(
            {
                "board_id": str(payload.get("board_id", "")),
                "title": str(payload.get("title", "")),
                "order": int(payload.get("order", 0) or 0),
            }
        )
    boards.sort(
        key=lambda board: (
            1 if str(board["board_id"]).strip().lower() == "archived" or str(board["title"]).strip().lower() == "archived" else 0,
            board["order"],
            board["title"].lower(),
        )
    )
    return boards


def _verification_commands_to_lines(config: Dict[str, Any]) -> str:
    commands = config.get("verification", {}).get("commands", [])
    lines: List[str] = []
    for entry in commands:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        parts = [str(part) for part in entry.get("command", []) if str(part).strip()]
        if not parts:
            continue
        lines.append(shlex.join(parts))
    return "\n".join(lines)


def _resolved_verification_mode(config: Dict[str, Any]) -> str:
    verification = config.get("verification", {})
    mode = str(verification.get("mode", "auto")).strip().lower()
    if mode in VERIFICATION_MODES:
        return mode
    commands = verification.get("commands", [])
    has_commands = any(isinstance(entry, dict) and entry.get("enabled", True) for entry in commands)
    return "commands" if has_commands else "manual"


def _command_name(parts: List[str], index: int) -> str:
    token = Path(parts[0]).name if parts else "check"
    slug = re.sub(r"[^a-z0-9]+", "-", token.lower()).strip("-")
    return slug or "check-{0}".format(index)


def _parse_verification_command_lines(raw_text: str) -> List[Dict[str, Any]]:
    commands: List[Dict[str, Any]] = []
    for index, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            raise ValueError("Invalid verification command on line {0}: {1}".format(index, exc)) from exc
        if not parts:
            continue
        commands.append(
            {
                "name": _command_name(parts, index),
                "command": parts,
                "enabled": True,
                "timeout_seconds": 300,
            }
        )
    return commands


def _repo_agents_path(repo_root: Path) -> Path:
    resolved_repo = repo_root.resolve()
    for candidate_name in ("agents.md", "AGENTS.md"):
        candidate = resolved_repo / candidate_name
        if candidate.exists():
            return candidate
    return resolved_repo / "agents.md"


def _config_path_label_for_header(active_config: Dict[str, Any]) -> str:
    config_path_text = str(active_config.get("config_path", "")).strip()
    if not config_path_text:
        return "defaults only"

    config_path = Path(config_path_text).expanduser()
    if not config_path.is_absolute():
        return config_path_text

    repo_root_text = str(active_config.get("repo_root", "")).strip()
    if not repo_root_text:
        return config_path_text

    repo_root = Path(repo_root_text).expanduser()
    try:
        resolved_config_path = config_path.resolve()
        resolved_repo_root = repo_root.resolve()
        return str(resolved_config_path.relative_to(resolved_repo_root))
    except (OSError, ValueError):
        try:
            return str(config_path.resolve())
        except OSError:
            return config_path_text


def _configure_reasoning_effort_dropdown(dropdown: Any, configured_value: Any) -> None:
    raw_value = "" if configured_value is None else str(configured_value).strip()
    dropdown.addItem(REASONING_EFFORT_INHERIT_LABEL, "")
    supported_values = list(REASONING_EFFORT_CHOICES)
    if raw_value and raw_value not in supported_values:
        supported_values.insert(0, raw_value)
    for value in supported_values:
        dropdown.addItem(value, value)
    dropdown.setCurrentData(raw_value)


def _checkbox_indicator_tick_icon_path() -> Path:
    return Path(__file__).resolve().parent / "assets" / "checkbox-tick.svg"


def _trash_icon_path() -> Path:
    return Path(__file__).resolve().parent / "assets" / "trash-can.svg"


START_LOOP_DIALOG_DEFAULT_ITERATIONS = 5


def _start_loop_run_args(run_indefinitely: bool, iterations: int) -> List[str]:
    if run_indefinitely:
        return ["run", "--continuous"]
    return ["run", "--iterations", str(max(1, int(iterations)))]


def _command_enter_shortcut_sequences() -> tuple[str, str]:
    return ("Ctrl+Return", "Ctrl+Enter")


def _command_enter_modifier(qt_namespace: Any) -> Any:
    # Qt maps macOS Command to ControlModifier for shortcut matching.
    return qt_namespace.ControlModifier


def _install_command_enter_shortcuts(
    parent: Any,
    on_activated: Any,
    shortcut_cls: Any,
    key_sequence_cls: Any,
    shortcut_context: Any,
) -> List[Any]:
    shortcuts: List[Any] = []
    for sequence in _command_enter_shortcut_sequences():
        shortcut = shortcut_cls(key_sequence_cls(sequence), parent)
        shortcut.setContext(shortcut_context)
        shortcut.activated.connect(on_activated)
        shortcuts.append(shortcut)
    return shortcuts


def _command_enter_dialog_candidate(watched: Any, active_modal_widget: Any) -> Any:
    if active_modal_widget is not None:
        return active_modal_widget

    window_getter = getattr(watched, "window", None)
    if callable(window_getter):
        return window_getter()

    return None


def _command_enter_preferred_button(buttons: List[Any]) -> Any:
    enabled_buttons: List[Any] = []
    for button in buttons:
        if button is None:
            continue
        is_enabled = getattr(button, "isEnabled", None)
        if callable(is_enabled) and not is_enabled():
            continue
        enabled_buttons.append(button)
        text_getter = getattr(button, "text", None)
        if callable(text_getter):
            label = str(text_getter()).replace("&", "").strip().lower()
            if label == "ok":
                return button

    for button in enabled_buttons:
        is_default = getattr(button, "isDefault", None)
        if callable(is_default) and is_default():
            return button

    return enabled_buttons[0] if enabled_buttons else None


def _command_enter_should_activate(
    event: Any,
    activation_event_types: tuple[Any, ...],
    return_keys: tuple[Any, ...],
    meta_modifier: Any,
) -> bool:
    event_type_getter = getattr(event, "type", None)
    key_getter = getattr(event, "key", None)
    modifiers_getter = getattr(event, "modifiers", None)
    if not callable(event_type_getter) or not callable(key_getter) or not callable(modifiers_getter):
        return False
    if event_type_getter() not in activation_event_types:
        return False
    if key_getter() not in return_keys:
        return False
    return bool(modifiers_getter() & meta_modifier)


def _command_enter_activate_candidate(candidate_widget: Any, button_cls: Any) -> bool:
    if candidate_widget is None:
        return False

    buttons: List[Any] = []
    find_children = getattr(candidate_widget, "findChildren", None)
    if callable(find_children):
        try:
            buttons = list(find_children(button_cls))
        except TypeError:
            buttons = list(find_children())

    default_button_getter = getattr(candidate_widget, "defaultButton", None)
    if callable(default_button_getter):
        candidate_button = default_button_getter()
        if candidate_button is not None and candidate_button not in buttons:
            buttons.append(candidate_button)

    preferred_button = _command_enter_preferred_button(buttons)
    if preferred_button is not None:
        click = getattr(preferred_button, "click", None)
        if callable(click):
            click()
            return True

    accept = getattr(candidate_widget, "accept", None)
    if callable(accept):
        accept()
        return True

    return False


def _command_enter_submit_dialog(watched: Any, active_modal_widget: Any, button_cls: Any) -> bool:
    candidate_widget = _command_enter_dialog_candidate(watched, active_modal_widget)
    if candidate_widget is None:
        return False
    inherits = getattr(candidate_widget, "inherits", None)
    if callable(inherits) and not inherits("QDialog"):
        return False
    return _command_enter_activate_candidate(candidate_widget, button_cls)


TASK_DRAG_MIME_TYPE = "application/x-taskbot-task-id"
TASK_DRAG_PHASE_MIME_TYPE = "application/x-taskbot-task-phase"


def _drag_payload_from_mime_data(mime_data: Any) -> tuple[str, str]:
    task_id = ""
    source_phase = ""
    if mime_data is None:
        return task_id, source_phase

    has_format = getattr(mime_data, "hasFormat", None)
    data_getter = getattr(mime_data, "data", None)
    if callable(has_format) and callable(data_getter):
        if has_format(TASK_DRAG_MIME_TYPE):
            try:
                task_id = bytes(data_getter(TASK_DRAG_MIME_TYPE)).decode("utf-8").strip()
            except Exception:
                task_id = ""
        if has_format(TASK_DRAG_PHASE_MIME_TYPE):
            try:
                source_phase = bytes(data_getter(TASK_DRAG_PHASE_MIME_TYPE)).decode("utf-8").strip()
            except Exception:
                source_phase = ""

    if not task_id:
        text_getter = getattr(mime_data, "text", None)
        if callable(text_getter):
            task_id = str(text_getter()).strip()

    return task_id, source_phase


def _set_drag_highlight(widget: QWidget, active: bool) -> None:
    if bool(widget.property("dragOver")) == active:
        return
    widget.setProperty("dragOver", active)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def _phase_label(phase: str) -> str:
    return PHASE_TITLES.get(phase, phase.replace("_", " ").title())


def _task_card_can_start_task(phase: str) -> bool:
    return phase in {"backlog", "planning"}


def _start_task_run_args(task_id: str) -> List[str]:
    return ["run", "--task-id", task_id]


def _taskbot_title_html() -> str:
    letters = [
        ("T", "#c8643b"),
        ("A", "#d07a34"),
        ("S", "#d39b3c"),
        ("K", "#c9a64b"),
        ("B", "#6f9d5f"),
        ("O", "#4f98a6"),
        ("T", "#8b6bd1"),
    ]
    return "".join(
        '<span style="color:{0};">{1}</span>'.format(color, html.escape(letter))
        for letter, color in letters
    )


def launch_ui(config: Dict[str, Any]) -> int:
    preflight_error = _ui_launch_preflight_error()
    if preflight_error is not None:
        print(preflight_error, file=sys.stderr)
        return 1

    try:
        from PySide6.QtCore import QEvent, QObject, QSize, QTimer, Qt, QMimeData, QRect
        from PySide6.QtGui import QAction, QDrag, QFont, QFontDatabase, QIcon, QKeySequence, QShortcut, QTextCursor, QColor, QPainter, QPalette
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QDialog,
            QDialogButtonBox,
            QFileDialog,
            QFrame,
            QGridLayout,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMessageBox,
            QMenu,
            QPlainTextEdit,
            QPushButton,
            QScrollArea,
            QScrollBar,
            QSizePolicy,
            QSpinBox,
            QStyledItemDelegate,
            QSplitter,
            QSpacerItem,
            QStyle,
            QToolButton,
            QVBoxLayout,
            QWidget,
            QTextEdit,
            QSplitterHandle,
        )
    except ModuleNotFoundError:
        print(
            "PySide6 is not installed. Install it with: python3 -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    refresh_ms = max(250, int(float(config.get("ui", {}).get("refresh_seconds", 1.0)) * 1000))
    tail_lines = int(config.get("ui", {}).get("terminal_tail_lines", 250))
    app_root = Path(config.get("app_root", Path(__file__).resolve().parents[1])).resolve()
    taskbot_entry = app_root / "taskbot.py"
    ui_state_path = app_root / "state" / "ui_session.json"

    def _resolve_repo_path(raw_value: str) -> Path:
        candidate = Path(raw_value).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (Path.cwd() / candidate).resolve()

    def _runtime_config_for_repo(repo_root: Path) -> Dict[str, Any]:
        config_path = discover_config_path(app_root, repo_root.resolve())
        active_config = load_config(repo_root.resolve(), config_path, app_root=app_root)
        ensure_runtime_directories(active_config)
        ensure_task_store(active_config)
        return active_config

    def _load_saved_session() -> Dict[str, str]:
        if not ui_state_path.exists():
            return {}
        try:
            payload = json.loads(ui_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            "repo_root": str(payload.get("repo_root", "")).strip(),
            "selected_board_id": str(payload.get("selected_board_id", "")).strip(),
        }

    def _save_session(repo_root: Path, selected_board_id: Optional[str]) -> None:
        ui_state_path.parent.mkdir(parents=True, exist_ok=True)
        ui_state_path.write_text(
            json.dumps(
                {
                    "repo_root": str(repo_root.resolve()),
                    "selected_board_id": selected_board_id or "",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _preferred_monospace_family() -> str:
        candidates = [
            "JetBrains Mono",
            "Berkeley Mono",
            "SF Mono",
            "Menlo",
            "Monaco",
            "Consolas",
            "Liberation Mono",
            "DejaVu Sans Mono",
            "Courier New",
        ]
        available = {family.lower(): family for family in QFontDatabase().families()}
        for candidate in candidates:
            match = available.get(candidate.lower())
            if match:
                return match
        return "Monospace"

    class _FormDropdown(QToolButton):
        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setObjectName("DialogDropdown")
            self.setPopupMode(QToolButton.InstantPopup)
            self.setToolButtonStyle(Qt.ToolButtonTextOnly)
            self.setFocusPolicy(Qt.StrongFocus)
            self.setCursor(Qt.PointingHandCursor)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.setMinimumHeight(32)
            self._menu = QMenu(self)
            self._menu.setObjectName("DialogDropdownMenu")
            self.setMenu(self._menu)
            self._entries: List[Dict[str, Any]] = []
            self._current_index = -1

        def _sync_display(self) -> None:
            current_text = self.currentText()
            self.setText(current_text)
            self.setToolTip(current_text)

        def _select_index(self, index: int) -> None:
            if index < 0 or index >= len(self._entries):
                self._current_index = -1
                self._sync_display()
                return
            self._current_index = index
            self._sync_display()

        def addItem(self, text: str, user_data: Any = None) -> None:
            label = str(text)
            data = label if user_data is None else user_data
            action = QAction(label, self)
            action.setData(data)
            index = len(self._entries)
            action.triggered.connect(lambda _checked=False, selected=index: self.setCurrentIndex(selected))
            self._menu.addAction(action)
            self._entries.append({"text": label, "data": data, "action": action})
            if self._current_index < 0:
                self._select_index(index)

        def addItems(self, texts: List[str]) -> None:
            for text in texts:
                self.addItem(text)

        def addCustomAction(self, text: str, callback: Any) -> QAction:
            if self._entries and self._menu.actions():
                self._menu.addSeparator()
            action = self._menu.addAction(text)
            action.triggered.connect(lambda _checked=False: callback())
            return action

        def clear(self) -> None:
            self._menu.clear()
            self._entries.clear()
            self._current_index = -1
            self._sync_display()

        def count(self) -> int:
            return len(self._entries)

        def currentIndex(self) -> int:
            return self._current_index

        def currentText(self) -> str:
            if 0 <= self._current_index < len(self._entries):
                return str(self._entries[self._current_index]["text"])
            return ""

        def currentData(self) -> Any:
            if 0 <= self._current_index < len(self._entries):
                return self._entries[self._current_index]["data"]
            return None

        def findText(self, text: str) -> int:
            target = str(text)
            for index, entry in enumerate(self._entries):
                if entry["text"] == target:
                    return index
            return -1

        def findData(self, data: Any) -> int:
            for index, entry in enumerate(self._entries):
                if entry["data"] == data:
                    return index
            return -1

        def setCurrentIndex(self, index: int) -> None:
            self._select_index(index)

        def setCurrentText(self, text: str) -> None:
            target = str(text)
            index = self.findText(target)
            if index < 0:
                if target:
                    self.addItem(target, target)
                    index = len(self._entries) - 1
                else:
                    self._select_index(-1)
                    return
            self._select_index(index)

        def setCurrentData(self, data: Any) -> None:
            index = self.findData(data)
            if index < 0:
                if data is None:
                    self._select_index(-1)
                    return
                label = str(data)
                self.addItem(label, data)
                index = len(self._entries) - 1
            self._select_index(index)

    class _CenteredSplitterHandle(QSplitterHandle):
        def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
            super().__init__(orientation, parent)
            self.setMouseTracking(True)
            self.setAttribute(Qt.WA_Hover, True)

        def _line_rect(self) -> QRect:
            rect = self.rect()
            if rect.width() <= 0 or rect.height() <= 0:
                return QRect()

            thickness = 2
            if self.orientation() == Qt.Horizontal:
                line_length = min(50, max(0, rect.height() - 16))
                if line_length <= 0:
                    return QRect()
                x = rect.x() + (rect.width() - thickness) // 2
                y = rect.y() + (rect.height() - line_length) // 2
                return QRect(x, y, thickness, line_length)

            line_length = min(50, max(0, rect.width() - 16))
            if line_length <= 0:
                return QRect()
            x = rect.x() + (rect.width() - line_length) // 2
            y = rect.y() + (rect.height() - thickness) // 2
            return QRect(x, y, line_length, thickness)

        def enterEvent(self, event) -> None:
            self.update()
            super().enterEvent(event)

        def leaveEvent(self, event) -> None:
            self.update()
            super().leaveEvent(event)

        def paintEvent(self, event) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            line_rect = self._line_rect()
            if not line_rect.isValid():
                return
            hovered = self.underMouse()
            color = QColor(77, 62, 51, 114 if hovered else 72)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(line_rect, 1, 1)

    class _CenteredLineSplitter(QSplitter):
        def createHandle(self) -> QSplitterHandle:
            return _CenteredSplitterHandle(self.orientation(), self)

    def _set_primary_button_default(buttons: QDialogButtonBox, standard_button: Any) -> None:
        primary_button = buttons.button(standard_button)
        if primary_button is not None:
            primary_button.setDefault(True)
            primary_button.setAutoDefault(True)

    class CommandEnterDialog(QDialog):
        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._command_enter_shortcuts = _install_command_enter_shortcuts(
                self,
                lambda: _command_enter_submit_dialog(self, self, QPushButton),
                QShortcut,
                QKeySequence,
                Qt.WindowShortcut,
            )

    class CommandEnterModalFilter(QObject):
        def eventFilter(self, watched: QObject, event: Any) -> bool:
            if not _command_enter_should_activate(
                event,
                (QEvent.ShortcutOverride, QEvent.KeyPress),
                (Qt.Key_Return, Qt.Key_Enter),
                _command_enter_modifier(Qt),
            ):
                return super().eventFilter(watched, event)

            if _command_enter_submit_dialog(watched, app.activeModalWidget(), QPushButton):
                return True

            return super().eventFilter(watched, event)

    class BoardTitleDialog(CommandEnterDialog):
        def __init__(self,
                     *,
                     window_title: str,
                     dialog_title: str,
                     caption: str,
                     board_title: str = "",
                     parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setObjectName("AppDialog")
            self.setWindowTitle(window_title)
            self.setModal(False)
            self.resize(400, 180)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(10)

            title = QLabel(dialog_title)
            title.setObjectName("DialogTitle")
            layout.addWidget(title)

            caption_label = QLabel(caption)
            caption_label.setObjectName("DialogCaption")
            caption_label.setWordWrap(True)
            layout.addWidget(caption_label)

            self.title_input = QLineEdit()
            self.title_input.setPlaceholderText("Board title")
            self.title_input.setMinimumHeight(32)
            self.title_input.setText(board_title)
            layout.addWidget(self.title_input)

            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.setObjectName("DialogButtons")
            _set_primary_button_default(buttons, QDialogButtonBox.Ok)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addSpacing(6)
            layout.addWidget(buttons)

            if board_title:
                self.title_input.selectAll()
            self.title_input.setFocus()

        def accept(self) -> None:
            if not self.board_title():
                QMessageBox.warning(self, "Board Title Required", "Enter a board title.")
                self.title_input.setFocus()
                return
            super().accept()

        def board_title(self) -> str:
            return self.title_input.text().strip()

    class AddBoardDialog(BoardTitleDialog):
        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(
                window_title="New Board",
                dialog_title="Create Board",
                caption="Boards can stay empty until you need them.",
                parent=parent,
            )

    class AddTaskDialog(CommandEnterDialog):
        def __init__(self,
                     board_titles: List[str],
                     default_board: str,
                     parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setObjectName("AppDialog")
            self.setWindowTitle("Add Task")
            self.setModal(False)
            self.resize(540, 400)

            available_boards = list(board_titles)
            if default_board and default_board not in available_boards:
                available_boards.append(default_board)
            if not available_boards:
                available_boards.append("General")

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(10)

            title = QLabel("Add Task")
            title.setObjectName("DialogTitle")
            layout.addWidget(title)

            caption = QLabel("Create a task in the selected board. Context is optional.")
            caption.setObjectName("DialogCaption")
            caption.setWordWrap(True)
            layout.addWidget(caption)

            board_label = QLabel("Board")
            board_label.setObjectName("FieldLabel")
            layout.addWidget(board_label)

            self.board_dropdown = _FormDropdown()
            self.board_dropdown.addItems(available_boards)
            self.board_dropdown.setCurrentText(default_board or available_boards[0])
            layout.addWidget(self.board_dropdown)

            task_label = QLabel("Title")
            task_label.setObjectName("FieldLabel")
            layout.addWidget(task_label)

            self.title_input = QLineEdit()
            self.title_input.setPlaceholderText("What needs to be done?")
            layout.addWidget(self.title_input)

            context_label = QLabel("Context")
            context_label.setObjectName("FieldLabel")
            layout.addWidget(context_label)

            self.context_input = QPlainTextEdit()
            self.context_input.setPlaceholderText("Optional notes or constraints.")
            self.context_input.setFixedHeight(96)
            layout.addWidget(self.context_input)

            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.setObjectName("DialogButtons")
            _set_primary_button_default(buttons, QDialogButtonBox.Ok)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addSpacing(8)
            layout.addWidget(buttons)

            self.title_input.setFocus()

        def accept(self) -> None:
            if not self.task_title():
                QMessageBox.warning(self, "Task Title Required", "Enter a task title.")
                self.title_input.setFocus()
                return
            if not self.board_title():
                QMessageBox.warning(self, "Board Required", "Choose a board.")
                self.board_dropdown.setFocus()
                return
            super().accept()

        def board_title(self) -> str:
            return self.board_dropdown.currentText().strip()

        def task_title(self) -> str:
            return self.title_input.text().strip()

        def context_notes(self) -> str:
            return self.context_input.toPlainText().strip()

    class EditTaskDialog(CommandEnterDialog):
        def __init__(self,
                     task: StoredTask,
                     board_titles: List[str],
                     phases: List[str],
                     new_board_callback: Any | None = None,
                     parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.task = task
            self._new_board_callback = new_board_callback
            self.setObjectName("AppDialog")
            self.setWindowTitle("Edit Task")
            self.setModal(False)
            self.resize(540, 470)

            available_boards = list(board_titles)
            if task.board_title and task.board_title not in available_boards:
                available_boards.append(task.board_title)
            if not available_boards:
                available_boards.append("General")

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(10)

            title = QLabel("Edit Task")
            title.setObjectName("DialogTitle")
            layout.addWidget(title)

            caption_text = "Update the board, phase, title, or notes."
            if task.source_kind != "ui":
                caption_text = "Markdown-synced tasks may be overridden by the source file."
            caption = QLabel(caption_text)
            caption.setObjectName("DialogCaption")
            caption.setWordWrap(True)
            layout.addWidget(caption)

            board_label = QLabel("Board")
            board_label.setObjectName("FieldLabel")
            layout.addWidget(board_label)

            self.board_dropdown = _FormDropdown()
            self.board_dropdown.addItems(available_boards)
            self.board_dropdown.setCurrentText(task.board_title)
            if self._new_board_callback is not None:
                self.board_dropdown.addCustomAction("New board...", self._create_new_board)
            layout.addWidget(self.board_dropdown)

            phase_label = QLabel("Phase")
            phase_label.setObjectName("FieldLabel")
            layout.addWidget(phase_label)

            self.phase_dropdown = _FormDropdown()
            for phase in phases:
                self.phase_dropdown.addItem(PHASE_TITLES.get(phase, phase), phase)
            self.phase_dropdown.setCurrentData(task.phase)
            layout.addWidget(self.phase_dropdown)

            task_label = QLabel("Title")
            task_label.setObjectName("FieldLabel")
            layout.addWidget(task_label)

            self.title_input = QLineEdit()
            self.title_input.setPlaceholderText("What needs to be done?")
            self.title_input.setText(task.title)
            layout.addWidget(self.title_input)

            context_label = QLabel("Context")
            context_label.setObjectName("FieldLabel")
            layout.addWidget(context_label)

            self.context_input = QPlainTextEdit()
            self.context_input.setPlaceholderText("Optional notes or constraints.")
            self.context_input.setFixedHeight(112)
            self.context_input.setPlainText(task.context_notes)
            layout.addWidget(self.context_input)

            buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            buttons.setObjectName("DialogButtons")
            _set_primary_button_default(buttons, QDialogButtonBox.Save)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addSpacing(8)
            layout.addWidget(buttons)

            self.title_input.selectAll()
            self.title_input.setFocus()

        def accept(self) -> None:
            if not self.task_title():
                QMessageBox.warning(self, "Task Title Required", "Enter a task title.")
                self.title_input.setFocus()
                return
            if not self.board_title():
                QMessageBox.warning(self, "Board Required", "Choose or enter a board.")
                self.board_dropdown.setFocus()
                return
            if not self.phase_value():
                QMessageBox.warning(self, "Phase Required", "Choose a workflow phase.")
                self.phase_dropdown.setFocus()
                return
            super().accept()

        def board_title(self) -> str:
            return self.board_dropdown.currentText().strip()

        def phase_value(self) -> str:
            return str(self.phase_dropdown.currentData() or "").strip()

        def task_title(self) -> str:
            return self.title_input.text().strip()

        def context_notes(self) -> str:
            return self.context_input.toPlainText().strip()

        def _create_new_board(self) -> None:
            if self._new_board_callback is None:
                return
            created_board_title = self._new_board_callback()
            if not created_board_title:
                return
            if self.board_dropdown.findText(created_board_title) < 0:
                self.board_dropdown.addItem(created_board_title, created_board_title)
            self.board_dropdown.setCurrentText(created_board_title)

    class SettingsDialog(CommandEnterDialog):
        def __init__(self, active_config: Dict[str, Any], parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.active_config = active_config
            self.setObjectName("AppDialog")
            self.setWindowTitle("Settings")
            self.setModal(False)

            codex_config = active_config.get("codex", {})
            model_config = active_config.get("models", {})
            planning_config = active_config.get("planning", {})
            verification_config = active_config.get("verification", {})
            git_config = active_config.get("git", {})

            def _configure_model_dropdown(dropdown: Any, configured_value: Any, fallback_value: str) -> None:
                raw_value = "" if configured_value is None else str(configured_value).strip()
                selected_value = raw_value or fallback_value
                model_options = list(MODEL_CHOICES)
                if selected_value and selected_value not in model_options:
                    model_options.insert(0, selected_value)
                dropdown.addItems(model_options)
                dropdown.setCurrentText(selected_value)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(10)

            title = QLabel("Runner Settings")
            title.setObjectName("DialogTitle")
            layout.addWidget(title)

            caption = QLabel(
                "Repository settings for Codex permissions, default models, reasoning effort, verification, and git publishing."
            )
            caption.setObjectName("DialogCaption")
            caption.setWordWrap(True)
            layout.addWidget(caption)

            settings_scroll = QScrollArea()
            settings_scroll.setObjectName("SettingsScrollArea")
            settings_scroll.setWidgetResizable(True)
            settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            settings_scroll.setFrameShape(QFrame.NoFrame)
            settings_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

            settings_content = QWidget()
            settings_content.setObjectName("SettingsContent")
            settings_layout = QVBoxLayout(settings_content)
            settings_layout.setContentsMargins(0, 0, 0, 0)
            settings_layout.setSpacing(10)

            config_label = QLabel("Config")
            config_label.setObjectName("FieldLabel")
            settings_layout.addWidget(config_label)

            config_value = QLabel(str(editable_config_path(active_config)))
            config_value.setObjectName("SidebarCaption")
            config_value.setWordWrap(True)
            settings_layout.addWidget(config_value)

            sandbox_label = QLabel("Sandbox")
            sandbox_label.setObjectName("FieldLabel")
            settings_layout.addWidget(sandbox_label)

            self.sandbox_dropdown = _FormDropdown()
            self.sandbox_dropdown.addItems(SANDBOX_MODES)
            self.sandbox_dropdown.setCurrentText(str(codex_config.get("sandbox", "workspace-write")))
            settings_layout.addWidget(self.sandbox_dropdown)

            approval_label = QLabel("Approval")
            approval_label.setObjectName("FieldLabel")
            settings_layout.addWidget(approval_label)

            self.approval_dropdown = _FormDropdown()
            self.approval_dropdown.addItems(APPROVAL_POLICIES)
            self.approval_dropdown.setCurrentText(str(codex_config.get("ask_for_approval", "never")))
            settings_layout.addWidget(self.approval_dropdown)

            planner_label = QLabel("Planner")
            planner_label.setObjectName("FieldLabel")
            settings_layout.addWidget(planner_label)

            self.planner_model_dropdown = _FormDropdown()
            _configure_model_dropdown(self.planner_model_dropdown, model_config.get("planner", ""), "gpt-5.4")
            settings_layout.addWidget(self.planner_model_dropdown)

            planner_effort_label = QLabel("Planner Reasoning Effort")
            planner_effort_label.setObjectName("FieldLabel")
            settings_layout.addWidget(planner_effort_label)

            self.planner_reasoning_effort_dropdown = _FormDropdown()
            _configure_reasoning_effort_dropdown(
                self.planner_reasoning_effort_dropdown,
                model_config.get("planner_reasoning_effort", ""),
            )
            settings_layout.addWidget(self.planner_reasoning_effort_dropdown)

            implementer_label = QLabel("Implementer")
            implementer_label.setObjectName("FieldLabel")
            settings_layout.addWidget(implementer_label)

            self.implementer_model_dropdown = _FormDropdown()
            _configure_model_dropdown(
                self.implementer_model_dropdown,
                model_config.get("implementer", ""),
                "gpt-5.4-mini",
            )
            settings_layout.addWidget(self.implementer_model_dropdown)

            implementer_effort_label = QLabel("Implementer Reasoning Effort")
            implementer_effort_label.setObjectName("FieldLabel")
            settings_layout.addWidget(implementer_effort_label)

            self.implementer_reasoning_effort_dropdown = _FormDropdown()
            _configure_reasoning_effort_dropdown(
                self.implementer_reasoning_effort_dropdown,
                model_config.get("implementer_reasoning_effort", ""),
            )
            settings_layout.addWidget(self.implementer_reasoning_effort_dropdown)

            reasoning_caption = QLabel("Leave blank to inherit the current Codex reasoning effort.")
            reasoning_caption.setObjectName("SidebarCaption")
            reasoning_caption.setWordWrap(True)
            settings_layout.addWidget(reasoning_caption)

            self.fast_path_checkbox = QCheckBox("Skip full planner for tiny tasks")
            self.fast_path_checkbox.setChecked(bool(planning_config.get("auto_plan_tiny_tasks", True)))
            settings_layout.addWidget(self.fast_path_checkbox)

            verification_mode_label = QLabel("Verify Mode")
            verification_mode_label.setObjectName("FieldLabel")
            settings_layout.addWidget(verification_mode_label)

            self.verification_mode_dropdown = _FormDropdown()
            for value, label_text in VERIFICATION_MODES.items():
                self.verification_mode_dropdown.addItem(label_text, value)
            self.verification_mode_dropdown.setCurrentData(_resolved_verification_mode(active_config))
            settings_layout.addWidget(self.verification_mode_dropdown)

            verification_notes_label = QLabel("Verify Notes")
            verification_notes_label.setObjectName("FieldLabel")
            settings_layout.addWidget(verification_notes_label)

            self.verification_notes_input = QPlainTextEdit()
            self.verification_notes_input.setPlaceholderText(
                "Example: Manual QA only. Do not run automated tests in this repo."
            )
            self.verification_notes_input.setFixedHeight(96)
            self.verification_notes_input.setPlainText(str(verification_config.get("instructions", "")))
            settings_layout.addWidget(self.verification_notes_input)

            verification_commands_label = QLabel("Verify Commands")
            verification_commands_label.setObjectName("FieldLabel")
            settings_layout.addWidget(verification_commands_label)

            self.verification_commands_input = QPlainTextEdit()
            self.verification_commands_input.setPlaceholderText("One shell command per line, for example:\npython3 -m unittest")
            self.verification_commands_input.setFixedHeight(120)
            self.verification_commands_input.setPlainText(_verification_commands_to_lines(active_config))
            settings_layout.addWidget(self.verification_commands_input)

            commands_caption = QLabel("Repo-local verification hooks. Manual mode skips them.")
            commands_caption.setObjectName("SidebarCaption")
            commands_caption.setWordWrap(True)
            settings_layout.addWidget(commands_caption)

            self.git_enabled_checkbox = QCheckBox("Auto-commit and push after successful runs")
            self.git_enabled_checkbox.setChecked(bool(git_config.get("enabled", False)))
            settings_layout.addWidget(self.git_enabled_checkbox)

            self.git_require_clean_checkbox = QCheckBox("Skip publishing when local changes exist")
            self.git_require_clean_checkbox.setChecked(bool(git_config.get("require_clean_worktree", True)))
            settings_layout.addWidget(self.git_require_clean_checkbox)

            git_remote_label = QLabel("Remote")
            git_remote_label.setObjectName("FieldLabel")
            settings_layout.addWidget(git_remote_label)

            self.git_remote_input = QLineEdit()
            self.git_remote_input.setPlaceholderText("Leave blank to use upstream or the only remote")
            self.git_remote_input.setText(str(git_config.get("remote", "")))
            settings_layout.addWidget(self.git_remote_input)

            git_commit_label = QLabel("Commit Template")
            git_commit_label.setObjectName("FieldLabel")
            settings_layout.addWidget(git_commit_label)

            self.git_commit_message_input = QLineEdit()
            self.git_commit_message_input.setPlaceholderText("taskbot: {task_id} {task_title}")
            self.git_commit_message_input.setText(
                str(git_config.get("commit_message_template", "taskbot: {task_id} {task_title}"))
            )
            settings_layout.addWidget(self.git_commit_message_input)

            git_caption = QLabel(
                "Placeholders: {task_id}, {task_title}, {board_title}, {branch}, "
                "{report_status}, {final_phase}."
            )
            git_caption.setObjectName("SidebarCaption")
            git_caption.setWordWrap(True)
            settings_layout.addWidget(git_caption)

            settings_scroll.setWidget(settings_content)
            layout.addWidget(settings_scroll, 1)

            buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            buttons.setObjectName("DialogButtons")
            _set_primary_button_default(buttons, QDialogButtonBox.Save)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addSpacing(8)
            layout.addWidget(buttons)

            screen = QApplication.primaryScreen()
            if screen is not None:
                available_geometry = screen.availableGeometry()
                if available_geometry.width() > 0 and available_geometry.height() > 0:
                    max_width = max(1, available_geometry.width() - 48)
                    max_height = max(1, available_geometry.height() - 48)
                    self.setMaximumSize(max_width, max_height)
                    self.resize(min(600, max_width), min(700, max_height))
                    return
            self.resize(600, 700)

        def settings_payload(self) -> Dict[str, Any]:
            planner_model = self.planner_model_dropdown.currentText().strip()
            implementer_model = self.implementer_model_dropdown.currentText().strip()
            planner_reasoning_effort = str(self.planner_reasoning_effort_dropdown.currentData() or "").strip()
            implementer_reasoning_effort = str(self.implementer_reasoning_effort_dropdown.currentData() or "").strip()
            verification_mode = str(self.verification_mode_dropdown.currentData() or "manual").strip() or "manual"
            verification_commands = _parse_verification_command_lines(self.verification_commands_input.toPlainText())
            return {
                "codex": {
                    "sandbox": self.sandbox_dropdown.currentText().strip(),
                    "ask_for_approval": self.approval_dropdown.currentText().strip(),
                },
                "models": {
                    "planner": planner_model or "gpt-5.4",
                    "implementer": implementer_model or "gpt-5.4-mini",
                    "planner_reasoning_effort": planner_reasoning_effort,
                    "implementer_reasoning_effort": implementer_reasoning_effort,
                },
                "planning": {
                    "auto_plan_tiny_tasks": self.fast_path_checkbox.isChecked(),
                },
                "verification": {
                    "mode": verification_mode,
                    "instructions": self.verification_notes_input.toPlainText().strip(),
                    "commands": verification_commands,
                },
                "git": {
                    "enabled": self.git_enabled_checkbox.isChecked(),
                    "push_after_commit": True,
                    "require_clean_worktree": self.git_require_clean_checkbox.isChecked(),
                    "remote": self.git_remote_input.text().strip(),
                    "commit_message_template": (
                        self.git_commit_message_input.text().strip() or "taskbot: {task_id} {task_title}"
                    ),
                },
            }

        def accept(self) -> None:
            try:
                self.settings_payload()
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid Settings", str(exc))
                return
            super().accept()

    class AgentsFileDialog(CommandEnterDialog):
        def __init__(self, agents_path: Path, initial_text: str, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.agents_path = agents_path
            self.setObjectName("AppDialog")
            self.setWindowTitle("agents.md")
            self.setModal(False)
            self.resize(720, 660)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(10)

            title = QLabel("Agent Instructions")
            title.setObjectName("DialogTitle")
            layout.addWidget(title)

            caption = QLabel("Edit the repo-local agent instructions. Saving creates the file if needed.")
            caption.setObjectName("DialogCaption")
            caption.setWordWrap(True)
            layout.addWidget(caption)

            path_label = QLabel("File")
            path_label.setObjectName("FieldLabel")
            layout.addWidget(path_label)

            path_value = QLabel(str(self.agents_path))
            path_value.setObjectName("SidebarCaption")
            path_value.setWordWrap(True)
            layout.addWidget(path_value)

            self.editor = QPlainTextEdit()
            self.editor.setPlaceholderText("Write repo-specific agent instructions here.")
            self.editor.setPlainText(initial_text)
            self.editor.setMinimumHeight(360)
            layout.addWidget(self.editor, 1)

            buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            buttons.setObjectName("DialogButtons")
            _set_primary_button_default(buttons, QDialogButtonBox.Save)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addSpacing(6)
            layout.addWidget(buttons)

        def file_text(self) -> str:
            return self.editor.toPlainText()

    class StartLoopDialog(CommandEnterDialog):
        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setObjectName("AppDialog")
            self.setWindowTitle("Loop")
            self.setModal(True)
            self.resize(440, 220)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(10)

            title = QLabel("Loop Length")
            title.setObjectName("DialogTitle")
            layout.addWidget(title)

            caption = QLabel("Run indefinitely or choose a fixed iteration count.")
            caption.setObjectName("DialogCaption")
            caption.setWordWrap(True)
            layout.addWidget(caption)

            self.run_indefinitely_checkbox = QCheckBox("Run indefinitely")
            self.run_indefinitely_checkbox.setChecked(True)
            self.run_indefinitely_checkbox.toggled.connect(self._update_iteration_controls)
            layout.addWidget(self.run_indefinitely_checkbox)

            self.iterations_label = QLabel("Iterations")
            self.iterations_label.setObjectName("FieldLabel")
            layout.addWidget(self.iterations_label)

            self.iterations_spin = QSpinBox()
            self.iterations_spin.setRange(1, 9999)
            self.iterations_spin.setValue(START_LOOP_DIALOG_DEFAULT_ITERATIONS)
            self.iterations_spin.setEnabled(False)
            self.iterations_spin.setMinimumWidth(120)
            layout.addWidget(self.iterations_spin)

            self._update_iteration_controls(self.run_indefinitely_checkbox.isChecked())

            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.setObjectName("DialogButtons")
            start_button = buttons.button(QDialogButtonBox.Ok)
            if start_button is not None:
                start_button.setText("Start")
                start_button.setDefault(True)
                start_button.setAutoDefault(True)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addSpacing(8)
            layout.addWidget(buttons)

        def _update_iteration_controls(self, run_indefinitely: bool) -> None:
            enabled = not run_indefinitely
            self.iterations_label.setEnabled(enabled)
            self.iterations_spin.setEnabled(enabled)

        def run_indefinitely(self) -> bool:
            return self.run_indefinitely_checkbox.isChecked()

        def iteration_count(self) -> int:
            return self.iterations_spin.value()

        def loop_run_args(self) -> List[str]:
            return _start_loop_run_args(self.run_indefinitely(), self.iteration_count())

    class TaskCard(QFrame):
        def __init__(self,
                     task: StoredTask,
                     *,
                     on_start_task: Any = None,
                     on_edit: Any,
                     on_delete: Any,
                     on_move_task: Any = None,
                     on_move_task_to_board: Any = None) -> None:
            super().__init__()
            self._task_id = task.task_id
            self._task_board_id = task.board_id
            self._task_phase = task.phase
            self._on_start_task = on_start_task
            self._on_edit = on_edit
            self._on_delete = on_delete
            self._on_move_task = on_move_task
            self._on_move_task_to_board = on_move_task_to_board
            self._drag_start_position: Any = None
            self._drag_in_progress = False
            self.setObjectName("TaskCard")
            self.setCursor(Qt.PointingHandCursor)
            self.setFocusPolicy(Qt.StrongFocus)
            self.setMouseTracking(True)
            self.setAttribute(Qt.WA_Hover, True)
            self.setAcceptDrops(True)
            self.setProperty("dragOver", False)
            self.setToolTip("Click to edit. Drag to move between columns.")

            layout = QVBoxLayout(self)
            layout.setContentsMargins(12, 9, 12, 9)
            layout.setSpacing(5)

            title = QLabel(task.title)
            title.setObjectName("TaskTitle")
            title.setTextFormat(Qt.PlainText)
            title.setWordWrap(True)
            title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            layout.addWidget(title)

            context_text = task.context_notes.strip()
            if context_text:
                context = QLabel(context_text)
                context.setObjectName("TaskContext")
                context.setTextFormat(Qt.PlainText)
                context.setWordWrap(True)
                context.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                context.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                layout.addWidget(context)

            footer_row = QHBoxLayout()
            footer_row.setContentsMargins(0, 0, 0, 0)
            footer_row.setSpacing(8)
            footer_row.setAlignment(Qt.AlignLeft | Qt.AlignTop)

            board_badge = QLabel(task.board_title)
            board_badge.setObjectName("BoardBadge")
            board_badge.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            board_badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            footer_row.addWidget(board_badge, 0, Qt.AlignLeft | Qt.AlignTop)

            meta_parts = ["ready" if task.plan_status == "ready" else "pending"]
            relevant_files = []
            if isinstance(task.plan, dict):
                relevant_files = task.plan.get("relevant_files", [])
            if isinstance(relevant_files, list) and relevant_files:
                meta_parts.append("{0} files".format(len(relevant_files)))
            meta = QLabel(" | ".join(meta_parts))
            meta.setObjectName("TaskMeta")
            meta.setTextFormat(Qt.PlainText)
            meta.setWordWrap(True)
            meta.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            meta.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            footer_row.addWidget(meta)
            layout.addLayout(footer_row)

            if task.last_error:
                error = QLabel(task.last_error)
                error.setObjectName("TaskError")
                error.setTextFormat(Qt.PlainText)
                error.setWordWrap(True)
                error.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                layout.addWidget(error)

        def _copy_task_id(self) -> None:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(self._task_id)

        def _archive_task(self) -> None:
            if self._on_move_task_to_board is None:
                return
            self._on_move_task_to_board(self._task_id, "archived")

        def _start_task(self) -> None:
            if self._on_start_task is None:
                return
            self._on_start_task(self._task_id)

        def _show_context_menu(self, global_pos: Any) -> None:
            menu = QMenu(self)
            copy_action = menu.addAction("Copy Card ID")
            start_action = None
            if _task_card_can_start_task(self._task_phase):
                start_action = menu.addAction("Start Task")
            edit_action = menu.addAction("Edit Card")
            archive_action = menu.addAction("Archive Card")
            delete_action = menu.addAction("Delete Card")

            if self._on_move_task_to_board is None or self._task_board_id == "archived":
                archive_action.setEnabled(False)

            action = menu.exec(global_pos)
            if action == copy_action:
                self._copy_task_id()
            elif action == start_action:
                self._start_task()
            elif action == edit_action:
                self._on_edit()
            elif action == archive_action and archive_action.isEnabled():
                self._archive_task()
            elif action == delete_action:
                self._on_delete()

        def _accepts_task_drop(self, event: Any) -> bool:
            if self._on_move_task is None:
                return False
            task_id, source_phase = _drag_payload_from_mime_data(event.mimeData())
            if not task_id or not source_phase:
                return False
            if task_id == self._task_id or source_phase == self._task_phase:
                return False
            return True

        def dragEnterEvent(self, event) -> None:
            if self._accepts_task_drop(event):
                _set_drag_highlight(self, True)
                event.acceptProposedAction()
                return
            event.ignore()

        def dragMoveEvent(self, event) -> None:
            if self._accepts_task_drop(event):
                _set_drag_highlight(self, True)
                event.acceptProposedAction()
                return
            event.ignore()

        def dragLeaveEvent(self, event) -> None:
            _set_drag_highlight(self, False)
            super().dragLeaveEvent(event)

        def dropEvent(self, event) -> None:
            _set_drag_highlight(self, False)
            if not self._accepts_task_drop(event):
                event.ignore()
                return
            task_id, _source_phase = _drag_payload_from_mime_data(event.mimeData())
            self._on_move_task(task_id, self._task_phase)
            event.setDropAction(Qt.MoveAction)
            event.acceptProposedAction()

        def mousePressEvent(self, event) -> None:
            if event.button() == Qt.LeftButton:
                self._drag_start_position = event.position().toPoint()
                self._drag_in_progress = False
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event) -> None:
            if not (event.buttons() & Qt.LeftButton):
                super().mouseMoveEvent(event)
                return
            if self._on_move_task is None or self._drag_start_position is None:
                super().mouseMoveEvent(event)
                return
            if self._drag_in_progress:
                return
            if (event.position().toPoint() - self._drag_start_position).manhattanLength() < QApplication.startDragDistance():
                super().mouseMoveEvent(event)
                return

            self._drag_in_progress = True
            drag = QDrag(self)
            mime_data = QMimeData()
            mime_data.setData(TASK_DRAG_MIME_TYPE, self._task_id.encode("utf-8"))
            mime_data.setData(TASK_DRAG_PHASE_MIME_TYPE, self._task_phase.encode("utf-8"))
            mime_data.setText(self._task_id)
            drag.setMimeData(mime_data)
            drag.exec(Qt.MoveAction)

        def mouseReleaseEvent(self, event) -> None:
            if event.button() == Qt.LeftButton:
                was_dragging = self._drag_in_progress
                self._drag_start_position = None
                self._drag_in_progress = False
                if was_dragging:
                    event.accept()
                    return
                self._on_edit()
                event.accept()
                return
            super().mouseReleaseEvent(event)

        def keyPressEvent(self, event) -> None:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
                self._on_edit()
                event.accept()
                return
            super().keyPressEvent(event)

        def contextMenuEvent(self, event) -> None:
            self._show_context_menu(event.globalPos())
            event.accept()

    class BoardListDelegate(QStyledItemDelegate):
        _title_role = Qt.UserRole + 1
        _count_role = Qt.UserRole + 2
        _text_padding = 24
        _drop_target_role = "dropTargetRow"

        def initStyleOption(self, option, index) -> None:
            super().initStyleOption(option, index)

            board_id = str(index.data(Qt.UserRole) or "")
            title = str(index.data(self._title_role) or "")
            count = index.data(self._count_role)
            is_archived = board_id == "archived" or title.lower() == "archived"
            if is_archived:
                option.font.setBold(True)
                option.palette.setColor(QPalette.Text, QColor("#a13e35"))
                option.palette.setColor(QPalette.WindowText, QColor("#a13e35"))
                option.palette.setColor(QPalette.HighlightedText, QColor("#a13e35"))
            if count is None or option.rect.width() <= 0:
                return

            count_text = str(count)
            suffix = " | {0}".format(count_text)
            available_width = max(0, option.rect.width() - self._text_padding)
            title_width = max(0, available_width - option.fontMetrics.horizontalAdvance(suffix))
            option.text = "{0}{1}".format(
                option.fontMetrics.elidedText(title, Qt.ElideRight, title_width),
                suffix,
            )

        def paint(self, painter, option, index) -> None:
            super().paint(painter, option, index)

            view = self.parent()
            try:
                drop_target_row = int(view.property(self._drop_target_role)) if view is not None else -1
            except (TypeError, ValueError):
                drop_target_row = -1
            if index.row() == drop_target_row:
                painter.save()
                painter.setRenderHint(QPainter.Antialiasing, True)
                rect = option.rect.adjusted(2, 2, -2, -2)
                fill = QColor("#f5ddcf")
                fill.setAlpha(150)
                border = QColor("#c8643b")
                border.setAlpha(190)
                painter.setPen(border)
                painter.setBrush(fill)
                painter.drawRoundedRect(rect, 4, 4)
                painter.restore()

            board_id = str(index.data(Qt.UserRole) or "")
            title = str(index.data(self._title_role) or "")
            if board_id != "archived" and title.lower() != "archived":
                return

            painter.save()
            painter.setPen(QColor("#d7b4a6"))
            y = option.rect.top()
            painter.drawLine(option.rect.left() + 8, y, option.rect.right() - 8, y)
            painter.restore()

    class BoardList(QListWidget):
        def __init__(self, on_move_task_to_board: Any, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._on_move_task_to_board = on_move_task_to_board
            self._drop_target_row = -1
            self.setAcceptDrops(True)
            self.setProperty("dropTargetRow", -1)

        def _drop_target_row_for_event(self, event: Any) -> int:
            item = self.itemAt(event.position().toPoint())
            if item is None:
                return -1
            board_id = item.data(Qt.UserRole)
            if not board_id:
                return -1
            return self.row(item)

        def _set_drop_target_row(self, row: int) -> None:
            if row == self._drop_target_row:
                return
            previous_row = self._drop_target_row
            self._drop_target_row = row
            self.setProperty("dropTargetRow", row)
            for target_row in (previous_row, row):
                if target_row < 0:
                    continue
                item = self.item(target_row)
                if item is not None:
                    self.viewport().update(self.visualItemRect(item))

        def _clear_drop_target_row(self) -> None:
            self._set_drop_target_row(-1)

        def _accepts_task_drop(self, event: Any) -> bool:
            task_id, _source_phase = _drag_payload_from_mime_data(event.mimeData())
            if not task_id or self._on_move_task_to_board is None:
                return False
            return self._drop_target_row_for_event(event) >= 0

        def dragEnterEvent(self, event) -> None:
            drop_target_row = self._drop_target_row_for_event(event)
            if drop_target_row >= 0 and self._on_move_task_to_board is not None:
                self._set_drop_target_row(drop_target_row)
                event.acceptProposedAction()
                return
            self._clear_drop_target_row()
            event.ignore()

        def dragMoveEvent(self, event) -> None:
            drop_target_row = self._drop_target_row_for_event(event)
            if drop_target_row >= 0 and self._on_move_task_to_board is not None:
                self._set_drop_target_row(drop_target_row)
                event.acceptProposedAction()
                return
            self._clear_drop_target_row()
            event.ignore()

        def dragLeaveEvent(self, event) -> None:
            self._clear_drop_target_row()
            super().dragLeaveEvent(event)

        def dropEvent(self, event) -> None:
            self._clear_drop_target_row()
            if not self._accepts_task_drop(event):
                event.ignore()
                return

            item = self.itemAt(event.position().toPoint())
            if item is None or self._on_move_task_to_board is None:
                event.ignore()
                return

            board_id = item.data(Qt.UserRole)
            if board_id is None:
                event.ignore()
                return

            task_id, _source_phase = _drag_payload_from_mime_data(event.mimeData())
            self._on_move_task_to_board(task_id, str(board_id))
            event.setDropAction(Qt.MoveAction)
            event.acceptProposedAction()

    class PhaseColumn(QFrame):
        def __init__(self, phase: str) -> None:
            super().__init__()
            self.phase = phase
            self._on_move_task: Any = None
            self.setObjectName("PhaseColumn")
            self.setMinimumWidth(300)
            self.setMaximumWidth(348)
            self.setAcceptDrops(True)
            self.setProperty("dragOver", False)
            self.setToolTip("Drop a card here to move it to {0}.".format(_phase_label(phase)))

            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(10)

            header_row = QHBoxLayout()
            header_row.setContentsMargins(0, 0, 0, 0)
            header_row.setSpacing(6)

            self.title_label = QLabel(PHASE_TITLES.get(phase, phase))
            self.title_label.setObjectName("PhaseTitle")
            header_row.addWidget(self.title_label)

            header_row.addItem(QSpacerItem(12, 12, QSizePolicy.Expanding, QSizePolicy.Minimum))

            self.count_label = QLabel("0")
            self.count_label.setObjectName("PhaseCount")
            header_row.addWidget(self.count_label)

            layout.addLayout(header_row)

            self.body_layout = QVBoxLayout()
            self.body_layout.setContentsMargins(0, 0, 0, 0)
            self.body_layout.setSpacing(10)
            layout.addLayout(self.body_layout)
            layout.addStretch(1)

        def set_tasks(self,
                      tasks: List[StoredTask],
                      *,
                      on_start_task: Any,
                      on_edit_task: Any,
                      on_delete_task: Any,
                      on_move_task: Any,
                      on_move_task_to_board: Any) -> None:
            self._on_move_task = on_move_task
            self.count_label.setText(str(len(tasks)))
            while self.body_layout.count():
                item = self.body_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

            if not tasks:
                empty = QLabel("No tasks. Drop a card here.")
                empty.setObjectName("ColumnEmpty")
                empty.setWordWrap(True)
                empty.setAlignment(Qt.AlignCenter)
                empty.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                self.body_layout.addWidget(empty)
                return

            for task in tasks:
                self.body_layout.addWidget(
                    TaskCard(
                        task,
                        on_start_task=on_start_task,
                        on_edit=lambda _checked=False, current_task=task: on_edit_task(current_task),
                        on_delete=lambda _checked=False, current_task=task: on_delete_task(current_task),
                        on_move_task=on_move_task,
                        on_move_task_to_board=on_move_task_to_board,
                    )
                )

            self.body_layout.addStretch(1)

        def _accepts_task_drop(self, event: Any) -> bool:
            if self._on_move_task is None:
                return False
            task_id, source_phase = _drag_payload_from_mime_data(event.mimeData())
            if not task_id or not source_phase:
                return False
            if source_phase == self.phase:
                return False
            return True

        def dragEnterEvent(self, event) -> None:
            if self._accepts_task_drop(event):
                _set_drag_highlight(self, True)
                event.acceptProposedAction()
                return
            event.ignore()

        def dragMoveEvent(self, event) -> None:
            if self._accepts_task_drop(event):
                _set_drag_highlight(self, True)
                event.acceptProposedAction()
                return
            event.ignore()

        def dragLeaveEvent(self, event) -> None:
            _set_drag_highlight(self, False)
            super().dragLeaveEvent(event)

        def dropEvent(self, event) -> None:
            _set_drag_highlight(self, False)
            if not self._accepts_task_drop(event):
                event.ignore()
                return
            task_id, _source_phase = _drag_payload_from_mime_data(event.mimeData())
            self._on_move_task(task_id, self.phase)
            event.setDropAction(Qt.MoveAction)
            event.acceptProposedAction()

    class TaskbotWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.saved_session = _load_saved_session()
            self.status_note = "Ready"
            self._last_terminal_text: Optional[str] = None
            self._store_signature: tuple[bool, int, int] | None = None
            self._runtime_signature: tuple[bool, int, int] | None = None
            self._terminal_signature: tuple[bool, int, int] | None = None
            self._cached_boards: List[Dict[str, Any]] = []
            self._cached_tasks: List[StoredTask] = []
            self._cached_runtime_payload: Dict[str, Any] = {}
            self._last_rendered_selected_board_id: str | None | object = object()
            self._open_dialogs: List[QDialog] = []
            self.active_config = self._initial_config()
            self.selected_board_id = self._initial_board_id()
            self.terminal_font_family = _preferred_monospace_family()
            self.phase_order: List[str] = []
            self.phase_columns: Dict[str, PhaseColumn] = {}
            self.board_shell: QFrame
            self.stage_shell: QFrame
            self.stage_scrollbar: QScrollBar

            self.setWindowTitle("Taskbot")
            self.resize(1480, 920)
            self.setMinimumSize(1080, 720)

            self._build_ui()
            self._apply_window_style()
            self._rebuild_phase_columns(phase_labels(self.active_config))
            self._refresh_repo_widgets()

            self.refresh_timer = QTimer(self)
            self.refresh_timer.timeout.connect(self.refresh_view)
            self.refresh_timer.start(refresh_ms)
            self.refresh_view()

        def _initial_config(self) -> Dict[str, Any]:
            saved_repo = self.saved_session.get("repo_root", "")
            if saved_repo:
                repo_root = _resolve_repo_path(saved_repo)
                if repo_root.exists() and repo_root.is_dir():
                    try:
                        return _runtime_config_for_repo(repo_root)
                    except Exception:
                        pass
            return _runtime_config_for_repo(Path(config["repo_root"]))

        def _initial_board_id(self) -> Optional[str]:
            saved_repo = self.saved_session.get("repo_root", "")
            active_repo = str(self.active_config["repo_root"]).strip()
            if saved_repo and Path(saved_repo).resolve() == Path(active_repo).resolve():
                board_id = self.saved_session.get("selected_board_id", "")
                return board_id or None
            return None

        def _build_ui(self) -> None:
            central = QWidget()
            self.setCentralWidget(central)

            root = QVBoxLayout(central)
            root.setContentsMargins(12, 12, 12, 12)
            root.setSpacing(10)

            top_region = QWidget()
            top_region_layout = QVBoxLayout(top_region)
            top_region_layout.setContentsMargins(0, 0, 0, 0)
            top_region_layout.setSpacing(10)

            top_shell = QFrame()
            top_shell.setObjectName("TopShell")
            top_layout = QVBoxLayout(top_shell)
            top_layout.setContentsMargins(12, 12, 12, 12)
            top_layout.setSpacing(10)

            header_stack = QVBoxLayout()
            header_stack.setContentsMargins(0, 0, 0, 0)
            header_stack.setSpacing(8)

            title_row = QHBoxLayout()
            title_row.setContentsMargins(0, 0, 0, 0)
            title_row.setSpacing(14)

            headline = QLabel()
            headline.setObjectName("Headline")
            headline.setTextFormat(Qt.RichText)
            headline.setText(_taskbot_title_html())
            headline.setWordWrap(False)
            headline.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            title_row.addWidget(headline, 0, Qt.AlignVCenter)

            self.runtime_label = QLabel("")
            self.runtime_label.setObjectName("RuntimeLabel")
            self.runtime_label.setTextFormat(Qt.RichText)
            self.runtime_label.setWordWrap(False)
            self.runtime_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.runtime_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            title_row.addWidget(self.runtime_label, 1, Qt.AlignVCenter)

            self.status_chip = QLabel("Idle")
            self.status_chip.setObjectName("StatusChip")
            title_row.addWidget(self.status_chip, 0, Qt.AlignTop)
            header_stack.addLayout(title_row)

            repo_row = QHBoxLayout()
            repo_row.setContentsMargins(0, 0, 0, 0)
            repo_row.setSpacing(8)

            repo_label = QLabel("Repo")
            repo_label.setObjectName("TopFieldLabel")
            repo_row.addWidget(repo_label)

            self.repo_input = QLineEdit()
            self.repo_input.setPlaceholderText("Repository path")
            self.repo_input.returnPressed.connect(self._load_repo_from_input)
            repo_row.addWidget(self.repo_input, 1)

            browse_button = QPushButton("Browse")
            browse_button.clicked.connect(self._browse_repo)
            repo_row.addWidget(browse_button)

            load_button = QPushButton("Load")
            load_button.setObjectName("PrimaryButton")
            load_button.clicked.connect(self._load_repo_from_input)
            repo_row.addWidget(load_button)

            settings_button = QPushButton("Settings")
            settings_button.clicked.connect(self._open_settings_dialog)
            repo_row.addWidget(settings_button)

            agents_button = QPushButton("Agents")
            agents_button.clicked.connect(self._open_agents_dialog)
            repo_row.addWidget(agents_button)
            header_stack.addLayout(repo_row)

            controls_panel = QWidget()
            controls_panel.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            controls_row = QHBoxLayout(controls_panel)
            controls_row.setContentsMargins(0, 0, 0, 0)
            controls_row.setSpacing(8)

            self.plan_button = QPushButton("Plan")
            self.plan_button.setObjectName("HeaderPlanButton")
            self.plan_button.setToolTip(RUNNER_CONTROL_TOOLTIPS["plan_once"])
            self.plan_button.clicked.connect(lambda: self._spawn_runner(["plan"]))
            controls_row.addWidget(self.plan_button)

            self.run_button = QPushButton("Run")
            self.run_button.setObjectName("HeaderRunButton")
            self.run_button.setToolTip(RUNNER_CONTROL_TOOLTIPS["run_once"])
            self.run_button.clicked.connect(lambda: self._spawn_runner(["run", "--iterations", "1"]))
            controls_row.addWidget(self.run_button)

            self.loop_button = QPushButton("Loop")
            self.loop_button.setObjectName("HeaderLoopButton")
            self.loop_button.setToolTip(RUNNER_CONTROL_TOOLTIPS["start_loop"])
            self.loop_button.clicked.connect(self._open_start_loop_dialog)
            controls_row.addWidget(self.loop_button)

            self.stop_button = QPushButton("Stop")
            self.stop_button.setObjectName("HeaderStopButton")
            self.stop_button.setToolTip(RUNNER_CONTROL_TOOLTIPS["stop"])
            self.stop_button.clicked.connect(self._request_stop)
            controls_row.addWidget(self.stop_button)

            top_layout.addLayout(header_stack)

            top_region_layout.addWidget(top_shell)

            sidebar = QFrame()
            sidebar.setObjectName("Sidebar")
            sidebar.setMinimumWidth(220)
            sidebar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
            sidebar_layout = QVBoxLayout(sidebar)
            sidebar_layout.setContentsMargins(12, 12, 12, 12)
            sidebar_layout.setSpacing(8)

            boards_header = QHBoxLayout()
            boards_header.setContentsMargins(0, 0, 0, 0)
            boards_header.setSpacing(6)

            boards_label = QLabel("Boards")
            boards_label.setObjectName("SidebarTitle")
            boards_header.addWidget(boards_label)
            boards_header.addItem(QSpacerItem(12, 12, QSizePolicy.Expanding, QSizePolicy.Minimum))

            add_board_button = QToolButton()
            add_board_button.setText("+")
            add_board_button.setObjectName("SmallActionButton")
            add_board_button.clicked.connect(self._open_add_board_dialog)
            boards_header.addWidget(add_board_button)
            sidebar_layout.addLayout(boards_header)

            self.board_list = BoardList(self._move_task_to_board)
            self.board_list.setObjectName("BoardList")
            self.board_list.setItemDelegate(BoardListDelegate(self.board_list))
            self.board_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.board_list.setTextElideMode(Qt.ElideNone)
            self.board_list.setContextMenuPolicy(Qt.CustomContextMenu)
            self.board_list.customContextMenuRequested.connect(self._open_board_context_menu)
            self.board_list.currentItemChanged.connect(self._on_board_selection_changed)
            sidebar_layout.addWidget(self.board_list, 1)

            center_shell = QWidget()
            center_shell.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            center_shell_layout = QVBoxLayout(center_shell)
            center_shell_layout.setContentsMargins(0, 0, 0, 0)
            center_shell_layout.setSpacing(8)

            board_header = QFrame()
            board_header.setObjectName("BoardHeader")
            board_header_layout = QHBoxLayout(board_header)
            board_header_layout.setContentsMargins(12, 10, 12, 10)
            board_header_layout.setSpacing(8)

            board_title_stack = QVBoxLayout()
            board_title_stack.setContentsMargins(0, 0, 0, 0)
            board_title_stack.setSpacing(1)

            self.board_title_label = QLabel("All Boards")
            self.board_title_label.setObjectName("BoardTitle")
            board_title_stack.addWidget(self.board_title_label)

            self.board_summary_label = QLabel("")
            self.board_summary_label.setObjectName("BoardSummary")
            self.board_summary_label.setWordWrap(False)
            board_title_stack.addWidget(self.board_summary_label)
            board_header_layout.addLayout(board_title_stack)
            board_header_layout.addItem(QSpacerItem(12, 12, QSizePolicy.Expanding, QSizePolicy.Minimum))

            self.add_task_button = QPushButton("+ Task")
            self.add_task_button.setObjectName("AccentButton")
            self.add_task_button.clicked.connect(self._open_add_task_dialog)
            self.add_task_button.setMaximumWidth(84)
            board_header_layout.addWidget(self.add_task_button)

            refresh_button = QPushButton("Refresh")
            refresh_button.clicked.connect(self.refresh_view)
            board_header_layout.addWidget(refresh_button)

            board_header_divider = QFrame()
            board_header_divider.setObjectName("HeaderDivider")
            board_header_divider.setFrameShape(QFrame.VLine)
            board_header_divider.setFrameShadow(QFrame.Plain)
            board_header_divider.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
            board_header_divider.setFixedWidth(1)
            board_header_layout.addWidget(board_header_divider)
            board_header_layout.addWidget(controls_panel)

            self.columns_scroll = QScrollArea()
            self.columns_scroll.setObjectName("ColumnsScroll")
            self.columns_scroll.setWidgetResizable(True)
            self.columns_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.columns_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.columns_scroll.viewport().setObjectName("ColumnsViewport")

            self.columns_container = QWidget()
            self.columns_container.setObjectName("ColumnsContainer")
            self.columns_container_layout = QHBoxLayout(self.columns_container)
            self.columns_container_layout.setContentsMargins(12, 12, 12, 20)
            self.columns_container_layout.setSpacing(8)
            self.columns_scroll.setWidget(self.columns_container)

            self.stage_shell = QFrame()
            self.stage_shell.setObjectName("StageShell")
            stage_layout = QVBoxLayout(self.stage_shell)
            stage_layout.setContentsMargins(6, 6, 6, 6)
            stage_layout.setSpacing(0)
            stage_layout.addWidget(self.columns_scroll)

            self.stage_scrollbar = QScrollBar(Qt.Horizontal, self.stage_shell)
            self.stage_scrollbar.setObjectName("StageScrollBar")
            self.stage_scrollbar.hide()
            self.stage_scrollbar.raise_()

            internal_scrollbar = self.columns_scroll.horizontalScrollBar()
            internal_scrollbar.rangeChanged.connect(self._sync_stage_scrollbar)
            internal_scrollbar.valueChanged.connect(self._sync_stage_scrollbar)
            self.stage_scrollbar.valueChanged.connect(internal_scrollbar.setValue)

            self.board_shell = QFrame()
            self.board_shell.setObjectName("BoardShell")
            board_shell_layout = QVBoxLayout(self.board_shell)
            board_shell_layout.setContentsMargins(0, 0, 0, 0)
            board_shell_layout.setSpacing(0)
            board_shell_layout.addWidget(board_header)

            board_divider = QFrame()
            board_divider.setObjectName("BoardDivider")
            board_divider.setFrameShape(QFrame.HLine)
            board_divider.setFrameShadow(QFrame.Plain)
            board_divider.setFixedHeight(1)
            board_shell_layout.addWidget(board_divider)

            board_shell_layout.addWidget(self.stage_shell, 1)
            center_shell_layout.addWidget(self.board_shell, 1)

            content_splitter = _CenteredLineSplitter(Qt.Horizontal)
            content_splitter.setObjectName("ContentSplitter")
            content_splitter.setChildrenCollapsible(False)
            content_splitter.setHandleWidth(8)
            content_splitter.addWidget(sidebar)
            content_splitter.addWidget(center_shell)
            content_splitter.setStretchFactor(0, 0)
            content_splitter.setStretchFactor(1, 1)
            content_splitter.setSizes([230, 970])
            top_region_layout.addWidget(content_splitter, 1)

            terminal_shell = QFrame()
            terminal_shell.setObjectName("TerminalShell")
            terminal_layout = QVBoxLayout(terminal_shell)
            terminal_layout.setContentsMargins(12, 10, 12, 10)
            terminal_layout.setSpacing(6)

            terminal_header = QHBoxLayout()
            terminal_header.setContentsMargins(0, 0, 0, 0)
            terminal_header.setSpacing(10)

            terminal_title = QLabel("Terminal Output")
            terminal_title.setObjectName("TerminalTitle")
            terminal_header.addWidget(terminal_title)
            terminal_header.addItem(QSpacerItem(12, 12, QSizePolicy.Expanding, QSizePolicy.Minimum))

            terminal_hint = QLabel("Live tail.")
            terminal_hint.setObjectName("TerminalHint")
            terminal_header.addWidget(terminal_hint)
            terminal_layout.addLayout(terminal_header)

            self.terminal_output = QTextEdit()
            self.terminal_output.setObjectName("TerminalOutput")
            self.terminal_output.setReadOnly(True)
            self.terminal_output.setLineWrapMode(QTextEdit.NoWrap)
            self.terminal_output.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.terminal_output.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            terminal_font = QFont(self.terminal_font_family)
            terminal_font.setStyleHint(QFont.Monospace)
            terminal_font.setPointSize(10)
            self.terminal_output.setFont(terminal_font)
            self.terminal_output.setMinimumHeight(130)
            terminal_layout.addWidget(self.terminal_output)

            self.main_splitter = _CenteredLineSplitter(Qt.Vertical)
            self.main_splitter.setObjectName("MainSplitter")
            self.main_splitter.setChildrenCollapsible(False)
            self.main_splitter.setHandleWidth(6)
            self.main_splitter.addWidget(top_region)
            self.main_splitter.addWidget(terminal_shell)
            self.main_splitter.setStretchFactor(0, 1)
            self.main_splitter.setStretchFactor(1, 0)
            self.main_splitter.setSizes([700, 180])
            self.main_splitter.splitterMoved.connect(self._on_main_splitter_moved)
            root.addWidget(self.main_splitter, 1)

        def _apply_window_style(self) -> None:
            checkbox_tick_icon = _checkbox_indicator_tick_icon_path().resolve().as_posix()
            stylesheet = """
            QMainWindow {
                background: #efe7dc;
                color: #261d18;
            }

            QWidget {
                font-size: 12px;
            }

            QFrame#TopShell,
            QFrame#BoardShell,
            QFrame#TerminalShell {
                background: #fbf8f3;
                border: 1px solid #ddcfbf;
                border-radius: 8px;
            }

            QFrame#BoardDivider {
                background: rgba(77, 62, 51, 0.10);
                border: none;
            }

            QFrame#Sidebar {
                background: #f5efe5;
                border: 1px solid #ddd0bf;
                border-radius: 8px;
            }

            QFrame#HeaderDivider {
                background: rgba(77, 62, 51, 0.12);
                border: none;
                min-width: 1px;
                max-width: 1px;
                margin: 4px 0;
            }

            QLabel#Headline {
                color: #1f1814;
                font-size: 22px;
                font-weight: 800;
            }

            QLabel#StatusChip {
                background: #f5ddcf;
                color: #8d4728;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
                font-weight: 700;
            }

            QLabel#TopFieldLabel {
                color: #765d4f;
                font-size: 10px;
                font-weight: 700;
                text-transform: uppercase;
            }

            QLabel#FieldLabel {
                color: #6b584c;
                font-size: 10px;
                font-weight: 700;
                text-transform: uppercase;
            }

            QCheckBox {
                color: #2a221d;
                spacing: 6px;
                font-size: 12px;
            }

            QCheckBox::indicator {
                width: 14px;
                height: 14px;
            }

            QCheckBox::indicator:unchecked {
                background: #fffdf9;
                border: 1px solid #d8cab9;
                border-radius: 3px;
            }

            QCheckBox::indicator:checked {
                background: #c8643b;
                border: 1px solid #b65731;
                border-radius: 3px;
                image: url("__CHECKBOX_TICK_ICON__");
            }

            QLineEdit,
            QPlainTextEdit,
            QListWidget,
            QSpinBox,
            QDialog#AppDialog QToolButton#DialogDropdown {
                background: #fffdf9;
                color: #221a16;
                border: 1px solid #d8cab9;
                border-radius: 4px;
                padding: 6px 8px;
            }

            QLineEdit:focus,
            QPlainTextEdit:focus,
            QListWidget:focus,
            QSpinBox:focus,
            QDialog#AppDialog QToolButton#DialogDropdown:focus {
                border: 1px solid #c8643b;
            }

            QSpinBox:disabled {
                background: #f4ece2;
                color: #8c7b6f;
            }

            QPushButton,
            QToolButton,
            QDialogButtonBox QPushButton {
                background: #efe4d7;
                color: #2a221d;
                border: 1px solid #d8cab9;
                border-radius: 4px;
                padding: 6px 10px;
                font-weight: 600;
            }

            QPushButton:hover,
            QToolButton:hover,
            QDialogButtonBox QPushButton:hover {
                background: #e6d7c6;
            }

            QPushButton#PrimaryButton,
            QPushButton#AccentButton {
                background: #c8643b;
                color: #fff8f2;
                border: 1px solid #b65731;
            }

            QPushButton#PrimaryButton:hover,
            QPushButton#AccentButton:hover {
                background: #b85b34;
            }

            QPushButton#HeaderPlanButton {
                background: #d8e9fa;
                color: #23456a;
                border: 1px solid #b6cfe7;
            }

            QPushButton#HeaderPlanButton:hover {
                background: #cae0f4;
            }

            QPushButton#HeaderRunButton {
                background: #dcefdc;
                color: #275533;
                border: 1px solid #b9d6ba;
            }

            QPushButton#HeaderRunButton:hover {
                background: #cde4ce;
            }

            QPushButton#HeaderLoopButton {
                background: #215a34;
                color: #ffffff;
                border: 1px solid #174325;
            }

            QPushButton#HeaderLoopButton:hover {
                background: #2a6a3e;
            }

            QPushButton#HeaderStopButton {
                background: #6f2222;
                color: #ffffff;
                border: 1px solid #551818;
            }

            QPushButton#HeaderStopButton:hover {
                background: #832929;
            }

            QToolButton#SmallActionButton {
                min-width: 24px;
                min-height: 24px;
                max-width: 24px;
                max-height: 24px;
                font-size: 14px;
                font-weight: 700;
                background: #f4c9b3;
                color: #7a3c22;
                border: 1px solid #d3997e;
                border-radius: 4px;
                padding: 0;
            }

            QLabel#RuntimeLabel,
            QLabel#BoardSummary,
            QLabel#TerminalHint,
            QLabel#SidebarCaption {
                color: #6d594d;
                font-size: 11px;
            }

            QLabel#DialogCaption {
                color: #6d594d;
                font-size: 11px;
            }

            QLabel#SidebarTitle {
                color: #2a221d;
                font-size: 14px;
                font-weight: 700;
            }

            QListWidget#BoardList {
                background: #fbf8f3;
                color: #2a221d;
                border: 1px solid #e2d6c7;
                border-radius: 4px;
                padding: 3px;
            }

            QListWidget#BoardList::item {
                padding: 7px 9px;
                border-radius: 3px;
                margin-bottom: 1px;
            }

            QListWidget#BoardList::item:selected {
                background: #edd6c7;
                color: #251c17;
            }

            QListWidget#BoardList::item:hover:!selected {
                background: #f1e7dc;
            }

            QLabel#BoardTitle {
                color: #1f1814;
                font-size: 16px;
                font-weight: 700;
            }

            QScrollArea#ColumnsScroll {
                border: none;
                background: #f3ece2;
            }

            QWidget#ColumnsViewport,
            QWidget#ColumnsContainer {
                background: #f3ece2;
            }

            QScrollBar#StageScrollBar:horizontal {
                background: rgba(243, 236, 226, 0.78);
                height: 12px;
                margin: 0;
                border: 1px solid rgba(119, 96, 80, 0.12);
                border-radius: 3px;
            }

            QScrollBar#StageScrollBar::handle:horizontal {
                background: rgba(77, 62, 51, 0.25);
                min-width: 60px;
                border-radius: 3px;
            }

            QScrollBar#StageScrollBar::handle:horizontal:hover {
                background: rgba(77, 62, 51, 0.36);
            }

            QScrollBar#StageScrollBar::add-line:horizontal,
            QScrollBar#StageScrollBar::sub-line:horizontal,
            QScrollBar#StageScrollBar::left-arrow:horizontal,
            QScrollBar#StageScrollBar::right-arrow:horizontal,
            QScrollBar#StageScrollBar::add-page:horizontal,
            QScrollBar#StageScrollBar::sub-page:horizontal {
                background: transparent;
                border: none;
                width: 0px;
            }

            QSplitter#MainSplitter::handle,
            QSplitter#ContentSplitter::handle {
                background: transparent;
                border: none;
            }

            QFrame#PhaseColumn {
                background: #faf6ef;
                border: 1px solid #ddcfbf;
                border-radius: 6px;
            }

            QFrame#PhaseColumn[dragOver="true"] {
                background: #f4eadf;
                border: 1px solid #c8643b;
            }

            QLabel#PhaseTitle {
                color: #241c17;
                font-size: 14px;
                font-weight: 700;
            }

            QLabel#PhaseCount {
                background: #efe4d8;
                color: #7f6658;
                border-radius: 3px;
                padding: 2px 6px;
                font-weight: 700;
            }

            QFrame#TaskCard {
                background: #ffffff;
                border: 1px solid #eadfce;
                border-radius: 6px;
            }

            QFrame#TaskCard[dragOver="true"] {
                background: #fff4e8;
                border: 1px solid #c8643b;
            }

            QFrame#TaskCard:hover {
                background: #fff9f1;
                border: 1px solid #d8c0a8;
            }

            QFrame#TaskCard:focus {
                background: #fff7eb;
                border: 1px solid #cfa683;
                outline: none;
            }

            QToolButton#CardDeleteButton {
                background: #8f2f29;
                color: #fff8f4;
                border: 1px solid #70221d;
                border-radius: 6px;
                padding: 0px;
            }

            QToolButton#CardDeleteButton:hover {
                background: #a33b34;
                border: 1px solid #81302a;
            }

            QToolButton#CardDeleteButton:pressed {
                background: #74211c;
                border: 1px solid #5f1c18;
            }

            QToolButton#CardCompleteButton {
                background: #e5f2e5;
                color: #2f6b44;
                border: 1px solid #c8dec9;
                border-radius: 10px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 700;
            }

            QToolButton#CardCompleteButton:hover {
                background: #d8ead9;
                border: 1px solid #b8d2b8;
            }

            QToolButton#CardNeedsTestingButton {
                background: #fff0d9;
                color: #8d5c15;
                border: 1px solid #ebd2a2;
                border-radius: 10px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 700;
            }

            QToolButton#CardNeedsTestingButton:hover {
                background: #f8e2b8;
                border: 1px solid #dfc08d;
            }

            QLabel#BoardBadge {
                background: #f5ddcf;
                color: #934e2e;
                border-radius: 3px;
                padding: 2px 5px;
                font-size: 10px;
                font-weight: 700;
            }

            QLabel#TaskTitle {
                color: #1f1814;
                font-size: 14px;
                font-weight: 700;
            }

            QLabel#TaskContext {
                color: #655449;
                font-size: 12px;
            }

            QLabel#TaskMeta {
                color: #957d6e;
                font-size: 10px;
                font-weight: 600;
            }

            QLabel#TaskError {
                color: #a13e35;
                font-size: 11px;
                font-weight: 600;
            }

            QLabel#ColumnEmpty {
                color: #9b8577;
                font-style: italic;
                padding: 10px 2px;
            }

            QLabel#TerminalTitle {
                color: #1f1814;
                font-size: 14px;
                font-weight: 700;
            }

            QLabel#DialogTitle {
                color: #1f1814;
                font-size: 14px;
                font-weight: 700;
            }

            QDialog#AppDialog {
                background: #fbf8f3;
                border: 1px solid #ddcfbf;
                border-radius: 8px;
            }

            QDialog#AppDialog QScrollArea#SettingsScrollArea {
                background: #fbf8f3;
                border: none;
            }

            QDialog#AppDialog QWidget#SettingsContent {
                background: #fbf8f3;
            }

            QDialog#AppDialog QToolButton#DialogDropdown {
                min-height: 32px;
                text-align: left;
                padding-right: 26px;
                font-weight: 500;
            }

            QDialog#AppDialog QToolButton#DialogDropdown:hover {
                background: #f7f0e8;
            }

            QDialog#AppDialog QToolButton#DialogDropdown:pressed {
                background: #efe4d7;
            }

            QDialog#AppDialog QToolButton#DialogDropdown::menu-indicator {
                subcontrol-origin: padding;
                subcontrol-position: center right;
                width: 14px;
                height: 14px;
            }

            QDialog#AppDialog QScrollArea#SettingsScrollArea > QWidget {
                background: #fbf8f3;
            }

            QDialog#AppDialog QScrollArea#SettingsScrollArea > QWidget > QWidget {
                background: #fbf8f3;
            }

            QDialog#AppDialog QMenu#DialogDropdownMenu {
                background: #fffdf9;
                color: #221a16;
                border: 1px solid #d8cab9;
            }

            QDialog#AppDialog QMenu#DialogDropdownMenu::item {
                padding: 6px 8px;
            }

            QDialog#AppDialog QMenu#DialogDropdownMenu::item:selected {
                background: #edd6c7;
                color: #251c17;
            }

            QDialog#AppDialog QMenu#DialogDropdownMenu::separator {
                height: 1px;
                background: #eadfce;
                margin: 4px 6px;
            }

            QDialog#AppDialog QDialogButtonBox#DialogButtons {
                padding-top: 2px;
            }

            QTextEdit#TerminalOutput {
                background: #171d22;
                color: #d7e0ea;
                border: 1px solid #273341;
                border-radius: 4px;
                padding: 8px;
            }

            QTextEdit#TerminalOutput QScrollBar:vertical {
                background: rgba(23, 29, 34, 0.24);
                width: 10px;
                margin: 2px 1px 2px 1px;
            }

            QTextEdit#TerminalOutput QScrollBar::handle:vertical {
                background: rgba(128, 145, 160, 0.58);
                min-height: 36px;
                border-radius: 4px;
            }

            QTextEdit#TerminalOutput QScrollBar:horizontal {
                background: rgba(23, 29, 34, 0.24);
                height: 10px;
                margin: 1px 2px 1px 2px;
            }

            QTextEdit#TerminalOutput QScrollBar::handle:horizontal {
                background: rgba(128, 145, 160, 0.58);
                min-width: 36px;
                border-radius: 4px;
            }

            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 2px 1px 2px 1px;
            }

            QScrollBar::handle:vertical {
                background: rgba(77, 62, 51, 0.16);
                min-height: 28px;
                border-radius: 3px;
            }

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical,
            QScrollBar::up-arrow:vertical,
            QScrollBar::down-arrow:vertical {
                background: transparent;
                border: none;
                height: 0px;
            }

            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: transparent;
            }

            QScrollBar:horizontal {
                background: transparent;
                height: 8px;
                margin: 1px 2px 1px 2px;
            }

            QScrollBar::handle:horizontal {
                background: rgba(77, 62, 51, 0.16);
                min-width: 28px;
                border-radius: 3px;
            }

            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal,
            QScrollBar::left-arrow:horizontal,
            QScrollBar::right-arrow:horizontal {
                background: transparent;
                border: none;
                width: 0px;
            }

            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                background: transparent;
            }
            """.replace("__CHECKBOX_TICK_ICON__", checkbox_tick_icon)
            self.setStyleSheet(stylesheet)

        def _refresh_repo_widgets(self) -> None:
            self.repo_input.setText(str(self.active_config["repo_root"]))

        def _invalidate_refresh_cache(self) -> None:
            self._store_signature = None
            self._runtime_signature = None
            self._terminal_signature = None
            self._cached_boards = []
            self._cached_tasks = []
            self._cached_runtime_payload = {}
            self._last_terminal_text = None
            self._last_rendered_selected_board_id = object()

        def _refresh_store_cache(self) -> bool:
            current_signature = _path_signature(store_path(self.active_config))
            if current_signature == self._store_signature:
                return False

            store = load_store_snapshot(self.active_config)
            self._cached_boards = _boards_from_store_snapshot(store)
            board_orders = {board["board_id"]: board["order"] for board in self._cached_boards}
            self._cached_tasks = [
                StoredTask.from_payload(payload)
                for payload in store.get("tasks", [])
                if isinstance(payload, dict)
            ]
            self._cached_tasks.sort(key=lambda task: (board_orders.get(task.board_id, 9999), task.order, task.title.lower()))
            self._store_signature = _path_signature(store_path(self.active_config))
            return True

        def _refresh_runtime_cache(self) -> bool:
            current_signature = _path_signature(self._runtime_path())
            if current_signature == self._runtime_signature:
                return False

            self._runtime_signature = current_signature
            self._cached_runtime_payload = self._read_runtime_payload() if current_signature[0] else {}
            return True

        def _capture_scroll_state(self, widget: Any) -> tuple[int, int]:
            return (
                widget.verticalScrollBar().value(),
                widget.horizontalScrollBar().value(),
            )

        def _restore_scroll_state(self, widget: Any, vertical_value: int, horizontal_value: int) -> None:
            vertical_scrollbar = widget.verticalScrollBar()
            horizontal_scrollbar = widget.horizontalScrollBar()
            vertical_scrollbar.setValue(min(vertical_value, vertical_scrollbar.maximum()))
            horizontal_scrollbar.setValue(min(horizontal_value, horizontal_scrollbar.maximum()))

        def _restore_columns_scroll_state(self, vertical_value: int, horizontal_value: int) -> None:
            self._restore_scroll_state(self.columns_scroll, vertical_value, horizontal_value)
            self._sync_stage_scrollbar()

        def _rebuild_phase_columns(self, phases: List[str]) -> None:
            self.phase_order = list(phases)
            self.phase_columns = {}
            while self.columns_container_layout.count():
                item = self.columns_container_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

            self.columns_container_layout.addStretch(1)
            for phase in self.phase_order:
                column = PhaseColumn(phase)
                self.phase_columns[phase] = column
                self.columns_container_layout.addWidget(column)
            self.columns_container_layout.addStretch(1)
            QTimer.singleShot(0, self._sync_stage_scrollbar)

        def resizeEvent(self, event) -> None:
            super().resizeEvent(event)
            self._sync_stage_scrollbar()

        def _on_main_splitter_moved(self, _position: int, _index: int) -> None:
            QTimer.singleShot(0, self._sync_stage_scrollbar)

        def _position_stage_scrollbar(self) -> None:
            scroll_geometry = self.columns_scroll.geometry()
            if scroll_geometry.width() <= 0 or scroll_geometry.height() <= 0:
                return
            inset_x = 10
            scrollbar_height = 14
            x = scroll_geometry.x() + inset_x
            width = max(96, scroll_geometry.width() - (inset_x * 2))
            y = self.stage_shell.height() - scrollbar_height - 8
            self.stage_scrollbar.setGeometry(x, y, width, scrollbar_height)
            self.stage_scrollbar.raise_()

        def _sync_stage_scrollbar(self, *_args: Any) -> None:
            internal_scrollbar = self.columns_scroll.horizontalScrollBar()
            self.stage_scrollbar.blockSignals(True)
            self.stage_scrollbar.setRange(
                internal_scrollbar.minimum(),
                internal_scrollbar.maximum(),
            )
            self.stage_scrollbar.setPageStep(internal_scrollbar.pageStep())
            self.stage_scrollbar.setSingleStep(max(24, internal_scrollbar.singleStep()))
            self.stage_scrollbar.setValue(internal_scrollbar.value())
            self.stage_scrollbar.blockSignals(False)
            self.stage_scrollbar.setVisible(internal_scrollbar.maximum() > 0)
            self._position_stage_scrollbar()

        def _config_path_label(self) -> str:
            return _config_path_label_for_header(self.active_config)

        def _runtime_path(self) -> Path:
            return Path(self.active_config["control_dir"]) / "runtime.json"

        def _stop_path(self) -> Path:
            return Path(self.active_config["control_dir"]) / "stop"

        def _read_runtime_payload(self) -> Dict[str, Any]:
            runtime_path = self._runtime_path()
            if not runtime_path.exists():
                return {}
            try:
                payload = json.loads(runtime_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {"phase": "runtime unreadable"}
            return payload if isinstance(payload, dict) else {}

        def _selected_board_title(self) -> Optional[str]:
            item = self.board_list.currentItem()
            if item is None:
                return None
            return item.data(Qt.UserRole + 1)

        def _available_board_titles(self) -> List[str]:
            self._refresh_store_cache()
            return [
                board["title"]
                for board in self._cached_boards
                if str(board["board_id"]).strip().lower() != "archived"
            ]

        def _active_tasks(self, tasks: List[StoredTask]) -> List[StoredTask]:
            return [task for task in tasks if task.board_id != "archived"]

        def _board_from_item(self, item: QListWidgetItem | None) -> Optional[Dict[str, Any]]:
            if item is None:
                return None

            board_id = item.data(Qt.UserRole)
            title = str(item.data(Qt.UserRole + 1) or "")
            count = int(item.data(Qt.UserRole + 2) or 0)
            if board_id is None:
                return {
                    "board_id": None,
                    "title": title,
                    "order": -1,
                    "count": count,
                    "is_virtual": True,
                    "is_protected": True,
                }

            board = next(
                (
                    payload
                    for payload in self._cached_boards
                    if payload["board_id"] == str(board_id)
                ),
                None,
            )
            resolved = dict(board) if board is not None else {
                "board_id": str(board_id),
                "title": title,
                "order": 0,
            }
            resolved["count"] = count
            resolved["is_virtual"] = False
            resolved["is_protected"] = (
                str(resolved.get("board_id", "")) == "archived"
                or str(resolved.get("title", "")).lower() == "archived"
            )
            return resolved

        def _open_board_context_menu(self, pos: Any) -> None:
            item = self.board_list.itemAt(pos)
            board = self._board_from_item(item)
            if board is None:
                return

            if board.get("is_virtual"):
                return

            menu = QMenu(self.board_list)
            rename_action = menu.addAction("Rename")
            delete_action = menu.addAction("Delete")
            if board.get("is_protected"):
                rename_action.setEnabled(False)
                delete_action.setEnabled(False)

            action = menu.exec(self.board_list.mapToGlobal(pos))
            if action == rename_action and rename_action.isEnabled():
                self._open_rename_board_dialog(board)
            elif action == delete_action and delete_action.isEnabled():
                self._delete_board(board)

        def _browse_repo(self) -> None:
            selected = QFileDialog.getExistingDirectory(
                self,
                "Choose Repository",
                str(self.active_config["repo_root"]),
            )
            if not selected:
                return
            self.repo_input.setText(selected)
            self._load_repo_from_input()

        def _load_repo_from_input(self) -> None:
            raw_value = self.repo_input.text().strip()
            if not raw_value:
                QMessageBox.warning(self, "Repository Required", "Enter a repository path.")
                return

            repo_root = _resolve_repo_path(raw_value)
            if not repo_root.exists() or not repo_root.is_dir():
                QMessageBox.warning(self, "Invalid Repository", "Choose an existing directory.")
                return

            try:
                self.active_config = _runtime_config_for_repo(repo_root)
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Load Repository", str(exc))
                return

            new_phase_order = phase_labels(self.active_config)
            if new_phase_order != self.phase_order:
                self._rebuild_phase_columns(new_phase_order)

            self.selected_board_id = None
            self.status_note = "Loaded repo {0}".format(repo_root.name)
            self._invalidate_refresh_cache()
            self._refresh_repo_widgets()
            _save_session(repo_root, self.selected_board_id)
            self.refresh_view()

        def _show_modeless_dialog(self, dialog: QDialog, on_accepted: Any) -> None:
            self._open_dialogs.append(dialog)
            dialog.finished.connect(
                lambda result, current_dialog=dialog: self._finish_modeless_dialog(
                    current_dialog,
                    result,
                    on_accepted,
                )
            )
            dialog.setModal(False)
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        def _finish_modeless_dialog(self, dialog: QDialog, result: int, on_accepted: Any) -> None:
            try:
                if result == QDialog.Accepted:
                    on_accepted()
            finally:
                if dialog in self._open_dialogs:
                    self._open_dialogs.remove(dialog)
                dialog.deleteLater()

        def _activate_repo_config(self, repo_root: Path) -> None:
            self.active_config = _runtime_config_for_repo(repo_root)
            self._invalidate_refresh_cache()
            self._refresh_repo_widgets()

        def _open_settings_dialog(self) -> None:
            dialog = SettingsDialog(self.active_config, self)
            config = dialog.active_config

            def accept_settings() -> None:
                try:
                    saved_path = save_config_overrides(config, dialog.settings_payload())
                    self._activate_repo_config(Path(config["repo_root"]))
                except Exception as exc:
                    QMessageBox.critical(self, "Failed To Save Settings", str(exc))
                    return

                self.status_note = "Saved settings to {0}".format(saved_path)
                self.refresh_view()

            self._show_modeless_dialog(dialog, accept_settings)

        def _open_agents_dialog(self) -> None:
            active_config = self.active_config
            agents_path = _repo_agents_path(Path(active_config["repo_root"]))
            try:
                initial_text = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Read agents.md", str(exc))
                return

            dialog = AgentsFileDialog(agents_path, initial_text, self)

            def accept_agents() -> None:
                try:
                    updated_text = dialog.file_text()
                    if updated_text and not updated_text.endswith("\n"):
                        updated_text += "\n"
                    agents_path.write_text(updated_text, encoding="utf-8")
                except Exception as exc:
                    QMessageBox.critical(self, "Failed To Save agents.md", str(exc))
                    return

                self.status_note = "Saved {0}".format(agents_path.name)
                self._activate_repo_config(Path(active_config["repo_root"]))
                self.refresh_view()

            self._show_modeless_dialog(dialog, accept_agents)

        def _open_start_loop_dialog(self) -> None:
            dialog = StartLoopDialog(self)
            try:
                if dialog.exec() == QDialog.Accepted:
                    self._spawn_runner(dialog.loop_run_args())
            finally:
                dialog.deleteLater()

        def _open_rename_board_dialog(self, board: Dict[str, Any]) -> None:
            dialog = BoardTitleDialog(
                window_title="Rename Board",
                dialog_title="Rename Board",
                caption="Update the board title. Existing tasks stay on the same board.",
                board_title=str(board.get("title", "")),
                parent=self,
            )
            active_config = self.active_config
            board_id = str(board.get("board_id", ""))

            def accept_board() -> None:
                try:
                    updated = rename_board(active_config, board_id, dialog.board_title())
                except Exception as exc:
                    QMessageBox.critical(self, "Failed To Rename Board", str(exc))
                    return
                if updated is None:
                    QMessageBox.critical(self, "Failed To Rename Board", "Board could not be found.")
                    return

                renamed_board_id = str(updated.get("board_id", board_id)).strip() or board_id
                self.selected_board_id = renamed_board_id
                self.status_note = "Renamed board {0} to {1}".format(board.get("title", ""), updated["title"])
                _save_session(Path(active_config["repo_root"]), self.selected_board_id)
                self._activate_repo_config(Path(active_config["repo_root"]))
                self.refresh_view()

            self._show_modeless_dialog(dialog, accept_board)

        def _delete_board(self, board: Dict[str, Any]) -> None:
            board_title = str(board.get("title", ""))
            task_count = int(board.get("count", 0) or 0)
            task_word = "task" if task_count == 1 else "tasks"
            detail = "This will remove {0} {1} from the board store and markdown task file.".format(
                task_count,
                task_word,
            )

            confirmed = QMessageBox.question(
                self,
                "Delete Board",
                'Delete "{0}"?\n\n{1}'.format(board_title, detail),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirmed != QMessageBox.Yes:
                return

            try:
                deleted = delete_board(self.active_config, str(board.get("board_id", "")))
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Delete Board", str(exc))
                return
            if deleted is None:
                QMessageBox.critical(self, "Failed To Delete Board", "Board could not be found.")
                return

            if self.selected_board_id == deleted["board_id"]:
                self.selected_board_id = None
            self.status_note = "Deleted board {0}".format(deleted["title"])
            _save_session(Path(self.active_config["repo_root"]), self.selected_board_id)
            self._activate_repo_config(Path(self.active_config["repo_root"]))
            self.refresh_view()

        def _open_add_board_dialog(self) -> None:
            dialog = AddBoardDialog(self)
            active_config = self.active_config

            def accept_board() -> None:
                try:
                    board = create_board(active_config, dialog.board_title())
                except Exception as exc:
                    QMessageBox.critical(self, "Failed To Create Board", str(exc))
                    return
                self.selected_board_id = board["board_id"]
                self.status_note = "Created board {0}".format(board["title"])
                _save_session(Path(active_config["repo_root"]), self.selected_board_id)
                self._activate_repo_config(Path(active_config["repo_root"]))
                self.refresh_view()

            self._show_modeless_dialog(dialog, accept_board)

        def _open_add_task_dialog(self) -> None:
            board_titles = self._available_board_titles()
            default_board = self._selected_board_title()
            if default_board and default_board.lower() == "archived":
                default_board = None
            if self.selected_board_id is None:
                default_board = str(self.active_config.get("store", {}).get("default_board", "General"))
            elif not default_board:
                default_board = str(self.active_config.get("store", {}).get("default_board", "General"))
            dialog = AddTaskDialog(board_titles, default_board, self)
            active_config = self.active_config

            def accept_task() -> None:
                try:
                    task = create_task(
                        active_config,
                        board_title=dialog.board_title(),
                        title=dialog.task_title(),
                        context_notes=dialog.context_notes(),
                    )
                except Exception as exc:
                    QMessageBox.critical(self, "Failed To Create Task", str(exc))
                    return
                self.selected_board_id = task.board_id
                self.status_note = "Added task {0}".format(task.task_id)
                _save_session(Path(active_config["repo_root"]), self.selected_board_id)
                self._activate_repo_config(Path(active_config["repo_root"]))
                self.refresh_view()

            self._show_modeless_dialog(dialog, accept_task)

        def _open_edit_task_dialog(self, task: StoredTask) -> None:
            active_config = self.active_config
            board_titles = self._available_board_titles()
            phase_order = list(self.phase_order)

            def create_board_for_edit() -> Optional[str]:
                dialog = AddBoardDialog(self)
                try:
                    if dialog.exec() != QDialog.Accepted:
                        return None
                    board = create_board(active_config, dialog.board_title())
                except Exception as exc:
                    QMessageBox.critical(self, "Failed To Create Board", str(exc))
                    return None

                self.status_note = "Created board {0}".format(board["title"])
                _save_session(Path(active_config["repo_root"]), self.selected_board_id)
                self._activate_repo_config(Path(active_config["repo_root"]))
                self.refresh_view()
                return str(board["title"])

            dialog = EditTaskDialog(task, board_titles, phase_order, create_board_for_edit, self)

            def accept_task_update() -> None:
                try:
                    updated = edit_task(
                        active_config,
                        task.task_id,
                        board_title=dialog.board_title(),
                        title=dialog.task_title(),
                        context_notes=dialog.context_notes(),
                        phase=dialog.phase_value(),
                    )
                except Exception as exc:
                    QMessageBox.critical(self, "Failed To Update Task", str(exc))
                    return
                if updated is None:
                    QMessageBox.critical(self, "Failed To Update Task", "Task could not be found.")
                    return
                self.selected_board_id = updated.board_id
                self.status_note = "Updated task {0}".format(updated.task_id)
                _save_session(Path(active_config["repo_root"]), self.selected_board_id)
                self._activate_repo_config(Path(active_config["repo_root"]))
                self.refresh_view()

            self._show_modeless_dialog(dialog, accept_task_update)

        def _delete_task(self, task: StoredTask) -> None:
            if task.source_kind == "markdown":
                detail = "This will remove the task from the task store and the markdown task file."
            else:
                detail = "This will remove the task from the task store."

            confirmed = QMessageBox.question(
                self,
                "Delete Task",
                'Delete "{0}"?\n\n{1}'.format(task.title, detail),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirmed != QMessageBox.Yes:
                return

            try:
                deleted = delete_task(self.active_config, task.task_id)
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Delete Task", str(exc))
                return
            if deleted is None:
                QMessageBox.critical(self, "Failed To Delete Task", "Task could not be found.")
                return

            self.status_note = "Deleted task {0}".format(deleted.task_id)
            _save_session(Path(self.active_config["repo_root"]), self.selected_board_id)
            self.refresh_view()

        def _move_task_to_phase(self, task: StoredTask | str, phase: str) -> None:
            task_id = task.task_id if isinstance(task, StoredTask) else str(task).strip()
            if not task_id:
                QMessageBox.critical(self, "Failed To Update Task", "Task could not be found.")
                return
            try:
                updated = update_task_phase(self.active_config, task_id, phase)
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Update Task", str(exc))
                return
            if updated is None:
                QMessageBox.critical(self, "Failed To Update Task", "Task could not be found.")
                return

            if phase == "completed":
                self.status_note = "Completed task {0}".format(updated.task_id)
            else:
                self.status_note = "Moved task {0} to {1}".format(updated.task_id, _phase_label(phase))
            _save_session(Path(self.active_config["repo_root"]), self.selected_board_id)
            self._activate_repo_config(Path(self.active_config["repo_root"]))
            QTimer.singleShot(0, self.refresh_view)

        def _complete_task(self, task: StoredTask) -> None:
            self._move_task_to_phase(task, "completed")

        def _needs_testing_task(self, task: StoredTask) -> None:
            self._move_task_to_phase(task, "needs_testing")

        def _move_task_to_board(self, task_id: str, board_id: str) -> None:
            current_task = next((task for task in self._cached_tasks if task.task_id == task_id), None)
            if current_task is not None and current_task.board_id == board_id:
                return
            try:
                updated = move_task_to_board(self.active_config, task_id, board_id)
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Update Task", str(exc))
                return
            if updated is None:
                QMessageBox.critical(self, "Failed To Update Task", "Task could not be found.")
                return

            if board_id == "archived":
                self.status_note = "Archived task {0}".format(updated.task_id)
            else:
                self.status_note = "Moved task {0} to {1}".format(updated.task_id, updated.board_title)
            _save_session(Path(self.active_config["repo_root"]), self.selected_board_id)
            self.refresh_view()

        def _start_task(self, task_id: str) -> None:
            self._spawn_runner(_start_task_run_args(task_id))

        def _spawn_runner(self, args: List[str]) -> None:
            runtime_path = self._runtime_path()
            if runtime_path.exists():
                self.status_note = "Runner already active"
                self.refresh_view()
                return

            command = [
                sys.executable,
                str(taskbot_entry),
                "--repo-root",
                str(self.active_config["repo_root"]),
            ]
            config_path = str(self.active_config.get("config_path", "")).strip()
            if config_path:
                command.extend(["--config", config_path])
            command.extend(args)

            subprocess.Popen(
                command,
                cwd=self.active_config["repo_root"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.status_note = "Spawned " + " ".join(args)
            self.refresh_view()

        def _request_stop(self) -> None:
            stop_path = self._stop_path()
            stop_path.parent.mkdir(parents=True, exist_ok=True)
            stop_path.write_text("stop requested\n", encoding="utf-8")
            self.status_note = "Stop requested"
            self.refresh_view()

        def _on_board_selection_changed(self,
                                        current: QListWidgetItem | None,
                                        _previous: QListWidgetItem | None) -> None:
            self.selected_board_id = current.data(Qt.UserRole) if current is not None else None
            _save_session(Path(self.active_config["repo_root"]), self.selected_board_id)
            self.refresh_view()

        def _update_status_header(self,
                                  boards: List[Dict[str, Any]],
                                  tasks: List[StoredTask],
                                  runtime_payload: Dict[str, Any]) -> None:
            runner_phase = str(runtime_payload.get("phase", "idle")).strip() or "idle"
            stop_requested = self._stop_path().exists() or self.status_note.lower() == "stop requested"
            note_html = html.escape(self.status_note)
            if stop_requested:
                note_html = '<span style="color:#a13e35; font-weight:700;">Stop Requested</span>'
            active_tasks = self._active_tasks(tasks)
            self.status_chip.setText(runner_phase.upper())
            self.runtime_label.setText(
                "Config: {0} | Boards: {1} | Tasks: {2} | Note: {3}".format(
                    html.escape(self._config_path_label()),
                    len(boards),
                    len(active_tasks),
                    note_html,
                )
            )

        def _populate_board_list(self, boards: List[Dict[str, Any]], tasks: List[StoredTask]) -> None:
            active_tasks = self._active_tasks(tasks)
            counts: Dict[str, int] = {}
            archived_count = 0
            for task in active_tasks:
                counts[task.board_id] = counts.get(task.board_id, 0) + 1
            for task in tasks:
                if task.board_id == "archived":
                    archived_count += 1

            selected_board_id = self.selected_board_id
            vertical_value, horizontal_value = self._capture_scroll_state(self.board_list)
            self.board_list._clear_drop_target_row()
            self.board_list.blockSignals(True)
            self.board_list.clear()

            all_item = QListWidgetItem("All Boards | {0}".format(len(active_tasks)))
            all_item.setData(Qt.UserRole, None)
            all_item.setData(Qt.UserRole + 1, "All Boards")
            all_item.setData(Qt.UserRole + 2, len(active_tasks))
            self.board_list.addItem(all_item)

            selected_row = 0
            matched_selection = False
            for index, board in enumerate(boards, start=1):
                title = board["title"]
                count = archived_count if board["board_id"] == "archived" else counts.get(board["board_id"], 0)
                item = QListWidgetItem("{0} | {1}".format(title, count))
                item.setData(Qt.UserRole, board["board_id"])
                item.setData(Qt.UserRole + 1, title)
                item.setData(Qt.UserRole + 2, count)
                self.board_list.addItem(item)
                if selected_board_id and board["board_id"] == selected_board_id:
                    selected_row = index
                    matched_selection = True

            if not matched_selection:
                self.selected_board_id = None
            self.board_list.setCurrentRow(selected_row)
            self.board_list.blockSignals(False)
            QTimer.singleShot(
                0,
                lambda: self._restore_scroll_state(
                    self.board_list,
                    vertical_value,
                    horizontal_value,
                ),
            )

        def _update_board_header(self, tasks: List[StoredTask], boards: List[Dict[str, Any]]) -> None:
            selected_title = self._selected_board_title()
            if selected_title:
                self.board_title_label.setText(selected_title)
                self.board_summary_label.setText("{0} tasks | {1} columns".format(len(tasks), len(self.phase_order)))
            else:
                active_tasks = self._active_tasks(tasks)
                self.board_title_label.setText("All Boards")
                self.board_summary_label.setText(
                    "{0} tasks | {1} boards | {2} columns".format(
                        len(active_tasks),
                        len(boards),
                        len(self.phase_order),
                    )
                )

        def _refresh_columns(self, tasks: List[StoredTask]) -> None:
            selected_board_id = self.selected_board_id
            vertical_value, horizontal_value = self._capture_scroll_state(self.columns_scroll)
            visible_tasks = self._active_tasks(tasks)
            if selected_board_id:
                visible_tasks = [task for task in tasks if task.board_id == selected_board_id]

            for phase in self.phase_order:
                phase_tasks = [task for task in visible_tasks if task.phase == phase]
                self.phase_columns[phase].set_tasks(
                    phase_tasks,
                    on_start_task=self._start_task,
                    on_edit_task=self._open_edit_task_dialog,
                    on_delete_task=self._delete_task,
                    on_move_task=self._move_task_to_phase,
                    on_move_task_to_board=self._move_task_to_board,
                )
            QTimer.singleShot(
                0,
                lambda: self._restore_columns_scroll_state(
                    vertical_value,
                    horizontal_value,
                ),
            )

        def _refresh_terminal(self) -> None:
            path = terminal_log_path(self.active_config)
            current_signature = _path_signature(path)
            if current_signature == self._terminal_signature:
                return

            self._terminal_signature = current_signature
            if not current_signature[0]:
                text = ""
            else:
                text = "\n".join(
                    path.read_text(encoding="utf-8", errors="ignore").splitlines()[-tail_lines:]
                )
            if not _terminal_text_should_refresh(self._last_terminal_text, text):
                return

            vertical_scrollbar = self.terminal_output.verticalScrollBar()
            horizontal_scrollbar = self.terminal_output.horizontalScrollBar()
            follow_tail = vertical_scrollbar.value() >= max(0, vertical_scrollbar.maximum() - 8)
            vertical_value = vertical_scrollbar.value()
            horizontal_value = horizontal_scrollbar.value()
            self._last_terminal_text = text
            self.terminal_output.setHtml(_ansi_text_to_html(text, self.terminal_font_family))
            QTimer.singleShot(
                0,
                lambda: self._restore_terminal_viewport(
                    follow_tail,
                    vertical_value,
                    horizontal_value,
                ),
            )

        def _restore_terminal_viewport(self,
                                       follow_tail: bool,
                                       vertical_value: int,
                                       horizontal_value: int) -> None:
            vertical_scrollbar = self.terminal_output.verticalScrollBar()
            horizontal_scrollbar = self.terminal_output.horizontalScrollBar()
            if follow_tail:
                self.terminal_output.moveCursor(QTextCursor.End)
                vertical_scrollbar.setValue(vertical_scrollbar.maximum())
            else:
                vertical_scrollbar.setValue(min(vertical_value, vertical_scrollbar.maximum()))
            horizontal_scrollbar.setValue(min(horizontal_value, horizontal_scrollbar.maximum()))

        def refresh_view(self) -> None:
            store_changed = self._refresh_store_cache()
            self._refresh_runtime_cache()

            boards = self._cached_boards
            tasks = self._cached_tasks
            runtime_payload = self._cached_runtime_payload
            self._update_status_header(boards, tasks, runtime_payload)
            if store_changed:
                self._populate_board_list(boards, tasks)

            selection_changed = self.selected_board_id != self._last_rendered_selected_board_id
            if store_changed or selection_changed:
                visible_tasks = self._active_tasks(tasks) if self.selected_board_id is None else [
                    task for task in tasks if task.board_id == self.selected_board_id
                ]
                self._update_board_header(visible_tasks, boards)
                self._refresh_columns(tasks)
                self._last_rendered_selected_board_id = self.selected_board_id
                QTimer.singleShot(0, self._sync_stage_scrollbar)

            self._refresh_terminal()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app_font = QFont(_preferred_monospace_family())
    app_font.setPointSize(10)
    app.setFont(app_font)
    command_enter_filter = CommandEnterModalFilter(app)
    app.installEventFilter(command_enter_filter)
    app._command_enter_modal_filter = command_enter_filter
    window = TaskbotWindow()
    window.show()
    return int(app.exec())


def launch_textual_ui(config: Dict[str, Any]) -> int:
    return launch_ui(config)
