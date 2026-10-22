"""Release-engine cases S3–S8 for native media storage/resource acceptance.

Reuses the existing prepare/rollout engine (no second parallel release path).
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fetchnow_release.c3_constants import STATUS_ROLLED_BACK
from fetchnow_release.current_state import current_path
from fetchnow_release.journal import utc_now
from fetchnow_release.media_prerequisites import (
    MediaPrerequisiteError,
    assert_running_media_budget,
)
from fetchnow_release.media_state import load_media_activation_state
from fetchnow_release.media_storage import (
    LAYOUT_SUBDIRS,
    MediaStorageError,
    assert_media_storage_ready,
    assert_running_storage_mounts,
    default_findmnt,
    parse_media_storage_contract,
)
from fetchnow_release.prepare import PrepareInput, prepare_release
from fetchnow_release.resource_budget import parse_resource_budget
from fetchnow_release.rollout import RolloutInput, rollout_release
from fetchnow_release.rollout_project import make_rollout_project_name
from fetchnow_release.stabilize import StabilizationPolicy
from release_rollout_integration_test import (
    commit,
    compose_argv,
    ensure_rollout_fixture_compatibility_policy,
    wait_for_postgres,
    write_env,
)


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 120,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed rc={proc.returncode}: {' '.join(argv)}\n"
            f"stderr={proc.stderr[-2000:]}"
        )
    return proc


def run_release_cases(
    *,
    root: Path,
    work: Path,
    contract: dict[str, Any],
    budget_raw: dict[str, Any],
    provision_loop_fs: Callable[..., tuple[Path, str, Path, str]],
    safe_unmount: Callable[..., None],
    record: Callable[..., None],
    track: Callable[..., None],
    image_ids: dict[str, str],
    set_project: Callable[[str], None],
    set_slice: Callable[[str, Path], None],
) -> None:
    """Execute S3–S8 against a validated contract filesystem."""
    from fetchnow_release import rollout as rollout_mod

    clone = work / "clone"
    deploy = work / "deploy"
    deploy.mkdir(parents=True, exist_ok=True)
    _run(
        ["git", "clone", "--local", "--no-hardlinks", str(root), str(clone)],
        check=True,
        timeout=300,
    )
    _run(
        ["git", "config", "user.email", "media-storage-accept@example.test"],
        cwd=clone,
        check=True,
    )
    _run(
        ["git", "config", "user.name", "Media Storage Acceptance"],
        cwd=clone,
        check=True,
    )
    ensure_rollout_fixture_compatibility_policy(clone)

    project = make_rollout_project_name()
    set_project(project)
    track("compose_project", project)
    suffix = secrets.token_hex(6)
    marker = clone / "docs" / "media-storage-accept-marker.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(suffix, encoding="utf-8")
    revision = commit(clone, f"test: media storage acceptance {suffix}")

    env = work / "env"
    port = f"127.0.0.1:{18000 + secrets.randbelow(1800)}"
    write_env(
        env,
        project=project,
        revision=revision,
        gateway_port=port,
        deploy_root=deploy,
    )
    with env.open("a", encoding="utf-8") as stream:
        stream.write(
            "MEDIA_EXECUTOR_ENABLED=false\n"
            "MEDIA_NET_EXECUTOR_ENABLED=false\n"
            "EGRESS_DENY_CIDRS=172.16.0.0/12\n"
            "EGRESS_DENY_IPS=127.0.0.1\n"
        )

    budget = parse_resource_budget(budget_raw)
    slice_unit = f"fetchnow-media-storage-accept-{suffix}.slice"
    unit = Path("/run/systemd/system") / slice_unit
    if unit.exists():
        raise RuntimeError(f"refusing existing slice {slice_unit}")
    unit.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=FetchNow disposable media storage/resource acceptance",
                "[Slice]",
                f"MemoryMax={budget.aggregate_memory_max_bytes}",
                "MemorySwapMax=0",
                f"CPUQuota={budget.aggregate_cpu_quota_milli // 10}%",
                f"TasksMax={budget.aggregate_tasks_max}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    set_slice(slice_unit, unit)
    track("slice_unit", slice_unit)
    _run(["systemctl", "daemon-reload"], check=True)
    _run(["systemctl", "start", slice_unit], check=True)

    def prepare(rev: str) -> None:
        result = prepare_release(
            PrepareInput(
                project_name=project,
                env_file=env,
                compose_files=(clone / "compose.yaml", clone / "compose.staging.yaml"),
                expected_revision=rev,
                repo_root=clone,
                deploy_root=deploy,
                release_mode="media",
            )
        )
        if not result.ok:
            raise RuntimeError(f"prepare failed: {result.messages}")
        man = json.loads(
            (deploy / "releases" / rev / "release.json").read_text(encoding="utf-8")
        )
        for image in man.get("images") or []:
            image_ids[str(image["service"])] = str(image["id"])
            track(
                "image",
                str(image["id"]),
                reference=image.get("reference"),
            )

    def inp(rev: str, *, active: bool, bootstrap: bool = False) -> RolloutInput:
        return RolloutInput(
            project_name=project,
            env_file=env,
            expected_revision=rev,
            repo_root=clone,
            deploy_root=deploy,
            gateway_base_url=f"http://{port}",
            bootstrap=bootstrap,
            policy=StabilizationPolicy(
                consecutive_successes=2, interval_seconds=1, window_seconds=90
            ),
            media_activation=active,
            resource_budget=budget_raw if active else None,
            media_storage_contract=contract if active else None,
            required_deny_cidrs=("172.16.0.0/12",),
            required_deny_ips=("127.0.0.1",),
            media_denied_host="127.0.0.1",
            media_slice_unit=slice_unit,
        )

    compose = compose_argv(project, env, clone)
    prepare(revision)
    _run(compose + ["up", "-d", "postgres"], cwd=clone, check=True, timeout=300)
    wait_for_postgres(compose, clone)
    _run(
        compose + ["run", "--rm", "api", "alembic", "upgrade", "head"],
        cwd=clone,
        check=True,
        timeout=600,
    )

    off = rollout_release(inp(revision, active=False, bootstrap=True))
    if not off.ok:
        record(
            "S8_transaction_rollback",
            "FAIL",
            f"canonical OFF failed: {off.messages}",
        )
        raise RuntimeError("OFF FAIL")
    authority_off = current_path(deploy).read_bytes()

    # --- S5 legacy volume ---------------------------------------------------
    legacy_name = f"{project}_tmp"
    sentinel_text = "legacy-sentinel-" + secrets.token_hex(16)
    _run(["docker", "volume", "create", "--driver", "local", legacy_name], check=True)
    track("docker_volume", legacy_name, role="legacy_tmp")
    _run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{legacy_name}:/data",
            "alpine:3.20",
            "sh",
            "-c",
            f"printf '%s' '{sentinel_text}' > /data/sentinel.bin",
        ],
        check=True,
        timeout=120,
    )
    refused = False
    refuse_detail = ""
    try:
        attempt = rollout_release(inp(revision, active=True))
        if attempt.ok:
            refuse_detail = "ON unexpectedly succeeded with legacy volume"
        else:
            refused = True
            refuse_detail = ";".join(attempt.messages)
    except Exception as exc:  # noqa: BLE001
        refused = True
        refuse_detail = f"{type(exc).__name__}: {exc}"
    text_back = _run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{legacy_name}:/data",
            "alpine:3.20",
            "cat",
            "/data/sentinel.bin",
        ],
        check=True,
        timeout=60,
    ).stdout
    sentinel_ok = text_back == sentinel_text
    authority_same = current_path(deploy).read_bytes() == authority_off
    still = json.loads(
        _run(["docker", "volume", "inspect", legacy_name], check=True).stdout
    )[0]
    if not (
        refused
        and sentinel_ok
        and authority_same
        and still.get("Name") == legacy_name
    ):
        record(
            "S5_legacy_volume_refusal",
            "FAIL",
            refuse_detail or "legacy volume mutated or authority changed",
            refused=refused,
            sentinel_ok=sentinel_ok,
            authority_same=authority_same,
        )
        raise RuntimeError("S5 FAIL")
    record(
        "S5_legacy_volume_refusal",
        "PASS",
        "migration-required refusal; sentinel preserved",
        volume=legacy_name,
        refuse_detail=refuse_detail[:500],
    )
    _run(["docker", "volume", "rm", legacy_name], check=True)

    # --- S4 wrong / missing FS ----------------------------------------------
    wrong_parent = work / "wrong-fs"
    wrong_parent.mkdir()
    _w_img, w_loop, w_mnt, w_uuid = provision_loop_fs(
        size_bytes=64 * 1024 * 1024, prefix="wrong", parent=wrong_parent
    )
    for name in LAYOUT_SUBDIRS.values():
        (w_mnt / name).mkdir()
    wrong_contract = {
        **contract,
        "host_bind_root": str(w_mnt),
        "fs_uuid": w_uuid,
        "source_device": w_loop,
    }
    s4_rejected = False
    s4_detail = ""
    try:
        bad = rollout_release(
            RolloutInput(
                **{
                    **inp(revision, active=True).__dict__,
                    "media_storage_contract": wrong_contract,
                }
            )
        )
        if not bad.ok:
            s4_rejected = True
            s4_detail = ";".join(bad.messages)
        else:
            s4_detail = "wrong FS unexpectedly accepted"
    except Exception as exc:  # noqa: BLE001
        s4_rejected = True
        s4_detail = f"{type(exc).__name__}: {exc}"
    safe_unmount(w_mnt, w_loop, _w_img)
    try:
        assert_media_storage_ready(parse_media_storage_contract(wrong_contract))
        s4_rejected = False
        s4_detail += "; missing mount unexpectedly ready"
    except (MediaStorageError, Exception) as exc:  # noqa: BLE001
        s4_detail += f"; missing:{type(exc).__name__}"
    if not s4_rejected or current_path(deploy).read_bytes() != authority_off:
        record(
            "S4_missing_or_wrong_fs",
            "FAIL",
            s4_detail,
        )
        raise RuntimeError("S4 FAIL")
    record(
        "S4_missing_or_wrong_fs",
        "PASS",
        "wrong/missing FS refused before activation",
        detail=s4_detail[:500],
    )

    # --- OFF→ON success -----------------------------------------------------
    on = rollout_release(inp(revision, active=True))
    if not on.ok:
        record("S8_transaction_rollback", "FAIL", f"OFF→ON failed: {on.messages}")
        raise RuntimeError("ON FAIL")
    media = load_media_activation_state(deploy)
    if media is None or not media.activation:
        record("S3_container_mounts", "FAIL", "media authority not ON")
        raise RuntimeError("S3 FAIL")

    # Resolve the exact compose file list Docker used for this project.
    cid = _run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=media-executor",
        ],
        check=True,
    ).stdout.strip().split()
    if len(cid) != 1:
        record(
            "S3_container_mounts",
            "FAIL",
            f"expected one media-executor, got {len(cid)}",
        )
        raise RuntimeError("S3 FAIL")
    cfg = _run(
        [
            "docker",
            "inspect",
            "-f",
            '{{index .Config.Labels "com.docker.compose.project.config_files"}}',
            cid[0],
        ],
        check=True,
    ).stdout.strip()
    compose_files = tuple(Path(p) for p in cfg.split(",") if p)

    try:
        assert_running_storage_mounts(
            project_name=project,
            env_file=env,
            compose_files=compose_files,
            contract=parse_media_storage_contract(contract),
            sidecars_only=False,
        )
        record(
            "S3_container_mounts",
            "PASS",
            "worker/delivery/executors mounts on contract FS",
            compose_files=[str(p) for p in compose_files],
        )
    except MediaStorageError as exc:
        record("S3_container_mounts", "FAIL", str(exc))
        raise RuntimeError("S3 FAIL") from exc

    # --- S6 -----------------------------------------------------------------
    try:
        assert_running_media_budget(
            project_name=project,
            env_file=env,
            compose_files=compose_files,
            cwd=clone,
            budget=budget,
            slice_unit=slice_unit,
        )
        neg_ok = False
        try:
            assert_running_media_budget(
                project_name=project,
                env_file=env,
                compose_files=compose_files,
                cwd=clone,
                budget=budget,
                slice_unit="system.slice",
            )
        except MediaPrerequisiteError:
            neg_ok = True
        cg = _run(
            ["systemctl", "show", slice_unit, "-p", "ControlGroup", "--value"],
            check=True,
        ).stdout.strip()
        swap_val = (
            Path("/sys/fs/cgroup" + cg + "/memory.swap.max").read_text().strip()
        )
        if swap_val != "0" or not neg_ok:
            raise MediaPrerequisiteError(
                f"swap={swap_val!r} or negative parent check failed"
            )
        # Unlimited-swap negative: inspect MemorySwap == Memory already covered.
        record(
            "S6_sidecar_resources",
            "PASS",
            "cgroup ancestry, aggregate limits, swap=0, negatives",
            control_group=cg,
            memory_swap_max=swap_val,
            concurrency=budget.concurrency,
            concurrency_note=(
                "concurrency=1 is per executor; offline and network may "
                "still run simultaneously"
            ),
        )
    except Exception as exc:
        record("S6_sidecar_resources", "FAIL", str(exc))
        raise RuntimeError("S6 FAIL") from exc

    # --- S7 -----------------------------------------------------------------
    mount_point = Path(contract["host_bind_root"])
    fixtures = mount_point / "tmp" / "acceptance-fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    gen = _run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env),
            "-p",
            project,
            *[x for f in compose_files for x in ("-f", str(f))],
            "exec",
            "-T",
            "media-executor",
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            "/var/lib/fetchnow/tmp/acceptance-fixtures/clip.mp4",
        ],
        timeout=180,
    )
    if gen.returncode != 0:
        record("S7_media_operations", "FAIL", f"ffmpeg generate: {gen.stderr[-500:]}")
        raise RuntimeError("S7 FAIL")
    copy = _run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env),
            "-p",
            project,
            *[x for f in compose_files for x in ("-f", str(f))],
            "exec",
            "-T",
            "media-executor",
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "/var/lib/fetchnow/tmp/acceptance-fixtures/clip.mp4",
            "-c",
            "copy",
            "-y",
            "/var/lib/fetchnow/tmp/acceptance-fixtures/clip-copy.mp4",
        ],
        timeout=180,
    )
    if copy.returncode != 0:
        record("S7_media_operations", "FAIL", f"stream-copy: {copy.stderr[-500:]}")
        raise RuntimeError("S7 FAIL")
    probe = _run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env),
            "-p",
            project,
            *[x for f in compose_files for x in ("-f", str(f))],
            "exec",
            "-T",
            "media-executor",
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name",
            "-of",
            "csv=p=0",
            "/var/lib/fetchnow/tmp/acceptance-fixtures/clip-copy.mp4",
        ],
        timeout=60,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        record("S7_media_operations", "FAIL", f"ffprobe: {probe.stderr[-500:]}")
        raise RuntimeError("S7 FAIL")
    out = fixtures / "clip-copy.mp4"
    if not out.is_file():
        record("S7_media_operations", "FAIL", "stream-copy output missing on media FS")
        raise RuntimeError("S7 FAIL")
    identity = default_findmnt(out)
    if identity is None or identity.fs_uuid.lower() != str(contract["fs_uuid"]).lower():
        record("S7_media_operations", "FAIL", "media output not on contract FS")
        raise RuntimeError("S7 FAIL")
    # WebM stream-copy path when encoder available.
    webm_ok = False
    webm_gen = _run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env),
            "-p",
            project,
            *[x for f in compose_files for x in ("-f", str(f))],
            "exec",
            "-T",
            "media-executor",
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=0.5",
            "-c:v",
            "libvpx",
            "-y",
            "/var/lib/fetchnow/tmp/acceptance-fixtures/clip.webm",
        ],
        timeout=180,
    )
    if webm_gen.returncode == 0:
        webm_copy = _run(
            [
                "docker",
                "compose",
                "--env-file",
                str(env),
                "-p",
                project,
                *[x for f in compose_files for x in ("-f", str(f))],
                "exec",
                "-T",
                "media-executor",
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "/var/lib/fetchnow/tmp/acceptance-fixtures/clip.webm",
                "-c",
                "copy",
                "-y",
                "/var/lib/fetchnow/tmp/acceptance-fixtures/clip-copy.webm",
            ],
            timeout=180,
        )
        webm_ok = webm_copy.returncode == 0
    net_fix = mount_point / "media-net" / "acceptance-fixtures"
    net_fix.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out, net_fix / "clip-copy.mp4")
    net_probe = _run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env),
            "-p",
            project,
            *[x for f in compose_files for x in ("-f", str(f))],
            "exec",
            "-T",
            "media-net-executor",
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name",
            "-of",
            "csv=p=0",
            "/var/lib/fetchnow/media-net/acceptance-fixtures/clip-copy.mp4",
        ],
        timeout=60,
    )
    if net_probe.returncode != 0:
        record(
            "S7_media_operations",
            "FAIL",
            f"media-net ffprobe: {net_probe.stderr[-500:]}",
        )
        raise RuntimeError("S7 FAIL")
    record(
        "S7_media_operations",
        "PASS",
        "offline stream-copy+ffprobe; media-net local fixture; no external providers",
        ffprobe=probe.stdout.strip(),
        webm_ok=webm_ok,
        output_on_contract_fs=True,
    )

    # --- S8 rollback --------------------------------------------------------
    authority_on = current_path(deploy).read_bytes()
    media_before = load_media_activation_state(deploy)
    sentinel_path = mount_point / "tmp" / "rollback-sentinel.bin"
    sentinel_path.write_bytes(b"keep-me")

    def fail_gateway(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("injected activation failure for rollback proof")

    with patch.object(rollout_mod, "activate_gateway", fail_gateway):
        rolled = rollout_release(inp(revision, active=True))
    if rolled.ok:
        record(
            "S8_transaction_rollback",
            "FAIL",
            "injected ON failure unexpectedly succeeded",
        )
        raise RuntimeError("S8 FAIL")
    authority_after = current_path(deploy).read_bytes()
    media_after = load_media_activation_state(deploy)
    still_mounted = _run(["mountpoint", "-q", str(mount_point)]).returncode == 0
    if (
        authority_after != authority_on
        or media_after is None
        or media_before is None
        or media_after.activation != media_before.activation
        or not still_mounted
        or sentinel_path.read_bytes() != b"keep-me"
    ):
        record(
            "S8_transaction_rollback",
            "FAIL",
            "rollback mutated authority or storage",
            still_mounted=still_mounted,
        )
        raise RuntimeError("S8 FAIL")
    record(
        "S8_transaction_rollback",
        "PASS",
        "OFF ok; OFF→ON ok; ON activation failure restored prior ON; "
        "storage not formatted/unmounted; sentinel kept",
        status=rolled.status,
        utc=utc_now(),
        expected_rolled_back=STATUS_ROLLED_BACK,
    )
