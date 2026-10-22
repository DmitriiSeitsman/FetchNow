"""Offline schema, scenario isolation and failure-reason regressions.

Docker/release transports are stubbed; these are NOT native containment proof.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import native_media_storage_media_inside as media
import native_media_storage_resource_release_path as mod
import pytest
from fetchnow_release import image_build
from fetchnow_release.bounded_subprocess import BoundedResult
from fetchnow_release.env_file import load_env_file
from fetchnow_release.manifest import (
    ImageRecord,
    ManifestError,
    ReleaseManifest,
    write_manifest,
)
from fetchnow_release.media_storage import (
    MediaStorageError,
    disposable_test_storage_contract,
)
from fetchnow_release.release_mode import (
    expected_image_services,
    image_reference_for_service,
)
from fetchnow_release.resource_budget import disposable_test_budget
from fetchnow_release.rollout import RolloutResult

REV = "a" * 40
LEGACY_REV = "c" * 40


def media_manifest(*, revision=REV) -> ReleaseManifest:
    return ReleaseManifest(
        schema_version=1,
        source_contract_version=4,
        revision=revision,
        tree_oid="b" * 40,
        created_at_utc="2026-01-01T00:00:00Z",
        archive_commit_verified=True,
        source_repository="FetchNow",
        compose_version="2.0.0",
        docker_version="29.0.0",
        build_platform="linux/amd64",
        images=tuple(
            ImageRecord(
                service=service,
                reference=image_reference_for_service(service, revision),
                image_id="sha256:"
                + hashlib.sha256((revision + service).encode()).hexdigest(),
                oci_revision=revision,
                platform="linux/amd64",
            )
            for service in sorted(expected_image_services("media"))
        ),
        contract_hashes={"compose.yaml": "c" * 64},
        tool_version="test",
        status="prepared",
        compose_overlay="compose.staging.yaml",
        release_mode="media",
    )


def test_manifest_uses_real_serializer_image_id(tmp_path):
    path = tmp_path / "release.json"
    manifest = media_manifest()
    write_manifest(path, manifest)
    ids, tracked = {}, []
    mod.record_prepared_images(path, REV, ids, lambda *a, **kw: tracked.append((a, kw)))
    assert ids == {image.service: image.image_id for image in manifest.images}
    assert len(tracked) == 6
    assert all(entry[1]["reference"] for entry in tracked)


@pytest.mark.parametrize(
    "mutation", ["id", "missing", "duplicate", "wrong_revision", "arm64", "truncated"]
)
def test_manifest_bad_input_does_not_record_partial_ids(tmp_path, mutation):
    path = tmp_path / "release.json"
    write_manifest(path, media_manifest())
    raw = json.loads(path.read_text())
    if mutation == "id":
        for image in raw["images"]:
            image["id"] = image.pop("image_id")
    elif mutation == "missing":
        raw["images"].pop()
    elif mutation == "duplicate":
        raw["images"].append(raw["images"][0])
    elif mutation == "arm64":
        raw["images"][-1]["platform"] = "linux/arm64"
    path.write_text("{" if mutation == "truncated" else json.dumps(raw))
    ids, tracked = {"prior": "retained"}, []
    with pytest.raises(ManifestError):
        mod.record_prepared_images(
            path,
            "d" * 40 if mutation == "wrong_revision" else REV,
            ids,
            lambda *a, **kw: tracked.append(a),
        )
    assert ids == {"prior": "retained"} and not tracked


@pytest.mark.parametrize(
    "ok,messages",
    [
        (True, ("requires explicit data migration",)),
        (False, ("KeyError: id",)),
        (False, ("unresolved journal blocks rollout",)),
        (False, ("docker unavailable",)),
    ],
)
def test_negative_requires_expected_domain_failure(ok, messages):
    with pytest.raises(RuntimeError, match="expected refusal"):
        mod.require_refusal(
            RolloutResult(ok, False, messages),
            reason="requires explicit data migration",
        )


def test_expected_refusal_is_accepted():
    assert "migration" in mod.require_refusal(
        RolloutResult(
            False,
            False,
            ("FAIL: existing volume tmp requires explicit data migration",),
        ),
        reason="requires explicit data migration",
    )


@pytest.mark.parametrize(
    "error",
    [
        KeyError("id"),
        RuntimeError("docker failure"),
        MediaStorageError("approved must be true"),
    ],
)
def test_missing_mount_does_not_accept_arbitrary_error(monkeypatch, error):
    def broken(*args):
        raise error

    monkeypatch.setattr(mod, "assert_media_storage_ready", broken)
    with pytest.raises((KeyError, RuntimeError)):
        mod.require_missing_mount_refusal(
            disposable_test_storage_contract(host_bind_root="/test")
        )


def test_missing_mount_accepts_root_fallback_only(monkeypatch):
    def missing(*args):
        raise MediaStorageError("host_bind_root resolves onto the root filesystem")

    monkeypatch.setattr(mod, "assert_media_storage_ready", missing)
    assert "root filesystem" in mod.require_missing_mount_refusal(
        disposable_test_storage_contract(host_bind_root="/test")
    )


class ReachedPositiveError(RuntimeError):
    pass


def test_fixture_revisions_are_marker_only_real_disposable_git(tmp_path):
    """Only this test's temp repo is committed; no FetchNow checkout writes."""
    clone = tmp_path / "clone"
    clone.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Fixture Test"],
        ["git", "config", "user.email", "fixture@example.test"],
    ):
        subprocess.run(argv, cwd=clone, check=True, capture_output=True)
    (clone / "runtime.txt").write_text("unchanged")
    mod.commit(clone, "disposable test baseline")
    marker = clone / "docs" / "media-storage-accept-marker.txt"
    legacy, positive = mod.create_fixture_revisions(clone, marker, "fixture")
    assert legacy != positive
    for rev in (legacy, positive):
        contents = subprocess.run(
            ["git", "show", f"{rev}:runtime.txt"],
            cwd=clone,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert contents == "unchanged"
    assert (
        mod._run(["git", "status", "--porcelain"], cwd=clone, check=True).stdout == ""
    )


@pytest.mark.parametrize(
    "failure", ["same", "empty_delta", "runtime_delta", "not_ancestor"]
)
def test_fixture_revision_identity_or_runtime_drift_refused(
    tmp_path, monkeypatch, failure
):
    commits = iter([LEGACY_REV, LEGACY_REV if failure == "same" else REV])
    monkeypatch.setattr(mod, "commit", lambda *a: next(commits))

    def run(argv, **kwargs):
        if failure == "not_ancestor":
            raise RuntimeError("git merge-base failed")
        stdout = "backend/Dockerfile\n" if failure == "runtime_delta" else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(mod, "_run", run)
    with pytest.raises(RuntimeError):
        mod.create_fixture_revisions(tmp_path, tmp_path / "docs" / "marker", "fixture")


@pytest.mark.parametrize("broken_negative", [None, "KeyError", "unrelated"])
def test_projects_are_separate_and_legacy_never_removed_in_use(
    tmp_path, monkeypatch, broken_negative
):
    """Real schema + occupied-tag guard; Docker metadata transport is stubbed."""
    work = tmp_path / "work"
    work.mkdir()
    projects = ["fetchnow-rollout-test-11111111", "fetchnow-rollout-test-22222222"]
    generated = iter(projects)
    monkeypatch.setattr(mod, "make_rollout_project_name", lambda: next(generated))
    monkeypatch.setattr(
        mod,
        "Path",
        lambda p: tmp_path / "units" if p == "/run/systemd/system" else Path(p),
    )
    (tmp_path / "units").mkdir()
    monkeypatch.setattr(
        mod, "ensure_rollout_fixture_compatibility_policy", lambda p: None
    )
    revisions = iter([LEGACY_REV, REV])
    monkeypatch.setattr(mod, "commit", lambda *a: next(revisions))
    monkeypatch.setattr(mod, "wait_for_postgres", lambda *a: None)
    calls, volumes, records, tracks, unmounts = [], {}, [], [], []
    images, running, cid_images, prepares = {}, {}, {}, []
    envs = {}
    sentinel = []

    def probe(argv, **kwargs):
        """No Docker calls: exercise production image-ID/ancestor resolution."""
        stdout, rc = "", 0
        if argv[:3] == ["docker", "image", "inspect"]:
            image = images.get(argv[3])
            if image is None:
                rc = 1
            else:
                stdout = image if argv[-1] == "{{.Id}}" else json.dumps({"Id": image})
        elif argv[:2] == ["docker", "ps"]:
            image = argv[argv.index("--filter") + 1].removeprefix("ancestor=")
            stdout = "\n".join(f"{cid} {name}" for cid, name in running.get(image, []))
        elif argv[:2] == ["docker", "inspect"]:
            stdout = json.dumps(cid_images[argv[2]])
        else:
            raise AssertionError(f"unexpected metadata probe: {argv}")
        return BoundedResult(tuple(argv), rc, stdout.encode(), b"")

    monkeypatch.setattr(image_build, "_probe", probe)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        stdout, rc = "", 0
        if argv[:3] == ["git", "diff", "--name-only"]:
            stdout = "docs/media-storage-accept-marker.txt\n"
        elif argv[:3] == ["docker", "volume", "inspect"]:
            if argv[-1] not in volumes:
                rc = 1
            else:
                stdout = json.dumps([{"Name": argv[-1], "Options": volumes[argv[-1]]}])
        elif argv[:3] == ["docker", "volume", "create"]:
            volumes[argv[-1]] = {
                argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1]
                for i, value in enumerate(argv)
                if value == "--opt"
            }
        elif argv[:2] == ["docker", "run"]:
            if "sh" in argv:
                sentinel.append(argv[-1].split("'")[3])
            else:
                stdout = sentinel[0]
        return subprocess.CompletedProcess(argv, rc, stdout, "")

    monkeypatch.setattr(mod, "_run", fake_run)

    def prepare(inp):
        values = load_env_file(inp.env_file)
        assert values["FETCHNOW_RELEASE_REVISION"] == inp.expected_revision
        assert values["COMPOSE_PROJECT_NAME"] == inp.project_name
        envs[inp.project_name] = inp.expected_revision
        # This is the genuine production guard, not a stub that always succeeds.
        image_build.assert_tag_collision_safe(
            inp.expected_revision, release_mode="media"
        )
        manifest = media_manifest(revision=inp.expected_revision)
        path = inp.deploy_root / "releases" / inp.expected_revision / "release.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_manifest(path, manifest)
        for image in manifest.images:
            images[image.reference] = image.image_id
        prepares.append((inp.project_name, inp.expected_revision, path))
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(mod, "prepare_release", prepare)

    def rollout(inp):
        assert inp.expected_revision == envs[inp.project_name]
        if not inp.media_activation:
            if inp.project_name == projects[0]:
                assert volumes[f"{projects[0]}_tmp"]["type"] == "none"
                assert volumes[f"{projects[0]}_tmp"]["o"] == "bind"
            else:
                volumes[f"{projects[1]}_tmp"] = {}
                image = images[image_reference_for_service("api", LEGACY_REV)]
                cid = "legacy-container"
                running[image] = [(cid, inp.project_name + "-api")]
                cid_images[cid] = image
                # Reproduce the historical failure: rebuilding that same
                # revision MUST still be refused while legacy is running.
                with pytest.raises(
                    image_build.ImageBuildError, match="refusing to overwrite"
                ):
                    image_build.assert_tag_collision_safe(
                        LEGACY_REV, release_mode="media"
                    )
            authority = mod.current_path(inp.deploy_root)
            authority.parent.mkdir(parents=True, exist_ok=True)
            authority.write_bytes(inp.project_name.encode())
            return RolloutResult(True, False, ())
        if inp.project_name == projects[1]:
            if broken_negative == "KeyError":
                raise KeyError("id")
            return RolloutResult(
                False,
                False,
                (
                    "unresolved journal"
                    if broken_negative
                    else "requires explicit data migration",
                ),
            )
        if inp.media_storage_contract["source_device"] == "/dev/wrong":
            return RolloutResult(
                False, False, ("filesystem capacity outside approved bounds",)
            )
        raise ReachedPositiveError()

    monkeypatch.setattr(mod, "rollout_release", rollout)
    monkeypatch.setattr(
        mod, "require_missing_mount_refusal", lambda c: "root filesystem"
    )

    def provision(**kwargs):
        mount = tmp_path / "wrong-mount"
        mount.mkdir()
        return (
            tmp_path / "loop.img",
            "/dev/wrong",
            mount,
            "00000000-0000-4000-8000-000000000002",
        )

    expected = (
        KeyError
        if broken_negative == "KeyError"
        else RuntimeError
        if broken_negative
        else ReachedPositiveError
    )

    def record(name, status, detail="", **evidence):
        # Same signature as the real orchestrator: duplicate detail must fail.
        records.append(((name, status, detail), evidence))

    with pytest.raises(expected):
        mod.run_release_cases(
            root=tmp_path,
            work=work,
            contract=disposable_test_storage_contract(
                host_bind_root=str(tmp_path / "media")
            ),
            budget_raw=disposable_test_budget().to_dict(),
            provision_loop_fs=provision,
            safe_unmount=lambda *a: unmounts.append(a),
            record=record,
            track=lambda *a, **kw: tracks.append((a, kw)),
            image_ids={},
            set_project=lambda p: None,
            set_slice=lambda *a: None,
        )
    assert not any(argv[:3] == ["docker", "volume", "rm"] for argv in calls)
    assert {a[1] for a, _ in tracks if a[0] == "compose_project"} == set(projects)
    assert [(p, r) for p, r, _ in prepares] == [
        (projects[1], LEGACY_REV),
        (projects[0], REV),
    ]
    assert envs == {projects[1]: LEGACY_REV, projects[0]: REV}
    # Positive prepare must not retarget any legacy reference or stale its manifest.
    for _, _, path in prepares:
        manifest = mod.load_manifest(path)
        assert all(
            images[image.reference] == image.image_id for image in manifest.images
        )
    if broken_negative:
        assert not any(a[1] == "PASS" for a, _ in records)
    else:
        assert {a[0] for a, _ in records} == {
            "S5_legacy_volume_refusal",
            "S4_missing_or_wrong_fs",
        }
        assert len(unmounts) == 1
        legacy = next(e for a, e in records if a[0] == "S5_legacy_volume_refusal")
        assert legacy["legacy_revision"] == LEGACY_REV
        assert legacy["positive_revision"] == REV


