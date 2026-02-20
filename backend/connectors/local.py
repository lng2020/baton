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
from backend.models import GitLogEntry, PlanSummary, TaskDetail, TaskSummary, TaskType, TASK_TYPE_VALUES, WorktreeInfo

logger = logging.getLogger(__name__)


class LocalConnector(ProjectConnector):
    def __init__(self, config: ProjectConfig):
        self.config = config
        self.project_path = config.project_path
        self.data_path = config.project_path / "data"

    def _dev_tasks_path(self) -> Path:
        return self.data_path / "dev-tasks.json"

    def _load_dev_tasks(self) -> dict:
        path = self._dev_tasks_path()
        if not path.exists():
            return {"tasks": {}}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"tasks": {}}

    def _save_dev_tasks(self, data: dict) -> None:
        path = self._dev_tasks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def list_tasks(self, status: str) -> list[TaskSummary]:
        data = self._load_dev_tasks()
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

    def create_task(self, title: str, content: str = "", task_type: str = "feature", needs_plan_review: bool = False) -> TaskDetail:
        task_id = uuid.uuid4().hex[:8]
        tt = TaskType(task_type) if task_type in TaskType.__members__ else TaskType.feature
        now = datetime.now(timezone.utc)
        data = self._load_dev_tasks()
        data["tasks"][task_id] = {
            "id": task_id,
            "title": title,
            "content": content,
            "task_type": tt.value,
            "status": "pending",
            "created": now.isoformat(),
            "modified": now.isoformat(),
            "worker_port": None,
            "error": None,
            "needs_plan_review": needs_plan_review,
            "plan_content": None,
        }
        self._save_dev_tasks(data)
        logger.info("Task created locally: id=%s, title=%s", task_id, title)
        return TaskDetail(
            id=task_id,
            filename=f"{task_id}.md",
            status="pending",
            title=title,
            modified=now,
            content=content,
            task_type=tt,
            needs_plan_review=needs_plan_review,
        )

    def read_task(self, status: str, filename: str) -> TaskDetail | None:
        task_id = filename.replace(".md", "")
        data = self._load_dev_tasks()
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

        session_log = None
        log_path = self.data_path / f"{task_id}.log.json"
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
        return self.project_path.is_dir()

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

    async def approve_plan_review(self, task_id: str) -> dict:
        raise NotImplementedError("Plan review requires an agent connection")

    async def revise_plan_review(self, task_id: str, feedback: str = "") -> dict:
        raise NotImplementedError("Plan review requires an agent connection")

    async def reject_plan_review(self, task_id: str) -> dict:
        raise NotImplementedError("Plan review requires an agent connection")

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
