"""Tests for the plan review workflow — JSON-based task tracking,
dispatcher routing, and approve/revise/reject flows.

All task state is in data/dev-tasks.json (single source of truth).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.agent import (
    AgentDir,
    Dispatcher,
    _add_task_to_json,
    _claim_task_json,
    _load_dev_tasks,
    _mark_task_failed_json,
    _mark_task_pending_json,
    _mark_task_plan_review_json,
    _save_dev_tasks,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_agent_dir(tmp_path):
    """Create a temporary AgentDir and patch backend.agent.agent_dir."""
    ad = AgentDir(root=tmp_path)
    (tmp_path / "data").mkdir(parents=True)
    with patch("backend.agent.agent_dir", ad):
        yield ad


@pytest.fixture
def dispatcher():
    with patch("backend.agent.AGENT_CONFIG") as mock_cfg:
        mock_cfg.max_parallel_workers = 1
        mock_cfg.poll_interval_seconds = 10
        mock_cfg.claude_code = MagicMock()
        mock_cfg.port_range_start = 9200
        mock_cfg.port_range_end = 9299
        mock_cfg.test_command = "pytest"
        mock_cfg.push_to_remote = True
        mock_cfg.symlink_files = []
        mock_cfg.copy_files = ["CLAUDE.md", "PROGRESS.md"]
        mock_cfg.max_merge_retries = 3
        d = Dispatcher(mock_cfg)
    return d


# ---------------------------------------------------------------------------
# JSON tracking for plan_review and pending
# ---------------------------------------------------------------------------

class TestPlanReviewJsonTracking:
    def test_mark_plan_review(self, tmp_agent_dir):
        _add_task_to_json("abc123", "Test", "content", "feature", needs_plan_review=True)
        _claim_task_json("abc123")
        _mark_task_plan_review_json("abc123", plan_content="The plan")
        data = _load_dev_tasks()
        assert data["tasks"]["abc123"]["status"] == "plan_review"
        assert data["tasks"]["abc123"]["worker_port"] is None
        assert data["tasks"]["abc123"]["plan_content"] == "The plan"

    def test_mark_pending_from_plan_review(self, tmp_agent_dir):
        _add_task_to_json("abc123", "Test", "content", "feature", needs_plan_review=True)
        _claim_task_json("abc123")
        _mark_task_plan_review_json("abc123", plan_content="Plan")
        _mark_task_pending_json("abc123")
        data = _load_dev_tasks()
        assert data["tasks"]["abc123"]["status"] == "pending"

    def test_needs_plan_review_stored_in_json(self, tmp_agent_dir):
        _add_task_to_json("abc123", "Test", "content", "feature", needs_plan_review=True)
        data = _load_dev_tasks()
        assert data["tasks"]["abc123"]["needs_plan_review"] is True

    def test_needs_plan_review_default_false(self, tmp_agent_dir):
        _add_task_to_json("abc123", "Test", "content", "feature")
        data = _load_dev_tasks()
        assert data["tasks"]["abc123"]["needs_plan_review"] is False

    def test_plan_content_stored_in_json(self, tmp_agent_dir):
        _add_task_to_json("abc123", "Test", "content", "feature", needs_plan_review=True)
        _claim_task_json("abc123")
        _mark_task_plan_review_json("abc123", plan_content="Detailed plan here")
        data = _load_dev_tasks()
        assert data["tasks"]["abc123"]["plan_content"] == "Detailed plan here"

    def test_plan_content_default_none(self, tmp_agent_dir):
        _add_task_to_json("abc123", "Test", "content", "feature")
        data = _load_dev_tasks()
        assert data["tasks"]["abc123"]["plan_content"] is None


# ---------------------------------------------------------------------------
# Dispatcher routing decision
# ---------------------------------------------------------------------------

class TestPlanDecision:
    def test_routes_to_plan_phase_when_needs_plan_no_plan_content(self, dispatcher, tmp_agent_dir):
        _add_task_to_json("abc123", "Task", "Do something", "feature", needs_plan_review=True)

        with patch.object(dispatcher, "_execute_plan_phase") as mock_plan, \
             patch.object(dispatcher, "_execute_full") as mock_full:
            dispatcher._execute_task("abc123")
            mock_plan.assert_called_once_with("abc123")
            mock_full.assert_not_called()

    def test_routes_to_full_when_has_plan_content(self, dispatcher, tmp_agent_dir):
        _add_task_to_json("abc123", "Task", "Do something", "feature", needs_plan_review=True)
        # Simulate plan content being stored
        with patch("backend.agent._dev_tasks_lock"):
            data = _load_dev_tasks()
            data["tasks"]["abc123"]["plan_content"] = "the plan"
            _save_dev_tasks(data)

        with patch.object(dispatcher, "_execute_plan_phase") as mock_plan, \
             patch.object(dispatcher, "_execute_full") as mock_full:
            dispatcher._execute_task("abc123")
            mock_full.assert_called_once_with("abc123")
            mock_plan.assert_not_called()

    def test_routes_to_full_when_no_plan_review(self, dispatcher, tmp_agent_dir):
        _add_task_to_json("abc123", "Task", "Do something", "feature")

        with patch.object(dispatcher, "_execute_plan_phase") as mock_plan, \
             patch.object(dispatcher, "_execute_full") as mock_full:
            dispatcher._execute_task("abc123")
            mock_full.assert_called_once_with("abc123")
            mock_plan.assert_not_called()


# ---------------------------------------------------------------------------
# Plan approval flow (JSON only)
# ---------------------------------------------------------------------------

class TestPlanApproval:
    def test_approve_moves_to_pending_with_plan(self, tmp_agent_dir):
        task_id = "abc123"
        _add_task_to_json(task_id, "Task", "content", "feature", needs_plan_review=True)
        _claim_task_json(task_id)
        _mark_task_plan_review_json(task_id, plan_content="The implementation plan")

        # Approve: just change status to pending
        _mark_task_pending_json(task_id)

        data = _load_dev_tasks()
        assert data["tasks"][task_id]["status"] == "pending"
        assert data["tasks"][task_id]["plan_content"] == "The implementation plan"


# ---------------------------------------------------------------------------
# Plan revision flow (JSON only)
# ---------------------------------------------------------------------------

class TestPlanRevision:
    def test_revise_appends_feedback_clears_plan(self, tmp_agent_dir):
        task_id = "abc123"
        _add_task_to_json(task_id, "Task", "Original content", "feature", needs_plan_review=True)
        _claim_task_json(task_id)
        _mark_task_plan_review_json(task_id, plan_content="Old plan")

        # Revise: append feedback, clear plan, move to pending
        from datetime import datetime, timezone
        with patch("backend.agent._dev_tasks_lock"):
            data = _load_dev_tasks()
            task = data["tasks"][task_id]
            task["content"] = task["content"] + "\n\n## Revision Feedback\n\nPlease also handle edge cases\n"
            task["plan_content"] = None
            task["status"] = "pending"
            task["modified"] = datetime.now(timezone.utc).isoformat()
            _save_dev_tasks(data)

        data = _load_dev_tasks()
        assert data["tasks"][task_id]["status"] == "pending"
        assert data["tasks"][task_id]["plan_content"] is None
        assert "edge cases" in data["tasks"][task_id]["content"]


# ---------------------------------------------------------------------------
# Plan rejection flow (JSON only)
# ---------------------------------------------------------------------------

class TestPlanRejection:
    def test_reject_moves_to_failed(self, tmp_agent_dir):
        task_id = "abc123"
        _add_task_to_json(task_id, "Task", "content", "feature", needs_plan_review=True)
        _claim_task_json(task_id)
        _mark_task_plan_review_json(task_id, plan_content="The plan")

        # Reject
        _mark_task_failed_json(task_id, "Plan rejected by user")

        data = _load_dev_tasks()
        assert data["tasks"][task_id]["status"] == "failed"
        assert data["tasks"][task_id]["error"] == "Plan rejected by user"