@pytest.mark.parametrize(
    "mode", ["ok", "idempotent", "failed", "double_injection", "same_revision"]
)
def test_rollback_requires_distinct_target_and_one_shot_injection(
    tmp_path, monkeypatch, mode
):
    from fetchnow_release import rollout
    from fetchnow_release.activate import ActivateError

    env, marker = tmp_path / "env", tmp_path / "marker"
    env.write_text(f"FETCHNOW_RELEASE_REVISION={REV}\n")
    env.chmod(0o600)
    marker.write_text("fixture")
    target = "b" * 40
    prepared, delegations = [], []
    monkeypatch.setattr(
        mod, "commit", lambda *a: REV if mode == "same_revision" else target
    )
    monkeypatch.setattr(
        rollout, "activate_gateway", lambda **kw: delegations.append(kw)
    )

    def execute(inp):
        assert inp.expected_revision == target
        assert f"FETCHNOW_RELEASE_REVISION={target}" in env.read_text()
        assert prepared == [target]
        if mode == "idempotent":
            return RolloutResult(True, True, ())
        with pytest.raises(ActivateError, match="test-only gateway injection"):
            rollout.activate_gateway(target=True)
        if mode != "double_injection":
            rollout.activate_gateway(previous=True)
        return RolloutResult(
            False, False, (), status="failed" if mode == "failed" else "rolled_back"
        )

    monkeypatch.setattr(mod, "rollout_release", execute)
    kwargs = {
        "clone": tmp_path,
        "marker": marker,
        "env": env,
        "revision": REV,
        "prepare": prepared.append,
        "inp": lambda rev, **kw: SimpleNamespace(expected_revision=rev),
    }
    if mode == "ok":
        result, revision, evidence = mod.probe_distinct_revision_rollback(**kwargs)
        assert result.status == "rolled_back" and revision == target
        assert (
            evidence["gateway_injections"] == 1 and evidence["gateway_delegations"] == 1
        )
        assert delegations == [{"previous": True}]
    else:
        with pytest.raises(RuntimeError):
            mod.probe_distinct_revision_rollback(**kwargs)


