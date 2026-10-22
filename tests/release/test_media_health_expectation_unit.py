"""Media-aware health classification: orphans, sidecars, managed authority."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from fetchnow_release.health import (  # noqa: E402
    HealthInput,
    HealthMediaExpectation,
    health_media_from_plan,
    run_health,
    validate_health_media_expectation,
)
from fetchnow_release.media_activation import (  # noqa: E402
    MEDIA_SIDECAR_START_ORDER,
    MediaActivationPlan,
)
from fetchnow_release.media_storage import disposable_test_storage_contract  # noqa: E402
from fetchnow_release.resource_budget import disposable_test_budget  # noqa: E402
from fetchnow_release.stabilize import StabilizeError, stabilize_full_health  # noqa: E402


REV = "a" * 40
PROJECT = "fetchnow-health-test-mediaaaaa"


def _ids() -> dict[str, str]:
    return {
        "api": "sha256:" + ("1" * 64),
        "worker": "sha256:" + ("1" * 64),
        "web": "sha256:" + ("2" * 64),
        "gateway": "sha256:" + ("3" * 64),
    }


def _media_ids() -> dict[str, str]:
    return {
        key: "sha256:" + str(i) * 64
        for i, key in enumerate(MEDIA_SIDECAR_START_ORDER, 4)
    }


def _active_plan() -> MediaActivationPlan:
    budget = disposable_test_budget().to_dict()
    return MediaActivationPlan(
        activation=True,
        release_mode="media",
        overlays=(
            "compose.media-executor.yaml",
            "compose.media-net-executor.yaml",
        ),
        profiles=("media-executor", "media-net-executor"),
        media_image_ids=_media_ids(),
        executor_settings={
            "MEDIA_EXECUTOR_ENABLED": "true",
            "MEDIA_NET_EXECUTOR_ENABLED": "true",
            "MEDIA_EXECUTOR_SOCKET": "/run/fetchnow-executor/ctrl/worker.sock",
            "MEDIA_NET_EXECUTOR_SOCKET": "/run/fetchnow-media-net/ctrl/worker.sock",
            "MEDIA_EXECUTOR_WORK_ROOT": "/var/lib/fetchnow/executor",
            "MEDIA_NET_EXECUTOR_WORK_ROOT": "/var/lib/fetchnow/media-net",
        },
        required_deny_cidrs=("172.16.0.0/12",),
        required_deny_ips=("127.0.0.1",),
        storage_contract=disposable_test_storage_contract(
            host_bind_root="/var/lib/fetchnow-test-media-storage"
        ),
        resource_budget=budget,
        slice_unit="fetchnow-test-media.slice",
        denied_host="127.0.0.1",
    )


def _canonical_rows(rev: str = REV) -> list[dict]:
    rows = []
    for svc in ("gateway", "api", "worker", "delivery", "postgres", "web"):
        rows.append(
            {
                "Service": svc,
                "State": "running",
                "Health": "healthy",
                "ID": f"cid-{svc}",
                "Image": f"fetchnow-api:{rev}",
            }
        )
    return rows


def _media_rows() -> list[dict]:
    return [
        {
            "Service": svc,
            "State": "running",
            "Health": "",
            "ID": f"cid-{svc}",
            "Image": f"fetchnow-{svc}:x",
        }
        for svc in MEDIA_SIDECAR_START_ORDER
    ]


def _inspect_factory(
    *,
    project: str = PROJECT,
    rev: str = REV,
    ids: dict[str, str] | None = None,
    media_ids: dict[str, str] | None = None,
    bad_label: tuple[str, str] | None = None,
):
    app_ids = ids or _ids()
    media = media_ids or _media_ids()

    def inspect(cid: str) -> dict:
        svc = cid.removeprefix("cid-")
        if svc in {"api", "worker", "delivery"}:
            image = app_ids["api"]
        elif svc in app_ids:
            image = app_ids[svc]
        elif svc in media:
            image = media[svc]
        else:
            image = f"postgres:{svc}"
        project_label = project
        service_label = svc
        if bad_label and bad_label[0] == svc:
            kind, value = bad_label[1].split("=", 1)
            if kind == "project":
                project_label = value
            else:
                service_label = value
        health = None if svc in MEDIA_SIDECAR_START_ORDER else {"Status": "healthy"}
        labels = {
            "com.docker.compose.project": project_label,
            "com.docker.compose.service": service_label,
        }
        if svc != "postgres":
            labels["org.opencontainers.image.revision"] = rev
        return {
            "Image": image,
            "RestartCount": 0,
            "Config": {
                "Image": image,
                "Labels": labels,
            },
            "State": {"Status": "running", "Health": health},
        }

    return inspect


def _patch_health_transport(monkeypatch, rows, inspect=None):
    monkeypatch.setattr("fetchnow_release.health.require_compose_v2", lambda: None)
    monkeypatch.setattr("fetchnow_release.health.require_docker_daemon", lambda: None)
    monkeypatch.setattr(
        "fetchnow_release.health.compose_files_include_delivery", lambda _paths: True
    )
    monkeypatch.setattr("fetchnow_release.health._ps_services", lambda _inp: rows)
    monkeypatch.setattr(
        "fetchnow_release.health._inspect",
        inspect or _inspect_factory(),
    )
    monkeypatch.setattr(
        "fetchnow_release.health.check_endpoint",
        lambda *_a, **_k: SimpleNamespace(ok=True, attempts=1, detail="ok"),
    )


def test_off_canonical_pass(monkeypatch, tmp_path):
    _patch_health_transport(monkeypatch, _canonical_rows())
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            expected_image_ids=_ids(),
            media=HealthMediaExpectation(activation=False),
        )
    )
    assert result.ok, result.messages


def test_off_media_sidecar_is_orphan(monkeypatch, tmp_path):
    _patch_health_transport(monkeypatch, _canonical_rows() + _media_rows())
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            expected_image_ids=_ids(),
            media=HealthMediaExpectation(activation=False),
        )
    )
    assert not result.ok
    assert "unknown/orphan services present" in result.messages[0]
    assert "media-executor" in result.messages[0]


def test_on_with_strict_media_probes_pass(monkeypatch, tmp_path):
    _patch_health_transport(monkeypatch, _canonical_rows() + _media_rows())
    monkeypatch.setattr(
        "fetchnow_release.health._assert_media_probes",
        lambda **kwargs: kwargs["messages"].append("OK: media probes"),
    )
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            expected_image_ids=_ids(),
            media=health_media_from_plan(_active_plan()),
        )
    )
    assert result.ok, result.messages
    assert any("media-executor" in m for m in result.messages)


@pytest.mark.parametrize(
    "mutate",
    ["missing", "duplicate", "stopped"],
)
def test_on_missing_duplicate_stopped_sidecar_fails(monkeypatch, tmp_path, mutate):
    rows = _canonical_rows() + _media_rows()
    if mutate == "missing":
        rows = [r for r in rows if r["Service"] != "egress-proxy"]
    elif mutate == "duplicate":
        rows.append(dict(rows[-1], ID="cid-egress-proxy-2"))
    else:
        for row in rows:
            if row["Service"] == "media-executor":
                row["State"] = "exited"
    _patch_health_transport(monkeypatch, rows)
    monkeypatch.setattr(
        "fetchnow_release.health._assert_media_probes",
        lambda **kwargs: None,
    )
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            expected_image_ids=_ids(),
            media=health_media_from_plan(_active_plan()),
        )
    )
    assert not result.ok
    joined = " ".join(result.messages)
    if mutate == "missing":
        assert "missing media service container: egress-proxy" in joined
    elif mutate == "duplicate":
        assert "containers" in joined
    else:
        assert "media-executor state" in joined or "exited" in joined


def test_on_foreign_service_is_orphan(monkeypatch, tmp_path):
    rows = _canonical_rows() + _media_rows()
    rows.append(
        {
            "Service": "evil",
            "State": "running",
            "Health": "",
            "ID": "cid-evil",
            "Image": "x",
        }
    )
    _patch_health_transport(monkeypatch, rows)
    monkeypatch.setattr(
        "fetchnow_release.health._assert_media_probes",
        lambda **kwargs: None,
    )
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            expected_image_ids=_ids(),
            media=health_media_from_plan(_active_plan()),
        )
    )
    assert not result.ok
    assert "unknown/orphan services present: evil" in result.messages[0]


def test_on_wrong_image_and_labels_fail(monkeypatch, tmp_path):
    _patch_health_transport(
        monkeypatch,
        _canonical_rows() + _media_rows(),
        inspect=_inspect_factory(
            media_ids={**_media_ids(), "media-executor": "sha256:" + ("9" * 64)}
        ),
    )
    monkeypatch.setattr(
        "fetchnow_release.health._assert_media_probes",
        lambda **kwargs: None,
    )
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            expected_image_ids=_ids(),
            media=health_media_from_plan(_active_plan()),
        )
    )
    assert not result.ok
    assert "image ID" in result.messages[0]

    _patch_health_transport(
        monkeypatch,
        _canonical_rows() + _media_rows(),
        inspect=_inspect_factory(bad_label=("egress-proxy", "project=other")),
    )
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            expected_image_ids=_ids(),
            media=health_media_from_plan(_active_plan()),
        )
    )
    assert not result.ok
    assert "project label" in result.messages[0]


def test_protocol_and_proxy_failures_fail_health(monkeypatch, tmp_path):
    _patch_health_transport(monkeypatch, _canonical_rows() + _media_rows())

    def probes(**kwargs):
        from fetchnow_release.health import HealthError

        raise HealthError("FAIL: media-executor protocol readiness failed")

    monkeypatch.setattr("fetchnow_release.health._assert_media_probes", probes)
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            expected_image_ids=_ids(),
            media=health_media_from_plan(_active_plan()),
        )
    )
    assert not result.ok
    assert "protocol readiness failed" in result.messages[0]

    def proxy(**kwargs):
        from fetchnow_release.health import HealthError

        raise HealthError("FAIL: proxy deny policy not proven")

    monkeypatch.setattr("fetchnow_release.health._assert_media_probes", proxy)
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            expected_image_ids=_ids(),
            media=health_media_from_plan(_active_plan()),
        )
    )
    assert not result.ok
    assert "proxy deny" in result.messages[0]


def test_health_media_from_plan_off_and_on():
    off = health_media_from_plan(None)
    assert off.activation is False
    on = health_media_from_plan(_active_plan())
    assert on.activation is True
    assert set(on.media_image_ids) == set(MEDIA_SIDECAR_START_ORDER)
    with pytest.raises(Exception, match="denied_host"):
        validate_health_media_expectation(
            HealthMediaExpectation(
                activation=True,
                media_image_ids=_media_ids(),
                denied_host="",
            )
        )


def test_on_to_on_rollback_uses_previous_media_ids():
    from dataclasses import replace

    previous = _active_plan()
    other = dict(_media_ids())
    other["media-executor"] = "sha256:" + ("f" * 64)
    target = replace(previous, media_image_ids=other)
    prev_exp = health_media_from_plan(previous)
    target_exp = health_media_from_plan(target)
    assert prev_exp.media_image_ids != target_exp.media_image_ids
    assert (
        prev_exp.media_image_ids["media-executor"]
        == previous.media_image_ids["media-executor"]
    )


def test_off_rollback_expectation_rejects_sidecars(monkeypatch, tmp_path):
    _patch_health_transport(monkeypatch, _canonical_rows() + _media_rows())
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            expected_image_ids=_ids(),
            media=health_media_from_plan(None),
        )
    )
    assert not result.ok
    assert "orphan" in result.messages[0]


def test_managed_health_loads_media_authority(monkeypatch, tmp_path):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    _patch_health_transport(monkeypatch, _canonical_rows() + _media_rows())
    monkeypatch.setattr(
        "fetchnow_release.health._managed_image_ids",
        lambda *_a, **_k: _ids(),
    )
    monkeypatch.setattr(
        "fetchnow_release.health._managed_media_expectation",
        lambda *_a, **_k: health_media_from_plan(_active_plan()),
    )
    monkeypatch.setattr(
        "fetchnow_release.health._assert_media_probes",
        lambda **kwargs: kwargs["messages"].append("OK: media probes"),
    )
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            deploy_root=deploy,
        )
    )
    assert result.ok, result.messages

    monkeypatch.setattr(
        "fetchnow_release.health._managed_media_expectation",
        lambda *_a, **_k: HealthMediaExpectation(activation=False),
    )
    result = run_health(
        HealthInput(
            project_name=PROJECT,
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=REV,
            repo_root=tmp_path,
            deploy_root=deploy,
        )
    )
    assert not result.ok
    assert "orphan" in result.messages[0]


def test_managed_health_refuses_caller_media_expectation(tmp_path):
    from fetchnow_release.health import HealthError, _managed_media_expectation

    env = tmp_path / ".env"
    env.write_text("X=1\n")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    with pytest.raises(HealthError, match="refuses caller-supplied media"):
        _managed_media_expectation(
            HealthInput(
                project_name=PROJECT,
                env_file=env,
                compose_files=(tmp_path / "c.yaml",),
                expected_revision=REV,
                repo_root=tmp_path,
                deploy_root=deploy,
                media=health_media_from_plan(_active_plan()),
            ),
            REV,
        )


def test_stabilization_does_not_commit_on_media_failure(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    monkeypatch.setattr(
        "fetchnow_release.stabilize.wait_gateway_routing_ready",
        lambda *_a, **_k: SimpleNamespace(ok=True, messages=("OK: routing",)),
    )
    monkeypatch.setattr(
        "fetchnow_release.stabilize.collect_restart_counts",
        lambda **_k: {},
    )
    monkeypatch.setattr(
        "fetchnow_release.stabilize.resolve_public_https_config",
        lambda _inp: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.stabilize.run_health",
        lambda _inp: SimpleNamespace(
            ok=False,
            messages=("FAIL: unknown/orphan services present: media-executor",),
            http=(),
        ),
    )
    with pytest.raises(StabilizeError, match="orphan|media-executor"):
        stabilize_full_health(
            HealthInput(
                project_name=PROJECT,
                env_file=env,
                compose_files=(tmp_path / "c.yaml",),
                expected_revision=REV,
                repo_root=tmp_path,
                expected_image_ids=_ids(),
                media=HealthMediaExpectation(activation=False),
            ),
            policy=SimpleNamespace(
                consecutive_successes=1,
                interval_seconds=0,
                window_seconds=0,
                service_poll_seconds=0,
            ),
            sleeper=lambda _s: None,
            clock=lambda: 0.0,
        )


def test_gateway_injection_still_fail_once():
    import release_media_activation_integration_test as harness
    from fetchnow_release.activate import ActivateError

    calls: list[str] = []

    def real(**_k):
        calls.append("real")

    injected, marker = harness.gateway_fail_once(real)
    with pytest.raises(ActivateError, match="test-only gateway injection"):
        injected()
    injected()
    assert calls == ["real"]
    assert harness.intended_gateway_injection_reached(marker)
