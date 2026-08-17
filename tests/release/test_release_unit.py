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
            "delivery": {
                "image": f"fetchnow-api:{rev}",
                "command": ["fetchnow-delivery"],
            },
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


def _storage_init_svc(rev: str) -> dict:
    return {
        "image": f"fetchnow-api:{rev}",
        "restart": "no",
        "command": ["fetchnow-storage-init"],
        "user": "10001:10001",
        "network_mode": "none",
        "environment": {
            "LOG_LEVEL": "INFO",
            "TEMP_STORAGE_PATH": "/var/lib/fetchnow/tmp",
            "MEDIA_DOWNLOAD_TEMP_ROOT": "/var/lib/fetchnow/tmp/downloads",
        },
        "volumes": ["tmp:/var/lib/fetchnow/tmp"],
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
        validate_staging_password("replace-with-32plus-url-safe-production-password")
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


def test_pr9_render_accepts_storage_init_and_rejects_unknown() -> None:
    rev = "b" * 40
    cfg = _good_cfg(rev)
    cfg["services"]["storage-init"] = _storage_init_svc(rev)
    validate_staging_rendered(
        cfg, expected_revision=rev, expected_project="fetchnow-staging"
    )
    leaked = json.loads(json.dumps(cfg))
    leaked["services"]["storage-init"]["environment"]["DATABASE_URL"] = (
        "postgresql+asyncpg://fetchnow:secret@postgres:5432/fetchnow"
    )
    with pytest.raises(ComposeContractError, match="DATABASE_URL"):
        validate_staging_rendered(
            leaked, expected_revision=rev, expected_project="fetchnow-staging"
        )
    net = json.loads(json.dumps(cfg))
    net["services"]["storage-init"]["network_mode"] = "bridge"
    with pytest.raises(ComposeContractError, match="network_mode"):
        validate_staging_rendered(
            net, expected_revision=rev, expected_project="fetchnow-staging"
        )
    sidecar = json.loads(json.dumps(cfg))
    sidecar["services"]["sidecar"] = {"image": "busybox"}
    with pytest.raises(ComposeContractError, match="unknown"):
        validate_staging_rendered(
            sidecar, expected_revision=rev, expected_project="fetchnow-staging"
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
    url = redact("https://example.test/path?token=abc#frag")
    assert "token=abc" not in url
    assert "#frag" not in url
    assert url == "https://example.test/path"


@pytest.mark.parametrize(
    ("raw", "forbidden", "must_contain"),
    [
        (
            "https://user:password@example.test/path",
            ("user:password", "password@", ":password"),
            ("https://example.test/path",),
        ),
        (
            "https://alice%40corp:p%40ssword@example.test/api",
            ("alice%40corp", "p%40ssword", "%40ssword", "alice@corp"),
            ("https://example.test/api",),
        ),
        (
            "https://deploy:s3cret@example.test:8443/v1?token=abc#frag",
            ("s3cret", "token=abc", "#frag", "deploy:", "?token", "<redacted>"),
            ("https://example.test:8443/v1",),
        ),
        (
            "https://u:p@[2001:db8::1]:443/x?q=1#f",
            ("u:p@", "q=1", "#f"),
            ("https://[2001:db8::1]:443/x",),
        ),
        (
            "https://user:pass@[::1]/health",
            ("user:pass", "pass@"),
            ("https://[::1]/health",),
        ),
        (
            "see https://user:pass@example.test/a).",
            ("user:pass", "pass@"),
            ("https://example.test/a",),
        ),
        (
            "a=https://u:p@one.test/x b=https://v:q@two.test/y?z=1",
            ("u:p@", "v:q@", "z=1"),
            ("https://one.test/x", "https://two.test/y"),
        ),
    ],
)
def test_http_url_credentials_and_query_redaction(
    raw: str, forbidden: tuple[str, ...], must_contain: tuple[str, ...]
) -> None:
    out = redact(raw)
    for needle in forbidden:
        assert needle not in out
    for needle in must_contain:
        assert needle in out


@pytest.mark.parametrize(
    "raw",
    [
        "https://user:pass@",
        "https://[",
        "https://user:pass@[::1",
        "https://example.test:99999/x",
        "https://user:pass@exa mple.test/x",
        "http://:@example.test/",
    ],
)
def test_malformed_http_url_redaction_fail_closed(raw: str) -> None:
    out = redact(f"error at {raw} done")
    assert "user:pass" not in out
    assert ":pass@" not in out
    assert "pass@" not in out


def test_redact_never_raises_on_malformed_urls() -> None:
    blobs = [
        "https://",
        "https://@",
        "https://[",
        "https://user:pass@[::1",
        "https://example.test:notaport/x",
        "https://u:p@" + ("x" * 5000),
        "http://[::::::::]/",
        "HTTPS://USER:PASS@EXAMPLE.TEST/x?y=1#z",
    ]
    for blob in blobs:
        redact(blob)
    redact("postgresql://u:secret@host/db https://u:p@h/x")


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
        for svc in ("gateway", "api", "worker", "delivery", "postgres", "web"):
            rows.append(
                {
                    "Service": svc,
                    "State": "running",
                    "Health": "healthy",
                    "ID": f"id-{svc}",
                    "Image": f"fetchnow-api:{rev}"
                    if svc in {"api", "worker", "delivery"}
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
    monkeypatch.setattr(
        "fetchnow_release.health.compose_files_include_delivery", lambda _paths: True
    )

    def rows(_inp):  # noqa: ANN001
        out = []
        for svc in ("gateway", "api", "worker", "delivery", "postgres", "web"):
            image = {
                "api": f"fetchnow-api:{rev}",
                "worker": f"fetchnow-api:{rev}",
                "delivery": f"fetchnow-api:{rev}",
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
            "Image": (
                "sha256:shared"
                if svc in {"api", "worker", "delivery"}
                else f"sha256:{svc}"
            ),
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
    monkeypatch.setattr(
        "fetchnow_release.health.compose_files_include_delivery", lambda _paths: True
    )

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
                    "delivery": f"fetchnow-api:{rev}",
                    "web": f"fetchnow-web:{rev}",
                    "gateway": f"fetchnow-gateway:{rev}",
                    "postgres": "postgres:16.9-alpine",
                }[svc],
            }
            for svc in ("gateway", "api", "worker", "delivery", "postgres", "web")
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


def test_health_ignores_exited_storage_init_oneshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fetchnow_release.health import HealthError

    rev = "c" * 40
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  storage-init:\n    image: x\n", encoding="utf-8")
    monkeypatch.setattr("fetchnow_release.health.require_compose_v2", lambda: None)
    monkeypatch.setattr("fetchnow_release.health.require_docker_daemon", lambda: None)
    monkeypatch.setattr(
        "fetchnow_release.health.compose_files_include_delivery", lambda _paths: True
    )

    def rows(_inp):  # noqa: ANN001
        out = []
        for svc in ("gateway", "api", "worker", "delivery", "postgres", "web"):
            out.append(
                {
                    "Service": svc,
                    "State": "running",
                    "Health": "healthy",
                    "ID": f"cid-{svc}",
                    "Image": f"fetchnow-api:{rev}",
                }
            )
        out.append(
            {
                "Service": "storage-init",
                "State": "exited",
                "Health": "",
                "ID": "cid-storage-init",
                "Image": f"fetchnow-api:{rev}",
            }
        )
        return out

    def inspect(_cid: str) -> dict:
        raise HealthError("inspected-runtime")

    monkeypatch.setattr("fetchnow_release.health._ps_services", rows)
    monkeypatch.setattr("fetchnow_release.health._inspect", inspect)
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name="fetchnow-health-test-eeeeeeee",
            env_file=env,
            compose_files=(compose,),
            expected_revision=rev,
            repo_root=tmp_path,
        )
    )
    assert not result.ok
    assert "inspected-runtime" in result.messages[0]
    assert "unknown" not in result.messages[0]
    assert "oneshot" not in result.messages[0]


def test_health_fails_if_storage_init_still_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rev = "c" * 40
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  storage-init:\n    image: x\n", encoding="utf-8")
    monkeypatch.setattr("fetchnow_release.health.require_compose_v2", lambda: None)
    monkeypatch.setattr("fetchnow_release.health.require_docker_daemon", lambda: None)
    monkeypatch.setattr(
        "fetchnow_release.health.compose_files_include_delivery", lambda _paths: True
    )

    def rows(_inp):  # noqa: ANN001
        out = []
        for svc in ("gateway", "api", "worker", "delivery", "postgres", "web"):
            out.append(
                {
                    "Service": svc,
                    "State": "running",
                    "Health": "healthy",
                    "ID": f"cid-{svc}",
                    "Image": f"fetchnow-api:{rev}",
                }
            )
        out.append(
            {
                "Service": "storage-init",
                "State": "running",
                "Health": "",
                "ID": "cid-storage-init",
                "Image": f"fetchnow-api:{rev}",
            }
        )
        return out

    monkeypatch.setattr("fetchnow_release.health._ps_services", rows)
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name="fetchnow-health-test-ffffffff",
            env_file=env,
            compose_files=(compose,),
            expected_revision=rev,
            repo_root=tmp_path,
        )
    )
    assert not result.ok
    assert "still running" in result.messages[0]


