"""Offline regressions for six storage review findings; no native operations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from fetchnow_release import cli  # noqa: E402
from fetchnow_release import media_prerequisites as prereq  # noqa: E402
from fetchnow_release import media_storage as storage  # noqa: E402
from fetchnow_release.resource_budget import (  # noqa: E402
    disposable_test_budget,
    parse_resource_budget,
)


def contract_at(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    for name in ("tmp", "executor", "media-net"):
        (root / name).mkdir()
    return storage.parse_media_storage_contract(
        storage.disposable_test_storage_contract(host_bind_root=str(root))
    )


def identity(contract, **overrides):
    raw = dict(
        target=contract.host_bind_root,
        source=contract.source_device,
        fstype="ext4",
        fs_uuid=contract.fs_uuid,
        options="rw",
        size_bytes=None,
    )
    raw.update(overrides)
    return storage.MountIdentity(**raw)


@pytest.mark.parametrize(
    "bad",
    ["uuid", "prefix", "source", "root_missing", "root_uuid", "root_same", "readonly"],
)
def test_identity_is_fail_closed(tmp_path, bad):
    contract = contract_at(tmp_path)
    changes = {
        "uuid": {"fs_uuid": ""},
        "prefix": {"source": "/dev/testdisk-WRONG"},
        "source": {"source": ""},
        "readonly": {"options": "ro"},
    }

    def findmnt(path):
        if path == Path("/"):
            if bad == "root_missing":
                return None
            return (
                identity(contract)
                if bad == "root_same"
                else storage.MountIdentity(
                    "/",
                    "/dev/root",
                    "ext4",
                    "" if bad == "root_uuid" else "root-uuid",
                    "rw",
                    None,
                )
            )
        return identity(contract, **changes.get(bad, {}))

    with pytest.raises(storage.MediaStorageError):
        storage.assert_path_on_contract_fs(
            Path(contract.host_bind_root), contract, findmnt=findmnt, label="test"
        )


@pytest.mark.parametrize(
    "delta", [1, storage.CALCULATED_CAPACITY_BYTES * 9, -storage.FS_RESERVE_BYTES - 1]
)
def test_capacity_has_a_hard_upper_bound(tmp_path, delta):
    contract = contract_at(tmp_path)

    def findmnt(path):
        return (
            storage.MountIdentity("/", "/dev/root", "ext4", "root-uuid", "rw", None)
            if path == Path("/")
            else identity(contract)
        )

    def statfs(path):
        return SimpleNamespace(
            f_blocks=contract.capacity_bytes + delta,
            f_frsize=1,
            f_bavail=contract.capacity_bytes,
        )

    with pytest.raises(storage.MediaStorageError, match="capacity"):
        storage.assert_media_storage_ready(contract, findmnt=findmnt, statfs=statfs)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_volume_preflight_never_reconfigures_legacy_data(tmp_path, existing, legacy):
    contract = contract_at(tmp_path)
    volumes = storage.compose_bind_volume_specs(contract)
    for key, spec in volumes.items():
        spec["name"] = "project_" + key
    calls = []
    roles = {
        "worker": ("tmp", "executor", "media_net"),
        "media-executor": ("executor",),
        "media-net-executor": ("media_net",),
    }
    keys = {"tmp": "tmp", "executor": "executor-work", "media_net": "media-net-work"}
    rendered = {
        "volumes": volumes,
        "services": {
            service: {
                "volumes": [
                    {
                        "target": storage.MEDIA_ROOT_TARGETS[k],
                        "source": keys[k],
                        "type": "volume",
                    }
                    for k in roots
                ]
            }
            for service, roots in roles.items()
        },
    }

    def probe(argv):
        calls.append(argv)
        if "ls" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout_text="\n".join(v["name"] for v in volumes.values())
                if existing
                else "",
            )
        name = argv[-1]
        spec = next(v for v in volumes.values() if v["name"] == name)
        row = {
            "Name": name,
            "Driver": "local",
            "Options": {} if legacy else spec["driver_opts"],
        }
        return SimpleNamespace(returncode=0, stdout_text=json.dumps([row]))

    if existing and legacy:
        with pytest.raises(storage.MediaStorageError, match="explicit data migration"):
            storage.assert_rendered_storage_volumes(rendered, contract, probe=probe)
    else:
        storage.assert_rendered_storage_volumes(rendered, contract, probe=probe)
    assert all(a[2] in {"ls", "inspect"} for a in calls)


@pytest.mark.parametrize("bad", [None, "options", "source", "kernel", "missing"])
def test_actual_mounts_not_just_host_directory(tmp_path, bad):
    contract = contract_at(tmp_path)
    specs = storage.compose_bind_volume_specs(contract)
    mapping = {"tmp": "tmp", "executor": "executor-work", "media_net": "media-net-work"}
    roles = {
        "media-executor": ("executor",),
        "media-net-executor": ("media_net",),
        "worker": ("tmp", "executor", "media_net"),
        "api": ("tmp",),
        "delivery": ("tmp",),
    }

    def probe(argv):
        if "ps" in argv:
            return SimpleNamespace(returncode=0, stdout_text=argv[-1])
        if argv[1:3] == ["volume", "inspect"]:
            key = argv[-1]
            row = {
                "Name": key,
                "Driver": "local",
                "Options": {} if bad == "options" else specs[key]["driver_opts"],
                "Mountpoint": str(tmp_path / key),
            }
        else:
            service = argv[-1]
            mounts = [
                {
                    "Type": "volume",
                    "Name": mapping[k],
                    "Destination": storage.MEDIA_ROOT_TARGETS[k],
                    "Source": str(tmp_path / mapping[k]),
                    "RW": service != "delivery",
                }
                for k in roles[service]
            ]
            if bad == "missing":
                mounts = []
            elif bad == "source":
                mounts[0]["Source"] = "/legacy/root-volume"
            row = {"State": {"Running": True}, "Mounts": mounts}
        return SimpleNamespace(returncode=0, stdout_text=json.dumps([row]))

    def findmnt(path):
        if path == Path("/"):
            return storage.MountIdentity(
                "/", "/dev/root", "ext4", "root-uuid", "rw", None
            )
        return identity(
            contract, fs_uuid="wrong" if bad == "kernel" else contract.fs_uuid
        )

    kwargs = dict(
        project_name="project",
        env_file=Path("/env"),
        compose_files=(),
        contract=contract,
        probe=probe,
        findmnt=findmnt,
    )
    if bad:
        with pytest.raises(storage.MediaStorageError):
            storage.assert_running_storage_mounts(**kwargs)
    else:
        storage.assert_running_storage_mounts(**kwargs)


@pytest.mark.parametrize("swap", ["0", "max", "1"])
def test_swap_is_serialized_and_checked_at_ancestor(tmp_path, monkeypatch, swap):
    budget = disposable_test_budget()
    assert (
        parse_resource_budget(budget.to_dict()).to_dict()["memory_swap_max_bytes"] == 0
    )
    monkeypatch.setattr(prereq, "CGROUP_ROOT", tmp_path)
    directory = tmp_path / "proof.slice"
    directory.mkdir()
    for name, value in {
        "memory.max": str(budget.aggregate_memory_max_bytes),
        "memory.swap.max": swap,
        "pids.max": str(budget.aggregate_tasks_max),
        "cpu.max": "50000 100000",
    }.items():
        (directory / name).write_text(value)
    if swap == "0":
        prereq.assert_slice_budget("/proof.slice", budget)
    else:
        with pytest.raises(prereq.MediaPrerequisiteError):
            prereq.assert_slice_budget("/proof.slice", budget)


def test_public_cli_passes_storage_to_rollout(tmp_path, monkeypatch):
    payload = storage.disposable_test_storage_contract(host_bind_root="/media")
    path = tmp_path / "storage.json"
    path.write_text(json.dumps(payload))
    calls = []
    monkeypatch.setattr(
        cli,
        "rollout_release",
        lambda inp: calls.append(inp) or SimpleNamespace(ok=True, messages=[]),
    )
    rc = cli.main(
        [
            "rollout",
            "--env-file",
            str(tmp_path / "env"),
            "--expected-revision",
            "a" * 40,
            "--deploy-root",
            str(tmp_path / "deploy"),
            "--media-activation",
            "--media-storage-json",
            str(path),
        ]
    )
    assert rc == 0
    assert calls[0].media_storage_contract == payload


def test_managed_health_cannot_ignore_wrong_storage(tmp_path, monkeypatch):
    from fetchnow_release import health, media_health

    contract = contract_at(tmp_path)
    budget = disposable_test_budget()
    media = health.HealthMediaExpectation(
        activation=True,
        resource_budget=budget.to_dict(),
        storage_contract=contract.to_dict(),
    )
    inp = health.HealthInput(
        project_name="proof",
        env_file=tmp_path / "env",
        compose_files=(),
        expected_revision="a" * 40,
        repo_root=tmp_path,
    )
    monkeypatch.setattr(
        media_health,
        "run_media_health",
        lambda **kw: SimpleNamespace(ok=True, messages=[]),
    )
    monkeypatch.setattr(prereq, "assert_running_media_budget", lambda **kw: None)
    monkeypatch.setattr(storage, "assert_media_storage_ready", lambda *a: ())

    def fail_mounts(**kwargs):
        raise storage.MediaStorageError("actual mounts wrong")

    monkeypatch.setattr(storage, "assert_running_storage_mounts", fail_mounts)
    with pytest.raises(health.HealthError, match="actual mounts wrong"):
        health._assert_media_probes(inp=inp, media=media, messages=[])


def test_harness_guard_refuses_caller_directory_without_native_calls(tmp_path):
    script = ROOT / "scripts/native_media_storage_acceptance_harness.sh"
    # This takes the refusal branch before uname/root/mktemp/mount.
    env = {
        **os.environ,
        "FETCHNOW_NATIVE_MEDIA_STORAGE_HARNESS_EXECUTE": "1",
        "FETCHNOW_NATIVE_MEDIA_STORAGE_WORKDIR": str(tmp_path),
    }
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("preserve")
    result = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 2
    assert sentinel.read_text() == "preserve"


@pytest.mark.parametrize("failure", ["unmount", "detach"])
def test_cleanup_stops_before_deletion_on_failure(failure):
    text = (ROOT / "scripts/native_media_storage_acceptance_harness.sh").read_text()
    function = text[text.index("cleanup() {") : text.index("\non_exit() {")]
    # Only the cleanup function with shell-command stubs, not the native harness.
    setup = """
WORKDIR=/tmp/fetchnow-media-storage-proof.TEST
WORK_ID=owned
MNT=$WORKDIR/mnt
LOOP_FILE=$WORKDIR/media.img
LOOP_DEV=/dev/loop-test
stat() { echo owned; }
findmnt() { echo /dev/loop-test; }
umount() { return 1; }
rm() { echo DELETE_CALLED; }
"""
    setup += (
        "mountpoint() { return 0; }\n"
        if failure == "unmount"
        else "mountpoint() { return 1; }\n"
    )
    setup += 'losetup() { if [[ $1 == -d ]]; then return 1; fi; echo "$LOOP_FILE"; }\n'
    result = subprocess.run(
        ["bash", "-c", setup + function + "\ncleanup"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "DELETE_CALLED" not in result.stdout
