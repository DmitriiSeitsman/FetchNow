"""Corrective-pass contracts; no daemon, advisory API or host changes."""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from fetchnow_release import media_activation as activation
from fetchnow_release import media_health as health
from fetchnow_release import media_prerequisites as prereq
from fetchnow_release import media_rollout as media
from fetchnow_release.bounded_subprocess import BoundedResult
from fetchnow_release.resource_budget import disposable_test_budget


def active_plan():
    return activation.MediaActivationPlan(
        activation=True,
        release_mode="media",
        overlays=activation.MEDIA_OVERLAYS,
        profiles=activation.MEDIA_PROFILES,
        media_image_ids={
            key: "sha256:" + str(i) * 64
            for i, key in enumerate(activation.MEDIA_SIDECAR_START_ORDER, 1)
        },
        executor_settings={
            "MEDIA_EXECUTOR_ENABLED": "true",
            "MEDIA_NET_EXECUTOR_ENABLED": "true",
            "MEDIA_EXECUTOR_SOCKET": activation.DEFAULT_EXECUTOR_SOCKET,
            "MEDIA_NET_EXECUTOR_SOCKET": activation.DEFAULT_NET_SOCKET,
            "MEDIA_EXECUTOR_WORK_ROOT": activation.DEFAULT_EXECUTOR_WORK_ROOT,
            "MEDIA_NET_EXECUTOR_WORK_ROOT": activation.DEFAULT_NET_WORK_ROOT,
        },
        required_deny_cidrs=("172.18.0.0/16",),
        required_deny_ips=("195.209.215.87",),
        resource_budget=disposable_test_budget().to_dict(),
        slice_unit="fetchnow-test-media.slice",
        denied_host="195.209.215.87",
    )


def release_files(tmp_path):
    release = tmp_path / ("a" * 40)
    source = release / "source"
    source.mkdir(parents=True)
    for name in ("compose.yaml", *activation.MEDIA_OVERLAYS):
        (source / name).write_text("services: {}\n")
    override = tmp_path / "overrides" / "images.yaml"
    override.parent.mkdir()
    override.write_text("services: {}\n")
    return release, (source / "compose.yaml", override)


def test_immutable_override_last_and_settings_explicit(tmp_path):
    release, files = release_files(tmp_path)
    plan = active_plan()
    result = activation.compose_for_media_plan(release, base_compose=files, plan=plan)
    assert result[-1] == files[-1]
    assert [p.name for p in result] == [
        "compose.yaml",
        *activation.MEDIA_OVERLAYS,
        "media-settings.json",
        "images.yaml",
    ]
    payload = json.loads(result[-2].read_text())["services"]
    assert payload["worker"]["environment"] == plan.executor_settings
    for service, memory in plan.resource_budget["per_container_memory_bytes"].items():
        assert payload[service]["mem_limit"] == memory
        assert payload[service]["cgroup_parent"] == plan.slice_unit
    assert payload["media-executor"]["environment"]["MEDIA_EXECUTOR_MAX_JOBS"] == "1"
    assert (
        activation.compose_for_media_plan(release, base_compose=result, plan=plan)
        == result
    )


def test_off_plan_overrides_stale_true_env_without_media_services(tmp_path):
    release, files = release_files(tmp_path)
    result = activation.compose_for_media_plan(
        release,
        base_compose=files,
        plan=activation.build_source_only_plan(release_mode="canonical"),
    )
    assert [p.name for p in result] == [
        "compose.yaml",
        "media-settings.json",
        "images.yaml",
    ]
    assert json.loads(result[-2].read_text())["services"] == {
        "worker": {
            "environment": {
                "MEDIA_EXECUTOR_ENABLED": "false",
                "MEDIA_NET_EXECUTOR_ENABLED": "false",
            }
        }
    }


@pytest.mark.parametrize("bad", [False, None, "true"])
def test_unauthorized_or_malformed_protocol_is_not_ready(monkeypatch, bad):
    run_socket_probe(monkeypatch, ok=bad, expect_success=False)


def test_protocol_success_runs_as_worker_and_releases_reservation(monkeypatch):
    ops = run_socket_probe(monkeypatch, ok=True, expect_success=True)
    assert ops == ["reserve", "cancel", "release"]


