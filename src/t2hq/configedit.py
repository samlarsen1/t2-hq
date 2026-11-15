"""Apply a t2-3po suggestion to a droid's YAML, keeping its comments and layout.

Edits are small, line-based changes, never a reformat. Every edit is then verified: the edited
file must parse to exactly the original data with the one intended change, and the value being
changed must still be what the suggestion saw. Anything else raises ``EditError``.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

import yaml

_PLAIN = re.compile(r"^[A-Za-z0-9_.\-]+$")


class EditError(Exception):
    """The suggestion can't be applied safely."""


@dataclass(frozen=True)
class Change:
    """A suggestion, reduced to what's needed to edit one file."""

    kind: str
    file: str
    target: str
    current: str | None
    proposed: str
    details: dict[str, str]

    @classmethod
    def from_suggestion(cls, suggestion: dict[str, Any]) -> Change:
        try:
            return cls(
                kind=str(suggestion["kind"]),
                file=str(suggestion["file"]),
                target=str(suggestion["target"]),
                current=None if suggestion.get("current") is None else str(suggestion["current"]),
                proposed=str(suggestion["proposed"]),
                details={str(k): str(v) for k, v in (suggestion.get("details") or {}).items()},
            )
        except KeyError as exc:
            raise EditError(f"the suggestion has no {exc.args[0]!r}") from None


def _scalar(value: str) -> str:
    return value if _PLAIN.match(value) else json.dumps(value)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _block_end(lines: list[str], start: int, indent: int) -> int:
    """Index after the block that starts at ``start``: the lines indented deeper than ``indent``.

    Trailing blank and comment lines are left outside the block.
    """
    end = start + 1
    last = start
    while end < len(lines):
        line = lines[end]
        if not _is_blank(line):
            if _indent(line) <= indent:
                break
            last = end
        end += 1
    return last + 1


def _find_key(lines: list[str], key: str, start: int, stop: int, indent: int | None) -> int:
    """The line of ``key:`` in ``lines[start:stop]``, at the first indent past ``indent``."""
    pattern = re.compile(rf"^(\s*){re.escape(key)}:(\s|$)")
    child_indent: int | None = None
    for index in range(start, stop):
        line = lines[index]
        if _is_blank(line):
            continue
        level = _indent(line)
        if indent is not None and level <= indent:
            continue
        if child_indent is None:
            child_indent = level
        if level != child_indent:
            continue
        if pattern.match(line):
            return index
    raise EditError(f"no {key!r} key found")


def _find_list_item(lines: list[str], symbol: str) -> tuple[int, int]:
    """The ``- symbol: X`` item for a stock: its first line and its indent."""
    pattern = re.compile(
        rf"^(\s*)-\s+symbol:\s*['\"]?{re.escape(symbol)}['\"]?\s*(#.*)?$", re.IGNORECASE
    )
    found = [(i, _indent(line)) for i, line in enumerate(lines) if pattern.match(line)]
    if not found:
        if any(re.search(rf"symbol:\s*{re.escape(symbol)}\b", line) for line in lines):
            raise EditError(f"{symbol} is written in a layout this editor doesn't handle")
        raise EditError(f"{symbol} isn't in the file")
    if len(found) > 1:
        raise EditError(f"{symbol} appears more than once")
    return found[0]


def _replace_value(line: str, value: str) -> str:
    match = re.match(r"^(\s*[^:#]+:\s*)([^#]*?)(\s*#.*)?$", line)
    if match is None:
        raise EditError(f"can't edit the line {line.strip()!r}")
    return f"{match.group(1)}{value}{match.group(3) or ''}"


def set_stock_mode(text: str, symbol: str, current: str, new: str) -> str:
    lines = text.splitlines()
    start, indent = _find_list_item(lines, symbol)
    end = _block_end(lines, start, indent)
    for index in range(start, end):
        match = re.match(r"^\s*(-\s+)?strategy:\s*([^\s#]+)", lines[index])
        if match:
            if match.group(2).strip("'\"") != current:
                raise EditError(
                    f"{symbol} uses {match.group(2)} now, not {current}; the config has changed"
                )
            lines[index] = _replace_value(lines[index], _scalar(new))
            return "\n".join(lines) + "\n"
    raise EditError(f"{symbol} has no strategy line")


def add_stock(text: str, market: str, symbol: str, t212_ticker: str, name: str, mode: str) -> str:
    lines = text.splitlines()
    markets = _find_key(lines, "markets", 0, len(lines), None)
    markets_end = _block_end(lines, markets, _indent(lines[markets]))
    section = _find_key(lines, market, markets + 1, markets_end, _indent(lines[markets]))
    section_end = _block_end(lines, section, _indent(lines[section]))
    tickers = _find_key(lines, "tickers", section + 1, section_end, _indent(lines[section]))
    items = [
        i
        for i in range(tickers + 1, section_end)
        if re.match(r"^\s*-\s+symbol:", lines[i]) and _indent(lines[i]) > _indent(lines[tickers])
    ]
    if not items:
        raise EditError(f"the {market} market has no stocks to copy the layout from")
    item_indent = _indent(lines[items[0]])
    insert_at = _block_end(lines, items[-1], item_indent)
    pad = " " * item_indent
    block = [
        f"{pad}- symbol: {_scalar(symbol)}",
        f"{pad}  t212_ticker: {_scalar(t212_ticker)}",
        f"{pad}  name: {json.dumps(name)}",
        f"{pad}  strategy: {_scalar(mode)}",
    ]
    lines[insert_at:insert_at] = block
    return "\n".join(lines) + "\n"


