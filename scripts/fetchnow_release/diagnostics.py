"""Sanitized failure diagnostics for release integration assertions."""

from __future__ import annotations

import json
from pathlib import Path

from .redact import redact


def _event_phase_from_filename(name: str) -> str | None:
    if not name.endswith(".json") or "_" not in name:
        return None
    return name.split("_", 1)[1][: -len(".json")]


def format_rollout_failure_diagnostics(
    *,
    status: str | None = None,
    messages: tuple[str, ...] | list[str] = (),
    deployment_id: str | None = None,
    deploy_root: Path | None = None,
) -> str:
    """Render assertion-failure context without secrets or env values.

    Includes status, sanitized rollout messages, deployment ID, and journal
    phase/result names when the deployment directory is available. Never dumps
    container environment, credentials, or URL query/fragment strings.
    """
    lines = [
        f"status={status or 'none'}",
        f"deployment_id={deployment_id or 'none'}",
    ]
    if deploy_root is not None and deployment_id:
        dep_dir = deploy_root / "deployments" / deployment_id
        events_dir = dep_dir / "events"
        if events_dir.is_dir():
            phases = [
                phase
                for path in sorted(events_dir.glob("*.json"))
                if (phase := _event_phase_from_filename(path.name)) is not None
            ]
            if phases:
                lines.append("journal_phases=" + ",".join(phases))
        result_path = dep_dir / "result.json"
        if result_path.is_file():
            try:
                raw = json.loads(result_path.read_text(encoding="utf-8"))
                result_status = raw.get("status") if isinstance(raw, dict) else None
                lines.append(f"journal_result={result_status or 'none'}")
            except (OSError, json.JSONDecodeError, TypeError):
                lines.append("journal_result=unreadable")
    lines.append("messages:")
    if not messages:
        lines.append("  (none)")
    else:
        for msg in messages:
            try:
                lines.append("  - " + redact(str(msg)))
            except Exception:  # noqa: BLE001
                lines.append("  - <redacted>")
    return "\n".join(lines)
