"""PRD1E-B2.1 config-only rollout contracts and transaction tests."""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.config_contract import (  # noqa: E402
    BUILD_TIME_CONFIG,
    RUNTIME_CONFIG_ALLOWLIST,
    RUNTIME_WIRING,
    ConfigContractError,
    affected_services,
    build_config_fingerprint,
    changed_runtime_keys,
    legacy_runtime_config_fingerprint_schema1,
    runtime_config_fingerprint,
    runtime_values_from_compose,
)
from fetchnow_release.config_journal import (  # noqa: E402
    STATUS_COMMITTED,
    STATUS_ROLLED_BACK,
    STATUS_ROLLBACK_FAILED,
    find_unresolved_config_rollouts,
)
from fetchnow_release.config_rollout import (  # noqa: E402
    ConfigRolloutInput,
    run_config_rollout,
)
from fetchnow_release.config_state import (  # noqa: E402
    RUNTIME_CONFIG_STATE_SCHEMA,
    ConfigStateError,
    RuntimeConfigState,
    build_runtime_config_state,
    load_runtime_config_state,
    runtime_config_state_path,
    write_runtime_config_state,
)
from fetchnow_release.cli import build_parser  # noqa: E402

REVISION = "a" * 40
DEPLOYMENT_ID = "11111111-1111-4111-8111-111111111111"


def _image_ids() -> dict[str, str]:
    api = "sha256:" + "a" * 64
    return {
        "api": api,
        "worker": api,
        "web": "sha256:" + "b" * 64,
        "gateway": "sha256:" + "c" * 64,
    }


def _build_values(*, indexing: str = "false") -> dict[str, str]:
    return {
        "PUBLIC_MEDIA_FLOW_ENABLED": "true",
        "PUBLIC_SEARCH_INDEXING_ENABLED": indexing,
        "PUBLIC_SITE_URL": "https://fetchnow.online",
    }


def _runtime(
    quota: str,
    *,
    limiter: str = "false",
    rate: str = "524288",
) -> dict[str, str]:
    return {
        "FREE_DOWNLOAD_QUOTA_ENABLED": quota,
        "FREE_DELIVERY_RATE_LIMIT_ENABLED": limiter,
        "FREE_DELIVERY_RATE_BYTES_PER_SECOND": rate,
    }


def _rendered(
    quota: str,
    *,
    limiter: str = "false",
    rate: str = "524288",
    extra_api: dict[str, str] | None = None,
) -> dict:
    api = {"FREE_DOWNLOAD_QUOTA_ENABLED": quota, **(extra_api or {})}
    return {
        "services": {
            "api": {"environment": api},
            "worker": {"environment": {}},
            "delivery": {
                "environment": {
                    "FREE_DELIVERY_RATE_LIMIT_ENABLED": limiter,
                    "FREE_DELIVERY_RATE_BYTES_PER_SECOND": rate,
                }
            },
            "web": {"environment": {}},
            "gateway": {"environment": {}},
        }
    }


def _live(
    quota: str,
    *,
    limiter: str = "false",
    rate: str = "524288",
    extra_api: dict[str, str] | None = None,
):
    ids = {
        "api": "api-old",
        "worker": "worker-old",
        "delivery": "delivery-old",
        "web": "web-old",
        "gateway": "gateway-old",
    }
    env = {service: {} for service in ids}
    env["api"] = {
        "FREE_DOWNLOAD_QUOTA_ENABLED": quota,
        **(extra_api or {}),
    }
    env["delivery"] = {
        "FREE_DELIVERY_RATE_LIMIT_ENABLED": limiter,
        "FREE_DELIVERY_RATE_BYTES_PER_SECOND": rate,
    }
    return ids, env


def _after(
    quota: str,
    *,
    limiter: str = "false",
    rate: str = "524288",
    changed_service: str = "api",
):
    ids, env = _live(quota, limiter=limiter, rate=rate)
    ids[changed_service] = f"{changed_service}-new"
    return ids, env


