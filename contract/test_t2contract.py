"""Tests for the reference copy of the T2 droid contract module."""

from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path

import pytest

import t2contract as c


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T2_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("T2_RUNS_DIR", str(tmp_path / "runs"))


def _hold(lock_dir: str, name: str, ready: object, release: object) -> None:
    import os

    os.environ["T2_LOCK_DIR"] = lock_dir
    lock = c.run_lock(name) if name != "t212" else c.t212_lock(0)
    with lock:
        ready.set()  # type: ignore[attr-defined]
        release.wait(10)  # type: ignore[attr-defined]


def _held_by_other_process(tmp_path: Path, name: str) -> tuple[object, object, object]:
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold, args=(str(tmp_path / "locks"), name, ready, release))
    proc.start()
    assert ready.wait(10)
    return proc, ready, release


def test_report_shape_and_default_path(tmp_path: Path) -> None:
    report = c.RunReport(droid="t2-x", version="1.2.3", command="Session eu_morning", argv=["-v"])
    assert report.finish(c.Exit.OK, "2 trades", {"trades": 2}) is c.Exit.OK
    path = report.write()

    assert path.parent == tmp_path / "runs"
    assert path.name.endswith("-session-eu-morning.json")
    data = json.loads(path.read_text())
    assert data["schema"] == "t2.run-report/1"
    assert data["status"] == "ok"
    assert data["exit_code"] == 0
    assert data["result"] == {"trades": 2}
    assert data["argv"] == ["-v"]
    assert data["duration_seconds"] >= 0
    assert set(data) == {
        "schema", "droid", "version", "command", "argv", "started_at", "finished_at",
        "duration_seconds", "dry_run", "status", "exit_code", "summary", "result", "errors",
    }


@pytest.mark.parametrize(
    ("code", "status"),
    [(c.Exit.FAILED, "failed"), (c.Exit.CONFIG, "config_error"), (c.Exit.SKIPPED, "skipped")],
)
def test_statuses(code: c.Exit, status: str) -> None:
    report = c.RunReport(droid="t2-x", version="1", command="run")
    report.finish(code, "why", errors=["boom"])
    data = report.to_dict()
    assert (data["status"], data["exit_code"], data["errors"]) == (status, int(code), ["boom"])


def test_explicit_report_path(tmp_path: Path) -> None:
    target = tmp_path / "custom" / "r.json"
    report = c.RunReport(droid="t2-x", version="1", command="run")
    report.finish(c.Exit.OK, "")
    assert report.write(target) == target
    assert json.loads(target.read_text())["droid"] == "t2-x"
    assert [p.name for p in target.parent.iterdir()] == ["r.json"]


def test_unfinished_report_counts_as_failed() -> None:
    assert c.RunReport(droid="t2-x", version="1", command="run").to_dict()["status"] == "failed"


def test_run_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    proc, _, release = _held_by_other_process(tmp_path, "t2-x")
    try:
        with pytest.raises(c.LockBusy), c.run_lock("t2-x"):
            pass
        with c.run_lock("t2-y"):  # other droids are unaffected
            pass
    finally:
        release.set()  # type: ignore[attr-defined]
        proc.join(10)  # type: ignore[attr-defined]
    with c.run_lock("t2-x"):  # free again once the holder exits
        pass


def test_t212_lock_waits_then_gives_up(tmp_path: Path) -> None:
    proc, _, release = _held_by_other_process(tmp_path, "t212")
    try:
        start = time.monotonic()
        with pytest.raises(c.LockBusy), c.t212_lock(timeout=0.3):
            pass
        assert time.monotonic() - start >= 0.3
    finally:
        release.set()  # type: ignore[attr-defined]
        proc.join(10)  # type: ignore[attr-defined]
    with c.t212_lock(timeout=0):
        pass


def test_lock_busy_is_a_skip() -> None:
    assert issubclass(c.LockBusy, c.Skipped)


def test_lock_dir_defaults_without_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T2_LOCK_DIR")
    assert c.lock_dir().is_dir()


@pytest.mark.parametrize(("raw", "expected"), [(None, 600.0), ("5", 5.0), ("x", 600.0)])
def test_t212_lock_timeout(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
) -> None:
    if raw is None:
        monkeypatch.delenv("T2_T212_LOCK_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("T2_T212_LOCK_TIMEOUT", raw)
    assert c.t212_lock_timeout() == expected