def _mark_empty_list(lines: list[str], removed_at: int) -> None:
    """Write ``key: []`` if removing an item left its list key with no items."""
    above = next((i for i in range(removed_at - 1, -1, -1) if not _is_blank(lines[i])), None)
    if above is None or not re.match(r"^\s*[\w.-]+:\s*(#.*)?$", lines[above]):
        return
    below = next((i for i in range(removed_at, len(lines)) if not _is_blank(lines[i])), None)
    if below is None or _indent(lines[below]) <= _indent(lines[above]):
        key, _, comment = lines[above].partition("#")
        lines[above] = key.rstrip() + " []" + (f"  #{comment}" if comment else "")


def move_to_watchlist(text: str, symbol: str, current: str) -> str:
    lines = text.splitlines()
    start, indent = _find_list_item(lines, symbol)
    end = _block_end(lines, start, indent)
    block = lines[start:end]
    mode_line = next((line for line in block if re.match(r"^\s*strategy:", line)), None)
    if (
        mode_line is None
        or mode_line.split(":", 1)[1].split("#")[0].strip().strip("'\"") != current
    ):
        raise EditError(f"{symbol} isn't on {current} any more; the config has changed")
    name_line = next((line for line in block if re.match(r"^\s*name:", line)), None)
    del lines[start:end]
    _mark_empty_list(lines, start)

    try:
        watch = _find_key(lines, "watchlist", 0, len(lines), None)
    except EditError:
        lines += ["", "watchlist:"]
        watch = len(lines) - 1
    watch_end = _block_end(lines, watch, _indent(lines[watch]))
    existing = [i for i in range(watch + 1, watch_end) if re.match(r"^\s*-\s", lines[i])]
    pad = " " * (_indent(lines[existing[0]]) if existing else 2)
    entry = [f"{pad}- symbol: {_scalar(symbol)}"]
    if name_line is not None:
        entry.append(f"{pad}  name: {name_line.split(':', 1)[1].strip()}")
    lines[watch_end:watch_end] = entry
    return "\n".join(lines) + "\n"


def set_value(text: str, dotted: str, current: str, new: str) -> str:
    lines = text.splitlines()
    start, stop, parent_indent = 0, len(lines), None
    index = -1
    for key in dotted.split("."):
        index = _find_key(lines, key, start, stop, parent_indent)
        parent_indent = _indent(lines[index])
        start, stop = index + 1, _block_end(lines, index, parent_indent)
    raw = lines[index].split(":", 1)[1].split("#")[0].strip()
    try:
        if float(raw) != float(current):
            raise EditError(f"{dotted} is {raw} now, not {current}; the config has changed")
        number = float(new)
    except ValueError:
        raise EditError(f"{dotted} or the proposed value isn't a number") from None
    value = str(int(number)) if "." not in raw and number.is_integer() else str(number)
    lines[index] = _replace_value(lines[index], value)
    return "\n".join(lines) + "\n"


# --- Verification ---------------------------------------------------------------------------


def _expected(data: Any, change: Change) -> Any:
    """What the parsed file should look like after the change."""
    data = copy.deepcopy(data)
    if change.kind in ("change_mode", "remove_holding", "add_holding"):
        markets = data.get("markets") or {}
        if change.kind == "add_holding":
            d = change.details
            markets[d["market"]]["tickers"].append(
                {
                    "symbol": d["symbol"],
                    "t212_ticker": d["t212_ticker"],
                    "name": d["name"],
                    "strategy": change.proposed,
                }
            )
            return data
        for section in markets.values():
            for item in list(section.get("tickers") or []):
                if str(item.get("symbol")).upper() != change.target.upper():
                    continue
                if change.kind == "change_mode":
                    item["strategy"] = change.proposed
                else:
                    section["tickers"].remove(item)
                    entry = {"symbol": item["symbol"]}
                    if "name" in item:
                        entry["name"] = item["name"]
                    data.setdefault("watchlist", []).append(entry)
                return data
        raise EditError(f"{change.target} isn't in the file")
    node = data
    *parents, last = change.target.split(".")
    for key in parents:
        node = node[key]
    old = node[last]
    number = float(change.proposed)
    node[last] = int(number) if isinstance(old, int) and number.is_integer() else number
    return data


def apply_change(change: Change, text: str) -> str:
    """The edited file text, verified to contain exactly the intended change."""
    if change.kind == "change_mode":
        edited = set_stock_mode(text, change.target, change.current or "", change.proposed)
    elif change.kind == "add_holding":
        d = change.details
        missing = [k for k in ("market", "symbol", "t212_ticker", "name") if not d.get(k)]
        if missing:
            raise EditError(f"the suggestion is missing {', '.join(missing)}")
        edited = add_stock(
            text, d["market"], d["symbol"], d["t212_ticker"], d["name"], change.proposed
        )
    elif change.kind == "remove_holding":
        edited = move_to_watchlist(text, change.target, change.current or "")
    elif change.kind == "change_threshold":
        edited = set_value(text, change.target, change.current or "", change.proposed)
    else:
        raise EditError(f"unknown kind of suggestion: {change.kind}")
    try:
        before = yaml.safe_load(text)
        after = yaml.safe_load(edited)
        expected = _expected(before, change)
    except (yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise EditError(f"can't verify the edit ({type(exc).__name__})") from None
    if after != expected:
        raise EditError("the edit changed more than the suggestion; nothing was written")
    return edited
