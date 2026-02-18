# Baton — Multi-Project Dashboard

## Quick Start
```bash
pip install -e .
python -m uvicorn backend.server:app --reload --port 8888
```

## Project Layout
- `backend/` — FastAPI server, Pydantic models, filesystem connector
- `frontend/` — Jinja2 templates + vanilla JS
- `config/projects.yaml` — project registry
- `tasks/` — baton's own task queue

## Conventions
- Dark theme palette: #1a1a2e, #16213e, #0f3460, #e94560
- Status colors: pending=#f39c12, in_progress=#3498db, completed=#2ecc71, failed=#e74c3c
- Task files live in `tasks/{pending,in_progress,completed,failed}/*.md`
- Error logs: `{task_id}.error.log` alongside task file
- Session logs: `{task_id}.log.json` alongside task file

## Running
- Server runs on port 8888
- API prefix: `/api/`
- Static files: `/css/`, `/js/`
