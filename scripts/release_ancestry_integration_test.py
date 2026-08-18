#!/usr/bin/env python3
"""Isolated temporary-Git ancestry + positive preflight integration (PRD1C1).

Never mutates refs in the developer's real repository. Builds a disposable
Git graph, proves unmerged descendants are rejected, and runs the real
operator preflight against a temporary repo whose
refs/remotes/origin/main points at the tested commit.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests" / "release"))

from fetchnow_release.git_checks import (  # noqa: E402
    GitCheckError,
    assert_revision_contained_in_trusted_main,
)
from fetchnow_release.preflight import PreflightInput, run_preflight  # noqa: E402
from fetchnow_release.rollout_project import make_rollout_project_name  # noqa: E402
from git_graph_fixture import build_trusted_main_graph  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fetchnow-ancestry-"))
    deploy_root = Path(tempfile.mkdtemp(prefix="fetchnow-ancestry-deploy-"))
    backup_root = Path(tempfile.mkdtemp(prefix="fetchnow-ancestry-backup-"))
    env_dir = Path(tempfile.mkdtemp(prefix="fetchnow-ancestry-env-"))
    try:
        os.chmod(deploy_root, 0o700)
        os.chmod(backup_root, 0o700)
        g = build_trusted_main_graph(tmp)
        print(f"OK: temp git graph at {g.repo}")
        print(f"    main_tip={g.main_tip}")
        print(f"    feature_tip={g.feature_tip}")

        # Positive: tip contained in origin/main
        assert_revision_contained_in_trusted_main(g.repo, g.main_tip)
        print("OK: expected == origin/main accepted")

        # Positive: older main commit
        assert_revision_contained_in_trusted_main(g.repo, g.main_old)
        print("OK: older main commit accepted")

        # Negative: feature descendant
        try:
            assert_revision_contained_in_trusted_main(g.repo, g.feature_tip)
        except GitCheckError as exc:
            print(f"OK: feature descendant rejected ({exc})")
        else:
            raise RuntimeError("feature descendant was incorrectly accepted")

        # Negative: unrelated
        try:
            assert_revision_contained_in_trusted_main(g.repo, g.unrelated)
        except GitCheckError as exc:
            print(f"OK: unrelated commit rejected ({exc})")
        else:
            raise RuntimeError("unrelated commit was incorrectly accepted")

        # Positive end-to-end preflight against temporary repo only.
        # Compose files come from the real worktree as absolute paths; Git trust
        # checks use the temporary repository (never the developer's refs).
        project = make_rollout_project_name()
        env_path = env_dir / ".env.ancestry-test"
        password = "ancestry-test-url-safe-password-xxxx"
        env_path.write_text(
            "\n".join(
                [
                    f"COMPOSE_PROJECT_NAME={project}",
                    f"FETCHNOW_RELEASE_REVISION={g.main_tip}",
                    "GATEWAY_PORT=127.0.0.1:8091",
                    "PUBLIC_SITE_URL=https://staging.example.test",
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

        compose_files = (
            (ROOT / "compose.yaml").resolve(),
            (ROOT / "compose.staging.yaml").resolve(),
        )
        result = run_preflight(
            PreflightInput(
                project_name=project,
                env_file=env_path,
                compose_files=compose_files,
                expected_revision=g.main_tip,
                repo_root=g.repo,
                deploy_root=deploy_root,
                backup_root=backup_root,
            )
        )
        if not result.ok:
            raise RuntimeError(
                "positive temp-repo preflight failed: " + result.messages[0]
            )
        print("OK: positive preflight against temporary trusted main")

        # Negative: preflight with feature tip must fail closed.
        env_bad = env_dir / ".env.ancestry-test-feature"
        env_bad.write_text(
            env_path.read_text(encoding="utf-8").replace(g.main_tip, g.feature_tip),
            encoding="utf-8",
        )
        os.chmod(env_bad, 0o600)
        bad = run_preflight(
            PreflightInput(
                project_name=project,
                env_file=env_bad,
                compose_files=compose_files,
                expected_revision=g.feature_tip,
                repo_root=g.repo,
                deploy_root=deploy_root,
                backup_root=backup_root,
            )
        )
        if bad.ok:
            raise RuntimeError("preflight incorrectly accepted unmerged descendant")
        print(f"OK: preflight rejected feature descendant ({bad.messages[0]})")

        print("OK: ancestry integration passed")
        return 0
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(deploy_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)
        shutil.rmtree(env_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
