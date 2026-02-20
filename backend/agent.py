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
import signal
import shutil
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from backend.chat import build_system_prompt, chat_stream
from backend.models import (
    BulkTaskCreateRequest,
    ChatRequest,
    DispatcherStatus,
    GitLogEntry,
    PlanReviewRequest,
    TaskCreateRequest,
    TaskDetail,
    TaskSummary,
    TASK_TYPE_VALUES,
    TaskType,
    WorktreeInfo,
)

from backend.logging_config import create_task_handler, setup_logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project directory — single entry point for all project paths
# ---------------------------------------------------------------------------

@dataclass
class AgentDir:
    """Single entry point for accessing project directories."""
    root: Path

    @property
    def tasks(self) -> Path:
        return self.root / "tasks"

    @property
    def worktrees(self) -> Path:
        return self.root / "worktrees"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def uploads(self) -> Path:
        return self.root / "uploads"

    def tasks_status(self, status: str) -> Path:
        return self.tasks / status

    @classmethod
    def resolve(cls, path: str | Path | None = None) -> AgentDir:
        if path is not None:
            return cls(root=Path(path).resolve())
        env = os.environ.get("BATON_PROJECT_DIR")
        if env:
            return cls(root=Path(env).resolve())
        return cls(root=Path.cwd().resolve())


agent_dir = AgentDir.resolve()

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
class ChatConfig:
    model: str = "claude-sonnet-4-5-20250929"
    max_tokens: int = 4096


@dataclass
class AgentConfig:
    max_parallel_workers: int = 5
    poll_interval_seconds: int = 10
    claude_code: ClaudeCodeConfig = field(default_factory=ClaudeCodeConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    port_range_start: int = 9200
    port_range_end: int = 9299
    test_command: str = "pytest"
    push_to_remote: bool = True
    symlink_files: list[str] = field(default_factory=list)
    copy_files: list[str] = field(default_factory=lambda: ["CLAUDE.md", "PROGRESS.md"])
    max_merge_retries: int = 3


def _load_agent_config(project_dir: Path) -> AgentConfig:
    for candidate in [project_dir / "agent.yaml", project_dir / "config.yaml"]:
        if candidate.exists():
            with open(candidate) as f:
                raw = yaml.safe_load(f) or {}
            cc_raw = raw.get("claude_code", {})
            chat_raw = raw.get("chat", {})
            return AgentConfig(
                max_parallel_workers=raw.get("max_parallel_workers", 5),
                poll_interval_seconds=raw.get("poll_interval_seconds", 10),
                claude_code=ClaudeCodeConfig(
                    skip_permissions=cc_raw.get("skip_permissions", True),
                    output_format=cc_raw.get("output_format", "stream-json"),
                    verbose=cc_raw.get("verbose", True),
                    timeout=cc_raw.get("timeout", 600),
                ),
                chat=ChatConfig(
                    model=chat_raw.get("model", "claude-sonnet-4-5-20250929"),
                    max_tokens=chat_raw.get("max_tokens", 4096),
                ),
                port_range_start=raw.get("port_range_start", 9200),
                port_range_end=raw.get("port_range_end", 9299),
                test_command=raw.get("test_command", "pytest"),
                push_to_remote=raw.get("push_to_remote", True),
                symlink_files=raw.get("symlink_files", []),
                copy_files=raw.get("copy_files", ["CLAUDE.md", "PROGRESS.md"]),
                max_merge_retries=raw.get("max_merge_retries", 3),
            )
    return AgentConfig()


AGENT_CONFIG = _load_agent_config(agent_dir.root)

# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------

STATUSES = ("pending", "plan_review", "in_progress", "completed", "failed")



def _list_tasks(status: str) -> list[TaskSummary]:
    """List tasks from dev-tasks.json (single source of truth)."""
    data = _load_dev_tasks()
    tasks = []
    for task_id, t in data.get("tasks", {}).items():
        if t.get("status") != status:
            continue
        task_type = t.get("task_type", "feature")
        if task_type not in TASK_TYPE_VALUES:
            task_type = "feature"
        modified_str = t.get("modified") or t.get("created", "")
        try:
            modified = datetime.fromisoformat(modified_str)
        except (ValueError, TypeError):
            modified = datetime.now(timezone.utc)
        tasks.append(TaskSummary(
            id=task_id,
            filename=f"{task_id}.md",
            status=status,
            title=t.get("title", task_id),
            modified=modified,
            has_error_log=bool(t.get("error")),
            task_type=task_type,
            needs_plan_review=t.get("needs_plan_review", False),
            has_plan=bool(t.get("plan_content")),
        ))
    tasks.sort(key=lambda x: x.modified, reverse=True)
    return tasks


def _read_task(status: str, filename: str) -> TaskDetail | None:
    """Read task detail from dev-tasks.json (single source of truth).

    Session logs are stored as separate files in data/ due to their size.
    """
    task_id = filename.replace(".md", "")
    data = _load_dev_tasks()
    t = data.get("tasks", {}).get(task_id)
    if t is None or t.get("status") != status:
        return None

    task_type = t.get("task_type", "feature")
    if task_type not in TASK_TYPE_VALUES:
        task_type = "feature"
    modified_str = t.get("modified") or t.get("created", "")
    try:
        modified = datetime.fromisoformat(modified_str)
    except (ValueError, TypeError):
        modified = datetime.now(timezone.utc)

    # Session logs are large — stored as files in data/
    session_log = None
    log_path = agent_dir.data / f"{task_id}.log.json"
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
        title=t.get("title", task_id),
        modified=modified,
        content=t.get("content", ""),
        task_type=task_type,
        needs_plan_review=t.get("needs_plan_review", False),
        plan_content=t.get("plan_content"),
        error_log=t.get("error"),
        session_log=session_log,
    )


