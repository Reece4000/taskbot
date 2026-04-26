from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class GitCommandResult:
    command: List[str]
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class GitSessionState:
    enabled: bool
    repo_available: bool
    publishable_dirty_files_at_start: List[str]
    staged_files_at_start: List[str]

    @property
    def clean_at_start(self) -> bool:
        return not self.publishable_dirty_files_at_start and not self.staged_files_at_start


@dataclass
class GitPublishResult:
    status: str
    reason: str
    branch: str
    remote: str
    commit_message: str
    commit_sha: str
    changed_files: List[str]
    dirty_at_start: bool
    commit_created: bool
    push_attempted: bool
    push_succeeded: bool

    def to_payload(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "branch": self.branch,
            "remote": self.remote,
            "commit_message": self.commit_message,
            "commit_sha": self.commit_sha,
            "changed_files": list(self.changed_files),
            "dirty_at_start": self.dirty_at_start,
            "commit_created": self.commit_created,
            "push_attempted": self.push_attempted,
            "push_succeeded": self.push_succeeded,
        }


def _git_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    settings = config.get("git", {})
    return settings if isinstance(settings, dict) else {}


def _run_git_capture(repo_root: Path, args: List[str]) -> GitCommandResult:
    command = ["git", *args]
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return GitCommandResult(command=command, exit_code=127, stdout="", stderr="git executable not found\n")

    return GitCommandResult(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _run_git_logged(repo_root: Path, args: List[str], log_dir: Path, name: str) -> GitCommandResult:
    result = _run_git_capture(repo_root, args)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "{0}.stdout.log".format(name)).write_text(result.stdout, encoding="utf-8")
    (log_dir / "{0}.stderr.log".format(name)).write_text(result.stderr, encoding="utf-8")
    return result


