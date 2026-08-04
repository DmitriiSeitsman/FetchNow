"""Helpers to build isolated temporary Git commit graphs for ancestry tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TempGitGraph:
    repo: Path
    main_old: str
    main_tip: str
    feature_tip: str
    unrelated: str


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def build_trusted_main_graph(root: Path) -> TempGitGraph:
    """Create a disposable repo with main history + unmerged feature descendant.

    Layout::

        unrelated (orphan)
        main_old -> main_tip   <= refs/remotes/origin/main, refs/heads/main
                       \\
                        feature_tip  (NOT in origin/main)

    Never touches the caller's real repository refs.
    """
    repo = root / "repo"
    repo.mkdir(parents=True)
    # Empty template avoids writing sample hooks (sandbox/CI friendly).
    _git(repo, "-c", "init.templateDir=", "init")
    _git(repo, "config", "user.email", "test@fetchnow.invalid")
    _git(repo, "config", "user.name", "FetchNow Test")
    # Avoid depending on default branch name.
    _git(repo, "checkout", "-b", "main")

    (repo / "a.txt").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "main-old")
    main_old = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.txt").write_text("tip\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "main-tip")
    main_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Simulate remote-tracking main without fetching.
    _git(repo, "update-ref", "refs/remotes/origin/main", main_tip)

    _git(repo, "checkout", "-b", "feature")
    (repo / "feat.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feat.txt")
    _git(repo, "commit", "-m", "feature-descendant")
    feature_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Orphan unrelated history.
    _git(repo, "checkout", "--orphan", "unrelated")
    _git(repo, "rm", "-rf", ".", check=False)
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "unrelated")
    unrelated = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Leave worktree on main, clean, trusted tip current.
    _git(repo, "checkout", "main")
    return TempGitGraph(
        repo=repo,
        main_old=main_old,
        main_tip=main_tip,
        feature_tip=feature_tip,
        unrelated=unrelated,
    )
