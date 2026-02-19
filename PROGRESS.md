# Progress

## 2026-02-19: Fix execute plan 500 Internal Server Error caused by unhandled httpx exception

### What was done
- The dashboard's `POST /api/projects/{id}/plans/{plan_id}/execute` was returning a 500 ISE because `HTTPConnector.execute_plan()` called `resp.raise_for_status()` which threw `httpx.HTTPStatusError` on non-2xx agent responses — this exception wasn't caught by `server.py` which only handles `ConnectionError` and `NotImplementedError`
- Fixed `execute_plan()` in `backend/connectors/http.py`: replaced bare `raise_for_status()` with explicit status code checks, converting non-200 responses to `ConnectionError` with a descriptive message — consistent with how `create_task()` already handles errors
- Applied the same fix to `create_plan()` which had the identical vulnerability
- Both methods now also catch `httpx.HTTPError` (connection failures) and convert to `ConnectionError`

### Lessons learned
- `httpx.HTTPStatusError` from `raise_for_status()` is NOT a subclass of `ConnectionError` — if the calling code only catches `ConnectionError`, the HTTP error propagates as an unhandled 500 ISE
- The existing sync methods in HTTPConnector (e.g., `create_task`) already followed the correct pattern: catching `httpx.HTTPStatusError` and converting to `ConnectionError`. The async methods (`execute_plan`, `create_plan`) were added later without this pattern
- Explicit status code checks (`if resp.status_code != 200`) are clearer and safer than `raise_for_status()` + catch — they make the error handling visible at the point of the HTTP call rather than relying on exception propagation

## 2026-02-19: Fix 405 Method Not Allowed on POST /agent/plans/{plan_id}/execute

### What was done
- Reordered FastAPI route definitions in `backend/agent.py` so that `POST /agent/plans/{plan_id}/execute` is defined before `GET /agent/plans/{status}/{filename}`
- Removed leftover dead code (duplicate function body) that remained after the route reorder

### Lessons learned
- FastAPI evaluates routes in definition order. When two routes share the same path structure (e.g., `/plans/{a}/{b}` vs `/plans/{x}/execute`), the first registered route wins the match. If the matched route doesn't support the request's HTTP method, FastAPI returns 405 Method Not Allowed instead of trying other routes
- Routes with literal path segments (like `/execute`) should always be defined before routes with all-variable path segments (like `/{status}/{filename}`) to avoid this class of routing conflict
- A 405 response (not 404) is a strong signal that the path matched a route but the HTTP method didn't — look for overlapping path patterns in the route table

## 2026-02-18: Add centralized logging with file output

### What was done
- Created `backend/logging_config.py` — centralized logging setup with both console and file handlers
- Log file written to `logs/baton.log` with `RotatingFileHandler` (5 MB max, 3 backups)
- Log format includes module name: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
- Replaced `logging.basicConfig()` in `agent.py` with `setup_logging()` call in `main()`
- Added `--log-level` CLI flag to both `baton-agent` and dashboard `main()`
- Added `logger = logging.getLogger(__name__)` and logging calls to modules that lacked them: `server.py`, `config.py`, `github.py`, `connectors/local.py`
- Added `logs/` to `.gitignore` and to the dashboard's `_RELOAD_EXCLUDES`
- `setup_logging()` is idempotent — safe to call multiple times (guarded by `_configured` flag)

### Lessons learned
- `logging.basicConfig()` at module import time (line 49 of agent.py) configures the root logger before CLI args are parsed, making `--log-level` ineffective — moving setup into `main()` gives the CLI flag control over the level
- The format string should include `%(name)s` so log lines are traceable to their source module — the original format omitted this, making it hard to distinguish agent vs chat vs connector log lines
- `RotatingFileHandler` is preferable to a plain `FileHandler` for long-running agent processes — without rotation, the log file grows unbounded
- Adding `logs/` to `_RELOAD_EXCLUDES` in the dashboard prevents uvicorn's file watcher from triggering reloads on every log write when `--reload` is enabled

## 2026-02-18: Remove scripts folder (replaced by Python code)

### What was done
- Removed `scripts/` directory containing `worktree_manager.sh`, `merge_worktrees.sh`, `setup.sh`, and `backup/db_backup_cron.sh`
- These shell scripts were superseded by Python equivalents in `backend/agent.py` (worktree create/merge/cleanup) and `backend/init_project.py` (project setup)
- Updated `README.md` to remove the `scripts/` section from the repository structure tree
- Updated a comment in `backend/agent.py` that referenced `worktree_manager.sh`
- Did not modify `CLAUDE.md` per project rules (still references scripts/ in project layout — can be updated separately if instructed)

