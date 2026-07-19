from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.notion_client import NotionClientError, NotionDailyRecordsClient
from app.openai_client import OpenAIClient, OpenAIClientError
from app.storage import Storage


logger = logging.getLogger(__name__)


class WeeklyReportError(Exception):
    """Raised when the weekly report cannot be generated."""


STATUS_COLORS = {
    "Completed": "#16a34a",
    "Partially Completed": "#d97706",
    "Not Completed": "#dc2626",
}
STATUS_LABELS = {
    "Completed": "已完成",
    "Partially Completed": "部分完成",
    "Not Completed": "未完成",
    "Planned": "已计划",
    "Unknown": "未知",
}
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


@dataclass(slots=True)
class DayData:
    record_date: date
    has_record: bool = False
    tasks: list[dict[str, Any]] = field(default_factory=list)
    completion_score: Optional[int] = None
    review_summary: str = ""
    completed_tasks: list[str] = field(default_factory=list)
    unfinished_tasks: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    reflection: dict[str, str] = field(default_factory=dict)
    fallback_text: str = ""


def last_completed_week(today: date) -> tuple[date, date]:
    """Return (monday, sunday) of the most recent completed week.

    Asking on any weekday returns the week that ended on the most recent
    Sunday; asking on a Sunday treats today as that week's end.
    """
    days_since_sunday = (today.weekday() + 1) % 7
    end_sunday = today - timedelta(days=days_since_sunday)
    return end_sunday - timedelta(days=6), end_sunday


class WeeklyReportService:
    def __init__(
        self,
        notion_client: NotionDailyRecordsClient,
        openai_client: OpenAIClient,
        storage: Storage,
        timezone_name: str,
        prompts_dir: Path,
        data_dir: Path,
    ) -> None:
        self.notion_client = notion_client
        self.openai_client = openai_client
        self.storage = storage
        self.timezone = ZoneInfo(timezone_name)
        self.prompts_dir = prompts_dir
        self.data_dir = data_dir

    async def generate(self, today: Optional[date] = None) -> tuple[Path, date, date]:
        if today is None:
            today = datetime.now(self.timezone).date()
        week_start, week_end = last_completed_week(today)

        days = await self._collect_week_data(week_start, week_end)
        ai_summary = await self._generate_ai_summary(days)

        output_path = self.data_dir / f"weekly_report_{week_start.isoformat()}_{week_end.isoformat()}.pdf"
        self._render_pdf(days, week_start, week_end, ai_summary, output_path)
        return output_path, week_start, week_end

    async def _collect_week_data(self, week_start: date, week_end: date) -> list[DayData]:
        try:
            notion_pages = await self.notion_client.find_records_in_range(
                week_start.isoformat(), week_end.isoformat()
            )
        except NotionClientError:
            logger.warning("Notion range query failed; relying on local cache only.")
            notion_pages = []
        pages_by_date = {}
        for page in notion_pages:
            start = ((page.get("properties", {}).get("Date", {}).get("date") or {}).get("start") or "")[:10]
            if start:
                pages_by_date[start] = page

        days: list[DayData] = []
        for offset in range(7):
            record_date = week_start + timedelta(days=offset)
            day = DayData(record_date=record_date)
            payload = self.storage.get_daily_payload(record_date.isoformat())
            if payload:
                self._fill_from_payload(day, payload)
            elif record_date.isoformat() in pages_by_date:
                self._fill_from_notion_page(day, pages_by_date[record_date.isoformat()])
            days.append(day)
        return days

    @staticmethod
    def _fill_from_payload(day: DayData, payload: dict[str, Any]) -> None:
        review = payload.get("review", {}) or {}
        reflection = payload.get("reflection", {}) or {}
        day.tasks = payload.get("tasks", []) or []
        score = review.get("completion_score")
        day.completion_score = int(score) if score is not None else None
        day.review_summary = (review.get("summary") or "").strip()
        day.completed_tasks = [str(item) for item in review.get("completed_tasks", []) if item]
        day.unfinished_tasks = [str(item) for item in review.get("unfinished_tasks", []) if item]
        day.blockers = [str(item) for item in review.get("blockers", []) if item]
        day.reflection = {
            key: str(reflection.get(key, "")).strip()
            for key in ("feeling", "insight", "problem", "improvement", "reflection_summary")
            if str(reflection.get(key, "")).strip()
        }
        day.has_record = bool(day.tasks or day.review_summary or day.reflection)

    @staticmethod
    def _fill_from_notion_page(day: DayData, page: dict[str, Any]) -> None:
        properties = page.get("properties", {})

        def text_of(name: str) -> str:
            return "".join(
                item.get("plain_text", "") for item in properties.get(name, {}).get("rich_text", [])
            ).strip()

        tasks_text = text_of("Tasks")
        if tasks_text and not tasks_text.startswith("{") and "No tasks recorded" not in tasks_text:
            day.tasks = [
                {"title": line.split(". ", 1)[-1].strip(), "status": "Unknown"}
                for line in tasks_text.splitlines()
                if line.strip() and line.strip()[0].isdigit()
            ]
        score = properties.get("Completion Score", {}).get("number")
        day.completion_score = int(score) if score is not None else None
        day.fallback_text = text_of("Daily Summary")
        day.has_record = bool(day.tasks or day.fallback_text)

    async def _generate_ai_summary(self, days: list[DayData]) -> dict[str, Any]:
        recorded = [day for day in days if day.has_record]
        if not recorded:
            return {}

        lines: list[str] = []
        for day in recorded:
            lines.append(f"### {day.record_date.isoformat()} ({WEEKDAY_NAMES[day.record_date.weekday()]})")
            if day.completion_score is not None:
                lines.append(f"完成度: {day.completion_score}%")
            for task in day.tasks:
                lines.append(f"- 任务: {task.get('title', '')} [{task.get('status', 'Unknown')}]")
            if day.review_summary:
                lines.append(f"回顾: {day.review_summary}")
            if day.blockers:
                lines.append("阻碍: " + "; ".join(day.blockers))
            for key, label in (("insight", "收获"), ("improvement", "改进"), ("reflection_summary", "反思")):
                if day.reflection.get(key):
                    lines.append(f"{label}: {day.reflection[key]}")
            if day.fallback_text:
                lines.append(day.fallback_text[:600])
            lines.append("")

        try:
            prompt_template = (self.prompts_dir / "weekly_summary.md").read_text(encoding="utf-8")
            prompt = prompt_template.replace("{{week_data}}", "\n".join(lines))
            return await self.openai_client.generate_json(prompt)
        except (OpenAIClientError, OSError):
            logger.warning("Weekly AI summary generation failed; the report will omit it.")
            return {}

    def _render_pdf(
        self,
        days: list[DayData],
        week_start: date,
        week_end: date,
        ai_summary: dict[str, Any],
        output_path: Path,
    ) -> None:
        from weasyprint import HTML  # imported lazily: heavy native deps

        document = build_report_html(days, week_start, week_end, ai_summary, self.timezone)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        HTML(string=document).write_pdf(str(output_path))


