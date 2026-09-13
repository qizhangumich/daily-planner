from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from notion_client import AsyncClient

from app.config import Settings


logger = logging.getLogger(__name__)

AGGREGATE_HINTS = ("全部", "all done", "全完成")


class RitualsError(Exception):
    """Raised when the rituals checklist cannot be read or updated."""


class RitualsService:
    """Daily habit checklist backed by a Notion database (one row per day,
    one checkbox property per ritual, plus an optional 'all done' aggregate)."""

    def __init__(self, settings: Settings) -> None:
        self.database_id = settings.notion_rituals_database_id
        self.client = AsyncClient(auth=settings.notion_token)
        self.timezone = ZoneInfo(settings.timezone)
        self._date_prop: Optional[str] = None
        self._ritual_props: list[str] = []
        self._aggregate_prop: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.database_id)

    def today(self) -> str:
        return datetime.now(self.timezone).date().isoformat()

    async def _load_schema(self) -> None:
        try:
            database = await self.client.databases.retrieve(database_id=self.database_id)
        except Exception as exc:  # noqa: BLE001
            raise RitualsError(
                "无法读取打卡数据库。请确认已在 Notion 中把它连接到集成："
                "打开数据库 → ⋯ → Connections → Daily Task Telegram Bot"
            ) from exc
        date_prop = None
        rituals: list[str] = []
        aggregate = None
        for name, prop in database.get("properties", {}).items():
            if prop.get("type") == "date" and date_prop is None:
                date_prop = name
            elif prop.get("type") == "checkbox":
                if any(hint in name.lower() for hint in AGGREGATE_HINTS):
                    aggregate = name
                else:
                    rituals.append(name)
        if not rituals:
            raise RitualsError("打卡数据库里没有找到 checkbox 属性。")
        self._date_prop = date_prop
        self._ritual_props = sorted(rituals)
        self._aggregate_prop = aggregate

    async def ensure_schema(self) -> None:
        if not self._ritual_props:
            await self._load_schema()

    @property
    def rituals(self) -> list[str]:
        return self._ritual_props

    async def _find_row(self, date_iso: str) -> Optional[dict[str, Any]]:
        if self._date_prop:
            response = await self.client.databases.query(
                database_id=self.database_id,
                filter={"property": self._date_prop, "date": {"equals": date_iso}},
                page_size=3,
            )
            for page in response.get("results", []):
                actual = (
                    (page["properties"].get(self._date_prop, {}).get("date") or {}).get("start") or ""
                )[:10]
                if actual == date_iso:  # never trust cross-timezone filter matches
                    return page
        return None

    async def _create_row(self, date_iso: str) -> dict[str, Any]:
        day = datetime.strptime(date_iso, "%Y-%m-%d")
        title = day.strftime("%b %d · %a")  # matches existing rows like "Aug 09 · Sun"
        properties: dict[str, Any] = {}
        # Find the title property name from the schema.
        database = await self.client.databases.retrieve(database_id=self.database_id)
        for name, prop in database.get("properties", {}).items():
            if prop.get("type") == "title":
                properties[name] = {"title": [{"type": "text", "text": {"content": title}}]}
                break
        if self._date_prop:
            properties[self._date_prop] = {"date": {"start": date_iso}}
        return await self.client.pages.create(
            parent={"database_id": self.database_id}, properties=properties
        )

    async def today_row(self) -> tuple[str, dict[str, bool]]:
        """(page_id, {ritual: checked}) for today, creating the row if needed."""
        await self.ensure_schema()
        date_iso = self.today()
        try:
            page = await self._find_row(date_iso) or await self._create_row(date_iso)
        except RitualsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RitualsError(f"读取今天的打卡记录失败：{exc}") from exc
        return page["id"], self._checks_from(page)

    def _checks_from(self, page: dict[str, Any]) -> dict[str, bool]:
        properties = page.get("properties", {})
        return {
            name: bool(properties.get(name, {}).get("checkbox"))
            for name in self._ritual_props
        }

    async def toggle(self, page_id: str, ritual: str) -> dict[str, bool]:
        """Flip one ritual and keep the aggregate in sync; returns fresh states."""
        await self.ensure_schema()
        try:
            page = await self.client.pages.retrieve(page_id=page_id)
            checks = self._checks_from(page)
            if ritual not in checks:
                raise RitualsError(f"没有找到打卡项：{ritual}")
            checks[ritual] = not checks[ritual]
            updates: dict[str, Any] = {ritual: {"checkbox": checks[ritual]}}
            if self._aggregate_prop:
                updates[self._aggregate_prop] = {"checkbox": all(checks.values())}
            await self.client.pages.update(page_id=page_id, properties=updates)
            return checks
        except RitualsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RitualsError(f"更新打卡失败：{exc}") from exc
