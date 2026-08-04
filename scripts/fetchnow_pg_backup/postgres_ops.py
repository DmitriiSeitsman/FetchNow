"""PostgreSQL operations via Compose exec into the postgres service."""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import RESTORE_DB_PREFIX
from .compose_target import ComposeTarget, build_compose_argv, build_exec_argv
from .process_util import CommandError, CommandResult, require_ok, run_command
from .redaction import redact


RunFn = Callable[..., CommandResult]


@dataclass(frozen=True)
class PostgresVersions:
    server: str
    pg_dump: str
    pg_restore: str


def _run(
    argv: list[str],
    *,
    cwd: Path | None,
    runner: RunFn,
) -> CommandResult:
    return runner(argv, cwd=str(cwd) if cwd else None)


def compose_ps_postgres_running(
    target: ComposeTarget, *, runner: RunFn = run_command
) -> None:
    argv = build_compose_argv(target, "ps", "--status", "running", "-q", target.service)
    result = require_ok(
        _run(argv, cwd=target.repo_root, runner=runner),
        context="compose ps",
    )
    if not result.stdout_text.strip():
        raise CommandError(
            f"postgres service is not running in project {target.project_name!r}"
        )


def compose_postgres_healthy(
    target: ComposeTarget, *, runner: RunFn = run_command
) -> None:
    # pg_isready inside container using env credentials (no password on argv).
    argv = build_exec_argv(
        target,
        "sh",
        "-c",
        'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
    )
    require_ok(_run(argv, cwd=target.repo_root, runner=runner), context="pg_isready")


def require_utilities(target: ComposeTarget, *, runner: RunFn = run_command) -> None:
    argv = build_exec_argv(
        target,
        "sh",
        "-c",
        "command -v pg_dump && command -v pg_restore && command -v psql "
        "&& command -v createdb && command -v dropdb",
    )
    require_ok(_run(argv, cwd=target.repo_root, runner=runner), context="utility check")


def read_versions(
    target: ComposeTarget, *, runner: RunFn = run_command
) -> PostgresVersions:
    argv = build_exec_argv(
        target,
        "sh",
        "-c",
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SHOW server_version;" '
        "&& pg_dump --version && pg_restore --version",
    )
    result = require_ok(
        _run(argv, cwd=target.repo_root, runner=runner), context="version probe"
    )
    lines = [ln.strip() for ln in result.stdout_text.splitlines() if ln.strip()]
    if len(lines) < 3:
        raise CommandError(
            f"unexpected version probe output: {redact(result.stdout_text)}"
        )
    server = lines[0]
    dump_m = re.search(r"(\d+\.\d+)", lines[1])
    restore_m = re.search(r"(\d+\.\d+)", lines[2])
    if not dump_m or not restore_m:
        raise CommandError(
            f"could not parse tool versions: {redact(result.stdout_text)}"
        )
    return PostgresVersions(
        server=server,
        pg_dump=dump_m.group(1),
        pg_restore=restore_m.group(1),
    )


def read_database_name(target: ComposeTarget, *, runner: RunFn = run_command) -> str:
    argv = build_exec_argv(
        target,
        "sh",
        "-c",
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT current_database();"',
    )
    result = require_ok(
        _run(argv, cwd=target.repo_root, runner=runner), context="current_database"
    )
    name = result.stdout_text.strip()
    if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise CommandError(f"unexpected database name: {redact(name)!r}")
    return name


def read_database_size_bytes(
    target: ComposeTarget, *, runner: RunFn = run_command
) -> int:
    argv = build_exec_argv(
        target,
        "sh",
        "-c",
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc '
        '"SELECT pg_database_size(current_database());"',
    )
    result = require_ok(
        _run(argv, cwd=target.repo_root, runner=runner), context="pg_database_size"
    )
    text = result.stdout_text.strip()
    if not text.isdigit():
        raise CommandError(f"unexpected database size: {redact(text)!r}")
    return int(text)


def _validate_compression(compression: str) -> str:
    if not re.fullmatch(r"(?:gzip|lz4|zstd):\d+|\d+", compression):
        raise CommandError(f"unsupported compression spec: {compression!r}")
    return compression


def dump_to_file(
    target: ComposeTarget,
    *,
    dump_path: Path,
    compression: str,
    runner: RunFn | None = None,
) -> None:
    """Stream pg_dump stdout directly to dump_path (never buffer whole archive)."""
    compression = _validate_compression(compression)
    # Use Popen for true streaming; tests may inject a runner that writes bytes.
    argv = build_exec_argv(
        target,
        "sh",
        "-c",
        'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
        f"-Fc --compress={compression} --no-owner --no-privileges",
    )
    if runner is not None:
        # Test seam: runner returns stdout bytes of the dump.
        result = runner(argv, cwd=str(target.repo_root) if target.repo_root else None)
        if result.returncode != 0:
            raise CommandError(
                f"pg_dump failed: {redact(result.stderr_text)}",
                result=result,
            )
        dump_path.write_bytes(result.stdout)
        return

    import subprocess

    with dump_path.open("wb") as out:
        proc = subprocess.Popen(
            argv,
            cwd=str(target.repo_root) if target.repo_root else None,
            stdout=out,
            stderr=subprocess.PIPE,
        )
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise CommandError(
                f"pg_dump failed: {redact((stderr or b'').decode('utf-8', errors='replace'))}"
            )