def _env_file(
    tmp_path: Path,
    *,
    quota: str,
    limiter: str = "false",
    rate: str = "524288",
    indexing: str = "false",
) -> Path:
    path = tmp_path / ".env.production"
    values = {
        "FREE_DOWNLOAD_QUOTA_ENABLED": quota,
        "FREE_DELIVERY_RATE_LIMIT_ENABLED": limiter,
        "FREE_DELIVERY_RATE_BYTES_PER_SECOND": rate,
        **_build_values(indexing=indexing),
    }
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    path.chmod(0o600)
    return path


def _persist_state(
    deploy: Path,
    *,
    quota: str = "false",
    limiter: str = "false",
    rate: str = "524288",
) -> None:
    write_runtime_config_state(
        deploy,
        build_runtime_config_state(
            revision=REVISION,
            deployment_id=DEPLOYMENT_ID,
            latest_config_rollout_id=None,
            runtime_values=_runtime(quota, limiter=limiter, rate=rate),
            build_values=_build_values(),
            updated_at_utc="2026-08-30T00:00:00Z",
        ),
    )


def _persist_legacy_schema1_state(deploy: Path) -> None:
    values = {"FREE_DOWNLOAD_QUOTA_ENABLED": "false"}
    write_runtime_config_state(
        deploy,
        RuntimeConfigState(
            schema_version=1,
            revision=REVISION,
            deployment_id=DEPLOYMENT_ID,
            latest_config_rollout_id=None,
            runtime_config_fingerprint=legacy_runtime_config_fingerprint_schema1(
                values
            ),
            runtime_values=values,
            build_config_fingerprint=build_config_fingerprint(_build_values()),
            build_values=_build_values(),
            updated_at_utc="2026-08-30T00:00:00Z",
        ),
    )