def _split_lines(text: str) -> List[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _excluded_repo_paths(config: Dict[str, Any]) -> List[str]:
    repo_root = Path(config["repo_root"]).resolve()
    values = [
        config.get("state_dir", ""),
        config.get("artifact_dir", ""),
        config.get("control_dir", ""),
        config.get("task_file", ""),
        config.get("store", {}).get("path", ""),
    ]
    excluded: List[str] = []
    for raw_value in values:
        text = str(raw_value or "").strip()
        if not text:
            continue
        candidate = Path(text).expanduser()
        resolved = candidate.resolve() if candidate.is_absolute() else (repo_root / candidate).resolve()
        try:
            relative_path = resolved.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        cleaned = relative_path.strip("/")
        if cleaned:
            excluded.append(cleaned)
    return sorted(set(excluded))


def _is_excluded_path(path_text: str, excluded_paths: List[str]) -> bool:
    candidate = path_text.replace("\\", "/").strip().lstrip("./")
    if not candidate:
        return False
    for excluded in excluded_paths:
        if candidate == excluded or candidate.startswith(excluded.rstrip("/") + "/"):
            return True
    return False


def _filter_publishable_files(paths: List[str], excluded_paths: List[str]) -> List[str]:
    publishable: List[str] = []
    seen = set()
    for path_text in paths:
        cleaned = path_text.replace("\\", "/").strip()
        if not cleaned or cleaned in seen or _is_excluded_path(cleaned, excluded_paths):
            continue
        seen.add(cleaned)
        publishable.append(cleaned)
    return publishable


def _git_changed_files(repo_root: Path, excluded_paths: List[str]) -> Dict[str, List[str]]:
    worktree = _run_git_capture(repo_root, ["diff", "--name-only", "--relative", "--"])
    staged = _run_git_capture(repo_root, ["diff", "--cached", "--name-only", "--relative", "--"])
    untracked = _run_git_capture(repo_root, ["ls-files", "--others", "--exclude-standard", "--"])

    if worktree.exit_code != 0 or staged.exit_code != 0 or untracked.exit_code != 0:
        return {
            "error": _first_error(
                [worktree, staged, untracked],
                "failed to inspect git worktree state",
            ),
            "worktree": [],
            "staged": [],
            "untracked": [],
        }

    return {
        "error": "",
        "worktree": _filter_publishable_files(_split_lines(worktree.stdout), excluded_paths),
        "staged": _split_lines(staged.stdout),
        "untracked": _filter_publishable_files(_split_lines(untracked.stdout), excluded_paths),
    }


def capture_git_session_state(repo_root: Path, config: Dict[str, Any]) -> GitSessionState:
    settings = _git_settings(config)
    if not bool(settings.get("enabled", False)):
        return GitSessionState(
            enabled=False,
            repo_available=False,
            publishable_dirty_files_at_start=[],
            staged_files_at_start=[],
        )

    repo_check = _run_git_capture(repo_root, ["rev-parse", "--is-inside-work-tree"])
    if repo_check.exit_code != 0 or repo_check.stdout.strip() != "true":
        return GitSessionState(
            enabled=True,
            repo_available=False,
            publishable_dirty_files_at_start=[],
            staged_files_at_start=[],
        )

    excluded_paths = _excluded_repo_paths(config)
    changed = _git_changed_files(repo_root, excluded_paths)
    if changed["error"]:
        return GitSessionState(
            enabled=True,
            repo_available=True,
            publishable_dirty_files_at_start=["<error: {0}>".format(changed["error"])],
            staged_files_at_start=[],
        )

    publishable_dirty = changed["worktree"] + changed["untracked"]
    staged_files = changed["staged"]
    return GitSessionState(
        enabled=True,
        repo_available=True,
        publishable_dirty_files_at_start=publishable_dirty,
        staged_files_at_start=staged_files,
    )


def _format_commit_message(template: str,
                           task: Any,
                           *,
                           branch: str,
                           report_status: str,
                           final_phase: str) -> str:
    try:
        rendered = template.format(
            task_id=str(getattr(task, "task_id", "")).strip(),
            task_title=str(getattr(task, "title", "")).strip(),
            board_title=str(getattr(task, "board_title", "")).strip(),
            branch=branch,
            report_status=report_status,
            final_phase=final_phase,
        )
    except KeyError as exc:
        raise ValueError(
            "invalid git.commit_message_template placeholder: {0}".format(exc.args[0])
        ) from exc
    message = re.sub(r"\s+", " ", rendered).strip()
    if not message:
        raise ValueError("git.commit_message_template rendered an empty commit message")
    return message


def _first_error(results: List[GitCommandResult], fallback: str) -> str:
    for result in results:
        stderr = result.stderr.strip()
        if stderr:
            return stderr
        stdout = result.stdout.strip()
        if stdout:
            return stdout
    return fallback


def _select_push_command(repo_root: Path,
                         settings: Dict[str, Any],
                         *,
                         branch: str,
                         log_dir: Path) -> tuple[Optional[List[str]], str, str]:
    configured_remote = str(settings.get("remote", "") or "").strip()
    if configured_remote:
        return (["push", configured_remote, "HEAD:{0}".format(branch)], configured_remote, "")

    upstream = _run_git_logged(
        repo_root,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        log_dir,
        "upstream",
    )
    if upstream.exit_code == 0 and upstream.stdout.strip():
        upstream_ref = upstream.stdout.strip()
        remote = upstream_ref.split("/", 1)[0]
        return (["push"], remote, "")

    remotes = _run_git_logged(repo_root, ["remote"], log_dir, "remotes")
    if remotes.exit_code == 0:
        remote_names = _split_lines(remotes.stdout)
        if len(remote_names) == 1:
            remote = remote_names[0]
            return (["push", remote, "HEAD:{0}".format(branch)], remote, "")

    return (None, "", "no git upstream is configured for the active branch and no git.remote override is set")


def publish_git_changes(repo_root: Path,
                        config: Dict[str, Any],
                        artifact_dir: Path,
                        task: Any,
                        report: Dict[str, Any],
                        final_phase: str,
                        session_state: GitSessionState) -> GitPublishResult:
    settings = _git_settings(config)
    if not session_state.enabled:
        return GitPublishResult(
            status="disabled",
            reason="git integration is disabled",
            branch="",
            remote="",
            commit_message="",
            commit_sha="",
            changed_files=[],
            dirty_at_start=False,
            commit_created=False,
            push_attempted=False,
            push_succeeded=False,
        )

    if not session_state.repo_available:
        return GitPublishResult(
            status="skipped",
            reason="repository is not a git worktree",
            branch="",
            remote="",
            commit_message="",
            commit_sha="",
            changed_files=[],
            dirty_at_start=False,
            commit_created=False,
            push_attempted=False,
            push_succeeded=False,
        )

    require_clean = bool(settings.get("require_clean_worktree", True))
    dirty_at_start = not session_state.clean_at_start
    if require_clean and dirty_at_start:
        if session_state.staged_files_at_start:
            reason = "git integration skipped because the index already had staged changes at session start"
        else:
            reason = "git integration skipped because the worktree already had publishable changes at session start"
        return GitPublishResult(
            status="skipped",
            reason=reason,
            branch="",
            remote="",
            commit_message="",
            commit_sha="",
            changed_files=[],
            dirty_at_start=True,
            commit_created=False,
            push_attempted=False,
            push_succeeded=False,
        )

    log_dir = artifact_dir / "git"
    excluded_paths = _excluded_repo_paths(config)

    branch_result = _run_git_logged(repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"], log_dir, "branch")
    if branch_result.exit_code != 0 or not branch_result.stdout.strip():
        return GitPublishResult(
            status="skipped",
            reason="git integration requires a checked-out branch and does not run on detached HEAD",
            branch="",
            remote="",
            commit_message="",
            commit_sha="",
            changed_files=[],
            dirty_at_start=dirty_at_start,
            commit_created=False,
            push_attempted=False,
            push_succeeded=False,
        )
    branch = branch_result.stdout.strip()

    current_changes = _git_changed_files(repo_root, excluded_paths)
    if current_changes["error"]:
        return GitPublishResult(
            status="failed",
            reason=current_changes["error"],
            branch=branch,
            remote="",
            commit_message="",
            commit_sha="",
            changed_files=[],
            dirty_at_start=dirty_at_start,
            commit_created=False,
            push_attempted=False,
            push_succeeded=False,
        )

    paths_to_stage: List[str] = []
    seen_paths = set()
    for path_text in current_changes["worktree"] + current_changes["untracked"]:
        if path_text in seen_paths:
            continue
        seen_paths.add(path_text)
        paths_to_stage.append(path_text)

    if paths_to_stage:
        add_result = _run_git_logged(repo_root, ["add", "-A", "--", *paths_to_stage], log_dir, "add")
        if add_result.exit_code != 0:
            return GitPublishResult(
                status="failed",
                reason=_first_error([add_result], "git add failed"),
                branch=branch,
                remote="",
                commit_message="",
                commit_sha="",
                changed_files=[],
                dirty_at_start=dirty_at_start,
                commit_created=False,
                push_attempted=False,
                push_succeeded=False,
            )

    staged_result = _run_git_logged(repo_root, ["diff", "--cached", "--name-only", "--relative", "--"], log_dir, "staged")
    if staged_result.exit_code != 0:
        return GitPublishResult(
            status="failed",
            reason=_first_error([staged_result], "failed to inspect staged git changes"),
            branch=branch,
            remote="",
            commit_message="",
            commit_sha="",
            changed_files=[],
            dirty_at_start=dirty_at_start,
            commit_created=False,
            push_attempted=False,
            push_succeeded=False,
        )

    all_staged_files = _split_lines(staged_result.stdout)
    excluded_staged_files = [path_text for path_text in all_staged_files if _is_excluded_path(path_text, excluded_paths)]
    changed_files = [path_text for path_text in all_staged_files if path_text not in excluded_staged_files]

    if excluded_staged_files:
        return GitPublishResult(
            status="skipped",
            reason="git integration skipped because excluded taskbot runtime files are already staged",
            branch=branch,
            remote="",
            commit_message="",
            commit_sha="",
            changed_files=changed_files,
            dirty_at_start=dirty_at_start,
            commit_created=False,
            push_attempted=False,
            push_succeeded=False,
        )

    if not changed_files:
        return GitPublishResult(
            status="noop",
            reason="no publishable git changes were produced by this implementation session",
            branch=branch,
            remote="",
            commit_message="",
            commit_sha="",
            changed_files=[],
            dirty_at_start=dirty_at_start,
            commit_created=False,
            push_attempted=False,
            push_succeeded=False,
        )

    template = str(settings.get("commit_message_template", "taskbot: {task_id} {task_title}") or "").strip()
    try:
        commit_message = _format_commit_message(
            template,
            task,
            branch=branch,
            report_status=str(report.get("status", "unknown")).strip() or "unknown",
            final_phase=str(final_phase).strip() or "ready",
        )
    except ValueError as exc:
        return GitPublishResult(
            status="failed",
            reason=str(exc),
            branch=branch,
            remote="",
            commit_message="",
            commit_sha="",
            changed_files=changed_files,
            dirty_at_start=dirty_at_start,
            commit_created=False,
            push_attempted=False,
            push_succeeded=False,
        )

    commit_result = _run_git_logged(repo_root, ["commit", "-m", commit_message], log_dir, "commit")
    if commit_result.exit_code != 0:
        combined_text = "{0}\n{1}".format(commit_result.stdout, commit_result.stderr).lower()
        if "nothing to commit" in combined_text:
            return GitPublishResult(
                status="noop",
                reason="no publishable git changes were available to commit",
                branch=branch,
                remote="",
                commit_message=commit_message,
                commit_sha="",
                changed_files=[],
                dirty_at_start=dirty_at_start,
                commit_created=False,
                push_attempted=False,
                push_succeeded=False,
            )
        return GitPublishResult(
            status="failed",
            reason=_first_error([commit_result], "git commit failed"),
            branch=branch,
            remote="",
            commit_message=commit_message,
            commit_sha="",
            changed_files=changed_files,
            dirty_at_start=dirty_at_start,
            commit_created=False,
            push_attempted=False,
            push_succeeded=False,
        )

    head_result = _run_git_logged(repo_root, ["rev-parse", "HEAD"], log_dir, "head")
    commit_sha = head_result.stdout.strip() if head_result.exit_code == 0 else ""

    if not bool(settings.get("push_after_commit", True)):
        return GitPublishResult(
            status="committed",
            reason="created a local commit and skipped push because git.push_after_commit is disabled",
            branch=branch,
            remote="",
            commit_message=commit_message,
            commit_sha=commit_sha,
            changed_files=changed_files,
            dirty_at_start=dirty_at_start,
            commit_created=True,
            push_attempted=False,
            push_succeeded=False,
        )

    push_args, remote, push_reason = _select_push_command(repo_root, settings, branch=branch, log_dir=log_dir)
    if push_args is None:
        return GitPublishResult(
            status="committed",
            reason=push_reason,
            branch=branch,
            remote=remote,
            commit_message=commit_message,
            commit_sha=commit_sha,
            changed_files=changed_files,
            dirty_at_start=dirty_at_start,
            commit_created=True,
            push_attempted=False,
            push_succeeded=False,
        )

    push_result = _run_git_logged(repo_root, push_args, log_dir, "push")
    if push_result.exit_code != 0:
        return GitPublishResult(
            status="failed",
            reason=_first_error([push_result], "git push failed"),
            branch=branch,
            remote=remote,
            commit_message=commit_message,
            commit_sha=commit_sha,
            changed_files=changed_files,
            dirty_at_start=dirty_at_start,
            commit_created=True,
            push_attempted=True,
            push_succeeded=False,
        )

    return GitPublishResult(
        status="pushed",
        reason="commit created and pushed successfully",
        branch=branch,
        remote=remote,
        commit_message=commit_message,
        commit_sha=commit_sha,
        changed_files=changed_files,
        dirty_at_start=dirty_at_start,
        commit_created=True,
        push_attempted=True,
        push_succeeded=True,
    )
