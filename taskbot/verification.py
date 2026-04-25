from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from taskbot.terminal_stream import append_terminal_log


@dataclass
class VerificationResult:
    name: str
    exit_code: int
    duration_seconds: float
    command: List[str]
    stdout_path: str
    stderr_path: str


def run_verification_steps(repo_root: Path, config: Dict[str, Any], artifact_dir: Path) -> List[VerificationResult]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    results: List[VerificationResult] = []

    for entry in config["verification"]["commands"]:
        if not entry.get("enabled", True):
            continue

        name = str(entry["name"])
        command = [str(part) for part in entry["command"]]
        timeout_seconds = float(entry.get("timeout_seconds", 300))
        append_terminal_log(config, "[verify] {0} > {1}".format(name, " ".join(command)))
        stdout_path = artifact_dir / "{0}.stdout.log".format(name)
        stderr_path = artifact_dir / "{0}.stderr.log".format(name)
        started = time.time()

        try:
            completed = subprocess.run(
                command,
                cwd=repo_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            stdout_text = completed.stdout
            stderr_text = completed.stderr
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout_text = exc.stdout or ""
            stderr_text = (exc.stderr or "") + "\nTimed out after {0} seconds.\n".format(timeout_seconds)
            exit_code = 124

        stdout_path.write_text(stdout_text, encoding="utf-8")
        stderr_path.write_text(stderr_text, encoding="utf-8")

        results.append(
            VerificationResult(
                name=name,
                exit_code=exit_code,
                duration_seconds=time.time() - started,
                command=command,
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
            )
        )
        append_terminal_log(
            config,
            "[verify] {0} exit={1} duration={2:.2f}s".format(name, exit_code, time.time() - started),
        )

    summary_path = artifact_dir / "verification.summary.json"
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump([result.__dict__ for result in results], handle, indent=2)

    return results
