from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from taskbot.codex_cli import CodexRunResult, run_codex_exec
from taskbot.config import discover_config_path, ensure_runtime_directories, load_config
from taskbot.git_integration import capture_git_session_state, publish_git_changes
from taskbot.indexer import build_repo_index, rank_files_for_task
from taskbot.prompts import build_implementation_prompt, build_plan_prompt
from taskbot.store import (
    StoredTask,
    apply_task_decomposition,
    apply_plan_result,
    create_task,
    ensure_task_store,
    list_store_tasks,
    phase_labels,
    select_next_task,
    store_path,
    sync_markdown_into_store,
    update_task_phase,
)
from taskbot.terminal_stream import append_terminal_log, format_terminal_header, terminal_log_path
from taskbot.verification import VerificationResult, run_verification_steps


APP_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = APP_ROOT / "schemas"
TINY_TASK_TEXT_HINTS = (
    "ui",
    "tooltip",
    "style",
    "stylesheet",
    "css",
    "spacing",
    "padding",
    "margin",
    "icon",
    "hover",
    "label",
    "copy",
    "text",
    "title",
    "dialog",
    "modal",
    "button",
    "card",
    "splitter",
    "width",
    "height",
    "size",
    "sizing",
    "layout",
    "color",
)
TINY_TASK_PATH_HINTS = ("ui", "view", "dialog", "widget", "theme", "style", "stylesheet", "css", "qml")
TINY_TASK_NEGATIVE_HINTS = (
    "refactor",
    "architecture",
    "migration",
    "database",
    "auth",
    "permission system",
    "task store",
    "indexer",
    "schema",
    "subtask",
    "subagent",
)
TINY_TASK_PLANNING_INTENT_PATTERNS = (
    re.compile(r"\btoo large\b"),
    re.compile(r"\blarge task\b"),
    re.compile(r"\bbig task\b"),
    re.compile(r"\bmulti[- ]step\b"),
    re.compile(r"\bdecompose(?:d|s|ing)?\b"),
    re.compile(r"\bdecomposition\b"),
    re.compile(r"\bneeds splitting\b"),
    re.compile(r"\bsplit(?:ting)?\s+into\s+(?:separate\s+|smaller\s+)?(?:sub)?tasks?\b"),
    re.compile(r"\bbreak(?:ing)?\s+into\s+(?:separate\s+|smaller\s+)?(?:sub)?tasks?\b"),
)


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _resolve_cli_path(value: Optional[str]) -> Optional[Path]:
    if value is None or not str(value).strip():
        return None
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path.cwd() / path).resolve()


ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


def _stdout_ansi_enabled(config: Dict[str, Any]) -> bool:
    mode = str(config.get("codex", {}).get("stream_ansi", "auto")).strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.getenv("NO_COLOR")


def _log_ansi_enabled(config: Dict[str, Any]) -> bool:
    mode = str(config.get("codex", {}).get("stream_ansi", "auto")).strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    return bool(config.get("ui", {}).get("terminal_ansi", True))


def _strip_ansi(text: str) -> str:
    return ANSI_PATTERN.sub("", text)


def _banner(config: Dict[str, Any], label: str, text: str) -> str:
    if not _log_ansi_enabled(config):
        return "[taskbot] {0} {1}".format(label, text)
    return "\033[1;35m[taskbot]\033[0m \033[1;36m{0}\033[0m {1}".format(label, text)


def _emit_line(config: Dict[str, Any], text: str, *, stderr: bool = False) -> None:
    append_terminal_log(config, text)
    printable = text if _stdout_ansi_enabled(config) else _strip_ansi(text)
    if stderr:
        print(printable, file=sys.stderr, flush=True)
    else:
        print(printable, flush=True)


def _emit_header(config: Dict[str, Any], title: str, details: List[str]) -> None:
    _emit_line(config, format_terminal_header(title, details))


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def _task_summary(task: StoredTask) -> str:
    return "{0} [{1}] {2}".format(task.board_title, task.phase, task.title)


def _verification_mode(config: Dict[str, Any]) -> str:
    verification = config.get("verification", {})
    mode = str(verification.get("mode", "auto")).strip().lower()
    if mode in {"manual", "commands"}:
        return mode
    commands = verification.get("commands", [])
    has_commands = any(isinstance(entry, dict) and entry.get("enabled", True) for entry in commands)
    return "commands" if has_commands else "manual"


