# Progress

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