def _create_task(
    title: str,
    content: str = "",
    task_type: TaskType = TaskType.feature,
    needs_plan_review: bool = False,
) -> TaskDetail:
    task_id = uuid.uuid4().hex[:8]
    _add_task_to_json(task_id, title, content, task_type.value, needs_plan_review=needs_plan_review)
    now = datetime.now(timezone.utc)
    return TaskDetail(
        id=task_id,
        filename=f"{task_id}.md",
        status="pending",
        title=title,
        modified=now,
        content=content,
        task_type=task_type,
        needs_plan_review=needs_plan_review,
    )

# ---------------------------------------------------------------------------
# Centralized JSON task tracking (data/dev-tasks.json)
# ---------------------------------------------------------------------------

_dev_tasks_lock = threading.Lock()


def _dev_tasks_path() -> Path:
    return agent_dir.data / "dev-tasks.json"


def _load_dev_tasks() -> dict:
    """Load the dev-tasks.json file, returning empty structure if missing."""
    path = _dev_tasks_path()
    if not path.exists():
        return {"tasks": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"tasks": {}}


def _save_dev_tasks(data: dict) -> None:
    """Write dev-tasks.json atomically (write to temp, then rename)."""
    path = _dev_tasks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _add_task_to_json(
    task_id: str, title: str, content: str, task_type: str, needs_plan_review: bool = False,
) -> None:
    """Add a new task entry to dev-tasks.json."""
    now = datetime.now(timezone.utc).isoformat()
    with _dev_tasks_lock:
        data = _load_dev_tasks()
        data["tasks"][task_id] = {
            "id": task_id,
            "title": title,
            "content": content,
            "task_type": task_type,
            "status": "pending",
            "created": now,
            "modified": now,
            "worker_port": None,
            "error": None,
            "needs_plan_review": needs_plan_review,
            "plan_content": None,
        }
        _save_dev_tasks(data)


def _claim_task_json(task_id: str, port: int | None = None) -> dict | None:
    """Atomically claim a task by setting it to in_progress in JSON."""
    now = datetime.now(timezone.utc).isoformat()
    with _dev_tasks_lock:
        data = _load_dev_tasks()
        task = data["tasks"].get(task_id)
        if task is None or task["status"] != "pending":
            return None
        task["status"] = "in_progress"
        task["modified"] = now
        task["worker_port"] = port
        _save_dev_tasks(data)
        return task


def _mark_task_complete_json(task_id: str) -> None:
    """Mark a task as completed in dev-tasks.json."""
    now = datetime.now(timezone.utc).isoformat()
    with _dev_tasks_lock:
        data = _load_dev_tasks()
        task = data["tasks"].get(task_id)
        if task:
            task["status"] = "completed"
            task["modified"] = now
            task["worker_port"] = None
            _save_dev_tasks(data)


