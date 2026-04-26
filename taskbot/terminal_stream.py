from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


def terminal_log_path(config: dict[str, Any]) -> Path:
    configured = str(config.get("ui", {}).get("terminal_log", "")).strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else (Path(config["repo_root"]) / path).resolve()
    return Path(config["control_dir"]) / "terminal.log"


def append_terminal_log(config: dict[str, Any], text: str) -> None:
    path = terminal_log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for line in text.splitlines():
            handle.write(line + "\n")


def format_terminal_header(title: str, details: Iterable[str] = ()) -> str:
    lines = ["", "[taskbot] === {0} ===".format(str(title).strip())]
    for detail in details:
        cleaned = str(detail).strip()
        if cleaned:
            lines.append("[taskbot] {0}".format(cleaned))
    return "\n".join(lines)
