"""Trusted-main ancestry policy tests (PRD1C1 correction)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.git_checks import (  # noqa: E402
    GitCheckError,
    assert_revision_contained_in_trusted_main,
)
from fetchnow_release.revision import RevisionError, validate_full_sha  # noqa: E402

from git_graph_fixture import build_trusted_main_graph  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )


def test_expected_equals_origin_main_passes(tmp_path: Path) -> None:
    g = build_trusted_main_graph(tmp_path)
    oid = assert_revision_contained_in_trusted_main(g.repo, g.main_tip)
    assert oid == g.main_tip


def test_older_main_commit_passes(tmp_path: Path) -> None:
    g = build_trusted_main_graph(tmp_path)
    oid = assert_revision_contained_in_trusted_main(g.repo, g.main_old)
    assert oid == g.main_old


def test_feature_descendant_not_in_main_fails(tmp_path: Path) -> None:
    """Regression: unmerged descendants must be rejected (old OR-clause accepted them)."""
    g = build_trusted_main_graph(tmp_path)
    # Prove the reversed (wrong) direction would succeed — documents the bug class.
    wrong = _git(
        g.repo, "merge-base", "--is-ancestor", "refs/remotes/origin/main", g.feature_tip
    )
    assert wrong.returncode == 0, (
        "fixture must make feature a descendant of origin/main"
    )
    correct = _git(
        g.repo, "merge-base", "--is-ancestor", g.feature_tip, "refs/remotes/origin/main"
    )
    assert correct.returncode != 0, (
        "fixture feature must NOT be contained in origin/main"
    )

    with pytest.raises(GitCheckError, match="not contained"):
        assert_revision_contained_in_trusted_main(g.repo, g.feature_tip)


def test_unrelated_commit_fails(tmp_path: Path) -> None:
    g = build_trusted_main_graph(tmp_path)
    with pytest.raises(GitCheckError, match="not contained"):
        assert_revision_contained_in_trusted_main(g.repo, g.unrelated)


def test_unknown_revision_fails(tmp_path: Path) -> None:
    g = build_trusted_main_graph(tmp_path)
    missing = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    with pytest.raises(GitCheckError, match="not a known Git object|not resolvable"):
        assert_revision_contained_in_trusted_main(g.repo, missing)


def test_missing_origin_main_fails(tmp_path: Path) -> None:
    g = build_trusted_main_graph(tmp_path)
    _git(g.repo, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(GitCheckError, match="missing|not resolvable|does not fetch"):
        assert_revision_contained_in_trusted_main(g.repo, g.main_tip)


def test_trusted_ref_non_commit_fails(tmp_path: Path) -> None:
    g = build_trusted_main_graph(tmp_path)
    blob_path = g.repo / "blob.bin"
    blob_path.write_bytes(b"not-a-commit")
    blob_oid = _git(g.repo, "hash-object", "-w", str(blob_path)).stdout.strip()
    assert _git(g.repo, "cat-file", "-t", blob_oid).stdout.strip() == "blob"
    _git(g.repo, "update-ref", "refs/remotes/origin/main", blob_oid)
    with pytest.raises(GitCheckError, match="not a commit"):
        assert_revision_contained_in_trusted_main(g.repo, g.main_tip)


def test_abbreviated_sha_fails_before_git(tmp_path: Path) -> None:
    g = build_trusted_main_graph(tmp_path)
    short = g.main_tip[:12]
    with pytest.raises(RevisionError):
        validate_full_sha(short)
    with pytest.raises(GitCheckError):
        assert_revision_contained_in_trusted_main(g.repo, short)


def test_uppercase_sha_fails(tmp_path: Path) -> None:
    g = build_trusted_main_graph(tmp_path)
    with pytest.raises(RevisionError):
        validate_full_sha(g.main_tip.upper())
    with pytest.raises(GitCheckError):
        assert_revision_contained_in_trusted_main(g.repo, g.main_tip.upper())


def test_reversed_ancestry_regression_fails(tmp_path: Path) -> None:
    """Policy must use is-ancestor expected trusted — never the reverse alone."""
    g = build_trusted_main_graph(tmp_path)
    # Feature tip is a descendant: reverse check passes, correct check fails.
    with pytest.raises(GitCheckError, match="not contained"):
        assert_revision_contained_in_trusted_main(g.repo, g.feature_tip)


def test_annotated_tag_object_rejected(tmp_path: Path) -> None:
    g = build_trusted_main_graph(tmp_path)
    _git(g.repo, "checkout", "main")
    tag = _git(
        g.repo,
        "tag",
        "-a",
        "v-test",
        "-m",
        "annotated",
        g.main_tip,
    )
    assert tag.returncode == 0
    tag_oid = _git(g.repo, "rev-parse", "v-test").stdout.strip()
    assert _git(g.repo, "cat-file", "-t", tag_oid).stdout.strip() == "tag"
    # Full tag object SHA is hex but not a deployable commit identity.
    with pytest.raises(GitCheckError, match="not a commit"):
        assert_revision_contained_in_trusted_main(g.repo, tag_oid)