def pg_restore_list(
    target: ComposeTarget,
    *,
    dump_host_path: Path,
    runner: RunFn = run_command,
) -> tuple[int, str]:
    """Validate archive via pg_restore -l inside container using stdin stream."""
    data = dump_host_path.read_bytes()
    # Feed archive via stdin to avoid bind-mounting backup dirs into the container.
    argv = build_exec_argv(target, "pg_restore", "-l")
    result = runner(
        argv,
        cwd=str(target.repo_root) if target.repo_root else None,
        input_bytes=data,
    )
    if result.returncode != 0:
        raise CommandError(
            f"pg_restore -l failed: {redact(result.stderr_text)}",
            result=result,
        )
    text = result.stdout_text
    # Count non-comment TOC entries.
    entries = [
        ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith(";")
    ]
    if not entries:
        raise CommandError("pg_restore -l produced no TOC entries")
    return len(entries), text


def generate_restore_database_name(*, source_database: str) -> str:
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(12))
    name = f"{RESTORE_DB_PREFIX}{suffix}"
    if name == source_database:
        raise RuntimeError("generated restore DB collided with source database name")
    if not name.startswith(RESTORE_DB_PREFIX):
        raise RuntimeError("generated restore DB missing required prefix")
    return name


def assert_safe_temp_database_name(name: str, *, source_database: str) -> str:
    if name == source_database:
        raise CommandError(
            "refusing to use source application database as restore target"
        )
    if not name.startswith(RESTORE_DB_PREFIX):
        raise CommandError(
            f"restore database must start with {RESTORE_DB_PREFIX!r}, got {name!r}"
        )
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise CommandError(f"unsafe restore database name: {name!r}")
    return name


def create_database(
    target: ComposeTarget,
    database: str,
    *,
    runner: RunFn = run_command,
) -> None:
    source = read_database_name(target, runner=runner)
    assert_safe_temp_database_name(database, source_database=source)
    argv = build_exec_argv(
        target,
        "sh",
        "-c",
        f'createdb -U "$POSTGRES_USER" -- {database}',
    )
    require_ok(_run(argv, cwd=target.repo_root, runner=runner), context="createdb")


def drop_database(
    target: ComposeTarget,
    database: str,
    *,
    runner: RunFn = run_command,
    source_database: str,
) -> None:
    assert_safe_temp_database_name(database, source_database=source_database)
    # Terminate backends only for the exact temp DB, then drop.
    sql = (
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{database}' AND pid <> pg_backend_pid();"
    )
    term = build_exec_argv(
        target,
        "sh",
        "-c",
        f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "{sql}"',
    )
    # Termination may return rows; ignore non-fatal empty.
    _run(term, cwd=target.repo_root, runner=runner)
    drop = build_exec_argv(
        target,
        "sh",
        "-c",
        f'dropdb -U "$POSTGRES_USER" --if-exists {database}',
    )
    require_ok(_run(drop, cwd=target.repo_root, runner=runner), context="dropdb")


def restore_archive_to_database(
    target: ComposeTarget,
    *,
    dump_host_path: Path,
    database: str,
    source_database: str,
    runner: RunFn = run_command,
) -> None:
    assert_safe_temp_database_name(database, source_database=source_database)
    data = dump_host_path.read_bytes()
    argv = build_exec_argv(
        target,
        "pg_restore",
        "-U",
        # Avoid putting password on argv; use container default user via env in shell form.
        # pg_restore needs -d database. Use sh -c with env user.
    )
    # Prefer shell so POSTGRES_USER is expanded inside the container.
    argv = build_exec_argv(
        target,
        "sh",
        "-c",
        f'pg_restore -U "$POSTGRES_USER" -d {database} --exit-on-error --no-owner --no-privileges',
    )
    result = runner(
        argv,
        cwd=str(target.repo_root) if target.repo_root else None,
        input_bytes=data,
    )
    if result.returncode != 0:
        raise CommandError(
            f"pg_restore failed: {redact(result.stderr_text)}",
            result=result,
        )


def psql_on_database(
    target: ComposeTarget,
    database: str,
    sql: str,
    *,
    source_database: str,
    runner: RunFn = run_command,
) -> str:
    assert_safe_temp_database_name(database, source_database=source_database)
    # ON_ERROR_STOP ensures first SQL error fails the script.
    argv = build_exec_argv(
        target,
        "sh",
        "-c",
        f'psql -U "$POSTGRES_USER" -d {database} -v ON_ERROR_STOP=1 -At',
    )
    result = runner(
        argv,
        cwd=str(target.repo_root) if target.repo_root else None,
        input_bytes=sql.encode("utf-8"),
    )
    if result.returncode != 0:
        raise CommandError(
            f"psql semantic check failed: {redact(result.stderr_text)}",
            result=result,
        )
    return result.stdout_text