def run_socket_probe(monkeypatch, *, ok, expect_success):
    ops = []

    class Socket:
        def __init__(self, *_args):
            pass

        def settimeout(self, _value):
            pass

        def connect(self, _path):
            pass

        def close(self):
            pass

        def sendall(self, data):
            self.op = json.loads(data)["op"]
            ops.append(self.op)

        def recv(self, _size):
            codes = {
                "reserve": "reserved",
                "cancel": "not_running",
                "release": "released",
            }
            return (
                json.dumps(
                    {
                        "v": 1,
                        "ok": ok,
                        "code": codes[self.op] if ok is True else "unauthorized",
                    }
                ).encode()
                + b"\n"
            )

    real_import = builtins.__import__

    def importer(name, *args, **kwargs):
        if name == "socket":
            return SimpleNamespace(AF_UNIX=1, SOCK_STREAM=1, socket=Socket)
        if name == "os":
            return SimpleNamespace(
                lstat=lambda _: SimpleNamespace(st_mode=stat.S_IFSOCK)
            )
        return real_import(name, *args, **kwargs)

    def probe(argv, **_kwargs):
        assert argv[argv.index("--user") + 1] == "10001:10001"
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                exec(
                    argv[-1],
                    {"__builtins__": {**vars(builtins), "__import__": importer}},
                )
            rc = 0
        except SystemExit:
            rc = 1
        return BoundedResult(tuple(argv), rc, output.getvalue().encode(), b"")

    monkeypatch.setattr(health, "run_docker_probe", probe)
    kwargs = dict(
        project_name="fetchnow-test",
        env_file=Path("/env"),
        compose_files=(),
        cwd=Path("/"),
        service="media-executor",
        socket_path="/sock",
        label="executor",
    )
    if expect_success:
        health.assert_executor_socket_ready(**kwargs)
    else:
        with pytest.raises(health.MediaHealthError):
            health.assert_executor_socket_ready(**kwargs)
    return ops


def test_actual_ancestor_limits_must_match_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(prereq, "CGROUP_ROOT", tmp_path)
    directory = tmp_path / "test.slice"
    directory.mkdir()
    budget = disposable_test_budget()
    (directory / "memory.max").write_text(str(budget.aggregate_memory_max_bytes))
    (directory / "pids.max").write_text(str(budget.aggregate_tasks_max))
    (directory / "cpu.max").write_text("50000 100000")
    prereq.assert_slice_budget("/test.slice", budget)
    for file in ("memory.max", "pids.max", "cpu.max"):
        original = (directory / file).read_text()
        (directory / file).write_text("max 100000" if file == "cpu.max" else "max")
        with pytest.raises(prereq.MediaPrerequisiteError):
            prereq.assert_slice_budget("/test.slice", budget)
        (directory / file).write_text(original)
    (directory / "memory.max").write_text(str(budget.aggregate_memory_max_bytes * 2))
    with pytest.raises(prereq.MediaPrerequisiteError, match="match"):
        prereq.assert_slice_budget("/test.slice", budget)


def test_systemctl_output_is_keyed_not_order_dependent(tmp_path, monkeypatch):
    unit = tmp_path / "test.slice"
    unit.write_text("[Slice]\n")
    monkeypatch.setattr(prereq.shutil, "which", lambda _: "/bin/systemctl")
    monkeypatch.setattr(
        prereq,
        "_probe",
        lambda argv: BoundedResult(
            tuple(argv),
            0,
            f"MemoryMax=123\nControlGroup=/nested/test.slice\nFragmentPath={unit}\nLoadState=loaded\n".encode(),
            b"",
        ),
    )
    assert prereq.assert_slice_control_group("test.slice") == "/nested/test.slice"


def test_docker_version_prevents_unsupported_writable_cgroups(monkeypatch):
    payload = {
        "SecurityOptions": [],
        "CgroupDriver": "systemd",
        "CgroupVersion": "2",
        "ServerVersion": "27.5.1",
    }
    monkeypatch.setattr(
        prereq,
        "_probe",
        lambda argv: BoundedResult(tuple(argv), 0, json.dumps(payload).encode(), b""),
    )
    with pytest.raises(prereq.MediaPrerequisiteError, match=">=28"):
        prereq.assert_rootful_docker_writable_cgroups()


