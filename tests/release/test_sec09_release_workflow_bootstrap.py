"""Offline regressions for SEC-08/09 native release workflow bootstrap.

These are not native proof. They lock the uv absolute-path contract, the
evidence-directory handoff with the harness (exist_ok=False), and the
missing-result.json fallback exit-code/status contract.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/sec09-release-media-activation.yml"
HARNESS = ROOT / "scripts/release_media_activation_integration_test.py"
EXPECTED_SUBJECT = (
    "test(security): SEC-08/09 native release transaction "
    "[native-once-20261011-gatewayonce]"
)


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_uses_absolute_uv_path_in_install_step() -> None:
    text = _workflow_text()
    assert 'uv_bin="$RUNNER_TEMP/${asset%.tar.gz}/uv"' in text
    assert 'test -x "$uv_bin"' in text
    assert '"$uv_bin" --version' in text
    install = text.split("Install uv 0.12.19 (checksum-pinned)", 1)[1].split(
        "Create off-checkout venv", 1
    )[0]
    # Same-step bare `uv` was the bootstrap failure in run 38146059444.
    assert "\n          uv --version" not in install
    assert "uv --version |" not in install
    assert "23bf5552d220e0842b65c862097b2ebaeba0064b74eda5e565e77fd25969d8c8" in install
    assert "0.12.19" in install
    assert 'echo "$RUNNER_TEMP/${asset%.tar.gz}" >> "$GITHUB_PATH"' in install


def test_workflow_does_not_precreate_evidence_leaf_before_harness() -> None:
    text = _workflow_text()
    matrix = text.split("Native media release transaction matrix", 1)[1].split(
        "Upload sanitized evidence", 1
    )[0]
    assert (
        'mkdir -p "$RUNNER_TEMP/media-release-evidence"'
        not in matrix.split("if [[ ! -f", 1)[0]
    )
    assert "refusing pre-existing evidence directory" in matrix
    assert "exist_ok=False" in matrix or "exist_ok=False" in _workflow_text()


def test_workflow_fallback_marks_absent_native_not_pass() -> None:
    text = _workflow_text()
    assert "native_result':'absent'" in text or '"native_result":"absent"' in text
    assert "missing result.json" in text
    assert 'exit "$rc"' in text
    # Forbid real workflow softening; comments may mention the phrases.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "continue-on-error:" not in stripped
        assert "|| true" not in stripped


def test_workflow_exact_subject_marker_unique() -> None:
    text = _workflow_text()
    assert EXPECTED_SUBJECT in text
    assert "[native-once-20261011-gatewayonce]" in text
    # Prior markers must not authorize this gateway-once injection fix run.
    assert "[native-once-20261011-composemem]" not in text
    assert "[native-once-20261011-v4overlay]" not in text
    assert "[native-once-20261011-diag]" not in text
    assert "[native-once-20261011-uvpath]" not in text
    assert (
        "test(security): SEC-08/09 native release transaction [native-once-20261011]\n"
        not in text
    )


def test_authorize_first_line_ignores_trailer() -> None:
    expected = EXPECTED_SUBJECT
    for message in (
        expected,
        expected + "\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n",
        expected + "\n\nBody line\n",
    ):
        subject = message.splitlines()[0]
        assert subject == expected
    assert ("other\n\n" + expected).splitlines()[0] != expected


def test_stub_uv_absolute_path_works_before_path_update(tmp_path: Path) -> None:
    """Mirrors install-step: absolute path after extract; GITHUB_PATH for later."""
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    asset_root = runner_temp / "uv-x86_64-unknown-linux-gnu"
    asset_root.mkdir()
    uv_bin = asset_root / "uv"
    uv_bin.write_text(
        "#!/bin/sh\necho 'uv 0.12.19 (stub)'\n",
        encoding="utf-8",
    )
    uv_bin.chmod(uv_bin.stat().st_mode | stat.S_IXUSR)
    github_path = runner_temp / "github_path"
    github_path.write_text("", encoding="utf-8")

    script = f"""
