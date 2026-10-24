#!/usr/bin/env python3
"""SEC-08/09 final candidate image audit wrapper (audit-only, no product logic).

Reuses the candidate-7 gate (`image_audit_candidate7.run_candidate7_gate`) for
the seven runtime targets and adds only what the final audit needs:

* ``authorize`` — push authorization of one audit-only commit whose single
  parent is the pinned remediated product source; that source's parent, tree
  and exact product manifest, and the exact audit manifest (no message match).
* ``source-proof`` — the clean build context is ``git archive`` of the product
  source SHA: file set and blob ids equal the Git tree, pinned runtime hashes
  match, and runtime inputs equal those at the audit commit.
* ``audit`` — candidate-7 gate with a recording runner that proves one advisory
  DB download and an unchanged DB snapshot across all seven scans; corrects the
  inherited native/predecessor metadata (old approvals do not transfer).
* ``smoke-isolated`` — API certifi pin plus executor / egress-proxy import and
  startup checks that the candidate-7 smoke does not cover.

Findings are not technical failures. No VEX, residual acceptance,
ignore-unfixed or severity lowering is applied here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import image_audit_candidate7 as c7  # noqa: E402
from fetchnow_release.bounded_subprocess import run_bounded  # noqa: E402

import image_audit as ia  # noqa: E402

GATE_NAME = "sec-08-09-final-candidate-image-audit"
PRODUCT_SOURCE_SHA = "3792c377ecde1218ad9da9f3d5b91d620ed63b46"
PRODUCT_SOURCE_PARENT_SHA = "5dfd0524758c7ae55e04cc7045f709c6cb22c66f"
PRODUCT_SOURCE_TREE = "e9dbbc4588c2e968fd32e0201dac99b075924134"
AUDITED_SOURCE_SHA = PRODUCT_SOURCE_SHA
HISTORICAL_SOURCE_SHA = "9934ebe63943e7c1007564402e50258865a3a380"
BRANCH_REF = "refs/heads/codex/security-sec-09-network-isolation"
HISTORICAL_RUN = "38689849543"
HISTORICAL_SUMMARY_SHA256 = (
    "163d272b35cf43b06efd6888e2083607b01e4d612a09160e4c270a1db015448f"
)
PREVIOUS_AUDIT_RUN = "40031834156"
PREVIOUS_AUDIT_HARNESS_SHA = PRODUCT_SOURCE_PARENT_SHA
PREVIOUS_AUDIT_SUMMARY_SHA256 = (
    "58ae63d73f0f5140ff4e41029ba98add16623be336c98ee18a050b988d407cd4"
)
NATIVE_STORAGE_RUN = "40004249842"

PRODUCT_MANIFEST: tuple[str, ...] = (
    "backend/uv.lock",
    "deploy/nginx/Dockerfile",
    "docs/operations/sec-08-09-final-candidate-remediation-20261024.md",
    "scripts/image_audit_candidate7_smoke.py",
    "tests/image_audit/test_final_candidate_remediation_unit.py",
    "tests/image_audit/test_image_audit_candidate7_smoke_unit.py",
    "web/Dockerfile",
)

AUDIT_MANIFEST: tuple[str, ...] = (
    ".github/workflows/sec08-09-final-candidate-image-audit.yml",
    "docs/operations/sec-08-09-remediated-source-image-audit.md",
    "scripts/image_audit_final_candidate.py",
    "tests/image_audit/test_image_audit_final_candidate_unit.py",
)

PRODUCT_RUNTIME_SHA256: dict[str, str] = {
    "backend/Dockerfile": (
        "6a57fbae29555e70eaa231f898b3ec88d26314a9999f41443f7de43a4eb76355"
    ),
    "backend/Dockerfile.media-executor": (
        "9462f93d05d85277272a544f6960924d050b036b609f8cb06a8f8a0d01c73f09"
    ),
    "backend/Dockerfile.media-net-executor": (
        "cd72e370ff67e174b02bea038ea8f03a9378453bc5af3a094c4f457a71709251"
    ),
    "backend/Dockerfile.media-egress-proxy": (
        "876c565b643314bcf6057ca943240030436911cdbc6d753b676fbe85d68e2f1a"
    ),
    "web/Dockerfile": (
        "130edb1b23e0b1cf896db9ed328251eeecd21b1a7acce93439324f5e2f28deba"
    ),
    "deploy/nginx/Dockerfile": (
        "b4f065d5e1086da4b993f0183fc9471dcab8064961fdfa8c8cc6b48e4baab38a"
    ),
    "backend/uv.lock": (
        "be412a0b63f41f3fea408b6aa1a199834780e15e4888f634ff9eb6f43bda08a4"
    ),
    "backend/pyproject.toml": (
        "0005f35e735f396a2dd3f6eccddb3cedf7416713c606be6f1caab9768a39b8f2"
    ),
    "backend/ci/pytest-requirements.txt": (
        "ef963b8d360983d177cdd0e61655400f52edb951738c18450b3d5607564f8391"
    ),
    "web/package-lock.json": (
        "e62923098830723100a168e46bd8e83e0265dce38b92603f25504fb4c7fc7f6c"
    ),
}
EXPECTED_CERTIFI = "2026.10.19"

DB_FILES: tuple[str, ...] = ("db/trivy.db", "db/metadata.json")
EXECUTOR_PREFLIGHT_MARKER = "media executor preflight failed"
PROXY_LISTEN_MARKER = "media-egress-proxy listening on 0.0.0.0:8888"
CMD_TIMEOUT_S = 60.0
PROXY_DEADLINE_S = 30.0

Runner = Callable[..., Any]


# --- authorization ---------------------------------------------------------------


def authorize(
    *,
    event_name: str,
    run_attempt: str,
    ref: str,
    github_sha: str,
    head_sha: str,
    parent_shas: Sequence[str],
    changed_paths: Sequence[str],
    product_parent_shas: Sequence[str],
    product_tree: str,
    product_changed_paths: Sequence[str],
) -> dict[str, Any]:
    reasons: list[str] = []
    if event_name != "push":
        reasons.append("event_not_push")
    if run_attempt != "1":
        reasons.append("run_attempt_not_1")
    if ref != BRANCH_REF:
        reasons.append("ref_mismatch")
    if head_sha != github_sha:
        reasons.append("head_not_github_sha")
    if list(parent_shas) != [PRODUCT_SOURCE_SHA]:
        reasons.append("parent_not_product_source")
    if list(product_parent_shas) != [PRODUCT_SOURCE_PARENT_SHA]:
        reasons.append("product_parent_mismatch")
    if product_tree != PRODUCT_SOURCE_TREE:
        reasons.append("product_tree_mismatch")
    if list(product_changed_paths) != sorted(set(product_changed_paths)) or list(
        product_changed_paths
    ) != list(PRODUCT_MANIFEST):
        reasons.append("product_paths_not_exact_manifest")
    if list(changed_paths) != sorted(set(changed_paths)) or list(changed_paths) != list(
        AUDIT_MANIFEST
    ):
        reasons.append("audit_paths_not_exact_manifest")
    return {
        "allowed": not reasons,
        "reasons": reasons,
        "product_source_sha": PRODUCT_SOURCE_SHA,
        "product_source_parent_sha": PRODUCT_SOURCE_PARENT_SHA,
        "product_source_tree": PRODUCT_SOURCE_TREE,
        "audit_harness_sha": head_sha,
        "parents": list(parent_shas),
        "changed_paths": list(changed_paths),
        "audit_manifest": list(AUDIT_MANIFEST),
        "product_parents": list(product_parent_shas),
        "observed_product_tree": product_tree,
        "product_changed_paths": list(product_changed_paths),
        "product_manifest": list(PRODUCT_MANIFEST),
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def cmd_authorize(args: argparse.Namespace) -> int:
    repo = args.repo
    head = _git(repo, "rev-parse", "HEAD").strip()
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    changed: list[str] = []
    if len(parents) == 1:
        changed = [
            line
            for line in _git(
                repo, "diff", "--name-only", "--no-renames", parents[0], head
            ).splitlines()
            if line.strip()
        ]
    product_parents = _git(
        repo, "rev-list", "--parents", "-n", "1", PRODUCT_SOURCE_SHA
    ).split()[1:]
    product_tree = _git(repo, "rev-parse", f"{PRODUCT_SOURCE_SHA}^{{tree}}").strip()
    product_changed = [
        line
        for line in _git(
            repo,
            "diff",
            "--name-only",
            "--no-renames",
            PRODUCT_SOURCE_PARENT_SHA,
            PRODUCT_SOURCE_SHA,
        ).splitlines()
        if line.strip()
    ]
    result = authorize(
        event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        ref=os.environ.get("GITHUB_REF", ""),
        github_sha=os.environ.get("GITHUB_SHA", ""),
        head_sha=head,
        parent_shas=parents,
        changed_paths=changed,
        product_parent_shas=product_parents,
        product_tree=product_tree,
        product_changed_paths=product_changed,
    )
    args.json_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"allowed={str(result['allowed']).lower()}\n")
    return 0


# --- clean source proof ------------------------------------------------------------


def compare_runtime_inputs(
    clean: Mapping[str, str], harness: Mapping[str, str]
) -> dict[str, Any]:
    keys = sorted(set(clean) | set(harness))
    differing = [k for k in keys if clean.get(k) != harness.get(k)]
    missing = sorted(k for k, v in clean.items() if v == "MISSING")
    return {
        "ok": not differing and not missing,
        "differing": differing,
        "missing_in_clean_source": missing,
    }


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def parse_ls_tree(raw: bytes) -> dict[str, tuple[str, str]]:
    """``git ls-tree -r -z`` output -> {path: (mode, blob id)}; non-blobs rejected."""
    entries: dict[str, tuple[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        if kind != "blob":
            raise ValueError(f"unsupported tree entry {kind} {path!r}")
        entries[path.decode()] = (mode, oid)
    return entries


def compare_clean_tree(
    clean_dir: Path, entries: Mapping[str, tuple[str, str]]
) -> dict[str, Any]:
    found: set[str] = set()
    for path in clean_dir.rglob("*"):
        if path.is_symlink() or path.is_file():
            found.add(path.relative_to(clean_dir).as_posix())
    mismatched: list[str] = []
    for rel, (mode, oid) in sorted(entries.items()):
        path = clean_dir / rel
        if rel not in found:
            continue
        if mode == "120000":
            data = os.readlink(path).encode() if path.is_symlink() else b""
        else:
            data = b"" if path.is_symlink() else path.read_bytes()
        if git_blob_id(data) != oid:
            mismatched.append(rel)
    missing = sorted(set(entries) - found)
    extra = sorted(found - set(entries))
    return {
        "ok": bool(entries) and not missing and not extra and not mismatched,
        "tree_blob_count": len(entries),
        "clean_file_count": len(found),
        "missing": missing,
        "extra": extra,
        "blob_mismatches": mismatched,
    }


def compare_pinned_runtime(clean_dir: Path) -> dict[str, Any]:
    observed = {
        rel: (c7.sha256_file(clean_dir / rel) if (clean_dir / rel).is_file() else None)
        for rel in PRODUCT_RUNTIME_SHA256
    }
    differing = sorted(
        rel for rel, want in PRODUCT_RUNTIME_SHA256.items() if observed[rel] != want
    )
    return {"ok": not differing, "observed": observed, "differing": differing}


def cmd_source_proof(args: argparse.Namespace) -> int:
    repo = args.repo
    source = args.audited_source_sha
    resolved = _git(repo, "rev-parse", f"{source}^{{commit}}").strip()
    source_tree = _git(repo, "rev-parse", f"{source}^{{tree}}").strip()
    clean_hashes = c7.collect_runtime_input_hashes(args.clean_dir)
    harness_hashes = c7.collect_runtime_input_hashes(repo)
    comparison = compare_runtime_inputs(clean_hashes, harness_hashes)
    entries = parse_ls_tree(
        subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "-r", "-z", source],
            check=True,
            capture_output=True,
        ).stdout
    )
    tree_check = compare_clean_tree(args.clean_dir, entries)
    pinned = compare_pinned_runtime(args.clean_dir)
    identity_ok = (
        source == PRODUCT_SOURCE_SHA
        and resolved == PRODUCT_SOURCE_SHA
        and source_tree == PRODUCT_SOURCE_TREE
    )
    trees = {
        rel: _git(repo, "rev-parse", f"{source}:{rel}").strip()
        for rel in ("backend", "web", "deploy/nginx")
    }
    proof = {
        "ok": identity_ok and comparison["ok"] and tree_check["ok"] and pinned["ok"],
        "audited_source_sha": source,
        "product_source_identity_ok": identity_ok,
        "resolved_source_commit": resolved,
        "source_tree": source_tree,
        "expected_source_tree": PRODUCT_SOURCE_TREE,
        "context_trees": trees,
        "audit_harness_sha": _git(repo, "rev-parse", "HEAD").strip(),
        "clean_build_context": "git archive of product source SHA (no worktree files)",
        "clean_tree_vs_git_tree": tree_check,
        "pinned_runtime_sha256": pinned,
        "runtime_input_hashes": clean_hashes,
        "comparison_vs_harness_checkout": comparison,
    }
    args.json_out.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": proof["ok"],
                "identity_ok": identity_ok,
                "comparison": comparison,
                "tree_ok": tree_check["ok"],
                "pinned_ok": pinned["ok"],
            },
            sort_keys=True,
        )
    )
    return 0 if proof["ok"] else 1


# --- frozen DB recorder ------------------------------------------------------------


def hash_db(cache_dir: Path) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for rel in DB_FILES:
        path = cache_dir / rel
        out[rel] = c7.sha256_file(path) if path.is_file() else None
    return out


@dataclass
class DbFreezeRunner:
    """Runner wrapper recording every Trivy invocation of one gate run."""

    inner: Runner
    cache_dir: Path
    trivy_bin: str
    downloads: int = 0
    scans: list[list[str]] = field(default_factory=list)
    after_download: dict[str, str | None] | None = None
    download_exit_codes: list[int | str | None] = field(default_factory=list)
    scan_exit_codes: list[int | str | None] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> Any:
        argv_list = list(argv)
        is_trivy = bool(argv_list) and argv_list[0] == self.trivy_bin
        is_download = is_trivy and "--download-db-only" in argv_list
        is_scan = is_trivy and not is_download and "image" in argv_list
        try:
            result = self.inner(argv_list, **kwargs)
        except Exception as exc:
            if is_download:
                self.downloads += 1
                self.download_exit_codes.append(type(exc).__name__)
            elif is_scan:
                self.scans.append(argv_list)
                self.scan_exit_codes.append(type(exc).__name__)
            raise
        if is_download:
            self.downloads += 1
            self.download_exit_codes.append(getattr(result, "returncode", None))
            self.after_download = hash_db(self.cache_dir)
        elif is_scan:
            self.scans.append(argv_list)
            self.scan_exit_codes.append(getattr(result, "returncode", None))
        return result

    def proof(self, after_scans: Mapping[str, str | None]) -> dict[str, Any]:
        offline_flags = ("--skip-db-update", "--skip-java-db-update", "--offline-scan")
        scans_offline = all(
            all(flag in argv for flag in offline_flags) for argv in self.scans
        )
        hashes_present = bool(self.after_download) and all(
            v for v in (self.after_download or {}).values()
        )
        unchanged = hashes_present and dict(self.after_download or {}) == dict(
            after_scans
        )
        return {
            "ok": self.downloads == 1 and scans_offline and unchanged,
            "db_download_invocations": self.downloads,
            "db_download_exit_codes": list(self.download_exit_codes),
            "scan_invocations": len(self.scans),
            "scan_exit_codes": list(self.scan_exit_codes),
            "all_scans_offline_skip_update": scans_offline,
            "db_sha256_after_download": self.after_download,
            "db_sha256_after_all_scans": dict(after_scans),
            "snapshot_unchanged_across_scans": unchanged,
        }


def finalize_summary(
    summary: dict[str, Any],
    *,
    freeze: Mapping[str, Any],
    audited_source_sha: str,
    audit_harness_sha: str,
    source_proof: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """Replace inherited candidate-7 metadata; return (summary, technical)."""
    out = dict(summary)
    out["gate"] = GATE_NAME
    out["base_gate"] = c7.GATE_NAME
    out["artifact_kind"] = "NEW_AUDIT_ARTIFACT"
    out["does_not_inherit_prior_image_ids"] = True
    out["native_acceptance_history"] = {
        "run": NATIVE_STORAGE_RUN,
        "source_sha": HISTORICAL_SOURCE_SHA,
        "transfers_to_these_images": False,
        "note": (
            "Native storage/resource acceptance ran on an earlier source with "
            "synthetic L/A/B revisions; not byte-identical to this audit's images."
        ),
    }
    out["audit_predecessor"] = {
        "run": PREVIOUS_AUDIT_RUN,
        "source_sha": HISTORICAL_SOURCE_SHA,
        "harness_sha": PREVIOUS_AUDIT_HARNESS_SHA,
        "summary_sha256": PREVIOUS_AUDIT_SUMMARY_SHA256,
        "note": "Previous final audit; reconciled offline, nothing inherited",
    }
    out["historical_approval_source"] = {
        "run": HISTORICAL_RUN,
        "summary_sha256": HISTORICAL_SUMMARY_SHA256,
        "note": "Approvals bound to historical artifacts; not applied or extended",
    }
    out.pop("approved_runtime_delta", None)
    out.pop("runtime_inputs_unchanged", None)
    out["shas"] = {
        "audited_source_sha": audited_source_sha,
        "product_source_parent_sha": PRODUCT_SOURCE_PARENT_SHA,
        "product_source_tree": PRODUCT_SOURCE_TREE,
        "audit_harness_sha": audit_harness_sha,
    }
    out["source_proof_ok"] = bool(source_proof and source_proof.get("ok"))
    source_identity_ok = (
        audited_source_sha == PRODUCT_SOURCE_SHA
        and bool(source_proof)
        and (source_proof or {}).get("audited_source_sha") == PRODUCT_SOURCE_SHA
    )
    out["source_identity_ok"] = source_identity_ok
    out["db_freeze"] = dict(freeze)
    technical = summary.get("status") == ia.STATUS_TECHNICAL
    error_code = None
    if not source_identity_ok:
        error_code = "source_identity_mismatch"
    elif not out["source_proof_ok"]:
        error_code = "source_proof_failed"
    elif not freeze.get("ok"):
        error_code = "db_snapshot_not_frozen"
    if error_code is not None:
        technical = True
        out["status"] = ia.STATUS_TECHNICAL
        out["error_code"] = error_code
    verdicts = dict(summary.get("verdicts") or {})
    verdicts["native_acceptance_history"] = "NOT_TRANSFERRED"
    verdicts["audit_execution_completeness"] = "FAIL" if technical else "PASS"
    verdicts["image_security"] = out.get("status")
    verdicts["residual_acceptance"] = "NOT_GRANTED"
    verdicts["production_readiness"] = "NO"
    out["verdicts"] = verdicts
    return out, technical


def cmd_audit(args: argparse.Namespace) -> int:
    targets = c7.parse_targets_file(args.targets_file)
    source_proof = json.loads(args.source_proof.read_text(encoding="utf-8"))
    recorder = DbFreezeRunner(
        inner=run_bounded, cache_dir=args.cache_dir, trivy_bin=args.trivy_bin
    )
    summary, code = c7.run_candidate7_gate(
        trivy_bin=args.trivy_bin,
        cache_dir=args.cache_dir,
        targets=targets,
        expected_platform=args.expected_platform,
        work_dir=args.work_dir,
        repo_root=args.clean_dir,
        audited_source_sha=args.audited_source_sha,
        audit_commit_sha=args.audit_harness_sha,
        runner=recorder,
    )
    freeze = recorder.proof(hash_db(args.cache_dir))
    final, technical = finalize_summary(
        summary,
        freeze=freeze,
        audited_source_sha=args.audited_source_sha,
        audit_harness_sha=args.audit_harness_sha,
        source_proof=source_proof,
    )
    text = ia.redact_secrets(json.dumps(final, indent=2, ensure_ascii=True))
    args.summary_out.write_text(text + "\n", encoding="utf-8")
    exit_code: int = ia.EXIT_FAIL if technical else code
    print(
        json.dumps(
            {
                "status": final.get("status"),
                "technical": technical,
                "exit_code": exit_code,
                "db_freeze_ok": freeze["ok"],
            },
            sort_keys=True,
        )
    )
    return exit_code


# --- executor / proxy smoke ----------------------------------------------------------

EXECUTOR_IMPORT = (
    "import fetchnow.media_executor.__main__, fetchnow.media_executor.server, "
    "fetchnow.media_executor.runner, fetchnow.media_executor.job_process, "
    "fetchnow.media_executor.argv; print('import_ok')"
)
PROXY_IMPORT = (
    "import fetchnow.media_egress_proxy.server, fetchnow.media_egress_proxy.policy; "
    "print('import_ok')"
)
PROXY_CONNECT = (
    "import socket; s=socket.create_connection(('127.0.0.1', 8888), timeout=3); "
    "s.close(); print('connect_ok')"
)


CERTIFI_PROBE = (
    "import certifi, importlib.metadata as m; "
    "print(m.version('certifi')); print(certifi.where())"
)


def certifi_argv(image: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/opt/venv/bin/python",
        image,
        "-c",
        CERTIFI_PROBE,
    ]


def evaluate_certifi(rc: int, out: str) -> SmokeStep:
    lines = out.strip().splitlines()
    ok = (
        rc == 0
        and len(lines) == 2
        and lines[0] == EXPECTED_CERTIFI
        and lines[1].startswith("/opt/venv/")
    )
    return SmokeStep("certifi_pin", ok, f"rc={rc} out={out[-300:]!r}")


def import_argv(image: str, code: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "python",
        image,
        "-c",
        code,
    ]


def shell_argv(image: str, script: str, *, user: str | None = None) -> list[str]:
    argv = ["docker", "run", "--rm", "--network", "none"]
    if user is not None:
        argv += ["--user", user]
    return [*argv, "--entrypoint", "/bin/sh", image, "-c", script]


def executor_startup_argv(image: str) -> list[str]:
    """Unprivileged start: the supervisor must refuse (fail closed)."""
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "-e",
        "MEDIA_EXECUTOR_WORK_ROOT=/tmp/fn-work",
        "-e",
        "MEDIA_EXECUTOR_SOCKET=/tmp/fn-sock/executor.sock",
        image,
    ]


@dataclass
class SmokeStep:
    name: str
    ok: bool
    detail: str


def _run(argv: Sequence[str], timeout: float = CMD_TIMEOUT_S) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    return proc.returncode, proc.stdout[-4000:], proc.stderr[-4000:]


def evaluate_executor_startup(rc: int, stderr: str) -> SmokeStep:
    ok = rc not in (0, -1) and EXECUTOR_PREFLIGHT_MARKER in stderr
    return SmokeStep("startup_fail_closed", ok, f"rc={rc} stderr={stderr[-300:]!r}")


def smoke_executor(image: str, *, network_profile: bool) -> list[SmokeStep]:
    steps: list[SmokeStep] = []
    rc, out, err = _run(import_argv(image, EXECUTOR_IMPORT))
    steps.append(
        SmokeStep("import", rc == 0 and "import_ok" in out, f"rc={rc} {err[-200:]}")
    )
    if network_profile:
        tool = (
            "set -eu; test -x /usr/local/bin/sec08-launch; "
            "/opt/venv/bin/yt-dlp --version; "
            "if command -v ffmpeg >/dev/null 2>&1; "
            "then echo ffmpeg_present; exit 3; fi; "
            "echo tools_ok"
        )
    else:
        tool = (
            "set -eu; test -x /usr/local/bin/sec08-launch; "
            "ffmpeg -hide_banner -version | head -n1; "
            "ffprobe -hide_banner -version | head -n1; echo tools_ok"
        )
    rc, out, err = _run(shell_argv(image, tool))
    steps.append(
        SmokeStep("tools", rc == 0 and "tools_ok" in out, f"rc={rc} out={out[-300:]!r}")
    )
    rc, _out, err = _run(executor_startup_argv(image))
    steps.append(evaluate_executor_startup(rc, err))
    return steps


def smoke_proxy(image: str, name: str) -> list[SmokeStep]:
    steps: list[SmokeStep] = []
    rc, out, err = _run(import_argv(image, PROXY_IMPORT))
    steps.append(
        SmokeStep("import", rc == 0 and "import_ok" in out, f"rc={rc} {err[-200:]}")
    )
    rc, out, err = _run(
        ["docker", "run", "-d", "--network", "none", "--name", name, image]
    )
    if rc != 0:
        steps.append(SmokeStep("startup", False, f"run rc={rc} {err[-300:]}"))
        return steps
    deadline = time.monotonic() + PROXY_DEADLINE_S
    logs = ""
    while time.monotonic() < deadline:
        _, lout, lerr = _run(["docker", "logs", name])
        logs = lout + lerr
        if PROXY_LISTEN_MARKER in logs:
            break
        time.sleep(1)
    steps.append(SmokeStep("startup_listen", PROXY_LISTEN_MARKER in logs, logs[-300:]))
    rc, out, err = _run(["docker", "exec", name, "python", "-c", PROXY_CONNECT])
    steps.append(
        SmokeStep(
            "loopback_connect", rc == 0 and "connect_ok" in out, f"rc={rc} {err[-200:]}"
        )
    )
    rc, out, _ = _run(["docker", "inspect", "-f", "{{.Config.User}}", name])
    steps.append(SmokeStep("runtime_user", out.strip() == "65534:65534", out.strip()))
    return steps


def cmd_smoke_isolated(args: argparse.Namespace) -> int:
    targets = json.loads(args.targets_file.read_text(encoding="utf-8"))
    proxy_name = f"fn-final-proxy-{uuid.uuid4().hex[:10]}"
    report: dict[str, Any] = {}
    cleanup_errors: list[str] = []
    try:
        rc, out, _err = _run(certifi_argv(targets["api"]))
        report["api"] = [evaluate_certifi(rc, out)]
        report["media-executor"] = smoke_executor(
            targets["media-executor"], network_profile=False
        )
        report["media-net-executor"] = smoke_executor(
            targets["media-net-executor"], network_profile=True
        )
        report["egress-proxy"] = smoke_proxy(targets["egress-proxy"], proxy_name)
    finally:
        rc, _, err = _run(["docker", "rm", "-f", "-v", proxy_name])
        if rc != 0 and "No such container" not in err:
            cleanup_errors.append(f"rm {proxy_name}: {err[-200:]}")
        rc, out, _ = _run(["docker", "ps", "-aq", "--filter", f"name=^{proxy_name}$"])
        if rc != 0 or out.strip():
            cleanup_errors.append(f"container {proxy_name} still present")
    serial = {
        target: [step.__dict__ for step in steps] for target, steps in report.items()
    }
    ok = (
        len(serial) == 4
        and all(step["ok"] for steps in serial.values() for step in steps)
        and not cleanup_errors
    )
    payload = {"ok": ok, "targets": serial, "cleanup_errors": cleanup_errors}
    args.json_out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": ok}, sort_keys=True))
    return 0 if ok else 1


# --- CLI ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SEC-08/09 final candidate image audit"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("authorize")
    p.add_argument("--repo", type=Path, default=Path.cwd())
    p.add_argument("--json-out", type=Path, required=True)
    p.set_defaults(func=cmd_authorize)

    p = sub.add_parser("source-proof")
    p.add_argument("--repo", type=Path, default=Path.cwd())
    p.add_argument("--clean-dir", type=Path, required=True)
    p.add_argument("--audited-source-sha", default=AUDITED_SOURCE_SHA)
    p.add_argument("--json-out", type=Path, required=True)
    p.set_defaults(func=cmd_source_proof)

    p = sub.add_parser("audit")
    p.add_argument("--trivy-bin", default="trivy")
    p.add_argument("--cache-dir", type=Path, required=True)
    p.add_argument("--targets-file", type=Path, required=True)
    p.add_argument("--expected-platform", default=ia.DEFAULT_EXPECTED_PLATFORM)
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--summary-out", type=Path, required=True)
    p.add_argument("--clean-dir", type=Path, required=True)
    p.add_argument("--source-proof", type=Path, required=True)
    p.add_argument("--audited-source-sha", default=AUDITED_SOURCE_SHA)
    p.add_argument("--audit-harness-sha", required=True)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("smoke-isolated")
    p.add_argument("--targets-file", type=Path, required=True)
    p.add_argument("--json-out", type=Path, required=True)
    p.set_defaults(func=cmd_smoke_isolated)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
