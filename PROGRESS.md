# Progress

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
