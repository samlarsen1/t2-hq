"""Read-only access to the deployed droids: versions, timers, run reports, config, dry runs.

Nothing here can place, change or cancel an order. Dry runs are built by code from a fixed
argument list, checked against ``FORBIDDEN_ARGS`` before they start, run with Telegram switched
off for the droid, and report into this app's own directory rather than the droid's history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)

DroidName = Literal["t2-d2", "t2-k2", "t2-bb8", "t2-3po"]
DryRunDroid = Literal["t2-d2", "t2-k2"]

# Arguments that would let a droid trade. No command this module builds may contain them.
FORBIDDEN_ARGS = frozenset({"--execute", "--no-dry-run"})
SUGGESTION_ID = re.compile(r"^[0-9a-f]{8}$")
# Keep tool results small enough for a free-tier model's context.
MAX_CONFIG_CHARS = 12_000


@dataclass(frozen=True)
class DroidSpec:
    name: DroidName
    unit_prefix: str
    configs: tuple[str, ...]


DROIDS: dict[str, DroidSpec] = {
    "t2-d2": DroidSpec(
        "t2-d2", "t2d2", ("config/stocks.yaml", "config/strategy.yaml", "config/schedules.yaml")
    ),
    "t2-k2": DroidSpec("t2-k2", "t2k2", ("config/strategy.yaml",)),
    "t2-bb8": DroidSpec("t2-bb8", "t2bb8", ()),
    "t2-3po": DroidSpec("t2-3po", "t2-3po", ()),
}


class DroidError(Exception):
    """A request about a droid can't be carried out."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


async def run_process(
    argv: Sequence[str], cwd: Path, env: dict[str, str], timeout: float
) -> ProcessResult:
    """Run a command without a shell, with a timeout."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise DroidError(f"{argv[0]} timed out after {timeout:.0f}s") from None
    return ProcessResult(
        process.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


def _usec(value: Any) -> str | None:
    if not isinstance(value, int) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1_000_000, UTC).isoformat(timespec="minutes")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def report_summary(report: dict[str, Any]) -> dict[str, Any]:
    """The run report's header, without its droid-specific detail."""
    keys = ("droid", "version", "command", "finished_at", "dry_run", "status", "summary", "errors")
    return {key: report.get(key) for key in keys}


Runner = Callable[[Sequence[str], Path, dict[str, str], float], Awaitable[ProcessResult]]


