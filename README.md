# Baton

A multi-project dashboard and agentic task execution platform for Claude Code-driven development.

**Core philosophy: Humans think and decide, AI codes and executes.** The only bottleneck should be the speed at which you generate ideas.

## Quick Start

```bash
# 1. Install
pip install -e .

# 2. Start the agent (runs per-project, manages tasks + dispatcher)
baton-agent --port 9100

# 3. Start the dashboard (central web UI)
baton-dashboard --reload --port 8888

# 4. Open http://localhost:8888
```

## Architecture

Baton has two components:

- **Agent** (`baton-agent`) — runs per-project. Exposes task CRUD, git info, and dispatcher lifecycle over HTTP. The dispatcher runs as a background thread, picking up pending tasks, creating git worktrees, and launching Claude Code instances in parallel.

- **Dashboard** (`backend.server`) — central web UI. Talks to one or more agents via HTTP. Shows all projects on a single kanban board with task counts, health status, and dispatcher controls.

```
┌─────────────────────────────────────────────────┐
│  Dashboard (port 8888)                          │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐   │
│  │ Project A │  │ Project B │  │ Project C │   │
│  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘   │
└────────┼──────────────┼──────────────┼──────────┘
         │              │              │
    HTTP /agent/*  HTTP /agent/*  HTTP /agent/*
         │              │              │
   ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐
   │  Agent A  │  │  Agent B  │  │  Agent C  │
   │ :9100     │  │ :9101     │  │ :9102     │
   └───────────┘  └───────────┘  └───────────┘
```

## Repository Structure

```
baton/
├── backend/                     # Python package
│   ├── agent.py                 # Baton Agent (task exec, dispatcher, log monitor)
│   ├── server.py                # Dashboard server
│   ├── models.py                # Shared Pydantic models
│   ├── config.py                # Project config loader
│   ├── chat.py                  # Chat service (Anthropic API streaming)
│   ├── init_project.py          # baton-init CLI tool
│   ├── logging_config.py        # Centralized logging setup
│   ├── connectors/
│   │   ├── base.py              # ProjectConnector ABC
│   │   ├── http.py              # HTTPConnector (talks to agents)
│   │   └── local.py             # LocalConnector (direct filesystem)
│   └── github.py                # GitHub PR integration
│
├── frontend/                    # Single-page app (vanilla JS, dark theme)
│   ├── index.html               # Dashboard SPA
│   ├── css/style.css
│   └── js/app.js                # SPA controller
│
├── config/
│   └── projects.yaml            # Project registry
├── agent.yaml                   # Agent config (workers, polling, Claude Code, merge, ports)
│
├── data/                        # Runtime state (gitignored)
│   └── dev-tasks.json           # Centralized JSON task tracker (atomic claiming)
│
├── tasks/                       # Task queue
│   ├── pending/                 # Waiting for execution
│   ├── in_progress/             # Currently being worked on
│   ├── completed/               # Done
│   └── failed/                  # Failed (with error logs)
│
├── plans/                       # Plan storage (JSON files)
│   ├── draft/
│   ├── ready/
│   ├── executing/
│   ├── done/
│   └── failed/
│
├── CLAUDE.md                    # Claude Code behavior spec
├── PROGRESS.md                  # AI experience log
└── pyproject.toml               # Package config
```

## Agent API

The agent exposes these endpoints under `/agent/`:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/agent/health` | Health check |
| GET | `/agent/tasks` | All tasks grouped by status |
| GET | `/agent/tasks/{status}` | Tasks for a specific status |
| GET | `/agent/tasks/{status}/{filename}` | Single task detail |
| POST | `/agent/tasks` | Create a new task |
| GET | `/agent/worktrees` | Git worktree list |
| GET | `/agent/commits?count=N` | Recent git commits |
| GET | `/agent/dispatcher` | Dispatcher status |
| POST | `/agent/dispatcher/start` | Start the dispatcher |
| POST | `/agent/dispatcher/stop` | Stop the dispatcher |
| POST | `/agent/dispatcher/restart` | Restart the dispatcher |

## How Task Execution Works

1. Create a task (via dashboard UI or API) — written to `tasks/pending/` and `data/dev-tasks.json`
2. Dispatcher picks up the task, allocates a port, and claims it atomically in JSON
3. Creates an isolated git worktree (`worktrees/{task_id}`) with its own `data/` dir, symlinked shared files, and copied config files
4. Launches Claude Code with the task prompt and `TASK_PORT` environment variable
5. Monitors execution via stream-json output
6. **Merge + Test**: fetches `origin/main`, merges into task branch, runs configured test command
7. **Rebase merge**: rebases task branch onto `origin/main`, fast-forward merges to main, pushes to remote (with configurable retry logic)
8. Marks task complete in JSON **before** cleanup (crash-safe), moves `.md` to `completed/`
9. Cleans up worktree, deletes local and remote branches, releases port
10. On failure: marks failed in JSON, moves `.md` to `failed/` with error log

## Key Design Principles

1. **Agent as Single Source of Truth** — each project runs its own agent. The dashboard only reads/writes through agents.
2. **Context, not Control** — spend energy on better requirements, not micromanaging AI.
3. **Closed-loop Feedback** — AI writes code, runs tests, checks results, and debugs autonomously.
4. **Experience Accumulation** — PROGRESS.md is the AI's long-term memory. Same mistake never repeated.
5. **Parallelization** — git worktrees enable multiple Claude Code instances developing in parallel.
6. **Graceful Degradation** — if an agent is down, the dashboard shows it as unhealthy instead of crashing.

## Configuration

### `config/projects.yaml`

```yaml
projects:
  - id: my-project
    name: My Project
    path: /path/to/project
    agent_url: "http://localhost:9100"
    description: Project description
    color: "#e94560"
```

### `agent.yaml`

```yaml
max_parallel_workers: 5
poll_interval_seconds: 10
claude_code:
  skip_permissions: true
  output_format: "stream-json"
  verbose: true
  timeout: 600

# Port range for task workers (TASK_PORT env var)
port_range_start: 9200
port_range_end: 9299

# Test command run after merge (empty to skip)
test_command: "pytest"

# Push merged commits + delete remote branches
push_to_remote: true

# Files symlinked into worktrees (shared)
symlink_files:
  - "data/dev-tasks.json"

# Files copied into worktrees (isolated per worktree)
copy_files:
  - "CLAUDE.md"
  - "PROGRESS.md"

# Retry count for merge+test+rebase cycle
max_merge_retries: 3
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `BATON_PROJECT_DIR` | Override project directory for agent | Optional |
| `TASK_PORT` | Set automatically per task worker by dispatcher | Auto |

## New Project Setup

```bash
baton-init  # Interactive setup: clone repo, configure project, assign port
```

Or manually:
1. Install baton: `pip install -e /path/to/baton`
2. Initialize task directories: `mkdir -p tasks/{pending,in_progress,completed,failed}`
3. Create `agent.yaml` with desired settings
4. Start the agent: `baton-agent --project-dir /path/to/project --port 9100`
5. Add the project to baton's `config/projects.yaml`
6. Create tasks and let the dispatcher handle execution
