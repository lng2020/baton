# Progress

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
