"""
Baton Remote Agent — lightweight FastAPI server that exposes task CRUD,
git info, and dispatcher lifecycle over HTTP.

Run with:
    baton-agent --port 9100
    # or
    BATON_PROJECT_DIR=/path/to/project python -m uvicorn backend.agent:app --port 9100
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException

from backend.models import (
    DispatcherStatus,
    GitLogEntry,
    TaskCreateRequest,
    TaskDetail,
    TaskSummary,
    WorktreeInfo,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project directory resolution
# ---------------------------------------------------------------------------

def _resolve_project_dir() -> Path:
    env = os.environ.get("BATON_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


PROJECT_DIR = _resolve_project_dir()
TASKS_DIR = PROJECT_DIR / "tasks"

# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------

STATUSES = ("pending", "in_progress", "completed", "failed")


def _extract_title(filepath: Path) -> str:
    try:
        for line in filepath.open(encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
            if line:
                return line[:80]
    except OSError:
        pass
    return filepath.stem


def _list_tasks(status: str) -> list[TaskSummary]:
    status_dir = TASKS_DIR / status
    if not status_dir.is_dir():
        return []
    tasks = []
    for md_file in sorted(status_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True):
        if md_file.name == ".gitkeep":
            continue
        task_id = md_file.stem
        error_log = status_dir / f"{task_id}.error.log"
        tasks.append(TaskSummary(
            id=task_id,
            filename=md_file.name,
            status=status,
            title=_extract_title(md_file),
            modified=datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc),
            has_error_log=error_log.exists(),
        ))
    return tasks


def _read_task(status: str, filename: str) -> TaskDetail | None:
    filepath = TASKS_DIR / status / filename
    if not filepath.is_file():
        return None
    content = filepath.read_text(encoding="utf-8", errors="replace")
    task_id = filepath.stem

    error_log = None
    error_path = filepath.parent / f"{task_id}.error.log"
    if error_path.exists():
        error_log = error_path.read_text(encoding="utf-8", errors="replace")

    session_log = None
    log_path = filepath.parent / f"{task_id}.log.json"
    if log_path.exists():
        try:
            session_log = json.loads(log_path.read_text(encoding="utf-8"))
            if not isinstance(session_log, list):
                session_log = [session_log]
        except (json.JSONDecodeError, OSError):
            session_log = None

    return TaskDetail(
        id=task_id,
        filename=filename,
        status=status,
        title=_extract_title(filepath),
        modified=datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc),
        content=content,
        error_log=error_log,
        session_log=session_log,
    )


def _create_task(title: str, content: str = "") -> TaskDetail:
    task_id = uuid.uuid4().hex[:8]
    pending_dir = TASKS_DIR / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    filepath = pending_dir / f"{task_id}.md"
    body = f"# {title}\n\n{content}"
    filepath.write_text(body, encoding="utf-8")
    return TaskDetail(
        id=task_id,
        filename=filepath.name,
        status="pending",
        title=title,
        modified=datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc),
        content=body,
    )

# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _get_worktrees() -> list[WorktreeInfo]:
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=PROJECT_DIR, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    worktrees: list[WorktreeInfo] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if current:
                worktrees.append(WorktreeInfo(
                    path=current.get("worktree", ""),
                    branch=current.get("branch", "").replace("refs/heads/", ""),
                    commit=current.get("HEAD", ""),
                    is_bare=current.get("bare", "") == "bare",
                ))
                current = {}
            continue
        if line.startswith("worktree "):
            current["worktree"] = line[len("worktree "):]
        elif line.startswith("HEAD "):
            current["HEAD"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):]
        elif line == "bare":
            current["bare"] = "bare"
    if current:
        worktrees.append(WorktreeInfo(
            path=current.get("worktree", ""),
            branch=current.get("branch", "").replace("refs/heads/", ""),
            commit=current.get("HEAD", ""),
            is_bare=current.get("bare", "") == "bare",
        ))
    return worktrees


def _get_recent_commits(count: int = 10) -> list[GitLogEntry]:
    sep = "---BATON-SEP---"
    fmt = f"%H{sep}%s{sep}%an{sep}%ci{sep}%D"
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={count}", f"--format={fmt}"],
            cwd=PROJECT_DIR, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(sep)
        if len(parts) < 4:
            continue
        entries.append(GitLogEntry(
            sha=parts[0],
            message=parts[1],
            author=parts[2],
            date=parts[3],
            branch=parts[4] if len(parts) > 4 else "",
        ))
    return entries

# ---------------------------------------------------------------------------
# Dispatcher management
# ---------------------------------------------------------------------------

_dispatcher_proc: subprocess.Popen | None = None


def _dispatcher_status() -> DispatcherStatus:
    global _dispatcher_proc
    if _dispatcher_proc is None:
        return DispatcherStatus(status="stopped")
    rc = _dispatcher_proc.poll()
    if rc is None:
        return DispatcherStatus(status="running", pid=_dispatcher_proc.pid)
    if rc == 0:
        return DispatcherStatus(status="stopped")
    return DispatcherStatus(status="crashed")


def _start_dispatcher() -> DispatcherStatus:
    global _dispatcher_proc
    if _dispatcher_proc is not None and _dispatcher_proc.poll() is None:
        return DispatcherStatus(status="running", pid=_dispatcher_proc.pid)

    cmd = ["python3", "-m", "manager.task_dispatcher", "--project-dir", str(PROJECT_DIR)]
    log_path = Path(f"/tmp/baton-agent-dispatcher-{PROJECT_DIR.name}.log")
    log_file = open(log_path, "a")
    _dispatcher_proc = subprocess.Popen(
        cmd, cwd=PROJECT_DIR, stdout=log_file, stderr=log_file,
        preexec_fn=os.setsid,
    )
    logger.info(f"Started dispatcher (pid={_dispatcher_proc.pid}, log={log_path})")
    return DispatcherStatus(status="running", pid=_dispatcher_proc.pid)


def _stop_dispatcher() -> DispatcherStatus:
    global _dispatcher_proc
    if _dispatcher_proc is None or _dispatcher_proc.poll() is not None:
        _dispatcher_proc = None
        return DispatcherStatus(status="stopped")
    try:
        os.killpg(os.getpgid(_dispatcher_proc.pid), signal.SIGTERM)
        _dispatcher_proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(_dispatcher_proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    logger.info("Stopped dispatcher")
    _dispatcher_proc = None
    return DispatcherStatus(status="stopped")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Baton Agent", version="0.1.0")


@app.get("/agent/health")
async def health():
    return {"healthy": TASKS_DIR.is_dir()}


@app.get("/agent/tasks")
async def all_tasks() -> dict[str, list[TaskSummary]]:
    return {status: _list_tasks(status) for status in STATUSES}


@app.get("/agent/tasks/{status}")
async def tasks_by_status(status: str) -> list[TaskSummary]:
    if status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    return _list_tasks(status)


@app.get("/agent/tasks/{status}/{filename}")
async def task_detail(status: str, filename: str) -> TaskDetail:
    task = _read_task(status, filename)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/agent/tasks")
async def create_task(body: TaskCreateRequest) -> TaskDetail:
    return _create_task(body.title, body.content)


@app.get("/agent/worktrees")
async def worktrees() -> list[WorktreeInfo]:
    return _get_worktrees()


@app.get("/agent/commits")
async def commits(count: int = 10) -> list[GitLogEntry]:
    return _get_recent_commits(count)


@app.get("/agent/dispatcher")
async def dispatcher_status() -> DispatcherStatus:
    return _dispatcher_status()


@app.post("/agent/dispatcher/start")
async def dispatcher_start() -> DispatcherStatus:
    return _start_dispatcher()


@app.post("/agent/dispatcher/stop")
async def dispatcher_stop() -> DispatcherStatus:
    return _stop_dispatcher()


@app.post("/agent/dispatcher/restart")
async def dispatcher_restart() -> DispatcherStatus:
    _stop_dispatcher()
    return _start_dispatcher()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Baton Remote Agent")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9100, help="Bind port (default: 9100)")
    parser.add_argument("--project-dir", default=None, help="Project root (default: BATON_PROJECT_DIR or cwd)")
    args = parser.parse_args()

    if args.project_dir:
        global PROJECT_DIR, TASKS_DIR
        PROJECT_DIR = Path(args.project_dir).resolve()
        TASKS_DIR = PROJECT_DIR / "tasks"

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
