"""Offline regressions for candidate-7 baseline history proof."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WORKFLOW = ROOT / ".github/workflows/sec03c-candidate7-image-audit.yml"
sys.path.insert(0, str(SCRIPTS))

import image_audit_candidate7_history as hist  # noqa: E402


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
        "compose.yaml",
    ):
        _write(repo, rel, f"fixture:{rel}\n")


def test_shallow_clone_missing_baseline_fails(tmp_path: Path) -> None:
    origin = _init_repo(tmp_path / "origin")
    _seed_runtime_tree(origin)
    baseline = _commit(origin, "baseline native")
    _write(origin, "docs/note.txt", "mid\n")
    _commit(origin, "mid")
    _write(
        origin,
        "scripts/image_audit_candidate7.py",
        "print('audit')\n",
    )
    tip = _commit(origin, "audit tip")

    shallow = tmp_path / "shallow"
    # file:// + --depth forces a true shallow clone (path clones can copy all objects).
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
    # Shallow tip cannot resolve the older baseline object.
    evidence = hist.prove_baseline_history(
        shallow,
        baseline_sha=baseline,
        expected_head_sha=_git(shallow, "rev-parse", "HEAD"),
    )
    assert evidence["ok"] is False
    assert evidence["error_code"] == "baseline_missing"
    assert tip  # tip exists in origin; shallow HEAD may equal tip SHA


def test_full_history_allows_baseline_and_audit_only_diff(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "full")
    _seed_runtime_tree(repo)
    baseline = _commit(repo, "baseline native")
    _write(
        repo,
        "scripts/image_audit_candidate7.py",
        "print('harness')\n",
    )
    _write(
        repo,
        "tests/image_audit/test_x.py",
        "def test_x():\n    assert True\n",
    )
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
    # Return to the first branch tip (parent of 'other').
    _git(repo, "checkout", f"{other}^")
    _git(repo, "checkout", "-B", "mainline")
    _write(repo, "scripts/image_audit_candidate7.py", "print(1)\n")
    tip = _commit(repo, "tip on baseline line")
    assert primary  # keep fixture deterministic
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


def test_workflow_has_fetch_depth_zero_and_new_marker() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "fetch-depth: 0" in text
    assert "persist-credentials: false" in text
    assert "[native-once-20261014-img7d0]" in text
    assert "[native-once-20261014-img7]" not in text.split("EXPECTED_SUBJECT", 1)[1]
    assert "image_audit_candidate7_history.py" in text
    assert "38631542113" in text
    # Exact-subject authorization: first line only (trailers ignored by splitlines[0]).
    assert "message.splitlines()[0]" in text
    assert "subject == expected" in text


def test_authorize_subject_ignores_trailers() -> None:
    # Mirrors workflow authorize logic without GitHub event files.
    message = (
        "test(security): SEC-03C/08/09 candidate7 image audit "
        "[native-once-20261014-img7d0]\n\n"
        "Signed-off-by: Example <ex@example.invalid>\n"
    )
    expected = (
        "test(security): SEC-03C/08/09 candidate7 image audit "
        "[native-once-20261014-img7d0]"
    )
    subject = message.splitlines()[0]
    assert subject == expected
    bare = expected + "\n"
    assert bare.splitlines()[0] == expected


def test_cli_json_out(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "cli")
    _seed_runtime_tree(repo)
    baseline = _commit(repo, "baseline")
    _write(repo, "scripts/image_audit_candidate7.py", "print('x')\n")
    head = _commit(repo, "audit")
    out = tmp_path / "proof.json"
    rc = hist.main(
        [
            "--repo",
            str(repo),
            "--baseline-sha",
            baseline,
            "--expected-head-sha",
            head,
            "--json-out",
            str(out),
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is True
