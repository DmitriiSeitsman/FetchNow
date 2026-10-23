"""Offline regressions for candidate-7 compatibility smoke harness."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WORKFLOW = ROOT / ".github/workflows/sec03c-candidate7-image-audit.yml"
sys.path.insert(0, str(SCRIPTS))

import image_audit_candidate7_smoke as smoke  # noqa: E402


@pytest.mark.parametrize("dockerfile", ["web/Dockerfile", "deploy/nginx/Dockerfile"])
def test_alpine_expectations_match_pinned_audited_source(dockerfile: str) -> None:
    # Read committed product pins, not an old image or dirty Dockerfile.
    source = "9934ebe63943e7c1007564402e50258865a3a380"
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{source}:{dockerfile}"],
        check=True,
        capture_output=True,
        text=True,
    )
    pins = re.findall(r"'([a-z0-9-]+)=([^']+)'", proc.stdout)
    for package in ("pcre2", "tiff", "libexpat"):
        versions = [version for name, version in pins if name == package]
        assert len(versions) == 1, (dockerfile, package, versions)
        assert f"{package}-{versions[0]}" in smoke.EXPECTED_ALPINE_APK
    assert len(smoke.EXPECTED_ALPINE_APK) == 3


@pytest.mark.parametrize("label", ["web", "gateway"])
def test_apk_inventory_accepts_current_pin_and_rejects_stale_pin(label: str) -> None:
    current = "pcre2-10.49-r0\ntiff-4.7.2-r0\nlibexpat-2.9.0-r0\n"
    stage = f"{label}_apk_inventory"
    smoke.assert_apk_pins(current, stage=stage)
    stale = current.replace("libexpat-2.9.0-r0", "libexpat-2.8.5-r0")
    with pytest.raises(smoke.SmokeError, match="libexpat-2.9.0-r0"):
        smoke.assert_apk_pins(stale, stage=stage)
    for pin in smoke.EXPECTED_ALPINE_APK:
        with pytest.raises(smoke.SmokeError, match="missing"):
            smoke.assert_apk_pins(current.replace(pin, "wrong-version"), stage=stage)


def test_dpkg_query_argv_is_literal_and_shell_free() -> None:
    argv = smoke.dpkg_query_argv(
        "fetchnow-api:test",
        ("libpcre2-8-0", "libssl3t64", "openssl", "openssl-provider-legacy"),
    )
    assert argv[:5] == [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "dpkg-query",
    ]
    assert argv[5] == "fetchnow-api:test"
    assert argv[6] == "-W"
    assert argv[7] == f"-f={smoke.DPKG_FORMAT}"
    assert "${Package}" in argv[7]
    assert "${Version}" in argv[7]
    assert "/bin/sh" not in argv
    assert "-c" not in argv
    assert argv[8:] == [
        "libpcre2-8-0",
        "libssl3t64",
        "openssl",
        "openssl-provider-legacy",
    ]


def test_legacy_broken_dpkg_shell_pattern_is_rejected_by_inventory_guard(
    monkeypatch: object,
) -> None:
    """Negative: old nested-shell expansion must not be used by inventory."""
    broken = smoke.legacy_broken_dpkg_shell_argv("img")
    assert "/bin/sh" in broken
    assert any("${Package}" in a for a in broken)

    # Inventory stage refuses any argv that nests /bin/sh -c.
    calls: list[list[str]] = []

    def fake_run_cmd(argv, *, stage, timeout=120.0, check=True):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        raise AssertionError("run_cmd must not be reached with broken argv")

    monkeypatch.setattr(smoke, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        smoke,
        "dpkg_query_argv",
        lambda image, packages: broken,
    )
    ctx = smoke.SmokeContext(
        run_id="t",
        work_dir=Path("/tmp"),
        api_image="img",
        web_image="web",
        gateway_image="gw",
    )
    try:
        smoke.stage_inventory(ctx)
        raise AssertionError("expected SmokeError")
    except smoke.SmokeError as exc:
        assert exc.stage == "api_dpkg_inventory"
        assert "nested shell" in str(exc)


def test_assert_api_dpkg_rejects_blank_and_wrong_version() -> None:
    try:
        smoke.assert_api_dpkg(" \n \n")
        raise AssertionError("blank should fail")
    except smoke.SmokeError as exc:
        assert exc.stage == "api_dpkg_inventory"

    good_lines = "\n".join(
        f"{pkg} {ver}" for pkg, ver in smoke.EXPECTED_API_DPKG.items()
    )
    assert smoke.assert_api_dpkg(good_lines + "\n") == dict(smoke.EXPECTED_API_DPKG)

    bad = good_lines.replace("10.46-1~deb13u3", "10.46-1~deb13u2")
    try:
        smoke.assert_api_dpkg(bad)
        raise AssertionError("wrong version should fail")
    except smoke.SmokeError as exc:
        assert "libpcre2-8-0" in str(exc)


def test_assert_api_dpkg_rejects_duplicates() -> None:
    line = "libpcre2-8-0 10.46-1~deb13u3\nlibpcre2-8-0 10.46-1~deb13u3\n"
    try:
        smoke.parse_dpkg_lines(line)
        raise AssertionError("duplicate should fail")
    except smoke.SmokeError as exc:
        assert "duplicate" in str(exc)


def test_workflow_invokes_smoke_helper_not_legacy_dpkg_shell() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "image_audit_candidate7_smoke.py" in text
    assert "[native-once-20261015-img7smk]" in text
    # Legacy nested expansion must not remain in the smoke step.
    smoke_step = text.split("Compatibility smoke", 1)[1].split("Install Trivy", 1)[0]
    assert 'entrypoint /bin/sh "$api"' not in smoke_step
    assert '-f="${Package} ${Version}' not in smoke_step
    assert "image_audit_candidate7_smoke.py" in smoke_step


def test_apk_and_nginx_argv_shell_free() -> None:
    apk = smoke.apk_info_argv("web:img", ("pcre2", "tiff", "libexpat"))
    assert apk[4] == "apk"
    assert "-c" not in apk
    ngx = smoke.nginx_t_argv("gw:img")
    assert ngx[4] == "nginx"
    assert ngx[-1] == "-t"


def test_media_workspace_prepare_is_chown_only_not_media_root_smoke() -> None:
    prep = smoke.media_workspace_prepare_argv("api:img", "vol")
    assert prep[prep.index("--user") + 1] == "0:0"
    script = prep[prep.index("-c") + 1]
    assert "chown 10001:10001 /m" in script
    assert "chmod 0700 /m" in script
    assert "ffmpeg" not in script
    assert "ffprobe" not in script

    probe = smoke.media_write_probe_argv("api:img", "vol")
    assert probe[probe.index("--user") + 1] == "10001:10001"

    ffmpeg = smoke.media_ffmpeg_argv("api:img", "vol")
    assert ffmpeg[ffmpeg.index("--user") + 1] == "10001:10001"
    assert "media_smoke_ok" in smoke.media_ffmpeg_script()
    assert "pipefail" not in smoke.media_ffmpeg_script()
