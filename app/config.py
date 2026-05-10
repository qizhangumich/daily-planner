from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
DATA_DIR = BASE_DIR / "data"


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    telegram_user_id: int
    notion_token: str
    notion_daily_database_id: str
    openai_api_key: str
    timezone: str
    daily_review_hour: int
    daily_review_minute: int
    database_path: Path
    log_path: Path
    openai_text_model: str
    openai_audio_model: str
    prompts_dir: Path
    data_dir: Path

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()

        def require(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"Missing required environment variable: {name}")
            return value

        data_dir = DATA_DIR
        database_path = Path(os.getenv("DATABASE_PATH", "./data/app.db")).expanduser()
        log_path = Path(os.getenv("LOG_PATH", "./data/app.log")).expanduser()

        if not database_path.is_absolute():
            database_path = (BASE_DIR / database_path).resolve()
        if not log_path.is_absolute():
            log_path = (BASE_DIR / log_path).resolve()

        return cls(
            telegram_bot_token=require("TELEGRAM_BOT_TOKEN"),
            telegram_user_id=int(require("TELEGRAM_USER_ID")),
            notion_token=require("NOTION_TOKEN"),
            notion_daily_database_id=require("NOTION_DAILY_DATABASE_ID"),
            openai_api_key=require("OPENAI_API_KEY"),
            timezone=os.getenv("TIMEZONE", "Asia/Singapore"),
            daily_review_hour=int(os.getenv("DAILY_REVIEW_HOUR", "21")),
            daily_review_minute=int(os.getenv("DAILY_REVIEW_MINUTE", "30")),
            database_path=database_path,
            log_path=log_path,
            openai_text_model=os.getenv("OPENAI_TEXT_MODEL", "gpt-4.1-mini"),
            openai_audio_model=os.getenv("OPENAI_AUDIO_MODEL", "gpt-4o-mini-transcribe"),
            prompts_dir=PROMPTS_DIR,
            data_dir=data_dir,
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
