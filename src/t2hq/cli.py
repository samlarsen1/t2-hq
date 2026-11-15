"""Command line entry point: ``t2-hq run|status|ask|digest``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import ModelMessage

from .agent import Deps, build_agent
from .config import Settings
from .digest import advice_messages
from .droids import Droids
from .llm import build_model
from .service import Ask, Orchestrator, format_status
from .store import Store
from .telegram import Telegram

logger = logging.getLogger("t2hq")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2


def _droids(settings: Settings) -> Droids:
    return Droids(
        settings.apps_dir.resolve(),
        settings.state_dir,
        dry_run_timeout=settings.dry_run_timeout_seconds,
    )


def _asker(settings: Settings, droids: Droids) -> Ask:
    agent = build_agent(build_model(settings))

    async def ask(question: str, history: list[ModelMessage]) -> str:
        try:
            result = await agent.run(question, deps=Deps(droids), message_history=history)
        except AgentRunError as exc:
            logger.error("agent_failed error=%s", type(exc).__name__)
            return f"I couldn't answer that: the model failed ({type(exc).__name__})."
        return result.output

    return ask


async def _serve(settings: Settings) -> int:
    if settings.telegram_bot_token is None or settings.telegram_chat_id is None:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        return EXIT_CONFIG
    droids = _droids(settings)
    try:
        ask = _asker(settings, droids)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    telegram = Telegram(settings.telegram_bot_token.get_secret_value())
    store = Store(settings.state_dir / "t2-hq.db")
    orchestrator = Orchestrator(
        telegram,
        settings.telegram_chat_id,
        droids,
        store,
        ask,
        poll_seconds=settings.telegram_poll_seconds,
        watch_seconds=settings.watch_seconds,
    )
    logger.info("t2hq_started apps_dir=%s", settings.apps_dir.resolve())
    try:
        await orchestrator.run_forever()
    finally:
        await telegram.aclose()
        store.close()
    return EXIT_OK


async def _status(settings: Settings) -> int:
    print(format_status(await _droids(settings).status()))
    return EXIT_OK


async def _ask(settings: Settings, question: str) -> int:
    droids = _droids(settings)
    try:
        ask = _asker(settings, droids)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(await ask(question, []))
    return EXIT_OK


def _digest(settings: Settings) -> int:
    droids = _droids(settings)
    files = droids.advice_files()
    if not files:
        print("No advice yet.", file=sys.stderr)
        return EXIT_FAILED
    run = droids.read_advice(files[-1])
    if run is None:
        print(f"Can't read {files[-1]}", file=sys.stderr)
        return EXIT_FAILED
    for text, buttons in advice_messages(run):
        print(text)
        if buttons:
            print("   [" + "] [".join(label for row in buttons for label, _ in row) + "]")
        print()
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="t2-hq", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Run the orchestrator: Telegram bot and watcher")
    commands.add_parser("status", help="Print each droid's version, last run and next timers")
    ask = commands.add_parser("ask", help="Ask the assistant one question and print the answer")
    ask.add_argument("question")
    commands.add_parser("digest", help="Print the latest advice digest as it would be sent")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    args = _parser().parse_args(argv)
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    if args.command == "run":
        return asyncio.run(_serve(settings))
    if args.command == "status":
        return asyncio.run(_status(settings))
    if args.command == "ask":
        return asyncio.run(_ask(settings, args.question))
    return _digest(settings)


if __name__ == "__main__":
    sys.exit(main())
