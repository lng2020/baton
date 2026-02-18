from __future__ import annotations

import logging

import httpx

from backend.connectors.base import ProjectConnector
from backend.models import (
    DispatcherStatus,
    GitLogEntry,
    TaskDetail,
    TaskSummary,
    WorktreeInfo,
)

logger = logging.getLogger(__name__)


class HTTPConnector(ProjectConnector):
    """Connector that proxies all operations to a remote Baton agent over HTTP."""

    def __init__(self, agent_url: str, timeout: float = 10.0):
        self.base_url = agent_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def list_tasks(self, status: str) -> list[TaskSummary]:
        try:
            resp = self.client.get(f"/agent/tasks/{status}")
            resp.raise_for_status()
            return [TaskSummary.model_validate(t) for t in resp.json()]
        except (httpx.HTTPError, Exception) as e:
            logger.warning(f"HTTPConnector.list_tasks({status}) failed: {e}")
            return []

    def read_task(self, status: str, filename: str) -> TaskDetail | None:
        try:
            resp = self.client.get(f"/agent/tasks/{status}/{filename}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return TaskDetail.model_validate(resp.json())
        except (httpx.HTTPError, Exception) as e:
            logger.warning(f"HTTPConnector.read_task({status}, {filename}) failed: {e}")
            return None

    def create_task(self, title: str, content: str = "") -> TaskDetail:
        resp = self.client.post("/agent/tasks", json={"title": title, "content": content})
        resp.raise_for_status()
        return TaskDetail.model_validate(resp.json())

    def get_all_tasks(self) -> dict[str, list[TaskSummary]]:
        try:
            resp = self.client.get("/agent/tasks")
            resp.raise_for_status()
            data = resp.json()
            return {
                status: [TaskSummary.model_validate(t) for t in tasks]
                for status, tasks in data.items()
            }
        except (httpx.HTTPError, Exception) as e:
            logger.warning(f"HTTPConnector.get_all_tasks() failed: {e}")
            return {s: [] for s in ("pending", "in_progress", "completed", "failed")}

    def get_worktrees(self) -> list[WorktreeInfo]:
        try:
            resp = self.client.get("/agent/worktrees")
            resp.raise_for_status()
            return [WorktreeInfo.model_validate(w) for w in resp.json()]
        except (httpx.HTTPError, Exception) as e:
            logger.warning(f"HTTPConnector.get_worktrees() failed: {e}")
            return []

    def get_recent_commits(self, count: int = 10) -> list[GitLogEntry]:
        try:
            resp = self.client.get("/agent/commits", params={"count": count})
            resp.raise_for_status()
            return [GitLogEntry.model_validate(c) for c in resp.json()]
        except (httpx.HTTPError, Exception) as e:
            logger.warning(f"HTTPConnector.get_recent_commits() failed: {e}")
            return []

    def is_healthy(self) -> bool:
        try:
            resp = self.client.get("/agent/health")
            resp.raise_for_status()
            return resp.json().get("healthy", False)
        except (httpx.HTTPError, Exception):
            return False

    def get_dispatcher_status(self) -> DispatcherStatus:
        try:
            resp = self.client.get("/agent/dispatcher")
            resp.raise_for_status()
            return DispatcherStatus.model_validate(resp.json())
        except (httpx.HTTPError, Exception):
            return DispatcherStatus(status="unknown")

    def dispatcher_action(self, action: str) -> DispatcherStatus:
        """Call /agent/dispatcher/{start|stop|restart}."""
        resp = self.client.post(f"/agent/dispatcher/{action}")
        resp.raise_for_status()
        return DispatcherStatus.model_validate(resp.json())
