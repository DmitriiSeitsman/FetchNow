#!/usr/bin/env python3
"""Isolated health-gate integration for PRD1C1.

Uses unique project fetchnow-health-test-<suffix>. Builds revision-tagged
images from HEAD, starts an isolated stack on a unique loopback gateway port,
runs the health CLI (pass → break → fail → restore → pass), then cleans up.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.health import HealthInput, run_health  # noqa: E402
from fetchnow_release.revision import validate_full_sha  # noqa: E402

PROJECT_RE = re.compile(r"^fetchnow-health-test-[a-z0-9]{8,32}$")
FORBIDDEN = frozenset(
    {"", "fetchnow", "fetchnow-staging", "fetchnow-prod", "fetchnow-health-test"}
)


def assert_safe_project(name: str) -> str:
    if name in FORBIDDEN or not PROJECT_RE.fullmatch(name):
        raise SystemExit(f"refusing unsafe project name: {name!r}")
    return name


def make_project() -> str:
    run = re.sub(r"[^0-9]", "", os.environ.get("GITHUB_RUN_ID", ""))
    rand = secrets.token_hex(4)
    suffix = f"{run[-8:]}{rand}" if run else secrets.token_hex(6)
    if len(suffix) > 32:
        suffix = suffix[-32:]
    return assert_safe_project(f"fetchnow-health-test-{suffix}")


def run(
    cmd: list[str], *, cwd: Path = ROOT, check: bool = True
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}"
        )
    return proc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-file", type=Path, default=None)
    args = parser.parse_args(argv)

    project = make_project()
    if args.project_file:
        args.project_file.parent.mkdir(parents=True, exist_ok=True)
        args.project_file.write_text(project + "\n", encoding="utf-8")
        os.chmod(args.project_file, 0o600)

    rev = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    validate_full_sha(rev)

    # Unique loopback port outside 8080/8091.
    gateway_port = f"127.0.0.1:{19000 + (secrets.randbelow(1000))}"
    env_dir = Path(tempfile.mkdtemp(prefix="fetchnow-health-env-"))
    deploy_root = Path(tempfile.mkdtemp(prefix="fetchnow-health-deploy-"))
    backup_root = Path(tempfile.mkdtemp(prefix="fetchnow-health-backup-"))
    os.chmod(deploy_root, 0o700)
    os.chmod(backup_root, 0o700)
    env_path = env_dir / ".env.health-test"
    password = "health-test-url-safe-password-xxxxxxxx"
    env_path.write_text(
        "\n".join(
            [
                f"COMPOSE_PROJECT_NAME={project}",
                f"FETCHNOW_RELEASE_REVISION={rev}",
                f"GATEWAY_PORT={gateway_port}",
                "APP_ENV=test",
                "PUBLIC_SITE_URL=http://127.0.0.1",
                "POSTGRES_DB=fetchnow",
                "POSTGRES_USER=fetchnow",
                f"POSTGRES_PASSWORD={password}",
                f"FETCHNOW_DEPLOY_ROOT={deploy_root}",
                f"FETCHNOW_BACKUP_ROOT={backup_root}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)

    compose = [
        "docker",
        "compose",
        "--env-file",
        str(env_path),
        "--project-name",
        project,
        "-f",
        "compose.yaml",
        "-f",
        "compose.staging.yaml",
    ]
    rc = 1
    try:
        assert_safe_project(project)
        print(f"OK: health integration project {project} revision={rev}")

        print(
            "NOTE: Compose health integration does not run operator preflight "
            "(PR HEAD is not trusted main; dirty worktrees also cannot pass). "
            "Positive trusted-main preflight is covered by "
            "scripts/release_ancestry_integration_test.py. "
            "This is NOT a passed preflight."
        )

        print("Building revision-tagged application images…")
        run(compose + ["build", "api", "web", "gateway"])
        print("OK: images built")
        # Label proof
        inspect = run(
            [
                "docker",
                "image",
                "inspect",
                f"fetchnow-api:{rev}",
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            ]
        )
        if inspect.stdout.strip() != rev:
            raise RuntimeError(f"API OCI label mismatch: {inspect.stdout!r}")
        print("OK: tag/label inspection")

        run(compose + ["up", "-d"])
        base = f"http://{gateway_port}"
        health_input = HealthInput(
            project_name=project,
            env_file=env_path,
            compose_files=(
                (ROOT / "compose.yaml").resolve(),
                (ROOT / "compose.staging.yaml").resolve(),
            ),
            expected_revision=rev,
            repo_root=ROOT,
            gateway_base_url=base,
        )
        for attempt in range(1, 91):
            result = run_health(health_input)
            if result.ok:
                break
            detail = result.messages[0] if result.messages else "not ready"
            print(f"WAIT: health not ready (attempt {attempt}/90): {detail}")
            time.sleep(2)
        else:
            raise RuntimeError("initial health gate did not pass")
        print("OK: healthy stack / live/ready success")

        print("Stopping api to force unhealthy gateway dependency…")
        run(compose + ["stop", "api"])
        time.sleep(3)
        bad = run_health(health_input)
        if bad.ok:
            raise RuntimeError("health unexpectedly passed with api stopped")
        print(
            "EXPECTED: health gate failed with api stopped "
            f"({bad.messages[0] if bad.messages else 'FAIL'})"
        )
        print("OK: health failure after deliberate unhealthy service")

        run(compose + ["start", "api"])
        for attempt in range(1, 91):
            good = run_health(health_input)
            if good.ok:
                break
            detail = good.messages[0] if good.messages else "not ready"
            print(f"RETRY: waiting for recovery (attempt {attempt}/90): {detail}")
            time.sleep(2)
        else:
            raise RuntimeError("health did not recover")
        print("OK: recovery / health pass")

        print("OK: integration health verification passed")
        rc = 0
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rc = 1
    finally:
        assert_safe_project(project)
        run(compose + ["down", "-v", "--remove-orphans"], check=False)
        print(f"OK: isolated Compose project cleanup completed ({project})")
        shutil.rmtree(env_dir, ignore_errors=True)
        shutil.rmtree(deploy_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