class Droids:
    def __init__(
        self,
        apps_dir: Path,
        state_dir: Path,
        dry_run_timeout: float = 900,
        systemctl: str = "systemctl",
        runner: Runner = run_process,
    ) -> None:
        self.apps_dir = apps_dir
        self.state_dir = state_dir
        self.dry_run_timeout = dry_run_timeout
        self.systemctl = systemctl
        self._run = runner

    def app(self, name: str) -> Path:
        if name not in DROIDS:
            raise DroidError(f"unknown droid {name!r}; expected one of {', '.join(DROIDS)}")
        return self.apps_dir / name

    def deployed(self, name: str) -> bool:
        return (self.app(name) / ".venv").is_dir()

    def version(self, name: str) -> str | None:
        """The release tag checked out in the app, or ``None`` if it isn't a git checkout."""
        app = self.app(name)
        if not (app / ".git").exists():
            return None
        try:
            result = subprocess.run(
                ["git", "-C", str(app), "describe", "--tags", "--always"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() or None

    def reports(self, name: str, limit: int = 20) -> list[dict[str, Any]]:
        """The newest run reports, newest first."""
        runs = self.app(name) / "var" / "runs"
        paths = sorted(runs.glob("*.json"), reverse=True)[:limit] if runs.is_dir() else []
        return [report for path in paths if (report := _read_json(path)) is not None]

    def report_paths(self, name: str) -> list[Path]:
        runs = self.app(name) / "var" / "runs"
        return sorted(runs.glob("*.json")) if runs.is_dir() else []

    def latest_report(self, name: str) -> dict[str, Any] | None:
        reports = self.reports(name, limit=1)
        return reports[0] if reports else None

    async def timers(self) -> list[dict[str, Any]]:
        """The droids' systemd user timers, with their last and next runs (UTC)."""
        try:
            result = await self._run(
                [self.systemctl, "--user", "list-timers", "--all", "--output=json", "--no-pager"],
                self.apps_dir,
                dict(os.environ),
                30,
            )
        except OSError as exc:
            raise DroidError(f"can't run systemctl ({type(exc).__name__})") from exc
        if result.returncode != 0:
            raise DroidError(f"systemctl failed: {result.stderr.strip()[:200]}")
        try:
            rows = json.loads(result.stdout or "[]")
        except ValueError as exc:
            raise DroidError("systemctl returned malformed JSON") from exc
        prefixes = tuple(spec.unit_prefix for spec in DROIDS.values()) + ("t2-hq",)
        return [
            {
                "timer": row.get("unit"),
                "runs": row.get("activates"),
                "last": _usec(row.get("last")),
                "next": _usec(row.get("next")),
            }
            for row in rows
            if str(row.get("unit", "")).startswith(prefixes)
        ]

    async def status(self) -> dict[str, Any]:
        droids = {}
        for name in DROIDS:
            latest = self.latest_report(name)
            droids[name] = {
                "deployed": self.deployed(name),
                "version": self.version(name),
                "last_run": None if latest is None else report_summary(latest),
            }
        try:
            timers: list[dict[str, Any]] | str = await self.timers()
        except DroidError as exc:
            timers = f"unavailable: {exc}"
        return {"droids": droids, "timers": timers}

    def config(self, name: str) -> dict[str, str]:
        """The droid's configuration files, as text."""
        app = self.app(name)
        files = {}
        for relative in DROIDS[name].configs:
            path = app / relative
            try:
                files[relative] = path.read_text(encoding="utf-8")[:MAX_CONFIG_CHARS]
            except OSError as exc:
                files[relative] = f"unreadable ({type(exc).__name__})"
        if not files:
            raise DroidError(f"{name} has no configuration files to show")
        return files

    def sessions(self) -> list[str]:
        """t2-d2's session names, from its schedules.yaml."""
        try:
            data = yaml.safe_load((self.app("t2-d2") / "config/schedules.yaml").read_text())
            return [str(name) for name in (data.get("sessions") or {})]
        except (OSError, yaml.YAMLError, AttributeError):
            return []

    def dry_run_command(self, droid: str, session: str | None) -> list[str]:
        """The command for a dry run. Never one that can trade."""
        app = self.app(droid)
        if droid == "t2-d2":
            sessions = self.sessions()
            if session not in sessions:
                raise DroidError(f"t2-d2 needs a session, one of: {', '.join(sessions)}")
            argv = [str(app / ".venv/bin/t2d2"), "--session", session, "--dry-run"]
        elif droid == "t2-k2":
            argv = [str(app / ".venv/bin/t2k2-harvest")]
        else:
            raise DroidError("only t2-d2 and t2-k2 have dry runs")
        if FORBIDDEN_ARGS.intersection(argv):
            raise DroidError("refusing a command that could trade")
        return argv

    async def dry_run(self, droid: str, session: str | None = None) -> dict[str, Any]:
        """Run the droid in dry-run mode and return its run report."""
        argv = self.dry_run_command(droid, session)
        if not Path(argv[0]).exists():
            raise DroidError(f"{droid} isn't deployed in {self.app(droid)}")
        out_dir = self.state_dir / "dry-runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        runs = Path(tempfile.mkdtemp(prefix=f"{droid}-", dir=out_dir))
        env = dict(os.environ)
        env.update(
            {
                # The droid's own .env doesn't override variables that are already set.
                "DRY_RUN": "true",
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_CHAT_ID": "",
                "T2_RUNS_DIR": str(runs.resolve()),
            }
        )
        logger.info("dry_run_start droid=%s session=%s", droid, session)
        try:
            result = await self._run(argv, self.app(droid), env, self.dry_run_timeout)
            reports = sorted(runs.glob("*.json"))
            report = _read_json(reports[-1]) if reports else None
        finally:
            shutil.rmtree(runs, ignore_errors=True)
        if report is None:
            tail = result.stderr.strip().splitlines()[-3:]
            raise DroidError(f"{droid} wrote no run report (exit {result.returncode}): {tail}")
        return report

    async def t2_3po(self, *args: str, timeout: float = 120) -> ProcessResult:
        """Run a t2-3po command in its app directory."""
        app = self.app("t2-3po")
        executable = app / ".venv/bin/t2-3po"
        if not executable.exists():
            raise DroidError("t2-3po isn't deployed")
        return await self._run([str(executable), *args], app, dict(os.environ), timeout)

    async def decide(self, suggestion_id: str, accept: bool, note: str) -> ProcessResult:
        """Record the operator's answer to a t2-3po suggestion."""
        if not SUGGESTION_ID.match(suggestion_id):
            raise DroidError(f"not a suggestion id: {suggestion_id!r}")
        action = "accept" if accept else "reject"
        return await self.t2_3po("suggestions", action, suggestion_id, "--note", note)

    def advice_files(self) -> list[Path]:
        directory = self.app("t2-3po") / "var" / "suggestions"
        return sorted(directory.glob("*_advice*.json")) if directory.is_dir() else []

    @staticmethod
    def read_advice(path: Path) -> dict[str, Any] | None:
        return _read_json(path)
