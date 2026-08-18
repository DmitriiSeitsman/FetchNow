"""Read-only deployment planner (PRD1C3B1).

Computes a canonical migration-aware deploy plan without mutating the
database, containers, deployment journal, or release artifacts.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import PRODUCTION_PROJECT, STAGING_PROJECT
from .bootstrap_freshness import (
    CLASS_INITIALIZED,
    BootstrapFreshnessError,
    inspect_bootstrap_freshness,
)
from .bootstrap_journal import (
    BootstrapIdentity,
    BootstrapJournalError,
    find_matching_committed_bootstrap,
    find_unresolved_bootstraps,
)
from .bootstrap_project import BootstrapProjectError, assert_bootstrap_project
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
from .environment import (
    assert_real_environment_bundle,
    real_identity,
    resolve_runtime_overlay,
    snapshot_compose_files_for_release,
)
from .application_compatibility import DatabaseDriftError
from .current_state import CurrentStateError, load_and_resolve_current_state
from .image_identity import ImageIdentityError, assert_release_images_present
from .journal import JournalError, find_unresolved_deployments, sha256_file
from .manifest import ManifestError, load_manifest, manifest_path
from .migration_compatibility import (
    CompatibilityError,
    compatibility_contract_sha256,
    find_matching_transition,
    load_compatibility_contract,
)
from .migration_journal import find_unresolved_migrations
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
    if name == STAGING_PROJECT or name == PRODUCTION_PROJECT:
        return
    try:
        assert_deploy_plan_project(name)
        return
    except DeployPlanProjectError:
        pass
    try:
        assert_bootstrap_project(name)
    except BootstrapProjectError as exc:
        raise DeployPlanError(
            f"project name must be {STAGING_PROJECT!r}, {PRODUCTION_PROJECT!r}, "
            f"a validated fetchnow-deploy-plan-test-* name, "
            f"or a validated fetchnow-bootstrap-test-* name, got {name!r}"
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
        assert_real_environment_bundle(
            project_name=inp.project_name,
            env=env,
            env_file=inp.env_file,
            compose_files=inp.compose_files,
            deploy_root=inp.deploy_root,
            cli_backup_root=inp.backup_root,
            require_cli_backup_root=False,
            require_deploy_root=real_identity(inp.project_name) is not None,
        )

        deploy_root = validate_deploy_root(
            inp.deploy_root.expanduser().resolve(), repo_root=inp.repo_root
        )
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
            resolve_runtime_overlay(
                project_name=inp.project_name,
                source_contract_version=target_manifest.source_contract_version,
                compose_overlay=target_manifest.compose_overlay,
            )
        except ValueError as exc:
            raise DeployPlanError(str(exc)) from exc

        current = load_and_resolve_current_state(
            deploy_root, repo_root=inp.repo_root
        )
        if current is None:
            return _plan_initial_bootstrap(
                inp,
                deploy_root=deploy_root,
                target_revision=target_revision,
                target_release=target_release,
                target_manifest=target_manifest,
                diagnostics=diagnostics,
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
        snapshot_files = snapshot_compose_files_for_release(
            target_release,
            project_name=inp.project_name,
            manifest=target_manifest,
        )
        database_heads = database_heads_via_postgres(
            project_name=inp.project_name,
            env_file=inp.env_file,
            compose_files=snapshot_files,
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
        BootstrapFreshnessError,
        BootstrapJournalError,
        OSError,
        ValueError,
    ) as exc:
        return DeployPlanResult(
            ok=False,
            plan_json=None,
            messages=(f"FAIL: {redact(str(exc))}",),
        )


def _plan_initial_bootstrap(
    inp: DeployPlanInput,
    *,
    deploy_root: Path,
    target_revision: str,
    target_release: Path,
    target_manifest: object,
    diagnostics: list[str],
) -> DeployPlanResult:
    """Read-only initial-bootstrap plan when current.json is absent.

    Does not treat missing current.json as proof of an empty database.
    """
    unresolved_deployments = find_unresolved_deployments(deploy_root)
    if unresolved_deployments:
        raise DeployPlanError(
            "unresolved deployment journal(s) block initial planning: "
            + ", ".join(unresolved_deployments)
        )
    unresolved_bootstraps = find_unresolved_bootstraps(deploy_root)
    if unresolved_bootstraps:
        raise DeployPlanError(
            "unresolved bootstrap journal(s) block initial planning: "
            + ", ".join(unresolved_bootstraps)
        )

    snapshot_files = snapshot_compose_files_for_release(
        target_release,
        project_name=inp.project_name,
        manifest=target_manifest,
    )
    cwd = target_release / SOURCE_DIRNAME
    freshness = inspect_bootstrap_freshness(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=snapshot_files,
        cwd=cwd,
    )
    if freshness.application_services:
        raise DeployPlanError(freshness.reason)

    target_heads = target_heads_from_release_source(target_release)
    if not target_heads:
        raise DeployPlanError("target release has no Alembic heads")
    target_heads_sorted = list(sorted(target_heads))
    contract_version = getattr(target_manifest, "source_contract_version", None)
    if type(contract_version) is not int:
        raise DeployPlanError("target release source_contract_version is required")
    target_ids = assert_release_images_present(target_manifest)
    man_hash = sha256_file(manifest_path(target_release))
    identity = BootstrapIdentity(
        project_name=inp.project_name,
        target_revision=target_revision,
        target_db_heads=tuple(target_heads_sorted),
        release_manifest_sha256=man_hash,
        target_api_image_id=target_ids["api"],
        compose_overlay=snapshot_files[-1].name,
        source_contract_version=contract_version,
    )
    committed = find_matching_committed_bootstrap(deploy_root, identity)

    initial_schema_required = True
    current_db_heads: list[str] = []
    if freshness.classification == CLASS_INITIALIZED:
        live = freshness.initialized_heads or frozenset()
        if committed is None or frozenset(committed.target_db_heads) != live:
            raise DeployPlanError(
                "current.json is absent but the live database already has Alembic "
                f"heads {sorted(live)}; this is not a proven-empty bootstrap"
            )
        if live != target_heads:
            raise DeployPlanError(
                "committed bootstrap journal heads "
                f"{committed.target_db_heads} do not match target release heads "
                f"{target_heads_sorted}"
            )
        initial_schema_required = False
        current_db_heads = list(sorted(live))
        diagnostics.append(
            "OK: initial plan — schema already bootstrapped for this revision"
        )
    elif freshness.safe_for_initial_plan:
        diagnostics.append("OK: initial plan — proven-empty bootstrap environment")
    else:
        raise DeployPlanError(
            "current.json is absent but the environment is not a proven-empty "
            "bootstrap: " + freshness.reason
        )

    contract_path = _compatibility_path(target_release)
    contract = load_compatibility_contract(contract_path)
    contract_hash = compatibility_contract_sha256(contract_path)
    _ = contract

    plan = {
        "schema_version": DEPLOY_PLAN_SCHEMA_VERSION,
        "project": inp.project_name,
        "initial_bootstrap": True,
        "current_revision": None,
        "target_revision": target_revision,
        "current_db_heads": current_db_heads,
        "target_db_heads": target_heads_sorted,
        "initial_schema_required": initial_schema_required,
        "migration_required": False,
        "included_revisions": [],
        "compatibility_contract_sha256": contract_hash,
        "verified_backup_required": False,
        "previous_application_rollback_allowed": False,
        "database_downgrade_allowed": False,
        "application_rollout_required": True,
    }
    plan_json = _canonical_plan(plan)
    diagnostics.append("OK: deploy plan computed")
    return DeployPlanResult(ok=True, plan_json=plan_json, messages=tuple(diagnostics))


def emit_deploy_plan(result: DeployPlanResult) -> int:
    for line in result.messages:
        print(line, file=sys.stderr)
    if result.ok and result.plan_json is not None:
        sys.stdout.write(result.plan_json)
        return 0
    return 1
