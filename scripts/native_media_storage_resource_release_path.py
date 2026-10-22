"""Release-engine cases S3–S8 for native media storage/resource acceptance.

Reuses the existing prepare/rollout engine (no second parallel release path).
"""

from __future__ import annotations

import json
import secrets
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fetchnow_release.c3_constants import STATUS_ROLLED_BACK
from fetchnow_release.current_state import current_path
from fetchnow_release.egress_deny_coverage import assert_deny_coverage
from fetchnow_release.env_file import load_env_file, replace_env_file_key
from fetchnow_release.journal import utc_now
from fetchnow_release.manifest import ManifestError, load_manifest
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
    compose_bind_volume_specs,
    default_findmnt,
    parse_media_storage_contract,
)
from fetchnow_release.prepare import PrepareInput, prepare_release
from fetchnow_release.resource_budget import parse_resource_budget
from fetchnow_release.revision import validate_full_sha
from fetchnow_release.rollout import RolloutInput, RolloutResult, rollout_release
from fetchnow_release.rollout_project import make_rollout_project_name
from fetchnow_release.stabilize import StabilizationPolicy
from release_media_activation_integration_test import (
    gateway_fail_once,
    intended_gateway_injection_reached,
)
from release_rollout_integration_test import (
    commit,
    compose_argv,
    ensure_rollout_fixture_compatibility_policy,
    wait_for_postgres,
    write_env,
)

FIXTURE_DENY_CIDRS = ("172.16.0.0/12",)
FIXTURE_DENY_IPS = ("127.0.0.1",)


