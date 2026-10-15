"""Offline regressions for candidate-7 baseline / successor history proof."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WORKFLOW = ROOT / ".github/workflows/sec03c-candidate7-image-audit.yml"
sys.path.insert(0, str(SCRIPTS))

import image_audit_candidate7_history as hist

MARKER = "[native-once-20261015-img7smk]"
EXPECTED_SUBJECT = "test(security): SEC-03C/08/09 candidate7 successor audit " + MARKER


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "-c", "init.templateDir=", "init")
    _git(path, "config", "user.name", "FetchNow Audit Test")
    _git(path, "config", "user.email", "audit-test@example.invalid")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _sha256_text(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _seed_runtime_tree(repo: Path) -> None:
    for rel in (
        "backend/Dockerfile",
        "backend/Dockerfile.media-executor",
        "backend/Dockerfile.media-net-executor",
        "backend/Dockerfile.media-egress-proxy",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/alembic.ini",
        "backend/src/fetchnow/__init__.py",
        "backend/migrations/README",
        "web/Dockerfile",
        "web/package.json",
        "web/package-lock.json",
        "web/src/index.ts",
        "web/docker/nginx.conf",
        "deploy/nginx/Dockerfile",
        "deploy/nginx/nginx.conf",
        "compose.yaml",
    ):
        _write(repo, rel, f"fixture:{rel}\n")


def _patch_approved(
    monkeypatch: object, *, files: dict[str, str], locks: dict[str, str]
) -> None:
    monkeypatch.setattr(hist, "APPROVED_SUCCESSOR_CONTENT_SHA256", dict(files))
    monkeypatch.setattr(hist, "IMMUTABLE_LOCK_SHA256", dict(locks))


def _build_three_phase(
    repo: Path,
    monkeypatch: object,
    *,
    successor_extra: dict[str, str] | None = None,
    successor_docker_override: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """baseline → predecessor (audit-only) → successor (approved delta)."""
    _seed_runtime_tree(repo)
    baseline = _commit(repo, "baseline native")

    _write(repo, "scripts/image_audit_candidate7.py", "print('harness')\n")
    _write(repo, "tests/image_audit/test_x.py", "def test_x():\n    assert True\n")
    _write(
        repo,
        ".github/workflows/sec03c-candidate7-image-audit.yml",
        "name: audit\n",
    )
    _write(
        repo,
        "docs/operations/sec-03c-candidate7-image-audit.md",
        "# audit\n",
    )
    predecessor = _commit(repo, "audit predecessor")

    docker_bodies = {
        "backend/Dockerfile": (
            "FROM pin-api\nRUN apt-get install libpcre2-8-0=10.46-1~deb13u3\n"
        ),
        "web/Dockerfile": "FROM pin-web\nRUN apk add pcre2=10.49-r0 tiff=4.7.2-r0\n",
        "deploy/nginx/Dockerfile": (
            "FROM pin-gw\nRUN apk add pcre2=10.49-r0 tiff=4.7.2-r0\n"
        ),
    }
    if successor_docker_override:
        docker_bodies.update(successor_docker_override)
    gosu_body = "# gosu evidence append\n"
    for rel, body in docker_bodies.items():
        _write(repo, rel, body)
    _write(repo, hist.APPROVED_GOSU_EVIDENCE_DOC, gosu_body)
    if successor_extra:
        for rel, body in successor_extra.items():
            _write(repo, rel, body)

    approved = {rel: _sha256_text(body) for rel, body in docker_bodies.items()}
    approved[hist.APPROVED_GOSU_EVIDENCE_DOC] = _sha256_text(gosu_body)
    locks = {
        "backend/uv.lock": _sha256_text("fixture:backend/uv.lock\n"),
        "web/package-lock.json": _sha256_text("fixture:web/package-lock.json\n"),
    }
    _patch_approved(monkeypatch, files=approved, locks=locks)
    monkeypatch.setattr(hist, "NATIVE_BASELINE_SHA", baseline)
    monkeypatch.setattr(hist, "AUDIT_PREDECESSOR_SHA", predecessor)

    head = _commit(repo, "successor with approved delta")
    return baseline, predecessor, head


def test_shallow_clone_missing_baseline_fails(tmp_path: Path) -> None:
    origin = _init_repo(tmp_path / "origin")
    _seed_runtime_tree(origin)
    baseline = _commit(origin, "baseline native")
    _write(origin, "docs/note.txt", "mid\n")
    _commit(origin, "mid")
    _write(origin, "scripts/image_audit_candidate7.py", "print('audit')\n")
    tip = _commit(origin, "audit tip")

    shallow = tmp_path / "shallow"
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--no-local",
            f"file://{origin}",
            str(shallow),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = hist.prove_baseline_history(
        shallow,
        baseline_sha=baseline,
        expected_head_sha=_git(shallow, "rev-parse", "HEAD"),
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] == "baseline_missing"
    assert tip


def test_full_history_allows_baseline_and_audit_only_diff(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "full")
    _seed_runtime_tree(repo)
    baseline = _commit(repo, "baseline native")
    _write(repo, "scripts/image_audit_candidate7.py", "print('harness')\n")
    _write(repo, "tests/image_audit/test_x.py", "def test_x():\n    assert True\n")
    _write(
        repo,
        ".github/workflows/sec03c-candidate7-image-audit.yml",
        "name: audit\n",
    )
    _write(
        repo,
        "docs/operations/sec-03c-candidate7-image-audit.md",
        "# audit\n",
    )
    head = _commit(repo, "audit harness only")

    evidence = hist.prove_baseline_history(
        repo,
        baseline_sha=baseline,
        expected_head_sha=head,
    )
    assert evidence["ok"] is True
    assert evidence["baseline_is_ancestor"] is True
    assert evidence["runtime_inputs_unchanged"] is True
    assert evidence["audit_only_changes_only"] is True


def test_runtime_diff_blocks_audit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "runtime")
    _seed_runtime_tree(repo)
    baseline = _commit(repo, "baseline")
    _write(repo, "backend/uv.lock", "tampered-lock\n")
    head = _commit(repo, "lock change")
    evidence = hist.prove_baseline_history(
        repo,
        baseline_sha=baseline,
        expected_head_sha=head,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] == "runtime_inputs_changed"
    assert "backend/uv.lock" in evidence["runtime_input_changed"]


def test_missing_baseline_object_fails(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "nobase")
    _seed_runtime_tree(repo)
    head = _commit(repo, "only tip")
    missing = "a" * 40
    evidence = hist.prove_baseline_history(
        repo,
        baseline_sha=missing,
        expected_head_sha=head,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] == "baseline_missing"


def test_non_ancestor_baseline_fails(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "diverge")
    _seed_runtime_tree(repo)
    _commit(repo, "root")
    _git(repo, "checkout", "-b", "other")
    _write(repo, "other.txt", "x\n")
    other = _commit(repo, "other branch")
    primary = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", f"{other}^")
    _git(repo, "checkout", "-B", "mainline")
    _write(repo, "scripts/image_audit_candidate7.py", "print(1)\n")
    tip = _commit(repo, "tip on baseline line")
    assert primary
    evidence = hist.prove_baseline_history(
        repo,
        baseline_sha=other,
        expected_head_sha=tip,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] == "baseline_not_ancestor"


def test_head_mismatch_fails(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "mismatch")
    _seed_runtime_tree(repo)
    baseline = _commit(repo, "baseline")
    _write(repo, "scripts/image_audit_candidate7.py", "print(2)\n")
    real_head = _commit(repo, "real head")
    evidence = hist.prove_baseline_history(
        repo,
        baseline_sha=baseline,
        expected_head_sha="b" * 40,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] == "head_sha_mismatch"
    assert real_head != "b" * 40


def test_successor_happy_path(tmp_path: Path, monkeypatch: object) -> None:
    repo = _init_repo(tmp_path / "succ-ok")
    baseline, predecessor, head = _build_three_phase(repo, monkeypatch)
    evidence = hist.prove_successor_history(
        repo,
        expected_head_sha=head,
        native_baseline_sha=baseline,
        audit_predecessor_sha=predecessor,
    )
    assert evidence["ok"] is True
    assert evidence["runtime_inputs_unchanged"] is False
    assert evidence["approved_runtime_delta"] is True
    assert evidence["phase_a_audit_only"] is True
    assert evidence["phase_b_approved_delta_only"] is True
    assert evidence["artifact_kind"] == "NEW_AUDIT_ARTIFACT"


def test_successor_rejects_extra_runtime_src(
    tmp_path: Path, monkeypatch: object
) -> None:
    repo = _init_repo(tmp_path / "succ-src")
    baseline, predecessor, head = _build_three_phase(
        repo,
        monkeypatch,
        successor_extra={"backend/src/fetchnow/__init__.py": "tampered\n"},
    )
    evidence = hist.prove_successor_history(
        repo,
        expected_head_sha=head,
        native_baseline_sha=baseline,
        audit_predecessor_sha=predecessor,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] in {
        "runtime_inputs_changed",
        "phase_b_unexpected_paths",
    }


def test_successor_rejects_wrong_dockerfile_pin(
    tmp_path: Path, monkeypatch: object
) -> None:
    repo = _init_repo(tmp_path / "succ-pin")
    # Build with correct approved hashes, then rewrite Dockerfile and recommit
    # without updating the approved hash map → mismatch.
    baseline, predecessor, head = _build_three_phase(repo, monkeypatch)
    _write(repo, "backend/Dockerfile", "FROM evil\nRUN apt-get install evil=1\n")
    bad_head = _commit(repo, "wrong pin")
    evidence = hist.prove_successor_history(
        repo,
        expected_head_sha=bad_head,
        native_baseline_sha=baseline,
        audit_predecessor_sha=predecessor,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] == "approved_content_hash_mismatch"
    assert head != bad_head


def test_successor_rejects_extra_run_copy_via_hash(
    tmp_path: Path, monkeypatch: object
) -> None:
    repo = _init_repo(tmp_path / "succ-run")
    baseline, predecessor, head = _build_three_phase(repo, monkeypatch)
    current = (repo / "web/Dockerfile").read_text(encoding="utf-8")
    _write(repo, "web/Dockerfile", current + "COPY extra /evil\n")
    bad_head = _commit(repo, "extra COPY")
    evidence = hist.prove_successor_history(
        repo,
        expected_head_sha=bad_head,
        native_baseline_sha=baseline,
        audit_predecessor_sha=predecessor,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] == "approved_content_hash_mismatch"
    assert head != bad_head


def test_successor_rejects_lock_drift(tmp_path: Path, monkeypatch: object) -> None:
    repo = _init_repo(tmp_path / "succ-lock")
    baseline, predecessor, head = _build_three_phase(repo, monkeypatch)
    _write(repo, "backend/uv.lock", "lock-drift\n")
    bad_head = _commit(repo, "lock drift")
    evidence = hist.prove_successor_history(
        repo,
        expected_head_sha=bad_head,
        native_baseline_sha=baseline,
        audit_predecessor_sha=predecessor,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] in {
        "runtime_inputs_changed",
        "lock_hash_mismatch",
        "phase_b_unexpected_paths",
    }
    assert head != bad_head


def test_successor_rejects_compose_change(tmp_path: Path, monkeypatch: object) -> None:
    repo = _init_repo(tmp_path / "succ-compose")
    baseline, predecessor, head = _build_three_phase(
        repo,
        monkeypatch,
        successor_extra={"compose.yaml": "services: {}\n"},
    )
    evidence = hist.prove_successor_history(
        repo,
        expected_head_sha=head,
        native_baseline_sha=baseline,
        audit_predecessor_sha=predecessor,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] in {
        "runtime_inputs_changed",
        "phase_b_unexpected_paths",
    }


def test_successor_rejects_sidecar_dockerfile(
    tmp_path: Path, monkeypatch: object
) -> None:
    repo = _init_repo(tmp_path / "succ-side")
    baseline, predecessor, head = _build_three_phase(
        repo,
        monkeypatch,
        successor_extra={
            "backend/Dockerfile.media-executor": "FROM evil-sidecar\n",
        },
    )
    evidence = hist.prove_successor_history(
        repo,
        expected_head_sha=head,
        native_baseline_sha=baseline,
        audit_predecessor_sha=predecessor,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] in {
        "runtime_inputs_changed",
        "phase_b_unexpected_paths",
    }


def test_successor_missing_baseline_fails(tmp_path: Path, monkeypatch: object) -> None:
    repo = _init_repo(tmp_path / "succ-miss")
    _baseline, predecessor, head = _build_three_phase(repo, monkeypatch)
    evidence = hist.prove_successor_history(
        repo,
        expected_head_sha=head,
        native_baseline_sha="c" * 40,
        audit_predecessor_sha=predecessor,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] == "baseline_missing"


def test_successor_non_ancestor_predecessor_fails(
    tmp_path: Path, monkeypatch: object
) -> None:
    repo = _init_repo(tmp_path / "succ-div")
    baseline, _predecessor, head = _build_three_phase(repo, monkeypatch)
    _git(repo, "checkout", "--orphan", "orphan")
    _seed_runtime_tree(repo)
    orphan = _commit(repo, "orphan tip")
    _git(repo, "checkout", head)
    evidence = hist.prove_successor_history(
        repo,
        expected_head_sha=head,
        native_baseline_sha=baseline,
        audit_predecessor_sha=orphan,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] in {
        "predecessor_not_ancestor",
        "baseline_not_ancestor",
    }


def test_successor_mismatched_gosu_doc_hash(
    tmp_path: Path, monkeypatch: object
) -> None:
    repo = _init_repo(tmp_path / "succ-gosu")
    baseline, predecessor, head = _build_three_phase(repo, monkeypatch)
    _write(repo, hist.APPROVED_GOSU_EVIDENCE_DOC, "# tampered gosu doc\n")
    bad_head = _commit(repo, "gosu doc drift")
    evidence = hist.prove_successor_history(
        repo,
        expected_head_sha=bad_head,
        native_baseline_sha=baseline,
        audit_predecessor_sha=predecessor,
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] == "approved_content_hash_mismatch"
    assert head != bad_head


def test_workflow_has_fetch_depth_zero_and_successor_marker() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "fetch-depth: 0" in text
    assert "persist-credentials: false" in text
    assert MARKER in text
    assert "[native-once-20261015-img7suc]" not in text.split("EXPECTED_SUBJECT", 1)[1]
    assert "prove_successor" in text or "image_audit_candidate7_history.py" in text
    assert "FETCHNOW_AUDIT_PREDECESSOR_SHA" in text
    assert "38631542113" in text
    assert "38641657351" in text
    assert "image_audit_candidate7_smoke.py" in text
    assert "message.splitlines()[0]" in text
    assert "subject == expected" in text
    assert "Compatibility smoke" in text
    assert "scripts/image_audit_candidate7_smoke.py" in hist.AUDIT_ONLY_PATH_PREFIXES


def test_authorize_subject_ignores_trailers() -> None:
    message = EXPECTED_SUBJECT + "\n\nSigned-off-by: Example <ex@example.invalid>\n"
    subject = message.splitlines()[0]
    assert subject == EXPECTED_SUBJECT
    bare = EXPECTED_SUBJECT + "\n"
    assert bare.splitlines()[0] == EXPECTED_SUBJECT


def test_cli_json_out_successor(tmp_path: Path, monkeypatch: object) -> None:
    repo = _init_repo(tmp_path / "cli")
    baseline, predecessor, head = _build_three_phase(repo, monkeypatch)
    out = tmp_path / "proof.json"
    rc = hist.main(
        [
            "--repo",
            str(repo),
            "--expected-head-sha",
            head,
            "--native-baseline-sha",
            baseline,
            "--audit-predecessor-sha",
            predecessor,
            "--json-out",
            str(out),
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["approved_runtime_delta"] is True
    assert data["runtime_inputs_unchanged"] is False
