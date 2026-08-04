"""Compose command construction for postgres service operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class ComposeTarget:
    project_name: str
    env_file: Path
    compose_files: tuple[Path, ...]
    service: str = "postgres"
    repo_root: Path | None = None

    def __post_init__(self) -> None:
        if not self.project_name or not self.project_name.strip():
            raise ValueError("Compose project name must be non-empty")
        if not self.compose_files:
            raise ValueError("At least one Compose file is required")


def build_compose_argv(target: ComposeTarget, *compose_args: str) -> list[str]:
    argv: list[str] = [
        "docker",
        "compose",
        "--env-file",
        str(target.env_file),
        "--project-name",
        target.project_name,
    ]
    for path in target.compose_files:
        argv.extend(["-f", str(path)])
    argv.extend(compose_args)
    return argv


def build_exec_argv(target: ComposeTarget, *command: str) -> list[str]:
    return build_compose_argv(
        target,
        "exec",
        "-T",
        target.service,
        *command,
    )


def normalize_compose_files(
    files: Sequence[str | Path], *, repo_root: Path
) -> tuple[Path, ...]:
    out: list[Path] = []
    for item in files:
        path = Path(item)
        if not path.is_absolute():
            path = (repo_root / path).resolve()
        else:
            path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Compose file not found: {path}")
        out.append(path)
    return tuple(out)
