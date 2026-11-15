"""Applying suggestions to droid YAML: minimal edits, comments kept, every edit verified."""

from __future__ import annotations

import pytest
import yaml

from t2hq import configedit
from t2hq.configedit import Change, EditError, apply_change

STOCKS = """\
# T2-D2 Stock Configuration
markets:
  uk:
    currency: GBP
    tickers:
      - symbol: RIO
        t212_ticker: RIOl_EQ
        name: Rio Tinto
        strategy: mild   # steady miner
      - symbol: NG.
        t212_ticker: NGl_EQ
        name: National Grid
        strategy: mild

  us:
    currency: USD
    tickers:
      - symbol: TSLA
        t212_ticker: TSLA_US_EQ
        name: Tesla
        strategy: moderate

# Global watchlist
watchlist:
  - symbol: SPY
    name: S&P 500 ETF
"""

STRATEGY = """\
modes:
  mild:
    description: >
      Conservative DCA.
    # Minimum daily decline before considering a buy
    min_daily_dip_pct: -3.0
    max_portfolio_weight_pct: 10.0
  moderate:
    min_daily_dip_pct: -2.0
overbought:
  rsi_min: 70.0   # inclusive
  min_signals: 2
"""


def change(kind: str, target: str, current: str | None, proposed: str, **details: str) -> Change:
    return Change(kind, "config/x.yaml", target, current, proposed, dict(details))


def changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    return [(a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=False) if a != b]


def test_change_mode_edits_one_line_and_keeps_its_comment() -> None:
    out = apply_change(change("change_mode", "RIO", "mild", "moderate"), STOCKS)
    assert changed_lines(STOCKS, out) == [
        ("        strategy: mild   # steady miner", "        strategy: moderate   # steady miner")
    ]
    assert out.startswith("# T2-D2 Stock Configuration\n")


def test_change_mode_matches_symbols_with_dots_and_any_case() -> None:
    out = apply_change(change("change_mode", "ng.", "mild", "deep_value"), STOCKS)
    assert yaml.safe_load(out)["markets"]["uk"]["tickers"][1]["strategy"] == "deep_value"


def test_add_holding_appends_to_its_market_in_the_same_layout() -> None:
    out = apply_change(
        change(
            "add_holding",
            "SHELl_EQ",
            None,
            "mild",
            market="uk",
            symbol="SHEL",
            t212_ticker="SHELl_EQ",
            name="Shell: Energy & Co",
        ),
        STOCKS,
    )
    lines = out.splitlines()
    at = lines.index("      - symbol: SHEL")
    assert lines[at - 1] == "        strategy: mild"  # after NG., before the blank line
    assert lines[at + 1 : at + 4] == [
        "        t212_ticker: SHELl_EQ",
        '        name: "Shell: Energy & Co"',
        "        strategy: mild",
    ]
    assert lines[at + 4] == ""
    assert yaml.safe_load(out)["markets"]["uk"]["tickers"][-1]["name"] == "Shell: Energy & Co"


def test_remove_holding_moves_the_stock_to_the_watchlist() -> None:
    out = apply_change(change("remove_holding", "TSLA", "moderate", "watchlist"), STOCKS)
    data = yaml.safe_load(out)
    assert data["markets"]["us"]["tickers"] == []
    assert "    tickers: []" in out.splitlines()  # the last stock in its market
    assert data["watchlist"][-1] == {"symbol": "TSLA", "name": "Tesla"}

    rio = yaml.safe_load(apply_change(change("remove_holding", "RIO", "mild", "watchlist"), STOCKS))
    assert [s["symbol"] for s in rio["markets"]["uk"]["tickers"]] == ["NG."]
    assert "# Global watchlist" in out


@pytest.mark.parametrize(
    ("target", "current", "proposed", "old", "new"),
    [
        (
            "modes.mild.min_daily_dip_pct",
            "-3",
            "-2.5",
            "    min_daily_dip_pct: -3.0",
            "    min_daily_dip_pct: -2.5",
        ),
        (
            "modes.moderate.min_daily_dip_pct",
            "-2",
            "-1.5",
            "    min_daily_dip_pct: -2.0",
            "    min_daily_dip_pct: -1.5",
        ),
        (
            "overbought.rsi_min",
            "70",
            "75",
            "  rsi_min: 70.0   # inclusive",
            "  rsi_min: 75.0   # inclusive",
        ),
        ("overbought.min_signals", "2", "3", "  min_signals: 2", "  min_signals: 3"),
    ],
)
def test_change_threshold_keeps_number_style_and_comments(
    target: str, current: str, proposed: str, old: str, new: str
) -> None:
    out = apply_change(change("change_threshold", target, current, proposed), STRATEGY)
    assert changed_lines(STRATEGY, out) == [(old, new)]


@pytest.mark.parametrize(
    ("the_change", "text", "message"),
    [
        (change("change_mode", "RIO", "moderate", "aggressive"), STOCKS, "the config has changed"),
        (
            change("change_threshold", "overbought.rsi_min", "65", "75"),
            STRATEGY,
            "the config has changed",
        ),
        (
            change("remove_holding", "RIO", "aggressive", "watchlist"),
            STOCKS,
            "the config has changed",
        ),
        (change("change_mode", "AAPL", "mild", "moderate"), STOCKS, "isn't in the file"),
        (
            change("change_threshold", "modes.turbo.min_daily_dip_pct", "-3", "-2"),
            STRATEGY,
            "no 'turbo' key",
        ),
        (
            change("add_holding", "X", None, "mild", market="uk", symbol="X"),
            STOCKS,
            "missing t212_ticker, name",
        ),
        (
            change(
                "add_holding",
                "X_EQ",
                None,
                "mild",
                market="asia",
                symbol="X",
                t212_ticker="X_EQ",
                name="X",
            ),
            STOCKS,
            "no 'asia' key",
        ),
        (
            change("change_mode", "RIO", "mild", "moderate"),
            "markets:\n  uk:\n    tickers:\n      - {symbol: RIO, strategy: mild}\n",
            "layout",
        ),
        (
            change("change_mode", "RIO", "mild", "moderate"),
            STOCKS + "  - symbol: RIO\n",
            "more than once",
        ),
        (change("buy_now", "RIO", "mild", "moderate"), STOCKS, "unknown kind"),
    ],
)
def test_refuses_what_it_cannot_apply_safely(the_change: Change, text: str, message: str) -> None:
    with pytest.raises(EditError, match=message):
        apply_change(the_change, text)


def test_verification_catches_an_edit_that_changes_more(monkeypatch: pytest.MonkeyPatch) -> None:
    real = configedit.set_stock_mode

    def sloppy(text: str, symbol: str, current: str, new: str) -> str:
        return real(text, symbol, current, new).replace("currency: USD", "currency: EUR")

    monkeypatch.setattr(configedit, "set_stock_mode", sloppy)
    with pytest.raises(EditError, match="changed more than the suggestion"):
        apply_change(change("change_mode", "RIO", "mild", "moderate"), STOCKS)


def test_change_from_a_t2_3po_suggestion() -> None:
    suggestion = {
        "id": "e3a79f4f",
        "kind": "change_mode",
        "file": "config/stocks.yaml",
        "target": "TSLA",
        "current": "moderate",
        "proposed": "mild",
        "details": {"symbol": "TSLA"},
    }
    assert Change.from_suggestion(suggestion).target == "TSLA"
    with pytest.raises(EditError, match="no 'file'"):
        Change.from_suggestion({k: v for k, v in suggestion.items() if k != "file"})
