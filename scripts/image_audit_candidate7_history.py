#!/usr/bin/env python3
"""Baseline history / runtime-input proof for candidate-7 image audit.

Offline-safe: uses only local git objects. Does not fetch remotes or call
advisory services. Fail-closed when baseline is missing, not an ancestor, or
runtime/build inputs changed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

# Paths that may change on an audit-harness commit without invalidating the
# native-acceptance runtime baseline.
AUDIT_ONLY_PATH_PREFIXES: tuple[str, ...] = (
    ".github/workflows/sec03c-candidate7-image-audit.yml",
    "scripts/image_audit_candidate7.py",
    "scripts/image_audit_candidate7_history.py",
    "tests/image_audit/",
    "docs/operations/sec-03c-candidate7-image-audit.md",
)

# Build/runtime inputs that must be byte-identical to the native PASS tip.
RUNTIME_BUILD_INPUT_PATHS: tuple[str, ...] = (
    "backend/Dockerfile",
    "backend/Dockerfile.media-executor",
    "backend/Dockerfile.media-net-executor",
    "backend/Dockerfile.media-egress-proxy",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/alembic.ini",
    "backend/src",
    "backend/migrations",
    "web/Dockerfile",
    "web/package.json",
    "web/package-lock.json",
    "web/src",
    "web/docker",
    "deploy/nginx",
    "compose.yaml",
)


def _run_git(
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
            f"git {' '.join(args)} failed: "
            f"{(proc.stderr or proc.stdout).strip() or proc.returncode}"
        )
    return proc


def resolve_commit(repo: Path, rev: str) -> str:
    """Return full 40-char lowercase SHA for a commit object, or raise."""
    proc = _run_git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")
    sha = proc.stdout.strip().lower()
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise RuntimeError(f"resolved revision is not a full commit SHA: {sha!r}")
    # Object must exist as a commit.
    _run_git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    return sha


def is_ancestor(repo: Path, *, ancestor: str, tip: str) -> bool:
    proc = _run_git(repo, "merge-base", "--is-ancestor", ancestor, tip, check=False)
    return proc.returncode == 0


def path_is_audit_only(path: str) -> bool:
    text = path.strip().replace("\\", "/")
    if not text:
        return False
    for prefix in AUDIT_ONLY_PATH_PREFIXES:
        if prefix.endswith("/"):
            if text.startswith(prefix) or text == prefix[:-1]:
                return True
        elif text == prefix:
            return True
    return False


def changed_paths(repo: Path, *, baseline: str, head: str) -> list[str]:
    proc = _run_git(repo, "diff", "--name-only", f"{baseline}..{head}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def runtime_input_diff_names(repo: Path, *, baseline: str, head: str) -> list[str]:
    proc = _run_git(
        repo,
        "diff",
        "--name-only",
        f"{baseline}..{head}",
        "--",
        *RUNTIME_BUILD_INPUT_PATHS,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def prove_baseline_history(
    repo: Path,
    *,
    baseline_sha: str,
    expected_head_sha: str,
) -> dict[str, Any]:
    """Fail-closed proof used by the candidate-7 audit workflow."""
    evidence: dict[str, Any] = {
        "ok": False,
        "repo": str(repo),
        "requested_baseline": baseline_sha,
        "requested_head": expected_head_sha,
    }
    try:
        head = resolve_commit(repo, "HEAD")
        evidence["resolved_head"] = head
    except RuntimeError as exc:
        evidence["error_code"] = "head_unresolvable"
        evidence["message"] = str(exc)
        return evidence

    if head != expected_head_sha.lower():
        evidence["error_code"] = "head_sha_mismatch"
        evidence["message"] = (
            f"HEAD {head} != expected GITHUB_SHA {expected_head_sha.lower()}"
        )
        return evidence

    try:
        baseline = resolve_commit(repo, baseline_sha)
        evidence["resolved_baseline"] = baseline
    except RuntimeError as exc:
        evidence["error_code"] = "baseline_missing"
        evidence["message"] = str(exc)
        return evidence

    if not is_ancestor(repo, ancestor=baseline, tip=head):
        evidence["error_code"] = "baseline_not_ancestor"
        evidence["message"] = f"{baseline} is not an ancestor of {head}"
        return evidence
    evidence["baseline_is_ancestor"] = True

    runtime_changed = runtime_input_diff_names(repo, baseline=baseline, head=head)
    evidence["runtime_input_changed"] = runtime_changed
    if runtime_changed:
        evidence["error_code"] = "runtime_inputs_changed"
        evidence["message"] = "runtime/build inputs differ from baseline: " + ", ".join(
            runtime_changed
        )
        return evidence

    all_changed = changed_paths(repo, baseline=baseline, head=head)
    evidence["changed_paths"] = all_changed
    unexpected = [p for p in all_changed if not path_is_audit_only(p)]
    evidence["unexpected_changed_paths"] = unexpected
    if unexpected:
        evidence["error_code"] = "unexpected_paths_changed"
        evidence["message"] = "non-audit paths changed vs baseline: " + ", ".join(
            unexpected
        )
        return evidence

    evidence["ok"] = True
    evidence["error_code"] = None
    evidence["runtime_inputs_unchanged"] = True
    evidence["audit_only_changes_only"] = True
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prove candidate-7 baseline history and runtime inputs"
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--json-out", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    evidence = prove_baseline_history(
        args.repo,
        baseline_sha=args.baseline_sha,
        expected_head_sha=args.expected_head_sha,
    )
    text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if evidence.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
