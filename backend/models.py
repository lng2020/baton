from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class TaskType(str, Enum):
    feature = "feature"
    bugfix = "bugfix"
    refactor = "refactor"
    chore = "chore"
    docs = "docs"
    test = "test"


TASK_TYPE_VALUES = [t.value for t in TaskType]


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
    task_type: TaskType = TaskType.feature


class TaskDetail(BaseModel):
    id: str
    filename: str
    status: str
    title: str
    modified: datetime
    content: str
    task_type: TaskType = TaskType.feature
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
    task_type: TaskType = TaskType.feature


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


class PlanCreateRequest(BaseModel):
    title: str
    summary: str = ""
    content: str = ""


class ProjectDetail(BaseModel):
    id: str
    name: str
    description: str
    color: str
    tasks: dict[str, list[TaskSummary]]
    worktrees: list[WorktreeInfo]
    recent_commits: list[GitLogEntry]
