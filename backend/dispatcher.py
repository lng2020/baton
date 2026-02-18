from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from backend.config import ProjectConfig

logger = logging.getLogger(__name__)

_dispatchers: dict[str, DispatcherProcess] = {}


@dataclass
class DispatcherProcess:
    project_id: str
    process: subprocess.Popen
    log_path: Path = field(default_factory=lambda: Path("/dev/null"))

    @property
    def status(self) -> str:
        rc = self.process.poll()
        if rc is None:
            return "running"
        if rc == 0:
            return "stopped"
        return "crashed"

    @property
    def pid(self) -> int | None:
        if self.process.poll() is None:
            return self.process.pid
        return None


def start_dispatcher(config: ProjectConfig) -> DispatcherProcess:
    """Start the dispatcher process for a project."""
    if config.id in _dispatchers:
        existing = _dispatchers[config.id]
        if existing.status == "running":
            logger.info(f"Dispatcher for {config.id} already running (pid={existing.pid})")
            return existing
        # Dead process — clean up before restarting
        del _dispatchers[config.id]

    if not config.dispatcher or not config.dispatcher.command:
        raise ValueError(f"No dispatcher command configured for project {config.id}")

    log_path = Path(f"/tmp/baton-dispatcher-{config.id}.log")
    log_file = open(log_path, "a")

    cmd = config.dispatcher.command.split()
    cmd.extend(["--project-dir", config.path])

    process = subprocess.Popen(
        cmd,
        cwd=config.path,
        stdout=log_file,
        stderr=log_file,
        preexec_fn=os.setsid,
    )

    dp = DispatcherProcess(
        project_id=config.id,
        process=process,
        log_path=log_path,
    )
    _dispatchers[config.id] = dp
    logger.info(f"Started dispatcher for {config.id} (pid={process.pid}, log={log_path})")
    return dp


def stop_dispatcher(project_id: str) -> None:
    """Stop the dispatcher process for a project."""
    dp = _dispatchers.pop(project_id, None)
    if dp is None:
        return
    if dp.process.poll() is None:
        try:
            os.killpg(os.getpgid(dp.process.pid), signal.SIGTERM)
            dp.process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(dp.process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    logger.info(f"Stopped dispatcher for {project_id}")


def get_dispatcher_status(project_id: str) -> dict:
    """Get the status and PID for a project's dispatcher."""
    dp = _dispatchers.get(project_id)
    if dp is None:
        return {"status": "stopped", "pid": None}
    return {"status": dp.status, "pid": dp.pid}


def stop_all() -> None:
    """Stop all running dispatchers."""
    for project_id in list(_dispatchers.keys()):
        stop_dispatcher(project_id)


atexit.register(stop_all)
