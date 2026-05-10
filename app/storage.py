from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class Storage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row

    def initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_state (
                    user_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_cache (
                    record_date TEXT PRIMARY KEY,
                    page_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def set_state(self, user_id: int, state: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO app_state (user_id, state, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    state=excluded.state,
                    updated_at=excluded.updated_at
                """,
                (str(user_id), state, self._now()),
            )

    def get_state(self, user_id: int, default: str = "idle") -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT state FROM app_state WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()
        return row["state"] if row else default

    def save_daily_payload(self, record_date: str, page_id: str, payload: dict[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO daily_cache (record_date, page_id, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(record_date) DO UPDATE SET
                    page_id=excluded.page_id,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (record_date, page_id, json.dumps(payload, ensure_ascii=False), self._now()),
            )

    def get_daily_payload(self, record_date: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM daily_cache WHERE record_date = ?",
                (record_date,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])

    def get_daily_page_id(self, record_date: str) -> Optional[str]:
        with self._lock:
            row = self._connection.execute(
                "SELECT page_id FROM daily_cache WHERE record_date = ?",
                (record_date,),
            ).fetchone()
        return row["page_id"] if row else None

    def delete_daily_cache(self, record_date: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM daily_cache WHERE record_date = ?",
                (record_date,),
            )

    def append_event_log(self, level: str, message: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO event_logs (level, message, created_at) VALUES (?, ?, ?)",
                (level, message, self._now()),
            )

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat()
