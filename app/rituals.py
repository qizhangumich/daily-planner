from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from notion_client import AsyncClient

from app.config import Settings


logger = logging.getLogger(__name__)

# The active ritual set, in display order; matched as substrings against the
# database's checkbox property names (so emoji prefixes in Notion don't matter).
RITUAL_KEYWORDS = ["锻炼", "日记", "健康饮食", "体态管理"]
NETWORK_KEYWORD = "人脉"


class RitualsError(Exception):
    """Raised when the rituals checklist cannot be read or updated."""


class RitualsService:
    """Daily rituals backed by a Notion database: one row per day,
    four checkbox rituals plus a free-text 人脉 field; the aggregate
    checkbox flips when all four are checked AND 人脉 is filled."""

    def __init__(self, settings: Settings) -> None:
        self.database_id = settings.notion_rituals_database_id
        self.client = AsyncClient(auth=settings.notion_token)
        self.timezone = ZoneInfo(settings.timezone)
        self._date_prop: Optional[str] = None
        self._title_prop: Optional[str] = None
        self._ritual_props: list[str] = []
        self._network_prop: Optional[str] = None
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
        properties = database.get("properties", {})
        checkboxes = {
            name for name, prop in properties.items()
            if prop.get("type") == "checkbox" and not name.strip().startswith("↳")
        }
        rituals: list[str] = []
        for keyword in RITUAL_KEYWORDS:
            match = next(
                (name for name in checkboxes if keyword in name and "全部完成" not in name), None
            )
            if match:
                rituals.append(match)
        if not rituals:
            raise RitualsError("打卡数据库里没有找到对应的打卡项。")
        self._ritual_props = rituals
        self._aggregate_prop = next(
            (name for name in checkboxes if "全部完成" in name and "Workout" not in name), None
        )
        self._network_prop = next(
            (name for name, prop in properties.items()
             if prop.get("type") == "rich_text" and NETWORK_KEYWORD in name), None
        )
        self._date_prop = next(
            (name for name, prop in properties.items() if prop.get("type") == "date"), None
        )
        self._title_prop = next(
            (name for name, prop in properties.items() if prop.get("type") == "title"), None
        )

    async def ensure_schema(self) -> None:
        if not self._ritual_props:
            await self._load_schema()

    @property
    def rituals(self) -> list[str]:
        return self._ritual_props

    @property
    def has_network_field(self) -> bool:
        return self._network_prop is not None

    async def _find_row(self, date_iso: str) -> Optional[dict[str, Any]]:
        if not self._date_prop:
            return None
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
        properties: dict[str, Any] = {}
        if self._title_prop:
            properties[self._title_prop] = {
                "title": [{"type": "text", "text": {"content": day.strftime("%b %d · %a")}}]
            }
        if self._date_prop:
            properties[self._date_prop] = {"date": {"start": date_iso}}
        return await self.client.pages.create(
            parent={"database_id": self.database_id}, properties=properties
        )

    async def today_row(self) -> tuple[str, dict[str, bool], str]:
        """(page_id, {ritual: checked}, network_text) for today; row created on demand."""
        await self.ensure_schema()
        date_iso = self.today()
        try:
            page = await self._find_row(date_iso) or await self._create_row(date_iso)
        except RitualsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RitualsError(f"读取今天的打卡记录失败：{exc}") from exc
        return page["id"], self._checks_from(page), self._network_from(page)

    def _checks_from(self, page: dict[str, Any]) -> dict[str, bool]:
        properties = page.get("properties", {})
        return {
            name: bool(properties.get(name, {}).get("checkbox"))
            for name in self._ritual_props
        }

    def _network_from(self, page: dict[str, Any]) -> str:
        if not self._network_prop:
            return ""
        items = page.get("properties", {}).get(self._network_prop, {}).get("rich_text", [])
        return "".join(item.get("plain_text", "") for item in items).strip()

    def _aggregate_value(self, checks: dict[str, bool], network_text: str) -> bool:
        complete = all(checks.values())
        if self._network_prop:
            complete = complete and bool(network_text)
        return complete

    async def _apply(self, page_id: str, updates: dict[str, Any]) -> tuple[dict[str, bool], str]:
        page = await self.client.pages.retrieve(page_id=page_id)
        checks = self._checks_from(page)
        network_text = self._network_from(page)
        for name, value in updates.items():
            if name in checks:
                checks[name] = bool(value["checkbox"])
            elif name == self._network_prop:
                network_text = "".join(
                    part["text"]["content"] for part in value["rich_text"]
                ).strip()
        if self._aggregate_prop:
            updates[self._aggregate_prop] = {
                "checkbox": self._aggregate_value(checks, network_text)
            }
        await self.client.pages.update(page_id=page_id, properties=updates)
        return checks, network_text

    async def toggle(self, page_id: str, ritual: str) -> tuple[dict[str, bool], str]:
        await self.ensure_schema()
        try:
            page = await self.client.pages.retrieve(page_id=page_id)
            checks = self._checks_from(page)
            if ritual not in checks:
                raise RitualsError(f"没有找到打卡项：{ritual}")
            return await self._apply(page_id, {ritual: {"checkbox": not checks[ritual]}})
        except RitualsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RitualsError(f"更新打卡失败：{exc}") from exc

    async def set_many(self, page_id: str, names: list[str], value: bool = True) -> tuple[dict[str, bool], str]:
        await self.ensure_schema()
        try:
            updates = {name: {"checkbox": value} for name in names if name in self._ritual_props}
            return await self._apply(page_id, updates)
        except Exception as exc:  # noqa: BLE001
            raise RitualsError(f"更新打卡失败：{exc}") from exc

    async def set_network_text(self, page_id: str, text: str) -> tuple[dict[str, bool], str]:
        """Append today's 人脉 note (keeps earlier entries, joined by '；')."""
        await self.ensure_schema()
        if not self._network_prop:
            raise RitualsError("打卡数据库里没有 人脉 字段。")
        try:
            page = await self.client.pages.retrieve(page_id=page_id)
            existing = self._network_from(page)
            combined = f"{existing}；{text.strip()}" if existing else text.strip()
            return await self._apply(
                page_id,
                {self._network_prop: {"rich_text": [{"type": "text", "text": {"content": combined[:1900]}}]}},
            )
        except RitualsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RitualsError(f"保存人脉失败：{exc}") from exc

    def match_rituals(self, text: str) -> list[str]:
        """Rituals whose (emoji-stripped) name appears in the text."""
        matches = []
        compact = text.lower().replace(" ", "")
        for name in self._ritual_props:
            core = "".join(ch for ch in name if ch.isalnum() or "一" <= ch <= "鿿").strip()
            if core and core.lower() in compact:
                matches.append(name)
        return matches
