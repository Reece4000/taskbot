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


def read_terminal_tail(path: Path, max_lines: int, chunk_size: int = 8192) -> str:
    if max_lines <= 0:
        return ""

    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            file_size = handle.tell()
            if file_size <= 0:
                return ""

            position = file_size
            buffer = b""
            newline_target = max_lines + 1
            while position > 0 and buffer.count(b"\n") < newline_target:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                buffer = handle.read(read_size) + buffer
    except OSError:
        return ""

    return "\n".join(buffer.decode("utf-8", errors="ignore").splitlines()[-max_lines:])


def format_terminal_header(title: str, details: Iterable[str] = ()) -> str:
    lines = ["", "[taskbot] === {0} ===".format(str(title).strip())]
    for detail in details:
        cleaned = str(detail).strip()
        if cleaned:
            lines.append("[taskbot] {0}".format(cleaned))
    return "\n".join(lines)
