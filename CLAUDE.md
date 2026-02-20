# Baton

Multi-project dashboard and agentic task execution platform.

## Architecture

Two components:
- **Agent** (`baton-agent`) — per-project process. HTTP API + built-in dispatcher that picks up pending tasks, creates git worktrees, and runs Claude Code in parallel.
- **Dashboard** (`backend.server`) — central web UI on port 8888. Talks to agents via HTTPConnector. Never accesses tasks or dispatchers directly.

The agent is the single source of truth. All task and dispatcher operations go through it.

## Quick Start
```bash
pip install -e .
baton-agent --port 9100                                    # agent
python -m uvicorn backend.server:app --reload --port 8888  # dashboard
```

## Project Layout
```
backend/
  agent.py          — Agent: HTTP API, dispatcher, CC launcher, log monitor, plan review
  server.py         — Dashboard: routes, connector selection, template rendering
  models.py         — Shared Pydantic models (TaskSummary, TaskDetail, DispatcherStatus, etc.)
  config.py         — ProjectConfig loader from config/projects.yaml
  connectors/
    base.py         — ProjectConnector ABC
    http.py         — HTTPConnector (proxies to agent)
    local.py        — LocalConnector (direct filesystem, fallback)
  github.py         — GitHub PR lookup via gh CLI
frontend/           — Jinja2 templates + vanilla JS (dark theme)
config/projects.yaml — project registry (id, path, agent_url, color)
agent.yaml          — agent config (workers, polling, claude_code, merge, ports, push)
tasks/              — task queue: pending/ plan_review/ in_progress/ completed/ failed/
data/               — runtime state: dev-tasks.json (centralized JSON task tracker)
```

## Conventions
- Python 3.11+, FastAPI, Pydantic v2
- Dark theme: #1a1a2e (bg), #16213e (secondary), #0f3460 (accent-dark), #e94560 (accent)
- Status colors: pending=#f39c12, in_progress=#3498db, completed=#2ecc71, failed=#e74c3c
- Task files: `tasks/{status}/{task_id}.md` — title extracted from first `# heading`
- Error logs: `{task_id}.error.log` alongside task file
- Session logs: `{task_id}.log.json` alongside task file
- Commit style: Conventional Commits — `feat(task_id): description`

## API Endpoints

Dashboard (`/api/`):
- `GET /api/projects` — all projects with task counts and health
- `GET /api/projects/{id}/tasks` — all tasks grouped by status (includes `plan_review`)
- `POST /api/projects/{id}/tasks` — create task (`{title, content, needs_plan_review}`)
- `GET /api/projects/{id}/tasks/{status}/{filename}` — task detail (includes `plan_content`)
- `POST /api/projects/{id}/tasks/{task_id}/approve-plan` — approve plan, move to pending
- `POST /api/projects/{id}/tasks/{task_id}/revise-plan` — revise plan (`{feedback}`)
- `POST /api/projects/{id}/tasks/{task_id}/reject-plan` — reject plan, move to failed
- `GET /api/projects/{id}/worktrees` — git worktrees
- `GET /api/projects/{id}/commits` — recent commits
- `GET /api/projects/{id}/dispatcher` — dispatcher status
- `POST /api/projects/{id}/dispatcher/restart` — restart dispatcher

Agent (`/agent/`):
- `GET /agent/health` — `{healthy: bool}`
- `GET /agent/tasks` — `{pending: [...], plan_review: [...], in_progress: [...], ...}`
- `POST /agent/tasks` — create task (with optional `needs_plan_review`)
- `POST /agent/tasks/{task_id}/approve-plan` — approve plan review
- `POST /agent/tasks/{task_id}/revise-plan` — revise plan (body: `{feedback}`)
- `POST /agent/tasks/{task_id}/reject-plan` — reject plan
- `GET /agent/dispatcher` — `{status, pid}`
- `POST /agent/dispatcher/{start|stop|restart}` — control dispatcher

## Task Execution Workflow

The dispatcher (background thread in the agent) handles this automatically.

