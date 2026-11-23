"""The orchestrator: who it answers, commands, buttons, the digest and failure alerts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage

from conftest import ADVICE, report
from t2hq.digest import advice_messages, failure_alert, parse_callback, review_messages
from t2hq.droids import Droids
from t2hq.prs import PullRequest, PullRequestError
from t2hq.service import Orchestrator
from t2hq.store import HISTORY_EXCHANGES, Store
from t2hq.telegram import Buttons, Update

CHAT = 42


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Buttons | None]] = []
        self.answers: list[str] = []
        self.buttons: list[Buttons | None] = []

    async def updates(self, offset: int | None, timeout: int) -> list[Update]:
        return []

    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> int:
        assert chat_id == CHAT
        self.sent.append((text, buttons))
        return len(self.sent)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answers.append(text)

    async def set_buttons(self, chat_id: int, message_id: int, buttons: Buttons | None) -> None:
        self.buttons.append(buttons)

    async def typing(self, chat_id: int) -> None:
        pass


@pytest.fixture
def setup(droids: Droids, tmp_path: Path) -> tuple[Orchestrator, FakeBot, list[str]]:
    bot = FakeBot()
    questions: list[str] = []

    async def ask(question: str, history: list[ModelMessage]) -> str:
        questions.append(f"{question} (history {len(history)})")
        return f"answer to {question}"

    orchestrator = Orchestrator(bot, CHAT, droids, Store(tmp_path / "state.db"), ask)
    return orchestrator, bot, questions


def text(update_id: int, body: str, chat: int = CHAT) -> Update:
    return Update(update_id=update_id, chat_id=chat, text=body, message_id=update_id)


async def test_ignores_other_chats(setup: tuple[Orchestrator, FakeBot, list[str]]) -> None:
    orchestrator, bot, questions = setup
    await orchestrator.handle(text(1, "/status", chat=999))
    await orchestrator.handle(text(2, "sell everything", chat=999))
    assert bot.sent == [] and questions == []


async def test_commands(setup: tuple[Orchestrator, FakeBot, list[str]]) -> None:
    orchestrator, bot, _ = setup
    await orchestrator.handle(text(1, "/help"))
    await orchestrator.handle(text(2, "/status@t2_bot"))
    await orchestrator.handle(text(3, "/advice"))
    help_text, status, advice = (message for message, _ in bot.sent)
    assert "never trade" in help_text
    assert "t2-d2 None: 2026-11-15T08:31 session eu_morning: ok, fine" in status
    assert "t2-bb8: not deployed" in status
    assert "2026-11-16T08:30 t2d2@eu_morning.service" in status
    assert "Move TSLA" in advice


async def test_questions_go_to_the_agent_with_history(
    setup: tuple[Orchestrator, FakeBot, list[str]],
) -> None:
    orchestrator, bot, questions = setup
    for n in range(HISTORY_EXCHANGES + 2):
        await orchestrator.handle(text(n, f"q{n}"))
    assert questions[0] == "q0 (history 0)"
    assert questions[1] == "q1 (history 2)"
    assert questions[-1] == f"q{HISTORY_EXCHANGES + 1} (history {2 * HISTORY_EXCHANGES})"
    assert bot.sent[-1][0] == f"answer to q{HISTORY_EXCHANGES + 1}"
    await orchestrator.handle(text(99, "/reset"))
    await orchestrator.handle(text(100, "again"))
    assert questions[-1] == "again (history 0)"


async def test_a_failing_handler_replies_instead_of_crashing(
    setup: tuple[Orchestrator, FakeBot, list[str]],
) -> None:
    orchestrator, bot, _ = setup

    async def broken(question: str, history: list[ModelMessage]) -> str:
        raise RuntimeError("model down")

    orchestrator.ask = broken
    await orchestrator.handle(text(1, "hello"))
    assert bot.sent[-1][0] == "Sorry, that failed (RuntimeError)."


async def test_buttons_record_answers(
    setup: tuple[Orchestrator, FakeBot, list[str]], apps: Path
) -> None:
    orchestrator, bot, _ = setup
    press = Update(
        update_id=1, chat_id=CHAT, message_id=5, callback_id="cb", callback_data="rej:e3a79f4f"
    )
    await orchestrator.handle(press)
    assert bot.answers == ["Rejected e3a79f4f"]
    assert bot.buttons == [[[("Rejected ✓", "noop")]]]
    assert (
        "suggestions reject e3a79f4f --note via Telegram"
        in (apps / "t2-3po/.venv/bin/calls.log").read_text()
    )

    unknown = Update(
        update_id=2, chat_id=CHAT, message_id=6, callback_id="cb", callback_data="acc:ffffffff"
    )
    await orchestrator.handle(unknown)
    assert bot.answers[-1] == "No suggestion with id ffffffff."

    bogus = Update(
        update_id=3, chat_id=CHAT, message_id=7, callback_id="cb", callback_data="acc:x; ls"
    )
    await orchestrator.handle(bogus)
    assert "not a suggestion id" in bot.answers[-1]


async def test_watch_sends_new_advice_once_and_alerts_on_failures(
    setup: tuple[Orchestrator, FakeBot, list[str]], apps: Path
) -> None:
    orchestrator, bot, _ = setup
    suggestions = apps / "t2-3po/var/suggestions"
    (suggestions / "2026-11-15_advice.json").write_text(json.dumps(ADVICE))
    orchestrator.seed_watch()  # existing advice and reports count as already seen
    await orchestrator.watch_once()
    assert bot.sent == []

    (suggestions / "2026-11-22_advice.json").write_text(json.dumps(ADVICE))
    runs = apps / "t2-d2/var/runs"
    (runs / "20261116T083000Z-session-eu-morning.json").write_text(
        report("t2-d2", "16", "failed", "boom")
    )
    (runs / "20261116T150000Z-session-us-morning.json").write_text(report("t2-d2", "16"))
    await orchestrator.watch_once()
    await orchestrator.watch_once()

    texts = [message for message, _ in bot.sent]
    assert texts[0].startswith("T2-3PO strategy advice, 2026-11-22: 1 suggestion(s).")
    assert texts[1].startswith("1. Move TSLA from moderate to mild")
    assert bot.sent[1][1] == [
        [("📝 Open PR", "pr:e3a79f4f")],
        [("✅ Accept", "acc:e3a79f4f"), ("❌ Reject", "rej:e3a79f4f")],
    ]
    assert texts[2].startswith("⚠️ t2-d2 session eu_morning: failed")
    assert len(texts) == 3


def _review(dry_run: bool = False, status: str = "ok") -> dict[str, Any]:
    summary = "\n".join(
        [
            "CWR Ceres Power: HOLD at 2.5% (VALIDATED_WITH_RISKS, GROWTH)",
            "Pillars: Electrification, Renewables",
            "Strategy: mild, no change",
            "Why: Strong net cash.",
            "Risk: Partner delays.",
        ]
    )
    data: dict[str, Any] = json.loads(report("t2-3po", "24", status, "1 of 2 stocks reviewed"))
    data["command"] = "review"
    data["dry_run"] = dry_run
    data["result"] = {
        "stocks": [
            {"symbol": "CWR", "summary": summary},
            {"symbol": "OLD", "status": "VALIDATED"},  # from a t2-3po without summaries
        ],
        "failed": ["TRIG"],
    }
    return data


async def test_watch_sends_each_reviewed_stock_then_the_failure(
    setup: tuple[Orchestrator, FakeBot, list[str]], apps: Path
) -> None:
    orchestrator, bot, _ = setup
    orchestrator.seed_watch()
    runs = apps / "t2-3po/var/runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "20261124T070700Z-review.json").write_text(json.dumps(_review(status="failed")))
    await orchestrator.watch_once()
    await orchestrator.watch_once()

    texts = [message for message, _ in bot.sent]
    assert len(texts) == 2
    assert texts[0].splitlines()[:2] == [
        "🔭 T2-3PO review",
        "CWR Ceres Power: HOLD at 2.5% (VALIDATED_WITH_RISKS, GROWTH)",
    ]
    assert texts[1].startswith("⚠️ t2-3po review: failed")


def test_review_messages_are_capped_and_only_for_reviews() -> None:
    long = _review(dry_run=True)
    long["result"] = {"stocks": [{"summary": "\n".join(["x" * 500] * 9)}]}
    [message] = review_messages(long)
    lines = message.splitlines()
    assert lines[0] == "🔭 T2-3PO review (dry run)"
    assert len(lines) == 1 + 5 and all(len(line) <= 200 for line in lines)
    advise = _review()
    advise["command"] = "advise"
    assert review_messages(advise) == []
    assert review_messages(json.loads(report("t2-k2", "17"))) == []


def test_digest_and_alert_formatting() -> None:
    header, item = advice_messages(ADVICE)
    assert "Keep as is: Everything else stays." in header[0]
    assert "1 draft(s) were dropped" in header[0]
    assert "Change: t2-d2 config/stocks.yaml: TSLA moderate -> mild" in item[0]
    assert "Evidence:\n- weight=21.5%" in item[0]
    empty = advice_messages({"created_at": "2026-11-22", "suggestions": []})
    assert len(empty) == 1 and "No changes suggested." in empty[0][0]
    alert = failure_alert(json.loads(report("t2-k2", "17", "failed", "API down")))
    assert alert.startswith("⚠️ t2-k2 harvest: failed at 2026-11-17T08:31 UTC")
    assert "Errors: HTTPStatusError" in alert
    assert parse_callback("acc:abc") == ("acc", "abc")
    assert parse_callback("rej:abc") == ("rej", "abc")
    assert parse_callback("pr:abc") == ("pr", "abc")
    assert parse_callback("noop") is None and parse_callback("buy:abc") is None


def test_store_offset_and_seen(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    assert store.get("telegram_offset") is None
    store.set("telegram_offset", "5")
    store.set("telegram_offset", "6")
    assert store.get("telegram_offset") == "6"
    assert store.mark_seen("advice", "a") is True
    assert store.mark_seen("advice", "a") is False
    assert store.is_seen("advice", "a") and not store.is_seen("report", "a")


class FakePullRequests:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.opened: list[str] = []

    async def open(self, suggestion_id: str) -> PullRequest:
        if self.fail:
            raise PullRequestError("the config has changed")
        self.opened.append(suggestion_id)
        return PullRequest("https://github.com/owner/t2-d2/pull/31", created=True)


async def test_open_pr_button(setup: tuple[Orchestrator, FakeBot, list[str]], apps: Path) -> None:
    orchestrator, bot, _ = setup
    press = Update(
        update_id=1, chat_id=CHAT, message_id=5, callback_id="cb", callback_data="pr:e3a79f4f"
    )

    await orchestrator.handle(press)
    assert bot.answers == ["Pull requests aren't set up"]

    fake = FakePullRequests()
    orchestrator.pull_requests = fake  # type: ignore[assignment]
    await orchestrator.handle(press)
    assert fake.opened == ["e3a79f4f"]
    assert bot.answers[-1] == "Opening a pull request..."
    assert bot.sent[-1][0].startswith("Opened https://github.com/owner/t2-d2/pull/31")
    assert bot.buttons[-1] == [[("PR opened ✓", "noop")]]
    log = (apps / "t2-3po/.venv/bin/calls.log").read_text()
    assert (
        "suggestions accept e3a79f4f --note pull request https://github.com/owner/t2-d2/pull/31"
        in log
    )

    orchestrator.pull_requests = FakePullRequests(fail=True)  # type: ignore[assignment]
    await orchestrator.handle(press)
    assert bot.sent[-1][0] == "Couldn't open a pull request for e3a79f4f: the config has changed"

    bad = Update(
        update_id=2, chat_id=CHAT, message_id=6, callback_id="cb", callback_data="pr:../../x"
    )
    await orchestrator.handle(bad)
    assert bot.answers[-1] == "Not a suggestion id"
