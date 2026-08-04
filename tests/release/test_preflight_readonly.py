"""Preflight read-only subprocess allowlist regression tests."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release import docker_checks, git_checks  # noqa: E402
from fetchnow_release.docker_checks import compose_config_json  # noqa: E402
from fetchnow_release.git_checks import GitCheckError, _run_git  # noqa: E402


@pytest.mark.parametrize(
    "args",
    [
        ("fetch", "origin"),
        ("checkout", "main"),
        ("reset", "--hard"),
        ("clean", "-fd"),
        ("update-ref", "refs/remotes/origin/main", "abc"),
        ("push", "origin", "HEAD"),
    ],
)
def test_git_run_rejects_mutating_verbs(tmp_path: Path, args: tuple[str, ...]) -> None:
    with pytest.raises(GitCheckError, match="non-read-only"):
        _run_git(tmp_path, *args)


def test_git_run_allows_read_verbs(tmp_path: Path) -> None:
    with mock.patch.object(git_checks.subprocess, "run") as run:
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        _run_git(tmp_path, "status", "--porcelain")
        _run_git(tmp_path, "cat-file", "-t", "a" * 40)
        _run_git(tmp_path, "rev-parse", "--verify", "refs/remotes/origin/main")
        _run_git(tmp_path, "merge-base", "--is-ancestor", "a" * 40, "b" * 40)
    assert run.call_count == 4
    for call in run.call_args_list:
        argv = call.args[0]
        assert argv[0] == "git"
        assert argv[1] in {"status", "cat-file", "rev-parse", "merge-base"}


def test_compose_config_json_is_config_only(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n")
    with mock.patch.object(docker_checks.subprocess, "run") as run:
        run.return_value = mock.Mock(
            returncode=0, stdout='{"services":{}}', stderr=""
        )
        compose_config_json(
            project_name="fetchnow-staging",
            env_file=env,
            compose_files=(compose,),
            repo_root=tmp_path,
        )
    argv = run.call_args.args[0]
    assert argv[:2] == ["docker", "compose"]
    assert "config" in argv
    assert argv[argv.index("--format") + 1] == "json"
    for banned in ("up", "down", "build", "pull", "run", "exec", "restart"):
        assert banned not in argv
