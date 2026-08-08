"""Read-only deployment planner (PRD1C3B1).

Computes a canonical migration-aware deploy plan without mutating the
database, containers, deployment journal, or release artifacts.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import STAGING_PROJECT
from .c2_constants import SOURCE_DIRNAME
from .c3b1_constants import COMPATIBILITY_REL_PATH, DEPLOY_PLAN_SCHEMA_VERSION
from .db_heads import (
    DbHeadsError,
    database_heads_via_postgres,
    target_heads_from_release_source,
)
from .deploy_plan_project import DeployPlanProjectError, assert_deploy_plan_project
from .deploy_root import DeployRootError, release_dir, validate_deploy_root
from .env_file import EnvFileError, load_env_file, require_keys
from .application_compatibility import DatabaseDriftError
from .current_state import CurrentStateError, load_and_resolve_current_state
from .image_identity import ImageIdentityError, assert_release_images_present
from .journal import JournalError, sha256_file
from .manifest import ManifestError, load_manifest, manifest_path
from .migration_compatibility import (
    CompatibilityError,
    compatibility_contract_sha256,
    find_matching_transition,
    load_compatibility_contract,
)
from .password import PasswordError, validate_staging_password
from .preflight import REQUIRED_ENV
from .redact import redact
from .revision import RevisionError, validate_full_sha
from .roots import RootPathError, validate_operator_root
from .source_contract import (
    assert_deploy_plan_target_contract,
)
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


class DeployPlanError(RuntimeError):
    """Read-only deployment planning failure."""


@dataclass(frozen=True)
class DeployPlanInput:
    project_name: str
    env_file: Path
    compose_files: tuple[Path, ...]
    expected_revision: str
    repo_root: Path
    deploy_root: Path
    backup_root: Path | None = None


@dataclass(frozen=True)
class DeployPlanResult:
    ok: bool
    plan_json: str | None
    messages: tuple[str, ...]


def _assert_allowed_project(name: str) -> None:
    if name == STAGING_PROJECT:
        return
    try:
        assert_deploy_plan_project(name)
    except DeployPlanProjectError as exc:
        raise DeployPlanError(
            f"project name must be {STAGING_PROJECT!r} or a validated "
            f"fetchnow-deploy-plan-test-* name, got {name!r}"
        ) from exc


def _compatibility_path(release_directory: Path) -> Path:
    return release_directory / SOURCE_DIRNAME / COMPATIBILITY_REL_PATH


def _canonical_plan(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def run_deploy_plan(inp: DeployPlanInput) -> DeployPlanResult:
    diagnostics: list[str] = []
    try:
        _assert_allowed_project(inp.project_name)
        if not inp.compose_files:
            raise DeployPlanError("at least one Compose file is required")

        env = load_env_file(inp.env_file)
        require_keys(env, REQUIRED_ENV)
        if env.get("COMPOSE_PROJECT_NAME") != inp.project_name:
            raise DeployPlanError(
                "COMPOSE_PROJECT_NAME must match --project-name "
                f"({inp.project_name!r})"
            )

        target_revision = validate_full_sha(inp.expected_revision)
        env_rev = validate_full_sha(env["FETCHNOW_RELEASE_REVISION"])
        if target_revision != env_rev:
            raise DeployPlanError(
                "expected revision does not match FETCHNOW_RELEASE_REVISION in env file"
            )
        validate_staging_password(env["POSTGRES_PASSWORD"])

        deploy_root = validate_deploy_root(
            inp.deploy_root.expanduser().resolve(), repo_root=inp.repo_root
        )
        from .migration_journal import find_unresolved_migrations

        unresolved_migrations = find_unresolved_migrations(deploy_root)
        if unresolved_migrations:
            raise DeployPlanError(
                "unresolved migration journal(s) block deployment mutation: "
                + ", ".join(unresolved_migrations)
                + "; resolve with recover-migration before rollout"
            )
        backup_root = Path(inp.backup_root or env["FETCHNOW_BACKUP_ROOT"])
        validate_operator_root(
            backup_root, repo_root=inp.repo_root, label="backup root"
        )

        target_release = release_dir(deploy_root, target_revision)
        verified = verify_prepared_release(
            target_release,
            expected_revision=target_revision,
            repo_root=inp.repo_root,
        )
        if not verified.ok:
            raise DeployPlanError(verified.messages[0])
        target_manifest = load_manifest(manifest_path(target_release))
        try:
            assert_deploy_plan_target_contract(target_manifest.source_contract_version)
        except ValueError as exc:
            raise DeployPlanError(str(exc)) from exc

        current = load_and_resolve_current_state(
            deploy_root, repo_root=inp.repo_root
        )
        if current is None:
            raise DeployPlanError(
                "current.json is absent — initial deployment/bootstrap planning is "
                "PRD1D; deploy-plan supports upgrade planning only when current "
                "application state is already published"
            )
        current_revision = validate_full_sha(current.application.revision)
        current_release = release_dir(deploy_root, current_revision)
        if not current_release.is_dir():
            raise DeployPlanError(
                f"current release directory missing: {current_release}"
            )
        current_manifest = load_manifest(manifest_path(current_release))
        if (
            sha256_file(manifest_path(current_release))
            != current.application.release_manifest_sha256
        ):
            raise DeployPlanError("current.json manifest hash mismatch")
        assert_release_images_present(current_manifest)
        current_target_release = verify_prepared_release(
            current_release,
            expected_revision=current_revision,
            repo_root=inp.repo_root,
        )
        if not current_target_release.ok:
            raise DeployPlanError(current_target_release.messages[0])

        schema_release = release_dir(
            deploy_root, current.database.schema_release_revision
        )
        schema_verified = verify_prepared_release(
            schema_release,
            expected_revision=current.database.schema_release_revision,
            repo_root=inp.repo_root,
        )
        if not schema_verified.ok:
            raise DeployPlanError(schema_verified.messages[0])

        cwd = target_release / SOURCE_DIRNAME
        database_heads = database_heads_via_postgres(
            project_name=inp.project_name,
            env_file=inp.env_file,
            compose_files=inp.compose_files,
            cwd=cwd,
        )
        saved_heads = frozenset(current.database.heads)
        if database_heads != saved_heads:
            raise DeployPlanError(
                "live database Alembic heads "
                f"{sorted(database_heads)} drift from saved current.database.heads "
                f"{sorted(saved_heads)}"
            )
        target_heads = target_heads_from_release_source(target_release)
        versions_dir = cwd / "backend" / "migrations" / "versions"
        schema_versions_dir = (
            schema_release / SOURCE_DIRNAME / "backend" / "migrations" / "versions"
        )
        current_graph = build_alembic_graph(schema_versions_dir)
        graph = build_alembic_graph(versions_dir)
        planning_graph = merge_alembic_graphs(current_graph, graph)

        contract_path = _compatibility_path(target_release)
        contract = load_compatibility_contract(contract_path)
        contract_hash = compatibility_contract_sha256(contract_path)

        current_heads_sorted = tuple(sorted(database_heads))
        target_heads_sorted = tuple(sorted(target_heads))
        relation = planning_graph.classify_transition(database_heads, target_heads)

        if relation == "downgrade":
            raise DeployPlanError(
                "target release heads are behind current database heads; "
                "database downgrade is forbidden"
            )
        if relation == "divergent":
            raise DeployPlanError(
                "target release heads are divergent or unrelated to current database heads"
            )

        included = frozenset()
        migration_required = relation == "forward"
        if migration_required:
            included = graph.included_revisions(database_heads, target_heads)
            if not included:
                raise DeployPlanError(
                    "forward migration required but computed included revisions is empty"
                )
        for head in database_heads:
            validate_revision_id(head, field="database head")

        previous_application_rollback_allowed = True
        if migration_required:
            transition = find_matching_transition(
                contract,
                from_heads=database_heads,
                to_heads=target_heads,
                included_revisions=included,
            )
            previous_application_rollback_allowed = (
                transition.previous_application_rollback_compatible
            )
        elif relation != "equal":
            raise DeployPlanError(f"unexpected head relation: {relation}")

        application_rollout_required = target_revision != current_revision

        plan = {
            "schema_version": DEPLOY_PLAN_SCHEMA_VERSION,
            "project": inp.project_name,
            "current_revision": current_revision,
            "target_revision": target_revision,
            "current_db_heads": list(current_heads_sorted),
            "target_db_heads": list(target_heads_sorted),
            "migration_required": migration_required,
            "included_revisions": list(sorted(included)),
            "compatibility_contract_sha256": contract_hash,
            "verified_backup_required": migration_required,
            "previous_application_rollback_allowed": previous_application_rollback_allowed,
            "database_downgrade_allowed": False,
            "application_rollout_required": application_rollout_required,
        }
        plan_json = _canonical_plan(plan)
        diagnostics.append("OK: deploy plan computed")
        return DeployPlanResult(ok=True, plan_json=plan_json, messages=tuple(diagnostics))
    except (
        DeployPlanError,
        EnvFileError,
        RevisionError,
        PasswordError,
        DeployRootError,
        RootPathError,
        JournalError,
        ManifestError,
        ImageIdentityError,
        DbHeadsError,
        CompatibilityError,
        CurrentStateError,
        DatabaseDriftError,
        AlembicGraphError,
        ReleaseVerifyError,
        OSError,
        ValueError,
    ) as exc:
        return DeployPlanResult(
            ok=False,
            plan_json=None,
            messages=(f"FAIL: {redact(str(exc))}",),
        )


def emit_deploy_plan(result: DeployPlanResult) -> int:
    for line in result.messages:
        print(line, file=sys.stderr)
    if result.ok and result.plan_json is not None:
        sys.stdout.write(result.plan_json)
        return 0
    return 1
