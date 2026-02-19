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
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from starlette.responses import StreamingResponse

from backend.chat import build_system_prompt, chat_stream
from backend.models import (
    BulkTaskCreateRequest,
    ChatRequest,
    DispatcherStatus,
    GitLogEntry,
    PlanCreateRequest,
    PlanDetail,
    PlanStatus,
    PlanSummary,
    TaskCreateRequest,
    TaskDetail,
    TaskSummary,
    TaskType,
    WorktreeInfo,
)

from backend.logging_config import setup_logging

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
    def plans(self) -> Path:
        return self.root / "plans"

    def tasks_status(self, status: str) -> Path:
        return self.tasks / status

    def plans_status(self, status: str) -> Path:
        return self.plans / status

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
            )
    return AgentConfig()


AGENT_CONFIG = _load_agent_config(agent_dir.root)

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


def _extract_task_type(filepath: Path) -> TaskType:
    """Extract task type from a metadata line like ``type: bugfix`` in the task file."""
    try:
        for line in filepath.open(encoding="utf-8", errors="replace"):
            stripped = line.strip()
            if stripped.lower().startswith("type:"):
                value = stripped.split(":", 1)[1].strip().lower()
                try:
                    return TaskType(value)
                except ValueError:
                    return TaskType.feature
    except OSError:
        pass
    return TaskType.feature


def _list_tasks(status: str) -> list[TaskSummary]:
    status_dir = agent_dir.tasks_status(status)
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
            task_type=_extract_task_type(md_file),
        ))
    return tasks


def _read_task(status: str, filename: str) -> TaskDetail | None:
    filepath = agent_dir.tasks_status(status) / filename
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
        task_type=_extract_task_type(filepath),
        error_log=error_log,
        session_log=session_log,
    )


def _create_task(title: str, content: str = "", task_type: TaskType = TaskType.feature) -> TaskDetail:
    task_id = uuid.uuid4().hex[:8]
    pending_dir = agent_dir.tasks_status("pending")
    pending_dir.mkdir(parents=True, exist_ok=True)
    filepath = pending_dir / f"{task_id}.md"
    body = f"# {title}\n\ntype: {task_type.value}\n\n{content}"
    filepath.write_text(body, encoding="utf-8")
    return TaskDetail(
        id=task_id,
        filename=filepath.name,
        status="pending",
        title=title,
        modified=datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc),
        content=body,
        task_type=task_type,
    )

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