def write_fixture_env(
    path: Path, *, project: str, revision: str, gateway_port: str, deploy_root: Path
) -> None:
    """Both disposable projects need ALL prerequisites except the tested defect."""
    write_env(
        path,
        project=project,
        revision=revision,
        gateway_port=gateway_port,
        deploy_root=deploy_root,
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            "MEDIA_EXECUTOR_ENABLED=false\n"
            "MEDIA_NET_EXECUTOR_ENABLED=false\n"
            f"EGRESS_DENY_CIDRS={','.join(FIXTURE_DENY_CIDRS)}\n"
            f"EGRESS_DENY_IPS={','.join(FIXTURE_DENY_IPS)}\n"
        )
    values = load_env_file(path)
    assert_deny_coverage(
        deny_cidrs=values.get("EGRESS_DENY_CIDRS", ""),
        deny_ips=values.get("EGRESS_DENY_IPS", ""),
        required_cidrs=FIXTURE_DENY_CIDRS,
        required_ips=FIXTURE_DENY_IPS,
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


def record_prepared_images(
    path: Path,
    revision: str,
    image_ids: dict[str, str],
    track: Callable[..., None],
) -> None:
    """Use the release engine's schema; validate the whole set before recording."""
    manifest = load_manifest(path)
    if manifest.revision != revision or manifest.release_mode != "media":
        raise ManifestError("acceptance requires the requested media manifest")
    if manifest.build_platform != "linux/amd64" or any(
        image.platform != "linux/amd64" for image in manifest.images
    ):
        raise ManifestError("acceptance requires linux/amd64 image identities")
    for image in manifest.images:
        image_ids[image.service] = image.image_id
        track(
            "image",
            image.image_id,
            reference=image.reference,
            service=image.service,
            revision=image.oci_revision,
            platform=image.platform,
        )


def require_refusal(result: RolloutResult, *, reason: str) -> str:
    """A negative is proven by its domain reason, never an arbitrary exception."""
    detail = ";".join(result.messages)
    if result.ok or reason not in detail:
        raise RuntimeError(f"expected refusal {reason!r}; got: {detail}")
    return detail


def require_missing_mount_refusal(contract: dict[str, Any]) -> str:
    try:
        assert_media_storage_ready(parse_media_storage_contract(contract))
    except MediaStorageError as exc:
        detail = str(exc)
        if not any(
            reason in detail
            for reason in (
                "root filesystem",
                "mount identity unproven",
                "fs UUID",
                "fstype",
                "source ",
            )
        ):
            raise RuntimeError(f"unrelated missing-mount refusal: {detail}") from exc
        return detail
    raise RuntimeError("missing mount unexpectedly ready")


def create_contract_volumes(
    project: str, contract: dict[str, Any], track: Callable[..., None]
) -> None:
    """Prepare a NEW positive fixture, not a migration of a running OFF volume."""
    specs = compose_bind_volume_specs(parse_media_storage_contract(contract))
    for key, spec in specs.items():
        name = f"{project}_{key}"
        if _run(["docker", "volume", "inspect", name]).returncode == 0:
            raise RuntimeError(f"refusing pre-existing positive fixture volume: {name}")
        argv = ["docker", "volume", "create", "--driver", "local"]
        for option, value in spec["driver_opts"].items():
            argv.extend(["--opt", f"{option}={value}"])
        _run([*argv, name], check=True)
        track("docker_volume", name, role="positive_contract")


def create_fixture_revisions(clone: Path, marker: Path, suffix: str) -> tuple[str, str]:
    """Different projects need different revision tags, not two builds of one tag.

    Only the disposable documentation marker may differ. Do not weaken the
    release engine's occupied-image guard or reuse a stale manifest after retag.
    """
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(suffix + "-legacy", encoding="utf-8")
    legacy = validate_full_sha(commit(clone, f"test: legacy storage fixture {suffix}"))
    marker.write_text(suffix + "-positive", encoding="utf-8")
    positive = validate_full_sha(
        commit(clone, f"test: positive storage fixture {suffix}")
    )
    if legacy == positive:
        raise RuntimeError("legacy and positive fixture revisions must differ")
    _run(
        ["git", "merge-base", "--is-ancestor", legacy, positive], cwd=clone, check=True
    )
    changed = _run(
        ["git", "diff", "--name-only", "--no-renames", legacy, positive],
        cwd=clone,
        check=True,
    ).stdout.splitlines()
    if changed != [marker.relative_to(clone).as_posix()]:
        raise RuntimeError(
            "fixture revisions must differ only in the documentation marker"
        )
    return legacy, positive


def probe_distinct_revision_rollback(
    *,
    clone: Path,
    marker: Path,
    env: Path,
    revision: str,
    prepare: Callable[[str], None],
    inp: Callable[..., RolloutInput],
) -> tuple[RolloutResult, str, dict[str, object]]:
    """Exercise a real target transaction; inject only once so recovery is real."""
    from fetchnow_release import rollout as rollout_mod

    marker.write_text(marker.read_text() + "-rollback-target", encoding="utf-8")
    target_revision = commit(clone, "test: distinct media storage rollback target")
    if target_revision == revision:
        raise RuntimeError("S8 rollback target must differ from active revision")
    replace_env_file_key(env, "FETCHNOW_RELEASE_REVISION", target_revision)
    prepare(target_revision)
    fail_gateway, injection = gateway_fail_once(rollout_mod.activate_gateway)
    with patch.object(rollout_mod, "activate_gateway", fail_gateway):
        rolled = rollout_release(inp(target_revision, active=True))
    if (
        rolled.ok
        or rolled.status != STATUS_ROLLED_BACK
        or not intended_gateway_injection_reached(injection)
    ):
        raise RuntimeError(
            f"S8 intended gateway failure/rollback not proven: {injection}"
        )
    return rolled, target_revision, injection


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
    begin_case: Callable[[str], None] | None = None,
) -> None:
    """Execute S3–S8 against a validated contract filesystem."""
    start = begin_case if begin_case is not None else lambda _name: None
    start("S3_container_mounts")
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
    legacy_revision, revision = create_fixture_revisions(clone, marker, suffix)
    track("fixture_revision", legacy_revision, role="legacy")
    track("fixture_revision", revision, role="positive")

    env = work / "env"
    port = f"127.0.0.1:{18000 + secrets.randbelow(1800)}"
    write_fixture_env(
        env,
        project=project,
        revision=revision,
        gateway_port=port,
        deploy_root=deploy,
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

    def prepare(rev: str, *, p: str = project, e: Path = env, d: Path = deploy) -> None:
        result = prepare_release(
            PrepareInput(
                project_name=p,
                env_file=e,
                compose_files=(clone / "compose.yaml", clone / "compose.staging.yaml"),
                expected_revision=rev,
                repo_root=clone,
                deploy_root=d,
                release_mode="media",
            )
        )
        if not result.ok:
            raise RuntimeError(f"prepare failed: {result.messages}")
        record_prepared_images(
            d / "releases" / rev / "release.json", rev, image_ids, track
        )

    def inp(
        rev: str,
        *,
        active: bool,
        bootstrap: bool = False,
        p: str = project,
        e: Path = env,
        d: Path = deploy,
        gateway_port: str = port,
    ) -> RolloutInput:
        return RolloutInput(
            project_name=p,
            env_file=e,
            expected_revision=rev,
            repo_root=clone,
            deploy_root=d,
            gateway_base_url=f"http://{gateway_port}",
            bootstrap=bootstrap,
            policy=StabilizationPolicy(
                consecutive_successes=2, interval_seconds=1, window_seconds=90
            ),
            media_activation=active,
            resource_budget=budget_raw if active else None,
            media_storage_contract=contract if active else None,
            required_deny_cidrs=FIXTURE_DENY_CIDRS,
            required_deny_ips=FIXTURE_DENY_IPS,
            media_denied_host="127.0.0.1",
            media_slice_unit=slice_unit,
        )

    # The negative has its own OFF deployment, database, volume and authority.
    # Its legacy data stays in place until outer cleanup stops its containers.
    legacy_project = make_rollout_project_name()
    if legacy_project == project:
        raise RuntimeError("legacy and positive projects must differ")
    track("compose_project", legacy_project)
    legacy_env, legacy_deploy = work / "legacy-env", work / "legacy-deploy"
    legacy_port = f"127.0.0.1:{20000 + secrets.randbelow(1800)}"
    write_fixture_env(
        legacy_env,
        project=legacy_project,
        revision=legacy_revision,
        gateway_port=legacy_port,
        deploy_root=legacy_deploy,
    )
    prepare(legacy_revision, p=legacy_project, e=legacy_env, d=legacy_deploy)
    legacy_compose = compose_argv(legacy_project, legacy_env, clone)
    _run(legacy_compose + ["up", "-d", "postgres"], cwd=clone, check=True, timeout=300)
    wait_for_postgres(legacy_compose, clone)
    _run(
        legacy_compose + ["run", "--rm", "api", "alembic", "upgrade", "head"],
        cwd=clone,
        check=True,
        timeout=600,
    )
    legacy_off = rollout_release(
        inp(
            legacy_revision,
            active=False,
            bootstrap=True,
            p=legacy_project,
            e=legacy_env,
            d=legacy_deploy,
            gateway_port=legacy_port,
        )
    )
    if not legacy_off.ok:
        raise RuntimeError(f"legacy OFF fixture failed: {legacy_off.messages}")
    legacy_authority = current_path(legacy_deploy).read_bytes()

    compose = compose_argv(project, env, clone)
    prepare(revision)
    create_contract_volumes(project, contract, track)
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
            "S3_container_mounts",
            "FAIL",
            f"canonical OFF failed: {off.messages}",
        )
        raise RuntimeError("OFF FAIL")
    authority_off = current_path(deploy).read_bytes()

    # --- S5 legacy volume ---------------------------------------------------
    start("S5_legacy_volume_refusal")
    legacy_name = f"{legacy_project}_tmp"
    sentinel_text = "legacy-sentinel-" + secrets.token_hex(16)
    legacy_volume = json.loads(
        _run(["docker", "volume", "inspect", legacy_name], check=True).stdout
    )[0]
    if legacy_volume.get("Options"):
        raise RuntimeError("negative fixture is not a legacy root-backed volume")
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
    attempt = rollout_release(
        inp(
            legacy_revision,
            active=True,
            p=legacy_project,
            e=legacy_env,
            d=legacy_deploy,
            gateway_port=legacy_port,
        )
    )
    refuse_detail = require_refusal(attempt, reason="requires explicit data migration")
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
    authority_same = current_path(legacy_deploy).read_bytes() == legacy_authority
    still = json.loads(
        _run(["docker", "volume", "inspect", legacy_name], check=True).stdout
    )[0]
    if not (sentinel_ok and authority_same and still.get("Name") == legacy_name):
        record(
            "S5_legacy_volume_refusal",
            "FAIL",
            refuse_detail or "legacy volume mutated or authority changed",
            sentinel_ok=sentinel_ok,
            authority_same=authority_same,
        )
        raise RuntimeError("S5 FAIL")
    record(
        "S5_legacy_volume_refusal",
        "PASS",
        "migration-required refusal; sentinel preserved",
        volume=legacy_name,
        legacy_revision=legacy_revision,
        positive_revision=revision,
        refuse_detail=refuse_detail[:500],
    )
    # Do not remove an in-use volume or migrate it for the positive fixture.

    # --- S4 wrong / missing FS ----------------------------------------------
    start("S4_missing_or_wrong_fs")
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
    try:
        bad = rollout_release(
            RolloutInput(
                **{
                    **inp(revision, active=True).__dict__,
                    "media_storage_contract": wrong_contract,
                }
            )
        )
        s4_detail = require_refusal(bad, reason="outside approved bounds")
    finally:
        safe_unmount(w_mnt, w_loop, _w_img)
    s4_detail += "; missing:" + require_missing_mount_refusal(wrong_contract)
    if current_path(deploy).read_bytes() != authority_off:
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
        refusal_detail=s4_detail[:500],
    )

    # --- OFF→ON success -----------------------------------------------------
    start("S3_container_mounts")
    on = rollout_release(inp(revision, active=True))
    if not on.ok:
        record("S3_container_mounts", "FAIL", f"OFF→ON failed: {on.messages}")
        raise RuntimeError("ON FAIL")
    media = load_media_activation_state(deploy)
    if media is None or not media.activation:
        record("S3_container_mounts", "FAIL", "media authority not ON")
        raise RuntimeError("S3 FAIL")

    # Resolve the exact compose file list Docker used for this project.
    cid = (
        _run(
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
        )
        .stdout.strip()
        .split()
    )
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
    start("S6_sidecar_resources")
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
        swap_val = Path("/sys/fs/cgroup" + cg + "/memory.swap.max").read_text().strip()
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
    start("S7_media_operations")
    mount_point = Path(contract["host_bind_root"])
    media_compose = [
        "docker",
        "compose",
        "--env-file",
        str(env),
        "-p",
        project,
        *[x for f in compose_files for x in ("-f", str(f))],
    ]
    driver = (root / "scripts" / "native_media_storage_media_inside.py").read_text()
    smoke = _run(
        media_compose
        + ["exec", "-T", "--user", "10001:10001", "worker", "python", "-c", driver],
        check=True,
        timeout=300,
    )
    observed = json.loads(smoke.stdout)
    if (
        observed.get("ok") is not True
        or observed.get("transport") != "production_uds"
        or observed.get("operations") != ["mp4_mux_probe", "webm_mux_probe"]
        or observed.get("uid") != 10001
        or observed.get("gid") != 10001
        or observed.get("outputs")
        != [
            "/var/lib/fetchnow/tmp/acceptance-fixtures/clip-copy.mp4",
            "/var/lib/fetchnow/tmp/acceptance-fixtures/clip-copy.webm",
        ]
    ):
        raise RuntimeError("S7 incomplete worker media proof")
    output = mount_point / "tmp" / "acceptance-fixtures" / "clip-copy.mp4"
    for extension in ("mp4", "webm"):
        path = output.with_suffix("." + extension)
        identity = default_findmnt(path)
        if (
            not path.is_file()
            or path.stat().st_size <= 0
            or identity is None
            or identity.fs_uuid.lower() != str(contract["fs_uuid"]).lower()
        ):
            raise RuntimeError("S7 media output not on contract FS")

    # The network image intentionally has NO ffprobe. This is a local storage
    # fixture as its tool identity, not a claim of network/yt-dlp acceptance.
    net_code = (
        "import json,os; from pathlib import Path; "
        "assert os.getuid()==10003 and os.getgid()==10001; "
        "p=Path('/var/lib/fetchnow/media-net/acceptance-fixture.bin'); "
        "p.write_bytes(b'storage-fixture'); assert p.read_bytes()==b'storage-fixture'; "
        "print(json.dumps({'ok':True,'uid':os.getuid(),'gid':os.getgid()}))"
    )
    net = _run(
        media_compose
        + [
            "exec",
            "-T",
            "--user",
            "10003:10001",
            "media-net-executor",
            "python",
            "-c",
            net_code,
        ],
        check=True,
        timeout=30,
    )
    network_observed = json.loads(net.stdout)
    network_path = mount_point / "media-net" / "acceptance-fixture.bin"
    network_identity = default_findmnt(network_path)
    if (
        network_observed != {"ok": True, "uid": 10003, "gid": 10001}
        or network_path.read_bytes() != b"storage-fixture"
        or network_identity is None
        or network_identity.fs_uuid.lower() != str(contract["fs_uuid"]).lower()
    ):
        raise RuntimeError("S7 network local storage fixture failed")
    record(
        "S7_media_operations",
        "PASS",
        "worker UDS mux+ffprobe MP4 and WebM; network tool-uid local storage fixture",
        media=observed,
        output_on_contract_fs=True,
        network_fixture="local_write_read_only_not_yt_dlp_or_egress",
    )

    # --- S8 rollback --------------------------------------------------------
    start("S8_transaction_rollback")
    authority_on = current_path(deploy).read_bytes()
    media_before = load_media_activation_state(deploy)
    sentinel_path = mount_point / "tmp" / "rollback-sentinel.bin"
    sentinel_path.write_bytes(b"keep-me")

    rolled, target_revision, injection = probe_distinct_revision_rollback(
        clone=clone,
        marker=marker,
        env=env,
        revision=revision,
        prepare=prepare,
        inp=inp,
    )
    authority_after = current_path(deploy).read_bytes()
    media_after = load_media_activation_state(deploy)
    still_mounted = _run(["mountpoint", "-q", str(mount_point)]).returncode == 0
    if (
        authority_after != authority_on
        or media_after is None
        or media_before is None
        # Rollback republishes the previous authority with a fresh timestamp.
        # Compare ALL operational fields, not merely activation=true.
        or {**media_after.to_dict(), "updated_at_utc": None}
        != {**media_before.to_dict(), "updated_at_utc": None}
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
        injection=injection,
        previous_revision=revision,
        target_revision=target_revision,
        utc=utc_now(),
        expected_rolled_back=STATUS_ROLLED_BACK,
    )
