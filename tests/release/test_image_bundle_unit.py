"""Exact-image delivery tests: real files/tars, stubbed Docker/Git/preflight.

No Docker daemon, build, image pull, network or live deployment is used.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from fetchnow_release import image_bundle as b  # noqa: E402
from fetchnow_release import prepare as p  # noqa: E402
from fetchnow_release import verify_release as v  # noqa: E402
from fetchnow_release.archive import MaterializedSource, verify_required_files  # noqa: E402
from fetchnow_release.cli import build_parser, main  # noqa: E402
from fetchnow_release.image_archive import (  # noqa: E402
    ImageArchiveError,
    parse_json,
    sha256_file,
    validate_image_archive,
)
from fetchnow_release.image_build import ImageBuildError  # noqa: E402
from fetchnow_release.manifest import ImageRecord, ReleaseManifest, write_manifest  # noqa: E402
from fetchnow_release.preflight import PreflightResult  # noqa: E402
from fetchnow_release.release_mode import (  # noqa: E402
    build_services_for_mode,
    image_reference_for_service,
)
from fetchnow_release.source_contract import required_source_files_for_version  # noqa: E402

REV = "a" * 40
TREE = "b" * 40


@pytest.fixture(autouse=True)
def restore_owned_fixture_directory_modes(tmp_path: Path):
    """Readonly release fixtures must not prevent pytest's scoped teardown."""
    yield
    for path in tmp_path.rglob("*"):
        if not path.is_symlink() and path.is_dir():
            path.chmod(path.stat().st_mode | 0o700)


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def image(service: str, *, revision: str = REV, arch: str = "amd64") -> dict[str, Any]:
    return {
        "os": "linux",
        "architecture": arch,
        "config": {
            "Labels": {
                "org.opencontainers.image.revision": revision,
                "fixture.service": service,
            }
        },
    }


def image_id(config: dict[str, Any]) -> str:
    return "sha256:" + digest(encoded(config))


def pg_identity() -> dict[str, Any]:
    return {
        "config_image_id": image_id(image("postgres")),
        "registry_index_digest": "sha256:" + "1" * 64,
        "platform_manifest_digest_linux_amd64": "sha256:" + "2" * 64,
        "platform": "linux/amd64",
        "candidate_only": True,
    }


def pg_identity_file(root: Path) -> Path:
    path = root / "postgres-identity.json"
    path.write_bytes(encoded(pg_identity()))
    return path


def write_tar(
    path: Path,
    config: dict[str, Any],
    *,
    tags: Any = None,
    oci: bool = False,
    extra: tuple[str, bytes] | None = None,
    second: bool = False,
) -> None:
    config_bytes = encoded(config)
    config_hash = digest(config_bytes)
    config_path = "blobs/sha256/" + config_hash if oci else config_hash + ".json"
    files = {config_path: config_bytes, "layer/layer.tar": b"synthetic layer payload"}
    manifest = [
        {"Config": config_path, "RepoTags": tags, "Layers": ["layer/layer.tar"]}
    ]
    if second:
        manifest.append(dict(manifest[0]))
    files["manifest.json"] = encoded(manifest)
    if oci:
        descriptor = encoded(
            {"config": {"digest": "sha256:" + config_hash}, "layers": []}
        )
        oid = digest(descriptor)
        files["blobs/sha256/" + oid] = descriptor
        files["index.json"] = encoded(
            {
                "manifests": [
                    {
                        "digest": "sha256:" + oid,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    }
                ]
            }
        )
    if extra:
        files[extra[0]] = extra[1]
    with tarfile.open(path, "w") as tf:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            tf.addfile(item, io.BytesIO(data))


def manifest(
    mode: str = "media", *, overlay: str = "compose.production.yaml"
) -> ReleaseManifest:
    records = tuple(
        ImageRecord(
            svc,
            image_reference_for_service(svc, REV),
            image_id(image(svc)),
            REV,
            "linux/amd64",
        )
        for svc in build_services_for_mode(mode)
    )
    return ReleaseManifest(
        schema_version=1,
        source_contract_version=4,
        revision=REV,
        tree_oid=TREE,
        created_at_utc="2026-10-24T00:00:00Z",
        archive_commit_verified=True,
        source_repository="fixture",
        compose_version="2.0",
        docker_version="28.0.4",
        build_platform="linux/amd64",
        images=records,
        contract_hashes={},
        tool_version="fixture",
        status="prepared",
        compose_overlay=overlay,
        release_mode=mode,
    )