def _esc(value: str) -> str:
    return html.escape(str(value or ""))


def _score_color(score: int) -> str:
    if score >= 80:
        return "#16a34a"
    if score >= 50:
        return "#d97706"
    return "#dc2626"


def _render_day_card(day: DayData) -> str:
    weekday = WEEKDAY_NAMES[day.record_date.weekday()]
    date_label = day.record_date.strftime("%m月%d日").lstrip("0").replace("月0", "月")

    if not day.has_record:
        return f"""
        <section class="day day-empty">
          <div class="day-head">
            <span class="day-name">{weekday}</span>
            <span class="day-date">{date_label}</span>
          </div>
          <p class="empty-note">这一天没有记录</p>
        </section>"""

    score_html = ""
    if day.completion_score is not None:
        color = _score_color(day.completion_score)
        score_html = f"""
          <div class="score">
            <span class="score-num" style="color:{color}">{day.completion_score}%</span>
            <div class="bar"><div class="bar-fill" style="width:{max(2, min(100, day.completion_score))}%;background:{color}"></div></div>
          </div>"""

    tasks_html = ""
    if day.tasks:
        items = []
        for task in day.tasks:
            status = task.get("status", "Unknown")
            dot = STATUS_COLORS.get(status, "#9ca3af")
            label = STATUS_LABELS.get(status, status)
            items.append(
                f'<li><span class="dot" style="background:{dot}"></span>'
                f"{_esc(task.get('title', ''))}"
                f'<span class="status-label" style="color:{dot}">{_esc(label)}</span></li>'
            )
        tasks_html = f'<ul class="tasks">{"".join(items)}</ul>'

    blocks: list[str] = []
    if day.review_summary:
        blocks.append(f'<div class="block"><h4>当日回顾</h4><p>{_esc(day.review_summary)}</p></div>')
    if day.blockers:
        blockers = "".join(f"<li>{_esc(item)}</li>" for item in day.blockers)
        blocks.append(f'<div class="block"><h4>遇到的阻碍</h4><ul class="plain">{blockers}</ul></div>')
    if day.reflection:
        reflection_labels = {
            "feeling": "感受",
            "insight": "收获",
            "problem": "问题",
            "improvement": "改进",
            "reflection_summary": "小结",
        }
        rows = "".join(
            f'<p><span class="ref-label">{reflection_labels[key]}</span>{_esc(value)}</p>'
            for key, value in day.reflection.items()
            if key in reflection_labels
        )
        blocks.append(f'<div class="block reflection"><h4>当日反思</h4>{rows}</div>')
    if day.fallback_text and not blocks:
        blocks.append(f'<div class="block"><h4>当日记录</h4><p class="pre">{_esc(day.fallback_text)}</p></div>')

    return f"""
    <section class="day">
      <div class="day-head">
        <span class="day-name">{weekday}</span>
        <span class="day-date">{date_label}</span>
        {score_html}
      </div>
      {tasks_html}
      {''.join(blocks)}
    </section>"""


