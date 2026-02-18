"""
Voice Input -> Task Creation
Records audio, transcribes via speech recognition API,
and creates a task file in tasks/pending/.
"""

import os
import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

TASKS_DIR = Path("/workspace/tasks/pending")


def sanitize_filename(text: str) -> str:
    """Convert text to a safe filename."""
    # Take first 50 chars, replace non-alphanumeric with hyphens
    clean = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", text[:50]).strip("-").lower()
    return clean or "untitled"


def get_next_task_id() -> str:
    """Generate the next task ID based on existing tasks."""
    existing = list(TASKS_DIR.parent.rglob("*.md"))
    numbers = []
    for f in existing:
        match = re.match(r"^(\d+)", f.stem)
        if match:
            numbers.append(int(match.group(1)))
    next_num = max(numbers, default=0) + 1
    return f"{next_num:03d}"


def create_task_from_text(text: str) -> Path:
    """Create a task file from transcribed text."""
    task_id = get_next_task_id()
    filename_hint = sanitize_filename(text)
    filename = f"{task_id}-{filename_hint}.md"

    task_path = TASKS_DIR / filename
    task_path.write_text(text, encoding="utf-8")

    logger.info(f"Task created: {task_path}")
    return task_path


def transcribe_with_deepgram(audio_path: str, api_key: str) -> str:
    """Transcribe audio using Deepgram API."""
    try:
        import httpx
    except ImportError:
        raise ImportError("httpx is required: pip install httpx")

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    response = httpx.post(
        "https://api.deepgram.com/v1/listen",
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "audio/wav",
        },
        content=audio_data,
        params={"model": "general", "language": "zh"},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()

    transcript = (
        result.get("results", {})
        .get("channels", [{}])[0]
        .get("alternatives", [{}])[0]
        .get("transcript", "")
    )
    return transcript


def transcribe_with_whisper(audio_path: str, api_key: str) -> str:
    """Transcribe audio using OpenAI Whisper API."""
    try:
        import httpx
    except ImportError:
        raise ImportError("httpx is required: pip install httpx")

    with open(audio_path, "rb") as f:
        response = httpx.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("audio.wav", f, "audio/wav")},
            data={"model": "whisper-1"},
            timeout=30,
        )
    response.raise_for_status()
    return response.json().get("text", "")


def process_voice_input(
    audio_path: str, provider: str = "deepgram", api_key: str = ""
) -> Path:
    """Process voice input: transcribe audio and create a task."""
    if not api_key:
        api_key = os.environ.get("VOICE_API_KEY", "")
    if not api_key:
        raise ValueError("Voice API key is required")

    if provider == "deepgram":
        text = transcribe_with_deepgram(audio_path, api_key)
    elif provider == "whisper":
        text = transcribe_with_whisper(audio_path, api_key)
    else:
        raise ValueError(f"Unsupported voice provider: {provider}")

    if not text.strip():
        raise ValueError("Transcription returned empty text")

    return create_task_from_text(text)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python voice_input.py <audio_file> [provider]")
        sys.exit(1)

    audio_file = sys.argv[1]
    provider = sys.argv[2] if len(sys.argv) > 2 else "deepgram"

    task = process_voice_input(audio_file, provider)
    print(f"Task created: {task}")