def _patch_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    target_quota: str,
    target_limiter: str = "false",
    target_rate: str = "524288",
    inspections: list[tuple[dict[str, str], dict[str, dict[str, str]]]],
    extra_target_api: dict[str, str] | None = None,
    indexing: str = "false",
) -> tuple[ConfigRolloutInput, dict[str, list]]:
    import fetchnow_release.config_rollout as module

    deploy = tmp_path / "deploy"
    deploy.mkdir(exist_ok=True)
    release = tmp_path / "release"
    (release / "source").mkdir(parents=True, exist_ok=True)
    (release / "release.json").write_text("{}", encoding="utf-8")
    base = release / "source" / "compose.yaml"
    overlay = release / "source" / "compose.production.yaml"
    base.write_text("services: {}\n", encoding="utf-8")
    overlay.write_text("services: {}\n", encoding="utf-8")
    env_file = _env_file(
        tmp_path,
        quota=target_quota,
        limiter=target_limiter,
        rate=target_rate,
        indexing=indexing,
    )
    ids = _image_ids()
    current = SimpleNamespace(
        revision=REVISION,
        release_manifest_sha256="d" * 64,
        image_ids=ids,
        deployment_id=DEPLOYMENT_ID,
        database=SimpleNamespace(heads=("0007_free_download_quota",)),
    )
    calls: dict[str, list] = {"activate": [], "wait": [], "health": []}

    monkeypatch.setattr(module, "validate_deploy_root", lambda *_a, **_k: deploy)
    monkeypatch.setattr(module, "real_identity", lambda _p: None)
    monkeypatch.setattr(
        module, "load_and_resolve_current_state", lambda *_a, **_k: current
    )
    monkeypatch.setattr(module, "release_dir", lambda *_a, **_k: release)
    monkeypatch.setattr(
        module,
        "verify_prepared_release",
        lambda *_a, **_k: SimpleNamespace(ok=True, messages=("OK",)),
    )
    monkeypatch.setattr(module, "load_manifest", lambda _p: object())
    monkeypatch.setattr(module, "sha256_file", lambda _p: "d" * 64)
    monkeypatch.setattr(module, "assert_release_images_present", lambda _m: ids)
    monkeypatch.setattr(
        module, "snapshot_compose_files_for_release", lambda *_a, **_k: (base, overlay)
    )
    monkeypatch.setattr(
        module,
        "database_heads_via_postgres",
        lambda **_k: frozenset({"0007_free_download_quota"}),
    )
    monkeypatch.setattr(
        module, "assert_live_matches_saved_database_heads", lambda **_k: None
    )
    monkeypatch.setattr(
        module, "assert_resolved_application_compatible", lambda **_k: None
    )
    monkeypatch.setattr(
        module,
        "target_heads_from_release_source",
        lambda _r: frozenset({"0007_free_download_quota"}),
    )
    monkeypatch.setattr(
        module,
        "compose_config_json",
        lambda **_k: _rendered(
            target_quota,
            limiter=target_limiter,
            rate=target_rate,
            extra_api=extra_target_api,
        ),
    )
    monkeypatch.setattr(module, "RolloutLock", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(module, "find_unresolved_deployments", lambda _d: [])
    monkeypatch.setattr(module, "find_unresolved_migrations", lambda _d: [])
    monkeypatch.setattr(module, "find_unresolved_bootstraps", lambda _d: [])
    monkeypatch.setattr(module, "find_unresolved_config_rollouts", lambda _d: ())
    queue = list(inspections)
    monkeypatch.setattr(module, "inspect_live_service_state", lambda **_k: queue.pop(0))
    monkeypatch.setattr(module, "compose_files_include_delivery", lambda _f: False)
    monkeypatch.setattr(
        module,
        "write_images_override",
        lambda directory, *_a, **_k: directory / "images.yaml",
    )

    def activate(**kwargs):
        calls["activate"].append(kwargs["services"])

    monkeypatch.setattr(module, "activate_services", activate)
    monkeypatch.setattr(
        module,
        "wait_services_healthy",
        lambda **kwargs: calls["wait"].append(kwargs["services"]),
    )
    monkeypatch.setattr(
        module,
        "stabilize_full_health",
        lambda *_a, **_k: calls["health"].append("pass"),
    )
    return (
        ConfigRolloutInput(
            project_name="fetchnow-config-test",
            env_file=env_file,
            expected_revision=REVISION,
            repo_root=tmp_path,
            deploy_root=deploy,
            policy=SimpleNamespace(),  # type: ignore[arg-type]
        ),
        calls,
    )


def test_fingerprint_is_stable_and_domain_separated() -> None:
    runtime_a = runtime_config_fingerprint(_runtime("false"))
    runtime_b = runtime_config_fingerprint(dict(reversed(list(_runtime("false").items()))))
    assert runtime_a == runtime_b
    assert runtime_a != build_config_fingerprint(_build_values())


@pytest.mark.parametrize("rate", ["0", "-1", "262143", "67108865", "0524288"])
def test_runtime_rate_normalization_rejects_invalid_values(rate: str) -> None:
    with pytest.raises(ConfigContractError):
        runtime_config_fingerprint(_runtime("false", rate=rate))


@pytest.mark.parametrize("rate", ["262144", "524288", "67108864"])
def test_runtime_rate_normalization_accepts_reviewed_bounds(rate: str) -> None:
    assert len(runtime_config_fingerprint(_runtime("false", rate=rate))) == 64


def test_allowlist_and_wiring_are_narrow() -> None:
    assert set(RUNTIME_CONFIG_ALLOWLIST) == {
        "FREE_DOWNLOAD_QUOTA_ENABLED",
        "FREE_DELIVERY_RATE_LIMIT_ENABLED",
        "FREE_DELIVERY_RATE_BYTES_PER_SECOND",
    }
    assert affected_services(("FREE_DOWNLOAD_QUOTA_ENABLED",)) == ("api",)
    assert affected_services(
        (
            "FREE_DELIVERY_RATE_BYTES_PER_SECOND",
            "FREE_DELIVERY_RATE_LIMIT_ENABLED",
        )
    ) == ("delivery",)
    assert set(BUILD_TIME_CONFIG) >= {
        "PUBLIC_MEDIA_FLOW_ENABLED",
        "PUBLIC_SEARCH_INDEXING_ENABLED",
        "PUBLIC_SITE_URL",
    }
    classified_not_allowlisted = set(RUNTIME_WIRING) - set(RUNTIME_CONFIG_ALLOWLIST)
    assert classified_not_allowlisted == {
        "FREE_DOWNLOAD_LIMIT",
        "FREE_DOWNLOAD_WINDOW_SECONDS",
        "FREE_DOWNLOAD_QUOTA_RETENTION_SECONDS",
        "MEDIA_INSPECTION_ENABLED",
        "MEDIA_JOBS_ENABLED",
        "MEDIA_DOWNLOADS_ENABLED",
        "MEDIA_DELIVERY_ENABLED",
        "MEDIA_BROWSER_DELIVERY_ENABLED",
        "MEDIA_MUXING_ENABLED",
    }
    payment_keys = {
        "ROBOKASSA_MODE",
        "ROBOKASSA_MERCHANT_LOGIN",
        "ROBOKASSA_SIGNATURE_ALGORITHM",
        "ROBOKASSA_TEST_PASSWORD1",
        "ROBOKASSA_TEST_PASSWORD2",
        "ROBOKASSA_TEST_AMOUNT_MINOR",
        "ROBOKASSA_RECEIPT_TAX",
        "ROBOKASSA_RECEIPT_PAYMENT_METHOD",
        "ROBOKASSA_ORDER_TTL_SECONDS",
    }
    assert payment_keys.isdisjoint(RUNTIME_CONFIG_ALLOWLIST)
    assert payment_keys.isdisjoint(RUNTIME_WIRING)
    assert payment_keys.isdisjoint(BUILD_TIME_CONFIG)


def test_rendered_compose_receiver_drift_fails_closed() -> None:
    rendered = _rendered("true")
    rendered["services"]["worker"]["environment"]["FREE_DOWNLOAD_QUOTA_ENABLED"] = (
        "true"
    )
    with pytest.raises(ConfigContractError, match="receiver drift"):
        runtime_values_from_compose(rendered)


def test_service_without_environment_is_an_empty_runtime_map() -> None:
    rendered = _rendered("false")
    del rendered["services"]["gateway"]["environment"]
    assert runtime_values_from_compose(rendered) == {
        **_runtime("false")
    }


def test_state_round_trip_and_tamper_rejection(tmp_path: Path) -> None:
    _persist_state(tmp_path)
    state = load_runtime_config_state(tmp_path)
    assert state is not None
    assert state.runtime_values["FREE_DOWNLOAD_QUOTA_ENABLED"] == "false"
    path = runtime_config_state_path(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["runtime_values"]["FREE_DOWNLOAD_QUOTA_ENABLED"] = "true"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigStateError, match="fingerprint mismatch"):
        load_runtime_config_state(tmp_path)


def test_existing_schema1_runtime_state_remains_readable(tmp_path: Path) -> None:
    _persist_legacy_schema1_state(tmp_path)
    state = load_runtime_config_state(tmp_path)
    assert state is not None
    assert state.schema_version == 1
    assert state.runtime_values == {"FREE_DOWNLOAD_QUOTA_ENABLED": "false"}


def test_schema1_state_requires_explicit_no_delta_reinitialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="false",
        inspections=[_live("false"), _live("false")],
    )
    _persist_legacy_schema1_state(inp.deploy_root)
    result = run_config_rollout(inp)
    assert not result.ok
    assert "not initialized" in result.messages[0]
    assert calls["activate"] == []

    inp = ConfigRolloutInput(**{**inp.__dict__, "initialize_active_config": True})
    result = run_config_rollout(inp)
    assert result.ok and result.already_active_config
    state = load_runtime_config_state(inp.deploy_root)
    assert state is not None
    assert state.schema_version == RUNTIME_CONFIG_STATE_SCHEMA
    assert state.runtime_values == _runtime("false")


