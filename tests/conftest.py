"""Fake deployed apps: droid executables are scripts that record how they were called."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from t2hq.droids import Droids

SCHEDULES = """
sessions:
  eu_morning: {schedule: "Mon..Fri 08:30 Europe/London", enabled: true, markets: [uk]}
  daily_close_wrap: {schedule: "Mon..Fri 17:00 America/New_York", enabled: true, markets: [us]}
"""

# Writes a run report to $T2_RUNS_DIR and records its argv and Telegram/dry-run environment.
DROID_SCRIPT = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$(dirname "$0")/calls.log"
printf 'TELEGRAM_BOT_TOKEN=[%s] DRY_RUN=[%s] T2_RUNS_DIR=[%s]\\n' \\
  "$TELEGRAM_BOT_TOKEN" "$DRY_RUN" "$T2_RUNS_DIR" >> "$(dirname "$0")/env.log"
mkdir -p "${T2_RUNS_DIR:-var/runs}"
cat > "${T2_RUNS_DIR:-var/runs}/20261116T090000Z-run.json" <<JSON
{"schema": "t2.run-report/1", "droid": "DROID", "version": "0.2.0", "command": "dry",
 "argv": [], "started_at": "2026-11-16T09:00:00+00:00",
 "finished_at": "2026-11-16T09:00:05+00:00", "duration_seconds": 5, "dry_run": true,
 "status": "ok", "exit_code": 0, "summary": "3 decisions, 0 trades simulated",
 "result": {"decisions": []}, "errors": []}
JSON
"""

T2_3PO_SCRIPT = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$(dirname "$0")/calls.log"
if [[ "$1" == suggestions && "$2" == accept && "$3" == ffffffff ]]; then
  echo "No suggestion with id ffffffff." >&2; exit 2
fi
if [[ "$1" == suggestions && -z "$2" ]]; then echo "e3a79f4f  open  2026-11-22  HIGH  Move TSLA"; fi
if [[ "$1" == suggestions && "$2" == --all ]]; then echo "e3a79f4f  accepted  ..."; fi
if [[ "$2" == accept || "$2" == reject ]]; then echo "$3 recorded"; fi
"""

SYSTEMCTL_SCRIPT = """#!/usr/bin/env bash
cat <<'JSON'
[{"next": 1794817800000000, "last": 0, "unit": "t2d2-eu_morning.timer",
  "activates": "t2d2@eu_morning.service"},
 {"next": 1794990000000000, "last": 1794212345000000, "unit": "t2k2.timer",
  "activates": "t2k2.service"},
 {"next": 1795000000000000, "last": 0, "unit": "apt-daily.timer", "activates": "apt-daily.service"}]
JSON
"""


def write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def report(droid: str, stamp: str, status: str = "ok", summary: str = "fine") -> str:
    return json.dumps(
        {
            "schema": "t2.run-report/1",
            "droid": droid,
            "version": "0.2.0",
            "command": "session eu_morning" if droid == "t2-d2" else "harvest",
            "argv": [],
            "started_at": f"2026-11-{stamp}T08:30:00+00:00",
            "finished_at": f"2026-11-{stamp}T08:31:00+00:00",
            "duration_seconds": 60,
            "dry_run": False,
            "status": status,
            "exit_code": 0 if status == "ok" else 1,
            "summary": summary,
            "result": {"decisions": [{"ticker": "RIO"}]},
            "errors": [] if status == "ok" else ["HTTPStatusError"],
        }
    )


ADVICE: dict[str, Any] = {
    "created_at": "2026-11-22T08:05:00Z",
    "droids": ["t2-d2", "t2-k2"],
    "no_change": "Everything else stays.",
    "notes": [],
    "dropped": [{"draft": {}, "reason": "reviewer: no"}],
    "suggestions": [
        {
            "id": "e3a79f4f",
            "created_at": "2026-11-22T08:05:00Z",
            "kind": "change_mode",
            "droid": "t2-d2",
            "file": "config/stocks.yaml",
            "target": "TSLA",
            "current": "moderate",
            "proposed": "mild",
            "details": {"symbol": "TSLA", "t212_ticker": "TSLA_US_EQ"},
            "rationale": "Overweight.",
            "evidence": ["weight=21.5%"],
            "expected_effect": "Fewer buys.",
            "risk": "Misses rebounds.",
            "confidence": "HIGH",
            "reviewer_note": "Holds up.",
            "repeat": False,
        }
    ],
}


@pytest.fixture
def apps(tmp_path: Path) -> Path:
    apps = tmp_path / "apps"
    d2 = apps / "t2-d2"
    write_exec(d2 / ".venv/bin/t2d2", DROID_SCRIPT.replace("DROID", "t2-d2"))
    (d2 / "config").mkdir(parents=True)
    (d2 / "config/schedules.yaml").write_text(SCHEDULES)
    (d2 / "config/stocks.yaml").write_text("markets: {}\n")
    (d2 / "config/strategy.yaml").write_text("modes: {mild: {}}\n")
    (d2 / "var/runs").mkdir(parents=True)
    (d2 / "var/runs/20261115T083000Z-session-eu-morning.json").write_text(report("t2-d2", "15"))
    k2 = apps / "t2-k2"
    write_exec(k2 / ".venv/bin/t2k2-harvest", DROID_SCRIPT.replace("DROID", "t2-k2"))
    (k2 / "config").mkdir(parents=True)
    (k2 / "config/strategy.yaml").write_text("overbought: {rsi_min: 70}\n")
    po = apps / "t2-3po"
    write_exec(po / ".venv/bin/t2-3po", T2_3PO_SCRIPT)
    (po / "var/suggestions").mkdir(parents=True)
    write_exec(tmp_path / "bin/systemctl", SYSTEMCTL_SCRIPT)
    return apps


@pytest.fixture
def droids(apps: Path, tmp_path: Path) -> Droids:
    return Droids(
        apps, tmp_path / "state", dry_run_timeout=30, systemctl=str(tmp_path / "bin/systemctl")
    )


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see the developer's Telegram or model settings."""
    for name in list(os.environ):
        if name.startswith(("TELEGRAM_", "GOOGLE_", "T2_")):
            monkeypatch.delenv(name)
