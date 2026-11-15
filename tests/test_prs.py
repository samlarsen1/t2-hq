"""Opening a pull request for a suggestion, against a local bare repo standing in for GitHub."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import ADVICE, write_exec
from t2hq.droids import Droids
from t2hq.prs import PullRequestError, PullRequests, repo_slug
from test_configedit import STOCKS, STRATEGY

# Records its arguments; "pr create" prints a URL and remembers the branch, "pr list" finds it.
FAKE_GH = """#!/usr/bin/env bash
dir="$(dirname "$0")"
printf '%s\\n' "$*" >> "$dir/gh.log"
case "$1 $2" in
  "auth git-credential") exit 0 ;;
  "pr list") head="$(printf '%s\\n' "$@" | grep -A1 -- --head | tail -1)"
             [[ -f "$dir/pr-$(basename "$head")" ]] && cat "$dir/pr-$(basename "$head")"; exit 0 ;;
  "pr create") head="$(printf '%s\\n' "$@" | grep -A1 -- --head | tail -1)"
               echo "https://github.com/owner/t2-d2/pull/31" | tee "$dir/pr-$(basename "$head")" ;;
esac
"""


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.com")


@pytest.fixture
def github(tmp_path: Path, apps: Path) -> Path:
    """owner/t2-d2 and owner/t2-k2 as bare repos, and deployed apps pointing at them."""
    hub = tmp_path / "github/owner"
    for droid, files in {
        "t2-d2": {"config/stocks.yaml": STOCKS, "config/strategy.yaml": STRATEGY},
        "t2-k2": {"config/strategy.yaml": STRATEGY},
    }.items():
        work = tmp_path / "seed" / droid
        (work / "config").mkdir(parents=True)
        for name, text in files.items():
            (work / name).write_text(text)
        git("init", "-q", "-b", "main", cwd=work)
        git("add", ".", cwd=work)
        git("commit", "-q", "-m", "seed", cwd=work)
        git("clone", "-q", "--bare", str(work), str(hub / f"{droid}.git"), cwd=tmp_path)
        app = apps / droid
        git("init", "-q", cwd=app)
        git("remote", "add", "origin", f"git@github.com:owner/{droid}.git", cwd=app)
    advice = dict(ADVICE)
    k2 = {
        **ADVICE["suggestions"][0],
        "id": "0badc0de",
        "kind": "change_threshold",
        "droid": "t2-k2",
        "file": "config/strategy.yaml",
        "target": "overbought.rsi_min",
        "current": "70",
        "proposed": "75",
        "details": {},
    }
    advice["suggestions"] = [ADVICE["suggestions"][0], k2]
    advice["suggestions"][0] = {
        **advice["suggestions"][0],
        "current": "moderate",
        "proposed": "mild",
    }
    (apps / "t2-3po/var/suggestions/2026-11-22_advice.json").write_text(json.dumps(advice))
    write_exec(tmp_path / "bin/gh", FAKE_GH)
    return tmp_path / "github"


@pytest.fixture
def prs(droids: Droids, github: Path, tmp_path: Path) -> PullRequests:
    return PullRequests(
        droids,
        tmp_path / "state/repos",
        gh=str(tmp_path / "bin/gh"),
        git_url=f"file://{github}/{{slug}}.git",
    )


def test_repo_slug() -> None:
    assert repo_slug("git@github.com:owner/t2-d2.git") == "owner/t2-d2"
    assert repo_slug("https://github.com/owner/t2-k2") == "owner/t2-k2"
    with pytest.raises(PullRequestError):
        repo_slug("https://gitlab.com/owner/x.git")


async def test_opens_a_pull_request_with_exactly_the_change(
    prs: PullRequests, github: Path, tmp_path: Path
) -> None:
    pr = await prs.open("e3a79f4f")
    assert (pr.url, pr.created) == ("https://github.com/owner/t2-d2/pull/31", True)

    bare = github / "owner/t2-d2.git"
    branch = "task/3po-e3a79f4f"
    diff = git("diff", "main", branch, cwd=bare)
    assert "-        strategy: moderate\n+        strategy: mild" in diff
    body = [
        line for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    assert body == ["-        strategy: moderate", "+        strategy: mild"]
    message = git("log", "-1", "--format=%B", branch, cwd=bare)
    assert message.startswith("Move TSLA from moderate to mild")
    assert "t2-3po (e3a79f4f)" in message
    assert git("rev-parse", "main", cwd=bare) == git(
        "rev-parse", "main~0", cwd=bare
    )  # main untouched

    log = (tmp_path / "bin/gh.log").read_text()
    create = next(line for line in log.splitlines() if line.startswith("pr create"))
    assert "-R owner/t2-d2 --base main --head task/3po-e3a79f4f" in create
    assert "--title Move TSLA from moderate to mild" in create

    again = await prs.open("e3a79f4f")
    assert again.created is False and again.url == pr.url


async def test_threshold_pull_request_on_t2_k2(prs: PullRequests, github: Path) -> None:
    await prs.open("0badc0de")
    diff = git("diff", "main", "task/3po-0badc0de", cwd=github / "owner/t2-k2.git")
    assert "-  rsi_min: 70.0   # inclusive\n+  rsi_min: 75.0   # inclusive" in diff


async def test_refuses_a_stale_suggestion(prs: PullRequests, github: Path, tmp_path: Path) -> None:
    work = tmp_path / "seed/t2-d2"
    stocks = work / "config/stocks.yaml"
    stocks.write_text(stocks.read_text().replace("strategy: moderate", "strategy: aggressive"))
    git("commit", "-q", "-am", "manual change", cwd=work)
    git("push", "-q", str(github / "owner/t2-d2.git"), "main", cwd=work)

    with pytest.raises(PullRequestError, match="config has changed"):
        await prs.open("e3a79f4f")
    branches = git("branch", "--list", "task/*", cwd=github / "owner/t2-d2.git")
    assert branches == ""


async def test_unknown_suggestion(prs: PullRequests) -> None:
    with pytest.raises(PullRequestError, match="no suggestion deadbeef"):
        await prs.open("deadbeef")
