"""The orchestrator's state in SQLite: the Telegram position, what was sent, and chat history."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

# Question-and-answer pairs kept as context for the next question.
HISTORY_EXCHANGES = 6


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (
                kind TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY (kind, key)
            );
            CREATE TABLE IF NOT EXISTS history (chat_id INTEGER PRIMARY KEY, messages TEXT);
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()

    def mark_seen(self, kind: str, key: str) -> bool:
        """Record ``key``; True if it is new."""
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO seen (kind, key) VALUES (?, ?)", (kind, key)
        )
        self._db.commit()
        return cursor.rowcount == 1

    def is_seen(self, kind: str, key: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM seen WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
        return row is not None

    def history(self, chat_id: int) -> list[ModelMessage]:
        row = self._db.execute(
            "SELECT messages FROM history WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None or not row[0]:
            return []
        return ModelMessagesTypeAdapter.validate_json(row[0])

    def add_exchange(self, chat_id: int, question: str, answer: str) -> None:
        """Keep the last few question-and-answer pairs, without tool calls in between."""
        messages = self.history(chat_id)
        messages += [
            ModelRequest(parts=[UserPromptPart(content=question)]),
            ModelResponse(parts=[TextPart(content=answer)]),
        ]
        messages = messages[-2 * HISTORY_EXCHANGES :]
        self._db.execute(
            "INSERT INTO history (chat_id, messages) VALUES (?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET messages = excluded.messages",
            (chat_id, ModelMessagesTypeAdapter.dump_json(messages).decode()),
        )
        self._db.commit()

    def clear_history(self, chat_id: int) -> None:
        self._db.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))
        self._db.commit()
