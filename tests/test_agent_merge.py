from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from backend.agent import AgentDir, Dispatcher


@pytest.fixture
def dispatcher():
    """Create a Dispatcher with default config for testing."""
    with patch("backend.agent.AGENT_CONFIG") as mock_cfg:
        mock_cfg.max_parallel_workers = 1
        mock_cfg.poll_interval_seconds = 10
        mock_cfg.claude_code = MagicMock()
        d = Dispatcher(mock_cfg)
    return d


def _make_run_result(returncode=0, stdout="", stderr=""):
    r = subprocess.CompletedProcess(args=[], returncode=returncode)
    r.stdout = stdout
    r.stderr = stderr
    return r


_fake_agent_dir = AgentDir(root=Path("/fake/project"))


class TestMergeToMain:
    """Tests for Dispatcher._merge_to_main()."""

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_successful_merge(self, mock_run, dispatcher):
        mock_run.return_value = _make_run_result(0)

        dispatcher._merge_to_main("abc12345")

        assert mock_run.call_count == 2
        # First call: git checkout main
        checkout_call = mock_run.call_args_list[0]
        assert checkout_call[0][0] == ["git", "checkout", "main"]
        # Second call: git merge
        merge_call = mock_run.call_args_list[1]
        assert merge_call[0][0] == ["git", "merge", "task/abc12345", "--no-ff"]

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_checkout_failure_raises(self, mock_run, dispatcher):
        mock_run.return_value = _make_run_result(1, stderr="error: pathspec 'main'")

        with pytest.raises(Exception, match="git checkout main failed"):
            dispatcher._merge_to_main("abc12345")

        # Only checkout was called, merge was not attempted
        assert mock_run.call_count == 1

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_merge_conflict_aborts_and_raises(self, mock_run, dispatcher):
        # checkout succeeds, merge fails, abort succeeds
        mock_run.side_effect = [
            _make_run_result(0),  # checkout
            _make_run_result(1, stderr="CONFLICT (content): Merge conflict in file.py"),  # merge
            _make_run_result(0),  # merge --abort
        ]

        with pytest.raises(Exception, match="git merge task/abc12345 failed"):
            dispatcher._merge_to_main("abc12345")

        assert mock_run.call_count == 3
        abort_call = mock_run.call_args_list[2]
        assert abort_call[0][0] == ["git", "merge", "--abort"]

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_merge_exit_status_2_aborts(self, mock_run, dispatcher):
        """Exit status 2 (the exact error from the bug report) triggers abort."""
        mock_run.side_effect = [
            _make_run_result(0),  # checkout
            _make_run_result(2, stderr="Automatic merge failed; fix conflicts"),  # merge
            _make_run_result(0),  # merge --abort
        ]

        with pytest.raises(Exception, match="rc=2"):
            dispatcher._merge_to_main("abc12345")

        abort_call = mock_run.call_args_list[2]
        assert abort_call[0][0] == ["git", "merge", "--abort"]

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_git_lock_serializes_access(self, mock_run, dispatcher):
        """The git lock should exist and be a threading.Lock."""
        import threading

        assert hasattr(dispatcher, "_git_lock")
        assert isinstance(dispatcher._git_lock, type(threading.Lock()))


class TestCreateWorktree:
    """Tests for Dispatcher._create_worktree()."""

    @patch("backend.agent.shutil.copy2")
    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_create_worktree_calls_git(self, mock_run, mock_copy, dispatcher, tmp_path):
        mock_run.return_value = _make_run_result(0)
        # Patch worktrees dir to use tmp_path so mkdir works
        fake_dir = AgentDir(root=tmp_path)
        with patch("backend.agent.agent_dir", fake_dir):
            result = dispatcher._create_worktree("task123")

        assert result == fake_dir.worktrees / "task123"
        mock_run.assert_called_once()
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

        # Replace the lock with a MagicMock to track __enter__/__exit__
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=False)
        dispatcher._git_lock = mock_lock

        with patch("backend.agent.agent_dir", fake_dir):
            dispatcher._create_worktree("locktest")

        mock_lock.__enter__.assert_called()
        mock_lock.__exit__.assert_called()


class TestCleanupWorktree:
    """Tests for Dispatcher._cleanup_worktree()."""

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_cleanup_calls_remove_and_branch_delete(self, mock_run, dispatcher):
        mock_run.return_value = _make_run_result(0)

        dispatcher._cleanup_worktree("task456")

        assert mock_run.call_count == 2
        remove_call = mock_run.call_args_list[0]
        assert "worktree" in remove_call[0][0]
        assert "remove" in remove_call[0][0]
        branch_call = mock_run.call_args_list[1]
        assert branch_call[0][0] == ["git", "branch", "-D", "task/task456"]

    @patch("backend.agent.agent_dir", _fake_agent_dir)
    @patch("backend.agent.subprocess.run")
    def test_cleanup_uses_git_lock(self, mock_run, dispatcher):
        """Cleanup worktree acquires the same git lock as merge."""
        mock_run.return_value = _make_run_result(0)

        # Replace the lock with a MagicMock to track __enter__/__exit__
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=False)
        dispatcher._git_lock = mock_lock

        dispatcher._cleanup_worktree("task456")

        mock_lock.__enter__.assert_called()
        mock_lock.__exit__.assert_called()
