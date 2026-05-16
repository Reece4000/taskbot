from __future__ import annotations

import ctypes
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from taskbot.config import (
    discover_config_path,
    editable_config_path,
    ensure_runtime_directories,
    load_config,
    save_config_overrides,
)
from taskbot.git_integration import checkout_git_branch, inspect_git_branches
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
    update_task_fields,
    update_task_phase,
)
from taskbot.terminal_stream import read_terminal_tail, terminal_log_path


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
TICKET_DEVELOPMENT_TURN_TIMEOUT_MS = 90_000

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
UI_THEME_NAMES = ("light", "dark")
UI_LIGHT_THEME: Dict[str, str] = {
    "main_window_bg": "#efe7dc",
    "main_window_fg": "#261d18",
    "shell_bg": "#fbf8f3",
    "shell_border": "#ddcfbf",
    "board_divider": "rgba(77, 62, 51, 0.10)",
    "sidebar_bg": "#f5efe5",
    "sidebar_border": "#ddd0bf",
    "header_divider": "rgba(77, 62, 51, 0.12)",
    "headline": "#1f1814",
    "status_bg": "#f5ddcf",
    "status_fg": "#8d4728",
    "top_field_label": "#765d4f",
    "field_label": "#6b584c",
    "checkbox_text": "#2a221d",
    "checkbox_bg": "#fffdf9",
    "checkbox_border": "#d8cab9",
    "checkbox_checked_bg": "#c8643b",
    "checkbox_checked_border": "#b65731",
    "input_bg": "#fffdf9",
    "input_fg": "#221a16",
    "input_border": "#d8cab9",
    "input_focus": "#c8643b",
    "input_disabled_bg": "#f4ece2",
    "input_disabled_fg": "#8c7b6f",
    "button_bg": "#efe4d7",
    "button_fg": "#2a221d",
    "button_border": "#d8cab9",
    "button_hover": "#e6d7c6",
    "primary_bg": "#c8643b",
    "primary_fg": "#fff8f2",
    "primary_border": "#b65731",
    "primary_hover": "#b85b34",
    "plan_bg": "#d8e9fa",
    "plan_fg": "#23456a",
    "plan_border": "#b6cfe7",
    "plan_hover": "#cae0f4",
    "run_bg": "#dcefdc",
    "run_fg": "#275533",
    "run_border": "#b9d6ba",
    "run_hover": "#cde4ce",
    "loop_bg": "#215a34",
    "loop_fg": "#ffffff",
    "loop_border": "#174325",
    "loop_hover": "#2a6a3e",
    "stop_bg": "#6f2222",
    "stop_fg": "#ffffff",
    "stop_border": "#551818",
    "stop_hover": "#832929",
    "terminal_button_bg": "#111111",
    "terminal_button_fg": "#ffffff",
    "terminal_button_border": "#000000",
    "terminal_button_hover": "#222222",
    "theme_toggle_bg": "#f1ebe2",
    "theme_toggle_fg": "#58473d",
    "theme_toggle_border": "#d8cab9",
    "theme_toggle_hover": "#e8ddd0",
    "theme_toggle_checked_bg": "#2b323a",
    "theme_toggle_checked_fg": "#eff4fa",
    "theme_toggle_checked_border": "#1f262d",
    "small_action_bg": "#f4c9b3",
    "small_action_fg": "#7a3c22",
    "small_action_border": "#d3997e",
    "muted_text": "#6d594d",
    "sidebar_title": "#2a221d",
    "board_list_bg": "#fbf8f3",
    "board_list_fg": "#2a221d",
    "board_list_border": "#e2d6c7",
    "board_list_selected_bg": "#edd6c7",
    "board_list_selected_fg": "#251c17",
    "board_list_hover_bg": "#f1e7dc",
    "board_title": "#1f1814",
    "columns_bg": "#f3ece2",
    "stage_scrollbar_bg": "rgba(243, 236, 226, 0.78)",
    "stage_scrollbar_border": "rgba(119, 96, 80, 0.12)",
    "stage_scrollbar_handle": "rgba(77, 62, 51, 0.25)",
    "stage_scrollbar_handle_hover": "rgba(77, 62, 51, 0.36)",
    "phase_column_bg": "#faf6ef",
    "phase_column_border": "#ddcfbf",
    "phase_drag_bg": "#f4eadf",
    "phase_title": "#241c17",
    "phase_count_bg": "#efe4d8",
    "phase_count_fg": "#7f6658",
    "task_card_bg": "#ffffff",
    "task_card_border": "#eadfce",
    "task_card_drag_bg": "#fff4e8",
    "task_card_hover_bg": "#fff9f1",
    "task_card_hover_border": "#d8c0a8",
    "task_card_focus_bg": "#fff7eb",
    "task_card_focus_border": "#cfa683",
    "card_delete_bg": "#8f2f29",
    "card_delete_fg": "#fff8f4",
    "card_delete_border": "#70221d",
    "card_delete_hover_bg": "#a33b34",
    "card_delete_hover_border": "#81302a",
    "card_delete_pressed_bg": "#74211c",
    "card_delete_pressed_border": "#5f1c18",
    "success_bg": "#e5f2e5",
    "success_fg": "#2f6b44",
    "success_border": "#c8dec9",
    "success_hover_bg": "#d8ead9",
    "success_hover_border": "#b8d2b8",
    "reject_bg": "#f6dfdc",
    "reject_fg": "#8f2f29",
    "reject_border": "#ebc4bf",
    "reject_hover_bg": "#f1cfca",
    "reject_hover_border": "#dda8a1",
    "needs_testing_bg": "#fff0d9",
    "needs_testing_fg": "#8d5c15",
    "needs_testing_border": "#ebd2a2",
    "needs_testing_hover_bg": "#f8e2b8",
    "needs_testing_hover_border": "#dfc08d",
    "board_badge_bg": "#f5ddcf",
    "board_badge_fg": "#934e2e",
    "task_title": "#1f1814",
    "task_context": "#655449",
    "task_meta": "#957d6e",
    "task_error": "#a13e35",
    "column_empty": "#9b8577",
    "terminal_title": "#1f1814",
    "dialog_title": "#1f1814",
    "dialog_dropdown_hover": "#f7f0e8",
    "dialog_dropdown_pressed": "#efe4d7",
    "menu_bg": "#fffdf9",
    "menu_fg": "#221a16",
    "menu_selected_bg": "#edd6c7",
    "menu_selected_fg": "#251c17",
    "menu_separator": "#eadfce",
    "terminal_output_bg": "#171d22",
    "terminal_output_fg": "#d7e0ea",
    "terminal_output_border": "#273341",
    "terminal_scroll_bg": "rgba(23, 29, 34, 0.24)",
    "terminal_scroll_handle": "rgba(128, 145, 160, 0.58)",
    "scrollbar_handle": "rgba(77, 62, 51, 0.16)",
    "archived_text": "#a13e35",
    "board_drop_fill": "#f5ddcf",
    "board_drop_border": "#c8643b",
    "board_archived_line": "#d7b4a6",
    "splitter_handle_idle": "rgba(77, 62, 51, 0.28)",
    "splitter_handle_hover": "rgba(77, 62, 51, 0.45)",
    "stop_requested": "#a13e35",
}
UI_DARK_THEME: Dict[str, str] = {
    **UI_LIGHT_THEME,
    "main_window_bg": "#171b20",
    "main_window_fg": "#e8ebef",
    "shell_bg": "#20262d",
    "shell_border": "#343e48",
    "board_divider": "rgba(195, 205, 214, 0.10)",
    "sidebar_bg": "#1c2228",
    "sidebar_border": "#323b44",
    "header_divider": "rgba(195, 205, 214, 0.14)",
    "headline": "#f3f5f7",
    "status_bg": "#3a2a23",
    "status_fg": "#f0b596",
    "top_field_label": "#9aa6b1",
    "field_label": "#93a0ab",
    "checkbox_text": "#dde4ea",
    "checkbox_bg": "#252c34",
    "checkbox_border": "#41505f",
    "checkbox_checked_bg": "#d07a4e",
    "checkbox_checked_border": "#b7653f",
    "input_bg": "#252c34",
    "input_fg": "#e7edf3",
    "input_border": "#41505f",
    "input_focus": "#d07a4e",
    "input_disabled_bg": "#20262d",
    "input_disabled_fg": "#74808c",
    "button_bg": "#2c343d",
    "button_fg": "#e8edf2",
    "button_border": "#465362",
    "button_hover": "#36404a",
    "primary_bg": "#d07a4e",
    "primary_fg": "#161a1f",
    "primary_border": "#b7653f",
    "primary_hover": "#df8a5e",
    "plan_bg": "#24384c",
    "plan_fg": "#cee4f9",
    "plan_border": "#34516d",
    "plan_hover": "#2b4560",
    "run_bg": "#243f2e",
    "run_fg": "#d5f0da",
    "run_border": "#335842",
    "run_hover": "#2b4d38",
    "loop_bg": "#2d7a49",
    "loop_fg": "#f4fff7",
    "loop_border": "#215a36",
    "loop_hover": "#359056",
    "stop_bg": "#7f2f31",
    "stop_fg": "#fff3f3",
    "stop_border": "#5f2123",
    "stop_hover": "#984042",
    "terminal_button_bg": "#0f1317",
    "terminal_button_fg": "#f3f6fa",
    "terminal_button_border": "#06080a",
    "terminal_button_hover": "#1b2229",
    "theme_toggle_bg": "#2b333c",
    "theme_toggle_fg": "#cfd7df",
    "theme_toggle_border": "#46515c",
    "theme_toggle_hover": "#333d48",
    "theme_toggle_checked_bg": "#d07a4e",
    "theme_toggle_checked_fg": "#15191d",
    "theme_toggle_checked_border": "#b7653f",
    "small_action_bg": "#4b352b",
    "small_action_fg": "#f2c0a5",
    "small_action_border": "#6c4a3c",
    "muted_text": "#98a4af",
    "sidebar_title": "#e6ebf0",
    "board_list_bg": "#20262d",
    "board_list_fg": "#e4eaf0",
    "board_list_border": "#333d47",
    "board_list_selected_bg": "#33404c",
    "board_list_selected_fg": "#f3f7fa",
    "board_list_hover_bg": "#29313a",
    "board_title": "#f2f5f7",
    "columns_bg": "#181d22",
    "stage_scrollbar_bg": "rgba(24, 29, 34, 0.88)",
    "stage_scrollbar_border": "rgba(201, 212, 223, 0.16)",
    "stage_scrollbar_handle": "rgba(186, 198, 210, 0.30)",
    "stage_scrollbar_handle_hover": "rgba(208, 220, 232, 0.46)",
    "phase_column_bg": "#222930",
    "phase_column_border": "#39444f",
    "phase_drag_bg": "#2a3139",
    "phase_title": "#eef2f5",
    "phase_count_bg": "#303943",
    "phase_count_fg": "#b9c4ce",
    "task_card_bg": "#262d35",
    "task_card_border": "#3d4752",
    "task_card_drag_bg": "#322d28",
    "task_card_hover_bg": "#2d3640",
    "task_card_hover_border": "#536171",
    "task_card_focus_bg": "#313b46",
    "task_card_focus_border": "#d79a75",
    "success_bg": "#203427",
    "success_fg": "#bfe4c9",
    "success_border": "#31513a",
    "success_hover_bg": "#27402f",
    "success_hover_border": "#3b6347",
    "reject_bg": "#3c2626",
    "reject_fg": "#f0b2aa",
    "reject_border": "#603535",
    "reject_hover_bg": "#492e2e",
    "reject_hover_border": "#764646",
    "needs_testing_bg": "#433621",
    "needs_testing_fg": "#f2d196",
    "needs_testing_border": "#685335",
    "needs_testing_hover_bg": "#534228",
    "needs_testing_hover_border": "#7f643d",
    "board_badge_bg": "#413027",
    "board_badge_fg": "#f0bc9d",
    "task_title": "#f2f5f7",
    "task_context": "#b5bfc8",
    "task_meta": "#8f9ca7",
    "task_error": "#f0a7a0",
    "column_empty": "#7f8b95",
    "terminal_title": "#eef2f5",
    "dialog_title": "#eef2f5",
    "dialog_dropdown_hover": "#2a333c",
    "dialog_dropdown_pressed": "#343d47",
    "menu_bg": "#252c34",
    "menu_fg": "#e6ebf0",
    "menu_selected_bg": "#33404c",
    "menu_selected_fg": "#f3f7fa",
    "menu_separator": "#3d4752",
    "terminal_output_bg": "#0f1419",
    "terminal_output_fg": "#dbe4ee",
    "terminal_output_border": "#23303c",
    "terminal_scroll_bg": "rgba(15, 20, 25, 0.34)",
    "terminal_scroll_handle": "rgba(132, 148, 164, 0.68)",
    "scrollbar_handle": "rgba(193, 205, 216, 0.20)",
    "archived_text": "#f0a7a0",
    "board_drop_fill": "#564038",
    "board_drop_border": "#d07a4e",
    "board_archived_line": "#705247",
    "splitter_handle_idle": "rgba(218, 228, 240, 0.45)",
    "splitter_handle_hover": "rgba(236, 242, 250, 0.72)",
    "stop_requested": "#f0a7a0",
}
UI_THEMES = {
    "light": UI_LIGHT_THEME,
    "dark": UI_DARK_THEME,
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


def _now_timestamp_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _normalize_theme_name(theme_name: str) -> str:
    normalized = str(theme_name or "").strip().lower()
    if normalized in UI_THEME_NAMES:
        return normalized
    return "light"


def _theme_values(theme_name: str) -> Dict[str, str]:
    return UI_THEMES[_normalize_theme_name(theme_name)]


def _approval_response_path(control_dir: str | Path, request_id: str) -> Path:
    return Path(control_dir) / "approval-response-{0}.json".format(request_id)


def _pending_approval_request(runtime_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    approval_request = runtime_payload.get("approval_request")
    if not isinstance(approval_request, dict):
        return None
    request_id = str(approval_request.get("id", "")).strip()
    if not request_id:
        return None
    if str(approval_request.get("status", "pending")).strip().lower() != "pending":
        return None
    return approval_request


def _write_approval_response(control_dir: str | Path, request_id: str, approved: bool, source: str) -> Path:
    response_path = _approval_response_path(control_dir, request_id)
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "approved": bool(approved),
                "source": str(source).strip() or "ui",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return response_path


def _terminal_text_should_refresh(
    last_terminal_text: Optional[str],
    current_terminal_text: str,
) -> bool:
    return last_terminal_text is None or current_terminal_text != last_terminal_text


def _capture_modeless_dialog_value(dialog: Any, value_reader: Callable[[], Any]) -> Callable[[], Any]:
    captured = {"ready": False, "value": None}
    accepted_signal = getattr(dialog, "accepted", None)
    connect = getattr(accepted_signal, "connect", None)
    if callable(connect):
        def capture() -> None:
            captured["value"] = value_reader()
            captured["ready"] = True

        connect(capture)

    def read_value() -> Any:
        if captured["ready"]:
            return captured["value"]
        return value_reader()

    return read_value


def _sync_dialog_board_titles(dialogs: List[Any], old_title: str, new_title: str) -> None:
    cleaned_old_title = str(old_title).strip()
    cleaned_new_title = str(new_title).strip()
    if not cleaned_old_title or not cleaned_new_title or cleaned_old_title == cleaned_new_title:
        return
    for dialog in list(dialogs):
        rename_option = getattr(dialog, "rename_board_option", None)
        if callable(rename_option):
            rename_option(cleaned_old_title, cleaned_new_title)


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


def _repo_run_command_parts(config: Dict[str, Any]) -> List[str]:
    command = config.get("ui", {}).get("repo_run_command", [])
    if not isinstance(command, list):
        return []
    return [str(part).strip() for part in command if str(part).strip()]


def _repo_run_command_text(config: Dict[str, Any]) -> str:
    parts = _repo_run_command_parts(config)
    return shlex.join(parts) if parts else ""


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


def _parse_repo_run_command(raw_text: str) -> List[str]:
    line = raw_text.strip()
    if not line:
        return []
    try:
        return [part for part in shlex.split(line) if str(part).strip()]
    except ValueError as exc:
        raise ValueError("Invalid repo run command: {0}".format(exc)) from exc


def _launch_terminal(repo_root: Path) -> tuple[bool, str]:
    resolved_repo = repo_root.resolve()
    command: Optional[List[str]] = None

    if sys.platform == "darwin":
        command = ["open", "-a", "Terminal", str(resolved_repo)]
    elif sys.platform.startswith("win"):
        command = ["cmd.exe", "/K", 'cd /d "{0}"'.format(str(resolved_repo))]
    else:
        for candidate in (
            "xdg-terminal-exec",
            "x-terminal-emulator",
            "gnome-terminal",
            "konsole",
            "xfce4-terminal",
            "kitty",
            "alacritty",
            "wezterm",
            "xterm",
        ):
            executable = shutil.which(candidate)
            if executable:
                command = [executable]
                break

    if not command:
        return (False, "No supported terminal launcher was found on this platform.")

    try:
        subprocess.Popen(
            command,
            cwd=resolved_repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return (False, str(exc))
    return (True, "")


def _launch_terminal_command(repo_root: Path, shell_command: str) -> tuple[bool, str]:
    resolved_repo = repo_root.resolve()
    command: Optional[List[str]] = None
    cleaned_command = str(shell_command).strip()
    if not cleaned_command:
        return (False, "No terminal command was provided.")

    if sys.platform == "darwin":
        script = 'tell application "Terminal"\nactivate\ndo script {0}\nend tell'.format(
            json.dumps(cleaned_command)
        )
        command = ["osascript", "-e", script]
    elif sys.platform.startswith("win"):
        command = ["cmd.exe", "/K", cleaned_command]
    else:
        shell_parts = ["bash", "-lc", cleaned_command]
        terminal_specs = (
            ("gnome-terminal", ["--", *shell_parts]),
            ("konsole", ["-e", *shell_parts]),
            ("xfce4-terminal", ["--command", shlex.join(shell_parts)]),
            ("kitty", [*shell_parts]),
            ("alacritty", ["-e", *shell_parts]),
            ("wezterm", ["start", "--cwd", str(resolved_repo), "--", *shell_parts]),
            ("xterm", ["-e", *shell_parts]),
            ("x-terminal-emulator", ["-e", *shell_parts]),
        )
        for executable_name, args in terminal_specs:
            executable = shutil.which(executable_name)
            if executable:
                command = [executable, *args]
                break

    if not command:
        return (False, "No supported terminal launcher was found on this platform.")

    try:
        subprocess.Popen(
            command,
            cwd=resolved_repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return (False, str(exc))
    return (True, "")


def _repo_agents_path(repo_root: Path) -> Path:
    resolved_repo = repo_root.resolve()
    for candidate_name in ("agents.md", "AGENTS.md"):
        candidate = resolved_repo / candidate_name
        if candidate.exists():
            return candidate
    return resolved_repo / "agents.md"


def _configured_reasoning_effort(config: Dict[str, Any], role: str) -> Optional[str]:
    models = config.get("models", {})
    configured_value = models.get("{0}_reasoning_effort".format(role))
    if configured_value is None:
        return None
    cleaned = str(configured_value).strip()
    return cleaned or None


def _ticket_development_schema_path() -> Path:
    return Path(__file__).resolve().parents[1] / "schemas" / "ticket_development.schema.json"


def _build_ticket_development_prompt(
    repo_root: Path,
    board_titles: List[str],
    phase_order: List[str],
    transcript: List[Dict[str, str]],
) -> str:
    cleaned_boards = [str(item).strip() for item in board_titles if str(item).strip()]
    if not cleaned_boards:
        cleaned_boards = ["General"]
    cleaned_phases = [str(item).strip() for item in phase_order if str(item).strip()]
    if not cleaned_phases:
        cleaned_phases = ["backlog", "planning", "ready", "in_progress", "needs_testing", "blocked", "completed"]
    payload = {
        "repo_root": str(repo_root.resolve()),
        "existing_boards": cleaned_boards,
        "workflow_phases": cleaned_phases,
        "conversation": transcript,
    }
    return """You are Taskbot's ticket development agent.

Your job is to turn messy user notes into concrete execution tickets for a repo-local Codex implementation loop.
This is a short ticket-development turn, not an implementation or deep planning run.

Conversation and repo context:
{payload}

Operating rules:
- Prefer asking clarifying questions quickly over doing extended investigation.
- Do not try to resolve every ambiguity from the repo. If expectations are unclear, return questions now.
- Keep repo inspection surgical: at most 3 targeted searches or file reads, only when needed to name likely files or verify existing behaviour.
- Do not run broad scans, tests, builds, package installs, servers, or long commands.
- Be methodical. Extract multiple tickets when the notes contain multiple independent changes.
- Ask targeted follow-up questions when expectations, behaviour, constraints, or acceptance criteria are unclear.
- If a ticket is clear enough to implement, include it in `tickets`.
- If one clear ticket and one unclear ticket are both present, create the clear ticket and ask only about the unclear part.
- Ready tickets must be concrete enough for the execution agent to start without a separate planning pass.
- Complex or broad tickets should use phase `planning`; simple and well-scoped tickets should use phase `ready`.
- Use existing board names when they fit; otherwise propose a concise board title.
- Keep ticket titles imperative and specific.
- Context notes should include relevant user intent, decisions made in the chat, repo observations, constraints, and non-goals.
- Acceptance criteria must be observable and testable.
- File targets should be repo-relative paths only when you have useful confidence.
- For `ready` tickets, include an implementation `plan` matching Taskbot's plan shape. Keep it concise but actionable.
- For `planning` tickets, omit the plan or set it to null.
- Do not edit files and do not modify Taskbot's task store.
- Complete this turn promptly. If more than a small amount of repo inspection seems necessary, ask the user which direction to take.

Return JSON only. Do not wrap it in Markdown.
""".format(payload=json.dumps(payload, indent=2))


def _build_interactive_ticket_development_prompt(
    repo_root: Path,
    board_titles: List[str],
    phase_order: List[str],
    taskbot_entry: Path,
    python_executable: str,
) -> str:
    cleaned_boards = [str(item).strip() for item in board_titles if str(item).strip()]
    if not cleaned_boards:
        cleaned_boards = ["General"]
    cleaned_phases = [str(item).strip() for item in phase_order if str(item).strip()]
    if not cleaned_phases:
        cleaned_phases = ["backlog", "planning", "ready", "in_progress", "needs_testing", "blocked", "completed"]
    add_task_prefix = shlex.join(
        [
            python_executable,
            str(taskbot_entry.resolve()),
            "--repo-root",
            str(repo_root.resolve()),
            "add-task",
        ]
    )
    return """You are Taskbot's interactive ticket development agent.

Help the user turn messy notes into concrete Taskbot cards. This is an interactive terminal session, so ask questions directly whenever expectations, requirements, scope, or acceptance criteria are unclear.

Repository root:
{repo_root}

Existing board names:
{boards}

Workflow phases:
{phases}

Create cards only through this supported CLI command:
{add_task_prefix} --board "Board" --title "Imperative title" --context "Concrete notes" --acceptance "Observable criterion"

Use these creation rules:
- Simple, well-specified tickets should go straight to execution:
  {add_task_prefix} --board "Board" --title "Title" --phase ready --ready-plan --context "..." --file-target path/to/file --acceptance "..."
- Complex, broad, or risky tickets should go to planning:
  {add_task_prefix} --board "Board" --title "Title" --phase planning --context "..." --acceptance "..."
- Repeat --file-target and --acceptance as needed.
- Ask before creating a card if the desired behavior, constraints, or success criteria are ambiguous.
- You may inspect the repo surgically to clarify likely files or existing behavior. Do not implement code in this session.
- Do not hand-edit _taskbot/tasks.yaml.
- After creating cards, summarize what was created and which ones will run directly.

Start by asking the user to paste their disorganized notes.
""".format(
        repo_root=str(repo_root.resolve()),
        boards="\n".join("- {0}".format(item) for item in cleaned_boards),
        phases=", ".join(cleaned_phases),
        add_task_prefix=add_task_prefix,
    )


def _write_interactive_ticket_development_script(
    active_config: Dict[str, Any],
    *,
    board_titles: List[str],
    phase_order: List[str],
    taskbot_entry: Path,
) -> Path:
    repo_root = Path(active_config["repo_root"]).resolve()
    artifact_root = Path(active_config["artifact_dir"]) / "ticket-development"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    artifact_dir = artifact_root / "interactive-{0}".format(stamp)
    suffix = 2
    while artifact_dir.exists():
        artifact_dir = artifact_root / "interactive-{0}-{1}".format(stamp, suffix)
        suffix += 1
    artifact_dir.mkdir(parents=True, exist_ok=True)

    prompt = _build_interactive_ticket_development_prompt(
        repo_root,
        board_titles,
        phase_order,
        taskbot_entry,
        sys.executable,
    )
    prompt_path = artifact_dir / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    codex_config = active_config.get("codex", {})
    model_config = active_config.get("models", {})
    command = [
        "codex",
        "-a",
        str(codex_config.get("ask_for_approval", "on-request")),
    ]
    reasoning_effort = _configured_reasoning_effort(active_config, "planner")
    if reasoning_effort:
        command.extend(["-c", "model_reasoning_effort={0}".format(reasoning_effort)])
    command.extend(
        [
            "-C",
            str(repo_root),
            "-m",
            str(model_config.get("planner", "gpt-5.4")),
            "--sandbox",
            str(codex_config.get("sandbox", "workspace-write")),
            "--color",
            str(codex_config.get("color", "auto")),
        ]
    )
    command_text = shlex.join(command)
    script_path = artifact_dir / "start-ticket-developer.sh"
    script_path.write_text(
        "\n".join(
            [
                "#!/bin/zsh",
                "cd {0}".format(shlex.quote(str(repo_root))),
                "clear",
                "echo 'Taskbot interactive ticket developer'",
                "echo 'Paste your notes after Codex starts. Created cards will appear on the board.'",
                "echo",
                "exec {0} \"$(<{1})\"".format(command_text, shlex.quote(str(prompt_path))),
                "",
            ]
        ),
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    return script_path


def _extract_json_object(text: str) -> Dict[str, Any]:
    stripped = str(text or "").strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
            if match is not None:
                parsed = json.loads(match.group(1))
            else:
                start = stripped.find("{")
                end = stripped.rfind("}")
                if start < 0 or end <= start:
                    return {}
                parsed = json.loads(stripped[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _ticket_development_payload_from_text(text: str) -> Dict[str, Any]:
    payload = _extract_json_object(text)
    if not payload:
        return {"message": str(text or "").strip(), "questions": [], "tickets": []}
    questions = payload.get("questions", [])
    tickets = payload.get("tickets", [])
    return {
        "message": str(payload.get("message", "")).strip(),
        "questions": [str(item).strip() for item in questions if str(item).strip()] if isinstance(questions, list) else [],
        "tickets": [item for item in tickets if isinstance(item, dict)] if isinstance(tickets, list) else [],
    }


def _normalise_ticket_plan(ticket: Dict[str, Any]) -> Dict[str, Any]:
    raw_plan = ticket.get("plan")
    if isinstance(raw_plan, dict):
        relevant_files = [
            str(item).strip()
            for item in raw_plan.get("relevant_files", ticket.get("file_targets", []))
            if str(item).strip()
        ]
        raw_steps = raw_plan.get("steps", [])
        steps: List[Dict[str, Any]] = []
        if isinstance(raw_steps, list):
            for item in raw_steps:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "")).strip() or "Implement the ticket"
                details = str(item.get("details", "")).strip() or str(ticket.get("context_notes", "")).strip()
                files = [str(path).strip() for path in item.get("files", []) if str(path).strip()]
                steps.append(
                    {
                        "title": title,
                        "details": details or "Make the requested change while preserving existing behaviour.",
                        "files": files,
                        "parallelisable": bool(item.get("parallelisable", False)),
                    }
                )
        if steps:
            return {
                "summary": str(raw_plan.get("summary", "")).strip() or str(ticket.get("title", "")).strip(),
                "constraints": [str(item).strip() for item in raw_plan.get("constraints", []) if str(item).strip()],
                "relevant_files": relevant_files,
                "steps": steps,
                "verification": [str(item).strip() for item in raw_plan.get("verification", []) if str(item).strip()],
                "subagent_splits": [
                    item
                    for item in raw_plan.get("subagent_splits", [])
                    if isinstance(item, dict)
                ],
                "decomposition": {
                    "should_split": False,
                    "reason": "",
                    "subtasks": [],
                },
            }

    file_targets = [str(item).strip() for item in ticket.get("file_targets", []) if str(item).strip()]
    acceptance = [str(item).strip() for item in ticket.get("acceptance", []) if str(item).strip()]
    details = str(ticket.get("context_notes", "")).strip() or str(ticket.get("title", "")).strip()
    return {
        "summary": str(ticket.get("title", "")).strip(),
        "constraints": [],
        "relevant_files": file_targets,
        "steps": [
            {
                "title": "Implement the ticket",
                "details": details or "Make the requested change while preserving existing behaviour.",
                "files": file_targets,
                "parallelisable": False,
            }
        ],
        "verification": acceptance,
        "subagent_splits": [],
        "decomposition": {
            "should_split": False,
            "reason": "",
            "subtasks": [],
        },
    }


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

    command_enter_submitter = getattr(candidate_widget, "_trigger_command_enter_submit", None)
    if callable(command_enter_submitter):
        return bool(command_enter_submitter())

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


def _read_json_file(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _pretty_output_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, indent=2, sort_keys=True)
    except TypeError:
        return str(payload)


def _agent_output_kind_title(kind: str) -> str:
    if kind == "plan":
        return "Plan"
    if kind == "implementation":
        return "Implementation"
    return str(kind).replace("_", " ").title()


def _agent_output_label(entry: Dict[str, Any]) -> str:
    phase = PHASE_TITLES.get(str(entry.get("phase", "")).strip(), str(entry.get("phase", "")).replace("_", " ").title())
    kind = _agent_output_kind_title(str(entry.get("kind", "")).strip())
    created_at = str(entry.get("created_at", "")).strip()
    summary = str(entry.get("summary", "")).strip()
    label = "{0} {1}".format(phase, kind).strip()
    if created_at:
        label = "{0} | {1}".format(label, created_at.replace("T", " "))
    if summary:
        label = "{0} | {1}".format(label, summary)
    return label


def _task_agent_output_entries(task: StoredTask) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    saved_pairs = set()
    fallback_pairs = set()
    seen_output_ids = set()

    def append_entry(entry: Dict[str, Any], *, legacy: bool = False) -> None:
        phase = str(entry.get("phase", "")).strip()
        kind = str(entry.get("kind", "")).strip()
        if not phase or not kind:
            return
        output_id = str(entry.get("output_id", "")).strip() or "{0}-{1}".format(phase, kind)
        if output_id in seen_output_ids:
            return
        seen_output_ids.add(output_id)
        payload = entry.get("payload")
        normalised = {
            "output_id": output_id,
            "phase": phase,
            "kind": kind,
            "created_at": str(entry.get("created_at", "")).strip(),
            "summary": str(entry.get("summary", "")).strip(),
            "payload": payload,
            "label": _agent_output_label(entry) + (" (Legacy)" if legacy else ""),
        }
        entries.append(normalised)

    for entry in task.agent_outputs:
        if isinstance(entry, dict):
            append_entry(entry)
            saved_pairs.add((str(entry.get("phase", "")).strip(), str(entry.get("kind", "")).strip()))

    if isinstance(task.plan, dict) and task.plan and ("planning", "plan") not in saved_pairs:
        append_entry(
            {
                "output_id": "legacy-plan",
                "phase": "planning",
                "kind": "plan",
                "created_at": task.updated_at or task.created_at,
                "summary": str(task.plan.get("summary", "")),
                "payload": task.plan,
            },
            legacy=True,
        )
        fallback_pairs.add(("planning", "plan"))

    artifact_dir_text = str(task.artifact_dir).strip()
    if artifact_dir_text:
        artifact_dir = Path(artifact_dir_text)
        legacy_files = [
            ("plan.result.json", "planning", "plan"),
            ("implement.result.json", "in_progress", "implementation"),
        ]
        for filename, phase, kind in legacy_files:
            if (phase, kind) in saved_pairs or (phase, kind) in fallback_pairs:
                continue
            payload = _read_json_file(artifact_dir / filename)
            if payload is None:
                continue
            summary = ""
            if isinstance(payload, dict):
                summary = str(payload.get("summary", ""))
            append_entry(
                {
                    "output_id": "legacy-{0}".format(kind),
                    "phase": phase,
                    "kind": kind,
                    "created_at": task.updated_at or task.created_at,
                    "summary": summary,
                    "payload": payload,
                },
                legacy=True,
            )
            fallback_pairs.add((phase, kind))

    entries.sort(key=lambda item: (str(item.get("created_at", "")), str(item.get("output_id", ""))))
    return entries


def _phase_label(phase: str) -> str:
    return PHASE_TITLES.get(phase, phase.replace("_", " ").title())


def _board_summary_text(
    tasks: List[StoredTask],
    phase_order: List[str],
    *,
    board_count: Optional[int] = None,
) -> str:
    parts: List[str] = []
    if board_count is not None:
        parts.append("{0} boards".format(board_count))

    phase_counts = {phase: 0 for phase in phase_order}
    for task in tasks:
        if task.phase in phase_counts:
            phase_counts[task.phase] += 1

    for phase in phase_order:
        parts.append("{0} {1}".format(_phase_label(phase), phase_counts[phase]))
    return " | ".join(parts)


def _board_header_title(title: str, task_count: int) -> str:
    return "{0} ({1} tasks)".format(title, max(0, int(task_count)))


def _task_card_can_start_task(phase: str) -> bool:
    return phase in {"backlog", "planning"}


def _task_move_targets(current_phase: str, phase_order: List[str]) -> List[str]:
    return [phase for phase in phase_order if phase != current_phase]


def _task_board_recency_sort_key(task: StoredTask) -> tuple[str, str, int, str]:
    updated_at = str(task.updated_at or "").strip()
    created_at = str(task.created_at or "").strip()
    return (updated_at or created_at, created_at, int(task.order), task.task_id)


def _sort_board_phase_tasks(tasks: List[StoredTask]) -> List[StoredTask]:
    return sorted(tasks, key=_task_board_recency_sort_key, reverse=True)


def _create_form_dropdown_class(*,
                                Qt: Any,
                                QAction: Any,
                                QMenu: Any,
                                QSizePolicy: Any,
                                QToolButton: Any,
                                Signal: Any) -> Any:
    class _FormDropdown(QToolButton):
        currentIndexChanged = Signal(int)

        def __init__(self, parent: Any = None) -> None:
            super().__init__(parent)
            self.setObjectName("DialogDropdown")
            self.setPopupMode(QToolButton.InstantPopup)
            self.setToolButtonStyle(Qt.ToolButtonTextOnly)
            self.setFocusPolicy(Qt.StrongFocus)
            self.setCursor(Qt.PointingHandCursor)
            self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self.setMinimumHeight(32)
            self.setMinimumWidth(0)
            self._menu = QMenu(self)
            self._menu.setObjectName("DialogDropdownMenu")
            self.setMenu(self._menu)
            self._entries: List[Dict[str, Any]] = []
            self._current_index = -1
            self._use_foreground_popup = False

        def _sync_display(self) -> None:
            current_text = self.currentText()
            self.setToolTip(current_text)
            available_width = self.contentsRect().width() - 30
            if available_width <= 0:
                self.setText(current_text)
                return
            elided = self.fontMetrics().elidedText(current_text, Qt.ElideRight, available_width)
            self.setText(elided)

        def resizeEvent(self, event: Any) -> None:
            super().resizeEvent(event)
            self._sync_display()

        def _select_index(self, index: int) -> None:
            previous_index = self._current_index
            if index < 0 or index >= len(self._entries):
                self._current_index = -1
                self._sync_display()
            else:
                self._current_index = index
                self._sync_display()
            if self._current_index != previous_index:
                self.currentIndexChanged.emit(self._current_index)

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

        def addCustomAction(self, text: str, callback: Any) -> Any:
            if self._entries and self._menu.actions():
                self._menu.addSeparator()
            action = self._menu.addAction(text)
            action.triggered.connect(lambda _checked=False: callback())
            return action

        def clear(self) -> None:
            self._menu.clear()
            self._entries.clear()
            self._select_index(-1)

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

        def replaceItem(self, index: int, text: str, user_data: Any = None) -> None:
            if index < 0 or index >= len(self._entries):
                return
            label = str(text)
            data = label if user_data is None else user_data
            entry = self._entries[index]
            entry["text"] = label
            entry["data"] = data
            action = entry["action"]
            action.setText(label)
            action.setData(data)
            if self._current_index == index:
                self._sync_display()

        def removeItem(self, index: int) -> None:
            if index < 0 or index >= len(self._entries):
                return
            entry = self._entries.pop(index)
            action = entry["action"]
            self._menu.removeAction(action)
            action.deleteLater()
            if self._current_index == index:
                if self._entries:
                    self._select_index(min(index, len(self._entries) - 1))
                else:
                    self._select_index(-1)
                return
            if self._current_index > index:
                self._current_index -= 1
                self._sync_display()

        def setForegroundPopup(self, enabled: bool) -> None:
            self._use_foreground_popup = bool(enabled)

        def showMenu(self) -> None:
            if not self._use_foreground_popup:
                super().showMenu()
                return
            menu = self.menu()
            if menu is None:
                return
            menu.setMinimumWidth(max(self.width(), menu.sizeHint().width()))
            popup_position = self.mapToGlobal(self.rect().bottomLeft())
            menu.popup(popup_position)
            menu.raise_()

    return _FormDropdown


def _start_task_run_args(task_id: str) -> List[str]:
    return ["run", "--task-id", task_id]


def _taskbot_title_html() -> str:
    letters = [
        ("T", "#c86b2f"),
        ("A", "#c86b2f"),
        ("S", "#c86b2f"),
        ("K", "#c86b2f"),
        ("B", "#5f8f3a"),
        ("O", "#5f8f3a"),
        ("T", "#5f8f3a"),
    ]
    return "".join(
        '<span style="color:{0};">{1}</span>'.format(color, html.escape(letter))
        for letter, color in letters
    )


def _wrapped_plain_text_height(label: Any, width: int) -> int:
    try:
        from PySide6.QtCore import QRect, Qt
    except ModuleNotFoundError:
        return 0

    margins = label.contentsMargins()
    available_width = max(1, width - margins.left() - margins.right())
    text_rect = label.fontMetrics().boundingRect(
        QRect(0, 0, available_width, 0),
        int(Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop),
        label.text(),
    )
    return text_rect.height() + margins.top() + margins.bottom()


def _sync_task_card_footer_heights(
    footer: Any,
    badge: Any,
    meta: Any,
    spacing: int,
    actions: Any | None = None,
) -> tuple[int, int]:
    margins = footer.contentsMargins()
    content_width = footer.contentsRect().width()
    if content_width <= 0:
        content_width = max(1, footer.width() - margins.left() - margins.right())

    badge_width = badge.width() if badge.width() > 0 else badge.sizeHint().width()
    badge_height = badge.height() if badge.height() > 0 else badge.sizeHint().height()
    actions_width = 0
    actions_height = 0
    if actions is not None and actions.isVisible():
        actions_width = actions.width() if actions.width() > 0 else actions.sizeHint().width()
        actions_height = actions.height() if actions.height() > 0 else actions.sizeHint().height()
    meta_width = max(1, content_width - badge_width - spacing - actions_width - (spacing if actions_width else 0))
    meta_height = _wrapped_plain_text_height(meta, meta_width)
    footer_height = max(badge_height, meta_height, actions_height) + margins.top() + margins.bottom()

    height_changed = False
    if meta.minimumHeight() != meta_height:
        meta.setMinimumHeight(meta_height)
        height_changed = True
    if footer.minimumHeight() != footer_height:
        footer.setMinimumHeight(footer_height)
        height_changed = True

    if height_changed:
        footer.updateGeometry()
        footer_layout = footer.layout()
        if footer_layout is not None:
            footer_layout.invalidate()
        parent = footer.parentWidget()
        if parent is not None:
            parent.updateGeometry()
            parent_layout = parent.layout()
            if parent_layout is not None:
                parent_layout.invalidate()

    return footer_height, meta_height


def launch_ui(config: Dict[str, Any]) -> int:
    preflight_error = _ui_launch_preflight_error()
    if preflight_error is not None:
        print(preflight_error, file=sys.stderr)
        return 1

    try:
        from PySide6.QtCore import QEvent, QObject, QSize, QByteArray, QProcess, QTimer, Qt, QMimeData, QRect, QRectF, Signal
        from PySide6.QtGui import QAction, QDrag, QFont, QFontDatabase, QIcon, QKeySequence, QShortcut, QTextCursor, QColor, QPainter, QPalette, QTextOption
        from PySide6.QtSvg import QSvgRenderer
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

    _theme_rgba_css = re.compile(
        r"^rgba\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*([0-9]+(?:\.[0-9]+)?)\s*\)\s*$",
        re.IGNORECASE,
    )

    def _theme_qcolor(css: str) -> QColor:
        """Resolve theme colors for QPainter. QColor rejects CSS rgba(...); invalid colors paint black."""
        normalized = str(css or "").strip()
        rgba_match = _theme_rgba_css.match(normalized)
        if rgba_match is not None:
            r = max(0, min(255, int(rgba_match.group(1))))
            g = max(0, min(255, int(rgba_match.group(2))))
            b = max(0, min(255, int(rgba_match.group(3))))
            a_src = rgba_match.group(4)
            if "." in a_src:
                alpha_f = max(0.0, min(1.0, float(a_src)))
                a = int(round(alpha_f * 255))
            else:
                a = max(0, min(255, int(a_src)))
            return QColor(r, g, b, a)
        return QColor(normalized)

    refresh_ms = max(250, int(float(config.get("ui", {}).get("refresh_seconds", 1.0)) * 1000))
    tail_lines = int(config.get("ui", {}).get("terminal_tail_lines", 250))
    app_root = Path(config.get("app_root", Path(__file__).resolve().parents[1])).resolve()
    taskbot_entry = app_root / "taskbot.py"
    ui_state_path = app_root / "state" / "ui_session.json"

    def _active_theme(widget: Any) -> Dict[str, str]:
        window = widget.window() if widget is not None else None
        theme_name = getattr(window, "current_theme", "light") if window is not None else "light"
        return _theme_values(theme_name)
    _FormDropdown = _create_form_dropdown_class(
        Qt=Qt,
        QAction=QAction,
        QMenu=QMenu,
        QSizePolicy=QSizePolicy,
        QToolButton=QToolButton,
        Signal=Signal,
    )

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
            "theme": _normalize_theme_name(str(payload.get("theme", "")).strip()),
        }

    def _save_session(repo_root: Path, selected_board_id: Optional[str], theme_name: str) -> None:
        ui_state_path.parent.mkdir(parents=True, exist_ok=True)
        ui_state_path.write_text(
            json.dumps(
                {
                    "repo_root": str(repo_root.resolve()),
                    "selected_board_id": selected_board_id or "",
                    "theme": _normalize_theme_name(theme_name),
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
            theme = _active_theme(self)
            hovered = self.underMouse()
            color = _theme_qcolor(theme["splitter_handle_hover" if hovered else "splitter_handle_idle"])
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(line_rect, 1, 1)

    class _CenteredLineSplitter(QSplitter):
        def createHandle(self) -> QSplitterHandle:
            return _CenteredSplitterHandle(self.orientation(), self)

    _theme_toggle_icons_dir = Path(__file__).resolve().parents[1] / "resources"
    _theme_toggle_light_svg_raw = (_theme_toggle_icons_dir / "light_mode.svg").read_text(encoding="utf-8")
    _theme_toggle_dark_svg_raw = (_theme_toggle_icons_dir / "dark_mode.svg").read_text(encoding="utf-8")

    class _ThemeToggleButton(QPushButton):
        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__("", parent)
            self.setCursor(Qt.PointingHandCursor)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        def sizeHint(self) -> QSize:
            return QSize(56, 24)

        def _draw_toggle_svg(self, painter: QPainter, rect: QRect, svg_markup: str, tint: QColor) -> None:
            if not rect.isValid():
                return
            fill_hex = QColor(tint).name(QColor.NameFormat.HexRgb)
            tinted_markup = re.sub(r'fill="#[^"]*"', f'fill="{fill_hex}"', svg_markup, count=1)
            renderer = QSvgRenderer(QByteArray(tinted_markup.encode("utf-8")))
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            renderer.render(painter, QRectF(rect))
            painter.restore()

        def paintEvent(self, event) -> None:
            del event
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            theme = _active_theme(self)
            checked = self.isChecked()
            hovered = self.underMouse()
            track_rect = self.rect().adjusted(0, 0, -1, -1)
            track_radius = track_rect.height() / 2.0
            track_fill = QColor(
                theme["theme_toggle_checked_bg" if checked else "theme_toggle_hover" if hovered else "theme_toggle_bg"]
            )
            track_border = QColor(
                theme["theme_toggle_checked_border" if checked else "theme_toggle_border"]
            )
            thumb_fill = QColor(theme["shell_bg" if checked else "input_bg"])
            thumb_border = QColor(theme["theme_toggle_checked_border" if checked else "theme_toggle_border"])
            active_icon = QColor(theme["main_window_fg" if checked else "primary_bg"])
            inactive_icon = QColor(theme["theme_toggle_checked_fg" if checked else "theme_toggle_fg"])
            inactive_icon.setAlpha(155)

            painter.setPen(track_border)
            painter.setBrush(track_fill)
            painter.drawRoundedRect(track_rect, track_radius, track_radius)

            thumb_size = track_rect.height() - 6
            thumb_y = track_rect.y() + 3
            thumb_x = track_rect.right() - thumb_size - 3 if checked else track_rect.x() + 3
            thumb_rect = QRect(thumb_x, thumb_y, thumb_size, thumb_size)
            painter.setPen(thumb_border)
            painter.setBrush(thumb_fill)
            painter.drawEllipse(thumb_rect)

            icon_size = max(10, thumb_size - 6)
            tc = thumb_rect.center()
            thumb_icon_rect = QRect(tc.x() - icon_size // 2, tc.y() - icon_size // 2, icon_size, icon_size)
            left_icon_rect = QRect(track_rect.x() + 6, track_rect.y() + 3, icon_size, icon_size)
            right_icon_rect = QRect(track_rect.right() - icon_size - 6, track_rect.y() + 3, icon_size, icon_size)
            self._draw_toggle_svg(
                painter,
                thumb_icon_rect if not checked else left_icon_rect,
                _theme_toggle_light_svg_raw,
                active_icon if not checked else inactive_icon,
            )
            self._draw_toggle_svg(
                painter,
                thumb_icon_rect if checked else right_icon_rect,
                _theme_toggle_dark_svg_raw,
                active_icon if checked else inactive_icon,
            )

            if self.hasFocus():
                focus_pen = QColor(theme["input_focus"])
                focus_pen.setAlpha(180)
                painter.setBrush(Qt.NoBrush)
                painter.setPen(focus_pen)
                painter.drawRoundedRect(track_rect.adjusted(1, 1, -1, -1), track_radius - 1, track_radius - 1)

    class _WrappingPlainTextLabel(QLabel):
        def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
            super().__init__(text, parent)
            self.setTextFormat(Qt.PlainText)
            self.setWordWrap(True)
            self.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        def hasHeightForWidth(self) -> bool:
            return True

        def heightForWidth(self, width: int) -> int:
            return _wrapped_plain_text_height(self, width)

        def sizeHint(self) -> QSize:
            hint = super().sizeHint()
            width = self.width() if self.width() > 0 else hint.width()
            if width > 0:
                hint.setHeight(max(hint.height(), self.heightForWidth(width)))
            return hint

        def resizeEvent(self, event) -> None:
            width_changed = event.size().width() != event.oldSize().width()
            super().resizeEvent(event)
            if width_changed:
                self.updateGeometry()

    class _TaskCardFooter(QWidget):
        def __init__(
            self,
            board_title: str,
            meta_text: str,
            *,
            actions_widget: QWidget | None = None,
            parent: QWidget | None = None,
        ) -> None:
            super().__init__(parent)
            self._spacing = 8
            self._actions = actions_widget

            layout = QHBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(self._spacing)
            layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)

            self._board_badge = QLabel(board_title)
            self._board_badge.setObjectName("BoardBadge")
            self._board_badge.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self._board_badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            layout.addWidget(self._board_badge, 0, Qt.AlignLeft | Qt.AlignTop)

            self._meta = _WrappingPlainTextLabel(meta_text)
            self._meta.setObjectName("TaskMeta")
            self._meta.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self._meta.setMinimumWidth(0)
            self._meta.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            layout.addWidget(self._meta, 1, Qt.AlignLeft | Qt.AlignTop)

            if self._actions is not None:
                self._actions.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                layout.addWidget(self._actions, 0, Qt.AlignRight | Qt.AlignTop)

            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        def _sync_heights(self) -> None:
            _sync_task_card_footer_heights(
                self,
                self._board_badge,
                self._meta,
                self._spacing,
                self._actions,
            )

        def hasHeightForWidth(self) -> bool:
            return True

        def heightForWidth(self, width: int) -> int:
            margins = self.contentsMargins()
            available_width = max(1, width - margins.left() - margins.right())
            badge_hint = self._board_badge.sizeHint()
            actions_width = 0
            actions_height = 0
            if self._actions is not None and self._actions.isVisible():
                actions_hint = self._actions.sizeHint()
                actions_width = actions_hint.width()
                actions_height = actions_hint.height()
            meta_width = max(
                1,
                available_width - badge_hint.width() - self._spacing - actions_width - (self._spacing if actions_width else 0),
            )
            meta_height = _wrapped_plain_text_height(self._meta, meta_width)
            return max(badge_hint.height(), meta_height, actions_height) + margins.top() + margins.bottom()

        def sizeHint(self) -> QSize:
            hint = super().sizeHint()
            width = self.width() if self.width() > 0 else hint.width()
            if width > 0:
                hint.setHeight(max(hint.height(), self.heightForWidth(width)))
            return hint

        def showEvent(self, event) -> None:
            super().showEvent(event)
            self._sync_heights()

        def resizeEvent(self, event) -> None:
            width_changed = event.size().width() != event.oldSize().width()
            super().resizeEvent(event)
            if width_changed:
                self._sync_heights()

        def mousePressEvent(self, event) -> None:
            event.ignore()

        def mouseReleaseEvent(self, event) -> None:
            event.ignore()

    def _set_primary_button_default(buttons: QDialogButtonBox, standard_button: Any) -> None:
        primary_button = buttons.button(standard_button)
        if primary_button is not None:
            primary_button.setDefault(True)
            primary_button.setAutoDefault(True)

    class CommandEnterDialog(QDialog):
        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._command_enter_submit_in_progress = False
            self._command_enter_shortcuts = _install_command_enter_shortcuts(
                self,
                lambda: _command_enter_submit_dialog(self, self, QPushButton),
                QShortcut,
                QKeySequence,
                Qt.WindowShortcut,
            )

        def _trigger_command_enter_submit(self) -> bool:
            if self._command_enter_submit_in_progress:
                return True
            self._command_enter_submit_in_progress = True
            try:
                self.accept()
            finally:
                if self.isVisible():
                    self._command_enter_submit_in_progress = False
            return True

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
            self.title_input.returnPressed.connect(self.accept)
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
            self.title_input.returnPressed.connect(self.accept)
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

        def rename_board_option(self, old_title: str, new_title: str) -> None:
            old_index = self.board_dropdown.findText(old_title)
            new_index = self.board_dropdown.findText(new_title)
            current_was_old = self.board_dropdown.currentText().strip() == old_title
            if old_index >= 0 and new_index < 0:
                self.board_dropdown.replaceItem(old_index, new_title, new_title)
            elif old_index >= 0 and new_index >= 0:
                self.board_dropdown.removeItem(old_index)
            elif new_index < 0:
                self.board_dropdown.addItem(new_title, new_title)
            if current_was_old:
                self.board_dropdown.setCurrentText(new_title)

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
            self.setSizeGripEnabled(True)
            self._agent_output_entries = _task_agent_output_entries(task)

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

            form_scroll = QScrollArea()
            form_scroll.setObjectName("SettingsScrollArea")
            form_scroll.setWidgetResizable(True)
            form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            form_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            form_scroll.setFrameShape(QFrame.NoFrame)
            layout.addWidget(form_scroll, 1)

            form_content = QWidget()
            form_content.setObjectName("SettingsContent")
            form_layout = QVBoxLayout(form_content)
            form_layout.setContentsMargins(0, 0, 0, 0)
            form_layout.setSpacing(10)

            board_label = QLabel("Board")
            board_label.setObjectName("FieldLabel")
            form_layout.addWidget(board_label)

            self.board_dropdown = _FormDropdown()
            self.board_dropdown.addItems(available_boards)
            self.board_dropdown.setCurrentText(task.board_title)
            if self._new_board_callback is not None:
                self.board_dropdown.addCustomAction("New board...", self._create_new_board)
            form_layout.addWidget(self.board_dropdown)

            phase_label = QLabel("Phase")
            phase_label.setObjectName("FieldLabel")
            form_layout.addWidget(phase_label)

            self.phase_dropdown = _FormDropdown()
            for phase in phases:
                self.phase_dropdown.addItem(PHASE_TITLES.get(phase, phase), phase)
            self.phase_dropdown.setCurrentData(task.phase)
            form_layout.addWidget(self.phase_dropdown)

            task_label = QLabel("Title")
            task_label.setObjectName("FieldLabel")
            form_layout.addWidget(task_label)

            self.title_input = QLineEdit()
            self.title_input.setPlaceholderText("What needs to be done?")
            self.title_input.setText(task.title)
            self.title_input.returnPressed.connect(self.accept)
            form_layout.addWidget(self.title_input)

            context_label = QLabel("Context")
            context_label.setObjectName("FieldLabel")
            form_layout.addWidget(context_label)

            self.context_input = QPlainTextEdit()
            self.context_input.setPlaceholderText("Optional notes or constraints.")
            self.context_input.setFixedHeight(96)
            self.context_input.setPlainText(task.context_notes)
            form_layout.addWidget(self.context_input)

            outputs_label = QLabel("Agent Output")
            outputs_label.setObjectName("FieldLabel")
            form_layout.addWidget(outputs_label)

            if self._agent_output_entries:
                self.output_selector = _FormDropdown()
                for entry in self._agent_output_entries:
                    self.output_selector.addItem(str(entry.get("label", "")), str(entry.get("output_id", "")))
                self.output_selector.currentIndexChanged.connect(self._refresh_output_viewer)
                form_layout.addWidget(self.output_selector)

                self.output_viewer = QPlainTextEdit()
                self.output_viewer.setReadOnly(True)
                self.output_viewer.setMinimumHeight(160)
                self.output_viewer.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
                self.output_viewer.setLineWrapMode(QPlainTextEdit.WidgetWidth)
                self.output_viewer.setWordWrapMode(QTextOption.WrapAnywhere)
                form_layout.addWidget(self.output_viewer, 1)
                self._refresh_output_viewer()
            else:
                empty_outputs = QLabel("No saved agent output is available for this card yet.")
                empty_outputs.setObjectName("SidebarCaption")
                empty_outputs.setWordWrap(True)
                form_layout.addWidget(empty_outputs)

            form_scroll.setWidget(form_content)

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
                    self.resize(min(720, max_width), min(760, max_height))
                else:
                    self.resize(720, 760)
            else:
                self.resize(720, 760)

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

        def _refresh_output_viewer(self) -> None:
            viewer = getattr(self, "output_viewer", None)
            selector = getattr(self, "output_selector", None)
            if viewer is None or selector is None:
                return
            output_id = str(selector.currentData() or "").strip()
            for entry in self._agent_output_entries:
                if str(entry.get("output_id", "")).strip() != output_id:
                    continue
                viewer.setPlainText(_pretty_output_text(entry.get("payload")))
                return
            viewer.clear()

        def rename_board_option(self, old_title: str, new_title: str) -> None:
            old_index = self.board_dropdown.findText(old_title)
            new_index = self.board_dropdown.findText(new_title)
            current_was_old = self.board_dropdown.currentText().strip() == old_title
            if old_index >= 0 and new_index < 0:
                self.board_dropdown.replaceItem(old_index, new_title, new_title)
            elif old_index >= 0 and new_index >= 0:
                self.board_dropdown.removeItem(old_index)
            elif new_index < 0:
                self.board_dropdown.addItem(new_title, new_title)
            if current_was_old:
                self.board_dropdown.setCurrentText(new_title)
            if self.task.board_title == old_title:
                self.task.board_title = new_title

        def _create_new_board(self) -> None:
            if self._new_board_callback is None:
                return
            created_board_title = self._new_board_callback()
            if not created_board_title:
                return
            if self.board_dropdown.findText(created_board_title) < 0:
                self.board_dropdown.addItem(created_board_title, created_board_title)
            self.board_dropdown.setCurrentText(created_board_title)

    class TestingFeedbackDialog(CommandEnterDialog):
        def __init__(self, task: StoredTask, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setObjectName("AppDialog")
            self.setWindowTitle("Return Task To Backlog")
            self.setModal(True)
            self.resize(520, 280)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(10)

            title = QLabel("What needs fixing?")
            title.setObjectName("DialogTitle")
            layout.addWidget(title)

            caption = QLabel(
                'Add testing feedback for "{0}". The task will move back to Backlog with these notes appended.'.format(
                    task.title
                )
            )
            caption.setObjectName("DialogCaption")
            caption.setWordWrap(True)
            layout.addWidget(caption)

            notes_label = QLabel("Testing feedback")
            notes_label.setObjectName("FieldLabel")
            layout.addWidget(notes_label)

            self.notes_input = QPlainTextEdit()
            self.notes_input.setPlaceholderText("Describe what failed, what is missing, or what should change.")
            self.notes_input.setFixedHeight(132)
            layout.addWidget(self.notes_input)

            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.setObjectName("DialogButtons")
            submit_button = buttons.button(QDialogButtonBox.Ok)
            if submit_button is not None:
                submit_button.setText("Return to Backlog")
            _set_primary_button_default(buttons, QDialogButtonBox.Ok)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addSpacing(8)
            layout.addWidget(buttons)

            self.notes_input.setFocus()

        def accept(self) -> None:
            if not self.feedback_notes():
                QMessageBox.warning(self, "Testing Feedback Required", "Describe what needs fixing before sending the task back.")
                self.notes_input.setFocus()
                return
            super().accept()

        def feedback_notes(self) -> str:
            return self.notes_input.toPlainText().strip()

    class TicketDevelopmentDialog(QDialog):
        def __init__(
            self,
            active_config: Dict[str, Any],
            board_titles: List[str],
            phase_order: List[str],
            on_tickets_created: Any,
            parent: QWidget | None = None,
        ) -> None:
            super().__init__(parent)
            self.active_config = active_config
            self.board_titles = list(board_titles)
            self.phase_order = list(phase_order)
            self._on_tickets_created = on_tickets_created
            self._messages: List[Dict[str, str]] = []
            self._process: QProcess | None = None
            self._stdout_buffer = ""
            self._stderr_text = ""
            self._agent_message_text = ""
            self._last_message_path: Path | None = None
            self._turn_timer = QTimer(self)
            self._turn_timer.setSingleShot(True)
            self._turn_timer.timeout.connect(self._agent_turn_timed_out)
            self._turn_timed_out = False
            self._busy = False

            self.setObjectName("AppDialog")
            self.setWindowTitle("Develop Tickets")
            self.setModal(False)
            self.setSizeGripEnabled(True)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(10)

            title = QLabel("Develop Tickets")
            title.setObjectName("DialogTitle")
            layout.addWidget(title)

            caption = QLabel("Paste notes, answer questions, and Taskbot will create ready or planning cards.")
            caption.setObjectName("DialogCaption")
            caption.setWordWrap(True)
            layout.addWidget(caption)

            self.transcript_view = QTextEdit()
            self.transcript_view.setObjectName("TicketChatTranscript")
            self.transcript_view.setReadOnly(True)
            self.transcript_view.setMinimumHeight(320)
            self.transcript_view.setLineWrapMode(QTextEdit.WidgetWidth)
            layout.addWidget(self.transcript_view, 1)

            self.input_editor = QPlainTextEdit()
            self.input_editor.setPlaceholderText("Paste notes or answer the agent's questions.")
            self.input_editor.setFixedHeight(120)
            layout.addWidget(self.input_editor)

            button_row = QHBoxLayout()
            button_row.setContentsMargins(0, 0, 0, 0)
            button_row.setSpacing(8)

            self.status_label = QLabel("")
            self.status_label.setObjectName("SidebarCaption")
            self.status_label.setWordWrap(False)
            button_row.addWidget(self.status_label, 1)

            self.stop_button = QPushButton("Stop")
            self.stop_button.setEnabled(False)
            self.stop_button.clicked.connect(self._stop_agent)
            button_row.addWidget(self.stop_button)

            self.send_button = QPushButton("Send")
            self.send_button.setObjectName("PrimaryButton")
            self.send_button.clicked.connect(self._send_message)
            button_row.addWidget(self.send_button)

            close_button = QPushButton("Close")
            close_button.clicked.connect(self.reject)
            button_row.addWidget(close_button)
            layout.addLayout(button_row)

            self.resize(780, 720)
            self._append_system("Paste rough notes to begin. The agent can inspect the repo before creating cards.")
            self.input_editor.setFocus()

        def _append_html(self, html_text: str) -> None:
            self.transcript_view.append(html_text)
            scrollbar = self.transcript_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

        def _append_message(self, role: str, text: str) -> None:
            cleaned = str(text or "").strip()
            if not cleaned:
                return
            label = html.escape(role)
            body = html.escape(cleaned).replace("\n", "<br>")
            self._append_html("<p><b>{0}</b><br>{1}</p>".format(label, body))

        def _append_system(self, text: str) -> None:
            cleaned = str(text or "").strip()
            if not cleaned:
                return
            self._append_html(
                '<p><span style="color:#7d8790;">{0}</span></p>'.format(
                    html.escape(cleaned).replace("\n", "<br>")
                )
            )

        def _set_busy(self, busy: bool, status: str = "") -> None:
            self._busy = busy
            self.input_editor.setEnabled(not busy)
            self.send_button.setEnabled(not busy)
            self.stop_button.setEnabled(busy)
            self.status_label.setText(status)

        def _send_message(self) -> None:
            if self._busy:
                return
            message = self.input_editor.toPlainText().strip()
            if not message:
                QMessageBox.warning(self, "Notes Required", "Paste notes or answer the current questions.")
                self.input_editor.setFocus()
                return
            if shutil.which("codex") is None:
                QMessageBox.critical(self, "Codex CLI Not Found", "The `codex` command is not available on PATH.")
                return

            self.input_editor.clear()
            self._messages.append({"role": "user", "content": message})
            self._append_message("You", message)
            self._start_agent_turn()

        def _artifact_dir(self) -> Path:
            root = Path(self.active_config["artifact_dir"]) / "ticket-development"
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            candidate = root / stamp
            suffix = 2
            while candidate.exists():
                candidate = root / "{0}-{1}".format(stamp, suffix)
                suffix += 1
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate

        def _codex_command(self, last_message_path: Path) -> List[str]:
            codex_config = self.active_config.get("codex", {})
            model_config = self.active_config.get("models", {})
            command = [
                "codex",
                "-a",
                str(codex_config.get("ask_for_approval", "never")),
            ]
            reasoning_effort = _configured_reasoning_effort(self.active_config, "planner")
            if reasoning_effort:
                command.extend(["-c", "model_reasoning_effort={0}".format(reasoning_effort)])
            command.extend(
                [
                    "exec",
                    "-",
                    "-C",
                    str(self.active_config["repo_root"]),
                    "-m",
                    str(model_config.get("planner", "gpt-5.4")),
                    "--sandbox",
                    str(codex_config.get("sandbox", "workspace-write")),
                    "--color",
                    str(codex_config.get("color", "never")),
                    "--json",
                    "-o",
                    str(last_message_path),
                ]
            )
            if codex_config.get("ephemeral", True):
                command.append("--ephemeral")
            if codex_config.get("skip_git_repo_check", True):
                command.append("--skip-git-repo-check")
            schema_path = _ticket_development_schema_path()
            if schema_path.exists():
                command.extend(["--output-schema", str(schema_path)])
            return command

        def _start_agent_turn(self) -> None:
            artifact_dir = self._artifact_dir()
            self._last_message_path = artifact_dir / "ticket-development.last_message.json"
            self._stdout_buffer = ""
            self._stderr_text = ""
            self._agent_message_text = ""

            prompt = _build_ticket_development_prompt(
                Path(self.active_config["repo_root"]),
                self.board_titles,
                self.phase_order,
                self._messages,
            )
            (artifact_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

            process = QProcess(self)
            self._process = process
            command = self._codex_command(self._last_message_path)
            process.setProgram(command[0])
            process.setArguments(command[1:])
            process.setWorkingDirectory(str(self.active_config["repo_root"]))
            process.started.connect(lambda current_prompt=prompt: self._write_prompt(current_prompt))
            process.readyReadStandardOutput.connect(self._read_stdout)
            process.readyReadStandardError.connect(self._read_stderr)
            process.finished.connect(self._agent_finished)
            process.errorOccurred.connect(self._agent_process_error)
            self._append_system("Ticket developer started.")
            self._set_busy(True, "Agent working...")
            process.start()
            self._turn_timed_out = False
            self._turn_timer.start(TICKET_DEVELOPMENT_TURN_TIMEOUT_MS)

        def _write_prompt(self, prompt: str) -> None:
            if self._process is None:
                return
            self._process.write(prompt.encode("utf-8"))
            self._process.closeWriteChannel()

        def _read_stdout(self) -> None:
            if self._process is None:
                return
            chunk = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
            self._stdout_buffer += chunk
            while "\n" in self._stdout_buffer:
                line, self._stdout_buffer = self._stdout_buffer.split("\n", 1)
                self._handle_stdout_line(line.strip())

        def _read_stderr(self) -> None:
            if self._process is None:
                return
            self._stderr_text += bytes(self._process.readAllStandardError()).decode("utf-8", errors="replace")

        def _handle_stdout_line(self, line: str) -> None:
            if not line:
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                self._append_system(line)
                return
            if not isinstance(event, dict):
                return
            event_type = str(event.get("type", ""))
            if event_type == "thread.started":
                self._append_system("Agent session started.")
                return
            if event_type == "turn.started":
                self._append_system("Agent is analysing the notes.")
                return

            item = event.get("item", {})
            if not isinstance(item, dict):
                return
            item_type = str(item.get("type", ""))
            if item_type == "command_execution" and event_type == "item.started":
                command = str(item.get("command", "")).strip()
                if command:
                    self._append_system("Inspecting repo: {0}".format(command))
                return
            if item_type == "todo_list" and event_type == "item.updated":
                items = item.get("items", [])
                if isinstance(items, list):
                    remaining = [
                        str(entry.get("text", "")).strip()
                        for entry in items
                        if isinstance(entry, dict) and not entry.get("completed") and str(entry.get("text", "")).strip()
                    ]
                    if remaining:
                        self.status_label.setText(remaining[0])
                return
            if item_type == "agent_message" and event_type == "item.completed":
                self._agent_message_text = str(item.get("text", "")).strip()

        def _agent_process_error(self, _error: Any) -> None:
            if self._process is None:
                return
            self._append_system("Agent process error: {0}".format(self._process.errorString()))
            if self._process.state() == QProcess.NotRunning:
                self._turn_timer.stop()
                self._set_busy(False)
                self._process = None

        def _agent_turn_timed_out(self) -> None:
            if self._process is None:
                return
            if self._process.state() == QProcess.NotRunning:
                return
            self._turn_timed_out = True
            self._append_system(
                "Stopped this ticket-development turn after {0} seconds. Ask a narrower question or paste the most important notes first.".format(
                    TICKET_DEVELOPMENT_TURN_TIMEOUT_MS // 1000
                )
            )
            self._process.terminate()
            QTimer.singleShot(3000, self._kill_timed_out_agent)

        def _kill_timed_out_agent(self) -> None:
            if self._process is None:
                return
            if self._process.state() != QProcess.NotRunning:
                self._process.kill()

        def _agent_finished(self, exit_code: int, _exit_status: Any) -> None:
            self._turn_timer.stop()
            if self._stdout_buffer.strip():
                self._handle_stdout_line(self._stdout_buffer.strip())
            self._stdout_buffer = ""
            self._set_busy(False)
            self._process = None

            if self._turn_timed_out:
                self._turn_timed_out = False
                self.input_editor.setFocus()
                return

            raw_text = self._agent_message_text
            if self._last_message_path is not None and self._last_message_path.exists():
                raw_text = self._last_message_path.read_text(encoding="utf-8")

            if int(exit_code) != 0:
                detail = self._stderr_text.strip() or raw_text.strip() or "No error detail was emitted."
                self._append_system("Ticket developer failed with exit code {0}.\n{1}".format(exit_code, detail))
                return

            payload = _ticket_development_payload_from_text(raw_text)
            self._handle_agent_payload(payload, raw_text)
            self.input_editor.setFocus()

        def _handle_agent_payload(self, payload: Dict[str, Any], raw_text: str) -> None:
            message = str(payload.get("message", "")).strip()
            questions = [str(item).strip() for item in payload.get("questions", []) if str(item).strip()]
            tickets = [item for item in payload.get("tickets", []) if isinstance(item, dict)]

            visible_parts = []
            if message:
                visible_parts.append(message)
            if questions:
                visible_parts.append("\n".join("- {0}".format(question) for question in questions))
            visible_text = "\n\n".join(visible_parts).strip()
            if not visible_text and raw_text.strip():
                visible_text = raw_text.strip()
            if visible_text:
                self._messages.append({"role": "assistant", "content": visible_text})
                self._append_message("Agent", visible_text)

            created_tasks = self._create_tickets(tickets)
            if created_tasks:
                lines = [
                    "{0} [{1}] {2}".format(task.task_id, task.phase, task.title)
                    for task in created_tasks
                ]
                self._append_system("Created cards:\n{0}".format("\n".join(lines)))
                self._on_tickets_created(created_tasks)

        def _create_tickets(self, tickets: List[Dict[str, Any]]) -> List[StoredTask]:
            created_tasks: List[StoredTask] = []
            for ticket in tickets:
                title = str(ticket.get("title", "")).strip()
                if not title:
                    continue
                board_title = str(ticket.get("board_title", "")).strip() or str(
                    self.active_config.get("store", {}).get("default_board", "General")
                )
                phase = str(ticket.get("phase", "")).strip().lower()
                complexity = str(ticket.get("complexity", "")).strip().lower()
                if complexity == "complex" or phase == "planning":
                    target_phase = "planning"
                else:
                    target_phase = "ready"
                if target_phase not in self.phase_order:
                    target_phase = "backlog"
                context_notes = str(ticket.get("context_notes", "")).strip()
                file_targets = [str(item).strip() for item in ticket.get("file_targets", []) if str(item).strip()]
                acceptance = [str(item).strip() for item in ticket.get("acceptance", []) if str(item).strip()]
                task = create_task(
                    self.active_config,
                    board_title=board_title,
                    title=title,
                    context_notes=context_notes,
                    file_targets=file_targets,
                    acceptance=acceptance,
                    phase=target_phase,
                )
                if target_phase == "ready":
                    plan = _normalise_ticket_plan(ticket)
                    task = update_task_fields(
                        self.active_config,
                        task.task_id,
                        plan_status="ready",
                        plan=plan,
                        file_targets=plan.get("relevant_files", file_targets),
                        acceptance=acceptance,
                        last_result_status="planned",
                        last_summary=str(plan.get("summary", title)).strip(),
                        last_error="",
                    ) or task
                created_tasks.append(task)
            return created_tasks

        def _stop_agent(self) -> None:
            if self._process is None:
                return
            self._append_system("Stopping ticket developer.")
            self._process.terminate()

        def reject(self) -> None:
            if self._process is not None:
                self._process.terminate()
            super().reject()

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

            repo_run_command_label = QLabel("Repo Run Command")
            repo_run_command_label.setObjectName("FieldLabel")
            settings_layout.addWidget(repo_run_command_label)

            self.repo_run_command_input = QLineEdit()
            self.repo_run_command_input.setPlaceholderText("Optional command to launch from the repo root")
            self.repo_run_command_input.setText(_repo_run_command_text(active_config))
            settings_layout.addWidget(self.repo_run_command_input)

            repo_run_command_caption = QLabel(
                "Optional repo-local command for the header Run Command button. It runs from the repo root."
            )
            repo_run_command_caption.setObjectName("SidebarCaption")
            repo_run_command_caption.setWordWrap(True)
            settings_layout.addWidget(repo_run_command_caption)

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
            repo_run_command = _parse_repo_run_command(self.repo_run_command_input.text())
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
                "ui": {
                    "repo_run_command": repo_run_command,
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
                     phase_order: List[str],
                     on_start_task: Any = None,
                     on_approve_testing: Any = None,
                     on_reject_testing: Any = None,
                     on_edit: Any,
                     on_delete: Any,
                     on_move_task: Any = None,
                     on_move_task_to_board: Any = None) -> None:
            super().__init__()
            self._task_id = task.task_id
            self._task_board_id = task.board_id
            self._task_phase = task.phase
            self._move_targets = _task_move_targets(task.phase, phase_order)
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
            self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.setToolTip("Click to edit. Drag to move between columns.")

            layout = QVBoxLayout(self)
            layout.setContentsMargins(12, 9, 12, 9)
            layout.setSpacing(5)

            title = QLabel(task.title)
            title.setObjectName("TaskTitle")
            title.setTextFormat(Qt.PlainText)
            title.setWordWrap(True)
            title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            title.setMinimumWidth(0)
            title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            layout.addWidget(title)

            context_text = task.context_notes.strip()
            if context_text:
                context = QLabel(context_text)
                context.setObjectName("TaskContext")
                context.setTextFormat(Qt.PlainText)
                context.setWordWrap(True)
                context.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                context.setMinimumWidth(0)
                context.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                layout.addWidget(context)

            meta_parts = ["ready" if task.plan_status == "ready" else "pending"]
            relevant_files = []
            if isinstance(task.plan, dict):
                relevant_files = task.plan.get("relevant_files", [])
            if isinstance(relevant_files, list) and relevant_files:
                meta_parts.append("{0} files".format(len(relevant_files)))
            footer_actions = None
            if task.phase == "needs_testing" and (on_approve_testing is not None or on_reject_testing is not None):
                footer_actions = QWidget(self)
                footer_actions_layout = QHBoxLayout(footer_actions)
                footer_actions_layout.setContentsMargins(0, 0, 0, 0)
                footer_actions_layout.setSpacing(6)

                if on_approve_testing is not None:
                    approve_button = QToolButton(footer_actions)
                    approve_button.setObjectName("CardApproveButton")
                    approve_button.setIcon(self.style().standardIcon(QStyle.SP_DialogApplyButton))
                    approve_button.setToolTip("Approve testing and move this card to Completed.")
                    approve_button.clicked.connect(lambda _checked=False, current_task=task: on_approve_testing(current_task))
                    footer_actions_layout.addWidget(approve_button)

                if on_reject_testing is not None:
                    reject_button = QToolButton(footer_actions)
                    reject_button.setObjectName("CardRejectButton")
                    reject_button.setIcon(self.style().standardIcon(QStyle.SP_DialogCancelButton))
                    reject_button.setToolTip("Return this card to Backlog with testing feedback.")
                    reject_button.clicked.connect(lambda _checked=False, current_task=task: on_reject_testing(current_task))
                    footer_actions_layout.addWidget(reject_button)

            footer = _TaskCardFooter(task.board_title, " | ".join(meta_parts), actions_widget=footer_actions)
            layout.addWidget(footer)

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
            move_menu = None
            if self._on_move_task is not None and self._move_targets:
                move_menu = menu.addMenu("Move to")
                for phase in self._move_targets:
                    action = move_menu.addAction(_phase_label(phase))
                    action.setData(phase)
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
            elif move_menu is not None and action in move_menu.actions():
                target_phase = str(action.data() or "").strip()
                if target_phase:
                    self._on_move_task(self._task_id, target_phase)
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
                theme = _active_theme(self.parent())
                option.font.setBold(True)
                option.palette.setColor(QPalette.Text, QColor(theme["archived_text"]))
                option.palette.setColor(QPalette.WindowText, QColor(theme["archived_text"]))
                option.palette.setColor(QPalette.HighlightedText, QColor(theme["archived_text"]))
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
                theme = _active_theme(view)
                painter.save()
                painter.setRenderHint(QPainter.Antialiasing, True)
                rect = option.rect.adjusted(2, 2, -2, -2)
                fill = QColor(theme["board_drop_fill"])
                fill.setAlpha(150)
                border = QColor(theme["board_drop_border"])
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
            painter.setPen(QColor(_active_theme(view)["board_archived_line"]))
            y = option.rect.top()
            painter.drawLine(option.rect.left() + 8, y, option.rect.right() - 8, y)
            painter.restore()

    class BoardList(QListWidget):
        def __init__(self, on_move_task_to_board: Any, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._on_move_task_to_board = on_move_task_to_board
            self._drop_target_row = -1
            self._drop_target_board_id = ""
            self.setAcceptDrops(True)
            self.setProperty("dropTargetRow", -1)

        def _drop_target_for_event(self, event: Any) -> tuple[int, str]:
            item = self.itemAt(event.position().toPoint())
            if item is None:
                return -1, ""
            board_id = item.data(Qt.UserRole)
            if board_id is None:
                return self.row(item), ""
            return self.row(item), str(board_id).strip()

        def _set_drop_target(self, row: int, board_id: str) -> None:
            board_id = board_id.strip()
            if row == self._drop_target_row and board_id == self._drop_target_board_id:
                return
            if row == self._drop_target_row:
                self._drop_target_board_id = board_id
            previous_row = self._drop_target_row
            self._drop_target_row = row
            self._drop_target_board_id = board_id
            self.setProperty("dropTargetRow", row)
            for target_row in (previous_row, row):
                if target_row < 0:
                    continue
                item = self.item(target_row)
                if item is not None:
                    self.viewport().update(self.visualItemRect(item))

        def _clear_drop_target_row(self) -> None:
            self._set_drop_target(-1, "")

        def _accepts_task_drop(self, event: Any) -> bool:
            task_id, _source_phase = _drag_payload_from_mime_data(event.mimeData())
            if not task_id or self._on_move_task_to_board is None:
                return False
            _drop_target_row, board_id = self._drop_target_for_event(event)
            return bool(board_id)

        def dragEnterEvent(self, event) -> None:
            drop_target_row, board_id = self._drop_target_for_event(event)
            if board_id and self._on_move_task_to_board is not None:
                self._set_drop_target(drop_target_row, board_id)
                event.acceptProposedAction()
                return
            self._clear_drop_target_row()
            event.ignore()

        def dragMoveEvent(self, event) -> None:
            drop_target_row, board_id = self._drop_target_for_event(event)
            if board_id and self._on_move_task_to_board is not None:
                self._set_drop_target(drop_target_row, board_id)
                event.acceptProposedAction()
                return
            self._clear_drop_target_row()
            event.ignore()

        def dragLeaveEvent(self, event) -> None:
            self._clear_drop_target_row()
            super().dragLeaveEvent(event)

        def dropEvent(self, event) -> None:
            if not self._accepts_task_drop(event):
                self._clear_drop_target_row()
                event.ignore()
                return

            board_id = self._drop_target_board_id
            self._clear_drop_target_row()
            if not board_id or self._on_move_task_to_board is None:
                event.ignore()
                return

            task_id, _source_phase = _drag_payload_from_mime_data(event.mimeData())
            self._on_move_task_to_board(task_id, board_id)
            event.setDropAction(Qt.MoveAction)
            event.acceptProposedAction()

    class PhaseColumnBody(QWidget):
        def __init__(self, owner: Any) -> None:
            super().__init__(owner)
            self._owner = owner
            self.setAcceptDrops(True)

        def dragEnterEvent(self, event) -> None:
            self._owner._handle_drag_enter(event)

        def dragMoveEvent(self, event) -> None:
            self._owner._handle_drag_move(event)

        def dragLeaveEvent(self, event) -> None:
            self._owner._handle_drag_leave(event)

        def dropEvent(self, event) -> None:
            self._owner._handle_drop(event)

    class PhaseColumn(QFrame):
        def __init__(self, phase: str) -> None:
            super().__init__()
            self.phase = phase
            self._on_move_task: Any = None
            self.setObjectName("PhaseColumn")
            self._base_width = 300
            self.setMinimumWidth(self._base_width)
            self.setAcceptDrops(True)
            self.setProperty("dragOver", False)
            self.setToolTip("Drop a card here to move it to {0}.".format(_phase_label(phase)))
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

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

            self.cards_scroll = QScrollArea()
            self.cards_scroll.setObjectName("PhaseBodyScroll")
            self.cards_scroll.setWidgetResizable(True)
            self.cards_scroll.setFrameShape(QFrame.NoFrame)
            self.cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.cards_scroll.viewport().setObjectName("PhaseBodyViewport")

            self.body_widget = PhaseColumnBody(self)
            self.body_widget.setObjectName("PhaseBody")
            self.body_layout = QVBoxLayout(self.body_widget)
            self.body_layout.setContentsMargins(0, 0, 0, 0)
            self.body_layout.setSpacing(10)
            self.cards_scroll.setWidget(self.body_widget)
            layout.addWidget(self.cards_scroll, 1)

        def _update_column_width(self) -> None:
            self.setFixedWidth(self._base_width)

        def set_tasks(self,
                      tasks: List[StoredTask],
                      *,
                      phase_order: List[str],
                      on_start_task: Any,
                      on_approve_testing: Any,
                      on_reject_testing: Any,
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
                self.body_layout.addStretch(1)
                self.body_layout.addWidget(empty)
                self.body_layout.addStretch(1)
                self._update_column_width()
                return

            for task in tasks:
                self.body_layout.addWidget(
                    TaskCard(
                        task,
                        phase_order=phase_order,
                        on_start_task=on_start_task,
                        on_approve_testing=on_approve_testing,
                        on_reject_testing=on_reject_testing,
                        on_edit=lambda _checked=False, current_task=task: on_edit_task(current_task),
                        on_delete=lambda _checked=False, current_task=task: on_delete_task(current_task),
                        on_move_task=on_move_task,
                        on_move_task_to_board=on_move_task_to_board,
                    )
                )

            self.body_layout.addStretch(1)
            self._update_column_width()

        def scroll_value(self) -> int:
            return self.cards_scroll.verticalScrollBar().value()

        def restore_scroll_value(self, value: int) -> None:
            scrollbar = self.cards_scroll.verticalScrollBar()
            scrollbar.setValue(min(value, scrollbar.maximum()))

        def _accepts_task_drop(self, event: Any) -> bool:
            if self._on_move_task is None:
                return False
            task_id, source_phase = _drag_payload_from_mime_data(event.mimeData())
            if not task_id or not source_phase:
                return False
            if source_phase == self.phase:
                return False
            return True

        def _handle_drag_enter(self, event) -> None:
            if self._accepts_task_drop(event):
                _set_drag_highlight(self, True)
                event.acceptProposedAction()
                return
            event.ignore()

        def _handle_drag_move(self, event) -> None:
            if self._accepts_task_drop(event):
                _set_drag_highlight(self, True)
                event.acceptProposedAction()
                return
            event.ignore()

        def _handle_drag_leave(self, event) -> None:
            _set_drag_highlight(self, False)
            event.accept()

        def _handle_drop(self, event) -> None:
            _set_drag_highlight(self, False)
            if not self._accepts_task_drop(event):
                event.ignore()
                return
            task_id, _source_phase = _drag_payload_from_mime_data(event.mimeData())
            self._on_move_task(task_id, self.phase)
            event.setDropAction(Qt.MoveAction)
            event.acceptProposedAction()

        def dragEnterEvent(self, event) -> None:
            self._handle_drag_enter(event)

        def dragMoveEvent(self, event) -> None:
            self._handle_drag_move(event)

        def dragLeaveEvent(self, event) -> None:
            self._handle_drag_leave(event)

        def dropEvent(self, event) -> None:
            self._handle_drop(event)

    class TaskbotWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.saved_session = _load_saved_session()
            self.current_theme = _normalize_theme_name(self.saved_session.get("theme", "light"))
            self.theme_values = _theme_values(self.current_theme)
            self.status_note = "Ready"
            self._last_terminal_text: Optional[str] = None
            self._store_signature: tuple[bool, int, int] | None = None
            self._runtime_signature: tuple[bool, int, int] | None = None
            self._terminal_signature: tuple[bool, int, int] | None = None
            self._cached_boards: List[Dict[str, Any]] = []
            self._cached_tasks: List[StoredTask] = []
            self._cached_runtime_payload: Dict[str, Any] = {}
            self._handled_approval_request_ids: set[str] = set()
            self._last_rendered_selected_board_id: str | None | object = object()
            self._last_rendered_board_search_query: str | object = object()
            self._git_repo_available = False
            self._git_branch_reason = ""
            self._current_git_branch = ""
            self._open_dialogs: List[QDialog] = []
            self.active_config = self._initial_config()
            self.selected_board_id = self._initial_board_id()
            self.board_search_query = ""
            self.terminal_font_family = _preferred_monospace_family()
            self.phase_order: List[str] = []
            self.phase_columns: Dict[str, PhaseColumn] = {}
            self.board_shell: QFrame
            self.board_search_input: QLineEdit
            self.stage_shell: QFrame
            self.stage_scrollbar: QScrollBar

            self.setWindowTitle("Taskbot")
            self.resize(1480, 920)
            self.setMinimumSize(1080, 720)

            self._build_ui()
            self._set_theme(self.current_theme)
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

            self.theme_toggle = _ThemeToggleButton()
            self.theme_toggle.setObjectName("ThemeToggleButton")
            self.theme_toggle.setCheckable(True)
            self.theme_toggle.setChecked(self.current_theme == "dark")
            self.theme_toggle.setToolTip("Toggle the dark Material theme.")
            self.theme_toggle.toggled.connect(self._on_theme_toggle_changed)
            title_row.addWidget(self.theme_toggle, 0, Qt.AlignTop)
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

            branch_label = QLabel("Branch")
            branch_label.setObjectName("TopFieldLabel")
            repo_row.addWidget(branch_label)

            self.branch_dropdown = _FormDropdown()
            self.branch_dropdown.setObjectName("HeaderBranchDropdown")
            self.branch_dropdown.setMinimumWidth(160)
            self.branch_dropdown.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            self.branch_dropdown.setForegroundPopup(True)
            self.branch_dropdown.currentIndexChanged.connect(self._on_branch_selection_changed)
            repo_row.addWidget(self.branch_dropdown)

            settings_button = QPushButton("Settings")
            settings_button.clicked.connect(self._open_settings_dialog)
            repo_row.addWidget(settings_button)

            agents_button = QPushButton("Agents.md")
            agents_button.clicked.connect(self._open_agents_dialog)
            repo_row.addWidget(agents_button)

            self.open_terminal_button = QPushButton("Open Terminal")
            self.open_terminal_button.setObjectName("HeaderOpenTerminalButton")
            self.open_terminal_button.clicked.connect(self._open_repo_terminal)
            repo_row.addWidget(self.open_terminal_button)

            self.run_repo_command_button = QPushButton("Run Command")
            self.run_repo_command_button.clicked.connect(self._run_repo_command)
            repo_row.addWidget(self.run_repo_command_button)

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

            repo_header_divider = QFrame()
            repo_header_divider.setObjectName("HeaderDivider")
            repo_header_divider.setFrameShape(QFrame.VLine)
            repo_header_divider.setFrameShadow(QFrame.Plain)
            repo_header_divider.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
            repo_header_divider.setFixedWidth(1)
            repo_row.addWidget(repo_header_divider)
            repo_row.addWidget(controls_panel)
            header_stack.addLayout(repo_row)

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
            board_header_layout.setContentsMargins(12, 8, 12, 8)
            board_header_layout.setSpacing(8)

            board_title_stack = QVBoxLayout()
            board_title_stack.setContentsMargins(0, 0, 0, 0)
            board_title_stack.setSpacing(1)

            board_title_row = QHBoxLayout()
            board_title_row.setContentsMargins(0, 0, 0, 0)
            board_title_row.setSpacing(8)

            self.board_title_label = QLabel(_board_header_title("All Boards", 0))
            self.board_title_label.setObjectName("BoardTitle")
            board_title_row.addWidget(self.board_title_label)
            board_title_row.addItem(QSpacerItem(12, 12, QSizePolicy.Expanding, QSizePolicy.Minimum))
            board_title_stack.addLayout(board_title_row)

            self.board_summary_label = QLabel("")
            self.board_summary_label.setObjectName("BoardSummary")
            self.board_summary_label.setWordWrap(False)
            board_title_stack.addWidget(self.board_summary_label)
            board_header_layout.addLayout(board_title_stack)

            self.add_task_button = QPushButton("+ Task")
            self.add_task_button.setObjectName("AccentButton")
            self.add_task_button.clicked.connect(self._open_add_task_dialog)
            self.add_task_button.setMaximumWidth(84)
            board_header_layout.addWidget(self.add_task_button)

            self.develop_tickets_button = QPushButton("Develop Tickets")
            self.develop_tickets_button.clicked.connect(self._open_ticket_development_dialog)
            board_header_layout.addWidget(self.develop_tickets_button)

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

            self.board_search_input = QLineEdit()
            self.board_search_input.setObjectName("BoardSearchInput")
            self.board_search_input.setClearButtonEnabled(True)
            self.board_search_input.setPlaceholderText("Filter cards by title")
            self.board_search_input.setFixedWidth(240)
            self.board_search_input.setFixedHeight(30)
            self.board_search_input.textChanged.connect(self._on_board_search_changed)
            board_header_layout.addWidget(self.board_search_input, 0, Qt.AlignVCenter)

            self.columns_scroll = QScrollArea()
            self.columns_scroll.setObjectName("ColumnsScroll")
            self.columns_scroll.setWidgetResizable(True)
            self.columns_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.columns_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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

        def _persist_session(self, repo_root: Optional[Path] = None) -> None:
            target_repo = repo_root or Path(self.active_config["repo_root"])
            _save_session(target_repo, self.selected_board_id, self.current_theme)

        def _on_theme_toggle_changed(self, checked: bool) -> None:
            self._set_theme("dark" if checked else "light", persist=True)

        def _set_theme(self, theme_name: str, persist: bool = False) -> None:
            normalized_theme = _normalize_theme_name(theme_name)
            self.current_theme = normalized_theme
            self.theme_values = _theme_values(normalized_theme)
            if hasattr(self, "theme_toggle"):
                self.theme_toggle.blockSignals(True)
                self.theme_toggle.setChecked(normalized_theme == "dark")
                self.theme_toggle.blockSignals(False)
            self._apply_window_style()
            self.update()
            if hasattr(self, "board_list"):
                self.board_list.viewport().update()
            if hasattr(self, "runtime_label") and hasattr(self, "status_chip"):
                self._update_status_header(self._cached_boards, self._cached_tasks, self._cached_runtime_payload)
            if persist:
                self._persist_session()

        def _apply_window_style(self) -> None:
            checkbox_tick_icon = _checkbox_indicator_tick_icon_path().resolve().as_posix()
            theme = self.theme_values
            stylesheet = """
            QMainWindow {
                background: __MAIN_WINDOW_BG__;
                color: __MAIN_WINDOW_FG__;
            }

            QWidget {
                font-size: 12px;
            }

            QFrame#TopShell,
            QFrame#BoardShell,
            QFrame#TerminalShell {
                background: __SHELL_BG__;
                border: 1px solid __SHELL_BORDER__;
                border-radius: 8px;
            }

            QFrame#BoardDivider {
                background: __BOARD_DIVIDER__;
                border: none;
            }

            QFrame#Sidebar {
                background: __SIDEBAR_BG__;
                border: 1px solid __SIDEBAR_BORDER__;
                border-radius: 8px;
            }

            QFrame#HeaderDivider {
                background: __HEADER_DIVIDER__;
                border: none;
                min-width: 1px;
                max-width: 1px;
                margin: 4px 0;
            }

            QLabel#Headline {
                color: __HEADLINE__;
                font-size: 22px;
                font-weight: 800;
            }

            QLabel#StatusChip {
                background: __STATUS_BG__;
                color: __STATUS_FG__;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
                font-weight: 700;
            }

            QPushButton#ThemeToggleButton {
                background: transparent;
                color: transparent;
                border: none;
                padding: 0;
                min-width: 56px;
                max-width: 56px;
                min-height: 24px;
                max-height: 24px;
            }

            QPushButton#ThemeToggleButton:hover {
                background: transparent;
            }

            QPushButton#ThemeToggleButton:checked {
                background: transparent;
                color: transparent;
            }

            QPushButton#ThemeToggleButton:focus {
                outline: none;
            }

            QLabel#TopFieldLabel {
                color: __TOP_FIELD_LABEL__;
                font-size: 10px;
                font-weight: 700;
                text-transform: uppercase;
            }

            QLabel#FieldLabel {
                color: __FIELD_LABEL__;
                font-size: 10px;
                font-weight: 700;
                text-transform: uppercase;
            }

            QCheckBox {
                color: __CHECKBOX_TEXT__;
                spacing: 6px;
                font-size: 12px;
            }

            QCheckBox::indicator {
                width: 14px;
                height: 14px;
            }

            QCheckBox::indicator:unchecked {
                background: __CHECKBOX_BG__;
                border: 1px solid __CHECKBOX_BORDER__;
                border-radius: 3px;
            }

            QCheckBox::indicator:checked {
                background: __CHECKBOX_CHECKED_BG__;
                border: 1px solid __CHECKBOX_CHECKED_BORDER__;
                border-radius: 3px;
                image: url("__CHECKBOX_TICK_ICON__");
            }

            QLineEdit,
            QPlainTextEdit,
            QTextEdit#TicketChatTranscript,
            QListWidget,
            QSpinBox,
            QDialog#AppDialog QToolButton#DialogDropdown,
            QToolButton#HeaderBranchDropdown {
                background: __INPUT_BG__;
                color: __INPUT_FG__;
                border: 1px solid __INPUT_BORDER__;
                border-radius: 4px;
                padding: 6px 8px;
            }

            QLineEdit:focus,
            QPlainTextEdit:focus,
            QTextEdit#TicketChatTranscript:focus,
            QListWidget:focus,
            QSpinBox:focus,
            QDialog#AppDialog QToolButton#DialogDropdown:focus,
            QToolButton#HeaderBranchDropdown:focus {
                border: 1px solid __INPUT_FOCUS__;
            }

            QLineEdit#BoardSearchInput {
                padding-top: 4px;
                padding-bottom: 4px;
            }

            QSpinBox:disabled {
                background: __INPUT_DISABLED_BG__;
                color: __INPUT_DISABLED_FG__;
            }

            QPushButton,
            QToolButton,
            QDialogButtonBox QPushButton {
                background: __BUTTON_BG__;
                color: __BUTTON_FG__;
                border: 1px solid __BUTTON_BORDER__;
                border-radius: 4px;
                padding: 6px 10px;
                font-weight: 600;
            }

            QPushButton:hover,
            QToolButton:hover,
            QDialogButtonBox QPushButton:hover {
                background: __BUTTON_HOVER__;
            }

            QPushButton#PrimaryButton,
            QPushButton#AccentButton {
                background: __PRIMARY_BG__;
                color: __PRIMARY_FG__;
                border: 1px solid __PRIMARY_BORDER__;
            }

            QPushButton#PrimaryButton:hover,
            QPushButton#AccentButton:hover {
                background: __PRIMARY_HOVER__;
            }

            QPushButton#HeaderPlanButton {
                background: __PLAN_BG__;
                color: __PLAN_FG__;
                border: 1px solid __PLAN_BORDER__;
            }

            QPushButton#HeaderPlanButton:hover {
                background: __PLAN_HOVER__;
            }

            QPushButton#HeaderRunButton {
                background: __RUN_BG__;
                color: __RUN_FG__;
                border: 1px solid __RUN_BORDER__;
            }

            QPushButton#HeaderRunButton:hover {
                background: __RUN_HOVER__;
            }

            QPushButton#HeaderLoopButton {
                background: __LOOP_BG__;
                color: __LOOP_FG__;
                border: 1px solid __LOOP_BORDER__;
            }

            QPushButton#HeaderLoopButton:hover {
                background: __LOOP_HOVER__;
            }

            QPushButton#HeaderStopButton {
                background: __STOP_BG__;
                color: __STOP_FG__;
                border: 1px solid __STOP_BORDER__;
            }

            QPushButton#HeaderStopButton:hover {
                background: __STOP_HOVER__;
            }

            QPushButton#HeaderOpenTerminalButton {
                background: __TERMINAL_BUTTON_BG__;
                color: __TERMINAL_BUTTON_FG__;
                border: 1px solid __TERMINAL_BUTTON_BORDER__;
            }

            QPushButton#HeaderOpenTerminalButton:hover {
                background: __TERMINAL_BUTTON_HOVER__;
            }

            QToolButton#SmallActionButton {
                min-width: 24px;
                min-height: 24px;
                max-width: 24px;
                max-height: 24px;
                font-size: 14px;
                font-weight: 700;
                background: __SMALL_ACTION_BG__;
                color: __SMALL_ACTION_FG__;
                border: 1px solid __SMALL_ACTION_BORDER__;
                border-radius: 4px;
                padding: 0;
            }

            QLabel#RuntimeLabel,
            QLabel#BoardSummary,
            QLabel#TerminalHint,
            QLabel#SidebarCaption {
                color: __MUTED_TEXT__;
                font-size: 11px;
            }

            QLabel#DialogCaption {
                color: __MUTED_TEXT__;
                font-size: 11px;
            }

            QLabel#SidebarTitle {
                color: __SIDEBAR_TITLE__;
                font-size: 14px;
                font-weight: 700;
            }

            QListWidget#BoardList {
                background: __BOARD_LIST_BG__;
                color: __BOARD_LIST_FG__;
                border: 1px solid __BOARD_LIST_BORDER__;
                border-radius: 4px;
                padding: 3px;
            }

            QListWidget#BoardList::item {
                padding: 7px 9px;
                border-radius: 3px;
                margin-bottom: 1px;
            }

            QListWidget#BoardList::item:selected {
                background: __BOARD_LIST_SELECTED_BG__;
                color: __BOARD_LIST_SELECTED_FG__;
            }

            QListWidget#BoardList::item:hover:!selected {
                background: __BOARD_LIST_HOVER_BG__;
            }

            QLabel#BoardTitle {
                color: __BOARD_TITLE__;
                font-size: 16px;
                font-weight: 700;
            }

            QScrollArea#ColumnsScroll {
                border: none;
                background: __COLUMNS_BG__;
            }

            QWidget#ColumnsViewport,
            QWidget#ColumnsContainer {
                background: __COLUMNS_BG__;
            }

            QScrollBar#StageScrollBar:horizontal {
                background: __STAGE_SCROLLBAR_BG__;
                height: 12px;
                margin: 0;
                border: 1px solid __STAGE_SCROLLBAR_BORDER__;
                border-radius: 3px;
            }

            QScrollBar#StageScrollBar::handle:horizontal {
                background: __STAGE_SCROLLBAR_HANDLE__;
                min-width: 60px;
                border-radius: 3px;
            }

            QScrollBar#StageScrollBar::handle:horizontal:hover {
                background: __STAGE_SCROLLBAR_HANDLE_HOVER__;
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
                background: __PHASE_COLUMN_BG__;
                border: 1px solid __PHASE_COLUMN_BORDER__;
                border-radius: 6px;
            }

            QFrame#PhaseColumn[dragOver="true"] {
                background: __PHASE_DRAG_BG__;
                border: 1px solid __PRIMARY_BG__;
            }

            QLabel#PhaseTitle {
                color: __PHASE_TITLE__;
                font-size: 14px;
                font-weight: 700;
            }

            QLabel#PhaseCount {
                background: __PHASE_COUNT_BG__;
                color: __PHASE_COUNT_FG__;
                border-radius: 3px;
                padding: 2px 6px;
                font-weight: 700;
            }

            QScrollArea#PhaseBodyScroll {
                background: transparent;
                border: none;
            }

            QWidget#PhaseBodyViewport,
            QWidget#PhaseBody {
                background: transparent;
            }

            QFrame#TaskCard {
                background: __TASK_CARD_BG__;
                border: 1px solid __TASK_CARD_BORDER__;
                border-radius: 6px;
            }

            QFrame#TaskCard[dragOver="true"] {
                background: __TASK_CARD_DRAG_BG__;
                border: 1px solid __PRIMARY_BG__;
            }

            QFrame#TaskCard:hover {
                background: __TASK_CARD_HOVER_BG__;
                border: 1px solid __TASK_CARD_HOVER_BORDER__;
            }

            QFrame#TaskCard:focus {
                background: __TASK_CARD_FOCUS_BG__;
                border: 1px solid __TASK_CARD_FOCUS_BORDER__;
                outline: none;
            }

            QToolButton#CardDeleteButton {
                background: __CARD_DELETE_BG__;
                color: __CARD_DELETE_FG__;
                border: 1px solid __CARD_DELETE_BORDER__;
                border-radius: 6px;
                padding: 0px;
            }

            QToolButton#CardDeleteButton:hover {
                background: __CARD_DELETE_HOVER_BG__;
                border: 1px solid __CARD_DELETE_HOVER_BORDER__;
            }

            QToolButton#CardDeleteButton:pressed {
                background: __CARD_DELETE_PRESSED_BG__;
                border: 1px solid __CARD_DELETE_PRESSED_BORDER__;
            }

            QToolButton#CardCompleteButton {
                background: __SUCCESS_BG__;
                color: __SUCCESS_FG__;
                border: 1px solid __SUCCESS_BORDER__;
                border-radius: 10px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 700;
            }

            QToolButton#CardCompleteButton:hover {
                background: __SUCCESS_HOVER_BG__;
                border: 1px solid __SUCCESS_HOVER_BORDER__;
            }

            QToolButton#CardApproveButton {
                background: __SUCCESS_BG__;
                color: __SUCCESS_FG__;
                border: 1px solid __SUCCESS_BORDER__;
                border-radius: 10px;
                padding: 3px;
            }

            QToolButton#CardApproveButton:hover {
                background: __SUCCESS_HOVER_BG__;
                border: 1px solid __SUCCESS_HOVER_BORDER__;
            }

            QToolButton#CardRejectButton {
                background: __REJECT_BG__;
                color: __REJECT_FG__;
                border: 1px solid __REJECT_BORDER__;
                border-radius: 10px;
                padding: 3px;
            }

            QToolButton#CardRejectButton:hover {
                background: __REJECT_HOVER_BG__;
                border: 1px solid __REJECT_HOVER_BORDER__;
            }

            QToolButton#CardNeedsTestingButton {
                background: __NEEDS_TESTING_BG__;
                color: __NEEDS_TESTING_FG__;
                border: 1px solid __NEEDS_TESTING_BORDER__;
                border-radius: 10px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 700;
            }

            QToolButton#CardNeedsTestingButton:hover {
                background: __NEEDS_TESTING_HOVER_BG__;
                border: 1px solid __NEEDS_TESTING_HOVER_BORDER__;
            }

            QLabel#BoardBadge {
                background: __BOARD_BADGE_BG__;
                color: __BOARD_BADGE_FG__;
                border-radius: 3px;
                padding: 2px 5px;
                font-size: 10px;
                font-weight: 700;
            }

            QLabel#TaskTitle {
                color: __TASK_TITLE__;
                font-size: 14px;
                font-weight: 700;
            }

            QLabel#TaskContext {
                color: __TASK_CONTEXT__;
                font-size: 12px;
            }

            QLabel#TaskMeta {
                color: __TASK_META__;
                font-size: 10px;
                font-weight: 600;
            }

            QLabel#TaskError {
                color: __TASK_ERROR__;
                font-size: 11px;
                font-weight: 600;
            }

            QLabel#ColumnEmpty {
                color: __COLUMN_EMPTY__;
                font-style: italic;
                padding: 10px 2px;
            }

            QLabel#TerminalTitle {
                color: __TERMINAL_TITLE__;
                font-size: 14px;
                font-weight: 700;
            }

            QLabel#DialogTitle {
                color: __DIALOG_TITLE__;
                font-size: 14px;
                font-weight: 700;
            }

            QDialog#AppDialog {
                background: __SHELL_BG__;
                border: 1px solid __SHELL_BORDER__;
                border-radius: 8px;
            }

            QDialog#AppDialog QScrollArea#SettingsScrollArea {
                background: __SHELL_BG__;
                border: none;
            }

            QDialog#AppDialog QWidget#SettingsContent {
                background: __SHELL_BG__;
            }

            QDialog#AppDialog QToolButton#DialogDropdown {
                min-height: 32px;
                text-align: left;
                padding-right: 26px;
                font-weight: 500;
            }

            QDialog#AppDialog QToolButton#DialogDropdown:hover {
                background: __DIALOG_DROPDOWN_HOVER__;
            }

            QDialog#AppDialog QToolButton#DialogDropdown:pressed {
                background: __DIALOG_DROPDOWN_PRESSED__;
            }

            QDialog#AppDialog QToolButton#DialogDropdown::menu-indicator {
                subcontrol-origin: padding;
                subcontrol-position: center right;
                width: 14px;
                height: 14px;
            }

            QDialog#AppDialog QScrollArea#SettingsScrollArea > QWidget {
                background: __SHELL_BG__;
            }

            QDialog#AppDialog QScrollArea#SettingsScrollArea > QWidget > QWidget {
                background: __SHELL_BG__;
            }

            QDialog#AppDialog QMenu#DialogDropdownMenu {
                background: __MENU_BG__;
                color: __MENU_FG__;
                border: 1px solid __INPUT_BORDER__;
            }

            QDialog#AppDialog QMenu#DialogDropdownMenu::item {
                padding: 6px 8px;
            }

            QDialog#AppDialog QMenu#DialogDropdownMenu::item:selected {
                background: __MENU_SELECTED_BG__;
                color: __MENU_SELECTED_FG__;
            }

            QDialog#AppDialog QMenu#DialogDropdownMenu::separator {
                height: 1px;
                background: __MENU_SEPARATOR__;
                margin: 4px 6px;
            }

            QDialog#AppDialog QDialogButtonBox#DialogButtons {
                padding-top: 2px;
            }

            QTextEdit#TerminalOutput {
                background: __TERMINAL_OUTPUT_BG__;
                color: __TERMINAL_OUTPUT_FG__;
                border: 1px solid __TERMINAL_OUTPUT_BORDER__;
                border-radius: 4px;
                padding: 8px;
            }

            QTextEdit#TerminalOutput QScrollBar:vertical {
                background: __TERMINAL_SCROLL_BG__;
                width: 10px;
                margin: 2px 1px 2px 1px;
            }

            QTextEdit#TerminalOutput QScrollBar::handle:vertical {
                background: __TERMINAL_SCROLL_HANDLE__;
                min-height: 36px;
                border-radius: 4px;
            }

            QTextEdit#TerminalOutput QScrollBar:horizontal {
                background: __TERMINAL_SCROLL_BG__;
                height: 10px;
                margin: 1px 2px 1px 2px;
            }

            QTextEdit#TerminalOutput QScrollBar::handle:horizontal {
                background: __TERMINAL_SCROLL_HANDLE__;
                min-width: 36px;
                border-radius: 4px;
            }

            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 2px 1px 2px 1px;
            }

            QScrollBar::handle:vertical {
                background: __SCROLLBAR_HANDLE__;
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
                background: __SCROLLBAR_HANDLE__;
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
            replacements = {"checkbox_tick_icon": checkbox_tick_icon, **theme}
            for key, value in replacements.items():
                stylesheet = stylesheet.replace("__{0}__".format(key.upper()), value)
            self.setStyleSheet(stylesheet)

        def _refresh_repo_widgets(self) -> None:
            self.repo_input.setText(str(self.active_config["repo_root"]))
            self._refresh_branch_dropdown()
            self._refresh_repo_action_buttons()

        def _runner_active(self) -> bool:
            return self._runtime_path().exists()

        def _refresh_branch_dropdown(self) -> None:
            repo_root = Path(self.active_config["repo_root"])
            branch_state = inspect_git_branches(repo_root)
            self._git_repo_available = branch_state.repo_available
            self._git_branch_reason = branch_state.reason
            self._current_git_branch = branch_state.current_branch

            self.branch_dropdown.blockSignals(True)
            self.branch_dropdown.clear()

            if branch_state.repo_available:
                if branch_state.current_branch:
                    for branch_name in branch_state.branches:
                        self.branch_dropdown.addItem(branch_name, branch_name)
                    if branch_state.branches:
                        self.branch_dropdown.setCurrentData(branch_state.current_branch)
                else:
                    self.branch_dropdown.addItem("Detached HEAD", "")
                    for branch_name in branch_state.branches:
                        self.branch_dropdown.addItem(branch_name, branch_name)
                    self.branch_dropdown.setCurrentIndex(0)
            else:
                self.branch_dropdown.addItem("No git repo", "")

            self.branch_dropdown.blockSignals(False)
            self.branch_dropdown.setEnabled(branch_state.repo_available and not self._runner_active())
            self.branch_dropdown.setToolTip(branch_state.reason or branch_state.current_branch or "Git branch selector")

        def _refresh_repo_action_buttons(self) -> None:
            repo_root = Path(self.active_config["repo_root"])
            run_command = _repo_run_command_parts(self.active_config)
            runner_active = self._runner_active()
            self.open_terminal_button.setEnabled(repo_root.exists() and repo_root.is_dir())
            self.run_repo_command_button.setVisible(bool(run_command))
            self.run_repo_command_button.setEnabled(bool(run_command) and not runner_active)
            self.run_repo_command_button.setToolTip(
                shlex.join(run_command) if run_command else "Configure a repo run command in Settings"
            )

        def _invalidate_refresh_cache(self) -> None:
            self._store_signature = None
            self._runtime_signature = None
            self._terminal_signature = None
            self._cached_boards = []
            self._cached_tasks = []
            self._cached_runtime_payload = {}
            self._handled_approval_request_ids.clear()
            self._last_terminal_text = None
            self._last_rendered_selected_board_id = object()
            self._last_rendered_board_search_query = object()

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

        def _capture_phase_column_scroll_state(self) -> Dict[str, int]:
            return {
                phase: column.scroll_value()
                for phase, column in self.phase_columns.items()
            }

        def _restore_phase_column_scroll_state(self, scroll_values: Dict[str, int]) -> None:
            for phase, value in scroll_values.items():
                column = self.phase_columns.get(phase)
                if column is not None:
                    column.restore_scroll_value(value)

        def _restore_columns_scroll_state(self,
                                          horizontal_value: int,
                                          column_scroll_values: Dict[str, int]) -> None:
            horizontal_scrollbar = self.columns_scroll.horizontalScrollBar()
            horizontal_scrollbar.setValue(min(horizontal_value, horizontal_scrollbar.maximum()))
            self._restore_phase_column_scroll_state(column_scroll_values)
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

        def _respond_to_approval_request(self, request_id: str, approved: bool, source: str = "ui") -> None:
            _write_approval_response(self.active_config["control_dir"], request_id, approved, source)

        def _maybe_prompt_for_approval_request(self, runtime_payload: Dict[str, Any]) -> None:
            approval_request = _pending_approval_request(runtime_payload)
            if approval_request is None:
                return
            request_id = str(approval_request.get("id", "")).strip()
            if not request_id or request_id in self._handled_approval_request_ids:
                return
            if _approval_response_path(self.active_config["control_dir"], request_id).exists():
                self._handled_approval_request_ids.add(request_id)
                return

            self._handled_approval_request_ids.add(request_id)
            task_id = str(approval_request.get("task_id", "")).strip()
            phase_name = str(approval_request.get("phase", "")).strip() or "current"
            message = str(approval_request.get("message", "")).strip()
            prompt = message or "This runner phase needs a one-off broader retry."
            answer = QMessageBox.question(
                self,
                "One-Off Approval Required",
                "{0}\n\nAllow task {1} to retry the {2} phase once with broader Codex access?".format(
                    prompt,
                    task_id or "the active task",
                    phase_name,
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            self._respond_to_approval_request(request_id, answer == QMessageBox.Yes)

        def _selected_board_title(self) -> Optional[str]:
            item = self.board_list.currentItem()
            if item is None:
                return None
            return item.data(Qt.UserRole + 1)

        def _selected_board_task_count(self) -> Optional[int]:
            item = self.board_list.currentItem()
            if item is None:
                return None
            try:
                return int(item.data(Qt.UserRole + 2) or 0)
            except (TypeError, ValueError):
                return 0

        def _available_board_titles(self) -> List[str]:
            self._refresh_store_cache()
            return [
                board["title"]
                for board in self._cached_boards
                if str(board["board_id"]).strip().lower() != "archived"
            ]

        def _active_tasks(self, tasks: List[StoredTask]) -> List[StoredTask]:
            return [task for task in tasks if task.board_id != "archived"]

        def _visible_board_tasks(self, tasks: List[StoredTask]) -> List[StoredTask]:
            visible_tasks = self._active_tasks(tasks)
            if self.selected_board_id is not None:
                visible_tasks = [task for task in tasks if task.board_id == self.selected_board_id]
            if not self.board_search_query:
                return visible_tasks
            return [
                task for task in visible_tasks
                if self.board_search_query in task.title.lower()
            ]

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
                next_config = _runtime_config_for_repo(repo_root)
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Load Repository", str(exc))
                return

            self._dismiss_open_dialogs()
            self.active_config = next_config
            new_phase_order = phase_labels(self.active_config)
            if new_phase_order != self.phase_order:
                self._rebuild_phase_columns(new_phase_order)

            self.selected_board_id = None
            self._reset_board_search()
            self.status_note = "Loaded repo {0}".format(repo_root.name)
            self._invalidate_refresh_cache()
            self._refresh_repo_widgets()
            self._persist_session(repo_root)
            self.refresh_view()

        def _on_branch_selection_changed(self, _index: int) -> None:
            target_branch = str(self.branch_dropdown.currentData() or "").strip()
            if not target_branch or target_branch == self._current_git_branch:
                return
            if self._runner_active():
                QMessageBox.warning(
                    self,
                    "Runner Active",
                    "Branch changes are blocked while a Taskbot runner is active.",
                )
                self._refresh_branch_dropdown()
                return

            repo_root = Path(self.active_config["repo_root"])
            checkout_result = checkout_git_branch(repo_root, target_branch)
            if not checkout_result.ok:
                QMessageBox.critical(self, "Failed To Switch Branch", checkout_result.reason)
                self._refresh_branch_dropdown()
                return

            self._dismiss_open_dialogs()
            self.active_config = _runtime_config_for_repo(repo_root)
            new_phase_order = phase_labels(self.active_config)
            if new_phase_order != self.phase_order:
                self._rebuild_phase_columns(new_phase_order)

            self.selected_board_id = None
            self._reset_board_search()
            self.status_note = "Switched to branch {0}".format(checkout_result.branch)
            self._invalidate_refresh_cache()
            self._refresh_repo_widgets()
            self._persist_session(repo_root)
            self.refresh_view()

        def _open_repo_terminal(self) -> None:
            ok, error_text = _launch_terminal(Path(self.active_config["repo_root"]))
            if not ok:
                QMessageBox.critical(self, "Failed To Open Terminal", error_text)
                return
            self.status_note = "Opened terminal for {0}".format(Path(self.active_config["repo_root"]).name)
            self.refresh_view()

        def _run_repo_command(self) -> None:
            command = _repo_run_command_parts(self.active_config)
            if not command:
                QMessageBox.warning(
                    self,
                    "Run Command Not Configured",
                    "Configure ui.repo_run_command in Settings before using this action.",
                )
                return
            if self._runner_active():
                QMessageBox.warning(
                    self,
                    "Runner Active",
                    "Repo run commands are blocked while a Taskbot runner is active.",
                )
                return
            try:
                subprocess.Popen(
                    command,
                    cwd=self.active_config["repo_root"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Run Command", str(exc))
                return

            self.status_note = "Started repo command: {0}".format(Path(command[0]).name or command[0])
            self.refresh_view()

        def _show_modeless_dialog(self, dialog: QDialog, on_accepted: Any) -> None:
            self._open_dialogs.append(dialog)
            dialog._taskbot_finish_handled = False
            dialog._taskbot_accept_handled = False
            dialog.accepted.connect(
                lambda current_dialog=dialog: self._handle_modeless_dialog_accept(
                    current_dialog,
                    on_accepted,
                )
            )
            dialog.finished.connect(
                lambda result, current_dialog=dialog: self._finish_modeless_dialog(
                    current_dialog,
                    result,
                )
            )
            dialog.setModal(False)
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        def _handle_modeless_dialog_accept(self, dialog: QDialog, on_accepted: Any) -> None:
            if getattr(dialog, "_taskbot_accept_handled", False):
                return
            dialog._taskbot_accept_handled = True
            on_accepted()

        def _finish_modeless_dialog(self, dialog: QDialog, result: int) -> None:
            if getattr(dialog, "_taskbot_finish_handled", False):
                return
            dialog._taskbot_finish_handled = True
            if dialog in self._open_dialogs:
                self._open_dialogs.remove(dialog)
            dialog.deleteLater()

        def _dismiss_open_dialogs(self) -> None:
            for dialog in list(self._open_dialogs):
                try:
                    dialog.reject()
                except RuntimeError:
                    if dialog in self._open_dialogs:
                        self._open_dialogs.remove(dialog)

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
            submitted_board_title = _capture_modeless_dialog_value(dialog, dialog.board_title)

            def accept_board() -> None:
                try:
                    updated = rename_board(active_config, board_id, submitted_board_title())
                except Exception as exc:
                    QMessageBox.critical(self, "Failed To Rename Board", str(exc))
                    return
                if updated is None:
                    QMessageBox.critical(self, "Failed To Rename Board", "Board could not be found.")
                    return

                renamed_board_id = str(updated.get("board_id", board_id)).strip() or board_id
                self.selected_board_id = renamed_board_id
                self.status_note = "Renamed board {0} to {1}".format(board.get("title", ""), updated["title"])
                _sync_dialog_board_titles(self._open_dialogs, str(board.get("title", "")), str(updated["title"]))
                self._persist_session(Path(active_config["repo_root"]))
                self._invalidate_refresh_cache()
                self.refresh_view()
                default_board_update_error = str(updated.get("default_board_update_error", "")).strip()
                if default_board_update_error:
                    QMessageBox.warning(
                        self,
                        "Board Renamed With Config Warning",
                        (
                            "The board was renamed, but Taskbot could not update the repo's "
                            "default board setting:\n\n{0}"
                        ).format(default_board_update_error),
                    )

            self._show_modeless_dialog(dialog, accept_board)

        def _delete_board(self, board: Dict[str, Any]) -> None:
            board_title = str(board.get("title", ""))
            task_count = int(board.get("count", 0) or 0)
            task_word = "task" if task_count == 1 else "tasks"
            detail = "This will remove {0} {1} from the task store.".format(
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
            self._persist_session()
            self._activate_repo_config(Path(self.active_config["repo_root"]))
            self.refresh_view()

        def _open_add_board_dialog(self) -> None:
            dialog = AddBoardDialog(self)
            active_config = self.active_config
            submitted_board_title = _capture_modeless_dialog_value(dialog, dialog.board_title)

            def accept_board() -> None:
                try:
                    board = create_board(active_config, submitted_board_title())
                except Exception as exc:
                    QMessageBox.critical(self, "Failed To Create Board", str(exc))
                    return
                self.selected_board_id = board["board_id"]
                self.status_note = "Created board {0}".format(board["title"])
                self._persist_session(Path(active_config["repo_root"]))
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
                self._persist_session(Path(active_config["repo_root"]))
                self._activate_repo_config(Path(active_config["repo_root"]))
                self.refresh_view()

            self._show_modeless_dialog(dialog, accept_task)

        def _open_ticket_development_dialog(self) -> None:
            board_titles = self._available_board_titles()
            try:
                script_path = _write_interactive_ticket_development_script(
                    self.active_config,
                    board_titles=board_titles,
                    phase_order=list(self.phase_order),
                    taskbot_entry=taskbot_entry,
                )
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Prepare Ticket Developer", str(exc))
                return

            ok, error_text = _launch_terminal_command(
                Path(self.active_config["repo_root"]),
                shlex.quote(str(script_path)),
            )
            if not ok:
                QMessageBox.critical(self, "Failed To Open Ticket Developer", error_text)
                return
            self.status_note = "Opened interactive ticket developer"
            self.refresh_view()

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
                self._persist_session(Path(active_config["repo_root"]))
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
                self._persist_session(Path(active_config["repo_root"]))
                self._activate_repo_config(Path(active_config["repo_root"]))
                self.refresh_view()

            self._show_modeless_dialog(dialog, accept_task_update)

        def _delete_task(self, task: StoredTask) -> None:
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
            self._persist_session()
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
            self._persist_session()
            self._activate_repo_config(Path(self.active_config["repo_root"]))
            QTimer.singleShot(0, self.refresh_view)

        def _complete_task(self, task: StoredTask) -> None:
            self._move_task_to_phase(task, "completed")

        def _needs_testing_task(self, task: StoredTask) -> None:
            self._move_task_to_phase(task, "needs_testing")

        def _approve_needs_testing_task(self, task: StoredTask) -> None:
            self._move_task_to_phase(task, "completed")

        def _reject_needs_testing_task(self, task: StoredTask) -> None:
            dialog = TestingFeedbackDialog(task, self)
            if dialog.exec() != QDialog.Accepted:
                return

            failure_note = "Testing feedback ({0}):\n{1}".format(_now_timestamp_label(), dialog.feedback_notes())
            existing_context = task.context_notes.strip()
            updated_context = failure_note if not existing_context else "{0}\n\n{1}".format(existing_context, failure_note)

            try:
                updated = edit_task(
                    self.active_config,
                    task.task_id,
                    board_title=task.board_title,
                    title=task.title,
                    context_notes=updated_context,
                    phase="backlog",
                )
            except Exception as exc:
                QMessageBox.critical(self, "Failed To Update Task", str(exc))
                return
            if updated is None:
                QMessageBox.critical(self, "Failed To Update Task", "Task could not be found.")
                return

            self.selected_board_id = updated.board_id
            self.status_note = "Returned task {0} to Backlog with testing feedback".format(updated.task_id)
            self._persist_session()
            self._activate_repo_config(Path(self.active_config["repo_root"]))
            self.refresh_view()

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
            self._persist_session()
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

        def _on_board_search_changed(self, value: str) -> None:
            self.board_search_query = value.strip().lower()
            self.refresh_view()

        def _reset_board_search(self) -> None:
            self.board_search_query = ""
            self.board_search_input.blockSignals(True)
            self.board_search_input.clear()
            self.board_search_input.blockSignals(False)

        def _on_board_selection_changed(self,
                                        current: QListWidgetItem | None,
                                        _previous: QListWidgetItem | None) -> None:
            self.selected_board_id = current.data(Qt.UserRole) if current is not None else None
            self._persist_session()
            self.refresh_view()

        def _update_status_header(self,
                                  boards: List[Dict[str, Any]],
                                  tasks: List[StoredTask],
                                  runtime_payload: Dict[str, Any]) -> None:
            runner_phase = str(runtime_payload.get("phase", "idle")).strip() or "idle"
            stop_requested = self._stop_path().exists() or self.status_note.lower() == "stop requested"
            note_html = html.escape(self.status_note)
            if stop_requested:
                note_html = '<span style="color:{0}; font-weight:700;">Stop Requested</span>'.format(
                    self.theme_values["stop_requested"]
                )
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
                self.board_title_label.setText(
                    _board_header_title(
                        selected_title,
                        self._selected_board_task_count() or len(tasks),
                    )
                )
                self.board_summary_label.setText(
                    _board_summary_text(tasks, self.phase_order)
                )
            else:
                active_tasks = self._active_tasks(tasks)
                self.board_title_label.setText(
                    _board_header_title("All Boards", len(active_tasks))
                )
                self.board_summary_label.setText(
                    _board_summary_text(
                        active_tasks,
                        self.phase_order,
                        board_count=len(boards),
                    )
                )

        def _refresh_columns(self, tasks: List[StoredTask]) -> None:
            horizontal_value = self.columns_scroll.horizontalScrollBar().value()
            column_scroll_values = self._capture_phase_column_scroll_state()
            visible_tasks = self._visible_board_tasks(tasks)

            for phase in self.phase_order:
                phase_tasks = _sort_board_phase_tasks([task for task in visible_tasks if task.phase == phase])
                self.phase_columns[phase].set_tasks(
                    phase_tasks,
                    phase_order=self.phase_order,
                    on_start_task=self._start_task,
                    on_approve_testing=self._approve_needs_testing_task,
                    on_reject_testing=self._reject_needs_testing_task,
                    on_edit_task=self._open_edit_task_dialog,
                    on_delete_task=self._delete_task,
                    on_move_task=self._move_task_to_phase,
                    on_move_task_to_board=self._move_task_to_board,
                )
            QTimer.singleShot(
                0,
                lambda: self._restore_columns_scroll_state(
                    horizontal_value,
                    column_scroll_values,
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
                text = read_terminal_tail(path, tail_lines)
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
            self._refresh_repo_action_buttons()
            self.branch_dropdown.setEnabled(self._git_repo_available and not self._runner_active())

            boards = self._cached_boards
            tasks = self._cached_tasks
            runtime_payload = self._cached_runtime_payload
            self._maybe_prompt_for_approval_request(runtime_payload)
            self._update_status_header(boards, tasks, runtime_payload)
            if store_changed:
                self._populate_board_list(boards, tasks)

            selection_changed = self.selected_board_id != self._last_rendered_selected_board_id
            filter_changed = self.board_search_query != self._last_rendered_board_search_query
            if store_changed or selection_changed or filter_changed:
                visible_tasks = self._visible_board_tasks(tasks)
                self._update_board_header(visible_tasks, boards)
                self._refresh_columns(tasks)
                self._last_rendered_selected_board_id = self.selected_board_id
                self._last_rendered_board_search_query = self.board_search_query
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
