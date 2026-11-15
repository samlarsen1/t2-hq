"""Turn an accepted t2-3po suggestion into a pull request on the droid's repository.

Code, not the model, writes the change (``configedit``). The work happens in this app's own
clones under ``var/repos``, never in a development checkout. Only the operator starts it, by
pressing a button; the agent has no tool for it.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .configedit import Change, EditError, apply_change
from .droids import DroidError, Droids, Runner, run_process

_SLUG = re.compile(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$")
_DROIDS_WITH_CONFIG = frozenset({"t2-d2", "t2-k2"})


class PullRequestError(Exception):
    """The pull request couldn't be opened. The message is safe to show the operator."""


@dataclass(frozen=True)
class PullRequest:
    url: str
    created: bool


def repo_slug(remote_url: str) -> str:
    """``owner/repo`` from a GitHub remote URL (SSH or HTTPS)."""
    match = _SLUG.search(remote_url.strip())
    if match is None:
        raise PullRequestError(f"not a GitHub remote: {remote_url}")
    return match.group(1)


def _title(change: Change) -> str:
    d = change.details
    if change.kind == "change_mode":
        return f"Move {change.target} from {change.current} to {change.proposed}"
    if change.kind == "add_holding":
        return f"Add {d.get('symbol', change.target)} as {change.proposed}"
    if change.kind == "remove_holding":
        return f"Move {change.target} to the watchlist"
    return f"Change {change.target} from {change.current} to {change.proposed}"


def _body(suggestion: dict[str, Any], change: Change) -> str:
    evidence = "\n".join(f"- {item}" for item in suggestion.get("evidence") or [])
    return (
        f"Suggested by t2-3po (`{suggestion.get('id')}`, confidence "
        f"{suggestion.get('confidence')}) and accepted by the operator in Telegram. "
        "t2-hq applied the change to the file; the model wrote none of it.\n\n"
        f"**Change:** `{change.file}`: `{change.target}` "
        f"{change.current or '(new)'} → {change.proposed}\n\n"
        f"**Why:** {suggestion.get('rationale')}\n\n"
        + (f"**Evidence:**\n{evidence}\n\n" if evidence else "")
        + f"**Expected effect:** {suggestion.get('expected_effect')}\n\n"
        f"**Risk:** {suggestion.get('risk')}\n\n"
        f"**Reviewer:** {suggestion.get('reviewer_note')}\n\n"
        "The change takes effect after merging and redeploying: "
        "`bin/t2-deploy release` and `bin/t2-deploy deploy` in t2-hq.\n"
    )


class PullRequests:
    def __init__(
        self,
        droids: Droids,
        repos_dir: Path,
        github_token: str | None = None,
        gh: str = "gh",
        git_url: str = "https://github.com/{slug}.git",
        runner: Runner = run_process,
    ) -> None:
        self.droids = droids
        self.repos_dir = repos_dir
        self.github_token = github_token
        self.gh = gh
        self.git_url = git_url
        self._run = runner
        self._lock = asyncio.Lock()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"})
        if self.github_token:
            env["GH_TOKEN"] = self.github_token
        return env

    async def _cmd(self, argv: list[str], cwd: Path, timeout: float = 120) -> str:
        result = await self._run(argv, cwd, self._env(), timeout)
        if result.returncode != 0:
            detail = (result.stderr.strip() or result.stdout.strip()).splitlines()[-1:]
            raise PullRequestError(f"{argv[0]} {argv[1]} failed: {' '.join(detail)[:300]}")
        return result.stdout.strip()

    async def _git(self, repo: Path, *args: str, timeout: float = 120) -> str:
        return await self._cmd(["git", "-C", str(repo), *args], repo.parent, timeout)

    def find_suggestion(self, suggestion_id: str) -> dict[str, Any]:
        """The newest version of a suggestion, from t2-3po's advice runs."""
        for path in reversed(self.droids.advice_files()):
            run = self.droids.read_advice(path) or {}
            for suggestion in run.get("suggestions") or []:
                if suggestion.get("id") == suggestion_id:
                    return dict(suggestion)
        raise PullRequestError(f"no suggestion {suggestion_id} in t2-3po's advice")

    async def _prepare(self, droid: str, slug: str) -> Path:
        """This app's clone of the droid's repository, reset to the latest main."""
        repo = self.repos_dir / droid
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        url = self.git_url.format(slug=slug)
        if not (repo / ".git").is_dir():
            await self._cmd(["git", "clone", "--quiet", url, str(repo)], self.repos_dir, 300)
        await self._git(repo, "remote", "set-url", "origin", url)
        await self._git(repo, "fetch", "--quiet", "--prune", "origin")
        await self._git(repo, "checkout", "--quiet", "-B", "main", "origin/main")
        await self._git(repo, "reset", "--quiet", "--hard", "origin/main")
        await self._git(repo, "clean", "-fdq")
        return repo

    async def _existing(self, slug: str, branch: str) -> str | None:
        out = await self._cmd(
            [
                self.gh,
                "pr",
                "list",
                "-R",
                slug,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "url",
                "--jq",
                ".[0].url // empty",
            ],
            self.repos_dir,
        )
        return out or None

    async def open(self, suggestion_id: str) -> PullRequest:
        """Apply the suggestion on a ``task/3po-<id>`` branch and open a pull request."""
        async with self._lock:
            suggestion = self.find_suggestion(suggestion_id)
            droid = str(suggestion.get("droid"))
            if droid not in _DROIDS_WITH_CONFIG:
                raise PullRequestError(f"{droid} has no configuration to change")
            try:
                change = Change.from_suggestion(suggestion)
                remote = await self._git(self.droids.app(droid), "remote", "get-url", "origin")
            except (EditError, DroidError) as exc:
                raise PullRequestError(str(exc)) from None
            slug = repo_slug(remote)
            branch = f"task/3po-{suggestion_id}"
            self.repos_dir.mkdir(parents=True, exist_ok=True)
            existing = await self._existing(slug, branch)
            if existing:
                return PullRequest(existing, created=False)

            repo = await self._prepare(droid, slug)
            path = repo / change.file
            try:
                edited = apply_change(change, path.read_text(encoding="utf-8"))
            except EditError as exc:
                raise PullRequestError(str(exc)) from None
            except OSError as exc:
                raise PullRequestError(f"can't read {change.file} ({type(exc).__name__})") from None

            title = _title(change)
            await self._git(repo, "checkout", "--quiet", "-B", branch)
            path.write_text(edited, encoding="utf-8")
            await self._git(repo, "add", change.file)
            message = f"{title}\n\nSuggested by t2-3po ({suggestion_id}); accepted via t2-hq."
            await self._git(repo, "commit", "--quiet", "-m", message)
            await self._git(
                repo,
                "-c",
                "credential.helper=",
                "-c",
                f"credential.helper=!{self.gh} auth git-credential",
                "push",
                "--quiet",
                "--force-with-lease",
                "origin",
                f"{branch}:{branch}",
                timeout=180,
            )
            url = await self._cmd(
                [
                    self.gh,
                    "pr",
                    "create",
                    "-R",
                    slug,
                    "--base",
                    "main",
                    "--head",
                    branch,
                    "--title",
                    title,
                    "--body",
                    _body(suggestion, change),
                ],
                repo,
            )
            await self._git(repo, "checkout", "--quiet", "main")
            return PullRequest(url.splitlines()[-1].strip(), created=True)
