from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from backend.connectors.base import ProjectConnector
from backend.models import (
    GitLogEntry,
    PlanSummary,
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
        self._async_client = httpx.AsyncClient(base_url=self.base_url, timeout=120.0)

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

    def create_task(self, title: str, content: str = "", task_type: str = "feature") -> TaskDetail:
        try:
            resp = self.client.post("/agent/tasks", json={"title": title, "content": content, "task_type": task_type})
            resp.raise_for_status()
            return TaskDetail.model_validate(resp.json())
        except httpx.ConnectError:
            raise ConnectionError(f"Agent unreachable at {self.base_url}")
        except httpx.HTTPStatusError as e:
            raise ConnectionError(f"Agent returned {e.response.status_code}")

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
        try:
            resp = self.client.post(f"/agent/dispatcher/{action}")
            resp.raise_for_status()
            return DispatcherStatus.model_validate(resp.json())
        except (httpx.HTTPError, Exception) as e:
            logger.warning(f"HTTPConnector.dispatcher_action({action}) failed: {e}")
            return DispatcherStatus(status="unknown")

    async def chat_stream(self, messages: list[dict], session_id: str | None = None) -> AsyncIterator[bytes]:
        """Stream SSE response from agent chat endpoint."""
        payload = {"messages": messages}
        if session_id:
            payload["session_id"] = session_id
        async with self._async_client.stream(
            "POST",
            "/agent/chat",
            json=payload,
            timeout=120.0,
        ) as response:
            async for line in response.aiter_lines():
                yield (line + "\n").encode()

    async def chat_plan(self, messages: list[dict]) -> dict:
        """Get structured plan from agent."""
        resp = await self._async_client.post(
            "/agent/chat/plan",
            json={"messages": messages},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def create_tasks_bulk(self, tasks: list[dict]) -> list:
        """Create multiple tasks at once."""
        resp = await self._async_client.post(
            "/agent/tasks/bulk",
            json={"tasks": tasks},
        )
        resp.raise_for_status()
        return resp.json()

    def get_all_plans(self) -> dict[str, list[PlanSummary]]:
        try:
            resp = self.client.get("/agent/plans")
            resp.raise_for_status()
            data = resp.json()
            return {
                status: [PlanSummary.model_validate(p) for p in plans]
                for status, plans in data.items()
            }
        except (httpx.HTTPError, Exception) as e:
            logger.warning(f"HTTPConnector.get_all_plans() failed: {e}")
            return {}

    async def create_plan(self, title: str, summary: str, content: str) -> dict:
        """Save a plan via the agent."""
        try:
            resp = await self._async_client.post(
                "/agent/plans",
                json={"title": title, "summary": summary, "content": content},
            )
        except httpx.HTTPError as e:
            raise ConnectionError(f"Agent unreachable: {e}")
        if resp.status_code != 200:
            detail = resp.text[:200] if resp.text else f"HTTP {resp.status_code}"
            raise ConnectionError(f"Agent returned {resp.status_code}: {detail}")
        return resp.json()

    async def execute_plan(self, plan_id: str) -> dict:
        """Execute a plan via the agent — creates tasks from plan content."""
        try:
            resp = await self._async_client.post(f"/agent/plans/{plan_id}/execute")
        except httpx.HTTPError as e:
            raise ConnectionError(f"Agent unreachable: {e}")
        if resp.status_code == 404:
            raise ConnectionError("Plan not found")
        if resp.status_code != 200:
            detail = resp.text[:200] if resp.text else f"HTTP {resp.status_code}"
            raise ConnectionError(f"Agent returned {resp.status_code}: {detail}")
        return resp.json()
