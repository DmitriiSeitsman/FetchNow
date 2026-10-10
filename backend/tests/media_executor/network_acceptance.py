"""Native Linux AMD64 Compose acceptance for SEC-09 (N1–N13).

Prepared for an authorized remote run. Local Mac/QEMU is NOT native evidence.
Does not publish images. Uses disposable project names only.
One build per image; native checks and optional Trivy share the same Image IDs.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PATTERN = re.compile(r"fetchnow-sec09-net-[0-9a-f]{8}\Z")
PROJECT_PREFIX = "fetchnow-sec09-"
SCRIPTS = ROOT / "scripts"
UNIT_ROOT = Path("/run/systemd/system")
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROOF_MEMORY_MAX = 2 * 1024 * 1024 * 1024
_ACTIVE_OVERLAY: str | None = None
_EVIDENCE_OUTPUT: Path | None = None
_EVIDENCE_LOCK = threading.Lock()


def run(
    argv: list[str],
    timeout: int = 120,
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        proc = subprocess.run(
            argv,
            cwd=ROOT,
            stdout=output,
            stderr=errors,
            timeout=timeout,
            check=False,
            env=env,
        )
        output.seek(0)
        errors.seek(0)
        stdout, stderr = output.read(2_000_001), errors.read(2_000_001)
    if max(len(stdout), len(stderr)) > 2_000_000:
        raise OSError("command output limit")
    result = subprocess.CompletedProcess(
        argv, proc.returncode, stdout.decode(), stderr.decode()
    )
    if check and result.returncode:
        print(result.stderr[-4000:], flush=True)
        raise OSError(f"{argv[0]} failed: exit {result.returncode}")
    return result


def save(path: Path, payload: object) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temp.chmod(0o644)
    temp.replace(path)


def _diagnostic_text(text: str) -> str:
    """Bounded synthetic-fixture stderr only; never publish URLs or credentials."""
    text = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"']+", "[url]", text)
    text = re.sub(
        r"(?i)\b(password|secret|token|api[_-]?key|authorization)\b"
        r"\s*[:=]\s*[^\s,;]+",
        "[credential]",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+\S+", "[credential]", text)
    text = re.sub(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]+", "[credential]", text)
    return text[-4000:]


def _response_diagnostic(payload: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in ("ok", "code", "exit_code", "timed_out", "cancelled", "artifact_bytes"):
        value = payload.get(key)
        if value is None or isinstance(value, bool | int):
            result[key] = value
        elif isinstance(value, str):
            result[key] = _diagnostic_text(value)[:120]
    for stream in ("stdout", "stderr"):
        value = payload.get(f"{stream}_b64")
        if not isinstance(value, str):
            continue
        try:
            data = base64.b64decode(value, validate=True)
        except ValueError:
            result[f"{stream}_error"] = "invalid_base64"
            continue
        result[f"{stream}_bytes"] = len(data)
        result[f"{stream}_sha256"] = hashlib.sha256(data).hexdigest()
        if stream == "stderr":
            result["stderr_excerpt"] = _diagnostic_text(data.decode("utf-8", "replace"))
    return result


def _record_rpc(request: dict[str, object], response: dict[str, object]) -> None:
    if _EVIDENCE_OUTPUT is None:
        return
    identity = {
        key: request[key]
        for key in ("op", "job_id", "attempt", "fence")
        if key in request
    }
    event = {"request": identity, "response": _response_diagnostic(response)}
    with _EVIDENCE_LOCK:
        path = _EVIDENCE_OUTPUT / "rpc-diagnostics.jsonl"
        with path.open("a") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        path.chmod(0o644)


def inspect_image_id(image: str) -> str:
    image_id = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image]
    ).stdout.strip()
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise OSError("image id invalid")
    return image_id


def compose_argv(project: str, *extra: str, overlay: str | None = None) -> list[str]:
    if overlay is None:
        overlay = _ACTIVE_OVERLAY
    files = [
        "compose.yaml",
        "compose.media-executor.yaml",
        "compose.media-net-executor.yaml",
    ]
    if overlay:
        files.append(overlay)
    argv = ["docker", "compose", "-p", project]
    for path in files:
        argv.extend(["-f", path])
    argv.extend(
        [
            "--profile",
            "media-executor",
            "--profile",
            "media-net-executor",
            *extra,
        ]
    )
    return argv


def cleanup(state: dict[str, object]) -> list[str]:
    name = state.get("name")
    if not isinstance(name, str) or not PATTERN.fullmatch(name):
        raise ValueError("unsafe cleanup identity")
    project = f"{PROJECT_PREFIX}{name[-8:]}"
    failures: list[str] = []
    overlay = state.get("overlay")
    overlay_path = overlay if isinstance(overlay, str) else None
    down = run(
        compose_argv(project, "down", "-v", "--remove-orphans", overlay=overlay_path),
        timeout=180,
        check=False,
    )
    if down.returncode != 0:
        failures.append(f"compose-down:{down.returncode}")
    for key in ("net_image", "proxy_image", "offline_image"):
        ref = state.get(key)
        if isinstance(ref, str):
            listed = run(["docker", "image", "ls", "-q", ref], check=False)
            if listed.returncode != 0:
                failures.append(f"image-ls:{key}")
                continue
            if listed.stdout.strip():
                removed = run(["docker", "image", "rm", "-f", ref], check=False)
                if removed.returncode != 0:
                    failures.append(f"image-rm:{key}:{removed.returncode}")
    unit_name = state.get("slice")
    if unit_name is not None:
        if unit_name != f"{name}.slice":
            raise ValueError("unsafe slice cleanup identity")
        unit = UNIT_ROOT / str(unit_name)
        if unit.exists():
            stopped = run(["systemctl", "stop", str(unit_name)], check=False)
            if stopped.returncode:
                failures.append(f"slice-stop:{stopped.returncode}")
            else:
                unit.unlink()
                reload = run(["systemctl", "daemon-reload"], check=False)
                if reload.returncode:
                    failures.append(f"slice-reload:{reload.returncode}")
    return failures


def _prepare_slice(state: dict[str, object], output: Path) -> dict[str, object]:
    """Create only this proof's transient ancestor; never use production's unit."""
    name = state.get("name")
    if not isinstance(name, str) or not PATTERN.fullmatch(name):
        raise ValueError("unsafe slice identity")
    unit_name = f"{name}.slice"
    unit = UNIT_ROOT / unit_name
    # Exclusive creation prevents cleanup from adopting a pre-existing unit.
    with unit.open("x") as handle:
        state["slice"] = unit_name
        save(output / "state.json", state)
        handle.write(f"[Slice]\nMemoryMax={PROOF_MEMORY_MAX}\nTasksMax=256\n")
    unit.chmod(0o644)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "start", unit_name])
    relative = run(
        ["systemctl", "show", "--property=ControlGroup", "--value", unit_name]
    ).stdout.strip()
    if not relative.startswith("/") or ".." in Path(relative).parts or relative == "/":
        raise OSError("unsafe slice ControlGroup")
    control = CGROUP_ROOT / relative.lstrip("/")
    if (control / "memory.max").read_text().strip() != str(PROOF_MEMORY_MAX):
        raise OSError("slice memory ceiling mismatch")
    return {
        "unit": unit_name,
        "control_group": relative,
        "memory_max": PROOF_MEMORY_MAX,
    }


def _wait_socket(project: str, service: str, path: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    probe = json.dumps(_request(str(uuid.uuid4()), 1, "cancel")) + "\n"
    script = (
        "import json,socket\n"
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.settimeout(1)\n"
        f"s.connect({path!r}); s.sendall({probe!r}.encode())\n"
        "data=b''\n"
        "while b'\\n' not in data and len(data)<4096:\n"
        " chunk=s.recv(4096)\n"
        " if not chunk: break\n"
        " data+=chunk\n"
        "s.close(); response=json.loads(data)\n"
        "assert response.get('ok') is True and response.get('code')=='not_running'\n"
    )
    while time.monotonic() < deadline:
        proc = run(
            compose_argv(
                project,
                "exec",
                "-T",
                "--user",
                "10001:10001",
                service,
                "python",
                "-c",
                script,
            ),
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            return
        time.sleep(1)
    raise OSError(f"socket not ready: {service}:{path}")


def _rpc(
    project: str, service: str, socket_path: str, payload: dict[str, object]
) -> dict[str, object]:  # noqa: E501
    request = payload
    body = json.dumps(payload, separators=(",", ":")) + "\n"
    script = (
        "import json,socket,sys\n"
        f"path={socket_path!r}\n"
        f"raw={body!r}.encode()\n"
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
        "s.settimeout(30)\n"
        "s.connect(path)\n"
        "s.sendall(raw)\n"
        "data=b''\n"
        "while b'\\n' not in data:\n"
        " chunk=s.recv(65536)\n"
        " if not chunk: break\n"
        " data+=chunk\n"
        "s.close()\n"
        "sys.stdout.write(data.decode())\n"
    )
    # Run as worker uid so SO_PEERCRED accepts the client.
    proc = run(
        compose_argv(
            project,
            "exec",
            "-T",
            "--user",
            "10001:10001",
            service,
            "python",
            "-c",
            script,
        ),
        timeout=90,
        check=False,
    )
    if proc.returncode != 0:
        raise OSError(f"rpc failed: {proc.stderr[-500:]}")
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    if not isinstance(payload, dict):
        raise OSError("rpc response is not an object")
    _record_rpc(request, payload)
    return payload


def _build_images(state: dict[str, object]) -> dict[str, str]:
    run(
        [
            "docker",
            "build",
            "-f",
            "backend/Dockerfile.media-net-executor",
            "-t",
            str(state["net_image"]),
            "backend",
        ],
        timeout=900,
    )
    run(
        [
            "docker",
            "build",
            "-f",
            "backend/Dockerfile.media-egress-proxy",
            "-t",
            str(state["proxy_image"]),
            "backend",
        ],
        timeout=600,
    )
    run(
        [
            "docker",
            "build",
            "-f",
            "backend/Dockerfile.media-executor",
            "-t",
            str(state["offline_image"]),
            "backend",
        ],
        timeout=900,
    )
    return {
        "network_executor": inspect_image_id(str(state["net_image"])),
        "egress_proxy": inspect_image_id(str(state["proxy_image"])),
        "offline_executor": inspect_image_id(str(state["offline_image"])),
    }


def _import_smoke(image: str, module: str, forbidden: list[str]) -> None:
    names = ", ".join(repr(name) for name in forbidden)
    script = (
        "import importlib.util as u\n"
        f"import {module}\n"
        "def missing(name):\n"
        "    try:\n"
        "        return u.find_spec(name) is None\n"
        "    except ModuleNotFoundError:\n"
        "        return True\n"
        f"assert all(missing(n) for n in [{names}])\n"
        "print('ok')\n"
    )
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            image,
            "-c",
            script,
        ],
        timeout=60,
    )


def _status(value: object) -> str:
    if value is True:
        return "PASS"
    if value is False:
        return "FAIL"
    if value == "NOT_RUN":
        return "NOT_RUN"
    return "FAIL"


def aggregate_verdict(
    checks: dict[str, object],
    *,
    audits: dict[str, object] | None = None,
    trivy_required: bool,
) -> tuple[str, str | None]:
    """Return (PASS|FAIL|NOT_RUN, error). Required NOT_RUN blocks PASS."""
    required = [
        "N1_inspect_ok",
        "N2_download_ok",
        "N3_direct_egress_denied",
        "N4_private_connect_denied",
        "N5_metadata_connect_denied",
        "N6_ipv6_mapped_connect_denied",
        "N7_rebinding_connect_denied",
        "N8_proxy_down_fail_closed",
        "N9_limits_enforced",
        "N10_cancel_neighbor_ok",
        "N11_fence_lease_ok",
        "N12_offline_unreachable",
        "N13_same_artifact_audit",
    ]
    statuses = {key: _status(checks.get(key)) for key in required}
    if audits is not None:
        checks = {**checks, "audits": audits}
    if any(statuses[key] == "NOT_RUN" for key in required):
        return "NOT_RUN", "required_not_run"
    if any(statuses[key] != "PASS" for key in required):
        failed = [key for key, st in statuses.items() if st != "PASS"]
        return "FAIL", ",".join(failed[:8])
    if trivy_required is False and statuses["N13_same_artifact_audit"] == "PASS":
        # Missing trivy must not be recorded as PASS.
        return "FAIL", "trivy_missing_marked_pass"
    return "PASS", None


def _write_disposable_overlay(path: Path, *, helpers: Path, tag: str) -> None:
    """Disposable-only overlay: loopback public canary, mock DNS, mock yt-dlp."""
    text = f"""
