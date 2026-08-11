"""Dual application/database current state (PRD1C3B2A).

Parsing is pure JSON validation. Resolution verifies prepared releases and
builds an in-memory dual-state view without rewriting legacy v1 files.
Writers always publish schema v2.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .c2_constants import SOURCE_DIRNAME
from .c3_constants import CURRENT_STATE_NAME, STATE_DIRNAME
from .c3b2a_constants import (
    APPLICATION_STATE_KEYS,
    CURRENT_SCHEMA_VERSION_V1,
    CURRENT_SCHEMA_VERSION_V2,
    CURRENT_STATE_V1_TOP_KEYS,
    CURRENT_STATE_V2_TOP_KEYS,
    DATABASE_STATE_KEYS,
)
from .db_heads import DbHeadsError, target_heads_from_release_source
from .deploy_root import release_dir
from .journal_io import atomic_write_json, read_json
from .revision import RevisionError, validate_full_sha
from .verify_release import verify_prepared_release

_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEPLOYMENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_MANIFEST_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTRACT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ALEMBIC_REV_RE = re.compile(r"^[a-zA-Z0-9_]+$")


class CurrentStateError(ValueError):
    """Invalid current.json parse or resolution failure."""


def _reject_symlink(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise CurrentStateError(f"refusing symlink {label}: {path}")


def validate_image_id_map(ids: dict[str, str]) -> dict[str, str]:
    required = {"api", "worker", "web", "gateway"}
    if set(ids) != required:
        raise CurrentStateError(f"image_ids keys {sorted(ids)} != {sorted(required)}")
    out: dict[str, str] = {}
    for key, value in ids.items():
        if not _IMAGE_ID_RE.fullmatch(value):
            raise CurrentStateError(f"invalid image id for {key}")
        out[key] = value
    if out["api"] != out["worker"]:
        raise CurrentStateError("api and worker image IDs must match")
    return out


def _require_exact_keys(raw: dict[str, Any], expected: frozenset[str], *, label: str) -> None:
    keys = frozenset(raw)
    if keys != expected:
        raise CurrentStateError(
            f"{label} keys {sorted(keys)} != required {sorted(expected)}"
        )


def _parse_exact_int(value: object, *, field: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise CurrentStateError(f"{field} must be an exact integer")
    return value


def _parse_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise CurrentStateError(f"{field} must be a string")
    try:
        return validate_full_sha(value)
    except (RevisionError, ValueError) as exc:
        raise CurrentStateError(f"invalid {field}") from exc


def _parse_manifest_hash(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _MANIFEST_HASH_RE.fullmatch(value):
        raise CurrentStateError(f"{field} must be 64 lowercase hex chars")
    return value


def _parse_deployment_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _DEPLOYMENT_ID_RE.fullmatch(value):
        raise CurrentStateError(f"{field} malformed")
    return value


def _parse_utc(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise CurrentStateError(f"{field} must be canonical UTC timestamp")
    return value


def _parse_alembic_head(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _ALEMBIC_REV_RE.fullmatch(value):
        raise CurrentStateError(f"invalid {field}")
    return value


def _parse_sorted_unique_heads(raw: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise CurrentStateError(f"{field} must be a non-empty list")
    heads = [_parse_alembic_head(item, field=field) for item in raw]
    if len(set(heads)) != len(heads):
        raise CurrentStateError(f"{field} must be unique")
    if tuple(sorted(heads)) != tuple(heads):
        raise CurrentStateError(f"{field} must be sorted")
    return tuple(heads)


def _parse_sorted_unique_revisions(raw: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise CurrentStateError(f"{field} must be a non-empty list")
    revs = [_parse_sha(item, field=field) for item in raw]
    if len(set(revs)) != len(revs):
        raise CurrentStateError(f"{field} must be unique")
    if tuple(sorted(revs)) != tuple(revs):
        raise CurrentStateError(f"{field} must be sorted")
    return tuple(revs)


def _parse_optional_uuid(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _parse_deployment_id(value, field=field)


def _parse_optional_contract_hash(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _CONTRACT_HASH_RE.fullmatch(value):
        raise CurrentStateError(f"{field} must be 64 lowercase hex or null")
    return value


@dataclass(frozen=True)
class ApplicationState:
    revision: str
    release_manifest_sha256: str
    image_ids: dict[str, str]
    deployment_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "release_manifest_sha256": self.release_manifest_sha256,
            "image_ids": dict(self.image_ids),
            "deployment_id": self.deployment_id,
        }


@dataclass(frozen=True)
class DatabaseState:
    heads: tuple[str, ...]
    schema_release_revision: str
    last_migration_id: str | None
    compatibility_contract_sha256: str | None
    compatible_application_revisions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "heads": list(self.heads),
            "schema_release_revision": self.schema_release_revision,
            "last_migration_id": self.last_migration_id,
            "compatibility_contract_sha256": self.compatibility_contract_sha256,
            "compatible_application_revisions": list(
                self.compatible_application_revisions
            ),
        }


@dataclass(frozen=True)
class LegacyCurrentStateV1:
    schema_version: int
    revision: str
    release_manifest_sha256: str
    image_ids: dict[str, str]
    updated_at_utc: str
    deployment_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "release_manifest_sha256": self.release_manifest_sha256,
            "image_ids": dict(self.image_ids),
            "updated_at_utc": self.updated_at_utc,
            "deployment_id": self.deployment_id,
        }


@dataclass(frozen=True)
class CurrentStateV2:
    schema_version: int
    application: ApplicationState
    database: DatabaseState
    updated_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "application": self.application.to_dict(),
            "database": self.database.to_dict(),
            "updated_at_utc": self.updated_at_utc,
        }


@dataclass(frozen=True)
class ResolvedCurrentState:
    """Canonical dual-state view used by rollout, recover, and deploy-plan."""

    schema_version: int
    application: ApplicationState
    database: DatabaseState
    updated_at_utc: str
    legacy_v1: bool

    @property
    def revision(self) -> str:
        return self.application.revision

    @property
    def release_manifest_sha256(self) -> str:
        return self.application.release_manifest_sha256

    @property
    def image_ids(self) -> dict[str, str]:
        return dict(self.application.image_ids)

    @property
    def deployment_id(self) -> str:
        return self.application.deployment_id

    def to_v2(self) -> CurrentStateV2:
        return CurrentStateV2(
            schema_version=CURRENT_SCHEMA_VERSION_V2,
            application=self.application,
            database=self.database,
            updated_at_utc=self.updated_at_utc,
        )


# Backward-compatible name used throughout C3A call sites.
CurrentState = ResolvedCurrentState


def parse_application_state(raw: dict[str, Any]) -> ApplicationState:
    _require_exact_keys(raw, APPLICATION_STATE_KEYS, label="application")
    image_ids = raw["image_ids"]
    if not isinstance(image_ids, dict):
        raise CurrentStateError("application.image_ids must be an object")
    return ApplicationState(
        revision=_parse_sha(raw["revision"], field="application.revision"),
        release_manifest_sha256=_parse_manifest_hash(
            raw["release_manifest_sha256"], field="application.release_manifest_sha256"
        ),
        image_ids=validate_image_id_map(
            {str(k): str(v) for k, v in image_ids.items()}
        ),
        deployment_id=_parse_deployment_id(
            raw["deployment_id"], field="application.deployment_id"
        ),
    )


def parse_database_state(raw: dict[str, Any]) -> DatabaseState:
    _require_exact_keys(raw, DATABASE_STATE_KEYS, label="database")
    heads = _parse_sorted_unique_heads(raw["heads"], field="database.heads")
    schema_release = _parse_sha(
        raw["schema_release_revision"], field="database.schema_release_revision"
    )
    last_migration_id = _parse_optional_uuid(
        raw["last_migration_id"], field="database.last_migration_id"
    )
    contract_hash = _parse_optional_contract_hash(
        raw["compatibility_contract_sha256"],
        field="database.compatibility_contract_sha256",
    )
    if (last_migration_id is None) != (contract_hash is None):
        raise CurrentStateError(
            "database.last_migration_id and compatibility_contract_sha256 "
            "must both be null or both be non-null"
        )
    compatible = _parse_sorted_unique_revisions(
        raw["compatible_application_revisions"],
        field="database.compatible_application_revisions",
    )
    if schema_release not in compatible:
        raise CurrentStateError(
            "database.schema_release_revision must be present in "
            "compatible_application_revisions"
        )
    return DatabaseState(
        heads=heads,
        schema_release_revision=schema_release,
        last_migration_id=last_migration_id,
        compatibility_contract_sha256=contract_hash,
        compatible_application_revisions=compatible,
    )


def parse_legacy_current_state_v1(raw: dict[str, Any]) -> LegacyCurrentStateV1:
    _require_exact_keys(raw, CURRENT_STATE_V1_TOP_KEYS, label="current.json v1")
    schema = _parse_exact_int(raw["schema_version"], field="schema_version")
    if schema != CURRENT_SCHEMA_VERSION_V1:
        raise CurrentStateError(f"unsupported current schema: {schema}")
    return LegacyCurrentStateV1(
        schema_version=schema,
        revision=_parse_sha(raw["revision"], field="revision"),
        release_manifest_sha256=_parse_manifest_hash(
            raw["release_manifest_sha256"], field="release_manifest_sha256"
        ),
        image_ids=validate_image_id_map(
            {str(k): str(v) for k, v in raw["image_ids"].items()}
        ),
        updated_at_utc=_parse_utc(raw["updated_at_utc"], field="updated_at_utc"),
        deployment_id=_parse_deployment_id(
            raw["deployment_id"], field="deployment_id"
        ),
    )


def parse_current_state_v2(raw: dict[str, Any]) -> CurrentStateV2:
    _require_exact_keys(raw, CURRENT_STATE_V2_TOP_KEYS, label="current.json v2")
    schema = _parse_exact_int(raw["schema_version"], field="schema_version")
    if schema != CURRENT_SCHEMA_VERSION_V2:
        raise CurrentStateError(f"unsupported current schema: {schema}")
    if not isinstance(raw["application"], dict) or not isinstance(raw["database"], dict):
        raise CurrentStateError("application and database must be objects")
    application = parse_application_state(raw["application"])
    database = parse_database_state(raw["database"])
    if application.revision not in database.compatible_application_revisions:
        raise CurrentStateError(
            "application.revision must be listed in "
            "database.compatible_application_revisions"
        )
    return CurrentStateV2(
        schema_version=schema,
        application=application,
        database=database,
        updated_at_utc=_parse_utc(raw["updated_at_utc"], field="updated_at_utc"),
    )


def parse_current_state_document(
    raw: dict[str, Any],
) -> LegacyCurrentStateV1 | CurrentStateV2:
    if "schema_version" not in raw:
        raise CurrentStateError("current.json missing schema_version")
    schema = raw["schema_version"]
    if type(schema) is not int or isinstance(schema, bool):
        raise CurrentStateError("schema_version must be an exact integer")
    if schema == CURRENT_SCHEMA_VERSION_V1:
        return parse_legacy_current_state_v1(raw)
    if schema == CURRENT_SCHEMA_VERSION_V2:
        return parse_current_state_v2(raw)
    raise CurrentStateError(f"unsupported current schema: {schema}")


def parse_current_state_bytes(data: bytes) -> LegacyCurrentStateV1 | CurrentStateV2:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentStateError(f"invalid current.json JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CurrentStateError("current.json must be a JSON object")
    return parse_current_state_document(raw)


def current_path(deploy_root: Path) -> Path:
    return deploy_root / STATE_DIRNAME / CURRENT_STATE_NAME


def load_parsed_current_state(
    deploy_root: Path,
) -> LegacyCurrentStateV1 | CurrentStateV2 | None:
    path = current_path(deploy_root)
    if not path.exists():
        return None
    _reject_symlink(path, label="current.json")
    return parse_current_state_document(read_json(path))


def _verify_release_or_raise(
    *,
    deploy_root: Path,
    revision: str,
    repo_root: Path,
    label: str,
) -> Path:
    release = release_dir(deploy_root, revision)
    if release.is_symlink():
        raise CurrentStateError(f"{label} release path is a symlink: {release}")
    if not release.is_dir():
        raise CurrentStateError(f"{label} prepared release missing: {revision}")
    verified = verify_prepared_release(
        release, expected_revision=revision, repo_root=repo_root
    )
    if not verified.ok:
        raise CurrentStateError(
            f"{label} prepared release failed verification: {verified.messages[0]}"
        )
    return release


def resolve_current_state(
    parsed: LegacyCurrentStateV1 | CurrentStateV2,
    *,
    deploy_root: Path,
    repo_root: Path,
) -> ResolvedCurrentState:
    if isinstance(parsed, LegacyCurrentStateV1):
        release = _verify_release_or_raise(
            deploy_root=deploy_root,
            revision=parsed.revision,
            repo_root=repo_root,
            label="legacy current",
        )
        try:
            heads = target_heads_from_release_source(release)
        except DbHeadsError as exc:
            raise CurrentStateError(
                f"legacy current release Alembic graph unusable: {exc}"
            ) from exc
        if not heads:
            raise CurrentStateError("legacy current release has empty Alembic heads")
        database = DatabaseState(
            heads=tuple(sorted(heads)),
            schema_release_revision=parsed.revision,
            last_migration_id=None,
            compatibility_contract_sha256=None,
            compatible_application_revisions=(parsed.revision,),
        )
        application = ApplicationState(
            revision=parsed.revision,
            release_manifest_sha256=parsed.release_manifest_sha256,
            image_ids=dict(parsed.image_ids),
            deployment_id=parsed.deployment_id,
        )
        return ResolvedCurrentState(
            schema_version=CURRENT_SCHEMA_VERSION_V1,
            application=application,
            database=database,
            updated_at_utc=parsed.updated_at_utc,
            legacy_v1=True,
        )

    _verify_release_or_raise(
        deploy_root=deploy_root,
        revision=parsed.application.revision,
        repo_root=repo_root,
        label="application",
    )
    schema_release = _verify_release_or_raise(
        deploy_root=deploy_root,
        revision=parsed.database.schema_release_revision,
        repo_root=repo_root,
        label="database schema",
    )
    try:
        static_heads = target_heads_from_release_source(schema_release)
    except DbHeadsError as exc:
        raise CurrentStateError(
            f"database schema release Alembic graph unusable: {exc}"
        ) from exc
    saved = frozenset(parsed.database.heads)
    if static_heads != saved:
        raise CurrentStateError(
            "database.schema_release_revision static heads "
            f"{sorted(static_heads)} do not equal saved database heads "
            f"{sorted(saved)}"
        )
    return ResolvedCurrentState(
        schema_version=CURRENT_SCHEMA_VERSION_V2,
        application=parsed.application,
        database=parsed.database,
        updated_at_utc=parsed.updated_at_utc,
        legacy_v1=False,
    )


def load_current_state(
    deploy_root: Path,
    *,
    repo_root: Path | None = None,
) -> ResolvedCurrentState | None:
    """Load and resolve current.json.

    Callers that mutate or plan must pass ``repo_root`` so legacy v1 and v2
    are fully resolved against prepared releases.
    """
    parsed = load_parsed_current_state(deploy_root)
    if parsed is None:
        return None
    if repo_root is None:
        if isinstance(parsed, LegacyCurrentStateV1):
            raise CurrentStateError(
                "legacy current.json requires repo_root for dual-state resolution"
            )
        return ResolvedCurrentState(
            schema_version=parsed.schema_version,
            application=parsed.application,
            database=parsed.database,
            updated_at_utc=parsed.updated_at_utc,
            legacy_v1=False,
        )
    return resolve_current_state(parsed, deploy_root=deploy_root, repo_root=repo_root)


def load_and_resolve_current_state(
    deploy_root: Path,
    *,
    repo_root: Path,
) -> ResolvedCurrentState | None:
    return load_current_state(deploy_root, repo_root=repo_root)


def build_bootstrap_database_state(
    *,
    heads: frozenset[str] | tuple[str, ...],
    schema_release_revision: str,
) -> DatabaseState:
    rev = validate_full_sha(schema_release_revision)
    sorted_heads = tuple(sorted(heads))
    if not sorted_heads:
        raise CurrentStateError("bootstrap database heads must be non-empty")
    return DatabaseState(
        heads=sorted_heads,
        schema_release_revision=rev,
        last_migration_id=None,
        compatibility_contract_sha256=None,
        compatible_application_revisions=(rev,),
    )


def schema_release_migrations_versions_dir(
    deploy_root: Path,
    schema_release_revision: str,
    *,
    repo_root: Path,
) -> Path:
    """Return the authoritative migrations/versions directory for a schema release."""
    rev = validate_full_sha(schema_release_revision)
    release = release_dir(deploy_root, rev)
    verified = verify_prepared_release(
        release, expected_revision=rev, repo_root=repo_root
    )
    if not verified.ok:
        raise CurrentStateError(verified.messages[0])
    versions = release / SOURCE_DIRNAME / "backend" / "migrations" / "versions"
    if not versions.is_dir():
        raise CurrentStateError(
            f"schema release migrations directory missing: {versions}"
        )
    return versions


def migrations_graph_sha256_for_schema_release(
    deploy_root: Path,
    schema_release_revision: str,
    *,
    repo_root: Path,
) -> str:
    """Recompute migrations graph SHA from an immutable prepared release snapshot."""
    import sys

    scripts = Path(__file__).resolve().parents[1]
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from fetchnow_pg_backup.migrations_authority import (  # noqa: E402
        migrations_graph_content_sha256,
    )

    versions = schema_release_migrations_versions_dir(
        deploy_root, schema_release_revision, repo_root=repo_root
    )
    return migrations_graph_content_sha256(versions)


def assert_schema_release_graph_sha256(
    deploy_root: Path,
    schema_release_revision: str,
    expected_graph_sha256: str,
    *,
    repo_root: Path,
) -> None:
    """Require attestation/plan graph SHA to match the schema release authority."""
    if not _CONTRACT_HASH_RE.fullmatch(expected_graph_sha256):
        raise CurrentStateError("expected migrations graph SHA must be 64 hex chars")
    actual = migrations_graph_sha256_for_schema_release(
        deploy_root, schema_release_revision, repo_root=repo_root
    )
    if actual != expected_graph_sha256:
        raise CurrentStateError(
            "migrations graph SHA mismatch for schema release "
            f"{schema_release_revision}: attestation/plan binding "
            f"{expected_graph_sha256} != recomputed {actual} from prepared release"
        )


def next_database_state_after_migration(
    *,
    previous: DatabaseState,
    target_revision: str,
    target_heads: frozenset[str] | tuple[str, ...],
    migration_id: str,
    compatibility_contract_sha256: str,
) -> DatabaseState:
    """Publish database state after a verified forward migration transaction.

    ``database.schema_release_revision`` is the sole persisted migrations-graph
    authority in schema v2. Graph SHA-256 is recomputed from the verified prepared
    release at that revision during migration gates; it is not stored in
    ``current.json``.
    """
    target = validate_full_sha(target_revision)
    sorted_heads = tuple(sorted(frozenset(target_heads)))
    if not sorted_heads:
        raise CurrentStateError("target database heads must be non-empty")
    mig_id = _parse_deployment_id(migration_id, field="last_migration_id")
    contract = _parse_optional_contract_hash(
        compatibility_contract_sha256, field="compatibility_contract_sha256"
    )
    if contract is None:
        raise CurrentStateError("migration requires compatibility_contract_sha256")
    envelope = set(previous.compatible_application_revisions)
    envelope.add(previous.schema_release_revision)
    for rev in previous.compatible_application_revisions:
        envelope.add(rev)
    envelope.add(target)
    return DatabaseState(
        heads=sorted_heads,
        schema_release_revision=target,
        last_migration_id=mig_id,
        compatibility_contract_sha256=contract,
        compatible_application_revisions=tuple(sorted(envelope)),
    )


def next_database_state_after_app_commit(
    *,
    previous: DatabaseState,
    target_revision: str,
    target_source_heads: frozenset[str],
) -> DatabaseState:
    """Preserve database heads; extend envelope when target shares schema heads."""
    target = validate_full_sha(target_revision)
    saved = frozenset(previous.heads)
    if target_source_heads == saved:
        envelope = set(previous.compatible_application_revisions)
        envelope.add(target)
        return DatabaseState(
            heads=previous.heads,
            schema_release_revision=previous.schema_release_revision,
            last_migration_id=previous.last_migration_id,
            compatibility_contract_sha256=previous.compatibility_contract_sha256,
            compatible_application_revisions=tuple(sorted(envelope)),
        )
    if target in previous.compatible_application_revisions:
        return previous
    raise CurrentStateError(
        "cannot commit application revision outside compatibility envelope "
        f"when target source heads {sorted(target_source_heads)} differ from "
        f"database heads {sorted(saved)}"
    )


def build_committed_current_state(
    *,
    application: ApplicationState,
    database: DatabaseState,
    updated_at_utc: str,
) -> CurrentStateV2:
    if application.revision not in database.compatible_application_revisions:
        raise CurrentStateError(
            "committed application.revision must be in compatibility envelope"
        )
    return CurrentStateV2(
        schema_version=CURRENT_SCHEMA_VERSION_V2,
        application=application,
        database=database,
        updated_at_utc=_parse_utc(updated_at_utc, field="updated_at_utc"),
    )


def write_current_state(
    deploy_root: Path, state: CurrentStateV2 | ResolvedCurrentState
) -> None:
    """Atomically publish schema v2 current.json. Never writes legacy v1."""
    from .journal import ensure_rollout_layout

    if isinstance(state, ResolvedCurrentState):
        payload = state.to_v2()
    else:
        payload = state
    if payload.schema_version != CURRENT_SCHEMA_VERSION_V2:
        raise CurrentStateError("writers must publish current schema version 2")
    parse_current_state_v2(payload.to_dict())
    ensure_rollout_layout(deploy_root)
    path = current_path(deploy_root)
    _reject_symlink(path, label="current.json")
    atomic_write_json(path, payload.to_dict(), mode=0o600)


def write_legacy_v1_fixture_for_tests(
    deploy_root: Path, state: LegacyCurrentStateV1
) -> None:
    """Test-only helper: publish a legacy v1 current.json without resolution."""
    from .journal import ensure_rollout_layout

    if state.schema_version != CURRENT_SCHEMA_VERSION_V1:
        raise CurrentStateError("fixture helper only writes schema v1")
    parse_legacy_current_state_v1(state.to_dict())
    ensure_rollout_layout(deploy_root)
    path = current_path(deploy_root)
    _reject_symlink(path, label="current.json")
    atomic_write_json(path, state.to_dict(), mode=0o600)
