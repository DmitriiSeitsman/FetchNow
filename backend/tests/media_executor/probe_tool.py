#!/usr/local/bin/python
"""Synthetic hostile tool, mounted read-only ONLY by native acceptance.

Invoked by the production server's fixed mux argv, through the real launcher.
Never copied into the runtime image and never selectable by an RPC field.
"""

import errno
import json
import os
import socket
import sys
import time
from pathlib import Path

video = Path(sys.argv[sys.argv.index("-i") + 1])
attempt = video.parent
mode = video.read_text().strip()

if mode == "probe":
    observations = {
        "uid": os.getresuid(),
        "gid": os.getresgid(),
        "groups": os.getgroups(),
        "env": dict(os.environ),
    }
    denied = {}
    for name, target in (
        (
            "sibling",
            attempt.parent / "22222222-2222-4222-8222-222222222222_1_1" / "canary",
        ),
        ("published", attempt.parent / "published" / "canary"),
        ("symlink", attempt / "escape"),
        ("proc", Path("/proc/1/environ")),
        ("cgroup", Path("/sys/fs/cgroup/cgroup.procs")),
    ):
        try:
            target.read_bytes()
        except OSError as exc:
            denied[name] = exc.errno
        else:
            denied[name] = 0
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(1)
        try:
            sock.connect("/run/fetchnow-executor/ctrl/worker.sock")
        except OSError as exc:
            denied["socket"] = exc.errno
        else:
            denied["socket"] = 0
    observations["denied"] = denied
    (attempt / "output-mux").write_text(json.dumps(observations))
    print(json.dumps(observations), flush=True)
    sys.exit(
        0
        if all(value in (errno.EACCES, errno.EPERM) for value in denied.values())
        else 1
    )

if mode != "hold":
    raise SystemExit("invalid synthetic mode")

# Double fork + setsid: the descendant escapes the process group but not cgroup.
child = os.fork()
if child == 0:
    os.setsid()
    grandchild = os.fork()
    if grandchild:
        os._exit(0)
    (attempt / "descendant.pid").write_text(str(os.getpid()))
    heartbeat = attempt / "descendant.hb"
else:
    os.waitpid(child, 0)
    (attempt / "parent.pid").write_text(str(os.getpid()))
    heartbeat = attempt / "parent.hb"
while True:
    with heartbeat.open("ab") as stream:
        stream.write(b".")
    time.sleep(0.05)