@pytest.mark.parametrize("bad", [None, "memory", "scope", "namespace", "concurrency"])
def test_running_budget_validates_real_container_contract(monkeypatch, bad):
    budget = disposable_test_budget()
    monkeypatch.setattr(
        prereq, "assert_slice_control_group", lambda *a, **k: "/test.slice"
    )
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if str(path).startswith("/proc/"):
            return (
                "0::/test.slice\n"
                if bad == "scope"
                else "0::/test.slice/docker-proof.scope\n"
            )
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    selected = []

    def probe(argv):
        if "inspect" not in argv:
            selected[:] = [argv[-1]]
            return BoundedResult(tuple(argv), 0, b"cid\n", b"")
        service = selected[0]
        row = {
            "State": {"Running": True, "Pid": 123},
            "Config": {
                "Env": [
                    "MEDIA_EXECUTOR_MAX_JOBS=2"
                    if bad == "concurrency"
                    else "MEDIA_EXECUTOR_MAX_JOBS=1"
                ]
            },
            "HostConfig": {
                "Memory": budget.per_container_memory_bytes[service]
                * (2 if bad == "memory" else 1),
                "CgroupParent": "test.slice",
                "CgroupnsMode": "host" if bad == "namespace" else "private",
                "CapDrop": ["ALL"],
                "CapAdd": ["CAP_SETUID", "CAP_SETGID", "CAP_SETPCAP"],
                "Privileged": False,
            },
        }
        return BoundedResult(tuple(argv), 0, json.dumps([row]).encode(), b"")

    monkeypatch.setattr(prereq, "_probe", probe)
    kwargs = dict(
        project_name="fetchnow-test",
        env_file=Path("/env"),
        compose_files=(),
        cwd=Path("/"),
        budget=budget,
        slice_unit="test.slice",
    )
    if bad is None:
        prereq.assert_running_media_budget(**kwargs)
    else:
        with pytest.raises(prereq.MediaPrerequisiteError):
            prereq.assert_running_media_budget(**kwargs)


def test_native_harness_not_run_is_nonzero_and_creates_nothing(monkeypatch, tmp_path):
    import release_media_activation_integration_test as harness

    monkeypatch.setattr(harness.platform, "system", lambda: "Darwin")
    evidence = tmp_path / "not-run"
    assert harness.main(["--evidence-dir", str(evidence)]) == 2
    assert not evidence.exists()


def test_native_harness_uses_real_bounded_command(tmp_path):
    import release_media_activation_integration_test as harness

    assert (
        harness.checked([sys.executable, "-c", "print('bounded')"], tmp_path)
        == "bounded"
    )


def test_active_recovery_requires_budget_and_proxy_target():
    raw = active_plan().to_dict()
    assert media.media_plan_from_journal(raw).resource_budget == raw["resource_budget"]
    for field in ("resource_budget", "denied_host"):
        broken = {**raw, field: None}
        with pytest.raises((ValueError, media.MediaRolloutError)):
            media.media_plan_from_journal(broken)


