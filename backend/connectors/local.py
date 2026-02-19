from __future__ import annotations

import json
import logging
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

from backend.config import ProjectConfig
from backend.connectors.base import ProjectConnector
from backend.models import GitLogEntry, PlanSummary, TaskDetail, TaskSummary, TaskType, WorktreeInfo

logger = logging.getLogger(__name__)


class LocalConnector(ProjectConnector):
    def __init__(self, config: ProjectConfig):
        self.config = config
        self.project_path = config.project_path
        self.tasks_path = config.tasks_path

    def list_tasks(self, status: str) -> list[TaskSummary]:
        status_dir = self.tasks_path / status
        if not status_dir.is_dir():
            return []
        tasks = []
        for md_file in sorted(status_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True):
            if md_file.name == ".gitkeep":
                continue
            title = self._extract_title(md_file)
            task_id = md_file.stem
            error_log = status_dir / f"{task_id}.error.log"
            tasks.append(TaskSummary(
                id=task_id,
                filename=md_file.name,
                status=status,
                title=title,
                modified=datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc),
                has_error_log=error_log.exists(),
                task_type=self._extract_task_type(md_file),
            ))
        return tasks

    def create_task(self, title: str, content: str = "", task_type: str = "feature") -> TaskDetail:
        task_id = uuid.uuid4().hex[:8]
        pending_dir = self.tasks_path / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        filepath = pending_dir / f"{task_id}.md"
        tt = TaskType(task_type) if task_type in TaskType.__members__ else TaskType.feature
        body = f"# {title}\n\ntype: {tt.value}\n\n{content}"
        filepath.write_text(body, encoding="utf-8")
        logger.info("Task created locally: id=%s, title=%s", task_id, title)
        return TaskDetail(
            id=task_id,
            filename=filepath.name,
            status="pending",
            title=title,
            modified=datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc),
            content=body,
            task_type=tt,
        )

    def read_task(self, status: str, filename: str) -> TaskDetail | None:
        filepath = self.tasks_path / status / filename
        if not filepath.is_file():
            return None
        content = filepath.read_text(encoding="utf-8", errors="replace")
        task_id = filepath.stem
        title = self._extract_title(filepath)

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
            title=title,
            modified=datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc),
            content=content,
            task_type=self._extract_task_type(filepath),
            error_log=error_log,
            session_log=session_log,
        )

    def get_worktrees(self) -> list[WorktreeInfo]:
        if not self.project_path.is_dir():
            return []
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=self.project_path,
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
        if result.returncode != 0:
            return []
        return self._parse_worktrees(result.stdout)

    def get_recent_commits(self, count: int = 10) -> list[GitLogEntry]:
        if not self.project_path.is_dir():
            return []
        sep = "---BATON-SEP---"
        fmt = f"%H{sep}%s{sep}%an{sep}%ci{sep}%D"
        try:
            result = subprocess.run(
                ["git", "log", f"--max-count={count}", f"--format={fmt}"],
                cwd=self.project_path,
                capture_output=True, text=True, timeout=10,
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

    def is_healthy(self) -> bool:
        return self.project_path.is_dir() and self.tasks_path.is_dir()

    async def chat_stream(self, messages: list[dict], session_id: str | None = None):
        raise NotImplementedError("Chat requires an agent connection")

    async def chat_plan(self, messages: list[dict]) -> dict:
        raise NotImplementedError("Chat requires an agent connection")

    async def create_tasks_bulk(self, tasks: list[dict]) -> list:
        return [self.create_task(t["title"], t.get("content", ""), t.get("task_type", "feature")) for t in tasks]

    def get_all_plans(self) -> dict[str, list[PlanSummary]]:
        return {}

    async def create_plan(self, title: str, summary: str, content: str) -> dict:
        plan_id = uuid.uuid4().hex[:8]
        plans_dir = self.project_path / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan_data = {
            "task_id": plan_id,
            "title": title,
            "summary": summary,
            "content": content,
            "status": "pending",
            "reviewer_notes": "",
        }
        plan_file = plans_dir / f"{plan_id}.plan.json"
        with open(plan_file, "w") as f:
            json.dump(plan_data, f, indent=2, ensure_ascii=False)
        return plan_data

    async def execute_plan(self, plan_id: str) -> dict:
        plans_dir = self.project_path / "plans"
        plan_file = plans_dir / f"{plan_id}.plan.json"
        if not plan_file.is_file():
            raise ConnectionError("Plan not found")
        plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
        try:
            plan_content = json.loads(plan_data.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            raise ConnectionError("Plan content is not valid JSON")
        tasks_data = plan_content.get("tasks", [])
        created = []
        for t in tasks_data:
            task = self.create_task(t.get("title", "Untitled"), t.get("content", ""))
            created.append(task.model_dump(mode="json"))
        plan_file.unlink(missing_ok=True)
        return {"created_tasks": created}

    async def upload_image(self, file_data: bytes, filename: str) -> dict:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ALLOWED_IMAGE_TYPES:
            raise ConnectionError(f"File type '{ext}' not allowed")
        if len(file_data) > MAX_UPLOAD_SIZE:
            raise ConnectionError(f"File too large ({len(file_data)} bytes)")
        uploads_dir = self.project_path / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        uuid_name = f"{uuid.uuid4().hex}.{ext}"
        save_path = uploads_dir / uuid_name
        save_path.write_bytes(file_data)
        return {
            "filename": uuid_name,
            "url": f"/uploads/{uuid_name}",
            "original_name": filename,
        }

    @staticmethod
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

    @staticmethod
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

    @staticmethod
    def _parse_worktrees(output: str) -> list[WorktreeInfo]:
        worktrees = []
        current: dict[str, str] = {}
        for line in output.splitlines():
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