### Task Mode (default: `needs_plan_review=false`)
1. Allocates a port from the configured range (`TASK_PORT` env var)
2. Claims task atomically in `data/dev-tasks.json`, moves `.md` to `tasks/in_progress/`
3. Creates git worktree (`worktrees/{task_id}`, branch `task/{task_id}`) with isolated `data/` dir, symlinked shared files, and copied config files
4. Launches Claude Code with task prompt and `TASK_PORT` environment variable
5. Monitors stream-json output, builds session log
6. Merge + Test: fetches `origin/main`, merges into task branch, runs test command
7. Rebase merge: rebases task branch onto `origin/main`, fast-forward merges to main, pushes to remote (with retry logic, configurable via `max_merge_retries`)
8. Marks task complete in JSON **before** cleanup (crash-safe), moves `.md` to `completed/`
9. Cleans up worktree, deletes local and remote branch, releases port
10. On failure: marks failed in JSON, moves `.md` to `failed/`, writes `{task_id}.error.log`

### Plan Mode (`needs_plan_review=true`)
Tasks created with the Plan toggle go through an additional CC-powered planning phase:

1. **Plan Phase**: Dispatcher picks up pending task with `needs_plan_review=true` and no `.plan.md` sidecar
   - Runs CC in project root (read-only, no worktree, no port)
   - Prompt: "Analyze this task and create a detailed implementation plan. Do NOT modify any files."
   - Extracts plan from CC output → saves as `{task_id}.plan.md` sidecar
   - Moves task to `tasks/plan_review/`, updates JSON status to `plan_review`

2. **User Review**: User sees the plan in the dashboard detail panel
   - **Approve** → moves task+plan to `pending/` (dispatcher will execute with plan context)
   - **Revise** → appends feedback to task `.md`, deletes `.plan.md`, moves to `pending/` (re-plans)
   - **Reject** → moves to `failed/`

3. **Execution Phase**: When dispatcher picks up a task with `needs_plan_review=true` AND `.plan.md` exists, it runs the normal Task Mode flow but injects the approved plan as context in the CC prompt.

Status flow: `pending → in_progress [PLANNING] → plan_review → pending → in_progress [EXECUTING] → completed/failed`

Task file metadata: `plan_review: true` line in the `.md` file. Sidecar: `{task_id}.plan.md`.

## Parallel Development (Git Worktree)

Multiple Claude Code instances work in parallel, each in its own git worktree.

```
                    Parallel Development Workflow
 ┌──────────────────────────────────────────────────────────┐
 │                                                          │
 │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐      │
 │  │  Worker 1   │  │  Worker 2   │  │  Worker 3   │ ...  │
 │  │  port:9200  │  │  port:9201  │  │  port:9202  │      │
 │  │  worktree   │  │  worktree   │  │  worktree   │      │
 │  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘      │
 │         │                │                │              │
 │      ┌──┴──┐          ┌──┴──┐          ┌──┴──┐          │
 │      │data/│          │data/│          │data/│  isolated │
 │      └─────┘          └─────┘          └─────┘          │
 │                                                          │
 └──────────────────────────────────────────────────────────┘

 Shared files (symlink):
   - data/dev-tasks.json   (task queue)

 Copied files (isolated per worktree):
   - CLAUDE.md
   - PROGRESS.md

 Do NOT symlink PROGRESS.md — each worker edits it
 independently; use `git -C` to write to main repo instead.
```

Configured in `agent.yaml`:
- `symlink_files` — files symlinked into worktrees (shared state)
- `copy_files` — files copied into worktrees (isolated per worker)
- `port_range_start` / `port_range_end` — port range for `TASK_PORT` env var

## Error Handling
- On blocking issues, task moves to `tasks/failed/` with error log
- Never repeat the same mistake — refer to PROGRESS.md
- HTTPConnector degrades gracefully: reads return empty on connection failure, writes return 502
- Task state tracked in both `data/dev-tasks.json` (authoritative) and `tasks/` filesystem (for dashboard display)

### Conflict Resolution

**When rebase fails:**
1. If "unstaged changes" error — commit or stash current changes first
2. If merge conflicts:
   - `git status` to see conflicting files
   - Read the conflicting files, understand both sides' intent
   - Resolve manually (keep the correct code)
   - `git add <resolved-files>`
   - `git rebase --continue`
3. Repeat until rebase completes

**When tests fail:**
1. Run the test command (configured in `agent.yaml`, default: `pytest`)
2. If failures, analyze the error output
3. Fix the bug in the code
4. Re-run tests until all pass
5. Commit the fix: `git commit -m "fix: ..."`

**Never give up**: when rebase or tests fail, you must resolve the issue before continuing — do not mark the task as failed without attempting a fix.

## Rules
- Do not modify CLAUDE.md unless explicitly instructed
- Do not modify code in other worktrees
- Do not delete database data unless explicitly instructed
- Always run tests after completing a task
- Update PROGRESS.md with lessons learned after each task
