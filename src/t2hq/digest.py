"""Messages the orchestrator sends on its own: the advice digest, review summaries and alerts.

They are formatted by code from structured data, not written by the model.
"""

from __future__ import annotations

from typing import Any

from .droids import report_summary
from .telegram import Buttons

ACCEPT = "acc"
REJECT = "rej"
OPEN_PR = "pr"


def advice_messages(run: dict[str, Any]) -> list[tuple[str, Buttons | None]]:
    """One header message, then one message per suggestion with accept and reject buttons."""
    suggestions = run.get("suggestions") or []
    created = str(run.get("created_at", ""))[:10]
    header = f"T2-3PO strategy advice, {created}: {len(suggestions)} suggestion(s)."
    if not suggestions:
        header += "\n\nNo changes suggested."
    no_change = str(run.get("no_change") or "").strip()
    if no_change:
        header += f"\n\nKeep as is: {no_change}"
    dropped = run.get("dropped") or []
    if dropped:
        header += f"\n\n{len(dropped)} draft(s) were dropped by the checks; see the report."
    messages: list[tuple[str, Buttons | None]] = [(header, None)]
    for number, s in enumerate(suggestions, start=1):
        evidence = "\n".join(f"- {item}" for item in s.get("evidence") or [])
        repeat = " (suggested before)" if s.get("repeat") else ""
        text = (
            f"{number}. {_title(s)}{repeat}\n"
            f"Confidence: {s.get('confidence')}\n"
            f"Change: {s.get('droid')} {s.get('file')}: {s.get('target')} "
            f"{s.get('current') or '(new)'} -> {s.get('proposed')}\n\n"
            f"Why: {s.get('rationale')}\n"
            + (f"Evidence:\n{evidence}\n" if evidence else "")
            + f"Effect: {s.get('expected_effect')}\n"
            f"Risk: {s.get('risk')}\n"
            f"Reviewer: {s.get('reviewer_note')}\n\n"
            f"Open PR: t2-hq makes the change in a pull request for you to review. "
            f"Accept: you've made it yourself. Reject: it isn't suggested again. "
            f"Id {s.get('id')}"
        )
        buttons: Buttons = [
            [("📝 Open PR", f"{OPEN_PR}:{s.get('id')}")],
            [("✅ Accept", f"{ACCEPT}:{s.get('id')}"), ("❌ Reject", f"{REJECT}:{s.get('id')}")],
        ]
        messages.append((text, buttons))
    return messages


def _title(s: dict[str, Any]) -> str:
    details = s.get("details") or {}
    target = details.get("symbol", s.get("target"))
    kind = s.get("kind")
    if kind == "change_mode":
        return f"Move {target} from {s.get('current')} to {s.get('proposed')}"
    if kind == "add_holding":
        return f"Add {target} ({details.get('name', '')}) as {s.get('proposed')}"
    if kind == "remove_holding":
        return f"Move {target} to the watchlist"
    return f"Change {s.get('droid')} {s.get('target')} to {s.get('proposed')}"


SUMMARY_LINES = 5
SUMMARY_LINE_CHARS = 200


def review_messages(report: dict[str, Any]) -> list[str]:
    """One message per stock in a t2-3po ``review`` run report: its recommendation in five lines.

    The lines come from t2-3po's ``result.stocks[].summary``; they are cut to five lines of 200
    characters here too, so a long summary can't flood the chat. Stocks without one are skipped.
    """
    if report.get("droid") != "t2-3po" or report.get("command") != "review":
        return []
    result = report.get("result")
    stocks = result.get("stocks") if isinstance(result, dict) else None
    if not isinstance(stocks, list):
        return []
    label = "T2-3PO review (dry run)" if report.get("dry_run") else "T2-3PO review"
    messages = []
    for entry in stocks:
        summary = entry.get("summary") if isinstance(entry, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            continue
        lines = [line[:SUMMARY_LINE_CHARS] for line in summary.strip().splitlines()]
        messages.append(f"🔭 {label}\n" + "\n".join(lines[:SUMMARY_LINES]))
    return messages


def failure_alert(report: dict[str, Any]) -> str:
    summary = report_summary(report)
    errors = "; ".join(str(e) for e in summary.get("errors") or [])[:500]
    return (
        f"⚠️ {summary.get('droid')} {summary.get('command')}: {summary.get('status')} "
        f"at {str(summary.get('finished_at'))[:16]} UTC\n"
        f"{summary.get('summary') or ''}" + (f"\nErrors: {errors}" if errors else "")
    )


def parse_callback(data: str | None) -> tuple[str, str] | None:
    """(action, suggestion id) from an advice button's data, or None for any other button."""
    if not data or ":" not in data:
        return None
    action, _, suggestion_id = data.partition(":")
    if action not in (ACCEPT, REJECT, OPEN_PR):
        return None
    return action, suggestion_id