### Lessons learned
- Before deleting code, grep for all references across the codebase — documentation files (README, CLAUDE.md) and code comments often reference deleted artifacts
- The shell scripts were already fully replaced: `_create_worktree()`, `_merge_to_main()`, and `_cleanup_worktree()` in `agent.py` cover all worktree operations, and `baton-init` CLI covers project setup
- PROGRESS.md itself contained historical references to `worktree_manager.sh` — these are fine to leave as historical context since they describe what was done at that time, not current state

## 2026-02-18: Add Plan models and plans/ file storage to agent

### What was done
- Added `PlanStatus` enum (`draft`, `ready`, `executing`, `done`, `failed`), `PlanSummary`, `PlanDetail`, and `PlanCreateRequest` models to `backend/models.py`
- Added `plans` property and `plans_status()` method to `AgentDir` in `backend/agent.py`, mirroring the existing `tasks`/`tasks_status` pattern
- Replaced dead-code `PlanReviewQueue` class and `Plan`/`ReviewStatus` dataclasses (lines 377-455) with new plan CRUD helpers: `_list_plans`, `_read_plan`, `_create_plan`, `_update_plan_status`, `_link_tasks_to_plan`
- Plans stored as JSON files in `plans/{status}/{plan_id}.plan.json`, mirroring the tasks directory pattern
- Added agent API endpoints: `GET /agent/plans`, `GET /agent/plans/{status}/{filename}`, `POST /agent/plans`, `POST /agent/plans/{plan_id}/start`
- Updated `GET /agent/health` to include `plan_counts` with per-status counts
- Created `plans/` directory structure with `draft/`, `ready/`, `executing/`, `done/`, `failed/` subdirectories and `.gitkeep` files
- Cleaned up unused imports (`Enum`, `time`) from `backend/agent.py`

### Lessons learned
- Storing plans as JSON files (rather than markdown like tasks) makes sense because plans have structured metadata (tasks list, error, timestamps) that is awkward to encode in markdown frontmatter
- The `_update_plan_status` and `_link_tasks_to_plan` helpers search all status directories to find a plan by ID, which is simple but O(n) in the number of statuses — acceptable since there are only 5 statuses
- `PlanDetail` extends `PlanSummary` (inheritance) to avoid field duplication, unlike `TaskDetail` which is a standalone model — the inheritance approach is cleaner for JSON-backed models where all fields come from the same source

## 2026-02-18: Add task type support

### What was done
- Added `TaskType` enum to `backend/models.py` with values: `feature`, `bugfix`, `refactor`, `chore`, `docs`, `test`
- Added `task_type` field to `TaskSummary`, `TaskDetail`, and `TaskCreateRequest` models (defaults to `feature` for backward compatibility)
- Task type is stored in the markdown task file as a `type: <value>` metadata line after the title heading
- Added `_extract_task_type()` helper in both `backend/agent.py` and `backend/connectors/local.py` to parse the type from task files
- Updated `_create_task()` in agent and local connector to write the type metadata line
- Updated all connector methods (`create_task`, `list_tasks`, `read_task`, `create_tasks_bulk`) to pass task_type through
- Updated dashboard server routes to forward task_type from request body to connectors
- Added task type selector (`<select>`) to the task creation form in `frontend/index.html`
- Added colored task type badges on kanban cards and in the task detail panel in `frontend/js/app.js`
- Added CSS styles for `.task-type-badge` with per-type colors (feature=blue, bugfix=red, refactor=purple, chore=gray, docs=green, test=yellow)
- Added CSS for the task form select element with custom dropdown arrow

### Lessons learned
- Using a `type:` metadata line in the markdown file (rather than frontmatter or filename convention) is simple to parse and backward-compatible — existing tasks without the line default to `feature`
- Making `TaskType` a `str, Enum` hybrid allows Pydantic to serialize it as a plain string in JSON responses, which simplifies frontend consumption
- The `_extract_task_type` helper needs to exist in both `agent.py` and `local.py` since they independently parse task files — a shared utility would reduce duplication but these are small functions

## 2026-02-18: Fix multiple worktree conflict with unified git lock

