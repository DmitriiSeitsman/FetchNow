"""Process and cgroup lifecycle. The worker never supplies a PID or a path."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fetchnow.media_executor.argv import tool_env
from fetchnow.media_executor.constants import MIN_LANDLOCK_ABI
from fetchnow.media_executor.layout import layout_rejection

_CGROUP_NAME = re.compile(r"\Afn-[0-9a-f]{16}\Z")
CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    cancelled: bool


class ToolRunner:
    def run(
        self,
        *,
        attempt: Path,
        argv: list[str],
        timeout_seconds: float,
        cancel: threading.Event,
        protected: list[str],
        max_output_bytes: int | None = None,
        min_free_bytes: int | None = None,
    ) -> ToolOutcome:
        raise NotImplementedError


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, 9)
    except OSError:
        try:
            os.kill(pid, 9)
        except OSError:
            return


def _tree_bytes(root: Path) -> int:
    """LocalRunner helper: fail closed on walk/stat errors (not production DAC)."""
    total = 0

    def onerror(err: OSError) -> None:
        raise err

    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=onerror
    ):
        # Do not descend through symlinks.
        dirnames[:] = [
            name for name in dirnames if not Path(dirpath, name).is_symlink()
        ]
        for name in filenames:
            path = Path(dirpath, name)
            if path.is_symlink():
                continue
            total += path.stat().st_size
    return total


class LocalRunner(ToolRunner):
    """In-process fallback is not used in production. Tests use this runner."""

    def run(
        self,
        *,
        attempt: Path,
        argv: list[str],
        timeout_seconds: float,
        cancel: threading.Event,
        protected: list[str],
        max_output_bytes: int | None = None,
        min_free_bytes: int | None = None,
    ) -> ToolOutcome:
        del protected
        proc = subprocess.Popen(
            argv,
            cwd=str(attempt),
            env=tool_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        cancelled = False
        limit_hit = False
        while proc.poll() is None:
            if cancel.is_set():
                cancelled = True
                _kill_group(proc.pid)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_group(proc.pid)
                break
            try:
                if (
                    max_output_bytes is not None
                    and _tree_bytes(attempt) > max_output_bytes
                ):
                    limit_hit = True
                    _kill_group(proc.pid)
                    break
                if min_free_bytes is not None:
                    import shutil

                    if shutil.disk_usage(attempt).free < min_free_bytes:
                        limit_hit = True
                        _kill_group(proc.pid)
                        break
            except OSError:
                limit_hit = True
                _kill_group(proc.pid)
                break
            time.sleep(0.02)
        stdout, stderr = proc.communicate(timeout=5)
        return ToolOutcome(
            exit_code=1 if limit_hit else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            cancelled=cancelled,
        )


def write_cgroup_kill(cgroup: Path) -> None:
    path = cgroup / "cgroup.kill"
    fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
    try:
        if os.write(fd, b"1\n") != 2:
            raise OSError("cgroup.kill short write")
    finally:
        os.close(fd)


def own_cgroup_names(root: Path = CGROUP_ROOT) -> list[str]:
    """Names this namespace created. Missing or foreign trees are not safe empties."""
    names: list[str] = []
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise OSError("cgroup inventory unavailable") from exc
    for entry in entries:
        if entry.is_symlink():
            continue
        if _CGROUP_NAME.fullmatch(entry.name) and entry.is_dir():
            names.append(entry.name)
    return names


def sweep_own_cgroups(root: Path = CGROUP_ROOT) -> None:
    for name in own_cgroup_names(root):
        path = root / name
        write_cgroup_kill(path)
        deadline = time.monotonic() + 3
        while "populated 1" in (path / "cgroup.events").read_text():
            if time.monotonic() >= deadline:
                raise OSError("stale cgroup did not terminate")
            time.sleep(0.02)
        path.rmdir()


class LandlockRunner(ToolRunner):
    def __init__(self, launcher: Path, *, cgroup_root: Path = CGROUP_ROOT) -> None:
        self._launcher = launcher
        self._cgroup_root = cgroup_root

    def identity_abi(self) -> int:
        proc = subprocess.run(
            [str(self._launcher), "identity"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            raise OSError("landlock abi below floor or launcher failed")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise OSError("launcher identity unreadable") from exc
        abi = payload.get("abi")
        if type(abi) is not int or abi < MIN_LANDLOCK_ABI:
            raise OSError("landlock abi below floor")
        return abi

    def run(
        self,
        *,
        attempt: Path,
        argv: list[str],
        timeout_seconds: float,
        cancel: threading.Event,
        protected: list[str],
        max_output_bytes: int | None = None,
        min_free_bytes: int | None = None,
    ) -> ToolOutcome:
        read_only = ["/usr", "/bin", "/lib", "/lib64"]
        for item in os.environ.get("MEDIA_EXECUTOR_RO_ROOTS", "").split(":"):
            root = item.strip()
            if root and (os.path.isdir(root) or os.path.isfile(root)):
                read_only.append(root)
        read_only = [
            item for item in read_only if os.path.isdir(item) or os.path.isfile(item)
        ]
        dir_roots = [item for item in read_only if os.path.isdir(item)]
        if layout_rejection(dir_roots, protected + [str(attempt)]):
            raise OSError("ro root covers a protected path")
        token = os.urandom(8).hex()
        cgroup = self._cgroup_root / f"fn-{token}"
        cgroup.mkdir()
        command = [
            str(self._launcher),
            "launch",
            "--attempt",
            str(attempt),
            "--cgroup-procs",
            str(cgroup / "cgroup.procs"),
        ]
        if os.environ.get("MEDIA_EXECUTOR_PROFILE", "").strip() == "network":
            command.append("--allow-net-resolver")
        for root in read_only:
            command.extend(["--ro", root])
        for secret in protected:
            command.extend(["--protect", secret])
        command.append("--")
        command.extend(argv)
        # A fresh, single-threaded subreaper owns this job's children. Calling
        # waitpid(-1) in server threads can reap another job's Popen child.
        read_fd, write_fd = os.pipe()
        proc = None
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "fetchnow.media_executor.job_process",
                    str(cgroup),
                    str(read_fd),
                    str(timeout_seconds),
                    str(max_output_bytes or 0),
                    str(min_free_bytes or 0),
                    str(attempt),
                    *command,
                ],
                cwd="/",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(read_fd,),
                start_new_session=True,
            )
            os.close(read_fd)
            read_fd = -1
            deadline = time.monotonic() + timeout_seconds + 12
            sent = False
            while True:
                if cancel.is_set() and not sent:
                    with contextlib.suppress(BrokenPipeError):
                        os.write(write_fd, b"c")
                    sent = True
                try:
                    stdout, _stderr = proc.communicate(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise OSError("job supervisor exceeded deadline") from None
            if proc.returncode != 0:
                raise OSError("job supervisor failed")
            payload = json.loads(stdout)
            return ToolOutcome(
                exit_code=payload["exit_code"],
                stdout=base64.b64decode(payload["stdout"], validate=True),
                stderr=base64.b64decode(payload["stderr"], validate=True),
                timed_out=payload["timed_out"],
                cancelled=payload["cancelled"],
            )
        finally:
            if read_fd >= 0:
                os.close(read_fd)
            os.close(write_fd)
            try:
                write_cgroup_kill(cgroup)
            finally:
                if proc is not None and proc.poll() is None:
                    try:
                        # Let its subreaper collect descendants after cgroup.kill.
                        proc.communicate(timeout=6)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate(timeout=5)
                cgroup.rmdir()
