"""Subprocess helpers (list-form argv only)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Mapping, Sequence

from .redaction import redact


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


class CommandError(RuntimeError):
    def __init__(self, message: str, *, result: CommandResult | None = None) -> None:
        super().__init__(redact(message))
        self.result = result


def run_command(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> CommandResult:
    if not argv:
        raise CommandError("empty command")
    if isinstance(argv, str):
        raise CommandError("command must be a list, not a shell string")
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        input=input_bytes,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    return CommandResult(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=proc.stdout or b"",
        stderr=proc.stderr or b"",
    )


def require_ok(result: CommandResult, *, context: str) -> CommandResult:
    if result.returncode != 0:
        err = redact(
            result.stderr_text.strip() or result.stdout_text.strip() or "no output"
        )
        raise CommandError(
            f"{context} failed (exit {result.returncode}): {err}",
            result=result,
        )
    return result