def _mark_task_failed_json(task_id: str, error: str) -> None:
    """Mark a task as failed in dev-tasks.json."""
    now = datetime.now(timezone.utc).isoformat()
    with _dev_tasks_lock:
        data = _load_dev_tasks()
        task = data["tasks"].get(task_id)
        if task:
            task["status"] = "failed"
            task["modified"] = now
            task["worker_port"] = None
            task["error"] = error
            _save_dev_tasks(data)


def _mark_task_plan_review_json(task_id: str, plan_content: str | None = None) -> None:
    """Mark a task as plan_review in dev-tasks.json, storing the plan content."""
    now = datetime.now(timezone.utc).isoformat()
    with _dev_tasks_lock:
        data = _load_dev_tasks()
        task = data["tasks"].get(task_id)
        if task:
            task["status"] = "plan_review"
            task["modified"] = now
            task["worker_port"] = None
            if plan_content is not None:
                task["plan_content"] = plan_content
            _save_dev_tasks(data)


def _mark_task_pending_json(task_id: str) -> None:
    """Mark a task as pending in dev-tasks.json (used after plan approve/revise)."""
    now = datetime.now(timezone.utc).isoformat()
    with _dev_tasks_lock:
        data = _load_dev_tasks()
        task = data["tasks"].get(task_id)
        if task:
            task["status"] = "pending"
            task["modified"] = now
            task["worker_port"] = None
            _save_dev_tasks(data)



# ---------------------------------------------------------------------------
# Port allocator
# ---------------------------------------------------------------------------

class PortAllocator:
    """Allocates unique ports for task workers from a configured range."""

    def __init__(self, start: int, end: int):
        self._range = set(range(start, end + 1))
        self._in_use: set[int] = set()
        self._lock = threading.Lock()

    def allocate(self) -> int:
        with self._lock:
            available = self._range - self._in_use
            if not available:
                raise RuntimeError("No ports available")
            port = min(available)
            self._in_use.add(port)
            return port

    def release(self, port: int) -> None:
        with self._lock:
            self._in_use.discard(port)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _get_worktrees() -> list[WorktreeInfo]:
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=agent_dir.root, capture_output=True, text=True, timeout=10,
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
            cwd=agent_dir.root, capture_output=True, text=True, timeout=10,
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


