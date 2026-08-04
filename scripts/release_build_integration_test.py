#!/usr/bin/env python3
"""Isolated PRD1C2 release materialization/build integration.

Uses a clean temporary clone of the trusted revision (so a dirty developer
worktree cannot affect the result), a unique temporary deploy root, and a
test-only env. Does not start containers or touch shared volumes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests" / "release"))

from fetchnow_release.prepare import PrepareInput, prepare_release  # noqa: E402
from fetchnow_release.verify_release import verify_prepared_release  # noqa: E402
from password_fixture import valid_test_password  # noqa: E402


def run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd, cwd=str(cwd or ROOT), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}"
        )
    return proc


def main() -> int:
    rev = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    deploy = Path(tempfile.mkdtemp(prefix="fetchnow-release-deploy-"))
    clone = Path(tempfile.mkdtemp(prefix="fetchnow-release-clone-"))
    env_dir = Path(tempfile.mkdtemp(prefix="fetchnow-release-env-"))
    backup = Path(tempfile.mkdtemp(prefix="fetchnow-release-backup-"))
    os.chmod(deploy, 0o750)
    os.chmod(backup, 0o700)
    sentinel_name = "UNTRACKED_RELEASE_SENTINEL_DO_NOT_SHIP.txt"
    # Create sentinel in the *developer* worktree; must not appear in release.
    sentinel = ROOT / sentinel_name
    created_sentinel = False
    rc = 1
    try:
        if not sentinel.exists():
            sentinel.write_text("dirty-worktree-only\n", encoding="utf-8")
            created_sentinel = True

        # Clean clone of current HEAD (committed tree only).
        run(["git", "clone", "--local", "--no-hardlinks", str(ROOT), str(clone)])
        run(["git", "checkout", "--detach", rev], cwd=clone)
        # Ensure clone has origin/main pointing at trusted tip.
        run(["git", "update-ref", "refs/remotes/origin/main", rev], cwd=clone)
        # Prove sentinel is not in clone.
        if (clone / sentinel_name).exists():
            raise RuntimeError("sentinel leaked into clean clone")

        env_path = env_dir / ".env.release-test"
        env_path.write_text(
            "\n".join(
                [
                    "COMPOSE_PROJECT_NAME=fetchnow-staging",
                    f"FETCHNOW_RELEASE_REVISION={rev}",
                    "GATEWAY_PORT=127.0.0.1:8091",
                    "APP_ENV=test",
                    "PUBLIC_SITE_URL=https://staging.example.test",
                    "POSTGRES_DB=fetchnow",
                    "POSTGRES_USER=fetchnow",
                    f"POSTGRES_PASSWORD={valid_test_password()}",
                    f"FETCHNOW_DEPLOY_ROOT={deploy}",
                    f"FETCHNOW_BACKUP_ROOT={backup}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(env_path, 0o600)

        compose = (
            (clone / "compose.yaml").resolve(),
            (clone / "compose.staging.yaml").resolve(),
        )
        print(
            f"OK: preflight/prepare use clean temporary clone ({clone}); "
            "developer worktree dirtiness does not weaken production clean-worktree policy"
        )
        print(
            f"OK: dirty-worktree sentinel present only in developer tree "
            f"({sentinel_name}); absent from clone and must stay absent from archive"
        )
        first = prepare_release(
            PrepareInput(
                project_name="fetchnow-staging",
                env_file=env_path,
                compose_files=compose,
                expected_revision=rev,
                repo_root=clone,
                deploy_root=deploy,
            )
        )
        if not first.ok:
            raise RuntimeError(first.messages[0])
        print("\n".join(first.messages))
        if first.already_prepared:
            raise RuntimeError("first prepare unexpectedly idempotent")

        release_path = first.release_dir
        assert release_path is not None
        if (release_path / "source" / sentinel_name).exists():
            raise RuntimeError("dirty/untracked sentinel entered release source")
        print("OK: sentinel absent from materialized source")

        man = json.loads((release_path / "release.json").read_text(encoding="utf-8"))
        if man["revision"] != rev:
            raise RuntimeError("manifest revision mismatch")
        print("OK: manifest revision")

        for svc, ref in (
            ("api", f"fetchnow-api:{rev}"),
            ("web", f"fetchnow-web:{rev}"),
            ("gateway", f"fetchnow-gateway:{rev}"),
        ):
            label = run(
                [
                    "docker",
                    "image",
                    "inspect",
                    ref,
                    "--format",
                    '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                ]
            ).stdout.strip()
            if label != rev:
                raise RuntimeError(f"{svc} OCI label mismatch: {label!r}")
        print("OK: tags/labels match SHA")

        # Source non-writable
        sample = release_path / "source" / "compose.yaml"
        mode = sample.stat().st_mode
        if mode & 0o222:
            raise RuntimeError("source still writable")
        print("OK: source snapshot non-writable")

        api_id_before = run(
            [
                "docker",
                "image",
                "inspect",
                f"fetchnow-api:{rev}",
                "--format",
                "{{.Id}}",
            ]
        ).stdout.strip()

        second = prepare_release(
            PrepareInput(
                project_name="fetchnow-staging",
                env_file=env_path,
                compose_files=compose,
                expected_revision=rev,
                repo_root=clone,
                deploy_root=deploy,
            )
        )
        if not second.ok or not second.already_prepared:
            raise RuntimeError(f"second prepare not idempotent: {second.messages}")
        api_id_after = run(
            [
                "docker",
                "image",
                "inspect",
                f"fetchnow-api:{rev}",
                "--format",
                "{{.Id}}",
            ]
        ).stdout.strip()
        if api_id_before != api_id_after:
            raise RuntimeError("idempotent prepare mutated image ID")
        print("OK: idempotent second prepare (no image ID mutation)")

        verified = verify_prepared_release(
            release_path, expected_revision=rev, repo_root=clone
        )
        if not verified.ok:
            raise RuntimeError(verified.messages[0])
        print("OK: release verify passed")

        # Controlled drift in a *copy* of the release dir (do not mutate published).
        drift = Path(tempfile.mkdtemp(prefix="fetchnow-release-drift-"))
        try:
            shutil.copytree(release_path, drift / rev)
            man_path = drift / rev / "release.json"
            data = json.loads(man_path.read_text(encoding="utf-8"))
            data["images"][0]["image_id"] = "sha256:" + ("0" * 64)
            man_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            # Make copy writable for the edit already done; verify should fail on ID drift.
            bad = verify_prepared_release(
                drift / rev, expected_revision=rev, repo_root=clone
            )
            if bad.ok:
                raise RuntimeError("drift not detected")
            print(f"OK: drift detected ({bad.messages[0]})")
        finally:
            shutil.rmtree(drift, ignore_errors=True)

        # No compose project containers for build project.
        ps = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "name=fetchnow-release-build",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if ps.stdout.strip():
            raise RuntimeError(f"unexpected build containers: {ps.stdout!r}")
        print("OK: no release-build containers created")

        print("OK: release-build integration passed")
        rc = 0
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rc = 1
    finally:
        if created_sentinel and sentinel.exists():
            sentinel.unlink()
        shutil.rmtree(deploy, ignore_errors=True)
        shutil.rmtree(clone, ignore_errors=True)
        shutil.rmtree(env_dir, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
