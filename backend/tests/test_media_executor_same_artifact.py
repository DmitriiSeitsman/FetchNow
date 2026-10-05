"""Offline regressions for same-artifact orchestration. NOT native acceptance."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def load(name: str) -> ModuleType:
    path = Path(__file__).parent / "media_executor" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_identity_mismatch_before_native_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = load("same_artifact")
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gate.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        gate,
        "build_once",
        lambda _ref: {
            "image_id": "sha256:" + "a" * 64,
            "platform": "linux/amd64",
            "image_ref": "img:t",
        },
    )
    monkeypatch.setattr(gate, "dockerfile_sha256", lambda: "d" * 64)
    monkeypatch.setattr(
        gate.native,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "deadbeef\n", ""),
    )

    def boom(*_a: object, **_k: object) -> dict[str, Any]:
        raise OSError("image identity drift before native: x != y")

    monkeypatch.setattr(gate, "run_native_stage", boom)
    monkeypatch.setattr(gate, "remove_image", lambda _ref: [])
    out = tmp_path / "out"
    code = gate.execute(out, trivy_bin="/bin/false")
    result = json.loads((out / "result.json").read_text())
    assert code == 1
    assert result["status"] == "technical_failure"
    assert result["error_type"] == "OSError"
    assert "identity drift" in result["error"]


def test_audit_does_not_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = load("same_artifact")
    builds: list[str] = []
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gate.platform, "machine", lambda: "x86_64")
    image_id = "sha256:" + "b" * 64

    def build(ref: str) -> dict[str, str]:
        builds.append(ref)
        return {"image_id": image_id, "platform": "linux/amd64", "image_ref": ref}

    monkeypatch.setattr(gate, "build_once", build)
    monkeypatch.setattr(gate, "dockerfile_sha256", lambda: "d" * 64)
    monkeypatch.setattr(
        gate.native,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "cafebabe\n", ""),
    )
    monkeypatch.setattr(
        gate,
        "run_native_stage",
        lambda **k: {
            "exit_code": 0,
            "status": "PASS",
            "result": {"status": "PASS"},
            "identity": {
                "image_id": image_id,
                "platform": "linux/amd64",
                "image_ref": k["image_ref"],
            },
        },
    )
    monkeypatch.setattr(
        gate,
        "run_audit_stage",
        lambda **k: {
            "exit_code": 2,
            "status": "needs_owner_decision",
            "summary": {
                "status": "needs_owner_decision",
                "cve_2026_103111_present": False,
                "gate_fixable_unique_ids": [],
                "gate_unfixed_unique_ids": ["CVE-OLD"],
                "gate_fixable_instances": 0,
                "gate_unfixed_instances": 1,
                "db": {"Version": 2},
                "scanner": {"version": "0.74.0"},
            },
            "identity": {
                "image_id": image_id,
                "platform": "linux/amd64",
                "image_ref": k["image_ref"],
            },
            "summary_sha256": "e" * 64,
            "raw_report_sha256": "f" * 64,
        },
    )
    removed: list[str] = []
    monkeypatch.setattr(gate, "remove_image", lambda ref: removed.append(ref) or [])
    out = tmp_path / "out"
    code = gate.execute(out, trivy_bin="/bin/trivy")
    result = json.loads((out / "result.json").read_text())
    assert builds == [result["identity"]["image_ref"]]
    assert len(builds) == 1
    assert result["rebuild_between_stages"] is False
    assert result["native_image_id"] == image_id == result["audit_image_id"]
    assert result["status"] == "unfixed_residual"
    assert code == 2
    assert removed == [result["identity"]["image_ref"]]


def test_scanner_technical_failure_is_not_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = load("same_artifact")
    image_id = "sha256:" + "c" * 64
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gate.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        gate,
        "build_once",
        lambda ref: {
            "image_id": image_id,
            "platform": "linux/amd64",
            "image_ref": ref,
        },
    )
    monkeypatch.setattr(gate, "dockerfile_sha256", lambda: "d" * 64)
    monkeypatch.setattr(
        gate.native,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "cafebabe\n", ""),
    )
    monkeypatch.setattr(
        gate,
        "run_native_stage",
        lambda **k: {
            "exit_code": 0,
            "status": "PASS",
            "result": {"status": "PASS"},
            "identity": {
                "image_id": image_id,
                "platform": "linux/amd64",
                "image_ref": k["image_ref"],
            },
        },
    )
    monkeypatch.setattr(
        gate,
        "run_audit_stage",
        lambda **k: {
            "exit_code": 1,
            "status": "technical_failure",
            "summary": {
                "status": "technical_failure",
                "error_code": "scanner_bad_exit",
            },
            "identity": {
                "image_id": image_id,
                "platform": "linux/amd64",
                "image_ref": k["image_ref"],
            },
            "summary_sha256": "e" * 64,
            "raw_report_sha256": None,
        },
    )
    monkeypatch.setattr(gate, "remove_image", lambda _ref: [])
    out = tmp_path / "out"
    code = gate.execute(out, trivy_bin="/bin/trivy")
    result = json.loads((out / "result.json").read_text())
    assert code == 1
    assert result["status"] == "audit_technical_failure"
    assert result["native_verdict"] == "PASS"
    assert result["audit_verdict"] == "technical_failure"


def test_cleanup_runs_even_when_native_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = load("same_artifact")
    image_id = "sha256:" + "d" * 64
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gate.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        gate,
        "build_once",
        lambda ref: {
            "image_id": image_id,
            "platform": "linux/amd64",
            "image_ref": ref,
        },
    )
    monkeypatch.setattr(gate, "dockerfile_sha256", lambda: "d" * 64)
    monkeypatch.setattr(
        gate.native,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "cafebabe\n", ""),
    )
    monkeypatch.setattr(
        gate,
        "run_native_stage",
        lambda **k: {
            "exit_code": 1,
            "status": "FAIL",
            "result": {"status": "FAIL"},
            "identity": {
                "image_id": image_id,
                "platform": "linux/amd64",
                "image_ref": k["image_ref"],
            },
        },
    )
    monkeypatch.setattr(
        gate,
        "run_audit_stage",
        lambda **k: {
            "exit_code": 2,
            "status": "needs_owner_decision",
            "summary": {
                "status": "needs_owner_decision",
                "cve_2026_103111_present": False,
                "gate_fixable_unique_ids": [],
                "gate_unfixed_unique_ids": [],
                "gate_fixable_instances": 0,
                "gate_unfixed_instances": 0,
                "db": {},
                "scanner": {},
            },
            "identity": {
                "image_id": image_id,
                "platform": "linux/amd64",
                "image_ref": k["image_ref"],
            },
            "summary_sha256": "e" * 64,
            "raw_report_sha256": None,
        },
    )
    removed: list[str] = []
    monkeypatch.setattr(gate, "remove_image", lambda ref: removed.append(ref) or [])
    out = tmp_path / "out"
    code = gate.execute(out, trivy_bin="/bin/trivy")
    result = json.loads((out / "result.json").read_text())
    assert code == 1
    assert result["status"] == "native_fail"
    assert removed == [result["identity"]["image_ref"]]


def test_runtime_inventory_rejects_c_source_and_pycache() -> None:
    gate = load("native_acceptance")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], timeout: int = 60, *, check: bool = True):
        calls.append(argv)
        payload = json.dumps(
            {
                "bad": [
                    "/opt/fetchnow/src/fetchnow/media_executor/sec08-launch.c",
                    "/opt/fetchnow/src/fetchnow/media_executor/__pycache__",
                ],
                "libpcre2": "libpcre2-8-0\tamd64\t10.46-1~deb13u3\n",
                "c_files": [
                    "/opt/fetchnow/src/fetchnow/media_executor/sec08-launch.c"
                ],
                "pyc": [],
                "pycache": [
                    "/opt/fetchnow/src/fetchnow/media_executor/__pycache__"
                ],
            }
        )
        return subprocess.CompletedProcess(argv, 0, payload + "\n", "")

    gate.run = fake_run  # type: ignore[assignment]
    with pytest.raises(AssertionError):
        gate.assert_runtime_inventory("container")
    assert calls and calls[0][:3] == ["docker", "exec", "container"]


@pytest.mark.parametrize("package", ["libpcre2-8-0", "libpcre2-8-0:amd64"])
def test_runtime_inventory_accepts_clean_pinned_pcre2(package: str) -> None:
    gate = load("native_acceptance")

    def fake_run(argv: list[str], timeout: int = 60, *, check: bool = True):
        # Exercise the actual generated in-container script with a fake dpkg,
        # so the real command's format is checked, not just the parser fixture.
        observed: list[list[str]] = []

        def query(command: list[str], *, text: bool) -> str:
            assert text is True
            observed.append(command)
            return f"{package}\tamd64\t10.46-1~deb13u3\n"

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(subprocess, "check_output", query)
            exec(compile(argv[-1], "inventory-probe", "exec"), {})
        assert observed == [[
            "dpkg-query", "-W", "-f=${Package}\t${Architecture}\t${Version}\n",
            "libpcre2-8-0",
        ]]
        payload = json.dumps(
            {
                "bad": [],
                "libpcre2": f"{package}\tamd64\t10.46-1~deb13u3\n",
                "c_files": [],
                "pyc": [],
                "pycache": [],
            }
        )
        return subprocess.CompletedProcess(argv, 0, payload + "\n", "")

    gate.run = fake_run  # type: ignore[assignment]
    data = gate.assert_runtime_inventory("container")
    assert data["bad"] == []


@pytest.mark.parametrize(
    "raw",
    [
        "libpcre2-8-0:arm64\tarm64\t10.46-1~deb13u3\n",
        "libpcre2-8-0:arm64\tamd64\t10.46-1~deb13u3\n",
        "libpcre2-8-0:amd64\tarm64\t10.46-1~deb13u3\n",
        "libpcre2-8-0\tarm64\t10.46-1~deb13u3\n",
        "libpcre2-8-0\tamd64\t10.46-1~deb13u2\n",
        "other-package\tamd64\t10.46-1~deb13u3\n",
        "libpcre2-8-0\t10.46-1~deb13u3\n",
        "libpcre2-8-0\tamd64\t10.46-1~deb13u3\textra\n",
        "libpcre2-8-0\tamd64\t10.46-1~deb13u3\n" * 2,
        "",
        None,
    ],
)
def test_runtime_inventory_rejects_wrong_or_incomplete_pcre2(raw: object) -> None:
    gate = load("native_acceptance")
    with pytest.raises(AssertionError):
        gate.validate_pcre2_inventory(raw)


def test_export_includes_sanitized_native_and_audit(tmp_path: Path) -> None:
    source = tmp_path / "private"
    source.mkdir()
    (source / "native").mkdir()
    (source / "audit").mkdir()
    raw = b'{"status":"unfixed_residual","native_verdict":"PASS"}\n'
    (source / "result.json").write_bytes(raw)
    (source / "result.json").chmod(0o600)
    (source / "hashes.json").write_text(
        json.dumps({"result.json": hashlib.sha256(raw).hexdigest()})
    )
    (source / "native" / "result.json").write_text('{"status":"PASS"}\n')
    (source / "audit" / "summary.json").write_text(
        '{"status":"needs_owner_decision"}\n'
    )
    (source / "audit" / "work").mkdir()
    (source / "audit" / "work" / "media-executor.trivy.json").write_text(
        '{"Results":[]}\n'
    )
    target = tmp_path / "public"
    load("export_evidence").export(source, target)
    assert (target / "result.json").read_bytes() == raw
    assert (target / "native" / "result.json").is_file()
    assert (target / "audit" / "summary.json").is_file()
    assert not (target / "audit" / "work").exists()


def test_audit_adapter_rejects_identity_drift() -> None:
    adapter = load("executor_image_audit")
    report = {
        "SchemaVersion": 2,
        "Metadata": {
            "ImageID": "sha256:" + "1" * 64,
            "ImageConfig": {"os": "linux", "architecture": "amd64"},
            "OS": {"Family": "debian", "Name": "13.7"},
        },
        "Results": [{"Class": "os-pkgs", "Type": "debian", "Vulnerabilities": []}],
    }
    details, err = adapter.check_executor_completeness(
        report,
        expected_image_id="sha256:" + "2" * 64,
        expected_platform="linux/amd64",
    )
    assert err == "image_mismatch"
    assert details["report_image_id"] == "sha256:" + "1" * 64
