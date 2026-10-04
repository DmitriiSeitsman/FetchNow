"""Offline gate regressions. These are NOT native executor acceptance."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def load(name: str) -> ModuleType:
    path = Path(__file__).parent / "media_executor" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("code", [1, 84, 90, 92, 127])
def test_launcher_or_command_failure_never_proves_isolation(code: int) -> None:
    with pytest.raises(AssertionError):
        load("native_inside").validate_isolation(
            {"ok": False, "exit_code": code, "stdout_b64": ""}
        )


@pytest.mark.parametrize("err", [0, 2, 110])
def test_wrong_denial_errno_is_not_accepted(err: int) -> None:
    data = {
        "uid": [10003] * 3,
        "gid": [10003] * 3,
        "groups": [],
        "env": {},
        "denied": dict.fromkeys(
            ("sibling", "published", "symlink", "proc", "cgroup", "socket"), err
        ),
    }
    with pytest.raises(AssertionError):
        load("native_inside").validate_isolation(
            {
                "ok": True,
                "exit_code": 0,
                "stdout_b64": base64.b64encode(json.dumps(data).encode()).decode(),
            }
        )


def test_cleanup_rejects_unscoped_identity() -> None:
    with pytest.raises(ValueError):
        load("native_acceptance").cleanup({"name": "fetchnow-production"})


def test_timeout_still_records_failure_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = load("native_acceptance")
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gate.platform, "machine", lambda: "x86_64")

    def fail(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("docker info", 60)

    monkeypatch.setattr(gate, "run", fail)
    monkeypatch.setattr(
        gate, "cleanup", lambda *_a, **_k: ["container-media"]
    )
    out = tmp_path / "out"
    assert gate.execute(out) == 1
    result = json.loads((out / "result.json").read_text())
    assert result["status"] == "FAIL" and result["error_type"] == "TimeoutExpired"
    assert result["cleanup_errors"] == ["container-media"]
    assert (
        json.loads((out / "hashes.json").read_text())["result.json"]
        == hashlib.sha256((out / "result.json").read_bytes()).hexdigest()
    )


def test_export_is_separate_readable_and_hash_checked(tmp_path: Path) -> None:
    source = tmp_path / "private"
    source.mkdir()
    raw = b'{"status":"FAIL"}\n'
    (source / "result.json").write_bytes(raw)
    (source / "result.json").chmod(0o600)
    (source / "hashes.json").write_text(
        json.dumps({"result.json": hashlib.sha256(raw).hexdigest()})
    )
    target = tmp_path / "public"
    load("export_evidence").export(source, target)
    assert (target / "result.json").read_bytes() == raw
    assert (target / "result.json").stat().st_mode & 0o777 == 0o644
    assert (source / "result.json").stat().st_mode & 0o777 == 0o600
    (source / "result.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        load("export_evidence").export(source, tmp_path / "bad")
