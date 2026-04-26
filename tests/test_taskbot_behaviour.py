from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from taskbot.config import DEFAULT_CONFIG, load_config, save_config_overrides
from taskbot.runner import _build_tiny_task_plan, _should_fast_path_tiny_task
from taskbot.store import StoredTask
from taskbot.ui import _repo_agents_path
from taskbot.verification import run_verification_steps


class TaskbotBehaviourTests(unittest.TestCase):
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
                },
            )

            self.assertEqual(saved_path, (repo_root / "_taskbot" / "config.json").resolve())
            saved_payload = json.loads(saved_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_payload["codex"]["sandbox"], "danger-full-access")
            self.assertFalse(saved_payload["planning"]["auto_plan_tiny_tasks"])
            self.assertEqual(saved_payload["verification"]["mode"], "commands")

            reloaded = load_config(repo_root, saved_path, app_root=repo_root)
            self.assertEqual(reloaded["codex"]["sandbox"], "danger-full-access")
            self.assertFalse(reloaded["planning"]["auto_plan_tiny_tasks"])
            self.assertEqual(reloaded["verification"]["instructions"], "Run smoke checks only.")

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


if __name__ == "__main__":
    unittest.main()
