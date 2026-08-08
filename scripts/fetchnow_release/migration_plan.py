"""Typed migration plan computation (PRD1C3B2B2).

Computes a canonical migration plan for verified backup + Alembic execution.
Callers must invoke ``compute_migration_plan`` while holding ``RolloutLock``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import STAGING_PROJECT
from .application_compatibility import (
    CompatibilityError,
    DatabaseDriftError,
    assert_application_compatible_with_database,
    assert_live_matches_saved_database_heads,
)
from .c2_constants import SOURCE_DIRNAME
from .c3b1_constants import COMPATIBILITY_REL_PATH
from .c3b2b2_constants import MIGRATION_PLAN_SCHEMA_VERSION
from .current_state import (
    CurrentStateError,
    CurrentStateV2,
    DatabaseState,
    ResolvedCurrentState,
    assert_schema_release_graph_sha256,
    build_committed_current_state,
    current_path,
    load_and_resolve_current_state,
    next_database_state_after_migration,
)
from .db_heads import (
    DbHeadsError,
    database_heads_via_postgres,
    target_heads_from_release_source,
)
from .deploy_root import DeployRootError, release_dir, validate_deploy_root
from .env_file import EnvFileError, load_env_file, require_keys
from .image_identity import ImageIdentityError, assert_release_images_present
from .journal import JournalError, sha256_file, utc_now
from .manifest import ManifestError, load_manifest, manifest_path
from .migration_compatibility import (
    CompatibilityError as MigrationCompatibilityError,
    compatibility_contract_sha256,
    find_matching_transition,
    load_compatibility_contract,
)
from .migration_journal import MigrationPlanDocument, new_migration_id
from .migration_project import MigrationProjectError, assert_migration_project
from .password import PasswordError, validate_staging_password
from .preflight import REQUIRED_ENV
from .redact import redact
from .revision import RevisionError, validate_full_sha
from .roots import RootPathError, validate_operator_root
from .source_contract import assert_deploy_plan_target_contract
from .verify_release import ReleaseVerifyError, verify_prepared_release

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fetchnow_pg_backup.alembic_graph import (  # noqa: E402
    AlembicGraphError,
    build_alembic_graph,
    merge_alembic_graphs,
    validate_revision_id,
)
from fetchnow_pg_backup.checksums import sha256_bytes  # noqa: E402
from fetchnow_pg_backup.migrations_authority import (  # noqa: E402
    MigrationsAuthorityError,
    migrations_graph_content_sha256,
)


class MigrationPlanError(RuntimeError):
    """Migration planning failure."""


@dataclass(frozen=True)
class MigrationPlanInput:
    project_name: str
    env_file: Path
    compose_files: tuple[Path, ...]
    expected_revision: str
    repo_root: Path
    deploy_root: Path
    backup_root: Path | None = None


@dataclass(frozen=True)
class MigrationPlanComputed:
    ok: bool
    plan: MigrationPlanDocument | None
    already_migrated: bool
    messages: tuple[str, ...]


def _assert_allowed_project(name: str) -> None:
    try:
        assert_migration_project(name, allow_staging=True)
    except MigrationProjectError as exc:
        raise MigrationPlanError(
            f"project name must be {STAGING_PROJECT!r} or a validated "
            f"fetchnow-migration-test-* name, got {name!r}"
        ) from exc


def _compatibility_path(release_directory: Path) -> Path:
    return release_directory / SOURCE_DIRNAME / COMPATIBILITY_REL_PATH


def _canonical_state_json(state: CurrentStateV2) -> str:
    return json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"


def _expected_post_migration_state_sha256(
    *,
    current: ResolvedCurrentState,
    target_revision: str,
    target_heads: frozenset[str],
    migration_id: str,
    compatibility_contract_sha256: str,
) -> str:
    database = next_database_state_after_migration(
        previous=current.database,
        target_revision=target_revision,
        target_heads=target_heads,
        migration_id=migration_id,
        compatibility_contract_sha256=compatibility_contract_sha256,
    )
    expected = build_committed_current_state(
        application=current.application,
        database=database,
        updated_at_utc=current.updated_at_utc,
    )
    return sha256_bytes(_canonical_state_json(expected).encode())


def _backup_root_identity_sha256(resolved: Path) -> str:
    return hashlib.sha256(str(resolved).encode()).hexdigest()


def _database_matches_target(
    database: DatabaseState,
    *,
    target_revision: str,
    target_heads: frozenset[str],
    contract_hash: str,
) -> bool:
    if database.schema_release_revision != target_revision:
        return False
    if frozenset(database.heads) != target_heads:
        return False
    if database.last_migration_id is None:
        return False
    if database.compatibility_contract_sha256 != contract_hash:
        return False
    return True


def compute_migration_plan(inp: MigrationPlanInput) -> MigrationPlanComputed:
    diagnostics: list[str] = []
    try:
        _assert_allowed_project(inp.project_name)
        if not inp.compose_files:
            raise MigrationPlanError("at least one Compose file is required")

        env = load_env_file(inp.env_file)
        require_keys(env, REQUIRED_ENV)
        if env.get("COMPOSE_PROJECT_NAME") != inp.project_name:
            raise MigrationPlanError(
                "COMPOSE_PROJECT_NAME must match --project-name "
                f"({inp.project_name!r})"
            )

        target_revision = validate_full_sha(inp.expected_revision)
        env_rev = validate_full_sha(env["FETCHNOW_RELEASE_REVISION"])
        if target_revision != env_rev:
            raise MigrationPlanError(
                "expected revision does not match FETCHNOW_RELEASE_REVISION in env file"
            )
        validate_staging_password(env["POSTGRES_PASSWORD"])

        deploy_root = validate_deploy_root(
            inp.deploy_root.expanduser().resolve(), repo_root=inp.repo_root
        )
        backup_root = Path(inp.backup_root or env["FETCHNOW_BACKUP_ROOT"])
        validate_operator_root(
            backup_root, repo_root=inp.repo_root, label="backup root"
        )
        resolved_backup_root = backup_root.expanduser().resolve()
        backup_identity = _backup_root_identity_sha256(resolved_backup_root)

        target_release = release_dir(deploy_root, target_revision)
        verified = verify_prepared_release(
            target_release,
            expected_revision=target_revision,
            repo_root=inp.repo_root,
        )
        if not verified.ok:
            raise MigrationPlanError(verified.messages[0])
        target_manifest = load_manifest(manifest_path(target_release))
        try:
            assert_deploy_plan_target_contract(target_manifest.source_contract_version)
        except ValueError as exc:
            raise MigrationPlanError(str(exc)) from exc
        target_ids = assert_release_images_present(target_manifest)
        target_api_image_id = target_ids["api"]

        current = load_and_resolve_current_state(
            deploy_root, repo_root=inp.repo_root
        )
        if current is None:
            raise MigrationPlanError(
                "current.json is absent — migration requires published dual state"
            )

        current_path_file = current_path(deploy_root)
        if not current_path_file.is_file():
            raise MigrationPlanError("current.json missing on disk")
        current_state_bytes = current_path_file.read_bytes()
        current_state_sha256 = sha256_bytes(current_state_bytes)

        current_revision = validate_full_sha(current.application.revision)
        current_release = release_dir(deploy_root, current_revision)
        if not current_release.is_dir():
            raise MigrationPlanError(
                f"current application release directory missing: {current_release}"
            )
        current_manifest = load_manifest(manifest_path(current_release))
        if (
            sha256_file(manifest_path(current_release))
            != current.application.release_manifest_sha256
        ):
            raise MigrationPlanError("current.json manifest hash mismatch")
        assert_release_images_present(current_manifest)
        current_verified = verify_prepared_release(
            current_release,
            expected_revision=current_revision,
            repo_root=inp.repo_root,
        )
        if not current_verified.ok:
            raise MigrationPlanError(current_verified.messages[0])

        source_schema_revision = validate_full_sha(
            current.database.schema_release_revision
        )
        schema_release = release_dir(deploy_root, source_schema_revision)
        schema_verified = verify_prepared_release(
            schema_release,
            expected_revision=source_schema_revision,
            repo_root=inp.repo_root,
        )
        if not schema_verified.ok:
            raise MigrationPlanError(schema_verified.messages[0])

        cwd = current_release / SOURCE_DIRNAME
        database_heads = database_heads_via_postgres(
            project_name=inp.project_name,
            env_file=inp.env_file,
            compose_files=inp.compose_files,
            cwd=cwd,
        )
        saved_heads = frozenset(current.database.heads)
        assert_live_matches_saved_database_heads(
            live_heads=database_heads,
            saved_heads=saved_heads,
        )

        target_heads = target_heads_from_release_source(target_release)
        target_source_heads = target_heads
        assert_application_compatible_with_database(
            target_revision=current.application.revision,
            target_source_heads=target_source_heads,
            database=current.database,
        )

        source_versions_dir = (
            schema_release / SOURCE_DIRNAME / "backend" / "migrations" / "versions"
        )
        target_versions_dir = (
            target_release / SOURCE_DIRNAME / "backend" / "migrations" / "versions"
        )
        source_graph_sha256 = migrations_graph_content_sha256(source_versions_dir)
        target_graph_sha256 = migrations_graph_content_sha256(target_versions_dir)
        assert_schema_release_graph_sha256(
            deploy_root,
            source_schema_revision,
            source_graph_sha256,
            repo_root=inp.repo_root,
        )
        assert_schema_release_graph_sha256(
            deploy_root,
            target_revision,
            target_graph_sha256,
            repo_root=inp.repo_root,
        )

        current_graph = build_alembic_graph(source_versions_dir)
        target_graph = build_alembic_graph(target_versions_dir)
        planning_graph = merge_alembic_graphs(current_graph, target_graph)

        contract_path = _compatibility_path(target_release)
        contract = load_compatibility_contract(contract_path)
        contract_hash = compatibility_contract_sha256(contract_path)

        relation = planning_graph.classify_transition(database_heads, target_heads)
        if relation == "downgrade":
            raise MigrationPlanError(
                "target release heads are behind current database heads; "
                "database downgrade is forbidden"
            )
        if relation == "divergent":
            raise MigrationPlanError(
                "target release heads are divergent or unrelated to current database heads"
            )

        source_heads_sorted = tuple(sorted(database_heads))
        target_heads_sorted = tuple(sorted(target_heads))

        if (
            relation == "equal"
            and database_heads == target_heads
            and _database_matches_target(
                current.database,
                target_revision=target_revision,
                target_heads=target_heads,
                contract_hash=contract_hash,
            )
            and current.application.revision in current.database.compatible_application_revisions
        ):
            diagnostics.extend(
                (
                    "OK: live database heads match target release heads",
                    "OK: saved database state already reflects target schema",
                    "OK: migration already committed (idempotent)",
                )
            )
            return MigrationPlanComputed(
                ok=True,
                plan=None,
                already_migrated=True,
                messages=tuple(diagnostics),
            )

        if relation != "forward":
            raise MigrationPlanError(
                "forward migration required but head relation is "
                f"{relation!r}; saved database state does not match target schema"
            )

        included = target_graph.included_revisions(database_heads, target_heads)
        if not included:
            raise MigrationPlanError(
                "forward migration required but computed included revisions is empty"
            )
        for head in database_heads:
            validate_revision_id(head, field="database head")
        find_matching_transition(
            contract,
            from_heads=database_heads,
            to_heads=target_heads,
            included_revisions=included,
        )

        migration_id = new_migration_id()
        expected_state_sha256 = _expected_post_migration_state_sha256(
            current=current,
            target_revision=target_revision,
            target_heads=target_heads,
            migration_id=migration_id,
            compatibility_contract_sha256=contract_hash,
        )

        plan = MigrationPlanDocument(
            schema_version=MIGRATION_PLAN_SCHEMA_VERSION,
            migration_id=migration_id,
            target_revision=target_revision,
            current_state_sha256=current_state_sha256,
            source_application_revision=current_revision,
            source_application_image_ids=dict(current.application.image_ids),
            source_database_release_revision=source_schema_revision,
            source_database_heads=list(source_heads_sorted),
            source_migrations_graph_sha256=source_graph_sha256,
            target_database_release_revision=target_revision,
            target_database_heads=list(target_heads_sorted),
            target_migrations_graph_sha256=target_graph_sha256,
            included_revisions=list(sorted(included)),
            compatibility_contract_sha256=contract_hash,
            target_api_image_id=target_api_image_id,
            backup_root_identity_sha256=backup_identity,
            expected_post_migration_state_sha256=expected_state_sha256,
            project_name=inp.project_name,
            compose_files=[str(path) for path in inp.compose_files],
            created_at_utc=utc_now(),
        )
        diagnostics.extend(
            (
                "OK: migration plan computed",
                f"migration_id={migration_id}",
                f"included_revisions={list(sorted(included))}",
            )
        )
        return MigrationPlanComputed(
            ok=True,
            plan=plan,
            already_migrated=False,
            messages=tuple(diagnostics),
        )
    except (
        MigrationPlanError,
        EnvFileError,
        RevisionError,
        PasswordError,
        DeployRootError,
        RootPathError,
        JournalError,
        ManifestError,
        ImageIdentityError,
        DbHeadsError,
        MigrationCompatibilityError,
        CompatibilityError,
        DatabaseDriftError,
        CurrentStateError,
        AlembicGraphError,
        MigrationsAuthorityError,
        ReleaseVerifyError,
        OSError,
        ValueError,
    ) as exc:
        return MigrationPlanComputed(
            ok=False,
            plan=None,
            already_migrated=False,
            messages=(f"FAIL: {redact(str(exc))}",),
        )
