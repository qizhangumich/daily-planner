from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import Settings
from app.daily_record_service import DailyRecordService, DailyRecordServiceError
from app.speech_client import SpeechClient
from app.state_manager import StateManager
from app.storage import Storage


logger = logging.getLogger(__name__)

TAB_RECORD = "记录"
TAB_REVIEW = "回顾"
TAB_REFLECTION = "反思"

HELP_TEXT = """欢迎使用每日记录助手。

底部有 3 个常用入口：
- 记录：记录今天早上的安排、任务或计划
- 回顾：记录当天的完成情况
- 反思：记录今天的感受、收获和改进点

也可以继续使用这些命令：
/start - 显示欢迎信息
/add - 添加今天的任务记录
/today - 查看今天已记录的任务
/review - 手动开始当天回顾
/reflection - 手动开始今天反思
/help - 显示帮助信息

你也可以直接发送文字或语音：
- 默认会作为“记录”内容处理
- 进入“回顾”后，会把输入理解为当天完成情况
- 进入“反思”后，会把输入理解为今天的收获与思考"""

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(TAB_RECORD), KeyboardButton(TAB_REVIEW), KeyboardButton(TAB_REFLECTION)]],
    resize_keyboard=True,
    is_persistent=True,
)


class TelegramDailyAssistantBot:
    def __init__(
        self,
        settings: Settings,
        daily_record_service: DailyRecordService,
        speech_client: SpeechClient,
        state_manager: StateManager,
        storage: Storage,
    ) -> None:
        self.settings = settings
        self.daily_record_service = daily_record_service
        self.speech_client = speech_client
        self.state_manager = state_manager
        self.storage = storage
        self.application: Application | None = None

    def build_application(self, post_init=None, post_shutdown=None) -> Application:
        builder = ApplicationBuilder().token(self.settings.telegram_bot_token)
        if post_init is not None:
            builder = builder.post_init(post_init)
        if post_shutdown is not None:
            builder = builder.post_shutdown(post_shutdown)

        application = builder.build()
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("add", self.add_command))
        application.add_handler(CommandHandler("today", self.today_command))
        application.add_handler(CommandHandler("review", self.review_command))
        application.add_handler(CommandHandler("reflection", self.reflection_command))
        application.add_handler(MessageHandler(filters.VOICE, self.voice_message_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message_handler))
        application.add_error_handler(self.error_handler)
        self.application = application
        return application

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        self.state_manager.set_state(self.settings.telegram_user_id, "idle")
        await update.message.reply_text(HELP_TEXT, reply_markup=MAIN_KEYBOARD)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        await update.message.reply_text(HELP_TEXT, reply_markup=MAIN_KEYBOARD)

    async def add_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        self.state_manager.set_state(self.settings.telegram_user_id, "adding_task")
        await update.message.reply_text(
            "把今天早上要记录的内容发给我吧。可以是文字，也可以是一段语音。",
            reply_markup=MAIN_KEYBOARD,
        )

    async def today_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        try:
            tasks = await self.daily_record_service.get_today_tasks()
        except DailyRecordServiceError as exc:
            await update.message.reply_text(f"读取今天任务失败：{exc}", reply_markup=MAIN_KEYBOARD)
            return

        if not tasks:
            await update.message.reply_text(
                "今天还没有记录内容。你可以点“记录”，或者直接发文字/语音给我。",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        lines = [
            f"{index}. {task['title']} [{task['priority']}] ({task['status']})"
            for index, task in enumerate(tasks, start=1)
        ]
        await update.message.reply_text(
            "今天已经记录的任务：\n\n" + "\n".join(lines),
            reply_markup=MAIN_KEYBOARD,
        )

    async def review_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        await self._send_review_prompt(chat_id=update.effective_chat.id)

    async def reflection_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        self.state_manager.set_state(self.settings.telegram_user_id, "awaiting_reflection")
        await update.message.reply_text(
            "请直接回复今天的感受、反思或收获。可以是一句话，也可以是一段语音。",
            reply_markup=MAIN_KEYBOARD,
        )

    async def text_message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            await update.message.reply_text("我没有收到有效文字内容，请再发一次。", reply_markup=MAIN_KEYBOARD)
            return

        if text == TAB_RECORD:
            await self.add_command(update, context)
            return

        if text == TAB_REVIEW:
            await self.review_command(update, context)
            return

        if text == TAB_REFLECTION:
            await self.reflection_command(update, context)
            return

        await self._route_user_input(update, text, source="Telegram Text")

    async def voice_message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        await update.message.chat.send_action(action=ChatAction.TYPING)
        try:
            transcript = await self.transcribe_telegram_voice(update, context)
        except DailyRecordServiceError as exc:
            await update.message.reply_text(f"语音转写失败：{exc}", reply_markup=MAIN_KEYBOARD)
            return

        if not transcript:
            await update.message.reply_text(
                "语音已经收到，但没有成功识别出文字内容，请再试一次。",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        await update.message.reply_text(f"语音已转写：\n{transcript}", reply_markup=MAIN_KEYBOARD)
        await self._route_user_input(update, transcript, source="Telegram Voice")

    async def transcribe_telegram_voice(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> str:
        voice = update.message.voice
        if voice is None:
            raise DailyRecordServiceError("No voice message was found.")

        temp_path: Path | None = None
        try:
            telegram_file = await context.bot.get_file(voice.file_id)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_file:
                temp_path = Path(temp_file.name)
            await telegram_file.download_to_drive(custom_path=str(temp_path))
            return await self.speech_client.transcribe(str(temp_path))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Telegram voice transcription failed")
            raise DailyRecordServiceError(f"Failed to transcribe Telegram voice: {exc}") from exc
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    async def send_evening_review_reminder(self) -> None:
        if self.application is None:
            raise RuntimeError("Telegram application has not been initialized.")
        await self._send_review_prompt(chat_id=self.settings.telegram_user_id)

    async def _send_review_prompt(self, chat_id: int) -> None:
        if self.application is None:
            raise RuntimeError("Telegram application has not been initialized.")

        try:
            payload = await self.daily_record_service.prepare_evening_review()
        except DailyRecordServiceError as exc:
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=f"今晚回顾提醒发送失败：{exc}",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        tasks = payload.get("tasks", [])
        if tasks:
            lines = [f"{index}. {task['title']}" for index, task in enumerate(tasks, start=1)]
            task_text = "\n".join(lines)
            message = (
                "今天的回顾时间到了。\n\n"
                "你今天记录了以下任务：\n\n"
                f"{task_text}\n\n"
                "请直接回复这些任务的完成情况。\n"
                "你可以用文字，也可以用语音。"
            )
        else:
            message = (
                "今天的回顾时间到了。\n\n"
                "今天还没有记录任务，你也可以直接做一次自由回顾。\n"
                "请告诉我今天完成了什么、没完成什么，以及原因。"
            )

        self.state_manager.set_state(self.settings.telegram_user_id, "awaiting_review")
        await self.application.bot.send_message(chat_id=chat_id, text=message, reply_markup=MAIN_KEYBOARD)

    async def _route_user_input(self, update: Update, user_input: str, source: str) -> None:
        state = self.state_manager.get_state(self.settings.telegram_user_id)
        await update.message.chat.send_action(action=ChatAction.TYPING)

        if state in {"idle", "adding_task"}:
            success = await self._handle_task_input(update, user_input, source)
            if success:
                self.state_manager.set_state(self.settings.telegram_user_id, "idle")
            return

        if state == "awaiting_review":
            success = await self._handle_review_input(update, user_input, source)
            if success:
                self.state_manager.set_state(self.settings.telegram_user_id, "awaiting_reflection")
            return

        if state == "awaiting_reflection":
            success = await self._handle_reflection_input(update, user_input, source)
            if success:
                self.state_manager.set_state(self.settings.telegram_user_id, "idle")
            return

        success = await self._handle_task_input(update, user_input, source)
        if success:
            self.state_manager.set_state(self.settings.telegram_user_id, "idle")

    async def _handle_task_input(self, update: Update, user_input: str, source: str) -> bool:
        try:
            result = await self.daily_record_service.add_task_to_today(user_input=user_input, source=source)
        except DailyRecordServiceError as exc:
            await update.message.reply_text(f"添加记录失败：{exc}", reply_markup=MAIN_KEYBOARD)
            return False

        task_lines = [f"{index}. {task['title']}" for index, task in enumerate(result["tasks"], start=1)]
        message = (
            "已添加到今天的记录：\n\n"
            + "\n".join(task_lines)
            + f"\n\n你今天目前一共记录了 {result['task_count']} 个任务。"
        )
        await update.message.reply_text(message, reply_markup=MAIN_KEYBOARD)
        return True

    async def _handle_review_input(self, update: Update, review_input: str, source: str) -> bool:
        try:
            review = await self.daily_record_service.handle_review_input(review_input=review_input, source=source)
        except DailyRecordServiceError as exc:
            await update.message.reply_text(f"回顾写入失败：{exc}", reply_markup=MAIN_KEYBOARD)
            return False

        message = (
            "我已经整理好今天的完成情况。\n"
            f"今日完成度：{review.get('completion_score', 0)}%\n\n"
            "接下来，请简单写一下今天的感受、反思或收获。\n"
            "可以是一句话，也可以是一段语音。"
        )
        await update.message.reply_text(message, reply_markup=MAIN_KEYBOARD)
        return True

    async def _handle_reflection_input(self, update: Update, reflection_input: str, source: str) -> bool:
        try:
            await self.daily_record_service.handle_reflection_input(
                reflection_input=reflection_input,
                source=source,
            )
        except DailyRecordServiceError as exc:
            await update.message.reply_text(f"反思写入失败：{exc}", reply_markup=MAIN_KEYBOARD)
            return False

        message = (
            "今天的记录已经完成。\n\n"
            "已写入 Notion：\n"
            "- 今日任务\n"
            "- 任务完成情况\n"
            "- 今日完成度\n"
            "- 明日延续事项\n"
            "- 今日反思\n\n"
            "明天可以继续从这些未完成事项开始。"
        )
        await update.message.reply_text(message, reply_markup=MAIN_KEYBOARD)
        return True

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Telegram application error", exc_info=context.error)
        self.storage.append_event_log("ERROR", str(context.error))

    async def _is_authorized(self, update: Update) -> bool:
        user = update.effective_user
        if user is None or user.id != self.settings.telegram_user_id:
            if update.effective_message:
                await update.effective_message.reply_text("Sorry, you are not authorized to use this bot.")
            return False
        return True
