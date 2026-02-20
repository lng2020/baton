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
        logger.warning("chat_stream called with no message content")
        yield f"data: {json.dumps({'type': 'error', 'error': 'No message provided'})}\n\n"
        return

    cmd = _build_chat_command(system, session_id)
    logger.info("chat_stream starting: session_id=%s, cmd=%s", session_id, cmd)
    logger.debug("chat_stream prompt (first 200 chars): %.200s", last_message)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,  # 10 MB — CC stream-json can emit very long lines
        )
        logger.info("chat_stream subprocess started: pid=%s", proc.pid)

        # Send the message via stdin and close it
        proc.stdin.write(last_message.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        result_session_id = None
        event_count = 0
        text_chunks_yielded = 0
        total_text_length = 0

        async for line in proc.stdout:
            line = line.decode().strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("chat_stream: non-JSON line from subprocess: %.200s", line)
                continue

            event_count += 1
            event_type = event.get("type", "")
            logger.debug("chat_stream event #%d: type=%s", event_count, event_type)

            # Capture session_id from any event that has it
            if "session_id" in event:
                result_session_id = event["session_id"]

            if event_type == "assistant":
                # Extract text content from assistant message
                message_data = event.get("message", {})
                content_blocks = message_data.get("content", [])
                logger.debug(
                    "chat_stream assistant event: %d content blocks, stop_reason=%s",
                    len(content_blocks),
                    message_data.get("stop_reason"),
                )
                for block in content_blocks:
                    block_type = block.get("type")
                    if block_type == "text":
                        text = block.get("text", "")
                        if text:
                            text_chunks_yielded += 1
                            total_text_length += len(text)
                            yield f"data: {json.dumps({'type': 'text', 'text': text})}\n\n"
                        else:
                            logger.warning(
                                "chat_stream: assistant text block with empty content"
                            )
                    elif block_type == "tool_use":
                        logger.debug(
                            "chat_stream: assistant tool_use block: %s",
                            block.get("name", "unknown"),
                        )
                    else:
                        logger.debug(
                            "chat_stream: assistant block type=%s (not text)", block_type
                        )

            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        text_chunks_yielded += 1
                        total_text_length += len(text)
                        yield f"data: {json.dumps({'type': 'text', 'text': text})}\n\n"
                    else:
                        logger.debug("chat_stream: content_block_delta with empty text")
                else:
                    logger.debug(
                        "chat_stream: content_block_delta type=%s (not text_delta)",
                        delta.get("type"),
                    )

            elif event_type == "result":
                # Final result event — may contain session_id
                if "session_id" in event:
                    result_session_id = event["session_id"]
                logger.info(
                    "chat_stream result event: session_id=%s, cost=$%.4f",
                    event.get("session_id"),
                    event.get("cost_usd", 0),
                )

            elif event_type:
                logger.debug("chat_stream: unhandled event type=%s", event_type)

        await proc.wait()
        logger.info(
            "chat_stream subprocess exited: returncode=%s, events=%d, "
            "text_chunks=%d, total_text_len=%d",
            proc.returncode,
            event_count,
            text_chunks_yielded,
            total_text_length,
        )

        if total_text_length == 0:
            logger.warning(
                "chat_stream: completed with ZERO text output "
                "(events=%d, returncode=%s) — plan mode may show blank content",
                event_count,
                proc.returncode,
            )

        if proc.returncode != 0:
            stderr = await proc.stderr.read()
            error_msg = stderr.decode().strip() if stderr else f"Claude Code exited with code {proc.returncode}"
            logger.error("chat_stream subprocess failed: %s", error_msg)
            yield f"data: {json.dumps({'type': 'error', 'error': error_msg})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'done', 'session_id': result_session_id})}\n\n"

    except FileNotFoundError:
        logger.error("Claude Code CLI not found on PATH")
        yield f"data: {json.dumps({'type': 'error', 'error': 'Claude Code CLI not found. Install it with: npm install -g @anthropic-ai/claude-code'})}\n\n"
    except Exception as e:
        logger.error("Chat stream error: %s", e, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"


def _extract_json(text: str) -> dict:
    """Extract JSON from response text, handling markdown code fences."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    return json.loads(text)
