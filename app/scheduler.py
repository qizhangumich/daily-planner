from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings


class DailyScheduler:
    def __init__(self, settings: Settings) -> None:
        self.scheduler = AsyncIOScheduler(timezone=settings.timezone)
        self.settings = settings

    def start(self, reminder_callback) -> None:
        self.scheduler.add_job(
            reminder_callback,
            CronTrigger(
                hour=self.settings.daily_review_hour,
                minute=self.settings.daily_review_minute,
                timezone=self.settings.timezone,
            ),
            id="evening_review_reminder",
            replace_existing=True,
        )
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