def test_no_delta_initializes_state_without_recreation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="false",
        inspections=[_live("false")],
    )
    inp = ConfigRolloutInput(**{**inp.__dict__, "initialize_active_config": True})
    result = run_config_rollout(inp)
    assert result.ok and result.already_active_config
    assert calls["activate"] == []
    assert load_runtime_config_state(inp.deploy_root) is not None


def test_missing_state_requires_explicit_initialization_even_without_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="false",
        inspections=[_live("false")],
    )
    result = run_config_rollout(inp)
    assert not result.ok
    assert "--initialize-active-config" in result.messages[0]
    assert calls["activate"] == []


def test_explicit_initialization_after_env_edit_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="true",
        inspections=[_live("false")],
    )
    inp = ConfigRolloutInput(**{**inp.__dict__, "initialize_active_config": True})
    result = run_config_rollout(inp)
    assert not result.ok
    assert "after runtime env edit" in result.messages[0]
    assert calls["activate"] == []


def test_repeated_same_rollout_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="false",
        inspections=[_live("false")],
    )
    _persist_state(inp.deploy_root)
    result = run_config_rollout(inp)
    assert result.ok and result.already_active_config
    assert calls["activate"] == []


def test_quota_false_to_true_recreates_only_api_and_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="true",
        inspections=[_live("false"), _after("true")],
    )
    _persist_state(inp.deploy_root)
    result = run_config_rollout(inp)
    assert result.ok and result.status == STATUS_COMMITTED
    assert calls["activate"] == [("api",)]
    assert calls["wait"] == [("api",)]
    state = load_runtime_config_state(inp.deploy_root)
    assert state is not None
    assert state.runtime_values == _runtime("true")
    assert state.revision == REVISION
    assert state.deployment_id == DEPLOYMENT_ID
    assert result.config_rollout_id == state.latest_config_rollout_id
    assert not find_unresolved_config_rollouts(inp.deploy_root)
    journal_root = inp.deploy_root / "config-rollouts" / str(result.config_rollout_id)
    for path in journal_root.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "DATABASE_URL" not in text
            assert "POSTGRES_PASSWORD" not in text
            assert "do-not-print" not in text


