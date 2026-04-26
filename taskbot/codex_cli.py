from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from taskbot.terminal_stream import append_terminal_log


@dataclass
class CodexRunResult:
    command: List[str]
    exit_code: int
    stdout: str
    stderr: str
    last_message_text: str
    parsed_output: Optional[Dict[str, Any]]
    json_events: List[Dict[str, Any]]


ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"
ANSI_BRIGHT_BLACK = "\033[90m"


def _load_json_if_possible(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _parse_jsonl(stdout: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _ansi_enabled(mode: str, stream: Any) -> bool:
    cleaned = (mode or "auto").strip().lower()
    if cleaned == "always":
        return True
    if cleaned == "never":
        return False
    if os.getenv("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _log_ansi_enabled(config: Dict[str, Any], mode: str) -> bool:
    cleaned = (mode or "auto").strip().lower()
    if cleaned == "always":
        return True
    if cleaned == "never":
        return False
    return bool(config.get("ui", {}).get("terminal_ansi", True))


def _style(text: str, *codes: str, enabled: bool) -> str:
    if not enabled or not codes:
        return text
    return "{0}{1}{2}".format("".join(codes), text, ANSI_RESET)


def _phase_prefix(phase_name: str, enabled: bool) -> str:
    colour = ANSI_MAGENTA if phase_name == "plan" else ANSI_BLUE if phase_name == "implement" else ANSI_CYAN
    return _style("[{0}]".format(phase_name), ANSI_BOLD, colour, enabled=enabled)


def _indent_block(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else prefix.rstrip() for line in text.splitlines())


def _format_agent_message(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    return json.dumps(parsed, indent=2, ensure_ascii=False)


def _render_json_event(phase_name: str,
                       event: Dict[str, Any],
                       stream_commands: bool,
                       ansi_enabled: bool) -> Optional[str]:
    event_type = str(event.get("type", ""))
    prefix = _phase_prefix(phase_name, ansi_enabled)

    if event_type == "thread.started":
        return "{0} {1}".format(prefix, _style("session started", ANSI_DIM, enabled=ansi_enabled))
    if event_type == "turn.started":
        return "{0} {1}".format(prefix, _style("turn started", ANSI_DIM, enabled=ansi_enabled))
    if event_type == "turn.completed":
        usage = event.get("usage", {})
        if isinstance(usage, dict):
            return "{0} {1} | input={2} cached={3} output={4}".format(
                prefix,
                _style("turn completed", ANSI_GREEN, enabled=ansi_enabled),
                usage.get("input_tokens", 0),
                usage.get("cached_input_tokens", 0),
                usage.get("output_tokens", 0),
            )
        return "{0} {1}".format(prefix, _style("turn completed", ANSI_GREEN, enabled=ansi_enabled))
    if event_type == "error":
        return "{0} {1}: {2}".format(
            prefix,
            _style("error", ANSI_BOLD, ANSI_RED, enabled=ansi_enabled),
            str(event.get("message", "")).strip(),
        )
    if event_type == "turn.failed":
        error_payload = event.get("error", {})
        if isinstance(error_payload, dict):
            return "{0} {1}: {2}".format(
                prefix,
                _style("failed", ANSI_BOLD, ANSI_RED, enabled=ansi_enabled),
                str(error_payload.get("message", "")).strip(),
            )
        return "{0} {1}".format(prefix, _style("failed", ANSI_BOLD, ANSI_RED, enabled=ansi_enabled))

    item = event.get("item", {})
    if not isinstance(item, dict):
        return None

    item_type = str(item.get("type", ""))
    if event_type == "item.updated" and item_type == "todo_list":
        items = item.get("items", [])
        if not isinstance(items, list):
            return None
        completed = sum(1 for entry in items if isinstance(entry, dict) and entry.get("completed"))
        total = len(items)
        summaries = [
            str(entry.get("text", "")).strip()
            for entry in items
            if isinstance(entry, dict) and not entry.get("completed") and str(entry.get("text", "")).strip()
        ]
        summary = "; ".join(summaries[:2])
        if summary:
            return "{0} {1} {2}/{3} | next: {4}".format(
                prefix,
                _style("todo", ANSI_YELLOW, enabled=ansi_enabled),
                completed,
                total,
                summary,
            )
        return "{0} {1} {2}/{3}".format(prefix, _style("todo", ANSI_YELLOW, enabled=ansi_enabled), completed, total)

    if item_type == "command_execution":
        command = str(item.get("command", "")).strip()
        if event_type == "item.started" and stream_commands:
            return "{0} {1} {2}".format(
                prefix,
                _style("cmd>", ANSI_CYAN, enabled=ansi_enabled),
                _style(command, ANSI_BRIGHT_BLACK, enabled=ansi_enabled),
            )
        if event_type == "item.completed":
            exit_code = item.get("exit_code")
            if exit_code not in (0, None):
                return "{0} {1} ({2}): {3}".format(
                    prefix,
                    _style("cmd failed", ANSI_BOLD, ANSI_RED, enabled=ansi_enabled),
                    exit_code,
                    command,
                )
        return None

    if item_type == "agent_message" and event_type == "item.completed":
        text = _format_agent_message(str(item.get("text", "")))
        if not text:
            return None
        label = _style("agent output", ANSI_BOLD, ANSI_GREEN, enabled=ansi_enabled)
        return "{0} {1}\n{2}".format(prefix, label, _indent_block(text, "  "))

    return None


def _terminate_process(process: subprocess.Popen[Any], timeout_seconds: float) -> int:
    if process.poll() is not None:
        return int(process.returncode or 0)

    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass

    try:
        return int(process.wait(timeout=timeout_seconds))
    except subprocess.TimeoutExpired:
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        return int(process.wait())


def run_codex_exec(repo_root: Path,
                   config: Dict[str, Any],
                   *,
                   model: str,
                   reasoning_effort: Optional[str] = None,
                   prompt: str,
                   artifact_dir: Path,
                   phase_name: str,
                   output_schema: Optional[Path] = None,
                   on_process_started: Optional[Callable[[subprocess.Popen[Any], List[str]], None]] = None,
                   on_process_finished: Optional[Callable[[], None]] = None,
                   should_terminate: Optional[Callable[[], bool]] = None,
                   process_termination_timeout: float = 5.0) -> CodexRunResult:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifact_dir / "{0}.stdout.log".format(phase_name)
    stderr_path = artifact_dir / "{0}.stderr.log".format(phase_name)
    last_message_path = artifact_dir / "{0}.last_message.json".format(phase_name)

    codex_config = config["codex"]
    stream_output = bool(codex_config.get("stream_output", True))
    stream_commands = bool(codex_config.get("stream_commands", True))
    stream_stderr = bool(codex_config.get("stream_stderr", False))
    stream_ansi_mode = str(codex_config.get("stream_ansi", "auto"))
    ansi_stdout = _ansi_enabled(stream_ansi_mode, sys.stdout)
    ansi_stderr = _ansi_enabled(stream_ansi_mode, sys.stderr)
    ansi_log = _log_ansi_enabled(config, stream_ansi_mode)
    command = [
        "codex",
        "-a",
        str(codex_config.get("ask_for_approval", "never")),
    ]
    cleaned_reasoning_effort = "" if reasoning_effort is None else str(reasoning_effort).strip()
    if cleaned_reasoning_effort:
        command.extend(["-c", "model_reasoning_effort={0}".format(cleaned_reasoning_effort)])
    command.extend(
        [
            "exec",
            "-",
            "-C",
            str(repo_root),
            "-m",
            model,
            "--sandbox",
            str(codex_config["sandbox"]),
            "--color",
            str(codex_config["color"]),
            "--json",
            "-o",
            str(last_message_path),
        ]
    )

    if codex_config.get("ephemeral", True):
        command.append("--ephemeral")
    if codex_config.get("skip_git_repo_check", True):
        command.append("--skip-git-repo-check")
    if output_schema is not None:
        command.extend(["--output-schema", str(output_schema)])

    stdout_lines: List[str] = []
    stderr_lines: List[str] = []
    cache_root = Path(config["state_dir"]) / "tool-cache"
    pycache_root = cache_root / "pycache"
    xdg_cache_root = cache_root / "xdg"
    pycache_root.mkdir(parents=True, exist_ok=True)
    xdg_cache_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("PYTHONPYCACHEPREFIX", str(pycache_root))
    env.setdefault("XDG_CACHE_HOME", str(xdg_cache_root))

    process = subprocess.Popen(
        command,
        cwd=repo_root,
        env=env,
        stdin=subprocess.PIPE,
        start_new_session=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )

    try:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("failed to open Codex subprocess pipes")

        if on_process_started is not None:
            on_process_started(process, list(command))

        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
            process.stdin.write(prompt)
            process.stdin.close()

            def drain_stdout() -> None:
                for line in process.stdout:
                    stdout_lines.append(line)
                    stdout_handle.write(line)
                    stdout_handle.flush()
                    if stream_output:
                        rendered_log = None
                        rendered_stdout = None
                        stripped = line.strip()
                        if stripped.startswith("{") and stripped.endswith("}"):
                            try:
                                parsed = json.loads(stripped)
                            except json.JSONDecodeError:
                                rendered_log = "{0} {1}".format(_phase_prefix(phase_name, ansi_log), stripped)
                                rendered_stdout = "{0} {1}".format(_phase_prefix(phase_name, ansi_stdout), stripped)
                            else:
                                if isinstance(parsed, dict):
                                    rendered_log = _render_json_event(phase_name, parsed, stream_commands, ansi_log)
                                    rendered_stdout = _render_json_event(phase_name, parsed, stream_commands, ansi_stdout)
                        elif stripped:
                            rendered_log = "{0} {1}".format(_phase_prefix(phase_name, ansi_log), stripped)
                            rendered_stdout = "{0} {1}".format(_phase_prefix(phase_name, ansi_stdout), stripped)
                        if rendered_log:
                            append_terminal_log(config, rendered_log)
                        if rendered_stdout:
                            print(rendered_stdout, flush=True)

            def drain_stderr() -> None:
                for line in process.stderr:
                    stderr_lines.append(line)
                    stderr_handle.write(line)
                    stderr_handle.flush()
                    if stream_output and stream_stderr:
                        log_prefix = _style("[{0}:stderr]".format(phase_name), ANSI_BOLD, ANSI_RED, enabled=ansi_log)
                        stderr_prefix = _style("[{0}:stderr]".format(phase_name), ANSI_BOLD, ANSI_RED, enabled=ansi_stderr)
                        append_terminal_log(config, "{0} {1}".format(log_prefix, line.rstrip()))
                        print("{0} {1}".format(stderr_prefix, line.rstrip()), file=sys.stderr, flush=True)

            stdout_thread = threading.Thread(target=drain_stdout, name="taskbot-codex-stdout", daemon=True)
            stderr_thread = threading.Thread(target=drain_stderr, name="taskbot-codex-stderr", daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            try:
                if should_terminate is not None and should_terminate():
                    exit_code = _terminate_process(process, process_termination_timeout)
                else:
                    while True:
                        try:
                            exit_code = int(process.wait(timeout=0.5))
                            break
                        except subprocess.TimeoutExpired:
                            if should_terminate is not None and should_terminate():
                                exit_code = _terminate_process(process, process_termination_timeout)
                                break
            finally:
                stdout_thread.join()
                stderr_thread.join()
    except Exception:
        if process.poll() is None:
            _terminate_process(process, process_termination_timeout)
        raise
    finally:
        if on_process_finished is not None:
            on_process_finished()

    completed_stdout = "".join(stdout_lines)
    completed_stderr = "".join(stderr_lines)
    last_message_text = last_message_path.read_text(encoding="utf-8") if last_message_path.exists() else ""

    return CodexRunResult(
        command=command,
        exit_code=exit_code,
        stdout=completed_stdout,
        stderr=completed_stderr,
        last_message_text=last_message_text,
        parsed_output=_load_json_if_possible(last_message_path),
        json_events=_parse_jsonl(completed_stdout),
    )
