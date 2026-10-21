from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/generate_changelog.py"


def git(repo: Path, *args: str, date: str = "2026-01-01T12:00:00+00:00") -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        timeout=5,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "History Author",
            "GIT_AUTHOR_EMAIL": "history@example.com",
            "GIT_COMMITTER_NAME": "History Author",
            "GIT_COMMITTER_EMAIL": "history@example.com",
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        },
    ).strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    git(tmp_path, "init", "--initial-branch=main")
    return tmp_path


def commit(repo: Path, message: str, date: str = "2026-01-01T12:00:00+00:00") -> str:
    git(repo, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", message, date=date)
    return git(repo, "rev-parse", "HEAD")


def generate(repo: Path, *args: str) -> list[dict]:
    output = repo / "exported/CHANGELOG.json"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--output", str(output), *args], check=True, timeout=10
    )
    return json.loads(output.read_text())


def test_untagged_root(repo: Path) -> None:
    root = commit(repo, "Initial media support\n\n- Keep this list\n- Keep indentation\n\n```\n  example\n```")
    head = commit(
        repo,
        "Add libraries\n\nCo-authored-by: Another Author <author@example.test>\nCO-AUTHORED-BY: Other <other@example.test>",
        "2026-01-02T12:00:00+00:00",
    )

    groups = generate(repo)

    assert [group["tag"] for group in groups] == ["Unreleased"]
    assert groups[0]["full_sha"] == head
    assert datetime.fromisoformat(groups[0]["date"]) == datetime(2026, 1, 2, 12, tzinfo=UTC)
    assert [item["full_sha"] for item in groups[0]["commits"]] == [head, root]
    assert groups[0]["commits"][0]["message"] == "Add libraries"
    initial = groups[0]["commits"][-1]
    assert initial["message"] == git(repo, "show", "-s", "--format=%B", root)
    assert initial["author"] == "History Author"
    assert datetime.fromisoformat(initial["date"]) == datetime(2026, 1, 1, 12, tzinfo=UTC)


def test_tagged_ranges(repo: Path) -> None:
    root = commit(repo, "Start conversion", "2026-01-03T12:00:00+00:00")
    git(repo, "-c", "tag.gpgsign=false", "tag", "-a", "v0.1.0-alpha.1", "-m", "Alpha")
    second = commit(repo, "Add nodes", "2026-01-02T12:00:00+00:00")
    git(repo, "tag", "v0.1.0-alpha.2")

    groups = generate(repo)
    assert [group["tag"] for group in groups] == ["v0.1.0-alpha.2", "v0.1.0-alpha.1"]
    assert [[item["full_sha"] for item in group["commits"]] for group in groups] == [[second], [root]]
    git(repo, "branch", "dev")
    assert generate(repo, "--include-ref", "dev") == groups

    head = commit(repo, "Add playback")
    groups = generate(repo)
    assert [group["tag"] for group in groups] == ["Unreleased", "v0.1.0-alpha.2", "v0.1.0-alpha.1"]
    assert [[item["full_sha"] for item in group["commits"]] for group in groups] == [[head], [second], [root]]


def test_reachable_versions(repo: Path) -> None:
    root = commit(repo, "Start conversion")
    git(repo, "tag", "v0.1.0")
    git(repo, "checkout", "-b", "other")
    commit(repo, "Unmerged work")
    git(repo, "tag", "v9.0.0")
    git(repo, "checkout", "main")
    head = commit(repo, "Add nodes")
    git(repo, "tag", "checkpoint")

    groups = generate(repo)

    assert [group["tag"] for group in groups] == ["Unreleased", "v0.1.0"]
    assert [[item["full_sha"] for item in group["commits"]] for group in groups] == [[head], [root]]


def test_development_after_release(repo: Path) -> None:
    root = commit(repo, "Start conversion")
    git(repo, "tag", "v0.1.0")
    git(repo, "checkout", "-b", "dev")
    development = commit(repo, "Development branch change")

    groups = generate(repo, "--ref", "main", "--include-ref", "dev")

    assert [group["tag"] for group in groups] == ["Unreleased", "v0.1.0"]
    assert [[item["full_sha"] for item in group["commits"]] for group in groups] == [[development], [root]]


@pytest.mark.parametrize("tagged", [False, True])
def test_combined_branches(repo: Path, tagged: bool) -> None:
    root = commit(repo, "Start conversion")
    if tagged:
        git(repo, "tag", "v0.1.0")
    shared = commit(repo, "Shared development")
    git(repo, "branch", "dev")
    primary = commit(repo, "Primary branch change")
    git(repo, "update-ref", "refs/remotes/origin/master", primary)
    git(repo, "checkout", "dev")
    development = commit(repo, "Development branch change")
    git(repo, "tag", "v9.0.0")
    git(repo, "update-ref", "refs/remotes/origin/dev", development)

    groups = generate(repo, "--ref", "origin/master", "--include-ref", "origin/dev")

    assert [group["tag"] for group in groups] == (["Unreleased", "v0.1.0"] if tagged else ["Unreleased"])
    assert groups[0]["full_sha"] == development
    commits = [item["full_sha"] for item in groups[0]["commits"]]
    expected = {development, primary, shared} if tagged else {development, primary, shared, root}
    assert set(commits) == expected
    assert len(commits) == len(expected)
    if tagged:
        assert [item["full_sha"] for item in groups[1]["commits"]] == [root]
