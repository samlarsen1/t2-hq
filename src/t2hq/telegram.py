"""A minimal Telegram Bot API client: long polling, messages and inline buttons.

The bot token is part of every request URL, so errors never include the URL or the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

API = "https://api.telegram.org"
# Telegram rejects messages over 4096 characters.
MAX_MESSAGE = 4000

Buttons = list[list[tuple[str, str]]]


class TelegramError(Exception):
    """A Telegram API call failed. The message never contains the token."""


@dataclass(frozen=True)
class Update:
    update_id: int
    chat_id: int | None
    text: str | None = None
    message_id: int | None = None
    callback_id: str | None = None
    callback_data: str | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Update:
        if "callback_query" in raw:
            query = raw["callback_query"]
            message = query.get("message") or {}
            return cls(
                update_id=int(raw["update_id"]),
                chat_id=(message.get("chat") or {}).get("id"),
                message_id=message.get("message_id"),
                callback_id=query.get("id"),
                callback_data=query.get("data"),
            )
        message = raw.get("message") or raw.get("edited_message") or {}
        return cls(
            update_id=int(raw["update_id"]),
            chat_id=(message.get("chat") or {}).get("id"),
            text=message.get("text"),
            message_id=message.get("message_id"),
        )


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split on line breaks into chunks Telegram accepts."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current or not chunks:
        chunks.append(current)
    return [chunk.rstrip("\n") or "." for chunk in chunks]


class Telegram:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None) -> None:
        self._base = f"{API}/bot{token}"
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=70.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, **params: Any) -> Any:
        try:
            response = await self._http.post(f"{self._base}/{method}", json=params)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method}: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {response.status_code}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', response.status_code)}")
        return body.get("result")

    async def updates(self, offset: int | None, timeout: int) -> list[Update]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        return [Update.parse(raw) for raw in await self._call("getUpdates", **params)]

    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> int:
        """Send plain text, split if long; buttons go on the last part. Returns its message id."""
        message_id = 0
        parts = split_message(text)
        for index, part in enumerate(parts):
            params: dict[str, Any] = {
                "chat_id": chat_id,
                "text": part,
                "disable_web_page_preview": True,
            }
            if buttons and index == len(parts) - 1:
                params["reply_markup"] = _keyboard(buttons)
            result = await self._call("sendMessage", **params)
            message_id = int(result["message_id"])
        return message_id

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def set_buttons(self, chat_id: int, message_id: int, buttons: Buttons | None) -> None:
        await self._call(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=_keyboard(buttons or []),
        )

    async def typing(self, chat_id: int) -> None:
        await self._call("sendChatAction", chat_id=chat_id, action="typing")


def _keyboard(buttons: Buttons) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in buttons
        ]
    }
