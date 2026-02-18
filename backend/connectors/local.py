from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from backend.config import ProjectConfig
from backend.connectors.base import ProjectConnector
from backend.models import GitLogEntry, TaskDetail, TaskSummary, WorktreeInfo


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
            ))
        return tasks

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
