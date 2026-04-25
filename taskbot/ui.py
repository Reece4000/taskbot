from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from taskbot.config import discover_config_path, ensure_runtime_directories, load_config
from taskbot.store import (
    StoredTask,
    create_board,
    create_task,
    ensure_task_store,
    list_boards,
    load_store_snapshot,
    phase_labels,
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
        '<pre style="margin:0; white-space:pre-wrap; '
        'font-family:\'{0}\'; font-size:11pt; color:#d7e0ea;">{1}</pre>'
        "</body></html>"
    ).format(html.escape(font_family, quote=True), "".join(parts))


def launch_ui(config: Dict[str, Any]) -> int:
    try:
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import (
            QApplication,
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QFileDialog,
            QFrame,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QScrollArea,
            QScrollBar,
            QSizePolicy,
            QSplitter,
            QSpacerItem,
            QToolButton,
            QVBoxLayout,
            QWidget,
            QTextEdit,
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

    class AddBoardDialog(QDialog):
        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setWindowTitle("New Board")
            self.setModal(True)
            self.resize(420, 160)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 24, 24, 24)
            layout.setSpacing(14)

            title = QLabel("Create Board")
            title.setObjectName("DialogTitle")
            layout.addWidget(title)

            caption = QLabel("Boards live in the left rail and can be empty until you add tasks.")
            caption.setObjectName("DialogCaption")
            caption.setWordWrap(True)
            layout.addWidget(caption)

            self.title_input = QLineEdit()
            self.title_input.setPlaceholderText("Board title")
            layout.addWidget(self.title_input)

            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def accept(self) -> None:
            if not self.board_title():
                QMessageBox.warning(self, "Board Title Required", "Enter a board title.")
                self.title_input.setFocus()
                return
            super().accept()

        def board_title(self) -> str:
            return self.title_input.text().strip()

    class AddTaskDialog(QDialog):
        def __init__(self,
                     board_titles: List[str],
                     default_board: str,
                     parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setWindowTitle("Add Task")
            self.setModal(True)
            self.resize(560, 320)

            available_boards = list(board_titles)
            if default_board and default_board not in available_boards:
                available_boards.append(default_board)
            if not available_boards:
                available_boards.append("General")

            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 24, 24, 24)
            layout.setSpacing(14)

            title = QLabel("Add Task")
            title.setObjectName("DialogTitle")
            layout.addWidget(title)

            caption = QLabel("Create a task in the selected board. Context is optional and can be concise.")
            caption.setObjectName("DialogCaption")
            caption.setWordWrap(True)
            layout.addWidget(caption)

            board_label = QLabel("Board")
            board_label.setObjectName("FieldLabel")
            layout.addWidget(board_label)

            self.board_combo = QComboBox()
            self.board_combo.addItems(available_boards)
            self.board_combo.setEditable(True)
            self.board_combo.setCurrentText(default_board or available_boards[0])
            layout.addWidget(self.board_combo)

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
            self.context_input.setPlaceholderText("Any constraints, notes, or acceptance details.")
            self.context_input.setFixedHeight(110)
            layout.addWidget(self.context_input)

            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

            self.title_input.setFocus()

        def accept(self) -> None:
            if not self.task_title():
                QMessageBox.warning(self, "Task Title Required", "Enter a task title.")
                self.title_input.setFocus()
                return
            if not self.board_title():
                QMessageBox.warning(self, "Board Required", "Choose or enter a board.")
                self.board_combo.setFocus()
                return
            super().accept()

        def board_title(self) -> str:
            return self.board_combo.currentText().strip()

        def task_title(self) -> str:
            return self.title_input.text().strip()

        def context_notes(self) -> str:
            return self.context_input.toPlainText().strip()

    class TaskCard(QFrame):
        def __init__(self, task: StoredTask, *, show_board: bool) -> None:
            super().__init__()
            self.setObjectName("TaskCard")

            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(7)

            if show_board:
                board_badge = QLabel(task.board_title)
                board_badge.setObjectName("BoardBadge")
                layout.addWidget(board_badge, 0, Qt.AlignLeft)

            title = QLabel(task.title)
            title.setObjectName("TaskTitle")
            title.setWordWrap(True)
            layout.addWidget(title)

            context_text = task.context_notes.strip()
            if context_text:
                context = QLabel(context_text[:220] + ("..." if len(context_text) > 220 else ""))
                context.setObjectName("TaskContext")
                context.setWordWrap(True)
                layout.addWidget(context)

            meta_parts = [task.task_id[:8]]
            meta_parts.append("plan ready" if task.plan_status == "ready" else "plan pending")
            relevant_files = []
            if isinstance(task.plan, dict):
                relevant_files = task.plan.get("relevant_files", [])
            if isinstance(relevant_files, list) and relevant_files:
                meta_parts.append("{0} files".format(len(relevant_files)))
            meta = QLabel("  •  ".join(meta_parts))
            meta.setObjectName("TaskMeta")
            layout.addWidget(meta)

            if task.last_error:
                error = QLabel(task.last_error)
                error.setObjectName("TaskError")
                error.setWordWrap(True)
                layout.addWidget(error)

    class PhaseColumn(QFrame):
        def __init__(self, phase: str) -> None:
            super().__init__()
            self.phase = phase
            self.setObjectName("PhaseColumn")
            self.setMinimumWidth(272)
            self.setMaximumWidth(304)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(12)

            header_row = QHBoxLayout()
            header_row.setContentsMargins(0, 0, 0, 0)
            header_row.setSpacing(8)

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

        def set_tasks(self, tasks: List[StoredTask], *, show_board: bool) -> None:
            self.count_label.setText(str(len(tasks)))
            while self.body_layout.count():
                item = self.body_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

            if not tasks:
                empty = QLabel("No tasks")
                empty.setObjectName("ColumnEmpty")
                self.body_layout.addWidget(empty)
                return

            for task in tasks:
                self.body_layout.addWidget(TaskCard(task, show_board=show_board))

            self.body_layout.addStretch(1)

    class TaskbotWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.saved_session = _load_saved_session()
            self.status_note = "Ready"
            self._last_terminal_text = ""
            self.active_config = self._initial_config()
            self.selected_board_id = self._initial_board_id()
            self.terminal_font_family = _preferred_monospace_family()
            self.phase_order: List[str] = []
            self.phase_columns: Dict[str, PhaseColumn] = {}
            self.stage_shell: QFrame
            self.stage_scrollbar: QScrollBar

            self.setWindowTitle("Taskbot")
            self.resize(1560, 980)
            self.setMinimumSize(1180, 760)

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
            root.setContentsMargins(16, 16, 16, 16)
            root.setSpacing(12)

            top_region = QWidget()
            top_region_layout = QVBoxLayout(top_region)
            top_region_layout.setContentsMargins(0, 0, 0, 0)
            top_region_layout.setSpacing(12)

            top_shell = QFrame()
            top_shell.setObjectName("TopShell")
            top_layout = QVBoxLayout(top_shell)
            top_layout.setContentsMargins(14, 12, 14, 12)
            top_layout.setSpacing(10)

            title_row = QHBoxLayout()
            title_row.setContentsMargins(0, 0, 0, 0)
            title_row.setSpacing(10)

            title_stack = QVBoxLayout()
            title_stack.setContentsMargins(0, 0, 0, 0)
            title_stack.setSpacing(0)

            eyebrow = QLabel("TASKBOT")
            eyebrow.setObjectName("Eyebrow")
            title_stack.addWidget(eyebrow)

            headline = QLabel("Local Agent Board")
            headline.setObjectName("Headline")
            title_stack.addWidget(headline)
            title_row.addLayout(title_stack)
            title_row.addItem(QSpacerItem(12, 12, QSizePolicy.Expanding, QSizePolicy.Minimum))

            self.status_chip = QLabel("Idle")
            self.status_chip.setObjectName("StatusChip")
            title_row.addWidget(self.status_chip, 0, Qt.AlignTop)
            top_layout.addLayout(title_row)

            repo_row = QHBoxLayout()
            repo_row.setContentsMargins(0, 0, 0, 0)
            repo_row.setSpacing(10)

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

            load_button = QPushButton("Load Repo")
            load_button.setObjectName("PrimaryButton")
            load_button.clicked.connect(self._load_repo_from_input)
            repo_row.addWidget(load_button)
            top_layout.addLayout(repo_row)

            controls_row = QHBoxLayout()
            controls_row.setContentsMargins(0, 0, 0, 0)
            controls_row.setSpacing(10)

            self.runtime_label = QLabel("")
            self.runtime_label.setObjectName("RuntimeLabel")
            self.runtime_label.setWordWrap(False)
            self.runtime_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            controls_row.addWidget(self.runtime_label, 1)

            self.plan_button = QPushButton("Plan Once")
            self.plan_button.clicked.connect(lambda: self._spawn_runner(["plan"]))
            controls_row.addWidget(self.plan_button)

            self.run_button = QPushButton("Run Once")
            self.run_button.clicked.connect(lambda: self._spawn_runner(["run", "--iterations", "1"]))
            controls_row.addWidget(self.run_button)

            self.loop_button = QPushButton("Start Loop")
            self.loop_button.clicked.connect(lambda: self._spawn_runner(["run", "--continuous"]))
            controls_row.addWidget(self.loop_button)

            self.stop_button = QPushButton("Stop")
            self.stop_button.setObjectName("DangerButton")
            self.stop_button.clicked.connect(self._request_stop)
            controls_row.addWidget(self.stop_button)
            top_layout.addLayout(controls_row)

            top_region_layout.addWidget(top_shell)

            content_row = QHBoxLayout()
            content_row.setContentsMargins(0, 0, 0, 0)
            content_row.setSpacing(12)

            sidebar = QFrame()
            sidebar.setObjectName("Sidebar")
            sidebar.setMinimumWidth(240)
            sidebar.setMaximumWidth(280)
            sidebar_layout = QVBoxLayout(sidebar)
            sidebar_layout.setContentsMargins(14, 14, 14, 14)
            sidebar_layout.setSpacing(10)

            boards_header = QHBoxLayout()
            boards_header.setContentsMargins(0, 0, 0, 0)
            boards_header.setSpacing(8)

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

            self.board_list = QListWidget()
            self.board_list.setObjectName("BoardList")
            self.board_list.currentItemChanged.connect(self._on_board_selection_changed)
            sidebar_layout.addWidget(self.board_list, 1)
            content_row.addWidget(sidebar)

            center_shell = QVBoxLayout()
            center_shell.setContentsMargins(0, 0, 0, 0)
            center_shell.setSpacing(10)

            board_header = QFrame()
            board_header.setObjectName("BoardHeader")
            board_header_layout = QHBoxLayout(board_header)
            board_header_layout.setContentsMargins(14, 12, 14, 12)
            board_header_layout.setSpacing(10)

            board_title_stack = QVBoxLayout()
            board_title_stack.setContentsMargins(0, 0, 0, 0)
            board_title_stack.setSpacing(2)

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
            self.add_task_button.setMaximumWidth(96)
            board_header_layout.addWidget(self.add_task_button)

            refresh_button = QPushButton("Refresh")
            refresh_button.clicked.connect(self.refresh_view)
            board_header_layout.addWidget(refresh_button)
            center_shell.addWidget(board_header)

            self.columns_scroll = QScrollArea()
            self.columns_scroll.setObjectName("ColumnsScroll")
            self.columns_scroll.setWidgetResizable(True)
            self.columns_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.columns_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.columns_scroll.viewport().setObjectName("ColumnsViewport")

            self.columns_container = QWidget()
            self.columns_container.setObjectName("ColumnsContainer")
            self.columns_container_layout = QHBoxLayout(self.columns_container)
            self.columns_container_layout.setContentsMargins(14, 14, 14, 32)
            self.columns_container_layout.setSpacing(10)
            self.columns_scroll.setWidget(self.columns_container)

            self.stage_shell = QFrame()
            self.stage_shell.setObjectName("StageShell")
            stage_layout = QVBoxLayout(self.stage_shell)
            stage_layout.setContentsMargins(8, 8, 8, 8)
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

            center_shell.addWidget(self.stage_shell, 1)

            content_row.addLayout(center_shell, 1)
            top_region_layout.addLayout(content_row, 1)

            terminal_shell = QFrame()
            terminal_shell.setObjectName("TerminalShell")
            terminal_layout = QVBoxLayout(terminal_shell)
            terminal_layout.setContentsMargins(14, 12, 14, 12)
            terminal_layout.setSpacing(8)

            terminal_header = QHBoxLayout()
            terminal_header.setContentsMargins(0, 0, 0, 0)
            terminal_header.setSpacing(10)

            terminal_title = QLabel("Terminal Output")
            terminal_title.setObjectName("TerminalTitle")
            terminal_header.addWidget(terminal_title)
            terminal_header.addItem(QSpacerItem(12, 12, QSizePolicy.Expanding, QSizePolicy.Minimum))

            terminal_hint = QLabel("Live tail from the selected repository.")
            terminal_hint.setObjectName("TerminalHint")
            terminal_header.addWidget(terminal_hint)
            terminal_layout.addLayout(terminal_header)

            self.terminal_output = QTextEdit()
            self.terminal_output.setObjectName("TerminalOutput")
            self.terminal_output.setReadOnly(True)
            self.terminal_output.setLineWrapMode(QTextEdit.NoWrap)
            terminal_font = QFont(self.terminal_font_family)
            terminal_font.setStyleHint(QFont.Monospace)
            terminal_font.setPointSize(11)
            self.terminal_output.setFont(terminal_font)
            self.terminal_output.setMinimumHeight(150)
            terminal_layout.addWidget(self.terminal_output)

            self.main_splitter = QSplitter(Qt.Vertical)
            self.main_splitter.setObjectName("MainSplitter")
            self.main_splitter.setChildrenCollapsible(False)
            self.main_splitter.addWidget(top_region)
            self.main_splitter.addWidget(terminal_shell)
            self.main_splitter.setStretchFactor(0, 1)
            self.main_splitter.setStretchFactor(1, 0)
            self.main_splitter.setSizes([760, 220])
            root.addWidget(self.main_splitter, 1)

        def _apply_window_style(self) -> None:
            stylesheet = """
            QMainWindow {
                background: #efe7dc;
                color: #261d18;
            }

            QWidget {
                font-size: 13px;
            }

            QFrame#TopShell,
            QFrame#BoardHeader,
            QFrame#TerminalShell,
            QFrame#StageShell {
                background: #fbf8f3;
                border: 1px solid #ddcfbf;
                border-radius: 8px;
            }

            QFrame#Sidebar {
                background: #f5efe5;
                border: 1px solid #ddd0bf;
                border-radius: 8px;
            }

            QLabel#Eyebrow {
                color: #a45b38;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 0.18em;
            }

            QLabel#Headline {
                color: #1f1814;
                font-size: 22px;
                font-weight: 700;
            }

            QLabel#StatusChip {
                background: #f5ddcf;
                color: #8d4728;
                border-radius: 4px;
                padding: 5px 10px;
                font-weight: 700;
            }

            QLabel#TopFieldLabel,
            QLabel#FieldLabel {
                color: #765d4f;
                font-size: 11px;
                font-weight: 700;
                text-transform: uppercase;
            }

            QLineEdit,
            QComboBox,
            QPlainTextEdit,
            QListWidget {
                background: #fffdf9;
                color: #221a16;
                border: 1px solid #d8cab9;
                border-radius: 4px;
                padding: 8px 10px;
            }

            QLineEdit:focus,
            QComboBox:focus,
            QPlainTextEdit:focus,
            QListWidget:focus {
                border: 1px solid #c8643b;
            }

            QComboBox::drop-down {
                border: none;
                width: 28px;
            }

            QPushButton,
            QToolButton,
            QDialogButtonBox QPushButton {
                background: #efe4d7;
                color: #2a221d;
                border: 1px solid #d8cab9;
                border-radius: 4px;
                padding: 8px 12px;
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

            QPushButton#DangerButton {
                background: #3c1f1f;
                color: #ffe9e7;
                border: 1px solid #613131;
            }

            QPushButton#DangerButton:hover {
                background: #512626;
            }

            QToolButton#SmallActionButton {
                min-width: 28px;
                min-height: 28px;
                max-width: 28px;
                max-height: 28px;
                font-size: 16px;
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
            QLabel#DialogCaption,
            QLabel#SidebarCaption {
                color: #6d594d;
                font-size: 12px;
            }

            QLabel#SidebarTitle {
                color: #2a221d;
                font-size: 16px;
                font-weight: 700;
            }

            QListWidget#BoardList {
                background: #fbf8f3;
                color: #2a221d;
                border: 1px solid #e2d6c7;
                border-radius: 4px;
                padding: 4px;
            }

            QListWidget#BoardList::item {
                padding: 10px 11px;
                border-radius: 3px;
                margin-bottom: 2px;
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
                font-size: 18px;
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
                background: rgba(244, 236, 226, 0.92);
                height: 18px;
                margin: 0;
                border: 1px solid rgba(119, 96, 80, 0.18);
                border-radius: 4px;
            }

            QScrollBar#StageScrollBar::handle:horizontal {
                background: rgba(77, 62, 51, 0.34);
                min-width: 84px;
                border-radius: 4px;
            }

            QScrollBar#StageScrollBar::handle:horizontal:hover {
                background: rgba(77, 62, 51, 0.46);
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

            QSplitter#MainSplitter::handle {
                background: transparent;
            }

            QSplitter#MainSplitter::handle:vertical {
                height: 8px;
                margin: 2px 0;
            }

            QSplitter#MainSplitter::handle:vertical:hover {
                background: rgba(77, 62, 51, 0.08);
            }

            QFrame#PhaseColumn {
                background: #faf6ef;
                border: 1px solid #ddcfbf;
                border-radius: 6px;
            }

            QLabel#PhaseTitle {
                color: #241c17;
                font-size: 16px;
                font-weight: 700;
            }

            QLabel#PhaseCount {
                background: #efe4d8;
                color: #7f6658;
                border-radius: 3px;
                padding: 3px 8px;
                font-weight: 700;
            }

            QFrame#TaskCard {
                background: #ffffff;
                border: 1px solid #eadfce;
                border-radius: 5px;
            }

            QLabel#BoardBadge {
                background: #f5ddcf;
                color: #934e2e;
                border-radius: 3px;
                padding: 3px 6px;
                font-size: 11px;
                font-weight: 700;
            }

            QLabel#TaskTitle {
                color: #1f1814;
                font-size: 15px;
                font-weight: 700;
            }

            QLabel#TaskContext {
                color: #655449;
                font-size: 13px;
            }

            QLabel#TaskMeta {
                color: #957d6e;
                font-size: 11px;
                font-weight: 600;
            }

            QLabel#TaskError {
                color: #a13e35;
                font-size: 12px;
                font-weight: 600;
            }

            QLabel#ColumnEmpty {
                color: #9b8577;
                font-style: italic;
                padding: 12px 2px;
            }

            QLabel#TerminalTitle,
            QLabel#DialogTitle {
                color: #1f1814;
                font-size: 16px;
                font-weight: 700;
            }

            QTextEdit#TerminalOutput {
                background: #171d22;
                color: #d7e0ea;
                border: 1px solid #273341;
                border-radius: 4px;
                padding: 10px;
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
            """
            self.setStyleSheet(stylesheet)

        def _refresh_repo_widgets(self) -> None:
            self.repo_input.setText(str(self.active_config["repo_root"]))

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

        def _position_stage_scrollbar(self) -> None:
            scroll_geometry = self.columns_scroll.geometry()
            if scroll_geometry.width() <= 0 or scroll_geometry.height() <= 0:
                return
            inset_x = 14
            scrollbar_height = 18
            x = scroll_geometry.x() + inset_x
            width = max(96, scroll_geometry.width() - (inset_x * 2))
            y = scroll_geometry.y() + scroll_geometry.height() - scrollbar_height - 10
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
            config_path = str(self.active_config.get("config_path", "")).strip()
            return config_path if config_path else "defaults only"

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
            return [board["title"] for board in list_boards(self.active_config)]

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
            self._last_terminal_text = ""
            self._refresh_repo_widgets()
            _save_session(repo_root, self.selected_board_id)
            self.refresh_view()

        def _open_add_board_dialog(self) -> None:
            dialog = AddBoardDialog(self)
            if dialog.exec() != QDialog.Accepted:
                return
            try:
                board = create_board(self.active_config, dialog.board_title())
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Create Board", str(exc))
                return
            self.selected_board_id = board["board_id"]
            self.status_note = "Created board {0}".format(board["title"])
            _save_session(Path(self.active_config["repo_root"]), self.selected_board_id)
            self.refresh_view()

        def _open_add_task_dialog(self) -> None:
            board_titles = self._available_board_titles()
            default_board = self._selected_board_title() or str(
                self.active_config.get("store", {}).get("default_board", "General")
            )
            dialog = AddTaskDialog(board_titles, default_board, self)
            if dialog.exec() != QDialog.Accepted:
                return
            try:
                task = create_task(
                    self.active_config,
                    board_title=dialog.board_title(),
                    title=dialog.task_title(),
                    context_notes=dialog.context_notes(),
                )
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Create Task", str(exc))
                return
            self.selected_board_id = task.board_id
            self.status_note = "Added task {0}".format(task.task_id)
            _save_session(Path(self.active_config["repo_root"]), self.selected_board_id)
            self.refresh_view()

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
            note = self.status_note
            self.status_chip.setText(runner_phase.upper())
            self.runtime_label.setText(
                "Config: {0}  •  Boards: {1}  •  Tasks: {2}  •  Note: {3}".format(
                    self._config_path_label(),
                    len(boards),
                    len(tasks),
                    note,
                )
            )

        def _populate_board_list(self, boards: List[Dict[str, Any]], tasks: List[StoredTask]) -> None:
            counts: Dict[str, int] = {}
            for task in tasks:
                counts[task.board_id] = counts.get(task.board_id, 0) + 1

            selected_board_id = self.selected_board_id
            self.board_list.blockSignals(True)
            self.board_list.clear()

            all_item = QListWidgetItem("All Boards  ·  {0}".format(len(tasks)))
            all_item.setData(Qt.UserRole, None)
            all_item.setData(Qt.UserRole + 1, None)
            self.board_list.addItem(all_item)

            selected_row = 0
            matched_selection = False
            for index, board in enumerate(boards, start=1):
                title = board["title"]
                count = counts.get(board["board_id"], 0)
                item = QListWidgetItem("{0}  ·  {1}".format(title, count))
                item.setData(Qt.UserRole, board["board_id"])
                item.setData(Qt.UserRole + 1, title)
                self.board_list.addItem(item)
                if selected_board_id and board["board_id"] == selected_board_id:
                    selected_row = index
                    matched_selection = True

            if not matched_selection:
                self.selected_board_id = None
            self.board_list.setCurrentRow(selected_row)
            self.board_list.blockSignals(False)

        def _update_board_header(self, tasks: List[StoredTask], boards: List[Dict[str, Any]]) -> None:
            selected_title = self._selected_board_title()
            if selected_title:
                self.board_title_label.setText(selected_title)
                self.board_summary_label.setText(
                    "{0} tasks across {1} workflow columns".format(
                        len(tasks),
                        len(self.phase_order),
                    )
                )
            else:
                self.board_title_label.setText("All Boards")
                self.board_summary_label.setText(
                    "{0} tasks across {1} boards and {2} workflow columns".format(
                        len(tasks),
                        len(boards),
                        len(self.phase_order),
                    )
                )

        def _refresh_columns(self, tasks: List[StoredTask]) -> None:
            selected_board_id = self.selected_board_id
            visible_tasks = tasks
            if selected_board_id:
                visible_tasks = [task for task in tasks if task.board_id == selected_board_id]

            show_board = selected_board_id is None
            for phase in self.phase_order:
                phase_tasks = [task for task in visible_tasks if task.phase == phase]
                self.phase_columns[phase].set_tasks(phase_tasks, show_board=show_board)

        def _refresh_terminal(self) -> None:
            path = terminal_log_path(self.active_config)
            if not path.exists():
                text = ""
            else:
                text = "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[-tail_lines:])
            if text == self._last_terminal_text:
                return

            scroll_bar = self.terminal_output.verticalScrollBar()
            at_bottom = scroll_bar.value() >= max(0, scroll_bar.maximum() - 8)
            self._last_terminal_text = text
            self.terminal_output.setHtml(_ansi_text_to_html(text, self.terminal_font_family))
            if at_bottom:
                scroll_bar.setValue(scroll_bar.maximum())

        def refresh_view(self) -> None:
            store = load_store_snapshot(self.active_config)
            boards = list_boards(self.active_config)
            tasks = [
                StoredTask.from_payload(payload)
                for payload in store.get("tasks", [])
                if isinstance(payload, dict)
            ]
            tasks.sort(key=lambda task: (task.board_title.lower(), task.order, task.title.lower()))

            runtime_payload = self._read_runtime_payload()
            self._update_status_header(boards, tasks, runtime_payload)
            self._populate_board_list(boards, tasks)
            self._update_board_header(tasks if self.selected_board_id is None else [
                task for task in tasks if task.board_id == self.selected_board_id
            ], boards)
            self._refresh_columns(tasks)
            self._refresh_terminal()
            QTimer.singleShot(0, self._sync_stage_scrollbar)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app_font = QFont(_preferred_monospace_family())
    app_font.setPointSize(11)
    app.setFont(app_font)
    window = TaskbotWindow()
    window.show()
    return int(app.exec())


def launch_textual_ui(config: Dict[str, Any]) -> int:
    return launch_ui(config)
