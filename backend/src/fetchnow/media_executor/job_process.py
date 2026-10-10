"""One trusted subreaper per invocation; never a network/RPC entrypoint."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fetchnow.media_executor.layout import measure_tree_bytes
from fetchnow.media_executor.runner import write_cgroup_kill


def reap_children(proc: subprocess.Popen[bytes], seconds: float = 5) -> None:
    """Also used on exceptional exits: never leave zombies for server threads."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == proc.pid:
            proc.returncode = os.waitstatus_to_exitcode(status)
        if time.monotonic() >= deadline:
            raise OSError("exception cleanup reaping deadline")
        if pid == 0:
            time.sleep(0.02)


def _tree_bytes(root: Path) -> int:
    """Identity-aware size via helper process. Access errors fail closed."""
    return measure_tree_bytes(root)


def execute(
    cgroup: Path,
    cancel_fd: int,
    seconds: float,
    max_output_bytes: int,
    min_free_bytes: int,
    attempt: Path,
    command: list[str],
) -> dict[str, object]:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError("subreaper unavailable")
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    # base64 plus JSON framing must fit MAX_RESPONSE_BYTES=400000.
    limits = {"stdout": 196608, "stderr": 65536}
    deadline = time.monotonic() + seconds
    cancelled = timed_out = overflow = killed = limit_hit = False
    empty = False
    cleanup_deadline = float("inf")
    last_size_check = 0.0
    with selectors.DefaultSelector() as selector:
        selector.register(cancel_fd, selectors.EVENT_READ, "cancel")
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        try:
            while True:
                for key, _ in selector.select(0.02):
                    if key.data == "cancel":
                        os.read(cancel_fd, 1)
                        cancelled = True  # EOF also means the owning supervisor died.
                        selector.unregister(cancel_fd)
                        continue
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    remaining = limits[name] - len(buffers[name])
                    buffers[name].extend(block[:remaining])
                    overflow |= len(block) > remaining
                now = time.monotonic()
                if now - last_size_check >= 0.2:
                    last_size_check = now
                    try:
                        if (
                            max_output_bytes > 0
                            and _tree_bytes(attempt) > max_output_bytes
                        ):
                            limit_hit = True
                        if min_free_bytes > 0:
                            free = int(shutil.disk_usage(attempt).free)
                            if free < min_free_bytes:
                                limit_hit = True
                    except OSError:
                        # Enumeration/stat/helper failure is not "size 0".
                        limit_hit = True
                # Only this process's children are reaped, never another job.
                while True:
                    try:
                        pid, status = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        empty = True
                        break
                    if pid == 0:
                        break
                    if pid == proc.pid:
                        proc.returncode = os.waitstatus_to_exitcode(status)
                timed_out |= time.monotonic() >= deadline and not killed
                if not killed and (
                    cancelled
                    or timed_out
                    or overflow
                    or limit_hit
                    or proc.returncode is not None
                ):
                    # Kill surviving descendants even after a successful parent exit.
                    write_cgroup_kill(cgroup)
                    killed = True
                    cleanup_deadline = time.monotonic() + 5
                if time.monotonic() >= cleanup_deadline:
                    raise OSError("job descendants not reaped")
                if empty and not any(
                    k.data != "cancel" for k in selector.get_map().values()
                ):
                    break
        finally:
            try:
                write_cgroup_kill(cgroup)
                reap_children(proc)
            finally:
                proc.stdout.close()
                proc.stderr.close()
    if not empty or proc.returncode is None:
        raise OSError("incomplete reaping")
    exit_code = proc.returncode
    if overflow or limit_hit:
        exit_code = 1
    return {
        "exit_code": exit_code,
        "stdout": base64.b64encode(buffers["stdout"]).decode("ascii"),
        "stderr": base64.b64encode(buffers["stderr"]).decode("ascii"),
        "cancelled": cancelled,
        "timed_out": timed_out,
    }


if __name__ == "__main__":
    group, descriptor, budget, max_out, min_free, attempt, *argv = sys.argv[1:]
    print(
        json.dumps(
            execute(
                Path(group),
                int(descriptor),
                float(budget),
                int(max_out),
                int(min_free),
                Path(attempt),
                argv,
            )
        )
    )
