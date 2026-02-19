from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.responses import StreamingResponse

from backend.config import ProjectConfig, get_config, get_project_by_id, load_config
from backend.connectors.base import ProjectConnector
from backend.connectors.http import HTTPConnector
from backend.connectors.local import LocalConnector
from backend.github import get_pr_for_branch, get_task_branch_name
from backend.models import (
    BulkTaskCreateRequest,
    ChatRequest,
    ProjectSummary,
    TaskCreateRequest,
    TaskDetail,
)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="Baton", version="0.1.0")

app.mount("/css", StaticFiles(directory=FRONTEND_DIR / "css"), name="css")
app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="js")

templates = Jinja2Templates(directory=FRONTEND_DIR)


@app.on_event("startup")
async def startup():
    load_config()


def _make_connector(cfg: ProjectConfig) -> ProjectConnector:
    if cfg.agent_url:
        return HTTPConnector(cfg.agent_url)
    return LocalConnector(cfg)


def _get_connector(project_id: str) -> ProjectConnector:
    cfg = get_project_by_id(project_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return _make_connector(cfg)


# ---- Page routes ----

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ---- API routes ----

@app.get("/api/projects")
async def api_projects() -> list[ProjectSummary]:
    config = get_config()
    result = []
    for p in config.projects:
        conn = _make_connector(p)
        all_tasks = conn.get_all_tasks()
        counts = {status: len(tasks) for status, tasks in all_tasks.items()}
        result.append(ProjectSummary(
            id=p.id,
            name=p.name,
            description=p.description,
            color=p.color,
            task_counts=counts,
            healthy=conn.is_healthy(),
        ))
    return result


@app.get("/api/projects/{project_id}/tasks")
async def api_tasks(project_id: str):
    conn = _get_connector(project_id)
    return conn.get_all_tasks()


@app.get("/api/projects/{project_id}/tasks/{status}/{filename}")
async def api_task_detail(project_id: str, status: str, filename: str):
    conn = _get_connector(project_id)
    task = conn.read_task(status, filename)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    cfg = get_project_by_id(project_id)
    if cfg and cfg.repo:
        branch = get_task_branch_name(task.id)
        pr = get_pr_for_branch(cfg.repo, branch)
        if pr:
            task.pr = pr

    return task


@app.post("/api/projects/{project_id}/tasks")
async def api_create_task(project_id: str, body: TaskCreateRequest) -> TaskDetail:
    conn = _get_connector(project_id)
    try:
        return conn.create_task(body.title, body.content)
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/projects/{project_id}/worktrees")
async def api_worktrees(project_id: str):
    conn = _get_connector(project_id)
    return conn.get_worktrees()


@app.get("/api/projects/{project_id}/commits")
async def api_commits(project_id: str, count: int = 10):
    conn = _get_connector(project_id)
    return conn.get_recent_commits(count)


# ---- Chat routes ----

@app.post("/api/projects/{project_id}/chat")
async def api_chat(project_id: str, body: ChatRequest):
    conn = _get_connector(project_id)
    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    try:
        return StreamingResponse(
            conn.chat_stream(messages),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except (ConnectionError, NotImplementedError) as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/projects/{project_id}/chat/plan")
async def api_chat_plan(project_id: str, body: ChatRequest):
    conn = _get_connector(project_id)
    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    try:
        return await conn.chat_plan(messages)
    except (ConnectionError, NotImplementedError) as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/projects/{project_id}/tasks/bulk")
async def api_create_tasks_bulk(project_id: str, body: BulkTaskCreateRequest):
    conn = _get_connector(project_id)
    try:
        return await conn.create_tasks_bulk(
            [{"title": t.title, "content": t.content} for t in body.tasks],
        )
    except (ConnectionError, NotImplementedError) as e:
        raise HTTPException(status_code=502, detail=str(e))