def patch_transaction(monkeypatch, tmp_path):
    from fetchnow_release import rollout
    from fetchnow_release.current_state import (
        ApplicationState,
        DatabaseState,
        ResolvedCurrentState,
    )
    from test_rollout_unit import _ids, _patch_bootstrap_rollout, _write_release_compose

    deploy = tmp_path / "deploy"
    release = deploy / "releases" / ("a" * 40)
    _write_release_compose(release, storage_init=False)
    for overlay in activation.MEDIA_OVERLAYS:
        (release / "source" / overlay).write_text("services: {}\n")
    calls = _patch_bootstrap_rollout(monkeypatch, deploy)
    current = ResolvedCurrentState(
        schema_version=2,
        application=ApplicationState(
            revision=release.name,
            release_manifest_sha256="e" * 64,
            image_ids=_ids(),
            deployment_id="11111111-1111-1111-1111-111111111111",
        ),
        database=DatabaseState(
            heads=("0001_baseline",),
            schema_release_revision=release.name,
            last_migration_id=None,
            compatibility_contract_sha256=None,
            compatible_application_revisions=(release.name,),
        ),
        updated_at_utc="2026-01-01T00:00:00Z",
        legacy_v1=False,
    )
    monkeypatch.setattr(
        rollout, "load_and_resolve_current_state", lambda *a, **k: current
    )
    monkeypatch.setattr(
        rollout,
        "resolve_media_plan_for_rollout",
        lambda **k: (active_plan(), active_plan().to_dict()),
    )
    monkeypatch.setattr(
        rollout,
        "media_image_ids_from_manifest",
        lambda _: active_plan().media_image_ids,
    )
    monkeypatch.setattr(
        rollout,
        "activate_media_before_worker",
        lambda **k: calls.append("media-before-worker") or k["base_compose"],
    )
    monkeypatch.setattr(
        health, "run_media_health", lambda **k: health.MediaHealthResult(True, ("OK",))
    )
    monkeypatch.setattr(
        rollout, "verify_media_plan", lambda **k: calls.append("verify-media")
    )
    monkeypatch.setattr(
        rollout, "restore_previous_media", lambda **k: k["base_compose"]
    )
    env = tmp_path / "env"
    env.write_text("MEDIA_EXECUTOR_ENABLED=false\nMEDIA_NET_EXECUTOR_ENABLED=false\n")
    inp = rollout.RolloutInput(
        project_name="fetchnow-rollout-test-11111111",
        env_file=env,
        expected_revision=release.name,
        repo_root=tmp_path / "repo",
        deploy_root=deploy,
        media_activation=True,
        media_denied_host="127.0.0.1",
    )
    return rollout, current, inp, calls


def test_same_revision_off_to_on_is_a_transaction(monkeypatch, tmp_path):
    rollout, current, inp, calls = patch_transaction(monkeypatch, tmp_path)
    monkeypatch.setattr(
        rollout,
        "_verify_already_active",
        lambda *a, **k: pytest.fail("canonical fast path must not run"),
    )
    result = rollout.rollout_release(inp)
    assert result.ok and not result.already_active, result.messages
    assert calls.index("media-before-worker") < calls.index("activate")


def test_post_current_publication_failure_restores_exact_authority(
    monkeypatch, tmp_path
):
    from fetchnow_release.current_state import current_path, write_current_state

    rollout, current, inp, calls = patch_transaction(monkeypatch, tmp_path)
    write_current_state(inp.deploy_root, current)
    previous = current_path(inp.deploy_root).read_bytes()
    monkeypatch.setattr(rollout, "write_current_state", write_current_state)
    original = rollout.commit_media_state
    attempts = []

    def publication(**kwargs):
        attempts.append(kwargs["plan"].activation)
        if kwargs["plan"].activation:
            raise OSError("media state write failed after current publication")
        return original(**kwargs)

    monkeypatch.setattr(rollout, "commit_media_state", publication)
    result = rollout.rollout_release(inp)
    assert result.status == "rolled_back", result.messages
    assert current_path(inp.deploy_root).read_bytes() == previous
    assert attempts == [True, False]
    assert "verify-media" in calls


def test_failed_bootstrap_removes_sidecars_and_retracts_owned_current(
    monkeypatch, tmp_path
):
    from dataclasses import replace
    from fetchnow_release import bootstrap_cleanup
    from fetchnow_release.current_state import write_current_state, current_path

    rollout, current, inp, calls = patch_transaction(monkeypatch, tmp_path)
    monkeypatch.setattr(rollout, "load_and_resolve_current_state", lambda *a, **k: None)
    monkeypatch.setattr(rollout, "write_current_state", write_current_state)

    def publication(**kwargs):
        raise OSError("media publication failed")

    monkeypatch.setattr(rollout, "commit_media_state", publication)
    cleanups = []

    def owned(**kwargs):
        assert "egress-proxy" in kwargs["allowed_services"]
        cleanups.append(kwargs)
        return [
            bootstrap_cleanup.RemovedBootstrapContainer(
                container_id="d" * 64,
                service="egress-proxy",
                labels={
                    "com.docker.compose.project": kwargs["project_name"],
                    "com.docker.compose.service": "egress-proxy",
                    "com.fetchnow.deployment-id": kwargs["deployment_id"],
                    "com.fetchnow.release-revision": kwargs["release_revision"],
                },
            )
        ]

    monkeypatch.setattr(bootstrap_cleanup, "discover_owned_bootstrap_containers", owned)
    monkeypatch.setattr(
        rollout,
        "remove_owned_bootstrap_containers",
        lambda **kwargs: tuple(owned(**kwargs)),
    )
    result = rollout.rollout_release(replace(inp, bootstrap=True))
    assert result.status == "failed", result.messages
    assert len(cleanups) == 2
    assert not current_path(inp.deploy_root).exists()


