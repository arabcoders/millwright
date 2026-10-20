#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import TypedDict


class Commit(TypedDict):
    sha: str
    full_sha: str
    message: str
    author: str
    date: str


class ChangeSet(TypedDict):
    tag: str
    full_sha: str
    date: str
    commits: list[Commit]


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, encoding="utf-8")  # noqa: S607


def commits_between(repo: Path, end: str, excluded: list[str]) -> list[Commit]:
    output = git(
        repo, "log", "--topo-order", "--no-merges", "-z", "--format=%H%x00%an%x00%cI%x00%B", end, "--not", *excluded
    )
    fields = output.split("\0")
    return [
        Commit(sha=sha[:8], full_sha=sha, author=author, date=date, message=message.strip())
        for sha, author, date, message in zip(fields[0::4], fields[1::4], fields[2::4], fields[3::4], strict=False)
    ]


def generate_changelog(repo: Path) -> list[ChangeSet]:
    if git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        msg = "Cannot generate complete commit history from a shallow checkout. Fetch full history and tags first."
        raise RuntimeError(msg)

    history = git(repo, "rev-list", "--topo-order", "HEAD").splitlines()
    positions = {sha: index for index, sha in enumerate(history)}
    tags = [
        (name, git(repo, "rev-parse", f"refs/tags/{name}^{{commit}}").strip())
        for name in git(repo, "tag", "--merged", "HEAD").splitlines()
        if re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", name)
    ]
    # Commit timestamps can move backwards. Release ranges follow ancestry instead.
    tags.sort(key=lambda tag: (positions[tag[1]], tag[0]))
    refs = tags.copy()
    if not tags or history[0] != tags[0][1]:
        refs.insert(0, ("Unreleased", history[0]))

    return [
        ChangeSet(
            tag=name,
            full_sha=sha,
            date=git(repo, "show", "-s", "--format=%cI", sha).strip(),
            # The oldest range has no lower bound, so it includes the root commit.
            commits=commits_between(repo, sha, [older_sha for _, older_sha in refs[index + 1 :]]),
        )
        for index, (name, sha) in enumerate(refs)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate commit history from reachable Git version tags.")
    parser.add_argument("--repo", type=Path, default=Path())
    parser.add_argument("--output", type=Path, default=Path("ui/exported/CHANGELOG.json"))
    args = parser.parse_args()
    changes = generate_changelog(args.repo)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(changes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