def build_report_html(
    days: list[DayData],
    week_start: date,
    week_end: date,
    ai_summary: dict[str, Any],
    timezone: ZoneInfo,
) -> str:
    recorded_days = [day for day in days if day.has_record]
    scores = [day.completion_score for day in recorded_days if day.completion_score is not None]
    avg_score = round(sum(scores) / len(scores)) if scores else None
    all_tasks = [task for day in recorded_days for task in day.tasks]
    completed_count = sum(1 for task in all_tasks if task.get("status") == "Completed")

    stats = [
        ("记录天数", f"{len(recorded_days)}<small>/7</small>"),
        ("平均完成度", f"{avg_score}<small>%</small>" if avg_score is not None else "—"),
        ("任务总数", str(len(all_tasks)) if all_tasks else "—"),
        ("已完成任务", str(completed_count) if all_tasks else "—"),
    ]
    stats_html = "".join(
        f'<div class="stat"><div class="stat-value">{value}</div><div class="stat-label">{label}</div></div>'
        for label, value in stats
    )

    summary_html = ""
    if ai_summary:
        highlights = ai_summary.get("highlights") or []
        highlight_items = "".join(f"<li>{_esc(item)}</li>" for item in highlights)
        parts = []
        if ai_summary.get("week_summary"):
            parts.append(f'<p class="lede">{_esc(ai_summary["week_summary"])}</p>')
        if highlight_items:
            parts.append(f"<h3>本周亮点</h3><ul>{highlight_items}</ul>")
        if ai_summary.get("suggestion_for_next_week"):
            parts.append(
                '<div class="suggestion"><h3>下周建议</h3>'
                f'<p>{_esc(ai_summary["suggestion_for_next_week"])}</p></div>'
            )
        summary_html = f'<section class="summary">{"".join(parts)}</section>'

    days_html = "".join(_render_day_card(day) for day in days)
    generated_at = datetime.now(timezone).strftime("%Y-%m-%d %H:%M")
    range_label = f"{week_start.strftime('%Y年%m月%d日')} — {week_end.strftime('%m月%d日')}"

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<style>
  @page {{
    size: A4;
    margin: 0 0 18mm 0;
    @bottom-center {{
      content: "每周回顾 · 第 " counter(page) " 页，共 " counter(pages) " 页";
      font-family: "Noto Sans CJK SC", "Noto Sans SC", sans-serif;
      font-size: 8pt;
      color: #9ca3af;
    }}
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: "Noto Sans CJK SC", "Noto Sans SC", sans-serif;
    color: #1f2937;
    font-size: 10pt;
    line-height: 1.65;
  }}
  .cover {{
    background: linear-gradient(135deg, #312e81 0%, #4f46e5 60%, #6d5ef1 100%);
    color: #fff;
    padding: 22mm 18mm 14mm 18mm;
  }}
  .cover .kicker {{
    font-size: 9pt; letter-spacing: 4px; text-transform: uppercase;
    color: #c7d2fe; margin-bottom: 4mm;
  }}
  .cover h1 {{ font-size: 26pt; font-weight: 700; margin-bottom: 2mm; }}
  .cover .range {{ font-size: 12pt; color: #e0e7ff; }}
  .cover .generated {{ font-size: 8pt; color: #a5b4fc; margin-top: 5mm; }}
  .content {{ padding: 10mm 18mm 0 18mm; }}
  .stats {{
    display: flex; gap: 4mm; margin-bottom: 8mm;
  }}
  .stat {{
    flex: 1; background: #f5f6ff; border: 1px solid #e0e3f8;
    border-radius: 10px; padding: 4mm 3mm; text-align: center;
  }}
  .stat-value {{ font-size: 17pt; font-weight: 700; color: #4f46e5; }}
  .stat-value small {{ font-size: 9pt; font-weight: 400; color: #818cf8; }}
  .stat-label {{ font-size: 8.5pt; color: #6b7280; margin-top: 1mm; }}
  .summary {{
    background: #fafaff; border-left: 3px solid #4f46e5;
    border-radius: 0 10px 10px 0; padding: 5mm 6mm; margin-bottom: 8mm;
  }}
  .summary .lede {{ font-size: 10.5pt; color: #111827; margin-bottom: 3mm; }}
  .summary h3 {{ font-size: 9.5pt; color: #4f46e5; margin: 3mm 0 1.5mm 0; }}
  .summary ul {{ padding-left: 5mm; }}
  .summary li {{ margin-bottom: 1mm; }}
  .summary .suggestion {{
    margin-top: 3mm; background: #eef2ff; border-radius: 8px; padding: 3mm 4mm;
  }}
  .day {{
    border: 1px solid #e5e7eb; border-radius: 10px;
    padding: 4.5mm 5mm; margin-bottom: 4.5mm;
    page-break-inside: avoid;
  }}
  .day-empty {{ background: #fafafa; border-style: dashed; }}
  .empty-note {{ color: #9ca3af; font-size: 9pt; }}
  .day-head {{
    display: flex; align-items: center; gap: 3mm; margin-bottom: 2.5mm;
  }}
  .day-name {{ font-size: 12pt; font-weight: 700; color: #312e81; }}
  .day-date {{ font-size: 9pt; color: #6b7280; }}
  .score {{ margin-left: auto; display: flex; align-items: center; gap: 2.5mm; }}
  .score-num {{ font-size: 11pt; font-weight: 700; }}
  .bar {{ width: 30mm; height: 2.2mm; background: #f3f4f6; border-radius: 2mm; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 2mm; }}
  .tasks {{ list-style: none; margin-bottom: 2.5mm; }}
  .tasks li {{ padding: 0.8mm 0; border-bottom: 1px dotted #f0f0f3; }}
  .tasks li:last-child {{ border-bottom: none; }}
  .dot {{
    display: inline-block; width: 2.4mm; height: 2.4mm; border-radius: 50%;
    margin-right: 2.5mm; vertical-align: middle;
  }}
  .status-label {{ float: right; font-size: 8pt; }}
  .block {{ margin-top: 2.5mm; }}
  .block h4 {{
    font-size: 8.5pt; color: #4f46e5; letter-spacing: 1px; margin-bottom: 1mm;
  }}
  .block p, .block li {{ font-size: 9.5pt; color: #374151; }}
  .block .pre {{ white-space: pre-wrap; }}
  .plain {{ padding-left: 5mm; }}
  .reflection {{
    background: #fffbeb; border-radius: 8px; padding: 3mm 4mm;
  }}
  .reflection h4 {{ color: #b45309; }}
  .ref-label {{
    display: inline-block; font-size: 8pt; color: #b45309;
    background: #fef3c7; border-radius: 3px; padding: 0 1.5mm; margin-right: 2mm;
  }}
</style>
</head>
<body>
  <div class="cover">
    <div class="kicker">Weekly Report</div>
    <h1>每周回顾</h1>
    <div class="range">{range_label}</div>
    <div class="generated">生成于 {generated_at}</div>
  </div>
  <div class="content">
    <div class="stats">{stats_html}</div>
    {summary_html}
    {days_html}
  </div>
</body>
</html>"""