### What was done
- Renamed `_merge_lock` to `_git_lock` to reflect its broader purpose: serializing all git operations on the root repo
- Wrapped `_create_worktree()` with `_git_lock` to prevent races between worktree creation and concurrent merges (both touch the root repo's HEAD/refs)
- Wrapped `_cleanup_worktree()` with `_git_lock` to prevent branch deletion from racing with checkout/merge operations
- Added proper error handling to `_create_worktree()`: replaced `check=True` with explicit return-code checking and `capture_output=True` for diagnostic stderr
- Added timeouts to all git subprocess calls in create/cleanup (30s each)
- Added `shutil.copy2` of CLAUDE.md and PROGRESS.md into new worktrees, mirroring `worktree_manager.sh` behavior
- Updated and expanded tests: renamed lock assertion, added `TestCreateWorktree` (3 tests) and `TestCleanupWorktree` (2 tests)

### Lessons learned
- The original `_merge_lock` only protected merges, but `git worktree add` also reads the base ref (main) — if a merge is in progress and has checked out main, a concurrent `git worktree add -b ... main` can see an inconsistent state or fail due to the index being locked
- `_cleanup_worktree` running `git branch -D` without the lock can race with `git checkout main` in the merge path, potentially deleting a branch that's being merged
- A single unified lock (`_git_lock`) for all root-repo git operations is simpler and safer than multiple fine-grained locks — the critical sections are short (git commands with timeouts) so contention is minimal
- The `worktree_manager.sh` script copies CLAUDE.md and PROGRESS.md into worktrees, but `agent.py` was not doing this — Claude Code instances in worktrees need these files for project context
- `_thread.lock` attributes (`acquire`, `release`) are read-only in Python — tests that need to verify lock usage should replace the lock with a `MagicMock` rather than monkey-patching individual methods

## 2026-02-18: Fix submitTask() race condition on project switch

### What was done
- Applied the async context capture + staleness guard pattern to `submitTask()` in `frontend/js/app.js`
- Captured `selectedProjectId` into `targetProjectId` before the POST fetch, so the request always targets the project that was active when the user clicked submit
- After successful task creation, only calls `loadTasks()` if the user is still on the same project — prevents a confusing refresh of a different project's kanban board

### Lessons learned
- The `submitTask()` function was missed when the staleness guard pattern was applied to `loadTasks()`, `loadWorktrees()`, `loadCommits()`, and `openTaskDetail()` — write operations (POST) need the same treatment as read operations (GET)
- Without the guard, creating a task and quickly switching projects makes the task appear "lost" because `loadTasks()` refreshes the kanban for the new project, not the one the task was created in
- The pattern is simple and consistent: capture project ID, use it in the fetch URL, guard state updates after await

## 2026-02-18: Fix tasks/worktrees/commits lost on project switch

### What was done
- Applied the same async context capture pattern (already used in `sendMessage()`) to `loadTasks()`, `loadWorktrees()`, `loadCommits()`, and `openTaskDetail()` in `frontend/js/app.js`
- Each function now captures `selectedProjectId` into a local `targetProjectId` at call time and uses it for the fetch URL
- After each `await` (fetch + JSON parse), a staleness guard (`selectedProjectId !== targetProjectId`) discards the response if the user switched projects during the request
- This prevents a late-arriving response from a previous project from overwriting the current project's data and polluting the polling cache

### Lessons learned
- The async context capture pattern must be applied consistently to ALL async functions that read a mutable global and update shared state — fixing it in one place (`sendMessage`) but not others (`loadTasks`, `loadWorktrees`, `loadCommits`) leaves the same class of bug open
- The polling cache (`lastTasksJson` etc.) amplifies the race condition: if a stale response from the old project gets cached, subsequent polls for the new project may find the cache "matches" and skip rendering, making the stale data persist until the next project switch
- `openTaskDetail()` had the same vulnerability — a slow detail fetch could render the wrong project's task in the detail panel after a project switch

## 2026-02-18: Fix chat session/project mismatch on project switch

### What was done
- In `sendMessage()`, captured `selectedProjectId` into a local `targetProjectId` at call time and used it for the fetch URL, so the request always targets the project that was active when the user clicked send
- Added guards in the streaming response handler: on `done` event, check `selectedProjectId === targetProjectId` before updating `chatSessionId` or calling `tryParsePlan()` — stale responses from a previous project are discarded
- Guarded `chatHistory.push()` after streaming completes — only appends the assistant response if the user is still on the same project
- Added `btnSend.disabled = false` to `resetChat()` so the send button is immediately re-enabled when switching projects during a stream
- Added `currentPlanProjectId` state variable, set when a plan is parsed, and used in `confirmPlan()` instead of `selectedProjectId` — ensures tasks are created in the project the plan was generated for
- In `confirmPlan()`, if the active project differs from the plan's project, a `confirm()` dialog warns the user before proceeding

### Lessons learned
- Async operations (streaming responses) that update shared state must capture their context (project ID) at invocation time, not rely on a mutable global that may change during execution
- A simple identity check (`selectedProjectId !== targetProjectId`) at key state-update points is sufficient to discard stale responses without needing AbortController or complex cancellation logic
- Plan confirmation is a separate concern from plan generation — storing the project ID at plan-parse time and using it at confirm time prevents a subtle bug where the user switches projects between seeing and confirming a plan
## 2026-02-18: Add Plan/Task mode toggle UI with task creation form

### What was done
- Added a Plan/Task mode toggle (pill buttons) to the chat header in `frontend/index.html`, between the title and action buttons
- Added a task creation form (`task-form`) as a sibling of `chat-body` inside `chat-section`, hidden by default
- Added `chatMode` state variable and `switchMode()` function in `frontend/js/app.js` that toggles visibility between the chat body and task form
- Added `submitTask()` function that POSTs to `/api/projects/{id}/tasks` and refreshes the kanban board on success
- Wired Enter key on the task title input to submit the form
- Mode state is preserved across project switches — `resetChat()` does not reset mode
- Added CSS styles for `.chat-mode-toggle`, `.mode-btn`, `.task-form`, and `.task-form-body` in `frontend/css/style.css`

### Lessons learned
- Placing the mode toggle between the `<h3>` title and `.chat-header-actions` div in the flex header naturally spaces it between the title and buttons via `justify-content: space-between`
- The task form is a sibling of `chat-body` rather than nested inside it, so `switchMode()` can toggle their `display` properties independently without affecting the chat's internal collapsed state
- Preserving `chatMode` across `resetChat()` calls means project switches don't disrupt the user's current workflow — if they were in task mode, they stay there

## 2026-02-18: Skip worktree/commits/tasks re-render when data is unchanged

### What was done
- Added module-level cache variables (`lastTasksJson`, `lastWorktreesJson`, `lastCommitsJson`) at the top of the IIFE in `frontend/js/app.js`
- In `loadTasks()`, `loadWorktrees()`, and `loadCommits()`: after fetching JSON, stringify and compare to cached value; skip DOM render if identical, otherwise update cache and proceed
- Reset all three caches to `null` in `selectProject()` so a project switch always triggers a full render
- Eliminates visual disruption (flashing, scroll position reset, hover state loss) from the 15-second polling interval when data hasn't changed

### Lessons learned
- `JSON.stringify()` comparison is a simple and effective way to detect data changes for moderate-sized payloads like task lists and commit histories
- Caches must be reset on project switch, otherwise stale cache from the previous project could suppress the initial render of the new project's data
- The cache check should happen after the fetch but before any DOM manipulation, so network requests still occur (detecting changes) but the DOM is left untouched when unnecessary

## 2026-02-18: Add `baton init` CLI command for project creation from GitHub repo

### What was done
- Created `backend/init_project.py` with a `main()` function implementing the `baton-init` CLI command
- Supports GitHub repo URL (`https://github.com/user/repo`) and shorthand (`user/repo`) formats
- Interactive flow: clone confirmation, project ID/name/description prompts, auto port assignment
- Creates tasks directory structure (`pending/`, `in_progress/`, `completed/`, `failed/`) in the project
- Copies `agent.yaml` template to the new project root
- Appends new project entry to `config/projects.yaml` with all required fields
- Auto-assigns agent port by scanning existing projects for used ports
- Handles edge cases: existing directory, duplicate project ID, missing `config/projects.yaml`
- Registered `baton-init` entry point in `pyproject.toml`

### Lessons learned
- Using `re.match` with separate patterns for full URL vs shorthand keeps the repo parsing clean and avoids complex combined regexes
- `load_projects_yaml()` handles both missing file and empty file (yaml.safe_load returns None) as separate cases to avoid TypeErrors
- Auto-port assignment by scanning `agent_url` fields in existing config is more reliable than trying to check if ports are actually in use, since the config is the source of truth for baton

## 2026-02-18: Add clear conversation button and auto-clear on plan confirm

### What was done
- Added a "Clear" button (✕) to the chat header actions, positioned before the collapse toggle button
- Styled the clear button to match the toggle button style, with red hover color to indicate destructive action
- Wired the clear button to `resetChat()` to clear entire conversation, plan, and session state
- Changed `confirmPlan()` to call `resetChat()` after successful task creation instead of manually hiding the plan element, since `resetChat()` already handles `currentPlan = null` and hiding the plan

### Lessons learned
- `resetChat()` already handles all the cleanup that `confirmPlan()` was doing manually (hiding plan element, nulling currentPlan), so replacing the manual lines with a single `resetChat()` call is both cleaner and ensures the full conversation is cleared after task creation
- Placing the clear button before the collapse toggle in the header actions div follows the convention of destructive actions appearing first (left) in a button group

## 2026-02-18: Discussion-first task creation with agent engineer chat

### What was done
- Replaced direct task creation with a ChatGPT-like discussion dialog as the default entry point for "+ Task"
- Added `backend/chat.py` — new module handling Anthropic API integration with async streaming and plan extraction
- Added 5 new Pydantic models: `ChatMessage`, `ChatRequest`, `ChatPlanTask`, `ChatPlan`, `BulkTaskCreateRequest`
- Added `ChatConfig` dataclass and `chat:` section to `agent.yaml` (configurable model and max_tokens)
- Added 3 new agent endpoints: `POST /agent/chat` (SSE streaming), `POST /agent/chat/plan`, `POST /agent/tasks/bulk`
- Added 3 new dashboard proxy routes: `/api/projects/{id}/chat`, `/api/projects/{id}/chat/plan`, `/api/projects/{id}/tasks/bulk`
- Extended `ProjectConnector` ABC with `chat_stream`, `chat_plan`, `create_tasks_bulk` abstract methods
- `HTTPConnector` uses a separate `httpx.AsyncClient` for streaming chat operations alongside existing sync client
- `LocalConnector` raises `NotImplementedError` for chat methods (requires agent connection)
- Frontend chat dialog: message bubbles (user/assistant), SSE stream reading via `fetch` + `ReadableStream`, plan display with numbered task cards, "Create Tasks" and "Continue Discussion" buttons
- "Simple mode" button provides fallback to the original title+content modal
- Added `anthropic>=0.39` to project dependencies
- Added 12 new tests covering system prompt building, JSON extraction, model validation, and bulk task creation

### Lessons learned
- `EventSource` only supports GET requests; for POST-based SSE (sending conversation history), use `fetch()` with `ReadableStream` reader and manual SSE line parsing instead
- Keeping the sync `httpx.Client` for existing connector methods and adding a separate `httpx.AsyncClient` for streaming avoids a large refactor while enabling async streaming proxy
- The system prompt instructs the LLM to output a specific JSON structure; having both inline detection (`tryParsePlan` in frontend) and a dedicated `/chat/plan` endpoint provides flexibility — the inline approach handles the common case while the explicit endpoint can force structured output
- Adding abstract methods to the connector ABC means all implementations must provide them, but `NotImplementedError` stubs in `LocalConnector` are acceptable since chat requires a running agent with API key access

## 2026-02-18: Redesign dashboard as single-layout SPA

### What was done
- Replaced the two-page layout (home grid + project detail page) with a single-page 3-column layout
- Left sidebar: project list with health indicators and task count badges; click to select project
- Main area top: kanban board (4 columns) for the selected project
- Right sidebar: worktrees and recent commits for the selected project
- Responsive breakpoints: right sidebar hides at <1200px, left sidebar hides at <768px, both accessible via toggle buttons
- Merged `kanban.js`, `detail.js`, and `app.js` into a single `app.js` SPA controller
- Removed `project.html` template and the `/project/{project_id}` server route
- All API endpoints remain unchanged; frontend is purely client-side routing
- Task detail slide-in panel and create-task modal preserved

### Lessons learned
- A single-page layout eliminates full-page navigations and keeps all context visible at once, but requires careful height management (`overflow: hidden` on body, flex column for main content, `min-height: 0` for nested scrollable areas)
- CSS Grid with named columns (`grid-template-columns: var(--sidebar-left-w) 1fr var(--sidebar-right-w)`) makes responsive overrides straightforward — just change the template at breakpoints
- Merging three JS files into one reduces the surface for duplicated helper functions (`escHtml`, `escAttr`) and eliminates coordination issues between separate IIFEs
- When consolidating, exposing functions via `window._functionName` for inline `onclick` handlers is a pragmatic bridge — avoids event delegation boilerplate while keeping the IIFE scope private

## 2026-02-18: Remove dispatcher UI from dashboard

### What was done
- Removed dispatcher status bar and restart button from `frontend/project.html`
- Removed `loadDispatcherStatus()` function, restart button handler, and polling interval from `frontend/js/kanban.js`
- Removed dispatcher indicator (play/stop icons) from project cards in `frontend/js/app.js`
- Removed all dispatcher CSS styles (`.dispatcher-bar`, `.dispatcher-dot`, `.btn-restart`, `.dispatcher-indicator`) from `frontend/css/style.css`
- Removed `GET /api/projects/{id}/dispatcher` and `POST /api/projects/{id}/dispatcher/restart` routes from `backend/server.py`
- Removed `dispatcher` field from `ProjectSummary` model and dispatcher status fetching from the projects list endpoint
- Removed `get_dispatcher_status()` and `dispatcher_action()` methods from `HTTPConnector`
- Kept `DispatcherStatus` model in `models.py` since the agent still uses it

### Lessons learned
- When removing a feature from one component (agent dispatcher), the UI layer must be cleaned up too — otherwise stale UI elements show confusing "unknown" status
- Shared models (`DispatcherStatus`) may still be needed by other components (agent) even when the dashboard no longer uses them; check all importers before deleting
- The dispatcher bar was hidden by default (`display:none`) and only shown on successful API response, so the "unknown" status appeared when the API route returned an error or the agent was unreachable

## 2026-02-18: Persist create-task modal values unless Cancel or Create

### What was done
- Changed `openCreateModal()` to no longer clear form fields on open — values are preserved across open/close cycles
- Extracted field clearing into `clearCreateModal()` helper
- Cancel button now calls `clearCreateModal()` before closing (intentional discard)
- Successful task creation calls `clearCreateModal()` before closing (fields no longer needed)
- X button and overlay click just close without clearing, preserving the user's in-progress input

### Lessons learned
- The previous approach of clearing fields on every modal open was hostile to accidental dismissals (clicking outside the modal lost all typed content)
- Separating "close" from "clear" into two distinct operations gives fine-grained control over when form state is discarded vs preserved

## 2026-02-18: Make agent_dir single entry point for project access

### What was done
- Introduced `AgentDir` dataclass in `backend/agent.py` as the single entry point for all project directory access
- `AgentDir` centralizes `root`, `tasks`, `worktrees`, and `tasks_status()` into one object instead of three separate module-level globals (`PROJECT_DIR`, `TASKS_DIR`, `WORKTREES_DIR`)
- `AgentDir.resolve()` classmethod handles resolution from CLI arg, `BATON_PROJECT_DIR` env var, or cwd
- Updated all references throughout agent.py (task helpers, git helpers, dispatcher, health endpoint, CLI) to use `agent_dir`
- Updated tests in `test_agent_merge.py` to mock `agent_dir` instead of the removed `PROJECT_DIR` global
- Fixed `test_reload_with_dispatcher_config` in `test_config.py` which was testing a removed `dispatcher` field on `ProjectConfig`

### Lessons learned
- Scattering directory paths across multiple module-level globals makes it easy for them to drift out of sync (e.g. the CLI `main()` had to reassign all three globals atomically)
- A dataclass with computed properties (`tasks`, `worktrees`) derived from a single `root` path eliminates the possibility of inconsistency
- When replacing a module-level global with a new name, tests that mock the old name via `@patch("module.OLD_NAME")` need updating to match
- The `test_reload_with_dispatcher_config` test was a pre-existing bug — it tested a `dispatcher` attribute that was already removed from `ProjectConfig` and explicitly stripped in `load_config()`

## 2026-02-18: Fix git merge failure handling in dispatcher

### What was done
- Extracted merge logic from `_execute_task` into a dedicated `_merge_to_main()` method in the `Dispatcher` class
- Added `threading.Lock` (`_merge_lock`) to serialize concurrent merge operations on the main branch
- Replaced bare `check=True` with explicit return-code handling and `capture_output=True` for diagnostic stderr
- On merge failure, the method now runs `git merge --abort` to restore main to a clean state before raising
- Added timeouts to all git subprocess calls (30s checkout, 60s merge, 10s abort)
- Added 5 unit tests covering: successful merge, checkout failure, merge conflict with abort, exit-status-2 scenario, and lock existence

### Lessons learned
- `git merge` exit status 2 typically signals a merge conflict; using `check=True` converts this to a `CalledProcessError` but leaves the repo in a half-merged state with conflict markers
- Without `git merge --abort`, the main branch stays dirty and all subsequent merges (from other tasks) will also fail, cascading the problem
- A `threading.Lock` is essential when multiple tasks can complete concurrently, since they all race to `git checkout main` + `git merge`
- Capturing stderr from git commands provides much better diagnostics in the error log than a generic `CalledProcessError` message

## 2026-02-18: Remove console display from dashboard

### What was done
- Removed the console panel (HTML container, header, clear button, output area) from `frontend/index.html`
- Removed all console CSS styles (`.console-container`, `.console-header`, `.console-clear`, `.console-output`, `.console-entry`, `.console-type` variants) from `frontend/css/style.css`
- Removed `consoleOutput` and `consoleClear` DOM refs, `renderConsole()` function, console clear event listener, and console clearing in `selectProject()` from `frontend/js/app.js`
- Removed session log summary display from the task detail panel's `renderDetail()` function
- Backend `session_log` field on `TaskDetail` model left intact — still used by the agent internally

### Lessons learned
- The console panel occupied 200px of fixed height at the bottom of the main content area; removing it gives the kanban board the full vertical space
- When removing a UI component, check for all references: DOM refs, event listeners, render functions, and any data-fetching code that populated it
- The session_log data is still produced by the agent and stored in `.log.json` files; the backend model doesn't need to change just because the frontend no longer displays it

## 2026-02-18: Hot-reload projects.yaml

### What was done
- Modified `backend/config.py` to hot-reload `config/projects.yaml` on file change
- `get_config()` now checks the file's mtime on each call and reloads when the file has been modified
- No server restart needed when adding/removing/updating projects in the YAML file
- Gracefully falls back to cached config if the file is deleted or unreadable

### Lessons learned
- The original `get_config()` cached the config once in a module-level global and never re-read the file, requiring a full server restart for any config change
- Using `stat().st_mtime` is a lightweight way to detect file changes without reading the entire file on every request
- Wrapping the stat call in `try/except OSError` handles edge cases like file deletion gracefully
- The `os.utime()` trick is needed in tests because some filesystems have coarse mtime resolution

## 2026-02-18: Fix project switch emptying conversation box

### What was done
- Fixed bug where switching projects in the sidebar would reset/empty the entire conversation box, even when the agent was still streaming a response
- Root cause: `selectProject()` called `resetChat()` unconditionally on every project switch, wiping chat history, session ID, plan state, and DOM content
- Added per-project chat state storage (`projectChatStates` map) that saves and restores conversation state when switching between projects
- `saveChatState()` captures: chat history array, session ID, plan state, messages HTML, input value, plan visibility, collapsed state, and current mode (plan/task)
- `restoreChatState()` restores all saved state, or calls `resetChat()` if no saved state exists for the project (first visit)
- `resetChat()` now also clears the saved state for the current project, so the explicit clear button works correctly
- `selectProject()` saves the outgoing project's state before switching, then restores the incoming project's state

### Lessons learned
- Per-project state storage is the correct pattern when a shared UI component (chat box) displays context-specific data — resetting on every switch destroys user work
- Saving the `innerHTML` of the messages container is simpler and more reliable than reconstructing messages from the history array, since it preserves streaming bubbles and formatted content
- The `resetChat()` function serves double duty: initializing a fresh conversation for a new project, and explicitly clearing via the clear button — adding `delete projectChatStates[selectedProjectId]` ensures the clear button properly wipes saved state

## 2026-02-18: Fix Ctrl+C not shutting down agent cleanly

### What was done
- Root cause: when Ctrl+C is pressed, `uvicorn` receives SIGINT and starts shutting down, but the dispatcher's child `claude` processes (launched via `subprocess.Popen`) block on `proc.stdout` iteration and never terminate — the agent hangs indefinitely
- This is especially problematic when Baton iterates on itself (Baton agent running in a worktree launched by Baton's own dispatcher), creating nested process trees
- Added `_active_procs` dict and `_procs_lock` to `Dispatcher` to track all running child processes (claude code instances)
- Added `start_new_session=True` to `subprocess.Popen` so child processes get their own process group — prevents the parent's SIGINT from being forwarded directly (which could leave grandchild processes orphaned)
- Added `_terminate_child_processes()` method that sends `SIGTERM` to the entire process group (`os.killpg`), with a 5s timeout and `SIGKILL` fallback
- `Dispatcher.stop()` now calls `_terminate_child_processes()` before joining the dispatch thread, unblocking the `proc.stdout` read
- Added `_stop_event` check in the stdout monitoring loop so tasks abort quickly when the dispatcher is stopping
- Added `signal.SIGTERM` handler in `main()` that calls `_dispatcher.stop()` for graceful shutdown
- Added `try/except KeyboardInterrupt` around `uvicorn.run()` that calls `_dispatcher.stop()` as a safety net

### Lessons learned
- `subprocess.Popen` without `start_new_session=True` shares the parent's process group — when the parent receives SIGINT, all children in the group also receive it, but this is unreliable for cleanup because children may handle SIGINT differently or ignore it
- `start_new_session=True` + `os.killpg()` is the robust pattern: children get their own session/process group, and the parent explicitly terminates the entire group on shutdown
- The blocking `for line in proc.stdout` pattern is the main reason Ctrl+C hangs — the thread is stuck in a read syscall that only returns when the child process exits or closes its stdout
- `ThreadPoolExecutor.shutdown(wait=False)` does NOT terminate running threads or their child processes — it only stops accepting new work. Active threads continue running until their blocking I/O completes
- When a tool (Baton) iterates on itself, nested process trees make clean shutdown critical — without explicit process group termination, orphaned grandchild processes accumulate

## 2026-02-18: Add logging to debug plan mode blank content output

### What was done
- Added comprehensive logging to `backend/chat.py` `chat_stream()` to trace the entire streaming pipeline:
  - Logs subprocess start (PID, command, session_id)
  - Logs every event type received from Claude Code (with event count)
  - Logs assistant event details: content block count, stop_reason, block types (text, tool_use, other)
  - Warns on empty text blocks in assistant events and empty content_block_deltas
  - Warns on non-JSON lines from subprocess stdout
  - Logs result events with session_id and cost
  - Logs summary on subprocess exit: returncode, total events, text chunks yielded, total text length
  - Explicit WARNING when stream completes with zero text output — the likely root cause of blank plan content
  - Logs subprocess failures with stderr content and uses `exc_info=True` for full tracebacks
- Added frontend logging in `frontend/js/app.js`:
  - `sendMessage()` streaming handler: logs on done event (response length), warns on empty response
  - `tryParsePlan()`: logs text length, plan marker detection, JSON marker position, parse attempts, success with task count, and failure with attempt count

### Lessons learned
- The chat streaming pipeline has multiple points where content can be silently dropped: empty text blocks in assistant events, non-text content_block_deltas, and non-JSON subprocess output — without logging at each point, it's impossible to diagnose which stage is producing blank output
- Using `logger.debug` for per-event logging and `logger.info`/`logger.warning` for summary-level events keeps the default INFO log readable while allowing detailed tracing with `--log-level DEBUG`
- Frontend `console.log`/`console.warn` for plan parsing is essential because the `tryParsePlan()` function has multiple early-return paths that silently discard the response — the user sees blank content but has no way to know why
- Tracking counters (event_count, text_chunks_yielded, total_text_length) through the stream provides a concise summary without needing to enable full DEBUG logging

## 2026-02-18: Add plan board to dashboard

### What was done
- The dashboard had backend plan infrastructure (agent endpoints, models, connector methods) but no UI to view saved plans — users could create plans via the chat but never see them afterward
- Fixed multiple code issues in `backend/agent.py`: removed duplicate `plans` property on `AgentDir`, removed orphaned `_create_plan()` function referencing non-existent `ReviewStatus`, removed duplicate `@app.post("/agent/plans")` endpoint
- Added `get_all_plans()` abstract method to `ProjectConnector` base class
- Implemented `get_all_plans()` in `HTTPConnector` (proxies to `GET /agent/plans`) and `LocalConnector` (returns empty dict)
- Added `GET /api/projects/{project_id}/plans` dashboard route in `server.py`
- Added plan board UI section in `frontend/index.html` between the chat section and kanban board — 5 columns (draft, ready, executing, done, failed) mirroring plan statuses
- Added plan board CSS styles in `frontend/css/style.css` with color-coded column headers matching the plan status semantics
- Added `loadPlans()` and `renderPlans()` functions in `frontend/js/app.js` with polling cache, staleness guards, and auto-hide when no plans exist
- Plan board auto-refreshes on 15-second polling interval alongside tasks/worktrees/commits
- Plan board refreshes immediately after confirming a plan via the chat
- Plan board is collapsible via a toggle button in its header

### Lessons learned
- When backend infrastructure is added incrementally across multiple tasks (models, agent endpoints, connector methods, dashboard routes), the frontend UI can easily be forgotten — the plan board had full API support but zero visibility in the dashboard
- Duplicate definitions (properties, functions, endpoints) accumulate when features are added in separate tasks without checking for conflicts — `agent.py` had two `plans` properties, two `_create_plan` functions (one broken), and two `@app.post("/agent/plans")` endpoints
- The orphaned `_create_plan` at line 258 referenced `ReviewStatus.PENDING` which was removed in an earlier task but not cleaned up — dead code referencing removed symbols is a latent import error waiting to happen
- Applying the existing polling cache pattern (`lastPlansJson`) and staleness guard pattern (`targetProjectId` check) to new data sources is straightforward because the patterns are already established in `loadTasks()` etc.

## 2026-02-18: Refactor plan mode display — plans as kanban column

### What was done
- Removed the separate plan board (5-column grid with draft/ready/executing/done/failed statuses) from the dashboard
- Added a "Plans" column as the first column in the kanban board, before Pending — plans are displayed as cards with an Execute button
- Changed kanban grid from 4 to 5 columns to accommodate the Plans column
- Plan cards show title, ID, modified date, task count, and a purple "Execute" button (play icon)
- Clicking Execute confirms with the user, then calls `POST /api/projects/{id}/plans/{plan_id}/execute` which creates pending tasks from the plan's task list and removes the plan
- Added `execute_plan` endpoint to the agent (`POST /agent/plans/{plan_id}/execute`), dashboard server, and all connectors (base, HTTP, local)
- Removed the old `POST /agent/plans/{plan_id}/start` endpoint that just changed plan status to "executing"
- Removed all plan board HTML, CSS (`.plan-board-*` classes), and JS (plan board refs, toggle, render into 5 status columns)
- Plans are flattened from all statuses into a single list — no more tracking plan lifecycle stages

### Lessons learned
- Plans as a separate board with their own lifecycle (draft/ready/executing/done/failed) added complexity without clear value — plans are fundamentally just groups of tasks, and the kanban board already tracks task lifecycle
- Adding the Plans column to the existing kanban board keeps everything in one view and makes the flow clear: Plan → Execute → tasks appear in Pending → dispatched to In Progress → Completed/Failed
- The execute endpoint needs to parse plan content (stored as JSON string with a `tasks` array) and call the existing `_create_task()` for each — reusing existing task creation avoids duplicating validation and file-writing logic
- When removing a UI component (plan board), all three layers need cleanup: HTML structure, CSS styles, and JS state/logic — missing any layer leaves dead code or broken references
