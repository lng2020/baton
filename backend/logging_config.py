"""Centralized logging configuration for Baton.

Configures the root logger with both console and file output.
All modules should use ``logging.getLogger(__name__)`` — this module
handles handler setup so individual modules don't need to call
``logging.basicConfig`` themselves.

The log file is written to ``logs/baton.log`` relative to the project
directory, with automatic rotation at 5 MB (3 backups kept).
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "baton.log"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 3


def setup_logging(
    level: str | int = "INFO",
    project_dir: str | Path | None = None,
) -> None:
    """Configure root logger with console and file handlers.

    Safe to call multiple times — subsequent calls are no-ops unless
    the module-level ``_configured`` flag is reset (e.g. in tests).
    """
    global _configured
    if _configured:
        return
    _configured = True

    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(console)

    # File handler — resolve log directory
    if project_dir is None:
        project_dir = os.environ.get("BATON_PROJECT_DIR", os.getcwd())
    log_dir = Path(project_dir) / LOG_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_FILE_NAME

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)


def create_task_handler(
    task_id: str,
    project_dir: str | Path | None = None,
    level: str | int = "INFO",
) -> logging.FileHandler:
    """Create a file handler that writes to ``logs/{task_id}.log``.

    Attach to the root logger at the start of task execution and remove
    in the ``finally`` block so each task gets its own log file.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    if project_dir is None:
        project_dir = os.environ.get("BATON_PROJECT_DIR", os.getcwd())
    log_dir = Path(project_dir) / LOG_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{task_id}.log"

    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def reset() -> None:
    """Reset configuration flag — for testing only."""
    global _configured
    _configured = False
