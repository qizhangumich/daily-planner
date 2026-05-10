from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.openai_client import OpenAIClient


class ReviewParser:
    def __init__(self, openai_client: OpenAIClient, prompt_path: Path) -> None:
        self.openai_client = openai_client
        self.prompt_template = prompt_path.read_text(encoding="utf-8")

    async def parse(self, tasks_payload: dict[str, Any], review_input: str) -> dict[str, Any]:
        prompt = self.prompt_template.replace(
            "{{tasks_json}}",
            json.dumps(tasks_payload.get("tasks", []), ensure_ascii=False, indent=2),
        ).replace("{{review_input}}", review_input)
        return await self.openai_client.generate_json(prompt)