def make_bundle(root: Path, release: ReleaseManifest | None = None) -> tuple[Path, str]:
    release = release or manifest()
    root.mkdir()
    write_manifest(root / "release.json", release)
    rows = []
    for svc in (*build_services_for_mode(release.release_mode), "postgres"):
        cfg = image(svc)
        path = root / (svc + ".tar")
        write_tar(path, cfg)
        rec = next((r for r in release.images if r.service == svc), None)
        rows.append(
            {
                "service": svc,
                "reference": rec.reference if rec else None,
                "image_id": image_id(cfg),
                "platform": "linux/amd64",
                "archive": path.name,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
        if svc == "postgres":
            rows[-1].update(
                {
                    k: v
                    for k, v in pg_identity().items()
                    if k
                    in {"registry_index_digest", "platform_manifest_digest_linux_amd64"}
                }
            )
    metadata = {
        "schema": b.SCHEMA,
        "platform": "linux/amd64",
        "revision": REV,
        "tree_oid": TREE,
        "release_mode": release.release_mode,
        "compose_overlay": release.compose_overlay,
        "release_manifest_sha256": sha256_file(root / "release.json"),
        "images": rows,
    }
    (root / b.BUNDLE_FILE).write_bytes(encoded(metadata))
    return root, sha256_file(root / b.BUNDLE_FILE)


class Docker:
    """Transport stub; it cannot build/pull/run/rm or contact an actual daemon."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.tags: dict[str, str] = {}
        self.configs: dict[str, dict[str, Any]] = {}
        self.available = {
            image_id(image(svc)): image(svc)
            for svc in (*build_services_for_mode("media"), "postgres")
        }
        self.fail_load = False

    def inspect(self, reference: str) -> dict[str, Any]:
        iid = self.tags.get(reference, reference)
        if iid not in self.configs:
            raise ImageBuildError("fixture image absent")
        config = self.configs[iid]
        return {
            "Id": iid,
            "Os": config["os"],
            "Architecture": config["architecture"],
            "Config": config["config"],
        }

    def command(self, argv: list[str], **kwargs: Any) -> str:
        self.calls.append(argv)
        if argv[:3] == ["docker", "info", "--format"]:
            return "linux/x86_64\n"
        if argv[:3] == ["docker", "image", "ls"]:
            rows = [
                {
                    "Repository": ref.rsplit(":", 1)[0],
                    "Tag": ref.rsplit(":", 1)[1],
                    "ID": iid,
                }
                for ref, iid in self.tags.items()
            ]
            rows += [
                {"Repository": "<none>", "Tag": "<none>", "ID": iid}
                for iid in self.configs
            ]
            return "\n".join(json.dumps(r) for r in rows)
        if argv[:3] == ["docker", "image", "save"]:
            assert argv[3:5] == ["--platform", "linux/amd64"]
            write_tar(Path(argv[argv.index("--output") + 1]), self.configs[argv[-1]])
            return ""
        if argv[:3] == ["docker", "image", "load"]:
            if self.fail_load:
                raise b.ImageBundleError("fixture uncertain load")
            assert argv[3:5] == ["--platform", "linux/amd64"]
            path = Path(argv[argv.index("--input") + 1])
            with tarfile.open(path) as tf:
                fh = tf.extractfile("manifest.json")
                assert fh is not None
                entry = json.load(fh)[0]
                fh = tf.extractfile(entry["Config"])
                assert fh is not None
                cfg = json.load(fh)
            self.configs[image_id(cfg)] = cfg
            return "not trusted as identity evidence"
        if argv[:3] == ["docker", "image", "tag"]:
            self.tags[argv[-1]] = argv[-2]
            return ""
        pytest.fail("unexpected Docker action: " + repr(argv))


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> Docker:
    engine = Docker()
    monkeypatch.setattr(b, "_docker", engine.command)
    monkeypatch.setattr(b, "inspect_image", engine.inspect)
    monkeypatch.setattr(v, "inspect_image", engine.inspect)
    monkeypatch.setattr(v, "image_exists", lambda ref: ref in engine.tags)
    return engine


@pytest.mark.parametrize("oci", [False, True])
def test_archive_config_identity_is_verified_without_extraction(
    tmp_path: Path, oci: bool
) -> None:
    path = tmp_path / "image.tar"
    cfg = image("api")
    write_tar(path, cfg, oci=oci)
    validate_image_archive(path, image_id=image_id(cfg), revision=REV)
    assert {p.name for p in tmp_path.iterdir()} == {"image.tar"}


@pytest.mark.parametrize(
    "case",
    [
        "tag",
        "extra_image",
        "wrong_id",
        "wrong_platform",
        "wrong_revision",
        "traversal",
        "absolute",
        "symlink",
        "duplicate",
    ],
)
def test_unsafe_archive_rejected(tmp_path: Path, case: str) -> None:
    cfg = image("api", arch="arm64" if case == "wrong_platform" else "amd64")
    path = tmp_path / "image.tar"
    write_tar(
        path,
        cfg,
        tags=["postgres:live"] if case == "tag" else None,
        second=case == "extra_image",
        extra=("../escape", b"x")
        if case == "traversal"
        else (("/escape", b"x") if case == "absolute" else None),
    )
    if case in {"symlink", "duplicate"}:
        with tarfile.open(path, "a") as tf:
            member = tarfile.TarInfo("link" if case == "symlink" else "manifest.json")
            if case == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
            tf.addfile(member)
    with pytest.raises(ImageArchiveError):
        validate_image_archive(
            path,
            image_id="sha256:" + "0" * 64 if case == "wrong_id" else image_id(cfg),
            revision="c" * 40 if case == "wrong_revision" else REV,
        )


@pytest.mark.parametrize("mode", ["canonical", "media"])
def test_complete_bundle_loads_ids_and_never_tags_postgres(
    tmp_path: Path, docker: Docker, mode: str
) -> None:
    path, checksum = make_bundle(tmp_path / "bundle", manifest(mode))
    validated = b.validate_bundle(path, expected_sha256=checksum)
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    docker.tags["postgres:16.9-alpine"] = "sha256:" + "f" * 64
    receipt = b.load_bundle_images(validated, incomplete=incomplete)
    assert receipt["bundle_sha256"] == checksum
    assert (
        receipt["postgres_candidate_only"] and not receipt["security_approval_applied"]
    )
    assert len(receipt["image_ids"]) == len(build_services_for_mode(mode)) + 1
    assert docker.tags["postgres:16.9-alpine"] == "sha256:" + "f" * 64
    assert not any(
        ref.startswith("postgres:")
        for ref in docker.tags
        if ref != "postgres:16.9-alpine"
    )
    assert not (incomplete / "image-staging").exists()
    assert (incomplete / b.IMPORT_RECEIPT).is_file()
    assert all(
        "build" not in call and "pull" not in call and "run" not in call
        for call in docker.calls
    )


@pytest.mark.parametrize(
    "case",
    [
        "wrong_sha",
        "bad_archive",
        "missing",
        "extra",
        "symlink",
        "duplicate_service",
        "wrong_role_id",
        "wrong_overlay",
        "traversal_name",
    ],
)
def test_invalid_bundle_rejected_before_docker(
    tmp_path: Path, docker: Docker, case: str
) -> None:
    path, checksum = make_bundle(tmp_path / "bundle")
    metadata = json.loads((path / b.BUNDLE_FILE).read_text())
    if case == "bad_archive":
        (path / "api.tar").write_bytes(b"broken")
    elif case == "missing":
        (path / "api.tar").unlink()
    elif case == "extra":
        (path / "untracked.env").write_text("not allowed")
    elif case == "symlink":
        (path / "api.tar").rename(tmp_path / "api.tar")
        (path / "api.tar").symlink_to(tmp_path / "api.tar")
    elif case == "duplicate_service":
        metadata["images"][1] = dict(metadata["images"][0])
    elif case == "wrong_role_id":
        metadata["images"][0]["image_id"] = "sha256:" + "e" * 64
    elif case == "wrong_overlay":
        metadata["compose_overlay"] = "compose.staging.yaml"
    elif case == "traversal_name":
        metadata["images"][0]["archive"] = "../api.tar"
    if case != "wrong_sha":
        (path / b.BUNDLE_FILE).write_bytes(encoded(metadata))
        checksum = sha256_file(path / b.BUNDLE_FILE)
    else:
        checksum = "0" * 64
    with pytest.raises((ValueError, OSError)):
        b.validate_bundle(path, expected_sha256=checksum)
    assert docker.calls == []


def test_tag_conflict_refused_before_any_load(tmp_path: Path, docker: Docker) -> None:
    path, checksum = make_bundle(tmp_path / "bundle")
    bundle = b.validate_bundle(path, expected_sha256=checksum)
    docker.tags[image_reference_for_service("api", REV)] = "sha256:" + "e" * 64
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    with pytest.raises(b.ImageBundleError, match="retarget"):
        b.load_bundle_images(bundle, incomplete=incomplete)
    assert not any("load" in call or "tag" in call for call in docker.calls)


def test_staging_detects_changed_archive_before_any_load(
    tmp_path: Path, docker: Docker
) -> None:
    path, checksum = make_bundle(tmp_path / "bundle")
    bundle = b.validate_bundle(path, expected_sha256=checksum)
    (path / "api.tar").write_bytes(b"changed after validation")
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    with pytest.raises(b.ImageBundleError, match="while staging"):
        b.load_bundle_images(bundle, incomplete=incomplete)
    assert not any("load" in call for call in docker.calls)


def test_load_failure_has_no_receipt_or_automatic_retry(
    tmp_path: Path, docker: Docker
) -> None:
    path, checksum = make_bundle(tmp_path / "bundle")
    bundle = b.validate_bundle(path, expected_sha256=checksum)
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    docker.fail_load = True
    with pytest.raises(b.ImageBundleError):
        b.load_bundle_images(bundle, incomplete=incomplete)
    assert sum("load" in call for call in docker.calls) == 1
    assert not any("tag" in call for call in docker.calls)
    assert not (incomplete / b.IMPORT_RECEIPT).exists()


def fake_source(incomplete: Path) -> MaterializedSource:
    source = incomplete / "source"
    source.mkdir()
    for name in required_source_files_for_version(4):
        path = source / name
        if name == "backend/src":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture source: " + name + "\n")
    archive = incomplete / "source.tar"
    archive.write_bytes(b"fixture source archive")
    return MaterializedSource(
        REV,
        TREE,
        source,
        archive,
        verify_required_files(source, source_contract_version=4),
    )


def test_prepare_import_path_publishes_release_without_build(
    tmp_path: Path, docker: Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    prototype = tmp_path / "prototype"
    prototype.mkdir()
    source = fake_source(prototype)
    release = replace(manifest(), contract_hashes=dict(source.contract_hashes))
    path, checksum = make_bundle(tmp_path / "bundle", release)
    monkeypatch.setattr(
        p,
        "run_preflight",
        lambda inp: PreflightResult(True, ("OK: fixture preflight",)),
    )
    checks: list[str] = []
    monkeypatch.setattr(p, "assert_clean_worktree", lambda repo: checks.append("clean"))
    monkeypatch.setattr(
        p,
        "assert_revision_in_origin_main",
        lambda repo, rev: checks.append("trusted-main"),
    )
    monkeypatch.setattr(
        p, "materialize_source", lambda **kw: fake_source(kw["incomplete_dir"])
    )
    monkeypatch.setattr(
        p,
        "build_application_images",
        lambda **kw: pytest.fail("import must never build"),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    inp = p.PrepareInput(
        "fetchnow-rollout-test-fixture",
        tmp_path / "env",
        (repo / "compose.yaml", repo / "compose.production.yaml"),
        REV,
        repo,
        tmp_path / "deploy",
        release_mode="media",
        image_bundle=path,
        image_bundle_sha256=checksum,
    )
    result = p.prepare_release(inp)
    assert result.ok, result.messages
    assert checks == ["clean", "trusted-main"]
    assert result.release_dir is not None
    assert v.verify_prepared_release(result.release_dir, expected_revision=REV).ok
    assert not (inp.deploy_root / "state/current.json").exists()
    assert (
        json.loads((result.release_dir / b.IMPORT_RECEIPT).read_text())["bundle_sha256"]
        == checksum
    )
    again = p.prepare_release(inp)
    assert again.ok and again.already_prepared, again.messages


@pytest.mark.parametrize(
    "case", ["missing_checksum", "wrong_overlay", "wrong_mode", "wrong_revision"]
)
def test_prepare_rejects_invalid_import_without_build(
    tmp_path: Path, docker: Docker, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    path, checksum = make_bundle(tmp_path / "bundle")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        p, "build_application_images", lambda **kw: pytest.fail("no build fallback")
    )
    inp = p.PrepareInput(
        "fixture",
        tmp_path / "env",
        (
            repo / "compose.yaml",
            repo
            / (
                "compose.staging.yaml"
                if case == "wrong_overlay"
                else "compose.production.yaml"
            ),
        ),
        "c" * 40 if case == "wrong_revision" else REV,
        repo,
        tmp_path / "deploy",
        release_mode="canonical" if case == "wrong_mode" else "media",
        image_bundle=path,
        image_bundle_sha256=None if case == "missing_checksum" else checksum,
    )
    result = p.prepare_release(inp)
    assert not result.ok
    assert docker.calls == []


def test_cli_requires_explicit_bundle_and_candidate_identity() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "prepare",
            "--env-file",
            "/env",
            "--compose-file",
            "compose.yaml",
            "--expected-revision",
            REV,
            "--deploy-root",
            "/deploy",
        ]
    )
    assert args.image_bundle is None and args.image_bundle_sha256 is None
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "export-images",
                "--expected-revision",
                REV,
                "--deploy-root",
                "/deploy",
                "--output",
                "/bundle",
            ]
        )


def test_duplicate_json_metadata_rejected() -> None:
    with pytest.raises(ImageArchiveError, match="duplicate"):
        parse_json(b'{"images":[],"images":[]}')


def prepared_fixture(
    tmp_path: Path, docker: Docker, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, ReleaseManifest]:
    repo = tmp_path / "repo"
    repo.mkdir()
    deploy = tmp_path / "deploy"
    final = deploy / "releases" / REV
    final.mkdir(parents=True)
    source = fake_source(final)
    release = replace(manifest(), contract_hashes=dict(source.contract_hashes))
    write_manifest(final / "release.json", release)
    p._chmod_tree_readonly(source.source_dir)
    monkeypatch.setattr(v, "resolve_tree_oid", lambda repo, rev: TREE)
    docker.configs.update(docker.available)
    docker.tags.update({r.reference: r.image_id for r in release.images})
    return repo, deploy, release


def test_export_saves_existing_ids_without_build_or_tag_change(
    tmp_path: Path, docker: Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, deploy, release = prepared_fixture(tmp_path, docker, monkeypatch)
    tags_before = dict(docker.tags)
    result = b.export_image_bundle(
        deploy_root=deploy,
        repo_root=repo,
        revision=REV,
        output=tmp_path / "export",
        postgres_identity_json=pg_identity_file(tmp_path),
    )
    assert result.ok, result.messages
    out = tmp_path / "export"
    bundle = b.validate_bundle(out, expected_sha256=sha256_file(out / b.BUNDLE_FILE))
    assert bundle.manifest.to_dict() == release.to_dict()
    assert len(bundle.images) == 7
    assert docker.tags == tags_before
    saves = [call for call in docker.calls if call[:3] == ["docker", "image", "save"]]
    assert len(saves) == 7 and all(call[-1].startswith("sha256:") for call in saves)
    assert not any(
        "build" in call or "pull" in call or "load" in call or "tag" in call
        for call in docker.calls
    )
    again = b.export_image_bundle(
        deploy_root=deploy,
        repo_root=repo,
        revision=REV,
        output=out,
        postgres_identity_json=pg_identity_file(tmp_path),
    )
    assert not again.ok


def test_export_failure_retains_partial_output_without_bundle_marker(
    tmp_path: Path, docker: Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, deploy, _ = prepared_fixture(tmp_path, docker, monkeypatch)
    command = docker.command

    def fail(argv: list[str], **kwargs: Any) -> str:
        command(argv, **kwargs)
        raise b.ImageBundleError("fixture save failure")

    monkeypatch.setattr(b, "_docker", fail)
    out = tmp_path / "export"
    result = b.export_image_bundle(
        deploy_root=deploy,
        repo_root=repo,
        revision=REV,
        output=out,
        postgres_identity_json=pg_identity_file(tmp_path),
    )
    assert not result.ok
    assert (out / "release.json").exists()
    assert not (out / b.BUNDLE_FILE).exists()
    assert len(docker.calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "cancel", "output_limit", "nonzero"])
def test_docker_failure_is_bounded_and_not_retried(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from fetchnow_release.bounded_subprocess import (
        BoundedCancelledError,
        BoundedResult,
        BoundedTimeoutError,
        OutputLimitExceededError,
    )

    calls: list[float] = []

    def fail(argv: list[str], **kwargs: Any) -> Any:
        calls.append(kwargs["timeout"])
        if failure == "nonzero":
            return BoundedResult(tuple(argv), 1, b"", b"fixture daemon failure")
        raise {
            "timeout": BoundedTimeoutError,
            "cancel": BoundedCancelledError,
            "output_limit": OutputLimitExceededError,
        }[failure]("fixture")

    monkeypatch.setattr(b, "run_bounded", fail)
    with pytest.raises(b.ImageBundleError):
        b._docker(["docker", "image", "load", "--input", "/fixture"], mutating=True)
    assert calls == [b.IMAGE_IO_TIMEOUT]


def test_inventory_failure_is_not_absence(
    tmp_path: Path, docker: Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, checksum = make_bundle(tmp_path / "bundle")
    bundle = b.validate_bundle(path, expected_sha256=checksum)
    command = docker.command

    def fail(argv: list[str], **kwargs: Any) -> str:
        if argv[:3] == ["docker", "image", "ls"]:
            raise b.ImageBundleError("inventory unavailable")
        return command(argv, **kwargs)

    monkeypatch.setattr(b, "_docker", fail)
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    with pytest.raises(b.ImageBundleError, match="inventory"):
        b.load_bundle_images(bundle, incomplete=incomplete)
    assert not any("load" in call or "tag" in call for call in docker.calls)


def test_expanding_archive_copy_is_bounded(tmp_path: Path, docker: Docker) -> None:
    path, checksum = make_bundle(tmp_path / "bundle")
    bundle = b.validate_bundle(path, expected_sha256=checksum)
    original_size = (path / "api.tar").stat().st_size
    with (path / "api.tar").open("ab") as fh:
        fh.write(b"x" * 100000)
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    with pytest.raises(b.ImageBundleError, match="grew"):
        b.load_bundle_images(bundle, incomplete=incomplete)
    assert (incomplete / "image-staging/api.tar").stat().st_size == original_size
    assert not any("load" in call for call in docker.calls)


def test_cli_export_dispatches_without_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def export(**kwargs: Any) -> b.BundleResult:
        calls.append(kwargs)
        return b.BundleResult(True, ("OK: fixture",))

    monkeypatch.setattr("fetchnow_release.cli.export_image_bundle", export)
    code = main(
        [
            "export-images",
            "--expected-revision",
            REV,
            "--deploy-root",
            str(tmp_path / "deploy"),
            "--repo-root",
            str(tmp_path / "repo"),
            "--output",
            str(tmp_path / "bundle"),
            "--postgres-identity-json",
            str(tmp_path / "postgres-identity.json"),
        ]
    )
    assert code == 0 and len(calls) == 1


def import_input(
    tmp_path: Path, path: Path, checksum: str, repo: Path
) -> p.PrepareInput:
    return p.PrepareInput(
        "fetchnow-rollout-test-fixture",
        tmp_path / "env",
        (repo / "compose.yaml", repo / "compose.production.yaml"),
        REV,
        repo,
        tmp_path / "target-deploy",
        release_mode="media",
        image_bundle=path,
        image_bundle_sha256=checksum,
    )


def stub_import_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        p, "run_preflight", lambda inp: PreflightResult(True, ("OK: fixture",))
    )
    monkeypatch.setattr(p, "assert_clean_worktree", lambda repo: None)
    monkeypatch.setattr(p, "assert_revision_in_origin_main", lambda repo, rev: None)
    monkeypatch.setattr(
        p, "materialize_source", lambda **kw: fake_source(kw["incomplete_dir"])
    )
    monkeypatch.setattr(
        p, "build_application_images", lambda **kw: pytest.fail("no build fallback")
    )


def test_export_import_roundtrip_preserves_exact_ids_and_input_bytes(
    tmp_path: Path, docker: Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, producer, release = prepared_fixture(tmp_path, docker, monkeypatch)
    path = tmp_path / "saved"
    exported = b.export_image_bundle(
        deploy_root=producer,
        repo_root=repo,
        revision=REV,
        output=path,
        postgres_identity_json=pg_identity_file(tmp_path),
    )
    assert exported.ok, exported.messages
    checksum = sha256_file(path / b.BUNDLE_FILE)
    before = {f.name: sha256_file(f) for f in path.iterdir()}
    docker.tags = {"postgres:16.9-alpine": "sha256:" + "e" * 64}
    docker.configs.clear()
    docker.calls.clear()
    stub_import_source(monkeypatch)
    result = p.prepare_release(import_input(tmp_path, path, checksum, repo))
    assert result.ok and result.release_dir is not None, result.messages
    imported = json.loads((result.release_dir / b.IMPORT_RECEIPT).read_text())
    assert imported["image_ids"] == {
        **{i.service: i.image_id for i in release.images},
        "postgres": image_id(image("postgres")),
    }
    assert imported["postgres_registry_identity"] == {
        k: pg_identity()[k]
        for k in ("registry_index_digest", "platform_manifest_digest_linux_amd64")
    }
    assert docker.tags["postgres:16.9-alpine"] == "sha256:" + "e" * 64
    assert before == {f.name: sha256_file(f) for f in path.iterdir()}
    assert sum("load" in call for call in docker.calls) == 7
    assert not any(
        "build" in call or "pull" in call or "run" in call for call in docker.calls
    )


def test_failed_prepare_import_publishes_no_release_or_authority(
    tmp_path: Path, docker: Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    prototype = tmp_path / "prototype"
    prototype.mkdir()
    source = fake_source(prototype)
    release = replace(manifest(), contract_hashes=dict(source.contract_hashes))
    path, checksum = make_bundle(tmp_path / "bundle", release)
    repo = tmp_path / "repo"
    repo.mkdir()
    inp = import_input(tmp_path, path, checksum, repo)
    state = inp.deploy_root / "state/current.json"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"existing authority must remain untouched")
    stub_import_source(monkeypatch)
    docker.fail_load = True
    result = p.prepare_release(inp)
    assert not result.ok
    assert state.read_bytes() == b"existing authority must remain untouched"
    assert not (inp.deploy_root / "releases" / REV).exists()
    assert sum("load" in call for call in docker.calls) == 1
    assert not any(
        "build" in call or "pull" in call or "tag" in call for call in docker.calls
    )


@pytest.mark.parametrize("blocked", ["preflight", "trusted-main"])
def test_prepare_import_keeps_existing_safety_gates(
    tmp_path: Path, docker: Docker, monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    from fetchnow_release.git_checks import GitCheckError

    path, checksum = make_bundle(tmp_path / "bundle")
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_import_source(monkeypatch)
    if blocked == "preflight":
        monkeypatch.setattr(
            p,
            "run_preflight",
            lambda inp: PreflightResult(False, ("FAIL: fixture preflight",)),
        )
    else:

        def refuse(repo: Path, revision: str) -> None:
            raise GitCheckError("fixture untrusted source")

        monkeypatch.setattr(p, "assert_revision_in_origin_main", refuse)
    result = p.prepare_release(import_input(tmp_path, path, checksum, repo))
    assert not result.ok
    assert docker.calls == []


@pytest.mark.parametrize("failure", ["bad_inventory", "wrong_arch", "disk_full"])
def test_import_environment_failures_precede_load(
    tmp_path: Path, docker: Docker, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path, checksum = make_bundle(tmp_path / "bundle")
    bundle = b.validate_bundle(path, expected_sha256=checksum)
    command = docker.command

    def probe(argv: list[str], **kwargs: Any) -> str:
        if failure == "bad_inventory" and argv[:3] == ["docker", "image", "ls"]:
            return "not JSON"
        if failure == "wrong_arch" and argv[:2] == ["docker", "info"]:
            return "linux/aarch64"
        return command(argv, **kwargs)

    monkeypatch.setattr(b, "_docker", probe)
    if failure == "disk_full":
        from types import SimpleNamespace

        monkeypatch.setattr(
            b.shutil, "disk_usage", lambda path: SimpleNamespace(free=0)
        )
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    with pytest.raises(ValueError):
        b.load_bundle_images(bundle, incomplete=incomplete)
    assert not any("load" in call or "tag" in call for call in docker.calls)
