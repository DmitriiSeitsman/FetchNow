"""Local filesystem smoke for the SEC-08 workspace contract.

This is not the native cgroup proof. It starts one disposable container with
the same narrow capabilities and does not create a slice or job cgroups.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
IMAGE = "fetchnow-sec08-dacsmoke:local"
NAME = "fetchnow-sec08-dacsmoke-local"


def run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)


def main() -> int:
    build = run(
        ["docker", "build", "-f", str(SRC / "proof.Dockerfile"), "-t", IMAGE, str(SRC)],
        300,
    )
    if build.returncode != 0:
        sys.stderr.write(build.stderr[-2000:])
        return build.returncode or 1
    try:
        proc = run(
            [
                "docker",
                "run",
                "--name",
                NAME,
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "SETUID",
                "--cap-add",
                "SETGID",
                "--cap-add",
                "SETPCAP",
                "--security-opt",
                "no-new-privileges:true",
                "-v",
                f"{SRC}:/opt/sec08-trusted/harness:ro",
                IMAGE,
                "python3",
                "/opt/sec08-trusted/harness/container_supervisor.py",
                "dac-smoke",
            ],
            60,
        )
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr[-2000:])
        return proc.returncode
    finally:
        run(["docker", "rm", "-f", NAME], 30)
        run(["docker", "rmi", IMAGE], 60)


if __name__ == "__main__":
    sys.exit(main())