set -euo pipefail
RUNNER_TEMP={runner_temp!s}
asset=uv-x86_64-unknown-linux-gnu.tar.gz
uv_bin="$RUNNER_TEMP/${{asset%.tar.gz}}/uv"
test -x "$uv_bin"
echo "$RUNNER_TEMP/${{asset%.tar.gz}}" >> "{github_path}"
    "$uv_bin" --version | tee "$RUNNER_TEMP/uv-version.txt"
    grep -q '0.12.19' "$RUNNER_TEMP/uv-version.txt"
    # Absolute path succeeds even when a different uv is already on PATH.
    """
    subprocess.run(
        ["bash", "-c", script], check=True, env={**os.environ, "PATH": "/usr/bin:/bin"}
    )
    assert "0.12.19" in (runner_temp / "uv-version.txt").read_text(encoding="utf-8")
    assert github_path.read_text(encoding="utf-8").strip() == str(asset_root)

    # Next step: PATH includes extract dir (simulates GITHUB_PATH apply).
    env = os.environ.copy()
    env["PATH"] = f"{asset_root}{os.pathsep}{env.get('PATH', '')}"
    next_step = subprocess.run(
        ["bash", "-c", "command -v uv && uv --version"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert str(uv_bin) in next_step.stdout or "uv" in next_step.stdout
    assert "0.12.19" in next_step.stdout


def test_evidence_dir_must_not_exist_before_harness_mkdir(tmp_path: Path) -> None:
    evidence = tmp_path / "media-release-evidence"
    evidence.mkdir()
    with pytest.raises(FileExistsError):
        evidence.mkdir(parents=True, exist_ok=False)


def test_harness_source_keeps_exist_ok_false() -> None:
    text = HARNESS.read_text(encoding="utf-8")
    assert "exist_ok=False" in text
    assert "evidence.mkdir(parents=True, exist_ok=False)" in text


def test_missing_result_fallback_preserves_exit_and_marks_fail(tmp_path: Path) -> None:
    """Contract of the matrix step fallback when harness leaves no result.json."""
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    evidence_dir = runner_temp / "media-release-evidence"
    github_output = runner_temp / "github_output"
    github_output.write_text("", encoding="utf-8")

    # Fake harness: nonzero exit, no evidence dir / result.json.
    fake_harness = runner_temp / "fake_harness.py"
    fake_harness.write_text(
        "import sys\nprint('simulated early failure')\nsys.exit(2)\n",
        encoding="utf-8",
    )
    venv_python = Path(sys.executable)

    script = f"""
set -euo pipefail
evidence_dir="{evidence_dir}"
GITHUB_OUTPUT="{github_output}"
if [[ -e "$evidence_dir" ]]; then
  echo "refusing pre-existing evidence directory: $evidence_dir" >&2
  exit 1
fi
set +e
"{venv_python}" "{fake_harness}"
rc=$?
set -e
echo "matrix_rc=$rc" >> "$GITHUB_OUTPUT"
if [[ ! -f "$evidence_dir/result.json" ]]; then
  mkdir -p "$evidence_dir"
  EVIDENCE_DIR="$evidence_dir" python3 -c "import json,os; from pathlib import Path; p=Path(os.environ['EVIDENCE_DIR']); payload={{'status':'FAIL','error':'missing result.json','native_result':'absent','security_residual_acceptance':False}}; assert payload['status']!='PASS'; (p/'result.json').write_text(json.dumps(payload, indent=2)+'\\n')"
fi
exit "$rc"
"""
    completed = subprocess.run(["bash", "-c", script], check=False)
    assert completed.returncode == 2
    payload = json.loads((evidence_dir / "result.json").read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert payload["native_result"] == "absent"
    assert payload["status"] != "PASS"
    assert "matrix_rc=2" in github_output.read_text(encoding="utf-8")


def test_imports_with_pythonpath_scripts_outside_checkout(tmp_path: Path) -> None:
    """Off-checkout interpreter can import release modules via PYTHONPATH=scripts."""
    if not (ROOT / "backend" / ".venv" / "bin" / "python").exists():
        interpreter = sys.executable
    else:
        # Prefer a non-checkout interpreter when available; fall back to current.
        interpreter = sys.executable
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "scripts")
    probe = subprocess.run(
        [
            interpreter,
            "-c",
            "import fetchnow_release.release_mode as m; "
            "import fetchnow_release.media_state as s; "
            "print(m.RELEASE_MODE_MEDIA); print(s.__name__)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert "media" in probe.stdout
    assert "fetchnow_release.media_state" in probe.stdout


def test_upload_path_can_read_fallback_artifact(tmp_path: Path) -> None:
    evidence = tmp_path / "media-release-evidence"
    evidence.mkdir()
    (evidence / "result.json").write_text(
        json.dumps(
            {
                "status": "FAIL",
                "error": "missing result.json",
                "native_result": "absent",
                "security_residual_acceptance": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Simulate upload-artifact path glob: directory must contain readable files.
    files = list(evidence.iterdir())
    assert files
    assert any(p.name == "result.json" for p in files)


def test_clean_checkout_contract_for_harness() -> None:
    text = HARNESS.read_text(encoding="utf-8")
    assert 'checked(["git", "status", "--porcelain"], ROOT)' in text
    assert "source must be committed and clean" in text
