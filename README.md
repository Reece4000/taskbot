# Taskbot

`taskbot` is a thin local orchestration layer around the `codex` CLI.

It is designed to:

- start a fresh Codex session for each planning/execution pass
- keep prompts small by passing only the selected task plus compact repo index hints
- update `_taskbot/_tasks.md` statuses without asking the agent to manage the task file directly
- run repo-local verification commands after implementation
- optionally commit and push successful implementation changes to the active branch
- support repo-local execution settings for sandbox, approval policy, models, and verification mode
- stop gracefully between iterations via a stop file instead of killing the process
- stream agent progress into the terminal while still writing raw logs under `_taskbot/artifacts/`

## Commands

Run from the repo root:

```bash
python3 taskbot.py doctor
python3 taskbot.py list
python3 taskbot.py sync
python3 taskbot.py add-task --board "UI" --title "Example task"
python3 taskbot.py index --rebuild
python3 taskbot.py plan
python3 taskbot.py run --iterations 1
python3 taskbot.py run --continuous
python3 taskbot.py status
python3 taskbot.py stop
python3 taskbot.py resume
python3 taskbot.py ui
python3 taskbot.py --repo-root /path/to/repo status
```

## How It Works

1. Sync `_taskbot/_tasks.md` into `_taskbot/tasks.yaml`.
2. Pick the next runnable task from the YAML store.
3. If the task has not been scoped yet, run a planning pass and store the plan back into the task record.
4. Small localised tasks can skip the heavyweight planner pass and go straight into implementation with a compact auto-generated plan.
5. Otherwise, on the next pass, run implementation against the stored plan in a fresh Codex session.
6. Apply any verification commands configured for the selected repo, or skip them when the repo is configured for manual verification.
7. If git publishing is enabled, taskbot can create a commit and push the active branch after a successful implementation pass.
8. Move the task through phases such as `backlog`, `planning`, `ready`, `in_progress`, `needs_testing`, `blocked`, and `completed`.
9. Persist artifacts under `_taskbot/artifacts/`.

## Graceful Stop

`python3 taskbot.py stop` creates `_taskbot/control/stop`.

The runner checks for that file between phases and exits cleanly after the current phase finishes.

`python3 taskbot.py resume` clears the stop flag without starting a run.

## Task Store

Taskbot now uses `_taskbot/tasks.yaml` as its internal task store.

- The file is written as JSON-compatible YAML, which keeps the dependency surface small.
- Writes are atomic and guarded by a file lock.
- `_taskbot/_tasks.md` is still imported for legacy tasks.
- Markdown-origin tasks sync `completed` and `needs testing` back to `_taskbot/_tasks.md`.

This is what allows the CLI loop and a future long-running UI to update tasks safely without save conflicts.

## Two-Pass Flow

Tasks added with minimal detail start in `backlog`.

- First pass: taskbot runs planning, discovers relevant files and execution steps, and stores them in the YAML task record.
- Second pass: taskbot moves the task to implementation using the stored plan.
- If a task is too large for one clean pass, planning can decompose it into several smaller subtasks, create boards for them if needed, and store each subtask with its own ready plan.

That gives you lightweight task entry without throwing away structure.

## Desktop UI

Install the UI dependency:

```bash
python3 -m pip install -r requirements.txt
```

Then launch:

```bash
python3 taskbot.py ui
```

The desktop UI provides:

- a repository selector across the top
- a left-side board list with quick board creation
- centered workflow columns in the middle
- a compact add-task button that opens a non-blocking dialog
- runner controls for planning and execution, including a Start Loop dialog for indefinite or fixed-length runs
- a settings dialog for sandbox/approval policy, default models, tiny-task fast path, verification policy, and git publishing
- a bottom terminal pane backed by `_taskbot/control/terminal.log`

When you load a repository, task state, artifacts, and logs are written under that repository's `_taskbot/` directory.

## Token Strategy

Taskbot reduces prompt size by:

- sending only one task at a time
- using fresh ephemeral sessions
- avoiding raw file dumps
- feeding Codex a compact static repo index instead of broad code excerpts
- storing run artifacts locally instead of re-explaining prior attempts every turn

## Verification

Verification is repo-local. Configure it in either `taskbot.config.json` or `<repo>/_taskbot/config.json`.

- `verification.mode = "manual"` skips automated checks and tells the agent to leave concise manual follow-up notes instead of repeatedly retrying speculative tests.
- `verification.mode = "commands"` runs the configured command list after implementation.
- `verification.instructions` lets you store repo-specific testing guidance for the agent.

Terminal color output is controlled by `codex.stream_ansi` in taskbot config:

- `"auto"`: enable ANSI colors only when writing to a TTY
- `"always"`: always emit ANSI colors
- `"never"`: plain text only

## Git Integration

Git publishing is repo-local and disabled by default.

- Enable it under `git.enabled` in `taskbot.config.json` or `<repo>/_taskbot/config.json`.
- Taskbot only attempts git publishing after a successful implementation pass. Planning-only runs do not commit.
- By default, `git.require_clean_worktree = true` skips publishing if the session started with existing publishable changes or any staged changes.
- Taskbot excludes its own runtime files such as `_taskbot/artifacts`, `_taskbot/state`, `_taskbot/control`, the task store, and the markdown task file from auto-commits.
- Pushes use the current branch upstream when available. If there is no upstream, taskbot uses `git.remote` when set, or the single configured remote when there is exactly one.
- Session artifacts include `_taskbot/artifacts/.../git.result.json` plus per-command stdout/stderr logs under the run's `git/` subdirectory.

## Notes

- The runner is serial at the outer loop level. If subagents are useful, the implementation prompt explicitly allows Codex to delegate within a task.
- Runtime-only tasks should usually end as `[needs testing]` unless the available verification hooks make the behaviour clear.
- All state and artifacts are repo-local and ignored by Git.
