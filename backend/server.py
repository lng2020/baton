from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from backend.config import ProjectConfig, get_config, get_project_by_id, load_config
from backend.connectors.base import ProjectConnector
from backend.connectors.http import HTTPConnector
from backend.connectors.local import LocalConnector
from backend.dispatcher import (
    get_dispatcher_status,
    start_dispatcher,
    stop_all,
    stop_dispatcher,
)
from backend.github import get_pr_for_branch, get_task_branch_name
from backend.models import DispatcherStatus, ProjectSummary, TaskCreateRequest, TaskDetail

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="Baton", version="0.1.0")

app.mount("/css", StaticFiles(directory=FRONTEND_DIR / "css"), name="css")
app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="js")

templates = Jinja2Templates(directory=FRONTEND_DIR)


@app.on_event("startup")
async def startup():
    config = load_config()
    for p in config.projects:
        if p.agent_url:
            continue  # remote agent manages its own dispatcher
        if p.dispatcher and p.dispatcher.enabled:
            try:
                start_dispatcher(p)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to start dispatcher for {p.id}: {e}")


@app.on_event("shutdown")
async def shutdown():
    stop_all()


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


@app.get("/project/{project_id}", response_class=HTMLResponse)
async def project_page(request: Request, project_id: str):
    cfg = get_project_by_id(project_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return templates.TemplateResponse("project.html", {
        "request": request,
        "project_id": cfg.id,
        "project_name": cfg.name,
    })


# ---- API routes ----

@app.get("/api/projects")
async def api_projects() -> list[ProjectSummary]:
    config = get_config()
    result = []
    for p in config.projects:
        conn = _make_connector(p)
        all_tasks = conn.get_all_tasks()
        counts = {status: len(tasks) for status, tasks in all_tasks.items()}
        dispatcher = None
        if p.agent_url:
            # Fetch dispatcher status from the remote agent
            if isinstance(conn, HTTPConnector):
                dispatcher = conn.get_dispatcher_status()
        elif p.dispatcher and p.dispatcher.enabled:
            ds = get_dispatcher_status(p.id)
            dispatcher = DispatcherStatus(**ds)
        result.append(ProjectSummary(
            id=p.id,
            name=p.name,
            description=p.description,
            color=p.color,
            task_counts=counts,
            healthy=conn.is_healthy(),
            dispatcher=dispatcher,
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
    return conn.create_task(body.title, body.content)


@app.get("/api/projects/{project_id}/worktrees")
async def api_worktrees(project_id: str):
    conn = _get_connector(project_id)
    return conn.get_worktrees()


@app.get("/api/projects/{project_id}/commits")
async def api_commits(project_id: str, count: int = 10):
    conn = _get_connector(project_id)
    return conn.get_recent_commits(count)


@app.get("/api/projects/{project_id}/dispatcher")
async def api_dispatcher_status(project_id: str) -> DispatcherStatus:
    cfg = get_project_by_id(project_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    if cfg.agent_url:
        return HTTPConnector(cfg.agent_url).get_dispatcher_status()
    ds = get_dispatcher_status(project_id)
    return DispatcherStatus(**ds)


@app.post("/api/projects/{project_id}/dispatcher/restart")
async def api_dispatcher_restart(project_id: str) -> DispatcherStatus:
    cfg = get_project_by_id(project_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    if cfg.agent_url:
        return HTTPConnector(cfg.agent_url).dispatcher_action("restart")
    if not cfg.dispatcher or not cfg.dispatcher.enabled:
        raise HTTPException(status_code=400, detail="Dispatcher not configured for this project")
    stop_dispatcher(project_id)
    start_dispatcher(cfg)
    ds = get_dispatcher_status(project_id)
    return DispatcherStatus(**ds)
