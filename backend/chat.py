"""Chat service using Claude Code CLI subprocess.

Handles streaming chat via the Claude Code CLI for the discussion-first
task creation workflow. Uses --resume for multi-turn conversations.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a task planner for the project "{project_name}".
{project_description}

IMPORTANT: You are running inside a task management system. Your job is to \
help the user break down their request into one or more tasks that will be \
dispatched to separate Claude Code agents for execution. Do NOT implement \
changes yourself — instead, explore the codebase to understand it, then \
propose tasks.

Your workflow:
1. Read the user's request
2. Explore relevant files to understand the codebase structure
3. Ask clarifying questions if needed
4. Propose a plan as structured tasks

When you are ready to propose tasks, you MUST output this JSON block:

```json
{{
  "plan": true,
  "summary": "Brief description of the overall plan",
  "tasks": [
    {{"title": "Short task title", "content": "Detailed task description with specific files to modify and what to change"}},
    ...
  ]
}}
```

Rules for tasks:
- Each task title must be under 80 chars
- Each task content must be detailed enough for an autonomous Claude Code \
agent to execute without further context
- Include specific file paths, function names, and expected behavior
- Do NOT make code changes yourself — only propose tasks
- Output the plan JSON as soon as you have enough understanding\
"""


def build_system_prompt(project_name: str, project_description: str = "") -> str:
    desc = f"Project description: {project_description}" if project_description else ""
    return SYSTEM_PROMPT.format(
        project_name=project_name,
        project_description=desc,
    )


def _build_chat_command(
    system: str,
    session_id: str | None = None,
) -> list[str]:
    """Build the claude CLI command for chat."""
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.extend(["--append-system-prompt", system])
    return cmd


async def chat_stream(
    messages: list[dict],
    system: str,
    session_id: str | None = None,
) -> AsyncIterator[str]:
    """Stream chat response as SSE-formatted text chunks.

    Spawns a Claude Code CLI subprocess with stream-json output.
    The last user message is sent as the prompt via stdin.
    For multi-turn, pass session_id to use --resume.
    """
    last_message = messages[-1]["content"] if messages else ""
    if not last_message:
        yield f"data: {json.dumps({'type': 'error', 'error': 'No message provided'})}\n\n"
        return

    cmd = _build_chat_command(system, session_id)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Send the message via stdin and close it
        proc.stdin.write(last_message.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        result_session_id = None

        async for line in proc.stdout:
            line = line.decode().strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            # Capture session_id from any event that has it
            if "session_id" in event:
                result_session_id = event["session_id"]

            if event_type == "assistant":
                # Extract text content from assistant message
                message_data = event.get("message", {})
                content_blocks = message_data.get("content", [])
                for block in content_blocks:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            yield f"data: {json.dumps({'type': 'text', 'text': text})}\n\n"

            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield f"data: {json.dumps({'type': 'text', 'text': text})}\n\n"

            elif event_type == "result":
                # Final result event — may contain session_id
                if "session_id" in event:
                    result_session_id = event["session_id"]

        await proc.wait()

        if proc.returncode != 0:
            stderr = await proc.stderr.read()
            error_msg = stderr.decode().strip() if stderr else f"Claude Code exited with code {proc.returncode}"
            yield f"data: {json.dumps({'type': 'error', 'error': error_msg})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'done', 'session_id': result_session_id})}\n\n"

    except FileNotFoundError:
        yield f"data: {json.dumps({'type': 'error', 'error': 'Claude Code CLI not found. Install it with: npm install -g @anthropic-ai/claude-code'})}\n\n"
    except Exception as e:
        logger.error(f"Chat stream error: {e}")
        yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"


def _extract_json(text: str) -> dict:
    """Extract JSON from response text, handling markdown code fences."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    return json.loads(text)