@pytest.mark.parametrize(
    ("target_limiter", "target_rate"),
    [("true", "524288"), ("false", "1048576"), ("true", "1048576")],
)
def test_delivery_runtime_changes_recreate_only_delivery_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_limiter: str,
    target_rate: str,
) -> None:
    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="false",
        target_limiter=target_limiter,
        target_rate=target_rate,
        inspections=[
            _live("false"),
            _after(
                "false",
                limiter=target_limiter,
                rate=target_rate,
                changed_service="delivery",
            ),
        ],
    )
    _persist_state(inp.deploy_root)
    result = run_config_rollout(inp)
    assert result.ok and result.status == STATUS_COMMITTED
    assert calls["activate"] == [("delivery",)]
    assert calls["wait"] == [("delivery",)]
    state = load_runtime_config_state(inp.deploy_root)
    assert state is not None
    assert state.runtime_values == _runtime(
        "false", limiter=target_limiter, rate=target_rate
    )


def test_delivery_runtime_health_failure_rolls_back_delivery_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fetchnow_release.config_rollout as module

    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="false",
        target_limiter="true",
        inspections=[
            _live("false"),
            _after("false", limiter="true", changed_service="delivery"),
            _after("false", changed_service="delivery"),
        ],
    )
    _persist_state(inp.deploy_root)
    health_calls = 0

    def health(*_a, **_k):
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            raise RuntimeError("simulated target health failure")

    monkeypatch.setattr(module, "stabilize_full_health", health)
    result = run_config_rollout(inp)
    assert not result.ok and result.status == STATUS_ROLLED_BACK
    assert calls["activate"] == [("delivery",), ("delivery",)]
    state = load_runtime_config_state(inp.deploy_root)
    assert state is not None
    assert state.runtime_values == _runtime("false")


def test_live_runtime_drift_from_persisted_state_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="true",
        inspections=[_live("true")],
    )
    _persist_state(inp.deploy_root, quota="false")
    result = run_config_rollout(inp)
    assert not result.ok
    assert "drift" in result.messages[0]
    assert calls["activate"] == []


def test_missing_state_with_delta_fails_before_recreation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="true",
        inspections=[_live("false")],
    )
    result = run_config_rollout(inp)
    assert not result.ok
    assert "not initialized" in result.messages[0]
    assert calls["activate"] == []


def test_build_time_delta_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="false",
        indexing="true",
        inspections=[_live("false")],
    )
    _persist_state(inp.deploy_root)
    result = run_config_rollout(inp)
    assert not result.ok
    assert "PUBLIC_SEARCH_INDEXING_ENABLED" in result.messages[0]
    assert calls["activate"] == []


def test_unknown_or_secret_runtime_delta_is_rejected_without_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "do-not-print-this-secret"
    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="true",
        extra_target_api={"DATABASE_URL": secret},
        inspections=[_live("false", extra_api={"DATABASE_URL": "old-secret"})],
    )
    _persist_state(inp.deploy_root)
    result = run_config_rollout(inp)
    assert not result.ok
    assert "api:DATABASE_URL" in result.messages[0]
    assert secret not in "\n".join(result.messages)
    assert calls["activate"] == []


