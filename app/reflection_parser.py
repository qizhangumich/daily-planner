from __future__ import annotations

from pathlib import Path
from typing import Any

from app.openai_client import OpenAIClient


class ReflectionParser:
    def __init__(self, openai_client: OpenAIClient, prompt_path: Path) -> None:
        self.openai_client = openai_client
        self.prompt_template = prompt_path.read_text(encoding="utf-8")

    async def parse(self, reflection_input: str, review_summary: str) -> dict[str, Any]:
        prompt = (
            self.prompt_template.replace("{{reflection_input}}", reflection_input).replace(
                "{{review_summary}}",
                review_summary,
            )
        )
        return await self.openai_client.generate_json(prompt)
