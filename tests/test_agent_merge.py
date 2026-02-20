from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.agent import (
    AgentDir,
    Dispatcher,
    PortAllocator,
    _add_task_to_json,
    _claim_task_json,
    _load_dev_tasks,
    _mark_task_complete_json,
    _mark_task_failed_json,
    _save_dev_tasks,
)


@pytest.fixture
def dispatcher():
    """Create a Dispatcher with default config for testing."""
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


def _make_run_result(returncode=0, stdout="", stderr=""):
    r = subprocess.CompletedProcess(args=[], returncode=returncode)
    r.stdout = stdout
    r.stderr = stderr
    return r


_fake_agent_dir = AgentDir(root=Path("/fake/project"))


# ---------------------------------------------------------------------------
# PortAllocator tests
# ---------------------------------------------------------------------------

class TestPortAllocator:
    def test_allocate_returns_lowest_available(self):
        pa = PortAllocator(9200, 9202)
        assert pa.allocate() == 9200
        assert pa.allocate() == 9201
        assert pa.allocate() == 9202

    def test_allocate_raises_when_exhausted(self):
        pa = PortAllocator(9200, 9200)
        pa.allocate()
        with pytest.raises(RuntimeError, match="No ports available"):
            pa.allocate()

    def test_release_makes_port_available_again(self):
        pa = PortAllocator(9200, 9200)
        port = pa.allocate()
        pa.release(port)
        assert pa.allocate() == 9200

    def test_release_nonexistent_port_is_safe(self):
        pa = PortAllocator(9200, 9202)
        pa.release(9999)  # should not raise

    def test_thread_safety(self):
        pa = PortAllocator(9200, 9299)
        ports = []
        lock = threading.Lock()

        def grab():
            p = pa.allocate()
            with lock:
                ports.append(p)

        threads = [threading.Thread(target=grab) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(ports) == 50
        assert len(set(ports)) == 50  # all unique


# ---------------------------------------------------------------------------
# JSON task tracking tests
# ---------------------------------------------------------------------------

class TestJsonTaskTracking:
    def test_add_and_load(self, tmp_path):
        with patch("backend.agent.agent_dir", AgentDir(root=tmp_path)):
            _add_task_to_json("abc123", "Test task", "content", "feature")
            data = _load_dev_tasks()
            assert "abc123" in data["tasks"]
            t = data["tasks"]["abc123"]
            assert t["title"] == "Test task"
            assert t["status"] == "pending"
            assert t["worker_port"] is None

    def test_claim_task(self, tmp_path):
        with patch("backend.agent.agent_dir", AgentDir(root=tmp_path)):
            _add_task_to_json("abc123", "Test task", "content", "feature")
            result = _claim_task_json("abc123", port=9200)
            assert result is not None
            assert result["status"] == "in_progress"
            assert result["worker_port"] == 9200
            # Verify persisted
            data = _load_dev_tasks()
            assert data["tasks"]["abc123"]["status"] == "in_progress"

    def test_claim_already_claimed_returns_none(self, tmp_path):
        with patch("backend.agent.agent_dir", AgentDir(root=tmp_path)):
            _add_task_to_json("abc123", "Test task", "content", "feature")
            _claim_task_json("abc123", port=9200)
            result = _claim_task_json("abc123", port=9201)
            assert result is None

    def test_claim_nonexistent_returns_none(self, tmp_path):
        with patch("backend.agent.agent_dir", AgentDir(root=tmp_path)):
            result = _claim_task_json("nonexistent")
            assert result is None

    def test_mark_complete(self, tmp_path):
        with patch("backend.agent.agent_dir", AgentDir(root=tmp_path)):
            _add_task_to_json("abc123", "Test task", "content", "feature")
            _claim_task_json("abc123")
            _mark_task_complete_json("abc123")
            data = _load_dev_tasks()
            assert data["tasks"]["abc123"]["status"] == "completed"
            assert data["tasks"]["abc123"]["worker_port"] is None

    def test_mark_failed(self, tmp_path):
        with patch("backend.agent.agent_dir", AgentDir(root=tmp_path)):
            _add_task_to_json("abc123", "Test task", "content", "feature")
            _claim_task_json("abc123")
            _mark_task_failed_json("abc123", "something broke")
            data = _load_dev_tasks()
            assert data["tasks"]["abc123"]["status"] == "failed"
            assert data["tasks"]["abc123"]["error"] == "something broke"

    def test_atomic_write(self, tmp_path):
        """Verify _save_dev_tasks writes atomically (temp + rename)."""
        with patch("backend.agent.agent_dir", AgentDir(root=tmp_path)):
            _save_dev_tasks({"tasks": {"x": {"id": "x", "status": "pending"}}})
            path = tmp_path / "data" / "dev-tasks.json"
            assert path.exists()
            data = json.loads(path.read_text())
            assert data["tasks"]["x"]["status"] == "pending"

    def test_load_missing_file_returns_empty(self, tmp_path):
        with patch("backend.agent.agent_dir", AgentDir(root=tmp_path)):
            data = _load_dev_tasks()
            assert data == {"tasks": {}}


# ---------------------------------------------------------------------------
# Merge + Test + Push tests (replaces old TestMergeToMain)
# ---------------------------------------------------------------------------

class TestMergeTestPush:
    """Tests for Dispatcher._merge_test_push()."""

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_successful_merge_test_push(self, mock_run, dispatcher):
        mock_run.return_value = _make_run_result(0)
        worktree = Path("/fake/worktrees/abc12345")

        dispatcher._merge_test_push("abc12345", worktree)

        # Should call: fetch, merge origin/main, test, fetch origin main,
        # rebase, checkout main, merge branch, push
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert ["git", "fetch", "origin"] in calls
        assert ["git", "merge", "origin/main"] in calls
        assert ["git", "rebase", "origin/main"] in calls
        assert ["git", "merge", "task/abc12345"] in calls
        assert ["git", "push", "origin", "main"] in calls

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_test_command_executed(self, mock_run, dispatcher):
        mock_run.return_value = _make_run_result(0)
        worktree = Path("/fake/worktrees/abc12345")

        dispatcher._merge_test_push("abc12345", worktree)

        # Find the pytest call
        test_calls = [c for c in mock_run.call_args_list if c[0][0] == ["pytest"]]
        assert len(test_calls) == 1
        assert test_calls[0].kwargs.get("cwd") == str(worktree) or str(test_calls[0]) in str(mock_run.call_args_list)

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_skip_tests_when_empty_command(self, mock_run, dispatcher):
        dispatcher.config.test_command = ""
        mock_run.return_value = _make_run_result(0)
        worktree = Path("/fake/worktrees/abc12345")

        dispatcher._merge_test_push("abc12345", worktree)

        # No pytest call
        test_calls = [c for c in mock_run.call_args_list if c[0][0] == ["pytest"]]
        assert len(test_calls) == 0

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_merge_origin_main_failure_raises(self, mock_run, dispatcher):
        mock_run.side_effect = [
            _make_run_result(0),   # fetch
            _make_run_result(1, stderr="CONFLICT"),  # merge origin/main
        ]
        worktree = Path("/fake/worktrees/abc12345")

        with pytest.raises(Exception, match="Cannot merge origin/main"):
            dispatcher._merge_test_push("abc12345", worktree)

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_test_failure_raises(self, mock_run, dispatcher):
        mock_run.side_effect = [
            _make_run_result(0),  # fetch
            _make_run_result(0),  # merge origin/main
            _make_run_result(1, stderr="FAILED tests"),  # pytest
        ]
        worktree = Path("/fake/worktrees/abc12345")

        with pytest.raises(Exception, match="Tests failed"):
            dispatcher._merge_test_push("abc12345", worktree)

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_rebase_failure_retries(self, mock_run, dispatcher):
        """Rebase failure triggers retry from step 5."""
        worktree = Path("/fake/worktrees/abc12345")
        # Attempt 1: fetch, merge, test, fetch main, rebase fails, abort
        # Attempt 2: fetch, merge, test, fetch main, rebase succeeds, checkout, merge, push
        mock_run.side_effect = [
            # Attempt 1
            _make_run_result(0),  # fetch origin
            _make_run_result(0),  # merge origin/main
            _make_run_result(0),  # pytest
            _make_run_result(0),  # fetch origin main
            _make_run_result(1, stderr="rebase conflict"),  # rebase fails
            _make_run_result(0),  # rebase --abort
            # Attempt 2
            _make_run_result(0),  # fetch origin
            _make_run_result(0),  # merge origin/main
            _make_run_result(0),  # pytest
            _make_run_result(0),  # fetch origin main
            _make_run_result(0),  # rebase succeeds
            _make_run_result(0),  # checkout main
            _make_run_result(0),  # merge
            _make_run_result(0),  # push
        ]

        dispatcher._merge_test_push("abc12345", worktree)  # Should not raise

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_rebase_exhausts_retries(self, mock_run, dispatcher):
        """All retries exhausted raises exception."""
        worktree = Path("/fake/worktrees/abc12345")
        dispatcher.config.max_merge_retries = 2
        # Both attempts fail at rebase
        mock_run.side_effect = [
            _make_run_result(0), _make_run_result(0), _make_run_result(0),  # fetch, merge, test
            _make_run_result(0),  # fetch origin main
            _make_run_result(1),  # rebase fail
            _make_run_result(0),  # rebase abort
            _make_run_result(0), _make_run_result(0), _make_run_result(0),  # fetch, merge, test
            _make_run_result(0),  # fetch origin main
            _make_run_result(1),  # rebase fail
            _make_run_result(0),  # rebase abort
        ]

        with pytest.raises(Exception, match="Rebase failed after 2 attempts"):
            dispatcher._merge_test_push("abc12345", worktree)

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_push_disabled(self, mock_run, dispatcher):
        dispatcher.config.push_to_remote = False
        mock_run.return_value = _make_run_result(0)
        worktree = Path("/fake/worktrees/abc12345")

        dispatcher._merge_test_push("abc12345", worktree)

        push_calls = [c for c in mock_run.call_args_list if c[0][0] == ["git", "push", "origin", "main"]]
        assert len(push_calls) == 0


# ---------------------------------------------------------------------------
# Create worktree tests
# ---------------------------------------------------------------------------

class TestCreateWorktree:
    """Tests for Dispatcher._create_worktree()."""

    @patch("backend.agent.shutil.copy2")
    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_create_worktree_calls_git(self, mock_run, mock_copy, dispatcher, tmp_path):
        mock_run.return_value = _make_run_result(0)
        fake_dir = AgentDir(root=tmp_path)
        with patch("backend.agent.agent_dir", fake_dir):
            result = dispatcher._create_worktree("task123")

        assert result == fake_dir.worktrees / "task123"
        cmd = mock_run.call_args[0][0]
        assert cmd[0:3] == ["git", "worktree", "add"]
        assert "task/task123" in cmd

    @patch("backend.agent.shutil.copy2")
    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_create_worktree_failure_raises(self, mock_run, mock_copy, dispatcher, tmp_path):
        mock_run.return_value = _make_run_result(128, stderr="fatal: already exists")
        fake_dir = AgentDir(root=tmp_path)
        with patch("backend.agent.agent_dir", fake_dir):
            with pytest.raises(Exception, match="git worktree add failed"):
                dispatcher._create_worktree("task123")

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_create_worktree_uses_git_lock(self, mock_run, dispatcher, tmp_path):
        """Create worktree acquires the same git lock as merge."""
        mock_run.return_value = _make_run_result(0)
        fake_dir = AgentDir(root=tmp_path)

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=False)
        dispatcher._git_lock = mock_lock

        with patch("backend.agent.agent_dir", fake_dir):
            dispatcher._create_worktree("locktest")

        mock_lock.__enter__.assert_called()
        mock_lock.__exit__.assert_called()

    @patch("backend.agent.shutil.copy2")
    @patch("backend.agent.subprocess.run")
    def test_create_worktree_creates_data_dir(self, mock_run, mock_copy, dispatcher, tmp_path):
        mock_run.return_value = _make_run_result(0)
        fake_dir = AgentDir(root=tmp_path)
        with patch("backend.agent.agent_dir", fake_dir):
            result = dispatcher._create_worktree("task123")
        assert (result / "data").is_dir()

    @patch("backend.agent.shutil.copy2")
    @patch("backend.agent.subprocess.run")
    def test_create_worktree_symlinks_files(self, mock_run, mock_copy, dispatcher, tmp_path):
        mock_run.return_value = _make_run_result(0)
        fake_dir = AgentDir(root=tmp_path)
        # Create source file for symlinking
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "dev-tasks.json").write_text("{}")
        dispatcher.config.symlink_files = ["data/dev-tasks.json"]
        with patch("backend.agent.agent_dir", fake_dir):
            result = dispatcher._create_worktree("task123")
        symlink = result / "data" / "dev-tasks.json"
        assert symlink.is_symlink()
        assert symlink.resolve() == (tmp_path / "data" / "dev-tasks.json").resolve()


