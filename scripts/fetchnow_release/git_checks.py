"""Git worktree / trusted-main ancestry checks (read-only)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import TRUSTED_MAIN_REF
from .revision import RevisionError, validate_full_sha

# Read-only Git verbs only — never fetch/checkout/reset/clean/update-ref.
_ALLOWED_GIT_VERBS = frozenset({"status", "cat-file", "rev-parse", "merge-base"})


class GitCheckError(ValueError):
    """Git preflight failure."""


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    if not args or args[0] not in _ALLOWED_GIT_VERBS:
        raise GitCheckError(
            f"refusing non-read-only git invocation: {' '.join(args)!r}"
        )
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )


def assert_clean_worktree(repo: Path) -> None:
    proc = _run_git(repo, "status", "--porcelain")
    if proc.returncode != 0:
        raise GitCheckError(f"git status failed: {proc.stderr.strip()}")
    if proc.stdout.strip():
        raise GitCheckError(
            "worktree is dirty; commit or stash before staging preflight"
        )


def _object_type(repo: Path, oid: str) -> str:
    proc = _run_git(repo, "cat-file", "-t", oid)
    if proc.returncode != 0:
        raise GitCheckError(f"revision {oid} is not a known Git object")
    return proc.stdout.strip()


def resolve_commit_oid(repo: Path, rev: str, *, label: str) -> str:
    """Resolve *rev* to a canonical commit object ID (read-only)."""
    # Prefer exact object identity first for full SHAs / refs.
    exists = _run_git(repo, "cat-file", "-e", rev)
    if exists.returncode != 0:
        # Fall back to rev-parse for refs like refs/remotes/origin/main.
        parsed = _run_git(repo, "rev-parse", "--verify", rev)
        if parsed.returncode != 0:
            raise GitCheckError(
                f"{label} {rev!r} is missing or not resolvable locally "
                f"(preflight does not fetch; synchronize {TRUSTED_MAIN_REF} first)"
            )
        rev = parsed.stdout.strip()

    otype = _object_type(repo, rev)
    if otype != "commit":
        raise GitCheckError(
            f"{label} resolves to a {otype} object, not a commit "
            f"(annotated tags and non-commit objects are rejected)"
        )

    canonical = _run_git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")
    if canonical.returncode != 0:
        raise GitCheckError(f"{label} could not be resolved to a commit object")
    oid = canonical.stdout.strip().lower()
    if len(oid) != 40:
        raise GitCheckError(f"{label} did not resolve to a full commit object ID")
    return oid


def assert_revision_contained_in_trusted_main(repo: Path, revision: str) -> str:
    """Require *revision* to already be contained in refs/remotes/origin/main.

    Equivalent to:
      git merge-base --is-ancestor "$EXPECTED_REVISION" refs/remotes/origin/main

    Feature-branch descendants of main are rejected. Does not fetch or mutate refs.
    Trusted ref is fixed — no override/bypass. Returns the canonical expected
    commit OID on success.
    """
    try:
        expected = validate_full_sha(revision)
    except RevisionError as exc:
        raise GitCheckError(str(exc)) from exc

    expected_oid = resolve_commit_oid(repo, expected, label="expected revision")
    if expected_oid != expected:
        raise GitCheckError(
            f"expected revision {expected} does not match canonical commit "
            f"object ID {expected_oid}"
        )

    trusted_oid = resolve_commit_oid(
        repo, TRUSTED_MAIN_REF, label=f"trusted ref {TRUSTED_MAIN_REF}"
    )

    # Direction is intentional and must not be reversed:
    # expected must be an ancestor of (or equal to) trusted main.
    check = _run_git(repo, "merge-base", "--is-ancestor", expected_oid, trusted_oid)
    if check.returncode != 0:
        raise GitCheckError(
            f"revision {expected_oid} is not contained in {TRUSTED_MAIN_REF} "
            f"(trusted tip {trusted_oid}); feature-branch descendants and "
            f"unmerged commits are rejected. Fetch/synchronize separately, "
            f"then re-run preflight — preflight does not fetch."
        )
    return expected_oid


# Back-compat alias used by preflight.
def assert_revision_in_origin_main(repo: Path, revision: str) -> str:
    return assert_revision_contained_in_trusted_main(repo, revision)
