"""Media sidecar health: protocol socket + deny-aware proxy checks."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from .bounded_subprocess import (
    BoundedCancelledError,
    BoundedTimeoutError,
    OutputLimitExceededError,
    run_docker_probe,
)
from .redact import redact

PROTOCOL_VERSION = 1


class MediaHealthError(RuntimeError):
    """Media health gate failure."""


@dataclass(frozen=True)
class MediaHealthResult:
    ok: bool
    messages: tuple[str, ...]


def _compose_base(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
) -> list[str]:
    argv = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--project-name",
        project_name,
    ]
    for path in compose_files:
        argv.extend(["-f", str(path)])
    return argv


def assert_executor_socket_ready(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    service: str,
    socket_path: str,
    label: str,
) -> None:
    """Prove the executor speaks the bounded protocol via compose exec.

    Named volumes are not host paths, so readiness is checked inside the
    service container. A bare TCP/port open is not accepted as proof.
    """
    if not socket_path.startswith("/"):
        raise MediaHealthError(f"{label} socket must be absolute: {socket_path!r}")
    job_id = str(uuid.uuid4())
    script = (
        "import json,os,socket,stat,sys,uuid\n"
        f"path={socket_path!r}\n"
        "st=os.lstat(path)\n"
        "assert stat.S_ISSOCK(st.st_mode), 'not a socket'\n"
        f"job={job_id!r}\n"
        "def send(op):\n"
        "  payload=json.dumps({'v':1,'op':op,'job_id':job,'attempt':1,'fence':1},"
        "separators=(',',':'))+'\\n'\n"
        "  s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "  s.settimeout(5.0)\n"
        "  s.connect(path)\n"
        "  s.sendall(payload.encode())\n"
        "  data=b''\n"
        "  while b'\\n' not in data:\n"
        "    chunk=s.recv(4096)\n"
        "    if not chunk: raise SystemExit(f'{op}: incomplete')\n"
        "    data+=chunk\n"
        "    if len(data)>65536: raise SystemExit(f'{op}: oversized')\n"
        "  s.close()\n"
        "  if not data: raise SystemExit(f'{op}: empty')\n"
        "  body=json.loads(data.split(b'\\n',1)[0].decode())\n"
        "  codes={'reserve':'reserved','cancel':'not_running','release':'released'}\n"
        "  if (body.get('v')!=1 or body.get('ok') is not True "
        "or body.get('code')!=codes[op]):\n"
        "    raise SystemExit(f'{op}: rejected or unexpected response')\n"
        "send('reserve')\n"
        "send('cancel')\n"
        "send('release')\n"
        "print('ok')\n"
    )
    argv = _compose_base(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
    )
    argv.extend(
        ["exec", "-T", "--user", "10001:10001", service, "python", "-c", script]
    )
    try:
        result = run_docker_probe(argv, cwd=cwd)
    except (BoundedTimeoutError, BoundedCancelledError, OutputLimitExceededError) as exc:
        raise MediaHealthError(f"{label} protocol probe failed: {exc}") from exc
    if result.returncode != 0 or result.stdout_text.strip() != "ok":
        raise MediaHealthError(
            f"{label} protocol readiness failed: "
            + redact(result.stderr_text.strip() or result.stdout_text.strip())
        )


def assert_proxy_denies_target(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    denied_host: str,
    proxy_service: str = "egress-proxy",
    proxy_port: int = 8888,
) -> None:
    """Fail closed unless CONNECT to a required-denied host returns 403."""
    if not denied_host.strip():
        raise MediaHealthError("denied_host required for proxy policy proof")
    script = (
        "import socket,sys\n"
        f"host={denied_host!r}; port=443; proxy_port={proxy_port}\n"
        "s=socket.create_connection(('127.0.0.1', proxy_port), timeout=5)\n"
        "req=f'CONNECT {host}:{port} HTTP/1.1\\r\\nHost: {host}:{port}\\r\\n\\r\\n'\n"
        "s.sendall(req.encode())\n"
        "data=s.recv(1024).decode('latin1', errors='replace')\n"
        "s.close()\n"
        "sys.stdout.write(data.split('\\r\\n',1)[0])\n"
        "sys.exit(0 if data.startswith(('HTTP/1.1 403','HTTP/1.0 403')) else 4)\n"
    )
    argv = _compose_base(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
    )
    argv.extend(["exec", "-T", proxy_service, "python", "-c", script])
    try:
        result = run_docker_probe(argv, cwd=cwd)
    except (BoundedTimeoutError, BoundedCancelledError, OutputLimitExceededError) as exc:
        raise MediaHealthError(f"proxy deny probe failed: {exc}") from exc
    if result.returncode != 0:
        raise MediaHealthError(
            "proxy deny policy not proven (expected HTTP 403 to denied host): "
            + redact(result.stderr_text.strip() or result.stdout_text.strip())
        )


def assert_worker_executor_flags(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    expect_enabled: bool,
) -> None:
    """Inspect worker container env for expected executor flag values."""
    argv = _compose_base(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
    )
    argv.extend(
        [
            "exec",
            "-T",
            "worker",
            "python",
            "-c",
            (
                "import os,json; print(json.dumps({"
                "'MEDIA_EXECUTOR_ENABLED': "
                "os.environ.get('MEDIA_EXECUTOR_ENABLED','false'),"
                "'MEDIA_NET_EXECUTOR_ENABLED': "
                "os.environ.get('MEDIA_NET_EXECUTOR_ENABLED','false')"
                "}))"
            ),
        ]
    )
    try:
        result = run_docker_probe(argv, cwd=cwd)
    except (BoundedTimeoutError, BoundedCancelledError, OutputLimitExceededError) as exc:
        raise MediaHealthError(f"worker flag inspect failed: {exc}") from exc
    if result.returncode != 0:
        raise MediaHealthError(
            "worker flag inspect failed: "
            + redact(result.stderr_text.strip() or result.stdout_text.strip())
        )
    try:
        payload = json.loads(result.stdout_text.strip())
    except json.JSONDecodeError as exc:
        raise MediaHealthError("worker flag inspect returned invalid JSON") from exc
    want = "true" if expect_enabled else "false"
    for key in ("MEDIA_EXECUTOR_ENABLED", "MEDIA_NET_EXECUTOR_ENABLED"):
        got = str(payload.get(key, "false")).strip().lower()
        if got != want:
            raise MediaHealthError(f"worker {key}={got!r} expected {want!r}")


def assert_media_service_identities(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    expected_image_ids: dict[str, str],
) -> None:
    """Require running media sidecars match manifest image IDs."""
    for service, image_id in expected_image_ids.items():
        argv = _compose_base(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
        )
        argv.extend(
            [
                "ps",
                "-a",
                "--format",
                "{{.Service}} {{.ID}}",
            ]
        )
        try:
            result = run_docker_probe(argv, cwd=cwd)
        except (BoundedTimeoutError, BoundedCancelledError, OutputLimitExceededError) as exc:
            raise MediaHealthError(f"compose ps failed: {exc}") from exc
        if result.returncode != 0:
            raise MediaHealthError("compose ps failed for media identity check")
        container_id = None
        for line in result.stdout_text.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == service:
                container_id = parts[1]
                break
        if not container_id:
            raise MediaHealthError(f"missing media service container: {service}")
        insp = run_docker_probe(
            ["docker", "inspect", container_id, "--format", "{{.Image}}"],
            cwd=Path("/"),
        )
        if insp.returncode != 0:
            raise MediaHealthError(f"inspect failed for {service}")
        got = insp.stdout_text.strip()
        if got != image_id:
            raise MediaHealthError(
                f"{service} image identity mismatch: {got!r} != {image_id!r}"
            )


def run_media_health(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    executor_socket: str,
    net_socket: str,
    expect_flags_enabled: bool,
    denied_host: str,
    media_image_ids: dict[str, str],
    check_worker_flags: bool = True,
) -> MediaHealthResult:
    messages: list[str] = []
    try:
        assert_media_service_identities(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
            cwd=cwd,
            expected_image_ids=media_image_ids,
        )
        messages.append("OK: media service identities")
        assert_executor_socket_ready(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
            cwd=cwd,
            service="media-executor",
            socket_path=executor_socket,
            label="media-executor",
        )
        messages.append("OK: media-executor protocol ready")
        assert_executor_socket_ready(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
            cwd=cwd,
            service="media-net-executor",
            socket_path=net_socket,
            label="media-net-executor",
        )
        messages.append("OK: media-net-executor protocol ready")
        assert_proxy_denies_target(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
            cwd=cwd,
            denied_host=denied_host,
        )
        messages.append("OK: egress-proxy deny policy proven")
        if check_worker_flags:
            assert_worker_executor_flags(
                project_name=project_name,
                env_file=env_file,
                compose_files=compose_files,
                cwd=cwd,
                expect_enabled=expect_flags_enabled,
            )
            messages.append(
                "OK: worker executor flags "
                + ("enabled" if expect_flags_enabled else "disabled")
            )
        return MediaHealthResult(ok=True, messages=tuple(messages))
    except MediaHealthError as exc:
        return MediaHealthResult(ok=False, messages=(f"FAIL: {redact(str(exc))}",))
