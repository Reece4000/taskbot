from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


STATUS_ALIASES = {
    "completed": "completed",
    "complete": "completed",
    "done": "completed",
    "needs testing": "needs_testing",
    "needs-testing": "needs_testing",
    "needs_testing": "needs_testing",
    "pending": "pending",
    "backlog": "pending",
}

TASK_LINE_RE = re.compile(r"^(\s*-\s*)(\[[^\]]+\]\s*)?(.*?)(\r?\n?)$")


@dataclass
class TaskItem:
    task_id: str
    line_index: int
    section: str
    text: str
    status: str
    raw_line: str


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "task"


def _stable_task_id(section: str, text: str) -> str:
    digest = hashlib.sha1((section + "\n" + text).encode("utf-8")).hexdigest()[:8]
    return "{0}-{1}".format(_slugify(section), digest)


def _normalise_status(raw_status: Optional[str]) -> str:
    if not raw_status:
        return "pending"
    cleaned = raw_status.strip().lower().strip("[]").strip()
    return STATUS_ALIASES.get(cleaned, "pending")


def _status_for_update(raw_status: Optional[str]) -> Optional[str]:
    if not raw_status:
        return None
    cleaned = raw_status.strip().lower().strip("[]").strip()
    if not cleaned:
        return None
    return STATUS_ALIASES.get(cleaned)


def parse_tasks(task_file: Path) -> List[TaskItem]:
    lines = task_file.read_text(encoding="utf-8").splitlines(keepends=True)
    tasks: List[TaskItem] = []
    current_section = "root"

    for line_index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            current_section = stripped.lstrip("#").strip() or current_section
            continue
        if stripped.endswith(":") and not stripped.startswith("-"):
            current_section = stripped[:-1].strip() or current_section
            continue
        if not stripped.startswith("-"):
            continue

        match = TASK_LINE_RE.match(line)
        if not match:
            continue

        text = match.group(3).strip()
        if not text:
            continue

        raw_status = match.group(2) or ""
        status = _normalise_status(raw_status)
        task_id = _stable_task_id(current_section, text)
        tasks.append(
            TaskItem(
                task_id=task_id,
                line_index=line_index,
                section=current_section,
                text=text,
                status=status,
                raw_line=line,
            )
        )

    return tasks


def filter_tasks(tasks: Iterable[TaskItem],
                 *,
                 include_completed: bool = False,
                 include_needs_testing: bool = False,
                 task_id: Optional[str] = None,
                 text_query: Optional[str] = None) -> List[TaskItem]:
    selected: List[TaskItem] = []
    query = (text_query or "").strip().lower()
    for task in tasks:
        if task_id and task.task_id != task_id:
            continue
        if query and query not in task.text.lower() and query not in task.section.lower():
            continue
        if task.status == "completed" and not include_completed:
            continue
        if task.status == "needs_testing" and not include_needs_testing:
            continue
        selected.append(task)
    return selected


def choose_next_task(tasks: Iterable[TaskItem], include_needs_testing: bool) -> Optional[TaskItem]:
    pending = [task for task in tasks if task.status == "pending"]
    if pending:
        return pending[0]
    if include_needs_testing:
        needs_testing = [task for task in tasks if task.status == "needs_testing"]
        if needs_testing:
            return needs_testing[0]
    return None


def update_task_status(task_file: Path, task_id: str, new_status: str) -> bool:
    lines = task_file.read_text(encoding="utf-8").splitlines(keepends=True)
    tasks = parse_tasks(task_file)
    target = next((task for task in tasks if task.task_id == task_id), None)
    if target is None:
        return False

    canonical = _status_for_update(new_status)
    if canonical is None:
        return False

    original_line = lines[target.line_index]
    match = TASK_LINE_RE.match(original_line)
    if not match:
        return False

    prefix = match.group(1)
    text = match.group(3).strip()
    newline = match.group(4)
    if canonical == "pending":
        lines[target.line_index] = "{0}{1}{2}".format(prefix, text, newline)
    else:
        lines[target.line_index] = "{0}[{1}] {2}{3}".format(prefix, canonical.replace("_", " "), text, newline)
    task_file.write_text("".join(lines), encoding="utf-8")
    return True


def delete_task(task_file: Path, task_id: str) -> bool:
    lines = task_file.read_text(encoding="utf-8").splitlines(keepends=True)
    tasks = parse_tasks(task_file)
    target = next((task for task in tasks if task.task_id == task_id), None)
    if target is None:
        return False

    del lines[target.line_index]
    task_file.write_text("".join(lines), encoding="utf-8")
    return True