@pytest.mark.parametrize(
    "failure", [None, "webm_mux", "wrong_streams", "empty", "release"]
)
def test_media_smoke_uses_worker_uds_for_both_formats(tmp_path, monkeypatch, failure):
    work, outputs = tmp_path / "executor", tmp_path / "outputs"
    work.mkdir()
    monkeypatch.setattr(media, "WORK", work)
    monkeypatch.setattr(media, "OUTPUTS", outputs)
    monkeypatch.setattr(media.os, "getuid", lambda: 10001)
    monkeypatch.setattr(media.os, "getgid", lambda: 10001)
    requests, generations = [], []
    containers = {}

    def call(payload):
        requests.append(payload)
        directory = work / f"{payload['job_id']}_1_1"
        op = payload["op"]
        result = {"ok": True, "code": "done", "exit_code": 0}
        if op == "reserve":
            directory.mkdir()
            result["code"] = "reserved"
        elif op == "mux_copy":
            containers[payload["job_id"]] = payload["container"]
            (directory / "output-mux").write_bytes(
                b"" if failure == "empty" else b"fixture"
            )
            if failure == "webm_mux" and payload["container"] == "webm":
                result["exit_code"] = 1
        elif op == "ffprobe_validate":
            streams = [{"codec_type": "video"}]
            if failure != "wrong_streams":
                streams.append({"codec_type": "audio"})
            result["stdout_b64"] = base64.b64encode(
                json.dumps({"streams": streams}).encode()
            ).decode()
        elif op == "release":
            result["code"] = "busy" if failure == "release" else "released"
        return result

    def generate(argv, **kw):
        generations.append(argv)
        assert kw["check"] is True and kw["timeout"] == 30
        Path(argv[-1]).write_bytes(b"input")

    if failure:
        with pytest.raises(RuntimeError):
            media.run_media(call=call, run=generate)
    else:
        result = media.run_media(call=call, run=generate)
        assert result["operations"] == ["mp4_mux_probe", "webm_mux_probe"]
        assert len(result["outputs"]) == 2 and len(generations) == 4
        assert list(containers.values()) == ["mp4", "webm"]
        assert not any(
            "ffprobe" in arg or arg == "copy" for argv in generations for arg in argv
        )
    ops = [r["op"] for r in requests]
    assert ops[-2:] == ["cancel", "release"]


def test_media_smoke_refuses_root(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "OUTPUTS", tmp_path / "output")
    monkeypatch.setattr(media.os, "getuid", lambda: 0)
    with pytest.raises(RuntimeError, match="worker identity"):
        media.run_media()
    assert not (tmp_path / "output").exists()


def test_positive_fixture_refuses_preexisting_volumes(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(mod, "_run", run)
    with pytest.raises(RuntimeError, match="pre-existing positive fixture volume"):
        mod.create_contract_volumes(
            "fetchnow-rollout-test-11111111",
            disposable_test_storage_contract(host_bind_root="/test"),
            lambda *a, **kw: None,
        )
    assert len(calls) == 1 and calls[0][:3] == ["docker", "volume", "inspect"]
