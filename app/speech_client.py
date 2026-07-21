from __future__ import annotations

from app.openai_client import OpenAIClient


class SpeechClient:
    def __init__(self, openai_client: OpenAIClient) -> None:
        self.openai_client = openai_client

    async def transcribe(self, audio_path: str, vocabulary_hint: str = "") -> str:
        prompt = ""
        if vocabulary_hint:
            prompt = f"可能出现的专有名词：{vocabulary_hint}"
        return await self.openai_client.transcribe_audio(audio_path, prompt=prompt)