def test_failed_bootstrap_never_retracts_foreign_authority(monkeypatch, tmp_path):
    from fetchnow_release.current_state import write_current_state, current_path

    rollout, current, inp, calls = patch_transaction(monkeypatch, tmp_path)
    write_current_state(inp.deploy_root, current)
    before = current_path(inp.deploy_root).read_bytes()
    with pytest.raises(media.MediaRolloutError, match="foreign"):
        media.remove_failed_bootstrap_authority(
            inp.deploy_root,
            deployment_id="22222222-2222-2222-2222-222222222222",
            revision=current.revision,
        )
    assert current_path(inp.deploy_root).read_bytes() == before


@pytest.mark.parametrize("healthy", [True, False])
def test_accept_target_requires_media_health_before_publication(
    monkeypatch, tmp_path, healthy
):
    from fetchnow_release import recover
    from fetchnow_release.verify_release import VerifyResult

    release, files = release_files(tmp_path)
    ids = {
        "api": "sha256:" + "7" * 64,
        "worker": "sha256:" + "7" * 64,
        "web": "sha256:" + "8" * 64,
        "gateway": "sha256:" + "9" * 64,
    }
    plan = SimpleNamespace(
        target_revision=release.name,
        release_manifest_sha256="e" * 64,
        target_image_ids=ids,
        deployment_id="11111111-1111-1111-1111-111111111111",
        database_heads=("0001_baseline",),
        bootstrap=True,
        media=active_plan().to_dict(),
    )
    inp = recover.RecoverInput(
        project_name="fetchnow-test",
        env_file=tmp_path / "env",
        deploy_root=tmp_path,
        repo_root=tmp_path,
        deployment_id=plan.deployment_id,
        action="accept-target",
    )
    monkeypatch.setattr(recover, "release_dir", lambda *a: release)
    monkeypatch.setattr(
        recover, "verify_prepared_release", lambda *a, **k: VerifyResult(True, ("OK",))
    )
    monkeypatch.setattr(recover, "sha256_file", lambda _: "e" * 64)
    monkeypatch.setattr(recover, "load_manifest", lambda _: object())
    monkeypatch.setattr(recover, "assert_release_images_present", lambda _: ids)
    monkeypatch.setattr(recover, "_release_compose_files", lambda *a, **k: (files[0],))
    monkeypatch.setattr(
        recover, "database_heads_via_postgres", lambda **k: frozenset({"0001_baseline"})
    )
    monkeypatch.setattr(
        recover,
        "target_heads_from_release_source",
        lambda _: frozenset({"0001_baseline"}),
    )
    monkeypatch.setattr(recover, "load_and_resolve_current_state", lambda *a, **k: None)
    monkeypatch.setattr(recover, "stabilize_full_health", lambda *a, **k: None)
    calls = []

    def verify(**kwargs):
        calls.append("media-health")
        assert kwargs["plan"].media_image_ids == active_plan().media_image_ids
        if not healthy:
            raise media.MediaRolloutError("proxy stopped")

    monkeypatch.setattr(recover, "verify_media_plan", verify)
    monkeypatch.setattr(
        recover, "write_current_state", lambda *a, **k: calls.append("current")
    )
    monkeypatch.setattr(
        recover, "commit_media_state", lambda **k: calls.append("media-state")
    )
    kwargs = dict(
        inp=inp,
        deploy=tmp_path,
        plan=plan,
        dep_dir=tmp_path / "dep",
        compose_override_dir=tmp_path / "overrides",
    )
    if healthy:
        assert recover._execute_accept_target(**kwargs) == release.name
        assert calls == ["media-health", "current", "media-state"]
    else:
        with pytest.raises(media.MediaRolloutError):
            recover._execute_accept_target(**kwargs)
        assert calls == ["media-health"]


