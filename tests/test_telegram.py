"""The Telegram client, against a mock Bot API."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from t2hq.telegram import Telegram, TelegramError, Update, split_message

TOKEN = "123456:SECRET-TOKEN"  # pragma: allowlist secret


Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> tuple[Telegram, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.url.path.rsplit("/", 1)[-1], **json.loads(request.content)})
        return handler(request)

    return Telegram(TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(record))), calls


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def test_parse_message_and_callback() -> None:
    message = Update.parse(
        {"update_id": 5, "message": {"message_id": 9, "chat": {"id": 42}, "text": "hi"}}
    )
    assert (message.chat_id, message.text, message.callback_id) == (42, "hi", None)
    callback = Update.parse(
        {
            "update_id": 6,
            "callback_query": {
                "id": "cb1",
                "data": "acc:e3a79f4f",
                "message": {"message_id": 10, "chat": {"id": 42}},
            },
        }
    )
    assert (callback.chat_id, callback.message_id, callback.callback_data) == (
        42,
        10,
        "acc:e3a79f4f",
    )


def test_split_message() -> None:
    assert split_message("short") == ["short"]
    parts = split_message("\n".join(["x" * 30] * 10), limit=100)
    assert all(len(p) <= 100 for p in parts) and len(parts) == 4
    assert split_message("y" * 250, limit=100) == ["y" * 100, "y" * 100, "y" * 50]
    assert split_message("") == ["."]


async def test_send_puts_buttons_on_the_last_part() -> None:
    telegram, calls = client(lambda r: ok({"message_id": len(r.content)}))
    await telegram.send(42, "a" * 4500, [[("✅ Accept", "acc:x")]])
    assert [c["method"] for c in calls] == ["sendMessage", "sendMessage"]
    assert "reply_markup" not in calls[0]
    assert calls[1]["reply_markup"] == {
        "inline_keyboard": [[{"text": "✅ Accept", "callback_data": "acc:x"}]]
    }


async def test_updates_passes_offset_and_timeout() -> None:
    telegram, calls = client(
        lambda r: ok([{"update_id": 7, "message": {"chat": {"id": 1}, "text": "x"}}])
    )
    [update] = await telegram.updates(offset=7, timeout=50)
    assert update.update_id == 7
    assert calls[0]["offset"] == 7 and calls[0]["timeout"] == 50


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}),
        lambda r: httpx.Response(502, text="<html>bad gateway</html>"),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("boom", request=r)),
    ],
)
async def test_errors_never_contain_the_token(handler: Any) -> None:
    telegram, _ = client(handler)
    with pytest.raises(TelegramError) as caught:
        await telegram.send(42, "hi")
    assert TOKEN not in str(caught.value) and "SECRET" not in repr(caught.value)
    assert caught.value.__cause__ is None