def _save_task_log(task_log: TaskLog, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / f"{task_log.task_id}.log.json"
    with open(log_file, "w") as f:
        json.dump({"summary": task_log.summary, "events": task_log.events},
                  f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plan helpers
# ---------------------------------------------------------------------------

PLAN_STATUSES = ("draft", "ready", "executing", "done", "failed")


def _list_plans(status: str | None = None) -> list[PlanSummary]:
    statuses = [status] if status else list(PLAN_STATUSES)
    plans: list[PlanSummary] = []
    for s in statuses:
        status_dir = agent_dir.plans_status(s)
        if not status_dir.is_dir():
            continue
        for plan_file in sorted(status_dir.glob("*.plan.json"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                data = json.loads(plan_file.read_text(encoding="utf-8"))
                plans.append(PlanSummary(
                    id=data["id"],
                    title=data["title"],
                    summary=data.get("summary", ""),
                    status=PlanStatus(data["status"]),
                    created=datetime.fromisoformat(data["created"]),
                    modified=datetime.fromisoformat(data["modified"]),
                    task_count=len(data.get("tasks", [])),
                ))
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.warning(f"Skipping invalid plan file {plan_file}: {e}")
    return plans


def _read_plan(status: str, filename: str) -> PlanDetail | None:
    filepath = agent_dir.plans_status(status) / filename
    if not filepath.is_file():
        return None
    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
        return PlanDetail(
            id=data["id"],
            title=data["title"],
            summary=data.get("summary", ""),
            status=PlanStatus(data["status"]),
            created=datetime.fromisoformat(data["created"]),
            modified=datetime.fromisoformat(data["modified"]),
            task_count=len(data.get("tasks", [])),
            content=data.get("content", ""),
            tasks=data.get("tasks", []),
            error=data.get("error"),
        )
    except (json.JSONDecodeError, KeyError, OSError) as e:
        logger.warning(f"Failed to read plan {filepath}: {e}")
        return None


def _create_plan(title: str, summary: str, content: str) -> PlanDetail:
    plan_id = uuid.uuid4().hex[:8]
    draft_dir = agent_dir.plans_status("draft")
    draft_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    data = {
        "id": plan_id,
        "title": title,
        "summary": summary,
        "content": content,
        "status": "draft",
        "created": now.isoformat(),
        "modified": now.isoformat(),
        "tasks": [],
        "error": None,
    }
    filepath = draft_dir / f"{plan_id}.plan.json"
    filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return PlanDetail(
        id=plan_id,
        title=title,
        summary=summary,
        status=PlanStatus.draft,
        created=now,
        modified=now,
        content=content,
        tasks=[],
        error=None,
    )


def _update_plan_status(plan_id: str, new_status: str, error: str | None = None) -> PlanDetail | None:
    # Find the plan in any status directory
    for s in PLAN_STATUSES:
        filepath = agent_dir.plans_status(s) / f"{plan_id}.plan.json"
        if filepath.is_file():
            data = json.loads(filepath.read_text(encoding="utf-8"))
            data["status"] = new_status
            data["modified"] = datetime.now(timezone.utc).isoformat()
            if error is not None:
                data["error"] = error
            # Move to new status directory
            new_dir = agent_dir.plans_status(new_status)
            new_dir.mkdir(parents=True, exist_ok=True)
            new_path = new_dir / f"{plan_id}.plan.json"
            new_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            if filepath != new_path:
                filepath.unlink()
            return _read_plan(new_status, f"{plan_id}.plan.json")
    return None


def _link_tasks_to_plan(plan_id: str, task_ids: list[str]) -> PlanDetail | None:
    # Find the plan in any status directory
    for s in PLAN_STATUSES:
        filepath = agent_dir.plans_status(s) / f"{plan_id}.plan.json"
        if filepath.is_file():
            data = json.loads(filepath.read_text(encoding="utf-8"))
            existing = data.get("tasks", [])
            existing.extend(tid for tid in task_ids if tid not in existing)
            data["tasks"] = existing
            data["modified"] = datetime.now(timezone.utc).isoformat()
            filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            return _read_plan(s, f"{plan_id}.plan.json")
    return None


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
        self._thread.join(timeout=10)
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None
        self._thread = None
        logger.info("Dispatcher stopped")
        return DispatcherStatus(status="stopped")

    def _terminate_child_processes(self):
        """Terminate all tracked child processes (claude code instances).

        Processes are started in their own session (start_new_session=True)
        so we send SIGTERM to the entire process group to ensure all
        grandchild processes are also cleaned up.
        """
        with self._procs_lock:
            for task_id, proc in list(self._active_procs.items()):
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
                for task_file in pending:
                    task_id = task_file.stem
                    if task_id not in self._active_tasks:
                        future = self._executor.submit(self._execute_task, task_file)
                        self._active_tasks[task_id] = future

            self._stop_event.wait(timeout=self.config.poll_interval_seconds)

    def _get_pending_tasks(self) -> list[Path]:
        pending_dir = agent_dir.tasks_status("pending")
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
        in_progress_dir = agent_dir.tasks_status("in_progress")
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
                start_new_session=True,
            )

            # Track the process so stop() can terminate it
            with self._procs_lock:
                self._active_procs[task_id] = proc

            # Monitor output
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

            if proc.returncode == 0:
                self._merge_to_main(task_id)
                completed_dir = agent_dir.tasks_status("completed")
                completed_dir.mkdir(parents=True, exist_ok=True)
                in_progress_path.rename(completed_dir / task_file.name)
                logger.info(f"Task completed: {task_id}")
            else:
                raise Exception(f"Claude Code exit code: {proc.returncode}")

        except Exception as e:
            logger.error(f"Task failed: {task_id} — {e}")
            failed_dir = agent_dir.tasks_status("failed")
            failed_dir.mkdir(parents=True, exist_ok=True)
            in_progress_path.rename(failed_dir / task_file.name)
            (failed_dir / f"{task_id}.error.log").write_text(str(e))

        finally:
            # Untrack the child process
            with self._procs_lock:
                self._active_procs.pop(task_id, None)
            # Save session log
            status_dir = agent_dir.tasks_status("completed") if (agent_dir.tasks_status("completed") / task_file.name).exists() else agent_dir.tasks_status("failed")
            _save_task_log(task_log, status_dir)
            # Cleanup worktree
            self._cleanup_worktree(task_id)

    def _merge_to_main(self, task_id: str) -> None:
        """Merge a task branch into main with conflict recovery.

        Uses the git lock to prevent concurrent git operations from racing
        on the root repo.  If the merge fails (e.g. conflict), the merge is
        aborted so that main is left in a clean state for subsequent merges.
        """
        branch = f"task/{task_id}"
        with self._git_lock:
            checkout = subprocess.run(
                ["git", "checkout", "main"],
                cwd=str(agent_dir.root), capture_output=True, text=True, timeout=30,
            )
            if checkout.returncode != 0:
                raise Exception(
                    f"git checkout main failed (rc={checkout.returncode}): "
                    f"{checkout.stderr.strip()}"
                )

            merge = subprocess.run(
                ["git", "merge", branch, "--no-ff"],
                cwd=str(agent_dir.root), capture_output=True, text=True, timeout=60,
            )
            if merge.returncode != 0:
                logger.warning(f"Merge of {branch} failed, aborting merge to restore main")
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=str(agent_dir.root), capture_output=True, timeout=10,
                )
                raise Exception(
                    f"git merge {branch} failed (rc={merge.returncode}): "
                    f"{merge.stderr.strip() or merge.stdout.strip()}"
                )

    def _create_worktree(self, task_id: str) -> Path:
        """Create a git worktree for the task, serialized via git lock.

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
        # Copy shared config files into the worktree
        for name in ("CLAUDE.md", "PROGRESS.md"):
            src = agent_dir.root / name
            if src.exists():
                shutil.copy2(str(src), str(worktree_path / name))
        return worktree_path

    def _cleanup_worktree(self, task_id: str):
        """Remove a git worktree and its branch, serialized via git lock."""
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
    plan_counts = {s: len(_list_plans(s)) for s in PLAN_STATUSES}
    return {"healthy": agent_dir.tasks.is_dir(), "plan_counts": plan_counts}


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
    return _create_task(body.title, body.content, body.task_type)


# -- Plans --

@app.get("/agent/plans")
async def all_plans() -> dict[str, list[PlanSummary]]:
    return {status: _list_plans(status) for status in PLAN_STATUSES}


@app.get("/agent/plans/{status}/{filename}")
async def plan_detail(status: str, filename: str) -> PlanDetail:
    if status not in PLAN_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid plan status: {status}")
    plan = _read_plan(status, filename)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


@app.post("/agent/plans")
async def create_plan_endpoint(body: PlanCreateRequest) -> PlanDetail:
    return _create_plan(body.title, body.summary, body.content)


@app.post("/agent/plans/{plan_id}/start")
async def start_plan(plan_id: str) -> PlanDetail:
    plan = _update_plan_status(plan_id, "executing")
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


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

    setup_logging(level=args.log_level, project_dir=agent_dir.root)

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
