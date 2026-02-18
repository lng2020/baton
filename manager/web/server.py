"""
Web Manager - Mobile-friendly control panel for the Agentic Coding system.

Features:
- Task queue dashboard (pending / in_progress / completed / failed)
- Voice input integration for task creation
- Plan review interface for batch approval
- Real-time log viewer for Claude Code instances
- Git status overview (worktrees, recent commits, branches)

Designed for iPhone Safari, supports "Add to Home Screen" as PWA.
"""

import os
import json
import logging
import yaml
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
TASKS_DIR = BASE_DIR / "tasks"
CONFIG_PATH = BASE_DIR / "manager" / "config.yaml"

app = FastAPI(title="Agentic Coding Manager")

# Static files and templates
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    return {}


def verify_auth(request: Request):
    """Verify authentication token if auth is enabled."""
    config = load_config()
    web_config = config.get("web", {})
    if not web_config.get("auth_enabled", False):
        return True

    token = web_config.get("auth_token", "")
    if not token or token.startswith("$"):
        token = os.environ.get("WEB_AUTH_TOKEN", "")

    auth_header = request.headers.get("Authorization", "")
    query_token = request.query_params.get("token", "")

    if auth_header == f"Bearer {token}" or query_token == token:
        return True

    raise HTTPException(status_code=401, detail="Unauthorized")


def get_tasks(status: str) -> list[dict]:
    """Get tasks of a given status."""
    task_dir = TASKS_DIR / status
    tasks = []
    if task_dir.exists():
        for f in sorted(task_dir.glob("*.md")):
            if f.name == ".gitkeep":
                continue
            stat = f.stat()
            tasks.append(
                {
                    "id": f.stem,
                    "filename": f.name,
                    "content": f.read_text(encoding="utf-8"),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
    return tasks


# --- Routes ---


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    verify_auth(request)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "pending": get_tasks("pending"),
            "in_progress": get_tasks("in_progress"),
            "completed": get_tasks("completed"),
            "failed": get_tasks("failed"),
        },
    )


@app.get("/api/tasks")
async def api_tasks(request: Request):
    """API endpoint: get all tasks grouped by status."""
    verify_auth(request)
    return JSONResponse(
        {
            "pending": get_tasks("pending"),
            "in_progress": get_tasks("in_progress"),
            "completed": get_tasks("completed"),
            "failed": get_tasks("failed"),
        }
    )


@app.post("/api/tasks")
async def api_create_task(request: Request):
    """API endpoint: create a new task."""
    verify_auth(request)
    body = await request.json()
    content = body.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Task content is required")

    # Generate task ID
    existing = list(TASKS_DIR.rglob("*.md"))
    import re

    numbers = []
    for f in existing:
        match = re.match(r"^(\d+)", f.stem)
        if match:
            numbers.append(int(match.group(1)))
    next_num = max(numbers, default=0) + 1
    task_id = f"{next_num:03d}"

    # Create task file
    filename = f"{task_id}-task.md"
    task_path = TASKS_DIR / "pending" / filename
    task_path.write_text(content, encoding="utf-8")

    return JSONResponse(
        {"id": task_id, "filename": filename, "status": "pending"}, status_code=201
    )


@app.delete("/api/tasks/{status}/{filename}")
async def api_delete_task(request: Request, status: str, filename: str):
    """API endpoint: delete a task."""
    verify_auth(request)
    task_path = TASKS_DIR / status / filename
    if not task_path.exists():
        raise HTTPException(status_code=404, detail="Task not found")
    task_path.unlink()
    return JSONResponse({"deleted": filename})


@app.post("/api/tasks/{filename}/retry")
async def api_retry_task(request: Request, filename: str):
    """API endpoint: retry a failed task (move back to pending)."""
    verify_auth(request)
    failed_path = TASKS_DIR / "failed" / filename
    if not failed_path.exists():
        raise HTTPException(status_code=404, detail="Task not found in failed/")
    pending_path = TASKS_DIR / "pending" / filename
    failed_path.rename(pending_path)
    return JSONResponse({"retried": filename})


def main():
    """Start the Web Manager server."""
    import uvicorn

    config = load_config()
    web_config = config.get("web", {})
    host = web_config.get("host", "0.0.0.0")
    port = web_config.get("port", 8080)

    logger.info(f"Web Manager starting on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
