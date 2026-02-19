"""Tests for the chat service and agent engineer endpoints."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.chat import _extract_json, build_system_prompt
from backend.models import (
    BulkTaskCreateRequest,
    ChatMessage,
    ChatPlan,
    ChatPlanTask,
    ChatRequest,
    TaskCreateRequest,
)


def test_build_system_prompt_includes_project_name():
    prompt = build_system_prompt("MyProject")
    assert "MyProject" in prompt


def test_build_system_prompt_includes_description():
    prompt = build_system_prompt("MyProject", "A cool project")
    assert "A cool project" in prompt


def test_build_system_prompt_empty_description():
    prompt = build_system_prompt("MyProject", "")
    assert "MyProject" in prompt


def test_extract_json_from_code_fence():
    text = '```json\n{"plan": true, "summary": "Test", "tasks": []}\n```'
    result = _extract_json(text)
    assert result["plan"] is True
    assert result["summary"] == "Test"


def test_extract_json_from_bare_code_fence():
    text = '```\n{"plan": true, "summary": "Test", "tasks": []}\n```'
    result = _extract_json(text)
    assert result["plan"] is True


def test_extract_json_raw():
    text = '{"plan": true, "summary": "Test", "tasks": []}'
    result = _extract_json(text)
    assert result["plan"] is True


def test_extract_json_invalid_raises():
    with pytest.raises(json.JSONDecodeError):
        _extract_json("not json at all")


# ---- Model tests ----

def test_chat_message_model():
    msg = ChatMessage(role="user", content="hello")
    assert msg.role == "user"
    assert msg.content == "hello"


def test_chat_request_model():
    req = ChatRequest(messages=[
        ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="hi there"),
    ])
    assert len(req.messages) == 2
    assert req.session_id is None


def test_chat_request_with_session_id():
    req = ChatRequest(
        messages=[ChatMessage(role="user", content="hello")],
        session_id="abc123",
    )
    assert req.session_id == "abc123"


def test_chat_plan_model():
    plan = ChatPlan(
        summary="Test plan",
        tasks=[
            ChatPlanTask(title="Task 1", content="Do thing 1"),
            ChatPlanTask(title="Task 2", content="Do thing 2"),
        ],
    )
    assert plan.summary == "Test plan"
    assert len(plan.tasks) == 2
    assert plan.tasks[0].title == "Task 1"


def test_bulk_task_create_request():
    req = BulkTaskCreateRequest(tasks=[
        TaskCreateRequest(title="Task 1", content="Content 1"),
        TaskCreateRequest(title="Task 2"),
    ])
    assert len(req.tasks) == 2
    assert req.tasks[1].content == ""


# ---- Agent endpoint tests ----

@pytest.fixture
def _patch_agent_dir(tmp_path):
    """Patch agent_dir to use a temp directory for task creation."""
    from backend.agent import AgentDir
    fake_dir = AgentDir(root=tmp_path)
    (tmp_path / "tasks" / "pending").mkdir(parents=True)
    with patch("backend.agent.agent_dir", fake_dir):
        yield fake_dir


def test_create_tasks_bulk(_patch_agent_dir):
    """Test that _create_task works for bulk creation."""
    from backend.agent import _create_task

    tasks = []
    for i in range(3):
        t = _create_task(f"Task {i}", f"Content {i}")
        tasks.append(t)

    assert len(tasks) == 3
    for i, t in enumerate(tasks):
        assert t.title == f"Task {i}"
        assert t.status == "pending"
        assert f"Content {i}" in t.content
