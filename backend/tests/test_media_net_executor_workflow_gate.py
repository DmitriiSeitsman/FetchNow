"""Workflow gate shape for SEC-09. Does not invoke GitHub Actions."""

from __future__ import annotations

from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github/workflows/sec09-media-net-executor.yml"
)


def test_sec09_workflow_is_manual_and_attempt_gated() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "github.run_attempt == 1" in text
    assert "codex/security-sec-09-network-isolation" in text
    assert "native-once-" in text
    assert "network_acceptance.py" in text
    # Must not run on ordinary PR/main pushes.
    assert "pull_request:" not in text
    assert "branches: [main]" not in text


def test_sec09_workflow_has_no_trivy_job_steps() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    # Comments may mention a future same-artifact Trivy follow-up; jobs must not.
    assert "uses: aquasecurity/" not in text
    assert "trivy image" not in text.lower()
    assert "jobs:\n  native:" in text
    assert "jobs:\n  audit:" not in text
