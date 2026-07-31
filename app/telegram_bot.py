from __future__ import annotations

import logging
import tempfile
from datetime import date
from pathlib import Path
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
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
from app.weekly_report import WeeklyReportService


logger = logging.getLogger(__name__)

TAB_RECORD = "记录"
TAB_REVIEW = "回顾"
TAB_REFLECTION = "反思"
TAB_WEEKLY = "周报"

HELP_TEXT = """欢迎使用每日记录助手。

底部有 4 个常用入口：
- 记录：记录今天早上的安排、任务或计划
- 回顾：记录当天的完成情况
- 反思：记录今天的感受、收获和改进点
- 周报：生成上一周（周一到周日）的 PDF 周报

也可以继续使用这些命令：
/start - 显示欢迎信息
/add - 添加今天的任务记录
/today - 查看今天已记录的任务
/review - 手动开始当天回顾
/reflection - 手动开始今天反思
/weekly - 生成上一周的 PDF 周报
/names - 管理专有名词表（公司/人名/项目）
/help - 显示帮助信息

写入 Notion 前会先给你预览：
- 点“✅ 写入”确认保存
- 点“✏️ 修改”或直接回复文字来纠正内容（比如改错的公司名）
- 点“❌ 取消”放弃这条记录

你也可以直接发送文字或语音：
- 默认会作为“记录”内容处理
- 进入“回顾”后，会把输入理解为当天完成情况
- 进入“反思”后，会把输入理解为今天的收获与思考"""

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(TAB_RECORD),
            KeyboardButton(TAB_REVIEW),
            KeyboardButton(TAB_REFLECTION),
            KeyboardButton(TAB_WEEKLY),
        ]
    ],
    resize_keyboard=True,
    is_persistent=True,
)

CONFIRM_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ 写入", callback_data="pending:confirm"),
            InlineKeyboardButton("✏️ 修改", callback_data="pending:edit"),
            InlineKeyboardButton("❌ 取消", callback_data="pending:cancel"),
        ]
    ]
)