def _save_task_log(task_log: TaskLog):
    """Save session log to data/ directory."""
    data_dir = agent_dir.data
    data_dir.mkdir(parents=True, exist_ok=True)
    log_file = data_dir / f"{task_log.task_id}.log.json"
    with open(log_file, "w") as f:
        json.dump({"summary": task_log.summary, "events": task_log.events},
                  f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Dispatcher — runs as a background thread inside the agent
# ---------------------------------------------------------------------------

class Dispatcher:
    def __init__(self, config: AgentConfig):
        self.config = config
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active_tasks: dict[str, any] = {}
        self._active_procs: dict[str, subprocess.Popen] = {}
        self._procs_lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._git_lock = threading.Lock()
        self._port_allocator = PortAllocator(config.port_range_start, config.port_range_end)

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
        # Terminate all active child processes so blocking stdout reads unblock
        self._terminate_child_processes()
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self._thread.join(timeout=10)
        self._thread = None
        logger.info("Dispatcher stopped")
        return DispatcherStatus(status="stopped")

    def _terminate_child_processes(self):
        """Terminate all tracked child processes (claude code instances).

        Processes are started in their own session (start_new_session=True)
        so we send SIGTERM to the entire process group to ensure all
        grandchild processes are also cleaned up.

        The procs lock is released before waiting on each process to avoid
        blocking task threads that need the lock to clean up.
        """
        # Snapshot under lock, then release so task threads can untrack
        with self._procs_lock:
            procs = list(self._active_procs.items())

        for task_id, proc in procs:
            try:
                if proc.poll() is None:
                    logger.info(f"Terminating child process for task {task_id} (pid={proc.pid})")
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except OSError:
                        proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.warning(f"Force killing child process for task {task_id} (pid={proc.pid})")
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except OSError:
                            proc.kill()
                        proc.wait(timeout=3)
            except OSError as e:
                logger.warning(f"Error terminating process for task {task_id}: {e}")

    def restart(self) -> DispatcherStatus:
        self.stop()
        return self.start()

    def get_status(self) -> DispatcherStatus:
        return DispatcherStatus(status=self.status)

    # -- dispatch loop --

    def _run_loop(self):
        logger.info(f"Dispatch loop running (project={agent_dir.root})")
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
                for task_id in pending:
                    if task_id not in self._active_tasks:
                        future = self._executor.submit(self._execute_task, task_id)
                        self._active_tasks[task_id] = future

            self._stop_event.wait(timeout=self.config.poll_interval_seconds)

    def _get_pending_tasks(self) -> list[str]:
        """Return list of pending task IDs from dev-tasks.json."""
        data = _load_dev_tasks()
        pending = []
        for task_id, t in data.get("tasks", {}).items():
            if t.get("status") == "pending":
                created = t.get("created", "")
                pending.append((task_id, created))
        pending.sort(key=lambda x: x[1])
        return [tid for tid, _ in pending]

    def _execute_task(self, task_id: str):
        """Route task to plan phase or full execution based on JSON state."""
        data = _load_dev_tasks()
        t = data.get("tasks", {}).get(task_id, {})
        needs_plan = t.get("needs_plan_review", False)
        has_plan = bool(t.get("plan_content"))
        if needs_plan and not has_plan:
            self._execute_plan_phase(task_id)
        else:
            self._execute_full(task_id)

    def _execute_plan_phase(self, task_id: str):
        """Run CC in read-only plan mode — no worktree, no port.

        All state is tracked in dev-tasks.json (single source of truth).
        """
        task_handler = create_task_handler(task_id, project_dir=agent_dir.root)
        logging.getLogger().addHandler(task_handler)
        logger.info(f"Planning task: {task_id}")
        task_log = TaskLog(task_id=task_id)

        try:
            # Claim task in JSON
            _claim_task_json(task_id)

            # Read task content from JSON
            data = _load_dev_tasks()
            t = data.get("tasks", {}).get(task_id, {})
            task_content = t.get("content", "")

            prompt = (
                f"Analyze the following task and create a detailed implementation plan. "
                f"Do NOT modify any files. Only output your analysis and plan.\n\n"
                f"# {t.get('title', '')}\n\n{task_content}"
            )

            cmd = _build_cc_command(prompt, self.config.claude_code)
            proc = subprocess.Popen(
                cmd, cwd=str(agent_dir.root),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )

            with self._procs_lock:
                self._active_procs[task_id] = proc

            for line in proc.stdout:
                if self._stop_event.is_set():
                    break
                try:
                    event = json.loads(line.decode().strip())
                    _parse_log_event(event, task_log)
                except json.JSONDecodeError:
                    pass

            proc.wait(timeout=self.config.claude_code.timeout)

            if self._stop_event.is_set():
                raise Exception("Dispatcher stopped — task aborted")

            if proc.returncode != 0:
                raise Exception(f"Claude Code plan phase exit code: {proc.returncode}")

            # Extract plan text from assistant content blocks and result
            plan_parts = []
            for event in task_log.events:
                if event.get("type") == "assistant":
                    msg = event.get("message", "")
                    if isinstance(msg, str) and msg.strip():
                        plan_parts.append(msg)
                    elif isinstance(msg, dict):
                        for block in msg.get("content", []):
                            if isinstance(block, dict) and block.get("type") == "text":
                                plan_parts.append(block["text"])
                elif event.get("type") == "result":
                    result_text = event.get("result", "")
                    if isinstance(result_text, str) and result_text.strip():
                        plan_parts.append(result_text)

            plan_text = "\n\n".join(plan_parts) if plan_parts else "No plan generated."

            # Store plan content in JSON and move to plan_review
            _mark_task_plan_review_json(task_id, plan_content=plan_text)
            logger.info(f"Task plan ready for review: {task_id}")

        except Exception as e:
            logger.error(f"Plan phase failed: {task_id} — {e}")
            _mark_task_failed_json(task_id, str(e))

        finally:
            with self._procs_lock:
                self._active_procs.pop(task_id, None)
            _save_task_log(task_log)
            logging.getLogger().removeHandler(task_handler)
            task_handler.close()

    def _execute_full(self, task_id: str):
        """Full task execution — worktree, CC, merge+test+push, cleanup.

        All state is tracked in dev-tasks.json (single source of truth).
        """
        task_handler = create_task_handler(task_id, project_dir=agent_dir.root)
        logging.getLogger().addHandler(task_handler)
        logger.info(f"Executing task: {task_id}")

        port = self._port_allocator.allocate()
        task_log = TaskLog(task_id=task_id)

        try:
            # Step 1: Claim task (atomic JSON update)
            _claim_task_json(task_id, port)

            # Read task content from JSON
            data = _load_dev_tasks()
            t = data.get("tasks", {}).get(task_id, {})
            task_content = t.get("content", "")
            task_title = t.get("title", "")
            plan_content = t.get("plan_content")

            # Step 2: Create workspace (worktree with isolated data + symlinks)
            worktree_path = self._create_worktree(task_id)

            # Step 3: Implement feature (Claude Code)
            plan_context = ""
            if plan_content:
                plan_context = (
                    f"\n\n## Implementation Plan (approved)\n\n"
                    f"Follow this plan:\n\n{plan_content}\n\n"
                )

            prompt = (
                f"Please execute the following task:\n\n"
                f"# {task_title}\n\n{task_content}{plan_context}\n\n"
                f"When complete:\n"
                f"1. Run tests to ensure they pass\n"
                f"2. Commit code, commit message format: feat({task_id}): [description]\n"
                f"3. Update PROGRESS.md with lessons learned\n"
                f"4. Exit\n"
            )

            cmd = _build_cc_command(prompt, self.config.claude_code)
            env = os.environ.copy()
            env["TASK_PORT"] = str(port)
            proc = subprocess.Popen(
                cmd, cwd=str(worktree_path),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
            )

            with self._procs_lock:
                self._active_procs[task_id] = proc

            for line in proc.stdout:
                if self._stop_event.is_set():
                    break
                try:
                    event = json.loads(line.decode().strip())
                    _parse_log_event(event, task_log)
                except json.JSONDecodeError:
                    pass

            proc.wait(timeout=self.config.claude_code.timeout)

            if self._stop_event.is_set():
                raise Exception("Dispatcher stopped — task aborted")

            if proc.returncode != 0:
                raise Exception(f"Claude Code exit code: {proc.returncode}")

            # Steps 5-6: Merge + Test + Rebase merge (with retry)
            self._merge_test_push(task_id, worktree_path)

            # Step 7: Mark complete in JSON (crash-safe)
            _mark_task_complete_json(task_id)
            logger.info(f"Task completed: {task_id}")

        except Exception as e:
            logger.error(f"Task failed: {task_id} — {e}")
            _mark_task_failed_json(task_id, str(e))

        finally:
            with self._procs_lock:
                self._active_procs.pop(task_id, None)
            _save_task_log(task_log)
            # Cleanup worktree + remote branch (skip during shutdown)
            if not self._stop_event.is_set():
                self._cleanup_worktree(task_id)
            self._port_allocator.release(port)
            logging.getLogger().removeHandler(task_handler)
            task_handler.close()

    def _abort_merge(self) -> None:
        """Abort an in-progress merge, falling back to hard reset if needed.

        Must be called while holding ``_git_lock``.
        """
        root = str(agent_dir.root)
        abort = subprocess.run(
            ["git", "merge", "--abort"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        if abort.returncode != 0:
            logger.warning(
                f"git merge --abort failed (rc={abort.returncode}): "
                f"{abort.stderr.strip()}; falling back to git reset --hard"
            )
            subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=root, capture_output=True, timeout=10,
            )

        # Verify the merge state is fully cleared
        merge_head = Path(agent_dir.root) / ".git" / "MERGE_HEAD"
        if merge_head.exists():
            logger.error("MERGE_HEAD still present after abort — removing manually")
            merge_head.unlink(missing_ok=True)

    def _merge_test_push(self, task_id: str, worktree_path: Path) -> None:
        """Merge origin/main into task branch, run tests, rebase-merge to main, and push.

        Implements the robust merge+test+rebase lifecycle with retry logic.
        Steps:
          5. Fetch origin/main → merge into task branch → run tests
          6. Rebase task branch onto origin/main → fast-forward merge to main → push
        Retries from step 5 if rebase/merge/push fails.
        """
        branch = f"task/{task_id}"
        root = str(agent_dir.root)
        max_retries = self.config.max_merge_retries

        for attempt in range(max_retries):
            # Step 5: Merge + Test (in worktree)
            # Git operations must be serialized across workers because worktrees
            # share the same .git object database and ref store.  Without the
            # lock, a concurrent Step 6 (checkout/merge/push in root) can race
            # with the fetch+merge here and cause spurious failures (rc=1).
            with self._git_lock:
                subprocess.run(
                    ["git", "fetch", "origin"],
                    cwd=str(worktree_path), capture_output=True, text=True, timeout=60,
                )

                merge_result = subprocess.run(
                    ["git", "merge", "origin/main"],
                    cwd=str(worktree_path), capture_output=True, text=True, timeout=60,
                )
                if merge_result.returncode != 0:
                    raise Exception(
                        f"Cannot merge origin/main into {branch} "
                        f"(rc={merge_result.returncode}): {merge_result.stderr.strip()}"
                    )

            # Run tests (outside git lock — tests can run in parallel)
            if self.config.test_command:
                test_result = subprocess.run(
                    self.config.test_command.split(),
                    cwd=str(worktree_path), capture_output=True, text=True, timeout=300,
                )
                if test_result.returncode != 0:
                    raise Exception(
                        f"Tests failed in {branch} (rc={test_result.returncode}): "
                        f"{test_result.stderr.strip() or test_result.stdout.strip()}"
                    )

            # Step 6: Rebase merge to main (under git lock)
            with self._git_lock:
                # Fetch latest main in root repo
                subprocess.run(
                    ["git", "fetch", "origin", "main"],
                    cwd=root, capture_output=True, text=True, timeout=60,
                )

                # Rebase task branch onto origin/main
                rebase = subprocess.run(
                    ["git", "rebase", "origin/main"],
                    cwd=str(worktree_path), capture_output=True, text=True, timeout=60,
                )
                if rebase.returncode != 0:
                    subprocess.run(
                        ["git", "rebase", "--abort"],
                        cwd=str(worktree_path), capture_output=True, timeout=10,
                    )
                    if attempt < max_retries - 1:
                        logger.warning(f"Rebase failed for {branch} (attempt {attempt + 1}), retrying")
                        continue
                    raise Exception(f"Rebase failed after {max_retries} attempts for {branch}")

                # Safety: abort any lingering merge state from a previous crash
                merge_head = Path(agent_dir.root) / ".git" / "MERGE_HEAD"
                if merge_head.exists():
                    logger.warning("Stale MERGE_HEAD detected — aborting leftover merge")
                    self._abort_merge()

                # Checkout main in root
                checkout = subprocess.run(
                    ["git", "checkout", "main"],
                    cwd=root, capture_output=True, text=True, timeout=30,
                )
                if checkout.returncode != 0:
                    if attempt < max_retries - 1:
                        logger.warning(f"Checkout main failed (attempt {attempt + 1}), retrying")
                        continue
                    raise Exception(
                        f"git checkout main failed (rc={checkout.returncode}): "
                        f"{checkout.stderr.strip()}"
                    )

                # Fast-forward merge task branch into main
                merge = subprocess.run(
                    ["git", "merge", branch],
                    cwd=root, capture_output=True, text=True, timeout=60,
                )
                if merge.returncode != 0:
                    self._abort_merge()
                    if attempt < max_retries - 1:
                        logger.warning(f"Merge to main failed (attempt {attempt + 1}), retrying")
                        continue
                    raise Exception(
                        f"git merge {branch} failed (rc={merge.returncode}): "
                        f"{merge.stderr.strip() or merge.stdout.strip()}"
                    )

                # Push to remote
                if self.config.push_to_remote:
                    push = subprocess.run(
                        ["git", "push", "origin", "main"],
                        cwd=root, capture_output=True, text=True, timeout=60,
                    )
                    if push.returncode != 0:
                        if attempt < max_retries - 1:
                            logger.warning(f"Push failed (attempt {attempt + 1}), retrying")
                            continue
                        raise Exception(
                            f"git push origin main failed (rc={push.returncode}): "
                            f"{push.stderr.strip()}"
                        )

                break  # Success

    def _create_worktree(self, task_id: str) -> Path:
        """Create a git worktree for the task with isolated data and symlinks.

        Worktree creation must be serialized with merges and other worktree
        operations because ``git worktree add`` reads the current HEAD of
        the base ref (main) and concurrent merges/checkouts in the root repo
        can cause races.
        """
        branch = f"task/{task_id}"
        worktree_path = agent_dir.worktrees / task_id
        agent_dir.worktrees.mkdir(parents=True, exist_ok=True)
        with self._git_lock:
            result = subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(worktree_path), "main"],
                cwd=str(agent_dir.root), capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise Exception(
                    f"git worktree add failed for {task_id} (rc={result.returncode}): "
                    f"{result.stderr.strip()}"
                )

        # Create isolated data/ directory in worktree
        (worktree_path / "data").mkdir(parents=True, exist_ok=True)

        # Symlink shared files (e.g. data/dev-tasks.json → shared across worktrees)
        for rel_path in self.config.symlink_files:
            src = agent_dir.root / rel_path
            dst = worktree_path / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists() and not dst.exists():
                dst.symlink_to(src)

        # Symlink logs/ directory so worktree processes share central logs
        logs_src = agent_dir.logs
        logs_dst = worktree_path / "logs"
        logs_src.mkdir(parents=True, exist_ok=True)
        if not logs_dst.exists():
            logs_dst.symlink_to(logs_src)

        # Copy files (isolated per worktree, e.g. CLAUDE.md, PROGRESS.md)
        for name in self.config.copy_files:
            src = agent_dir.root / name
            if src.exists():
                dst = worktree_path / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))

        return worktree_path

    def _cleanup_worktree(self, task_id: str):
        """Remove a git worktree, its branch, and the remote branch."""
        worktree_path = agent_dir.worktrees / task_id
        with self._git_lock:
            subprocess.run(
                ["git", "worktree", "remove", str(worktree_path), "--force"],
                cwd=str(agent_dir.root), capture_output=True, timeout=30,
            )
            subprocess.run(
                ["git", "branch", "-D", f"task/{task_id}"],
                cwd=str(agent_dir.root), capture_output=True, timeout=30,
            )
            # Delete remote branch
            if self.config.push_to_remote:
                subprocess.run(
                    ["git", "push", "origin", "--delete", f"task/{task_id}"],
                    cwd=str(agent_dir.root), capture_output=True, timeout=30,
                )


