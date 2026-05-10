from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings


logger = logging.getLogger(__name__)


class OpenAIClientError(Exception):
    """Raised when OpenAI API calls fail."""


class OpenAIClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def generate_json(self, prompt: str) -> dict[str, Any]:
        try:
            response = await self.client.chat.completions.create(
                model=self.settings.openai_text_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            text = response.choices[0].message.content or "{}"
            return json.loads(text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OpenAI text generation failed")
            raise OpenAIClientError(f"OpenAI text generation failed: {exc}") from exc

    async def transcribe_audio(self, audio_path: str) -> str:
        try:
            with open(audio_path, "rb") as audio_file:
                transcript = await self.client.audio.transcriptions.create(
                    model=self.settings.openai_audio_model,
                    file=audio_file,
                )
            return transcript.text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.exception("OpenAI transcription failed")
            raise OpenAIClientError(f"OpenAI transcription failed: {exc}") from exc
