"""Export bounded final reports only, never workspaces or tool-owned files."""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def export(source: Path, destination: Path) -> None:
    if source.is_symlink() or destination.exists():
        raise ValueError("unsafe evidence directory")
    payloads = {}
    for name in ("result.json", "hashes.json"):
        fd = os.open(source / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not regular evidence")
            payloads[name] = stream.read(1_000_001)
        if len(payloads[name]) > 1_000_000:
            raise ValueError("evidence limit")
    digest = hashlib.sha256(payloads["result.json"]).hexdigest()
    if json.loads(payloads["hashes.json"]) != {"result.json": digest}:
        raise ValueError("evidence hash mismatch")
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    for name, data in payloads.items():
        target = destination / name
        target.write_bytes(data)
        target.chmod(0o644)
        if (
            hashlib.sha256(target.read_bytes()).digest()
            != hashlib.sha256(data).digest()
        ):
            raise ValueError("copy hash mismatch")


if __name__ == "__main__":
    export(Path(sys.argv[1]), Path(sys.argv[2]))
