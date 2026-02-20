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
    def create_task(self, title: str, content: str = "", task_type: str = "feature", needs_plan_review: bool = False) -> TaskDetail:
        ...

    def get_all_tasks(self) -> dict[str, list[TaskSummary]]:
        result: dict[str, list[TaskSummary]] = {}
        for status in ("pending", "plan_review", "in_progress", "completed", "failed"):
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

    @abstractmethod
    async def execute_plan(self, plan_id: str) -> dict:
        ...

    @abstractmethod
    async def upload_image(self, file_data: bytes, filename: str) -> dict:
        ...

    @abstractmethod
    async def approve_plan_review(self, task_id: str) -> dict:
        ...

    @abstractmethod
    async def revise_plan_review(self, task_id: str, feedback: str = "") -> dict:
        ...

    @abstractmethod
    async def reject_plan_review(self, task_id: str) -> dict:
        ...