class TelegramDailyAssistantBot:
    def __init__(
        self,
        settings: Settings,
        daily_record_service: DailyRecordService,
        speech_client: SpeechClient,
        state_manager: StateManager,
        storage: Storage,
        weekly_report_service: WeeklyReportService,
    ) -> None:
        self.settings = settings
        self.daily_record_service = daily_record_service
        self.speech_client = speech_client
        self.state_manager = state_manager
        self.storage = storage
        self.weekly_report_service = weekly_report_service
        self.application: Application | None = None
        self._pending: dict | None = None

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
        application.add_handler(CommandHandler("weekly", self.weekly_command))
        application.add_handler(CommandHandler("names", self.names_command))
        application.add_handler(CallbackQueryHandler(self.pending_callback, pattern=r"^pending:"))
        application.add_handler(CallbackQueryHandler(self.review_date_callback, pattern=r"^rvd:"))
        application.add_handler(MessageHandler(filters.VOICE, self.voice_message_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message_handler))
        application.add_error_handler(self.error_handler)
        self.application = application
        return application

    def _date_label(self, record_date: str) -> str:
        delta = (date.fromisoformat(self.daily_record_service.today()) - date.fromisoformat(record_date)).days
        if delta == 0:
            return "今天"
        if delta == 1:
            return "昨天"
        if delta == 2:
            return "前天"
        return f"{record_date[5:7]}月{record_date[8:10]}日"

    @staticmethod
    def _parse_state(state: str) -> tuple[str, Optional[str]]:
        """States like 'awaiting_review@2026-07-26' carry the day being reviewed."""
        if "@" in state:
            name, iso = state.split("@", 1)
            return name, iso
        return state, None

    def _backfill_line(self, parsed: dict) -> str:
        target = self.daily_record_service._validate_backfill_date(parsed.get("record_date"))
        if target and target != self.daily_record_service.today():
            return f"📅 将记录到：{int(target[5:7])}月{int(target[8:10])}日（{self._date_label(target)}）\n"
        return ""

    def _parse_date_arg(self, raw: str) -> Optional[str]:
        """Accept '2026-07-29' or '07-29'/'7-29' (current year)."""
        raw = raw.strip().replace("/", "-")
        if raw.count("-") == 1:
            today = self.daily_record_service.today()
            raw = f"{today[:4]}-{raw}"
        try:
            parts = raw.split("-")
            normalized = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except (ValueError, IndexError):
            return None
        return self.daily_record_service._validate_backfill_date(normalized)

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
            f"{index}. {task['title']} ({task.get('category', 'W2')}) - {task.get('status', 'Planned')}"
            for index, task in enumerate(tasks, start=1)
        ]
        await update.message.reply_text(
            "今天已经记录的任务：\n\n" + "\n".join(lines),
            reply_markup=MAIN_KEYBOARD,
        )

    async def review_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        args = context.args or []
        if args:
            target = self._parse_date_arg(args[0])
            if target is None:
                await update.message.reply_text(
                    "日期格式不对，示例：/review 07-29 或 /review 2026-07-29",
                    reply_markup=MAIN_KEYBOARD,
                )
                return
            await self._begin_review_for(update.effective_chat.id, target)
            return
        await self._send_review_prompt(chat_id=update.effective_chat.id, allow_picker=True)

    async def reflection_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        try:
            target = (
                await self.daily_record_service.latest_reflectable_date()
                or self.daily_record_service.today()
            )
        except DailyRecordServiceError:
            target = self.daily_record_service.today()
        label = self._date_label(target)
        self.state_manager.set_state(self.settings.telegram_user_id, f"awaiting_reflection@{target}")
        await update.message.reply_text(
            f"请直接回复{label}的感受、反思或收获。可以是一句话，也可以是一段语音。",
            reply_markup=MAIN_KEYBOARD,
        )

    async def weekly_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        await update.message.reply_text(
            "正在生成上一周的周报，大约需要十几秒，请稍等……",
            reply_markup=MAIN_KEYBOARD,
        )
        await update.message.chat.send_action(action=ChatAction.UPLOAD_DOCUMENT)
        try:
            pdf_path, week_start, week_end = await self.weekly_report_service.generate()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Weekly report generation failed")
            await update.message.reply_text(f"周报生成失败：{exc}", reply_markup=MAIN_KEYBOARD)
            return

        caption = f"这是你 {week_start.strftime('%m月%d日')} 至 {week_end.strftime('%m月%d日')} 的周报。"
        try:
            with open(pdf_path, "rb") as pdf_file:
                await update.message.reply_document(
                    document=pdf_file,
                    filename=pdf_path.name,
                    caption=caption,
                    reply_markup=MAIN_KEYBOARD,
                )
        finally:
            pdf_path.unlink(missing_ok=True)

    async def text_message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            await update.message.reply_text("我没有收到有效文字内容，请再发一次。", reply_markup=MAIN_KEYBOARD)
            return

        if text in {TAB_RECORD, TAB_REVIEW, TAB_REFLECTION, TAB_WEEKLY}:
            # Switching tabs abandons any unconfirmed preview.
            self._pending = None

        if text == TAB_RECORD:
            await self.add_command(update, context)
            return

        if text == TAB_REVIEW:
            await self.review_command(update, context)
            return

        if text == TAB_REFLECTION:
            await self.reflection_command(update, context)
            return

        if text == TAB_WEEKLY:
            await self.weekly_command(update, context)
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
            return await self.speech_client.transcribe(
                str(temp_path),
                vocabulary_hint=self.daily_record_service.glossary_text(),
            )
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

    async def _send_review_prompt(self, chat_id: int, allow_picker: bool = False) -> None:
        if self.application is None:
            raise RuntimeError("Telegram application has not been initialized.")

        if allow_picker:
            try:
                candidates = await self.daily_record_service.reviewable_dates()
            except DailyRecordServiceError:
                candidates = []
            if len(candidates) >= 2:
                today = self.daily_record_service.today()
                buttons = [
                    [InlineKeyboardButton(
                        f"{record_date[5:]}（{self._date_label(record_date)}）· {count} 项",
                        callback_data=f"rvd:{record_date}",
                    )]
                    for record_date, count in candidates
                ]
                if all(record_date != today for record_date, _ in candidates):
                    buttons.append([InlineKeyboardButton("今天（自由回顾）", callback_data=f"rvd:{today}")])
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text="有几天的任务还没有回顾，选择要回顾的日期：",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
                return

        target = (
            await self.daily_record_service.latest_reviewable_date()
            or self.daily_record_service.today()
        )
        await self._begin_review_for(chat_id, target)

    async def review_date_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user is None or query.from_user.id != self.settings.telegram_user_id:
            return
        await query.answer()
        target = self._parse_date_arg(query.data.split(":", 1)[1])
        if target is None:
            await query.edit_message_text("这个日期已经不能回顾了，请重新点“回顾”。")
            return
        await query.edit_message_text(f"回顾 {self._date_label(target)}（{target[5:]}）：")
        await self._begin_review_for(query.message.chat_id, target)

    async def _begin_review_for(self, chat_id: int, target: str) -> None:
        if self.application is None:
            raise RuntimeError("Telegram application has not been initialized.")
        try:
            payload = await self.daily_record_service.prepare_evening_review(target)
        except DailyRecordServiceError as exc:
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=f"回顾提醒发送失败：{exc}",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        label = self._date_label(target)
        catch_up = "" if label == "今天" else f"（{label} {target} 的任务还没有回顾，先补上吧）"
        tasks = payload.get("tasks", [])
        if tasks:
            lines = [f"{index}. {task['title']}" for index, task in enumerate(tasks, start=1)]
            task_text = "\n".join(lines)
            message = (
                f"回顾时间到了。{catch_up}\n\n"
                f"{label}记录了以下任务：\n\n"
                f"{task_text}\n\n"
                "请直接回复这些任务的完成情况。\n"
                "你可以用文字，也可以用语音。"
            )
        else:
            message = (
                "回顾时间到了。\n\n"
                "今天还没有记录任务，你也可以直接做一次自由回顾。\n"
                "请告诉我今天完成了什么、没完成什么，以及原因。"
            )

        self.state_manager.set_state(self.settings.telegram_user_id, f"awaiting_review@{target}")
        await self.application.bot.send_message(chat_id=chat_id, text=message, reply_markup=MAIN_KEYBOARD)

    async def _route_user_input(self, update: Update, user_input: str, source: str) -> None:
        state = self.state_manager.get_state(self.settings.telegram_user_id)
        await update.message.chat.send_action(action=ChatAction.TYPING)

        if self._pending is not None:
            await self._apply_pending_correction(update, user_input)
            return

        state_name, state_date = self._parse_state(state)
        if state_name == "awaiting_review":
            await self._prepare_pending(
                update, kind="review", user_input=user_input, source=source, record_date=state_date
            )
            return

        if state_name == "awaiting_reflection":
            await self._prepare_pending(
                update, kind="reflection", user_input=user_input, source=source, record_date=state_date
            )
            return

        await self._prepare_pending(update, kind="task", user_input=user_input, source=source)

    async def _prepare_pending(
        self, update: Update, kind: str, user_input: str, source: str,
        record_date: Optional[str] = None,
    ) -> None:
        try:
            if kind == "task":
                parsed = await self.daily_record_service.parse_tasks(user_input)
            elif kind == "review":
                parsed = await self.daily_record_service.parse_review(user_input, record_date)
            else:
                parsed = await self.daily_record_service.parse_reflection(user_input, record_date)
        except DailyRecordServiceError as exc:
            await update.message.reply_text(f"内容解析失败：{exc}", reply_markup=MAIN_KEYBOARD)
            return

        self._pending = {
            "kind": kind,
            "parsed": parsed,
            "raw": user_input,
            "source": source,
            "record_date": record_date,
        }
        date_line = self._backfill_line(parsed) if kind == "task" else ""
        await update.message.reply_text(
            self._format_preview(kind, parsed, date_line),
            reply_markup=CONFIRM_KEYBOARD,
        )

    async def _apply_pending_correction(self, update: Update, correction: str) -> None:
        assert self._pending is not None
        try:
            corrected = await self.daily_record_service.apply_correction(
                self._pending["parsed"], correction
            )
        except DailyRecordServiceError as exc:
            await update.message.reply_text(
                f"修改失败：{exc}\n可以再试一次，或点“❌ 取消”。",
                reply_markup=CONFIRM_KEYBOARD,
            )
            return

        self._pending["parsed"] = corrected
        date_line = self._backfill_line(corrected) if self._pending["kind"] == "task" else ""
        await update.message.reply_text(
            "已按你的意见修改：\n\n" + self._format_preview(self._pending["kind"], corrected, date_line),
            reply_markup=CONFIRM_KEYBOARD,
        )

    @staticmethod
    def _format_preview(kind: str, parsed: dict, date_line: str = "") -> str:
        if kind == "task":
            tasks = parsed.get("tasks", [])
            lines = [
                f"{index}. {task.get('title', '')} ({task.get('category', 'W2')})"
                for index, task in enumerate(tasks, start=1)
            ]
            body = "\n".join(lines) if lines else "（没有解析出任务）"
            return (
                f"以下任务将写入 Notion：\n{date_line}\n"
                f"{body}\n\n"
                "确认写入吗？如果有错（比如公司名），直接回复修改意见即可。"
            )

        if kind == "review":
            lines = [f"今日完成度：{parsed.get('completion_score', 0)}%"]
            if parsed.get("overall_summary"):
                lines.append(f"总结：{parsed['overall_summary']}")
            completed = parsed.get("completed_tasks", [])
            unfinished = parsed.get("unfinished_tasks", [])
            if completed:
                lines.append("\n已完成：")
                lines.extend(f"- {item}" for item in completed)
            if unfinished:
                lines.append("\n未完成：")
                lines.extend(f"- {item}" for item in unfinished)
            if parsed.get("suggestion_for_tomorrow"):
                lines.append(f"\n明日建议：{parsed['suggestion_for_tomorrow']}")
            return (
                "以下回顾将写入 Notion：\n\n"
                + "\n".join(lines)
                + "\n\n确认写入吗？如果有错，直接回复修改意见即可。"
            )

        labels = [
            ("feeling", "感受"),
            ("insight", "收获"),
            ("problem", "问题"),
            ("improvement", "改进"),
            ("reflection_summary", "小结"),
        ]
        lines = [
            f"{label}：{str(parsed.get(key, '')).strip()}"
            for key, label in labels
            if str(parsed.get(key, "")).strip()
        ]
        body = "\n".join(lines) if lines else "（没有解析出内容）"
        return (
            "以下反思将写入 Notion：\n\n"
            f"{body}\n\n"
            "确认写入吗？如果有错，直接回复修改意见即可。"
        )

    async def pending_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        user = query.from_user
        if user is None or user.id != self.settings.telegram_user_id:
            await query.answer("Not authorized.")
            return
        await query.answer()

        action = query.data.split(":", 1)[1]
        if self._pending is None:
            await query.edit_message_text("这条记录已经处理过了。")
            return

        kind = self._pending["kind"]
        pending_date = self._pending.get("record_date")

        if action == "cancel":
            self._pending = None
            restore = {"review": "awaiting_review", "reflection": "awaiting_reflection"}.get(kind, "idle")
            if restore != "idle" and pending_date:
                restore = f"{restore}@{pending_date}"
            self.state_manager.set_state(self.settings.telegram_user_id, restore)
            await query.edit_message_text("已取消，这条记录没有写入 Notion。可以重新发送内容。")
            return

        if action == "edit":
            await query.edit_message_text(
                query.message.text + "\n\n✏️ 请直接回复需要修改的地方（例如：公司名应该是 XX）。",
                reply_markup=CONFIRM_KEYBOARD,
            )
            return

        # action == "confirm"
        pending = self._pending
        try:
            if kind == "task":
                result = await self.daily_record_service.commit_tasks(
                    pending["parsed"], pending["raw"], pending["source"]
                )
                self._pending = None
                self.state_manager.set_state(self.settings.telegram_user_id, "idle")
                task_lines = [
                    f"{index}. {task['title']}" for index, task in enumerate(result["tasks"], start=1)
                ]
                message = (
                    "已写入 Notion：\n\n"
                    + "\n".join(task_lines)
                    + f"\n\n你{self._date_label(result.get('record_date') or self.daily_record_service.today())}"
                    + f"一共记录了 {result['task_count']} 个任务。"
                )
            elif kind == "review":
                review = await self.daily_record_service.commit_review(
                    pending["parsed"], pending["raw"], pending["source"], pending_date
                )
                self._pending = None
                target = pending_date or self.daily_record_service.today()
                label = self._date_label(target)
                self.state_manager.set_state(
                    self.settings.telegram_user_id, f"awaiting_reflection@{target}"
                )
                message = (
                    f"{label}的回顾已写入 Notion。\n"
                    f"完成度：{review.get('completion_score', 0)}%\n\n"
                    f"接下来，请简单写一下{label}的感受、反思或收获。\n"
                    "可以是一句话，也可以是一段语音。"
                )
            else:
                await self.daily_record_service.commit_reflection(
                    pending["parsed"], pending["raw"], pending["source"], pending_date
                )
                self._pending = None
                self.state_manager.set_state(self.settings.telegram_user_id, "idle")
                message = (
                    "这一天的记录已经完成。\n\n"
                    "已写入 Notion：\n"
                    "- 今日任务\n"
                    "- 任务完成情况\n"
                    "- 今日完成度\n"
                    "- 明日延续事项\n"
                    "- 今日反思\n\n"
                    "明天可以继续从这些未完成事项开始。"
                )
        except DailyRecordServiceError as exc:
            await query.edit_message_text(
                query.message.text + f"\n\n⚠️ 写入失败：{exc}\n可以再点一次“✅ 写入”重试。",
                reply_markup=CONFIRM_KEYBOARD,
            )
            return

        await query.edit_message_text(message)

    async def names_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        args = context.args or []

        if args and args[0].lower() in {"add", "加", "添加"} and len(args) > 1:
            term = " ".join(args[1:]).strip()
            self.storage.add_glossary_term(term)
            await update.message.reply_text(f"已添加到名词表：{term}", reply_markup=MAIN_KEYBOARD)
            return

        if args and args[0].lower() in {"del", "delete", "remove", "删", "删除"} and len(args) > 1:
            term = " ".join(args[1:]).strip()
            if self.storage.delete_glossary_term(term):
                await update.message.reply_text(f"已从名词表删除：{term}", reply_markup=MAIN_KEYBOARD)
            else:
                await update.message.reply_text(f"名词表里没有找到：{term}", reply_markup=MAIN_KEYBOARD)
            return

        terms = self.storage.list_glossary_terms()
        listing = "\n".join(f"- {term}" for term in terms) if terms else "（还没有添加任何名词）"
        await update.message.reply_text(
            "专有名词表（语音转写和解析时会按这些拼写输出）：\n\n"
            f"{listing}\n\n"
            "用法：\n"
            "/names add 名词 - 添加（公司、人名、项目名）\n"
            "/names del 名词 - 删除",
            reply_markup=MAIN_KEYBOARD,
        )

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