_dispatcher = Dispatcher(AGENT_CONFIG)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

ALLOWED_IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

app = FastAPI(title="Baton Agent", version="0.1.0")

# Mount uploads directory for static file serving
_uploads_dir = agent_dir.uploads
_uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(_uploads_dir)), name="uploads")


@app.on_event("shutdown")
async def shutdown():
    _dispatcher.stop()


@app.get("/agent/health")
async def health():
    return {"healthy": agent_dir.data.is_dir()}


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
    return _create_task(body.title, body.content, body.task_type, body.needs_plan_review)


# -- Plan Review --

@app.post("/agent/tasks/{task_id}/approve-plan")
async def approve_plan(task_id: str):
    """Approve a plan: move task from plan_review to pending (plan_content stays in JSON)."""
    data = _load_dev_tasks()
    t = data.get("tasks", {}).get(task_id)
    if not t or t.get("status") != "plan_review":
        raise HTTPException(status_code=404, detail="Task not found in plan_review")
    _mark_task_pending_json(task_id)
    return {"status": "approved", "task_id": task_id}


@app.post("/agent/tasks/{task_id}/revise-plan")
async def revise_plan(task_id: str, body: PlanReviewRequest):
    """Revise a plan: append feedback to content, clear plan, move to pending for re-planning."""
    data = _load_dev_tasks()
    t = data.get("tasks", {}).get(task_id)
    if not t or t.get("status") != "plan_review":
        raise HTTPException(status_code=404, detail="Task not found in plan_review")
    with _dev_tasks_lock:
        data = _load_dev_tasks()
        task = data["tasks"].get(task_id)
        if task:
            if body.feedback.strip():
                task["content"] = task.get("content", "") + f"\n\n## Revision Feedback\n\n{body.feedback}\n"
            task["plan_content"] = None
            task["status"] = "pending"
            task["modified"] = datetime.now(timezone.utc).isoformat()
            task["worker_port"] = None
            _save_dev_tasks(data)
    return {"status": "revised", "task_id": task_id}


