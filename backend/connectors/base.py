from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from backend.models import GitLogEntry, PlanSummary, TaskDetail, TaskSummary, WorktreeInfo


class ProjectConnector(ABC):
    @abstractmethod
    def list_tasks(self, status: str) -> list[TaskSummary]:
        ...

    @abstractmethod
    def read_task(self, status: str, filename: str) -> TaskDetail | None:
        ...

    @abstractmethod
    def create_task(self, title: str, content: str = "", task_type: str = "feature") -> TaskDetail:
        ...

    def get_all_tasks(self) -> dict[str, list[TaskSummary]]:
        result: dict[str, list[TaskSummary]] = {}
        for status in ("pending", "in_progress", "completed", "failed"):
            result[status] = self.list_tasks(status)
        return result

    @abstractmethod
    def get_worktrees(self) -> list[WorktreeInfo]:
        ...

    @abstractmethod
    def get_recent_commits(self, count: int = 10) -> list[GitLogEntry]:
        ...

    @abstractmethod
    def is_healthy(self) -> bool:
        ...

    @abstractmethod
    async def chat_stream(self, messages: list[dict], session_id: str | None = None) -> AsyncIterator[bytes]:
        ...

    @abstractmethod
    async def chat_plan(self, messages: list[dict]) -> dict:
        ...

    @abstractmethod
    async def create_tasks_bulk(self, tasks: list[dict]) -> list:
        ...

    @abstractmethod
    def get_all_plans(self) -> dict[str, list[PlanSummary]]:
        ...

    @abstractmethod
    async def create_plan(self, title: str, summary: str, content: str) -> dict:
        ...
