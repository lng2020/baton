# Progress

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
