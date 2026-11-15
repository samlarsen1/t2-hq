"""The long-running orchestrator: answers the operator on Telegram and watches the droids."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic_ai.messages import ModelMessage

from .digest import advice_messages, failure_alert, parse_callback
from .droids import DROIDS, DroidError, Droids
from .store import Store
from .telegram import Buttons, TelegramError, Update

logger = logging.getLogger(__name__)

FAILED = frozenset({"failed", "config_error"})
HELP = """\
T2-HQ watches the droids and answers questions about them. It can read and dry-run, never trade.

/status  versions, last runs and next timers
/advice  open t2-3po suggestions
/reset   forget this conversation
Anything else is a question, for example "what did d2 do today?" or "dry-run k2"."""

Ask = Callable[[str, list[ModelMessage]], Awaitable[str]]


class Bot(Protocol):
    async def updates(self, offset: int | None, timeout: int) -> list[Update]: ...
    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> int: ...
    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...
    async def set_buttons(self, chat_id: int, message_id: int, buttons: Buttons | None) -> None: ...
    async def typing(self, chat_id: int) -> None: ...


def format_status(status: dict[str, Any]) -> str:
    lines = []
    for name, info in status["droids"].items():
        if not info["deployed"]:
            lines.append(f"{name}: not deployed")
            continue
        last = info["last_run"]
        run = (
            "no runs yet"
            if last is None
            else f"{str(last['finished_at'])[:16]} {last['command']}: {last['status']}, "
            f"{last['summary']}"
        )
        lines.append(f"{name} {info['version']}: {run}")
    timers = status["timers"]
    if isinstance(timers, str):
        lines.append(f"\nTimers {timers}")
    else:
        upcoming = sorted((t for t in timers if t["next"]), key=lambda t: t["next"])[:6]
        lines.append("\nNext runs (UTC):")
        lines += [f"{t['next'][:16]} {t['runs']}" for t in upcoming]
    return "\n".join(lines)


class Orchestrator:
    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        droids: Droids,
        store: Store,
        ask: Ask,
        poll_seconds: int = 50,
        watch_seconds: int = 60,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.droids = droids
        self.store = store
        self.ask = ask
        self.poll_seconds = poll_seconds
        self.watch_seconds = watch_seconds
        self._tasks: set[asyncio.Task[None]] = set()

    async def run_forever(self) -> None:
        self.seed_watch()
        await asyncio.gather(self.poll_loop(), self.watch_loop())

    # --- Telegram ---------------------------------------------------------------------------

    async def poll_loop(self) -> None:
        offset_text = self.store.get("telegram_offset")
        offset = int(offset_text) if offset_text else None
        while True:
            try:
                updates = await self.bot.updates(offset, self.poll_seconds)
            except TelegramError as exc:
                logger.warning("telegram_poll_failed error=%s", exc)
                await asyncio.sleep(15)
                continue
            for update in updates:
                offset = update.update_id + 1
                self.store.set("telegram_offset", str(offset))
                self.dispatch(update)

    def dispatch(self, update: Update) -> None:
        """Handle an update in the background, so polling continues during long answers."""
        task = asyncio.create_task(self.handle(update))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle(self, update: Update) -> None:
        if update.chat_id != self.chat_id:
            logger.warning("ignored_update chat_id=%s", update.chat_id)
            return
        try:
            if update.callback_id is not None:
                await self.handle_callback(update)
            elif update.text:
                await self.handle_text(update.text.strip())
        except Exception as exc:
            logger.exception("update_failed")
            await self._safe_send(f"Sorry, that failed ({type(exc).__name__}).")

    async def handle_text(self, text: str) -> None:
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
        if command in ("/start", "/help"):
            await self.bot.send(self.chat_id, HELP)
        elif command == "/status":
            await self.bot.send(self.chat_id, format_status(await self.droids.status()))
        elif command == "/advice":
            result = await self.droids.t2_3po("suggestions")
            await self.bot.send(self.chat_id, result.stdout.strip() or "No open suggestions.")
        elif command == "/reset":
            self.store.clear_history(self.chat_id)
            await self.bot.send(self.chat_id, "Conversation cleared.")
        else:
            await self.bot.typing(self.chat_id)
            answer = await self.ask(text, self.store.history(self.chat_id))
            self.store.add_exchange(self.chat_id, text, answer)
            await self.bot.send(self.chat_id, answer)

    async def handle_callback(self, update: Update) -> None:
        parsed = parse_callback(update.callback_data)
        assert update.callback_id is not None
        if parsed is None:
            await self.bot.answer_callback(update.callback_id, "Unknown button")
            return
        accept, suggestion_id = parsed
        try:
            result = await self.droids.decide(suggestion_id, accept, "via Telegram")
        except DroidError as exc:
            await self.bot.answer_callback(update.callback_id, str(exc)[:190])
            return
        if result.returncode != 0:
            message = (result.stderr.strip() or result.stdout.strip())[:190]
            await self.bot.answer_callback(update.callback_id, message or "That failed")
            return
        word = "Accepted" if accept else "Rejected"
        await self.bot.answer_callback(update.callback_id, f"{word} {suggestion_id}")
        if update.message_id is not None:
            await self.bot.set_buttons(self.chat_id, update.message_id, [[(f"{word} ✓", "noop")]])

    async def _safe_send(self, text: str, buttons: Buttons | None = None) -> None:
        try:
            await self.bot.send(self.chat_id, text, buttons)
        except TelegramError as exc:
            logger.error("telegram_send_failed error=%s", exc)

    # --- Watching ---------------------------------------------------------------------------

    def seed_watch(self) -> None:
        """On the first start, treat everything already there as seen, so nothing floods in."""
        if self.store.get("watch_seeded"):
            return
        for path in self.droids.advice_files():
            self.store.mark_seen("advice", path.name)
        for name in DROIDS:
            for path in self.droids.report_paths(name):
                self.store.mark_seen("report", f"{name}/{path.name}")
        self.store.set("watch_seeded", "1")

    async def watch_once(self) -> None:
        for path in self.droids.advice_files():
            if self.store.is_seen("advice", path.name):
                continue
            run = self.droids.read_advice(path)
            if run is None:
                continue  # still being written; try again next time
            for text, buttons in advice_messages(run):
                await self.bot.send(self.chat_id, text, buttons)
            self.store.mark_seen("advice", path.name)
        for name in DROIDS:
            for path in self.droids.report_paths(name):
                key = f"{name}/{path.name}"
                if self.store.is_seen("report", key):
                    continue
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if report.get("status") in FAILED:
                    await self.bot.send(self.chat_id, failure_alert(report))
                self.store.mark_seen("report", key)

    async def watch_loop(self) -> None:
        while True:
            try:
                await self.watch_once()
            except (TelegramError, OSError) as exc:
                logger.warning("watch_failed error=%s", exc)
            await asyncio.sleep(self.watch_seconds)