def test_off_to_on_cleanup_uses_failed_release_not_legacy(monkeypatch, tmp_path):
    from fetchnow_release import bootstrap_cleanup, environment, manifest

    failed, files = release_files(tmp_path)
    previous = tmp_path / ("b" * 40)
    (previous / "source").mkdir(parents=True)
    (previous / "source" / "compose.yaml").write_text("services: {}\n")
    calls = []
    monkeypatch.setattr(
        environment, "snapshot_compose_files_for_release", lambda *a, **k: (files[0],)
    )
    monkeypatch.setattr(manifest, "load_manifest", lambda _: object())
    monkeypatch.setattr(
        bootstrap_cleanup,
        "remove_owned_bootstrap_containers",
        lambda **k: calls.append(k),
    )
    result = media.restore_previous_media(
        project_name="fetchnow-test",
        env_file=tmp_path / "env",
        previous_release=previous,
        base_compose=(previous / "source" / "compose.yaml", files[-1]),
        cwd=previous / "source",
        plan_media={"activation": True, "previous_activation": False},
        deploy_root=tmp_path,
        previous_revision=previous.name,
        updated_at_utc="2026-01-01T00:00:00Z",
        failed_release=failed,
        deployment_id="11111111-1111-1111-1111-111111111111",
    )
    assert calls[0]["release_revision"] == failed.name
    assert calls[0]["deployment_id"] == "11111111-1111-1111-1111-111111111111"
    assert calls[0]["allowed_services"] == (
        "media-executor",
        "media-net-executor",
        "egress-proxy",
    )
    assert (
        json.loads(result[-2].read_text())["services"]["worker"]["environment"][
            "MEDIA_EXECUTOR_ENABLED"
        ]
        == "false"
    )
    assert not (tmp_path / "state" / "media-activation.json").exists()

    def failure(**kwargs):
        raise bootstrap_cleanup.BootstrapCleanupError("rm timeout")

    monkeypatch.setattr(bootstrap_cleanup, "remove_owned_bootstrap_containers", failure)
    with pytest.raises(bootstrap_cleanup.BootstrapCleanupError):
        media.restore_previous_media(
            project_name="fetchnow-test",
            env_file=tmp_path / "env",
            previous_release=previous,
            base_compose=(previous / "source" / "compose.yaml", files[-1]),
            cwd=previous / "source",
            plan_media={"activation": True, "previous_activation": False},
            deploy_root=tmp_path,
            previous_revision=previous.name,
            updated_at_utc="2026-01-01T00:00:00Z",
            failed_release=failed,
            deployment_id="11111111-1111-1111-1111-111111111111",
        )


def test_media_state_journal_keeps_budget_and_previous_authority(tmp_path):
    from fetchnow_release.journal import _parse_optional_media_plan

    media.commit_media_state(
        deploy_root=tmp_path,
        revision="a" * 40,
        plan=active_plan(),
        updated_at_utc="2026-01-01T00:00:00Z",
    )
    previous = media.previous_media_for_journal(tmp_path)
    block = media.journal_media_block(target=active_plan(), previous=previous)
    parsed = _parse_optional_media_plan(block)
    assert parsed["resource_budget"] == active_plan().resource_budget
    assert (
        media.media_plan_from_journal(parsed, previous=True).resource_budget
        == active_plan().resource_budget
    )


def test_bootstrap_cleanup_includes_owned_sidecars_only(monkeypatch):
    from fetchnow_release import bootstrap_cleanup as cleanup

    monkeypatch.setattr(
        cleanup,
        "_compose_ps_json",
        lambda **_: [
            {"Service": "media-executor", "ID": "owned"},
            {"Service": "media-net-executor", "ID": "foreign"},
            {"Service": "postgres", "ID": "db"},
        ],
    )

    def labels(cid):
        return {
            "com.docker.compose.project": "fetchnow-test",
            "com.docker.compose.service": "media-executor"
            if cid == "owned"
            else "media-net-executor",
            "com.fetchnow.deployment-id": "dep" if cid == "owned" else "foreign",
            "com.fetchnow.release-revision": "rev",
        }

    monkeypatch.setattr(cleanup, "_inspect_labels", labels)
    result = cleanup.discover_owned_bootstrap_containers(
        project_name="fetchnow-test",
        env_file=Path("/env"),
        compose_files=(),
        cwd=Path("/"),
        deployment_id="dep",
        release_revision="rev",
        allowed_services=activation.MEDIA_SIDECAR_START_ORDER,
    )
    assert [row.container_id for row in result] == ["owned"]
