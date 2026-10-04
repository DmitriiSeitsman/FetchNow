"""Real executor acceptance on disposable native Linux AMD64; no retries.

Never run on production. Synthetic tools are mounted ONLY in the lifecycle
container. Both phases use the same production image/entrypoint/runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PATTERN = re.compile(r"fetchnow-sec08-exec-[0-9a-f]{8}\Z")
LIMIT = 256 * 1024 * 1024  # Two-job disposable test ceiling, NOT production.


def run(
    argv: list[str], timeout: int = 60, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        proc = subprocess.run(
            argv, cwd=ROOT, stdout=output, stderr=errors, timeout=timeout, check=False
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
        print(result.stderr[-2000:], flush=True)
        raise OSError(f"{argv[0]} failed: exit {result.returncode}")
    return result


def save(path: Path, payload: object) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temp.chmod(0o644)
    temp.replace(path)


def cleanup(state: dict[str, object], *, remove_image: bool = True) -> list[str]:
    name = state["name"]
    if not isinstance(name, str) or not PATTERN.fullmatch(name):
        raise ValueError("unsafe cleanup identity")
    failures = []
    for suffix in ("media", "lifecycle"):
        target = f"{name}-{suffix}"
        try:
            present = run(
                ["docker", "container", "ls", "-aq", "--filter", f"name=^/{target}$"]
            ).stdout.strip()
            if present:
                run(["docker", "rm", "-f", target])
        except (OSError, subprocess.TimeoutExpired):
            failures.append(f"container-{suffix}")
    if not failures:
        unit = Path("/run/systemd/system") / f"{name}.slice"
        try:
            if unit.exists():
                run(["systemctl", "stop", unit.name])
                unit.unlink()
                run(["systemctl", "daemon-reload"])
        except (OSError, subprocess.TimeoutExpired):
            failures.append("slice")
    if remove_image:
        try:
            image = state.get("image_ref") or f"fetchnow-media-executor:{name}"
            if not isinstance(image, str):
                raise OSError("image ref invalid")
            if run(["docker", "image", "ls", "-q", image]).stdout.strip():
                run(["docker", "image", "rm", image])
        except (OSError, subprocess.TimeoutExpired):
            failures.append("image")
    return failures


def start(name: str, mode: str, work: Path, image: str, proof: Path) -> str:
    target = f"{name}-{mode}"
    private = work / mode
    private.mkdir()
    for part in ("work", "ipc"):
        (private / part).mkdir()
    argv = [
        "docker",
        "run",
        "-d",
        "--name",
        target,
        "--network",
        "none",
        "--cgroupns",
        "private",
        "--cgroup-parent",
        f"{name}.slice",
        "--security-opt",
        "writable-cgroups=true",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "SETUID",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETPCAP",
        "--pids-limit",
        "128",
        "--read-only",
        "--tmpfs",
        "/tmp:mode=1777,size=64m",
        "-v",
        f"{private / 'work'}:/var/lib/fetchnow/executor",
        "-v",
        f"{private / 'ipc'}:/run/fetchnow-executor",
        "-v",
        f"{proof}:/opt/sec08-tests:ro",
        "-e",
        "MEDIA_EXECUTOR_WORK_ROOT=/var/lib/fetchnow/executor",
        "-e",
        "MEDIA_EXECUTOR_SOCKET=/run/fetchnow-executor/ctrl/worker.sock",
    ]
    if mode == "lifecycle":
        argv += [
            "-v",
            f"{work / 'probe-tool'}:/usr/local/bin/sec08-probe:ro",
            "-e",
            "MEDIA_MUXING_FFMPEG_PATH=/usr/local/bin/sec08-probe",
        ]
    run([*argv, image])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if (private / "ipc/ctrl/worker.sock").exists():
            return target
        time.sleep(0.1)
    print(run(["docker", "logs", target], check=False).stderr[-2000:])
    raise OSError("executor startup deadline")


def inside(target: str, phase: str) -> dict[str, object]:
    payload: dict[str, object] = json.loads(
        run(
            [
                "docker",
                "exec",
                target,
                "python",
                "/opt/sec08-tests/native_inside.py",
                phase,
            ],
            timeout=90,
        ).stdout
    )
    return payload


def verify_runtime(target: str, control: Path) -> dict[str, object]:
    data = json.loads(run(["docker", "inspect", target]).stdout)[0]
    config = data["HostConfig"]
    assert config["Privileged"] is False and config["NetworkMode"] == "none"
    assert config["CgroupnsMode"] == "private" and config["ReadonlyRootfs"] is True
    assert {cap.removeprefix("CAP_") for cap in config["CapAdd"]} == {
        "SETUID",
        "SETGID",
        "SETPCAP",
    }
    assert config["CapDrop"] == ["ALL"]
    assert {"writable-cgroups=true", "no-new-privileges:true"} <= set(
        config["SecurityOpt"]
    )
    pid = data["State"]["Pid"]
    relative = Path(f"/proc/{pid}/cgroup").read_text().strip().removeprefix("0::")
    host_scope = Path("/sys/fs/cgroup") / relative.lstrip("/")
    assert host_scope.parent == control
    stat = host_scope.stat()
    observed = json.loads(
        run(
            [
                "docker",
                "exec",
                target,
                "python",
                "-c",
                "import os,json; s=os.stat('/sys/fs/cgroup'); "
                "print(json.dumps([s.st_dev,s.st_ino]))",
            ]
        ).stdout
    )
    assert observed == [stat.st_dev, stat.st_ino]
    assert (
        Path(f"/proc/{pid}/ns/cgroup").readlink()
        != Path("/proc/1/ns/cgroup").readlink()
    )
    run(
        [
            "docker",
            "exec",
            target,
            "python",
            "-c",
            "from pathlib import Path; p=Path('/sys/fs/cgroup/memory.max');"
            "\ntry: p.write_text('max')\nexcept PermissionError: pass",
        ]
    )
    assert (control / "memory.max").read_text().strip() == str(LIMIT)
    return {
        "image": data["Image"],
        "pid": pid,
        "host_cgroup": relative,
        "private_root_dev_inode": observed,
        "ancestor_memory_max": LIMIT,
    }


def inspect_image_id(image: str) -> str:
    image_id = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image]
    ).stdout.strip()
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise OSError("image id invalid")
    return image_id


def assert_runtime_inventory(target: str) -> dict[str, object]:
    """Fail closed if C source or build-cache objects leaked into runtime."""
    script = (
        "from pathlib import Path\n"
        "import json, subprocess\n"
        "root = Path('/opt/fetchnow/src/fetchnow/media_executor')\n"
        "c_files = sorted(str(p) for p in root.rglob('*.c'))\n"
        "pyc = sorted(str(p) for p in root.rglob('*.pyc'))\n"
        "pycache = sorted(str(p) for p in root.rglob('__pycache__'))\n"
        "bad = c_files + pyc + pycache\n"
        "pkgs = subprocess.check_output(\n"
        "  ['dpkg-query','-W','libpcre2-8-0'], text=True\n"
        ").strip()\n"
        "print(json.dumps({\n"
        "  'bad': bad, 'libpcre2': pkgs,\n"
        "  'c_files': c_files, 'pyc': pyc, 'pycache': pycache\n"
        "}))\n"
    )
    data: dict[str, object] = json.loads(
        run(["docker", "exec", target, "python", "-c", script]).stdout
    )
    assert data["bad"] == [], data["bad"]
    pkg, sep, ver = str(data["libpcre2"]).partition("\t")
    if not sep:
        pkg, _, ver = str(data["libpcre2"]).partition(" ")
    assert pkg == "libpcre2-8-0" and ver == "10.46-1~deb13u3", data["libpcre2"]
    return data


def execute(
    output: Path,
    *,
    image: str | None = None,
    expected_image_id: str | None = None,
    retain_image: bool = False,
) -> int:
    output.mkdir(parents=True, exist_ok=False)
    name = f"fetchnow-sec08-exec-{uuid.uuid4().hex[:8]}"
    image_ref = image or f"fetchnow-media-executor:{name}"
    state: dict[str, object] = {"name": name, "image_ref": image_ref}
    save(output / "state.json", state)
    result: dict[str, object] = {"status": "FAIL", "checks": {}, "phase": "preflight"}
    checks: dict[str, object] = {}
    result["checks"] = checks
    try:
        assert os.geteuid() == 0
        assert platform.system() == "Linux" and platform.machine() == "x86_64"
        info = json.loads(run(["docker", "info", "--format", "{{json .}}"]).stdout)
        assert info["Architecture"] in {"x86_64", "amd64"}
        assert info["CgroupDriver"] == "systemd" and info["CgroupVersion"] == "2"
        assert int(info["ServerVersion"].split(".")[0]) >= 28
        assert not any("rootless" in item for item in info["SecurityOptions"])
        checks["preflight"] = {
            key: info[key]
            for key in (
                "Architecture",
                "CgroupDriver",
                "CgroupVersion",
                "ServerVersion",
            )
        }
        result["phase"] = "slice"
        unit = Path("/run/systemd/system") / f"{name}.slice"
        unit.write_text(f"[Slice]\nMemoryMax={LIMIT}\nTasksMax=256\n")
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "start", unit.name])
        relative = run(
            ["systemctl", "show", "--property=ControlGroup", "--value", unit.name]
        ).stdout.strip()
        assert relative.startswith("/") and ".." not in Path(relative).parts
        control = Path("/sys/fs/cgroup") / relative.lstrip("/")
        assert (control / "memory.max").read_text().strip() == str(LIMIT)
        if image is None:
            result["phase"] = "build"
            run(
                [
                    "docker",
                    "build",
                    "-f",
                    "backend/Dockerfile.media-executor",
                    "-t",
                    image_ref,
                    "backend",
                ],
                timeout=600,
            )
        image_id = inspect_image_id(image_ref)
        if expected_image_id is not None and image_id != expected_image_id:
            raise OSError(
                f"image identity drift before native: {image_id} != {expected_image_id}"
            )
        arch = run(
            ["docker", "image", "inspect", "--format", "{{.Architecture}}", image_ref]
        ).stdout.strip()
        assert arch == "amd64"
        result["image_id"] = image_id
        result["image_ref"] = image_ref
        result["platform"] = f"linux/{arch}"
        checks["image_identity"] = {
            "image_id": image_id,
            "platform": result["platform"],
        }
        proof = Path(__file__).resolve().parent
        work = output / "private"
        work.mkdir(mode=0o700)
        shutil.copyfile(proof / "probe_tool.py", work / "probe-tool")
        (work / "probe-tool").chmod(0o555)
        for mode in ("media", "lifecycle"):
            result["phase"] = mode
            target = start(name, mode, work, image_ref, proof)
            if mode == "media":
                checks["runtime_inventory"] = assert_runtime_inventory(target)
            observed = run(
                ["docker", "inspect", "--format", "{{.Image}}", target]
            ).stdout.strip()
            if observed != image_id:
                raise OSError(f"container image drift: {observed} != {image_id}")
            checks[f"{mode}_runtime"] = verify_runtime(target, control)
            checks[mode] = inside(target, mode)
            if mode == "lifecycle":
                run(["docker", "restart", target])
                checks["restart"] = inside(target, "restart")
            run(["docker", "rm", "-f", target])
            save(output / "result.json", result)
        after = inspect_image_id(image_ref)
        if after != image_id:
            raise OSError(f"image identity drift after native: {after} != {image_id}")
        result["status"] = "PASS"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        print(f"native acceptance failed in {result['phase']}: {exc}", flush=True)
    finally:
        try:
            errors = cleanup(state, remove_image=not retain_image)
        except Exception as exc:
            errors = [type(exc).__name__]
        result["cleanup_errors"] = errors
        result["image_retained"] = retain_image
        if errors:
            result["status"] = "FAIL"
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
    parser.add_argument("--image", default=None)
    parser.add_argument("--expected-image-id", default=None)
    parser.add_argument("--retain-image", action="store_true")
    args = parser.parse_args()
    if args.cleanup:
        state_path = args.output / "state.json"
        errors = (
            cleanup(json.loads(state_path.read_text()), remove_image=True)
            if state_path.exists()
            else []
        )
        raise SystemExit(1 if errors else 0)
    raise SystemExit(
        execute(
            args.output,
            image=args.image,
            expected_image_id=args.expected_image_id,
            retain_image=args.retain_image,
        )
    )