def _enabled_verification_commands(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    commands = config.get("verification", {}).get("commands", [])
    return [entry for entry in commands if isinstance(entry, dict) and entry.get("enabled", True)]


def _unique_strings(values: List[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for value in values:
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ordered


def _tiny_task_relevant_files(task: StoredTask,
                              file_hints: List[Any],
                              *,
                              limit: Optional[int] = None) -> List[str]:
    relevant_files = _unique_strings(
        list(task.file_targets) + [
            str(item[0])
            for item in file_hints
            if isinstance(item, (list, tuple)) and item
        ]
    )
    if limit is None:
        return relevant_files
    return relevant_files[:limit]


def _contains_tiny_task_planning_intent(text: str) -> bool:
    return any(pattern.search(text) for pattern in TINY_TASK_PLANNING_INTENT_PATTERNS)


def _configured_reasoning_effort(config: Dict[str, Any], role: str) -> Optional[str]:
    models = config.get("models", {})
    if not isinstance(models, dict):
        return None
    configured_value = models.get("{0}_reasoning_effort".format(role))
    if configured_value is None:
        return None
    cleaned = str(configured_value).strip()
    return cleaned or None


def _should_fast_path_tiny_task(task: StoredTask,
                                file_hints: List[Any],
                                config: Dict[str, Any]) -> bool:
    planning_config = config.get("planning", {})
    if not bool(planning_config.get("auto_plan_tiny_tasks", True)):
        return False

    text = "{0}\n{1}".format(task.title, task.context_notes).lower()
    if not text.strip():
        return False
    if _contains_tiny_task_planning_intent(text):
        return False
    if any(token in text for token in TINY_TASK_NEGATIVE_HINTS):
        return False
    if len(task.acceptance) > 2:
        return False

    relevant_files = _tiny_task_relevant_files(task, file_hints)
    if not relevant_files or len(relevant_files) > 3:
        return False

    text_hits = sum(1 for token in TINY_TASK_TEXT_HINTS if token in text)
    path_hits = sum(1 for path in relevant_files if any(token in path.lower() for token in TINY_TASK_PATH_HINTS))
    if text_hits == 0 and path_hits == 0:
        return False

    if len(text) > 420 and path_hits == 0:
        return False
    return True


def _build_tiny_task_plan(task: StoredTask,
                          file_hints: List[Any],
                          config: Dict[str, Any]) -> Dict[str, Any]:
    relevant_files = _tiny_task_relevant_files(task, file_hints, limit=3)
    verification_mode = _verification_mode(config)
    verification_lines: List[str] = []
    if verification_mode == "manual" or not _enabled_verification_commands(config):
        verification_lines.append(
            "Manual verification repo: avoid speculative automated test retries and leave concise follow-up steps if runtime confirmation is still needed."
        )
    else:
        for entry in _enabled_verification_commands(config):
            command = " ".join(str(part) for part in entry.get("command", []))
            verification_lines.append("Outer runner will execute {0}: `{1}`".format(entry.get("name", "check"), command))
    instructions = str(config.get("verification", {}).get("instructions", "") or "").strip()
    if instructions:
        verification_lines.append(instructions)

    file_summary = ", ".join(relevant_files[:3]) if relevant_files else "the top-ranked UI files"
    return {
        "summary": (
            "This looks like a small, localised task, so taskbot can skip a separate planning pass and implement "
            "directly against {0}."
        ).format(file_summary),
        "constraints": [
            "Keep the change tightly scoped to the most relevant files.",
            "Preserve existing behaviour outside the requested tweak.",
            "Prefer needs_testing over completed when the configured verification policy is manual.",
        ],
        "relevant_files": relevant_files,
        "steps": [
            {
                "title": "Inspect the most likely files only",
                "details": "Read just the top-ranked files and confirm the exact local change needed before editing.",
                "files": relevant_files,
                "parallelisable": False,
            },
            {
                "title": "Implement the smallest viable change",
                "details": "Modify only the targeted code paths needed for this task and avoid broad refactors.",
                "files": relevant_files,
                "parallelisable": False,
            },
            {
                "title": "Validate using the configured verification policy",
                "details": "Use configured verification commands when present; otherwise leave precise manual follow-up notes instead of repeated speculative test attempts.",
                "files": relevant_files,
                "parallelisable": False,
            },
        ],
        "verification": verification_lines,
        "subagent_splits": [],
        "decomposition": {
            "should_split": False,
            "reason": "Task qualifies for the tiny-task fast path.",
            "subtasks": [],
        },
    }


def _read_recent_history(config: Dict[str, Any], task: StoredTask) -> List[Dict[str, Any]]:
    history_path = Path(config["state_dir"]) / "history.jsonl"
    if not history_path.exists():
        return []

    items: List[Dict[str, Any]] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("task_id") == task.task_id:
            items.append(payload)

    limit = int(config["context"]["max_history_items"])
    return items[-limit:]


def _append_history(config: Dict[str, Any], payload: Dict[str, Any]) -> None:
    history_path = Path(config["state_dir"]) / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _runtime_path(config: Dict[str, Any]) -> Path:
    return Path(config["control_dir"]) / "runtime.json"


def _stop_path(config: Dict[str, Any]) -> Path:
    return Path(config["control_dir"]) / "stop"


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_runtime_lock(config: Dict[str, Any], command_name: str) -> None:
    runtime_path = _runtime_path(config)
    if runtime_path.exists():
        try:
            payload = _load_json(runtime_path)
        except Exception:
            payload = {}
        pid = int(payload.get("pid", 0) or 0)
        if pid > 0 and _pid_is_running(pid):
            raise RuntimeError("taskbot is already running with pid {0}".format(pid))

    _write_json(
        runtime_path,
        {
            "pid": os.getpid(),
            "command": command_name,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "phase": "starting",
        },
    )


def _update_runtime(config: Dict[str, Any], payload: Dict[str, Any]) -> None:
    runtime_path = _runtime_path(config)
    existing = {}
    if runtime_path.exists():
        try:
            existing = _load_json(runtime_path)
        except Exception:
            existing = {}
    existing.update(payload)
    _write_json(runtime_path, existing)


def _clear_runtime_field(config: Dict[str, Any], field_name: str) -> None:
    runtime_path = _runtime_path(config)
    if not runtime_path.exists():
        return

    try:
        existing = _load_json(runtime_path)
    except Exception:
        return
    if not isinstance(existing, dict) or field_name not in existing:
        return

    existing.pop(field_name, None)
    if existing:
        _write_json(runtime_path, existing)
    else:
        runtime_path.unlink()


def _clear_runtime(config: Dict[str, Any]) -> None:
    runtime_path = _runtime_path(config)
    if runtime_path.exists():
        runtime_path.unlink()


def _stop_requested(config: Dict[str, Any]) -> bool:
    return _stop_path(config).exists()


def _request_stop(config: Dict[str, Any]) -> None:
    stop_path = _stop_path(config)
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path.write_text("stop requested\n", encoding="utf-8")


def _clear_stop(config: Dict[str, Any]) -> None:
    stop_path = _stop_path(config)
    if stop_path.exists():
        stop_path.unlink()


def _artifact_dir(config: Dict[str, Any], task: StoredTask) -> Path:
    safe_id = task.task_id.replace("/", "-")
    return Path(config["artifact_dir"]) / "{0}-{1}".format(_now_stamp(), safe_id)


def _select_task(config: Dict[str, Any],
                 *,
                 task_id: Optional[str],
                 include_needs_testing: bool,
                 text_query: Optional[str]) -> Optional[StoredTask]:
    ensure_task_store(config)
    return select_next_task(
        config,
        include_needs_testing=include_needs_testing,
        task_id=task_id,
        text_query=text_query,
    )


def _write_run_snapshot(artifact_dir: Path,
                        task: StoredTask,
                        file_hints: List[Any],
                        history: List[Dict[str, Any]]) -> None:
    payload = {
        "task_id": task.task_id,
        "board": task.board_title,
        "text": task.title,
        "phase": task.phase,
        "context_notes": task.context_notes,
        "file_targets": list(task.file_targets),
        "acceptance": list(task.acceptance),
        "plan_status": task.plan_status,
        "plan": dict(task.plan),
        "file_hints": file_hints,
        "history": history,
    }
    _write_json(artifact_dir / "task.snapshot.json", payload)


def _validate_phase_result(phase: str, result: CodexRunResult) -> None:
    if result.exit_code != 0:
        message = ""
        for event in result.json_events:
            if event.get("type") == "error":
                message = str(event.get("message", "")).strip()
                break
            if event.get("type") == "turn.failed":
                error_payload = event.get("error", {})
                if isinstance(error_payload, dict):
                    message = str(error_payload.get("message", "")).strip()
                    break

        detail = " See {0}.stdout.log and {0}.stderr.log".format(phase)
        if message:
            detail += ". Error: {0}".format(message)
        raise RuntimeError(
            "codex {0} phase failed with exit code {1}.{2}".format(
                phase,
                result.exit_code,
                detail,
            )
        )
    if result.parsed_output is None:
        raise RuntimeError("codex {0} phase did not emit valid JSON".format(phase))


def _run_codex_phase(config: Dict[str, Any],
                     repo_root: Path,
                     *,
                     model: str,
                     reasoning_effort: Optional[str],
                     prompt: str,
                     artifact_dir: Path,
                     phase_name: str,
                     output_schema: Optional[Path],
                     interrupt_state: Optional[Dict[str, bool]] = None) -> CodexRunResult:
    def on_process_started(process: subprocess.Popen[Any], command: List[str]) -> None:
        _update_runtime(
            config,
            {
                "active_session": {
                    "phase": phase_name,
                    "pid": process.pid,
                    "command": list(command),
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                }
            },
        )

    def on_process_finished() -> None:
        _clear_runtime_field(config, "active_session")

    def should_terminate() -> bool:
        return bool(interrupt_state and interrupt_state.get("requested"))

    return run_codex_exec(
        repo_root,
        config,
        model=model,
        reasoning_effort=reasoning_effort,
        prompt=prompt,
        artifact_dir=artifact_dir,
        phase_name=phase_name,
        output_schema=output_schema,
        on_process_started=on_process_started,
        on_process_finished=on_process_finished,
        should_terminate=should_terminate,
    )


def _verification_summary(results: List[VerificationResult]) -> Dict[str, Any]:
    return {
        "all_passed": all(result.exit_code == 0 for result in results),
        "results": [result.__dict__ for result in results],
    }


def _log_git_result(config: Dict[str, Any], result: Dict[str, Any]) -> None:
    status = str(result.get("status", "unknown")).strip() or "unknown"
    branch = str(result.get("branch", "")).strip()
    commit_sha = str(result.get("commit_sha", "")).strip()
    reason = str(result.get("reason", "")).strip()
    message_parts = ["status={0}".format(status)]
    if branch:
        message_parts.append("branch={0}".format(branch))
    if commit_sha:
        message_parts.append("commit={0}".format(commit_sha[:12]))
    if reason:
        message_parts.append(reason)
    _emit_line(config, _banner(config, "git", " | ".join(message_parts)))


def _run_plan_for_task(config: Dict[str, Any],
                       task: StoredTask,
                       *,
                       rebuild_index: bool = False,
                       allow_fast_path: bool = False,
                       interrupt_state: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    repo_root = Path(config["repo_root"])
    artifact_dir = _artifact_dir(config, task)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    planner_model = str(config["models"]["planner"])
    refreshed_task = update_task_phase(
        config,
        task.task_id,
        "planning",
        artifact_dir=str(artifact_dir),
        last_result_status="planning",
        last_summary=task.last_summary,
        last_error="",
    ) or task
    _emit_header(
        config,
        "planning",
        [
            "task_id={0}".format(task.task_id),
            "model={0}".format(planner_model),
        ],
    )
    index = build_repo_index(repo_root, config, rebuild=rebuild_index)
    file_hints = rank_files_for_task(
        index,
        refreshed_task.title + "\n" + refreshed_task.context_notes,
        refreshed_task.board_title,
        limit=int(config["context"]["plan_file_limit"]),
    )
    history = _read_recent_history(config, refreshed_task)
    _write_run_snapshot(artifact_dir, refreshed_task, file_hints, history)
    _update_runtime(
        config,
        {
            "phase": "planning",
            "task_id": refreshed_task.task_id,
            "task_summary": _task_summary(refreshed_task),
            "artifact_dir": str(artifact_dir),
        },
    )

    if allow_fast_path and _should_fast_path_tiny_task(refreshed_task, file_hints, config):
        plan_payload = _build_tiny_task_plan(refreshed_task, file_hints, config)
        _write_json(artifact_dir / "plan.result.json", plan_payload)
        planned_task = apply_plan_result(
            config,
            refreshed_task.task_id,
            plan_payload=plan_payload,
            artifact_dir=str(artifact_dir),
        ) or refreshed_task
        _emit_line(config, _banner(config, "planning", "{0} | tiny-task fast path".format(task.task_id)))
        return {
            "artifact_dir": artifact_dir,
            "plan": plan_payload,
            "file_hints": file_hints,
            "history": history,
            "task": planned_task,
            "split": False,
            "subtasks": [],
            "auto_planned": True,
        }

    prompt = build_plan_prompt(repo_root, config, refreshed_task, file_hints, history)
    plan_result = _run_codex_phase(
        config,
        repo_root,
        model=planner_model,
        reasoning_effort=_configured_reasoning_effort(config, "planner"),
        prompt=prompt,
        artifact_dir=artifact_dir,
        phase_name="plan",
        output_schema=SCHEMA_ROOT / "plan.schema.json",
        interrupt_state=interrupt_state,
    )
    _validate_phase_result("plan", plan_result)
    plan_payload = plan_result.parsed_output or {}
    _write_json(artifact_dir / "plan.result.json", plan_payload)
    decomposition = plan_payload.get("decomposition", {})
    should_split = bool(isinstance(decomposition, dict) and decomposition.get("should_split"))
    if should_split:
        split_result = apply_task_decomposition(
            config,
            refreshed_task.task_id,
            plan_payload=plan_payload,
            artifact_dir=str(artifact_dir),
        )
        return {
            "artifact_dir": artifact_dir,
            "plan": plan_payload,
            "file_hints": file_hints,
            "history": history,
            "task": split_result["parent_task"],
            "split": True,
            "subtasks": split_result["subtasks"],
        }

    planned_task = apply_plan_result(
        config,
        refreshed_task.task_id,
        plan_payload=plan_payload,
        artifact_dir=str(artifact_dir),
    ) or refreshed_task
    return {
        "artifact_dir": artifact_dir,
        "plan": plan_payload,
        "file_hints": file_hints,
        "history": history,
        "task": planned_task,
        "split": False,
        "subtasks": [],
        "auto_planned": False,
    }


def _run_task_once(config: Dict[str, Any],
                   task: StoredTask,
                   *,
                   rebuild_index: bool = False,
                   interrupt_state: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    needs_planning = task.needs_planning()
    planner_model = str(config["models"]["planner"])
    implementer_model = str(config["models"]["implementer"])
    task_start_model = planner_model if needs_planning else implementer_model
    _emit_header(
        config,
        "task start",
        [
            "task_id={0}".format(task.task_id),
            "title={0}".format(task.title),
            "model={0}".format(task_start_model),
        ],
    )
    if needs_planning:
        plan_context = _run_plan_for_task(
            config,
            task,
            rebuild_index=rebuild_index,
            allow_fast_path=True,
            interrupt_state=interrupt_state,
        )
        if plan_context.get("split"):
            subtasks: List[StoredTask] = plan_context.get("subtasks", [])
            decomposition = plan_context["plan"].get("decomposition", {})
            summary_text = str(decomposition.get("reason", "")).strip()
            if not summary_text:
                summary_text = "Split into {0} planned subtasks".format(len(subtasks))
            summary = {
                "task_id": task.task_id,
                "status": "split",
                "summary": summary_text,
                "mark_task_result": "created {0} planned subtasks".format(len(subtasks)),
                "verification": {"all_passed": True, "results": []},
                "artifact_dir": str(plan_context["artifact_dir"]),
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "subtasks": [
                    {
                        "task_id": item.task_id,
                        "board_title": item.board_title,
                        "title": item.title,
                        "phase": item.phase,
                    }
                    for item in subtasks
                ],
            }
            _append_history(config, summary)
            _write_json(Path(plan_context["artifact_dir"]) / "run.summary.json", summary)
            _emit_line(
                config,
                _banner(
                    config,
                    "split",
                    "{0} subtasks created from {1}".format(len(subtasks), task.task_id),
                ),
            )
            return summary

        task = plan_context.get("task", task)
        if not plan_context.get("auto_planned"):
            summary = {
                "task_id": task.task_id,
                "status": "planned",
                "summary": str(plan_context["plan"].get("summary", "")),
                "mark_task_result": "ready",
                "verification": {"all_passed": True, "results": []},
                "artifact_dir": str(plan_context["artifact_dir"]),
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }
            _append_history(config, summary)
            _write_json(Path(plan_context["artifact_dir"]) / "run.summary.json", summary)
            return summary

    repo_root = Path(config["repo_root"])
    artifact_dir = _artifact_dir(config, task)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    index = build_repo_index(repo_root, config, rebuild=rebuild_index)
    file_hints = rank_files_for_task(
        index,
        task.title + "\n" + task.context_notes,
        task.board_title,
        limit=int(config["context"]["implementation_file_limit"]),
    )
    history = _read_recent_history(config, task)
    _write_run_snapshot(artifact_dir, task, file_hints, history)
    git_session = capture_git_session_state(repo_root, config)

    active_task = update_task_phase(
        config,
        task.task_id,
        "in_progress",
        artifact_dir=str(artifact_dir),
        last_result_status="running",
        last_summary=task.last_summary,
        last_error="",
    ) or task

    if _stop_requested(config):
        return {
            "task_id": active_task.task_id,
            "status": "stopped",
            "artifact_dir": str(artifact_dir),
        }

    _update_runtime(
        config,
        {
            "phase": "implementing",
            "task_id": active_task.task_id,
            "task_summary": _task_summary(active_task),
            "artifact_dir": str(artifact_dir),
        },
    )
    _emit_header(
        config,
        "implementing",
        [
            "task_id={0}".format(active_task.task_id),
            "model={0}".format(implementer_model),
        ],
    )
    implementation_prompt = build_implementation_prompt(repo_root, config, active_task, file_hints, history, dict(active_task.plan))
    implementation_result = _run_codex_phase(
        config,
        repo_root,
        model=implementer_model,
        reasoning_effort=_configured_reasoning_effort(config, "implementer"),
        prompt=implementation_prompt,
        artifact_dir=artifact_dir,
        phase_name="implement",
        output_schema=SCHEMA_ROOT / "report.schema.json",
        interrupt_state=interrupt_state,
    )
    _validate_phase_result("implement", implementation_result)
    report = implementation_result.parsed_output or {}
    _write_json(artifact_dir / "implement.result.json", report)

    if _stop_requested(config):
        return {
            "task_id": active_task.task_id,
            "status": "stopped",
            "artifact_dir": str(artifact_dir),
            "report": report,
        }

    report_status = str(report.get("status", "unknown")).strip() or "unknown"
    requested_mark = str(report.get("mark_task_as", "leave_unchanged")).strip() or "leave_unchanged"
    git_result_payload: Dict[str, Any]
    if report_status in ("completed", "needs_testing"):
        publish_phase = requested_mark if requested_mark in ("completed", "needs_testing") else report_status
        git_result = publish_git_changes(
            repo_root,
            config,
            artifact_dir,
            active_task,
            report,
            publish_phase,
            git_session,
        )
        git_result_payload = git_result.to_payload()
    else:
        git_result_payload = {
            "status": "not_run",
            "reason": "git integration runs only after a successful implementation pass",
            "branch": "",
            "remote": "",
            "commit_message": "",
            "commit_sha": "",
            "changed_files": [],
            "dirty_at_start": False,
            "commit_created": False,
            "push_attempted": False,
            "push_succeeded": False,
        }

    _update_runtime(
        config,
        {
            "phase": "verifying",
            "task_id": active_task.task_id,
            "task_summary": _task_summary(active_task),
            "artifact_dir": str(artifact_dir),
        },
    )
    _emit_header(
        config,
        "verifying",
        [
            "task_id={0}".format(active_task.task_id),
            "model=none",
        ],
    )
    verification_results = run_verification_steps(repo_root, config, artifact_dir / "verification")
    verification = _verification_summary(verification_results)
    _write_json(artifact_dir / "verification.result.json", verification)

    if verification["all_passed"]:
        if requested_mark in ("completed", "needs_testing"):
            final_phase = requested_mark
            mark_task_result = requested_mark
        elif report_status == "blocked":
            final_phase = "blocked"
            mark_task_result = "blocked"
        else:
            final_phase = "ready"
            mark_task_result = "ready"
    else:
        final_phase = "blocked"
        mark_task_result = "blocked due to verification failure"

    update_task_phase(
        config,
        active_task.task_id,
        final_phase,
        artifact_dir=str(artifact_dir),
        last_result_status=report_status,
        last_summary=str(report.get("summary", "")),
        last_error="" if verification["all_passed"] else "verification failed",
    )
    _write_json(artifact_dir / "git.result.json", git_result_payload)
    _log_git_result(config, git_result_payload)

    summary = {
        "task_id": active_task.task_id,
        "status": report_status,
        "summary": str(report.get("summary", "")),
        "mark_task_result": mark_task_result,
        "verification": verification,
        "git": git_result_payload,
        "artifact_dir": str(artifact_dir),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    _append_history(config, summary)
    _write_json(artifact_dir / "run.summary.json", summary)
    return summary


def _cmd_doctor(config: Dict[str, Any]) -> int:
    ensure_task_store(config)
    checks = {
        "app_root": Path(config["app_root"]).exists(),
        "repo_root": Path(config["repo_root"]).exists(),
        "task_store": store_path(config).exists(),
        "codex": shutil.which("codex") is not None,
        "pyside6": __import__("importlib.util").util.find_spec("PySide6") is not None,
    }
    task_file_exists = Path(config["task_file"]).exists()
    version = ""
    if checks["codex"]:
        completed = subprocess.run(
            ["codex", "--version"],
            cwd=config["repo_root"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        version = completed.stdout.strip()
    for key, ok in checks.items():
        suffix = "ok" if ok else "missing"
        if key == "pyside6" and not ok:
            suffix = "optional"
        print("{0:12} {1}".format(key, suffix))
    print("{0:12} {1}".format("task_file", "ok" if task_file_exists else "optional"))
    print("selected_repo", config["repo_root"])
    if str(config.get("config_path", "")).strip():
        print("config_path", config["config_path"])
    if version:
        print("codex_version", version)
    return 0 if all(checks[key] for key in checks if key != "pyside6") else 1


def _cmd_list(config: Dict[str, Any], args: argparse.Namespace) -> int:
    tasks = list_store_tasks(
        config,
        include_completed=args.all,
        include_needs_testing=args.include_needs_testing or args.all,
        text_query=args.query,
    )
    for task in tasks:
        print("{0} | {1:13} | {2} | {3}".format(task.task_id, task.phase, task.board_title, task.title))
    return 0


def _cmd_index(config: Dict[str, Any], args: argparse.Namespace) -> int:
    index = build_repo_index(Path(config["repo_root"]), config, rebuild=args.rebuild)
    print("indexed", len(index.get("files", {})), "files")
    return 0


def _cmd_sync(config: Dict[str, Any]) -> int:
    result = sync_markdown_into_store(config)
    print(json.dumps(result, indent=2, sort_keys=True))
    print("store:", store_path(config))
    return 0


def _cmd_add_task(config: Dict[str, Any], args: argparse.Namespace) -> int:
    task = create_task(
        config,
        board_title=args.board,
        title=args.title,
        context_notes=args.context or "",
    )
    print("{0} | {1} | {2} | {3}".format(task.task_id, task.phase, task.board_title, task.title))
    return 0


def _cmd_plan(config: Dict[str, Any], args: argparse.Namespace) -> int:
    include_needs_testing = bool(
        args.include_needs_testing or config.get("selection", {}).get("include_needs_testing", False)
    )
    task = _select_task(
        config,
        task_id=args.task_id,
        include_needs_testing=include_needs_testing,
        text_query=args.query,
    )
    if task is None:
        print("no matching task found", file=sys.stderr)
        return 1
    _acquire_runtime_lock(config, "plan")
    interrupt_state = {"requested": False}

    def handle_signal(signum: int, _frame: Any) -> None:
        interrupt_state["requested"] = True
        _update_runtime(config, {"phase": "signal", "signal": signum})

    previous_sigint = signal.signal(signal.SIGINT, handle_signal)
    previous_sigterm = signal.signal(signal.SIGTERM, handle_signal)
    try:
        _emit_header(
            config,
            "task start",
            [
                "task_id={0}".format(task.task_id),
                "title={0}".format(task.title),
                "model={0}".format(str(config["models"]["planner"])),
            ],
        )
        result = _run_plan_for_task(
            config,
            task,
            rebuild_index=args.rebuild_index,
            allow_fast_path=True,
            interrupt_state=interrupt_state,
        )
    except Exception:
        if interrupt_state["requested"]:
            _emit_line(config, "planning interrupted", stderr=True)
            return 130
        raise
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        _clear_runtime(config)
    if result.get("split"):
        print("split:", task.task_id)
        for subtask in result.get("subtasks", []):
            print("  -> {0} | {1} | {2}".format(subtask.task_id, subtask.board_title, subtask.title))
    else:
        print("planned:", task.task_id)
    print("artifact:", result["artifact_dir"])
    return 0


def _cmd_status(config: Dict[str, Any]) -> int:
    runtime_path = _runtime_path(config)
    stop_path = _stop_path(config)
    if runtime_path.exists():
        payload = _load_json(runtime_path)
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("taskbot is idle")
    counts = {phase: 0 for phase in phase_labels(config)}
    for task in list_store_tasks(
        config,
        include_completed=True,
        include_needs_testing=True,
    ):
        counts[task.phase] = counts.get(task.phase, 0) + 1
    print("phase_counts:", json.dumps(counts, sort_keys=True))
    print("task_store:", store_path(config))
    print("terminal_log:", terminal_log_path(config))
    print("stop_requested:", stop_path.exists())
    return 0


def _cmd_stop(config: Dict[str, Any]) -> int:
    _request_stop(config)
    _emit_line(config, "stop requested")
    return 0


def _cmd_resume(config: Dict[str, Any]) -> int:
    _clear_stop(config)
    _emit_line(config, "stop cleared")
    return 0


def _cmd_ui(config: Dict[str, Any]) -> int:
    from taskbot.ui import launch_ui

    return int(launch_ui(config))


def _cmd_run(config: Dict[str, Any], args: argparse.Namespace) -> int:
    _acquire_runtime_lock(config, "run")
    _clear_stop(config)
    stop_flag = {"requested": False}
    interrupt_state = {"requested": False}
    include_needs_testing = bool(
        args.include_needs_testing or config.get("selection", {}).get("include_needs_testing", False)
    )

    def handle_signal(signum: int, _frame: Any) -> None:
        stop_flag["requested"] = True
        interrupt_state["requested"] = True
        _request_stop(config)
        _update_runtime(config, {"phase": "signal", "signal": signum})

    previous_sigint = signal.signal(signal.SIGINT, handle_signal)
    previous_sigterm = signal.signal(signal.SIGTERM, handle_signal)

    try:
        iterations = None if args.continuous else int(args.iterations or config["loop"]["default_iterations"])
        completed = 0
        while True:
            if stop_flag["requested"] or _stop_requested(config):
                _emit_line(config, "stop requested; exiting after current boundary")
                break

            task = _select_task(
                config,
                task_id=args.task_id,
                include_needs_testing=include_needs_testing,
                text_query=args.query,
            )
            if task is None:
                _emit_line(config, "no matching tasks remaining")
                break

            try:
                summary = _run_task_once(
                    config,
                    task,
                    rebuild_index=args.rebuild_index,
                    interrupt_state=interrupt_state,
                )
            except Exception as exc:
                if interrupt_state["requested"]:
                    _emit_line(config, "task {0} interrupted".format(task.task_id), stderr=True)
                    break
                update_task_phase(
                    config,
                    task.task_id,
                    "blocked",
                    last_result_status="blocked",
                    last_summary=task.last_summary,
                    last_error=str(exc),
                )
                error_line = "task {0} failed: {1}".format(task.task_id, exc)
                _emit_line(config, error_line, stderr=True)
                if args.task_id:
                    raise
                time.sleep(float(config["loop"]["sleep_seconds"]))
                continue

            print(json.dumps(summary, indent=2, sort_keys=True))
            append_terminal_log(config, json.dumps(summary, indent=2, sort_keys=True))
            completed += 1
            if args.task_id:
                break
            if iterations is not None and completed >= iterations:
                break
            time.sleep(float(config["loop"]["sleep_seconds"]))
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        _clear_runtime(config)
    return 130 if interrupt_state["requested"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Repo-local Codex task loop")
    parser.add_argument(
        "--config",
        help="Path to a taskbot JSON config file",
    )
    parser.add_argument(
        "--repo-root",
        help="Repository to operate on; defaults to this taskbot checkout",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.set_defaults(func=lambda config, args: _cmd_doctor(config))

    list_cmd = subparsers.add_parser("list")
    list_cmd.add_argument("--all", action="store_true")
    list_cmd.add_argument("--include-needs-testing", action="store_true")
    list_cmd.add_argument("--query")
    list_cmd.set_defaults(func=_cmd_list)

    sync_cmd = subparsers.add_parser("sync")
    sync_cmd.set_defaults(func=lambda config, args: _cmd_sync(config))

    add_task_cmd = subparsers.add_parser("add-task")
    add_task_cmd.add_argument("--board", default="General")
    add_task_cmd.add_argument("--title", required=True)
    add_task_cmd.add_argument("--context")
    add_task_cmd.set_defaults(func=_cmd_add_task)

    index_cmd = subparsers.add_parser("index")
    index_cmd.add_argument("--rebuild", action="store_true")
    index_cmd.set_defaults(func=_cmd_index)

    status_cmd = subparsers.add_parser("status")
    status_cmd.set_defaults(func=lambda config, args: _cmd_status(config))

    stop_cmd = subparsers.add_parser("stop")
    stop_cmd.set_defaults(func=lambda config, args: _cmd_stop(config))

    resume_cmd = subparsers.add_parser("resume")
    resume_cmd.set_defaults(func=lambda config, args: _cmd_resume(config))

    ui_cmd = subparsers.add_parser("ui")
    ui_cmd.set_defaults(func=lambda config, args: _cmd_ui(config))

    plan_cmd = subparsers.add_parser("plan")
    plan_cmd.add_argument("--task-id")
    plan_cmd.add_argument("--query")
    plan_cmd.add_argument("--include-needs-testing", action="store_true")
    plan_cmd.add_argument("--rebuild-index", action="store_true")
    plan_cmd.set_defaults(func=_cmd_plan)

    run_cmd = subparsers.add_parser("run")
    run_cmd.add_argument("--task-id")
    run_cmd.add_argument("--query")
    run_cmd.add_argument("--iterations", type=int)
    run_cmd.add_argument("--continuous", action="store_true")
    run_cmd.add_argument("--include-needs-testing", action="store_true")
    run_cmd.add_argument("--rebuild-index", action="store_true")
    run_cmd.set_defaults(func=_cmd_run)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = _resolve_cli_path(getattr(args, "repo_root", None)) or APP_ROOT
    explicit_config = _resolve_cli_path(getattr(args, "config", None))
    config_path = discover_config_path(APP_ROOT, repo_root, explicit_config)
    config = load_config(repo_root, config_path, app_root=APP_ROOT)
    ensure_runtime_directories(config)
    ensure_task_store(config)
    if args.command == "add-task" and getattr(args, "board", None) == "General":
        args.board = str(config.get("store", {}).get("default_board", "General"))
    return int(args.func(config, args))
