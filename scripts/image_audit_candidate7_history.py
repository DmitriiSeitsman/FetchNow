#!/usr/bin/env python3
"""Successor history / approved-delta proof for candidate-7 image audit.

Offline-safe: uses only local git objects. Does not fetch remotes or call
advisory services. Fail-closed when baseline/predecessor are missing, not
ancestors, or when changes escape the approved successor contract.

Native baseline remains the historical native PASS tip. Predecessor is the
completed audit-harness tip. Successor HEAD may carry an approved runtime
Dockerfile delta; ``runtime_inputs_unchanged`` is therefore not claimed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

NATIVE_BASELINE_SHA = "686369bd085bd073cb44bd791cb1c0e5beba8323"
NATIVE_ACCEPTANCE_RUN = "38627426197"
AUDIT_PREDECESSOR_SHA = "48d9fc5bda2ca739e6ea7cd6c938f3b5603f8c67"
AUDIT_PREDECESSOR_RUN = "38637415562"

# Paths that may change on an audit-harness commit without invalidating the
# native-acceptance runtime baseline (phase A: baseline → predecessor).
AUDIT_ONLY_PATH_PREFIXES: tuple[str, ...] = (
    ".github/workflows/sec03c-candidate7-image-audit.yml",
    "scripts/image_audit_candidate7.py",
    "scripts/image_audit_candidate7_history.py",
    "scripts/image_audit_candidate7_smoke.py",
    "tests/image_audit/",
    "docs/operations/sec-03c-candidate7-image-audit.md",
)

# Successor phase (predecessor → HEAD) may also touch these reviewed paths.
APPROVED_DELTA_DOCKERFILES: tuple[str, ...] = (
    "backend/Dockerfile",
    "web/Dockerfile",
    "deploy/nginx/Dockerfile",
)

APPROVED_GOSU_EVIDENCE_DOC = "docs/operations/sec-03c-gosu-applicability.md"

# Exact SHA-256 of file bytes at successor HEAD (git blob contents).
APPROVED_SUCCESSOR_CONTENT_SHA256: dict[str, str] = {
    "backend/Dockerfile": (
        "6a57fbae29555e70eaa231f898b3ec88d26314a9999f41443f7de43a4eb76355"
    ),
    "web/Dockerfile": (
        "426573042ae4812bf3a9d97b2d9a18d5f763178f1bdd81d2edf8bfcdb6b370ed"
    ),
    "deploy/nginx/Dockerfile": (
        "9ffc88bde15b88d7dfcec759d8b5607a4ac345dbf1f70a6852f03c9f234d804c"
    ),
    APPROVED_GOSU_EVIDENCE_DOC: (
        "8708bb39fc4840f2c76f6015e040e39444a2867fa3ad41a97a23bb08f3e7271a"
    ),
}

# Build/runtime inputs that must remain byte-identical to native baseline
# except for the three approved Dockerfile paths (checked separately).
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

IMMUTABLE_LOCK_SHA256: dict[str, str] = {
    "backend/uv.lock": (
        "db530eda51c316655b3d6d9df5da192459f334a1963ce65cd60269cc45077a36"
    ),
    "web/package-lock.json": (
        "e62923098830723100a168e46bd8e83e0265dce38b92603f25504fb4c7fc7f6c"
    ),
}


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


def _run_git_bytes(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
        raise RuntimeError(
            f"git {' '.join(args)} failed: {err.strip() or proc.returncode}"
        )
    return proc.stdout


def resolve_commit(repo: Path, rev: str) -> str:
    """Return full 40-char lowercase SHA for a commit object, or raise."""
    proc = _run_git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")
    sha = proc.stdout.strip().lower()
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise RuntimeError(f"resolved revision is not a full commit SHA: {sha!r}")
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


def path_is_successor_allowed(path: str) -> bool:
    text = path.strip().replace("\\", "/")
    if path_is_audit_only(text):
        return True
    if text in APPROVED_DELTA_DOCKERFILES:
        return True
    return text == APPROVED_GOSU_EVIDENCE_DOC


def changed_paths(repo: Path, *, baseline: str, head: str) -> list[str]:
    proc = _run_git(repo, "diff", "--name-only", f"{baseline}..{head}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def runtime_input_diff_names(
    repo: Path,
    *,
    baseline: str,
    head: str,
    exclude: frozenset[str] | None = None,
) -> list[str]:
    proc = _run_git(
        repo,
        "diff",
        "--name-only",
        f"{baseline}..{head}",
        "--",
        *RUNTIME_BUILD_INPUT_PATHS,
    )
    names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if exclude:
        names = [n for n in names if n not in exclude]
    return names


def blob_bytes_at(repo: Path, commit: str, path: str) -> bytes:
    """Read file bytes from the given commit (not the working tree)."""
    return _run_git_bytes(repo, "show", f"{commit}:{path}")


def blob_sha256_at(repo: Path, commit: str, path: str) -> str:
    return hashlib.sha256(blob_bytes_at(repo, commit, path)).hexdigest()


def prove_baseline_history(
    repo: Path,
    *,
    baseline_sha: str,
    expected_head_sha: str,
) -> dict[str, Any]:
    """Legacy audit-only proof (no approved runtime delta).

    Retained for offline regressions that model the predecessor tip. Successor
    tips with Dockerfile pins must use :func:`prove_successor_history`.
    """
    evidence: dict[str, Any] = {
        "ok": False,
        "repo": str(repo),
        "requested_baseline": baseline_sha,
        "requested_head": expected_head_sha,
        "contract": "audit_only_vs_native_baseline",
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


def prove_successor_history(
    repo: Path,
    *,
    expected_head_sha: str,
    native_baseline_sha: str = NATIVE_BASELINE_SHA,
    audit_predecessor_sha: str = AUDIT_PREDECESSOR_SHA,
) -> dict[str, Any]:
    """Fail-closed successor contract for the post-OS-pin candidate-7 tip."""
    evidence: dict[str, Any] = {
        "ok": False,
        "repo": str(repo),
        "contract": "successor_approved_runtime_delta",
        "requested_head": expected_head_sha,
        "requested_native_baseline": native_baseline_sha,
        "requested_audit_predecessor": audit_predecessor_sha,
        "native_acceptance": {
            "run": NATIVE_ACCEPTANCE_RUN,
            "baseline_sha": native_baseline_sha,
            "verdict": "PASS",
            "note": "Historical native release PASS; not image-security PASS",
        },
        "audit_predecessor": {
            "run": AUDIT_PREDECESSOR_RUN,
            "sha": audit_predecessor_sha,
            "note": "Prior candidate-7 audit tip (technical history preserved)",
        },
        "artifact_kind": "NEW_AUDIT_ARTIFACT",
        "runtime_inputs_unchanged": False,
        "approved_runtime_delta": False,
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
        baseline = resolve_commit(repo, native_baseline_sha)
        evidence["resolved_native_baseline"] = baseline
    except RuntimeError as exc:
        evidence["error_code"] = "baseline_missing"
        evidence["message"] = str(exc)
        return evidence

    try:
        predecessor = resolve_commit(repo, audit_predecessor_sha)
        evidence["resolved_audit_predecessor"] = predecessor
    except RuntimeError as exc:
        evidence["error_code"] = "predecessor_missing"
        evidence["message"] = str(exc)
        return evidence

    if not is_ancestor(repo, ancestor=baseline, tip=predecessor):
        evidence["error_code"] = "baseline_not_ancestor"
        evidence["message"] = (
            f"{baseline} is not an ancestor of predecessor {predecessor}"
        )
        return evidence
    if not is_ancestor(repo, ancestor=predecessor, tip=head):
        evidence["error_code"] = "predecessor_not_ancestor"
        evidence["message"] = f"{predecessor} is not an ancestor of {head}"
        return evidence
    evidence["baseline_is_ancestor"] = True
    evidence["predecessor_is_ancestor"] = True

    # Phase A: native baseline → predecessor must remain audit-only / runtime-clean.
    phase_a_runtime = runtime_input_diff_names(
        repo, baseline=baseline, head=predecessor
    )
    phase_a_changed = changed_paths(repo, baseline=baseline, head=predecessor)
    phase_a_unexpected = [p for p in phase_a_changed if not path_is_audit_only(p)]
    evidence["phase_a"] = {
        "range": f"{baseline}..{predecessor}",
        "runtime_input_changed": phase_a_runtime,
        "changed_paths": phase_a_changed,
        "unexpected_changed_paths": phase_a_unexpected,
        "audit_only_ok": not phase_a_runtime and not phase_a_unexpected,
    }
    if phase_a_runtime:
        evidence["error_code"] = "phase_a_runtime_inputs_changed"
        evidence["message"] = (
            "baseline→predecessor runtime inputs changed: " + ", ".join(phase_a_runtime)
        )
        return evidence
    if phase_a_unexpected:
        evidence["error_code"] = "phase_a_unexpected_paths"
        evidence["message"] = "baseline→predecessor non-audit paths: " + ", ".join(
            phase_a_unexpected
        )
        return evidence

    # Phase B: predecessor → successor — approved delta + harness/docs only.
    phase_b_changed = changed_paths(repo, baseline=predecessor, head=head)
    phase_b_unexpected = [
        p for p in phase_b_changed if not path_is_successor_allowed(p)
    ]
    evidence["phase_b"] = {
        "range": f"{predecessor}..{head}",
        "changed_paths": phase_b_changed,
        "unexpected_changed_paths": phase_b_unexpected,
    }
    if phase_b_unexpected:
        evidence["error_code"] = "phase_b_unexpected_paths"
        evidence["message"] = (
            "predecessor→successor paths outside approved contract: "
            + ", ".join(phase_b_unexpected)
        )
        return evidence

    # Non-approved runtime inputs must be unchanged vs predecessor (and baseline).
    frozen_runtime = runtime_input_diff_names(
        repo,
        baseline=predecessor,
        head=head,
        exclude=frozenset(APPROVED_DELTA_DOCKERFILES),
    )
    # deploy/nginx/ is a runtime tree; Dockerfile inside is approved, but any
    # other path under deploy/nginx must not change.
    nginx_changed = [
        p
        for p in changed_paths(repo, baseline=predecessor, head=head)
        if p.startswith("deploy/nginx/") and p not in APPROVED_DELTA_DOCKERFILES
    ]
    evidence["phase_b"]["frozen_runtime_changed"] = frozen_runtime
    evidence["phase_b"]["nginx_non_dockerfile_changed"] = nginx_changed
    if frozen_runtime:
        evidence["error_code"] = "runtime_inputs_changed"
        evidence["message"] = (
            "non-approved runtime/build inputs changed vs predecessor: "
            + ", ".join(frozen_runtime)
        )
        return evidence
    if nginx_changed:
        evidence["error_code"] = "runtime_inputs_changed"
        evidence["message"] = "deploy/nginx non-Dockerfile paths changed: " + ", ".join(
            nginx_changed
        )
        return evidence

    # Approved content hashes at HEAD (read from the commit under test).
    verified: dict[str, str] = {}
    mismatched: dict[str, dict[str, str]] = {}
    for path, want in APPROVED_SUCCESSOR_CONTENT_SHA256.items():
        try:
            got = blob_sha256_at(repo, head, path)
        except RuntimeError as exc:
            evidence["error_code"] = "approved_blob_missing"
            evidence["message"] = f"{path} missing at HEAD: {exc}"
            return evidence
        verified[path] = got
        if got != want:
            mismatched[path] = {"expected": want, "actual": got}
    evidence["approved_content_sha256_at_head"] = verified
    if mismatched:
        evidence["error_code"] = "approved_content_hash_mismatch"
        evidence["mismatched_approved_content"] = mismatched
        evidence["message"] = (
            "approved successor file content hash mismatch: " + ", ".join(mismatched)
        )
        return evidence

    # Immutable locks must match pinned digests at HEAD.
    lock_verified: dict[str, str] = {}
    for path, want in IMMUTABLE_LOCK_SHA256.items():
        try:
            got = blob_sha256_at(repo, head, path)
        except RuntimeError as exc:
            evidence["error_code"] = "lock_blob_missing"
            evidence["message"] = f"{path} missing at HEAD: {exc}"
            return evidence
        lock_verified[path] = got
        if got != want:
            evidence["error_code"] = "lock_hash_mismatch"
            evidence["message"] = f"{path} SHA-256 {got} != approved {want}"
            evidence["lock_hashes_at_head"] = lock_verified
            return evidence
    evidence["lock_hashes_at_head"] = lock_verified

    # Require the three Dockerfiles actually differ from predecessor (delta present).
    dockerfile_delta = [
        p
        for p in APPROVED_DELTA_DOCKERFILES
        if p in phase_b_changed
        or blob_sha256_at(repo, head, p) != blob_sha256_at(repo, predecessor, p)
    ]
    evidence["phase_b"]["dockerfile_delta_paths"] = dockerfile_delta
    missing_delta = [p for p in APPROVED_DELTA_DOCKERFILES if p not in dockerfile_delta]
    if missing_delta:
        evidence["error_code"] = "approved_dockerfile_delta_missing"
        evidence["message"] = (
            "expected Dockerfile delta absent vs predecessor: "
            + ", ".join(missing_delta)
        )
        return evidence

    evidence["ok"] = True
    evidence["error_code"] = None
    evidence["approved_runtime_delta"] = True
    evidence["runtime_inputs_unchanged"] = False
    evidence["phase_a_audit_only"] = True
    evidence["phase_b_approved_delta_only"] = True
    evidence["changed_paths_baseline_to_head"] = changed_paths(
        repo, baseline=baseline, head=head
    )
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prove candidate-7 successor history and approved runtime delta"
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--native-baseline-sha", default=NATIVE_BASELINE_SHA)
    parser.add_argument("--audit-predecessor-sha", default=AUDIT_PREDECESSOR_SHA)
    parser.add_argument(
        "--legacy-audit-only",
        action="store_true",
        help="Use legacy prove_baseline_history (no approved Dockerfile delta)",
    )
    parser.add_argument("--baseline-sha", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--json-out", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.legacy_audit_only:
        baseline = args.baseline_sha or args.native_baseline_sha
        evidence = prove_baseline_history(
            args.repo,
            baseline_sha=baseline,
            expected_head_sha=args.expected_head_sha,
        )
    else:
        evidence = prove_successor_history(
            args.repo,
            expected_head_sha=args.expected_head_sha,
            native_baseline_sha=args.native_baseline_sha,
            audit_predecessor_sha=args.audit_predecessor_sha,
        )
    text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if evidence.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
