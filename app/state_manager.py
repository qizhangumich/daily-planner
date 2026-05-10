from __future__ import annotations

from app.storage import Storage


class StateManager:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def get_state(self, user_id: int) -> str:
        return self.storage.get_state(user_id)

    def set_state(self, user_id: int, state: str) -> None:
        self.storage.set_state(user_id, state)