def test_health_failure_restores_previous_config_and_marks_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fetchnow_release.config_rollout as module

    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="true",
        inspections=[_live("false"), _after("true"), _after("false")],
    )
    _persist_state(inp.deploy_root)
    health_calls = 0

    def health(*_a, **_k):
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            raise RuntimeError("simulated target health failure")

    monkeypatch.setattr(module, "stabilize_full_health", health)
    result = run_config_rollout(inp)
    assert not result.ok and result.status == STATUS_ROLLED_BACK
    assert calls["activate"] == [("api",), ("api",)]
    state = load_runtime_config_state(inp.deploy_root)
    assert state is not None
    assert state.runtime_values == _runtime("false")
    result_path = (
        inp.deploy_root
        / "config-rollouts"
        / str(result.config_rollout_id)
        / "result.json"
    )
    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    assert persisted["status"] == STATUS_ROLLED_BACK
    assert persisted["rollback_status"] == "passed"


def test_rollback_failure_is_fail_closed_with_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fetchnow_release.config_rollout as module

    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="true",
        inspections=[_live("false"), _after("true")],
    )
    _persist_state(inp.deploy_root)
    activations = 0

    def activate(**kwargs):
        nonlocal activations
        activations += 1
        calls["activate"].append(kwargs["services"])
        if activations == 2:
            raise RuntimeError("simulated rollback failure")

    monkeypatch.setattr(module, "activate_services", activate)
    monkeypatch.setattr(
        module,
        "stabilize_full_health",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("target unhealthy")),
    )
    result = run_config_rollout(inp)
    assert not result.ok and result.status == STATUS_ROLLBACK_FAILED
    assert result.config_rollout_id is not None
    result_path = (
        inp.deploy_root / "config-rollouts" / result.config_rollout_id / "result.json"
    )
    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    assert persisted["status"] == STATUS_ROLLBACK_FAILED
    assert persisted["rollback_status"] == "failed"


def test_active_revision_mismatch_fails_before_container_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fetchnow_release.config_rollout as module

    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="false",
        inspections=[],
    )
    mismatch = SimpleNamespace(revision="b" * 40)
    monkeypatch.setattr(
        module, "load_and_resolve_current_state", lambda *_a, **_k: mismatch
    )
    result = run_config_rollout(inp)
    assert not result.ok
    assert "active revision" in result.messages[0]
    assert calls["activate"] == []


@pytest.mark.parametrize(
    ("patched_name", "message"),
    [
        ("verify_prepared_release", "release metadata invalid"),
        ("assert_release_images_present", "image identity mismatch"),
        ("database_heads_via_postgres", "database head mismatch"),
    ],
)
def test_pre_mutation_identity_gates_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_name: str,
    message: str,
) -> None:
    import fetchnow_release.config_rollout as module

    inp, calls = _patch_transaction(
        monkeypatch,
        tmp_path,
        target_quota="false",
        inspections=[],
    )
    if patched_name == "verify_prepared_release":
        monkeypatch.setattr(
            module,
            patched_name,
            lambda *_a, **_k: SimpleNamespace(ok=False, messages=(message,)),
        )
    else:
        monkeypatch.setattr(
            module,
            patched_name,
            lambda *_a, **_k: (_ for _ in ()).throw(ValueError(message)),
        )
    result = run_config_rollout(inp)
    assert not result.ok
    assert message in result.messages[0]
    assert calls["activate"] == []


def test_changed_keys_exact() -> None:
    assert changed_runtime_keys(
        _runtime("false"),
        _runtime("true"),
    ) == ("FREE_DOWNLOAD_QUOTA_ENABLED",)


def test_cli_exposes_config_rollout_without_bypass_flags() -> None:
    help_text = build_parser().format_help()
    assert "config-rollout" in help_text
    assert "skip-health" not in help_text
    parsed = build_parser().parse_args(
        [
            "config-rollout",
            "--env-file",
            "/tmp/env",
            "--expected-revision",
            REVISION,
            "--deploy-root",
            "/tmp/deploy",
            "--initialize-active-config",
        ]
    )
    assert parsed.initialize_active_config is True
