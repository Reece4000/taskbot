from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from taskbot.tasks import TaskItem, delete_task as delete_markdown_task, parse_tasks, update_task_status


STORE_VERSION = 1
DEFAULT_PHASES = [
    "backlog",
    "planning",
    "ready",
    "in_progress",
    "needs_testing",
    "blocked",
    "completed",
]
DEFAULT_RUNNER_PICK_PHASES = [
    "ready",
    "backlog",
]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _slugify(text: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "item"


def _stable_task_id(board_title: str, title: str) -> str:
    digest = hashlib.sha1((board_title + "\n" + title).encode("utf-8")).hexdigest()[:8]
    return "{0}-{1}".format(_slugify(board_title), digest)


def _task_status_from_phase(phase: str) -> str:
    if phase == "completed":
        return "completed"
    if phase == "needs_testing":
        return "needs_testing"
    return "pending"


def _phase_from_markdown_status(status: str) -> str:
    if status == "completed":
        return "completed"
    if status == "needs_testing":
        return "needs_testing"
    return "backlog"


@dataclass
class StoredTask:
    task_id: str
    board_id: str
    board_title: str
    title: str
    phase: str
    context_notes: str
    file_targets: List[str]
    acceptance: List[str]
    source_kind: str
    source_line_index: int
    plan_status: str
    plan: Dict[str, Any]
    artifact_dir: str
    last_result_status: str
    last_summary: str
    last_error: str
    order: int
    created_at: str
    updated_at: str

    @property
    def status(self) -> str:
        return _task_status_from_phase(self.phase)

    def to_task_item(self) -> TaskItem:
        return TaskItem(
            task_id=self.task_id,
            line_index=self.source_line_index,
            section=self.board_title,
            text=self.title,
            status=self.status,
            raw_line=self.title,
        )

    def needs_planning(self) -> bool:
        return self.plan_status != "ready" or not self.plan

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "StoredTask":
        return cls(
            task_id=str(payload.get("task_id", "")),
            board_id=str(payload.get("board_id", "")),
            board_title=str(payload.get("board_title", "")),
            title=str(payload.get("title", "")),
            phase=str(payload.get("phase", "backlog")),
            context_notes=str(payload.get("context_notes", "")),
            file_targets=[str(item) for item in payload.get("file_targets", [])],
            acceptance=[str(item) for item in payload.get("acceptance", [])],
            source_kind=str(payload.get("source_kind", "ui")),
            source_line_index=int(payload.get("source_line_index", -1) or -1),
            plan_status=str(payload.get("plan_status", "pending")),
            plan=dict(payload.get("plan", {})),
            artifact_dir=str(payload.get("artifact_dir", "")),
            last_result_status=str(payload.get("last_result_status", "")),
            last_summary=str(payload.get("last_summary", "")),
            last_error=str(payload.get("last_error", "")),
            order=int(payload.get("order", 0) or 0),
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
        )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "board_id": self.board_id,
            "board_title": self.board_title,
            "title": self.title,
            "phase": self.phase,
            "context_notes": self.context_notes,
            "file_targets": list(self.file_targets),
            "acceptance": list(self.acceptance),
            "source_kind": self.source_kind,
            "source_line_index": self.source_line_index,
            "plan_status": self.plan_status,
            "plan": dict(self.plan),
            "artifact_dir": self.artifact_dir,
            "last_result_status": self.last_result_status,
            "last_summary": self.last_summary,
            "last_error": self.last_error,
            "order": self.order,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def store_path(config: Dict[str, Any]) -> Path:
    configured = str(config.get("store", {}).get("path", "_taskbot/tasks.yaml")).strip()
    path = Path(configured)
    return path if path.is_absolute() else (Path(config["repo_root"]) / path).resolve()


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _default_store(config: Dict[str, Any]) -> Dict[str, Any]:
    phases = list(config.get("store", {}).get("phases", DEFAULT_PHASES))
    return {
        "version": STORE_VERSION,
        "phases": phases,
        "boards": [],
        "tasks": [],
    }


def _normalise_store(store: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    normalised = dict(store)
    normalised.setdefault("version", STORE_VERSION)
    normalised.setdefault("phases", list(config.get("store", {}).get("phases", DEFAULT_PHASES)))
    normalised.setdefault("boards", [])
    normalised.setdefault("tasks", [])
    return normalised


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def _load_store_unlocked(path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return _default_store(config)
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        return _default_store(config)
    return _normalise_store(loaded, config)


def _ensure_board(store: Dict[str, Any], board_title: str, order_hint: int) -> str:
    board_id = _slugify(board_title)
    boards = store["boards"]
    existing = next((board for board in boards if str(board.get("board_id", "")) == board_id), None)
    if existing is None:
        boards.append(
            {
                "board_id": board_id,
                "title": board_title,
                "order": order_hint,
            }
        )
    else:
        existing["title"] = board_title
        existing["order"] = min(int(existing.get("order", order_hint) or order_hint), order_hint)
    boards.sort(key=lambda board: (int(board.get("order", 0) or 0), str(board.get("title", ""))))
    return board_id


def _sync_markdown_unlocked(store: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, int]:
    task_file = Path(config["task_file"])
    if not task_file.exists():
        return {"added": 0, "updated": 0}

    parsed = parse_tasks(task_file)
    by_id = {
        str(task_payload.get("task_id", "")): task_payload
        for task_payload in store["tasks"]
        if isinstance(task_payload, dict)
    }
    added = 0
    updated = 0
    next_order = max((int(task_payload.get("order", 0) or 0) for task_payload in store["tasks"]), default=-1) + 1
    seen_boards: Dict[str, int] = {}

    for markdown_task in parsed:
        board_order = seen_boards.setdefault(markdown_task.section, len(seen_boards))
        board_id = _ensure_board(store, markdown_task.section, board_order)
        payload = by_id.get(markdown_task.task_id)
        markdown_phase = _phase_from_markdown_status(markdown_task.status)

        if payload is None:
            store["tasks"].append(
                StoredTask(
                    task_id=markdown_task.task_id,
                    board_id=board_id,
                    board_title=markdown_task.section,
                    title=markdown_task.text,
                    phase=markdown_phase,
                    context_notes="",
                    file_targets=[],
                    acceptance=[],
                    source_kind="markdown",
                    source_line_index=markdown_task.line_index,
                    plan_status="pending",
                    plan={},
                    artifact_dir="",
                    last_result_status="",
                    last_summary="",
                    last_error="",
                    order=next_order,
                    created_at=_now_iso(),
                    updated_at=_now_iso(),
                ).to_payload()
            )
            next_order += 1
            added += 1
            continue

        changed = False
        if str(payload.get("board_id", "")) != board_id:
            payload["board_id"] = board_id
            changed = True
        if str(payload.get("board_title", "")) != markdown_task.section:
            payload["board_title"] = markdown_task.section
            changed = True
        if str(payload.get("title", "")) != markdown_task.text:
            payload["title"] = markdown_task.text
            changed = True
        if int(payload.get("source_line_index", -1) or -1) != markdown_task.line_index:
            payload["source_line_index"] = markdown_task.line_index
            changed = True
        if str(payload.get("source_kind", "")) == "markdown" and markdown_phase in ("completed", "needs_testing"):
            if str(payload.get("phase", "")) != markdown_phase:
                payload["phase"] = markdown_phase
                changed = True

        if changed:
            payload["updated_at"] = _now_iso()
            updated += 1

    store["tasks"].sort(key=lambda task_payload: (int(task_payload.get("order", 0) or 0), str(task_payload.get("title", ""))))
    return {"added": added, "updated": updated}


def _mutate_store(config: Dict[str, Any],
                  mutator: Callable[[Dict[str, Any]], Any],
                  *,
                  sync_markdown: bool = True) -> Any:
    path = store_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        store = _load_store_unlocked(path, config)
        if sync_markdown:
            _sync_markdown_unlocked(store, config)
        result = mutator(store)
        _atomic_write_json(path, store)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return result


def ensure_task_store(config: Dict[str, Any]) -> Path:
    path = store_path(config)

    def noop(_store: Dict[str, Any]) -> None:
        return None

    _mutate_store(config, noop, sync_markdown=True)
    return path


def sync_markdown_into_store(config: Dict[str, Any]) -> Dict[str, int]:
    result: Dict[str, int] = {}

    def mutate(store: Dict[str, Any]) -> None:
        nonlocal result
        result = _sync_markdown_unlocked(store, config)

    _mutate_store(config, mutate, sync_markdown=False)
    return result


def load_store_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    ensure_task_store(config)
    path = store_path(config)
    return _load_store_unlocked(path, config)


def _board_order_map(store: Dict[str, Any]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for board in store.get("boards", []):
        board_id = str(board.get("board_id", ""))
        mapping[board_id] = int(board.get("order", 0) or 0)
    return mapping


def _sorted_tasks(store: Dict[str, Any]) -> List[StoredTask]:
    board_orders = _board_order_map(store)
    tasks = [StoredTask.from_payload(payload) for payload in store.get("tasks", []) if isinstance(payload, dict)]
    tasks.sort(key=lambda task: (board_orders.get(task.board_id, 9999), task.order, task.title.lower()))
    return tasks


def list_boards(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    store = load_store_snapshot(config)
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
    boards.sort(key=lambda board: (board["order"], board["title"].lower()))
    return boards


def list_store_tasks(config: Dict[str, Any],
                     *,
                     include_completed: bool = False,
                     include_needs_testing: bool = False,
                     task_id: Optional[str] = None,
                     text_query: Optional[str] = None) -> List[StoredTask]:
    store = load_store_snapshot(config)
    query = (text_query or "").strip().lower()
    tasks: List[StoredTask] = []
    for task in _sorted_tasks(store):
        if task_id and task.task_id != task_id:
            continue
        haystack = "{0}\n{1}\n{2}".format(task.board_title, task.title, task.context_notes).lower()
        if query and query not in haystack:
            continue
        if task.phase == "completed" and not include_completed:
            continue
        if task.phase == "needs_testing" and not include_needs_testing:
            continue
        tasks.append(task)
    return tasks


def select_next_task(config: Dict[str, Any],
                     *,
                     include_needs_testing: bool,
                     task_id: Optional[str] = None,
                     text_query: Optional[str] = None) -> Optional[StoredTask]:
    tasks = list_store_tasks(
        config,
        include_completed=True,
        include_needs_testing=True,
        task_id=task_id,
        text_query=text_query,
    )
    if task_id:
        return tasks[0] if tasks else None

    phase_order = list(config.get("store", {}).get("runner_pick_phases", DEFAULT_RUNNER_PICK_PHASES))
    if include_needs_testing and "needs_testing" not in phase_order:
        phase_order.append("needs_testing")

    for phase in phase_order:
        for task in tasks:
            if task.phase == phase:
                return task
    return None


def create_task(config: Dict[str, Any],
                *,
                board_title: str,
                title: str,
                context_notes: str = "",
                file_targets: Optional[Iterable[str]] = None,
                acceptance: Optional[Iterable[str]] = None,
                phase: Optional[str] = None) -> StoredTask:
    created: Optional[StoredTask] = None

    def mutate(store: Dict[str, Any]) -> None:
        nonlocal created
        cleaned_board = board_title.strip() or str(config.get("store", {}).get("default_board", "General"))
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ValueError("task title cannot be empty")
        board_id = _ensure_board(store, cleaned_board, len(store.get("boards", [])))
        existing_ids = {str(payload.get("task_id", "")) for payload in store.get("tasks", []) if isinstance(payload, dict)}
        candidate_id = _stable_task_id(cleaned_board, cleaned_title)
        counter = 1
        while candidate_id in existing_ids:
            candidate_id = "{0}-{1}".format(_stable_task_id(cleaned_board, cleaned_title), counter)
            counter += 1
        next_order = max((int(payload.get("order", 0) or 0) for payload in store.get("tasks", [])), default=-1) + 1
        payload = StoredTask(
            task_id=candidate_id,
            board_id=board_id,
            board_title=cleaned_board,
            title=cleaned_title,
            phase=phase or "backlog",
            context_notes=context_notes.strip(),
            file_targets=[str(item).strip() for item in (file_targets or []) if str(item).strip()],
            acceptance=[str(item).strip() for item in (acceptance or []) if str(item).strip()],
            source_kind="ui",
            source_line_index=-1,
            plan_status="pending",
            plan={},
            artifact_dir="",
            last_result_status="",
            last_summary="",
            last_error="",
            order=next_order,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        ).to_payload()
        store["tasks"].append(payload)
        created = StoredTask.from_payload(payload)

    _mutate_store(config, mutate, sync_markdown=True)
    if created is None:
        raise RuntimeError("failed to create task")
    return created


def create_board(config: Dict[str, Any], board_title: str) -> Dict[str, Any]:
    created: Optional[Dict[str, Any]] = None

    def mutate(store: Dict[str, Any]) -> None:
        nonlocal created
        cleaned_board = board_title.strip()
        if not cleaned_board:
            raise ValueError("board title cannot be empty")
        board_id = _ensure_board(store, cleaned_board, len(store.get("boards", [])))
        for payload in store.get("boards", []):
            if not isinstance(payload, dict):
                continue
            if str(payload.get("board_id", "")) != board_id:
                continue
            created = {
                "board_id": board_id,
                "title": str(payload.get("title", cleaned_board)),
                "order": int(payload.get("order", 0) or 0),
            }
            break

    _mutate_store(config, mutate, sync_markdown=True)
    if created is None:
        raise RuntimeError("failed to create board")
    return created


def apply_task_decomposition(config: Dict[str, Any],
                             parent_task_id: str,
                             *,
                             plan_payload: Dict[str, Any],
                             artifact_dir: str) -> Dict[str, Any]:
    created_tasks: List[StoredTask] = []
    parent_task: Optional[StoredTask] = None

    decomposition = plan_payload.get("decomposition", {})
    raw_subtasks = decomposition.get("subtasks", []) if isinstance(decomposition, dict) else []
    if not isinstance(raw_subtasks, list) or not raw_subtasks:
        raise ValueError("decomposition must include at least one subtask")

    def mutate(store: Dict[str, Any]) -> None:
        nonlocal parent_task
        existing_ids = {
            str(payload.get("task_id", ""))
            for payload in store.get("tasks", [])
            if isinstance(payload, dict)
        }
        next_order = max((int(payload.get("order", 0) or 0) for payload in store.get("tasks", [])), default=-1) + 1

        for raw_subtask in raw_subtasks:
            if not isinstance(raw_subtask, dict):
                continue
            board_title = str(raw_subtask.get("board_title", "")).strip() or str(
                config.get("store", {}).get("default_board", "General")
            )
            title = str(raw_subtask.get("title", "")).strip()
            if not title:
                continue
            phase = str(raw_subtask.get("phase", "ready")).strip() or "ready"
            board_id = _ensure_board(store, board_title, len(store.get("boards", [])))
            candidate_id = _stable_task_id(board_title, title)
            counter = 1
            while candidate_id in existing_ids:
                candidate_id = "{0}-{1}".format(_stable_task_id(board_title, title), counter)
                counter += 1
            existing_ids.add(candidate_id)

            plan = raw_subtask.get("plan", {})
            payload = StoredTask(
                task_id=candidate_id,
                board_id=board_id,
                board_title=board_title,
                title=title,
                phase=phase,
                context_notes=str(raw_subtask.get("context_notes", "")).strip(),
                file_targets=[str(item).strip() for item in plan.get("relevant_files", []) if str(item).strip()],
                acceptance=[str(item).strip() for item in raw_subtask.get("acceptance", []) if str(item).strip()],
                source_kind="ui",
                source_line_index=-1,
                plan_status="ready",
                plan=dict(plan) if isinstance(plan, dict) else {},
                artifact_dir=artifact_dir,
                last_result_status="planned",
                last_summary=str(plan.get("summary", "")) if isinstance(plan, dict) else "",
                last_error="",
                order=next_order,
                created_at=_now_iso(),
                updated_at=_now_iso(),
            ).to_payload()
            store["tasks"].append(payload)
            created_tasks.append(StoredTask.from_payload(payload))
            next_order += 1

        split_summary = str(decomposition.get("reason", "")).strip()
        if not split_summary:
            split_summary = "Split into {0} planned subtasks".format(len(created_tasks))

        for payload in store.get("tasks", []):
            if not isinstance(payload, dict):
                continue
            if str(payload.get("task_id", "")) != parent_task_id:
                continue
            payload["phase"] = "completed"
            payload["plan_status"] = "ready"
            payload["plan"] = dict(plan_payload)
            payload["artifact_dir"] = artifact_dir
            payload["last_result_status"] = "split"
            payload["last_summary"] = split_summary
            payload["last_error"] = ""
            payload["updated_at"] = _now_iso()
            parent_task = StoredTask.from_payload(payload)
            break

    _mutate_store(config, mutate, sync_markdown=True)

    if parent_task is None:
        raise RuntimeError("failed to update parent task during decomposition")
    if parent_task.source_kind == "markdown":
        update_task_status(Path(config["task_file"]), parent_task_id, "completed")

    return {
        "parent_task": parent_task,
        "subtasks": created_tasks,
    }


def edit_task(config: Dict[str, Any],
              task_id: str,
              *,
              board_title: str,
              title: str,
              context_notes: str,
              phase: str) -> Optional[StoredTask]:
    updated: Optional[StoredTask] = None

    def mutate(store: Dict[str, Any]) -> None:
        nonlocal updated
        cleaned_board = board_title.strip() or str(config.get("store", {}).get("default_board", "General"))
        cleaned_title = title.strip()
        cleaned_phase = phase.strip()
        if not cleaned_title:
            raise ValueError("task title cannot be empty")
        if cleaned_phase not in phase_labels(config):
            raise ValueError("invalid task phase: {0}".format(cleaned_phase))

        for payload in store.get("tasks", []):
            if not isinstance(payload, dict):
                continue
            if str(payload.get("task_id", "")) != task_id:
                continue

            board_id = _ensure_board(store, cleaned_board, len(store.get("boards", [])))
            payload["board_id"] = board_id
            payload["board_title"] = cleaned_board
            payload["title"] = cleaned_title
            payload["context_notes"] = context_notes.strip()
            payload["phase"] = cleaned_phase
            payload["updated_at"] = _now_iso()
            updated = StoredTask.from_payload(payload)
            break

    _mutate_store(config, mutate, sync_markdown=True)

    if updated and updated.source_kind == "markdown" and phase in ("completed", "needs_testing"):
        update_task_status(Path(config["task_file"]), task_id, "completed" if phase == "completed" else "needs_testing")
    return updated


def delete_task(config: Dict[str, Any], task_id: str) -> Optional[StoredTask]:
    deleted: Optional[StoredTask] = None

    def mutate(store: Dict[str, Any]) -> None:
        nonlocal deleted
        remaining_tasks: List[Dict[str, Any]] = []
        for payload in store.get("tasks", []):
            if not isinstance(payload, dict):
                remaining_tasks.append(payload)
                continue
            if str(payload.get("task_id", "")) == task_id:
                deleted = StoredTask.from_payload(payload)
                continue
            remaining_tasks.append(payload)
        store["tasks"] = remaining_tasks

    _mutate_store(config, mutate, sync_markdown=True)

    if deleted and deleted.source_kind == "markdown":
        delete_markdown_task(Path(config["task_file"]), task_id)
    return deleted


def update_task_fields(config: Dict[str, Any], task_id: str, **fields: Any) -> Optional[StoredTask]:
    updated: Optional[StoredTask] = None

    def mutate(store: Dict[str, Any]) -> None:
        nonlocal updated
        for payload in store.get("tasks", []):
            if not isinstance(payload, dict):
                continue
            if str(payload.get("task_id", "")) != task_id:
                continue
            for key, value in fields.items():
                payload[key] = value
            payload["updated_at"] = _now_iso()
            updated = StoredTask.from_payload(payload)
            break

    _mutate_store(config, mutate, sync_markdown=True)
    return updated


def apply_plan_result(config: Dict[str, Any],
                      task_id: str,
                      *,
                      plan_payload: Dict[str, Any],
                      artifact_dir: str) -> Optional[StoredTask]:
    return update_task_fields(
        config,
        task_id,
        phase="ready",
        plan_status="ready",
        plan=plan_payload,
        artifact_dir=artifact_dir,
        last_result_status="planned",
        last_summary=str(plan_payload.get("summary", "")),
        last_error="",
    )


def update_task_phase(config: Dict[str, Any],
                      task_id: str,
                      phase: str,
                      *,
                      artifact_dir: Optional[str] = None,
                      last_result_status: Optional[str] = None,
                      last_summary: Optional[str] = None,
                      last_error: Optional[str] = None) -> Optional[StoredTask]:
    fields: Dict[str, Any] = {"phase": phase}
    if artifact_dir is not None:
        fields["artifact_dir"] = artifact_dir
    if last_result_status is not None:
        fields["last_result_status"] = last_result_status
    if last_summary is not None:
        fields["last_summary"] = last_summary
    if last_error is not None:
        fields["last_error"] = last_error
    updated = update_task_fields(config, task_id, **fields)
    if updated and updated.source_kind == "markdown" and phase in ("completed", "needs_testing"):
        update_task_status(Path(config["task_file"]), task_id, "completed" if phase == "completed" else "needs_testing")
    return updated


def phase_labels(config: Dict[str, Any]) -> List[str]:
    return list(config.get("store", {}).get("phases", DEFAULT_PHASES))
