# Add Plan models and plans/ file storage to agent

type: feature

Create the plans infrastructure in the agent:

1. **Add Plan models** in `backend/models.py`:
   - `PlanStatus` enum: `draft`, `ready`, `executing`, `done`, `failed`
   - `PlanSummary(BaseModel)`: `id: str`, `title: str`, `summary: str`, `status: PlanStatus`, `created: datetime`, `modified: datetime`, `task_count: int` (number of tasks generated from this plan)
   - `PlanDetail(PlanSummary)`: `content: str` (the full plan markdown/JSON), `tasks: list[str]` (task IDs created from this plan), `error: str | None`
   - `PlanCreateRequest(BaseModel)`: `title: str`, `summary: str`, `content: str`

2. **Create plans/ directory structure** — plans are stored as JSON files in `plans/{status}/{plan_id}.plan.json` (mirroring the tasks/ pattern). Each file contains: `{id, title, summary, content, status, created, modified, tasks: [], error: null}`.

3. **Update `AgentDir`** in `backend/agent.py` (around line 52-80) to add:
   - `self.plans = self.root / 'plans'`
   - `self.plans_status(status)` method (same pattern as `tasks_status`)
   - Create the subdirectories: `plans/draft/`, `plans/ready/`, `plans/executing/`, `plans/done/`, `plans/failed/`

4. **Add plan CRUD helpers** in `backend/agent.py` (similar to `_list_tasks`, `_read_task`, `_create_task`):
   - `_list_plans(status: str | None = None) -> list[PlanSummary]` — list all plans or by status
   - `_read_plan(status: str, filename: str) -> PlanDetail | None` — read single plan
   - `_create_plan(title, summary, content) -> PlanDetail` — create plan in `draft` status
   - `_update_plan_status(plan_id, new_status, error=None)` — move plan file between status dirs
   - `_link_tasks_to_plan(plan_id, task_ids)` — update plan JSON with created task IDs

5. **Replace the existing dead-code `PlanReviewQueue` and `Plan` dataclass** (lines 377-455 in `backend/agent.py`) with the new plan management code.

6. **Add agent API endpoints** in `backend/agent.py`:
   - `GET /agent/plans` — returns `{draft: [...], ready: [...], executing: [...], done: [...], failed: [...]}`
   - `GET /agent/plans/{status}/{filename}` — returns `PlanDetail`
   - `POST /agent/plans` — create plan (body: `PlanCreateRequest`), returns `PlanDetail`
   - `POST /agent/plans/{plan_id}/start` — triggers the orchestrator (next task handles this)

7. **Add plan count to health endpoint** — update `GET /agent/health` to include plan counts.