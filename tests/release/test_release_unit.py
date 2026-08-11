"""Unit tests for FetchNow release preflight / health (PRD1C1)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.compose_validate import (  # noqa: E402
    ComposeContractError,
    validate_staging_rendered,
)
from fetchnow_release.env_file import EnvFileError, load_env_file, require_keys  # noqa: E402
from fetchnow_release.health import HealthInput, run_health  # noqa: E402
from fetchnow_release.http_health import HttpHealthError, check_endpoint  # noqa: E402
from fetchnow_release.password import PasswordError, validate_staging_password  # noqa: E402
from fetchnow_release.preflight import PreflightInput, run_preflight  # noqa: E402
from fetchnow_release.redact import redact  # noqa: E402
from fetchnow_release.revision import RevisionError, validate_full_sha  # noqa: E402
from fetchnow_release.roots import RootPathError, validate_operator_root  # noqa: E402
from password_fixture import valid_test_password  # noqa: E402


def _good_cfg(rev: str) -> dict:
    return {
        "name": "fetchnow-staging",
        "services": {
            "api": {"image": f"fetchnow-api:{rev}", "volumes": []},
            "worker": {"image": f"fetchnow-api:{rev}", "command": ["fetchnow-worker"]},
            "web": {"image": f"fetchnow-web:{rev}"},
            "gateway": {
                "image": f"fetchnow-gateway:{rev}",
                "ports": [
                    {
                        "host_ip": "127.0.0.1",
                        "published": "8091",
                        "target": 8080,
                    }
                ],
            },
            "postgres": {"image": "postgres:16.9-alpine"},
        },
        "volumes": {
            "pgdata": {"name": "fetchnow-staging_pgdata"},
            "tmp": {"name": "fetchnow-staging_tmp"},
        },
    }


def test_full_sha_acceptance() -> None:
    sha = "a" * 40
    assert validate_full_sha(sha) == sha


@pytest.mark.parametrize(
    "bad",
    [
        "abc",
        "A" * 40,
        "latest",
        "main",
        "0" * 40,
        "g" * 40,
        "",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ],
)
def test_revision_rejection(bad: str) -> None:
    with pytest.raises(RevisionError):
        validate_full_sha(bad)


def test_password_policy() -> None:
    validate_staging_password(valid_test_password())
    with pytest.raises(PasswordError):
        validate_staging_password("short")
    with pytest.raises(PasswordError):
        validate_staging_password("replace-with-32plus-url-safe-staging-password")
    with pytest.raises(PasswordError):
        validate_staging_password("x" * 32 + "@evil")
    with pytest.raises(PasswordError):
        validate_staging_password("fetchnow")
    with pytest.raises(PasswordError):
        validate_staging_password("x" * 32 + "/path")
    with pytest.raises(PasswordError):
        validate_staging_password("x" * 32 + ":colon")
    with pytest.raises(PasswordError):
        validate_staging_password("x" * 31 + " %")


def test_env_file_mode_and_symlink(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    os.chmod(path, 0o644)
    with pytest.raises(EnvFileError, match="0600"):
        load_env_file(path)
    os.chmod(path, 0o600)
    assert load_env_file(path)["A"] == "1"
    link = tmp_path / "link.env"
    link.symlink_to(path)
    with pytest.raises(EnvFileError, match="symlink"):
        load_env_file(link)
    path.write_text("A=1\nA=2\n")
    os.chmod(path, 0o600)
    with pytest.raises(EnvFileError, match="duplicate"):
        load_env_file(path)


def test_env_missing_keys(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    os.chmod(path, 0o600)
    with pytest.raises(EnvFileError, match="missing"):
        require_keys(load_env_file(path), ("A", "B"))


def test_unsafe_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(RootPathError):
        validate_operator_root(Path("/"), repo_root=repo, label="deploy root")
    with pytest.raises(RootPathError):
        validate_operator_root(repo, repo_root=repo, label="deploy root")
    inside = repo / "nested"
    inside.mkdir()
    with pytest.raises(RootPathError):
        validate_operator_root(inside, repo_root=repo, label="backup root")
    outside = tmp_path / "deploy"
    outside.mkdir()
    assert (
        validate_operator_root(outside, repo_root=repo, label="deploy root")
        == outside.resolve()
    )


def test_staging_render_image_and_ports() -> None:
    rev = "b" * 40
    cfg = _good_cfg(rev)
    validate_staging_rendered(
        cfg, expected_revision=rev, expected_project="fetchnow-staging"
    )
    bad = json.loads(json.dumps(cfg))
    bad["services"]["gateway"]["ports"][0]["host_ip"] = "0.0.0.0"
    with pytest.raises(ComposeContractError, match="loopback"):
        validate_staging_rendered(
            bad, expected_revision=rev, expected_project="fetchnow-staging"
        )
    leaked = json.loads(json.dumps(cfg))
    leaked["services"]["api"]["ports"] = [{"published": "8000", "target": 8000}]
    with pytest.raises(ComposeContractError, match="host ports"):
        validate_staging_rendered(
            leaked, expected_revision=rev, expected_project="fetchnow-staging"
        )
    leaked_pg = json.loads(json.dumps(cfg))
    leaked_pg["services"]["postgres"]["ports"] = [{"published": "5432", "target": 5432}]
    with pytest.raises(ComposeContractError, match="host ports"):
        validate_staging_rendered(
            leaked_pg, expected_revision=rev, expected_project="fetchnow-staging"
        )
    mismatch = json.loads(json.dumps(cfg))
    mismatch["services"]["worker"]["image"] = "fetchnow-api:other"
    with pytest.raises(ComposeContractError):
        validate_staging_rendered(
            mismatch, expected_revision=rev, expected_project="fetchnow-staging"
        )
    tag_mismatch = json.loads(json.dumps(cfg))
    tag_mismatch["services"]["web"]["image"] = "fetchnow-web:local"
    with pytest.raises(ComposeContractError):
        validate_staging_rendered(
            tag_mismatch, expected_revision=rev, expected_project="fetchnow-staging"
        )
    unknown = json.loads(json.dumps(cfg))
    unknown["services"]["sidecar"] = {"image": "busybox"}
    with pytest.raises(ComposeContractError, match="unknown"):
        validate_staging_rendered(
            unknown, expected_revision=rev, expected_project="fetchnow-staging"
        )
    reload = json.loads(json.dumps(cfg))
    reload["services"]["api"]["command"] = ["uvicorn", "--reload", "app"]
    with pytest.raises(ComposeContractError, match="reload"):
        validate_staging_rendered(
            reload, expected_revision=rev, expected_project="fetchnow-staging"
        )


def test_http_rejects_non_loopback() -> None:
    with pytest.raises(HttpHealthError, match="loopback"):
        check_endpoint(
            "https://staging.example", "/api/v1/health/live", deadline_seconds=0.1
        )


def test_http_retry_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_fetch(url: str, *, timeout: float):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 2:
            raise HttpHealthError("HTTP 503")
        return 200, {"status": "ok"}

    monkeypatch.setattr("fetchnow_release.http_health._fetch_json", fake_fetch)
    monkeypatch.setattr("fetchnow_release.http_health.time.sleep", lambda _x: None)
    result = check_endpoint(
        "http://127.0.0.1:8091",
        "/api/v1/health/live",
        deadline_seconds=5,
        retry_delay=0,
    )
    assert result.ok
    assert result.attempts >= 2


def test_http_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fetchnow_release.http_health._fetch_json",
        lambda *a, **k: (_ for _ in ()).throw(HttpHealthError("timeout")),
    )
    monkeypatch.setattr("fetchnow_release.http_health.time.sleep", lambda _x: None)
    times = [0.0, 0.0, 100.0]

    def mono() -> float:
        return times.pop(0) if times else 100.0

    monkeypatch.setattr("fetchnow_release.http_health.time.monotonic", mono)
    result = check_endpoint(
        "http://127.0.0.1:8091",
        "/api/v1/health/ready",
        deadline_seconds=1,
        retry_delay=0,
    )
    assert not result.ok


def test_http_non_200_and_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fetchnow_release.http_health.time.sleep", lambda _x: None)
    monkeypatch.setattr(
        "fetchnow_release.http_health._fetch_json",
        lambda *a, **k: (500, {"status": "ok"}),
    )
    times = [0.0, 0.0, 100.0]
    monkeypatch.setattr(
        "fetchnow_release.http_health.time.monotonic",
        lambda: times.pop(0) if times else 100.0,
    )
    result = check_endpoint(
        "http://127.0.0.1:8091",
        "/api/v1/health/live",
        deadline_seconds=1,
        retry_delay=0,
    )
    assert not result.ok

    times2 = [0.0, 0.0, 100.0]
    monkeypatch.setattr(
        "fetchnow_release.http_health.time.monotonic",
        lambda: times2.pop(0) if times2 else 100.0,
    )
    monkeypatch.setattr(
        "fetchnow_release.http_health._fetch_json",
        lambda *a, **k: (200, {"status": "degraded"}),
    )
    result2 = check_endpoint(
        "http://127.0.0.1:8091",
        "/api/v1/health/live",
        deadline_seconds=1,
        retry_delay=0,
    )
    assert not result2.ok


def test_http_redirect_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fetchnow_release.http_health._fetch_json",
        lambda *a, **k: (_ for _ in ()).throw(
            HttpHealthError("unexpected redirect HTTP 302 -> http://evil")
        ),
    )
    monkeypatch.setattr("fetchnow_release.http_health.time.sleep", lambda _x: None)
    times = [0.0, 0.0, 100.0]  # deadline base, loop check, post-attempt check
    monkeypatch.setattr(
        "fetchnow_release.http_health.time.monotonic",
        lambda: times.pop(0) if times else 100.0,
    )
    result = check_endpoint(
        "http://127.0.0.1:8091",
        "/api/v1/health/live",
        deadline_seconds=1,
        retry_delay=0,
    )
    assert not result.ok
    assert "redirect" in result.detail


def test_local_revision_allowed_only_when_flagged() -> None:
    assert validate_full_sha("local", allow_local=True) == "local"
    with pytest.raises(RevisionError):
        validate_full_sha("local", allow_local=False)


def test_redaction() -> None:
    marker = "Z" * 24
    text = (
        f"POSTGRES_PASSWORD={marker} "
        "DATABASE_URL=postgresql+asyncpg://u:p@h/db"
    )
    out = redact(text)
    assert marker not in out
    assert "postgresql+asyncpg://u:p@h/db" not in out
    assert "<redacted>" in out


def test_preflight_wrong_project(tmp_path: Path) -> None:
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow",
            env_file=tmp_path / "missing.env",
            compose_files=(tmp_path / "compose.yaml",),
            expected_revision="a" * 40,
            repo_root=tmp_path,
        )
    )
    assert not result.ok
    assert "fetchnow-staging" in result.messages[0]


def test_health_missing_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rev = "c" * 40
    monkeypatch.setattr("fetchnow_release.health.require_compose_v2", lambda: None)
    monkeypatch.setattr("fetchnow_release.health.require_docker_daemon", lambda: None)
    monkeypatch.setattr(
        "fetchnow_release.health._ps_services",
        lambda _inp: [
            {"Service": "gateway", "State": "running", "Health": "healthy", "ID": "1"}
        ],
    )
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name="fetchnow-health-test-aaaaaaaa",
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=rev,
            repo_root=tmp_path,
        )
    )
    assert not result.ok
    assert "missing service" in result.messages[0]


def test_health_unhealthy_and_duplicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rev = "d" * 40
    monkeypatch.setattr("fetchnow_release.health.require_compose_v2", lambda: None)
    monkeypatch.setattr("fetchnow_release.health.require_docker_daemon", lambda: None)

    def all_services_dup(_inp):  # noqa: ANN001
        rows = []
        for svc in ("gateway", "api", "worker", "postgres", "web"):
            rows.append(
                {
                    "Service": svc,
                    "State": "running",
                    "Health": "healthy",
                    "ID": f"id-{svc}",
                    "Image": f"fetchnow-api:{rev}"
                    if svc in {"api", "worker"}
                    else f"fetchnow-{svc}:{rev}".replace(
                        "fetchnow-postgres", "postgres:16.9-alpine"
                    ),
                }
            )
        rows.append(
            {
                "Service": "api",
                "State": "running",
                "Health": "healthy",
                "ID": "id-api-2",
            }
        )
        return rows

    monkeypatch.setattr("fetchnow_release.health._ps_services", all_services_dup)
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name="fetchnow-health-test-bbbbbbbb",
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=rev,
            repo_root=tmp_path,
        )
    )
    assert not result.ok
    assert "containers" in result.messages[0]


def test_health_wrong_project_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rev = "e" * 40
    monkeypatch.setattr("fetchnow_release.health.require_compose_v2", lambda: None)
    monkeypatch.setattr("fetchnow_release.health.require_docker_daemon", lambda: None)

    def rows(_inp):  # noqa: ANN001
        out = []
        for svc in ("gateway", "api", "worker", "postgres", "web"):
            image = {
                "api": f"fetchnow-api:{rev}",
                "worker": f"fetchnow-api:{rev}",
                "web": f"fetchnow-web:{rev}",
                "gateway": f"fetchnow-gateway:{rev}",
                "postgres": "postgres:16.9-alpine",
            }[svc]
            out.append(
                {
                    "Service": svc,
                    "State": "running",
                    "Health": "healthy",
                    "ID": f"cid-{svc}",
                    "Image": image,
                }
            )
        return out

    def inspect(cid: str) -> dict:
        svc = cid.replace("cid-", "")
        return {
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "wrong-project",
                    "org.opencontainers.image.revision": rev,
                },
                "Image": f"img-{svc}",
            },
            "Image": "sha256:shared" if svc in {"api", "worker"} else f"sha256:{svc}",
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
            "RestartCount": 0,
        }

    monkeypatch.setattr("fetchnow_release.health._ps_services", rows)
    monkeypatch.setattr("fetchnow_release.health._inspect", inspect)
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name="fetchnow-health-test-cccccccc",
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=rev,
            repo_root=tmp_path,
            gateway_base_url="http://127.0.0.1:8091",
        )
    )
    assert not result.ok
    assert "project label" in result.messages[0]


def test_health_wrong_oci_and_exited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rev = "f" * 40
    monkeypatch.setattr("fetchnow_release.health.require_compose_v2", lambda: None)
    monkeypatch.setattr("fetchnow_release.health.require_docker_daemon", lambda: None)

    def rows(_inp):  # noqa: ANN001
        return [
            {
                "Service": svc,
                "State": "running" if svc != "api" else "exited",
                "Health": "healthy",
                "ID": f"cid-{svc}",
                "Image": {
                    "api": f"fetchnow-api:{rev}",
                    "worker": f"fetchnow-api:{rev}",
                    "web": f"fetchnow-web:{rev}",
                    "gateway": f"fetchnow-gateway:{rev}",
                    "postgres": "postgres:16.9-alpine",
                }[svc],
            }
            for svc in ("gateway", "api", "worker", "postgres", "web")
        ]

    def inspect(cid: str) -> dict:
        svc = cid.removeprefix("cid-")
        return {
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "fetchnow-health-test-dddddddd",
                    "com.docker.compose.service": svc,
                    "org.opencontainers.image.revision": rev,
                },
                "Image": "img",
            },
            "Image": "sha256:x",
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
            "RestartCount": 0,
        }

    monkeypatch.setattr("fetchnow_release.health._ps_services", rows)
    monkeypatch.setattr("fetchnow_release.health._inspect", inspect)
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name="fetchnow-health-test-dddddddd",
            env_file=env,
            compose_files=(tmp_path / "c.yaml",),
            expected_revision=rev,
            repo_root=tmp_path,
        )
    )
    assert not result.ok
    assert "exited" in result.messages[0]


def test_preflight_tag_revision_mismatch_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rev = "a" * 40
    other = "b" * 40
    env = tmp_path / ".env.staging"
    env.write_text(
        "\n".join(
            [
                "COMPOSE_PROJECT_NAME=fetchnow-staging",
                f"FETCHNOW_RELEASE_REVISION={other}",
                "GATEWAY_PORT=127.0.0.1:8091",
                "PUBLIC_SITE_URL=https://example.test",
                "POSTGRES_DB=fetchnow",
                "POSTGRES_USER=fetchnow",
                f"POSTGRES_PASSWORD={valid_test_password()}",
                f"FETCHNOW_DEPLOY_ROOT={tmp_path / 'deploy'}",
                f"FETCHNOW_BACKUP_ROOT={tmp_path / 'backup'}",
            ]
        )
        + "\n"
    )
    os.chmod(env, 0o600)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "backup").mkdir()
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow-staging",
            env_file=env,
            compose_files=(compose,),
            expected_revision=rev,
            repo_root=tmp_path,
        )
    )
    assert not result.ok
    assert "does not match" in result.messages[0]
