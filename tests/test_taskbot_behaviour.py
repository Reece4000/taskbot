from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from taskbot.codex_cli import CodexRunResult
from taskbot.config import DEFAULT_CONFIG, load_config, save_config_overrides
from taskbot.git_integration import capture_git_session_state, publish_git_changes
from taskbot.runner import _build_tiny_task_plan, _run_task_once, _should_fast_path_tiny_task
from taskbot.store import StoredTask, create_task, update_task_fields
from taskbot.tasks import parse_tasks, rename_board as rename_markdown_board
from taskbot.ui import (
    START_LOOP_DIALOG_DEFAULT_ITERATIONS,
    _command_enter_shortcut_sequences,
    _command_enter_dialog_candidate,
    _command_enter_should_activate,
    _command_enter_submit_dialog,
    _command_enter_preferred_button,
    _install_command_enter_shortcuts,
    _checkbox_indicator_tick_icon_path,
    _trash_icon_path,
    RUNNER_CONTROL_TOOLTIPS,
    _config_path_label_for_header,
    _repo_agents_path,
    _start_loop_run_args,
    _terminal_text_should_refresh,
    _taskbot_title_html,
)
from taskbot.verification import VerificationResult, run_verification_steps


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

        (repo_root / ".gitignore").write_text("_taskbot/\n", encoding="utf-8")
        (repo_root / "app.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", ".gitignore", "app.txt")
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

    def test_taskbot_title_html_uses_colored_letter_spans(self) -> None:
        title_html = _taskbot_title_html()

        self.assertEqual(re.sub(r"<[^>]+>", "", title_html), "Taskbot")
        self.assertEqual(title_html.count("<span style=\"color:"), 7)
        self.assertTrue(title_html.startswith('<span style="color:#c8643b;">T'))

    def test_rename_markdown_board_rewrites_empty_section_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_file = Path(tmp) / "_tasks.md"
            task_file.write_text(
                "# Old Board\n\n# Other Board\n- unrelated task\n",
                encoding="utf-8",
            )

            remap = rename_markdown_board(task_file, "Old Board", "Renamed Board")

            self.assertEqual(remap, {})
            rewritten = task_file.read_text(encoding="utf-8")
            self.assertIn("# Renamed Board\n", rewritten)
            self.assertNotIn("# Old Board\n", rewritten)
            self.assertEqual(parse_tasks(task_file)[0].section, "Other Board")

    def test_rename_markdown_board_rewrites_populated_section_and_task_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_file = Path(tmp) / "_tasks.md"
            task_file.write_text(
                "# Old Board\n- First task\n",
                encoding="utf-8",
            )

            original_task = parse_tasks(task_file)[0]
            remap = rename_markdown_board(task_file, "Old Board", "Renamed Board")
            renamed_task = parse_tasks(task_file)[0]

            self.assertEqual(remap, {original_task.task_id: renamed_task.task_id})
            self.assertEqual(renamed_task.section, "Renamed Board")

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

    def test_command_enter_preferred_button_prefers_ok_label_over_default(self) -> None:
        class FakeButton:
            def __init__(self, text: str, enabled: bool = True, default: bool = False) -> None:
                self._text = text
                self._enabled = enabled
                self._default = default

            def text(self) -> str:
                return self._text

            def isEnabled(self) -> bool:
                return self._enabled

            def isDefault(self) -> bool:
                return self._default

        default_button = FakeButton("Cancel", default=True)
        ok_button = FakeButton("&OK")

        self.assertIs(
            _command_enter_preferred_button([default_button, ok_button]),
            ok_button,
        )

    def test_command_enter_preferred_button_falls_back_to_default_button(self) -> None:
        class FakeButton:
            def __init__(self, text: str, enabled: bool = True, default: bool = False) -> None:
                self._text = text
                self._enabled = enabled
                self._default = default

            def text(self) -> str:
                return self._text

            def isEnabled(self) -> bool:
                return self._enabled

            def isDefault(self) -> bool:
                return self._default

        default_button = FakeButton("Save", default=True)
        cancel_button = FakeButton("Cancel")

        self.assertIs(
            _command_enter_preferred_button([cancel_button, default_button]),
            default_button,
        )

    def test_command_enter_dialog_candidate_prefers_modal_widget_and_falls_back_to_window(self) -> None:
        modal_widget = object()
        watched_window = object()

        class WatchedWithWindow:
            def __init__(self, window) -> None:
                self._window = window

            def window(self):
                return self._window

        self.assertIs(
            _command_enter_dialog_candidate(WatchedWithWindow(watched_window), modal_widget),
            modal_widget,
        )
        self.assertIs(
            _command_enter_dialog_candidate(WatchedWithWindow(watched_window), None),
            watched_window,
        )
        self.assertIs(_command_enter_dialog_candidate(object(), None), None)

    def test_command_enter_should_activate_matches_meta_return_and_enter(self) -> None:
        class FakeEvent:
            def __init__(self, event_type: str, key: str, modifiers: int) -> None:
                self._event_type = event_type
                self._key = key
                self._modifiers = modifiers

            def type(self) -> str:
                return self._event_type

            def key(self) -> str:
                return self._key

            def modifiers(self) -> int:
                return self._modifiers

        activation_event_types = ("ShortcutOverride", "KeyPress")
        return_keys = ("Return", "Enter")
        meta_modifier = 0x01

        self.assertTrue(
            _command_enter_should_activate(
                FakeEvent("ShortcutOverride", "Return", meta_modifier),
                activation_event_types,
                return_keys,
                meta_modifier,
            )
        )
        self.assertTrue(
            _command_enter_should_activate(
                FakeEvent("KeyPress", "Enter", meta_modifier),
                activation_event_types,
                return_keys,
                meta_modifier,
            )
        )
        self.assertFalse(
            _command_enter_should_activate(
                FakeEvent("KeyPress", "Return", 0x00),
                activation_event_types,
                return_keys,
                meta_modifier,
            )
        )
        self.assertFalse(
            _command_enter_should_activate(
                FakeEvent("KeyPress", "Space", meta_modifier),
                activation_event_types,
                return_keys,
                meta_modifier,
            )
        )

    def test_command_enter_submit_dialog_clicks_preferred_ok_button(self) -> None:
        class FakeButton:
            def __init__(self, text: str, enabled: bool = True, default: bool = False) -> None:
                self._text = text
                self._enabled = enabled
                self._default = default
                self.clicked = 0

            def text(self) -> str:
                return self._text

            def isEnabled(self) -> bool:
                return self._enabled

            def isDefault(self) -> bool:
                return self._default

            def click(self) -> None:
                self.clicked += 1

        class FakeDialog:
            def __init__(self, buttons, default_button=None) -> None:
                self._buttons = buttons
                self._default_button = default_button

            def inherits(self, name: str) -> bool:
                return name == "QDialog"

            def findChildren(self, button_cls):
                return list(self._buttons)

            def defaultButton(self):
                return self._default_button

        ok_button = FakeButton("&OK")
        cancel_button = FakeButton("Cancel")
        dialog = FakeDialog([cancel_button, ok_button])

        self.assertTrue(_command_enter_submit_dialog(dialog, None, FakeButton))
        self.assertEqual(ok_button.clicked, 1)
        self.assertEqual(cancel_button.clicked, 0)

    def test_command_enter_submit_dialog_falls_back_to_default_button(self) -> None:
        class FakeButton:
            def __init__(self, text: str, enabled: bool = True, default: bool = False) -> None:
                self._text = text
                self._enabled = enabled
                self._default = default
                self.clicked = 0

            def text(self) -> str:
                return self._text

            def isEnabled(self) -> bool:
                return self._enabled

            def isDefault(self) -> bool:
                return self._default

            def click(self) -> None:
                self.clicked += 1

        class FakeDialog:
            def __init__(self, buttons, default_button=None) -> None:
                self._buttons = buttons
                self._default_button = default_button

            def inherits(self, name: str) -> bool:
                return name == "QDialog"

            def findChildren(self, button_cls):
                return list(self._buttons)

            def defaultButton(self):
                return self._default_button

        default_button = FakeButton("Save", default=True)
        cancel_button = FakeButton("Cancel")
        dialog = FakeDialog([cancel_button], default_button)

        self.assertTrue(_command_enter_submit_dialog(dialog, None, FakeButton))
        self.assertEqual(default_button.clicked, 1)
        self.assertEqual(cancel_button.clicked, 0)

    def test_checkbox_indicator_tick_icon_points_to_svg_asset(self) -> None:
        icon_path = _checkbox_indicator_tick_icon_path()

        self.assertEqual(icon_path.name, "checkbox-tick.svg")
        self.assertTrue(icon_path.exists())
        self.assertTrue(icon_path.is_file())

    def test_trash_icon_points_to_svg_asset(self) -> None:
        icon_path = _trash_icon_path()

        self.assertEqual(icon_path.name, "trash-can.svg")
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

    def test_tiny_task_fast_path_rejects_explicit_planning_intent(self) -> None:
        task = StoredTask(
            task_id="ux-1235",
            board_id="ux",
            board_title="UX",
            title="tooltip spacing fix",
            phase="backlog",
            context_notes="This is a large task and needs decomposition before editing the UI.",
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

        self.assertFalse(_should_fast_path_tiny_task(task, file_hints, config))

    def test_tiny_task_fast_path_rejects_broad_file_scope(self) -> None:
        task = StoredTask(
            task_id="ux-1236",
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
        file_hints = [
            ("taskbot/ui.py", ["launch_ui"], 18.0),
            ("taskbot/ui/dialogs.py", ["open_dialog"], 17.5),
            ("taskbot/ui/widgets.py", ["render_widget"], 16.0),
            ("taskbot/runner.py", ["_run_task_once"], 15.0),
        ]

        self.assertFalse(_should_fast_path_tiny_task(task, file_hints, config))

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

    def test_runner_publishes_after_implementation_even_if_verification_fails(self) -> None:
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

            task = create_task(
                config,
                board_title="General",
                title="Commit through runner",
                phase="ready",
            )
            task = update_task_fields(
                config,
                task.task_id,
                plan_status="ready",
                plan={"summary": "Existing implementation plan"},
            ) or task

            def fake_run_codex_phase(
                config_arg,
                repo_root_arg,
                *,
                model,
                reasoning_effort,
                prompt,
                artifact_dir,
                phase_name,
                output_schema,
                interrupt_state=None,
            ) -> CodexRunResult:
                self.assertEqual(repo_root_arg.resolve(), repo_root.resolve())
                self.assertEqual(phase_name, "implement")
                (repo_root / "app.txt").write_text("base\nrunner change\n", encoding="utf-8")
                return CodexRunResult(
                    command=["codex", "exec", "implement"],
                    exit_code=0,
                    stdout="",
                    stderr="",
                    last_message_text="",
                    parsed_output={
                        "status": "completed",
                        "summary": "Implemented the runner change.",
                        "files_touched": ["app.txt"],
                        "tests_run": [],
                        "follow_up_items": [],
                        "mark_task_as": "completed",
                    },
                    json_events=[],
                )

            def fake_run_verification_steps(repo_root_arg, config_arg, artifact_dir):
                artifact_dir.mkdir(parents=True, exist_ok=True)
                result = VerificationResult(
                    name="smoke",
                    exit_code=1,
                    duration_seconds=0.01,
                    command=["python3", "-m", "pytest"],
                    stdout_path=str(artifact_dir / "smoke.stdout.log"),
                    stderr_path=str(artifact_dir / "smoke.stderr.log"),
                )
                (artifact_dir / "verification.summary.json").write_text(
                    json.dumps([result.__dict__], indent=2),
                    encoding="utf-8",
                )
                return [result]

            with patch("taskbot.runner._run_codex_phase", side_effect=fake_run_codex_phase), patch(
                "taskbot.runner.run_verification_steps",
                side_effect=fake_run_verification_steps,
            ):
                summary = _run_task_once(config, task, rebuild_index=False)

            artifact_dir = Path(summary["artifact_dir"])
            git_payload = json.loads((artifact_dir / "git.result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "completed")
            self.assertFalse(summary["verification"]["all_passed"])
            self.assertEqual(summary["git"]["status"], "pushed")
            self.assertEqual(git_payload["status"], "pushed")
            self.assertTrue(git_payload["commit_created"])
            self.assertTrue(git_payload["push_attempted"])
            self.assertTrue(git_payload["push_succeeded"])
            self.assertEqual(self._run_git(repo_root, "rev-parse", "HEAD"), self._run_git(repo_root, "rev-parse", "@{upstream}"))
            changed_names = self._run_git(repo_root, "show", "--name-only", "--pretty=format:", "HEAD").splitlines()
            self.assertIn("app.txt", changed_names)

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
