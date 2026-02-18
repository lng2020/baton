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
    def test_merge_lock_serializes_access(self, mock_run, dispatcher):
        """The merge lock should exist and be a threading.Lock."""
        import threading

        assert hasattr(dispatcher, "_merge_lock")
        assert isinstance(dispatcher._merge_lock, type(threading.Lock()))
