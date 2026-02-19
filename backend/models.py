from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class PRInfo(BaseModel):
    number: int
    title: str
    url: str
    state: str
    branch: str


class TaskSummary(BaseModel):
    id: str
    filename: str
    status: str
    title: str
    modified: datetime
    has_error_log: bool = False


class TaskDetail(BaseModel):
    id: str
    filename: str
    status: str
    title: str
    modified: datetime
    content: str
    error_log: str | None = None
    session_log: list[dict] | None = None
    pr: PRInfo | None = None


class WorktreeInfo(BaseModel):
    path: str
    branch: str
    commit: str
    is_bare: bool = False


class GitLogEntry(BaseModel):
    sha: str
    message: str
    author: str
    date: str
    branch: str = ""


class DispatcherStatus(BaseModel):
    status: str
    pid: int | None = None


class ProjectSummary(BaseModel):
    id: str
    name: str
    description: str
    color: str
    task_counts: dict[str, int]
    healthy: bool


class TaskCreateRequest(BaseModel):
    title: str
    content: str = ""


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    session_id: str | None = None


class ChatPlanTask(BaseModel):
    title: str
    content: str


class ChatPlan(BaseModel):
    tasks: list[ChatPlanTask]
    summary: str


class BulkTaskCreateRequest(BaseModel):
    tasks: list[TaskCreateRequest]


class ProjectDetail(BaseModel):
    id: str
    name: str
    description: str
    color: str
    tasks: dict[str, list[TaskSummary]]
    worktrees: list[WorktreeInfo]
    recent_commits: list[GitLogEntry]
