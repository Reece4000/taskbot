from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from taskbot.codex_cli import CodexRunResult, _is_zero_match_search_failure, analyze_codex_failure
from taskbot.config import DEFAULT_CONFIG, load_config, save_config_overrides
from taskbot.git_integration import capture_git_session_state, publish_git_changes
from taskbot.runner import (
    _approval_response_path as _runner_approval_response_path,
    _build_tiny_task_plan,
    _cmd_doctor,
    _run_task_once,
    _should_fast_path_tiny_task,
    main as runner_main,
)
from taskbot.store import (
    StoredTask,
    create_board,
    create_task,
    edit_task,
    load_store_snapshot,
    rename_board as rename_store_board,
    update_task_fields,
)
from taskbot.ui import (
    START_LOOP_DIALOG_DEFAULT_ITERATIONS,
    _board_header_title,
    _capture_modeless_dialog_value,
    _board_summary_text,
    _command_enter_modifier,
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
    _create_form_dropdown_class,
    _macos_command_line_tools_python,
    _repo_agents_path,
    _start_task_run_args,
    _start_loop_run_args,
    _task_card_can_start_task,
    _task_move_targets,
    _terminal_text_should_refresh,
    _taskbot_title_html,
    _ticket_development_payload_from_text,
    _normalise_ticket_plan,
    launch_ui,
    _sync_dialog_board_titles,
    _sync_task_card_footer_heights,
    _pending_approval_request,
    _wrapped_plain_text_height,
    _write_approval_response,
    _ui_launch_preflight_error,
)
from taskbot.verification import VerificationResult, run_verification_steps


