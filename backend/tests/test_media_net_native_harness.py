"""Offline regressions for the six native harness review findings.

These tests deliberately cannot establish native containment or Trivy PASS.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

from fetchnow.media_executor.argv import network_download_argv
from fetchnow.media_executor.constants import PROFILE_NETWORK, WORKER_UID
from fetchnow.media_executor.runner import ToolOutcome, ToolRunner
from fetchnow.media_executor.server import ExecutorApp

HELPERS = Path(__file__).parent / "media_executor"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HELPERS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree(*, state: str = "absent", heartbeat: int = 2) -> dict[str, object]:
    return {
        role: {
            "ready": True,
            "pid": pid,
            "starttime": 123,
            "same_identity": True,
            "state": state,
            "heartbeat": heartbeat,
        }
        for role, pid in (("parent", 100), ("child", 101))
    }


def test_overlay_has_explicit_dns_entrypoint_and_no_runtime_install(
    tmp_path: Path,
) -> None:
    harness = load("network_acceptance")
    overlay = tmp_path / "overlay.yaml"
    harness._write_disposable_overlay(overlay, helpers=HELPERS, tag="deadbeef")
    text = overlay.read_text()
    assert 'entrypoint: ["/usr/local/bin/python", "/helpers/mock_dns.py"]' in text
    assert "apt-get" not in text and "NET_ADMIN" not in text
    assert "read_only: false" not in text
    assert "|| true" not in text
    for image in ("media-egress-proxy", "media-net-executor", "media-executor"):
        assert f"image: fetchnow-{image}:deadbeef" in text
    assert text.count("cgroup_parent: fetchnow-sec09-net-deadbeef.slice") == 2
    assert "cgroup_parent: fetchnow-media-executor.slice" not in text


def test_disposable_slice_resolves_systemd_control_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = load("network_acceptance")
    units = tmp_path / "units"
    units.mkdir()
    cgroups = tmp_path / "cgroups"
    control = cgroups / "nested" / "proof.slice"
    control.mkdir(parents=True)
    (control / "memory.max").write_text(str(harness.PROOF_MEMORY_MAX))
    monkeypatch.setattr(harness, "UNIT_ROOT", units)
    monkeypatch.setattr(harness, "CGROUP_ROOT", cgroups)
    calls: list[list[str]] = []

    def run(argv, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, "/nested/proof.slice\n" if "show" in argv else "", ""
        )

    monkeypatch.setattr(harness, "run", run)
    state = {"name": "fetchnow-sec09-net-deadbeef"}
    proof = harness._prepare_slice(state, tmp_path)
    assert proof["control_group"] == "/nested/proof.slice"
    unit_name = "fetchnow-sec09-net-deadbeef.slice"
    assert state["slice"] == unit_name
    assert "TasksMax=256" in (units / unit_name).read_text()
    assert harness.cleanup(state) == []
    assert not (units / unit_name).exists()
    assert ["systemctl", "stop", unit_name] in calls


def test_disposable_slice_does_not_adopt_existing_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = load("network_acceptance")
    monkeypatch.setattr(harness, "UNIT_ROOT", tmp_path)
    unit = tmp_path / "fetchnow-sec09-net-deadbeef.slice"
    unit.write_text("existing owner")
    state = {"name": "fetchnow-sec09-net-deadbeef"}
    with pytest.raises(FileExistsError):
        harness._prepare_slice(state, tmp_path)
    assert "slice" not in state and unit.read_text() == "existing owner"


def test_slice_cleanup_rejects_unowned_name(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = load("network_acceptance")
    monkeypatch.setattr(
        harness, "run", lambda argv, **_k: subprocess.CompletedProcess(argv, 0, "", "")
    )
    with pytest.raises(ValueError, match="unsafe slice cleanup"):
        harness.cleanup(
            {"name": "fetchnow-sec09-net-deadbeef", "slice": "production.slice"}
        )


def test_active_overlay_is_used_for_restart_and_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = load("network_acceptance")
    monkeypatch.setattr(harness, "_ACTIVE_OVERLAY", "/tmp/proof.yaml")
    for action in ("start", "stop", "exec", "down"):
        argv = harness.compose_argv("fetchnow-sec09-deadbeef", action)
        assert argv.count("/tmp/proof.yaml") == 1
    assert "/explicit.yaml" in harness.compose_argv(
        "x", "exec", overlay="/explicit.yaml"
    )


def test_canary_uses_host_tool_in_validated_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = load("network_acceptance")
    calls: list[list[str]] = []

    def run(argv, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        stdout = "container-id\n" if "ps" in argv else "4242\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(harness, "run", run)
    monkeypatch.setattr(harness, "_connect_probe", lambda *_a: "HTTP/1.1 200 OK")
    harness._configure_canary("fetchnow-sec09-deadbeef")
    assert calls[-1] == [
        "nsenter",
        "--target",
        "4242",
        "--net",
        "ip",
        "addr",
        "add",
        "1.2.3.4/32",
        "dev",
        "lo",
    ]


@pytest.mark.parametrize("pid", ["0", "1", "", "4242\n1", "oops"])
def test_canary_refuses_invalid_pid(pid: str, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = load("network_acceptance")
    calls: list[list[str]] = []

    def run(argv, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "cid" if "ps" in argv else pid, "")

    monkeypatch.setattr(harness, "run", run)
    with pytest.raises(OSError, match="namespace pid"):
        harness._configure_canary("fetchnow-sec09-deadbeef")
    assert not any(argv[0] == "nsenter" for argv in calls)


def test_mock_interpreter_and_real_argv_select_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = load("mock_ytdlp")
    assert (
        (HELPERS / "mock_ytdlp.py").read_text().startswith("#!/opt/venv/bin/python\n")
    )
    seen: list[str] = []
    monkeypatch.setattr(mock, "_connect_via_proxy", lambda *_a: None)
    monkeypatch.setattr(
        mock, "_controlled_writer", lambda _p, *, mode: seen.append(mode)
    )
    argv = network_download_argv(
        executable="/opt/venv/bin/yt-dlp",
        url="https://mock.invalid/oversize",
        provider_id="vk",
        format_token="fmt_1",
        proxy_url="http://egress-proxy:8888",
        socket_timeout=5,
        cache_dir=Path("cache"),
        output_template="output-artifact.%(ext)s",
        max_filesize_bytes=65_536,
    )
    assert mock.main(argv[1:]) == 0
    assert seen == ["oversize"]


def test_tool_identity_does_not_require_proc_access() -> None:
    mock = load("mock_ytdlp")
    # The launcher intentionally denies /proc. starttime must be observed by
    # the worker-side probe before growth/cancel, never by the confined tool.
    assert mock._identity() == {"pid": os.getpid()}


def test_snapshot_script_parses_and_carries_expected_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = load("network_acceptance")
    scripts: list[str] = []

    def run(argv, **_kwargs):  # type: ignore[no-untyped-def]
        script = argv[-1]
        compile(script, "<fixture-snapshot>", "exec")
        scripts.append(script)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(harness, "run", run)
    harness._fixture_snapshot(
        "fetchnow-sec09-deadbeef",
        "33333333-3333-4333-8333-333333333333",
        4,
        expected=tree(state="S"),
    )
    assert "starttime" in scripts[0] and "baseline" in scripts[0]


def test_oversize_mock_ignores_cap_and_requires_executor_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = load("mock_ytdlp")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    # Unlike the old regression, do NOT start in the attempt directory.
    monkeypatch.chdir(tmp_path)
    (attempt / "fixture-child.json").write_text("{}")
    (attempt / "fixture-grow").touch()
    monkeypatch.setattr(mock, "_identity", lambda: {"pid": 1, "starttime": 2})
    calls: list[dict[str, object]] = []

    def popen(_argv, **kwargs):  # type: ignore[no-untyped-def]
        assert Path.cwd() == attempt
        calls.append(kwargs)
        return object()

    def heartbeat(_path):  # type: ignore[no-untyped-def]
        raise InterruptedError("executor must kill the infinite writer")

    monkeypatch.setattr(mock.subprocess, "Popen", popen)
    monkeypatch.setattr(mock, "_heartbeat", heartbeat)
    with pytest.raises(InterruptedError):
        mock._controlled_writer(attempt / "output-artifact.bin", mode="oversize")
    assert (attempt / "output-artifact.bin").stat().st_size == 1_048_576
    assert (attempt / "fixture-parent.json").is_file()
    assert not (tmp_path / "fixture-parent.json").exists()
    assert calls == [{"start_new_session": True}]


def test_rpc_diagnostics_preserve_stderr_without_urls_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = load("network_acceptance")
    monkeypatch.setattr(harness, "_EVIDENCE_OUTPUT", tmp_path)
    request = {
        "op": "download_progressive",
        "job_id": "fixture",
        "attempt": 1,
        "fence": 4,
        "url": "https://private.invalid/source?token=not-public",
    }
    stderr = (
        b"PermissionError: [Errno 13] denied\n"
        b"https://user:password@private.invalid/source?token=secret-value\n"
        b"token=hidden authorization=hidden bearer hidden github_pat_hidden\n"
    )
    harness._record_rpc(
        request,
        {
            "ok": False,
            "code": "failed",
            "exit_code": 1,
            "stdout_b64": base64.b64encode(b"private media metadata").decode(),
            "stderr_b64": base64.b64encode(stderr).decode(),
        },
    )
    text = (tmp_path / "rpc-diagnostics.jsonl").read_text()
    value = json.loads(text)
    assert "PermissionError" in value["response"]["stderr_excerpt"]
    assert value["response"]["stderr_bytes"] == len(stderr)
    assert len(value["response"]["stderr_sha256"]) == 64
    for secret in (
        "private.invalid",
        "password@",
        "secret-value",
        "hidden",
        "media metadata",
        "not-public",
    ):
        assert secret not in text
    assert (tmp_path / "rpc-diagnostics.jsonl").stat().st_mode & 0o777 == 0o644


def test_fixture_failure_saves_last_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = load("network_acceptance")
    monkeypatch.setattr(harness, "_EVIDENCE_OUTPUT", tmp_path)
    times = iter((0, 0, 16))
    monkeypatch.setattr(harness.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(harness.time, "sleep", lambda *_a: None)
    missing = {"parent": {"ready": False}, "child": {"ready": False}}
    monkeypatch.setattr(harness, "_fixture_snapshot", lambda *_a: missing)
    job = "33333333-3333-4333-8333-333333333333"
    with pytest.raises(OSError, match="fixture tree did not start"):
        harness._wait_fixture_started("project", job, 4)
    value = json.loads((tmp_path / f"fixture-start-{job}_4.json").read_text())
    assert value["last_snapshot"] == missing


def test_matrix_exception_preserves_partial_checks_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = load("network_acceptance")
    monkeypatch.setattr(harness.os, "geteuid", lambda: 0)
    monkeypatch.setattr(harness.platform, "system", lambda: "Linux")
    monkeypatch.setattr(harness.platform, "machine", lambda: "x86_64")
    info = {
        "Architecture": "amd64",
        "CgroupDriver": "systemd",
        "CgroupVersion": "2",
        "ServerVersion": "28.0.4",
    }
    monkeypatch.setattr(
        harness,
        "run",
        lambda argv, **_k: subprocess.CompletedProcess(argv, 0, json.dumps(info), ""),
    )
    monkeypatch.setattr(harness, "_prepare_slice", lambda *_a: {})
    monkeypatch.setattr(
        harness,
        "_build_images",
        lambda *_a: {
            "network_executor": "sha256:" + "a" * 64,
            "egress_proxy": "sha256:" + "b" * 64,
            "offline_executor": "sha256:" + "c" * 64,
        },
    )
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda argv, **_k: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(harness, "_configure_canary", lambda *_a: None)
    cleaned: list[bool] = []
    monkeypatch.setattr(harness, "cleanup", lambda *_a: cleaned.append(True) or [])

    def matrix(**kwargs):  # type: ignore[no-untyped-def]
        kwargs["checks"].update({"N1_inspect_ok": True, "N2_download_ok": False})
        raise OSError("fixture tree did not start")

    monkeypatch.setattr(harness, "_matrix", matrix)
    out = tmp_path / "proof"
    assert harness.execute(out, trivy_bin="pinned-trivy") == 1
    result = json.loads((out / "result.json").read_text())
    assert result["checks"]["N1_inspect_ok"] is True
    assert result["checks"]["N2_download_ok"] is False
    assert result["checks"]["N13_same_artifact_audit"] == "NOT_RUN"
    assert result["status"] == "FAIL" and cleaned == [True]
    assert harness._EVIDENCE_OUTPUT is None


@pytest.mark.parametrize(
    "mutation", ["ok", "malformed", "timeout", "cancel", "small", "zombie", "reuse"]
)
def test_n9_rejects_false_limit_proof(mutation: str) -> None:
    harness = load("network_acceptance")
    outcome = {
        "ok": False,
        "code": "failed",
        "exit_code": 1,
        "timed_out": False,
        "cancelled": False,
    }
    snapshot = {**tree(), "artifact_bytes": 1_048_576}
    assert harness._limit_proven(outcome, snapshot)
    if mutation == "ok":
        outcome["ok"] = True
    elif mutation == "malformed":
        outcome["code"] = "malformed"
    elif mutation == "timeout":
        outcome["timed_out"] = True
    elif mutation == "cancel":
        outcome["cancelled"] = True
    elif mutation == "small":
        snapshot["artifact_bytes"] = 64
    elif mutation == "zombie":
        snapshot["child"]["state"] = "Z"
    else:
        snapshot["child"]["same_identity"] = False
    assert not harness._limit_proven(outcome, snapshot)


@pytest.mark.parametrize(
    "mutation",
    ["reserved", "not_cancelled", "zombie", "neighbor_dead", "no_progress", "reuse"],
)
def test_n10_requires_actual_cancel_reaping_and_live_neighbor(mutation: str) -> None:
    harness = load("network_acceptance")
    cancel = {"code": "cancelling", "cancelled": True}
    outcome = {"cancelled": True, "timed_out": False}
    after = tree()
    before_b, after_b = tree(state="S"), tree(state="S", heartbeat=4)
    assert harness._cancel_proven(cancel, outcome, after, before_b, after_b)
    if mutation == "reserved":
        cancel = {"code": "not_running", "cancelled": False}
    elif mutation == "not_cancelled":
        outcome["cancelled"] = False
    elif mutation == "zombie":
        after["parent"]["state"] = "Z"
    elif mutation == "neighbor_dead":
        after_b["child"]["state"] = "absent"
    elif mutation == "no_progress":
        after_b["child"]["heartbeat"] = 2
    else:
        after_b["parent"]["pid"] = 999
    assert not harness._cancel_proven(cancel, outcome, after, before_b, after_b)


@pytest.mark.parametrize("mode", ["stdout", "blocking"])
@pytest.mark.parametrize("failure", ["none", "wrong_reason", "cancel", "zombie"])
def test_n9_time_and_stdout_limits_require_specific_outcome(
    mode: str,
    failure: str,
) -> None:
    harness = load("network_acceptance")
    outcome = {
        "ok": False,
        "code": "failed" if mode == "stdout" else "timed_out",
        "exit_code": 1,
        "cancelled": False,
        "timed_out": mode == "blocking",
        "stdout_b64": base64.b64encode(b"x" * 196_608).decode(),
        "stderr_b64": "",
    }
    snapshot = tree()
    if failure == "wrong_reason":
        outcome["code"] = "malformed"
    elif failure == "cancel":
        outcome["cancelled"] = True
    elif failure == "zombie":
        snapshot["child"]["state"] = "Z"
    assert harness._other_limit_proven(mode, outcome, snapshot) is (failure == "none")


def test_readiness_requires_worker_rpc_not_just_socket_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = load("network_acceptance")
    calls: list[list[str]] = []

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        assert kwargs["timeout"] == 5
        compile(argv[-1], "<readiness-probe>", "exec")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(harness, "run", run)
    harness._wait_socket("fetchnow-sec09-deadbeef", "media-net-executor", "/tmp/x.sock")
    assert "10001:10001" in calls[0]
    assert "not_running" in calls[0][-1] and "os.path.exists" not in calls[0][-1]


def test_n11_actual_worker_watchdog_cancels_on_lost_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = load("network_acceptance")
    monkeypatch.setitem(sys.modules, "network_acceptance", harness)
    proof = load("worker_fence_acceptance")
    running = threading.Event()
    done = threading.Event()

    class Runner(ToolRunner):
        def run(self, **kwargs):  # type: ignore[no-untyped-def]
            running.set()
            assert kwargs["cancel"].wait(5), "worker never cancelled operation"
            done.set()
            return ToolOutcome(-9, b"", b"", False, True)

    app = ExecutorApp(
        work_root=tmp_path,
        socket_dir=tmp_path / "socket",
        runner=Runner(),
        profile=PROFILE_NETWORK,
        ytdlp="/opt/venv/bin/yt-dlp",
        proxy_url="http://egress-proxy:8888",
    )

    def rpc(_project, payload):  # type: ignore[no-untyped-def]
        raw = json.dumps(payload).encode() + b"\n"
        return json.loads(app.handle(raw, peer_uid=WORKER_UID))

    def started(*_args):  # type: ignore[no-untyped-def]
        assert running.wait(5)
        return tree(state="S")

    monkeypatch.setattr(harness, "_net_rpc", rpc)
    monkeypatch.setattr(harness, "_wait_fixture_started", started)
    monkeypatch.setattr(
        harness,
        "_fixture_snapshot",
        lambda *_a, **_k: tree() if done.is_set() else tree(state="S"),
    )
    result = asyncio.run(proof.prove("fetchnow-sec09-deadbeef"))
    assert result["pass"] is True
    assert result["lease_lost_rejected"] is True
    assert result["database_ready_transaction"] == "NOT_RUN"


def test_n11_missing_worker_proof_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = load("network_acceptance")
    monkeypatch.setattr(
        harness, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "")
    )
    assert (
        harness._worker_fence_proof("fetchnow-sec09-deadbeef", tmp_path)["pass"]
        is False
    )
