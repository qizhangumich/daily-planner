from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.notion_client import NotionClientError, NotionDailyRecordsClient
from app.openai_client import OpenAIClient
from app.reflection_parser import ReflectionParser
from app.review_parser import ReviewParser
from app.storage import Storage
from app.task_parser import TaskParser


logger = logging.getLogger(__name__)


class DailyRecordServiceError(Exception):
    """Raised when the daily record workflow fails."""


class DailyRecordService:
    def __init__(
        self,
        notion_client: NotionDailyRecordsClient,
        task_parser: TaskParser,
        review_parser: ReviewParser,
        reflection_parser: ReflectionParser,
        storage: Storage,
        timezone_name: str,
        openai_client: Optional[OpenAIClient] = None,
        prompts_dir: Optional[Path] = None,
    ) -> None:
        self.notion_client = notion_client
        self.task_parser = task_parser
        self.review_parser = review_parser
        self.reflection_parser = reflection_parser
        self.storage = storage
        self.timezone = ZoneInfo(timezone_name)
        self.openai_client = openai_client
        self.prompts_dir = prompts_dir

    def glossary_text(self) -> str:
        return "、".join(self.storage.list_glossary_terms())

    def today(self) -> str:
        return datetime.now(self.timezone).date().isoformat()

    def now_iso(self) -> str:
        return datetime.now(self.timezone).isoformat()

    async def parse_tasks(self, user_input: str) -> dict[str, Any]:
        try:
            return await self.task_parser.parse(
                user_input=user_input,
                date_text=self.today(),
                glossary_text=self.glossary_text(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Task parsing failed")
            raise DailyRecordServiceError(f"Failed to parse task: {exc}") from exc

    async def apply_correction(self, parsed: dict[str, Any], correction: str) -> dict[str, Any]:
        if self.openai_client is None or self.prompts_dir is None:
            raise DailyRecordServiceError("Correction support is not configured.")
        try:
            template = (self.prompts_dir / "apply_correction.md").read_text(encoding="utf-8")
            prompt = template.replace(
                "{{parsed_json}}", json.dumps(parsed, ensure_ascii=False, indent=2)
            ).replace("{{correction}}", correction)
            glossary = self.glossary_text()
            if glossary:
                prompt += f"\n\n已知专有名词表（输出时必须严格使用以下拼写）：\n{glossary}"
            return await self.openai_client.generate_json(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Applying correction failed")
            raise DailyRecordServiceError(f"Failed to apply correction: {exc}") from exc

    async def add_task_to_today(self, user_input: str, source: str) -> dict[str, Any]:
        parsed = await self.parse_tasks(user_input)
        return await self.commit_tasks(parsed, user_input, source)

    async def commit_tasks(self, parsed: dict[str, Any], user_input: str, source: str) -> dict[str, Any]:
        record_date = self.today()
        payload, page_id = await self._load_or_create_payload(record_date)

        new_tasks = []
        existing_count = len(payload["tasks"])
        for index, task in enumerate(parsed.get("tasks", []), start=1):
            new_tasks.append(
                {
                    "id": f"task_{existing_count + index:03d}",
                    "title": task.get("title", "Untitled Task"),
                    "category": task.get("category", "W2"),
                    "status": "Planned",
                    "estimated_time_minutes": task.get("estimated_time_minutes", 0),
                    "created_at": self.now_iso(),
                    "source": source,
                    "notes": task.get("notes", ""),
                }
            )

        if not new_tasks:
            raise DailyRecordServiceError("No tasks were parsed from the input.")

        payload["tasks"].extend(new_tasks)
        payload["sources"] = sorted(set(payload.get("sources", []) + [source]))
        payload["raw_inputs"].append(self._format_raw_input(source, user_input))
        payload["last_updated"] = self.now_iso()
        payload["review_status"] = "Not Started"

        await self._save_payload(record_date, page_id, payload)

        return {
            "summary": parsed.get("summary", "已添加新任务。"),
            "tasks": new_tasks,
            "task_count": len(payload["tasks"]),
        }

    async def get_today_tasks(self) -> list[dict[str, Any]]:
        payload = await self.get_or_create_today_payload()
        return payload.get("tasks", [])

    async def get_or_create_today_payload(self) -> dict[str, Any]:
        record_date = self.today()
        payload, _ = await self._load_or_create_payload(record_date)
        return payload

    @staticmethod
    def _page_review_info(page: dict[str, Any]) -> Optional[tuple[str, bool, bool, bool]]:
        """(record_date, has_tasks, is_reviewed, has_reflection) from a Notion query result."""
        props = page.get("properties", {})
        date_value = ((props.get("Date", {}).get("date") or {}).get("start") or "")[:10]
        if not date_value:
            return None
        status = (props.get("Review Status", {}).get("select") or {}).get("name") or "Not Started"
        tasks_text = "".join(
            item.get("plain_text", "") for item in props.get("Tasks", {}).get("rich_text", [])
        )
        stripped = tasks_text.strip()
        has_tasks = bool(stripped)  # pages store a readable task list, not JSON
        has_reflection = False
        if stripped.startswith("{"):  # legacy JSON payload pages
            try:
                payload = json.loads(stripped)
                has_tasks = bool(payload.get("tasks"))
                has_reflection = bool(payload.get("reflection"))
            except json.JSONDecodeError:
                pass
        return date_value, has_tasks, status == "Reviewed", has_reflection

    async def _recent_day_states(self, lookback_days: int) -> list[tuple[str, bool, bool, bool]]:
        """Newest-first (date, has_tasks, reviewed, has_reflection); local cache wins over Notion."""
        today = self.today()
        start = (date.fromisoformat(today) - timedelta(days=lookback_days)).isoformat()
        try:
            pages = await self.notion_client.find_records_in_range(start, today)
        except NotionClientError as exc:
            raise DailyRecordServiceError(f"Failed to look up recent records: {exc}") from exc

        states: list[tuple[str, bool, bool, bool]] = []
        for page in reversed(pages):  # ascending order -> reversed = newest first
            info = self._page_review_info(page)
            if info is None:
                continue
            record_date, has_tasks, reviewed, has_reflection = info
            cached = self.storage.get_daily_payload(record_date)
            if cached is not None:
                has_tasks = bool(cached.get("tasks"))
                reviewed = cached.get("review_status") == "Reviewed"
                has_reflection = bool(cached.get("reflection"))
            states.append((record_date, has_tasks, reviewed, has_reflection))
        return states

    async def latest_reviewable_date(self, lookback_days: int = 60) -> Optional[str]:
        """Most recent day with recorded tasks that has not been reviewed yet."""
        for record_date, has_tasks, reviewed, _ in await self._recent_day_states(lookback_days):
            if has_tasks and not reviewed:
                return record_date
        return None

    async def latest_reflectable_date(self, lookback_days: int = 60) -> Optional[str]:
        """Most recent reviewed day that has no reflection yet."""
        for record_date, _, reviewed, has_reflection in await self._recent_day_states(lookback_days):
            if reviewed and not has_reflection:
                return record_date
        return None

    async def prepare_evening_review(self, record_date: Optional[str] = None) -> dict[str, Any]:
        record_date = record_date or self.today()
        payload, page_id = await self._load_or_create_payload(record_date)
        payload["review_status"] = "Waiting Review"
        payload["last_updated"] = self.now_iso()
        await self._save_payload(record_date, page_id, payload)
        return payload

    async def parse_review(self, review_input: str, record_date: Optional[str] = None) -> dict[str, Any]:
        record_date = record_date or self.today()
        payload, _ = await self._load_or_create_payload(record_date)
        try:
            return await self.review_parser.parse(
                payload, review_input, glossary_text=self.glossary_text()
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Review parsing failed")
            raise DailyRecordServiceError(f"Failed to parse review: {exc}") from exc

    async def handle_review_input(self, review_input: str, source: str) -> dict[str, Any]:
        review_result = await self.parse_review(review_input)
        return await self.commit_review(review_result, review_input, source)

    async def commit_review(
        self, review_result: dict[str, Any], review_input: str, source: str,
        record_date: Optional[str] = None,
    ) -> dict[str, Any]:
        record_date = record_date or self.today()
        payload, page_id = await self._load_or_create_payload(record_date)

        task_status_map = {
            item.get("task_title"): item.get("status", "Unknown")
            for item in review_result.get("task_reviews", [])
        }
        for task in payload.get("tasks", []):
            task["status"] = task_status_map.get(task["title"], task.get("status", "Planned"))

        payload["review"] = {
            "completion_score": review_result.get("completion_score", 0),
            "summary": review_result.get("overall_summary", ""),
            "task_reviews": review_result.get("task_reviews", []),
            "completed_tasks": review_result.get("completed_tasks", []),
            "unfinished_tasks": review_result.get("unfinished_tasks", []),
            "blockers": review_result.get("blockers", []),
            "carry_over_tasks": review_result.get("carry_over_tasks", []),
            "suggestion_for_tomorrow": review_result.get("suggestion_for_tomorrow", ""),
        }
        payload["review_input"] = review_input
        payload["review_status"] = "Reviewed"
        payload["sources"] = sorted(set(payload.get("sources", []) + [source]))
        payload["raw_inputs"].append(self._format_raw_input(source, review_input))
        payload["last_updated"] = self.now_iso()

        await self._save_payload(record_date, page_id, payload)
        return payload["review"]

    async def parse_reflection(self, reflection_input: str, record_date: Optional[str] = None) -> dict[str, Any]:
        record_date = record_date or self.today()
        payload, _ = await self._load_or_create_payload(record_date)
        try:
            return await self.reflection_parser.parse(
                reflection_input=reflection_input,
                review_summary=payload.get("review", {}).get("summary", ""),
                glossary_text=self.glossary_text(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Reflection parsing failed")
            raise DailyRecordServiceError(f"Failed to parse reflection: {exc}") from exc

    async def handle_reflection_input(self, reflection_input: str, source: str) -> dict[str, Any]:
        reflection = await self.parse_reflection(reflection_input)
        return await self.commit_reflection(reflection, reflection_input, source)

    async def commit_reflection(
        self, reflection: dict[str, Any], reflection_input: str, source: str,
        record_date: Optional[str] = None,
    ) -> dict[str, Any]:
        record_date = record_date or self.today()
        payload, page_id = await self._load_or_create_payload(record_date)

        payload["reflection"] = reflection
        payload["reflection_input"] = reflection_input
        payload["sources"] = sorted(set(payload.get("sources", []) + [source]))
        payload["raw_inputs"].append(self._format_raw_input(source, reflection_input))
        payload["last_updated"] = self.now_iso()

        await self._save_payload(record_date, page_id, payload)
        return reflection

    async def _load_or_create_payload(self, record_date: str) -> tuple[dict[str, Any], str]:
        cached_payload = self.storage.get_daily_payload(record_date)
        cached_page_id = self.storage.get_daily_page_id(record_date)

        if cached_payload and cached_page_id:
            if await self._page_is_active(cached_page_id):
                return cached_payload, cached_page_id
            logger.warning("Cached Notion page %s is no longer active. Rebuilding mapping.", cached_page_id)

        notion_record = await self.notion_client.find_record_by_date(record_date)
        if notion_record:
            page_id = notion_record["id"]
            if await self._page_is_active(page_id):
                payload = await self.notion_client.get_payload_from_page(page_id)
                self.storage.save_daily_payload(record_date, page_id, payload)
                return payload, page_id

            logger.warning("Notion query returned inactive page %s. Creating replacement record.", page_id)
            payload = cached_payload or self._empty_payload(record_date)
            return await self._create_replacement_record(record_date, payload)

        if cached_payload or cached_page_id:
            logger.info(
                "No active Notion record exists for %s. Clearing local cache and starting fresh.",
                record_date,
            )
            self.storage.delete_daily_cache(record_date)

        payload = self._empty_payload(record_date)
        return await self._create_replacement_record(record_date, payload)

    async def _save_payload(self, record_date: str, page_id: str, payload: dict[str, Any]) -> None:
        try:
            await self.notion_client.update_daily_record(page_id, record_date, payload)
        except NotionClientError as exc:
            if self._is_recoverable_page_error(exc):
                logger.warning("Notion page %s could not be updated. Attempting recovery.", page_id)

                try:
                    await self.notion_client.unarchive_page(page_id)
                    await self.notion_client.update_daily_record(page_id, record_date, payload)
                except NotionClientError:
                    logger.warning(
                        "Automatic recovery failed for page %s. Creating a replacement daily record.",
                        page_id,
                    )
                    _, replacement_page_id = await self._create_replacement_record(record_date, payload)
                    logger.info("Replacement Notion page created: %s", replacement_page_id)
                    return

                self.storage.save_daily_payload(record_date, page_id, payload)
                return

            raise DailyRecordServiceError(f"Failed to update Notion daily record: {exc}") from exc

        self.storage.save_daily_payload(record_date, page_id, payload)

    async def _create_replacement_record(
        self,
        record_date: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        try:
            record = await self.notion_client.create_daily_record(record_date, payload)
        except NotionClientError as exc:
            raise DailyRecordServiceError(f"Failed to create daily record in Notion: {exc}") from exc

        page_id = record["id"]
        self.storage.save_daily_payload(record_date, page_id, payload)
        return payload, page_id

    async def _page_is_active(self, page_id: str) -> bool:
        try:
            return not await self.notion_client.is_page_archived(page_id)
        except NotionClientError:
            logger.warning("Failed to validate Notion page %s. Treating it as inactive.", page_id)
            return False

    @staticmethod
    def _is_recoverable_page_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            ("archived" in message and "edit block" in message)
            or "object_not_found" in message
            or "could not find page" in message
            or "page not found" in message
        )

    def _empty_payload(self, record_date: str) -> dict[str, Any]:
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
            "last_updated": self.now_iso(),
        }

    def _format_raw_input(self, source: str, user_input: str) -> str:
        timestamp = datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S")
        return f"[{timestamp}] {source}: {user_input}"
