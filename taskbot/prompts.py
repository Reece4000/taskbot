from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def _format_file_hints(file_hints: Iterable[Tuple[str, List[str], float]]) -> str:
    lines = []
    for rel_path, symbols, score in file_hints:
        symbol_text = ", ".join(symbols[:6]) if symbols else "no symbols cached"
        lines.append("- {0} | score={1:.1f} | {2}".format(rel_path, score, symbol_text))
    return "\n".join(lines) if lines else "- no strong file matches were found in the static index"


def _format_history(history: List[Dict[str, Any]]) -> str:
    if not history:
        return "No previous taskbot attempts recorded for this task."
    parts = []
    for item in history:
        status = item.get("status", "unknown")
        summary = item.get("summary", "").strip()
        started_at = item.get("started_at", "unknown")
        parts.append("- {0} | {1} | {2}".format(started_at, status, summary))
    return "\n".join(parts)


def _format_verification_commands(config: Dict[str, Any]) -> str:
    if _verification_mode(config) == "manual":
        return "- skipped because verification.mode is manual"
    parts = []
    for entry in config["verification"]["commands"]:
        if not entry.get("enabled", True):
            continue
        command = " ".join(entry["command"])
        parts.append("- {0}: `{1}`".format(entry["name"], command))
    return "\n".join(parts) if parts else "- none configured"


def _verification_mode(config: Dict[str, Any]) -> str:
    verification = config.get("verification", {})
    mode = str(verification.get("mode", "auto")).strip().lower()
    if mode in {"manual", "commands"}:
        return mode
    commands = verification.get("commands", [])
    has_commands = any(isinstance(entry, dict) and entry.get("enabled", True) for entry in commands)
    return "commands" if has_commands else "manual"


def _format_verification_policy(config: Dict[str, Any]) -> str:
    instructions = str(config.get("verification", {}).get("instructions", "") or "").strip()
    mode = _verification_mode(config)
    if mode == "manual":
        base = "- mode: manual\n- automated commands: disabled\n- expectation: leave concise manual follow-up steps when runtime confirmation is still needed"
    else:
        base = "- mode: commands\n- automated commands: outer runner will execute the configured verification hooks after implementation"
    if instructions:
        return "{0}\n- repo notes: {1}".format(base, instructions)
    return base


def _task_context_payload(task: Any) -> Dict[str, Any]:
    context_notes = str(getattr(task, "context_notes", "") or "").strip()
    file_targets = [str(item) for item in getattr(task, "file_targets", [])]
    acceptance = [str(item) for item in getattr(task, "acceptance", [])]
    plan_status = str(getattr(task, "plan_status", "") or "").strip()
    return {
        "context_notes": context_notes,
        "file_targets": file_targets,
        "acceptance": acceptance,
        "plan_status": plan_status,
    }


def _task_section(task: Any) -> str:
    return str(getattr(task, "board_title", getattr(task, "section", "General"))).strip()


def _task_text(task: Any) -> str:
    return str(getattr(task, "title", getattr(task, "text", ""))).strip()


def _task_status(task: Any) -> str:
    return str(getattr(task, "phase", getattr(task, "status", ""))).strip()


def build_plan_prompt(repo_root: Path,
                      config: Dict[str, Any],
                      task: Any,
                      file_hints: List[Tuple[str, List[str], float]],
                      history: List[Dict[str, Any]]) -> str:
    subagent_text = "Allowed" if config["codex"].get("allow_subagents", True) else "Not allowed"
    phase_text = ", ".join(str(item) for item in config.get("store", {}).get("phases", []))
    payload = {
        "repo_root": str(repo_root),
        "task_id": task.task_id,
        "section": _task_section(task),
        "task_text": _task_text(task),
        "current_status": _task_status(task),
        "file_hints": [item[0] for item in file_hints],
        "task_context": _task_context_payload(task),
    }
    return """You are creating an implementation plan for a repo-local Codex task loop.

Do not edit files. Inspect only what you need.

Task metadata:
{metadata}

Context file hints:
{file_hints}

Recent task history:
{history}

Verification hooks available after implementation:
{verification}

Verification policy:
{verification_policy}

Constraints:
- Preserve user-visible behaviour unless a deviation is explicitly justified.
- Keep the plan narrow and task-specific.
- Optimise for low token usage: inspect targeted files only.
- Respect the repository's existing architecture and conventions.
- If the task is obviously tiny and localised, keep the plan compact: 1-3 short steps and no filler detail.
- Treat zero-match search probes as informational, not as blockers.
- If the selected task is too large for one clean implementation pass, set `decomposition.should_split` to true and break it into 2-6 small modular subtasks with ready-to-run plans.
- Each decomposed subtask must include a `board_title`. Reuse the existing board unless a new board materially improves organisation.
- Each decomposed subtask must have its own concise context, acceptance list, workflow phase, and full plan payload.
- Valid workflow phases for subtasks: {phases}
- Subagent delegation: {subagents}

Return JSON only matching the provided schema.
""".format(
        metadata=json.dumps(payload, indent=2),
        file_hints=_format_file_hints(file_hints),
        history=_format_history(history),
        verification=_format_verification_commands(config),
        verification_policy=_format_verification_policy(config),
        phases=phase_text or "backlog, planning, ready, in_progress, needs_testing, blocked, completed",
        subagents=subagent_text,
    )


def build_implementation_prompt(repo_root: Path,
                                config: Dict[str, Any],
                                task: Any,
                                file_hints: List[Tuple[str, List[str], float]],
                                history: List[Dict[str, Any]],
                                plan_payload: Dict[str, Any]) -> str:
    implementation_limit = int(config["context"]["implementation_file_limit"])
    narrowed_hints = file_hints[:implementation_limit]
    subagent_text = (
        "You may delegate to subagents if the work cleanly splits into disjoint file ownership."
        if config["codex"].get("allow_subagents", True)
        else "Do not use subagents."
    )
    return """You are running inside a repo-local automated task loop for this project.

Implement the selected task. Keep repo exploration targeted and minimal.

Selected task:
- id: {task_id}
- section: {section}
- text: {text}
- current status: {status}

Static file hints:
{file_hints}

Previous attempts:
{history}

Stored task context:
{task_context}

Approved plan:
{plan}

Execution rules:
- Make the code changes needed for this task.
- Do not edit the task markdown or task store files directly; the outer runner manages task status.
- Inspect files surgically with `rg` and `sed -n` instead of broad scans.
- Preserve established behaviour and interfaces unless the task itself requires a change.
- Treat zero-match search commands as normal and move on without retry loops.
- If verification mode is manual or no commands are configured, do not invent broad automated test runs or keep retrying failing speculative checks.
- In manual-verification repos, prefer a safe static sanity check only when it is trivial and sandbox-safe; otherwise leave precise manual follow-up notes and mark `needs_testing`.
- If the task cannot be confidently runtime-verified with the available hooks, prefer `needs_testing` over `completed`.
- {subagents}

Verification commands the outer runner will execute after your turn:
{verification}

Verification policy:
{verification_policy}

Return JSON only matching the provided schema.
""".format(
        task_id=task.task_id,
        section=_task_section(task),
        text=_task_text(task),
        status=_task_status(task),
        file_hints=_format_file_hints(narrowed_hints),
        history=_format_history(history),
        task_context=json.dumps(_task_context_payload(task), indent=2),
        plan=json.dumps(plan_payload, indent=2),
        subagents=subagent_text,
        verification=_format_verification_commands(config),
        verification_policy=_format_verification_policy(config),
    )