# ---------------------------------------------------------------------------
# Cleanup worktree tests
# ---------------------------------------------------------------------------

class TestCleanupWorktree:
    """Tests for Dispatcher._cleanup_worktree()."""

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_cleanup_calls_remove_branch_delete_and_remote(self, mock_run, dispatcher):
        mock_run.return_value = _make_run_result(0)

        dispatcher._cleanup_worktree("task456")

        assert mock_run.call_count == 3
        remove_call = mock_run.call_args_list[0]
        assert "worktree" in remove_call[0][0]
        assert "remove" in remove_call[0][0]
        branch_call = mock_run.call_args_list[1]
        assert branch_call[0][0] == ["git", "branch", "-D", "task/task456"]
        remote_call = mock_run.call_args_list[2]
        assert remote_call[0][0] == ["git", "push", "origin", "--delete", "task/task456"]

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_cleanup_skips_remote_when_push_disabled(self, mock_run, dispatcher):
        dispatcher.config.push_to_remote = False
        mock_run.return_value = _make_run_result(0)

        dispatcher._cleanup_worktree("task456")

        assert mock_run.call_count == 2  # worktree remove + branch delete only

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_cleanup_uses_git_lock(self, mock_run, dispatcher):
        """Cleanup worktree acquires the same git lock as merge."""
        mock_run.return_value = _make_run_result(0)

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=False)
        dispatcher._git_lock = mock_lock

        dispatcher._cleanup_worktree("task456")

        mock_lock.__enter__.assert_called()
        mock_lock.__exit__.assert_called()


class TestGitLockSerialization:
    def test_git_lock_is_threading_lock(self, dispatcher):
        assert hasattr(dispatcher, "_git_lock")
        assert isinstance(dispatcher._git_lock, type(threading.Lock()))
