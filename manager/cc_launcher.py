"""
Claude Code Instance Launcher
Provides utilities to launch and manage individual Claude Code processes.
"""

import subprocess
import json
import logging
from pathlib import Path
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LaunchConfig:
    """Configuration for launching a Claude Code instance."""

    working_dir: str
    prompt: str
    skip_permissions: bool = True
    output_format: str = "stream-json"
    verbose: bool = True
    timeout: int = 600
    plan_mode: bool = False


def build_command(config: LaunchConfig) -> list[str]:
    """Build the Claude Code CLI command from config."""
    cmd = ["claude"]

    if config.plan_mode:
        cmd.extend(["--plan"])

    cmd.extend(["-p", config.prompt])
    cmd.extend(["--output-format", config.output_format])

    if config.verbose:
        cmd.append("--verbose")

    if config.skip_permissions:
        cmd.append("--dangerously-skip-permissions")

    return cmd


def launch_instance(config: LaunchConfig) -> subprocess.Popen:
    """Launch a Claude Code instance with the given configuration."""
    cmd = build_command(config)

    logger.info(f"Launching Claude Code in {config.working_dir}")
    logger.debug(f"Command: {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        cwd=config.working_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    return proc


def stream_events(proc: subprocess.Popen):
    """Generator that yields parsed JSON events from a Claude Code process."""
    for line in proc.stdout:
        line = line.decode().strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            yield event
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON: {line}")


def wait_for_completion(proc: subprocess.Popen, timeout: int = 600) -> int:
    """Wait for a Claude Code process to complete and return exit code."""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("Process timed out, terminating...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return proc.returncode
