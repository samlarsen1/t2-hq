"""Read-only access to the droids, and dry runs that can never trade."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from t2hq.droids import FORBIDDEN_ARGS, DroidError, Droids


async def test_status_reports_versions_last_runs_and_t2_timers_only(droids: Droids) -> None:
    status = await droids.status()
    d2 = status["droids"]["t2-d2"]
    assert d2["deployed"] is True
    assert d2["version"] is None  # not a git checkout in the test
    assert d2["last_run"]["status"] == "ok"
    assert "result" not in d2["last_run"]
    assert status["droids"]["t2-bb8"] == {"deployed": False, "version": None, "last_run": None}
    timers = status["timers"]
    assert [t["timer"] for t in timers] == ["t2d2-eu_morning.timer", "t2k2.timer"]
    assert timers[0]["last"] is None
    assert timers[1]["last"].startswith("2026-11-0")


async def test_status_survives_a_broken_systemctl(droids: Droids, tmp_path: Path) -> None:
    droids.systemctl = str(tmp_path / "missing-systemctl")
    status = await droids.status()
    assert status["timers"] == "unavailable: can't run systemctl (FileNotFoundError)"
    assert status["droids"]["t2-d2"]["deployed"] is True


def test_reports_and_config(droids: Droids, apps: Path) -> None:
    (apps / "t2-d2/var/runs/broken.json").write_text("{")
    assert len(droids.reports("t2-d2")) == 1
    assert droids.latest_report("t2-k2") is None
    config = droids.config("t2-d2")
    assert set(config) == {"config/stocks.yaml", "config/strategy.yaml", "config/schedules.yaml"}
    with pytest.raises(DroidError, match="no configuration"):
        droids.config("t2-bb8")
    with pytest.raises(DroidError, match="unknown droid"):
        droids.app("t2-xx")


@pytest.mark.parametrize(
    ("droid", "session"),
    [("t2-d2", "eu_morning"), ("t2-d2", "daily_close_wrap"), ("t2-k2", None)],
)
def test_dry_run_commands_never_trade(droids: Droids, droid: str, session: str | None) -> None:
    argv = droids.dry_run_command(droid, session)
    assert not FORBIDDEN_ARGS.intersection(argv)
    if droid == "t2-d2":
        assert argv[-1] == "--dry-run"


@pytest.mark.parametrize(
    ("droid", "session", "message"),
    [
        ("t2-d2", None, "needs a session"),
        ("t2-d2", "asia_morning", "needs a session"),
        ("t2-bb8", None, "only t2-d2 and t2-k2"),
        ("t2-3po", None, "only t2-d2 and t2-k2"),
    ],
)
def test_dry_run_rejects(droids: Droids, droid: str, session: str | None, message: str) -> None:
    with pytest.raises(DroidError, match=message):
        droids.dry_run_command(droid, session)


async def test_dry_run_switches_telegram_off_and_keeps_its_report_out_of_history(
    droids: Droids, apps: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "would-notify")
    report = await droids.dry_run("t2-d2", "eu_morning")

    assert report["status"] == "ok" and report["droid"] == "t2-d2"
    bin_dir = apps / "t2-d2/.venv/bin"
    assert (bin_dir / "calls.log").read_text().strip() == "--session eu_morning --dry-run"
    env = (bin_dir / "env.log").read_text()
    assert "TELEGRAM_BOT_TOKEN=[]" in env and "DRY_RUN=[true]" in env
    assert "/state/dry-runs/" in env
    # The droid's own history only has the scheduled run, and the temporary dir is gone.
    assert len(list((apps / "t2-d2/var/runs").glob("*.json"))) == 1
    assert list((droids.state_dir / "dry-runs").iterdir()) == []

    k2 = await droids.dry_run("t2-k2")
    assert k2["droid"] == "t2-k2"
    assert (apps / "t2-k2/.venv/bin/calls.log").read_text().strip() == ""


async def test_dry_run_without_a_report_fails(droids: Droids, apps: Path) -> None:
    (apps / "t2-k2/.venv/bin/t2k2-harvest").write_text(
        "#!/usr/bin/env bash\necho boom >&2\nexit 1\n"
    )
    with pytest.raises(DroidError, match="wrote no run report"):
        await droids.dry_run("t2-k2")


async def test_dry_run_of_an_undeployed_droid(droids: Droids, apps: Path) -> None:
    (apps / "t2-k2/.venv/bin/t2k2-harvest").unlink()
    with pytest.raises(DroidError, match="isn't deployed"):
        await droids.dry_run("t2-k2")


async def test_decide_checks_the_id_and_calls_t2_3po(droids: Droids, apps: Path) -> None:
    result = await droids.decide("e3a79f4f", True, "via Telegram")
    assert result.returncode == 0
    log = (apps / "t2-3po/.venv/bin/calls.log").read_text()
    assert "suggestions accept e3a79f4f --note via Telegram" in log
    with pytest.raises(DroidError, match="not a suggestion id"):
        await droids.decide("e3a79f4f; rm -rf /", False, "x")


def test_advice_files(droids: Droids, apps: Path) -> None:
    path = apps / "t2-3po/var/suggestions/2026-11-22_advice.json"
    path.write_text(json.dumps({"suggestions": []}))
    assert droids.advice_files() == [path]
    assert droids.read_advice(path) == {"suggestions": []}
