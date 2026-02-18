"""
Log Monitor for Claude Code stream-json output.
Parses and aggregates events from running Claude Code instances.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TaskLog:
    """Aggregated log for a single task execution."""

    task_id: str
    events: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    tool_uses: list = field(default_factory=list)
    assistant_messages: list = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def summary(self) -> dict:
        return {
            "task_id": self.task_id,
            "total_events": len(self.events),
            "errors": len(self.errors),
            "tool_uses": len(self.tool_uses),
            "messages": len(self.assistant_messages),
        }


def parse_event(event: dict, task_log: TaskLog):
    """Parse a single stream-json event and update the task log."""
    task_log.events.append(event)
    event_type = event.get("type", "")

    if event_type == "error":
        task_log.errors.append(event)
        logger.error(f"[{task_log.task_id}] Error: {event.get('error', {})}")

    elif event_type == "assistant":
        message = event.get("message", "")
        task_log.assistant_messages.append(message)

    elif event_type == "tool_use":
        tool_name = event.get("tool", "")
        task_log.tool_uses.append(
            {"tool": tool_name, "input": event.get("input", {})}
        )
        logger.debug(f"[{task_log.task_id}] Tool use: {tool_name}")

    elif event_type == "result":
        logger.info(
            f"[{task_log.task_id}] Result: cost=${event.get('cost_usd', 0):.4f}"
        )


def monitor_stream(stream, task_id: str) -> TaskLog:
    """Monitor a stream of JSON lines and return aggregated log."""
    task_log = TaskLog(task_id=task_id)

    for line in stream:
        if isinstance(line, bytes):
            line = line.decode().strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            parse_event(event, task_log)
        except json.JSONDecodeError:
            logger.warning(f"[{task_id}] Failed to parse: {line}")

    return task_log


def save_log(task_log: TaskLog, output_dir: Path):
    """Save task log to a JSON file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / f"{task_log.task_id}.log.json"
    with open(log_file, "w") as f:
        json.dump(
            {
                "summary": task_log.summary,
                "events": task_log.events,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    logger.info(f"Log saved: {log_file}")