def test_health_pr8_compose_does_not_require_storage_init(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fetchnow_release.health import HealthError

    rev = "c" * 40
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  api:\n    image: x\n", encoding="utf-8")
    monkeypatch.setattr("fetchnow_release.health.require_compose_v2", lambda: None)
    monkeypatch.setattr("fetchnow_release.health.require_docker_daemon", lambda: None)
    monkeypatch.setattr(
        "fetchnow_release.health.compose_files_include_delivery", lambda _paths: True
    )

    def rows(_inp):  # noqa: ANN001
        return [
            {
                "Service": svc,
                "State": "running",
                "Health": "healthy",
                "ID": f"cid-{svc}",
                "Image": f"fetchnow-api:{rev}",
            }
            for svc in ("gateway", "api", "worker", "delivery", "postgres", "web")
        ]

    def inspect(_cid: str) -> dict:
        raise HealthError("inspected-runtime")

    monkeypatch.setattr("fetchnow_release.health._ps_services", rows)
    monkeypatch.setattr("fetchnow_release.health._inspect", inspect)
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    result = run_health(
        HealthInput(
            project_name="fetchnow-health-test-pr8aaaaaa",
            env_file=env,
            compose_files=(compose,),
            expected_revision=rev,
            repo_root=tmp_path,
        )
    )
    assert not result.ok
    assert "inspected-runtime" in result.messages[0]
    assert "storage-init" not in result.messages[0]
    assert "missing service" not in result.messages[0]


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