class TaskbotBehaviourTests(unittest.TestCase):
    class _FakeSignal:
        def __init__(self) -> None:
            self._callbacks = []

        def connect(self, callback) -> None:
            self._callbacks.append(callback)

        def emit(self) -> None:
            for callback in list(self._callbacks):
                callback()

    class _FakeAcceptedDialog:
        def __init__(self, title: str) -> None:
            self._title = title
            self.accepted = TaskbotBehaviourTests._FakeSignal()

        def board_title(self) -> str:
            return self._title

        def set_board_title(self, title: str) -> None:
            self._title = title

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
            agent_outputs=[],
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

        self.assertEqual(re.sub(r"<[^>]+>", "", title_html), "TASKBOT")
        self.assertEqual(title_html.count("<span style=\"color:"), 7)
        self.assertEqual(
            title_html,
            (
                '<span style="color:#c86b2f;">T</span>'
                '<span style="color:#c86b2f;">A</span>'
                '<span style="color:#c86b2f;">S</span>'
                '<span style="color:#c86b2f;">K</span>'
                '<span style="color:#5f8f3a;">B</span>'
                '<span style="color:#5f8f3a;">O</span>'
                '<span style="color:#5f8f3a;">T</span>'
            ),
        )

    def test_ticket_development_payload_parses_json_from_agent_text(self) -> None:
        payload = _ticket_development_payload_from_text(
            """
            ```json
            {
              "message": "I found one clear ticket.",
              "questions": ["Which browser should be targeted?"],
              "tickets": [
                {
                  "board_title": "UI",
                  "title": "Add develop tickets button",
                  "phase": "ready"
                }
              ]
            }
            ```
            """
        )

        self.assertEqual(payload["message"], "I found one clear ticket.")
        self.assertEqual(payload["questions"], ["Which browser should be targeted?"])
        self.assertEqual(payload["tickets"][0]["title"], "Add develop tickets button")

    def test_normalise_ticket_plan_falls_back_to_executable_plan_shape(self) -> None:
        plan = _normalise_ticket_plan(
            {
                "title": "Add develop tickets button",
                "context_notes": "Open a chat window from the board header.",
                "file_targets": ["taskbot/ui.py"],
                "acceptance": ["Board header has a Develop Tickets button."],
            }
        )

        self.assertEqual(plan["summary"], "Add develop tickets button")
        self.assertEqual(plan["relevant_files"], ["taskbot/ui.py"])
        self.assertEqual(plan["steps"][0]["files"], ["taskbot/ui.py"])
        self.assertEqual(plan["verification"], ["Board header has a Develop Tickets button."])
        self.assertFalse(plan["decomposition"]["should_split"])

    def test_board_summary_text_for_selected_board_uses_phase_counts(self) -> None:
        planning_task = self._example_stored_task()
        planning_task.phase = "planning"
        ready_task = self._example_stored_task()
        ready_task.task_id = "engineering-5678"
        ready_task.phase = "ready"

        self.assertEqual(
            _board_summary_text([planning_task, ready_task], ["planning", "ready", "completed"]),
            "Planning 1 | Ready 1 | Completed 0",
        )

    def test_board_summary_text_for_all_boards_keeps_board_count(self) -> None:
        backlog_task = self._example_stored_task()
        backlog_task.phase = "backlog"
        completed_task = self._example_stored_task()
        completed_task.task_id = "engineering-9999"
        completed_task.phase = "completed"

        self.assertEqual(
            _board_summary_text(
                [backlog_task, completed_task],
                ["backlog", "completed"],
                board_count=3,
            ),
            "3 boards | Backlog 1 | Completed 1",
        )

    def test_board_summary_text_preserves_custom_phase_order_with_zero_counts(self) -> None:
        blocked_task = self._example_stored_task()
        blocked_task.phase = "blocked"

        self.assertEqual(
            _board_summary_text([blocked_task], ["blocked", "needs_testing", "completed"]),
            "Blocked 1 | Needs Testing 0 | Completed 0",
        )

    def test_board_header_title_appends_total_task_count(self) -> None:
        self.assertEqual(_board_header_title("UX", 3), "UX (3 tasks)")
        self.assertEqual(_board_header_title("All Boards", 12), "All Boards (12 tasks)")

    def test_task_move_targets_excludes_current_phase_and_keeps_board_order(self) -> None:
        self.assertEqual(
            _task_move_targets("in_progress", ["backlog", "planning", "in_progress", "completed"]),
            ["backlog", "planning", "completed"],
        )

    def test_rename_store_board_keeps_empty_board_identity_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(repo_root, None, app_root=repo_root)

            created = create_board(config, "Old Board")
            updated = rename_store_board(config, created["board_id"], "Renamed Board")
            store = load_store_snapshot(config)

            self.assertIsNotNone(updated)
            self.assertEqual(updated["board_id"], created["board_id"])
            self.assertEqual(store["boards"][0]["board_id"], created["board_id"])
            self.assertEqual(store["boards"][0]["title"], "Renamed Board")

    def test_rename_store_board_keeps_populated_board_tasks_on_the_same_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(repo_root, None, app_root=repo_root)
            created = create_board(config, "Old Board")
            created_task = create_task(config, board_title="Old Board", title="First task")
            updated = rename_store_board(config, created["board_id"], "Renamed Board")
            store = load_store_snapshot(config)

            self.assertIsNotNone(updated)
            self.assertEqual(store["boards"][0]["board_id"], created["board_id"])
            self.assertEqual(store["boards"][0]["title"], "Renamed Board")
            self.assertEqual(store["tasks"][0]["board_id"], created_task.board_id)
            self.assertEqual(store["tasks"][0]["board_title"], "Renamed Board")
            self.assertEqual(store["tasks"][0]["task_id"], created_task.task_id)

    def test_load_store_snapshot_ignores_stray_legacy_task_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            task_file = repo_root / "_taskbot" / "_tasks.md"
            task_file.parent.mkdir(parents=True, exist_ok=True)
            task_file.write_text("# Legacy\n- Migrate me\n", encoding="utf-8")

            config = load_config(repo_root, None, app_root=repo_root)
            store = load_store_snapshot(config)

            self.assertEqual(store["tasks"], [])
            persisted = json.loads(Path(config["store"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(persisted["tasks"], [])

    def test_add_task_cli_can_create_ready_task_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = runner_main(
                    [
                        "--repo-root",
                        str(repo_root),
                        "add-task",
                        "--board",
                        "UI",
                        "--title",
                        "Add interactive ticket developer",
                        "--phase",
                        "ready",
                        "--ready-plan",
                        "--context",
                        "Open an interactive Codex session for ticket development.",
                        "--file-target",
                        "taskbot/ui.py",
                        "--acceptance",
                        "The created card can run without a planning pass.",
                    ]
                )

            self.assertEqual(exit_code, 0)
            config = load_config(repo_root, None, app_root=Path.cwd())
            tasks = load_store_snapshot(config)["tasks"]
            self.assertEqual(len(tasks), 1)
            task = StoredTask.from_payload(tasks[0])
            self.assertEqual(task.board_title, "UI")
            self.assertEqual(task.phase, "ready")
            self.assertEqual(task.plan_status, "ready")
            self.assertEqual(task.file_targets, ["taskbot/ui.py"])
            self.assertEqual(task.acceptance, ["The created card can run without a planning pass."])
            self.assertIn("taskbot/ui.py", task.plan["relevant_files"])
            self.assertIn("ready", output.getvalue())

    def test_load_store_snapshot_normalises_legacy_markdown_source_kind_to_ui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(repo_root, None, app_root=repo_root)
            store_path = Path(config["store"]["path"])
            store_path.parent.mkdir(parents=True, exist_ok=True)
            store_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "phases": list(config["store"]["phases"]),
                        "boards": [
                            {
                                "board_id": "general",
                                "title": "General",
                                "order": 0,
                            }
                        ],
                        "tasks": [
                            {
                                "task_id": "general-1234abcd",
                                "board_id": "general",
                                "board_title": "General",
                                "title": "Legacy imported task",
                                "phase": "backlog",
                                "context_notes": "",
                                "file_targets": [],
                                "acceptance": [],
                                "source_kind": "markdown",
                                "source_line_index": 12,
                                "plan_status": "pending",
                                "plan": {},
                                "artifact_dir": "",
                                "agent_outputs": [],
                                "last_result_status": "",
                                "last_summary": "",
                                "last_error": "",
                                "order": 0,
                                "created_at": "",
                                "updated_at": "",
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            store = load_store_snapshot(config)
            reloaded_payload = json.loads(store_path.read_text(encoding="utf-8"))

            self.assertEqual(store["tasks"][0]["source_kind"], "ui")
            self.assertEqual(store["tasks"][0]["source_line_index"], -1)
            self.assertEqual(reloaded_payload["tasks"][0]["source_kind"], "ui")
            self.assertEqual(reloaded_payload["tasks"][0]["source_line_index"], -1)

    def test_edit_task_updates_legacy_markdown_origin_task_inside_store_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(repo_root, None, app_root=repo_root)
            task = create_task(config, board_title="General", title="Legacy imported task")
            update_task_fields(
                config,
                task.task_id,
                source_kind="markdown",
                source_line_index=12,
            )

            updated = edit_task(
                config,
                task.task_id,
                board_title="Renamed Legacy",
                title="Updated store task",
                context_notes="notes",
                phase="needs_testing",
            )
            store = load_store_snapshot(config)

            self.assertIsNotNone(updated)
            self.assertEqual(updated.source_kind, "ui")
            self.assertEqual(updated.board_title, "Renamed Legacy")
            self.assertEqual(updated.title, "Updated store task")
            self.assertEqual(len(store["tasks"]), 1)
            self.assertEqual(store["tasks"][0]["board_title"], "Renamed Legacy")
            self.assertEqual(store["tasks"][0]["title"], "Updated store task")

    def test_rename_store_board_does_not_revert_when_old_title_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(repo_root, None, app_root=repo_root)

            original = create_board(config, "Old Board")
            renamed = rename_store_board(config, original["board_id"], "Renamed Board")
            recreated = create_board(config, "Old Board")
            created_task = create_task(config, board_title="Old Board", title="New old-board task")
            renamed_task = create_task(config, board_title="Renamed Board", title="Task to move")
            moved_task = edit_task(
                config,
                renamed_task.task_id,
                board_title="Old Board",
                title=renamed_task.title,
                context_notes=renamed_task.context_notes,
                phase=renamed_task.phase,
            )
            store = load_store_snapshot(config)

            self.assertIsNotNone(renamed)
            self.assertIsNotNone(moved_task)
            self.assertNotEqual(recreated["board_id"], original["board_id"])
            self.assertEqual(created_task.board_id, recreated["board_id"])
            self.assertEqual(moved_task.board_id, recreated["board_id"])

            boards_by_id = {
                str(board["board_id"]): str(board["title"])
                for board in store["boards"]
                if isinstance(board, dict)
            }
            self.assertEqual(boards_by_id[original["board_id"]], "Renamed Board")
            self.assertEqual(boards_by_id[recreated["board_id"]], "Old Board")

    def test_rename_store_board_updates_default_board_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config_path = repo_root / "config.json"
            config_path.write_text(
                json.dumps({"store": {"default_board": "General"}}, indent=2) + "\n",
                encoding="utf-8",
            )
            config = load_config(repo_root, config_path, app_root=repo_root)

            original = create_board(config, "General")
            create_task(config, board_title="General", title="Existing task")

            renamed = rename_store_board(config, original["board_id"], "Platform")
            created_after_rename = create_task(config, board_title="", title="Follow-up task")
            reloaded_config = load_config(repo_root, config_path, app_root=repo_root)
            store = load_store_snapshot(reloaded_config)

            self.assertIsNotNone(renamed)
            self.assertTrue(renamed["default_board_updated"])
            self.assertEqual(config["store"]["default_board"], "Platform")
            self.assertEqual(reloaded_config["store"]["default_board"], "Platform")
            self.assertEqual(created_after_rename.board_title, "Platform")
            board_titles = {
                str(board["title"])
                for board in store["boards"]
                if isinstance(board, dict)
            }
            self.assertIn("Platform", board_titles)
            self.assertNotIn("General", board_titles)

    def test_capture_modeless_dialog_value_keeps_renamed_board_submission(self) -> None:
        dialog = self._FakeAcceptedDialog("Renamed Board")
        submitted_title = _capture_modeless_dialog_value(dialog, dialog.board_title)

        dialog.accepted.emit()
        dialog.set_board_title("Old Board")

        self.assertEqual(submitted_title(), "Renamed Board")

    def test_sync_dialog_board_titles_updates_dialogs_with_rename_hook(self) -> None:
        renamed: list[tuple[str, str]] = []

        class FakeDialog:
            def rename_board_option(self, old_title: str, new_title: str) -> None:
                renamed.append((old_title, new_title))

        _sync_dialog_board_titles([FakeDialog(), object(), FakeDialog()], "QA", "Platform")

        self.assertEqual(renamed, [("QA", "Platform"), ("QA", "Platform")])

    def test_sync_dialog_board_titles_ignores_blank_and_unchanged_titles(self) -> None:
        renamed: list[tuple[str, str]] = []

        class FakeDialog:
            def rename_board_option(self, old_title: str, new_title: str) -> None:
                renamed.append((old_title, new_title))

        dialog = FakeDialog()
        _sync_dialog_board_titles([dialog], "", "Platform")
        _sync_dialog_board_titles([dialog], "Platform", "Platform")

        self.assertEqual(renamed, [])

    def test_modeless_rename_board_dialog_persists_changes(self) -> None:
        original_platform = os.environ.get("QT_QPA_PLATFORM")
        original_headless = os.environ.get("TASKBOT_UI_ALLOW_HEADLESS")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = "1"
        try:
            try:
                from PySide6.QtWidgets import QApplication
            except ModuleNotFoundError:
                self.skipTest("PySide6 is not installed")

            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                config = load_config(repo_root, None, app_root=repo_root)
                original_board = create_board(config, "Old Board")
                captured: dict[str, object] = {}

                def fake_exec(app) -> int:
                    top_level = [
                        widget
                        for widget in app.topLevelWidgets()
                        if hasattr(widget, "_open_rename_board_dialog")
                    ]
                    captured["app"] = app
                    captured["window"] = top_level[0]
                    return 0

                with patch("PySide6.QtWidgets.QApplication.exec", fake_exec):
                    self.assertEqual(launch_ui(config), 0)

                app = captured["app"]
                window = captured["window"]
                self.assertIsInstance(app, QApplication)
                self.assertTrue(hasattr(window, "_open_rename_board_dialog"))

                window._open_rename_board_dialog(original_board)
                app.processEvents()
                dialog = window._open_dialogs[-1]
                dialog.title_input.setText("Renamed Board")
                dialog.accept()
                app.processEvents()

                boards = load_store_snapshot(window.active_config)["boards"]
                renamed = next(
                    board
                    for board in boards
                    if isinstance(board, dict) and board.get("board_id") == original_board["board_id"]
                )
                self.assertEqual(renamed["title"], "Renamed Board")
                self.assertEqual(window.selected_board_id, original_board["board_id"])
                self.assertIn("Renamed board Old Board to Renamed Board", window.status_note)
                window.close()
                app.processEvents()
        finally:
            if original_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = original_platform
            if original_headless is None:
                os.environ.pop("TASKBOT_UI_ALLOW_HEADLESS", None)
            else:
                os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = original_headless

    def test_modeless_add_board_dialog_submits_on_return_in_title_input(self) -> None:
        original_platform = os.environ.get("QT_QPA_PLATFORM")
        original_headless = os.environ.get("TASKBOT_UI_ALLOW_HEADLESS")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = "1"
        try:
            try:
                from PySide6.QtCore import Qt
                from PySide6.QtTest import QTest
                from PySide6.QtWidgets import QApplication
            except ModuleNotFoundError:
                self.skipTest("PySide6 is not installed")

            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                config = load_config(repo_root, None, app_root=repo_root)
                captured: dict[str, object] = {}

                def fake_exec(app) -> int:
                    top_level = [
                        widget
                        for widget in app.topLevelWidgets()
                        if hasattr(widget, "_open_add_board_dialog")
                    ]
                    captured["app"] = app
                    captured["window"] = top_level[0]
                    return 0

                with patch("PySide6.QtWidgets.QApplication.exec", fake_exec):
                    self.assertEqual(launch_ui(config), 0)

                app = captured["app"]
                window = captured["window"]
                self.assertIsInstance(app, QApplication)

                window._open_add_board_dialog()
                app.processEvents()
                dialog = window._open_dialogs[-1]
                dialog.title_input.setText("Platform")
                dialog.title_input.setFocus()
                QTest.keyClick(dialog.title_input, Qt.Key_Return)
                app.processEvents()

                boards = load_store_snapshot(window.active_config)["boards"]
                created = next(
                    board
                    for board in boards
                    if isinstance(board, dict) and board.get("title") == "Platform"
                )
                self.assertEqual(created["board_id"], "platform")
                window.close()
                app.processEvents()
        finally:
            if original_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = original_platform
            if original_headless is None:
                os.environ.pop("TASKBOT_UI_ALLOW_HEADLESS", None)
            else:
                os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = original_headless

    def test_modeless_rename_board_dialog_submits_on_return_in_title_input(self) -> None:
        original_platform = os.environ.get("QT_QPA_PLATFORM")
        original_headless = os.environ.get("TASKBOT_UI_ALLOW_HEADLESS")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = "1"
        try:
            try:
                from PySide6.QtCore import Qt
                from PySide6.QtTest import QTest
                from PySide6.QtWidgets import QApplication
            except ModuleNotFoundError:
                self.skipTest("PySide6 is not installed")

            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                config = load_config(repo_root, None, app_root=repo_root)
                original_board = create_board(config, "Old Board")
                captured: dict[str, object] = {}

                def fake_exec(app) -> int:
                    top_level = [
                        widget
                        for widget in app.topLevelWidgets()
                        if hasattr(widget, "_open_rename_board_dialog")
                    ]
                    captured["app"] = app
                    captured["window"] = top_level[0]
                    return 0

                with patch("PySide6.QtWidgets.QApplication.exec", fake_exec):
                    self.assertEqual(launch_ui(config), 0)

                app = captured["app"]
                window = captured["window"]
                self.assertIsInstance(app, QApplication)

                window._open_rename_board_dialog(original_board)
                app.processEvents()
                dialog = window._open_dialogs[-1]
                dialog.title_input.setText("Renamed Board")
                dialog.title_input.setFocus()
                QTest.keyClick(dialog.title_input, Qt.Key_Return)
                app.processEvents()

                boards = load_store_snapshot(window.active_config)["boards"]
                renamed = next(
                    board
                    for board in boards
                    if isinstance(board, dict) and board.get("board_id") == original_board["board_id"]
                )
                self.assertEqual(renamed["title"], "Renamed Board")
                window.close()
                app.processEvents()
        finally:
            if original_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = original_platform
            if original_headless is None:
                os.environ.pop("TASKBOT_UI_ALLOW_HEADLESS", None)
            else:
                os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = original_headless

    def test_modeless_rename_board_dialog_updates_open_edit_dialog_for_needs_testing_task(self) -> None:
        original_platform = os.environ.get("QT_QPA_PLATFORM")
        original_headless = os.environ.get("TASKBOT_UI_ALLOW_HEADLESS")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = "1"
        try:
            try:
                from PySide6.QtWidgets import QApplication
            except ModuleNotFoundError:
                self.skipTest("PySide6 is not installed")

            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                config = load_config(repo_root, None, app_root=repo_root)
                original_board = create_board(config, "QA")
                task = create_task(config, board_title="QA", title="Verify release", phase="needs_testing")
                captured: dict[str, object] = {}

                def fake_exec(app) -> int:
                    top_level = [
                        widget
                        for widget in app.topLevelWidgets()
                        if hasattr(widget, "_open_edit_task_dialog")
                    ]
                    captured["app"] = app
                    captured["window"] = top_level[0]
                    return 0

                with patch("PySide6.QtWidgets.QApplication.exec", fake_exec):
                    self.assertEqual(launch_ui(config), 0)

                app = captured["app"]
                window = captured["window"]
                self.assertIsInstance(app, QApplication)

                window._open_edit_task_dialog(task)
                app.processEvents()
                edit_dialog = window._open_dialogs[-1]

                board = next(
                    item
                    for item in load_store_snapshot(window.active_config)["boards"]
                    if isinstance(item, dict) and item.get("board_id") == original_board["board_id"]
                )
                window._open_rename_board_dialog(board)
                app.processEvents()
                rename_dialog = window._open_dialogs[-1]
                rename_dialog.title_input.setText("Platform")
                rename_dialog.accept()
                app.processEvents()

                self.assertEqual(edit_dialog.board_dropdown.currentText(), "Platform")
                edit_dialog.accept()
                app.processEvents()

                store = load_store_snapshot(window.active_config)
                board_titles = {
                    str(item["title"])
                    for item in store["boards"]
                    if isinstance(item, dict)
                }
                updated_task = next(
                    item
                    for item in store["tasks"]
                    if isinstance(item, dict) and item.get("task_id") == task.task_id
                )

                self.assertIn("Platform", board_titles)
                self.assertNotIn("QA", board_titles)
                self.assertEqual(updated_task["board_title"], "Platform")
                window.close()
                app.processEvents()
        finally:
            if original_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = original_platform
            if original_headless is None:
                os.environ.pop("TASKBOT_UI_ALLOW_HEADLESS", None)
            else:
                os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = original_headless

    def test_modeless_rename_board_dialog_persists_changes_after_loading_external_repo(self) -> None:
        original_platform = os.environ.get("QT_QPA_PLATFORM")
        original_headless = os.environ.get("TASKBOT_UI_ALLOW_HEADLESS")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = "1"
        try:
            try:
                from PySide6.QtWidgets import QApplication
            except ModuleNotFoundError:
                self.skipTest("PySide6 is not installed")

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app_root = root / "app-root"
                repo_root = root / "managed-repo"
                app_root.mkdir(parents=True, exist_ok=True)
                repo_root.mkdir(parents=True, exist_ok=True)
                (app_root / "config.json").write_text("{}\n", encoding="utf-8")

                repo_config = load_config(repo_root, None, app_root=app_root)
                original_board = create_board(repo_config, "Old Board")
                captured: dict[str, object] = {}

                def fake_exec(app) -> int:
                    top_level = [
                        widget
                        for widget in app.topLevelWidgets()
                        if hasattr(widget, "_load_repo_from_input")
                    ]
                    captured["app"] = app
                    captured["window"] = top_level[0]
                    return 0

                initial_config = load_config(app_root, app_root / "config.json", app_root=app_root)
                with patch("PySide6.QtWidgets.QApplication.exec", fake_exec):
                    self.assertEqual(launch_ui(initial_config), 0)

                app = captured["app"]
                window = captured["window"]
                self.assertIsInstance(app, QApplication)
                self.assertTrue(hasattr(window, "_load_repo_from_input"))
                self.assertTrue(hasattr(window, "_open_rename_board_dialog"))

                window.repo_input.setText(str(repo_root))
                window._load_repo_from_input()
                app.processEvents()

                boards = load_store_snapshot(window.active_config)["boards"]
                board = next(
                    item
                    for item in boards
                    if isinstance(item, dict) and item.get("board_id") == original_board["board_id"]
                )
                window._open_rename_board_dialog(board)
                app.processEvents()
                dialog = window._open_dialogs[-1]
                dialog.title_input.setText("Renamed Board")
                dialog.accept()
                app.processEvents()

                renamed_boards = load_store_snapshot(window.active_config)["boards"]
                renamed = next(
                    item
                    for item in renamed_boards
                    if isinstance(item, dict) and item.get("board_id") == original_board["board_id"]
                )
                self.assertEqual(Path(window.active_config["repo_root"]), repo_root.resolve())
                self.assertEqual(renamed["title"], "Renamed Board")
                self.assertEqual(window.selected_board_id, original_board["board_id"])
                self.assertIn("Renamed board Old Board to Renamed Board", window.status_note)
                self.assertIn(
                    "Renamed Board | 0",
                    [window.board_list.item(index).text() for index in range(window.board_list.count())],
                )
                window.close()
                app.processEvents()
        finally:
            if original_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = original_platform
            if original_headless is None:
                os.environ.pop("TASKBOT_UI_ALLOW_HEADLESS", None)
            else:
                os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = original_headless

    def test_modeless_add_board_dialog_targets_loaded_repo_after_repo_switch(self) -> None:
        original_platform = os.environ.get("QT_QPA_PLATFORM")
        original_headless = os.environ.get("TASKBOT_UI_ALLOW_HEADLESS")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = "1"
        try:
            try:
                from PySide6.QtWidgets import QApplication
            except ModuleNotFoundError:
                self.skipTest("PySide6 is not installed")

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app_root = root / "app-root"
                repo_root = root / "managed-repo"
                app_root.mkdir(parents=True, exist_ok=True)
                repo_root.mkdir(parents=True, exist_ok=True)
                (app_root / "config.json").write_text("{}\n", encoding="utf-8")
                captured: dict[str, object] = {}

                def fake_exec(app) -> int:
                    top_level = [
                        widget
                        for widget in app.topLevelWidgets()
                        if hasattr(widget, "_load_repo_from_input")
                    ]
                    captured["app"] = app
                    captured["window"] = top_level[0]
                    return 0

                initial_config = load_config(app_root, app_root / "config.json", app_root=app_root)
                with patch("PySide6.QtWidgets.QApplication.exec", fake_exec):
                    self.assertEqual(launch_ui(initial_config), 0)

                app = captured["app"]
                window = captured["window"]
                self.assertIsInstance(app, QApplication)
                self.assertTrue(hasattr(window, "_load_repo_from_input"))
                self.assertTrue(hasattr(window, "_open_add_board_dialog"))

                window._open_add_board_dialog()
                app.processEvents()
                self.assertEqual(len(window._open_dialogs), 1)

                window.repo_input.setText(str(repo_root))
                window._load_repo_from_input()
                app.processEvents()

                self.assertEqual(window._open_dialogs, [])
                self.assertEqual(Path(window.active_config["repo_root"]), repo_root.resolve())

                window._open_add_board_dialog()
                app.processEvents()
                dialog = window._open_dialogs[-1]
                dialog.title_input.setText("Platform")
                dialog.accept()
                app.processEvents()

                boards = load_store_snapshot(window.active_config)["boards"]
                created = next(
                    item
                    for item in boards
                    if isinstance(item, dict) and item.get("title") == "Platform"
                )
                self.assertEqual(created["board_id"], "platform")
                self.assertEqual(window.selected_board_id, created["board_id"])
                self.assertIn("Created board Platform", window.status_note)
                self.assertIn(
                    "Platform | 0",
                    [window.board_list.item(index).text() for index in range(window.board_list.count())],
                )
                window.close()
                app.processEvents()
        finally:
            if original_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = original_platform
            if original_headless is None:
                os.environ.pop("TASKBOT_UI_ALLOW_HEADLESS", None)
            else:
                os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = original_headless

    def test_modeless_rename_board_dialog_updates_default_board_after_loading_external_repo(self) -> None:
        original_platform = os.environ.get("QT_QPA_PLATFORM")
        original_headless = os.environ.get("TASKBOT_UI_ALLOW_HEADLESS")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = "1"
        try:
            try:
                from PySide6.QtWidgets import QApplication
            except ModuleNotFoundError:
                self.skipTest("PySide6 is not installed")

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app_root = root / "app-root"
                repo_root = root / "managed-repo"
                app_root.mkdir(parents=True, exist_ok=True)
                repo_root.mkdir(parents=True, exist_ok=True)
                (app_root / "config.json").write_text("{}\n", encoding="utf-8")
                repo_config_path = repo_root / "_taskbot" / "config.json"
                repo_config_path.parent.mkdir(parents=True, exist_ok=True)
                repo_config_path.write_text(
                    json.dumps({"store": {"default_board": "General"}}, indent=2) + "\n",
                    encoding="utf-8",
                )

                repo_config = load_config(repo_root, repo_config_path, app_root=app_root)
                original_board = create_board(repo_config, "General")
                create_task(repo_config, board_title="General", title="Existing task")
                captured: dict[str, object] = {}

                def fake_exec(app) -> int:
                    top_level = [
                        widget
                        for widget in app.topLevelWidgets()
                        if hasattr(widget, "_load_repo_from_input")
                    ]
                    captured["app"] = app
                    captured["window"] = top_level[0]
                    return 0

                initial_config = load_config(app_root, app_root / "config.json", app_root=app_root)
                with patch("PySide6.QtWidgets.QApplication.exec", fake_exec):
                    self.assertEqual(launch_ui(initial_config), 0)

                app = captured["app"]
                window = captured["window"]
                self.assertIsInstance(app, QApplication)
                self.assertTrue(hasattr(window, "_load_repo_from_input"))

                window.repo_input.setText(str(repo_root))
                window._load_repo_from_input()
                app.processEvents()

                boards = load_store_snapshot(window.active_config)["boards"]
                board = next(
                    item
                    for item in boards
                    if isinstance(item, dict) and item.get("board_id") == original_board["board_id"]
                )
                window._open_rename_board_dialog(board)
                app.processEvents()
                dialog = window._open_dialogs[-1]
                dialog.title_input.setText("Platform")
                dialog.accept()
                app.processEvents()

                created_after_rename = create_task(window.active_config, board_title="", title="Follow-up task")
                reloaded_repo_config = load_config(repo_root, repo_config_path, app_root=app_root)
                board_titles = {
                    str(item["title"])
                    for item in load_store_snapshot(reloaded_repo_config)["boards"]
                    if isinstance(item, dict)
                }

                self.assertEqual(created_after_rename.board_title, "Platform")
                self.assertEqual(reloaded_repo_config["store"]["default_board"], "Platform")
                self.assertEqual(json.loads(repo_config_path.read_text(encoding="utf-8"))["store"]["default_board"], "Platform")
                self.assertIn("Platform", board_titles)
                self.assertNotIn("General", board_titles)
                window.close()
                app.processEvents()
        finally:
            if original_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = original_platform
            if original_headless is None:
                os.environ.pop("TASKBOT_UI_ALLOW_HEADLESS", None)
            else:
                os.environ["TASKBOT_UI_ALLOW_HEADLESS"] = original_headless

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

    def test_task_card_start_task_is_limited_to_backlog_and_planning(self) -> None:
        self.assertTrue(_task_card_can_start_task("backlog"))
        self.assertTrue(_task_card_can_start_task("planning"))
        self.assertFalse(_task_card_can_start_task("ready"))
        self.assertFalse(_task_card_can_start_task("in_progress"))
        self.assertFalse(_task_card_can_start_task("completed"))

    def test_start_task_runner_args_target_a_single_task_id(self) -> None:
        self.assertEqual(
            _start_task_run_args("engineering-1234"),
            ["run", "--task-id", "engineering-1234"],
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

        self.assertEqual(_command_enter_shortcut_sequences(), ("Ctrl+Return", "Ctrl+Enter"))
        self.assertEqual(shortcuts, created)
        self.assertEqual([shortcut.key_sequence for shortcut in shortcuts], [
            "sequence:Ctrl+Return",
            "sequence:Ctrl+Enter",
        ])
        self.assertEqual([shortcut.parent for shortcut in shortcuts], ["dialog-parent", "dialog-parent"])
        self.assertEqual([shortcut.context for shortcut in shortcuts], ["window-shortcut", "window-shortcut"])
        self.assertTrue(all(shortcut.activated.connected is callback for shortcut in shortcuts))

    def test_command_enter_modifier_uses_passed_qt_namespace(self) -> None:
        class FakeQt:
            ControlModifier = 0x10

        self.assertEqual(_command_enter_modifier(FakeQt), 0x10)

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

        self.assertTrue(_command_enter_submit_dialog(None, dialog, FakeButton))
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

        self.assertTrue(_command_enter_submit_dialog(None, dialog, FakeButton))
        self.assertEqual(default_button.clicked, 1)
        self.assertEqual(cancel_button.clicked, 0)

    def test_form_dropdown_emits_current_index_changed_on_selection_change(self) -> None:
        original_platform = os.environ.get("QT_QPA_PLATFORM")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        try:
            try:
                from PySide6.QtCore import Qt, Signal
                from PySide6.QtGui import QAction
                from PySide6.QtWidgets import QApplication, QMenu, QSizePolicy, QToolButton
            except ModuleNotFoundError:
                self.skipTest("PySide6 is not installed")

            app = QApplication.instance() or QApplication([])
            dropdown_cls = _create_form_dropdown_class(
                Qt=Qt,
                QAction=QAction,
                QMenu=QMenu,
                QSizePolicy=QSizePolicy,
                QToolButton=QToolButton,
                Signal=Signal,
            )

            dropdown = dropdown_cls()
            dropdown.addItem("One", "one")
            dropdown.addItem("Two", "two")

            seen_indices: list[int] = []
            dropdown.currentIndexChanged.connect(seen_indices.append)

            dropdown.setCurrentIndex(1)
            dropdown.setCurrentIndex(1)
            dropdown.setCurrentData("one")

            self.assertEqual(seen_indices, [1, 0])
        finally:
            if original_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = original_platform

    def test_task_card_footer_sync_allocates_wrapped_meta_height(self) -> None:
        original_platform = os.environ.get("QT_QPA_PLATFORM")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        try:
            try:
                from PySide6.QtCore import Qt
                from PySide6.QtWidgets import (
                    QApplication,
                    QFrame,
                    QHBoxLayout,
                    QLabel,
                    QSizePolicy,
                    QVBoxLayout,
                    QWidget,
                )
            except ModuleNotFoundError:
                self.skipTest("PySide6 is not installed")

            app = QApplication.instance() or QApplication([])
            app.setStyleSheet(
                """
                QLabel#BoardBadge {
                    background: #f5ddcf;
                    color: #934e2e;
                    border-radius: 3px;
                    padding: 2px 5px;
                    font-size: 10px;
                    font-weight: 700;
                }

                QLabel#TaskMeta {
                    color: #957d6e;
                    font-size: 10px;
                    font-weight: 600;
                }
                """
            )

            root = QWidget()
            root_layout = QVBoxLayout(root)
            root_layout.setContentsMargins(0, 0, 0, 0)

            card = QFrame()
            card.setFixedWidth(240)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 9, 12, 9)
            card_layout.setSpacing(5)

            footer = QWidget()
            footer_layout = QHBoxLayout(footer)
            footer_layout.setContentsMargins(0, 0, 0, 0)
            footer_layout.setSpacing(8)
            footer_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)

            badge = QLabel("general issues and bugs")
            badge.setObjectName("BoardBadge")
            badge.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            footer_layout.addWidget(badge, 0, Qt.AlignLeft | Qt.AlignTop)

            meta = QLabel("ready | 1 files | needs testing")
            meta.setObjectName("TaskMeta")
            meta.setTextFormat(Qt.PlainText)
            meta.setWordWrap(True)
            meta.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            meta.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            meta.setMinimumWidth(0)
            footer_layout.addWidget(meta, 1, Qt.AlignLeft | Qt.AlignTop)

            card_layout.addWidget(footer)
            root_layout.addWidget(card)

            root.show()
            app.processEvents()

            footer_height, meta_height = _sync_task_card_footer_heights(footer, badge, meta, 8)
            card_layout.activate()
            app.processEvents()

            self.assertEqual(meta_height, _wrapped_plain_text_height(meta, meta.width()))
            self.assertEqual(meta.minimumHeight(), meta_height)
            self.assertGreaterEqual(meta.height(), meta_height)
            self.assertEqual(footer.minimumHeight(), footer_height)
            self.assertGreaterEqual(footer.height(), footer_height)
        finally:
            if original_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = original_platform

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

    def test_ui_launch_preflight_blocks_headless_macos_sessions(self) -> None:
        with patch("taskbot.ui.os.getenv", return_value=""), patch(
            "taskbot.ui.sys.platform",
            "darwin",
        ), patch("taskbot.ui._macos_window_server_available", return_value=False):
            error = _ui_launch_preflight_error()

        self.assertIsNotNone(error)
        self.assertIn("WindowServer", error or "")
        self.assertIn("TASKBOT_UI_ALLOW_HEADLESS=1", error or "")

    def test_ui_launch_preflight_allows_headless_override(self) -> None:
        with patch("taskbot.ui.os.getenv", return_value="1"), patch(
            "taskbot.ui.sys.platform",
            "darwin",
        ), patch("taskbot.ui._macos_window_server_available", return_value=False):
            self.assertIsNone(_ui_launch_preflight_error())

    def test_macos_command_line_tools_python_detection_matches_apple_runtime(self) -> None:
        with patch("taskbot.ui.sys.platform", "darwin"), patch(
            "taskbot.ui.sys.executable",
            "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9",
        ), patch(
            "taskbot.ui.sys.base_prefix",
            "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9",
        ):
            self.assertTrue(_macos_command_line_tools_python())

    def test_doctor_reports_ui_launch_constraints_on_macos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(repo_root, None, app_root=repo_root)

            with patch("taskbot.runner.sys.platform", "darwin"), patch(
                "taskbot.ui._ui_launch_preflight_error",
                return_value="No active macOS desktop session was detected.",
            ), patch(
                "taskbot.ui._macos_command_line_tools_python",
                return_value=True,
            ), patch(
                "taskbot.runner.shutil.which",
                return_value="/usr/local/bin/codex",
            ), patch(
                "taskbot.runner.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["codex", "--version"],
                    0,
                    stdout="codex 1.2.3\n",
                    stderr="",
                ),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = _cmd_doctor(config)

        self.assertEqual(exit_code, 0)
        doctor_output = output.getvalue()
        self.assertIn("ui_launch    blocked", doctor_output)
        self.assertIn("ui_reason No active macOS desktop session was detected.", doctor_output)
        self.assertIn("python_gui   apple-clt", doctor_output)

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
            agent_outputs=[],
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
            ("taskbot/ui/widgets.py", ["render_widget"], 17.0),
            ("taskbot/ui/theme.py", ["apply_theme"], 16.5),
        ]

        self.assertTrue(_should_fast_path_tiny_task(task, file_hints, config))
        plan = _build_tiny_task_plan(task, file_hints, config)
        self.assertEqual(
            plan["relevant_files"],
            ["taskbot/ui.py", "taskbot/ui/dialogs.py", "taskbot/ui/widgets.py"],
        )
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
            agent_outputs=[],
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
            agent_outputs=[],
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
            ("taskbot/store.py", ["StoredTask"], 17.5),
            ("taskbot/runner.py", ["_run_task_once"], 16.0),
        ]

        self.assertFalse(_should_fast_path_tiny_task(task, file_hints, config))

    def test_zero_match_search_failures_are_ignored(self) -> None:
        result = CodexRunResult(
            command=["codex", "exec"],
            exit_code=1,
            stdout="",
            stderr="",
            last_message_text="",
            parsed_output=None,
            json_events=[
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "/bin/zsh -lc 'rg -n \"missing\" tests/test_taskbot_behaviour.py'",
                        "exit_code": 1,
                    },
                }
            ],
        )

        analysis = analyze_codex_failure(result)

        self.assertTrue(_is_zero_match_search_failure(result.json_events[0]["item"]["command"], 1))
        self.assertEqual(len(analysis.ignored_failures), 1)
        self.assertEqual(len(analysis.actionable_failures), 0)
        self.assertFalse(analysis.permission_related)

    def test_permission_failure_analysis_marks_permission_related_commands(self) -> None:
        result = CodexRunResult(
            command=["codex", "exec"],
            exit_code=1,
            stdout="",
            stderr="tool call blocked by sandbox policy",
            last_message_text="",
            parsed_output=None,
            json_events=[
                {"type": "error", "message": "Command blocked by sandbox; broader permissions required."},
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "cat /root/secret.txt",
                        "exit_code": 126,
                    },
                },
            ],
        )

        analysis = analyze_codex_failure(result)

        self.assertTrue(analysis.permission_related)
        self.assertEqual(len(analysis.actionable_failures), 1)
        self.assertEqual(analysis.actionable_failures[0].command, "cat /root/secret.txt")

    def test_write_approval_response_uses_control_dir_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control_dir = Path(tmp)

            response_path = _write_approval_response(control_dir, "req-123", True, "ui")

            self.assertEqual(response_path, _runner_approval_response_path({"control_dir": str(control_dir)}, "req-123"))
            payload = json.loads(response_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["approved"])
            self.assertEqual(payload["request_id"], "req-123")
            self.assertEqual(payload["source"], "ui")

    def test_pending_approval_request_requires_pending_status_and_id(self) -> None:
        self.assertIsNone(_pending_approval_request({}))
        self.assertIsNone(_pending_approval_request({"approval_request": {"status": "done", "id": "req-1"}}))
        self.assertEqual(
            _pending_approval_request({"approval_request": {"status": "pending", "id": "req-1"}}),
            {"status": "pending", "id": "req-1"},
        )

    def test_runner_retries_permission_failures_once_with_broader_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(repo_root, None, app_root=repo_root)
            task = create_task(
                config,
                board_title="General",
                title="Retry permission failure once",
                phase="ready",
            )
            task = update_task_fields(
                config,
                task.task_id,
                plan_status="ready",
                plan={"summary": "Existing implementation plan"},
            ) or task

            calls: List[Dict[str, Any]] = []

            def fake_run_codex_phase(
                config_arg,
                repo_root_arg,
                *,
                model,
                reasoning_effort,
                prompt,
                artifact_dir,
                phase_name,
                sandbox_override=None,
                approval_override=None,
                output_schema,
                interrupt_state=None,
            ) -> CodexRunResult:
                calls.append(
                    {
                        "phase_name": phase_name,
                        "sandbox_override": sandbox_override,
                        "approval_override": approval_override,
                    }
                )
                if len(calls) == 1:
                    (artifact_dir / "implement.stdout.log").write_text("first attempt\n", encoding="utf-8")
                    return CodexRunResult(
                        command=["codex", "exec", "implement"],
                        exit_code=1,
                        stdout="",
                        stderr="sandbox denied command",
                        last_message_text="",
                        parsed_output=None,
                        json_events=[
                            {"type": "error", "message": "Sandbox blocked the requested command."},
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": "cat /root/secret.txt",
                                    "exit_code": 126,
                                },
                            },
                        ],
                    )
                return CodexRunResult(
                    command=["codex", "exec", "implement"],
                    exit_code=0,
                    stdout="",
                    stderr="",
                    last_message_text="",
                    parsed_output={
                        "status": "needs_testing",
                        "summary": "Retried with temporary broader access.",
                        "files_touched": [],
                        "tests_run": [],
                        "follow_up_items": [],
                        "mark_task_as": "needs_testing",
                    },
                    json_events=[],
                )

            with patch("taskbot.runner._run_codex_phase", side_effect=fake_run_codex_phase), patch(
                "taskbot.runner._request_phase_retry_approval",
                return_value=True,
            ), patch(
                "taskbot.runner.run_verification_steps",
                return_value=[],
            ):
                summary = _run_task_once(config, task, rebuild_index=False)

            self.assertEqual(summary["status"], "needs_testing")
            self.assertEqual(len(calls), 2)
            self.assertIsNone(calls[0]["sandbox_override"])
            self.assertEqual(calls[1]["sandbox_override"], "danger-full-access")
            self.assertEqual(calls[1]["approval_override"], "on-request")
            artifact_dir = Path(summary["artifact_dir"])
            self.assertTrue((artifact_dir / "implement.attempt1.stdout.log").exists())

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
