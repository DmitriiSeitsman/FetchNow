#!/usr/bin/env python3
"""Compatibility smoke for candidate-7 native image audit.

Host-side harness (CI ubuntu / local Docker). Prefers argv without nested shell.
Any container ``/bin/sh`` snippets avoid ``pipefail``. Failures preserve stage +
exit code; cleanup removes only resources this run created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXPECTED_API_DPKG: dict[str, str] = {
    "libpcre2-8-0": "10.46-1~deb13u3",
    "libssl3t64": "3.5.7-1~deb13u3",
    "openssl": "3.5.7-1~deb13u3",
    "openssl-provider-legacy": "3.5.7-1~deb13u3",
}

EXPECTED_ALPINE_APK: tuple[str, ...] = (
    "pcre2-10.49-r0",
    "tiff-4.7.2-r0",
    "libexpat-2.9.0-r0",
)

GOSU_SHA256 = "52c8749d0142edd234e9d6bd5237dff2d81e71f43537e2f4f66f75dd4b243dd0"
DPKG_FORMAT = "${Package} ${Version}\n"
TOOL_UID = 10001
TOOL_GID = 10001
STDOUT_CAP = 64_000
DEFAULT_HTTP_DEADLINE_S = 60.0
DEFAULT_CMD_TIMEOUT_S = 180.0
WORKER_START_MARKER = "Media job worker started"


class SmokeError(RuntimeError):
    def __init__(self, stage: str, message: str, *, exit_code: int = 1) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.exit_code = exit_code


@dataclass
class SmokeContext:
    run_id: str
    work_dir: Path
    api_image: str
    web_image: str
    gateway_image: str
    postgres_image: str = "postgres:16.15-alpine3.24"
    created: list[tuple[str, str]] = field(default_factory=list)
    partial: dict[str, Any] = field(default_factory=dict)
    cleanup_errors: list[str] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return f"cand7smk-{self.run_id}"


def dpkg_query_argv(image: str, packages: Sequence[str]) -> list[str]:
    """Literal argv for dpkg-query — no nested shell, no host expansion."""
    return [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "dpkg-query",
        image,
        "-W",
        f"-f={DPKG_FORMAT}",
        *packages,
    ]


def apk_info_argv(image: str, packages: Sequence[str]) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "apk",
        image,
        "info",
        "-e",
        "-v",
        *packages,
    ]


def nginx_t_argv(image: str) -> list[str]:
    return ["docker", "run", "--rm", "--entrypoint", "nginx", image, "-t"]


def legacy_broken_dpkg_shell_argv(image: str) -> list[str]:
    """Historical failing pattern (variable expansion inside nested sh -c)."""
    return [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        'dpkg-query -W -f="${Package} ${Version}\\n" libpcre2-8-0',
    ]


def _cap(text: str, limit: int = STDOUT_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} bytes]\n"


def run_cmd(
    argv: Sequence[str],
    *,
    stage: str,
    timeout: float = DEFAULT_CMD_TIMEOUT_S,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeError(
            stage,
            f"timeout after {timeout}s argv={list(argv)[:8]}…",
            exit_code=124,
        ) from exc
    if check and proc.returncode != 0:
        raise SmokeError(
            stage,
            (
                f"command failed rc={proc.returncode} "
                f"stdout={_cap(proc.stdout)!r} stderr={_cap(proc.stderr)!r}"
            ),
            exit_code=proc.returncode or 1,
        )
    return proc


def parse_dpkg_lines(text: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise SmokeError(
                "api_dpkg_inventory",
                f"malformed dpkg line {line!r} (blank/missing fields)",
            )
        pkg, ver = parts
        if pkg in rows:
            raise SmokeError(
                "api_dpkg_inventory",
                f"duplicate package row for {pkg!r}",
            )
        rows[pkg] = ver
    return rows


def assert_api_dpkg(text: str) -> dict[str, str]:
    rows = parse_dpkg_lines(text)
    for pkg, want in EXPECTED_API_DPKG.items():
        got = rows.get(pkg)
        if got != want:
            raise SmokeError(
                "api_dpkg_inventory",
                f"{pkg}: got {got!r} want {want!r}; raw={text!r}",
            )
    return rows


def assert_apk_pins(text: str, *, stage: str) -> None:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for want in EXPECTED_ALPINE_APK:
        if want not in lines:
            raise SmokeError(stage, f"missing {want!r} in {lines!r}")


def wait_http(
    url: str,
    *,
    stage: str,
    want: int = 200,
    deadline_s: float = DEFAULT_HTTP_DEADLINE_S,
) -> None:
    deadline = time.monotonic() + deadline_s
    last = ""
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", url],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        last = (proc.stdout or "").strip()
        if proc.returncode == 0 and last == str(want):
            return
        time.sleep(1)
    raise SmokeError(stage, f"HTTP {want} not observed for {url} last={last!r}")


def track(ctx: SmokeContext, kind: str, name: str) -> str:
    ctx.created.append((kind, name))
    return name


def cleanup(ctx: SmokeContext) -> None:
    """Remove resources created by this run (reverse order). Failures recorded."""
    for kind, name in reversed(ctx.created):
        if kind == "container":
            proc = subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                # Explicit missing-container handling (not a silent blanket || true).
                inspect = subprocess.run(
                    ["docker", "inspect", name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if inspect.returncode == 0:
                    ctx.cleanup_errors.append(
                        f"container {name}: rm failed rc={proc.returncode} "
                        f"{_cap(proc.stderr)}"
                    )
        elif kind == "network":
            proc = subprocess.run(
                ["docker", "network", "rm", name],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                inspect = subprocess.run(
                    ["docker", "network", "inspect", name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if inspect.returncode == 0:
                    ctx.cleanup_errors.append(
                        f"network {name}: rm failed rc={proc.returncode} "
                        f"{_cap(proc.stderr)}"
                    )
        elif kind == "volume":
            proc = subprocess.run(
                ["docker", "volume", "rm", name],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                inspect = subprocess.run(
                    ["docker", "volume", "inspect", name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if inspect.returncode == 0:
                    ctx.cleanup_errors.append(
                        f"volume {name}: rm failed rc={proc.returncode} "
                        f"{_cap(proc.stderr)}"
                    )


def stage_inventory(ctx: SmokeContext) -> None:
    pkgs = tuple(EXPECTED_API_DPKG.keys())
    argv = dpkg_query_argv(ctx.api_image, pkgs)
    # Fail-closed argv contract: entrypoint dpkg-query, no nested shell.
    if "/bin/sh" in argv or "sh" in argv:
        raise SmokeError("api_dpkg_inventory", "nested shell must not be used for dpkg")
    if "--entrypoint" not in argv or "dpkg-query" not in argv:
        raise SmokeError("api_dpkg_inventory", "dpkg-query entrypoint required")
    if argv[argv.index("--entrypoint") + 1] != "dpkg-query":
        raise SmokeError("api_dpkg_inventory", "dpkg-query entrypoint required")
    fmt_args = [a for a in argv if a.startswith("-f=")]
    if not fmt_args:
        raise SmokeError("api_dpkg_inventory", "missing -f= format argv")
    fmt_arg = fmt_args[0]
    if "${Package}" not in fmt_arg or "${Version}" not in fmt_arg:
        raise SmokeError(
            "api_dpkg_inventory", f"format argv lost literals: {fmt_arg!r}"
        )

    proc = run_cmd(argv, stage="api_dpkg_inventory")
    out_path = ctx.work_dir / "cand7-api-dpkg.txt"
    out_path.write_text(proc.stdout, encoding="utf-8")
    rows = assert_api_dpkg(proc.stdout)
    ctx.partial["api_dpkg"] = rows

    for label, image in (("web", ctx.web_image), ("gateway", ctx.gateway_image)):
        apk_argv = apk_info_argv(image, ("pcre2", "tiff", "libexpat"))
        apk_proc = run_cmd(apk_argv, stage=f"{label}_apk_inventory")
        apk_path = ctx.work_dir / f"cand7-{label}-apk.txt"
        apk_path.write_text(apk_proc.stdout, encoding="utf-8")
        assert_apk_pins(apk_proc.stdout, stage=f"{label}_apk_inventory")
        ctx.partial[f"{label}_apk"] = [
            ln.strip() for ln in apk_proc.stdout.splitlines() if ln.strip()
        ]


def stage_nginx_and_http(ctx: SmokeContext) -> None:
    run_cmd(nginx_t_argv(ctx.web_image), stage="web_nginx_t")
    run_cmd(nginx_t_argv(ctx.gateway_image), stage="gateway_nginx_t")

    net = track(ctx, "network", f"{ctx.prefix}-net")
    run_cmd(["docker", "network", "create", net], stage="network_create")

    # Production gateway resolves Compose DNS names `api` and `web`.
    web_name = track(ctx, "container", f"{ctx.prefix}-web")
    api_name = track(ctx, "container", f"{ctx.prefix}-api")
    gw_name = track(ctx, "container", f"{ctx.prefix}-gateway")
    pg_name = track(ctx, "container", f"{ctx.prefix}-pg")
    worker_name = track(ctx, "container", f"{ctx.prefix}-worker")

    web_host_port = _unused_host_port()
    run_cmd(
        [
            "docker",
            "run",
            "-d",
            "--name",
            web_name,
            "--network",
            net,
            "--network-alias",
            "web",
            "-p",
            f"{web_host_port}:8080",
            ctx.web_image,
        ],
        stage="web_start",
    )
    wait_http(f"http://127.0.0.1:{web_host_port}/", stage="web_http")
    ctx.partial["web_http"] = True

    # Postgres first — migration + api/worker need it.
    run_cmd(
        [
            "docker",
            "run",
            "-d",
            "--name",
            pg_name,
            "--network",
            net,
            "--network-alias",
            "postgres",
            "-e",
            "POSTGRES_PASSWORD=fetchnow",
            "-e",
            "POSTGRES_USER=fetchnow",
            "-e",
            "POSTGRES_DB=fetchnow",
            ctx.postgres_image,
        ],
        stage="postgres_start",
    )
    _wait_pg_ready(ctx, pg_name)

    db_url = "postgresql+asyncpg://fetchnow:fetchnow@postgres:5432/fetchnow"
    # alembic via argv entrypoint (no pipefail).
    run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            net,
            "-e",
            f"DATABASE_URL={db_url}",
            "-e",
            "APP_ENV=development",
            "--entrypoint",
            "alembic",
            ctx.api_image,
            "upgrade",
            "head",
        ],
        stage="api_migrate",
        timeout=180,
    )

    api_host_port = _unused_host_port()
    run_cmd(
        [
            "docker",
            "run",
            "-d",
            "--name",
            api_name,
            "--network",
            net,
            "--network-alias",
            "api",
            "-p",
            f"{api_host_port}:8000",
            "-e",
            f"DATABASE_URL={db_url}",
            "-e",
            "APP_ENV=development",
            ctx.api_image,
        ],
        stage="api_start",
    )
    wait_http(
        f"http://127.0.0.1:{api_host_port}/api/v1/health/live",
        stage="api_live",
    )
    wait_http(
        f"http://127.0.0.1:{api_host_port}/api/v1/health/ready",
        stage="api_ready",
    )
    ctx.partial["api_live_ready"] = True

    run_cmd(
        [
            "docker",
            "run",
            "-d",
            "--name",
            worker_name,
            "--network",
            net,
            "-e",
            f"DATABASE_URL={db_url}",
            "-e",
            "APP_ENV=development",
            "-e",
            "WORKER_POLL_INTERVAL_SECONDS=2",
            "--entrypoint",
            "fetchnow-worker",
            ctx.api_image,
        ],
        stage="worker_start",
    )
    _wait_worker_started(ctx, worker_name)
    ctx.partial["worker_started"] = True

    gw_host_port = _unused_host_port()
    run_cmd(
        [
            "docker",
            "run",
            "-d",
            "--name",
            gw_name,
            "--network",
            net,
            "-p",
            f"{gw_host_port}:8080",
            ctx.gateway_image,
        ],
        stage="gateway_start",
    )
    # Re-check nginx -t inside the running gateway container (same Image ID).
    run_cmd(
        ["docker", "exec", gw_name, "nginx", "-t"],
        stage="gateway_exec_nginx_t",
    )
    # With api+web aliases present, gateway should proxy health/live → 200.
    wait_http(
        f"http://127.0.0.1:{gw_host_port}/api/v1/health/live",
        stage="gateway_http_api_live",
    )
    wait_http(
        f"http://127.0.0.1:{gw_host_port}/",
        stage="gateway_http_web",
    )
    ctx.partial["gateway_http"] = True


def _unused_host_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_pg_ready(ctx: SmokeContext, pg_name: str) -> None:
    deadline = time.monotonic() + DEFAULT_HTTP_DEADLINE_S
    last = ""
    while time.monotonic() < deadline:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                pg_name,
                "pg_isready",
                "-U",
                "fetchnow",
                "-d",
                "fetchnow",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        last = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode == 0:
            return
        time.sleep(1)
    raise SmokeError("postgres_ready", f"pg_isready failed last={last!r}")


def _wait_worker_started(ctx: SmokeContext, worker_name: str) -> None:
    deadline = time.monotonic() + DEFAULT_HTTP_DEADLINE_S
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["docker", "logs", worker_name],
            capture_output=True,
            text=True,
            check=False,
        )
        blob = (proc.stdout or "") + (proc.stderr or "")
        if WORKER_START_MARKER in blob:
            # Process still running.
            inspect = run_cmd(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    worker_name,
                ],
                stage="worker_running",
            )
            if inspect.stdout.strip() != "true":
                raise SmokeError(
                    "worker_start",
                    f"marker seen but not running logs={_cap(blob)}",
                )
            return
        # Fail fast if container exited.
        state = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", worker_name],
            capture_output=True,
            text=True,
            check=False,
        )
        if state.stdout.strip() in {"exited", "dead"}:
            raise SmokeError(
                "worker_start",
                f"worker exited early logs={_cap(blob)}",
            )
        time.sleep(1)
    raise SmokeError("worker_start", f"marker {WORKER_START_MARKER!r} not observed")


def media_workspace_prepare_argv(image: str, volume: str) -> list[str]:
    """Root oneshot that only owns the disposable mount for uid 10001.

    This is workspace prep, not media smoke. The api image defaults to USER
    10001, so prepare must request ``--user 0:0`` solely to chown/chmod the
    empty volume. Fresh named volumes (and host bind dirs) are root-owned;
    production-shaped ffmpeg still runs as TOOL_UID after this step.
    """
    return [
        "docker",
        "run",
        "--rm",
        "--user",
        "0:0",
        "-v",
        f"{volume}:/m",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        (f"chown {TOOL_UID}:{TOOL_GID} /m && chmod 0700 /m && echo workspace_prepared"),
    ]


def media_write_probe_argv(image: str, volume: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{TOOL_UID}:{TOOL_GID}",
        "-v",
        f"{volume}:/m",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        "touch /m/.write_probe && rm /m/.write_probe && echo write_ok",
    ]


def media_ffmpeg_script() -> str:
    # Portable /bin/sh: no pipefail; check each required result explicitly.
    return (
        "set -eu\n"
        "ffmpeg -y -f lavfi -i color=c=black:s=160x120:d=0.2 "
        "-c:v libx264 -pix_fmt yuv420p /m/src.mp4\n"
        "test -s /m/src.mp4\n"
        "ffmpeg -y -i /m/src.mp4 -c copy /m/out.mp4\n"
        "test -s /m/out.mp4\n"
        "ffmpeg -y -f lavfi -i color=c=black:s=160x120:d=0.2 "
        "-c:v libvpx-vp9 /m/src.webm\n"
        "test -s /m/src.webm\n"
        "ffmpeg -y -i /m/src.webm -c copy /m/out.webm\n"
        "test -s /m/out.webm\n"
        "mp4=$(ffprobe -v error -show_entries stream=codec_name "
        "-of csv=p=0 /m/out.mp4)\n"
        "webm=$(ffprobe -v error -show_entries stream=codec_name "
        "-of csv=p=0 /m/out.webm)\n"
        'echo "mp4=$mp4"\n'
        'echo "webm=$webm"\n'
        'case "$mp4" in *h264*) ;; *) echo "mp4_codec_fail:$mp4"; exit 1 ;; esac\n'
        'case "$webm" in *vp9*) ;; *) echo "webm_codec_fail:$webm"; exit 1 ;; esac\n'
        "echo media_smoke_ok\n"
    )


def media_ffmpeg_argv(image: str, volume: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{TOOL_UID}:{TOOL_GID}",
        "-v",
        f"{volume}:/m",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        media_ffmpeg_script(),
    ]


def stage_media(ctx: SmokeContext) -> None:
    # Task-owned disposable Docker volume (unique per run). Host bind dirs do
    # not guarantee uid 10001 write; prepare ownership, then prove it.
    vol = track(ctx, "volume", f"{ctx.prefix}-media")
    run_cmd(["docker", "volume", "create", vol], stage="media_volume_create")

    prep = media_workspace_prepare_argv(ctx.api_image, vol)
    try:
        prep_user = prep[prep.index("--user") + 1]
    except (ValueError, IndexError) as exc:
        raise SmokeError(
            "media_workspace_prepare",
            "prepare must request --user 0:0 for chown-only",
        ) from exc
    if prep_user != "0:0":
        raise SmokeError(
            "media_workspace_prepare",
            f"prepare user must be 0:0 for chown-only, got {prep_user!r}",
        )
    if "ffmpeg" in " ".join(prep):
        raise SmokeError(
            "media_workspace_prepare",
            "prepare must not run media tools",
        )
    prep_proc = run_cmd(prep, stage="media_workspace_prepare")
    if "workspace_prepared" not in prep_proc.stdout:
        raise SmokeError(
            "media_workspace_prepare",
            f"prepare marker missing stdout={_cap(prep_proc.stdout)!r}",
        )
    ctx.partial["media_workspace_prepare"] = True

    # Prove write as production-shaped uid before media work (not root smoke).
    probe = media_write_probe_argv(ctx.api_image, vol)
    if f"{TOOL_UID}:{TOOL_GID}" not in probe:
        raise SmokeError("media_write_probe", "probe must run as TOOL_UID")
    probe_proc = run_cmd(probe, stage="media_write_probe")
    if "write_ok" not in probe_proc.stdout:
        raise SmokeError(
            "media_write_probe",
            f"write_ok missing stdout={_cap(probe_proc.stdout)!r}",
        )
    ctx.partial["media_write_probe"] = True

    ffmpeg_argv = media_ffmpeg_argv(ctx.api_image, vol)
    user_idx = ffmpeg_argv.index("--user")
    if ffmpeg_argv[user_idx + 1] != f"{TOOL_UID}:{TOOL_GID}":
        raise SmokeError(
            "media_ffmpeg",
            "media smoke must run as production-shaped uid, not root",
        )
    proc = run_cmd(ffmpeg_argv, stage="media_ffmpeg", timeout=180)
    if "media_smoke_ok" not in proc.stdout:
        raise SmokeError(
            "media_ffmpeg",
            f"success marker missing stdout={_cap(proc.stdout)} "
            f"stderr={_cap(proc.stderr)}",
        )
    # Confirm outputs still present via a follow-up inspect as TOOL_UID.
    inspect = run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            f"{TOOL_UID}:{TOOL_GID}",
            "-v",
            f"{vol}:/m",
            "--entrypoint",
            "/bin/sh",
            ctx.api_image,
            "-c",
            (
                "set -eu\n"
                "test -s /m/out.mp4\n"
                "test -s /m/out.webm\n"
                "ffprobe -v error -show_entries format=format_name "
                "-of default=nw=1:nk=1 /m/out.mp4\n"
                "ffprobe -v error -show_entries format=format_name "
                "-of default=nw=1:nk=1 /m/out.webm\n"
                "echo media_outputs_ok\n"
            ),
        ],
        stage="media_outputs_inspect",
    )
    if "media_outputs_ok" not in inspect.stdout:
        raise SmokeError(
            "media_outputs_inspect",
            f"outputs inspect failed stdout={_cap(inspect.stdout)!r} "
            f"stderr={_cap(inspect.stderr)!r}",
        )
    ctx.partial["media_ffmpeg"] = True
    ctx.partial["media_outputs_inspect"] = True


def stage_gosu(ctx: SmokeContext) -> None:
    cid_proc = run_cmd(
        ["docker", "create", ctx.postgres_image, "true"],
        stage="gosu_create",
    )
    cid = cid_proc.stdout.strip()
    track(ctx, "container", cid)
    gosu_path = ctx.work_dir / "cand7-gosu"
    run_cmd(
        ["docker", "cp", f"{cid}:/usr/local/bin/gosu", str(gosu_path)],
        stage="gosu_cp",
    )
    digest = hashlib.sha256(gosu_path.read_bytes()).hexdigest()
    if digest != GOSU_SHA256:
        raise SmokeError(
            "gosu_sha256",
            f"got {digest} want {GOSU_SHA256}",
        )
    proof = ctx.work_dir / "cand7-gosu-proof.txt"
    proof.write_text("gosu_sha256_ok\n", encoding="utf-8")
    ctx.partial["gosu_sha256"] = digest


def run_smoke(ctx: SmokeContext) -> dict[str, Any]:
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    primary_error: str | None = None
    primary_stage: str | None = None
    primary_rc = 0
    try:
        stage_inventory(ctx)
        stage_media(ctx)
        stage_nginx_and_http(ctx)
        stage_gosu(ctx)
        ctx.partial["ok"] = True
    except SmokeError as exc:
        primary_error = str(exc)
        primary_stage = exc.stage
        primary_rc = exc.exit_code
        ctx.partial["ok"] = False
        ctx.partial["failed_stage"] = exc.stage
        ctx.partial["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — surface unexpected as smoke failure
        primary_error = f"unexpected:{exc}"
        primary_stage = "unexpected"
        primary_rc = 1
        ctx.partial["ok"] = False
        ctx.partial["failed_stage"] = "unexpected"
        ctx.partial["error"] = primary_error
    finally:
        cleanup(ctx)
        if ctx.cleanup_errors:
            ctx.partial["cleanup_errors"] = list(ctx.cleanup_errors)

    report = {
        "ok": bool(ctx.partial.get("ok")),
        "run_id": ctx.run_id,
        "api_image": ctx.api_image,
        "web_image": ctx.web_image,
        "gateway_image": ctx.gateway_image,
        "partial": ctx.partial,
        "primary_stage": primary_stage,
        "primary_error": primary_error,
        "cleanup_errors": list(ctx.cleanup_errors),
    }
    (ctx.work_dir / "cand7-smoke-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not report["ok"]:
        raise SystemExit(primary_rc or 1)
    if ctx.cleanup_errors:
        # Cleanup failure after successful smoke still fails the job.
        raise SystemExit(1)
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Candidate-7 compatibility smoke")
    p.add_argument("--run-id", required=True)
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--api-image", required=True)
    p.add_argument("--web-image", required=True)
    p.add_argument("--gateway-image", required=True)
    p.add_argument("--postgres-image", default="postgres:16.15-alpine3.24")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = SmokeContext(
        run_id=args.run_id,
        work_dir=args.work_dir,
        api_image=args.api_image,
        web_image=args.web_image,
        gateway_image=args.gateway_image,
        postgres_image=args.postgres_image,
    )
    try:
        report = run_smoke(ctx)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else 1
        print(json.dumps({"ok": False, "exit_code": code}), flush=True)
        return code
    print(json.dumps({"ok": True, "partial": report.get("partial")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
