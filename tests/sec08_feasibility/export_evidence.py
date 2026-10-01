"""Export synthetic proof evidence AFTER cleanup, without changing test DAC.

Run as host root to read root/group-owned proof files. The separate copy uses
readable modes for upload-artifact; original bytes/modes remain untouched.
No environment, general workspace, binaries or unrelated files are exported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
ALLOWED = re.compile(
    r"(?:result\.json|stage-hashes\.json|stages/[a-z0-9-]+\.json|"
    r"(?:inside|fresh)/(?:supervisor\.json|hold|please-kill|"
    r"(?:a|b|probe)-(?:launcher-status\.txt|launch\.err|sleep\.json))|sentinel/hb)\Z"
)


def read_regular(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError(f"not a bounded regular evidence file: {path.name}")
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("evidence size limit")
    return data


def export_evidence(source: Path, destination: Path) -> dict:
    source = source.absolute()
    destination = destination.absolute()
    if source.is_symlink() or not source.is_dir():
        raise ValueError("source must be a real proof directory")
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination must not exist")
    if source == destination or source in destination.parents:
        raise ValueError("destination must be outside source")
    payloads = {}
    total = 0
    for directory, dirs, files in os.walk(source, followlinks=False):
        root = Path(directory)
        for name in dirs[:]:
            entry = root / name
            if entry.is_symlink():
                raise ValueError("symlink directory in evidence")
            if root == source and name == "bin":
                dirs.remove(name)  # Host-compiled executable is not an upload input.
            elif root != source or name not in {"inside", "fresh", "stages", "sentinel"}:
                raise ValueError("unexpected evidence directory")
        for name in files:
            path = root / name
            relative = path.relative_to(source).as_posix()
            if not ALLOWED.fullmatch(relative):
                raise ValueError(f"unexpected evidence file: {relative}")
            data = read_regular(path)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("total evidence size limit")
            payloads[relative] = data
    if not {"result.json", "stage-hashes.json"}.issubset(payloads):
        raise ValueError("final result or hashes missing")
    result = json.loads(payloads["result.json"])
    if result.get("kind") != "sec08-container-cgroup-proof":
        raise ValueError("unexpected proof report")
    hashes = json.loads(payloads["stage-hashes.json"])
    expected = {"result.json": "result.json"}
    expected.update({Path(p).name: p for p in payloads if p.startswith("stages/")})
    if set(hashes) != set(expected):
        raise ValueError("stage hash inventory mismatch")
    for name, relative in expected.items():
        if hashes[name] != hashlib.sha256(payloads[relative]).hexdigest():
            raise ValueError("original evidence hash mismatch")

    # Only create the copy after all inputs have been checked. This runs after
    # container cleanup; it does not chmod the mounted proof workspace.
    destination.mkdir(mode=0o755)
    os.chmod(destination, 0o755)
    manifest = {}
    for relative, data in sorted(payloads.items()):
        target = destination / relative
        if target.parent != destination:
            target.parent.mkdir(mode=0o755, exist_ok=True)
            os.chmod(target.parent, 0o755)
        with target.open("xb") as stream:
            stream.write(data)
        os.chmod(target, 0o644)
        manifest[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    index = destination / "evidence-manifest.json"
    index.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    os.chmod(index, 0o644)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    export_evidence(args.source, args.destination)


if __name__ == "__main__":
    main()
