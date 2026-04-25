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
from taskbot.indexer import build_repo_index, rank_files_for_task
from taskbot.prompts import build_implementation_prompt, build_plan_prompt
from taskbot.store import (
    StoredTask,
    apply_plan_result,
    create_task,
    ensure_task_store,
    list_store_tasks,
    load_store_snapshot,
    phase_labels,
    select_next_task,
    store_path,
    sync_markdown_into_store,
    update_task_phase,
)
from taskbot.terminal_stream import append_terminal_log, terminal_log_path
from taskbot.verification import VerificationResult, run_verification_steps


APP_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = APP_ROOT / "schemas"


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


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def _task_summary(task: StoredTask) -> str:
    return "{0} [{1}] {2}".format(task.board_title, task.phase, task.title)


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


def _verification_summary(results: List[VerificationResult]) -> Dict[str, Any]:
    return {
        "all_passed": all(result.exit_code == 0 for result in results),
        "results": [result.__dict__ for result in results],
    }


def _run_plan_for_task(config: Dict[str, Any], task: StoredTask, *, rebuild_index: bool = False) -> Dict[str, Any]:
    repo_root = Path(config["repo_root"])
    artifact_dir = _artifact_dir(config, task)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    refreshed_task = update_task_phase(
        config,
        task.task_id,
        "planning",
        artifact_dir=str(artifact_dir),
        last_result_status="planning",
        last_summary=task.last_summary,
        last_error="",
    ) or task

    message = _banner(config, "planning", "{0} | {1}".format(task.task_id, task.title))
    _emit_line(config, message)
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

    prompt = build_plan_prompt(repo_root, config, refreshed_task, file_hints, history)
    plan_result = run_codex_exec(
        repo_root,
        config,
        model=str(config["models"]["planner"]),
        prompt=prompt,
        artifact_dir=artifact_dir,
        phase_name="plan",
        output_schema=SCHEMA_ROOT / "plan.schema.json",
    )
    _validate_phase_result("plan", plan_result)
    plan_payload = plan_result.parsed_output or {}
    _write_json(artifact_dir / "plan.result.json", plan_payload)
    apply_plan_result(config, refreshed_task.task_id, plan_payload=plan_payload, artifact_dir=str(artifact_dir))
    return {
        "artifact_dir": artifact_dir,
        "plan": plan_payload,
        "file_hints": file_hints,
        "history": history,
        "task": refreshed_task,
    }


def _run_task_once(config: Dict[str, Any], task: StoredTask, *, rebuild_index: bool = False) -> Dict[str, Any]:
    if task.needs_planning():
        plan_context = _run_plan_for_task(config, task, rebuild_index=rebuild_index)
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
    _emit_line(config, _banner(config, "implementing", active_task.task_id))
    implementation_prompt = build_implementation_prompt(repo_root, config, active_task, file_hints, history, dict(active_task.plan))
    implementation_result = run_codex_exec(
        repo_root,
        config,
        model=str(config["models"]["implementer"]),
        prompt=implementation_prompt,
        artifact_dir=artifact_dir,
        phase_name="implement",
        output_schema=SCHEMA_ROOT / "report.schema.json",
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

    _update_runtime(
        config,
        {
            "phase": "verifying",
            "task_id": active_task.task_id,
            "task_summary": _task_summary(active_task),
            "artifact_dir": str(artifact_dir),
        },
    )
    _emit_line(config, _banner(config, "verifying", active_task.task_id))
    verification_results = run_verification_steps(repo_root, config, artifact_dir / "verification")
    verification = _verification_summary(verification_results)
    _write_json(artifact_dir / "verification.result.json", verification)

    requested_mark = str(report.get("mark_task_as", "leave_unchanged"))
    if verification["all_passed"]:
        if requested_mark in ("completed", "needs_testing"):
            final_phase = requested_mark
            mark_task_result = requested_mark
        elif str(report.get("status", "")) == "blocked":
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
        last_result_status=str(report.get("status", "unknown")),
        last_summary=str(report.get("summary", "")),
        last_error="" if verification["all_passed"] else "verification failed",
    )

    summary = {
        "task_id": active_task.task_id,
        "status": str(report.get("status", "unknown")),
        "summary": str(report.get("summary", "")),
        "mark_task_result": mark_task_result,
        "verification": verification,
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
    try:
        result = _run_plan_for_task(config, task, rebuild_index=args.rebuild_index)
    finally:
        _clear_runtime(config)
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
    store = load_store_snapshot(config)
    counts = {phase: 0 for phase in phase_labels(config)}
    for task_payload in store.get("tasks", []):
        if not isinstance(task_payload, dict):
            continue
        phase = str(task_payload.get("phase", "backlog"))
        counts[phase] = counts.get(phase, 0) + 1
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
    include_needs_testing = bool(
        args.include_needs_testing or config.get("selection", {}).get("include_needs_testing", False)
    )

    def handle_signal(signum: int, _frame: Any) -> None:
        stop_flag["requested"] = True
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
                summary = _run_task_once(config, task, rebuild_index=args.rebuild_index)
            except Exception as exc:
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
    return 0


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
