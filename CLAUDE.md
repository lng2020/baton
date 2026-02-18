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
agent.yaml          — agent config (max_parallel_workers, poll_interval, claude_code settings)
tasks/              — task queue: pending/ in_progress/ completed/ failed/
scripts/            — worktree_manager.sh, merge_worktrees.sh, voice_input.py
infra/              — Dockerfile, docker-compose.yml, setup.sh, backup/
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
- `GET /api/projects/{id}/tasks` — all tasks grouped by status
- `POST /api/projects/{id}/tasks` — create task (`{title, content}`)
- `GET /api/projects/{id}/tasks/{status}/{filename}` — task detail
- `GET /api/projects/{id}/worktrees` — git worktrees
- `GET /api/projects/{id}/commits` — recent commits
- `GET /api/projects/{id}/dispatcher` — dispatcher status
- `POST /api/projects/{id}/dispatcher/restart` — restart dispatcher

Agent (`/agent/`):
- `GET /agent/health` — `{healthy: bool}`
- `GET /agent/tasks` — `{pending: [...], in_progress: [...], ...}`
- `POST /agent/tasks` — create task
- `GET /agent/dispatcher` — `{status, pid}`
- `POST /agent/dispatcher/{start|stop|restart}` — control dispatcher

## Task Execution Workflow

The dispatcher (background thread in the agent) handles this automatically:
1. Picks task from `tasks/pending/`
2. Moves to `tasks/in_progress/`
3. Creates git worktree (`worktrees/{task_id}`, branch `task/{task_id}`)
4. Launches Claude Code with task prompt
5. Monitors stream-json output, builds session log
6. On success: merges branch to main (with lock), moves task to `completed/`
7. On failure: moves task to `failed/`, writes `{task_id}.error.log`
8. Cleans up worktree

## Error Handling
- On blocking issues, task moves to `tasks/failed/` with error log
- Never repeat the same mistake — refer to PROGRESS.md
- HTTPConnector degrades gracefully: reads return empty on connection failure, writes return 502
- Merge conflicts: merge is aborted to keep main clean, task marked failed

## Rules
- Do not modify CLAUDE.md unless explicitly instructed
- Do not modify code in other worktrees
- Do not delete database data unless explicitly instructed
- Always run tests after completing a task
- Update PROGRESS.md with lessons learned after each task
