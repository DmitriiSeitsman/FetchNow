"""One trusted subreaper per invocation; never a network/RPC entrypoint."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

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


def execute(
    cgroup: Path, cancel_fd: int, seconds: float, command: list[str]
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
    cancelled = timed_out = overflow = killed = False
    empty = False
    cleanup_deadline = float("inf")
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
                    cancelled or timed_out or overflow or proc.returncode is not None
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
    return {
        "exit_code": 1 if overflow else proc.returncode,
        "stdout": base64.b64encode(buffers["stdout"]).decode("ascii"),
        "stderr": base64.b64encode(buffers["stderr"]).decode("ascii"),
        "cancelled": cancelled,
        "timed_out": timed_out,
    }


if __name__ == "__main__":
    group, descriptor, budget, *argv = sys.argv[1:]
    print(json.dumps(execute(Path(group), int(descriptor), float(budget), argv)))
