"""Exercise the actual workflow subject gate, without GitHub or audit calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github/workflows/sec08-media-executor.yml"
)
SUBJECT = "ci(security): fix SEC-08 subject gate [native-once-20261004c]"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (SUBJECT, True),
        (SUBJECT + "\n", True),
        (SUBJECT + "\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n", True),
        (SUBJECT + "\r\n\r\nBody", True),
        (SUBJECT + "-unexpected-suffix", False),
        ("unrelated\n\n" + SUBJECT, False),
        ("", False),
        (None, False),
    ],
)
def test_exact_subject_with_optional_trailers(
    tmp_path: Path, message: str | None, expected: bool
) -> None:
    workflow = WORKFLOW.read_text()
    assert f"EXPECTED_SUBJECT: '{SUBJECT}'" in workflow
    script = textwrap.dedent(
        workflow.split("          python3 - <<'PY'\n", 1)[1].split(
            "          PY\n", 1
        )[0]
    )
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"head_commit": {"message": message}}))
    output = tmp_path / "output"
    subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_OUTPUT": str(output),
            "EXPECTED_SUBJECT": SUBJECT,
        },
        check=True,
        timeout=5,
        capture_output=True,
    )
    assert output.read_text() == f"allowed={str(expected).lower()}\n"


def test_branch_attempt_and_native_dependency_remain_gated() -> None:
    workflow = WORKFLOW.read_text()
    assert "github.run_attempt == 1 &&" in workflow
    assert "github.ref == 'refs/heads/codex/security-sec-08-media-executor'" in workflow
    assert "needs: authorize" in workflow
    assert "if: needs.authorize.outputs.allowed == 'true'" in workflow
