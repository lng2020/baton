"""Chat service for agent engineer discussions.

Handles all Anthropic API interaction for the discussion-first task
creation workflow. The agent engineer helps users plan tasks through
a multi-turn conversation, then proposes a structured plan.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an agent engineer for the project "{project_name}".
{project_description}

Your role is to help the user plan software engineering tasks. You should:
1. Ask clarifying questions to understand what needs to be done
2. Break down large requests into concrete, actionable tasks
3. When you have enough context, propose a plan with concrete tasks

When you are ready to propose a plan, output EXACTLY this JSON block \
(with no other text before or after):

```json
{{
  "plan": true,
  "summary": "Brief description of the overall plan",
  "tasks": [
    {{"title": "Short task title", "content": "Detailed task description"}},
    ...
  ]
}}
```

Keep task titles concise (under 80 chars). Task content should be detailed \
enough for an autonomous agent to execute without further clarification.
Only output the plan JSON when the user agrees or asks you to create the tasks.\
"""


def _get_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic()


def build_system_prompt(project_name: str, project_description: str = "") -> str:
    desc = f"Project description: {project_description}" if project_description else ""
    return SYSTEM_PROMPT.format(
        project_name=project_name,
        project_description=desc,
    )


async def chat_stream(
    messages: list[dict],
    system: str,
    model: str = "claude-sonnet-4-5-20250929",
    max_tokens: int = 4096,
) -> AsyncIterator[str]:
    """Stream chat response as SSE-formatted text chunks."""
    client = _get_client()
    try:
        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                yield f"data: {json.dumps({'type': 'text', 'text': text})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    except anthropic.AuthenticationError:
        yield f"data: {json.dumps({'type': 'error', 'error': 'ANTHROPIC_API_KEY not set or invalid'})}\n\n"
    except anthropic.RateLimitError:
        yield f"data: {json.dumps({'type': 'error', 'error': 'Rate limited, please try again later'})}\n\n"
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


async def chat_plan(
    messages: list[dict],
    system: str,
    model: str = "claude-sonnet-4-5-20250929",
    max_tokens: int = 4096,
) -> dict:
    """Non-streaming call that produces a structured plan."""
    client = _get_client()
    plan_system = system + (
        "\n\nIMPORTANT: You MUST respond with ONLY the JSON plan block now. "
        "The user has confirmed they want to create the tasks."
    )
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=plan_system,
        messages=messages,
    )
    return _extract_json(response.content[0].text)