@app.post("/agent/tasks/{task_id}/reject-plan")
async def reject_plan(task_id: str):
    """Reject a plan: mark task as failed."""
    data = _load_dev_tasks()
    t = data.get("tasks", {}).get(task_id)
    if not t or t.get("status") != "plan_review":
        raise HTTPException(status_code=404, detail="Task not found in plan_review")
    _mark_task_failed_json(task_id, "Plan rejected by user")
    return {"status": "rejected", "task_id": task_id}


@app.post("/agent/tasks/{task_id}/rerun")
async def rerun_task(task_id: str):
    """Rerun a failed task: clear error, reset to pending so dispatcher picks it up."""
    data = _load_dev_tasks()
    t = data.get("tasks", {}).get(task_id)
    if not t or t.get("status") != "failed":
        raise HTTPException(status_code=404, detail="Task not found in failed")
    now = datetime.now(timezone.utc).isoformat()
    with _dev_tasks_lock:
        data = _load_dev_tasks()
        task = data["tasks"].get(task_id)
        if task:
            task["status"] = "pending"
            task["modified"] = now
            task["worker_port"] = None
            task["error"] = None
            _save_dev_tasks(data)
    return {"status": "requeued", "task_id": task_id}


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


# -- Chat --

@app.post("/agent/chat")
async def agent_chat(body: ChatRequest):
    """Stream a chat response from the agent engineer."""
    system = build_system_prompt(agent_dir.root.name)
    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    return StreamingResponse(
        chat_stream(
            messages=messages,
            system=system,
            session_id=body.session_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/agent/tasks/bulk")
async def create_tasks_bulk(body: BulkTaskCreateRequest) -> list[TaskDetail]:
    """Create multiple tasks at once (used after plan confirmation)."""
    return [_create_task(t.title, t.content, t.task_type) for t in body.tasks]


# -- Upload --

@app.post("/agent/upload")
async def upload_image(file: UploadFile = File(...)):
    """Accept an image upload, validate type and size, save to uploads/."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext}' not allowed. Allowed: {', '.join(sorted(ALLOWED_IMAGE_TYPES))}",
        )

    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({len(data)} bytes). Maximum: {MAX_UPLOAD_SIZE} bytes",
        )

    uuid_name = f"{uuid.uuid4().hex}.{ext}"
    save_path = agent_dir.uploads / uuid_name
    save_path.write_bytes(data)

    return {
        "filename": uuid_name,
        "url": f"/uploads/{uuid_name}",
        "original_name": file.filename,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Baton Agent")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9100, help="Bind port (default: 9100)")
    parser.add_argument("--project-dir", default=None, help="Project root (default: BATON_PROJECT_DIR or cwd)")
    parser.add_argument("--log-level", default="INFO", help="Log level (default: INFO)")
    args = parser.parse_args()

    if args.project_dir:
        global agent_dir, AGENT_CONFIG, _dispatcher
        agent_dir = AgentDir.resolve(args.project_dir)
        AGENT_CONFIG = _load_agent_config(agent_dir.root)
        _dispatcher = Dispatcher(AGENT_CONFIG)
        # Remount uploads directory for the new project dir
        uploads_dir = agent_dir.uploads
        uploads_dir.mkdir(parents=True, exist_ok=True)
        # Replace the existing /uploads mount
        app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != "/uploads"]
        app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

    setup_logging(level=args.log_level, project_dir=agent_dir.root)

    # Auto-start the dispatcher
    _dispatcher.start()

    def _shutdown_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down dispatcher...")
        _dispatcher.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown_handler)

    import uvicorn
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down...")
        _dispatcher.stop()


if __name__ == "__main__":
    main()
