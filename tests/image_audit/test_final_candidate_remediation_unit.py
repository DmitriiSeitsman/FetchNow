"""Local successor regressions; no Docker/scanner/host operations are real."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import image_audit_candidate7_smoke as smoke  # noqa: E402

BASELINE = "5dfd0524758c7ae55e04cc7045f709c6cb22c66f"


def _baseline_bytes(path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{BASELINE}:{path}"],
        check=True,
        capture_output=True,
    ).stdout


def test_lock_changes_certifi_only_and_preserves_project_contract() -> None:
    before = tomllib.loads(_baseline_bytes("backend/uv.lock").decode())
    after = tomllib.loads((ROOT / "backend/uv.lock").read_text())
    old = {p["name"]: p for p in before.pop("package")}
    new = {p["name"]: p for p in after.pop("package")}
    assert before == after
    assert before["requires-python"] == ">=3.12"
    assert old.keys() == new.keys()
    changed = {name for name in old if old[name] != new[name]}
    assert changed == {"certifi"}
    assert old["certifi"]["version"] == "2026.7.22"
    certifi = new["certifi"]
    assert certifi["version"] == "2026.10.19"
    assert certifi["source"] == {"registry": "https://pypi.org/simple"}
    assert certifi["sdist"]["hash"] == (
        "sha256:035c1e85572b141bf1c3a3eff0e33a8eb74917522f2559075ff8d2fea780a1ae"
    )
    assert [w["hash"] for w in certifi["wheels"]] == [
        "sha256:42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b"
    ]
    for path in (
        "backend/pyproject.toml",
        "backend/ci/pytest-requirements.txt",
        "web/package-lock.json",
    ):
        assert (ROOT / path).read_bytes() == _baseline_bytes(path)


@pytest.mark.parametrize("path", ["web/Dockerfile", "deploy/nginx/Dockerfile"])
def test_alpine_remediation_adds_only_two_pins_without_changing_base(path: str) -> None:
    before = _baseline_bytes(path).decode()
    after = (ROOT / path).read_text()
    assert re.findall(r"^FROM .+$", before, re.M) == re.findall(
        r"^FROM .+$", after, re.M
    )
    pins = dict(re.findall(r"'([a-z0-9-]+)=([^']+)'", before))
    assert dict(re.findall(r"'([a-z0-9-]+)=([^']+)'", after)) == {
        **pins,
        "brotli-libs": "1.2.1-r0",
        "libx11": "1.8.14-r0",
    }
    assert "apk upgrade" not in after
    assert "/edge/" not in after


def _ctx(tmp_path: Path) -> smoke.SmokeContext:
    return smoke.SmokeContext(
        run_id="local-regression",
        work_dir=tmp_path,
        api_image="api",
        web_image="web",
        gateway_image="gateway",
    )


def test_cleanup_removes_owned_anonymous_volumes_not_named_or_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    pg, probe = f"{ctx.prefix}-pg", f"{ctx.prefix}-gosu"
    ctx.created = [("container", pg), ("container", probe)]
    containers = {pg: "anon-pg", probe: "anon-probe"}
    volumes = {"anon-pg", "anon-probe", "named-pg-data", "foreign-anonymous"}
    calls: list[list[str]] = []

    def host(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["timeout"] == smoke.DEFAULT_CMD_TIMEOUT_S
        assert argv[:4] == ["docker", "rm", "-f", "-v"]
        volumes.remove(containers.pop(argv[4]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(smoke.subprocess, "run", host)
    smoke.cleanup(ctx)
    assert not ctx.cleanup_errors
    assert not containers
    assert volumes == {"named-pg-data", "foreign-anonymous"}
    assert calls == [["docker", "rm", "-f", "-v", n] for n in (probe, pg)]


@pytest.mark.parametrize("kind", ["container", "network", "volume"])
@pytest.mark.parametrize("inventory_ok", [True, False])
@pytest.mark.parametrize("present", [True, False])
def test_cleanup_requires_successful_exact_absence_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    inventory_ok: bool,
    present: bool,
) -> None:
    ctx = _ctx(tmp_path)
    name = f"{ctx.prefix}-resource"
    ctx.created = [(kind, name)]
    calls: list[list[str]] = []

    def host(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if len(calls) == 1:
            assert "rm" in argv
            return subprocess.CompletedProcess(argv, 1, "", "remove failed")
        assert "ls" in argv or "ps" in argv
        output = name + "\n" if present else name + "-foreign\n"
        return subprocess.CompletedProcess(argv, 0 if inventory_ok else 1, output, "")

    monkeypatch.setattr(smoke.subprocess, "run", host)
    smoke.cleanup(ctx)
    assert bool(ctx.cleanup_errors) == (present or not inventory_ok)
    assert len(calls) == 2


def test_cleanup_failure_keeps_errors_and_continues_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    ctx.created = [("volume", "owned-media"), ("container", "owned-pg")]
    calls: list[list[str]] = []

    def host(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:2] == ["docker", "rm"]:
            raise subprocess.TimeoutExpired(argv, smoke.DEFAULT_CMD_TIMEOUT_S)
        assert argv == ["docker", "volume", "rm", "owned-media"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(smoke.subprocess, "run", host)
    smoke.cleanup(ctx)
    assert len(ctx.cleanup_errors) == 1
    assert "owned-pg" in ctx.cleanup_errors[0]
    assert len(calls) == 2


def test_gosu_container_tracked_before_create_and_removed_with_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    name = f"{ctx.prefix}-gosu"

    def create(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert ("container", name) in ctx.created
        assert argv == ["docker", "create", "--name", name, ctx.postgres_image, "true"]
        raise smoke.SmokeError("gosu_create", "injected partial creation")

    monkeypatch.setattr(smoke, "run_cmd", create)
    with pytest.raises(smoke.SmokeError, match="partial creation"):
        smoke.stage_gosu(ctx)
    calls: list[list[str]] = []

    def remove(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(smoke.subprocess, "run", remove)
    smoke.cleanup(ctx)
    assert calls == [["docker", "rm", "-f", "-v", name]]
    assert not ctx.cleanup_errors
