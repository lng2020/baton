"""
Ralph Loop Task Dispatcher
- Continuously monitors tasks/pending/ directory
- Launches independent Claude Code instances for each task
- Supports parallel execution via Git worktrees
- Monitors execution logs (stream-json)
"""

import argparse
import subprocess
import os
import json
import time
import yaml
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def resolve_project_dir(cli_arg: str | None = None) -> Path:
    """Resolve project directory from CLI arg, env var, or auto-detect."""
    if cli_arg:
        return Path(cli_arg).resolve()
    env = os.environ.get("BATON_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    # Auto-detect: go up from manager/ to the project root
    return Path(__file__).parent.parent.resolve()


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_pending_tasks(tasks_dir: Path):
    pending = tasks_dir / "pending"
    return sorted(
        [f for f in pending.glob("*.md") if f.name != ".gitkeep"],
        key=os.path.getmtime,
    )


def create_worktree(task_id: str, project_dir: Path, worktrees_dir: Path) -> Path:
    """Create an independent Git worktree for the task."""
    branch = f"task/{task_id}"
    worktree_path = worktrees_dir / task_id
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), "main"],
        cwd=str(project_dir),
        check=True,
    )
    return worktree_path


def cleanup_worktree(task_id: str, project_dir: Path, worktrees_dir: Path):
    """Clean up a completed worktree."""
    worktree_path = worktrees_dir / task_id
    subprocess.run(
        ["git", "worktree", "remove", str(worktree_path), "--force"],
        cwd=str(project_dir),
        capture_output=True,
    )
    branch = f"task/{task_id}"
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=str(project_dir),
        capture_output=True,
    )


def execute_task(task_file: Path, config: dict, project_dir: Path, tasks_dir: Path, worktrees_dir: Path):
    """Launch Claude Code in an independent worktree to execute a task."""
    task_id = task_file.stem
    logger.info(f"Starting task: {task_id}")

    # Move to in_progress
    in_progress = tasks_dir / "in_progress" / task_file.name
    task_file.rename(in_progress)

    try:
        # Create worktree
        worktree_path = create_worktree(task_id, project_dir, worktrees_dir)

        # Read task content
        task_content = in_progress.read_text()

        # Build prompt
        prompt = f"""Please execute the following task:

{task_content}

When complete:
1. Run tests to ensure they pass
2. Commit code, commit message format: feat({task_id}): [description]
3. Update PROGRESS.md with lessons learned
4. Exit
"""

        # Get Claude Code config
        cc_config = config.get("claude_code", {})
        timeout = cc_config.get("timeout", 600)

        # Launch Claude Code (stream-json mode)
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]

        if cc_config.get("skip_permissions", False):
            cmd.append("--dangerously-skip-permissions")

        proc = subprocess.Popen(
            cmd,
            cwd=str(worktree_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Monitor output
        for line in proc.stdout:
            try:
                event = json.loads(line.decode().strip())
                if event.get("type") == "error":
                    logger.error(f"[{task_id}] Error: {event}")
                elif event.get("type") == "assistant":
                    # Log assistant messages for monitoring
                    pass
            except json.JSONDecodeError:
                pass

        proc.wait(timeout=timeout)

        if proc.returncode == 0:
            # Success: merge to main
            subprocess.run(
                ["git", "checkout", "main"],
                cwd=str(project_dir),
                check=True,
            )
            subprocess.run(
                ["git", "merge", f"task/{task_id}", "--no-ff"],
                cwd=str(project_dir),
                check=True,
            )
            in_progress.rename(tasks_dir / "completed" / task_file.name)
            logger.info(f"Task complete: {task_id}")
        else:
            raise Exception(f"Claude Code exit code: {proc.returncode}")

    except Exception as e:
        logger.error(f"Task failed: {task_id} - {e}")
        failed_path = tasks_dir / "failed" / task_file.name
        in_progress.rename(failed_path)
        # Attach error log
        (tasks_dir / "failed" / f"{task_id}.error.log").write_text(str(e))

    finally:
        cleanup_worktree(task_id, project_dir, worktrees_dir)


def main():
    parser = argparse.ArgumentParser(description="Baton Task Dispatcher")
    parser.add_argument(
        "--project-dir",
        default=None,
        help="Project root directory (default: BATON_PROJECT_DIR env or auto-detect)",
    )
    args = parser.parse_args()

    project_dir = resolve_project_dir(args.project_dir)
    tasks_dir = project_dir / "tasks"
    worktrees_dir = project_dir / "worktrees"

    config = load_config()
    max_workers = config.get("max_parallel_workers", 3)
    poll_interval = config.get("poll_interval_seconds", 10)

    logger.info(f"Ralph Loop started - project: {project_dir}, max parallel: {max_workers}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        active_futures = {}
        while True:
            # Clean up completed futures
            done = [tid for tid, f in active_futures.items() if f.done()]
            for tid in done:
                future = active_futures.pop(tid)
                exc = future.exception()
                if exc:
                    logger.error(f"Task {tid} raised exception: {exc}")

            # Fill with new tasks
            available_slots = max_workers - len(active_futures)
            if available_slots > 0:
                tasks = get_pending_tasks(tasks_dir)[:available_slots]
                for task in tasks:
                    task_id = task.stem
                    if task_id not in active_futures:
                        future = executor.submit(
                            execute_task, task, config, project_dir, tasks_dir, worktrees_dir
                        )
                        active_futures[task_id] = future

            time.sleep(poll_interval)


if __name__ == "__main__":
    main()
