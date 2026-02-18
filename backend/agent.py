"""
Baton Agent — single source of truth for project task management.

Provides HTTP API for task CRUD, git info, and runs the task dispatcher
internally as a background thread (no subprocess). Includes Claude Code
launching, log monitoring, and plan review.

Run with:
    baton-agent --port 9100
    BATON_PROJECT_DIR=/path/to/project baton-agent --port 9100
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import yaml
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
WORKTREES_DIR = PROJECT_DIR / "worktrees"

# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------

@dataclass
class ClaudeCodeConfig:
    skip_permissions: bool = True
    output_format: str = "stream-json"
    verbose: bool = True
    timeout: int = 600


@dataclass
class AgentConfig:
    max_parallel_workers: int = 5
    poll_interval_seconds: int = 10
    claude_code: ClaudeCodeConfig = field(default_factory=ClaudeCodeConfig)


def _load_agent_config() -> AgentConfig:
    for candidate in [PROJECT_DIR / "agent.yaml", PROJECT_DIR / "config.yaml"]:
        if candidate.exists():
            with open(candidate) as f:
                raw = yaml.safe_load(f) or {}
            cc_raw = raw.get("claude_code", {})
            return AgentConfig(
                max_parallel_workers=raw.get("max_parallel_workers", 5),
                poll_interval_seconds=raw.get("poll_interval_seconds", 10),
                claude_code=ClaudeCodeConfig(
                    skip_permissions=cc_raw.get("skip_permissions", True),
                    output_format=cc_raw.get("output_format", "stream-json"),
                    verbose=cc_raw.get("verbose", True),
                    timeout=cc_raw.get("timeout", 600),
                ),
            )
    return AgentConfig()


AGENT_CONFIG = _load_agent_config()

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
# Claude Code launcher
# ---------------------------------------------------------------------------

def _build_cc_command(prompt: str, cc_config: ClaudeCodeConfig) -> list[str]:
    cmd = ["claude", "-p", prompt, "--output-format", cc_config.output_format]
    if cc_config.verbose:
        cmd.append("--verbose")
    if cc_config.skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    return cmd


# ---------------------------------------------------------------------------
# Log monitor
# ---------------------------------------------------------------------------

@dataclass
class TaskLog:
    task_id: str
    events: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    tool_uses: list = field(default_factory=list)
    assistant_messages: list = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def summary(self) -> dict:
        return {
            "task_id": self.task_id,
            "total_events": len(self.events),
            "errors": len(self.errors),
            "tool_uses": len(self.tool_uses),
            "messages": len(self.assistant_messages),
        }


def _parse_log_event(event: dict, task_log: TaskLog):
    task_log.events.append(event)
    event_type = event.get("type", "")
    if event_type == "error":
        task_log.errors.append(event)
        logger.error(f"[{task_log.task_id}] Error: {event.get('error', {})}")
    elif event_type == "assistant":
        task_log.assistant_messages.append(event.get("message", ""))
    elif event_type == "tool_use":
        tool_name = event.get("tool", "")
        task_log.tool_uses.append({"tool": tool_name, "input": event.get("input", {})})
    elif event_type == "result":
        logger.info(f"[{task_log.task_id}] Result: cost=${event.get('cost_usd', 0):.4f}")


def _save_task_log(task_log: TaskLog, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / f"{task_log.task_id}.log.json"
    with open(log_file, "w") as f:
        json.dump({"summary": task_log.summary, "events": task_log.events},
                  f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plan review
# ---------------------------------------------------------------------------

class ReviewStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"


@dataclass
class Plan:
    task_id: str
    content: str
    status: ReviewStatus = ReviewStatus.PENDING
    reviewer_notes: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "content": self.content,
            "status": self.status.value,
            "reviewer_notes": self.reviewer_notes,
        }


class PlanReviewQueue:
    def __init__(self, plans_dir: Path):
        self.plans_dir = plans_dir
        self.plans_dir.mkdir(parents=True, exist_ok=True)

    def add_plan(self, plan: Plan):
        plan_file = self.plans_dir / f"{plan.task_id}.plan.json"
        with open(plan_file, "w") as f:
            json.dump(plan.to_dict(), f, indent=2, ensure_ascii=False)

    def get_pending_plans(self) -> list[Plan]:
        plans = []
        for plan_file in self.plans_dir.glob("*.plan.json"):
            with open(plan_file) as f:
                data = json.load(f)
            if data["status"] == ReviewStatus.PENDING.value:
                plans.append(Plan(
                    task_id=data["task_id"], content=data["content"],
                    status=ReviewStatus(data["status"]),
                    reviewer_notes=data.get("reviewer_notes", ""),
                ))
        return plans

    def review_plan(self, task_id: str, status: ReviewStatus, notes: str = "") -> bool:
        plan_file = self.plans_dir / f"{task_id}.plan.json"
        if not plan_file.exists():
            return False
        with open(plan_file) as f:
            data = json.load(f)
        data["status"] = status.value
        data["reviewer_notes"] = notes
        with open(plan_file, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True

    def get_approved_plans(self) -> list[Plan]:
        plans = []
        for plan_file in self.plans_dir.glob("*.plan.json"):
            with open(plan_file) as f:
                data = json.load(f)
            if data["status"] == ReviewStatus.APPROVED.value:
                plans.append(Plan(
                    task_id=data["task_id"], content=data["content"],
                    status=ReviewStatus.APPROVED,
                    reviewer_notes=data.get("reviewer_notes", ""),
                ))
        return plans

    def remove_plan(self, task_id: str):
        plan_file = self.plans_dir / f"{task_id}.plan.json"
        if plan_file.exists():
            plan_file.unlink()


# ---------------------------------------------------------------------------
# Dispatcher — runs as a background thread inside the agent
# ---------------------------------------------------------------------------

class Dispatcher:
    def __init__(self, config: AgentConfig):
        self.config = config
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active_tasks: dict[str, any] = {}
        self._executor: ThreadPoolExecutor | None = None

    @property
    def status(self) -> str:
        if self._thread is not None and self._thread.is_alive():
            return "running"
        return "stopped"

    def start(self) -> DispatcherStatus:
        if self._thread is not None and self._thread.is_alive():
            return DispatcherStatus(status="running")
        self._stop_event.clear()
        self._executor = ThreadPoolExecutor(max_workers=self.config.max_parallel_workers)
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(f"Dispatcher started (max_workers={self.config.max_parallel_workers})")
        return DispatcherStatus(status="running")

    def stop(self) -> DispatcherStatus:
        if self._thread is None or not self._thread.is_alive():
            return DispatcherStatus(status="stopped")
        self._stop_event.set()
        self._thread.join(timeout=10)
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None
        self._thread = None
        logger.info("Dispatcher stopped")
        return DispatcherStatus(status="stopped")

    def restart(self) -> DispatcherStatus:
        self.stop()
        return self.start()

    def get_status(self) -> DispatcherStatus:
        return DispatcherStatus(status=self.status)

    # -- dispatch loop --

    def _run_loop(self):
        logger.info(f"Dispatch loop running (project={PROJECT_DIR})")
        while not self._stop_event.is_set():
            # Clean up completed futures
            done = [tid for tid, f in self._active_tasks.items() if f.done()]
            for tid in done:
                future = self._active_tasks.pop(tid)
                exc = future.exception()
                if exc:
                    logger.error(f"Task {tid} raised exception: {exc}")

            # Fill with new tasks
            available_slots = self.config.max_parallel_workers - len(self._active_tasks)
            if available_slots > 0:
                pending = self._get_pending_tasks()[:available_slots]
                for task_file in pending:
                    task_id = task_file.stem
                    if task_id not in self._active_tasks:
                        future = self._executor.submit(self._execute_task, task_file)
                        self._active_tasks[task_id] = future

            self._stop_event.wait(timeout=self.config.poll_interval_seconds)

    def _get_pending_tasks(self) -> list[Path]:
        pending_dir = TASKS_DIR / "pending"
        if not pending_dir.is_dir():
            return []
        return sorted(
            [f for f in pending_dir.glob("*.md") if f.name != ".gitkeep"],
            key=lambda f: f.stat().st_mtime,
        )

    def _execute_task(self, task_file: Path):
        task_id = task_file.stem
        logger.info(f"Executing task: {task_id}")

        # Move to in_progress
        in_progress_dir = TASKS_DIR / "in_progress"
        in_progress_dir.mkdir(parents=True, exist_ok=True)
        in_progress_path = in_progress_dir / task_file.name
        task_file.rename(in_progress_path)

        task_log = TaskLog(task_id=task_id)

        try:
            # Create worktree
            worktree_path = self._create_worktree(task_id)

            # Read task content and build prompt
            task_content = in_progress_path.read_text(encoding="utf-8")
            prompt = (
                f"Please execute the following task:\n\n{task_content}\n\n"
                f"When complete:\n"
                f"1. Run tests to ensure they pass\n"
                f"2. Commit code, commit message format: feat({task_id}): [description]\n"
                f"3. Update PROGRESS.md with lessons learned\n"
                f"4. Exit\n"
            )

            # Launch Claude Code
            cmd = _build_cc_command(prompt, self.config.claude_code)
            proc = subprocess.Popen(
                cmd, cwd=str(worktree_path),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )

            # Monitor output
            for line in proc.stdout:
                try:
                    event = json.loads(line.decode().strip())
                    _parse_log_event(event, task_log)
                except json.JSONDecodeError:
                    pass

            proc.wait(timeout=self.config.claude_code.timeout)

            if proc.returncode == 0:
                # Merge to main
                subprocess.run(["git", "checkout", "main"], cwd=str(PROJECT_DIR), check=True)
                subprocess.run(
                    ["git", "merge", f"task/{task_id}", "--no-ff"],
                    cwd=str(PROJECT_DIR), check=True,
                )
                completed_dir = TASKS_DIR / "completed"
                completed_dir.mkdir(parents=True, exist_ok=True)
                in_progress_path.rename(completed_dir / task_file.name)
                logger.info(f"Task completed: {task_id}")
            else:
                raise Exception(f"Claude Code exit code: {proc.returncode}")

        except Exception as e:
            logger.error(f"Task failed: {task_id} — {e}")
            failed_dir = TASKS_DIR / "failed"
            failed_dir.mkdir(parents=True, exist_ok=True)
            in_progress_path.rename(failed_dir / task_file.name)
            (failed_dir / f"{task_id}.error.log").write_text(str(e))

        finally:
            # Save session log
            status_dir = TASKS_DIR / "completed" if (TASKS_DIR / "completed" / task_file.name).exists() else TASKS_DIR / "failed"
            _save_task_log(task_log, status_dir)
            # Cleanup worktree
            self._cleanup_worktree(task_id)

    def _create_worktree(self, task_id: str) -> Path:
        branch = f"task/{task_id}"
        worktree_path = WORKTREES_DIR / task_id
        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree_path), "main"],
            cwd=str(PROJECT_DIR), check=True,
        )
        return worktree_path

    def _cleanup_worktree(self, task_id: str):
        worktree_path = WORKTREES_DIR / task_id
        subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            cwd=str(PROJECT_DIR), capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", f"task/{task_id}"],
            cwd=str(PROJECT_DIR), capture_output=True,
        )


_dispatcher = Dispatcher(AGENT_CONFIG)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Baton Agent", version="0.1.0")


@app.on_event("shutdown")
async def shutdown():
    _dispatcher.stop()


@app.get("/agent/health")
async def health():
    return {"healthy": TASKS_DIR.is_dir()}


# -- Tasks --

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


# -- Git --

@app.get("/agent/worktrees")
async def worktrees() -> list[WorktreeInfo]:
    return _get_worktrees()


@app.get("/agent/commits")
async def commits(count: int = 10) -> list[GitLogEntry]:
    return _get_recent_commits(count)


# -- Dispatcher --

@app.get("/agent/dispatcher")
async def dispatcher_status() -> DispatcherStatus:
    return _dispatcher.get_status()


@app.post("/agent/dispatcher/start")
async def dispatcher_start() -> DispatcherStatus:
    return _dispatcher.start()


@app.post("/agent/dispatcher/stop")
async def dispatcher_stop() -> DispatcherStatus:
    return _dispatcher.stop()


@app.post("/agent/dispatcher/restart")
async def dispatcher_restart() -> DispatcherStatus:
    return _dispatcher.restart()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Baton Agent")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9100, help="Bind port (default: 9100)")
    parser.add_argument("--project-dir", default=None, help="Project root (default: BATON_PROJECT_DIR or cwd)")
    args = parser.parse_args()

    if args.project_dir:
        global PROJECT_DIR, TASKS_DIR, WORKTREES_DIR, AGENT_CONFIG, _dispatcher
        PROJECT_DIR = Path(args.project_dir).resolve()
        TASKS_DIR = PROJECT_DIR / "tasks"
        WORKTREES_DIR = PROJECT_DIR / "worktrees"
        AGENT_CONFIG = _load_agent_config()
        _dispatcher = Dispatcher(AGENT_CONFIG)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
