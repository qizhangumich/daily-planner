from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from notion_client import AsyncClient as AsyncNotionClient

from app.config import Settings


logger = logging.getLogger(__name__)


class NotionClientError(Exception):
    """Raised when Notion operations fail."""


class NotionDailyRecordsClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AsyncNotionClient(auth=settings.notion_token)
        self.database_id = settings.notion_daily_database_id

    async def find_record_by_date(self, record_date: str) -> Optional[dict[str, Any]]:
        try:
            response = await self.client.databases.query(
                database_id=self.database_id,
                filter={"property": "Date", "date": {"equals": record_date}},
                page_size=1,
            )
            results = response.get("results", [])
            return results[0] if results else None
        except Exception as exc:  # noqa: BLE001
            logger.exception("Notion query failed")
            raise NotionClientError(f"Notion query failed: {exc}") from exc

    async def create_daily_record(self, record_date: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self.client.pages.create(
                parent={"database_id": self.database_id},
                properties=self._build_properties(record_date, payload),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Notion create page failed")
            raise NotionClientError(f"Notion create page failed: {exc}") from exc

    async def update_daily_record(self, page_id: str, record_date: str, payload: dict[str, Any]) -> None:
        try:
            await self.client.pages.update(
                page_id=page_id,
                properties=self._build_properties(record_date, payload),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Notion update page failed")
            raise NotionClientError(f"Notion update page failed: {exc}") from exc

    async def unarchive_page(self, page_id: str) -> None:
        try:
            await self.client.pages.update(page_id=page_id, archived=False)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Notion unarchive page failed")
            raise NotionClientError(f"Notion unarchive page failed: {exc}") from exc

    async def is_page_archived(self, page_id: str) -> bool:
        try:
            page = await self.client.pages.retrieve(page_id=page_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Notion retrieve page state failed")
            raise NotionClientError(f"Notion retrieve page state failed: {exc}") from exc

        return bool(page.get("archived") or page.get("in_trash"))

    async def get_payload_from_page(self, page_id: str) -> dict[str, Any]:
        try:
            page = await self.client.pages.retrieve(page_id=page_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Notion retrieve page failed")
            raise NotionClientError(f"Notion retrieve page failed: {exc}") from exc

        tasks_text = self._extract_rich_text(page["properties"].get("Tasks", {}).get("rich_text", []))
        if tasks_text.strip().startswith("{"):
            try:
                return json.loads(tasks_text)
            except json.JSONDecodeError:
                logger.warning("Legacy JSON tasks payload is invalid; using empty payload fallback.")

        date_value = (
            page["properties"].get("Date", {}).get("date", {}) or {}
        ).get("start") or datetime.utcnow().date().isoformat()
        return self._empty_payload(date_value)

    def _build_properties(self, record_date: str, payload: dict[str, Any]) -> dict[str, Any]:
        review = payload.get("review", {})
        title = f"Daily Record - {record_date}"
        completion_score = review.get("completion_score")
        review_status = payload.get("review_status", "Not Started")

        return {
            "Title": {"title": self._build_rich_text(title)},
            "Date": {"date": {"start": record_date}},
            "Tasks": {"rich_text": self._build_rich_text(self._format_tasks(payload.get("tasks", [])))},
            "Review Status": {"select": {"name": review_status}},
            "Completion Score": {"number": completion_score if completion_score is not None else 0},
            "Daily Summary": {"rich_text": self._build_rich_text(self._format_daily_summary(payload))},
            "Carry Over Tasks": {
                "rich_text": self._build_rich_text(self._format_list(review.get("carry_over_tasks", [])))
            },
            "Last Updated": {"date": {"start": payload.get("last_updated") or self._iso_now()}},
        }

    @staticmethod
    def _build_rich_text(content: str) -> list[dict[str, Any]]:
        if not content:
            return []
        chunk_size = 1800
        return [
            {"type": "text", "text": {"content": content[index : index + chunk_size]}}
            for index in range(0, len(content), chunk_size)
        ]

    @staticmethod
    def _join_lines(values: list[str]) -> str:
        return "\n".join(value for value in values if value).strip()

    @staticmethod
    def _iso_now() -> str:
        return datetime.utcnow().isoformat()

    @staticmethod
    def _extract_rich_text(items: list[dict[str, Any]]) -> str:
        return "".join(item.get("plain_text", "") for item in items)

    @staticmethod
    def _format_tasks(tasks: list[dict[str, Any]]) -> str:
        if not tasks:
            return "No tasks recorded yet."

        lines: list[str] = []
        for index, task in enumerate(tasks, start=1):
            title = task.get("title", "Untitled Task")
            notes = (task.get("notes") or "").strip()

            lines.append(f"{index}. {title}")
            if notes:
                lines.append(f"   {notes}")
            lines.append("")

        return "\n".join(lines).strip()

    @classmethod
    def _format_review_summary(cls, review: dict[str, Any]) -> str:
        if not review:
            return ""

        lines: list[str] = []
        completion_score = review.get("completion_score")
        summary = (review.get("summary") or "").strip()
        suggestion = (review.get("suggestion_for_tomorrow") or "").strip()

        if completion_score is not None:
            lines.append(f"Completion Score: {completion_score}%")
        if summary:
            lines.append(f"Overall Summary: {summary}")

        completed = review.get("completed_tasks", [])
        unfinished = review.get("unfinished_tasks", [])
        blockers = review.get("blockers", [])
        carry_over = review.get("carry_over_tasks", [])

        if completed:
            lines.append("")
            lines.append("Completed Tasks:")
            lines.append(cls._format_list(completed))
        if unfinished:
            lines.append("")
            lines.append("Unfinished Tasks:")
            lines.append(cls._format_list(unfinished))
        if blockers:
            lines.append("")
            lines.append("Blockers:")
            lines.append(cls._format_list(blockers))
        if carry_over:
            lines.append("")
            lines.append("Carry Over Tasks:")
            lines.append(cls._format_list(carry_over))
        if suggestion:
            lines.append("")
            lines.append(f"Suggestion for Tomorrow: {suggestion}")

        return "\n".join(lines).strip()

    @classmethod
    def _format_daily_summary(cls, payload: dict[str, Any]) -> str:
        review = payload.get("review", {})
        reflection = payload.get("reflection", {})
        review_input = (payload.get("review_input") or "").strip()
        reflection_input = (payload.get("reflection_input") or "").strip()

        sections: list[str] = []

        if review_input:
            sections.append("Review Input")
            sections.append(review_input)

        review_summary = cls._format_review_summary(review)
        if review_summary:
            sections.append("")
            sections.append("Review Summary")
            sections.append(review_summary)

        if reflection_input:
            sections.append("")
            sections.append("Reflection Input")
            sections.append(reflection_input)

        reflection_summary = cls._format_reflection_summary(reflection)
        if reflection_summary:
            sections.append("")
            sections.append("Reflection Summary")
            sections.append(reflection_summary)

        return "\n".join(section for section in sections if section is not None).strip()

    @staticmethod
    def _format_reflection_summary(reflection: dict[str, Any]) -> str:
        if not reflection:
            return ""

        mapping = [
            ("Feeling", reflection.get("feeling", "")),
            ("Insight", reflection.get("insight", "")),
            ("Problem", reflection.get("problem", "")),
            ("Improvement", reflection.get("improvement", "")),
            ("Summary", reflection.get("reflection_summary", "")),
        ]
        lines = [f"{label}: {str(value).strip()}" for label, value in mapping if str(value).strip()]
        return "\n".join(lines).strip()

    @staticmethod
    def _format_list(items: list[str]) -> str:
        if not items:
            return ""
        return "\n".join(f"- {item}" for item in items if item)

    @staticmethod
    def _empty_payload(record_date: str) -> dict[str, Any]:
        return {
            "date": record_date,
            "tasks": [],
            "review": {
                "completion_score": 0,
                "summary": "",
                "task_reviews": [],
                "completed_tasks": [],
                "unfinished_tasks": [],
                "blockers": [],
                "carry_over_tasks": [],
                "suggestion_for_tomorrow": "",
            },
            "reflection": {
                "feeling": "",
                "insight": "",
                "problem": "",
                "improvement": "",
                "reflection_summary": "",
            },
            "raw_inputs": [],
            "review_input": "",
            "reflection_input": "",
            "sources": [],
            "review_status": "Not Started",
            "last_updated": datetime.utcnow().isoformat(),
        }