networks:
  mock-net:
    driver: bridge
    ipam:
      config:
        - subnet: 172.31.200.0/24

services:
  mock-dns:
    image: fetchnow-media-egress-proxy:{tag}
    user: "0:0"
    entrypoint: ["/usr/local/bin/python", "/helpers/mock_dns.py"]
    volumes:
      - {helpers}:/helpers:ro
    networks:
      mock-net:
        ipv4_address: 172.31.200.53
    environment:
      MOCK_ORIGIN_IP: "1.2.3.4"
    cap_drop: [ALL]
    read_only: true

  egress-proxy:
    image: fetchnow-media-egress-proxy:{tag}
    user: "0:0"
    dns:
      - 172.31.200.53
    networks:
      mock-net: {{}}
      media-net:
        aliases: [egress-proxy]
      fetchnow: {{}}
    volumes:
      - {helpers}:/helpers:ro
    entrypoint: ["/bin/sh", "-c"]
    command:
      - |
        set -e
        python /helpers/mock_origin.py &
        exec python -m fetchnow.media_egress_proxy

  media-net-executor:
    image: fetchnow-media-net-executor:{tag}
    cgroup_parent: fetchnow-sec09-net-{tag}.slice
    volumes:
      - media-net-ipc:/run/fetchnow-media-net
      - media-net-work:/var/lib/fetchnow/media-net
      - {helpers}/mock_ytdlp.py:/opt/venv/bin/yt-dlp:ro
    environment:
      MEDIA_EXECUTOR_PROFILE: network
      MEDIA_EXECUTOR_SOCKET: /run/fetchnow-media-net/ctrl/worker.sock
      MEDIA_EXECUTOR_WORK_ROOT: /var/lib/fetchnow/media-net
      MEDIA_EXECUTOR_LAUNCHER: /usr/local/bin/sec08-launch
      MEDIA_EXECUTOR_RO_ROOTS: /opt/venv
      MEDIA_NET_YTDLP_PATH: /opt/venv/bin/yt-dlp
      MEDIA_NET_PROXY_URL: http://egress-proxy:8888
      MEDIA_NET_DOWNLOAD_TIMEOUT_SECONDS: "20"
      MOCK_ORIGIN_HOST: "1.2.3.4"
      MOCK_ORIGIN_PORT: "443"

  media-executor:
    image: fetchnow-media-executor:{tag}
    cgroup_parent: fetchnow-sec09-net-{tag}.slice
