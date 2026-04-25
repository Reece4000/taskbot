from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_CONFIG: Dict[str, Any] = {
    "task_file": "_taskbot/_tasks.md",
    "state_dir": "_taskbot/state",
    "artifact_dir": "_taskbot/artifacts",
    "control_dir": "_taskbot/control",
    "store": {
        "path": "_taskbot/tasks.yaml",
        "phases": [
            "backlog",
            "planning",
            "ready",
            "in_progress",
            "needs_testing",
            "blocked",
            "completed",
        ],
        "runner_pick_phases": [
            "ready",
            "backlog",
        ],
        "default_board": "General",
    },
    "models": {
        "planner": "gpt-5.4",
        "implementer": "gpt-5.4-mini",
        "reviewer": "gpt-5.4-mini",
    },
    "codex": {
        "sandbox": "workspace-write",
        "ask_for_approval": "never",
        "color": "never",
        "ephemeral": True,
        "skip_git_repo_check": True,
        "allow_subagents": True,
        "stream_output": True,
        "stream_commands": True,
        "stream_stderr": False,
        "stream_ansi": "auto",
    },
    "context": {
        "scan_roots": ["."],
        "exclude_paths": [
            ".git",
            ".hg",
            ".svn",
            ".venv",
            "venv",
            "node_modules",
            "dist",
            "build",
            "coverage",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".next",
            ".turbo",
            "_taskbot/artifacts",
            "_taskbot/state",
            "_taskbot/control",
            "_taskbot/tasks.yaml",
            "_taskbot/_tasks.md",
            "_taskbot/README.md",
            "_taskbot/__pycache__",
            "_taskbot/taskbot/__pycache__",
        ],
        "include_extensions": [".cpp", ".h", ".hpp", ".qml", ".json", ".md", ".py", ".txt", ".cmake"],
        "max_file_size_bytes": 180000,
        "max_symbols_per_file": 8,
        "plan_file_limit": 12,
        "implementation_file_limit": 16,
        "max_history_items": 2,
    },
    "selection": {
        "include_needs_testing": False,
    },
    "loop": {
        "default_iterations": 1,
        "sleep_seconds": 2.0,
    },
    "ui": {
        "refresh_seconds": 1.0,
        "terminal_log": "_taskbot/control/terminal.log",
        "terminal_tail_lines": 250,
        "terminal_ansi": True,
    },
    "verification": {
        "commands": [],
    },
}


def _merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_path(repo_root: Path, value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((repo_root / path).resolve())


def discover_config_path(app_root: Path,
                         repo_root: Path,
                         explicit_path: Optional[Path] = None) -> Optional[Path]:
    if explicit_path is not None:
        return explicit_path.resolve()

    candidates = [
        repo_root / "_taskbot" / "config.json",
        repo_root / "taskbot.config.json",
    ]
    if repo_root.resolve() == app_root.resolve():
        candidates.append(app_root / "config.json")

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def load_config(repo_root: Path,
                config_path: Optional[Path],
                *,
                app_root: Optional[Path] = None) -> Dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    if config_path is not None and config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        config = _merge_dict(config, loaded)

    resolved_app_root = (app_root or Path(__file__).resolve().parents[1]).resolve()
    config["repo_root"] = str(repo_root.resolve())
    config["app_root"] = str(resolved_app_root)
    config["config_path"] = str(config_path.resolve()) if config_path is not None else ""

    for key in ("task_file", "state_dir", "artifact_dir", "control_dir"):
        config[key] = _resolve_path(repo_root, config[key])
    if isinstance(config.get("store"), dict) and "path" in config["store"]:
        config["store"]["path"] = _resolve_path(repo_root, str(config["store"]["path"]))
    if isinstance(config.get("ui"), dict) and "terminal_log" in config["ui"]:
        config["ui"]["terminal_log"] = _resolve_path(repo_root, str(config["ui"]["terminal_log"]))

    return config


def ensure_runtime_directories(config: Dict[str, Any]) -> None:
    for key in ("state_dir", "artifact_dir", "control_dir"):
        Path(config[key]).mkdir(parents=True, exist_ok=True)
