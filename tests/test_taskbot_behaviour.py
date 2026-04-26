from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from taskbot.config import DEFAULT_CONFIG, load_config, save_config_overrides
from taskbot.git_integration import capture_git_session_state, publish_git_changes
from taskbot.runner import _build_tiny_task_plan, _should_fast_path_tiny_task
from taskbot.store import StoredTask
from taskbot.ui import (
    START_LOOP_DIALOG_DEFAULT_ITERATIONS,
    _command_enter_shortcut_sequences,
    _install_command_enter_shortcuts,
    _checkbox_indicator_tick_icon_path,
    RUNNER_CONTROL_TOOLTIPS,
    _config_path_label_for_header,
    _repo_agents_path,
    _start_loop_run_args,
    _terminal_text_should_refresh,
)
from taskbot.verification import run_verification_steps


class TaskbotBehaviourTests(unittest.TestCase):
    def _run_git(self, repo_root: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg="git {0} failed:\nstdout:\n{1}\nstderr:\n{2}".format(
                " ".join(args),
                completed.stdout,
                completed.stderr,
            ),
        )
        return completed.stdout.strip()

    def _create_git_repo_with_remote(self, root: Path) -> tuple[Path, Path]:
        repo_root = root / "repo"
        remote_root = root / "remote.git"
        repo_root.mkdir(parents=True, exist_ok=True)

        self._run_git(repo_root, "init")
        self._run_git(repo_root, "checkout", "-b", "main")
        self._run_git(repo_root, "config", "user.name", "Taskbot Tests")
        self._run_git(repo_root, "config", "user.email", "taskbot@example.com")

        (repo_root / "app.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "app.txt")
        self._run_git(repo_root, "commit", "-m", "initial")

        init_remote = subprocess.run(
            ["git", "init", "--bare", str(remote_root)],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(
            init_remote.returncode,
            0,
            msg="git init --bare failed:\nstdout:\n{0}\nstderr:\n{1}".format(
                init_remote.stdout,
                init_remote.stderr,
            ),
        )

        self._run_git(repo_root, "remote", "add", "origin", str(remote_root))
        self._run_git(repo_root, "push", "-u", "origin", "main")
        return repo_root, remote_root

    def _example_stored_task(self) -> StoredTask:
        return StoredTask(
            task_id="engineering-1234",
            board_id="engineering",
            board_title="Engineering",
            title="Implement session git publishing",
            phase="in_progress",
            context_notes="",
            file_targets=[],
            acceptance=[],
            source_kind="ui",
            source_line_index=-1,
            plan_status="ready",
            plan={},
            artifact_dir="",
            last_result_status="",
            last_summary="",
            last_error="",
            order=0,
            created_at="",
            updated_at="",
        )

    def test_save_config_overrides_persists_repo_local_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(repo_root, None, app_root=repo_root)
            saved_path = save_config_overrides(
                config,
                {
                    "codex": {"sandbox": "danger-full-access"},
                    "planning": {"auto_plan_tiny_tasks": False},
                    "verification": {
                        "mode": "commands",
                        "instructions": "Run smoke checks only.",
                        "commands": [
                            {
                                "name": "unit",
                                "command": ["python3", "-m", "unittest"],
                                "enabled": True,
                                "timeout_seconds": 300,
                            }
                        ],
                    },
                    "git": {
                        "enabled": True,
                        "push_after_commit": True,
                        "require_clean_worktree": True,
                        "remote": "origin",
                        "commit_message_template": "taskbot: {task_id} {task_title}",
                    },
                },
            )

            self.assertEqual(saved_path, (repo_root / "_taskbot" / "config.json").resolve())
            saved_payload = json.loads(saved_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_payload["codex"]["sandbox"], "danger-full-access")
            self.assertFalse(saved_payload["planning"]["auto_plan_tiny_tasks"])
            self.assertEqual(saved_payload["verification"]["mode"], "commands")
            self.assertTrue(saved_payload["git"]["enabled"])
            self.assertEqual(saved_payload["git"]["remote"], "origin")

            reloaded = load_config(repo_root, saved_path, app_root=repo_root)
            self.assertEqual(reloaded["codex"]["sandbox"], "danger-full-access")
            self.assertFalse(reloaded["planning"]["auto_plan_tiny_tasks"])
            self.assertEqual(reloaded["verification"]["instructions"], "Run smoke checks only.")
            self.assertEqual(reloaded["git"]["commit_message_template"], "taskbot: {task_id} {task_title}")

    def test_config_path_header_label_prefers_repo_relative_display(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            default_config = load_config(repo_root, None, app_root=repo_root)

            self.assertEqual(_config_path_label_for_header(default_config), "defaults only")

            repo_local_config = dict(default_config)
            repo_local_config["config_path"] = str((repo_root / "_taskbot" / "config.json").resolve())
            self.assertEqual(
                _config_path_label_for_header(repo_local_config),
                "_taskbot/config.json",
            )

            with tempfile.TemporaryDirectory() as external_tmp:
                external_root = Path(external_tmp)
                external_config = dict(default_config)
                external_config["config_path"] = str((external_root / "config.json").resolve())
                self.assertEqual(
                    _config_path_label_for_header(external_config),
                    str((external_root / "config.json").resolve()),
                )

    def test_terminal_refresh_cache_treats_repo_switch_to_blank_as_a_refresh(self) -> None:
        self.assertTrue(_terminal_text_should_refresh("previous repo output", ""))
        self.assertTrue(_terminal_text_should_refresh(None, ""))
        self.assertFalse(_terminal_text_should_refresh("", ""))
        self.assertTrue(_terminal_text_should_refresh(None, "fresh repo output"))

    def test_runner_control_tooltips_describe_the_controls(self) -> None:
        self.assertEqual(
            RUNNER_CONTROL_TOOLTIPS,
            {
                "plan_once": "Run the planner for the next runnable task once, then stop.",
                "run_once": (
                    "Run one full task pass for the next runnable task, including implementation "
                    "and verification, then stop."
                ),
                "start_loop": "Keep running full task passes until you press Stop.",
                "stop": "Request the active runner to stop after the current phase finishes.",
            },
        )

    def test_start_loop_dialog_defaults_and_runner_args(self) -> None:
        self.assertEqual(START_LOOP_DIALOG_DEFAULT_ITERATIONS, 5)
        self.assertEqual(
            _start_loop_run_args(True, START_LOOP_DIALOG_DEFAULT_ITERATIONS),
            ["run", "--continuous"],
        )
        self.assertEqual(
            _start_loop_run_args(False, START_LOOP_DIALOG_DEFAULT_ITERATIONS),
            ["run", "--iterations", "5"],
        )

    def test_command_enter_shortcuts_cover_return_and_enter(self) -> None:
        created = []

        class FakeSignal:
            def __init__(self) -> None:
                self.connected = None

            def connect(self, callback) -> None:
                self.connected = callback

        class FakeShortcut:
            def __init__(self, key_sequence, parent) -> None:
                self.key_sequence = key_sequence
                self.parent = parent
                self.context = None
                self.activated = FakeSignal()
                created.append(self)

            def setContext(self, context) -> None:
                self.context = context

        def fake_key_sequence(value: str) -> str:
            return "sequence:{0}".format(value)

        callback = object()
        shortcuts = _install_command_enter_shortcuts(
            "dialog-parent",
            callback,
            FakeShortcut,
            fake_key_sequence,
            "window-shortcut",
        )

        self.assertEqual(_command_enter_shortcut_sequences(), ("Meta+Return", "Meta+Enter"))
        self.assertEqual(shortcuts, created)
        self.assertEqual([shortcut.key_sequence for shortcut in shortcuts], [
            "sequence:Meta+Return",
            "sequence:Meta+Enter",
        ])
        self.assertEqual([shortcut.parent for shortcut in shortcuts], ["dialog-parent", "dialog-parent"])
        self.assertEqual([shortcut.context for shortcut in shortcuts], ["window-shortcut", "window-shortcut"])
        self.assertTrue(all(shortcut.activated.connected is callback for shortcut in shortcuts))

    def test_checkbox_indicator_tick_icon_points_to_svg_asset(self) -> None:
        icon_path = _checkbox_indicator_tick_icon_path()

        self.assertEqual(icon_path.name, "checkbox-tick.svg")
        self.assertTrue(icon_path.exists())
        self.assertTrue(icon_path.is_file())

    def test_manual_verification_skips_command_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(repo_root, None, app_root=repo_root)
            config["verification"] = {
                "mode": "manual",
                "instructions": "Manual QA only.",
                "commands": [
                    {
                        "name": "should-not-run",
                        "command": ["python3", "-c", "raise SystemExit(9)"],
                        "enabled": True,
                        "timeout_seconds": 300,
                    }
                ],
            }
            config["ui"]["terminal_log"] = str(repo_root / "_taskbot" / "control" / "terminal.log")
            artifact_dir = repo_root / "_taskbot" / "artifacts" / "manual-check"

            results = run_verification_steps(repo_root, config, artifact_dir)

            self.assertEqual(results, [])
            summary_path = artifact_dir / "verification.summary.json"
            self.assertTrue(summary_path.exists())
            self.assertEqual(json.loads(summary_path.read_text(encoding="utf-8")), [])

    def test_tiny_ui_tasks_use_fast_path_plan(self) -> None:
        task = StoredTask(
            task_id="ux-1234",
            board_id="ux",
            board_title="UX",
            title="tooltip spacing fix",
            phase="backlog",
            context_notes="Small UI stylesheet tweak in the task board header.",
            file_targets=[],
            acceptance=[],
            source_kind="ui",
            source_line_index=-1,
            plan_status="pending",
            plan={},
            artifact_dir="",
            last_result_status="",
            last_summary="",
            last_error="",
            order=0,
            created_at="",
            updated_at="",
        )
        config = {
            "planning": {"auto_plan_tiny_tasks": True},
            "verification": DEFAULT_CONFIG["verification"],
        }
        file_hints = [("taskbot/ui.py", ["launch_ui"], 18.0)]

        self.assertTrue(_should_fast_path_tiny_task(task, file_hints, config))
        plan = _build_tiny_task_plan(task, file_hints, config)
        self.assertEqual(plan["relevant_files"], ["taskbot/ui.py"])
        self.assertFalse(plan["decomposition"]["should_split"])
        self.assertIn("skip a separate planning pass", plan["summary"])

    def test_repo_agents_path_prefers_existing_repo_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)

            self.assertEqual(_repo_agents_path(repo_root), (repo_root / "agents.md").resolve())

            upper = repo_root / "AGENTS.md"
            upper.write_text("upper\n", encoding="utf-8")
            selected_after_upper = _repo_agents_path(repo_root)
            self.assertEqual(selected_after_upper.read_text(encoding="utf-8"), "upper\n")
            self.assertEqual(selected_after_upper.name.lower(), "agents.md")

            lower = repo_root / "agents.md"
            lower.write_text("lower\n", encoding="utf-8")
            selected_after_lower = _repo_agents_path(repo_root)
            self.assertEqual(selected_after_lower.read_text(encoding="utf-8"), "lower\n")
            self.assertEqual(selected_after_lower.name.lower(), "agents.md")

    def test_git_publish_commits_and_pushes_session_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, _remote_root = self._create_git_repo_with_remote(Path(tmp))
            config = load_config(repo_root, None, app_root=repo_root)
            config["git"] = {
                "enabled": True,
                "push_after_commit": True,
                "require_clean_worktree": True,
                "remote": "",
                "commit_message_template": "taskbot: {task_id} {task_title}",
            }

            (repo_root / "_taskbot" / "control").mkdir(parents=True, exist_ok=True)
            (repo_root / "_taskbot" / "control" / "terminal.log").write_text("internal log\n", encoding="utf-8")
            session_state = capture_git_session_state(repo_root, config)
            self.assertTrue(session_state.clean_at_start)

            (repo_root / "app.txt").write_text("base\nchange\n", encoding="utf-8")
            (repo_root / "_taskbot" / "state").mkdir(parents=True, exist_ok=True)
            (repo_root / "_taskbot" / "state" / "history.jsonl").write_text("{}\n", encoding="utf-8")

            artifact_dir = repo_root / "_taskbot" / "artifacts" / "git-publish"
            result = publish_git_changes(
                repo_root,
                config,
                artifact_dir,
                self._example_stored_task(),
                {"status": "completed"},
                "completed",
                session_state,
            )

            self.assertEqual(result.status, "pushed")
            self.assertTrue(result.commit_created)
            self.assertTrue(result.push_attempted)
            self.assertTrue(result.push_succeeded)
            self.assertEqual(result.branch, "main")
            self.assertIn("app.txt", result.changed_files)

            self.assertEqual(self._run_git(repo_root, "rev-parse", "HEAD"), self._run_git(repo_root, "rev-parse", "@{upstream}"))
            changed_names = self._run_git(repo_root, "show", "--name-only", "--pretty=format:", "HEAD").splitlines()
            self.assertIn("app.txt", changed_names)
            self.assertNotIn("_taskbot/state/history.jsonl", changed_names)

    def test_git_publish_skips_when_publishable_changes_exist_at_session_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, _remote_root = self._create_git_repo_with_remote(Path(tmp))
            config = load_config(repo_root, None, app_root=repo_root)
            config["git"] = {
                "enabled": True,
                "push_after_commit": True,
                "require_clean_worktree": True,
                "remote": "",
                "commit_message_template": "taskbot: {task_id} {task_title}",
            }

            initial_head = self._run_git(repo_root, "rev-parse", "HEAD")
            (repo_root / "app.txt").write_text("base\ndirty before session\n", encoding="utf-8")

            session_state = capture_git_session_state(repo_root, config)
            self.assertFalse(session_state.clean_at_start)
            self.assertIn("app.txt", session_state.publishable_dirty_files_at_start)

            artifact_dir = repo_root / "_taskbot" / "artifacts" / "git-skip"
            result = publish_git_changes(
                repo_root,
                config,
                artifact_dir,
                self._example_stored_task(),
                {"status": "completed"},
                "completed",
                session_state,
            )

            self.assertEqual(result.status, "skipped")
            self.assertIn("session start", result.reason)
            self.assertEqual(self._run_git(repo_root, "rev-parse", "HEAD"), initial_head)


if __name__ == "__main__":
    unittest.main()
