from __future__ import annotations

from pathlib import Path
from typing import Any

from app.openai_client import OpenAIClient


class TaskParser:
    def __init__(self, openai_client: OpenAIClient, prompt_path: Path) -> None:
        self.openai_client = openai_client
        self.prompt_template = prompt_path.read_text(encoding="utf-8")

    async def parse(self, user_input: str, date_text: str) -> dict[str, Any]:
        prompt = (
            self.prompt_template.replace("{{user_input}}", user_input).replace("{{date}}", date_text)
        )
        return await self.openai_client.generate_json(prompt)
