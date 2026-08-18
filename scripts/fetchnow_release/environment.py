"""Canonical real-environment identities for release parameterization (PRD1D-B)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .c2_constants import (
    SOURCE_CONTRACT_VERSION_V1,
    SOURCE_CONTRACT_VERSION_V2,
    SOURCE_CONTRACT_VERSION_V3,
    SOURCE_DIRNAME,
)


class EnvironmentError(ValueError):
    """Invalid real-environment identity or overlay selection."""


@dataclass(frozen=True)
class EnvironmentIdentity:
    name: str
    project: str
    overlay: str
    env_filename: str
    deploy_root: Path
    backup_root: Path
    app_env: str
    public_site_url: str


STAGING = EnvironmentIdentity(
    name="staging",
    project="fetchnow-staging",
    overlay="compose.staging.yaml",
    env_filename=".env.staging",
    deploy_root=Path("/srv/fetchnow-staging"),
    backup_root=Path("/srv/fetchnow-staging/backups"),
    app_env="staging",
    public_site_url="https://staging.fetchnow.online",
)

PRODUCTION = EnvironmentIdentity(
    name="production",
    project="fetchnow-production",
    overlay="compose.production.yaml",
    env_filename=".env.production",
    deploy_root=Path("/srv/fetchnow-production"),
    backup_root=Path("/srv/fetchnow-production/backups"),
    app_env="production",
    public_site_url="https://fetchnow.online",
)

REAL_BY_PROJECT: dict[str, EnvironmentIdentity] = {
    STAGING.project: STAGING,
    PRODUCTION.project: PRODUCTION,
}

CANONICAL_OVERLAYS: frozenset[str] = frozenset({STAGING.overlay, PRODUCTION.overlay})
BASE_COMPOSE_NAME = "compose.yaml"
FORBIDDEN_REAL_NAMES: frozenset[str] = frozenset(
    {
        "",
        "fetchnow",
        "fetchnow-prod",
        STAGING.project,
        PRODUCTION.project,
    }
)

_GATEWAY_RE = re.compile(r"^127\.0\.0\.1:([0-9]{1,5})$")


def real_identity(project: str) -> EnvironmentIdentity | None:
    return REAL_BY_PROJECT.get(project)


def is_real_project(project: str) -> bool:
    return project in REAL_BY_PROJECT


def recorded_compose_overlay(
    source_contract_version: int, compose_overlay: str | None
) -> str:
    """Resolve the overlay recorded on a prepared release.

    v1/v2 omit the field and always mean the staging overlay.
    """
    if source_contract_version in {
        SOURCE_CONTRACT_VERSION_V1,
        SOURCE_CONTRACT_VERSION_V2,
    }:
        if compose_overlay not in (None, "", STAGING.overlay):
            raise EnvironmentError(
                "legacy source contract overlay must be "
                f"{STAGING.overlay!r} or omitted, got {compose_overlay!r}"
            )
        return STAGING.overlay
    if source_contract_version != SOURCE_CONTRACT_VERSION_V3:
        raise EnvironmentError(
            f"unsupported source_contract_version: {source_contract_version!r}"
        )
    if compose_overlay not in CANONICAL_OVERLAYS:
        raise EnvironmentError(
            "v3 compose_overlay must be "
            f"{STAGING.overlay!r} or {PRODUCTION.overlay!r}, got {compose_overlay!r}"
        )
    return compose_overlay


def resolve_runtime_overlay(
    *,
    project_name: str,
    source_contract_version: int,
    compose_overlay: str | None,
) -> str:
    """Select the snapshot overlay for a prepared release + project.

    Real production cannot use v1/v2 snapshots. Real v3 overlay must match
    the canonical identity. Ephemeral v3 uses the recorded overlay.
    """
    recorded = recorded_compose_overlay(source_contract_version, compose_overlay)
    identity = real_identity(project_name)
    if identity is PRODUCTION and source_contract_version < SOURCE_CONTRACT_VERSION_V3:
        raise EnvironmentError(
            "production cannot use a v1/v2 prepared release; "
            "the production overlay is not part of the hashed legacy contract"
        )
    if identity is not None:
        if recorded != identity.overlay:
            raise EnvironmentError(
                f"manifest compose_overlay {recorded!r} does not match "
                f"real environment {identity.project!r} "
                f"(expected {identity.overlay!r})"
            )
        return identity.overlay
    return recorded


def overlay_from_compose_files(compose_files: tuple[Path, ...]) -> str:
    """Extract the single canonical overlay basename from a CLI file set."""
    if not compose_files:
        raise EnvironmentError("at least one Compose file is required")
    names = [path.name for path in compose_files]
    if BASE_COMPOSE_NAME not in names:
        raise EnvironmentError(
            f"compose file set must include {BASE_COMPOSE_NAME}"
        )
    overlays = [name for name in names if name in CANONICAL_OVERLAYS]
    if len(overlays) != 1:
        raise EnvironmentError(
            "compose file set must include exactly one canonical overlay "
            f"({STAGING.overlay} or {PRODUCTION.overlay}), got {names}"
        )
    return overlays[0]


def snapshot_compose_files_for_release(
    release_directory: Path,
    *,
    project_name: str,
    manifest: object,
) -> tuple[Path, ...]:
    """Resolve snapshot compose files from a loaded (or test-double) manifest."""
    version = getattr(manifest, "source_contract_version", 1)
    recorded = getattr(manifest, "compose_overlay", None)
    if type(version) is not int:
        version = 1
    if recorded is not None and type(recorded) is not str:
        recorded = None
    overlay = resolve_runtime_overlay(
        project_name=project_name,
        source_contract_version=version,
        compose_overlay=recorded,
    )
    return snapshot_compose_files(release_directory, overlay=overlay)


def snapshot_compose_files(
    release_directory: Path, *, overlay: str
) -> tuple[Path, ...]:
    """Compose files from an immutable prepared snapshot (never the live checkout)."""
    if overlay not in CANONICAL_OVERLAYS:
        raise EnvironmentError(f"refusing unknown snapshot overlay: {overlay!r}")
    source = release_directory / SOURCE_DIRNAME
    compose = (source / BASE_COMPOSE_NAME).resolve()
    overlay_path = (source / overlay).resolve()
    if not compose.is_file():
        raise EnvironmentError(f"snapshot missing {BASE_COMPOSE_NAME}: {compose}")
    if not overlay_path.is_file():
        raise EnvironmentError(f"snapshot missing overlay {overlay!r}: {overlay_path}")
    try:
        compose.relative_to(source.resolve())
        overlay_path.relative_to(source.resolve())
    except ValueError as exc:
        raise EnvironmentError("snapshot compose path escaped source/") from exc
    return (compose, overlay_path)


def assert_loopback_gateway_port(value: str) -> int:
    """Require GATEWAY_PORT exactly ``127.0.0.1:<1-65535>`` with no extra text."""
    raw = value.strip()
    match = _GATEWAY_RE.fullmatch(raw)
    if match is None:
        raise EnvironmentError(
            "GATEWAY_PORT must be loopback in the form 127.0.0.1:<port>"
        )
    digits = match.group(1)
    port = int(digits)
    if digits != str(port) or port < 1 or port > 65535:
        raise EnvironmentError(
            "GATEWAY_PORT loopback port must be an integer 1..65535"
        )
    return port


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _roots_equal(left: Path, right: Path) -> bool:
    return _resolved(left) == _resolved(right)


def assert_real_environment_bundle(
    *,
    project_name: str,
    env: dict[str, str],
    env_file: Path,
    compose_files: tuple[Path, ...] | None = None,
    deploy_root: Path | None = None,
    cli_backup_root: Path | None = None,
    require_cli_backup_root: bool = False,
    require_deploy_root: bool = True,
) -> EnvironmentIdentity | None:
    """Validate real-environment bindings. Ephemeral projects return None.

    Staging env files may live at a checkout-relative `.env.staging`; only the
    basename and env-key identity are required. Roots must still match the
    canonical real paths.

    Env ``FETCHNOW_BACKUP_ROOT`` is always bound to the canonical backup root
    for real environments. A caller-supplied CLI backup root is validated only
    when provided, and is required only when ``require_cli_backup_root`` is set.
    """
    identity = real_identity(project_name)
    overlay: str | None = None
    if compose_files is not None:
        overlay = overlay_from_compose_files(compose_files)
        if identity is None and overlay not in CANONICAL_OVERLAYS:
            raise EnvironmentError(f"unsupported overlay {overlay!r}")
    if identity is None:
        return None

    if env.get("COMPOSE_PROJECT_NAME") != identity.project:
        raise EnvironmentError(
            "COMPOSE_PROJECT_NAME must match requested project "
            f"({identity.project!r})"
        )
    env_name = env_file.name
    if env_name != identity.env_filename:
        raise EnvironmentError(
            f"env file basename {env_name!r} does not match "
            f"{identity.env_filename!r} for {identity.project}"
        )
    other_names = {
        other.env_filename
        for other in REAL_BY_PROJECT.values()
        if other.project != identity.project
    }
    if env_name in other_names:
        raise EnvironmentError(
            f"env file {env_name!r} belongs to a different real environment"
        )

    app_env = env.get("APP_ENV", "")
    if app_env != identity.app_env:
        raise EnvironmentError(
            f"APP_ENV must be {identity.app_env!r} for {identity.project}, "
            f"got {app_env!r}"
        )
    site = env.get("PUBLIC_SITE_URL", "")
    if site != identity.public_site_url:
        raise EnvironmentError(
            f"PUBLIC_SITE_URL must be {identity.public_site_url!r} for "
            f"{identity.project}, got {site!r}"
        )

    if overlay is not None and overlay != identity.overlay:
        raise EnvironmentError(
            f"overlay {overlay!r} does not match {identity.project} "
            f"(expected {identity.overlay!r})"
        )

    env_deploy = Path(env.get("FETCHNOW_DEPLOY_ROOT") or "")
    if not str(env_deploy):
        raise EnvironmentError("FETCHNOW_DEPLOY_ROOT is required")
    if not _roots_equal(env_deploy, identity.deploy_root):
        raise EnvironmentError(
            f"FETCHNOW_DEPLOY_ROOT must be {identity.deploy_root} for "
            f"{identity.project}, got {env_deploy}"
        )
    if require_deploy_root and deploy_root is None:
        raise EnvironmentError(
            f"CLI deploy root is required for real environment {identity.project}"
        )
    if deploy_root is not None and not _roots_equal(deploy_root, identity.deploy_root):
        raise EnvironmentError(
            f"CLI deploy root must be {identity.deploy_root} for "
            f"{identity.project}, got {deploy_root}"
        )
    if deploy_root is not None and not _roots_equal(deploy_root, env_deploy):
        raise EnvironmentError(
            "CLI deploy root must equal FETCHNOW_DEPLOY_ROOT for real environments"
        )

    env_backup_raw = env.get("FETCHNOW_BACKUP_ROOT") or ""
    if not str(env_backup_raw):
        raise EnvironmentError("FETCHNOW_BACKUP_ROOT is required")
    env_backup = Path(env_backup_raw)
    if not _roots_equal(env_backup, identity.backup_root):
        raise EnvironmentError(
            f"FETCHNOW_BACKUP_ROOT must be {identity.backup_root} for "
            f"{identity.project}, got {env_backup}"
        )
    if require_cli_backup_root and cli_backup_root is None:
        raise EnvironmentError(
            f"CLI backup root is required for real environment {identity.project}"
        )
    if cli_backup_root is not None:
        if not _roots_equal(cli_backup_root, identity.backup_root):
            raise EnvironmentError(
                f"CLI backup root must be {identity.backup_root} for "
                f"{identity.project}, got {cli_backup_root}"
            )
        if not _roots_equal(cli_backup_root, env_backup):
            raise EnvironmentError(
                "CLI backup root must equal FETCHNOW_BACKUP_ROOT for real environments"
            )
    return identity
