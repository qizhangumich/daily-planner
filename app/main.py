from __future__ import annotations

import logging

from app.config import Settings
from app.daily_record_service import DailyRecordService
from app.logger import setup_logging
from app.notion_client import NotionDailyRecordsClient
from app.openai_client import OpenAIClient
from app.reflection_parser import ReflectionParser
from app.review_parser import ReviewParser
from app.scheduler import DailyScheduler
from app.speech_client import SpeechClient
from app.state_manager import StateManager
from app.storage import Storage
from app.task_parser import TaskParser
from app.telegram_bot import TelegramDailyAssistantBot


logger = logging.getLogger(__name__)


def main() -> None:
    settings = Settings.load()
    settings.ensure_directories()
    setup_logging(settings.log_path)

    storage = Storage(settings.database_path)
    storage.initialize()

    openai_client = OpenAIClient(settings)
    speech_client = SpeechClient(openai_client)
    notion_client = NotionDailyRecordsClient(settings)

    task_parser = TaskParser(openai_client, settings.prompts_dir / "parse_task.md")
    review_parser = ReviewParser(openai_client, settings.prompts_dir / "parse_review.md")
    reflection_parser = ReflectionParser(openai_client, settings.prompts_dir / "parse_reflection.md")

    daily_record_service = DailyRecordService(
        notion_client=notion_client,
        task_parser=task_parser,
        review_parser=review_parser,
        reflection_parser=reflection_parser,
        storage=storage,
        timezone_name=settings.timezone,
    )
    scheduler = DailyScheduler(settings)

    state_manager = StateManager(storage)
    bot = TelegramDailyAssistantBot(
        settings=settings,
        daily_record_service=daily_record_service,
        speech_client=speech_client,
        state_manager=state_manager,
        storage=storage,
    )

    async def post_init(app) -> None:
        scheduler.start(bot.send_evening_review_reminder)

    async def post_shutdown(app) -> None:
        scheduler.shutdown()
        storage.close()

    application = bot.build_application(post_init=post_init, post_shutdown=post_shutdown)
    logger.info("Starting telegram-notion-daily-assistant")
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