"""
    path.write_text(text)


def _connect_probe(project: str, host: str, port: int = 443) -> str:
    """Send real CONNECT via egress-proxy from media-net-executor. Return status line."""  # noqa: E501
    script = (
        "import socket\n"
        f"s=socket.create_connection(('egress-proxy',8888),timeout=5)\n"
        f"s.sendall(b'CONNECT {host}:{port} HTTP/1.1\\r\\nHost: {host}:{port}\\r\\n\\r\\n')\n"  # noqa: E501
        "data=b''\n"
        "while b'\\r\\n\\r\\n' not in data and len(data)<4096:\n"
        "  chunk=s.recv(256)\n"
        "  if not chunk: break\n"
        "  data+=chunk\n"
        "print(data.split(b'\\r\\n',1)[0].decode('ascii','replace'))\n"
        "s.close()\n"
    )
    result = run(
        compose_argv(
            project, "exec", "-T", "media-net-executor", "python", "-c", script
        ),
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return "PROBE_FAIL"
    return result.stdout.strip().splitlines()[-1]


NET_SOCKET = "/run/fetchnow-media-net/ctrl/worker.sock"


def _request(job: str, fence: int, op: str, **fields: object) -> dict[str, object]:
    return {"v": 1, "op": op, "job_id": job, "attempt": 1, "fence": fence, **fields}


def _download_request(job: str, fence: int, mode: str, cap: int) -> dict[str, object]:
    return _request(
        job,
        fence,
        "download_progressive",
        url=f"https://mock.invalid/{mode}",
        provider_id="vk",
        format_token="fmt_1",
        max_bytes=cap,
        min_free_bytes=1_000_000,
    )


def _net_rpc(project: str, payload: dict[str, object]) -> dict[str, object]:
    return _rpc(project, "media-net-executor", NET_SOCKET, payload)


def _fixture_snapshot(
    project: str,
    job: str,
    fence: int,
    *,
    expected: dict[str, object] | None = None,
) -> dict[str, object]:
    """Observe both exact process identities from outside their Landlock domain."""
    uuid.UUID(job)
    if type(fence) is not int or fence < 1:
        raise ValueError("invalid fixture fence")
    path = f"/var/lib/fetchnow/media-net/{job}_1_{fence}"
    script = (
        "import json,os\nfrom pathlib import Path\n"
        f"root=Path({path!r})\n"
        f"expected=json.loads({json.dumps(expected or {})!r})\n"
        "result={}\n"
        "for role in ('parent','child'):\n"
        " marker=root/('fixture-'+role+'.json')\n"
        " try:\n"
        "  identity=json.loads(marker.read_text())\n"
        " except (FileNotFoundError,json.JSONDecodeError):\n"
        "  result[role]={'ready':False}; continue\n"
        " pid=identity['pid']; baseline=expected.get(role,{})\n"
        " start=baseline.get('starttime')\n"
        " try:\n"
        "  parts=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()\n"
        "  observed=int(parts[19]); state=parts[0]\n"
        "  same=(observed==start and pid==baseline.get('pid'))"
        " if start is not None else True\n"
        "  if start is None: start=observed\n"
        " except FileNotFoundError:\n"
        "  state='absent'; same=start is not None and pid==baseline.get('pid')\n"
        " hb=root/('fixture-'+role+'.hb')\n"
        " result[role]={**identity,'starttime':start,'ready':True,"
        "'state':state,'same_identity':same,"
        "'heartbeat':hb.stat().st_size if hb.exists() else 0}\n"
        "artifact=root/'output-artifact.bin'\n"
        "result['artifact_bytes']=artifact.stat().st_size if artifact.exists() else 0\n"
        "print(json.dumps(result))\n"
    )
    proc = run(
        compose_argv(
            project,
            "exec",
            "-T",
            "--user",
            "10001:10001",
            "media-net-executor",
            "python",
            "-c",
            script,
        )
    )
    value = json.loads(proc.stdout)
    if not isinstance(value, dict):
        raise OSError("fixture snapshot malformed")
    return value


def _tree_state(snapshot: dict[str, object], *, gone: bool) -> bool:
    for role in ("parent", "child"):
        row = snapshot.get(role)
        if not isinstance(row, dict) or row.get("ready") is not True:
            return False
        if row.get("same_identity") is not True:
            return False
        state = row.get("state")
        if gone:
            if state != "absent":  # Zombies are not reaped.
                return False
        elif state not in {"S", "R", "D"}:
            return False
    return True


def _wait_fixture_started(project: str, job: str, fence: int) -> dict[str, object]:
    deadline = time.monotonic() + 15
    snapshot: dict[str, object] = {}
    while time.monotonic() < deadline:
        snapshot = _fixture_snapshot(project, job, fence)
        if _tree_state(snapshot, gone=False):
            return snapshot
        time.sleep(0.05)
    if _EVIDENCE_OUTPUT is not None:
        save(
            _EVIDENCE_OUTPUT / f"fixture-start-{job}_{fence}.json",
            {"job_id": job, "fence": fence, "last_snapshot": snapshot},
        )
    raise OSError("fixture tree did not start")


def _limit_proven(response: dict[str, object], snapshot: dict[str, object]) -> bool:
    size = snapshot.get("artifact_bytes")
    return (
        response.get("ok") is False
        and response.get("code") == "failed"
        and response.get("exit_code") == 1
        and response.get("timed_out") is False
        and response.get("cancelled") is False
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size > 65_536
        and _tree_state(snapshot, gone=True)
    )


def _cancel_proven(
    cancel: dict[str, object],
    outcome: dict[str, object],
    after: dict[str, object],
    before_b: dict[str, object],
    after_b: dict[str, object],
) -> bool:
    if not (
        cancel.get("code") == "cancelling"
        and cancel.get("cancelled") is True
        and outcome.get("cancelled") is True
        and outcome.get("timed_out") is False
        and _tree_state(after, gone=True)
        and _tree_state(after_b, gone=False)
    ):
        return False
    for role in ("parent", "child"):
        before, now = before_b.get(role), after_b.get(role)
        if not isinstance(before, dict) or not isinstance(now, dict):
            return False
        if (before.get("pid"), before.get("starttime")) != (
            now.get("pid"),
            now.get("starttime"),
        ):
            return False
        current_hb, previous_hb = now.get("heartbeat"), before.get("heartbeat")
        if not isinstance(current_hb, int) or not isinstance(previous_hb, int):
            return False
        if current_hb <= previous_hb:
            return False
    return True


def _other_limit_proven(
    mode: str,
    response: dict[str, object],
    snapshot: dict[str, object],
) -> bool:
    if not (
        response.get("ok") is False
        and response.get("cancelled") is False
        and _tree_state(snapshot, gone=True)
    ):
        return False
    if mode == "blocking":
        return response.get("code") == "timed_out" and response.get("timed_out") is True
    if mode != "stdout":
        return False
    try:
        stdout = base64.b64decode(str(response.get("stdout_b64", "")), validate=True)
        stderr = base64.b64decode(str(response.get("stderr_b64", "")), validate=True)
    except ValueError:
        return False
    return (
        response.get("code") == "failed"
        and response.get("exit_code") == 1
        and response.get("timed_out") is False
        and len(stdout) == 196_608
        and len(stderr) <= 65_536
    )


def _worker_fence_proof(project: str, output: Path) -> dict[str, object]:
    """Actual worker watchdog with a real native executor and simulated lease loss."""
    proof_path = output / "worker-fence.json"
    env = os.environ.copy()
    env["SEC09_NATIVE_PROJECT"] = project
    env["SEC09_NATIVE_OVERLAY"] = str(_ACTIVE_OVERLAY or "")
    env["SEC09_NATIVE_PROOF"] = str(proof_path)
    env["UV_PYTHON_DOWNLOADS"] = "never"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Never upload a tool venv along with sanitized native evidence.
    with tempfile.TemporaryDirectory(prefix="fetchnow-sec09-worker-proof-") as temp:
        env["UV_PROJECT_ENVIRONMENT"] = str(Path(temp) / "venv")
        proc = run(
            [
                "uv",
                "run",
                "--directory",
                str(ROOT / "backend"),
                "--frozen",
                "--extra",
                "dev",
                "--python",
                "3.12",
                "python",
                "tests/media_executor/worker_fence_acceptance.py",
            ],
            timeout=180,
            check=False,
            env=env,
        )
    if proc.returncode != 0 or not proof_path.is_file():
        return {"pass": False, "exit_code": proc.returncode}
    proof = json.loads(proof_path.read_text())
    if not isinstance(proof, dict):
        return {"pass": False, "error": "invalid_worker_proof"}
    return proof


def _signal_fixture(project: str, job: str, fence: int, signal: str) -> None:
    uuid.UUID(job)
    if type(fence) is not int or fence < 1:
        raise ValueError("invalid fixture fence")
    if signal not in {"grow", "finish"}:
        raise ValueError("invalid fixture signal")
    marker = f"/var/lib/fetchnow/media-net/{job}_1_{fence}/fixture-{signal}"
    run(
        compose_argv(
            project,
            "exec",
            "-T",
            "--user",
            "10001:10001",
            "media-net-executor",
            "python",
            "-c",
            f"from pathlib import Path; Path({marker!r}).touch()",
        )
    )


def _configure_canary(project: str) -> None:
    """Host tooling enters only the disposable proxy's network namespace."""
    proxy_cid = run(compose_argv(project, "ps", "-q", "egress-proxy")).stdout.strip()
    proxy_pid = run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Pid}}",
            proxy_cid,
        ]
    ).stdout.strip()
    if not proxy_pid.isdecimal() or int(proxy_pid) <= 1:
        raise OSError("invalid disposable proxy namespace pid")
    run(
        [
            "nsenter",
            "--target",
            proxy_pid,
            "--net",
            "ip",
            "addr",
            "add",
            "1.2.3.4/32",
            "dev",
            "lo",
        ]
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _connect_probe(project, "1.2.3.4").startswith("HTTP/1.1 200"):
            return
        time.sleep(0.1)
    raise OSError("proxy canary did not become ready")


def _matrix(
    *,
    project: str,
    state: dict[str, object],
    identities: dict[str, str],
    checks: dict[str, object] | None = None,
) -> dict[str, object]:
    if checks is None:
        checks = {}

    def mark(key: str, ok: bool) -> None:
        checks[key] = bool(ok)
        if _EVIDENCE_OUTPUT is not None:
            save(_EVIDENCE_OUTPUT / "matrix-progress.json", checks)
        print(f"sec09: {key} = {'PASS' if ok else 'FAIL'}", flush=True)

    job = str(uuid.uuid4())
    _wait_socket(
        project, "media-net-executor", "/run/fetchnow-media-net/ctrl/worker.sock"
    )
    reserved = _rpc(
        project,
        "media-net-executor",
        "/run/fetchnow-media-net/ctrl/worker.sock",
        {"v": 1, "op": "reserve", "job_id": job, "attempt": 1, "fence": 4},
    )
    inspect = _rpc(
        project,
        "media-net-executor",
        "/run/fetchnow-media-net/ctrl/worker.sock",
        {
            "v": 1,
            "op": "inspect_metadata",
            "job_id": job,
            "attempt": 1,
            "fence": 4,
            "url": "https://mock.invalid/video",
            "provider_id": "vk",
        },
    )
    mark(
        "N1_inspect_ok",
        reserved.get("code") == "reserved"
        and inspect.get("ok") is True
        and inspect.get("code") == "ok",
    )
    _rpc(
        project,
        "media-net-executor",
        "/run/fetchnow-media-net/ctrl/worker.sock",
        {"v": 1, "op": "release", "job_id": job, "attempt": 1, "fence": 4},
    )

    job_dl = str(uuid.uuid4())
    _rpc(
        project,
        "media-net-executor",
        "/run/fetchnow-media-net/ctrl/worker.sock",
        {"v": 1, "op": "reserve", "job_id": job_dl, "attempt": 1, "fence": 4},
    )
    download = _rpc(
        project,
        "media-net-executor",
        "/run/fetchnow-media-net/ctrl/worker.sock",
        {
            "v": 1,
            "op": "download_progressive",
            "job_id": job_dl,
            "attempt": 1,
            "fence": 4,
            "url": "https://mock.invalid/video",
            "provider_id": "vk",
            "format_token": "fmt_1",
            "max_bytes": 1_000_000,
            "min_free_bytes": 1_000_000,
        },
    )
    artifact_size = download.get("artifact_bytes")
    mark(
        "N2_download_ok",
        download.get("ok") is True
        and isinstance(download.get("artifact_name"), str)
        and isinstance(artifact_size, int)
        and not isinstance(artifact_size, bool)
        and artifact_size > 0,
    )
    _rpc(
        project,
        "media-net-executor",
        "/run/fetchnow-media-net/ctrl/worker.sock",
        {"v": 1, "op": "release", "job_id": job_dl, "attempt": 1, "fence": 4},
    )

    direct = run(
        compose_argv(
            project,
            "exec",
            "-T",
            "media-net-executor",
            "python",
            "-c",
            "import socket\n"
            "s=socket.socket(); s.settimeout(2)\n"
            "try:\n"
            " s.connect(('1.1.1.1',443)); print('REACHED')\n"
            "except Exception as e:\n"
            " print(type(e).__name__)\n",
        ),
        check=False,
    )
    mark(
        "N3_direct_egress_denied",
        direct.returncode == 0
        and "REACHED" not in direct.stdout
        and bool(direct.stdout.strip()),
    )

    private_line = _connect_probe(project, "10.0.0.1")
    meta_line = _connect_probe(project, "169.254.169.254")
    v6_line = _connect_probe(project, "[::ffff:127.0.0.1]")
    mark("N4_private_connect_denied", private_line.startswith("HTTP/1.1 403"))
    mark("N5_metadata_connect_denied", meta_line.startswith("HTTP/1.1 403"))
    mark("N6_ipv6_mapped_connect_denied", v6_line.startswith("HTTP/1.1 403"))

    rebind_line = _connect_probe(project, "rebind.test")
    mark("N7_rebinding_connect_denied", rebind_line.startswith("HTTP/1.1 403"))

    run(compose_argv(project, "stop", "egress-proxy"), check=False)
    job2 = str(uuid.uuid4())
    _rpc(
        project,
        "media-net-executor",
        "/run/fetchnow-media-net/ctrl/worker.sock",
        {"v": 1, "op": "reserve", "job_id": job2, "attempt": 1, "fence": 4},
    )
    down = _rpc(
        project,
        "media-net-executor",
        "/run/fetchnow-media-net/ctrl/worker.sock",
        {
            "v": 1,
            "op": "inspect_metadata",
            "job_id": job2,
            "attempt": 1,
            "fence": 4,
            "url": "https://mock.invalid/video",
            "provider_id": "vk",
        },
    )
    mark("N8_proxy_down_fail_closed", down.get("ok") is not True)
    _rpc(
        project,
        "media-net-executor",
        "/run/fetchnow-media-net/ctrl/worker.sock",
        {"v": 1, "op": "cancel", "job_id": job2, "attempt": 1, "fence": 4},
    )
    for _ in range(40):
        rel = _rpc(
            project,
            "media-net-executor",
            "/run/fetchnow-media-net/ctrl/worker.sock",
            {"v": 1, "op": "release", "job_id": job2, "attempt": 1, "fence": 4},
        )
        if rel.get("ok") is True:
            break
        time.sleep(0.25)
    run(compose_argv(project, "start", "egress-proxy"))
    _configure_canary(project)
    _wait_socket(
        project, "media-net-executor", "/run/fetchnow-media-net/ctrl/worker.sock"
    )

    # N9: an adversarial writer ignores --max-filesize and stays alive.
    job9 = str(uuid.uuid4())
    _net_rpc(project, _request(job9, 4, "reserve"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        oversized = pool.submit(
            _net_rpc, project, _download_request(job9, 4, "oversize", 65_536)
        )
        try:
            before9 = _wait_fixture_started(project, job9, 4)
            _signal_fixture(project, job9, 4, "grow")
            over = oversized.result(timeout=20)
            snapshot9 = _fixture_snapshot(project, job9, 4, expected=before9)
        finally:
            _net_rpc(project, _request(job9, 4, "cancel"))
            oversized.result(timeout=20)
    mark("N9_limits_enforced", _limit_proven(over, snapshot9))
    checks["N9_evidence"] = {"before": before9, "outcome": over, "tree": snapshot9}
    release9 = _net_rpc(project, _request(job9, 4, "release"))
    if release9.get("code") != "released":
        mark("N9_limits_enforced", False)

    # Time and pipe-output limits must also kill/reap the controlled tree.
    for mode in ("stdout", "blocking"):
        jid = str(uuid.uuid4())
        _net_rpc(project, _request(jid, 4, "reserve"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(
                _net_rpc, project, _download_request(jid, 4, mode, 1_000_000)
            )
            try:
                before = _wait_fixture_started(project, jid, 4)
                if mode == "stdout":
                    _signal_fixture(project, jid, 4, "grow")
                outcome = task.result(timeout=30)
                after = _fixture_snapshot(project, jid, 4, expected=before)
            finally:
                _net_rpc(project, _request(jid, 4, "cancel"))
                task.result(timeout=20)
        release = _net_rpc(project, _request(jid, 4, "release"))
        checks[f"N9_{mode}_evidence"] = {
            "before": before,
            "outcome": outcome,
            "after": after,
            "release": release,
        }
        if (
            not _other_limit_proven(mode, outcome, after)
            or release.get("code") != "released"
        ):
            mark("N9_limits_enforced", False)

    # N10: run both real trees, cancel A, and observe B continuing.
    job_a, job_b = str(uuid.uuid4()), str(uuid.uuid4())
    _net_rpc(project, _request(job_a, 4, "reserve"))
    _net_rpc(project, _request(job_b, 5, "reserve"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        task_a = pool.submit(
            _net_rpc, project, _download_request(job_a, 4, "blocking", 1_000_000)
        )
        task_b = pool.submit(
            _net_rpc, project, _download_request(job_b, 5, "neighbor", 1_000_000)
        )
        try:
            before_a = _wait_fixture_started(project, job_a, 4)
            before_b = _wait_fixture_started(project, job_b, 5)
            cancel = _net_rpc(project, _request(job_a, 4, "cancel"))
            outcome_a = task_a.result(timeout=20)
            after_a = _fixture_snapshot(project, job_a, 4, expected=before_a)
            time.sleep(0.3)
            after_b = _fixture_snapshot(project, job_b, 5, expected=before_b)
            _signal_fixture(project, job_b, 5, "finish")
            neighbor = task_b.result(timeout=20)
            neighbor_reaped = _fixture_snapshot(project, job_b, 5, expected=before_b)
            mark(
                "N10_cancel_neighbor_ok",
                _cancel_proven(cancel, outcome_a, after_a, before_b, after_b)
                and neighbor.get("ok") is True
                and _tree_state(neighbor_reaped, gone=True),
            )
            checks["N10_evidence"] = {
                "before_a": before_a,
                "before_b": before_b,
                "cancel": cancel,
                "outcome_a": outcome_a,
                "after_a": after_a,
                "after_b": after_b,
                "neighbor": neighbor,
                "neighbor_reaped": neighbor_reaped,
            }
        finally:
            # This is cleanup, never a retry or acceptance substitute.
            for jid, fence in ((job_a, 4), (job_b, 5)):
                _net_rpc(project, _request(jid, fence, "cancel"))
            task_a.result(timeout=20)
            task_b.result(timeout=20)
    for jid, fence in ((job_a, 4), (job_b, 5)):
        released = _net_rpc(project, _request(jid, fence, "release"))
        if released.get("code") != "released":
            mark("N10_cancel_neighbor_ok", False)

    # N11: fencing belongs to the controlling worker, not reserve ordering.
    proof11 = _worker_fence_proof(project, Path(str(state["output"])))
    if proof11.get("pass") is True:
        old_job, old_fence = proof11.get("job_id"), proof11.get("fence")
        if not isinstance(old_job, str) or type(old_fence) is not int:
            raise OSError("worker proof missing attempt identity")
        run(compose_argv(project, "restart", "media-net-executor"))
        _wait_socket(project, "media-net-executor", NET_SOCKET)
        stale = _net_rpc(
            project, _download_request(old_job, old_fence, "video", 1_000_000)
        )
        fresh = _net_rpc(project, _request(old_job, old_fence + 1, "reserve"))
        completed = _net_rpc(
            project, _download_request(old_job, old_fence + 1, "video", 1_000_000)
        )
        released = _net_rpc(project, _request(old_job, old_fence + 1, "release"))
        proof11["restart"] = {
            "stale": stale,
            "fresh": fresh,
            "completed": completed,
            "release": released,
        }
        proof11["pass"] = (
            stale.get("code") == "not_reserved"
            and stale.get("ok") is False
            and fresh.get("code") == "reserved"
            and completed.get("ok") is True
            and released.get("code") == "released"
        )
    checks["N11_evidence"] = proof11
    mark("N11_fence_lease_ok", proof11.get("pass") is True)

    # N12: offline executor cannot reach proxy or origin (runtime, not compose text)
    offline = run(
        compose_argv(
            project,
            "exec",
            "-T",
            "media-executor",
            "python",
            "-c",
            "import socket\n"
            "out=[]\n"
            "for host,port in [('egress-proxy',8888),('1.2.3.4',443),('1.1.1.1',443)]:\n"  # noqa: E501
            "  s=socket.socket(); s.settimeout(1)\n"
            "  try:\n"
            "    s.connect((host,port)); out.append('REACHED:'+host)\n"
            "  except Exception as e:\n"
            "    out.append(type(e).__name__)\n"
            "  finally:\n"
            "    s.close()\n"
            "print('|'.join(out))\n",
        ),
        check=False,
    )
    mark(
        "N12_offline_unreachable",
        offline.returncode == 0
        and "REACHED" not in offline.stdout
        and bool(offline.stdout.strip()),
    )

    checks["identities"] = identities
    checks["probe_lines"] = {
        "private": private_line,
        "metadata": meta_line,
        "v6": v6_line,
        "rebind": rebind_line,
    }
    # Import closure still recorded but not a substitute for N-checks.
    for label, module, forbidden in (
        (
            "F1_net_import_closure",
            "fetchnow.media_executor.protocol",
            ["fetchnow.downloads.artifacts", "fetchnow.url.models", "sqlalchemy"],
        ),
        (
            "F1_proxy_import_closure",
            "fetchnow.media_egress_proxy.server",
            ["fetchnow.url.models", "fetchnow.url.validate"],
        ),
        (
            "F1_offline_import_closure",
            "fetchnow.media_executor.protocol",
            ["fetchnow.downloads.artifacts", "fetchnow.url.models"],
        ),
    ):
        image_key = {
            "F1_net_import_closure": "net_image",
            "F1_proxy_import_closure": "proxy_image",
            "F1_offline_import_closure": "offline_image",
        }[label]
        try:
            _import_smoke(str(state[image_key]), module, forbidden)
            checks[label] = True
        except OSError as exc:
            checks[label] = False
            checks[label + "_error"] = str(exc)[:200]

    verdict, err = aggregate_verdict(checks, trivy_required=True)
    # N13 filled by caller; temporary placeholder
    if "N13_same_artifact_audit" not in checks:
        checks["N13_same_artifact_audit"] = "NOT_RUN"
        verdict, err = aggregate_verdict(checks, trivy_required=True)
    checks["matrix_verdict"] = verdict
    checks["matrix_error"] = err
    return checks


def _audit_image(
    *,
    image_ref: str,
    image_id: str,
    output: Path,
    trivy_bin: str,
    label: str,
) -> tuple[dict[str, object], int]:
    audit_mod = Path(__file__).resolve().parent
    if str(audit_mod) not in sys.path:
        sys.path.insert(0, str(audit_mod))
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import executor_image_audit as audit

    audit_dir = output / "audit" / label
    audit_dir.mkdir(parents=True, exist_ok=True)
    summary, code = audit.audit_image(
        image_ref=image_ref,
        expected_image_id=image_id,
        work_dir=audit_dir / "work",
        cache_dir=audit_dir / "trivy-cache",
        trivy_bin=trivy_bin,
        expected_platform="linux/amd64",
    )
    if not isinstance(summary, dict):
        summary = {"status": "technical_failure", "error_code": "malformed_summary"}
        code = 1
    summary = {
        **summary,
        "scanner_exit_code": code,
        "expected_image_id": image_id,
        "image_ref": image_ref,
    }
    return summary, code


def _n13_from_audits(audits: dict[str, object]) -> object:
    if not audits:
        return "NOT_RUN"
    for _label, report in audits.items():
        if not isinstance(report, dict):
            return False
        status = report.get("status")
        code = report.get("scanner_exit_code")
        if status in {None, "technical_failure"}:
            return False
        if type(code) is not int:
            return False
        if code != 0 and status == "pass":
            return False
        if status == "fail":
            return False
        if status == "technical_failure":
            return False
        # needs_owner_decision is residual — not automatic PASS for N13 gate
        if status == "needs_owner_decision":
            return False
        if status != "pass":
            return False
        identity = report.get("identity")
        if not isinstance(identity, dict):
            return False
        if not (identity.get("image_id") and report.get("expected_image_id")):
            return False
    return True


def execute(output: Path, *, trivy_bin: str | None) -> int:
    output.mkdir(parents=True, exist_ok=False)
    name = f"fetchnow-sec09-net-{uuid.uuid4().hex[:8]}"
    project = f"{PROJECT_PREFIX}{name[-8:]}"
    tag = name[-8:]
    helpers = Path(__file__).resolve().parent
    overlay = output / "compose.sec09.disposable.yaml"
    _write_disposable_overlay(overlay, helpers=helpers, tag=tag)
    global _ACTIVE_OVERLAY, _EVIDENCE_OUTPUT
    _ACTIVE_OVERLAY = str(overlay)
    _EVIDENCE_OUTPUT = output
    state: dict[str, object] = {
        "name": name,
        "project": project,
        "net_image": f"fetchnow-media-net-executor:{tag}",
        "proxy_image": f"fetchnow-media-egress-proxy:{tag}",
        "offline_image": f"fetchnow-media-executor:{tag}",
        "overlay": str(overlay),
        "output": str(output.resolve()),
    }
    save(output / "state.json", state)
    result: dict[str, object] = {
        "status": "FAIL",
        "phase": "preflight",
        "checks": {},
        "note": "N1-N13 native Compose gate",
        "residual": {"accepted": False, "note": "owner acceptance not automatic"},
    }
    try:
        if os.geteuid() != 0:
            result["status"] = "NOT_RUN"
            result["error"] = "root required"
            raise OSError("root required")
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            result["status"] = "NOT_RUN"
            result["error"] = "native linux/amd64 required"
            raise OSError("native linux/amd64 required")
        info = json.loads(run(["docker", "info", "--format", "{{json .}}"]).stdout)
        if (
            info.get("Architecture") not in {"x86_64", "amd64"}
            or info.get("CgroupDriver") != "systemd"
            or info.get("CgroupVersion") != "2"
            or int(str(info.get("ServerVersion", "0")).split(".")[0]) < 28
            or any("rootless" in item for item in info.get("SecurityOptions", []))
        ):
            raise OSError("rootful Docker >=28 with systemd cgroup v2 required")
        result["preflight"] = {
            key: info.get(key)
            for key in (
                "Architecture",
                "CgroupDriver",
                "CgroupVersion",
                "ServerVersion",
            )
        }
        result["phase"] = "slice"
        result["slice"] = _prepare_slice(state, output)
        print(
            "sec09: preflight and disposable slice ready; building three images",
            flush=True,
        )
        result["phase"] = "build"
        identities = {
            "network_executor": {"image_id": "", "platform": "linux/amd64"},
            "egress_proxy": {"image_id": "", "platform": "linux/amd64"},
            "offline_executor": {"image_id": "", "platform": "linux/amd64"},
        }
        ids = _build_images(state)
        for key, image_id in ids.items():
            identities[key]["image_id"] = image_id
        result["identities"] = identities
        save(output / "state.json", state)

        result["phase"] = "compose-up"
        print(
            "sec09: image builds complete; starting disposable Compose project",
            flush=True,
        )
        env = os.environ.copy()
        env["FETCHNOW_RELEASE_REVISION"] = tag
        up = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                "compose.yaml",
                "-f",
                "compose.media-executor.yaml",
                "-f",
                "compose.media-net-executor.yaml",
                "-f",
                str(overlay),
                "--profile",
                "media-executor",
                "--profile",
                "media-net-executor",
                "up",
                "-d",
                "--no-build",
                "egress-proxy",
                "media-net-executor",
                "media-executor",
                "mock-dns",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if up.returncode != 0:
            raise OSError(up.stderr[-2000:] or "compose up failed")

        # The root host harness supplies the public-address canary in this
        # disposable network namespace. The audited proxy stays read-only and
        # gains no packages/capabilities; never modify the host namespace.
        _configure_canary(project)

        result["phase"] = "matrix"
        print("sec09: starting native N1-N12 matrix", flush=True)
        checks: dict[str, object] = {}
        result["checks"] = checks
        checks = _matrix(project=project, state=state, identities=ids, checks=checks)
        result["checks"] = checks

        if not trivy_bin:
            checks["N13_same_artifact_audit"] = "NOT_RUN"
            checks["N13_reason"] = "trivy_missing"
            result["audits"] = {}
        else:
            result["phase"] = "audit"
            print(
                "sec09: native matrix complete; starting same-artifact Trivy",
                flush=True,
            )
            audits: dict[str, object] = {}
            codes: list[int] = []
            for label, key, ref_key in (
                ("network", "network_executor", "net_image"),
                ("proxy", "egress_proxy", "proxy_image"),
                ("offline", "offline_executor", "offline_image"),
            ):
                summary, code = _audit_image(
                    image_ref=str(state[ref_key]),
                    image_id=ids[key],
                    output=output,
                    trivy_bin=trivy_bin,
                    label=label,
                )
                audits[label] = summary
                codes.append(code)
            result["audits"] = audits
            result["audit_exit_codes"] = codes
            residual = {
                label: {
                    "status": report.get("status"),
                    "unfixed": report.get("gate_unfixed_unique_ids"),
                }
                for label, report in audits.items()
                if isinstance(report, dict)
            }
            result["residual"] = {
                "accepted": False,
                "by_image": residual,
                "note": "owner acceptance not automatic",
            }
            checks["N13_same_artifact_audit"] = _n13_from_audits(audits)

        audit_reports = result.get("audits")
        verdict, err = aggregate_verdict(
            checks,
            audits=audit_reports if isinstance(audit_reports, dict) else None,
            trivy_required=bool(trivy_bin),
        )
        checks["matrix_verdict"] = verdict
        checks["matrix_error"] = err
        result["checks"] = checks
        result["status"] = verdict
        if err:
            result["error"] = err
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)[:800]
        if result["phase"] == "matrix":
            failed_checks = result["checks"]
            if isinstance(failed_checks, dict):
                failed_checks["N13_same_artifact_audit"] = "NOT_RUN"
                failed_checks["N13_reason"] = "matrix_exception"
        if result.get("status") not in {"NOT_RUN"}:
            result["status"] = "FAIL"
        print(f"sec09 native acceptance failed in {result['phase']}: {exc}", flush=True)
    finally:
        try:
            errors = cleanup(state)
            # noqa keep
        except Exception as exc:
            errors = [type(exc).__name__]
        _ACTIVE_OVERLAY = None
        _EVIDENCE_OUTPUT = None
        result["cleanup_errors"] = errors
        if errors and result["status"] == "PASS":
            result["status"] = "FAIL"
            result["error"] = "cleanup_failed"
        save(output / "result.json", result)
        save(
            output / "hashes.json",
            {
                "result.json": hashlib.sha256(
                    (output / "result.json").read_bytes()
                ).hexdigest()
            },
        )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--trivy-bin", default="")
    args = parser.parse_args()
    if args.cleanup:
        state_path = args.output / "state.json"
        errors = (
            cleanup(json.loads(state_path.read_text())) if state_path.exists() else []
        )
        raise SystemExit(1 if errors else 0)
    raise SystemExit(execute(args.output, trivy_bin=args.trivy_bin or None))
